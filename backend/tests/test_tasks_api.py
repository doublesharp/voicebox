"""Task status and error dismissal through the registered HTTP router."""

import asyncio
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes import tasks
from backend.routes import models as model_routes
from backend.utils.progress import ProgressManager
from backend.utils.tasks import TaskManager


@pytest.fixture
def task_client(monkeypatch):
    manager = TaskManager()
    progress = ProgressManager()
    monkeypatch.setattr(tasks, "get_task_manager", lambda: manager)
    monkeypatch.setattr(tasks, "get_progress_manager", lambda: progress)
    app = FastAPI()
    app.include_router(tasks.router)
    with TestClient(app) as client:
        yield client, manager, progress


def test_active_tasks_merges_download_progress_and_generation(task_client):
    client, manager, progress = task_client
    manager.start_download("model-a")
    progress.update_progress("model-a", 25, 100, filename="weights.bin")
    progress.update_progress("progress-only", 50, 100, status="extracting")
    manager.start_download("failed")
    manager.error_download("failed", "network error")
    manager.start_generation("gen", "voice", "A" * 60)
    response = client.get("/tasks/active")
    assert response.status_code == 200
    payload = response.json()
    downloads = {row["model_name"]: row for row in payload["downloads"]}
    assert downloads["model-a"]["progress"] == 25
    assert downloads["model-a"]["filename"] == "weights.bin"
    assert downloads["progress-only"]["status"] == "extracting"
    assert downloads["failed"]["error"] == "network error"
    assert payload["generations"][0]["task_id"] == "gen"
    assert payload["generations"][0]["text_preview"] == "A" * 50 + "..."


def test_clearing_errors_preserves_running_downloads_and_generations(task_client):
    client, manager, progress = task_client
    manager.start_download("active")
    progress.update_progress("active", 40, 100)
    progress.update_progress("progress-only", 10, 100)
    manager.start_download("failed")
    manager.error_download("failed", "network error")
    # Some loader failures update the task but leave its last progress active.
    progress.update_progress("failed", 20, 100)
    progress.update_progress("finished", 100, 100, status="complete")
    manager.start_generation("gen", "voice", "hello")
    assert client.post("/tasks/clear").status_code == 200
    payload = client.get("/tasks/active").json()
    assert {row["model_name"] for row in payload["downloads"]} == {"active", "progress-only"}
    assert payload["generations"][0]["task_id"] == "gen"
    assert progress.get_progress("active")["progress"] == 40
    assert progress.get_progress("failed") is None
    assert progress.get_progress("finished") is None
    assert [task.model_name for task in manager.get_pending_downloads()] == ["active"]


def test_cache_clear_reports_success_and_failure(task_client, monkeypatch):
    client, _, _ = task_client
    monkeypatch.setattr(tasks, "clear_voice_prompt_cache", lambda: 3)
    response = client.post("/cache/clear")
    assert response.status_code == 200
    assert response.json()["files_deleted"] == 3
    def fail():
        raise OSError("unavailable")
    monkeypatch.setattr(tasks, "clear_voice_prompt_cache", fail)
    response = client.post("/cache/clear")
    assert response.status_code == 500
    assert "unavailable" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["complete", "error"])
@pytest.mark.parametrize("threaded", [False, True])
async def test_slow_progress_subscriber_receives_terminal_event(terminal, threaded):
    progress = ProgressManager()
    progress.update_progress("model", 0, 100)
    stream = progress.subscribe("model")
    initial = await anext(stream)
    assert json.loads(initial[6:])["status"] == "downloading"
    try:
        # Fill the bounded queue while the consumer is paused.
        for current in range(10, 101, 10):
            progress.update_progress("model", current, 100)
        finish = (lambda: progress.mark_complete("model")) if terminal == "complete" else (
            lambda: progress.mark_error("model", "network error")
        )
        if threaded:
            await asyncio.to_thread(finish)
        else:
            finish()
        received = []
        for _ in range(10):
            event = await asyncio.wait_for(anext(stream), timeout=2)
            received.append(json.loads(event[6:]))
            if received[-1]["status"] in ("complete", "error"):
                break
        assert received[-1]["status"] == terminal
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["complete", "error"])
async def test_late_subscriber_can_request_terminal_snapshot(terminal):
    progress = ProgressManager()
    progress.update_progress("model", 0, 100)
    if terminal == "complete":
        progress.mark_complete("model")
    else:
        progress.mark_error("model", "load failed")
    stream = progress.subscribe("model", include_terminal=True)
    event = await asyncio.wait_for(anext(stream), timeout=1)
    assert json.loads(event[6:])["status"] == terminal
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_default_subscription_ignores_stale_terminal_state():
    progress = ProgressManager()
    progress.update_progress("model", 0, 100)
    progress.mark_complete("model")
    stream = progress.subscribe("model")
    next_event = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(0)
        progress.update_progress("model", 1, 100)
        event = await asyncio.wait_for(next_event, timeout=1)
        assert json.loads(event[6:])["status"] == "downloading"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_progress_endpoint_can_report_completion_before_subscription(monkeypatch):
    progress = ProgressManager()
    progress.update_progress("model", 0, 100)
    progress.mark_complete("model")
    monkeypatch.setattr(model_routes, "get_progress_manager", lambda: progress)
    response = await model_routes.get_model_progress("model", include_terminal=True)
    event = await asyncio.wait_for(anext(response.body_iterator), timeout=1)
    assert json.loads(event[6:])["status"] == "complete"
    await response.body_iterator.aclose()
