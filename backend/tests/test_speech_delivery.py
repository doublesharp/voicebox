"""Speech controls: deterministic DSP, replay, and adapter boundaries."""

import contextlib
import json
import sys
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from backend import backends, config
from backend.database import Generation, GenerationVersion
from backend.models import GenerationRequest
from backend.routes import generations
from backend.services import generation as generation_service
from backend.tests.test_generation_audio_processing import decoded, pipeline
from backend.tests.test_generation_replay_settings import replay
from backend.utils.audio_processing import process_generated_audio
from backend.utils.chunked_tts import generate_chunked
from backend.utils.speech_delivery import apply_speed

__all__ = ["pipeline", "replay"]

DELIVERY = {
    "speed": 0.85,
    "chatterbox_temperature": 0.6,
    "chatterbox_cfg_weight": 0.3,
    "chatterbox_exaggeration": 0.2,
}


def test_normal_speaking_rate_is_an_exact_noop():
    source = np.linspace(-0.2, 0.2, 2400, dtype=np.float32)
    assert apply_speed(source, 24000, 1.0) is source


@pytest.mark.parametrize("speed", [0.5, 0.85, 1.25, 2.0])
def test_rate_changes_duration_and_preserves_tone_pitch(speed):
    sr = 24000
    source = (0.2 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)
    changed = apply_speed(source, sr, speed)
    assert len(changed) == pytest.approx(len(source) / speed, abs=1)
    middle = changed[len(changed) // 10 : -len(changed) // 10]
    frequencies = np.fft.rfftfreq(len(middle), 1 / sr)
    peak_hz = frequencies[np.argmax(np.abs(np.fft.rfft(middle)))]
    assert peak_hz == pytest.approx(440, abs=3)
    assert np.isfinite(changed).all()
    assert changed.dtype == source.dtype


def test_time_stretch_overshoot_is_guarded_before_encoding(monkeypatch):
    from backend.utils import audio_processing

    monkeypatch.setattr(audio_processing, "apply_speed", lambda *args: np.array([2.0, 1.0, -0.5]))
    clean, processed = process_generated_audio(np.ones(3), 24000, speed=0.85, normalize=False)
    np.testing.assert_allclose(clean, [0.999, 0.4995, -0.24975])
    np.testing.assert_array_equal(clean, processed)


def test_request_retains_speaking_speed():
    request = GenerationRequest(profile_id="voice", text="Hello.", speed=0.85)
    assert request.model_dump().get("speed") == 0.85


@pytest.mark.parametrize("speed", [0.49, 2.01, float("nan"), float("inf")])
def test_request_rejects_invalid_speaking_speed(speed):
    with pytest.raises(ValidationError):
        GenerationRequest(profile_id="voice", text="Hello.", speed=speed)


@pytest.mark.parametrize(
    "options",
    [
        {"chatterbox_temperature": 0.09},
        {"chatterbox_temperature": 1.51},
        {"chatterbox_cfg_weight": -0.01},
        {"chatterbox_cfg_weight": 1.01},
        {"chatterbox_exaggeration": -0.01},
        {"chatterbox_exaggeration": 1.01},
    ],
)
def test_request_rejects_invalid_chatterbox_controls(options):
    with pytest.raises(ValidationError):
        GenerationRequest(profile_id="voice", text="Hello.", engine="chatterbox", **options)


def test_other_engines_reject_chatterbox_controls():
    with pytest.raises(ValidationError, match="require the Chatterbox"):
        GenerationRequest(profile_id="voice", text="Hello.", engine="qwen", **DELIVERY)


async def test_request_snapshots_delivery_before_work_is_enqueued(replay):
    db, calls = replay
    response = await generations.generate_speech(
        GenerationRequest(profile_id="voice", text="Hello.", engine="chatterbox", **DELIVERY), db
    )
    saved = json.loads(db.get(Generation, response.id).synthesis_config)
    for name, value in DELIVERY.items():
        assert saved[name] == calls[0][name] == value


@pytest.mark.parametrize(("action", "status"), [("retry", "failed"), ("regenerate", "completed")])
async def test_replay_retains_speech_delivery(replay, action, status):
    db, calls = replay
    db.add(Generation(
        id="delivery", profile_id="voice", text="Hello.", engine="chatterbox", status=status,
        synthesis_config=json.dumps({"version": 1, **DELIVERY}),
    ))
    db.commit()
    await getattr(generations, f"{action}_generation")("delivery", db)
    for name, value in DELIVERY.items():
        assert calls[0][name] == value


async def test_invalid_saved_speed_does_not_start_replay(replay):
    from fastapi import HTTPException

    db, calls = replay
    db.add(Generation(
        id="invalid-speed", profile_id="voice", text="Hello.", status="failed",
        synthesis_config=json.dumps({"speed": 0}),
    ))
    db.commit()
    with pytest.raises(HTTPException):
        await generations.retry_generation("invalid-speed", db)
    assert calls == []
    assert db.get(Generation, "invalid-speed").status == "failed"


async def test_chunking_routes_sampler_options_to_every_chunk():
    calls = []
    options = {"temperature": 0.6, "cfg_weight": 0.3}

    class Backend:
        async def generate(self, text, prompt, language, seed, instruct, *, generation_options):
            calls.append((text, dict(generation_options)))
            return np.ones(2400, dtype=np.float32), 24000

    await generate_chunked(
        Backend(), "This is a sentence. " * 20, {}, max_chunk_chars=100,
        generation_options=options,
    )
    assert len(calls) > 1
    assert all(received == options for _, received in calls)
    assert options == {"temperature": 0.6, "cfg_weight": 0.3}


@pytest.mark.parametrize("adapter", ["mlx", "torch"])
@pytest.mark.parametrize("language", ["en", "he"])
async def test_adapter_preserves_defaults_and_merges_only_explicit_controls(monkeypatch, adapter, language):
    calls = []

    class Model:
        sr = 24000

        def generate(self, *args, **kwargs):
            calls.append(kwargs)
            audio = np.full(2400, 0.1, dtype=np.float32)
            return [SimpleNamespace(audio=audio)] if adapter == "mlx" else audio

    if adapter == "mlx":
        from backend.backends import chatterbox_mlx_backend as module

        async def inline(call):
            return call()

        monkeypatch.setattr(module, "_run_on_mlx_thread", inline)
        backend = module.ChatterboxMLXTTSBackend()
        defaults = dict(backend._DEFAULTS)
    else:
        from backend.backends import chatterbox_backend as module

        monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=contextlib.nullcontext, Tensor=type("Tensor", (), {})))
        backend = module.ChatterboxTTSBackend()
        defaults = dict(backend._LANG_DEFAULTS.get(language, backend._GLOBAL_DEFAULTS))
    backend.model = Model()
    await backend.generate("Hello.", {}, language=language)
    for key, value in defaults.items():
        assert calls[-1][key] == value
    await backend.generate("Hello.", {}, language=language, generation_options={"cfg_weight": 0.3, "temperature": 1.0})
    assert calls[-1]["cfg_weight"] == 0.3
    assert calls[-1]["temperature"] == 1.0
    assert calls[-1]["exaggeration"] == defaults["exaggeration"]
    assert calls[-1]["repetition_penalty"] == defaults["repetition_penalty"]


