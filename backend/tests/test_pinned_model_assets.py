"""Pinned model readiness ignores moving refs and other revisions' downloads."""

import pytest
from huggingface_hub import constants

from backend import backends
from backend.backends.base import is_model_cached
from backend.routes.models import _scan_model_inventory

REVISION = "a" * 40
OTHER = "b" * 40


def write_snapshot(tmp_path, repo, revision, files):
    root = tmp_path / ("models--" + repo.replace("/", "--"))
    snapshot = root / "snapshots" / revision
    for name in files:
        path = snapshot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")
    return root, snapshot


def test_completed_pinned_assets_do_not_wait_for_another_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    root, _ = write_snapshot(tmp_path, "test/model", REVISION, ["config.json", "model.onnx"])
    (root / "blobs").mkdir()
    (root / "blobs" / "unrelated.incomplete").write_bytes(b"partial")
    assert is_model_cached("test/model", revision=REVISION, required_files=["config.json", "model.onnx"])


def test_another_snapshot_cannot_satisfy_pinned_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    write_snapshot(tmp_path, "test/model", OTHER, ["config.json", "model.onnx"])
    assert not is_model_cached("test/model", revision=REVISION, required_files=["config.json", "model.onnx"])


@pytest.mark.parametrize("missing", ["tts", "codec"])
def test_inventory_requires_all_exact_primary_and_auxiliary_assets(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    _, tts = write_snapshot(tmp_path, "test/tts", REVISION, ["config.json", "model.onnx"])
    _, codec = write_snapshot(tmp_path, "test/codec", OTHER, ["codec.onnx"])
    cfg = backends.ModelConfig(
        model_name="test-tts",
        display_name="Test",
        engine="test",
        hf_repo_id="test/tts",
        revision=REVISION,
        required_files=("config.json", "model.onnx"),
        aux_hf_repo_ids=("test/codec",),
        aux_revisions={"test/codec": OTHER},
        aux_required_files={"test/codec": ("codec.onnx",)},
    )
    assert _scan_model_inventory([cfg])[cfg.model_name][0]
    (tts / "model.onnx" if missing == "tts" else codec / "codec.onnx").unlink()
    assert not _scan_model_inventory([cfg])[cfg.model_name][0]
