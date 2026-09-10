#!/usr/bin/env python3
"""Read-only independent exact verification of a selected-source steps report."""
import argparse
from pathlib import Path
import sys

# The catalogue oracle's public gate is the only cross-spike dependency.
_import_path = sys.path[:]
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'health_catalogue'))
    from verify_catalogue import verify as verify_catalogue
finally:
    sys.path[:] = _import_path
    del _import_path
from oracle_core import canonical, digest, reconstruct, strict_read, unchanged
from oracle_time import Refusal

SCOPE = 'selected_steps_report_exact_recomputation'
CHECKS = ['binding_and_archive', 'pinned_timezone_boundaries', 'daily_steps_and_diagnostics', 'report_identity']


def verify(archive, catalogue, binding, report):
    archive, catalogue, binding, report = map(Path, (archive, catalogue, binding, report))
    cat, cat_hash = strict_read(catalogue)
    bound, binding_hash = strict_read(binding)
    envelope, report_hash = strict_read(report)
    if type(envelope) is not dict or set(envelope) != {'report', 'report_sha256'} or type(envelope['report_sha256']) is not str or envelope['report_sha256'] != digest(envelope['report']):
        raise Refusal('REPORT_INVALID')
    try:
        verify_catalogue(archive, catalogue, binding)
    except Exception:
        raise Refusal('BINDING_AND_ARCHIVE_INVALID') from None
    unchanged(catalogue, cat_hash)
    unchanged(binding, binding_hash)
    expected = reconstruct(archive, cat['catalogue'], bound, envelope['report'])
    unchanged(catalogue, cat_hash)
    unchanged(binding, binding_hash)
    unchanged(report, report_hash)
    if canonical(expected) != canonical(envelope['report']):
        raise Refusal('REPORT_MISMATCH')


class SafeParser(argparse.ArgumentParser):
    def error(self, _):
        raise Refusal('ARGUMENTS_INVALID')


def main(argv=None):
    try:
        parser = SafeParser(add_help=False)
        for name in ('archive', 'catalogue', 'binding', 'report'):
            parser.add_argument('--' + name, required=True, type=Path)
        args = parser.parse_args(argv)
        verify(args.archive, args.catalogue, args.binding, args.report)
        result = {'status': 'PASS', 'verification_scope': SCOPE, 'checks': CHECKS, 'diagnostics': []}
        code = 0
    except Exception as error:
        result = {'status': 'FAIL', 'verification_scope': SCOPE, 'checks': [], 'diagnostics': [str(error) if isinstance(error, Refusal) else 'INTERNAL_ERROR']}
        code = 2
    print(canonical(result).decode('ascii'))
    return code


if __name__ == '__main__':
    sys.exit(main())
