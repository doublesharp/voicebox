"""Model download requests must not schedule duplicate work."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import backends, models
from backend.routes import models as model_routes
from backend.utils.progress import ProgressManager
from backend.utils.tasks import TaskManager


@pytest.mark.asyncio
async def test_second_download_request_does_not_start_duplicate_work(monkeypatch):
    manager = TaskManager()
    progress = ProgressManager()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    created = []
    config = SimpleNamespace(model_name="model", engine="test")
    monkeypatch.setattr(model_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(model_routes, "get_progress_manager", lambda: progress)
    monkeypatch.setattr(backends, "get_model_config", lambda name: config if name == "model" else None)
    monkeypatch.setattr(backends, "get_model_load_func", lambda _config: None)

    async def model_work(_config, _load):
        calls.append("started")
        started.set()
        await release.wait()

    def create_task(coro):
        task = asyncio.create_task(coro)
        created.append(task)
        return task

    monkeypatch.setattr(model_routes, "_run_model_work", model_work)
    monkeypatch.setattr(model_routes, "create_background_task", create_task)
    request = models.ModelDownloadRequest(model_name="model")
    try:
        await model_routes.trigger_model_download(request)
        await asyncio.wait_for(started.wait(), timeout=2)
        with pytest.raises(HTTPException) as error:
            await model_routes.trigger_model_download(request)
        assert error.value.status_code == 409
        assert len(created) == 1
        assert manager.is_download_active("model")
    finally:
        release.set()
        await asyncio.gather(*created)
    assert calls == ["started"]
    assert manager.get_active_downloads() == []


@pytest.mark.asyncio
async def test_failed_download_updates_progress_and_can_be_retried(monkeypatch):
    manager, progress = TaskManager(), ProgressManager()
    tasks = []
    config = SimpleNamespace(model_name="model", engine="test")
    monkeypatch.setattr(model_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(model_routes, "get_progress_manager", lambda: progress)
    monkeypatch.setattr(backends, "get_model_config", lambda _name: config)
    monkeypatch.setattr(backends, "get_model_load_func", lambda _config: None)
    calls = 0

    async def model_work(_config, _load):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("connection lost")

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(model_routes, "_run_model_work", model_work)
    monkeypatch.setattr(model_routes, "create_background_task", create_task)
    request = models.ModelDownloadRequest(model_name="model")
    await model_routes.trigger_model_download(request)
    await tasks[-1]
    assert manager.get_active_downloads()[0].status == "error"
    assert progress.get_progress("model")["status"] == "error"
    assert progress.get_progress("model")["error"] == "connection lost"
    await model_routes.trigger_model_download(request)
    await tasks[-1]
    assert calls == 2
    assert manager.get_active_downloads() == []
    assert progress.get_progress("model")["status"] == "complete"
    assert progress.get_all_active() == []


@pytest.mark.asyncio
async def test_progress_only_download_blocks_duplicate_request(monkeypatch):
    manager, progress = TaskManager(), ProgressManager()
    progress.update_progress("model", 1, 100)
    monkeypatch.setattr(model_routes, "get_task_manager", lambda: manager)
    monkeypatch.setattr(model_routes, "get_progress_manager", lambda: progress)
    monkeypatch.setattr(backends, "get_model_config", lambda _name: SimpleNamespace(model_name="model"))
    with pytest.raises(HTTPException) as error:
        await model_routes.trigger_model_download(models.ModelDownloadRequest(model_name="model"))
    assert error.value.status_code == 409
