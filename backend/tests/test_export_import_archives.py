"""Archive integrity checks for saved voice work."""

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import Base, Generation, GenerationVersion, VoiceProfile
from backend.services import export_import


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def archive(manifest, samples=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        if samples is not None:
            z.writestr("samples.json", json.dumps(samples))
    return out.getvalue()


@pytest.mark.asyncio
async def test_invalid_profile_archive_does_not_create_a_profile(db, monkeypatch):
    async def unexpected_create(*args):
        pytest.fail("a profile must not be created before archive validation")

    monkeypatch.setattr(export_import, "create_profile", unexpected_create)
    data = archive({"version": "1.0", "profile": {"name": "Voice"}}, {"missing.wav": "words"})
    with pytest.raises(ValueError, match="Sample file not found"):
        await export_import.import_profile_from_zip(data, db)
    assert db.query(VoiceProfile).count() == 0


@pytest.mark.parametrize("path", [None, "missing.wav"])
def test_generation_export_rejects_missing_version_audio(db, monkeypatch, tmp_path, path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.add(Generation(id="g", profile_id="p", text="Hello", language="en", created_at=datetime.now(timezone.utc)))
    db.add(GenerationVersion(id="v", generation_id="g", label="Take", audio_path=path or "", is_default=True))
    db.commit()
    with pytest.raises(ValueError, match="Audio file not found"):
        export_import.export_generation_to_zip("g", db)


def test_generation_export_contains_all_version_audio_and_metadata(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    (tmp_path / "one.wav").write_bytes(b"first")
    (tmp_path / "two.wav").write_bytes(b"second")
    db.add(VoiceProfile(id="p", name="Voice"))
    db.add(Generation(id="g", profile_id="p", text="Hello", language="en", created_at=datetime.now(timezone.utc)))
    db.add_all([
        GenerationVersion(id="v1", generation_id="g", label="Original", audio_path="one.wav", is_default=True),
        GenerationVersion(id="v2", generation_id="g", label="Edited", audio_path="two.wav", effects_chain='[{"type":"gain"}]'),
    ])
    db.commit()
    with zipfile.ZipFile(io.BytesIO(export_import.export_generation_to_zip("g", db))) as z:
        assert z.read("audio/one.wav") == b"first"
        assert z.read("audio/two.wav") == b"second"
        manifest = json.loads(z.read("manifest.json"))
        assert {v["id"] for v in manifest["versions"]} == {"v1", "v2"}
        assert next(v for v in manifest["versions"] if v["id"] == "v2")["effects_chain"] == [{"type": "gain"}]


@pytest.mark.asyncio
async def test_generation_import_uses_selected_default_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.commit()
    manifest = {
        "version": "1.0",
        "profile": {"name": "Voice"},
        "generation": {"text": "Hello", "language": "en", "duration": 1.0},
        "versions": [
            {"filename": "one.wav", "is_default": False},
            {"filename": "two.wav", "is_default": True},
        ],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("audio/one.wav", b"original")
        z.writestr("audio/two.wav", b"chosen take")
    result = await export_import.import_generation_from_zip(out.getvalue(), db)
    generation = db.query(Generation).filter_by(id=result["id"]).one()
    assert config.resolve_storage_path(generation.audio_path).read_bytes() == b"chosen take"


@pytest.mark.asyncio
async def test_generation_import_rejects_missing_default_take(db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.commit()
    manifest = {"version": "1.0", "generation": {"text": "Hi", "language": "en", "duration": 1},
                "versions": [{"filename": "missing.wav", "is_default": True}]}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("audio/other.wav", b"other")
    with pytest.raises(ValueError, match="Default version audio not found"):
        await export_import.import_generation_from_zip(out.getvalue(), db)
    assert db.query(Generation).count() == 0


@pytest.mark.parametrize("path", [None, "missing.wav"])
def test_generation_export_rejects_missing_legacy_audio(db, monkeypatch, tmp_path, path):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    db.add(VoiceProfile(id="p", name="Voice"))
    db.add(Generation(id="g", profile_id="p", text="Hi", audio_path=path,
                      created_at=datetime.now(timezone.utc)))
    db.commit()
    with pytest.raises(ValueError, match="Audio file not found"):
        export_import.export_generation_to_zip("g", db)
