"""Audio routing and voice assignment through the real HTTP and SQLite boundary."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.database import AudioChannel, Base, VoiceProfile, get_db
from backend.routes import channels, profiles


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([VoiceProfile(id="voice-a", name="A"), VoiceProfile(id="voice-b", name="B")])
        db.add(AudioChannel(id="default", name="Default", is_default=True))
        db.commit()

    def session():
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.include_router(channels.router)
    app.include_router(profiles.router)
    app.dependency_overrides[get_db] = session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    engine.dispose()


def test_channel_workflow_and_cross_view_assignments(client):
    result = client.post('/channels', json={'name': 'Narration', 'device_ids': ['headphones', 'speakers']})
    assert result.status_code == 200
    channel_id = result.json()['id']
    assert set(client.get(f'/channels/{channel_id}').json()['device_ids']) == {'headphones', 'speakers'}
    assert client.put(f'/channels/{channel_id}/voices', json={'profile_ids': ['voice-a']}).status_code == 200
    assert client.get('/profiles/voice-a/channels').json()['channel_ids'] == [channel_id]
    assert client.put('/profiles/voice-b/channels', json={'channel_ids': [channel_id]}).status_code == 200
    assert set(client.get(f'/channels/{channel_id}/voices').json()['profile_ids']) == {'voice-a', 'voice-b'}
    assert client.put(f'/channels/{channel_id}', json={'name': 'Final', 'device_ids': ['speakers']}).status_code == 200
    assert client.get(f'/channels/{channel_id}').json()['device_ids'] == ['speakers']
    assert client.delete(f'/channels/{channel_id}').status_code == 200
    assert client.get('/profiles/voice-a/channels').json()['channel_ids'] == []
    assert client.get(f'/channels/{channel_id}').status_code == 404


def test_default_channel_is_protected(client):
    assert client.put('/channels/default', json={'name': 'Changed'}).status_code == 400
    assert client.delete('/channels/default').status_code == 400
    assert client.get('/channels/default').json()['name'] == 'Default'


@pytest.mark.parametrize('side', ['channel', 'profile'])
def test_duplicate_or_unknown_assignment_is_rejected_without_erasing_existing(client, side):
    channel_id = client.post('/channels', json={'name': 'Narration'}).json()['id']
    assert client.put(f'/channels/{channel_id}/voices', json={'profile_ids': ['voice-a']}).status_code == 200
    if side == 'channel':
        url, duplicate, unknown = f'/channels/{channel_id}/voices', {'profile_ids': ['voice-a', 'voice-a']}, {'profile_ids': ['voice-a', 'missing']}
    else:
        url, duplicate, unknown = '/profiles/voice-a/channels', {'channel_ids': [channel_id, channel_id]}, {'channel_ids': [channel_id, 'missing']}
    assert client.put(url, json=duplicate).status_code == 400
    assert client.put(url, json=unknown).status_code == 400
    assert client.get(f'/channels/{channel_id}/voices').json()['profile_ids'] == ['voice-a']
    assert client.get('/profiles/voice-a/channels').json()['channel_ids'] == [channel_id]


def test_duplicate_devices_are_rejected_without_overwriting_channel(client):
    assert client.post('/channels', json={'name': 'Invalid', 'device_ids': ['speaker', 'speaker']}).status_code == 400
    channel_id = client.post('/channels', json={'name': 'Narration', 'device_ids': ['speaker']}).json()['id']
    assert client.put(f'/channels/{channel_id}', json={'name': 'Changed', 'device_ids': ['headphones', 'headphones']}).status_code == 400
    assert client.get(f'/channels/{channel_id}').json()['device_ids'] == ['speaker']
    assert client.get(f'/channels/{channel_id}').json()['name'] == 'Narration'
