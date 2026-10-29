"""Model admission and inference contracts without network or model weights."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend import models
from backend.backends.openai_compat_backend import OpenAICompatLLMBackend
from backend.routes import llm as route
from backend.utils.progress import ProgressManager
from backend.utils.tasks import TaskManager


@pytest.fixture
def environment(monkeypatch):
    manager, progress, tasks = TaskManager(), ProgressManager(), []
    backend = SimpleNamespace(model_size='0.6B', is_loaded=lambda: True, _is_model_cached=lambda _: True, generate=AsyncMock(return_value='polished'))
    monkeypatch.setattr(route.llm, 'get_llm_model', lambda: backend)
    monkeypatch.setattr(route, 'get_llm_model_configs', lambda: [SimpleNamespace(model_size='0.6B'), SimpleNamespace(model_size='1.7B')])
    monkeypatch.setattr(route, 'get_task_manager', lambda: manager)
    monkeypatch.setattr(route, 'get_progress_manager', lambda: progress)
    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(route, 'create_background_task', start)
    return backend, manager, progress, tasks


def request(**values):
    return models.LLMGenerateRequest(prompt='  raw text  ', **values)


@pytest.mark.asyncio
async def test_loaded_model_receives_prompt_options_and_examples(environment):
    backend, _, _, _ = environment
    result = await route.llm_generate(request(system='rules', max_tokens=42, temperature=0.2, examples=[['input', 'output']]))
    assert result.text == 'polished' and result.model_size == '0.6B'
    backend.generate.assert_awaited_once_with(prompt='  raw text  ', system='rules', max_tokens=42, temperature=0.2, model_size='0.6B', examples=[('input', 'output')])


@pytest.mark.asyncio
async def test_invalid_size_is_rejected_before_loading(environment):
    backend, manager, progress, tasks = environment
    with pytest.raises(HTTPException) as error:
        await route.llm_generate(request(model_size='4B'))
    assert error.value.status_code == 400
    backend.generate.assert_not_awaited()
    assert not tasks


@pytest.mark.asyncio
async def test_invalid_examples_are_rejected_before_model_download(environment):
    backend, manager, progress, tasks = environment
    backend.is_loaded = lambda: False
    backend._is_model_cached = lambda _: False
    backend.load_model = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await route.llm_generate(request(examples=[['one']]))
    assert error.value.status_code == 400
    assert tasks == []
    assert manager.get_active_downloads() == []


@pytest.mark.asyncio
async def test_concurrent_requests_schedule_only_one_download(environment):
    backend, manager, progress, tasks = environment
    backend.is_loaded = lambda: False
    backend._is_model_cached = lambda _: False
    release, started = asyncio.Event(), asyncio.Event()
    calls = []
    async def load(size):
        calls.append(size)
        started.set()
        await release.wait()
    backend.load_model = load
    try:
        for _ in range(2):
            response = await route.llm_generate(request())
            assert response.status_code == 202
        await asyncio.wait_for(started.wait(), 2)
        assert len(tasks) == 1
        assert calls == ['0.6B']
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert manager.get_pending_downloads() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_before_start', [True, False])
async def test_cancelled_model_load_drains_and_becomes_retryable(environment, cancel_before_start):
    backend, manager, progress, tasks = environment
    backend.is_loaded = lambda: False
    backend._is_model_cached = lambda _: False
    started, release = asyncio.Event(), asyncio.Event()
    async def load(_size):
        started.set()
        await release.wait()
    backend.load_model = load
    assert (await route.llm_generate(request())).status_code == 202
    if not cancel_before_start:
        await asyncio.wait_for(started.wait(), 2)
    tasks[0].cancel()
    if not cancel_before_start:
        await asyncio.sleep(0.05)
        waiting = not tasks[0].done()
    else:
        waiting = True
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert waiting
    assert manager.get_pending_downloads() == []
    assert manager.get_active_downloads()[0].status == 'error'


@pytest.mark.asyncio
async def test_failed_load_is_visible_and_a_later_request_can_retry(environment):
    backend, manager, progress, tasks = environment
    backend.is_loaded = lambda: False
    backend._is_model_cached = lambda _: False
    backend.load_model = AsyncMock(side_effect=[RuntimeError('failed'), None])
    assert (await route.llm_generate(request())).status_code == 202
    await tasks[-1]
    assert manager.get_active_downloads()[0].status == 'error'
    assert (await route.llm_generate(request())).status_code == 202
    await tasks[-1]
    assert manager.get_active_downloads() == []


@pytest.mark.asyncio
async def test_local_exception_does_not_expose_private_details(environment):
    backend, _, _, _ = environment
    backend.generate = AsyncMock(side_effect=RuntimeError('/private/secret model failure'))
    with pytest.raises(HTTPException) as error:
        await route.llm_generate(request())
    assert error.value.status_code == 500
    assert error.value.detail == 'LLM generation failed'


@pytest.mark.asyncio
async def test_remote_backend_uses_remote_model_and_reports_provider_error(environment, monkeypatch):
    remote = OpenAICompatLLMBackend('https://example.invalid/v1', 'remote-model')
    remote.generate = AsyncMock(side_effect=RuntimeError('upstream unavailable'))
    monkeypatch.setattr(route.llm, 'get_llm_model', lambda: remote)
    with pytest.raises(HTTPException) as error:
        await route.llm_generate(request())
    assert error.value.status_code == 502
    assert error.value.detail == 'upstream unavailable'
    assert remote.generate.await_args.kwargs['model_size'] == 'remote-model'


def test_llm_http_contract_for_loaded_model(environment):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(route.router)
    with TestClient(app) as client:
        response = client.post('/llm/generate', json={'prompt': 'hello'})
    assert response.status_code == 200
    assert response.json() == {'text': 'polished', 'model_size': '0.6B'}


@pytest.mark.asyncio
async def test_progress_only_download_is_not_queued_again(environment):
    backend, manager, progress, tasks = environment
    backend.is_loaded = lambda: False
    backend._is_model_cached = lambda _: False
    backend.load_model = AsyncMock()
    progress.update_progress('qwen3-0.6b', 10, 100)
    assert (await route.llm_generate(request())).status_code == 202
    assert tasks == []
    backend.load_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduling_failure_is_safe_and_marks_download_failed(environment, monkeypatch):
    backend, manager, progress, tasks = environment
    backend.is_loaded = lambda: False
    backend._is_model_cached = lambda _: False
    backend.load_model = AsyncMock()
    def fail(_coro):
        raise RuntimeError('/private/scheduler error')
    monkeypatch.setattr(route, 'create_background_task', fail)
    with pytest.raises(HTTPException) as error:
        await route.llm_generate(request())
    assert error.value.status_code == 500
    assert error.value.detail == 'LLM download could not be started'
    assert manager.get_active_downloads()[0].status == 'error'


@pytest.mark.asyncio
async def test_invalid_backend_response_is_reported_without_private_details(environment):
    backend, _, _, _ = environment
    backend.generate = AsyncMock(return_value={'private': 'unexpected'})
    with pytest.raises(HTTPException) as error:
        await route.llm_generate(request())
    assert error.value.status_code == 500
    assert error.value.detail == 'LLM generation failed'
