"""Model cache migration must not destroy data or report false success."""

import asyncio
import json

import pytest
from fastapi import HTTPException
from huggingface_hub import constants

from backend import models
from backend.routes import models as routes
from backend.utils.progress import ProgressManager


@pytest.fixture
def migration(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    destination = tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    manager = ProgressManager()
    tasks = []
    monkeypatch.setattr(constants, 'HF_HUB_CACHE', str(source))
    monkeypatch.setattr(routes, 'get_progress_manager', lambda: manager)
    def start(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    monkeypatch.setattr(routes, 'create_background_task', start)
    return source, destination, manager, tasks


def model(parent, name, content):
    directory = parent / f'models--{name}'
    directory.mkdir()
    (directory / 'weight.bin').write_bytes(content)
    return directory


@pytest.mark.asyncio
async def test_successful_migration_and_late_progress_subscriber(migration):
    source, destination, manager, tasks = migration
    model(source, 'first', b'one')
    model(source, 'second', b'two')
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    await asyncio.gather(*tasks)
    assert (destination / 'models--first' / 'weight.bin').read_bytes() == b'one'
    assert not (source / 'models--first').exists()
    assert manager.get_progress('migration')['status'] == 'complete'
    response = await routes.get_migration_progress()
    event = await asyncio.wait_for(anext(response.body_iterator), 2)
    assert json.loads(event.removeprefix('data: ').strip())['status'] == 'complete'
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_destination_conflict_preserves_both_caches(migration):
    source, destination, manager, tasks = migration
    model(source, 'first', b'source')
    model(destination, 'first', b'destination')
    with pytest.raises(HTTPException) as error:
        await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    assert error.value.status_code == 409
    assert (source / 'models--first' / 'weight.bin').read_bytes() == b'source'
    assert (destination / 'models--first' / 'weight.bin').read_bytes() == b'destination'
    assert not tasks


@pytest.mark.asyncio
async def test_move_failure_reports_error_and_preserves_unmoved_source(migration, monkeypatch):
    source, destination, manager, tasks = migration
    model(source, 'first', b'original')
    def fail(*args, **kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr(routes.shutil, 'move', fail)
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    await asyncio.gather(*tasks)
    assert manager.get_progress('migration')['status'] == 'error'
    assert 'disk unavailable' in manager.get_progress('migration')['error']
    assert (source / 'models--first' / 'weight.bin').read_bytes() == b'original'
    assert not (destination / 'models--first').exists()


@pytest.mark.asyncio
async def test_existing_terminal_progress_is_reset_when_new_migration_starts(migration):
    source, destination, manager, tasks = migration
    model(source, 'first', b'original')
    manager.update_progress('migration', 1, 1, status='complete')
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    assert manager.get_progress('migration')['status'] == 'downloading'
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_empty_cache_reports_zero_without_starting_work(migration):
    source, destination, manager, tasks = migration
    result = await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    assert result['moved'] == 0
    assert not tasks


@pytest.mark.asyncio
@pytest.mark.parametrize('destination_kind', ['same', 'child'])
async def test_rejects_same_directory_and_nested_destination(migration, destination_kind):
    source, destination, manager, tasks = migration
    target = source if destination_kind == 'same' else source / 'child'
    with pytest.raises(HTTPException) as error:
        await routes.migrate_models(models.ModelMigrateRequest(destination=str(target)))
    assert error.value.status_code == 400
    assert not tasks


@pytest.mark.asyncio
async def test_later_move_failure_rolls_back_previous_move(migration, monkeypatch):
    source, destination, manager, tasks = migration
    model(source, 'first', b'first')
    model(source, 'second', b'second')
    original = routes.shutil.move
    calls = []
    def fail_second(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError('second move failed')
        return original(src, dst)
    monkeypatch.setattr(routes.shutil, 'move', fail_second)
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    await asyncio.gather(*tasks)
    assert manager.get_progress('migration')['status'] == 'error'
    assert (source / 'models--first' / 'weight.bin').read_bytes() == b'first'
    assert (source / 'models--second' / 'weight.bin').read_bytes() == b'second'
    assert list(destination.iterdir()) == []


@pytest.mark.asyncio
async def test_cross_filesystem_migration_preserves_cache_symlinks(migration, monkeypatch):
    source, destination, manager, tasks = migration
    root = model(source, 'first', b'weight')
    (root / 'snapshot').mkdir()
    (root / 'snapshot' / 'model.bin').symlink_to('../weight.bin')
    monkeypatch.setattr(routes, '_same_filesystem', lambda *_: False)
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    await asyncio.gather(*tasks)
    link = destination / 'models--first' / 'snapshot' / 'model.bin'
    assert link.is_symlink()
    assert link.read_bytes() == b'weight'
    assert not root.exists()
    assert manager.get_progress('migration')['status'] == 'complete'


@pytest.mark.asyncio
async def test_cross_filesystem_copy_failure_keeps_all_originals(migration, monkeypatch):
    source, destination, manager, tasks = migration
    model(source, 'first', b'first')
    model(source, 'second', b'second')
    monkeypatch.setattr(routes, '_same_filesystem', lambda *_: False)
    original = routes._copy_with_progress
    count = 0
    def fail_second(src, dst, *args):
        nonlocal count
        count += 1
        if count == 2:
            (dst / 'partial').write_bytes(b'incomplete')
            raise OSError('disk full')
        return original(src, dst, *args)
    monkeypatch.setattr(routes, '_copy_with_progress', fail_second)
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    await asyncio.gather(*tasks)
    assert manager.get_progress('migration')['status'] == 'error'
    assert (source / 'models--first' / 'weight.bin').read_bytes() == b'first'
    assert (source / 'models--second' / 'weight.bin').read_bytes() == b'second'
    assert list(destination.iterdir()) == []


@pytest.mark.asyncio
async def test_cross_filesystem_cleanup_failure_retains_all_destination_copies(migration, monkeypatch):
    source, destination, manager, tasks = migration
    model(source, 'first', b'first')
    model(source, 'second', b'second')
    monkeypatch.setattr(routes, '_same_filesystem', lambda *_: False)
    real_remove = routes.shutil.rmtree
    def fail_cleanup(path, *args, **kwargs):
        if str(path).startswith(str(source)):
            raise OSError('source cleanup failed')
        return real_remove(path, *args, **kwargs)
    monkeypatch.setattr(routes.shutil, 'rmtree', fail_cleanup)
    await routes.migrate_models(models.ModelMigrateRequest(destination=str(destination)))
    await asyncio.gather(*tasks)
    progress = manager.get_progress('migration')
    assert progress['status'] == 'error'
    assert 'source cleanup was incomplete' in progress['error']
    assert (destination / 'models--first' / 'weight.bin').read_bytes() == b'first'
    assert (destination / 'models--second' / 'weight.bin').read_bytes() == b'second'


@pytest.mark.asyncio
async def test_overlapping_migrations_are_rejected_before_second_task(migration):
    source, destination, manager, tasks = migration
    model(source, 'first', b'original')
    request = models.ModelMigrateRequest(destination=str(destination))
    await routes.migrate_models(request)
    with pytest.raises(HTTPException) as error:
        await routes.migrate_models(request)
    assert error.value.status_code == 409
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    assert manager.get_progress('migration')['status'] == 'complete'
    assert (destination / 'models--first' / 'weight.bin').read_bytes() == b'original'
