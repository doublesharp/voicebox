"""Validate release archives before changing an installed backend."""

import os
import shutil
import tarfile
import tempfile
from pathlib import Path


def extract_release_archive(archive: Path, destination: Path) -> None:
    """Stage and validate a tarball before merging its contents into place.

    The data filter is required even on older Python versions: never silently
    fall back to unrestricted extraction when it is unavailable.
    """
    data_filter = getattr(tarfile, 'data_filter', None)
    if data_filter is None:
        raise RuntimeError('Safe tar extraction is unavailable on this Python runtime')
    with tempfile.TemporaryDirectory(prefix='.voicebox-extract-', dir=destination) as staging:
        with tarfile.open(archive, 'r:gz') as tar:
            # Validate against existing destination paths as well as the fresh
            # staging tree, including any symlinks already on disk.
            for member in tar.getmembers():
                data_filter(member, str(destination))
            tar.extractall(path=staging, filter='data')

        backup = Path(tempfile.mkdtemp(prefix='.voicebox-backup-', dir=destination))
        journal = []
        created_dirs = []
        preserve_backup = False
        try:
            _merge_staged(Path(staging), destination, backup, journal, created_dirs)
        except BaseException as error:
            failures = []
            for target, previous in reversed(journal):
                try:
                    if previous is not None:
                        os.replace(previous, target)
                    else:
                        target.unlink(missing_ok=True)
                except OSError as rollback_error:
                    failures.append(str(rollback_error))
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError as rollback_error:
                    failures.append(str(rollback_error))
            if failures:
                preserve_backup = True
                raise RuntimeError(
                    f'Release update failed and rollback was incomplete; backups retained at {backup}: '
                    + '; '.join(failures)
                ) from error
            raise
        finally:
            if not preserve_backup:
                shutil.rmtree(backup)


def _merge_staged(source: Path, destination: Path, backup: Path, journal: list, created_dirs: list) -> None:
    """Move validated files and links into place, retaining a rollback journal."""
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir() and not entry.is_symlink():
            if not target.exists():
                target.mkdir()
                created_dirs.append(target)
            _merge_staged(entry, target, backup, journal, created_dirs)
        else:
            if target.is_dir() and not target.is_symlink():
                raise IsADirectoryError(f'Cannot replace directory with file: {target}')
            previous = None
            if os.path.lexists(target):
                previous = backup / str(len(journal))
                os.replace(target, previous)
            journal.append((target, previous))
            # Rename replaces the link itself rather than following it.
            os.replace(entry, target)
