"""HTTP preview shutdown keeps the scheduler until the physical model drains."""

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine

from backend import backends
from backend.backends import qwen_mlx_styles_backend as styles
from backend.models import GenerationRequest
from backend.routes import generations
from backend.services import profiles
from backend.services.inference_scheduler import run_serialized_inference


@pytest.mark.asyncio
@pytest.mark.parametrize("next_operation", ["unload", "other_engine"])
@pytest.mark.parametrize("repeated_cancel", [False, True])
async def test_disconnect_drains_native_worker_before_next_serialized_request(
    monkeypatch, next_operation, repeated_cancel
):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def generate(**kwargs):
        try:
            for _ in range(5):
                yield SimpleNamespace(audio=np.full(8, 0.1, dtype=np.float32), sample_rate=1000)
            entered.set()
            assert release.wait(10), "test did not release native worker"
            yield SimpleNamespace(audio=np.full(8, 0.1, dtype=np.float32), sample_rate=1000)
        finally:
            closed.set()

    adapter = styles.QwenMLXCustomVoiceBackend()
    adapter.model = SimpleNamespace(generate_custom_voice=generate)
    adapter._current_model_size = adapter.model_size

    async def noop(*args, **kwargs):
        pass

    async def profile(*args, **kwargs):
        return SimpleNamespace(effects_chain=None)

    async def prompt(*args, **kwargs):
        return {"voice_type": "preset", "preset_engine": "qwen_custom_voice", "preset_voice_id": "Ryan"}

    monkeypatch.setattr(backends, "get_tts_backend_for_engine", lambda _: adapter)
    monkeypatch.setattr(backends, "load_engine_model", noop)
    monkeypatch.setattr(backends, "ensure_model_cached_or_raise", noop)
    monkeypatch.setattr(backends, "engine_needs_trim", lambda _: False)
    monkeypatch.setattr(backends, "engine_retries_runaway", lambda _: False)
    monkeypatch.setattr(profiles, "get_profile", profile)
    monkeypatch.setattr(profiles, "create_voice_prompt_for_profile", prompt)
    monkeypatch.setattr(profiles, "validate_profile_engine", lambda *args: None)
    monkeypatch.setattr(generations, "_resolve_generation_engine", lambda *args: "qwen_custom_voice")
    memory_releases = []
    monkeypatch.setattr(generations, "release_generation_memory", lambda _: memory_releases.append(closed.is_set()))
    monkeypatch.setattr(styles, "empty_mlx_cache", lambda: None)
    database = create_engine("sqlite://")
    stream = None
    close_task = next_task = None
    admitted = asyncio.Event()
    try:
        response = await generations._incremental_speech(
            GenerationRequest(
                profile_id="synthetic", text="A sentence.", normalize=False, effects_chain=[], crossfade_ms=0
            ),
            SimpleNamespace(get_bind=lambda: database),
        )
        stream = response.body_iterator
        assert (await asyncio.wait_for(anext(stream), 2))[:4] == b"RIFF"
        assert await asyncio.to_thread(entered.wait, 2)
        # Stop consuming with the HTTP queue full; send() must unwind the
        # model stream before scheduler admission or memory release.
        close_task = asyncio.create_task(stream.aclose())

        async def next_request():
            admitted.set()
            assert closed.is_set(), "next engine entered before native worker exited"
            assert not adapter._op_lock.locked()
            if next_operation == "unload":
                adapter.unload_model()
            return "ready"

        next_task = asyncio.create_task(run_serialized_inference(next_request))
        await asyncio.sleep(0.03)
        assert not admitted.is_set(), "disconnect released the inference scheduler before physical worker exit"
        assert not close_task.done()
        assert adapter._op_lock.locked()
        if repeated_cancel:
            for _ in range(2):
                close_task.cancel()
                await asyncio.sleep(0.01)
                assert not close_task.done()
                assert not admitted.is_set()
        release.set()
        if repeated_cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close_task, 2)
        else:
            await asyncio.wait_for(close_task, 2)
        assert await asyncio.wait_for(next_task, 2) == "ready"
        assert closed.is_set()
        assert memory_releases == [True]
    finally:
        release.set()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        if stream is not None:
            await stream.aclose()
        if next_task is not None:
            await asyncio.gather(next_task, return_exceptions=True)
        database.dispose()
