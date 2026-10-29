"""Avatar persistence across successful and failed database mutations."""

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Generation, VoiceProfile
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
def images(tmp_path):
    paths = []
    for color in ("red", "blue"):
        path = tmp_path / f"{color}.png"
        Image.new("RGB", (32, 32), color).save(path)
        paths.append(path)
    return paths


async def test_failed_avatar_replacement_keeps_previous_image(db, images, monkeypatch):
    first = await profiles.upload_avatar("voice", str(images[0]), db)
    original = config.resolve_storage_path(first.avatar_path)
    content = original.read_bytes()

    def fail_commit():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await profiles.upload_avatar("voice", str(images[1]), db)
    db.rollback()
    assert config.resolve_storage_path(db.get(VoiceProfile, "voice").avatar_path) == original
    assert original.read_bytes() == content
    assert [p for p in original.parent.iterdir() if p.is_file()] == [original]


async def test_failed_avatar_deletion_keeps_previous_image(db, images, monkeypatch):
    first = await profiles.upload_avatar("voice", str(images[0]), db)
    original = config.resolve_storage_path(first.avatar_path)
    content = original.read_bytes()

    def fail_commit():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await profiles.delete_avatar("voice", db)
    db.rollback()
    assert config.resolve_storage_path(db.get(VoiceProfile, "voice").avatar_path) == original
    assert original.read_bytes() == content


async def test_avatar_replace_and_delete_remove_only_unshared_files(db, images):
    first = await profiles.upload_avatar("voice", str(images[0]), db)
    old = config.resolve_storage_path(first.avatar_path)
    second = await profiles.upload_avatar("voice", str(images[1]), db)
    new = config.resolve_storage_path(second.avatar_path)
    assert new.is_file()
    assert new != old
    assert not old.exists()
    assert await profiles.delete_avatar("voice", db) is True
    assert db.get(VoiceProfile, "voice").avatar_path is None
    assert not new.exists()
    assert await profiles.delete_avatar("voice", db) is False


async def test_avatar_processing_failure_removes_partial_file(db, images, monkeypatch):
    first = await profiles.upload_avatar("voice", str(images[0]), db)
    original = config.resolve_storage_path(first.avatar_path)
    content = original.read_bytes()

    def fail_processing(_source, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"partial")
        raise OSError("disk full")
    monkeypatch.setattr(profiles, "process_avatar", fail_processing)
    with pytest.raises(OSError, match="disk full"):
        await profiles.upload_avatar("voice", str(images[1]), db)
    assert original.read_bytes() == content
    assert list(original.parent.iterdir()) == [original]
    assert db.get(VoiceProfile, "voice").avatar_path == first.avatar_path


async def test_avatar_replacement_keeps_file_referenced_by_generation(db, images):
    first = await profiles.upload_avatar("voice", str(images[0]), db)
    original = config.resolve_storage_path(first.avatar_path)
    db.add(Generation(id="generation", profile_id="voice", text="hello", audio_path=str(original)))
    db.commit()
    await profiles.upload_avatar("voice", str(images[1]), db)
    assert original.is_file()
