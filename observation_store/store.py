"""Private immutable store. A terminal COMMITTED result is the visibility point."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from functools import wraps
import hashlib
import re
import os
import stat
import uuid
from adapters.health_observations import InputSet, PreparationError, prepare_observations
from adapters.health_observations import contracts as d1
from . import contracts as c, filesystem as fs
from .errors import StoreError

DIRS = ('archives', 'batches', 'attempts', 'staging')
HEX = re.compile(r'^[0-9a-f]{64}$')


def _checkpoint(phase: str):
    """Private test seam; never configurable through the public interface."""


def _boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except StoreError:
            raise
        except (ValueError, TypeError, KeyError):
            raise StoreError('INTEGRITY_ERROR') from None
        except OSError:
            raise StoreError('STORAGE_ERROR') from None
        except Exception:
            raise StoreError('INTERNAL_ERROR') from None
    return wrapped


def _ids(store_id, attempt_id=None):
    try:
        c.uuid4(store_id)
        if attempt_id is not None:
            c.uuid4(attempt_id)
    except Exception:
        raise StoreError('INPUT_INVALID') from None


def _definition(expected):
    try:
        d1.validate_definition(expected)
        return c.strict_load(c.canonical(expected))
    except Exception:
        raise StoreError('DEFINITION_INVALID') from None


def _compare(actual, expected):
    if actual != expected:
        raise StoreError('DEFINITION_CONFLICT', conflict={
            'actual_definition_id': d1.definition_id(actual),
            'expected_definition_id': d1.definition_id(expected),
            'differing_fields': sorted(k for k in expected if actual[k] != expected[k])})


def _json(directory, name):
    blob = fs.read_bytes(directory, name)
    value = c.strict_load(blob)
    if c.canonical(value) != blob:
        raise StoreError('INTEGRITY_ERROR')
    return value


def _manifest(root, store_id, previous=None):
    if 'manifest.json' not in root.names():
        raise StoreError('UNINITIALIZED')
    value = _json(root, 'manifest.json')
    c.validate_manifest(value)
    if value['store_id'] != store_id:
        raise StoreError('STORE_ID_MISMATCH')
    if previous is not None and value != previous:
        raise StoreError('INTEGRITY_ERROR')
    return value


@contextmanager
def _opened(path, store_id):
    _ids(store_id)
    with ExitStack() as stack:
        root = stack.enter_context(fs.CheckedDir.open(path))
        manifest = _manifest(root, store_id)
        if set(root.names()) != {'manifest.json', 'writer.lock', *DIRS}:
            raise StoreError('INTEGRITY_ERROR')
        if fs.read_bytes(root, 'writer.lock') != b'':
            raise StoreError('INTEGRITY_ERROR')
        dirs = {name: stack.enter_context(root.child(name)) for name in DIRS}
        for key, suffix in (('archives', '.zip'), ('batches', '.json')):
            for name in dirs[key].names():
                if not name.endswith(suffix) or not HEX.fullmatch(name[:-len(suffix)]):
                    raise StoreError('INTEGRITY_ERROR')
                with fs.regular(dirs[key], name):
                    pass
        _check_staging(dirs['staging'])
        yield root, dirs, manifest


def _status(manifest, operation):
    value = dict(schema_version='health-observation-status-v1', status='PASS', operation=operation,
                 store_id=manifest['store_id'], definition_id=manifest['definition_id'],
                 initialized=True, flush_profile=manifest['flush_profile'])
    c.validate_status(value, manifest=manifest)
    return c.canonical(value)


def _check_staging(directory, depth=0):
    if depth > 1:
        raise StoreError('INTEGRITY_ERROR')
    for name in directory.names():
        info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with directory.child(name) as child:
                _check_staging(child, depth + 1)
        else:
            with fs.regular(directory, name):
                pass


@_boundary
def initialize(store_dir, store_id, expected_definition):
    _ids(store_id)
    expected = _definition(expected_definition)
    fs.require_platform()
    parent, root = fs.reserve_root(store_dir)
    try:
        _checkpoint('initialized-root')
        fs.create_lock(root)
        with fs.writer_lock(root):
            for name in DIRS:
                with root.mkdir(name) as directory:
                    directory.flush()
            manifest = dict(schema_version='health-observation-store-v1', store_id=store_id,
                            definition=expected, definition_id=d1.definition_id(expected),
                            flush_profile='fsync-files-and-directories-v1')
            c.validate_manifest(manifest)
            with root.child('staging') as staging:
                fs.stage_bytes(staging, 'manifest.json', c.canonical(manifest))
                root.flush()
                _checkpoint('before-manifest')
                try:
                    fs.publish(staging, 'manifest.json', root, 'manifest.json',
                               _after_link=lambda: _checkpoint('after-manifest-publish'))
                    root.flush()
                    parent.flush()
                except fs.PublicationError as error:
                    raise StoreError('INITIALIZATION_UNKNOWN' if error.linked_may_have_occurred else error.code) from None
                except StoreError:
                    raise StoreError('INITIALIZATION_UNKNOWN') from None
        return _status(manifest, 'init')
    finally:
        root.close()
        parent.close()


@_boundary
def status(store_dir, store_id):
    with _opened(store_dir, store_id) as (root, dirs, manifest):
        with fs.writer_lock(root):
            _manifest(root, store_id, manifest)
            for name in _attempt_names(dirs['attempts']):
                with fs.regular(dirs['attempts'], name):
                    pass
            try:
                fs.flush_file(root, 'writer.lock')
                fs.flush_file(root, 'manifest.json')
                for directory in dirs.values():
                    directory.flush()
                root.flush()
                with fs.CheckedDir.open(root.path.parent) as parent:
                    parent.flush()
            except StoreError:
                raise StoreError('INITIALIZATION_UNKNOWN') from None
        return _status(manifest, 'status')


def _attempt_names(directory):
    names = directory.names()
    for name in names:
        suffix = next((s for s in ('.intent.json', '.result.json') if name.endswith(s)), None)
        if suffix is None:
            raise StoreError('INTEGRITY_ERROR')
        c.uuid4(name[:-len(suffix)])
    return names


def _intent(dirs, manifest, attempt):
    value = _json(dirs['attempts'], attempt + '.intent.json')
    c.validate_intent(value, manifest=manifest, attempt_id=attempt)
    return value


def _batch(dirs, manifest, intent):
    value = _json(dirs['batches'], intent['batch_id'] + '.json')
    c.validate_intent(intent, manifest=manifest, attempt_id=intent['attempt_id'], batch=value)
    archive = fs.read_bytes(dirs['archives'], value['archive']['sha256'] + '.zip')
    if len(archive) != value['archive']['bytes'] or hashlib.sha256(archive).hexdigest() != value['archive']['sha256']:
        raise StoreError('INTEGRITY_ERROR')
    return value


def _result(dirs, manifest, attempt, names):
    value = _json(dirs['attempts'], attempt + '.result.json')
    intent = _intent(dirs, manifest, attempt) if attempt + '.intent.json' in names else None
    batch = _batch(dirs, manifest, intent) if value.get('outcome') == 'COMMITTED' and intent else None
    c.validate_result(value, manifest=manifest, attempt_id=attempt, intent=intent, batch=batch)
    if intent is not None and value['intent_sha256'] is None:
        raise StoreError('INTEGRITY_ERROR')
    return value, intent, batch


def _terminal(manifest, attempt, outcome, intent=None, error=None):
    committed = outcome == 'COMMITTED'
    return dict(schema_version='health-observation-result-v1', store_id=manifest['store_id'],
                attempt_id=attempt, definition_id=manifest['definition_id'], outcome=outcome,
                intent_sha256=c.digest(intent) if intent else None,
                batch_id=intent['batch_id'] if committed else None,
                batch_sha256=intent['batch_sha256'] if committed else None,
                verification=intent['verification'] if committed else None, error=error,
                recorded_at=datetime.now(timezone.utc).isoformat(), storage_version='health-observation-storage-v1')


def _record(root, dirs, manifest, attempt, value, intent=None, batch=None):
    c.validate_result(value, manifest=manifest, attempt_id=attempt, intent=intent, batch=batch)
    _manifest(root, manifest['store_id'], manifest)
    name = str(uuid.uuid4()) + '.json'
    fs.stage_bytes(dirs['staging'], name, c.canonical(value))
    try:
        fs.publish(dirs['staging'], name, dirs['attempts'], attempt + '.result.json',
                   _after_link=lambda: _checkpoint('after-result-publish'))
        _checkpoint('after-result-flush')
    except fs.PublicationError as error:
        if error.linked_may_have_occurred:
            raise StoreError('PUBLICATION_UNKNOWN', store_id=manifest['store_id'], attempt_id=attempt) from None
        raise
    except Exception:
        raise StoreError('PUBLICATION_UNKNOWN', store_id=manifest['store_id'], attempt_id=attempt) from None
    return c.canonical(value)


@_boundary
def import_observations(store_dir, store_id, input_set, expected_definition, attempt_id):
    _ids(store_id, attempt_id)
    expected = _definition(expected_definition)
    fs.require_platform()
    if type(input_set) is not InputSet:
        raise StoreError('INPUT_INVALID')
    with _opened(store_dir, store_id) as (root, dirs, manifest):
        with fs.writer_lock(root):
            _manifest(root, store_id, manifest)
            _compare(manifest['definition'], expected)
            names = _attempt_names(dirs['attempts'])
            if any(attempt_id + suffix in names for suffix in ('.intent.json', '.result.json')):
                raise StoreError('ATTEMPT_EXISTS')
            intent = None
            try:
                try:
                    prepared = prepare_observations(input_set, expected)
                except PreparationError as error:
                    detail = c.strict_load(error.failure_bytes)
                    code = error.code if error.code in ('DEFINITION_INVALID', 'DEFINITION_CONFLICT', 'INPUT_CHANGED') else 'PREPARATION_FAILED'
                    raise StoreError(code, conflict=detail['conflict']) from None
                batch, receipt = prepared.batch, prepared.receipt
                _compare(batch['definition'], expected)
                _compare(batch['definition'], manifest['definition'])
                _checkpoint('prepared')
                _manifest(root, store_id, manifest)
                intent = dict(schema_version='health-observation-intent-v1', store_id=store_id,
                              attempt_id=attempt_id, definition_id=manifest['definition_id'],
                              batch_id=batch['batch_id'], batch_sha256=c.digest(batch),
                              identities=d1.identities(batch), verification=receipt)
                c.validate_intent(intent, manifest=manifest, attempt_id=attempt_id, batch=batch)
                fs.stage_bytes(dirs['staging'], attempt_id + '.intent.json', c.canonical(intent))
                fs.publish(dirs['staging'], attempt_id + '.intent.json', dirs['attempts'], attempt_id + '.intent.json')
                _checkpoint('after-intent')
                with dirs['staging'].mkdir(attempt_id) as stage:
                    captured = fs.capture_archive(input_set.archive, stage, 'archive.zip')
                    if (captured.sha256, captured.size) != (batch['archive']['sha256'], batch['archive']['bytes']):
                        raise StoreError('INPUT_CHANGED')
                    fs.stage_bytes(stage, 'batch.json', prepared.batch_bytes)
                    fs.publish(stage, 'archive.zip', dirs['archives'], captured.sha256 + '.zip', allow_reuse=True)
                    _checkpoint('after-archive')
                    fs.publish(stage, 'batch.json', dirs['batches'], batch['batch_id'] + '.json', allow_reuse=True)
                    _checkpoint('after-batch')
                _checkpoint('before-result')
                with dirs['staging'].child(attempt_id) as stage:
                    confirmed = fs.capture_archive(input_set.archive, stage, 'archive-recheck.zip')
                    if confirmed != captured:
                        raise StoreError('INPUT_CHANGED')
                _batch(dirs, manifest, intent)
                _manifest(root, store_id, manifest)
                for directory, name in ((dirs['archives'], captured.sha256 + '.zip'), (dirs['batches'], batch['batch_id'] + '.json'), (dirs['attempts'], attempt_id + '.intent.json')):
                    fs.flush_file(directory, name)
                    directory.flush()
                root.flush()
                value = _terminal(manifest, attempt_id, 'COMMITTED', intent)
                return _record(root, dirs, manifest, attempt_id, value, intent, batch)
            except (StoreError, OSError, ValueError) as error:
                code = error.code if isinstance(error, StoreError) else ('INTEGRITY_ERROR' if isinstance(error, ValueError) else 'STORAGE_ERROR')
                if code in ('DEFINITION_INVALID', 'DEFINITION_CONFLICT', 'PUBLICATION_UNKNOWN'):
                    raise
                # Discover actual linkage; never claim a non-published intent exists.
                try:
                    names = _attempt_names(dirs['attempts'])
                    if attempt_id + '.result.json' in names:
                        raise StoreError('PUBLICATION_UNKNOWN', store_id=store_id, attempt_id=attempt_id)
                    intent = _intent(dirs, manifest, attempt_id) if attempt_id + '.intent.json' in names else None
                    value = _terminal(manifest, attempt_id, 'FAILED', intent, code)
                    return _record(root, dirs, manifest, attempt_id, value, intent)
                except StoreError as failure:
                    if failure.code == 'PUBLICATION_UNKNOWN':
                        raise
                    raise StoreError('STORAGE_ERROR', store_id=store_id, attempt_id=attempt_id) from None


@_boundary
def list_batches(store_dir, store_id):
    with _opened(store_dir, store_id) as (root, dirs, manifest):
        names = _attempt_names(dirs['attempts'])
        batches = {}
        for name in names:
            if name.endswith('.intent.json'):
                _intent(dirs, manifest, name[:-12])
            else:
                result, _, batch = _result(dirs, manifest, name[:-12], names)
                if batch is not None:
                    item = batches.setdefault(batch['batch_id'], dict(batch_id=batch['batch_id'], definition_id=batch['definition_id'],
                        archive_sha256=batch['archive']['sha256'], report_sha256=batch['report']['report_sha256'], committed_attempt_ids=[]))
                    item['committed_attempt_ids'].append(result['attempt_id'])
        for item in batches.values():
            item['committed_attempt_ids'].sort()
        value = dict(schema_version='health-observation-list-v1', status='PASS', operation='list', store_id=store_id,
                     definition_id=manifest['definition_id'], batches=[batches[k] for k in sorted(batches)])
        c.validate_listing(value, manifest=manifest)
        _manifest(root, store_id, manifest)
        return c.canonical(value)


@_boundary
def read_batch(store_dir, store_id, batch_id):
    if type(batch_id) is not str or not HEX.fullmatch(batch_id):
        raise StoreError('INPUT_INVALID')
    with _opened(store_dir, store_id) as (root, dirs, manifest):
        names = _attempt_names(dirs['attempts'])
        found = None
        for name in names:
            if name.endswith('.result.json'):
                result, _, batch = _result(dirs, manifest, name[:-12], names)
                if batch is not None and result['batch_id'] == batch_id:
                    found = c.canonical(batch)
        _manifest(root, store_id, manifest)
        if found is None:
            raise StoreError('BATCH_NOT_COMMITTED')
        return found


@_boundary
def recover_attempt(store_dir, store_id, expected_definition, attempt_id):
    _ids(store_id, attempt_id)
    expected = _definition(expected_definition)
    fs.require_platform()
    with _opened(store_dir, store_id) as (root, dirs, manifest):
        with fs.writer_lock(root):
            _manifest(root, store_id, manifest)
            _compare(manifest['definition'], expected)
            names = _attempt_names(dirs['attempts'])
            result = None
            if attempt_id + '.result.json' in names:
                result, intent, batch = _result(dirs, manifest, attempt_id, names)
                try:
                    if batch is not None:
                        fs.flush_file(dirs['archives'], batch['archive']['sha256'] + '.zip')
                        fs.flush_file(dirs['batches'], batch['batch_id'] + '.json')
                    if intent is not None:
                        fs.flush_file(dirs['attempts'], attempt_id + '.intent.json')
                    fs.flush_file(dirs['attempts'], attempt_id + '.result.json')
                    fs.flush_file(root, 'manifest.json')
                    for directory in dirs.values():
                        directory.flush()
                    root.flush()
                except StoreError:
                    raise StoreError('PUBLICATION_UNKNOWN', store_id=store_id, attempt_id=attempt_id) from None
            elif attempt_id + '.intent.json' in names:
                intent = _intent(dirs, manifest, attempt_id)
                result = _terminal(manifest, attempt_id, 'INTERRUPTED', intent, 'PREPARATION_FAILED')
                _record(root, dirs, manifest, attempt_id, result, intent)
            value = dict(schema_version='health-observation-recovery-v1', status='PASS', operation='recover', store_id=store_id,
                         attempt_id=attempt_id, outcome=result['outcome'] if result else 'NOT_RECORDED',
                         batch_id=result['batch_id'] if result else None, error=result['error'] if result else None)
            c.validate_recovery(value, store_id=store_id, attempt_id=attempt_id)
            return c.canonical(value)
