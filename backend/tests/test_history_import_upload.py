"""Bound and validate generation archive uploads at the HTTP boundary."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.database import get_db
from backend.routes import history


class BoundedUpload:
    def __init__(self, content):
        self.content = content
        self.read_sizes = []

    async def read(self, size=-1):
        self.read_sizes.append(size)
        assert size > 0, 'generation archive reads must be bounded'
        result, self.content = self.content[:size], self.content[size:]
        return result


@pytest.mark.asyncio
async def test_import_reads_bounded_chunks_and_preserves_archive(monkeypatch):
    upload = BoundedUpload(b'voice archive')
    calls = []
    async def import_archive(content, db):
        calls.append(content)
        return {'id': 'imported'}
    monkeypatch.setattr(history.export_import, 'import_generation_from_zip', import_archive)
    assert await history.import_generation(upload, None) == {'id': 'imported'}
    assert calls == [b'voice archive']
    assert len(upload.read_sizes) >= 2
    assert all(0 < size <= 1024 * 1024 for size in upload.read_sizes)


@pytest.mark.asyncio
@pytest.mark.parametrize('size,accepted', [(0, True), (7, True), (8, True), (9, False), (20, False)])
async def test_history_archive_limit_is_checked_before_import(size, accepted, monkeypatch):
    monkeypatch.setattr(history, 'HISTORY_IMPORT_MAX_BYTES', 8)
    monkeypatch.setattr(history, 'HISTORY_UPLOAD_CHUNK_BYTES', 3)
    upload = BoundedUpload(b'x' * size)
    calls = []
    async def import_archive(content, db):
        calls.append(content)
        return {'id': 'imported'}
    monkeypatch.setattr(history.export_import, 'import_generation_from_zip', import_archive)
    if accepted:
        assert await history.import_generation(upload, None) == {'id': 'imported'}
        assert calls == [b'x' * size]
    else:
        with pytest.raises(HTTPException) as error:
            await history.import_generation(upload, None)
        assert error.value.status_code == 400
        assert calls == []
        assert len(upload.content) == size - 9
    assert all(0 < size <= 3 for size in upload.read_sizes)


@pytest.mark.asyncio
async def test_interrupted_history_upload_never_invokes_import(monkeypatch):
    class InterruptedUpload:
        calls = 0
        async def read(self, size):
            assert size > 0
            self.calls += 1
            if self.calls == 1:
                return b'partial'
            raise OSError('upload interrupted')
    invoked = False
    async def import_archive(content, db):
        nonlocal invoked
        invoked = True
    monkeypatch.setattr(history.export_import, 'import_generation_from_zip', import_archive)
    with pytest.raises(OSError, match='upload interrupted'):
        await history.import_generation(InterruptedUpload(), None)
    assert not invoked


def test_history_import_limit_is_enforced_via_http(monkeypatch):
    monkeypatch.setattr(history, 'HISTORY_IMPORT_MAX_BYTES', 8)
    monkeypatch.setattr(history, 'HISTORY_UPLOAD_CHUNK_BYTES', 3)
    calls = []
    async def import_archive(content, db):
        calls.append(content)
        return {'id': 'imported'}
    monkeypatch.setattr(history.export_import, 'import_generation_from_zip', import_archive)
    app = FastAPI()
    app.include_router(history.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        accepted = client.post('/history/import', files={'file': ('voice.zip', b'12345678', 'application/zip')})
        rejected = client.post('/history/import', files={'file': ('voice.zip', b'123456789', 'application/zip')})
    assert accepted.status_code == 200
    assert accepted.json() == {'id': 'imported'}
    assert rejected.status_code == 400
    assert calls == [b'12345678']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,status', [(ValueError('bad archive'), 400), (RuntimeError('storage failed'), 500)])
async def test_history_import_preserves_processing_error_mapping(failure, status, monkeypatch):
    async def import_archive(content, db):
        raise failure
    monkeypatch.setattr(history.export_import, 'import_generation_from_zip', import_archive)
    with pytest.raises(HTTPException) as error:
        await history.import_generation(BoundedUpload(b'archive'), None)
    assert error.value.status_code == status
    assert error.value.detail == str(failure)
