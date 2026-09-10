#!/usr/bin/env python3
"""Produce a private, standalone selected-source steps report."""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import math
import os
from pathlib import Path, PurePosixPath
import sys
import zipfile
import zlib
from xml.parsers import expat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'health_catalogue'))
import catalogue
from steps_core import Accumulator
from steps_time import StepsError, day_intervals, pinned_zone, window_dates

CHUNK = 1024 * 1024
CHECKOUT = Path(__file__).resolve().parents[2]


def strict_snapshot(path, code):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def reject(_):
        raise ValueError
    def floating(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError
        return value
    try:
        blob = path.read_bytes()
        obj = json.loads(blob.decode('utf-8'), object_pairs_hook=pairs,
                         parse_constant=reject, parse_float=floating)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise StepsError(code) from None
    return obj, hashlib.sha256(blob).digest()


def check_snapshot(path, digest):
    try:
        current = hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        raise StepsError('INPUT_CHANGED') from None
    if current != digest:
        raise StepsError('INPUT_CHANGED')


def validate_binding(envelope, cat, cat_digest):
    code = 'BINDING_INVALID'
    if not isinstance(envelope, dict) or set(envelope) != {'binding', 'binding_sha256'}:
        raise StepsError(code)
    binding = envelope['binding']
    keys = {'binding_schema', 'catalogue', 'catalogue_sha256', 'selection_request',
            'validation_scope', 'device_identity_verified', 'user_intent_verified',
            'health_source_priority_verified'}
    if not isinstance(binding, dict) or set(binding) != keys:
        raise StepsError(code)
    if (binding['binding_schema'] != catalogue.BINDING_SCHEMA
            or binding['validation_scope'] != catalogue.VALIDATION_SCOPE
            or any(binding[k] is not False for k in ('device_identity_verified', 'user_intent_verified', 'health_source_priority_verified'))
            or envelope['binding_sha256'] != catalogue.sha256_json(binding)):
        raise StepsError(code)
    try:
        bound_cat, bound_digest = catalogue.validate_catalogue_envelope({
            'catalogue': binding['catalogue'], 'catalogue_sha256': binding['catalogue_sha256']})
        if catalogue.canonical_json(bound_cat) != catalogue.canonical_json(cat) or bound_digest != cat_digest:
            raise StepsError(code)
        request = catalogue.validate_selection_request(binding['selection_request'], cat, cat_digest)
    except catalogue.CatalogueError:
        raise StepsError(code) from None
    if 'steps' not in request['selections']:
        raise StepsError('STEPS_NOT_SELECTED')
    return request['selections']['steps'], envelope['binding_sha256']


def hash_stream(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def read_records(path, expected, state):
    """Every pass hashes the same full open archive handle on both sides."""
    try:
        with path.open('rb') as stream:
            identity = (expected['archive_sha256'], expected['archive_bytes'])
            if hash_stream(stream) != identity:
                raise StepsError('ARCHIVE_CHANGED')
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                matches = [i for i in archive.infolist() if not i.is_dir() and PurePosixPath(i.filename).name == 'export.xml']
                if len(matches) != 1 or matches[0].filename != expected['xml_member']:
                    raise StepsError('ARCHIVE_MEMBER_INVALID')
                parser = expat.ParserCreate(namespace_separator='\x1f')
                depth = 0
                root_seen = False
                def start(name, attrs):
                    nonlocal depth, root_seen
                    depth += 1
                    if depth == 1:
                        root_seen = True
                        if name != 'HealthData':
                            raise StepsError('XML_INVALID')
                    if name == 'Record':
                        state.record(attrs)
                def end(name):
                    nonlocal depth
                    depth -= 1
                def doctype(name, system, public, internal):
                    if system is not None or public is not None:
                        raise StepsError('EXTERNAL_XML_RESOURCE')
                def external(*args):
                    raise StepsError('EXTERNAL_XML_RESOURCE')
                parser.StartElementHandler = start
                parser.EndElementHandler = end
                parser.StartDoctypeDeclHandler = doctype
                parser.ExternalEntityRefHandler = external
                parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_ALWAYS)
                try:
                    with archive.open(matches[0]) as member:
                        while chunk := member.read(CHUNK):
                            parser.Parse(chunk, False)
                        parser.Parse(b'', True)
                except (expat.ExpatError, LookupError, UnicodeError, ValueError):
                    raise StepsError('XML_INVALID') from None
                if not root_seen or depth:
                    raise StepsError('XML_INVALID')
            if hash_stream(stream) != identity:
                raise StepsError('ARCHIVE_CHANGED')
    except StepsError:
        raise
    except (OSError, EOFError, RuntimeError, ValueError, lzma.LZMAError,
            zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error):
        raise StepsError('ARCHIVE_INVALID') from None
    if state.ordinal != expected['record_count_all_types']:
        raise StepsError('CATALOGUE_MISMATCH')
    expected_steps = sum(g['record_count'] for g in expected['families']['steps'])
    if sum(c['occurrences'] for c in state.archive_counts.values()) != expected_steps:
        raise StepsError('CATALOGUE_MISMATCH')


def validate_output(path):
    try:
        resolved = path.resolve(strict=False)
        checkout = CHECKOUT.resolve(strict=True)
        if resolved == checkout or checkout in resolved.parents:
            raise StepsError('OUTPUT_NOT_PRIVATE')
        if os.path.lexists(path):
            raise StepsError('OUTPUT_EXISTS')
        if not resolved.parent.is_dir():
            raise StepsError('OUTPUT_PARENT_INVALID')
    except (OSError, RuntimeError, ValueError):
        raise StepsError('OUTPUT_INVALID') from None
    return resolved


def evaluate(archive, catalogue_path, binding_path, timezone_key, as_of, days, output):
    validate_output(output)
    cat_obj, cat_hash = strict_snapshot(catalogue_path, 'CATALOGUE_INVALID')
    binding_obj, binding_hash = strict_snapshot(binding_path, 'BINDING_INVALID')
    cat, cat_digest = catalogue.validate_catalogue_envelope(cat_obj)
    selection, binding_digest = validate_binding(binding_obj, cat, cat_digest)
    actual = catalogue.compute_catalogue(archive)
    if catalogue.canonical_json(actual) != catalogue.canonical_json(cat):
        raise StepsError('CATALOGUE_MISMATCH')
    check_snapshot(catalogue_path, cat_hash)
    check_snapshot(binding_path, binding_hash)
    dates, window = window_dates(as_of, days)
    zone, provenance = pinned_zone(timezone_key)
    state = Accumulator(day_intervals(dates, zone), selection['group_ids'], catalogue.sha256_json)
    read_records(archive, cat, state)
    report = {'schema_version': 'mesh-steps-report-v1', 'policy_version': 'mesh-selected-steps-v1',
              'producer_version': 'mesh-steps-evaluator-v1', 'metric': 'selected_source_recorded_steps',
              'unit': 'count', 'archive': {'sha256': cat['archive_sha256'], 'bytes': cat['archive_bytes'],
                                            'xml_member': cat['xml_member']},
              'binding_sha256': binding_digest, 'selection_handle': selection['selection_handle'],
              'window': window, 'timezone': provenance, **state.finish()}
    check_snapshot(catalogue_path, cat_hash)
    check_snapshot(binding_path, binding_hash)
    catalogue.write_new_private(output, {'report': report, 'report_sha256': catalogue.sha256_json(report)})
    return report


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StepsError('CLI_USAGE')


def main(argv=None):
    try:
        parser = Parser(add_help=False)
        for name in ('archive', 'catalogue', 'binding', 'output'):
            parser.add_argument('--' + name, required=True, type=Path)
        parser.add_argument('--timezone', required=True)
        parser.add_argument('--as-of', required=True)
        parser.add_argument('--days', required=True, type=int)
        args = parser.parse_args(argv)
        evaluate(args.archive, args.catalogue, args.binding, args.timezone,
                 args.as_of, args.days, args.output)
    except (StepsError, catalogue.CatalogueError) as error:
        result, status = {'status': 'FAIL', 'operation': 'evaluate', 'error': error.code}, 2
    except Exception:
        result, status = {'status': 'FAIL', 'operation': 'evaluate', 'error': 'INTERNAL_ERROR'}, 1
    else:
        result, status = {'status': 'PASS', 'operation': 'evaluate'}, 0
    print(catalogue.canonical_json(result).decode('ascii'))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