async def test_saved_preview_and_effects_edits_keep_the_same_rate(pipeline, monkeypatch):
    import soundfile as sf

    app, sessions = pipeline
    calls = []
    sr = 24000
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)

    class Backend:
        device = "cpu"

        def is_loaded(self):
            return True

        async def generate(self, *args, **kwargs):
            calls.append(kwargs)
            return tone.copy(), sr

    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda _engine: Backend())
    await generation_service.run_generation(
        generation_id="persisted", profile_id="voice", text="Hello.", language="en",
        engine="chatterbox", model_size="default", seed=None, normalize=False,
        effects_chain=[], mode="generate", **DELIVERY,
    )
    with sessions() as db:
        row = db.get(Generation, "persisted")
        assert row.status == "completed", row.error
        saved, _ = sf.read(config.resolve_storage_path(row.audio_path), dtype="float32")
        assert row.duration == pytest.approx(len(saved) / sr)
        assert len(saved) == pytest.approx(sr / DELIVERY["speed"], abs=1)
        assert db.query(GenerationVersion).count() == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://voicebox.test") as client:
        preview = decoded(await client.post("/generate/stream", json={
            "profile_id": "voice", "text": "Hello.", "engine": "chatterbox",
            "normalize": False, "effects_chain": [], **DELIVERY,
        }))
        edited = await client.post("/generations/persisted/versions/apply-effects", json={"effects_chain": []})
        assert edited.status_code == 200, edited.text
        edited_audio, _ = sf.read(config.resolve_storage_path(edited.json()["audio_path"]), dtype="float32")
    np.testing.assert_allclose(preview, saved, atol=4e-5)
    np.testing.assert_allclose(edited_audio, saved, atol=4e-5)
    assert all(call["generation_options"] == {"temperature": 0.6, "cfg_weight": 0.3, "exaggeration": 0.2} for call in calls)
