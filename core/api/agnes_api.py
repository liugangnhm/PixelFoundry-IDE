"""Agnes AI image and video contracts (https://agnes-ai.com/en/docs/).

Chat uses the standard LLMAPI. Images use extra_body; videos use the
Videos API and retrieve by video_id + model_name, rather than task_id.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from .base import APIResult
from .image_api import ImageAPI
from .video_api import VideoAPI


class AgnesImageAPI(ImageAPI):
    def call(self, prompt, size=None, n=1, seed=None, steps=None,
             negative_prompt=None, image=None) -> APIResult:
        if self.custom_enabled():
            return super().call(prompt, size, n, seed, steps, negative_prompt, image)
        error = self._validate_config()
        if error:
            return APIResult(ok=False, error=error)
        if int(n) != 1:
            return APIResult(ok=False, error="Agnes AI: generate one image per request (n=1).")
        fmt = self.params.get("response_format") or "b64_json"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": size or self.params.get("size") or "1024x1024",
        }
        if image:
            payload["extra_body"] = {
                "image": [self._to_data_uri(image)],
                "response_format": fmt,
            }
        elif fmt == "b64_json":
            payload["return_base64"] = True
        else:
            payload["extra_body"] = {"response_format": "url"}
        try:
            data = self._post_json(self._generations_url(), payload)
        except Exception as exc:  # noqa: BLE001
            return APIResult(ok=False, error=self._friendly_error(exc))
        images, urls = self._extract_images(data)
        if not images and not urls:
            return APIResult(ok=False, error="Agnes AI: response contains no image.", raw=data)
        return APIResult(ok=True, data={"images": images, "urls": urls}, raw=data)


class AgnesVideoAPI(VideoAPI):
    def _submit_url(self) -> str:
        if self.params.get("submit_url"):
            return super()._submit_url()
        return f"{self.base_url}/videos"

    def _poll_url(self, job_id: str) -> str:
        if self.params.get("poll_url"):
            return super()._poll_url(job_id)
        query = urlencode({"video_id": job_id, "model_name": self.model})
        return f"{self._task_base()}/agnesapi?{query}"

    def _find_job_id(self, data):
        # id and task_id need not be the video_id accepted by retrieval.
        return data.get("video_id") or super()._find_job_id(data)

    def _find_video_url(self, data):
        status = str(data.get("status") or "").lower()
        if status not in self._status_set("status_success", ["completed", "succeeded", "success", "done", "finished"]):
            return None
        return super()._find_video_url(data)

    def _build_payload(self, image_bytes, prompt, frames, fps, duration):
        if self.params.get("payload_template"):
            return super()._build_payload(image_bytes, prompt, frames, fps, duration)
        seconds = float(duration) if duration is not None else (
            int(frames if frames is not None else self.params.get("frames", 8))
            / max(1, int(fps if fps is not None else self.params.get("fps", 8)))
        )
        mode = self.params.get("mode") or "keyframe"
        resolution = str(self.params.get("resolution") or "720P").upper()
        if mode not in {"text", "keyframe", "reference"}:
            raise ValueError("Agnes AI: mode must be text, keyframe or reference.")
        allowed_sizes = {"720P"} if self.model.endswith("-flash") else {"720P", "1080P", "1K", "2K"}
        if resolution not in allowed_sizes:
            raise ValueError(f"Agnes AI: {self.model} supports size {', '.join(sorted(allowed_sizes))}.")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "mode": mode,
            "seconds": str(max(4, min(12, round(seconds)))),
            "size": resolution,
            "aspect_ratio": self.params.get("ratio") or "1:1",
            "n": 1,
        }
        if mode != "text":
            # Local first frames use Data URIs. image_url can override this for
            # gateways that require a public URL; no third-party upload occurs.
            first_frame = self.params.get("image_url") or (
                self._to_data_uri(image_bytes) if image_bytes else ""
            )
            if not first_frame:
                raise ValueError("Agnes AI: an input image is required for keyframe/reference mode.")
            if mode == "keyframe":
                payload["first_frame"] = first_frame
                if self._last_frame_enabled():
                    payload["last_frame"] = first_frame
            else:
                payload["images"] = [first_frame]
        seed = int(self.params.get("seed", -1))
        if seed >= 0:
            payload["seed"] = seed
        extra = self.params.get("extra_payload")
        if extra:
            payload.update(json.loads(extra) if isinstance(extra, str) else extra)
        return payload

    def call(self, image_bytes, prompt, frames=None, fps=None, duration=None):
        try:
            return super().call(image_bytes, prompt, frames, fps, duration)
        except (ValueError, TypeError) as exc:
            return APIResult(ok=False, error=str(exc))

    def _poll(self, job_id):
        interval = max(0, float(self.params.get("poll_interval", 5)))
        max_polls = int(self.params.get("max_polls", 180))
        delay = interval
        retries = self.max_retries
        # Polling itself retries with backoff; avoid rapid retries inside
        # BaseAPI when the status-query endpoint rate limits a request.
        self.max_retries = 0
        try:
            for attempt in range(max_polls):
                try:
                    data = self._send_json(self._poll_method(), self._poll_url(job_id), self._poll_payload(job_id))
                except Exception as exc:  # noqa: BLE001
                    response = getattr(exc.__cause__, "response", None)
                    status = getattr(response, "status_code", None)
                    if status in {400, 401, 403, 404}:
                        return APIResult(ok=False, error=self._friendly_error(exc))
                    delay = max(interval, min(60, max(1, delay) * 2))
                    if response is not None:
                        try:
                            delay = max(delay, float(response.headers.get("Retry-After", 0)))
                        except ValueError:
                            pass
                else:
                    status = str(data.get("status") or "").lower()
                    if status in self._status_set("status_failure", ["failed", "error", "cancelled", "canceled"]):
                        return APIResult(ok=False, error=f"Agnes AI: video task failed: {data.get('error')}", raw=data)
                    url = self._find_video_url(data)
                    if url:
                        return APIResult(ok=True, data={"video_url": url, "frames": []}, raw=data)
                    if status in self._status_set("status_success", ["completed", "succeeded", "success", "done", "finished"]):
                        return APIResult(ok=False, error="Agnes AI: completed task contains no video URL.", raw=data)
                    # Keep the slower pace after a rate limit for this task.
                if attempt < max_polls - 1:
                    time.sleep(delay)
        finally:
            self.max_retries = retries
        return APIResult(ok=False, error=f"Agnes AI: video polling timed out after {max_polls} queries (video_id={job_id}).")
