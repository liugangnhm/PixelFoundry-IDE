"""One character and action, rendered as separate directional animations.

All direction views are conditioned on the same master image, never on a
previously generated view. Each view then becomes the exact animation input.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from PIL import Image

from core.processing import frame_utils as fu, pixelizer as px
from .solo_workflow import SoloParams, SoloResult, SoloWorkflow, WorkflowCancelled, WorkflowError


# Clockwise when viewed on screen. South faces the viewer; north faces away.
DIRECTIONS = {
    "south": ("正面（向下）", "front view, facing the viewer / screen down"),
    "southwest": ("左前（左下）", "front-left three-quarter view, facing screen down-left"),
    "west": ("左侧（向左）", "left-facing side profile, facing screen left"),
    "northwest": ("左后（左上）", "rear-left three-quarter view, facing screen up-left"),
    "north": ("背面（向上）", "rear view, back toward the viewer / facing screen up"),
    "northeast": ("右后（右上）", "rear-right three-quarter view, facing screen up-right"),
    "east": ("右侧（向右）", "right-facing side profile, facing screen right"),
    "southeast": ("右前（右下）", "front-right three-quarter view, facing screen down-right"),
}
FOUR_DIRECTIONS = ("south", "west", "north", "east")
EIGHT_DIRECTIONS = tuple(DIRECTIONS)

IDENTITY_RULE = (
    "Use the supplied master image as the sole character identity reference. "
    "Preserve the SAME character's face, body proportions, silhouette, clothing, "
    "equipment, colors and pixel-art style. Do not redesign or invent accessories. "
    "Render the actual requested viewpoint; do not substitute a mirrored image "
    "(keep asymmetric equipment on its anatomical side). Keep the same camera "
    "elevation, full-body scale and centered framing with feet on the same baseline. "
)


@dataclass
class DirectionalResult:
    output_dir: Path
    master_image: Path
    animations: dict[str, SoloResult] = field(default_factory=dict)
    manifest_path: Path | None = None
    sprite_path: Path | None = None


class _PreparedAnimation(SoloWorkflow):
    def __init__(self, *args, prepared_prompts, **kwargs):
        super().__init__(*args, **kwargs)
        self._prepared_prompts = prepared_prompts

    def _step_prompts(self):
        prompts = dict(self._prepared_prompts)
        self._notify_prompts(prompts)
        return prompts, {}  # The batch has already resolved timing once.

    def _step_pixelize(self, frames, pix_params):
        if not self.params.pixelate:
            return [px.resize_nearest(f, self.params.target_size()) for f in frames], None
        return super()._step_pixelize(frames, pix_params)


class DirectionalWorkflow(SoloWorkflow):
    """Reuse the Solo processing/export pipeline for each independent view."""

    def run(self) -> DirectionalResult:
        p = replace(self.params)
        directions = tuple(p.directions)
        if not directions or len(set(directions)) != len(directions) or any(d not in DIRECTIONS for d in directions):
            raise WorkflowError("请选择有效且不重复的动画方向", step="方向设置")
        out = Path(p.output_dir)
        artifacts = out / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        result = DirectionalResult(out, artifacts / "character_master.png", manifest_path=out / "animations.json")
        manifest = {
            "version": 1, "status": "running", "action": p.action, "description": p.description,
            "master_image": "artifacts/character_master.png", "direction_order": list(directions),
            "animations": {}, "sprite_sheet": None,
        }

        def save_manifest():
            result.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        def relative(path):
            return Path(path).relative_to(out).as_posix() if path else None

        total = 1 + 7 * len(directions)
        save_manifest()
        try:
            self._check_cancel()
            self._report(0, total, "角色母图", 0, "准备共享角色参考")
            # Use an uploaded character directly; do not reinterpret its identity.
            master_params = replace(p, directions=(), initial_frame=p.initial_frame or p.reference_image)
            master = SoloWorkflow(self.llm_api, self.image_api, self.video_api, master_params,
                                  cancel=self._cancel, log=self._log, on_prompts=self._on_prompts_cb)
            prompts, rec = master._step_prompts()
            master._apply_llm_params(rec)
            p.frame_count, p.fps = master_params.frame_count, master_params.fps
            self._check_cancel()
            raw, _, _ = master._step_first_frame(prompts["image_prompt"], artifacts)
            result.master_image.write_bytes(raw)
            image = fu.bytes_to_image(raw)
            palette = px.extract_dominant_palette(image, p.max_colors) if p.pixelate else None
            manifest["palette"] = palette
            manifest["frame_size"] = list(p.target_size())
            manifest["requested_frame_count"] = p.frame_count
            self._report(0, total, "角色母图", 1, str(result.master_image))
            for i, direction in enumerate(directions):
                self._check_cancel()
                label, view = DIRECTIONS[direction]
                self._log_msg("info", f"生成方向 {i + 1}/{len(directions)}: {label}")
                offset = 1 + i * 7
                self._report(offset, total, label, 0, "生成方向首帧")
                view_prompt = (
                    IDENTITY_RULE + f"Requested view: {view}. Generate ONE still character, not a grid. "
                    + prompts["image_prompt"]
                    + f" FINAL VIEW OVERRIDE: {view}; the view overrides any conflicting pose or camera in the description."
                )
                w, h = master._api_image_size(view_prompt)
                response = self.image_api.call(prompt=view_prompt, size=f"{w}x{h}", n=1, image=raw)
                if not response.ok:
                    raise WorkflowError(f"{label}首帧生成失败: {response.message}", step=label)
                images, urls = response.data.get("images") or [], response.data.get("urls") or []
                if not images and not urls:
                    raise WorkflowError(f"{label}未返回方向首帧", step=label)
                view_image = fu.bytes_to_image(images[0] if images else fu.download_bytes(urls[0]))
                view_path = out / "directions" / direction / "artifacts" / "view.png"
                fu.save_image(view_image, view_path)
                self._check_cancel()
                self._report(offset, total, label, 1, str(view_path))
                view_prompts = dict(prompts)
                view_prompts["image_prompt"] = view_prompt
                view_prompts["animation_prompt"] = (
                    prompts["animation_prompt"] + f" Keep the character facing {view} throughout. "
                    "Animate in place. Preserve appearance and equipment; no turning toward another view, no camera rotation."
                )
                params = replace(p, directions=(), reference_image=None, initial_frame=view_path,
                                 fixed_palette=palette, output_dir=out / "directions" / direction)
                def progress(step, _total, name, pct, message, start=offset + 1, title=label):
                    self._report(start + step, total, f"{title} · {name}", pct, message)

                workflow = _PreparedAnimation(
                    self.llm_api, self.image_api, self.video_api, params, prepared_prompts=view_prompts,
                    progress=progress, log=self._log, cancel=self._cancel,
                    on_prompts=self._on_prompts_cb, on_first_frame=self._on_first_frame_cb,
                )
                animation = workflow.run()
                result.animations[direction] = animation
                manifest["animations"][direction] = {
                    "view": view, "fps": animation.fps, "frame_count": animation.frame_count,
                    "first_frame": relative(animation.first_frame), "frames": relative(animation.frames_dir),
                    "gif": relative(animation.gif_path), "apng": relative(animation.apng_path),
                    "sprite_sheet": relative(animation.sprite_path), "project": relative(animation.project_file),
                }
                save_manifest()  # Completed directions survive later errors/cancellation.
            self._check_cancel()
            if p.export_sprite:
                self._combined_sheet(result, manifest)
            manifest["status"] = "completed"
            save_manifest()
        except Exception as exc:
            manifest["status"] = "cancelled" if isinstance(exc, WorkflowCancelled) else "failed"
            # Keep metadata useful without persisting service error text or credentials.
            save_manifest()
            raise
        return result

    def _combined_sheet(self, result, manifest):
        counts = [r.frame_count for r in result.animations.values()]
        columns = max(counts)
        width, height = self.params.target_size()
        sheet = Image.new("RGBA", (columns * width, len(counts) * height))
        for row, (direction, animation) in enumerate(result.animations.items()):
            self._check_cancel()
            frames = sorted(Path(animation.frames_dir).glob("*.png"))
            for column, path in enumerate(frames):
                frame = fu.load_image(path).convert("RGBA")
                sheet.paste(frame, (column * width, row * height))
            manifest["animations"][direction]["row"] = row
        result.sprite_path = fu.save_image(sheet, result.output_dir / "export" / "directional_sprite_sheet.png")
        manifest["sprite_sheet"] = {
            "path": result.sprite_path.relative_to(result.output_dir).as_posix(),
            "columns": columns, "rows": len(counts), "cell_size": [width, height],
            "layout": "one direction per row; transparent padding after the last frame",
        }
