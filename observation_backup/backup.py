"""Complete private snapshots, with a single immutable manifest seal."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from functools import wraps
import os
from contextvars import ContextVar
_publication = ContextVar('d4_publication', default=False)
from observation_store import filesystem as fs, StoreError
from . import contracts as c, io
from .validation import evidence

def _checkpoint(name):
    """Private deterministic test seam; never accepts production hook input."""

def boundary(operation):

    def decorate(fn):

        @wraps(fn)
        def call(*args, **kwargs):
            token = _publication.set(False)
            try:
                return fn(*args, **kwargs)
            except c.BackupError as error:
                raise c.BackupError('PUBLICATION_UNKNOWN' if _publication.get() else error.code, operation) from None
            except StoreError as error:
                code = {'OUTPUT_INVALID': 'LOCATION_INVALID', 'STORE_EXISTS': 'DESTINATION_EXISTS', 'OUTPUT_EXISTS': 'DESTINATION_EXISTS'}.get(error.code, error.code)
                raise c.BackupError('PUBLICATION_UNKNOWN' if _publication.get() else code if code in c.ERROR_CODES else 'INTEGRITY_ERROR', operation) from None
            except (ValueError, TypeError, KeyError, OverflowError):
                raise c.BackupError('PUBLICATION_UNKNOWN' if _publication.get() else 'INTEGRITY_ERROR', operation) from None
            except OSError:
                raise c.BackupError('PUBLICATION_UNKNOWN' if _publication.get() else 'STORAGE_ERROR', operation) from None
            except Exception:
                raise c.BackupError('PUBLICATION_UNKNOWN' if _publication.get() else 'INTERNAL_ERROR', operation) from None
            finally:
                _publication.reset(token)
        return call
    return decorate

def ids(*values):
    try:
        for value in values:
            c.d2.uuid4(value)
    except Exception:
        raise c.BackupError('INPUT_INVALID') from None

@contextmanager
def opened_backup(path, backup_id, flush=False):
    with ExitStack() as stack:
        root = stack.enter_context(fs.CheckedDir.open(io.location(path)))
        if 'manifest.json' not in root.names():
            raise c.BackupError('NOT_SEALED')
        c.require(set(root.names()) == {'manifest.json', 'objects', 'staging'})
        objects = stack.enter_context(root.child('objects'))
        staging = stack.enter_context(root.child('staging'))
        c.require(staging.names() == [])
        raw = fs.read_bytes(root, 'manifest.json')
        manifest = c.validate_manifest(c.json_bytes(raw))
        if manifest['backup_id'] != backup_id:
            raise c.BackupError('STORE_ID_MISMATCH')
        names = {item['sha256'] + '.bin' for item in manifest['files']}
        c.require(set(objects.names()) == names)
        pinned = {}
        for item in manifest['files']:
            name = item['sha256'] + '.bin'
            if name not in pinned:
                pinned[name] = io.stream(objects, name)
            c.require(pinned[name][:2] == (item['size'], item['sha256']))
        index = {x['path']: x for x in manifest['files']}

        def read(path):
            return fs.read_bytes(objects, index[path]['sha256'] + '.bin')
        data = evidence(manifest['directories'], manifest['files'], read)
        c.require(data[0] == manifest['source_manifest'] and data[1] == manifest['listing'])

        def recheck(do_flush=False):
            c.require(set(root.names()) == {'manifest.json', 'objects', 'staging'} and (not staging.names()))
            c.require(fs.read_bytes(root, 'manifest.json') == raw and set(objects.names()) == names)
            for name, expected in pinned.items():
                c.require(io.stream(objects, name, flush=do_flush) == expected)
            if do_flush:
                fs.flush_file(root, 'manifest.json')
                staging.flush()
                objects.flush()
                root.flush()
                with fs.CheckedDir.open(io.location(path).parent) as parent:
                    parent.flush()
            if do_flush:
                for name, expected in pinned.items():
                    c.require(io.stream(objects, name) == expected)
            c.require(fs.read_bytes(root, 'manifest.json') == raw and set(objects.names()) == names and (not staging.names()))
        if flush:
            recheck(True)
        yield (root, objects, manifest, data, read, recheck)
        recheck()

@boundary('backup')
def create_backup(store_dir, store_id, backup_dir, backup_id):
    ids(store_id, backup_id)
    fs.require_platform()
    source = io.location(store_dir)
    destination = io.location(backup_dir, [source], True)
    with fs.CheckedDir.open(source) as root, fs.writer_lock(root), io.tree(root) as (dirs, files):
        directories, entries = io.inventory(dirs, files)
        data = evidence(directories, entries, lambda p: fs.read_bytes(files[p][0], files[p][1]))
        if data[0]['store_id'] != store_id:
            raise c.BackupError('STORE_ID_MISMATCH')
        parent, out = fs.reserve_root(destination)
        published = False
        try:
            with out.mkdir('objects') as objects, out.mkdir('staging') as staging:
                copied = {}
                for path, (directory, name, size, sha, sig) in sorted(files.items()):
                    target = sha + '.bin'
                    if target not in copied:
                        with fs.regular(objects, target, create=True) as fd:
                            c.require(io.stream(directory, name, fd) == (size, sha, sig))
                            os.fsync(fd)
                        c.require(io.stream(objects, target)[:2] == (size, sha))
                        copied[target] = (size, sha)
                    else:
                        with fs.regular(directory, name) as a, fs.regular(objects, target) as b:
                            while True:
                                left = os.read(a, fs.CHUNK)
                                right = os.read(b, fs.CHUNK)
                                c.require(left == right)
                                if not left:
                                    break
                io.recheck(dirs, files)
                manifest = dict(schema_version='health-observation-backup-v1', backup_id=backup_id, captured_at=datetime.now(timezone.utc).isoformat(), scope='complete-d2-managed-tree-v1', source_manifest=data[0], directories=directories, files=entries, listing=data[1])
                c.validate_manifest(manifest)
                objects.flush()
                staging.flush()
                out.flush()
                _checkpoint('before-backup-seal')
                io.recheck(dirs, files)

                def sealed():
                    nonlocal published
                    published = True
                    _publication.set(True)
                    _checkpoint('after-backup-seal')
                io.publish(out, staging, 'manifest.json', manifest, sealed)
                parent.flush()
                with opened_backup(destination, backup_id, True):
                    pass
                io.recheck(dirs, files)
        except BaseException as error:
            if published or (isinstance(error, fs.PublicationError) and error.linked_may_have_occurred):
                raise c.BackupError('PUBLICATION_UNKNOWN') from None
            raise
        finally:
            out.close()
            parent.close()
    return c.acknowledgement('backup')

@boundary('verify')
def verify_backup(backup_dir, backup_id):
    ids(backup_id)
    fs.require_platform()
    with opened_backup(backup_dir, backup_id, True):
        pass
    return c.acknowledgement('verify')
