"""Direction batches: shared identity input, independent outputs and UI selection."""
import json
import threading
from dataclasses import replace

import pytest
from PIL import Image

from core.api.base import APIResult
from core.api.mock_clients import MockImageAPI, MockLLMAPI, MockVideoAPI
from core.processing import frame_utils as fu
from core.workflow import SoloParams, SoloResult, WorkflowCancelled, WorkflowError
from core.workflow.directional_workflow import (
    DIRECTIONS, FOUR_DIRECTIONS, EIGHT_DIRECTIONS, DirectionalResult, DirectionalWorkflow,
)


class RecordingImages(MockImageAPI):
    def __init__(self):
        super().__init__({"params": {"size": "64x64"}})
        self.calls = []

    def call(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return super().call(prompt, **kwargs)


def params(tmp_path, **kwargs):
    return SoloParams(description="an orange cat with a sword in its right hand", action="walk",
                      output_dir=tmp_path, directions=FOUR_DIRECTIONS, pixel_size=32,
                      frame_count=4, fps=8, pixelate=False, force_pure_bg=False,
                      remove_bg=False, video_image_min_side=0, video_image_max_side=0,
                      export_sprite=True, **kwargs)


@pytest.mark.parametrize("directions", [FOUR_DIRECTIONS, EIGHT_DIRECTIONS])
def test_batch_reuses_master_for_every_direction_and_exports(tmp_path, directions):
    image = RecordingImages()
    p = replace(params(tmp_path), directions=directions)
    progress = []
    result = DirectionalWorkflow(MockLLMAPI(), image, MockVideoAPI(), p,
                                 progress=lambda *event: progress.append(event)).run()
    assert tuple(result.animations) == directions
    assert len(image.calls) == 1 + len(directions)  # One master, one view per direction; no extra first-frame generation.
    reference = result.master_image.read_bytes()
    for (prompt, kwargs), direction in zip(image.calls[1:], directions):
        assert kwargs["image"] == reference
        assert DIRECTIONS[direction][1] in prompt
        assert "anatomical side" in prompt  # No mirroring asymmetric equipment.
        animation = result.animations[direction]
        assert animation.gif_path.exists() and animation.project_file.exists()
        assert animation.width == 32 and animation.height == 32
        assert "no turning" in animation.prompts["animation_prompt"]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["direction_order"] == list(directions)
    sheet = fu.load_image(result.sprite_path)
    assert sheet.size == (manifest["sprite_sheet"]["columns"] * 32, len(directions) * 32)
    for row, direction in enumerate(directions):
        assert manifest["animations"][direction]["row"] == row
        assert (tmp_path / manifest["animations"][direction]["first_frame"]).exists()
        first = fu.load_image(sorted(result.animations[direction].frames_dir.glob("*.png"))[0]).convert("RGBA")
        assert sheet.crop((0, row * 32, 32, (row + 1) * 32)).tobytes() == first.tobytes()
    assert progress[-1][0] + 1 == progress[-1][1]
    assert progress[-1][3] == 1.0
    assert p.directions == directions  # Original user parameters are not mutated.


def test_uploaded_character_is_not_regenerated(tmp_path):
    character = tmp_path / "uploaded.png"
    Image.new("RGB", (64, 64), "orange").save(character)
    image = RecordingImages()
    p = replace(params(tmp_path), reference_image=character, directions=("north",))
    result = DirectionalWorkflow(MockLLMAPI(), image, MockVideoAPI(), p).run()
    assert len(image.calls) == 1
    assert fu.load_image(result.master_image).convert("RGB").tobytes() == fu.load_image(character).convert("RGB").tobytes()


def test_partial_batch_survives_failure(tmp_path):
    class FailingImages(RecordingImages):
        def call(self, prompt, **kwargs):
            if len(self.calls) == 2:
                return APIResult(ok=False, error="service unavailable")
            return super().call(prompt, **kwargs)
    with pytest.raises(WorkflowError, match="首帧生成失败"):
        DirectionalWorkflow(MockLLMAPI(), FailingImages(), MockVideoAPI(), params(tmp_path)).run()
    manifest = json.loads((tmp_path / "animations.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert list(manifest["animations"]) == ["south"]
    assert (tmp_path / manifest["animations"]["south"]["gif"]).exists()


def test_cancellation_preserves_finished_directions(tmp_path):
    cancel = threading.Event()
    class CancellingImages(RecordingImages):
        def call(self, prompt, **kwargs):
            result = super().call(prompt, **kwargs)
            if len(self.calls) == 3:
                cancel.set()
            return result
    with pytest.raises(WorkflowCancelled):
        DirectionalWorkflow(MockLLMAPI(), CancellingImages(), MockVideoAPI(), params(tmp_path), cancel=cancel).run()
    manifest = json.loads((tmp_path / "animations.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled" and list(manifest["animations"]) == ["south"]


@pytest.mark.parametrize("directions", [("north", "north"), ("invalid",), ()])
def test_invalid_directions_never_call_api(tmp_path, directions):
    image = RecordingImages()
    with pytest.raises(WorkflowError):
        DirectionalWorkflow(MockLLMAPI(), image, MockVideoAPI(), replace(params(tmp_path), directions=directions)).run()
    assert image.calls == []


def test_fixed_master_palette_applies_to_all_views(tmp_path):
    p = replace(params(tmp_path), directions=("south", "north"), pixelate=True, max_colors=4)
    result = DirectionalWorkflow(MockLLMAPI(), RecordingImages(), MockVideoAPI(), p).run()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    allowed = {tuple(color) for color in manifest["palette"]}
    for animation in result.animations.values():
        for path in animation.frames_dir.glob("*.png"):
            assert set(fu.load_image(path).convert("RGB").getdata()) <= allowed


def test_direction_preview_switches_export_and_ide_source(qtbot, tmp_path, monkeypatch):
    from config.api_config import APIConfigManager
    from core.storage.keyring import Keyring
    from ui.app_context import AppContext, UISettings
    from ui.pages.solo_page import SoloPage
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    ctx = AppContext(APIConfigManager(tmp_path / "apis.json", Keyring(tmp_path / "keyring")), UISettings(tmp_path / "ui.json"))
    page = SoloPage(ctx)
    qtbot.addWidget(page)
    page.show()
    animations = {}
    for direction in FOUR_DIRECTIONS:
        first = tmp_path / f"{direction}.png"
        Image.new("RGB", (32, 32), "orange").save(first)
        animations[direction] = SoloResult(output_dir=tmp_path / direction, first_frame=first)
    batch = DirectionalResult(tmp_path, tmp_path / "master.png", animations)
    page._on_success(batch)
    assert page._direction_preview.count() == 4
    page._direction_preview.setCurrentIndex(2)
    assert page._result is animations["north"]
    received = []
    page.sync_to_ide.connect(received.append)
    page._on_sync()
    assert received == [animations["north"]]
    page._directions_combo.setCurrentIndex(2)
    assert tuple(page._directions_combo.currentData()) == EIGHT_DIRECTIONS
    page._on_success(animations["south"])
    assert page._directional_result is None and page._direction_preview.isHidden()


def test_solo_worker_dispatches_directional_batch(qtbot, tmp_path):
    from config.api_config import APIConfig, APIConfigManager
    from core.storage.keyring import Keyring
    from ui.workers import SoloWorker
    manager = APIConfigManager(tmp_path / "apis.json", Keyring(tmp_path / "keyring"))
    for kind in ("llm", "image", "video"):
        manager.add(APIConfig(kind=kind, name=kind, base_url="mock", model="mock",
                              params={"mock": True, "size": "64x64"}))
    worker = SoloWorker(manager, replace(params(tmp_path / "result"), directions=("south", "north")))
    received = []
    worker.succeeded.connect(received.append)
    with qtbot.waitSignal(worker.succeeded, timeout=30_000):
        worker.start()
    worker.wait(5000)
    assert isinstance(received[0], DirectionalResult)
    assert list(received[0].animations) == ["south", "north"]
