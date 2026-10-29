"""Health readiness reflects actual cached model weights, not cache metadata."""

import sys
from types import SimpleNamespace

import huggingface_hub
import huggingface_hub.constants as hf_constants
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import backends
from backend.routes import health, models as model_routes


@pytest.fixture
def health_client(tmp_path, monkeypatch):
    repo = "test/model"
    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(backends, "get_model_config", lambda _: SimpleNamespace(hf_repo_id=repo))
    monkeypatch.setattr(health.tts, "get_tts_model", lambda: SimpleNamespace(is_loaded=lambda: False))
    monkeypatch.setattr(health, "get_backend_type", lambda: "pytorch")
    monkeypatch.setattr(health, "is_amd_rocm_capable", lambda: False)
    monkeypatch.setattr(health.config, "is_cloud_enabled", lambda: False)
    monkeypatch.setattr(health.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(health.torch.backends.mps, "is_available", lambda: False)
    monkeypatch.setitem(sys.modules, "intel_extension_for_pytorch", None)
    monkeypatch.setitem(sys.modules, "torch_directml", None)
    # Hugging Face's cache scan lists repositories even when no weights exist.
    monkeypatch.setattr(
        huggingface_hub, "scan_cache_dir", lambda cache_dir=None: SimpleNamespace(repos=[SimpleNamespace(repo_id=repo)])
    )
    app = FastAPI()
    app.include_router(health.router)
    with TestClient(app) as client:
        yield client, tmp_path / "models--test--model"


@pytest.mark.parametrize(
    ("weights", "incomplete", "expected"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
def test_health_reports_actual_model_cache_readiness(health_client, weights, incomplete, expected):
    client, repo = health_client
    snapshot = repo / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    if weights:
        (snapshot / "model.safetensors").write_bytes(b"weights")
    if incomplete:
        blobs = repo / "blobs"
        blobs.mkdir()
        (blobs / "download.incomplete").write_bytes(b"partial")

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["model_loaded"] is False
    assert body["model_downloaded"] is expected
    assert body["gpu_available"] is False
    assert body["backend_variant"] == "cpu"


def test_health_does_not_report_broken_weight_link_as_downloaded(health_client):
    client, repo = health_client
    snapshot = repo / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").symlink_to(repo / "blobs" / "missing")
    assert client.get("/health").json()["model_downloaded"] is False


def test_loaded_model_and_cuda_diagnostics_are_reported(health_client, monkeypatch):
    from backend.backends import base

    client, _ = health_client
    monkeypatch.setattr(health.tts, "get_tts_model", lambda: SimpleNamespace(is_loaded=lambda: True, model_size="1.7B"))
    monkeypatch.setattr(health.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(health.torch.cuda, "get_device_name", lambda _: "Test GPU")
    monkeypatch.setattr(health.torch.cuda, "memory_allocated", lambda: 256 * 1024 * 1024)
    monkeypatch.setattr(health.torch.version, "hip", None)
    monkeypatch.setattr(base, "check_cuda_compatibility", lambda: (False, "unsupported GPU"))
    body = client.get("/health").json()
    assert body["model_loaded"] is True
    assert body["model_size"] == "1.7B"
    assert body["gpu_available"] is True
    assert body["gpu_type"] == "CUDA (Test GPU)"
    assert body["vram_used_mb"] == 256
    assert body["gpu_compatibility_warning"] == "unsupported GPU"
    assert body["backend_variant"] == "cuda"


@pytest.mark.parametrize("scan_finds_repo", [False, True])
@pytest.mark.parametrize("weight_exists", [False, True])
def test_model_status_validates_weight_links(health_client, monkeypatch, scan_finds_repo, weight_exists):
    _, repo = health_client
    snapshot = repo / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    weight = snapshot / "model.safetensors"
    target = repo / "blobs" / "weight"
    if weight_exists:
        target.parent.mkdir()
        target.write_bytes(b"weights")
    weight.symlink_to(target)
    config = SimpleNamespace(
        model_name="test", display_name="Test", hf_repo_id="test/model", aux_hf_repo_ids=[], model_size="1.7B"
    )
    monkeypatch.setattr(backends, "get_all_model_configs", lambda: [config])
    monkeypatch.setattr(backends, "check_model_loaded", lambda _: False)
    file = SimpleNamespace(file_name="model.safetensors", file_path=weight)
    revision = SimpleNamespace(files=[file], size_on_disk=7)
    cached_repo = SimpleNamespace(repo_id="test/model", revisions=[revision])
    monkeypatch.setattr(
        huggingface_hub, "scan_cache_dir", lambda cache_dir=None: SimpleNamespace(repos=[cached_repo] if scan_finds_repo else [])
    )
    monkeypatch.setattr(model_routes, "get_task_manager", lambda: SimpleNamespace(get_pending_downloads=list))
    app = FastAPI()
    app.include_router(model_routes.router)
    with TestClient(app) as client:
        response = client.get("/models/status")
    assert response.status_code == 200
    assert response.json()["models"][0]["downloaded"] is weight_exists
