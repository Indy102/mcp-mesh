"""Independent synthetic acceptance tests for the primary evaluator."""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from fractions import Fraction
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from xml.sax.saxutils import quoteattr
import zoneinfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate
import steps_core
import steps_time
catalogue = evaluate.catalogue
STEP = 'HKQuantityTypeIdentifierStepCount'


def record(**updates):
    attrs = {'type': STEP, 'sourceName': 'Invented Pedometer', 'sourceVersion': '1',
             'device': 'synthetic', 'startDate': '2024-01-02T10:00:00Z',
             'endDate': '2024-01-02T10:10:00Z', 'unit': 'count', 'value': '5'}
    attrs.update(updates)
    return {k: v for k, v in attrs.items() if v is not None}


def xml_bytes(records, tail=''):
    return ('<HealthData>' + ''.join('<Record ' + ' '.join(k + '=' + quoteattr(v)
            for k, v in attrs.items()) + '/>' for attrs in records) + '</HealthData>' + tail).encode()


def binding_value(cat, families=('steps',)):
    digest = catalogue.sha256_json(cat)
    selections = {}
    for index, family in enumerate(families):
        ids = [g['group_id'] for g in cat['families'][family]
               if g['descriptor'][0] == ['present', 'Invented Pedometer']]
        selections[family] = {'selection_handle': f'12345678-1234-4234-8234-{index+1:012d}',
                              'group_ids': sorted(ids), 'source_scope': 'app_stream',
                              'user_choice': 'Synthetic explicit choice', 'identity_evidence': 'Synthetic assertion'}
    request = {'selection_schema': catalogue.SELECTION_SCHEMA,
               'archive_sha256': cat['archive_sha256'], 'catalogue_sha256': digest,
               'selections': selections}
    binding = {'binding_schema': catalogue.BINDING_SCHEMA, 'catalogue': cat,
               'catalogue_sha256': digest, 'selection_request': request,
               'validation_scope': catalogue.VALIDATION_SCOPE, 'device_identity_verified': False,
               'user_intent_verified': False, 'health_source_priority_verified': False}
    return {'binding': binding, 'binding_sha256': catalogue.sha256_json(binding)}


def tzif_disjoint():
    # On Jan 2 noon jump to Jan 3; an hour later jump back to Jan 2.
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    transitions = [int((datetime(2024, 1, 2, hour, tzinfo=timezone.utc) - epoch).total_seconds())
                   for hour in (12, 13)]
    header = b'TZif\0' + bytes(15) + struct.pack('>6l', 0, 0, 0, 2, 2, 4)
    return (header + struct.pack('>2l', *transitions) + bytes((1, 0))
            + struct.pack('>lbb', 0, 0, 0) + struct.pack('>lbb', 43200, 0, 0) + b'SYN\0')


