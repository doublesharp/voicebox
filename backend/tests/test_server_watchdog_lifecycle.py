"""Parent watchdog behavior without signaling real processes."""

import importlib
import sys
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI


@pytest.fixture(scope='module')
def server():
    stdout, stderr = sys.stdout, sys.stderr
    try:
        return importlib.import_module('backend.server')
    finally:
        sys.stdout, sys.stderr = stdout, stderr


def test_watchdog_starts_after_custom_app_lifespan(server, monkeypatch):
    import asyncio
    import runpy
    import threading
    import uvicorn
    import backend.main

    events = []
    @asynccontextmanager
    async def lifespan(_app):
        events.append('app-start')
        yield
        events.append('app-stop')
    app = FastAPI(lifespan=lifespan)
    monkeypatch.setattr(backend.main, 'app', app)
    monkeypatch.setattr(server.database, 'init_db', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['voicebox-server', '--parent-pid', '123'])
    class FakeThread:
        def __init__(self, target, daemon):
            pass
        def start(self):
            events.append('watchdog-start')
    monkeypatch.setattr(threading, 'Thread', FakeThread)
    def run_application(application, **_kwargs):
        async def run():
            async with application.router.lifespan_context(application):
                events.append('serving')
        asyncio.run(run())
    monkeypatch.setattr(uvicorn, 'run', run_application)
    stdout, stderr = sys.stdout, sys.stderr
    try:
        runpy.run_module('backend.server', run_name='__main__')
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    assert events == ['app-start', 'watchdog-start', 'serving', 'app-stop']


def test_unix_watchdog_does_not_treat_permission_denied_as_dead(server, monkeypatch):
    import os
    import threading
    import time
    if sys.platform == 'win32':
        pytest.skip('Unix process probing')
    calls = []
    monkeypatch.setattr(server.watchdog_state, 'disabled', False)
    def kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise PermissionError('process exists but cannot be inspected')
        pytest.fail('watchdog attempted to terminate server')
    monkeypatch.setattr(os, 'kill', kill)
    monkeypatch.setattr(time, 'sleep', lambda _seconds: setattr(server.watchdog_state, 'disabled', True))
    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(threading, 'Thread', ImmediateThread)
    server._start_parent_watchdog(123)
    assert calls == [(123, 0), (123, 0)]


def test_disable_endpoint_stops_running_entrypoint_watchdog(server, monkeypatch):
    import asyncio
    import os
    import runpy
    import signal
    import threading
    import time
    import uvicorn
    import backend.main
    from backend.routes.health import watchdog_disable

    if sys.platform == 'win32':
        pytest.skip('Unix process probing')
    monkeypatch.setattr(server.watchdog_state, 'disabled', False)
    app = FastAPI()
    monkeypatch.setattr(backend.main, 'app', app)
    monkeypatch.setattr(server.database, 'init_db', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['voicebox-server', '--parent-pid', '123'])
    monkeypatch.setattr(signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(time, 'sleep', lambda _seconds: None)
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0 and len(calls) > 1:
            raise ProcessLookupError('parent exited')
    monkeypatch.setattr(os, 'kill', kill)
    targets = []
    class FakeThread:
        def __init__(self, target, daemon):
            targets.append(target)
        def start(self):
            pass
    monkeypatch.setattr(threading, 'Thread', FakeThread)
    def run_application(application, **_kwargs):
        async def run():
            async with application.router.lifespan_context(application):
                await watchdog_disable()
                targets[0]()
        asyncio.run(run())
    monkeypatch.setattr(uvicorn, 'run', run_application)
    stdout, stderr = sys.stdout, sys.stderr
    try:
        runpy.run_module('backend.server', run_name='__main__')
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    assert calls == [(123, 0)]
