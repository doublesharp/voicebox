"""A copied Story owns its audio and replays one durable import intent."""

import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database.models import Base, Generation, GenerationVersion, Story, StoryItem
from backend.studio.storage import StudioStore
from backend.studio.story import import_story


@pytest.fixture
def setup(tmp_path):
    original = tmp_path / "original.wav"
    pinned = tmp_path / "pinned.wav"
    sf.write(original, np.zeros(4800), 24000)
    sf.write(pinned, np.full(9600, 0.1), 24000)
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.sqlite"))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                Story(id="story", name="My Story"),
                Generation(id="generation", profile_id="profile", text="Recorded words", audio_path=str(original)),
                GenerationVersion(id="pinned", generation_id="generation", label="Chosen", audio_path=str(pinned)),
                StoryItem(
                    id="item",
                    story_id="story",
                    generation_id="generation",
                    version_id="pinned",
                    start_time_ms=125,
                    trim_start_ms=20,
                    trim_end_ms=30,
                    volume=0.4,
                ),
                StoryItem(
                    id="overlap",
                    story_id="story",
                    generation_id="generation",
                    version_id="pinned",
                    start_time_ms=225,
                    volume=0.7,
                ),
            ]
        )
        db.commit()
    store = StudioStore(tmp_path / "studio")
    yield store, engine, pinned
    engine.dispose()


def test_lost_acknowledgement_replays_original_copy_without_reading_legacy(setup):
    store, engine, pinned = setup
    with Session(engine) as db:
        first = import_story(store, "story", db, idempotency_key="one-intent")
    # A later edit or removed legacy source cannot change the acknowledged copy.
    pinned.unlink()
    with Session(engine) as db:
        db.query(Story).filter_by(id="story").delete()
        db.commit()
        replay = import_story(store, "story", db, idempotency_key="one-intent")
    assert replay == first
    assert len(store.list_projects()) == 1
    assert len(list(store.asset_dir.iterdir())) == 2
    clips = first["document"]["clips"]
    assert [(c["start_sample"], c["gain"]) for c in clips] == [(6000, 0.4), (10800, 0.7)]
    assert clips[0]["trim_start_sample"] == 960
    assert clips[0]["trim_end_sample"] == 1440
    assert sf.info(store.asset_path(clips[0]["asset_id"])).frames == 9600


def test_concurrent_retry_publishes_exactly_one_copy(setup):
    store, engine, pinned = setup

    def copy(_):
        with Session(engine) as db:
            return import_story(store, "story", db, idempotency_key="same-intent")

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(copy, range(2)))
    assert results[0] == results[1]
    assert len(store.list_projects()) == 1
    assert len(list(store.asset_dir.iterdir())) == 2
    assert pinned.is_file()


def test_new_intent_and_legacy_calls_make_independent_copies(setup):
    store, engine, pinned = setup
    with Session(engine) as db:
        copies = [import_story(store, "story", db, idempotency_key=key) for key in ("a", "b", None, None)]
        assert db.get(Story, "story").name == "My Story"
        assert db.get(StoryItem, "item").version_id == "pinned"
        assert db.get(StoryItem, "item").volume == 0.4
    assert len({p["id"] for p in copies}) == 4
    assert pinned.is_file()


def test_registration_failure_rolls_back_copy_and_same_intent_can_retry(setup, monkeypatch):
    from backend.studio import story

    store, engine, pinned = setup
    original = story.publish_audio
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("destination full")
        return original(*args, **kwargs)

    monkeypatch.setattr(story, "publish_audio", fail_second)
    with Session(engine) as db, pytest.raises(OSError, match="destination full"):
        import_story(store, "story", db, idempotency_key="retry")
    assert store.list_projects() == []
    assert list(store.asset_dir.iterdir()) == []
    monkeypatch.setattr(story, "publish_audio", original)
    with Session(engine) as db:
        project = import_story(store, "story", db, idempotency_key="retry")
    assert len(project["assets"]) == 2
    assert pinned.is_file()


def test_uncertain_successful_commit_preserves_owned_audio_and_replays(setup, monkeypatch):
    store, engine, pinned = setup
    connect = store.connect

    class UncertainCommit:
        def __init__(self):
            self.db = connect()

        def __enter__(self):
            return self.db.__enter__()

        def __exit__(self, kind, error, traceback):
            wrote = self.db.total_changes > 0
            self.db.__exit__(kind, error, traceback)
            if kind is None and wrote:
                raise OSError("commit acknowledgement lost")

    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", UncertainCommit)
        with Session(engine) as db, pytest.raises(OSError, match="acknowledgement lost"):
            import_story(store, "story", db, idempotency_key="uncertain")
    assert len(store.list_projects()) == 1
    with Session(engine) as db:
        replay = import_story(store, "story", db, idempotency_key="uncertain")
    assert len(replay["assets"]) == 2
    for asset in replay["assets"]:
        assert store.asset_path(asset["id"]).read_bytes() == pinned.read_bytes()


def test_mismatched_recorded_identity_cannot_publish_another_copy(setup):
    from backend.studio.storage import Conflict

    store, engine, _ = setup
    with Session(engine) as db:
        original = import_story(store, "story", db, idempotency_key="same")
    with store.connect() as db:
        request = json.loads(
            db.execute("SELECT request FROM operations WHERE project_id=?", (original["id"],)).fetchone()[0]
        )
        request["story_id"] = "different"
        db.execute("UPDATE operations SET request=? WHERE project_id=?", (json.dumps(request), original["id"]))
    with Session(engine) as db, pytest.raises(Conflict):
        import_story(store, "story", db, idempotency_key="same")
    assert len(store.list_projects()) == 1


@pytest.mark.parametrize("key", ["", " ", 2, True, "a" * 201])
def test_invalid_import_identity_does_not_publish(setup, key):
    store, engine, _ = setup
    with Session(engine) as db, pytest.raises(ValueError, match="identity"):
        import_story(store, "story", db, idempotency_key=key)
    assert store.list_projects() == []