class CoreTests(unittest.TestCase):
    def scope(self, records, days=2, start='2024-01-02'):
        first = date.fromisoformat(start)
        intervals = []
        for offset in range(days):
            from datetime import timedelta
            day = first + timedelta(days=offset)
            next_day = day + timedelta(days=1)
            lo = steps_time.timestamp(day.isoformat() + 'T00:00:00Z')
            hi = steps_time.timestamp(next_day.isoformat() + 'T00:00:00Z')
            intervals.append((day.isoformat(), (lo, hi)))
        ids = {catalogue.sha256_json({'family': 'steps', 'descriptor': [
            ['present', r[k]] if k in r else ['missing'] for k in ('sourceName', 'sourceVersion', 'device')]})
            for r in records if r.get('sourceName') == 'Invented Pedometer'}
        state = steps_core.Accumulator(intervals, ids, catalogue.sha256_json)
        for attrs in records:
            state.record(attrs)
        return state.finish()

    def test_all_collision_boundaries(self):
        cases = [((0, 10), (10, 20), False), ((0, 10), (9, 20), True),
                 ((0, 10), (0, 0), True), ((0, 10), (5, 5), True),
                 ((0, 10), (10, 10), False), ((10, 10), (10, 10), True),
                 ((10, 10), (11, 11), False), ((0, 10), (2, 8), True)]
        for a, b, expected in cases:
            with self.subTest(a=a, b=b):
                self.assertIs(steps_core.collide(a, b), expected)
                self.assertIs(steps_core.collide(b, a), expected)

    def test_adjacent_versions_closing_midnight_and_zero(self):
        result = self.scope([record(value='2.5', startDate='2024-01-02T23:00:00Z', endDate='2024-01-02T23:30:00Z'),
                             record(sourceVersion='2', value='3.5', startDate='2024-01-02T23:30:00Z', endDate='2024-01-03T00:00:00Z'),
                             record(sourceVersion='3', value='-0', startDate='2024-01-03T00:00:00Z', endDate='2024-01-03T00:00:00Z')])
        self.assertEqual([d['value'] for d in result['days']], [6.0, 0.0])
        self.assertEqual([d['calculation_state'] for d in result['days']], ['computed', 'computed'])
        self.assertEqual(result['window_counts']['selected']['occurrences'], 3)

    def test_no_records_is_not_zero(self):
        result = self.scope([record(value='0')])
        self.assertEqual(result['days'][0]['value'], 0.0)
        self.assertIsNone(result['days'][1]['value'])
        self.assertEqual(result['days'][1]['calculation_state'], 'no_records')

    def test_repeat_time_equivalence_raw_distinction_and_points(self):
        records = [record(startDate='2024-01-02T10:00:00Z', endDate='2024-01-02T10:00:00Z'),
                   record(startDate='2024-01-02 11:00:00 +0100', endDate='2024-01-02 11:00:00 +01:00'),
                   record(value='5.0', startDate='2024-01-02T10:00:00Z', endDate='2024-01-02T10:00:00Z')]
        result = self.scope(records)
        self.assertEqual(result['archive_repeat_candidate_extras'], 1)
        self.assertEqual(result['window_repeat_candidate_extras'], 1)
        self.assertEqual(result['days'][0]['repeat_candidate_extras'], 1)
        self.assertEqual(result['days'][0]['reasons'], ['selected_quantity_overlap', 'selected_repeat_candidate'])

    def test_raw_tags_preserve_missing_empty(self):
        records = [record(unit=None, value=None), record(unit=None, value=None),
                   record(unit='', value=None), record(unit=None, value='')]
        result = self.scope(records)
        self.assertEqual(result['archive_repeat_candidate_extras'], 1)
        self.assertEqual(result['archive_counts']['selected']['invalid_occurrences'], 4)

    def test_cross_window_union_and_full_crossing(self):
        result = self.scope([record(startDate='2024-01-01T23:00:00Z', endDate='2024-01-05T01:00:00Z'),
                             record(sourceName='Other', startDate='2024-01-01T00:00:00Z', endDate='2024-01-05T00:00:00Z')])
        self.assertEqual(result['window_counts']['selected']['occurrences'], 1)
        self.assertEqual(result['window_counts']['excluded']['occurrences'], 1)
        for row in result['days']:
            self.assertEqual(row['counts']['selected']['occurrences'], 1)
            self.assertEqual(row['reasons'], ['cross_day_quantity_unallocated'])
            self.assertTrue(row['diagnostics']['selected_excluded_overlap'])

    def test_hsp25_union_counts_and_independent_reasons(self):
        records = [record(value='bad', unit='wrong'), record(value='bad', unit='wrong'),
                   record(value='-1', startDate='2024-01-02T11:00:00Z', endDate='2024-01-02T12:00:00Z'),
                   record(value='4', unit=None, startDate='2024-01-02T12:00:00Z', endDate='2024-01-02T13:00:00Z'),
                   record(value='bad', unit='wrong', startDate='bad'),
                   record(value='bad', startDate='2024-01-01T12:00:00Z', endDate='2024-01-01T13:00:00Z')]
        result = self.scope(records)
        self.assertEqual(result['archive_counts']['selected'], {'occurrences': 6, 'time_attributable': 5,
                         'time_unattributable': 1, 'invalid_occurrences': 6, 'invalid_time': 1,
                         'invalid_value': 5, 'invalid_unit': 4})
        self.assertEqual(result['window_counts']['selected'], {'occurrences': 4, 'invalid_occurrences': 4,
                                                             'invalid_value': 3, 'invalid_unit': 3})
        self.assertEqual(result['days'][0]['counts']['selected'], result['window_counts']['selected'])
        self.assertEqual(result['days'][0]['reasons'], ['invalid_selected_record', 'selected_quantity_overlap',
                         'selected_repeat_candidate', 'selected_time_unattributable'])
        self.assertEqual(result['days'][0]['repeat_candidate_extras'], 1)
        self.assertTrue(result['days'][0]['diagnostics']['selected_quantity_overlap'])
        self.assertIsNone(result['days'][1]['diagnostics']['selected_quantity_overlap'])

    def test_unresolved_sources_and_unknowns_preserve_witnesses(self):
        records = [record(), record(value='6'), record(sourceName='Other'),
                   record(sourceName=None, startDate='2024-01-01T00:00:00Z', endDate='2024-01-01T01:00:00Z'),
                   record(sourceName=''), record(sourceName=' \t ')]
        result = self.scope(records)
        self.assertEqual(result['archive_counts']['unresolved']['occurrences'], 3)
        self.assertEqual(result['family_reasons'], ['selection_unresolved'])
        self.assertEqual(result['days'][0]['diagnostics'], {'selected_quantity_overlap': True, 'selected_excluded_overlap': True})
        self.assertEqual(result['days'][1]['diagnostics'], {'selected_quantity_overlap': None, 'selected_excluded_overlap': None})

    def test_bad_time_is_family_wide_and_excluded_unknown_is_separate(self):
        result = self.scope([record(startDate='0001-01-01T00:00:00+2359', endDate='2023-01-01T00:00:00Z')])
        self.assertEqual(result['family_reasons'], ['selected_time_unattributable'])
        self.assertEqual(result['window_counts']['selected']['occurrences'], 0)
        other = self.scope([record(), record(sourceName='Other', startDate='invalid')])
        self.assertEqual(other['days'][0]['value'], 5.0)
        self.assertEqual(other['days'][0]['diagnostics'], {'selected_quantity_overlap': False, 'selected_excluded_overlap': None})

    def test_bad_quantity_outside_does_not_block_or_range_check(self):
        result = self.scope([record(), record(value='bad', startDate='2024-01-01T00:00:00Z', endDate='2024-01-01T01:00:00Z')])
        self.assertEqual(result['days'][0]['value'], 5.0)
        blocked = self.scope([record(value='1e400', startDate='2024-01-01T00:00:00Z', endDate='2024-01-02T11:00:00Z')])
        self.assertEqual(blocked['days'][0]['reasons'], ['cross_day_quantity_unallocated'])

    def test_exact_fractional_rounding_midpoint_and_range(self):
        result = self.scope([record(value='0.1'), record(value='0.2', startDate='2024-01-02T11:00:00Z', endDate='2024-01-02T11:10:00Z')])
        self.assertEqual(result['days'][0]['value'].hex(), (0.3).hex())
        midpoint = '1.00000000000000011102230246251565404236316680908203125'
        self.assertEqual(self.scope([record(value=midpoint)])['days'][0]['value'], 1.0)
        odd_midpoint = '1.00000000000000033306690738754696212708950042724609375'
        self.assertEqual(self.scope([record(value=odd_midpoint)])['days'][0]['value'].hex(), '0x1.0000000000002p+0')
        for value, expected in [('1e400', None), ('1e-400', None), ('5e-324', 5e-324)]:
            with self.subTest(value=value):
                row = self.scope([record(value=value)])['days'][0]
                self.assertEqual(row['value'], expected)
                self.assertEqual(row['reasons'], ['numeric_result_out_of_range'] if expected is None else [])
        zero = self.scope([record(value='-0.00')])['days'][0]['value']
        self.assertEqual(zero.hex(), (0.0).hex())

    def test_grammar_resource_guards_every_partition(self):
        for token in (' 1', '1 ', 'NaN', 'Infinity', '1_0', '１２', '.', '+', '-1'):
            self.assertIsNone(steps_core.exact_value(token), token)
        for token, expected in [('.5', Fraction(1, 2)), ('1.', Fraction(1)), ('+1e2', Fraction(100)), ('-0e99999', Fraction(0))]:
            self.assertEqual(steps_core.exact_value(token), expected)
        for name in ('Invented Pedometer', 'Other', ''):
            for value in ('0' * 4097, '1e100001', '1e-100001'):
                with self.subTest(name=name, value=value[:30]):
                    with self.assertRaisesRegex(steps_time.StepsError, 'INPUT_RESOURCE_LIMIT'):
                        self.scope([record(sourceName=name, value=value)])
        self.assertEqual(steps_core.exact_value('0' * 4096), 0)
        self.assertEqual(steps_core.exact_value('1e-100000'), Fraction(1, 10**100000))

    def test_timestamp_grammar_extremes_and_no_local_conversion(self):
        origin = steps_time.timestamp('2024-01-02T10:00:00Z')
        for token in ('2024-01-02 11:00:00 +0100', '2024-01-02T10:00:00 Z', '2024-01-02T10:00:00.000000Z'):
            self.assertEqual(steps_time.timestamp(token), origin)
        for token in (' 2024-01-02T10:00:00Z', '2024-01-02T10:00:00Z ', '2024-01-02T10:00:60Z',
                      '2024-01-02T10:00:00', '2024-01-02T10:00:00.1234567Z', '2024-01-02T10:00:00+2400',
                      '2024-01-02T10:00:00+0060', '0001-01-01T00:00:00+0001', '9999-12-31T23:59:59-0001'):
            self.assertIsNone(steps_time.timestamp(token), token)
        result = self.scope([record(startDate='9999-12-31T23:59:59Z', endDate='9999-12-31T23:59:59Z')])
        self.assertEqual(result['archive_counts']['selected']['invalid_time'], 0)
        self.assertEqual(result['window_counts']['selected']['occurrences'], 0)


