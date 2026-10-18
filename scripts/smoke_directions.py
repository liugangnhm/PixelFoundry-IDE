"""Generate a 4/8-direction animation batch, offline or with AGNES_API_KEY.

Live mode uses quota for one view image and one video per direction, plus
the character master if no --reference is supplied. Outputs are gitignored.
"""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.api_config import APIConfig, PROVIDER_PRESETS
from core.api.factory import create_api_client
from core.workflow import SoloParams
from core.workflow.directional_workflow import FOUR_DIRECTIONS, EIGHT_DIRECTIONS, DirectionalWorkflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Use deterministic offline APIs")
    parser.add_argument("--directions", choices=[4, 8], type=int, default=4)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".tmp/directional-smoke"))
    args = parser.parse_args()
    if not args.mock and not os.environ.get("AGNES_API_KEY"):
        parser.error("Live mode requires AGNES_API_KEY in the process environment")
    clients = []
    try:
        for kind in ("llm", "image", "video"):
            cfg = APIConfig.defaults(kind)
            preset = next(p for p in PROVIDER_PRESETS[kind] if p["key"] == "agnes")
            cfg.base_url, cfg.model = preset["base_url"], preset["model"]
            cfg.api_key = os.environ.get("AGNES_API_KEY", "")
            cfg.params.update(preset["params"])
            cfg.params.update(mock=args.mock, max_retries=0, timeout=180, poll_interval=15)
            if args.mock and kind == "image":
                cfg.params["size"] = "256x256"
            clients.append(create_api_client(kind, cfg))
        params = SoloParams(
            description="An orange pixel-art cat with a black outline, no accessories, white background",
            action="idle, gently breathing in place", frame_count=16, fps=8,
            directions=FOUR_DIRECTIONS if args.directions == 4 else EIGHT_DIRECTIONS,
            reference_image=args.reference, pixel_size=64, max_colors=16,
            export_sprite=True, output_dir=args.output,
        )
        result = DirectionalWorkflow(
            *clients, params,
            progress=lambda step, total, name, pct, message: print(
                f"{step + 1}/{total} {name} {pct:.0%}", flush=True),
        ).run()
        print(f"Completed: {result.manifest_path}", flush=True)
        for direction, animation in result.animations.items():
            assert animation.gif_path.exists() and animation.frames_dir.exists()
            print(direction, animation.frame_count, animation.width, animation.height, flush=True)
        assert result.sprite_path.exists()
    finally:
        for client in clients:
            client.close()


if __name__ == "__main__":
    main()
