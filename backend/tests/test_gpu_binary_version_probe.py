"""Backend auto-update must trust only successful unambiguous version probes."""
import subprocess
from types import SimpleNamespace

import pytest

from backend.services import cuda, rocm


@pytest.mark.parametrize('service,kind', [(cuda, 'cuda'), (rocm, 'rocm')])
@pytest.mark.parametrize('returncode,stdout,expected', [
    (0, 'voicebox-server 0.6.0\n', '0.6.0'),
    (0, 'warning\nvoicebox-server 0.6.0\n', '0.6.0'),
    (1, 'voicebox-server 0.6.0\n', None),
    (0, 'other-voicebox-server 0.6.0\n', None),
    (0, 'voicebox-server 0.6.0 extra\n', None),
    (0, 'voicebox-server 0.6.0\nvoicebox-server 0.7.0\n', None),
    (0, 'voicebox-server\n', None),
    (0, '', None),
])
def test_backend_version_only_accepts_successful_unambiguous_server_output(monkeypatch, tmp_path, service, kind, returncode, stdout, expected):
    path = tmp_path / ('voicebox-server-' + kind)
    path.write_bytes(b'placeholder')
    monkeypatch.setattr(service, f'get_{kind}_binary_path', lambda: path)
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=stdout, returncode=returncode)
    monkeypatch.setattr(subprocess, 'run', run)
    assert getattr(service, f'get_{kind}_binary_version')() == expected
    assert calls[0][0] == [str(path), '--version']
    assert calls[0][1]['cwd'] == str(path.parent)
    assert calls[0][1]['timeout'] == 30


@pytest.mark.parametrize('service,kind', [(cuda, 'cuda'), (rocm, 'rocm')])
def test_failed_version_probe_requires_server_update(monkeypatch, tmp_path, service, kind):
    path = tmp_path / 'server'
    path.write_bytes(b'placeholder')
    monkeypatch.setattr(service, f'get_{kind}_binary_path', lambda: path)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='voicebox-server 1.2.3\n'))
    assert service._needs_server_download('v1.2.3')


@pytest.mark.parametrize('service,kind', [(cuda, 'cuda'), (rocm, 'rocm')])
def test_missing_or_timed_out_binary_is_unknown(monkeypatch, tmp_path, service, kind):
    monkeypatch.setattr(service, f'get_{kind}_binary_path', lambda: None)
    assert getattr(service, f'get_{kind}_binary_version')() is None
    path = tmp_path / 'server'
    monkeypatch.setattr(service, f'get_{kind}_binary_path', lambda: path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)
    monkeypatch.setattr(subprocess, 'run', timeout)
    assert getattr(service, f'get_{kind}_binary_version')() is None
