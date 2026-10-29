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

        async def read(self, _size):
            raise OSError("upload interrupted")

    with pytest.raises(OSError, match="upload interrupted"):
        await profile_routes.upload_profile_avatar("voice", BrokenUpload(), None)
    assert list(temp.iterdir()) == []


class BoundedUpload:
    def __init__(self, data, filename):
        self.data = data
        self.filename = filename
        self.read_sizes = []

    async def read(self, size=-1):
        self.read_sizes.append(size)
        assert size > 0, 'upload must be read with a positive bound'
        result, self.data = self.data[:size], self.data[size:]
        return result


@pytest.mark.asyncio
async def test_profile_import_reads_bounded_chunks_and_preserves_archive_bytes(monkeypatch):
    upload = BoundedUpload(b'archive bytes', 'voice.zip')
    captured = []
    async def import_archive(data, db):
        captured.append(data)
        return 'imported'
    monkeypatch.setattr(profile_routes.export_import, 'import_profile_from_zip', import_archive)
    assert await profile_routes.import_profile(upload, None) == 'imported'
    assert captured == [b'archive bytes']
    assert len(upload.read_sizes) >= 2
    assert all(0 < size <= 1024 * 1024 for size in upload.read_sizes)


@pytest.mark.asyncio
async def test_avatar_upload_reads_bounded_chunks_and_removes_temporary_file(tmp_path, monkeypatch):
    temp = tmp_path / 'uploads'
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, 'tempdir', str(temp))
    upload = BoundedUpload(b'image bytes', 'avatar.png')
    captured = []
    async def upload_avatar(profile_id, path, db):
        from pathlib import Path
        captured.append((profile_id, Path(path).read_bytes()))
        return 'updated'
    monkeypatch.setattr(profile_routes.profiles, 'upload_avatar', upload_avatar)
    assert await profile_routes.upload_profile_avatar('voice', upload, None) == 'updated'
    assert captured == [('voice', b'image bytes')]
    assert len(upload.read_sizes) >= 2
    assert all(0 < size <= 1024 * 1024 for size in upload.read_sizes)
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('size,accepted', [(7, True), (8, True), (9, False), (20, False)])
async def test_profile_import_enforces_limit_before_processing(size, accepted, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(profile_routes, 'PROFILE_IMPORT_MAX_BYTES', 8)
    monkeypatch.setattr(profile_routes, 'PROFILE_UPLOAD_CHUNK_BYTES', 3)
    upload = BoundedUpload(b'x' * size, 'voice.zip')
    captured = []
    async def import_archive(data, db):
        captured.append(data)
        return 'imported'
    monkeypatch.setattr(profile_routes.export_import, 'import_profile_from_zip', import_archive)
    if accepted:
        assert await profile_routes.import_profile(upload, None) == 'imported'
        assert captured == [b'x' * size]
    else:
        with pytest.raises(HTTPException) as error:
            await profile_routes.import_profile(upload, None)
        assert error.value.status_code == 400
        assert captured == []
        assert len(upload.data) == size - 9
    assert all(0 < size <= 3 for size in upload.read_sizes)


@pytest.mark.asyncio
@pytest.mark.parametrize('size,accepted', [(7, True), (8, True), (9, False), (20, False)])
async def test_avatar_enforces_limit_and_cleans_staging_file(size, accepted, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from pathlib import Path
    temp = tmp_path / 'uploads'
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, 'tempdir', str(temp))
    monkeypatch.setattr(profile_routes, 'PROFILE_AVATAR_MAX_BYTES', 8)
    monkeypatch.setattr(profile_routes, 'PROFILE_UPLOAD_CHUNK_BYTES', 3)
    upload = BoundedUpload(b'x' * size, 'avatar.png')
    captured = []
    async def upload_avatar(profile_id, path, db):
        captured.append(Path(path).read_bytes())
        return 'updated'
    monkeypatch.setattr(profile_routes.profiles, 'upload_avatar', upload_avatar)
    if accepted:
        assert await profile_routes.upload_profile_avatar('voice', upload, None) == 'updated'
        assert captured == [b'x' * size]
    else:
        with pytest.raises(HTTPException) as error:
            await profile_routes.upload_profile_avatar('voice', upload, None)
        assert error.value.status_code == 413
        assert captured == []
        assert len(upload.data) == size - 9
    assert list(temp.iterdir()) == []
    assert all(0 < size <= 3 for size in upload.read_sizes)


def test_oversized_profile_import_and_avatar_are_rejected_by_http(client, tmp_path, monkeypatch):
    temp = tmp_path / 'uploads'
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, 'tempdir', str(temp))
    monkeypatch.setattr(profile_routes, 'PROFILE_IMPORT_MAX_BYTES', 8)
    monkeypatch.setattr(profile_routes, 'PROFILE_AVATAR_MAX_BYTES', 8)
    profile_id = client.post('/profiles', json={'name': 'Voice'}).json()['id']
    result = client.post('/profiles/import', files={'file': ('voice.zip', b'123456789', 'application/zip')})
    assert result.status_code == 400
    result = client.post(f'/profiles/{profile_id}/avatar', files={'file': ('avatar.png', b'123456789', 'image/png')})
    assert result.status_code == 413
    assert client.get(f'/profiles/{profile_id}').json()['avatar_path'] is None
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
async def test_interrupted_avatar_upload_removes_partially_written_file(tmp_path, monkeypatch):
    temp = tmp_path / 'uploads'
    temp.mkdir()
    monkeypatch.setattr(profile_routes.tempfile, 'tempdir', str(temp))
    class InterruptedUpload:
        filename = 'avatar.png'
        calls = 0
        async def read(self, size):
            assert size > 0
            self.calls += 1
            if self.calls == 1:
                return b'partial'
            raise OSError('upload interrupted')
    with pytest.raises(OSError, match='upload interrupted'):
        await profile_routes.upload_profile_avatar('voice', InterruptedUpload(), None)
    assert list(temp.iterdir()) == []


@pytest.mark.asyncio
async def test_interrupted_archive_upload_never_invokes_import(monkeypatch):
    class InterruptedUpload:
        filename = 'voice.zip'
        calls = 0
        async def read(self, size):
            assert size > 0
            self.calls += 1
            if self.calls == 1:
                return b'partial'
            raise OSError('upload interrupted')
    invoked = False
    async def import_archive(data, db):
        nonlocal invoked
        invoked = True
    monkeypatch.setattr(profile_routes.export_import, 'import_profile_from_zip', import_archive)
    with pytest.raises(OSError, match='upload interrupted'):
        await profile_routes.import_profile(InterruptedUpload(), None)
    assert not invoked
