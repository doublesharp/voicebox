"""Version editing/removal through the real API, SQLite, WAV files and DSP."""

import io
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend import config
from backend.database import Base, Generation, GenerationVersion, Story, StoryItem, VoiceProfile, get_db
from backend.routes.audio import router as audio_router
from backend.routes.effects import router as effects_router

GAIN = [{"type": "gain", "enabled": True, "params": {"gain_db": -6}}]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'versions.db'}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    clean = (0.2 * np.sin(np.arange(2400) * 2 * np.pi * 440 / 24000)).astype(np.float32)
    directory = config.get_generations_dir()
    sf.write(directory / "original.wav", clean, 24000, subtype="FLOAT")
    sf.write(directory / "processed.wav", clean * 0.5, 24000, subtype="FLOAT")
    with sessions() as db:
        db.add(VoiceProfile(id="voice", name="Voice"))
        db.commit()
        db.add(
            Generation(
                id="gen", profile_id="voice", text="Hello", status="completed", audio_path="generations/processed.wav"
            )
        )
        db.add(
            Generation(
                id="other", profile_id="voice", text="Other", status="completed", audio_path="generations/original.wav"
            )
        )
        db.commit()
        db.add(
            GenerationVersion(
                id="original",
                generation_id="gen",
                label="original",
                audio_path="generations/original.wav",
                is_default=False,
            )
        )
        db.commit()
        db.add(
            GenerationVersion(
                id="processed",
                generation_id="gen",
                label="version-2",
                audio_path="generations/processed.wav",
                effects_chain=json.dumps(GAIN),
                source_version_id="original",
                is_default=True,
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(effects_router)
    app.include_router(audio_router)

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, sessions, directory, clean
    engine.dispose()


def edit(client, chain, generation="gen", version="processed"):
    return client.put(f"/generations/{generation}/versions/{version}", json={"effects_chain": chain})


def test_edit_replaces_effects_from_source_without_adding_a_version(fixture):
    client, sessions, directory, clean = fixture
    chain = [{"type": "gain", "enabled": True, "params": {"gain_db": -12}}]
    response = edit(client, chain)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["id"] == "processed"
    assert result["label"] == "version-2"
    assert result["source_version_id"] == "original"
    assert result["is_default"] is True
    assert result["effects_chain"] == chain
    rendered, rate = sf.read(io.BytesIO(client.get("/audio/version/processed").content))
    assert rate == 24000
    np.testing.assert_allclose(rendered, clean * 10 ** (-12 / 20), atol=4e-5)
    with sessions() as db:
        assert db.query(GenerationVersion).count() == 2
        assert db.get(Generation, "gen").audio_path == result["audio_path"]
    assert not (directory / "processed.wav").exists()
    assert (directory / "original.wav").exists()


def test_removing_all_effects_keeps_the_version_editable(fixture):
    client, _, _, clean = fixture
    response = edit(client, [])
    assert response.status_code == 200, response.text
    assert response.json()["effects_chain"] == []
    rendered, _ = sf.read(io.BytesIO(client.get("/audio/version/processed").content))
    np.testing.assert_allclose(rendered, clean, atol=4e-5)
    assert edit(client, GAIN).status_code == 200


def test_legacy_processed_version_resolves_original_source(fixture):
    client, sessions, _, _ = fixture
    with sessions() as db:
        db.get(GenerationVersion, "processed").source_version_id = None
        db.commit()
    response = edit(client, [])
    assert response.status_code == 200, response.text
    assert response.json()["source_version_id"] == "original"


@pytest.mark.parametrize(
    ("generation", "version", "status"),
    [("other", "processed", 404), ("gen", "missing", 404), ("gen", "original", 409)],
)
def test_edit_rejects_other_generations_missing_versions_and_original(fixture, generation, version, status):
    client, _, _, _ = fixture
    assert edit(client, [], generation, version).status_code == status


def test_invalid_effect_leaves_audio_and_default_unchanged(fixture):
    client, sessions, directory, _ = fixture
    original_bytes = (directory / "processed.wav").read_bytes()
    response = edit(client, [{"type": "gain", "params": {"gain_db": 1000}}])
    assert response.status_code == 400, response.text
    assert (directory / "processed.wav").read_bytes() == original_bytes
    with sessions() as db:
        assert db.get(Generation, "gen").audio_path == "generations/processed.wav"


def test_delete_active_version_promotes_original_and_removes_audio(fixture):
    client, sessions, directory, _ = fixture
    response = client.delete("/generations/gen/versions/processed")
    assert response.status_code == 200, response.text
    with sessions() as db:
        assert db.get(GenerationVersion, "processed") is None
        assert db.get(GenerationVersion, "original").is_default is True
        assert db.get(Generation, "gen").audio_path == "generations/original.wav"
    assert not (directory / "processed.wav").exists()
    assert (directory / "original.wav").exists()


def test_original_cannot_be_deleted(fixture):
    client, _, directory, _ = fixture
    response = client.delete("/generations/gen/versions/original")
    assert response.status_code == 409, response.text
    assert (directory / "original.wav").exists()


@pytest.mark.parametrize("reference", ["version", "story"])
def test_used_versions_cannot_be_edited_or_deleted(fixture, reference):
    client, sessions, directory, _ = fixture
    with sessions() as db:
        if reference == "version":
            db.add(
                GenerationVersion(
                    id="child",
                    generation_id="gen",
                    label="child",
                    audio_path="generations/child.wav",
                    source_version_id="processed",
                    effects_chain=json.dumps(GAIN),
                )
            )
        else:
            db.add(Story(id="story", name="Story"))
            db.commit()
            db.add(StoryItem(id="clip", story_id="story", generation_id="gen", version_id="processed"))
        db.commit()
    for response in (edit(client, []), client.delete("/generations/gen/versions/processed")):
        assert response.status_code == 409, response.text
    assert (directory / "processed.wav").exists()


def test_deleting_versions_does_not_reuse_an_existing_label(fixture):
    client, _, _, _ = fixture
    result = client.post("/generations/gen/versions/apply-effects", json={"effects_chain": GAIN}).json()
    assert result["label"] == "version-3"
    assert client.delete("/generations/gen/versions/processed").status_code == 200
    response = client.post("/generations/gen/versions/apply-effects", json={"effects_chain": GAIN})
    assert response.status_code == 200, response.text
    assert response.json()["label"] == "version-4"


def test_editing_a_derived_version_uses_its_immediate_source(fixture):
    client, _, _, clean = fixture
    result = client.post(
        "/generations/gen/versions/apply-effects",
        json={
            "effects_chain": GAIN,
            "source_version_id": "processed",
        },
    ).json()
    response = edit(client, [], version=result["id"])
    assert response.status_code == 200, response.text
    rendered, _ = sf.read(io.BytesIO(client.get(f"/audio/version/{result['id']}").content))
    np.testing.assert_allclose(rendered, clean * 0.5, atol=4e-5)


def test_editing_inactive_version_keeps_default(fixture):
    client, sessions, _, _ = fixture
    assert client.put("/generations/gen/versions/original/set-default").status_code == 200
    response = edit(client, [])
    assert response.status_code == 200, response.text
    assert response.json()["is_default"] is False
    with sessions() as db:
        assert db.get(Generation, "gen").audio_path == "generations/original.wav"


def test_failed_render_leaves_no_partial_version(fixture, monkeypatch):
    client, sessions, directory, _ = fixture

    def fail_save(_audio, path, _rate):
        Path(path).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("backend.utils.audio.save_audio", fail_save)
    with pytest.raises(OSError, match="disk full"):
        edit(client, [])
    assert sorted(path.name for path in directory.iterdir()) == ["original.wav", "processed.wav"]
    with sessions() as db:
        assert db.get(GenerationVersion, "processed").audio_path == "generations/processed.wav"


@pytest.mark.parametrize("operation", ["edit", "delete"])
def test_failed_commit_preserves_rows_defaults_and_audio(fixture, monkeypatch, operation):
    client, sessions, directory, _ = fixture
    previous = (directory / "processed.wav").read_bytes()

    def fail_commit(_session):
        raise RuntimeError("commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)

        def request():
            return edit(client, []) if operation == "edit" else client.delete("/generations/gen/versions/processed")

        with pytest.raises(RuntimeError, match="commit failed"):
            request()
    assert sorted(path.name for path in directory.iterdir()) == ["original.wav", "processed.wav"]
    assert (directory / "processed.wav").read_bytes() == previous
    with sessions() as db:
        assert db.get(GenerationVersion, "processed").is_default is True
        assert db.get(Generation, "gen").audio_path == "generations/processed.wav"


def test_missing_source_is_reported_without_changing_processed_audio(fixture):
    client, _, directory, _ = fixture
    (directory / "original.wav").unlink()
    response = edit(client, [])
    assert response.status_code == 404, response.text
    assert (directory / "processed.wav").exists()


def test_locked_audio_rejects_deletion_without_changing_default(fixture, monkeypatch):
    client, sessions, directory, _ = fixture
    real_rename = Path.rename

    def rename(path, target):
        if path == directory / "processed.wav":
            raise PermissionError("Audio is still in use")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    response = client.delete("/generations/gen/versions/processed")
    assert response.status_code == 409, response.text
    assert (directory / "processed.wav").exists()
    with sessions() as db:
        assert db.get(GenerationVersion, "processed").is_default is True
