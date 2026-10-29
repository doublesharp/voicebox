import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from backend import config, models
from backend.database import Base, Generation, VoiceProfile
from backend.database.migrations import _migrate_generations
from backend.routes import generations

OPTIONS = {
    "normalize": False,
    "max_chunk_chars": 150,
    "crossfade_ms": 0,
    "effects_chain": [{"type": "gain", "params": {"gain_db": -6}}],
}


@pytest.fixture
def replay(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)

        async def job():
            pass

        return job()

    monkeypatch.setattr(generations, "run_generation", capture)
    monkeypatch.setattr(generations, "enqueue_generation", lambda _id, coro: coro.close())
    with Session(engine) as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.commit()
        yield db, calls
    engine.dispose()


async def test_generation_persists_its_effective_settings_before_enqueue(replay):
    db, calls = replay
    response = await generations.generate_speech(
        models.GenerationRequest(profile_id="voice", text="Hello", **OPTIONS), db
    )
    db.expire_all()
    row = db.get(Generation, response.id)
    saved = getattr(row, "synthesis_config", None)
    assert saved is not None, "generation settings must survive reload before the worker starts"
    stored = json.loads(saved)
    for key in ["normalize", "max_chunk_chars", "crossfade_ms"]:
        assert stored[key] == OPTIONS[key]
    assert stored["effects_chain"][0]["params"] == {"gain_db": -6}


@pytest.mark.parametrize(("action", "status"), [("retry", "failed"), ("regenerate", "completed")])
async def test_retry_and_take_restore_settings_instead_of_defaults(replay, action, status):
    db, calls = replay
    row = Generation(id="gen", profile_id="voice", text="Hello", status=status, seed=3)
    row.synthesis_config = json.dumps(OPTIONS)
    db.add(row)
    db.commit()
    await getattr(generations, f"{action}_generation")("gen", db)
    for key, value in OPTIONS.items():
        assert calls[0].get(key) == value, f"{action} lost {key}"


def test_old_database_migration_preserves_rows_and_is_repeatable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE generations (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("INSERT INTO generations (id) VALUES ('kept')"))
    for _ in range(2):
        _migrate_generations(engine, inspect(engine), {"generations"})
    columns = {column["name"] for column in inspect(engine).get_columns("generations")}
    assert "synthesis_config" in columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM generations")).scalar() == "kept"
    engine.dispose()


async def test_profile_effects_are_snapshotted_before_the_profile_can_change(replay):
    db, calls = replay
    db.get(VoiceProfile, "voice").effects_chain = json.dumps(OPTIONS["effects_chain"])
    db.commit()
    response = await generations.generate_speech(models.GenerationRequest(profile_id="voice", text="Hello"), db)
    db.get(VoiceProfile, "voice").effects_chain = json.dumps([])
    db.commit()
    stored = json.loads(db.get(Generation, response.id).synthesis_config)
    assert stored["effects_chain"] == calls[0]["effects_chain"] == OPTIONS["effects_chain"]


@pytest.mark.parametrize(("action", "status"), [("retry", "failed"), ("regenerate", "completed")])
async def test_legacy_replay_uses_persisted_api_defaults_not_changed_profile_effects(replay, action, status):
    db, calls = replay
    db.get(VoiceProfile, "voice").effects_chain = json.dumps(OPTIONS["effects_chain"])
    db.add(Generation(id="legacy", profile_id="voice", text="Hello", status=status))
    db.commit()
    await getattr(generations, f"{action}_generation")("legacy", db)
    expected = {"normalize": True, "max_chunk_chars": 800, "crossfade_ms": 50, "effects_chain": None}
    for key, value in expected.items():
        assert calls[0][key] == value
    stored = json.loads(db.get(Generation, "legacy").synthesis_config)
    for key, value in expected.items():
        assert stored[key] == value


async def test_invalid_snapshot_does_not_mark_the_generation_running(replay):
    from fastapi import HTTPException

    db, calls = replay
    db.add(Generation(id="invalid", profile_id="voice", text="Hello", status="failed", synthesis_config="not json"))
    db.commit()
    with pytest.raises(HTTPException) as error:
        await generations.retry_generation("invalid", db)
    assert error.value.status_code == 400
    assert db.get(Generation, "invalid").status == "failed"
    assert calls == []


@pytest.mark.parametrize("action", ["generate", "retry", "regenerate"])
async def test_enqueue_failure_does_not_leave_generation_active(replay, monkeypatch, action):
    from backend.utils.tasks import TaskManager

    db, _ = replay
    task_manager = TaskManager()
    monkeypatch.setattr(generations, "get_task_manager", lambda: task_manager)
    def reject(_generation_id, coro):
        coro.close()
        raise RuntimeError("queue unavailable")
    monkeypatch.setattr(generations, "enqueue_generation", reject)

    if action == "generate":
        call = generations.generate_speech(models.GenerationRequest(profile_id="voice", text="Hello"), db)
    else:
        status = "failed" if action == "retry" else "completed"
        db.add(Generation(id="gen", profile_id="voice", text="Hello", status=status,
                          error="old failure" if action == "retry" else None,
                          audio_path="preserved.wav", duration=1.25))
        db.commit()
        call = getattr(generations, f"{action}_generation")("gen", db)

    with pytest.raises(HTTPException) as error:
        await call
    assert error.value.status_code == 503
    assert task_manager.get_active_generations() == []
    db.expire_all()
    row = db.query(Generation).one()
    if action == "generate":
        assert row.status == "failed"
        assert row.error
    else:
        assert row.status == ("failed" if action == "retry" else "completed")
        assert row.audio_path == "preserved.wav"
        assert row.duration == 1.25
        assert row.error == ("old failure" if action == "retry" else None)


async def test_rejected_retry_does_not_remove_existing_task_tracking(replay, monkeypatch):
    from backend.utils.tasks import TaskManager

    db, _ = replay
    db.add(Generation(id="gen", profile_id="voice", text="Hello", status="failed"))
    db.commit()
    task_manager = TaskManager()
    task_manager.start_generation("gen", "voice", "already running")
    monkeypatch.setattr(generations, "get_task_manager", lambda: task_manager)
    def reject(_generation_id, coro):
        coro.close()
        raise ValueError("already queued or running")
    monkeypatch.setattr(generations, "enqueue_generation", reject)
    with pytest.raises(HTTPException):
        await generations.retry_generation("gen", db)
    assert task_manager.is_generation_active("gen")
    assert task_manager.get_active_generations()[0].text_preview == "already running"
