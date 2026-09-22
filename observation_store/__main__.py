"""Explicit private evidence files and argument-independent public envelopes."""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import uuid
from adapters.health_observations import InputSet
from . import store
from . import contracts as c, filesystem as fs
from .errors import StoreError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StoreError('CLI_USAGE')


def _output(path, store_path, inputs):
    requested = Path(path).absolute()
    if os.path.lexists(requested):
        raise StoreError('OUTPUT_EXISTS')
    resolved = fs.reject_location(requested)
    store_root = Path(store_path).resolve()
    if any((parent / 'manifest.json').exists() and (parent / 'writer.lock').exists() for parent in resolved.parents):
        raise StoreError('OUTPUT_INVALID')
    if resolved == store_root or store_root in resolved.parents:
        raise StoreError('OUTPUT_INVALID')
    if resolved in {Path(p).resolve() for p in inputs if p is not None}:
        raise StoreError('OUTPUT_INVALID')
    try:
        parent = fs.CheckedDir.open(resolved.parent)
    except StoreError:
        raise StoreError('OUTPUT_NOT_PRIVATE') from None
    return parent, resolved.name


def _write(output, blob):
    parent, name = output
    stage = '.observation-output-' + str(uuid.uuid4())
    fs.stage_bytes(parent, stage, blob)
    fs.publish(parent, stage, parent, name)


def main(argv=None):
    operation = 'observation_store'
    args = None
    output = failure_output = None
    result = None
    with ExitStack() as stack:
        try:
            parser = Parser(add_help=False)
            subs = parser.add_subparsers(dest='operation', required=True, parser_class=Parser)
            for name in ('init', 'status', 'import', 'read', 'list', 'recover'):
                command = subs.add_parser(name, add_help=False)
                for option in ('store', 'store-id', 'output'):
                    command.add_argument('--' + option, required=True)
                command.add_argument('--failure-output')
                if name in ('init', 'import', 'recover'):
                    command.add_argument('--definition', required=True, type=Path)
                if name in ('import', 'recover'):
                    command.add_argument('--attempt-id', required=True)
                if name == 'read':
                    command.add_argument('--batch-id', required=True)
                if name == 'import':
                    for option in ('archive', 'catalogue', 'binding'):
                        command.add_argument('--' + option, required=True, type=Path)
                    command.add_argument('--report', type=Path)
                    command.add_argument('--timezone', required=True)
                    command.add_argument('--as-of', required=True)
                    command.add_argument('--days', type=int, required=True)
            args = parser.parse_args(argv)
            operation = args.operation
            inputs = [getattr(args, k, None) for k in ('archive', 'catalogue', 'binding', 'report', 'definition')]
            output = _output(args.output, args.store, inputs)
            stack.enter_context(output[0])
            if args.failure_output is not None:
                failure_output = _output(args.failure_output, args.store, [*inputs, args.output])
                stack.enter_context(failure_output[0])
            definition = None
            if getattr(args, 'definition', None) is not None:
                try:
                    definition = c.strict_load(args.definition.read_bytes())
                except Exception:
                    raise StoreError('DEFINITION_INVALID') from None
            common = (args.store, args.store_id)
            if operation == 'init':
                result = store.initialize(*common, definition)
            elif operation == 'status':
                result = store.status(*common)
            elif operation == 'import':
                inputs_set = InputSet(args.archive, args.catalogue, args.binding, args.timezone, args.as_of, args.days, args.report)
                result = store.import_observations(*common, inputs_set, definition, args.attempt_id)
            elif operation == 'read':
                result = store.read_batch(*common, args.batch_id)
            elif operation == 'list':
                result = store.list_batches(*common)
            else:
                result = store.recover_attempt(*common, definition, args.attempt_id)
            try:
                _write(output, result)
            except Exception:
                code = 'PUBLICATION_UNKNOWN' if operation in ('import', 'recover') else ('INITIALIZATION_UNKNOWN' if operation == 'init' else 'OUTPUT_INVALID')
                raise StoreError(code, store_id=args.store_id, attempt_id=getattr(args, 'attempt_id', None)) from None
            value = c.strict_load(result)
            if operation == 'import' and value['outcome'] != 'COMMITTED':
                raise StoreError(value['error'], store_id=args.store_id, attempt_id=args.attempt_id)
            public, exit_code = dict(status='PASS', operation=operation, error=None), 0
        except Exception as error:
            if not isinstance(error, StoreError):
                error = StoreError('INTERNAL_ERROR')
            if failure_output is not None:
                try:
                    _write(failure_output, error.failure_bytes)
                except Exception:
                    # Preserve uncertainty classification if evidence publication fails.
                    if error.code not in ('PUBLICATION_UNKNOWN', 'INITIALIZATION_UNKNOWN'):
                        error = StoreError('OUTPUT_INVALID')
            public, exit_code = dict(status='FAIL', operation=operation, error=error.code), 2
        print(c.canonical(public).decode('ascii'))
        return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
