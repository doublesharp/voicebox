"""Replay settings and cancellation through real SQLite/WAV persistence."""

import asyncio
import threading

import numpy as np
import pytest
import soundfile as sf

from backend import config
from backend.database import Generation, GenerationVersion
from backend.services import generation as generation_service
from backend.tests.test_generation_audio_processing import GAIN, pipeline

__all__ = ["pipeline"]  # Re-export the shared SQLite/WAV fixture for pytest.


async def render(*, mode, normalize=False, effects_chain=None):
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
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["retry", "regenerate"])
async def test_replay_keeps_original_and_links_effects_to_its_new_clean_take(pipeline, mode):
    _, sessions = pipeline
    await render(mode="generate")
    with sessions() as db:
        original = db.query(GenerationVersion).filter_by(label="original").one()
        original_path = config.resolve_storage_path(original.audio_path)
        original_bytes = original_path.read_bytes()
        original_id = original.id
        original_storage_path = original.audio_path
        db.get(Generation, "persisted").status = "failed" if mode == "retry" else "completed"
        db.commit()

    await render(mode=mode, effects_chain=GAIN)
    with sessions() as db:
        row = db.get(Generation, "persisted")
        assert row.status == "completed", row.error
        assert original_path.read_bytes() == original_bytes
        processed = db.query(GenerationVersion).filter_by(is_default=True).one()
        clean = db.get(GenerationVersion, processed.source_version_id)
        assert clean.id != original_id
        assert clean.effects_chain is None
        assert clean.audio_path != original_storage_path
        clean_audio, _ = sf.read(config.resolve_storage_path(clean.audio_path), dtype="float32")
        processed_audio, _ = sf.read(config.resolve_storage_path(processed.audio_path), dtype="float32")
        # Explicit normalize=False survives takes, despite a sparse waveform.
        np.testing.assert_allclose(clean_audio[:3], [0.2, 0.1, -0.05], atol=4e-5)
        np.testing.assert_allclose(processed_audio[:3], clean_audio[:3] * 10 ** (-6 / 20), atol=4e-5)
        assert row.duration == pytest.approx(len(processed_audio) / 24000)
        from backend.services import history

        public_versions, _ = history._get_versions_for_generation("persisted", db)
        public_processed = next(version for version in public_versions if version.id == processed.id)
        assert public_processed.source_version_id == clean.id


@pytest.mark.parametrize("operation", ["persisted", "sync", "buffered"])
async def test_cancel_during_processing_never_publishes_completed_audio(pipeline, monkeypatch, operation):
    _, sessions = pipeline
    from backend.models import GenerationRequest
    from backend.routes import generations
    from backend.services import tts
    from backend.utils import audio_processing

    started = threading.Event()
    release = threading.Event()
    original_process = audio_processing.process_generated_audio
    encoded = []

    def blocked_process(*args, **kwargs):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test did not release DSP")
        return original_process(*args, **kwargs)

    def encode(*args, **kwargs):
        encoded.append(True)
        return b"audio"

    monkeypatch.setattr(audio_processing, "process_generated_audio", blocked_process)
    monkeypatch.setattr(tts, "audio_to_wav_bytes", encode)
    with sessions() as db:
        if operation == "persisted":
            task = render(mode="generate")
        elif operation == "sync":
            task = generation_service.generate_audio_sync(
                profile_id="voice", text="Hello.", language="en", engine="qwen", model_size="1.7B"
            )
        else:
            task = generations._buffered_speech(GenerationRequest(profile_id="voice", text="Hello."), db)
        pending = asyncio.create_task(task)
        try:
            assert await asyncio.to_thread(started.wait, 5)
            pending.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert encoded == []
    with sessions() as db:
        row = db.get(Generation, "persisted")
        assert row.status != "completed"
        if operation == "persisted":
            assert row.status == "failed"
            assert row.error == "Generation cancelled"
        assert not row.audio_path
        assert db.query(GenerationVersion).count() == 0
    assert not list(config.get_generations_dir().glob("*.wav"))
