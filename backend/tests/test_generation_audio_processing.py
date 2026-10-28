"""Real API/SQLite/PCM processing with model inference replaced by a waveform."""

import io
import json

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import backends, config
from backend.database import Base, Generation, VoiceProfile, get_db
from backend.routes import effects, generations, openai_compat
from backend.services import generation as generation_service, profiles

GAIN = [{"type": "gain", "enabled": True, "params": {"gain_db": -6.0}}]
HOT_GAIN = [{"type": "gain", "enabled": True, "params": {"gain_db": 40.0}}]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(VoiceProfile(id="voice", name="Voice", language="en", effects_chain=json.dumps(GAIN)))
        db.add(Generation(id="persisted", profile_id="voice", text="Hello.", status="generating"))
        db.commit()

    waveform = np.zeros(2400, dtype=np.float32)
    waveform[:3] = [0.2, 0.1, -0.05]

    class Backend:
        device = "cpu"

        def is_loaded(self):
            return True

        async def generate(self, *args, **kwargs):
            return waveform.copy(), 24000

    async def no_load(*args, **kwargs):
        return None

    async def prompt(*args, **kwargs):
        return {}

    def database():
        with sessions() as db:
            yield db

    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda _engine: Backend())
    monkeypatch.setattr(backends, "load_engine_model", no_load)
    monkeypatch.setattr(backends, "ensure_model_cached_or_raise", no_load)
    monkeypatch.setattr(backends, "engine_needs_trim", lambda _engine: False)
    monkeypatch.setattr(backends, "engine_retries_runaway", lambda _engine: False)
    monkeypatch.setattr(profiles, "create_voice_prompt_for_profile", prompt)
    monkeypatch.setattr(generation_service, "get_db", database)
    monkeypatch.setattr(generation_service, "release_generation_memory", lambda _model: None)
    monkeypatch.setattr(generations, "release_generation_memory", lambda _model: None)
    monkeypatch.setattr(generation_service, "_notify_speak_end", lambda *args, **kwargs: None)

    app = FastAPI()
    app.include_router(generations.router)
    app.include_router(openai_compat.router)
    app.include_router(effects.router)
    app.dependency_overrides[get_db] = database
    yield app, sessions
    engine.dispose()


async def persist(sessions, effects_chain=GAIN, normalize=True):
    await generation_service.run_generation(
        generation_id="persisted",
        profile_id="voice",
        text="Hello.",
        language="en",
        engine="qwen",
        model_size="1.7B",
        seed=1,
        normalize=normalize,
        effects_chain=effects_chain,
        mode="generate",
    )
    with sessions() as db:
        row = db.get(Generation, "persisted")
        assert row.status == "completed", row.error
        return sf.read(config.resolve_storage_path(row.audio_path), dtype="float32")[0]


def decoded(response):
    assert response.status_code == 200, response.text
    return sf.read(io.BytesIO(response.content), dtype="float32")[0]


@pytest.mark.parametrize("explicit_effects", [True, False])
async def test_same_processing_for_persisted_stream_and_openai(pipeline, explicit_effects):
    app, sessions = pipeline
    saved = await persist(sessions)
    body = {"profile_id": "voice", "text": "Hello.", "normalize": True}
    if explicit_effects:
        body["effects_chain"] = GAIN
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voicebox.test") as client:
        streamed = decoded(await client.post("/generate/stream", json=body))
        compatible = decoded(
            await client.post("/v1/audio/speech", json={"model": "qwen", "voice": "Voice", "input": "Hello."})
        )
    np.testing.assert_allclose(streamed, saved, atol=4e-5)
    np.testing.assert_allclose(compatible, saved, atol=4e-5)


