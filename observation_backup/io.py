"""Pinned checked-handle traversal and bounded object transfers."""
from contextlib import ExitStack, contextmanager
import os, stat, hashlib
from pathlib import Path
from observation_store import filesystem as fs
from . import contracts as c

def signature(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode, s.st_uid, s.st_nlink)

def stream(directory, name, target=None, flush=False):
    with fs.regular(directory, name) as fd:
        before = signature(os.fstat(fd))
        h = hashlib.sha256()
        size = 0
        while (chunk := os.read(fd, fs.CHUNK)):
            h.update(chunk)
            size += len(chunk)
            if target is not None:
                fs.write_all(target, chunk)
        if flush:
            try:
                os.fsync(fd)
            except OSError:
                raise c.BackupError('STORAGE_ERROR') from None
        c.require(signature(os.fstat(fd)) == before)
    return (size, h.hexdigest(), before)

@contextmanager
def tree(root):
    with ExitStack() as stack:
        directories = {'': root}
        files = {}

        def walk(directory, path, depth):
            c.require(depth <= 3)
            for name in sorted(directory.names()):
                c.portable(name)
                c.require('/' not in name and name != '')
                key = path + '/' + name if path else name
                s = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
                if stat.S_ISDIR(s.st_mode):
                    child = stack.enter_context(directory.child(name))
                    directories[key] = child
                    walk(child, key, depth + 1)
                else:
                    size, sha, sig = stream(directory, name)
                    files[key] = (directory, name, size, sha, sig)
        walk(root, '', 0)
        yield (directories, files)

def inventory(directories, files):
    return (sorted(directories), [dict(path=k, size=v[2], sha256=v[3]) for k, v in sorted(files.items())])

def recheck(directories, files, flush=False):
    for path, directory in directories.items():
        expected = {k[len(path) + 1:] if path else k for k in set(directories) | set(files) if k and (k.rsplit('/', 1)[0] if '/' in k else '') == path}
        c.require(set(directory.names()) == expected)
    for directory, name, size, sha, sig in files.values():
        c.require(stream(directory, name, flush=flush) == (size, sha, sig))
    if flush:
        for path in sorted(directories, key=lambda x: x.count('/') + bool(x), reverse=True):
            directories[path].flush()

def location(path, others=(), destination=False):
    requested = Path(path).absolute()
    resolved = requested.resolve(strict=False)
    for part in (requested, *requested.parents):
        if part.is_symlink():
            raise c.BackupError('LOCATION_INVALID')
    fs.reject_location(requested)
    for other in others:
        for a, b in ((requested, Path(other).absolute()), (resolved, Path(other).resolve(strict=False))):
            if a == b or a in b.parents or b in a.parents:
                raise c.BackupError('LOCATION_INVALID')
    if destination:
        for ancestor in requested.parents:
            names = set(os.listdir(ancestor))
            if 'manifest.json' in names or 'writer.lock' in names or {'objects', 'staging'} <= names or ({'work', 'plan.json'} <= names) or ({'work', 'store'} <= names):
                raise c.BackupError('LOCATION_INVALID')
        if os.path.lexists(requested):
            raise c.BackupError('DESTINATION_EXISTS')
        with fs.CheckedDir.open(requested.parent):
            pass
    return requested

def publish(root, staging, name, value, checkpoint=None):
    fs.stage_bytes(staging, name, c.canonical(value))
    fs.publish(staging, name, root, name, _after_link=checkpoint)
    root.flush()
