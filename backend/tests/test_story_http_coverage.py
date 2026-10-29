"""Story API behavior with real SQLite, including rejected edits and exports."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database import Base, Generation, Story, StoryItem, VoiceProfile, get_db
from backend.routes import stories as story_routes


@pytest.fixture
def app_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'story.db'}", connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([VoiceProfile(id='voice', name='Narrator'), Story(id='story', name='Draft')])
        db.add_all([
            Generation(id='a', profile_id='voice', text='First', duration=1, audio_path='first.wav'),
            Generation(id='b', profile_id='voice', text='Second', duration=2, audio_path='second.wav'),
            Generation(id='failed', profile_id='voice', text='Failed', status='failed', duration=None, audio_path=None),
            Generation(id='unknown', profile_id='voice', text='No duration', status='completed', duration=None, audio_path='missing.wav'),
        ])
        db.commit()
    app = FastAPI()
    app.include_router(story_routes.router)
    def get_session():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db] = get_session
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_story_http_lifecycle_and_clip_mutations(app_client):
    client, _ = app_client
    story = client.post('/stories', json={'name': 'A story', 'description': 'draft'}).json()
    sid = story['id']
    assert story['item_count'] == 0
    one = client.post(f'/stories/{sid}/items', json={'generation_id': 'a'}).json()
    two = client.post(f'/stories/{sid}/items', json={'generation_id': 'b'}).json()
    assert (one['start_time_ms'], two['start_time_ms']) == (0, 1200)
    assert client.post(f'/stories/{sid}/items', json={'generation_id': 'a'}).json()['id'] == one['id']
    assert client.put(f'/stories/{sid}/items/{one["id"]}/move', json={'start_time_ms': 400, 'track': 1}).json()['track'] == 1
    assert client.put(f'/stories/{sid}/items/{one["id"]}/trim', json={'trim_start_ms': 100, 'trim_end_ms': 200}).json()['trim_end_ms'] == 200
    assert client.put(f'/stories/{sid}/items/{one["id"]}/volume', json={'volume': 0.5}).json()['volume'] == 0.5
    assert client.put(f'/stories/{sid}/items/{one["id"]}/version', json={'version_id': None}).status_code == 200
    detail = client.get(f'/stories/{sid}').json()
    assert len(detail['items']) == 2
    assert client.put(f'/stories/{sid}', json={'name': 'Final'}).json()['item_count'] == 2
    assert next(row for row in client.get('/stories').json() if row['id'] == sid)['name'] == 'Final'
    assert client.delete(f'/stories/{sid}/items/{one["id"]}').status_code == 200
    assert client.delete(f'/stories/{sid}').status_code == 200
    assert client.get(f'/stories/{sid}').status_code == 404


@pytest.mark.parametrize('method,path,body', [
    ('get', '/stories/missing', None),
    ('put', '/stories/missing', {'name': 'No'}),
    ('delete', '/stories/missing', None),
    ('post', '/stories/missing/items', {'generation_id': 'a'}),
    ('post', '/stories/story/items', {'generation_id': 'absent'}),
    ('delete', '/stories/story/items/absent', None),
    ('put', '/stories/story/items/absent/move', {'start_time_ms': 0}),
    ('put', '/stories/story/items/absent/trim', {'trim_start_ms': 0, 'trim_end_ms': 0}),
    ('put', '/stories/story/items/absent/volume', {'volume': 1}),
    ('put', '/stories/story/items/absent/version', {'version_id': None}),
    ('post', '/stories/story/items/absent/split', {'split_time_ms': 100}),
    ('post', '/stories/story/items/absent/duplicate', None),
])
def test_missing_story_and_clip_returns_not_found(app_client, method, path, body):
    client, _ = app_client
    assert client.request(method, path, json=body).status_code == 404


def test_split_duplicate_and_generation_keyed_updates_reject_ambiguity(app_client):
    client, _ = app_client
    item = client.post('/stories/story/items', json={'generation_id': 'a'}).json()
    clip = item['id']
    assert client.post(f'/stories/story/items/{clip}/split', json={'split_time_ms': 500}).status_code == 200
    assert client.post(f'/stories/story/items/{clip}/duplicate').status_code == 200
    before = client.get('/stories/story').json()['items']
    assert client.put('/stories/story/items/times', json={'updates': [{'generation_id': 'a', 'start_time_ms': 5000}]}).status_code == 400
    assert client.put('/stories/story/items/reorder', json={'generation_ids': ['a']}).status_code == 400
    assert client.get('/stories/story').json()['items'] == before


@pytest.mark.parametrize('generation', ['failed', 'unknown'])
def test_unplayable_generation_is_rejected_without_creating_item(app_client, generation):
    client, engine = app_client
    response = client.post('/stories/story/items', json={'generation_id': generation})
    assert response.status_code == 400
    with Session(engine) as db:
        assert db.query(StoryItem).count() == 0
    assert client.get('/stories/story').status_code == 200


def test_negative_creation_time_is_rejected_without_creating_item(app_client):
    client, engine = app_client
    assert client.post('/stories/story/items', json={'generation_id': 'a', 'start_time_ms': -1}).status_code == 422
    with Session(engine) as db:
        assert db.query(StoryItem).count() == 0


@pytest.mark.parametrize('query', ['format=bogus', 'chapters=bogus'])
def test_export_rejects_invalid_query_before_render(app_client, query, monkeypatch):
    client, _ = app_client
    async def never(*args, **kwargs):
        pytest.fail('invalid export must not render')
    monkeypatch.setattr(story_routes.stories, 'export_story_file', never)
    assert client.get(f'/stories/story/export-audio?{query}').status_code == 400


@pytest.mark.parametrize('error,expected', [(ValueError('invalid clip'), 400), (RuntimeError('encoder unavailable'), 503)])
def test_export_surfaces_render_errors(app_client, monkeypatch, error, expected):
    client, _ = app_client
    async def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(story_routes.stories, 'export_story_file', fail)
    response = client.get('/stories/story/export-audio')
    assert response.status_code == expected
    assert response.json()['detail'] == str(error)


def test_export_streams_audio_and_removes_temporary_file(app_client, tmp_path, monkeypatch):
    client, _ = app_client
    output = tmp_path / 'rendered.wav'
    output.write_bytes(b'audio data')
    async def render(story_id, db, fmt, chapters_mode):
        assert (story_id, fmt, chapters_mode) == ('story', 'wav', 'auto')
        return output
    monkeypatch.setattr(story_routes.stories, 'export_story_file', render)
    response = client.get('/stories/story/export-audio?chapters=auto')
    assert response.status_code == 200
    assert response.content == b'audio data'
    assert response.headers['content-type'] == 'audio/wav'
    assert not output.exists()
