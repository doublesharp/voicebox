"""Agent binding edits preserve settings outside the submitted patch."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.database import Base, VoiceProfile, get_db
from backend.routes.mcp_bindings import router


@pytest.fixture
def client():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([VoiceProfile(id='voice-a', name='A'), VoiceProfile(id='voice-b', name='B')])
        db.commit()
    def session():
        with Session(engine) as db:
            yield db
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = session
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    engine.dispose()


def test_voice_change_preserves_agent_engine_and_personality(client):
    created = client.put('/mcp/bindings', json={'client_id': 'agent', 'label': 'Agent', 'profile_id': 'voice-a', 'default_engine': 'kokoro', 'default_personality': True})
    assert created.status_code == 200
    updated = client.put('/mcp/bindings', json={'client_id': 'agent', 'label': 'Agent', 'profile_id': 'voice-b'})
    assert updated.status_code == 200
    assert updated.json()['profile_id'] == 'voice-b'
    assert updated.json()['default_engine'] == 'kokoro'
    assert updated.json()['default_personality'] is True
    assert client.get('/mcp/bindings').json()['items'][0]['default_engine'] == 'kokoro'


def test_explicit_null_and_false_clear_fields(client):
    client.put('/mcp/bindings', json={'client_id': 'agent', 'label': 'Agent', 'profile_id': 'voice-a', 'default_engine': 'kokoro', 'default_personality': True})
    result = client.put('/mcp/bindings', json={'client_id': 'agent', 'label': None, 'profile_id': None, 'default_engine': None, 'default_personality': False})
    assert result.status_code == 200
    assert {k: result.json()[k] for k in ('label', 'profile_id', 'default_engine', 'default_personality')} == {'label': None, 'profile_id': None, 'default_engine': None, 'default_personality': False}


def test_new_minimal_binding_has_default_values(client):
    result = client.put('/mcp/bindings', json={'client_id': 'new'})
    assert result.status_code == 200
    assert {k: result.json()[k] for k in ('label', 'profile_id', 'default_engine', 'default_personality')} == {'label': None, 'profile_id': None, 'default_engine': None, 'default_personality': False}


def test_minimal_existing_update_preserves_all_user_settings(client):
    client.put('/mcp/bindings', json={'client_id': 'agent', 'label': 'Agent', 'profile_id': 'voice-a', 'default_engine': 'kokoro', 'default_personality': True})
    result = client.put('/mcp/bindings', json={'client_id': 'agent'})
    assert result.status_code == 200
    assert {k: result.json()[k] for k in ('label', 'profile_id', 'default_engine', 'default_personality')} == {'label': 'Agent', 'profile_id': 'voice-a', 'default_engine': 'kokoro', 'default_personality': True}


@pytest.mark.parametrize('body', [{'client_id': ''}, {'client_id': 'x', 'default_engine': 'not-an-engine'}, {'client_id': 'x', 'default_personality': None}])
def test_invalid_binding_does_not_change_existing_state(client, body):
    client.put('/mcp/bindings', json={'client_id': 'x', 'default_engine': 'kokoro', 'default_personality': True})
    assert client.put('/mcp/bindings', json=body).status_code == 422
    item = client.get('/mcp/bindings').json()['items'][0]
    assert item['default_engine'] == 'kokoro' and item['default_personality'] is True


def test_delete_missing_and_existing_binding(client):
    assert client.delete('/mcp/bindings/missing').status_code == 404
    client.put('/mcp/bindings', json={'client_id': 'agent'})
    assert client.delete('/mcp/bindings/agent').json() == {'deleted': 'agent'}
    assert client.get('/mcp/bindings').json()['items'] == []
