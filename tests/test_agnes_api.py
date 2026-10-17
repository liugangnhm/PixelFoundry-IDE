"""Agnes wire contracts, factory dispatch and settings persistence."""
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from config.api_config import APIConfig, APIConfigManager, PROVIDER_PRESETS
from core.api.agnes_api import AgnesImageAPI, AgnesVideoAPI
from core.api.factory import create_api_client
from core.api.image_api import ImageAPI
from core.storage.keyring import Keyring


def config(kind):
    preset = next(p for p in PROVIDER_PRESETS[kind] if p["key"] == "agnes")
    cfg = APIConfig.defaults(kind)
    cfg.base_url = preset["base_url"]
    cfg.model = preset["model"]
    cfg.api_key = "test-key"
    cfg.params.update(preset["params"])
    cfg.params.update(max_retries=0, poll_interval=0, max_polls=3)
    return cfg


def png():
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), "orange").save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("kind,typename", [("image", "AgnesImageAPI"), ("video", "AgnesVideoAPI"), ("llm", "LLMAPI")])
def test_factory_dispatch(kind, typename):
    api = create_api_client(kind, config(kind))
    assert type(api).__name__ == typename
    api.close()


@pytest.mark.parametrize("image_input", [False, True])
@pytest.mark.parametrize("fmt", ["b64_json", "url"])
def test_image_contract(image_input, fmt):
    cfg = config("image")
    cfg.params["response_format"] = fmt
    image = png()

    def handler(request):
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["prompt"] == 'orange "cat"\nwhite background'
        assert body["size"] == "1024x1024"
        assert not {"response_format", "image", "steps", "n"} & body.keys()
        if image_input:
            assert body["extra_body"]["image"] == ["data:image/png;base64," + base64.b64encode(image).decode()]
            assert body["extra_body"]["response_format"] == fmt
        elif fmt == "b64_json":
            assert body["return_base64"] is True
        else:
            assert body["extra_body"] == {"response_format": "url"}
        item = {"b64_json": base64.b64encode(image).decode()} if fmt == "b64_json" else {"url": "https://assets.example/cat.png"}
        return httpx.Response(200, json={"data": [item]})

    api = AgnesImageAPI(cfg, transport=httpx.MockTransport(handler))
    try:
        result = api.call('orange "cat"\nwhite background', image=image if image_input else None)
        assert result.ok, result.error
        assert result.data["images"] == ([image] if fmt == "b64_json" else [])
        assert result.data["urls"] == (["https://assets.example/cat.png"] if fmt == "url" else [])
    finally:
        api.close()


@pytest.mark.parametrize("terminal", ["completed", "failed", "missing_url", "timeout"])
def test_video_submit_poll_contract(terminal):
    cfg = config("video")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-key"
        if request.method == "POST":
            assert request.url.path == "/v1/videos"
            body = json.loads(request.content)
            assert body["seconds"] == "4"
            assert body["mode"] == "keyframe"
            assert body["size"] == "720P"
            assert body["first_frame"].startswith("data:image/png;base64,")
            assert not {"fps", "frame_num", "num_frames", "image", "duration"} & body.keys()
            return httpx.Response(200, json={"id": "task-other", "video_id": "video/a+b", "status": "queued"})
        assert request.url.path == "/agnesapi"
        assert request.url.params["video_id"] == "video/a+b"
        assert request.url.params["model_name"] == cfg.model
        # An early URL and stale internal_status must not stop polling.
        if len(calls) == 2 or terminal == "timeout":
            return httpx.Response(200, json={"status": "in_progress", "url": "https://assets.example/early.mp4"})
        if terminal == "failed":
            return httpx.Response(200, json={"status": "failed", "error": {"message": "Invalid reference media"}})
        return httpx.Response(200, json={"status": "completed", "internal_status": "pending",
                                       "url": None if terminal == "missing_url" else "https://assets.example/cat.mp4"})

    api = AgnesVideoAPI(cfg, transport=httpx.MockTransport(handler))
    try:
        result = api.call(png(), "rotate", frames=8, fps=8)
        assert result.ok == (terminal == "completed")
        if result.ok:
            assert result.data["job_id"] == "video/a+b"
            assert result.data["video_url"] == "https://assets.example/cat.mp4"
        elif terminal == "failed":
            assert "Invalid reference media" in result.error
    finally:
        api.close()


def test_video_modes_duration_size_and_preview():
    cfg = config("video")
    api = AgnesVideoAPI(cfg)
    api.params["last_frame"] = True
    body = api.preview_request(png(), "spin", duration=20)
    assert body["headers"]["Authorization"] == "Bearer ***"
    assert body["body"]["seconds"] == "12"
    assert body["body"]["first_frame"] == body["body"]["last_frame"]
    api.params["mode"] = "reference"
    assert "images" in api.preview_request(png(), "spin")["body"]
    api.params["mode"] = "text"
    assert "first_frame" not in api.preview_request(b"", "spin")["body"]
    api.params["resolution"] = "1080P"
    result = api.call(png(), "spin")
    assert not result.ok and "720P" in result.error
    api.close()


def test_video_rate_limit_backoff(monkeypatch):
    cfg = config("video")
    cfg.params["poll_interval"] = 5
    requests = []
    sleeps = []
    monkeypatch.setattr("core.api.agnes_api.time.sleep", sleeps.append)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "23"}, json={"error": "too many status queries"})
        if len(requests) == 2:
            return httpx.Response(200, json={"status": "in_progress"})
        return httpx.Response(200, json={"status": "completed", "url": "https://assets.example/cat.mp4"})

    api = AgnesVideoAPI(cfg, transport=httpx.MockTransport(handler))
    api.max_retries = 2
    result = api._poll("video-id")
    assert result.ok
    assert sleeps == [23, 23]
    assert api.max_retries == 2
    api.close()


@pytest.mark.parametrize("kind", ["llm", "image", "video"])
def test_settings_preset_roundtrip(qtbot, tmp_path, monkeypatch, kind):
    from ui.widgets.api_config_widget import ApiConfigWidget

    manager = APIConfigManager(tmp_path / "apis.json", Keyring(tmp_path / "keyring"))
    manager.add(APIConfig.defaults(kind))
    widget = ApiConfigWidget(manager, kind)
    qtbot.addWidget(widget)
    # Selecting a preset with a key normally opens a model picker; isolate it.
    monkeypatch.setattr(widget, "_on_list_models", lambda: None)
    widget._set_field("api_key", "test-key")
    widget._set_field("mock", True)
    widget._preset_combo.setCurrentIndex(widget._preset_combo.findData("agnes"))
    cfg = widget._collect_config()
    assert cfg.model == config(kind).model
    assert cfg.api_key == "test-key"
    assert cfg.params["mock"] is False
    manager.update(cfg)
    loaded = APIConfigManager(manager.config_file, manager.keyring).get_default(kind)
    assert loaded.model == cfg.model
    assert loaded.api_key == "test-key"
    assert loaded.params == cfg.params
    assert "test-key" not in manager.config_file.read_text(encoding="utf-8")
    if kind == "image":
        widget._preset_combo.setCurrentIndex(widget._preset_combo.findData("openai"))
        api = create_api_client(kind, widget._collect_config())
        assert type(api) is ImageAPI
        api.close()
