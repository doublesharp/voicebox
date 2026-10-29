"""Imported audio preserves files and database state across failure paths."""

import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend import config
from backend.database import Base, Generation
from backend.routes import generations


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    monkeypatch.setattr(generations, 'load_audio', lambda _: (np.zeros(8000), 8000))
    engine = create_engine(f'sqlite:///{tmp_path / "test.db"}')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield tmp_path, db
    engine.dispose()


def upload(name='music.wav', data=b'audio'):
    return UploadFile(filename=name, file=io.BytesIO(data))


def audio_files(root):
    return list(config.get_generations_dir().iterdir())


@pytest.mark.asyncio
async def test_successful_import_persists_file_and_history(setup):
    root, db = setup
    response = await generations.import_audio(upload(), db)
    row = db.get(Generation, response.id)
    assert row.text == 'music' and row.source == 'import' and row.duration == 1
    assert config.resolve_storage_path(row.audio_path).read_bytes() == b'audio'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,body,status', [('bad.txt', b'audio', 400), ('empty.wav', b'', 400), ('huge.wav', b'12345', 413)])
async def test_invalid_upload_does_not_create_audio_or_rows(setup, monkeypatch, name, body, status):
    root, db = setup
    monkeypatch.setattr(generations, 'IMPORT_AUDIO_MAX_BYTES', 4)
    with pytest.raises(HTTPException) as error:
        await generations.import_audio(upload(name, body), db)
    assert error.value.status_code == status
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_decode_failure_removes_owned_audio(setup, monkeypatch):
    root, db = setup
    def fail(_):
        raise ValueError('bad audio')
    monkeypatch.setattr(generations, 'load_audio', fail)
    with pytest.raises(HTTPException) as error:
        await generations.import_audio(upload(), db)
    assert error.value.status_code == 400
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_profile_creation_failure_removes_owned_audio(setup, monkeypatch):
    root, db = setup
    def fail(_):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(generations, '_get_or_create_import_profile', fail)
    with pytest.raises(RuntimeError):
        await generations.import_audio(upload(), db)
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_uncommitted_history_failure_removes_owned_audio(setup, monkeypatch):
    root, db = setup
    async def fail(**_kwargs):
        raise RuntimeError('save failed')
    monkeypatch.setattr(generations.history, 'create_generation', fail)
    with pytest.raises(RuntimeError):
        await generations.import_audio(upload(), db)
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_exception_after_history_commit_keeps_referenced_audio(setup, monkeypatch):
    root, db = setup
    real_create = generations.history.create_generation
    async def commit_then_fail(**kwargs):
        await real_create(**kwargs)
        raise RuntimeError('response failed after commit')
    monkeypatch.setattr(generations.history, 'create_generation', commit_then_fail)
    with pytest.raises(RuntimeError):
        await generations.import_audio(upload(), db)
    row = db.query(Generation).one()
    assert config.resolve_storage_path(row.audio_path).read_bytes() == b'audio'


@pytest.mark.asyncio
async def test_filename_collision_does_not_overwrite_existing_audio(setup, monkeypatch):
    root, db = setup
    monkeypatch.setattr(generations.uuid, 'uuid4', lambda: 'existing')
    path = config.get_generations_dir() / 'existing.wav'
    path.write_bytes(b'previous')
    with pytest.raises((FileExistsError, HTTPException)):
        await generations.import_audio(upload(data=b'replacement'), db)
    assert path.read_bytes() == b'previous'
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('samples,rate', [(0, 8000), (100, 0)])
async def test_zero_duration_audio_is_rejected_and_removed(setup, monkeypatch, samples, rate):
    root, db = setup
    monkeypatch.setattr(generations, 'load_audio', lambda _: (np.zeros(samples), rate))
    with pytest.raises(HTTPException) as error:
        await generations.import_audio(upload(), db)
    assert error.value.status_code == 400
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_upload_read_failure_removes_partial_audio(setup):
    root, db = setup
    count = 0
    async def read(_size):
        nonlocal count
        count += 1
        if count == 1:
            return b'partial'
        raise OSError('upload interrupted')
    with pytest.raises(OSError):
        await generations.import_audio(SimpleNamespace(filename='music.wav', read=read), db)
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_cancel_during_decode_waits_for_reader_then_removes_audio(setup, monkeypatch):
    import asyncio
    import threading

    root, db = setup
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    contents = []
    def decode(path):
        started.set()
        try:
            assert release.wait(3)
            contents.append(Path(path).read_bytes() if Path(path).exists() else None)
            return np.zeros(8000), 8000
        finally:
            finished.set()
    monkeypatch.setattr(generations, 'load_audio', decode)
    task = asyncio.create_task(generations.import_audio(upload(), db))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        retained = bool(audio_files(root))
        waiting = not task.done()
    finally:
        release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert await asyncio.to_thread(finished.wait, 2)
    assert retained and waiting
    assert contents == [b'audio']
    assert audio_files(root) == []
    assert db.query(Generation).count() == 0


@pytest.mark.asyncio
async def test_unknown_commit_state_retains_audio_for_recovery(setup, monkeypatch):
    root, db = setup
    async def fail(**_kwargs):
        raise RuntimeError('write failed')
    def unavailable(*_args, **_kwargs):
        raise OSError('database read unavailable')
    monkeypatch.setattr(generations.history, 'create_generation', fail)
    monkeypatch.setattr(generations, 'Session', unavailable)
    with pytest.raises(RuntimeError):
        await generations.import_audio(upload(), db)
    files = audio_files(root)
    assert len(files) == 1 and files[0].read_bytes() == b'audio'


def test_import_http_multipart_contract(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.database import get_db

    monkeypatch.setattr(config, '_data_dir', tmp_path)
    monkeypatch.setattr(generations, 'load_audio', lambda _: (np.zeros(8000), 8000))
    engine = create_engine(f'sqlite:///{tmp_path / "http.db"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(generations.router)
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    try:
        with TestClient(app) as client:
            result = client.post('/generate/import', files={'file': ('voice.wav', b'audio', 'audio/wav')})
        assert result.status_code == 200
        assert result.json()['text'] == 'voice'
        with Session(engine) as db:
            row = db.get(Generation, result.json()['id'])
            assert config.resolve_storage_path(row.audio_path).read_bytes() == b'audio'
    finally:
        engine.dispose()
