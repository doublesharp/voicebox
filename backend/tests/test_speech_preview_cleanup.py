"""Transient speech generation must release model resources on failure."""

import pytest

from backend import backends
from backend.models import GenerationRequest
from backend.routes import generations
from backend.services import generation as generation_service, profiles
from backend.tests.test_generation_audio_processing import pipeline

__all__ = ['pipeline']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['buffered', 'sync'])
@pytest.mark.parametrize('failure_stage', ['load', 'prompt'])
async def test_model_memory_released_if_initialization_fails(pipeline, monkeypatch, operation, failure_stage):
    _, sessions = pipeline
    model = object()
    released = []
    monkeypatch.setattr(backends, 'get_tts_backend_for_engine', lambda _: model)
    monkeypatch.setattr(generations, 'release_generation_memory', lambda item: released.append(item))
    monkeypatch.setattr(generation_service, 'release_generation_memory', lambda item: released.append(item))
    async def fail(*_args, **_kwargs):
        raise RuntimeError(f'{failure_stage} failed')
    if failure_stage == 'load':
        monkeypatch.setattr(backends, 'load_engine_model', fail)
    else:
        monkeypatch.setattr(profiles, 'create_voice_prompt_for_profile', fail)
    with sessions() as db:
        with pytest.raises(RuntimeError, match=f'{failure_stage} failed'):
            if operation == 'buffered':
                await generations._buffered_speech(GenerationRequest(profile_id='voice', text='Hello.'), db)
            else:
                await generation_service.generate_audio_sync(profile_id='voice', text='Hello.', language='en', engine='qwen', model_size='1.7B')
    assert released == [model]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['buffered', 'sync'])
async def test_model_memory_released_once_on_success(pipeline, monkeypatch, operation):
    _, sessions = pipeline
    released = []
    monkeypatch.setattr(generations, 'release_generation_memory', lambda item: released.append(item))
    monkeypatch.setattr(generation_service, 'release_generation_memory', lambda item: released.append(item))
    with sessions() as db:
        if operation == 'buffered':
            response = await generations._buffered_speech(GenerationRequest(profile_id='voice', text='Hello.'), db)
            assert response.media_type == 'audio/wav'
        else:
            audio = await generation_service.generate_audio_sync(profile_id='voice', text='Hello.', language='en', engine='qwen', model_size='1.7B')
            assert audio[:4] == b'RIFF'
    assert len(released) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['buffered', 'sync'])
async def test_model_memory_released_if_engine_capability_check_fails(pipeline, monkeypatch, operation):
    _, sessions = pipeline
    model = object()
    released = []
    monkeypatch.setattr(backends, 'get_tts_backend_for_engine', lambda _: model)
    monkeypatch.setattr(generations, 'release_generation_memory', lambda item: released.append(item))
    monkeypatch.setattr(generation_service, 'release_generation_memory', lambda item: released.append(item))
    def fail(_engine):
        raise RuntimeError('engine capability failed')
    monkeypatch.setattr(backends, 'engine_needs_trim', fail)
    with sessions() as db:
        with pytest.raises(RuntimeError, match='engine capability failed'):
            if operation == 'buffered':
                await generations._buffered_speech(GenerationRequest(profile_id='voice', text='Hello.'), db)
            else:
                await generation_service.generate_audio_sync(profile_id='voice', text='Hello.', language='en', engine='qwen', model_size='1.7B')
    assert released == [model]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['buffered', 'sync'])
async def test_cancel_during_load_drains_then_cleans_up(pipeline, monkeypatch, operation):
    import asyncio

    _, sessions = pipeline
    model = object()
    released = []
    started, finish = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(backends, 'get_tts_backend_for_engine', lambda _: model)
    monkeypatch.setattr(generations, 'release_generation_memory', lambda item: released.append(item))
    monkeypatch.setattr(generation_service, 'release_generation_memory', lambda item: released.append(item))
    async def load(*_args, **_kwargs):
        started.set()
        await finish.wait()
    monkeypatch.setattr(backends, 'load_engine_model', load)
    with sessions() as db:
        if operation == 'buffered':
            work = generations._buffered_speech(GenerationRequest(profile_id='voice', text='Hello.'), db)
        else:
            work = generation_service.generate_audio_sync(profile_id='voice', text='Hello.', language='en', engine='qwen', model_size='1.7B')
        task = asyncio.create_task(work)
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            assert released == []
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert released == [model]