class TimeTests(unittest.TestCase):
    def boundaries(self, zone, as_of, days=1):
        dates, _ = steps_time.window_dates(as_of, days)
        return steps_time.day_intervals(dates, zone)

    def test_dst_23_and_25_hour_days(self):
        zone, metadata = steps_time.pinned_zone('America/New_York')
        for as_of, duration in [('2024-03-11', 82800), ('2024-11-04', 90000)]:
            with self.subTest(as_of=as_of):
                _, (lo, hi) = self.boundaries(zone, as_of)[0]
                self.assertEqual((hi - lo) // 1000000, duration)
        import base64
        self.assertEqual(hashlib.sha256(base64.b64decode(metadata['tzif_base64'])).hexdigest(), metadata['tzif_sha256'])

    def test_midnight_gap(self):
        zone, _ = steps_time.pinned_zone('America/Sao_Paulo')
        _, (lo, hi) = self.boundaries(zone, '2018-11-05')[0]
        self.assertEqual(steps_time.format_utc(lo), '2018-11-04T03:00:00.000000Z')
        self.assertEqual((hi - lo) // 1000000, 82800)

    def test_skipped_date_overrides_family_reasons(self):
        zone, _ = steps_time.pinned_zone('Pacific/Apia')
        intervals = self.boundaries(zone, '2011-12-31')
        self.assertEqual(intervals, [('2011-12-30', None)])
        state = steps_core.Accumulator(intervals, [], catalogue.sha256_json)
        state.record(record(sourceName='', startDate='bad'))
        result = state.finish()
        row = result['days'][0]
        self.assertEqual(result['family_reasons'], ['selection_unresolved'])
        self.assertEqual(row['reasons'], ['no_civil_day'])
        self.assertEqual(row['calculation_state'], 'no_civil_day')
        self.assertEqual(row['diagnostics'], {'selected_quantity_overlap': False, 'selected_excluded_overlap': False})
        self.assertEqual(row['counts'], steps_core.counts())
        self.assertIsNone(row['utc_start'])
        self.assertEqual(row['utc_duration_seconds'], 0)

    def test_disjoint_preimage_refused(self):
        zone = zoneinfo.ZoneInfo.from_file(io.BytesIO(tzif_disjoint()), key='Synthetic/Disjoint')
        with self.assertRaisesRegex(steps_time.StepsError, 'WINDOW_UNREPRESENTABLE'):
            self.boundaries(zone, '2024-01-03')

    def test_key_window_and_four_digit_year(self):
        for key in ('', '/UTC', '../UTC', 'a/../UTC', 'a//b', 'a\\b', 'UTC\0'):
            self.assertFalse(steps_time.valid_zone_key(key))
        for as_of, days in [('0001-01-02', 1), ('9999-12-31', 1), ('2024-01-02', True), ('2024-01-02', 0), ('2024-1-02', 1)]:
            with self.assertRaisesRegex(steps_time.StepsError, 'WINDOW_UNREPRESENTABLE'):
                steps_time.window_dates(as_of, days)
        self.assertEqual(steps_time.format_utc(0), '0001-01-01T00:00:00.000000Z')


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-steps-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'invented.zip'
        self.cat_path = self.root / 'catalogue.json'
        self.binding_path = self.root / 'binding.json'
        self.output = self.root / 'report.json'
        self.setup_inputs([record()])

    def setup_inputs(self, records, families=('steps',), compression=zipfile.ZIP_DEFLATED):
        with zipfile.ZipFile(self.archive, 'w', compression=compression) as archive:
            archive.writestr('apple_health_export/export.xml', xml_bytes(records))
        cat = catalogue.compute_catalogue(self.archive)
        self.cat_path.write_bytes(catalogue.canonical_json(catalogue.make_catalogue_envelope(cat)))
        self.binding_path.write_bytes(catalogue.canonical_json(binding_value(cat, families)))
        return cat

    def command(self, *extra, expected=0):
        args = [sys.executable, str(Path(evaluate.__file__)), '--archive', str(self.archive),
                '--catalogue', str(self.cat_path), '--binding', str(self.binding_path),
                '--timezone', 'UTC', '--as-of', '2024-01-03', '--days', '1', '--output', str(self.output), *extra]
        process = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(process.stderr, '')
        self.assertEqual(process.returncode, expected, process.stdout)
        payload = json.loads(process.stdout)
        self.assertEqual(set(payload), {'status', 'operation'} if expected == 0 else {'status', 'operation', 'error'})
        self.assertEqual(payload['operation'], 'evaluate')
        self.assertEqual(payload['status'], 'PASS' if expected == 0 else 'FAIL')
        return payload

    def test_public_cli_private_exact_deterministic_report(self):
        self.command()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        original = self.output.read_bytes()
        envelope = json.loads(original)
        self.assertEqual(envelope['report_sha256'], catalogue.sha256_json(envelope['report']))
        report = envelope['report']
        self.assertEqual(report['days'][0]['value'], 5.0)
        self.assertIs(type(report['days'][0]['value']), float)
        for secret in ('Invented Pedometer', 'synthetic', str(self.root), 'sourceVersion', 'user_choice'):
            self.assertNotIn(secret, original.decode())
        second = self.root / 'second.json'
        self.command('--output', str(second))
        self.assertEqual(original, second.read_bytes())
        self.assertEqual(set(report), {'schema_version', 'policy_version', 'producer_version', 'metric', 'unit', 'archive',
            'binding_sha256', 'selection_handle', 'window', 'timezone', 'family_reasons', 'archive_counts', 'window_counts',
            'archive_repeat_candidate_extras', 'window_repeat_candidate_extras', 'days'})

    def test_public_cli_all_zip_compressions(self):
        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(method=method):
                self.setup_inputs([record()], compression=method)
                self.command('--output', str(self.root / f'report-{method}.json'))

    def test_public_cli_errors_are_fixed_and_private(self):
        self.assertEqual(self.command('--help', expected=2)['error'], 'CLI_USAGE')
        self.assertEqual(self.command('--days', 'not-a-private-number', expected=2)['error'], 'CLI_USAGE')
        self.assertEqual(self.command('--days', '0', expected=2)['error'], 'WINDOW_UNREPRESENTABLE')
        self.assertEqual(self.command('--timezone', '../private-zone', expected=2)['error'], 'TIMEZONE_INVALID')
        self.assertFalse(self.output.exists())

    def test_output_existing_checkout_symlink_missing_parent(self):
        self.output.write_text('preserve')
        self.assertEqual(self.command(expected=2)['error'], 'OUTPUT_EXISTS')
        self.assertEqual(self.output.read_text(), 'preserve')
        self.assertEqual(self.command('--output', str(self.root / 'missing' / 'report.json'), expected=2)['error'], 'OUTPUT_PARENT_INVALID')
        checkout_alias = self.root / 'alias'
        checkout_alias.symlink_to(evaluate.CHECKOUT, target_is_directory=True)
        self.assertEqual(self.command('--output', str(checkout_alias / 'forbidden.json'), expected=2)['error'], 'OUTPUT_NOT_PRIVATE')

    def test_binding_false_flags_and_other_family_validation(self):
        envelope = json.loads(self.binding_path.read_text())
        envelope['binding']['device_identity_verified'] = 0
        envelope['binding_sha256'] = catalogue.sha256_json(envelope['binding'])
        self.binding_path.write_bytes(catalogue.canonical_json(envelope))
        self.assertEqual(self.command(expected=2)['error'], 'BINDING_INVALID')
        self.setup_inputs([record(), record(type='HKQuantityTypeIdentifierHeartRateVariabilitySDNN')], ('hrv_sdnn',))
        self.assertEqual(self.command(expected=2)['error'], 'STEPS_NOT_SELECTED')
        self.setup_inputs([record(), record(type='HKQuantityTypeIdentifierHeartRateVariabilitySDNN')], ('steps', 'hrv_sdnn'))
        envelope = json.loads(self.binding_path.read_text())
        envelope['binding']['selection_request']['selections']['hrv_sdnn']['group_ids'] = ['0' * 64]
        envelope['binding_sha256'] = catalogue.sha256_json(envelope['binding'])
        self.binding_path.write_bytes(catalogue.canonical_json(envelope))
        self.assertEqual(self.command(expected=2)['error'], 'BINDING_INVALID')

    def test_strict_json_unknown_fields_and_stale_archive(self):
        self.binding_path.write_text('{"binding":{},"binding":{}}')
        self.assertEqual(self.command(expected=2)['error'], 'BINDING_INVALID')
        self.setup_inputs([record()])
        envelope = json.loads(self.cat_path.read_text())
        envelope['extra'] = True
        self.cat_path.write_bytes(catalogue.canonical_json(envelope))
        self.assertEqual(self.command(expected=2)['error'], 'CATALOGUE_INVALID')
        self.setup_inputs([record()])
        with zipfile.ZipFile(self.archive, 'a') as archive:
            archive.writestr('unrelated.txt', 'synthetic change')
        self.assertEqual(self.command(expected=2)['error'], 'CATALOGUE_MISMATCH')

    def test_full_xml_tail_encoding_namespace_and_external_failures(self):
        cat = catalogue.compute_catalogue(self.archive)
        for xml, code in [(xml_bytes([record(startDate='bad')], '<broken'), 'XML_INVALID'),
                          (b'<HealthData><Record/></HealthData>\xff', 'XML_INVALID'),
                          (b'<HealthData xmlns="urn:invented"/>', 'XML_INVALID'),
                          (b'<!DOCTYPE HealthData SYSTEM "file:///invented-private"><HealthData/>', 'EXTERNAL_XML_RESOURCE')]:
            with self.subTest(code=code, xml=xml[:40]):
                with zipfile.ZipFile(self.archive, 'w') as archive:
                    archive.writestr('export.xml', xml)
                cat['archive_sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
                cat['archive_bytes'] = self.archive.stat().st_size
                cat['xml_member'] = 'export.xml'
                state = steps_core.Accumulator([], [], catalogue.sha256_json)
                with self.assertRaisesRegex(steps_time.StepsError, code):
                    evaluate.read_records(self.archive, cat, state)
                self.assertEqual(self.command(expected=2)['error'], code)

    def test_archive_mutation_between_passes_and_snapshot_changes(self):
        original = catalogue.compute_catalogue
        def changed_archive(path):
            result = original(path)
            with zipfile.ZipFile(path, 'a') as archive:
                archive.writestr('changed.txt', 'invented')
            return result
        with mock.patch.object(catalogue, 'compute_catalogue', side_effect=changed_archive):
            with self.assertRaisesRegex(steps_time.StepsError, 'ARCHIVE_CHANGED'):
                evaluate.evaluate(self.archive, self.cat_path, self.binding_path, 'UTC', '2024-01-03', 1, self.output)
        self.setup_inputs([record()])
        for target in (self.cat_path, self.binding_path):
            with self.subTest(target=target.name):
                previous = target.read_bytes()
                def changed_snapshot(path):
                    result = original(path)
                    target.write_bytes(previous + b' ')
                    return result
                with mock.patch.object(catalogue, 'compute_catalogue', side_effect=changed_snapshot):
                    with self.assertRaisesRegex(steps_time.StepsError, 'INPUT_CHANGED'):
                        evaluate.evaluate(self.archive, self.cat_path, self.binding_path, 'UTC', '2024-01-03', 1, self.output)
                target.write_bytes(previous)
        self.assertFalse(self.output.exists())

    def test_resource_guard_is_public_run_failure(self):
        for source in ('Invented Pedometer', 'Other', ''):
            self.setup_inputs([record(), record(sourceName=source, value='1e100001')])
            self.assertEqual(self.command(expected=2)['error'], 'INPUT_RESOURCE_LIMIT')
            self.assertFalse(self.output.exists())

    def test_second_pass_after_hash_detects_same_handle_mutation(self):
        cat = catalogue.compute_catalogue(self.archive)
        real_hash = evaluate.hash_stream
        calls = 0
        def mutated_hash(stream):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.archive.open('ab') as writer:
                    writer.write(b'synthetic changed bytes')
            return real_hash(stream)
        state = steps_core.Accumulator([], [], catalogue.sha256_json)
        with mock.patch.object(evaluate, 'hash_stream', side_effect=mutated_hash):
            with self.assertRaisesRegex(steps_time.StepsError, 'ARCHIVE_CHANGED'):
                evaluate.read_records(self.archive, cat, state)
        self.assertEqual(calls, 2)

    def test_unexpected_failure_has_no_private_exception(self):
        output = io.StringIO()
        args = ['--archive', str(self.archive), '--catalogue', str(self.cat_path),
                '--binding', str(self.binding_path), '--timezone', 'UTC', '--as-of', '2024-01-03',
                '--days', '1', '--output', str(self.output)]
        with mock.patch.object(evaluate, 'evaluate', side_effect=RuntimeError('private raw detail')):
            with redirect_stdout(output):
                self.assertEqual(evaluate.main(args), 1)
        self.assertEqual(json.loads(output.getvalue()), {'operation': 'evaluate', 'status': 'FAIL', 'error': 'INTERNAL_ERROR'})

    def test_private_write_failure_removes_partial(self):
        with mock.patch.object(catalogue.os, 'write', side_effect=OSError('private detail')):
            with self.assertRaisesRegex(catalogue.CatalogueError, 'OUTPUT_WRITE_FAILED'):
                evaluate.evaluate(self.archive, self.cat_path, self.binding_path, 'UTC', '2024-01-03', 1, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
