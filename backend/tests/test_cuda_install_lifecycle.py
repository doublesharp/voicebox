"""CUDA backend update behavior with task-local files and no network or GPU."""

import asyncio
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from backend.services import cuda
from backend.utils.progress import ProgressManager


@pytest.fixture
def installer(monkeypatch, tmp_path):
    manager = ProgressManager()
    monkeypatch.setattr(cuda, 'get_data_dir', lambda: tmp_path)
    monkeypatch.setattr(cuda, 'ensure_cuda_download_supported', lambda: None)
    monkeypatch.setattr(cuda, 'server_asset_platform', lambda: 'linux-x86_64')
    monkeypatch.setattr(cuda, 'get_progress_manager', lambda: manager)
    class Client:
        def __init__(self, **_kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            pass
        async def head(self, _url):
            return SimpleNamespace(headers={'content-length': '100'})
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    destination = cuda.get_cuda_dir()
    (destination / cuda.get_cuda_exe_name()).write_bytes(b'old server')
    (destination / 'library.bin').write_bytes(b'old library')
    (destination / 'cuda-libs.json').write_text(json.dumps({'version': 'old'}))
    return destination, manager


@pytest.mark.asyncio
async def test_failed_library_update_preserves_complete_existing_install(installer, monkeypatch):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: True)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: True)
    async def download(_client, *, dest_dir, label, **_kwargs):
        if label == 'CUDA server':
            (dest_dir / cuda.get_cuda_exe_name()).write_bytes(b'new server')
            return 100
        (dest_dir / 'library.bin').write_bytes(b'incomplete library')
        raise RuntimeError('library download failed')
    monkeypatch.setattr(cuda, '_download_and_extract_archive', download)
    with pytest.raises(RuntimeError, match='library download failed'):
        await cuda._download_cuda_binary_locked('v1.2.3')
    assert (destination / cuda.get_cuda_exe_name()).read_bytes() == b'old server'
    assert (destination / 'library.bin').read_bytes() == b'old library'
    assert json.loads((destination / 'cuda-libs.json').read_text()) == {'version': 'old'}
    assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'error'
    assert sorted(path.name for path in destination.parent.iterdir()) == ['cuda']


@pytest.mark.asyncio
@pytest.mark.parametrize('need_server,need_libraries', [(True, True), (True, False), (False, True), (False, False)])
async def test_update_publishes_only_requested_components(installer, monkeypatch, need_server, need_libraries):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: need_server)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: need_libraries)
    downloads = []
    async def download(_client, *, dest_dir, label, progress_offset, **_kwargs):
        downloads.append((label, progress_offset))
        if label == 'CUDA server':
            (dest_dir / cuda.get_cuda_exe_name()).write_bytes(b'new server')
        else:
            (dest_dir / 'library.bin').write_bytes(b'new library')
        return 100
    monkeypatch.setattr(cuda, '_download_and_extract_archive', download)
    await cuda._download_cuda_binary_locked('v1.2.3')
    assert (destination / cuda.get_cuda_exe_name()).read_bytes() == (b'new server' if need_server else b'old server')
    assert (destination / 'library.bin').read_bytes() == (b'new library' if need_libraries else b'old library')
    version = json.loads((destination / 'cuda-libs.json').read_text())['version']
    assert version == (cuda.CUDA_LIBS_VERSION if need_libraries else 'old')
    assert downloads == ([('CUDA server', 0)] if need_server else []) + ([('CUDA libraries', 100 if need_server else 0)] if need_libraries else [])
    assert sorted(path.name for path in destination.parent.iterdir()) == ['cuda']
    if need_server or need_libraries:
        assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'complete'
    else:
        assert progress.get_progress(cuda.PROGRESS_KEY) is None


@pytest.mark.asyncio
async def test_archive_missing_server_executable_preserves_installed_backend(installer, monkeypatch):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: True)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: False)
    async def download(_client, **_kwargs):
        return 100
    monkeypatch.setattr(cuda, '_download_and_extract_archive', download)
    with pytest.raises(RuntimeError, match='did not contain the server executable'):
        await cuda._download_cuda_binary_locked('v1.2.3')
    assert (destination / cuda.get_cuda_exe_name()).read_bytes() == b'old server'
    assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'error'


@pytest.mark.asyncio
async def test_publish_failure_restores_existing_directory(installer, monkeypatch):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: True)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: False)
    async def download(_client, *, dest_dir, **_kwargs):
        (dest_dir / cuda.get_cuda_exe_name()).write_bytes(b'new server')
        return 100
    monkeypatch.setattr(cuda, '_download_and_extract_archive', download)
    real_rename = Path.rename
    def fail_publish(path, target):
        if path.name.startswith('.cuda-staging-') and not path.name.endswith('-backup'):
            raise OSError('publish failed')
        return real_rename(path, target)
    monkeypatch.setattr(Path, 'rename', fail_publish)
    with pytest.raises(OSError, match='publish failed'):
        await cuda._download_cuda_binary_locked('v1.2.3')
    assert (destination / cuda.get_cuda_exe_name()).read_bytes() == b'old server'
    assert (destination / 'library.bin').read_bytes() == b'old library'
    assert sorted(path.name for path in destination.parent.iterdir()) == ['cuda']
    assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'error'


