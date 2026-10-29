"""HTTP validation and real SQLite profile identity, without model downloads."""

import numpy as np
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from backend import backends, config
from backend.database import Base, Generation, ProfileSample, VoiceProfile, get_db
from backend.routes import model_residency as routes
from backend.services import model_residency as residency


class Backend:
    model_size = "default"

    def __init__(self):
        self.loaded = False
        self.prompts = []

    def is_loaded(self):
        return self.loaded

    def _is_model_cached(self):
        return True

    async def load_model(self):
        self.loaded = True

    def unload_model(self):
        self.loaded = False

    async def create_voice_prompt(self, path, text, use_cache=True):
        self.prompts.append((path, text, use_cache))
        return {"speaker": "exact"}, False

    async def generate(self, text, prompt, *args, **kwargs):
        assert prompt == {"speaker": "exact"}
        return np.ones(10, dtype=np.float32), 48000


@pytest.fixture
def api(monkeypatch, tmp_path):
    instance = residency.ModelResidency()
    backend = Backend()
    monkeypatch.setattr(residency, "model_residency", instance)
    monkeypatch.setattr(routes, "model_residency", instance)
    monkeypatch.setattr(residency, "_estimated_weights", lambda config: config.size_mb)
    monkeypatch.setattr(backends, "_tts_backends", {"moss_tts": backend})
    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda engine: backend)
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        sample = tmp_path / "reference.wav"
        sample.write_bytes(b"existing synthetic fixture")
        db.add(VoiceProfile(id="exact", name="Exact fixture", language="en", voice_type="cloned"))
        db.add(
            ProfileSample(id="sample", profile_id="exact", audio_path=str(sample), reference_text="Exact transcript")
        )
        db.commit()
        app = FastAPI()
        from backend.routes.generations import router as generations_router

        app.include_router(routes.router)
        app.include_router(generations_router)
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            yield client, db, backend, tmp_path
    engine.dispose()


def test_api_policy_and_weights_only_warm(api):
    client, db, backend, root = api
    assert client.get("/models/residency").json()["persistence"] == "session"
    for payload in [
        {"max_resident_engines": True, "estimated_weight_budget_mb": 1000},
        {"max_resident_engines": 0, "estimated_weight_budget_mb": 1000},
    ]:
        assert client.put("/models/residency", json=payload).status_code == 422
    assert (
        client.put(
            "/models/residency", json={"max_resident_engines": 2, "estimated_weight_budget_mb": 1000}
        ).status_code
        == 200
    )
    response = client.post("/models/prewarm", json={"model_name": "moss-tts"})
    assert response.status_code == 200
    assert response.json()["mode"] == "weights_only"
    assert not backend.prompts
    assert db.query(Generation).count() == 0
    assert set(p.name for p in root.iterdir()) == {"reference.wav"}


def test_api_voice_warm_uses_exact_existing_sample_without_history_or_audio(api):
    client, db, backend, root = api
    response = client.post("/models/prewarm", json={"model_name": "moss-tts", "profile_id": "exact"})
    assert response.status_code == 200
    assert response.json()["mode"] == "voice"
    assert response.json()["audio_saved"] is False
    assert backend.prompts == [(str(root / "reference.wav"), "Exact transcript", True)]
    assert db.query(Generation).count() == 0
    assert db.query(ProfileSample).count() == 1
    assert set(p.name for p in root.iterdir()) == {"reference.wav"}


def test_api_refuses_incompatible_missing_and_multi_sample_profile(api):
    client, db, backend, root = api
    for payload in [
        {"model_name": "whisper-base"},
        {"model_name": "missing"},
        {"model_name": "moss-tts", "profile_id": "missing"},
    ]:
        assert client.post("/models/prewarm", json=payload).status_code in {400, 404}
    db.add(
        ProfileSample(id="second", profile_id="exact", audio_path=str(root / "reference.wav"), reference_text="Second")
    )
    db.commit()
    response = client.post("/models/prewarm", json={"model_name": "moss-tts", "profile_id": "exact"})
    assert response.status_code == 400
    assert "exactly one" in response.json()["detail"]
    assert not backend.loaded
    assert not backend.prompts


def test_real_speech_request_omitting_model_size_keeps_single_model_compatibility(api):
    client, db, backend, root = api
    response = client.post(
        "/generate/stream",
        json={
            "profile_id": "exact",
            "text": "A synthetic generation request.",
            "engine": "moss_tts",
            "normalize": False,
            "effects_chain": [],
        },
    )
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"
    assert backend.loaded
    assert backend.prompts[0][0] == str(root / "reference.wav")
    assert db.query(Generation).count() == 0
    assert set(p.name for p in root.iterdir()) == {"reference.wav"}


@pytest.mark.asyncio
async def test_real_persisted_generation_service_accepts_single_model_none_size(api, monkeypatch):
    from backend.services import generation

    _, db, backend, root = api
    db.add(
        Generation(id="persisted", profile_id="exact", text="A real generation-service request.", status="generating")
    )
    db.commit()
    monkeypatch.setattr(generation, "get_db", lambda: iter([db]))
    monkeypatch.setattr(generation, "_notify_speak_end", lambda *args, **kwargs: None)
    await generation.run_generation(
        generation_id="persisted",
        profile_id="exact",
        text="A real generation-service request.",
        language="en",
        engine="moss_tts",
        model_size=None,
        seed=1,
        normalize=False,
        effects_chain=[],
        mode="generate",
    )
    row = db.get(Generation, "persisted")
    assert row.status == "completed", row.error
    assert backend.loaded
    assert config.resolve_storage_path(row.audio_path).is_file()
