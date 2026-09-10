"""Fixed public envelopes, explicit private artifacts, exclusive complete output."""
import argparse
import os
from pathlib import Path

from . import InputSet, PreparationError, inspect_definition, prepare_observations
from . import contracts as c
from .preparation import ROOT, _file_identity


class Parser(argparse.ArgumentParser):
    def error(self, _):
        raise PreparationError('CLI_USAGE')


def validate_output(path, inputs):
    path = Path(path).absolute()
    resolved = path.resolve(strict=False)
    if os.path.lexists(path):
        raise PreparationError('OUTPUT_EXISTS')
    if not resolved.parent.is_dir():
        raise PreparationError('OUTPUT_INVALID')
    if resolved.parent.stat().st_mode & 0o777 != 0o700:
        raise PreparationError('OUTPUT_NOT_PRIVATE')
    for parent in (resolved.parent, *resolved.parents):
        if parent == ROOT or (parent / '.git').exists():
            raise PreparationError('OUTPUT_NOT_PRIVATE')
    if resolved in {Path(p).resolve() for p in inputs if p is not None}:
        raise PreparationError('OUTPUT_INVALID')
    return resolved


def publish_private(path, blob, inputs):
    """Complete 0600 staging inode linked exclusively; no persistence guarantee."""
    resolved = validate_output(path, inputs)
    # Hold the parent directory open so ancestor replacement cannot redirect writes.
    fd = os.open(resolved.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = '.health-preparation-' + os.urandom(16).hex()
    staged = False
    try:
        mode = os.fstat(fd).st_mode & 0o777
        if mode != 0o700:
            raise PreparationError('OUTPUT_NOT_PRIVATE')
        file_fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
        staged = True
        with os.fdopen(file_fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(blob)
            stream.flush()
        # Recheck locators and permissions before making a complete object visible.
        validate_output(path, inputs)
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (resolved.parent.stat().st_dev, resolved.parent.stat().st_ino):
            raise PreparationError('OUTPUT_INVALID')
        try:
            os.link(name, resolved.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            raise PreparationError('OUTPUT_EXISTS') from None
    finally:
        if staged:
            os.unlink(name, dir_fd=fd)
        os.close(fd)


def main(argv=None):
    operation = 'health_observations'
    args = None
    inputs = []
    try:
        parser = Parser(add_help=False)
        parser.add_argument('operation', choices=['inspect-definition', 'prepare'])
        for name in ('archive', 'catalogue', 'binding', 'output'):
            parser.add_argument('--' + name, type=Path, required=True)
        for name in ('report', 'expected-definition', 'failure-output'):
            parser.add_argument('--' + name, type=Path)
        parser.add_argument('--timezone', required=True)
        parser.add_argument('--as-of', required=True)
        parser.add_argument('--days', type=int, required=True)
        args = parser.parse_args(argv)
        operation = args.operation
        if (operation == 'prepare') != (args.expected_definition is not None):
            raise PreparationError('CLI_USAGE')
        inputs = [args.archive, args.catalogue, args.binding, args.report, args.expected_definition]
        validate_output(args.output, inputs)
        if args.failure_output:
            validate_output(args.failure_output, [*inputs, args.output])
        input_set = InputSet(args.archive, args.catalogue, args.binding, args.timezone, args.as_of, args.days, args.report)
        # CLI definition file also remains immutable throughout admission.
        expected_identity = _file_identity(args.expected_definition) if args.expected_definition else None
        if operation == 'prepare':
            try:
                expected = c.strict_load(args.expected_definition.read_bytes())
            except Exception:
                raise PreparationError('DEFINITION_INVALID') from None
            result = prepare_observations(input_set, expected).private_bytes
        else:
            result = c.canonical(inspect_definition(input_set))
        if expected_identity is not None and _file_identity(args.expected_definition) != expected_identity:
            raise PreparationError('INPUT_CHANGED')
        publish_private(args.output, result, inputs)
        public, status = {'status': 'PASS', 'operation': operation}, 0
    except Exception as error:
        if not isinstance(error, PreparationError):
            error = PreparationError('INTERNAL_ERROR')
        if args is not None and args.failure_output:
            try:
                publish_private(args.failure_output, error.failure_bytes, [*inputs, args.output])
            except Exception:
                error = PreparationError('OUTPUT_INVALID')
        public, status = {'status': 'FAIL', 'operation': operation, 'error': error.code}, 2
    print(c.canonical(public).decode('ascii'))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
