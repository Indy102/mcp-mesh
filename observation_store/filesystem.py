"""Checked macOS filesystem primitives. No preparation or receipt orchestration."""
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys
import threading
from .errors import StoreError

# macOS rename(2), SDK sys/stdio.h: int renameatx_np(int, const char *, int,
# const char *, unsigned int); RENAME_EXCL = 0x00000004. No fallback.
RENAME_EXCL = 0x4
CHUNK = 1024 * 1024
_registry_guard = threading.Lock()
_registry = {}
_registry_pid = os.getpid()


def require_platform():
    if sys.platform != 'darwin':
        raise StoreError('PLATFORM_UNSUPPORTED')


def _name(name):
    if type(name) is not str or name in ('', '.', '..') or '/' in name or '\x00' in name:
        raise StoreError('INPUT_INVALID')
    return name


def _identity(s):
    return s.st_dev, s.st_ino


def _private(s, directory=False):
    if not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)):
        raise StoreError('INTEGRITY_ERROR')
    if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != (0o700 if directory else 0o600):
        raise StoreError('INTEGRITY_ERROR')
    if not directory and s.st_nlink != 1:
        raise StoreError('INTEGRITY_ERROR')


def reject_location(path):
    """Reject canonical code/RC2 ancestors, including aliases into those trees."""
    path = Path(path).absolute()
    resolved = path.resolve(strict=False)
    for candidate in (path, resolved):
        for ancestor in (candidate, *candidate.parents):
            if any((ancestor / marker).exists() for marker in ('.git', 'current.json', 'generations')):
                raise StoreError('OUTPUT_INVALID')
    return resolved


class CheckedDir:
    """Pinned directory; every operation rechecks its pathname and parent chain."""
    def __init__(self, fd, *, path=None, parent=None, name=None):
        self.fd, self.path, self.parent, self.name = fd, path, parent, name
        self.identity = _identity(os.fstat(fd))
        self.file_identities = {}
        try:
            self.check()
        except BaseException:
            os.close(fd)
            self.fd = -1
            raise

    @classmethod
    def open(cls, path):
        path = reject_location(path)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
            return cls(fd, path=path)
        except OSError:
            raise StoreError('INTEGRITY_ERROR') from None

    def check(self):
        try:
            info = os.fstat(self.fd)
            _private(info, True)
            if self.parent is None:
                link = os.stat(self.path, follow_symlinks=False)
            else:
                self.parent.check()
                link = os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
            if _identity(info) != self.identity or _identity(link) != self.identity or not stat.S_ISDIR(link.st_mode):
                raise StoreError('INTEGRITY_ERROR')
        except OSError:
            raise StoreError('INTEGRITY_ERROR') from None
        return self

    def child(self, name):
        self.check()
        try:
            fd = os.open(_name(name), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            return CheckedDir(fd, parent=self, name=name)
        except OSError:
            raise StoreError('INTEGRITY_ERROR') from None

    def mkdir(self, name):
        require_platform()
        self.check()
        try:
            os.mkdir(_name(name), 0o700, dir_fd=self.fd)
            return self.child(name)
        except FileExistsError:
            raise StoreError('STORE_EXISTS') from None
        except OSError:
            raise StoreError('STORAGE_ERROR') from None

    def names(self):
        self.check()
        try:
            result = os.listdir(self.fd)
            self.check()
            return result
        except OSError:
            raise StoreError('STORAGE_ERROR') from None

    def flush(self):
        self.check()
        try:
            os.fsync(self.fd)
            self.check()
        except OSError:
            raise StoreError('STORAGE_ERROR') from None

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self.check()

    def __exit__(self, *args):
        self.close()


def reserve_root(path):
    """Return (private_parent, newly_created_root); caller owns both handles."""
    require_platform()
    resolved = reject_location(path)
    parent = CheckedDir.open(resolved.parent)
    try:
        root = parent.mkdir(resolved.name)
        return parent, root
    except BaseException:
        parent.close()
        raise


@contextmanager
def regular(directory, name, *, create=False):
    directory.check()
    if create:
        require_platform()
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY)
    fd = None
    try:
        fd = os.open(_name(name), flags, 0o600, dir_fd=directory.fd)
        initial = os.fstat(fd)
        _private(initial)
        pinned = directory.file_identities.setdefault(name, _identity(initial))
        if pinned != _identity(initial):
            raise StoreError('INTEGRITY_ERROR')
        link = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if _identity(link) != _identity(initial):
            raise StoreError('INTEGRITY_ERROR')
        yield fd
        directory.check()
        final = os.fstat(fd)
        _private(final)
        link = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if _identity(link) != _identity(initial):
            raise StoreError('INTEGRITY_ERROR')
    except FileExistsError:
        raise StoreError('OUTPUT_EXISTS') from None
    except OSError:
        raise StoreError('STORAGE_ERROR' if create else 'INTEGRITY_ERROR') from None
    finally:
        if fd is not None:
            os.close(fd)


