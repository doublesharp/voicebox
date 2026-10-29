"""Profile HTTP workflows using the registered router and real SQLite storage."""

import io
import zipfile

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config
from backend.database import Base, get_db
from backend.routes.profiles import router
from backend.routes import profiles as profile_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_data_dir", tmp_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    session.close()
    engine.dispose()


def reference_audio():
    buffer = io.BytesIO()
    samples = 0.25 * np.sin(2 * np.pi * 220 * np.arange(48000) / 16000)
    sf.write(buffer, samples, 16000, format="WAV")
    return buffer.getvalue()


def test_profile_sample_export_and_import_workflow(client):
    response = client.post("/profiles", json={"name": "Narrator", "language": "en"})
    assert response.status_code == 200
    profile_id = response.json()["id"]
    assert client.post("/profiles", json={"name": "Narrator", "language": "en"}).status_code == 400
    response = client.post(
        f"/profiles/{profile_id}/samples", data={"reference_text": "Hello there"},
        files={"file": ("reference.wav", reference_audio(), "audio/wav")},
    )
    assert response.status_code == 200, response.text
    sample_id = response.json()["id"]
    assert client.put(f"/profiles/samples/{sample_id}", json={"reference_text": "Revised words"}).status_code == 200
    exported = client.get(f"/profiles/{profile_id}/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "manifest.json" in archive.namelist()
        assert "Revised words" in archive.read("samples.json").decode()
    imported = client.post("/profiles/import", files={"file": ("voice.zip", exported.content, "application/zip")})
    assert imported.status_code == 200, imported.text
    assert imported.json()["name"] == "Narrator (1)"
    imported_id = imported.json()["id"]
    assert client.get(f"/profiles/{imported_id}/samples").json()[0]["reference_text"] == "Revised words"
    assert client.delete(f"/profiles/{profile_id}").status_code == 200
    assert client.get(f"/profiles/{profile_id}").status_code == 404
    assert client.get(f"/profiles/{imported_id}").status_code == 200


def test_missing_profile_export_returns_not_found(client):
    response = client.get("/profiles/missing/export")
    assert response.status_code == 404
    assert response.json()["detail"] == "Profile not found"


def test_invalid_archives_and_audio_return_client_errors(client):
    response = client.post("/profiles/import", files={"file": ("bad.zip", b"bad", "application/zip")})
    assert response.status_code == 400
    profile_id = client.post("/profiles", json={"name": "Voice"}).json()["id"]
    response = client.post(f"/profiles/{profile_id}/samples", data={"reference_text": "hello"},
                           files={"file": ("bad.wav", b"not audio", "audio/wav")})
    assert response.status_code == 400
    assert client.get(f"/profiles/{profile_id}/samples").json() == []


def test_oversized_sample_does_not_leave_temporary_audio(client, tmp_path, monkeypatch):
    temp = tmp_path / "uploads"
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, "tempdir", str(temp))
    monkeypatch.setattr(profile_routes, "SAMPLE_MAX_FILE_SIZE", 8)
    profile_id = client.post("/profiles", json={"name": "Voice"}).json()["id"]
    response = client.post(f"/profiles/{profile_id}/samples", data={"reference_text": "hello"},
                           files={"file": ("large.wav", b"123456789", "audio/wav")})
    assert response.status_code == 413
    assert list(temp.iterdir()) == []
    assert client.get(f"/profiles/{profile_id}/samples").json() == []


@pytest.mark.asyncio
async def test_upload_read_failure_does_not_leave_temporary_audio(tmp_path, monkeypatch):
    temp = tmp_path / "uploads"
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, "tempdir", str(temp))

    class BrokenUpload:
        filename = "sample.wav"
        reads = 0

        async def read(self, _size):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise OSError("upload interrupted")

    with pytest.raises(OSError, match="upload interrupted"):
        await profile_routes.add_profile_sample("voice", BrokenUpload(), "words", None)
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
async def test_avatar_read_failure_does_not_leave_temporary_file(tmp_path, monkeypatch):
    temp = tmp_path / "uploads"
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, "tempdir", str(temp))

    class BrokenUpload:
        filename = "avatar.png"

        async def read(self):
            raise OSError("upload interrupted")

    with pytest.raises(OSError, match="upload interrupted"):
        await profile_routes.upload_profile_avatar("voice", BrokenUpload(), None)
    assert list(temp.iterdir()) == []
