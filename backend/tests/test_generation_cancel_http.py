"""Generation cancellation through HTTP with independent SQLite sessions."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database import Base, Generation, VoiceProfile, get_db
from backend.routes import generations


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'generations.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.add(Generation(id="gen", profile_id="voice", text="Hello", status="generating"))
        db.commit()

    class Tasks:
        def __init__(self):
            self.completed = []

        def complete_generation(self, generation_id):
            self.completed.append(generation_id)

    tasks = Tasks()
    monkeypatch.setattr(generations, "get_task_manager", lambda: tasks)
    app = FastAPI()
    app.include_router(generations.router)

    def session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        yield client, engine, tasks
    engine.dispose()


def current(engine):
    with Session(engine) as db:
        row = db.get(Generation, "gen")
        return row.status, row.error, row.audio_path, row.duration


def test_missing_generation_is_not_cancelled(api, monkeypatch):
    client, _, tasks = api
    monkeypatch.setattr(generations, "cancel_generation_job", lambda _: pytest.fail("queue must not be called"))
    assert client.post("/generate/missing/cancel").status_code == 404
    assert tasks.completed == []


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_generation_cannot_be_cancelled(api, monkeypatch, status):
    client, engine, tasks = api
    with Session(engine) as db:
        db.get(Generation, "gen").status = status
        db.commit()
    monkeypatch.setattr(generations, "cancel_generation_job", lambda _: pytest.fail("queue must not be called"))
    assert client.post("/generate/gen/cancel").status_code == 400
    assert current(engine)[0] == status
    assert tasks.completed == []


@pytest.mark.parametrize("status", ["generating", "loading_model"])
@pytest.mark.parametrize(("queue_state", "message", "error"), [
    (None, "Orphaned generation cleared", "Generation orphaned by worker"),
    ("queued", "Queued generation cancelled", "Generation cancelled"),
])
def test_queued_or_orphaned_generation_is_marked_failed(api, monkeypatch, status, queue_state, message, error):
    client, engine, tasks = api
    with Session(engine) as db:
        row = db.get(Generation, "gen")
        row.status = status
        row.audio_path = "previous.wav"
        row.duration = 1.25
        db.commit()
    monkeypatch.setattr(generations, "cancel_generation_job", lambda _: queue_state)
    response = client.post("/generate/gen/cancel")
    assert response.status_code == 200
    assert response.json() == {"message": message}
    assert current(engine) == ("failed", error, "previous.wav", 1.25)
    assert tasks.completed == ["gen"]


def test_running_generation_waits_for_worker_to_report_terminal_status(api, monkeypatch):
    client, engine, tasks = api
    monkeypatch.setattr(generations, "cancel_generation_job", lambda _: "running")
    response = client.post("/generate/gen/cancel")
    assert response.status_code == 200
    assert response.json() == {"message": "Generation cancellation requested"}
    assert current(engine)[0] == "generating"
    assert tasks.completed == []


@pytest.mark.parametrize("queue_state", [None, "queued"])
def test_finished_generation_is_not_overwritten_by_a_late_cancel(api, monkeypatch, queue_state):
    client, engine, tasks = api

    def completed_before_queue_check(generation_id):
        assert generation_id == "gen"
        with Session(engine) as other:
            row = other.get(Generation, generation_id)
            row.status = "completed"
            row.audio_path = "finished.wav"
            row.duration = 2.5
            other.commit()
        return queue_state

    monkeypatch.setattr(generations, "cancel_generation_job", completed_before_queue_check)
    response = client.post("/generate/gen/cancel")
    assert current(engine) == ("completed", None, "finished.wav", 2.5)
    assert response.status_code == 409
    assert tasks.completed == []


@pytest.mark.parametrize("queue_state", [None, "queued"])
def test_generation_deleted_during_cancel_does_not_clear_task_state(api, monkeypatch, queue_state):
    client, engine, tasks = api

    def delete_before_queue_check(generation_id):
        with Session(engine) as other:
            other.delete(other.get(Generation, generation_id))
            other.commit()
        return queue_state

    monkeypatch.setattr(generations, "cancel_generation_job", delete_before_queue_check)
    assert client.post("/generate/gen/cancel").status_code == 409
    with Session(engine) as db:
        assert db.get(Generation, "gen") is None
    assert tasks.completed == []


def test_cancelled_generation_status_stream_reports_terminal_failure(api, monkeypatch):
    import json

    client, _, tasks = api
    monkeypatch.setattr(generations, "cancel_generation_job", lambda _: "queued")
    assert client.post("/generate/gen/cancel").status_code == 200
    response = client.get("/generate/gen/status")
    assert response.status_code == 200
    events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["error"] == "Generation cancelled"
    assert tasks.completed == ["gen"]


def test_database_failure_does_not_clear_task_or_commit_partial_cancel(api, monkeypatch):
    client, engine, tasks = api
    monkeypatch.setattr(generations, "cancel_generation_job", lambda _: "queued")

    def failing_session():
        with Session(engine) as db:
            def fail_commit():
                raise RuntimeError("database unavailable")
            monkeypatch.setattr(db, "commit", fail_commit)
            yield db

    client.app.dependency_overrides[get_db] = failing_session
    with TestClient(client.app, raise_server_exceptions=False) as failing_client:
        response = failing_client.post("/generate/gen/cancel")
    assert response.status_code == 500
    assert current(engine)[0] == "generating"
    assert tasks.completed == []
