"""The central HTTP import endpoint preserves durable Story copy identity."""
# ruff: noqa: F811 -- pytest injects the imported SQLite/PCM fixture by name.

import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.studio.api import make_router
from backend.tests.test_studio_story_copy import setup  # noqa: F401 - shared real SQLite/PCM fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("different_source", [False, True])
async def test_copied_story_stems_keep_pinned_source_identity(setup, different_source):
    from backend.database.models import Generation, GenerationVersion, StoryItem
    from backend.studio.delivery import StudioDelivery
    from backend.studio.story import import_story

    store, engine, pinned = setup
    with Session(engine) as db:
        if different_source:
            db.add(Generation(id="another", profile_id="unknown", text="Other source", audio_path=str(pinned)))
            db.add(
                GenerationVersion(id="another-version", generation_id="another", label="Pinned", audio_path=str(pinned))
            )
            item = db.get(StoryItem, "overlap")
            item.generation_id = "another"
            item.version_id = "another-version"
            db.commit()
        project = import_story(store, "story", db, idempotency_key="stems")
    project = store.checkpoint(project["id"], "cp", 0, "Story")
    pending = []
    delivery = StudioDelivery(store, schedule=pending.append)
    delivery.submit(project["id"], "stems", project["revisions"][0]["id"], "stems", "wav", processing="dry")
    await pending.pop()
    files = store.get_project(project["id"])["deliverables"]
    assert len(files) == (2 if different_source else 1)
    combined = None
    for item in files:
        path, _ = delivery.path(item["id"])
        pcm, rate = sf.read(path)
        assert rate == 48000
        combined = pcm if combined is None else combined + pcm
    assert combined[6500, 0] == pytest.approx(0.04, abs=1e-4)
    assert combined[11600, 0] == pytest.approx(0.11, abs=1e-4)


def test_story_copy_http_replays_after_legacy_source_removed(setup, monkeypatch):
    from backend import database

    store, engine, pinned = setup

    def get_db():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(database, "get_db", get_db)
    app = FastAPI()
    app.include_router(make_router(store))
    client = TestClient(app)
    path = "/studio/v1/imports/story/story"
    first = client.post(path, json={"idempotency_key": "same-copy"})
    assert first.status_code == 200
    pinned.unlink()
    replay = client.post(path, json={"idempotency_key": "same-copy"})
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(store.list_projects()) == 1
    for invalid in [True, 1, "", "a" * 201]:
        assert client.post(path, json={"idempotency_key": invalid}).status_code == 422
