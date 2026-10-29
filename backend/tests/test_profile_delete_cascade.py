"""
Regression tests for DELETE /profiles/{id} cleaning up everything it owns.

``delete_profile`` used to remove only the profile row, its samples, and the
profile directory. Generations carry a non-null FK to the profile and
``list_generations`` inner-joins profiles, so every generation made with a
deleted profile turned into a row the UI could never show plus a ``.wav`` in
``data/generations`` the user could never reclaim — unbounded disk growth with
no way out. Version rows, story items, and channel mappings leaked the same way.

Usage:
    python -m pytest backend/tests/test_profile_delete_cascade.py -v
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Repo root on sys.path so ``backend`` imports as a package (the services use
# package-relative imports).
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backend import config
from backend.database import (
    Base,
    CaptureSettings,
    Generation,
    GenerationVersion,
    MCPClientBinding,
    ProfileChannelMapping,
    ProfileSample,
    Story,
    StoryItem,
    VoiceProfile,
)
from backend.models import HistoryQuery
from backend.services import history, profiles


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A session backed by a temp SQLite file, with the data dir redirected."""
    monkeypatch.setattr(config, "_data_dir", tmp_path)

    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    yield session

    session.close()


def _write_wav(name: str) -> tuple[Path, str]:
    """Create a placeholder audio file and return (abs path, stored path)."""
    path = config.get_generations_dir() / name
    path.write_bytes(b"RIFF placeholder")
    return path, f"generations/{name}"


def _make_profile(db, profile_id: str = "profile-1") -> VoiceProfile:
    profile = VoiceProfile(id=profile_id, name=f"Voice {profile_id}", language="en")
    db.add(profile)
    db.commit()
    return profile


@pytest.mark.asyncio
async def test_delete_profile_removes_generations_and_audio(db):
    """Generations, versions, and their files go with the profile."""
    _make_profile(db)
    gen_file, gen_stored = _write_wav("gen.wav")
    version_file, version_stored = _write_wav("gen-reverb.wav")

    db.add(Generation(id="gen-1", profile_id="profile-1", text="hello", audio_path=gen_stored))
    db.add(
        GenerationVersion(
            id="version-1",
            generation_id="gen-1",
            label="Reverb",
            audio_path=version_stored,
        )
    )
    db.commit()

    assert await profiles.delete_profile("profile-1", db) is True

    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert not gen_file.exists()
    assert not version_file.exists()


@pytest.mark.asyncio
async def test_orphaned_generations_are_invisible_in_history(db):
    """The history join hides orphans, so leaving them behind strands them."""
    _make_profile(db)
    _, gen_stored = _write_wav("gen.wav")
    db.add(Generation(id="gen-1", profile_id="profile-1", text="hello", audio_path=gen_stored))
    db.commit()

    assert (await history.list_generations(HistoryQuery(limit=50, offset=0), db)).total == 1

    await profiles.delete_profile("profile-1", db)

    listed = await history.list_generations(HistoryQuery(limit=50, offset=0), db)
    assert listed.total == 0
    assert db.query(Generation).count() == 0, "generation is unreachable but still on disk"


@pytest.mark.asyncio
async def test_delete_profile_removes_story_items(db):
    """Story items reference generations; the story query would hide leftovers."""
    _make_profile(db)
    _, gen_stored = _write_wav("gen.wav")
    db.add(Generation(id="gen-1", profile_id="profile-1", text="hello", audio_path=gen_stored))
    db.add(Story(id="story-1", name="Chapter one"))
    db.add(StoryItem(id="item-1", story_id="story-1", generation_id="gen-1"))
    db.commit()

    await profiles.delete_profile("profile-1", db)

    assert db.query(StoryItem).count() == 0
    assert db.query(Story).count() == 1, "the story itself must survive"


@pytest.mark.asyncio
async def test_delete_profile_clears_references_to_it(db):
    """Samples, channel mappings, and default-voice pointers are cleaned up."""
    _make_profile(db)
    db.add(ProfileSample(id="sample-1", profile_id="profile-1", audio_path="p.wav", reference_text="hi"))
    db.add(ProfileChannelMapping(profile_id="profile-1", channel_id="channel-1"))
    db.add(MCPClientBinding(client_id="claude-code", profile_id="profile-1"))
    db.add(CaptureSettings(id=1, default_playback_voice_id="profile-1"))
    db.commit()

    await profiles.delete_profile("profile-1", db)

    assert db.query(ProfileSample).count() == 0
    assert db.query(ProfileChannelMapping).count() == 0
    assert db.query(MCPClientBinding).filter_by(client_id="claude-code").one().profile_id is None
    assert db.query(CaptureSettings).filter_by(id=1).one().default_playback_voice_id is None