@pytest.mark.asyncio
async def test_failed_rollback_retains_backup_for_recovery(installer, monkeypatch):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: True)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: False)
    async def download(_client, *, dest_dir, **_kwargs):
        (dest_dir / cuda.get_cuda_exe_name()).write_bytes(b'new server')
        return 100
    monkeypatch.setattr(cuda, '_download_and_extract_archive', download)
    real_rename = Path.rename
    def fail_publish_and_restore(path, target):
        if path.name.startswith('.cuda-staging-'):
            raise OSError('rename failed')
        return real_rename(path, target)
    monkeypatch.setattr(Path, 'rename', fail_publish_and_restore)
    with pytest.raises(OSError, match='rename failed'):
        await cuda._download_cuda_binary_locked('v1.2.3')
    backups = list(destination.parent.glob('.cuda-staging-*-backup'))
    assert len(backups) == 1
    assert (backups[0] / cuda.get_cuda_exe_name()).read_bytes() == b'old server'
    assert (backups[0] / 'library.bin').read_bytes() == b'old library'
    assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'error'


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as tar:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('valid_libraries', [True, False])
async def test_verified_archives_publish_together_or_preserve_existing_install(installer, monkeypatch, valid_libraries):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: True)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: True)
    server = archive([(cuda.get_cuda_exe_name(), b'new server')])
    libraries = archive([('library.bin', b'new library')])
    def respond(request):
        data = libraries if 'cuda-libs-' in request.url.path else server
        if request.method == 'HEAD':
            return httpx.Response(200, headers={'content-length': str(len(data))})
        if request.url.path.endswith('.sha256'):
            digest = hashlib.sha256(data).hexdigest()
            if data is libraries and not valid_libraries:
                digest = '0' * 64
            return httpx.Response(200, text=digest)
        return httpx.Response(200, content=data)
    real_client = httpx._client.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    if valid_libraries:
        await cuda._download_cuda_binary_locked('v1.2.3')
        assert (destination / cuda.get_cuda_exe_name()).read_bytes() == b'new server'
        assert (destination / 'library.bin').read_bytes() == b'new library'
        assert json.loads((destination / 'cuda-libs.json').read_text())['version'] == cuda.CUDA_LIBS_VERSION
        assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'complete'
    else:
        with pytest.raises(ValueError, match='integrity check failed'):
            await cuda._download_cuda_binary_locked('v1.2.3')
        assert (destination / cuda.get_cuda_exe_name()).read_bytes() == b'old server'
        assert (destination / 'library.bin').read_bytes() == b'old library'
        assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'error'
    assert sorted(path.name for path in destination.parent.iterdir()) == ['cuda']


@pytest.mark.asyncio
async def test_cancelled_download_cleans_staging_and_reports_failure(installer, monkeypatch):
    destination, progress = installer
    monkeypatch.setattr(cuda, '_needs_server_download', lambda _version: True)
    monkeypatch.setattr(cuda, '_needs_cuda_libs_download', lambda: False)
    started = asyncio.Event()
    async def download(_client, **_kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(cuda, '_download_and_extract_archive', download)
    task = asyncio.create_task(cuda._download_cuda_binary_locked('v1.2.3'))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (destination / cuda.get_cuda_exe_name()).read_bytes() == b'old server'
    assert sorted(path.name for path in destination.parent.iterdir()) == ['cuda']
    assert progress.get_progress(cuda.PROGRESS_KEY)['status'] == 'error'


@pytest.mark.asyncio
async def test_duplicate_download_does_not_start_a_second_update(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def update(version):
        calls.append(version)
        started.set()
        await release.wait()
    monkeypatch.setattr(cuda, '_download_lock', asyncio.Lock())
    monkeypatch.setattr(cuda, '_download_cuda_binary_locked', update)
    first = asyncio.create_task(cuda.download_cuda_binary('v1'))
    await asyncio.wait_for(started.wait(), 2)
    await cuda.download_cuda_binary('v2')
    release.set()
    await first
    assert calls == ['v1']


@pytest.mark.asyncio
async def test_deleting_cuda_backend_reports_whether_files_were_present(installer):
    destination, _progress = installer
    assert await cuda.delete_cuda_binary() is True
    assert not destination.exists()
    assert await cuda.delete_cuda_binary() is False
