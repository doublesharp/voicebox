"""GPU release archives are tested locally; no hardware or network needed."""

import hashlib
import io
import tarfile

import httpx
import pytest

from backend.services import cuda, rocm
from backend.utils.progress import ProgressManager


def archive(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w:gz') as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return result.getvalue()


def client_for(data, checksum=None, checksum_status=200):
    digest = checksum if checksum is not None else hashlib.sha256(data).hexdigest()
    def respond(request):
        if request.url.path.endswith('.sha256'):
            return httpx.Response(checksum_status, text=digest)
        return httpx.Response(200, content=data)
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


async def extract(module, dest, data, checksum=None, checksum_status=200):
    async with client_for(data, checksum, checksum_status) as client:
        return await module._download_and_extract_archive(client, 'https://example.invalid/archive', 'https://example.invalid/archive.sha256', dest, 'Test release', 0, len(data))


@pytest.fixture(params=[cuda, rocm], ids=['cuda', 'rocm'])
def backend(request, monkeypatch):
    manager = ProgressManager()
    monkeypatch.setattr(request.param, 'get_progress_manager', lambda: manager)
    return request.param, manager


@pytest.mark.asyncio
async def test_valid_verified_archive_updates_existing_backend(backend, tmp_path):
    module, progress = backend
    (tmp_path / 'server').write_bytes(b'old')
    data = archive([('server', b'new'), ('lib/library.bin', b'library')])
    assert await extract(module, tmp_path, data) == len(data)
    assert (tmp_path / 'server').read_bytes() == b'new'
    assert (tmp_path / 'lib' / 'library.bin').read_bytes() == b'library'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['lib', 'server']


@pytest.mark.asyncio
async def test_checksum_mismatch_preserves_installed_files(backend, tmp_path):
    module, progress = backend
    (tmp_path / 'server').write_bytes(b'old')
    data = archive([('server', b'new')])
    with pytest.raises(ValueError, match='integrity check failed'):
        await extract(module, tmp_path, data, checksum='0' * 64)
    assert (tmp_path / 'server').read_bytes() == b'old'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['server']


@pytest.mark.asyncio
async def test_checksum_fetch_failure_preserves_installed_files(backend, tmp_path):
    module, progress = backend
    (tmp_path / 'server').write_bytes(b'old')
    with pytest.raises(RuntimeError, match='failed to fetch checksum'):
        await extract(module, tmp_path, archive([('server', b'new')]), checksum_status=404)
    assert (tmp_path / 'server').read_bytes() == b'old'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['server']


@pytest.mark.asyncio
async def test_invalid_later_member_cannot_partially_overwrite_backend(backend, tmp_path):
    module, progress = backend
    (tmp_path / 'server').write_bytes(b'old')
    data = archive([('server', b'new'), ('../escaped', b'bad')])
    with pytest.raises((tarfile.FilterError, OSError, ValueError)):
        await extract(module, tmp_path, data)
    assert (tmp_path / 'server').read_bytes() == b'old'
    assert not (tmp_path.parent / 'escaped').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['server']


@pytest.mark.asyncio
async def test_existing_destination_symlink_cannot_redirect_files_outside_backend(backend, tmp_path):
    module, progress = backend
    destination = tmp_path / 'backend'
    outside = tmp_path / 'outside'
    destination.mkdir()
    outside.mkdir()
    (destination / 'server').write_bytes(b'old')
    (destination / 'lib').symlink_to(outside, target_is_directory=True)
    data = archive([('server', b'new'), ('lib/escape.bin', b'bad')])
    with pytest.raises((tarfile.FilterError, OSError, ValueError)):
        await extract(module, destination, data)
    assert (destination / 'server').read_bytes() == b'old'
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_release_can_replace_existing_internal_symlink(backend, tmp_path):
    module, progress = backend
    (tmp_path / 'old.so').write_bytes(b'old')
    (tmp_path / 'current.so').symlink_to('old.so')
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w:gz') as tar:
        info = tarfile.TarInfo('new.so')
        info.size = 3
        tar.addfile(info, io.BytesIO(b'new'))
        link = tarfile.TarInfo('current.so')
        link.type = tarfile.SYMTYPE
        link.linkname = 'new.so'
        tar.addfile(link)
    await extract(module, tmp_path, result.getvalue())
    assert (tmp_path / 'current.so').is_symlink()
    assert (tmp_path / 'current.so').read_bytes() == b'new'
    assert (tmp_path / 'old.so').read_bytes() == b'old'


@pytest.mark.asyncio
async def test_corrupt_archive_preserves_installed_files(backend, tmp_path):
    module, progress = backend
    (tmp_path / 'server').write_bytes(b'old')
    data = b'not a gzip archive'
    with pytest.raises((tarfile.TarError, OSError)):
        await extract(module, tmp_path, data)
    assert (tmp_path / 'server').read_bytes() == b'old'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['server']


@pytest.mark.asyncio
async def test_missing_safe_tar_filter_fails_closed(backend, tmp_path, monkeypatch):
    module, progress = backend
    (tmp_path / 'server').write_bytes(b'old')
    data = archive([('server', b'new')])
    monkeypatch.delattr(tarfile, 'data_filter')
    with pytest.raises(RuntimeError, match='Safe tar extraction is unavailable'):
        await extract(module, tmp_path, data)
    assert (tmp_path / 'server').read_bytes() == b'old'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['server']


@pytest.mark.asyncio
async def test_publish_failure_restores_previously_replaced_files(backend, tmp_path, monkeypatch):
    from backend.utils import release_archive

    module, progress = backend
    (tmp_path / 'first').write_bytes(b'old-first')
    (tmp_path / 'second').write_bytes(b'old-second')
    data = archive([('first', b'new-first'), ('second', b'new-second')])
    real_replace = release_archive.os.replace
    failed = False
    def fail_once(src, dst):
        nonlocal failed
        if str(dst).endswith('/second') and not failed:
            failed = True
            raise OSError('simulated disk failure')
        return real_replace(src, dst)
    monkeypatch.setattr(release_archive.os, 'replace', fail_once)
    with pytest.raises(OSError, match='simulated disk failure'):
        await extract(module, tmp_path, data)
    assert (tmp_path / 'first').read_bytes() == b'old-first'
    assert (tmp_path / 'second').read_bytes() == b'old-second'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['first', 'second']


@pytest.mark.asyncio
async def test_incomplete_rollback_retains_backup_for_recovery(backend, tmp_path, monkeypatch):
    from backend.utils import release_archive

    module, progress = backend
    (tmp_path / 'first').write_bytes(b'old-first')
    (tmp_path / 'second').write_bytes(b'old-second')
    data = archive([('first', b'new-first'), ('second', b'new-second')])
    real_replace = release_archive.os.replace
    def fail_target(src, dst):
        if str(dst).endswith('/second'):
            raise OSError('cannot replace second')
        return real_replace(src, dst)
    monkeypatch.setattr(release_archive.os, 'replace', fail_target)
    with pytest.raises(RuntimeError, match='rollback was incomplete') as error:
        await extract(module, tmp_path, data)
    assert (tmp_path / 'first').read_bytes() == b'old-first'
    backups = list(tmp_path.glob('.voicebox-backup-*'))
    assert len(backups) == 1
    assert str(backups[0]) in str(error.value)
    assert [p.read_bytes() for p in backups[0].iterdir()] == [b'old-second']


@pytest.mark.asyncio
async def test_failed_publish_removes_new_files_and_directories(backend, tmp_path, monkeypatch):
    from backend.utils import release_archive

    module, progress = backend
    (tmp_path / 'second').write_bytes(b'old-second')
    data = archive([('newdir/first', b'new'), ('second', b'new-second')])
    real_replace = release_archive.os.replace
    failed = False
    def fail_once(src, dst):
        nonlocal failed
        if str(dst).endswith('/second') and not failed:
            failed = True
            raise OSError('simulated failure')
        return real_replace(src, dst)
    monkeypatch.setattr(release_archive.os, 'replace', fail_once)
    with pytest.raises(OSError, match='simulated failure'):
        await extract(module, tmp_path, data)
    assert (tmp_path / 'second').read_bytes() == b'old-second'
    assert not (tmp_path / 'newdir').exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['second']
