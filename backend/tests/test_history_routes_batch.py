"""HTTP contracts for history details, favorites, deletion and exports."""

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config
from backend.database import Base, Generation, VoiceProfile, get_db
from backend.routes import history as routes


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, '_data_dir', tmp_path)
    audio = tmp_path / 'generations' / 'clip.wav'
    audio.parent.mkdir()
    audio.write_bytes(b'wave-data')
    engine = create_engine(f'sqlite:///{tmp_path / "history.db"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(VoiceProfile(id='voice', name='Narrator', language='en'))
        db.add_all([
            Generation(id='clip-123456', profile_id='voice', text='Hello / narration', language='en', audio_path='generations/clip.wav', status='completed'),
            Generation(id='failed-1', profile_id='voice', text='Failed', status='failed'),
        ])
        db.commit()
    app = FastAPI()
    app.include_router(routes.router)
    def session():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        yield client, factory, audio
    engine.dispose()


def test_detail_favorite_and_missing_generation(history_client):
    client, _, _ = history_client
    detail = client.get('/history/clip-123456')
    assert detail.status_code == 200
    assert detail.json()['profile_name'] == 'Narrator'
    assert detail.json()['text'] == 'Hello / narration'
    assert client.post('/history/clip-123456/favorite').json() == {'is_favorited': True}
    assert client.get('/history/clip-123456').json()['is_favorited'] is True
    assert client.post('/history/clip-123456/favorite').json() == {'is_favorited': False}
    assert client.get('/history/missing').status_code == 404
    assert client.post('/history/missing/favorite').status_code == 404


def test_clear_failed_preserves_completed_generation_and_reports_count(history_client):
    client, _, audio = history_client
    assert client.delete('/history/failed').json() == {'deleted': 1}
    assert client.delete('/history/failed').json() == {'deleted': 0}
    assert client.get('/history/failed-1').status_code == 404
    assert client.get('/history/clip-123456').status_code == 200
    assert audio.read_bytes() == b'wave-data'


def test_delete_generation_removes_record_and_owned_audio(history_client):
    client, _, audio = history_client
    assert client.delete('/history/clip-123456').status_code == 200
    assert client.get('/history/clip-123456').status_code == 404
    assert not audio.exists()
    assert client.delete('/history/clip-123456').status_code == 404


def test_export_archive_contains_original_audio_and_safe_filename(history_client):
    client, _, _ = history_client
    response = client.get('/history/clip-123456/export')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/zip')
    assert 'Hello  narration-clip-123' in response.headers['content-disposition']
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read('audio/clip.wav') == b'wave-data'
        assert b'Hello / narration' in archive.read('manifest.json')
    assert client.get('/history/missing/export').status_code == 404


@pytest.mark.parametrize('failure,status', [(ValueError('missing asset'), 400), (RuntimeError('archive failed'), 500)])
def test_export_errors_are_returned_to_client(history_client, monkeypatch, failure, status):
    client, _, _ = history_client
    def fail(*_args):
        raise failure
    monkeypatch.setattr(routes.export_import, 'export_generation_to_zip', fail)
    response = client.get('/history/clip-123456/export')
    assert response.status_code == status
    assert response.json()['detail'] == str(failure)


def test_audio_export_missing_and_unsupported_cases(history_client):
    client, factory, audio = history_client
    assert client.get('/history/missing/export-audio').status_code == 404
    assert client.get('/history/clip-123456/export-audio?format=flac').status_code == 400
    with factory() as db:
        generation = db.query(Generation).filter_by(id='clip-123456').one()
        generation.audio_path = None
        db.commit()
    assert client.get('/history/clip-123456/export-audio').json()['detail'] == 'Generation has no audio file'
    with factory() as db:
        generation = db.query(Generation).filter_by(id='clip-123456').one()
        generation.audio_path = 'generations/clip.wav'
        db.commit()
    audio.unlink()
    assert client.get('/history/clip-123456/export-audio').json()['detail'] == 'Audio file not found'


def test_mp3_export_cleans_temporary_file_after_encoder_failure(history_client, monkeypatch, tmp_path):
    client, _, _ = history_client
    def fail(_source, destination, _format):
        destination.write_bytes(b'partial')
        raise RuntimeError('encoder failed')
    monkeypatch.setattr(routes, 'encode_audio', fail)
    original = routes.tempfile.mkstemp
    def make_file(*args, **kwargs):
        return original(*args, dir=tmp_path, **kwargs)
    monkeypatch.setattr(routes.tempfile, 'mkstemp', make_file)
    before = set(tmp_path.glob('*.mp3'))
    response = client.get('/history/clip-123456/export-audio?format=mp3')
    assert response.status_code == 503
    assert response.json()['detail'] == 'encoder failed'
    assert set(tmp_path.glob('*.mp3')) == before


def test_history_stats_include_all_generations_by_profile(history_client):
    client, _, _ = history_client
    response = client.get('/history/stats')
    assert response.status_code == 200
    assert response.json() == {
        'total_generations': 2,
        'total_duration_seconds': 0,
        'generations_by_profile': {'voice': 2},
    }


def test_missing_audio_prevents_a_partial_archive(history_client):
    client, _, audio = history_client
    audio.unlink()
    response = client.get('/history/clip-123456/export')
    assert response.status_code == 400
    assert 'Audio file not found' in response.json()['detail']
    assert not response.content.startswith(b'PK')


def test_mp3_export_returns_encoder_output_and_cleans_temporary_file(history_client, monkeypatch, tmp_path):
    client, _, audio = history_client
    def encode(source, destination, format_):
        assert source == audio
        assert format_ == 'mp3'
        destination.write_bytes(b'encoded-mp3')
    monkeypatch.setattr(routes, 'encode_audio', encode)
    original = routes.tempfile.mkstemp
    monkeypatch.setattr(routes.tempfile, 'mkstemp', lambda *args, **kwargs: original(*args, dir=tmp_path, **kwargs))
    response = client.get('/history/clip-123456/export-audio?format=MP3')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'audio/mpeg'
    assert response.content == b'encoded-mp3'
    assert list(tmp_path.glob('*.mp3')) == []
