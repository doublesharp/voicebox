import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.migrations import MIGRATIONS
from backend.studio.storage import StudioStore


def test_version_five_projects_receive_tokens_without_losing_saved_content(tmp_path):
    with sqlite3.connect(tmp_path / "studio.sqlite") as db:
        for version in range(1, 6):
            for sql in MIGRATIONS[version]:
                db.execute(sql)
        db.execute("PRAGMA user_version=5")
        db.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)",
            ("old", "Saved", None, 2, "now", "now", '{"passages":[]}', "[]", "[]"),
        )
    store = StudioStore(tmp_path)
    result, token = store.poll_project("old")
    assert result["name"] == "Saved"
    assert result["revision"] == 2
    assert result["document"] == {"passages": []}
    assert store.poll_project("old", token) == (None, token)


def test_tokens_are_project_scoped_and_rollback_with_their_data(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo")
    _, token = store.poll_project(project["id"])
    store.create_project("Unrelated")
    assert store.poll_project(project["id"], token) == (None, token)

    def rollback():
        with store.connect() as db:
            db.execute("UPDATE projects SET name='Rolled back' WHERE id=?", (project["id"],))
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError):
        rollback()
    assert store.poll_project(project["id"], token) == (None, token)


def test_unchanged_conditional_poll_skips_project_projection(tmp_path, monkeypatch):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo", script="Hello")
    app = FastAPI()
    app.include_router(make_router(store))
    with TestClient(app) as client:
        url = f"/studio/v1/projects/{project['id']}?compact=true"
        first = client.get(url)
        token = first.json()["change_token"]
        monkeypatch.setattr(store, "_project", lambda *a, **k: pytest.fail("project rebuilt during unchanged poll"))
        assert client.get(url, headers={"If-None-Match": token}).status_code == 304


def test_poll_token_covers_phase_updates_and_assets_without_document_revision(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo")
    app = FastAPI()
    app.include_router(make_router(store))
    with TestClient(app) as client:
        url = f"/studio/v1/projects/{project['id']}?compact=true"
        first = client.get(url).json()
        with store.connect() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("j", project["id"], "generation", "running", None, None, None, "now", "now", "{}"),
            )
        second = client.get(url, headers={"If-None-Match": first["change_token"]})
        assert second.status_code == 200
        assert second.json()["revision"] == first["revision"]
        with store.connect() as db:
            db.execute("INSERT INTO job_attempts VALUES (?,?,?,?,?,NULL)", ("j", 1, "loading", "now", "now"))
        third = client.get(url, headers={"If-None-Match": second.json()["change_token"]})
        assert third.status_code == 200
        assert third.json()["jobs"][0]["phase"] == "loading"
        with store.connect() as db:
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
                ("a", project["id"], "0" * 64, 48000, 1, 48, 0.001, 192, "a"),
            )
        fourth = client.get(url, headers={"If-None-Match": third.json()["change_token"]})
        assert fourth.status_code == 200
        assert fourth.json()["assets"][0]["id"] == "a"


def test_compact_checkpoints_fetch_documents_only_on_demand(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo", script="Frozen words")
    project = store.checkpoint(project["id"], "checkpoint", 0, "Review")
    revision = project["revisions"][0]
    app = FastAPI()
    app.include_router(make_router(store))
    with TestClient(app) as client:
        url = f"/studio/v1/projects/{project['id']}"
        assert "document" not in client.get(url + "?compact=true").json()["revisions"][0]
        assert client.get(url).json()["revisions"][0]["document"] == revision["document"]
        detail = client.get(f"{url}/revisions/{revision['id']}")
        assert detail.json()["document"] == revision["document"]
        other = store.create_project("Other")
        assert client.get(f"/studio/v1/projects/{other['id']}/revisions/{revision['id']}").status_code == 404


def test_project_job_projection_uses_constant_queries(tmp_path):
    store = StudioStore(tmp_path)
    project = store.create_project("Demo")
    with store.connect() as db:
        for index in range(20):
            job = f"j{index}"
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job, project["id"], "generation", "running", None, None, None, "now", "now", "{}"),
            )
            db.execute("INSERT INTO job_attempts VALUES (?,?,?,?,?,NULL)", (job, 1, "loading", "now", "now"))
            db.execute(
                "INSERT INTO job_phase_events (job_id,attempt,phase,created_at) VALUES (?,?,?,?)",
                (job, 1, "loading", "now"),
            )
        statements = []
        db.set_trace_callback(statements.append)
        result = store._project(db, project["id"])
        assert result["jobs"][0]["attempts"][0]["phases"][0]["phase"] == "loading"
        assert len([sql for sql in statements if sql.startswith("SELECT")]) <= 10
