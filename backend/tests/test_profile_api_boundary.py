"""Voice profile HTTP contracts, including failed uploads and stored effects."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.database import Base, VoiceProfile, get_db
from backend.routes import profiles as routes


@pytest.fixture
def client():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(VoiceProfile(id='voice', name='Narrator', language='en', personality='A friendly guide'))
        db.commit()

    def session():
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = session
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    engine.dispose()


def test_profile_and_sample_missing_contracts(client):
    assert client.get('/profiles/missing').status_code == 404
    assert client.put('/profiles/missing', json={'name': 'New'}).status_code == 404
    assert client.delete('/profiles/missing').status_code == 404
    assert client.delete('/profiles/samples/missing').status_code == 404
    assert client.put('/profiles/samples/missing', json={'reference_text': 'Words'}).status_code == 404
    assert client.get('/profiles/missing/avatar').status_code == 404
    assert client.get('/profiles/voice/avatar').status_code == 404
    assert client.delete('/profiles/voice/avatar').status_code == 404
    assert client.get('/profiles/missing/export').status_code == 404
    assert client.post('/profiles/missing/compose').status_code == 404


def test_effects_validate_persist_and_clear(client):
    url = '/profiles/voice/effects'
    chain = [{'type': 'gain', 'enabled': True, 'params': {'gain_db': 3}}]
    response = client.put(url, json={'effects_chain': chain})
    assert response.status_code == 200, response.text
    assert response.json()['effects_chain'] == chain
    bad = client.put(url, json={'effects_chain': [{'type': 'not-an-effect', 'params': {}}]})
    assert bad.status_code == 400
    assert client.get('/profiles/voice').json()['effects_chain'] == chain
    assert client.put(url, json={'effects_chain': None}).json()['effects_chain'] is None
    assert client.put('/profiles/missing/effects', json={'effects_chain': None}).status_code == 404


def test_compose_passes_personality_and_maps_invalid_input(client, monkeypatch):
    received = []
    async def compose(personality):
        received.append(personality)
        return SimpleNamespace(text='Hello!', model_size='small')
    monkeypatch.setattr(routes.personality, 'compose_as_profile', compose)
    result = client.post('/profiles/voice/compose')
    assert result.status_code == 200
    assert result.json() == {'text': 'Hello!', 'model_size': 'small'}
    assert received == ['A friendly guide']
    async def invalid(_):
        raise ValueError('personality is unavailable')
    monkeypatch.setattr(routes.personality, 'compose_as_profile', invalid)
    assert client.post('/profiles/voice/compose').status_code == 400


@pytest.mark.parametrize('kind,expected', [('sample', 413), ('avatar', 413), ('import', 400)])
def test_upload_limits_reject_without_calling_service(client, monkeypatch, kind, expected):
    monkeypatch.setattr(routes, 'SAMPLE_MAX_FILE_SIZE', 3)
    monkeypatch.setattr(routes, 'PROFILE_AVATAR_MAX_BYTES', 3)
    monkeypatch.setattr(routes, 'PROFILE_IMPORT_MAX_BYTES', 3)
    async def forbidden(*args, **kwargs):
        pytest.fail('oversized upload reached processing service')
    monkeypatch.setattr(routes.profiles, 'add_profile_sample', forbidden)
    monkeypatch.setattr(routes.profiles, 'upload_avatar', forbidden)
    monkeypatch.setattr(routes.export_import, 'import_profile_from_zip', forbidden)
    url = {'sample': '/profiles/voice/samples', 'avatar': '/profiles/voice/avatar', 'import': '/profiles/import'}[kind]
    result = client.post(url, files={'file': ('input.wav', b'1234')}, data={'reference_text': 'hello'})
    assert result.status_code == expected, result.text


@pytest.mark.parametrize('kind', ['sample', 'avatar'])
@pytest.mark.parametrize('error,status', [(ValueError('bad input'), 400), (RuntimeError('processing failed'), 500)])
def test_upload_temporary_file_is_cleaned_on_processing_failure(client, monkeypatch, kind, error, status):
    seen = []
    async def fail(profile_id, path, *args):
        seen.append(Path(path))
        assert seen[-1].read_bytes() == b'audio'
        raise error
    name = 'add_profile_sample' if kind == 'sample' else 'upload_avatar'
    monkeypatch.setattr(routes.profiles, name, fail)
    url = f'/profiles/voice/{"samples" if kind == "sample" else "avatar"}'
    result = client.post(url, files={'file': ('clip.wav', b'audio')}, data={'reference_text': 'hello'})
    assert result.status_code == status, result.text
    assert len(seen) == 1 and not seen[0].exists()


def test_export_returns_attachment_and_preserves_zip_bytes(client, monkeypatch):
    monkeypatch.setattr(routes.export_import, 'export_profile_to_zip', lambda profile_id, db: b'ZIP DATA')
    response = client.get('/profiles/voice/export')
    assert response.status_code == 200
    assert response.content == b'ZIP DATA'
    assert response.headers['content-type'] == 'application/zip'
    assert 'profile-Narrator.voicebox.zip' in response.headers['content-disposition']


@pytest.mark.parametrize('error,status', [(ValueError('bad archive'), 400), (RuntimeError('failed'), 500)])
def test_import_reports_processing_errors(client, monkeypatch, error, status):
    async def fail(data, db):
        assert data == b'zip bytes'
        raise error
    monkeypatch.setattr(routes.export_import, 'import_profile_from_zip', fail)
    result = client.post('/profiles/import', files={'file': ('profile.zip', b'zip bytes')})
    assert result.status_code == status
    assert result.json()['detail'] == str(error)