async def test_unpersisted_speak_honours_profile_effects(pipeline):
    _, sessions = pipeline
    saved = await persist(sessions)
    result = await generation_service.generate_audio_sync(
        profile_id="voice",
        text="Hello.",
        language="en",
        engine="qwen",
        model_size="1.7B",
        normalize=True,
    )
    audio, _ = sf.read(io.BytesIO(result), dtype="float32")
    np.testing.assert_allclose(audio, saved, atol=4e-5)


async def test_persisted_hot_effect_preserves_peak_ratios(pipeline):
    _, sessions = pipeline
    audio = await persist(sessions, effects_chain=HOT_GAIN, normalize=False)
    np.testing.assert_allclose(audio[:3], [0.999, 0.4995, -0.24975], atol=4e-5)


async def test_persisted_duration_includes_effect_tail(pipeline):
    _, sessions = pipeline
    audio = await persist(sessions, effects_chain=[{"type": "delay", "params": {"delay_seconds": 0.2}}])
    assert len(audio) > 2400
    with sessions() as db:
        assert db.get(Generation, "persisted").duration == pytest.approx(len(audio) / 24000)


@pytest.mark.parametrize("operation", ["preview", "apply"])
async def test_version_effect_paths_guard_hot_peaks(pipeline, operation):
    app, sessions = pipeline
    await persist(sessions, effects_chain=None, normalize=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voicebox.test") as client:
        if operation == "preview":
            audio = decoded(await client.post("/effects/preview/persisted", json={"effects_chain": HOT_GAIN}))
        else:
            response = await client.post(
                "/generations/persisted/versions/apply-effects",
                json={"effects_chain": HOT_GAIN},
            )
            assert response.status_code == 200, response.text
            audio, _ = sf.read(config.resolve_storage_path(response.json()["audio_path"]), dtype="float32")
    np.testing.assert_allclose(audio[:3], [0.999, 0.4995, -0.24975], atol=3e-4)


async def test_openai_speed_result_is_guarded_before_pcm(pipeline, monkeypatch):
    app, _ = pipeline
    monkeypatch.setattr(openai_compat, "apply_speed", lambda *args: np.array([2.0, 1.0, -0.5], dtype=np.float32))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voicebox.test") as client:
        audio = decoded(
            await client.post(
                "/v1/audio/speech", json={"model": "qwen", "voice": "Voice", "input": "Hello.", "speed": 2.0}
            )
        )
    np.testing.assert_allclose(audio, [0.999, 0.4995, -0.24975], atol=4e-5)


async def test_incremental_preview_emits_before_remaining_inference_finishes(pipeline, monkeypatch):
    import asyncio
    from backend.models import GenerationRequest

    _, sessions = pipeline
    remaining = asyncio.Event()

    class IncrementalBackend:
        device = "cpu"
        calls = 0
        async def generate(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                await remaining.wait()
            return np.full(2400, 0.1, dtype=np.float32), 24000

    backend = IncrementalBackend()
    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda _engine: backend)
    with sessions() as db:
        response = await generations.stream_speech(
            GenerationRequest(profile_id="voice", text="A sentence. "*100, normalize=False, effects_chain=[], crossfade_ms=0),
            db, incremental=True,
        )
    iterator = response.body_iterator
    try:
        header = await asyncio.wait_for(anext(iterator), 1)
        pcm = await asyncio.wait_for(anext(iterator), 1)
        assert header[:4] == b"RIFF"
        assert len(pcm) == 4800
        assert not remaining.is_set()
        assert response.headers["x-voicebox-audio-mode"] == "incremental-preview"
    finally:
        remaining.set()
        await iterator.aclose()


@pytest.mark.parametrize("options", [{"normalize": True, "effects_chain": []}, {"normalize": False}])
async def test_incremental_preview_requires_explicit_unmastered_audio(pipeline, options):
    app, _ = pipeline
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voicebox.test") as client:
        response = await client.post("/generate/stream?incremental=true", json={"profile_id": "voice", "text": "Hello.", **options})
    assert response.status_code == 400
    assert "Incremental preview" in response.json()["detail"]
