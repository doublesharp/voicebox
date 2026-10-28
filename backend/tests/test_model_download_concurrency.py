import asyncio
from types import SimpleNamespace

import pytest

from backend import backends, models
from backend.routes import models as model_routes
from backend.services.inference_scheduler import run_serialized_inference


@pytest.mark.parametrize("engine", ["whisper", "qwen_llm"])
async def test_non_tts_download_does_not_wait_for_or_block_speech(monkeypatch, engine):
    speech_started = asyncio.Event()
    release_speech = asyncio.Event()
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    tasks = []

    async def speech():
        speech_started.set()
        await release_speech.wait()

    async def download():
        download_started.set()
        await release_download.wait()

    configuration = SimpleNamespace(engine=engine, model_name="download-test")
    monkeypatch.setattr(backends, "get_model_config", lambda name: configuration)
    monkeypatch.setattr(backends, "get_model_load_func", lambda config: download)
    monkeypatch.setattr(model_routes, "create_background_task", lambda coro: tasks.append(asyncio.create_task(coro)))
    speech_task = asyncio.create_task(run_serialized_inference(speech))
    try:
        await speech_started.wait()
        await model_routes.trigger_model_download(models.ModelDownloadRequest(model_name="download-test"))
        try:
            await asyncio.wait_for(download_started.wait(), .2)
        except TimeoutError:
            pytest.fail("unrelated model download waited for speech ownership")
        release_speech.set()
        await speech_task
        async def next_speech():
            return "audio"
        assert await asyncio.wait_for(run_serialized_inference(next_speech), .2) == "audio"
    finally:
        release_speech.set()
        release_download.set()
        await asyncio.gather(speech_task, *tasks, return_exceptions=True)
