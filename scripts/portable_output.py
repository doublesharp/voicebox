"""Exclusive output directories on hosts with safe directory-relative POSIX IO.

Storage location is caller policy. Paths are traversed without resolving aliases,
so symlinks cannot redirect a run outside its explicitly chosen directory tree.
"""

import os
from pathlib import Path


class Output:
    """Create a fresh absolute directory without following any symlink component."""

    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("output root must be an absolute path without '..' components")
        parts = path.parts[1:]
        if not parts:
            raise ValueError("output root must be a fresh subdirectory, not a filesystem root")
        if (not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
                or os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd):
            raise ValueError("safe output requires POSIX directory-relative no-follow IO; this host is unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open(path.anchor, flags)
        try:
            for index, part in enumerate(parts):
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    if index == len(parts) - 1:
                        raise ValueError("output root already exists; choose a fresh run path") from None
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
            self.fd, self.path = current, path
        except BaseException:
            os.close(current)
            raise

    def open(self, name):
        if not name or name in (".", "..") or Path(name).name != name:
            raise ValueError("artifact name must be a single filename")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        return os.fdopen(fd, "wb")

    def write_text(self, name, value):
        with self.open(name) as handle:
            handle.write(value.encode("utf-8"))

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
