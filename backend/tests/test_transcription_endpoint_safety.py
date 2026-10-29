"""Endpoint ownership and download admission, without a real speech model."""

import asyncio
import io
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException, UploadFile

from backend import backends
from backend.routes import transcription as route
from backend.services import transcribe
from backend.utils import audio as audio_utils
from backend.utils.tasks import TaskManager
from backend.utils.progress import ProgressManager


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(route.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(audio_utils, 'load_audio', lambda path: (np.zeros(8000), 8000))
    monkeypatch.setattr(audio_utils, 'save_audio', lambda audio, path, rate: Path(path).write_bytes(b'wav'))
    manager = TaskManager()
    progress = ProgressManager()
    monkeypatch.setattr(route, 'get_task_manager', lambda: manager)
    monkeypatch.setattr(route, 'get_progress_manager', lambda: progress)
    async def recognize(path, language, size):
        assert Path(path).exists()
        return 'recognized words'
    model = SimpleNamespace(model_size='base', is_loaded=lambda: True, _is_model_cached=lambda _: True, transcribe=recognize)
    monkeypatch.setattr(transcribe, 'get_whisper_model', lambda: model)
    return tmp_path, model, manager


def upload(name='audio.wav', body=b'audio'):
    return UploadFile(filename=name, file=io.BytesIO(body))


@pytest.mark.asyncio
@pytest.mark.parametrize('name,expected_suffix', [('audio.wav', '.wav'), ('AUDIO.MP3', '.mp3.stt.wav'), ('voice.webm', '.webm.stt.wav'), ('unknown.xyz', '.wav')])
async def test_transcribes_uploaded_formats_and_removes_temporary_files(setup, name, expected_suffix):
    tmp_path, model, _ = setup
    seen = []
    async def recognize(path, language, size):
        seen.append((path, language, size, Path(path).read_bytes()))
        return 'words'
    model.transcribe = recognize
    result = await route.transcribe_audio(upload(name), language='pl', model='base')
    assert result.text == 'words' and result.duration == 1
    assert seen[0][0].endswith(expected_suffix)
    assert seen[0][1:3] == ('pl', 'base')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejected_model_cleans_temporary_audio(setup):
    tmp_path, model, _ = setup
    with pytest.raises(HTTPException) as error:
        await route.transcribe_audio(upload(), model='not-a-model')
    assert error.value.status_code == 400
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_decoder_failure_cleans_temporary_audio(setup, monkeypatch):
    tmp_path, model, _ = setup
    def fail(_):
        raise ValueError('bad audio')
    monkeypatch.setattr(audio_utils, 'load_audio', fail)
    with pytest.raises(HTTPException) as error:
        await route.transcribe_audio(upload())
    assert error.value.status_code == 500
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_read_failure_removes_partial_temp_file(setup):
    tmp_path, model, _ = setup
    count = 0
    async def read(_size):
        nonlocal count
        count += 1
        if count == 1:
            return b'partial'
        raise OSError('upload interrupted')
    with pytest.raises((OSError, HTTPException)):
        await route.transcribe_audio(SimpleNamespace(filename='voice.wav', read=read))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_uncached_model_is_queued_once_for_repeated_requests(setup, monkeypatch):
    tmp_path, model, manager = setup
    model.is_loaded = lambda: False
    model._is_model_cached = lambda _: False
    release = asyncio.Event()
    calls = []
    async def load(size):
        calls.append(size)
        await release.wait()
    model.load_model_async = load
    tasks = []
    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(route, 'create_background_task', start)
    try:
        for _ in range(2):
            with pytest.raises(HTTPException) as error:
                await route.transcribe_audio(upload(), model='base')
            assert error.value.status_code == 202
        await asyncio.sleep(0)
        assert len(tasks) == 1
        assert calls == ['base']
        assert [x.model_name for x in manager.get_pending_downloads()] == ['whisper-base']
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert manager.get_pending_downloads() == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_retains_audio_until_transcription_worker_finishes(setup):
    tmp_path, model, _ = setup
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []
    async def recognize(path, *_):
        def reader():
            started.set()
            try:
                assert release.wait(3)
                seen.append(Path(path).read_bytes() if Path(path).exists() else None)
            finally:
                finished.set()
            return 'words'
        return await asyncio.to_thread(reader)
    model.transcribe = recognize
    pending = asyncio.create_task(route.transcribe_audio(upload(), language=None, model=None))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel()
        await asyncio.sleep(0.05)
        pending.cancel()
        await asyncio.sleep(0.05)
        retained = bool(list(tmp_path.iterdir()))
        waiting = not pending.done()
    finally:
        release.set()
    await asyncio.gather(pending, return_exceptions=True)
    assert await asyncio.to_thread(finished.wait, 2)
    assert retained and waiting
    assert seen == [b'audio']
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_progress_only_download_does_not_start_another_worker(setup, monkeypatch):
    tmp_path, model, manager = setup
    model.is_loaded = lambda: False
    model._is_model_cached = lambda _: False
    progress = route.get_progress_manager()
    progress.update_progress('whisper-base', 10, 100)
    tasks = []
    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(route, 'create_background_task', start)
    try:
        with pytest.raises(HTTPException) as error:
            await route.transcribe_audio(upload(), language=None, model='base')
        assert error.value.status_code == 202
        assert tasks == []
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_conversion_waits_before_deleting_source_and_output(setup, monkeypatch):
    tmp_path, model, manager = setup
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def write(_audio, path, _rate):
        started.set()
        try:
            assert release.wait(3)
            Path(path).write_bytes(b'wav')
        finally:
            finished.set()
    monkeypatch.setattr(audio_utils, 'save_audio', write)
    pending = asyncio.create_task(route.transcribe_audio(upload('audio.webm'), language=None, model=None))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel()
        await asyncio.sleep(0.05)
        retained = bool(list(tmp_path.iterdir()))
        waiting = not pending.done()
    finally:
        release.set()
    await asyncio.gather(pending, return_exceptions=True)
    assert await asyncio.to_thread(finished.wait, 2)
    assert retained and waiting
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_model_download_is_visible_and_can_be_retried(setup, monkeypatch):
    tmp_path, model, manager = setup
    model.is_loaded = lambda: False
    model._is_model_cached = lambda _: False
    calls = []
    async def load(size):
        calls.append(size)
        if len(calls) == 1:
            raise RuntimeError('network failed')
    model.load_model_async = load
    tasks = []
    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(route, 'create_background_task', start)
    with pytest.raises(HTTPException) as error:
        await route.transcribe_audio(upload(), language=None, model='base')
    assert error.value.status_code == 202
    await tasks[-1]
    assert manager.get_active_downloads()[0].status == 'error'
    with pytest.raises(HTTPException):
        await route.transcribe_audio(upload(), language=None, model='base')
    await tasks[-1]
    assert calls == ['base', 'base']
    assert manager.get_active_downloads() == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_before_start', [True, False])
async def test_cancelled_background_download_is_not_left_active(setup, monkeypatch, cancel_before_start):
    tmp_path, model, manager = setup
    model.is_loaded = lambda: False
    model._is_model_cached = lambda _: False
    started, release = asyncio.Event(), asyncio.Event()
    async def load(_size):
        started.set()
        await release.wait()
    model.load_model_async = load
    tasks = []
    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(route, 'create_background_task', start)
    with pytest.raises(HTTPException):
        await route.transcribe_audio(upload(), language=None, model='base')
    if not cancel_before_start:
        await asyncio.wait_for(started.wait(), 2)
    tasks[0].cancel()
    if not cancel_before_start:
        await asyncio.sleep(0.05)
        assert not tasks[0].done(), 'the model load must drain before cancellation completes'
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert manager.get_pending_downloads() == []
    assert manager.get_active_downloads()[0].status == 'error'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_download_scheduling_reports_error_and_cleans_upload(setup, monkeypatch):
    tmp_path, model, manager = setup
    model.is_loaded = lambda: False
    model._is_model_cached = lambda _: False
    async def load(_size):
        pass
    model.load_model_async = load
    def fail(_work):
        raise RuntimeError('scheduler unavailable')
    monkeypatch.setattr(route, 'create_background_task', fail)
    with pytest.raises(HTTPException) as error:
        await route.transcribe_audio(upload(), language=None, model='base')
    assert error.value.status_code == 500
    assert manager.get_active_downloads()[0].status == 'error'
    assert list(tmp_path.iterdir()) == []


def test_transcription_http_multipart_contract(setup):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    tmp_path, model, manager = setup
    app = FastAPI()
    app.include_router(route.router)
    with TestClient(app) as client:
        response = client.post('/transcribe', files={'file': ('voice.wav', b'audio', 'audio/wav')}, data={'language': 'pl', 'model': 'base'})
    assert response.status_code == 200
    assert response.json() == {'text': 'recognized words', 'duration': 1.0}
    assert list(tmp_path.iterdir()) == []
