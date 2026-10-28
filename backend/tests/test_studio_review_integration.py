"""Real public-router coverage for the final S1 integration hooks."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studio.api import make_router
from backend.studio.archive import _validate_project
from backend.studio.storage import StudioStore


def test_review_save_export_and_import_through_central_router(tmp_path):
    store = StudioStore(tmp_path / "store")
    app = FastAPI()
    app.include_router(make_router(store))
    client = TestClient(app)
    project = client.post("/studio/v1/projects", json={"name": "Reviewed"}).json()
    project = client.post(
        f"/studio/v1/projects/{project['id']}/revisions",
        json={
            "operation_id": "checkpoint",
            "expected_revision": 0,
            "name": "First",
        },
    ).json()
    revision = project["revisions"][0]["id"]
    path = f"/studio/v1/projects/{project['id']}/reviews/{revision}"
    saved = client.post(
        path,
        json={
            "operation_id": "review",
            "expected_version": 0,
            "note": "Keep this",
            "checklist": [{"id": "pacing", "label": "Pacing", "checked": True}],
        },
    )
    assert saved.status_code == 200
    archive = client.get(f"/studio/v1/projects/{project['id']}/archive")
    assert archive.status_code == 200
    imported = client.post(
        "/studio/v1/imports/archive", files={"file": ("review.zip", archive.content, "application/zip")}
    )
    assert imported.status_code == 200
    other = imported.json()
    other_revision = other["revisions"][0]["id"]
    read = client.get(f"/studio/v1/projects/{other['id']}/reviews/{other_revision}")
    assert read.json()["review"]["note"] == "Keep this"
    assert read.json()["review"]["checklist"][0]["checked"] is True


def test_archive_validator_rejects_malformed_review(tmp_path):
    store = StudioStore(tmp_path / "store")
    project = store.create_project("Corrupt")
    project["reviews"] = None
    with pytest.raises(ValueError, match="(?i)review"):
        _validate_project(project, {a["id"]: a for a in project["assets"]})
