"""Exclusive output directories that no alias can redirect.

Storage location is caller policy. Every path component is opened without
resolving aliases and held open until close, so a symlink, junction or rename
cannot move a run outside its explicitly chosen directory tree. POSIX hosts do
this with directory descriptors and O_NOFOLLOW; Windows hosts pin each component
with a handle that denies delete sharing and never follows reparse points.
Everything created is private to the current user: mode 0700/0600 on POSIX, a
protected owner-and-SYSTEM DACL on Windows. Pre-existing ancestors are untouched.
"""

from contextlib import contextmanager
import functools
import os
from pathlib import Path

if os.name == "nt":
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    _kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _FILE_LIST_DIRECTORY = 0x1
    _GENERIC_WRITE = 0x40000000
    _SHARE_READ_WRITE = 0x1 | 0x2
    _CREATE_NEW, _OPEN_EXISTING = 1, 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _ERROR_ALREADY_EXISTS = 183
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _SDDL_REVISION_1 = 1

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit_handle", wintypes.BOOL)]


class _PosixRoot:
    """A run directory held as a descriptor; artifacts are created relative to it."""

    def __init__(self, path):
        if (not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
                or os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd):
            raise ValueError("safe output requires POSIX directory-relative no-follow IO; this host is unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parts = path.parts[1:]
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
            self.fd = current
        except BaseException:
            os.close(current)
            raise

    def create(self, name):
        return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)

    def close(self):
        os.close(self.fd)


class _WindowsRoot:
    """Every directory from the drive root to the run directory pinned by a handle.

    A handle with FILE_LIST_DIRECTORY access and no delete sharing blocks rename,
    removal and replacement of that directory (a handle with attribute-only access
    is exempt from share checks and blocks nothing). With each component pinned and
    verified from its handle, full-path creation inside the run directory cannot
    be redirected. Reserved device names and trailing dots are passed through
    literally by the extended-length prefix.
    """

    def __init__(self, path):
        self.handles = []
        try:
            self.handles.append(self._pin(path.anchor))
            parts = path.parts[1:]
            current = Path(path.anchor)
            with _owner_only_security(inheritable=True) as security:
                for index, part in enumerate(parts):
                    current = current / part
                    if not _kernel32.CreateDirectoryW(_extended(current), security):
                        if ctypes.get_last_error() != _ERROR_ALREADY_EXISTS:
                            raise _win_error(current)
                        if index == len(parts) - 1:
                            raise ValueError("output root already exists; choose a fresh run path")
                    self.handles.append(self._pin(current))
            final = _final_path(self.handles[-1])
            if os.path.normcase(final) != os.path.normcase(_extended(path)):
                raise ValueError(f"output root {path} resolves to {final}; choose a path on its real volume")
            self.fd = msvcrt.open_osfhandle(self.handles[-1], os.O_RDONLY)
            self.handles.pop()
            self.path = path
        except BaseException:
            for handle in self.handles:
                _kernel32.CloseHandle(handle)
            raise

    @staticmethod
    def _pin(directory):
        handle = _create_file(_extended(directory), _FILE_LIST_DIRECTORY, _OPEN_EXISTING,
                              _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT)
        try:
            info = _FileAttributeTagInfo()
            if not _kernel32.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO,
                                                          ctypes.byref(info), ctypes.sizeof(info)):
                raise _win_error(directory)
            if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"output path component {directory} is a reparse point (symlink, junction or "
                                 f"mount point, tag {info.reparse_tag:#x}); choose a path without aliases")
            if not info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
                raise NotADirectoryError(20, "Not a directory", str(directory))
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        return handle

    def create(self, name):
        with _owner_only_security(inheritable=False) as security:
            handle = _create_file(_extended(self.path / name), _GENERIC_WRITE, _CREATE_NEW,
                                  _FILE_FLAG_OPEN_REPARSE_POINT, security)
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)

    def close(self):
        while self.handles:
            _kernel32.CloseHandle(self.handles.pop())
        os.close(self.fd)


@functools.cache
def _current_user_sid():
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _win_error(None)
    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        user = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(token, _TOKEN_USER, user, size, ctypes.byref(size)):
            raise _win_error(None)
        text = wintypes.LPWSTR()
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID pointer.
        if not _advapi32.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(user).value, ctypes.byref(text)):
            raise _win_error(None)
        try:
            return text.value
        finally:
            _kernel32.LocalFree(text)
    finally:
        _kernel32.CloseHandle(token)


@contextmanager
def _owner_only_security(*, inheritable):
    """SECURITY_ATTRIBUTES with a protected DACL (nothing inherited from the
    parent) granting full access to the current user and SYSTEM only."""
    flags = "OICI" if inheritable else ""
    sddl = f"D:P(A;{flags};FA;;;{_current_user_sid()})(A;{flags};FA;;;SY)"
    descriptor, size = ctypes.c_void_p(), wintypes.ULONG()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, _SDDL_REVISION_1,
                                                                          ctypes.byref(descriptor), ctypes.byref(size)):
        raise _win_error(None)
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        yield ctypes.byref(attributes)
    finally:
        _kernel32.LocalFree(descriptor)


def _extended(path):
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _create_file(name, access, disposition, flags, security=None):
    handle = _kernel32.CreateFileW(name, access, _SHARE_READ_WRITE, security, disposition, flags, None)
    if handle == _INVALID_HANDLE:
        raise _win_error(name)
    return handle


def _final_path(handle):
    length = _kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
    if not length:
        raise _win_error(None)
    buffer = ctypes.create_unicode_buffer(length)
    if not _kernel32.GetFinalPathNameByHandleW(handle, buffer, length, 0):
        raise _win_error(None)
    return buffer.value


def _win_error(filename):
    code = ctypes.get_last_error()
    return OSError(0, ctypes.FormatError(code).strip(), str(filename) if filename else None, code)


_Root = _WindowsRoot if os.name == "nt" else _PosixRoot


class Output:
    """Create a fresh absolute directory without following any alias component."""

    def __init__(self, path):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("output root must be an absolute path without '..' components")
        if not path.parts[1:]:
            raise ValueError("output root must be a fresh subdirectory, not a filesystem root")
        self._root = _Root(path)
        self.fd, self.path = self._root.fd, path

    def open(self, name):
        # ':' names an NTFS alternate data stream, which would write through an existing file.
        if not name or name in (".", "..") or ":" in name or Path(name).name != name:
            raise ValueError("artifact name must be a single filename without ':'")
        return os.fdopen(self._root.create(name), "wb")

    def write_text(self, name, value):
        with self.open(name) as handle:
            handle.write(value.encode("utf-8"))

    def close(self):
        self._root.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