def write_all(fd, blob):
    if type(blob) is not bytes:
        raise StoreError('INPUT_INVALID')
    try:
        offset = 0
        while offset < len(blob):
            n = os.write(fd, blob[offset:])
            if n <= 0:
                raise StoreError('STORAGE_ERROR')
            offset += n
    except OSError:
        raise StoreError('STORAGE_ERROR') from None


def stage_bytes(directory, name, blob):
    with regular(directory, name, create=True) as fd:
        write_all(fd, blob)
        try:
            os.fsync(fd)
        except OSError:
            raise StoreError('STORAGE_ERROR') from None
    directory.flush()


def _signature(s):
    # D1's documented ctime-only metadata exception; no content-lock guarantee.
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode


def read_bytes(directory, name):
    with regular(directory, name) as fd:
        before = _signature(os.fstat(fd))
        chunks = []
        while chunk := os.read(fd, CHUNK):
            chunks.append(chunk)
        if _signature(os.fstat(fd)) != before:
            raise StoreError('INTEGRITY_ERROR')
    return b''.join(chunks)


def flush_file(directory, name):
    with regular(directory, name) as fd:
        try:
            os.fsync(fd)
        except OSError:
            raise StoreError('STORAGE_ERROR') from None


def create_lock(root):
    """Initialization only. Exclusive, permanent, empty lock inode."""
    stage_bytes(root, 'writer.lock', b'')


@contextmanager
def writer_lock(root):
    global _registry_pid
    root.check()
    key, owner = root.identity, threading.get_ident()
    with _registry_guard:
        if _registry_pid != os.getpid():
            _registry.clear()
            _registry_pid = os.getpid()
        if key in _registry:
            raise StoreError('NESTED_OPERATION' if _registry[key] == owner else 'WRITER_BUSY')
        _registry[key] = owner
    try:
        with regular(root, 'writer.lock') as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StoreError('WRITER_BUSY' if exc.errno in (errno.EAGAIN, errno.EACCES) else 'STORAGE_ERROR') from None
            try:
                root.check()
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with _registry_guard:
            _registry.pop(key, None)


@dataclass(frozen=True)
class Publication:
    linked: bool
    reused: bool


class PublicationError(StoreError):
    """linked_may_have_occurred=True forbids contradictory terminal publication."""
    def __init__(self, code, *, linked_may_have_occurred):
        self.linked_may_have_occurred = linked_may_have_occurred
        super().__init__(code)


def _rename_exclusive(source, name, destination, target):
    require_platform()
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source.fd, os.fsencode(name), destination.fd, os.fsencode(target), RENAME_EXCL) != 0:
        number = ctypes.get_errno()
        raise OSError(number, 'exclusive publication failed')


def publish(source, name, destination, target, *, allow_reuse=False):
    """Publish an already flushed stage; compare complete bytes on allowed reuse."""
    linked = False
    try:
        require_platform()
        _name(name)
        _name(target)
        source.check()
        destination.check()
        if source.identity[0] != destination.identity[0]:
            raise StoreError('STORAGE_ERROR')
        expected = read_bytes(source, name)
        flush_file(source, name)
        source.check()
        destination.check()
        try:
            _rename_exclusive(source, name, destination, target)
            linked = True
        except FileExistsError:
            if not allow_reuse:
                raise StoreError('OUTPUT_EXISTS') from None
            if read_bytes(destination, target) != expected:
                raise StoreError('INTEGRITY_ERROR')
            flush_file(destination, target)
            destination.flush()
            return Publication(False, True)
        if read_bytes(destination, target) != expected:
            raise StoreError('INTEGRITY_ERROR')
        flush_file(destination, target)
        destination.flush()
        source.flush()
        return Publication(True, False)
    except (OSError, StoreError) as exc:
        code = exc.code if isinstance(exc, StoreError) else 'STORAGE_ERROR'
        raise PublicationError(code, linked_may_have_occurred=linked) from None


@dataclass(frozen=True)
class Capture:
    sha256: str
    size: int


def capture_archive(path, destination, name):
    """Copy each admission's source afresh, using one no-follow regular handle."""
    require_platform()
    path = Path(path).absolute()
    fd = None
    try:
        before_link = _signature(path.lstat())
        resolved = path.resolve(strict=True)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StoreError('INPUT_INVALID')
        before = _signature(info)
        if before != before_link:
            raise StoreError('INPUT_CHANGED')
        h, size = hashlib.sha256(), 0
        with regular(destination, name, create=True) as output:
            while chunk := os.read(fd, CHUNK):
                write_all(output, chunk)
                h.update(chunk)
                size += len(chunk)
            if (before != _signature(os.fstat(fd)) or before_link != _signature(path.lstat())
                    or resolved != path.resolve(strict=True) or before != _signature(path.stat())):
                raise StoreError('INPUT_CHANGED')
            try:
                os.fsync(output)
            except OSError:
                raise StoreError('STORAGE_ERROR') from None
        destination.flush()
        return Capture(h.hexdigest(), size)
    except OSError:
        raise StoreError('INPUT_CHANGED' if fd is not None else 'INPUT_INVALID') from None
    finally:
        if fd is not None:
            os.close(fd)
