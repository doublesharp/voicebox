"""Profile sample persistence and failure recovery with real SQLite and audio."""

import numpy as np
import pytest
import soundfile as sf
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, ProfileSample, VoiceProfile
from backend.services import profiles


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(VoiceProfile(id="voice", name="Voice"))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "source.wav"
    samples = 0.25 * np.sin(2 * np.pi * 220 * np.arange(48000) / 16000)
    sf.write(path, samples, 16000)
    return path


async def test_failed_sample_delete_preserves_audio_and_record(db, audio, monkeypatch):
    sample = await profiles.add_profile_sample("voice", str(audio), "words", db)
    saved_audio = config.resolve_storage_path(sample.audio_path)
    original = saved_audio.read_bytes()

    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await profiles.delete_profile_sample(sample.id, db)
    db.rollback()
    assert db.get(ProfileSample, sample.id) is not None
    assert saved_audio.read_bytes() == original


async def test_failed_sample_add_leaves_no_audio_or_record(db, audio, monkeypatch):
    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await profiles.add_profile_sample("voice", str(audio), "words", db)
    db.rollback()
    assert db.query(ProfileSample).count() == 0
    assert list((config.get_profiles_dir() / "voice").glob("*.wav")) == []


async def test_sample_edit_and_delete_invalidate_only_own_profile_cache(db, audio, monkeypatch):
    invalidated = []
    monkeypatch.setattr(profiles, "clear_profile_cache", invalidated.append)
    sample = await profiles.add_profile_sample("voice", str(audio), "first", db)
    saved_audio = config.resolve_storage_path(sample.audio_path)
    assert saved_audio.is_file()
    assert (await profiles.update_profile_sample(sample.id, "revised", db)).reference_text == "revised"
    assert (await profiles.get_profile_samples("voice", db))[0].reference_text == "revised"
    assert await profiles.delete_profile_sample(sample.id, db) is True
    assert not saved_audio.exists()
    assert await profiles.delete_profile_sample(sample.id, db) is False
    assert await profiles.update_profile_sample(sample.id, "none", db) is None
    assert invalidated == ["voice", "voice", "voice"]


async def test_partial_sample_write_is_removed_on_error(db, audio, monkeypatch):
    def fail_save(_audio, path, _rate):
        Path(path).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(profiles, "save_audio", fail_save)
    with pytest.raises(OSError, match="disk full"):
        await profiles.add_profile_sample("voice", str(audio), "words", db)
    assert db.query(ProfileSample).count() == 0
    assert list((config.get_profiles_dir() / "voice").glob("*.wav")) == []


async def test_delete_succeeds_when_post_commit_file_cleanup_fails(db, audio, monkeypatch, caplog):
    sample = await profiles.add_profile_sample("voice", str(audio), "words", db)
    saved_audio = config.resolve_storage_path(sample.audio_path)
    original_unlink = Path.unlink

    def fail_sample_unlink(path, *args, **kwargs):
        if path == saved_audio:
            raise PermissionError("denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_sample_unlink)
    assert await profiles.delete_profile_sample(sample.id, db) is True
    assert db.get(ProfileSample, sample.id) is None
    assert saved_audio.exists()
    assert "Could not remove deleted profile sample audio" in caplog.text
