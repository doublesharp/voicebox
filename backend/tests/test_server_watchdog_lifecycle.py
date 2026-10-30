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


@pytest.fixture(autouse=True)
def close_watchdog_log_handlers():
    import logging
    logger = logging.getLogger('watchdog')
    original = set(logger.handlers)
    yield
    for handler in set(logger.handlers) - original:
        logger.removeHandler(handler)
        handler.close()


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


@pytest.mark.parametrize('outcome', ['sentinel', 'disabled', 'shutdown'])
def test_parent_exit_respects_grace_period_and_keep_running(server, monkeypatch, tmp_path, outcome):
    import os
    import signal
    import threading
    import time

    if sys.platform == 'win32':
        pytest.skip('Unix process probing')
    monkeypatch.setattr(server.watchdog_state, 'disabled', False)
    sentinel = tmp_path / '.keep-running'
    sentinel.write_text('stale')
    calls = []
    probes = 0

    def kill(pid, sig):
        nonlocal probes
        calls.append((pid, sig))
        if sig == 0:
            probes += 1
            if probes > 1:
                raise ProcessLookupError('parent exited')

    def sleep(seconds):
        assert seconds == 1
        # The watchdog must remove a sentinel left by an earlier session.
        assert not sentinel.exists()
        if outcome == 'sentinel':
            sentinel.write_text('new')
        elif outcome == 'disabled':
            server.watchdog_state.disabled = True

    class ImmediateThread:
        def __init__(self, target, daemon):
            assert daemon
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(os, 'kill', kill)
    monkeypatch.setattr(time, 'sleep', sleep)
    monkeypatch.setattr(threading, 'Thread', ImmediateThread)
    server._start_parent_watchdog(123, str(tmp_path))

    assert calls[:2] == [(123, 0), (123, 0)]
    assert calls[2:] == ([(os.getpid(), signal.SIGTERM)] if outcome == 'shutdown' else [])
    assert not sentinel.exists()


def test_missing_parent_at_start_does_not_terminate_server(server, monkeypatch, tmp_path):
    import os
    import threading

    if sys.platform == 'win32':
        pytest.skip('Unix process probing')
    calls = []
    def kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError('parent already gone')
        pytest.fail('watchdog attempted to terminate server')

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(os, 'kill', kill)
    monkeypatch.setattr(threading, 'Thread', ImmediateThread)
    server._start_parent_watchdog(123, str(tmp_path))
    assert calls == [(123, 0)]


def test_disable_watchdog_ignores_hangup_on_unix(server, monkeypatch):
    import signal

    if sys.platform == 'win32':
        pytest.skip('Unix signal handling')
    signals = []
    monkeypatch.setattr(server.watchdog_state, 'disabled', False)
    monkeypatch.setattr(signal, 'signal', lambda *args: signals.append(args))
    server.disable_watchdog()
    assert server.watchdog_state.disabled
    assert signals == [(signal.SIGHUP, signal.SIG_IGN)]


@pytest.mark.parametrize('exception', [BrokenPipeError, ValueError])
def test_server_output_survives_disconnected_desktop_pipe(server, exception):
    class BrokenStream:
        def write(self, _text):
            raise exception('pipe closed')
        def flush(self):
            raise exception('pipe closed')

    stream = server._PipeSafeStream(BrokenStream())
    assert stream.write('after exit') == len('after exit')
    assert stream.write('still usable') == len('still usable')
    stream.flush()
    stream._stream.close()


def test_server_output_flush_survives_disconnected_desktop_pipe(server):
    class BrokenStream:
        def flush(self):
            raise BrokenPipeError('pipe closed')

    stream = server._PipeSafeStream(BrokenStream())
    stream.flush()
    assert stream.write('still usable') == len('still usable')
    stream._stream.close()


@pytest.mark.parametrize('sentinel_owner,keeps_running', [(123, True), (456, False)])
def test_watchdog_distinguishes_current_exit_from_stale_sentinel(
    server, monkeypatch, tmp_path, sentinel_owner, keeps_running
):
    import os
    import signal
    import threading
    import time

    if sys.platform == 'win32':
        pytest.skip('Unix process probing')
    monkeypatch.setattr(server.watchdog_state, 'disabled', False)
    targets = []
    calls = []
    probes = 0

    class DeferredThread:
        def __init__(self, target, daemon):
            assert daemon
            targets.append(target)
        def start(self):
            pass

    def kill(pid, sig):
        nonlocal probes
        calls.append((pid, sig))
        if sig == 0:
            probes += 1
            if probes > 1:
                raise ProcessLookupError('parent exited')

    monkeypatch.setattr(threading, 'Thread', DeferredThread)
    monkeypatch.setattr(os, 'kill', kill)
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    server._start_parent_watchdog(123, str(tmp_path))
    # The desktop exits after the watchdog is scheduled but before its
    # thread starts running, so the signal must not be mistaken for stale.
    sentinel = tmp_path / f'.keep-running-{sentinel_owner}'
    sentinel.write_text('1')
    targets[0]()

    assert calls[:2] == [(123, 0), (123, 0)]
    assert calls[2:] == ([] if keeps_running else [(os.getpid(), signal.SIGTERM)])
    assert sentinel.exists() is (not keeps_running)
