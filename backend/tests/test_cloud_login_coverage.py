"""Cloud login contracts with a real local database and in-process HTTP."""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database.models import Base
from backend.services import cloud


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'cloud.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    cloud._pending.clear()
    opened = []
    monkeypatch.setattr(cloud.webbrowser, 'open', lambda url: opened.append(url))
    monkeypatch.setattr(cloud.config, 'get_cloud_web_url', lambda: 'https://web.example')
    monkeypatch.setattr(cloud.config, 'get_cloud_api_url', lambda: 'https://api.example')
    yield db, opened
    cloud._pending.clear()
    db.close()
    engine.dispose()


def start(opened):
    url = cloud.start_login('http://127.0.0.1:17493/cloud/callback', 'My laptop & desk')
    assert opened[-1] == url
    parsed = urlparse(url)
    assert (parsed.scheme, parsed.netloc, parsed.path) == ('https', 'web.example', '/connect')
    query = parse_qs(parsed.query)
    assert query['redirect_uri'] == ['http://127.0.0.1:17493/cloud/callback']
    assert query['name'] == ['My laptop & desk']
    return query['state'][0]


def transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(cloud.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def callback(db, state, code='one-time-code'):
    return asyncio.run(cloud.handle_callback(db, code, state))


def test_pairing_stores_verified_key_and_disconnect_clears_local_credentials(setup, monkeypatch):
    db, opened = setup
    state = start(opened)
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'POST':
            assert request.content == b'{"code":"one-time-code"}'
            return httpx.Response(200, json={'key': 'voicebox_123456789012345', 'label': 'Desk'})
        assert request.headers['Authorization'] == 'Bearer voicebox_123456789012345'
        return httpx.Response(200, json={'data': {'userId': 'user-1'}})
    transport(monkeypatch, handler)
    assert callback(db, state) == (True, 'Connected')
    assert len(requests) == 2
    status = cloud.get_status(db)
    assert status['connected'] and status['device_name'] == 'Desk'
    assert status['account_user_id'] == 'user-1'
    assert status['key_prefix'] == 'voicebox_12345678'
    assert 'voicebox_123456789012345' not in repr(status)
    assert callback(db, state)[0] is False
    assert len(requests) == 2
    cloud.disconnect(db)
    assert cloud.get_api_key(db) is None
    assert cloud.get_status(db)['connected'] is False


def test_expired_and_unknown_states_do_not_exchange_or_change_key(setup, monkeypatch):
    db, opened = setup
    state = start(opened)
    cloud._store_key(db, api_key='previous', device_name='old', account_user_id='user')
    expired_time = cloud._pending[state] + 1
    monkeypatch.setattr(cloud.time, 'time', lambda: expired_time)
    transport(monkeypatch, lambda request: pytest.fail('must not make an HTTP request'))
    assert callback(db, state)[0] is False
    assert callback(db, 'unknown')[0] is False
    assert cloud.get_api_key(db) == 'previous'


def test_missing_code_consumes_state_without_contacting_server(setup, monkeypatch):
    db, opened = setup
    state = start(opened)
    transport(monkeypatch, lambda request: pytest.fail('must not make an HTTP request'))
    assert callback(db, state, '') == (False, 'Missing authorization code.')
    assert callback(db, state)[0] is False
    assert cloud.get_api_key(db) is None


@pytest.mark.parametrize('response', [
    httpx.Response(400, json={'error': 'invalid'}),
    httpx.Response(200, text='not json'),
    httpx.Response(200, json=['not', 'an', 'object']),
    httpx.Response(200, json={}),
])
def test_exchange_failures_leave_previous_key_intact(setup, monkeypatch, response):
    db, opened = setup
    cloud._store_key(db, api_key='previous', device_name='old', account_user_id='user')
    state = start(opened)
    requests = []
    def handler(request):
        requests.append(request)
        return response
    transport(monkeypatch, handler)
    assert callback(db, state)[0] is False
    assert len(requests) == 1
    assert cloud.get_api_key(db) == 'previous'
    assert callback(db, state)[0] is False


@pytest.mark.parametrize('key', [123, True, ['token'], {'token': 'secret'}, '   ', 'bad\nkey', 'bad\x00key', 'clé'])
def test_malformed_key_is_rejected_before_verification_and_never_stored(setup, monkeypatch, key):
    db, opened = setup
    cloud._store_key(db, api_key='previous', device_name='old', account_user_id='user')
    state = start(opened)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'key': key, 'label': 'Desk'})
    transport(monkeypatch, handler)
    assert callback(db, state)[0] is False
    assert len(requests) == 1
    assert cloud.get_api_key(db) == 'previous'


def test_rejected_key_does_not_replace_existing_connection(setup, monkeypatch):
    db, opened = setup
    cloud._store_key(db, api_key='previous', device_name='old', account_user_id='user')
    state = start(opened)
    transport(monkeypatch, lambda request: httpx.Response(200, json={'key': 'voicebox_fresh'}) if request.method == 'POST' else httpx.Response(401))
    assert callback(db, state)[0] is False
    assert cloud.get_api_key(db) == 'previous'


def test_network_failure_preserves_existing_connection_and_consumes_state(setup, monkeypatch):
    db, opened = setup
    cloud._store_key(db, api_key='previous', device_name='old', account_user_id='user')
    state = start(opened)
    def handler(request):
        raise httpx.ConnectError('offline', request=request)
    transport(monkeypatch, handler)
    assert 'Could not reach' in callback(db, state)[1]
    assert callback(db, state)[0] is False
    assert cloud.get_api_key(db) == 'previous'


@pytest.mark.parametrize('body', [{'data': None}, {'data': []}, {}, 'invalid json'])
def test_verified_key_can_be_stored_without_user_id(setup, monkeypatch, body):
    db, opened = setup
    state = start(opened)
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'key': 'voicebox_fresh'})
        return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
    transport(monkeypatch, handler)
    assert callback(db, state)[0] is True
    assert cloud.get_status(db)['account_user_id'] is None
    assert cloud.get_api_key(db) == 'voicebox_fresh'


@pytest.mark.parametrize('label,user_id', [(['invalid'], 'user'), ('desk', {'invalid': True})])
def test_invalid_optional_metadata_cannot_break_verified_login(setup, monkeypatch, label, user_id):
    db, opened = setup
    state = start(opened)
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'key': 'voicebox_fresh', 'label': label})
        return httpx.Response(200, json={'data': {'userId': user_id}})
    transport(monkeypatch, handler)
    assert callback(db, state)[0] is True
    status = cloud.get_status(db)
    assert status['device_name'] == (label if isinstance(label, str) else None)
    assert status['account_user_id'] == (user_id if isinstance(user_id, str) else None)
