"""Opt-in real Agnes API smoke test. Requires AGNES_API_KEY; may use quota.

Run from the repository root: python scripts/smoke_agnes.py [--video]
Outputs stay in .tmp/agnes-smoke (gitignored). No API keys are saved.
"""
import argparse
import io
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image

from config.api_config import APIConfig, PROVIDER_PRESETS
from core.api.factory import create_api_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", action="store_true", help="Also generate a four-second keyframe video")
    parser.add_argument("--video-id", help="Poll an existing video instead of submitting another")
    args = parser.parse_args()
    key = os.environ.get("AGNES_API_KEY")
    if not key:
        parser.error("Set AGNES_API_KEY in the process environment first.")
    output = Path(".tmp/agnes-smoke")
    output.mkdir(parents=True, exist_ok=True)
    report = {}

    def client(kind):
        preset = next(p for p in PROVIDER_PRESETS[kind] if p["key"] == "agnes")
        cfg = APIConfig.defaults(kind)
        cfg.base_url, cfg.model, cfg.api_key = preset["base_url"], preset["model"], key
        cfg.params.update(preset["params"])
        cfg.params.update(max_retries=0, timeout=180)
        return create_api_client(kind, cfg)

    def record(name, result):
        report[name] = {"ok": result.ok}
        if not result.ok:
            report[name]["error"] = str(result.error).replace(key, "***")
        print(name, json.dumps(report[name]), flush=True)
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if not result.ok:
            raise RuntimeError(f"{name} failed; see {output / 'report.json'}")

    llm = client("llm")
    try:
        result = llm.call("Reply only with AGNES_OK", max_tokens=32)
        record("chat", result)
        assert "AGNES_OK" in result.data
    finally:
        llm.close()
    image = client("image")
    try:
        result = image.call("One small orange pixel art cat, side view, centered, white background, no text.")
        record("text_to_image", result)

        def save_image(result, name):
            raw = result.data["images"][0] if result.data["images"] else httpx.get(
                result.data["urls"][0], timeout=120, follow_redirects=True
            ).raise_for_status().content
            with Image.open(io.BytesIO(raw)) as im:
                im.save(output / name)
            return raw

        raw = save_image(result, "cat.png")
        result = image.call("Make the cat blue, preserve its pose and white background.", image=raw)
        record("image_to_image", result)
        save_image(result, "cat-blue.png")
    finally:
        image.close()
    if args.video or args.video_id:
        video = client("video")
        try:
            if args.video_id:
                result = video._poll(args.video_id)
            else:
                # Same nearest-neighbour resizing used for local first frames.
                with Image.open(io.BytesIO(raw)) as im:
                    im = im.convert("RGB")
                    im.thumbnail((512, 512), Image.Resampling.NEAREST)
                    buf = io.BytesIO()
                    im.save(buf, "PNG")
                result = video.call(buf.getvalue(), "The cat gently waves, fixed camera, white background, pixel art.", duration=4)
            record("image_to_video", result)
            response = httpx.get(result.data["video_url"], timeout=120, follow_redirects=True)
            response.raise_for_status()
            (output / "animation.mp4").write_bytes(response.content)
            from core.processing.frame_utils import extract_video_frames
            frames = extract_video_frames(output / "animation.mp4", max_frames=8)
            assert frames, "Downloaded video contains no decodable frames"
            report["image_to_video"]["decoded_frames"] = len(frames)
            import imageio.v2 as imageio
            with imageio.get_reader(output / "animation.mp4", format="ffmpeg") as reader:
                meta = reader.get_meta_data()
                report["image_to_video"].update(
                    duration=meta.get("duration"), fps=meta.get("fps"),
                    size=meta.get("size"), total_frames=sum(1 for _ in reader),
                )
        finally:
            video.close()
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