def _lock(monkeypatch, locked: Path) -> None:
    """Make ``locked`` refuse deletion, the way Windows does during playback."""
    real_unlink = Path.unlink

    def refuse_locked_file(self, *args, **kwargs):
        if self == locked:
            raise OSError("file is in use by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_locked_file)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["generation", "failed", "profile_generations", "profile"])
async def test_failed_delete_preserves_generation_version_audio_and_story(db, monkeypatch, operation):
    _make_profile(db)
    gen_file, gen_stored = _write_wav("gen.wav")
    version_file, version_stored = _write_wav("gen-take.wav")
    db.add(Generation(id="gen-1", profile_id="profile-1", text="hello", audio_path=gen_stored, status="failed"))
    db.add(GenerationVersion(id="version-1", generation_id="gen-1", label="Take", audio_path=version_stored))
    db.add(Story(id="story-1", name="Story"))
    db.add(StoryItem(id="item-1", story_id="story-1", generation_id="gen-1"))
    db.commit()
    gen_bytes, version_bytes = gen_file.read_bytes(), version_file.read_bytes()

    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        if operation == "generation":
            await history.delete_generation("gen-1", db)
        elif operation == "failed":
            await history.delete_failed_generations(db)
        elif operation == "profile_generations":
            await history.delete_generations_by_profile("profile-1", db)
        else:
            await profiles.delete_profile("profile-1", db)
    db.rollback()
    assert db.get(VoiceProfile, "profile-1") is not None
    assert db.get(Generation, "gen-1") is not None
    assert db.get(GenerationVersion, "version-1") is not None
    assert db.get(StoryItem, "item-1") is not None
    assert gen_file.read_bytes() == gen_bytes
    assert version_file.read_bytes() == version_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["generation", "version", "sample"])
async def test_deleting_generation_preserves_audio_referenced_by_other_rows(db, reference):
    _make_profile(db)
    shared, stored = _write_wav("shared.wav")
    db.add_all([
        Generation(id="gen-1", profile_id="profile-1", text="one", audio_path=stored),
        Generation(id="gen-2", profile_id="profile-1", text="two", audio_path=str(shared) if reference == "generation" else None),
    ])
    db.add(GenerationVersion(id="version-1", generation_id="gen-1", label="Take", audio_path=stored))
    if reference == "version":
        db.add(GenerationVersion(id="version-2", generation_id="gen-2", label="Take", audio_path=str(shared)))
    elif reference == "sample":
        db.add(ProfileSample(id="sample-1", profile_id="profile-1", audio_path=str(shared), reference_text="hello"))
    db.commit()
    assert await history.delete_generation("gen-1", db) is True
    assert db.get(Generation, "gen-2") is not None
    assert shared.read_bytes() == b"RIFF placeholder"


@pytest.mark.asyncio
async def test_delete_profile_survives_unremovable_audio(db, monkeypatch):
    """A file locked by playback must not abort the delete half-way through."""
    _make_profile(db)
    gen_file, gen_stored = _write_wav("gen.wav")
    db.add(Generation(id="gen-1", profile_id="profile-1", text="hello", audio_path=gen_stored))
    db.commit()

    _lock(monkeypatch, gen_file)

    assert await profiles.delete_profile("profile-1", db) is True
    assert db.query(VoiceProfile).count() == 0
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_delete_profile_survives_unremovable_version_audio(db, monkeypatch):
    """Same guarantee for version files, which are unlinked one call deeper."""
    _make_profile(db)
    _, gen_stored = _write_wav("gen.wav")
    version_file, version_stored = _write_wav("gen-reverb.wav")
    db.add(Generation(id="gen-1", profile_id="profile-1", text="hello", audio_path=gen_stored))
    db.add(
        GenerationVersion(
            id="version-1",
            generation_id="gen-1",
            label="Reverb",
            audio_path=version_stored,
        )
    )
    db.commit()

    _lock(monkeypatch, version_file)

    assert await profiles.delete_profile("profile-1", db) is True
    assert db.query(VoiceProfile).count() == 0
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0


@pytest.mark.asyncio
async def test_one_locked_file_does_not_strand_the_rest(db, monkeypatch):
    """One locked file must not leave the other generations' rows behind."""
    _make_profile(db)
    for i in range(3):
        _, gen_stored = _write_wav(f"gen{i}.wav")
        _, version_stored = _write_wav(f"gen{i}-take2.wav")
        db.add(Generation(id=f"gen-{i}", profile_id="profile-1", text="hi", audio_path=gen_stored))
        db.add(
            GenerationVersion(
                id=f"version-{i}",
                generation_id=f"gen-{i}",
                label="take-2",
                audio_path=version_stored,
            )
        )
    db.commit()

    _lock(monkeypatch, config.get_generations_dir() / "gen1-take2.wav")

    assert await profiles.delete_profile("profile-1", db) is True
    assert db.query(Generation).count() == 0
    assert db.query(GenerationVersion).count() == 0
    assert db.query(VoiceProfile).count() == 0


@pytest.mark.asyncio
async def test_delete_profile_leaves_other_profiles_alone(db):
    """Cleanup is scoped to the deleted profile."""
    _make_profile(db, "profile-1")
    _make_profile(db, "profile-2")
    _, keep_stored = _write_wav("keep.wav")
    _, drop_stored = _write_wav("drop.wav")
    db.add(Generation(id="gen-keep", profile_id="profile-2", text="keep", audio_path=keep_stored))
    db.add(Generation(id="gen-drop", profile_id="profile-1", text="drop", audio_path=drop_stored))
    db.add(ProfileChannelMapping(profile_id="profile-2", channel_id="channel-1"))
    db.commit()

    await profiles.delete_profile("profile-1", db)

    assert [g.id for g in db.query(Generation).all()] == ["gen-keep"]
    assert db.query(ProfileChannelMapping).count() == 1


@pytest.mark.asyncio
async def test_delete_missing_profile_returns_false(db):
    assert await profiles.delete_profile("nope", db) is False
