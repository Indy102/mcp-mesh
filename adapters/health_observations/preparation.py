"""Isolated evaluation and fresh replay against private per-operation snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import uuid
from xml.parsers import expat

from . import contracts as c

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 300
CHUNK = 1024 * 1024


class PreparationError(Exception):
    """Fixed public code; conflict identities are available only in private bytes."""
    def __init__(self, code, conflict=None):
        if code not in c.ERROR_CODES:
            code = 'INTERNAL_ERROR'
        super().__init__(code)
        self.code = code
        failure = {'schema_version': 'health-observation-failure-v1', 'status': 'FAIL', 'error': code, 'conflict': conflict}
        c.validate_failure(failure)
        self.failure_bytes = c.canonical(failure)


@dataclass(frozen=True, slots=True)
class InputSet:
    archive: Path
    catalogue: Path
    binding: Path
    timezone: str
    as_of: str
    days: int
    report: Path | None = None


@dataclass(frozen=True, slots=True)
class PreparedObservations:
    """Immutable bytes, not a persistence capability or RC2 FetchResult."""
    batch_bytes: bytes
    receipt_bytes: bytes

    def __post_init__(self):
        if type(self.batch_bytes) is not bytes or type(self.receipt_bytes) is not bytes:
            raise ValueError('CONTRACT_INVALID')
        c.validate_receipt(c.strict_load(self.receipt_bytes), c.strict_load(self.batch_bytes))

    @property
    def batch(self):
        return c.strict_load(self.batch_bytes)

    @property
    def receipt(self):
        return c.strict_load(self.receipt_bytes)

    @property
    def private_bytes(self):
        return c.canonical({'schema_version': 'health-observation-prepared-v1', 'batch': self.batch, 'receipt': self.receipt})


def _sha(blob):
    return hashlib.sha256(blob).hexdigest()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _file_identity(path, destination=None):
    """Hash one regular open handle; detect replacement or mutation while copying."""
    before_link = _signature(path.lstat())
    resolved = path.resolve(strict=True)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as src:
        before = _signature(os.fstat(src.fileno()))
        if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
            raise PreparationError('INPUT_INVALID')
        target = None
        try:
            if destination is not None:
                fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.fchmod(fd, 0o600)
                target = os.fdopen(fd, 'wb')
            h = hashlib.sha256()
            while chunk := src.read(CHUNK):
                h.update(chunk)
                if target:
                    target.write(chunk)
            if before != _signature(os.fstat(src.fileno())):
                raise PreparationError('INPUT_CHANGED')
        finally:
            if target:
                target.close()
    if before_link != _signature(path.lstat()) or resolved != path.resolve(strict=True) or before != _signature(path.stat()):
        raise PreparationError('INPUT_CHANGED')
    return (before_link, before, str(resolved), h.hexdigest())


def _code_identity():
    return {name: _sha((ROOT / 'spikes' / name).read_bytes()) for name in c.CODE_FILES}


def _adapter_identity():
    folder = Path(__file__).parent
    return c.digest({p.name: _sha(p.read_bytes()) for p in sorted(folder.glob('*.py'))})


def _run(tool, arguments):
    try:
        run = subprocess.run([sys.executable, '-B', str(ROOT / 'spikes' / tool), *map(str, arguments)],
                             capture_output=True, timeout=TIMEOUT, cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    except subprocess.TimeoutExpired:
        raise PreparationError('INPUT_RESOURCE_LIMIT') from None
    code = 'VERIFICATION_FAILED' if tool.endswith('verify_steps.py') else 'EVALUATION_FAILED'
    try:
        result = c.strict_load(run.stdout)
        if run.returncode != 0 or run.stderr:
            if isinstance(result, dict) and result.get('error') == 'INPUT_RESOURCE_LIMIT':
                raise PreparationError('INPUT_RESOURCE_LIMIT')
            raise PreparationError(code)
        if tool.endswith('verify_steps.py'):
            if result != {'status': 'PASS', 'verification_scope': 'selected_steps_report_exact_recomputation', 'checks': c.CHECKS, 'diagnostics': []}:
                raise PreparationError(code)
        elif result != {'status': 'PASS', 'operation': 'evaluate'}:
            raise PreparationError(code)
    except PreparationError:
        raise
    except Exception:
        raise PreparationError(code) from None


def _operating_inputs(inputs):
    if type(inputs) is not InputSet or type(inputs.days) is not int or inputs.days < 1 or type(inputs.as_of) is not str or type(inputs.timezone) is not str:
        raise PreparationError('INPUT_INVALID')
    try:
        c.zone_key(inputs.timezone)
        end = date.fromisoformat(inputs.as_of)
        if end.isoformat() != inputs.as_of:
            raise ValueError
        return {'as_of': inputs.as_of, 'days': inputs.days, 'start': (end - timedelta(days=inputs.days)).isoformat(), 'end': (end - timedelta(days=1)).isoformat()}
    except Exception:
        raise PreparationError('INPUT_INVALID') from None


def _prepare(inputs, expected):
    window = _operating_inputs(inputs)
    started = datetime.now(timezone.utc).isoformat()
    code_identity, adapter_identity = _code_identity(), _adapter_identity()
    runtime = {'implementation': platform.python_implementation(), 'version': platform.python_version(),
               'executable_sha256': _sha(Path(sys.executable).read_bytes()), 'expat_version': expat.EXPAT_VERSION, 'jsonschema_version': version('jsonschema')}
    # Explicit /tmp avoids a caller-controlled TMPDIR inside a checkout.
    with tempfile.TemporaryDirectory(prefix='health-observations-', dir='/tmp') as directory:
        scratch = Path(directory)
        os.chmod(scratch, 0o700)
        originals, snapshots = {}, {}
        for field in ('archive', 'catalogue', 'binding', 'report'):
            value = getattr(inputs, field)
            if value is None:
                continue
            path = Path(value).absolute()
            dest = scratch / field
            originals[path] = _file_identity(path, dest)
            snapshots[field] = dest
        def unchanged():
            for path, identity in originals.items():
                if _file_identity(path) != identity:
                    raise PreparationError('INPUT_CHANGED')
        archive, catalogue, binding = (snapshots[k] for k in ('archive', 'catalogue', 'binding'))
        snapshot_identities = {p: _file_identity(p) for p in snapshots.values()}
        if inputs.report is None:
            report_path = scratch / 'report'
            _run('health_steps/evaluate.py', ['--archive', archive, '--catalogue', catalogue, '--binding', binding,
                 '--timezone', inputs.timezone, '--as-of', inputs.as_of, '--days', inputs.days, '--output', report_path])
        else:
            report_path = snapshots['report']
        unchanged()
        # Capture exact bytes before replay; no unverified reread supplies the batch.
        blobs = {name: path.read_bytes() for name, path in [('catalogue', catalogue), ('binding', binding), ('report', report_path)]}
        objects = {name: c.strict_load(blob) for name, blob in blobs.items()}
        report = objects['report']['report']
        if c.canonical(report['window']) != c.canonical(window) or report['timezone']['key'] != inputs.timezone:
            raise PreparationError('OPERATING_INPUT_MISMATCH')
        report_identity = _file_identity(report_path)
        _run('health_steps/verify_steps.py', ['--archive', archive, '--catalogue', catalogue, '--binding', binding, '--report', report_path])
        for path, identity in snapshot_identities.items():
            if _file_identity(path) != identity:
                raise PreparationError('INPUT_CHANGED')
        if _file_identity(report_path) != report_identity:
            raise PreparationError('INPUT_CHANGED')
        for name, path in [('catalogue', catalogue), ('binding', binding), ('report', report_path)]:
            if _sha(blobs[name]) != _file_identity(path)[-1]:
                raise PreparationError('INPUT_CHANGED')
        unchanged()
        if code_identity != _code_identity() or adapter_identity != _adapter_identity():
            raise PreparationError('CODE_CHANGED')
        actual = c.derive_definition(objects['binding'], report)
        actual_id = c.definition_id(actual)
        if expected is not None and c.canonical(actual) != c.canonical(expected):
            raise PreparationError('DEFINITION_CONFLICT', {'actual_definition_id': actual_id, 'expected_definition_id': c.definition_id(expected),
                'differing_fields': sorted(k for k in actual if c.canonical(actual[k]) != c.canonical(expected[k]))})
        batch = {'schema_version': 'health-observation-batch-v1', 'definition': actual, 'definition_id': actual_id,
                 'archive': report['archive'], **objects}
        batch['batch_id'] = c.batch_id(batch)
        c.validate_batch(batch)
        receipt = {'schema_version': 'health-observation-verification-receipt-v1', 'attempt_id': str(uuid.uuid4()), 'batch_id': batch['batch_id'], 'batch_sha256': c.digest(batch),
                   'identities': c.identities(batch), 'outcome': 'PASS', 'verification_scope': 'selected_steps_report_exact_recomputation', 'checks': c.CHECKS[:],
                   'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(), 'input_mode': 'evaluated' if inputs.report is None else 'supplied_report',
                   'code_sha256': code_identity, 'adapter_sha256': adapter_identity, 'runtime': runtime,
                   'resource_envelope': {'subprocess_timeout_seconds': TIMEOUT, 'value_token_characters': '4096', 'exponent_magnitude': '100000'}}
        result = PreparedObservations(c.canonical(batch), c.canonical(receipt))
        unchanged()
        return result


def prepare_observations(input_set, expected_definition):
    """Fresh admission; expected_definition is content, never a supplied attestation."""
    try:
        try:
            expected = c.strict_load(c.canonical(expected_definition))
            c.validate_definition(expected)
        except Exception:
            raise PreparationError('DEFINITION_INVALID') from None
        return _prepare(input_set, expected)
    except PreparationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise PreparationError('INPUT_INVALID') from None
    except Exception:
        raise PreparationError('INTERNAL_ERROR') from None


def inspect_definition(input_set):
    """Run full evaluation/replay but return only a detached definition for review."""
    try:
        return _prepare(input_set, None).batch['definition']
    except PreparationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise PreparationError('INPUT_INVALID') from None
    except Exception:
        raise PreparationError('INTERNAL_ERROR') from None
