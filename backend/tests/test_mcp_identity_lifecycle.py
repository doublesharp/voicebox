"""Request identity and client-binding persistence across real ASGI requests."""

import asyncio
from datetime import datetime
import threading

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from backend.mcp_server import context


@pytest.mark.parametrize(
    ('address', 'expected'),
    [(None, False), ('', False), ('localhost', False), ('192.168.1.10', False),
     ('127.0.0.1', True), ('127.23.1.4', True), ('::1', True), ('2001:db8::1', False)],
)
def test_loopback_check_fails_closed_and_accepts_loopback_ips(address, expected):
    token = context.current_remote_addr.set(address)
    try:
        assert context.request_is_loopback() is expected
    finally:
        context.current_remote_addr.reset(token)


@pytest.mark.asyncio
async def test_concurrent_requests_isolate_identity_and_ignore_forwarded_address(monkeypatch):
    arrived = asyncio.Event()
    release = asyncio.Event()
    identities = []
    stamps = []
    monkeypatch.setattr(context, '_enqueue_stamp', stamps.append)

    async def inspect(request):
        identity = context.current_client_id.get()
        identities.append(identity)
        if len(identities) == 2:
            arrived.set()
        await release.wait()
        return JSONResponse({'identity': context.current_client_id.get(),
                             'loopback': context.request_is_loopback()})

    app = Starlette(routes=[Route('/mcp', inspect), Route('/speak', inspect)])
    app.add_middleware(context.ClientIdMiddleware)
    transport = httpx.ASGITransport(app=app, client=('203.0.113.10', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        first = asyncio.create_task(client.get('/mcp', headers={'X-Voicebox-Client-Id': 'one', 'X-Forwarded-For': '127.0.0.1'}))
        second = asyncio.create_task(client.get('/speak', headers={'X-Voicebox-Client-Id': 'two'}))
        await asyncio.wait_for(arrived.wait(), 2)
        release.set()
        responses = await asyncio.gather(first, second)
    assert [response.json() for response in responses] == [
        {'identity': 'one', 'loopback': False}, {'identity': 'two', 'loopback': False}
    ]
    assert sorted(stamps) == ['one', 'two']
    assert context.current_client_id.get() is None
    assert context.current_remote_addr.get() is None


@pytest.mark.asyncio
async def test_only_identified_semantic_requests_schedule_stamps(monkeypatch):
    stamps = []
    monkeypatch.setattr(context, '_enqueue_stamp', stamps.append)
    async def inspect(_request):
        return JSONResponse({'identity': context.current_client_id.get(), 'loopback': context.request_is_loopback()})
    app = Starlette(routes=[Route('/mcp', inspect), Route('/health', inspect)])
    app.add_middleware(context.ClientIdMiddleware)
    transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        assert (await client.get('/mcp')).json() == {'identity': None, 'loopback': True}
        assert (await client.get('/health', headers={'X-Voicebox-Client-Id': 'health'})).status_code == 200
        assert (await client.get('/mcp', headers={'X-Voicebox-Client-Id': 'agent'})).status_code == 200
    assert stamps == ['agent']


@pytest.mark.asyncio
async def test_request_failure_restores_identity_and_does_not_stamp(monkeypatch):
    stamps = []
    monkeypatch.setattr(context, '_enqueue_stamp', stamps.append)
    async def fail(_request):
        assert context.current_client_id.get() == 'agent'
        raise ValueError('request failed')
    app = Starlette(routes=[Route('/mcp', fail)])
    app.add_middleware(context.ClientIdMiddleware)
    client_token = context.current_client_id.set('outer')
    addr_token = context.current_remote_addr.set('::1')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            with pytest.raises(ValueError, match='request failed'):
                await client.get('/mcp', headers={'X-Voicebox-Client-Id': 'agent'})
        assert context.current_client_id.get() == 'outer'
        assert context.current_remote_addr.get() == '::1'
        assert stamps == []
    finally:
        context.current_client_id.reset(client_token)
        context.current_remote_addr.reset(addr_token)


@pytest.mark.asyncio
async def test_stamp_is_scheduled_off_event_loop_and_retained_until_finished(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []
    def stamp(client_id):
        calls.append(client_id)
        started.set()
        assert release.wait(2)
    monkeypatch.setattr(context, '_stamp_last_seen', stamp)
    try:
        context._enqueue_stamp('agent')
        assert await asyncio.to_thread(started.wait, 1)
        tasks = set(context._pending_stamps)
        assert tasks and all(not task.done() for task in tasks)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert not (tasks & context._pending_stamps)
        assert calls == ['agent']
    finally:
        release.set()


def test_stamp_without_event_loop_runs_inline(monkeypatch):
    calls = []
    monkeypatch.setattr(context, '_stamp_last_seen', calls.append)
    context._enqueue_stamp('agent')
    assert calls == ['agent']


def test_binding_stamp_creates_and_updates_row_without_closing_session_early(monkeypatch, tmp_path):
    import backend.database as database
    from backend.database.models import Base, MCPClientBinding

    engine = create_engine(f'sqlite:///{tmp_path / "bindings.db"}')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, close_resets_only=False)
    def get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()
    monkeypatch.setattr(database, 'get_db', get_db)
    try:
        context._stamp_last_seen('agent')
        with sessions() as db:
            row = db.get(MCPClientBinding, 'agent')
            assert row is not None
            assert isinstance(row.last_seen_at, datetime)
            row.label = 'My agent'
            db.commit()
        context._stamp_last_seen('agent')
        with sessions() as db:
            rows = db.query(MCPClientBinding).all()
            assert len(rows) == 1
            assert rows[0].label == 'My agent'
            assert isinstance(rows[0].last_seen_at, datetime)
    finally:
        engine.dispose()


def test_binding_stamp_rolls_back_failed_write_and_closes_session(monkeypatch, tmp_path):
    import backend.database as database
    from backend.database.models import Base, MCPClientBinding
    from sqlalchemy.orm import Session

    engine = create_engine(f'sqlite:///{tmp_path / "failure.db"}')
    Base.metadata.create_all(engine)
    events = []
    class FailingSession(Session):
        def commit(self):
            self.flush()
            raise RuntimeError('write failed')
        def rollback(self):
            events.append('rollback')
            return super().rollback()
        def close(self):
            events.append('close')
            return super().close()
    def get_db():
        db = FailingSession(bind=engine)
        try:
            yield db
        finally:
            db.close()
    monkeypatch.setattr(database, 'get_db', get_db)
    try:
        context._stamp_last_seen('agent')
        assert events == ['rollback', 'close']
        with Session(engine) as db:
            assert db.get(MCPClientBinding, 'agent') is None
    finally:
        engine.dispose()
