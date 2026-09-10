"""Invented, independently authored fixtures; expected measurements are literals."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import quoteattr
import zipfile

from oracle_core import exact_value, read_measurements, Measurement
from oracle_time import Refusal, civil_days, pinned_zone, timestamp
import verify_steps


def wire(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')


def sha(value):
    return hashlib.sha256(wire(value)).hexdigest()


def tzif(offsets=(0,), transitions=()):
    # Authored TZif v1: one abbreviation and no leap/standard/wall indicators.
    times = [int((datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()) for t, _ in transitions]
    raw = b'TZif\0' + bytes(15) + struct.pack('>6i', 0, 0, 0, len(times), len(offsets), 4)
    raw += b''.join(struct.pack('>i', t) for t in times)
    raw += bytes(index for _, index in transitions)
    raw += b''.join(struct.pack('>iBB', offset, 0, 0) for offset in offsets) + b'TST\0'
    return {'key': 'Invented/Pinned', 'tzif_base64': base64.b64encode(raw).decode('ascii'), 'tzif_sha256': hashlib.sha256(raw).hexdigest()}


def counts(n=0, invalid=0, value=0, unit=0, bad_time=None):
    result = {'occurrences': n, 'invalid_occurrences': invalid, 'invalid_value': value, 'invalid_unit': unit}
    if bad_time is not None:
        result.update(time_attributable=n-bad_time, time_unattributable=bad_time, invalid_time=bad_time)
    return result


def partitions(selected=None, excluded=None, unresolved=None, archive=False):
    return dict(zip(('selected', 'excluded', 'unresolved'), [v if v is not None else counts(bad_time=0 if archive else None) for v in (selected, excluded, unresolved)]))


def day(date='2024-01-02', state='computed', value=1.0, reasons=(), selected=None, excluded=None, unresolved=None, repeat=0, overlap=False, pair=False, start=None, end=None, duration=86400):
    # Fixture default boundaries are the two literal endpoints of this UTC day.
    return {'date': date, 'utc_start': start or '2024-01-02T00:00:00.000000Z', 'utc_end': end or '2024-01-03T00:00:00.000000Z', 'utc_duration_seconds': duration, 'calculation_state': state, 'value': value, 'reasons': sorted(reasons), 'observation_completeness': 'unknown', 'valid_metric_day': None, 'inference_eligible': False, 'counts': partitions(selected, excluded, unresolved), 'repeat_candidate_extras': repeat, 'diagnostics': {'selected_quantity_overlap': overlap, 'selected_excluded_overlap': pair}}


def record(start='2024-01-02 01:00:00Z', end='2024-01-02 02:00:00Z', value='1', unit='count', source='chosen', version='v1', **extra):
    row = {'type': 'HKQuantityTypeIdentifierStepCount', 'startDate': start, 'endDate': end, 'value': value, 'unit': unit, 'sourceName': source, 'sourceVersion': version}
    row.update(extra)
    return {key: val for key, val in row.items() if val is not None}


class Fixture:
    def __init__(self, root, records, selected_sources=('chosen',), selected_family='steps', compression=zipfile.ZIP_DEFLATED, tail='', xml_transform=None):
        self.archive, self.catalogue, self.binding, self.report = [root / name for name in ('invented.zip', 'catalogue.json', 'binding.json', 'report.json')]
        xml = '<HealthData>' + ''.join('<Record ' + ' '.join(k + '=' + quoteattr(v) for k, v in attrs.items()) + '/>' for attrs in records) + '</HealthData>' + tail
        if xml_transform is not None:
            xml = xml_transform(xml)
        with zipfile.ZipFile(self.archive, 'w', compression=compression) as output:
            output.writestr('fiction/export.xml', xml)
        content = self.archive.read_bytes()
        by_family = {'steps': {}, 'hrv_sdnn': {}, 'sleep': {}}
        types = {'HKQuantityTypeIdentifierStepCount': 'steps', 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN': 'hrv_sdnn', 'HKCategoryTypeIdentifierSleepAnalysis': 'sleep'}
        for attrs in records:
            family = types.get(attrs.get('type'))
            if family is None:
                continue
            descriptor = [['present', attrs[k]] if k in attrs else ['missing'] for k in ('sourceName', 'sourceVersion', 'device')]
            identifier = sha({'family': family, 'descriptor': descriptor})
            if identifier not in by_family[family]:
                by_family[family][identifier] = {'group_id': identifier, 'descriptor': descriptor, 'record_count': 0}
            by_family[family][identifier]['record_count'] += 1
        cat = {'catalogue_schema': 'mesh-health-catalogue-v1', 'archive_sha256': hashlib.sha256(content).hexdigest(), 'archive_bytes': len(content), 'xml_member': 'fiction/export.xml', 'record_count_all_types': len(records), 'families': {family: sorted(groups.values(), key=lambda g: wire(g['descriptor'])) for family, groups in by_family.items()}}
        self.cat = cat
        self.catalogue.write_bytes(wire({'catalogue': cat, 'catalogue_sha256': sha(cat)}))
        selection = {'selection_handle': '12345678-1234-4234-8234-123456789abc', 'group_ids': sorted(g['group_id'] for g in cat['families'][selected_family] if g['descriptor'][0][0] == 'present' and g['descriptor'][0][1] in selected_sources), 'source_scope': 'app_stream', 'user_choice': 'Invented fixture preference', 'identity_evidence': 'Invented fixture assertion'}
        request = {'selection_schema': 'mesh-health-selection-request-v1', 'archive_sha256': cat['archive_sha256'], 'catalogue_sha256': sha(cat), 'selections': {selected_family: selection}}
        self.bound = {'binding_schema': 'mesh-health-selection-binding-v1', 'catalogue': cat, 'catalogue_sha256': sha(cat), 'selection_request': request, 'validation_scope': 'archive_integrity_and_selection_membership', 'device_identity_verified': False, 'user_intent_verified': False, 'health_source_priority_verified': False}
        self.binding.write_bytes(wire({'binding': self.bound, 'binding_sha256': sha(self.bound)}))

    def expected(self, rows, archive_selected, window_selected=None, archive_excluded=None, archive_unresolved=None, window_excluded=None, window_unresolved=None, family=(), archive_repeat=0, window_repeat=0, zone=None, as_of='2024-01-03', start='2024-01-02', end='2024-01-02'):
        content = {'schema_version': 'mesh-steps-report-v1', 'policy_version': 'mesh-selected-steps-v1', 'producer_version': 'mesh-steps-evaluator-v1', 'metric': 'selected_source_recorded_steps', 'unit': 'count', 'archive': {'sha256': self.cat['archive_sha256'], 'bytes': self.cat['archive_bytes'], 'xml_member': self.cat['xml_member']}, 'binding_sha256': sha(self.bound), 'selection_handle': '12345678-1234-4234-8234-123456789abc', 'window': {'as_of': as_of, 'days': len(rows), 'start': start, 'end': end}, 'timezone': zone or tzif(), 'family_reasons': sorted(family), 'archive_counts': partitions(archive_selected, archive_excluded, archive_unresolved, archive=True), 'window_counts': partitions(window_selected, window_excluded, window_unresolved), 'archive_repeat_candidate_extras': archive_repeat, 'window_repeat_candidate_extras': window_repeat, 'days': rows}
        self.save(content)
        return content

    def save(self, content):
        self.report.write_bytes(wire({'report': content, 'report_sha256': sha(content)}))

    def verify(self):
        return verify_steps.verify(self.archive, self.catalogue, self.binding, self.report)


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='steps-oracle-invented-')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def fixture(self, records, **kwargs):
        return Fixture(self.root, records, **kwargs)

    def basic(self):
        fixture = self.fixture([record()])
        content = fixture.expected([day(selected=counts(1))], counts(1, bad_time=0), counts(1))
        return fixture, content

    def test_single_and_deterministic_read_only(self):
        fixture, _ = self.basic()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
        fixture.verify()
        fixture.verify()
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()})

    def test_adjacent_intervals_closing_midnight_and_bound_versions(self):
        fixture = self.fixture([record(end='2024-01-02 02:00:00Z', value='.1'), record(start='2024-01-02 02:00:00Z', end='2024-01-03 00:00:00Z', value='.2', version='v2')])
        fixture.expected([day(value=0.3, selected=counts(2))], counts(2, bad_time=0), counts(2))
        fixture.verify()

    def test_intervals_and_both_point_endpoints(self):
        for point, collides in [('2024-01-02 01:00:00Z', True), ('2024-01-02 01:30:00Z', True), ('2024-01-02 02:00:00Z', False)]:
            with self.subTest(point=point):
                fixture = self.fixture([record(), record(start=point, end=point, value='2')])
                fixture.expected([day(value=None if collides else 3.0, state='blocked' if collides else 'computed', reasons=['selected_quantity_overlap'] if collides else [], selected=counts(2), overlap=collides)], counts(2, bad_time=0), counts(2))
                fixture.verify()

    def test_overlapping_invalid_quantities_and_duplicate_points(self):
        point = '2024-01-02 01:30:00Z'
        fixture = self.fixture([record(), record(start=point, end=point, value='bad', unit='wrong'), record(start=point, end=point, value='bad', unit='wrong')])
        fixture.expected([day(state='blocked', value=None, reasons=['invalid_selected_record', 'selected_quantity_overlap', 'selected_repeat_candidate'], selected=counts(3, 2, 2, 2), overlap=True, repeat=1)], counts(3, 2, 2, 2, bad_time=0), counts(3, 2, 2, 2), archive_repeat=1, window_repeat=1)
        fixture.verify()

    def test_repeats_offset_equivalence_and_raw_distinctions(self):
        fixture = self.fixture([record(), record(start='2024-01-02T02:00:00+0100', end='2024-01-02T03:00:00+01:00'), record(value='1.0')])
        fixture.expected([day(state='blocked', value=None, reasons=['selected_quantity_overlap', 'selected_repeat_candidate'], selected=counts(3), overlap=True, repeat=1)], counts(3, bad_time=0), counts(3), archive_repeat=1, window_repeat=1)
        fixture.verify()

    def test_repeat_raw_missing_empty_and_xml_entities(self):
        # Missing and empty do not form a repeat; XML character-reference spellings do.
        fixture = self.fixture([record(value=None), record(value='')])
        fixture.expected([day(state='blocked', value=None, reasons=['invalid_selected_record', 'selected_quantity_overlap'], selected=counts(2, 2, 2), overlap=True)], counts(2, 2, 2, bad_time=0), counts(2, 2, 2))
        fixture.verify()
        fixture = self.fixture([record(value='&'), record(value='&')], xml_transform=lambda xml: xml.replace('value="&amp;"', 'value="&#38;"', 1))
        fixture.expected([day(state='blocked', value=None, reasons=['invalid_selected_record', 'selected_quantity_overlap', 'selected_repeat_candidate'], selected=counts(2, 2, 2), repeat=1, overlap=True)], counts(2, 2, 2, bad_time=0), counts(2, 2, 2), archive_repeat=1, window_repeat=1)
        fixture.verify()

    def test_named_excluded_collisions_do_not_change_selected_value(self):
        fixture = self.fixture([record(), record(source='other', value='900')])
        fixture.expected([day(selected=counts(1), excluded=counts(1), pair=True)], counts(1, bad_time=0), counts(1), archive_excluded=counts(1, bad_time=0), window_excluded=counts(1))
        fixture.verify()

    def test_source_missing_empty_whitespace_and_unknown_diagnostics(self):
        fixture = self.fixture([record(), record(source=None), record(source=''), record(source='  ')])
        fixture.expected([day(state='blocked', value=None, reasons=['selection_unresolved'], selected=counts(1), unresolved=counts(3), overlap=None, pair=None)], counts(1, bad_time=0), counts(1), archive_unresolved=counts(3, bad_time=0), window_unresolved=counts(3), family=['selection_unresolved'])
        fixture.verify()

    def test_witnesses_survive_unknown_inputs(self):
        fixture = self.fixture([record(), record(value='2'), record(source='other'), record(source=None, start='bad'), record(start='bad', end='1900-01-01 00:00:00Z')])
        reasons = ['selection_unresolved', 'selected_time_unattributable', 'selected_quantity_overlap']
        fixture.expected([day(state='blocked', value=None, reasons=reasons, selected=counts(2), excluded=counts(1), overlap=True, pair=True)], counts(3, 1, bad_time=1), counts(2), archive_excluded=counts(1, bad_time=0), window_excluded=counts(1), archive_unresolved=counts(1, 1, bad_time=1), family=reasons[:2])
        fixture.verify()

    def test_bad_selected_time_outside_window_blocks_every_ordinary_day(self):
        fixture = self.fixture([record(start='1900-01-01 00:00:00Z', end=None, value='bad', unit=None)])
        fixture.expected([day(state='blocked', value=None, reasons=['selected_time_unattributable'], overlap=None, pair=None)], counts(1, 1, 1, 1, bad_time=1), family=['selected_time_unattributable'])
        fixture.verify()

    def test_excluded_bad_time_only_makes_pair_unknown(self):
        fixture = self.fixture([record(), record(source='other', start='bad')])
        fixture.expected([day(selected=counts(1), pair=None)], counts(1, bad_time=0), counts(1), archive_excluded=counts(1, 1, bad_time=1))
        fixture.verify()

    def test_invalid_quantities_outside_window_do_not_block(self):
        fixture = self.fixture([record(), record(start='2024-01-01 00:00:00Z', end='2024-01-01 01:00:00Z', value='NaN', unit='COUNT')])
        fixture.expected([day(selected=counts(1))], counts(2, 1, 1, 1, bad_time=0), counts(1))
        fixture.verify()

    def test_cross_day_quantities_full_window_and_union_counts(self):
        fixture = self.fixture([record(start='2024-01-01 23:00:00Z', end='2024-01-04 01:00:00Z'), record(start='2024-01-01 23:00:00Z', end='2024-01-04 01:00:00Z', source='other')])
        rows = [day(state='blocked', value=None, reasons=['cross_day_quantity_unallocated'], selected=counts(1), excluded=counts(1), pair=True), day(date='2024-01-03', start='2024-01-03T00:00:00.000000Z', end='2024-01-04T00:00:00.000000Z', state='blocked', value=None, reasons=['cross_day_quantity_unallocated'], selected=counts(1), excluded=counts(1), pair=True)]
        fixture.expected(rows, counts(1, bad_time=0), counts(1), archive_excluded=counts(1, bad_time=0), window_excluded=counts(1), as_of='2024-01-04', end='2024-01-03')
        fixture.verify()

    def test_unselected_cross_day_keeps_counts_without_quantity_reason(self):
        fixture = self.fixture([record(), record(source='other', start='2024-01-01 23:00:00Z', end='2024-01-04 01:00:00Z'), record(source='', start='2024-01-01 23:00:00Z', end='2024-01-04 01:00:00Z')])
        fixture.expected([day(state='blocked', value=None, reasons=['selection_unresolved'], selected=counts(1), excluded=counts(1), unresolved=counts(1), overlap=None, pair=True)], counts(1, bad_time=0), counts(1), archive_excluded=counts(1, bad_time=0), window_excluded=counts(1), archive_unresolved=counts(1, bad_time=0), window_unresolved=counts(1), family=['selection_unresolved'])
        fixture.verify()

    def test_no_records_is_not_explicit_negative_zero(self):
        fixture = self.fixture([record(value='-0')])
        rows = [day(value=0.0, selected=counts(1)), day(date='2024-01-03', start='2024-01-03T00:00:00.000000Z', end='2024-01-04T00:00:00.000000Z', state='no_records', value=None)]
        fixture.expected(rows, counts(1, bad_time=0), counts(1), as_of='2024-01-04', end='2024-01-03')
        fixture.verify()

    def test_binary64_midpoint_subnormal_overflow_and_underflow(self):
        for token, number in [('1.00000000000000011102230246251565404236316680908203125', 1.0), ('1.00000000000000033306690738754696212708950042724609375', 1.0000000000000004), ('5e-324', 5e-324), ('1e400', None), ('1e-400', None)]:
            with self.subTest(token=token):
                fixture = self.fixture([record(value=token)])
                fixture.expected([day(value=number, state='computed' if number is not None else 'blocked', reasons=[] if number is not None else ['numeric_result_out_of_range'], selected=counts(1))], counts(1, bad_time=0), counts(1))
                fixture.verify()

    def test_exact_fractional_accumulation_not_context_rounded(self):
        fixture = self.fixture([record(value='10000000000000001', end='2024-01-02 02:00:00Z'), record(value='1', start='2024-01-02 02:00:00Z', end='2024-01-02 03:00:00Z')])
        fixture.expected([day(value=10000000000000002.0, selected=counts(2))], counts(2, bad_time=0), counts(2))
        fixture.verify()

    def test_do_not_report_numeric_range_when_other_reason_blocks(self):
        fixture = self.fixture([record(value='1e400'), record(value='1e400')])
        fixture.expected([day(state='blocked', value=None, reasons=['selected_quantity_overlap', 'selected_repeat_candidate'], selected=counts(2), repeat=1, overlap=True)], counts(2, bad_time=0), counts(2), archive_repeat=1, window_repeat=1)
        fixture.verify()

    def test_decimal_grammar_and_resource_guard_all_partitions(self):
        for token in ['', 'NaN', 'Infinity', ' 1', '1 ', '1_0', '１', '.', '-1', '1e', '0x1']:
            with self.subTest(token=token):
                self.assertIsNone(exact_value(token))
        for token, expected in [('+1.', 1), ('.5e1', 5), ('-0e100000', 0), ('0e-100000', 0), ('1e+0001', 10)]:
            self.assertEqual(exact_value(token), expected)
        for source in ['chosen', 'other', None]:
            for value in ['0'*4097, '1e100001', '1e-100001']:
                fixture = self.fixture([record(), record(source=source, value=value, start='bad')])
                fixture.expected([day(selected=counts(1))], counts(1, bad_time=0), counts(1))
                with self.assertRaisesRegex(Refusal, '^INPUT_RESOURCE_LIMIT$'):
                    fixture.verify()

    def test_timestamp_grammar_offsets_and_extreme_endpoint_membership(self):
        base = timestamp('2024-01-02T01:00:00Z')
        for token in ['2024-01-02 01:00:00 Z', '2024-01-02T02:00:00+01:00', '2024-01-01T20:00:00 -0500', '2024-01-02T01:00:00.000000+0000']:
            self.assertEqual(timestamp(token), base)
        for token in ['', '2024-01-02T01:00:00', '2024-01-02T01:00:60Z', '2024-02-30T01:00:00Z', '2024-01-02T01:00:00+2400', '2024-01-02T01:00:00+0060', '2024-01-02T01:00:00.0000000Z', '2024-01-02T01:00:00Z ', '0001-01-01T00:00:00+0001', '9999-12-31T23:59:59-0001']:
            self.assertIsNone(timestamp(token), token)
        fixture = self.fixture([record(), record(start='0001-01-01T00:00:00Z', end='0001-01-01T00:00:00Z')])
        fixture.expected([day(selected=counts(1), start='2024-01-01T23:00:00.000000Z', end='2024-01-02T23:00:00.000000Z')], counts(2, bad_time=0), counts(1), zone=tzif((3600,)))
        fixture.verify()

    def test_23_hour_midnight_gap_and_25_hour_ambiguous_midnight(self):
        for offsets, transition, closing, duration in [((0, 3600), '2024-01-02T00:00:00', '2024-01-02T23:00:00.000000Z', 82800), ((0, -3600), '2024-01-02T01:00:00', '2024-01-03T01:00:00.000000Z', 90000)]:
            fixture = self.fixture([record(start='2024-01-02T05:00:00Z', end='2024-01-02T06:00:00Z')])
            fixture.expected([day(selected=counts(1), end=closing, duration=duration)], counts(1, bad_time=0), counts(1), zone=tzif(offsets, ((transition, 1),)))
            fixture.verify()

    def test_skipped_day_ignores_family_reasons_and_has_zero_counts(self):
        fixture = self.fixture([record(start='bad'), record(source=None)])
        row = day(state='no_civil_day', value=None, reasons=['no_civil_day'], duration=0)
        row['utc_start'] = row['utc_end'] = None
        fixture.expected([row], counts(1, 1, bad_time=1), archive_unresolved=counts(1, bad_time=0), family=['selected_time_unattributable', 'selection_unresolved'], zone=tzif((-43200, 43200), (('2024-01-02T12:00:00', 1),)))
        fixture.verify()

    def test_disjoint_preimage_and_extreme_windows_refused(self):
        zone = pinned_zone(tzif((0, -43200), (('2024-01-02T06:00:00', 1),)))
        with self.assertRaisesRegex(Refusal, '^WINDOW_UNREPRESENTABLE$'):
            civil_days('2024-01-03', 1, zone)
        for closing, days in [('0001-01-02', 1), ('9999-12-31', 1), ('2024-01-03', True), ('2024-1-03', 1), ('2024-01-03', 0)]:
            with self.assertRaisesRegex(Refusal, '^WINDOW_UNREPRESENTABLE$'):
                civil_days(closing, days, pinned_zone(tzif()))

    def test_timezone_pinned_bytes_authoritative_and_invalid_payloads(self):
        fixture, content = self.basic()
        content['timezone']['key'] = 'A/Zone/Not/Installed'
        fixture.save(content)
        fixture.verify()
        for change in [{'key': '../UTC'}, {'key': '/UTC'}, {'key': ''}, {'key': 'A//B'}, {'tzif_base64': '????'}, {'tzif_sha256': '0'*64}, {'extra': 1}]:
            broken = copy.deepcopy(content)
            broken['timezone'].update(change)
            fixture.save(broken)
            with self.assertRaisesRegex(Refusal, '^TIMEZONE_INVALID$'):
                fixture.verify()

    def test_full_document_tail_zip_variants_and_unselected_family(self):
        for compression in [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]:
            fixture = self.fixture([record()], compression=compression)
            fixture.expected([day(selected=counts(1))], counts(1, bad_time=0), counts(1))
            fixture.verify()
        fixture = self.fixture([record(start='bad')], tail='<broken>')
        fixture.expected([day(selected=counts(1))], counts(1, bad_time=0), counts(1))
        with self.assertRaisesRegex(Refusal, '^BINDING_AND_ARCHIVE_INVALID$'):
            fixture.verify()
        fixture = self.fixture([record(type='HKCategoryTypeIdentifierSleepAnalysis')], selected_family='sleep')
        fixture.expected([day(state='no_records', value=None)], counts(bad_time=0))
        with self.assertRaisesRegex(Refusal, '^STEPS_NOT_SELECTED$'):
            fixture.verify()

    def test_stale_archive_binding_flags_and_metadata_mutation(self):
        fixture, content = self.basic()
        original = fixture.binding.read_bytes()
        for flag in ['device_identity_verified', 'user_intent_verified', 'health_source_priority_verified']:
            bound = copy.deepcopy(fixture.bound)
            bound[flag] = 0
            fixture.binding.write_bytes(wire({'binding': bound, 'binding_sha256': sha(bound)}))
            with self.assertRaisesRegex(Refusal, '^BINDING_AND_ARCHIVE_INVALID$'):
                fixture.verify()
        fixture.binding.write_bytes(original)
        with fixture.archive.open('ab') as output:
            output.write(b'changed')
        with self.assertRaisesRegex(Refusal, '^BINDING_AND_ARCHIVE_INVALID$'):
            fixture.verify()
        fixture, _ = self.basic()
        gate = verify_steps.verify_catalogue
        def mutate(*args):
            gate(*args)
            fixture.binding.write_bytes(fixture.binding.read_bytes() + b' ')
        with patch.object(verify_steps, 'verify_catalogue', side_effect=mutate):
            with self.assertRaisesRegex(Refusal, '^INPUT_CHANGED$'):
                fixture.verify()

    def test_archive_replaced_between_gate_and_measurement(self):
        fixture, _ = self.basic()
        gate = verify_steps.verify_catalogue
        def mutate(*args):
            gate(*args)
            with fixture.archive.open('ab') as output:
                output.write(b'stable different archive')
        with patch.object(verify_steps, 'verify_catalogue', side_effect=mutate):
            with self.assertRaisesRegex(Refusal, '^INPUT_CHANGED$'):
                fixture.verify()

    def test_archive_before_after_same_handle_and_full_xml_guard(self):
        fixture, content = self.basic()
        measurement = Measurement([], [])
        original = measurement.start
        touched = False
        def mutate(name, attrs):
            nonlocal touched
            original(name, attrs)
            if not touched:
                touched = True
                with fixture.archive.open('ab') as output:
                    output.write(b'mutation during parse')
        measurement.start = mutate
        with self.assertRaisesRegex(Refusal, '^INPUT_CHANGED$'):
            read_measurements(fixture.archive, content['archive'], measurement)

    def test_rehashed_wrong_fields_and_types_are_read_only_refused(self):
        fixture, content = self.basic()
        changes = [(('schema_version',), 'other'), (('policy_version',), 'other'), (('producer_version',), 'other'), (('metric',), 'other'), (('unit',), 'other'), (('selection_handle',), 'other'), (('binding_sha256',), '0'*64), (('archive', 'bytes'), float(content['archive']['bytes'])), (('archive', 'xml_member'), 'other'), (('window', 'start'), '2024-01-01'), (('family_reasons',), ['selection_unresolved']), (('archive_counts', 'selected', 'occurrences'), True), (('archive_counts', 'selected', 'time_attributable'), 0), (('archive_counts', 'selected', 'invalid_time'), 1), (('window_counts', 'selected', 'invalid_occurrences'), 1), (('archive_repeat_candidate_extras',), 1), (('window_repeat_candidate_extras',), 1), (('days', 0, 'date'), '2024-01-01'), (('days', 0, 'utc_start'), '2024-01-02T00:00:01.000000Z'), (('days', 0, 'utc_end'), '2024-01-03T00:00:01.000000Z'), (('days', 0, 'utc_duration_seconds'), 86400.0), (('days', 0, 'calculation_state'), 'no_records'), (('days', 0, 'value'), 2.0), (('days', 0, 'value'), 1), (('days', 0, 'reasons'), ['invalid_selected_record']), (('days', 0, 'observation_completeness'), 'known'), (('days', 0, 'valid_metric_day'), True), (('days', 0, 'inference_eligible'), True), (('days', 0, 'repeat_candidate_extras'), 1), (('days', 0, 'diagnostics', 'selected_quantity_overlap'), None), (('days', 0, 'diagnostics', 'selected_excluded_overlap'), True), (('days', 0, 'counts', 'selected', 'occurrences'), 0)]
        for keys, replacement in changes:
            with self.subTest(keys=keys, replacement=replacement):
                broken = copy.deepcopy(content)
                location = broken
                for key in keys[:-1]:
                    location = location[key]
                location[keys[-1]] = replacement
                fixture.save(broken)
                before = fixture.report.read_bytes()
                with self.assertRaises(Refusal):
                    fixture.verify()
                self.assertEqual(fixture.report.read_bytes(), before)
        for location in [(), ('days', 0), ('window',), ('archive',), ('days', 0, 'counts', 'selected')]:
            broken = copy.deepcopy(content)
            target = broken
            for key in location:
                target = target[key]
            target['unknown'] = None
            fixture.save(broken)
            with self.assertRaises(Refusal):
                fixture.verify()

    def test_same_total_wrong_distribution_order_and_negative_zero(self):
        fixture = self.fixture([record(value='0'), record(start='2024-01-03 01:00:00Z', end='2024-01-03 02:00:00Z', value='3')])
        rows = [day(value=0.0, selected=counts(1)), day(date='2024-01-03', start='2024-01-03T00:00:00.000000Z', end='2024-01-04T00:00:00.000000Z', value=3.0, selected=counts(1))]
        content = fixture.expected(rows, counts(2, bad_time=0), counts(2), as_of='2024-01-04', end='2024-01-03')
        fixture.verify()
        for first, second in [(1.0, 2.0), (-0.0, 3.0)]:
            broken = copy.deepcopy(content)
            broken['days'][0]['value'], broken['days'][1]['value'] = first, second
            fixture.save(broken)
            with self.assertRaisesRegex(Refusal, '^REPORT_MISMATCH$'):
                fixture.verify()
        content['days'].reverse()
        fixture.save(content)
        with self.assertRaisesRegex(Refusal, '^REPORT_MISMATCH$'):
            fixture.verify()

    def test_strict_json_and_safe_cli_errors(self):
        fixture, _ = self.basic()
        paths = ['--archive', str(fixture.archive), '--catalogue', str(fixture.catalogue), '--binding', str(fixture.binding), '--report', str(fixture.report)]
        for raw in [b'{"report":{},"report":{},"report_sha256":"x"}', b'{"report":NaN}', b'{"report":Infinity}', b'{"report":1e999}', b'\xff', b'{}']:
            fixture.report.write_bytes(raw)
            before = fixture.report.read_bytes()
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(verify_steps.main(paths), 2)
            message = json.loads(out.getvalue())
            self.assertEqual(set(message), {'status', 'verification_scope', 'checks', 'diagnostics'})
            self.assertEqual(message['status'], 'FAIL')
            self.assertEqual(message['checks'], [])
            self.assertEqual(len(message['diagnostics']), 1)
            self.assertNotIn(str(self.root), out.getvalue())
            self.assertEqual(err.getvalue(), '')
            self.assertEqual(fixture.report.read_bytes(), before)
        command = subprocess.run([sys.executable, str(Path(verify_steps.__file__)), '--help'], capture_output=True, text=True)
        self.assertEqual(command.returncode, 2)
        self.assertEqual(command.stderr, '')
        self.assertEqual(json.loads(command.stdout)['diagnostics'], ['ARGUMENTS_INVALID'])

    def test_archive_repeats_outside_window_and_missing_unit_repeat_tags(self):
        old = record(start='2023-01-01T01:00:00Z', end='2023-01-01T02:00:00Z')
        fixture = self.fixture([record(), old, old])
        fixture.expected([day(selected=counts(1))], counts(3, bad_time=0), counts(1), archive_repeat=1)
        fixture.verify()
        fixture = self.fixture([record(unit=None), record(unit='')])
        fixture.expected([day(state='blocked', value=None, reasons=['invalid_selected_record', 'selected_quantity_overlap'], selected=counts(2, 2, unit=2), overlap=True)], counts(2, 2, unit=2, bad_time=0), counts(2, 2, unit=2))
        fixture.verify()

    def test_multiple_defects_cross_day_repeats_accumulate_all_reasons(self):
        crossing = record(start='2024-01-01T23:00:00Z', end='2024-01-03T01:00:00Z', value='bad', unit='wrong')
        fixture = self.fixture([crossing, crossing, record(start=None)])
        expected = ['selected_time_unattributable', 'invalid_selected_record', 'cross_day_quantity_unallocated', 'selected_quantity_overlap', 'selected_repeat_candidate']
        fixture.expected([day(state='blocked', value=None, reasons=expected, selected=counts(2, 2, 2, 2), repeat=1, overlap=True, pair=None)], counts(3, 3, 2, 2, bad_time=1), counts(2, 2, 2, 2), family=['selected_time_unattributable'], archive_repeat=1, window_repeat=1)
        fixture.verify()

    def test_own_xml_pass_namespace_ordinal_tail_and_external_guards(self):
        path = self.root / 'direct.zip'
        examples = [
            ('<HealthData><Record type="unrelated"/><x:Record xmlns:x="urn:other"/><Record type="HKQuantityTypeIdentifierStepCount" sourceName="other" startDate="2024-01-02T00:00:00Z" endDate="2024-01-02T00:00:00Z" value="1" unit="count"/></HealthData>', None),
            ('<HealthData><Record/></HealthData><bad>', 'XML_INVALID'),
            ('<HealthData xmlns="urn:other"/>', 'XML_INVALID'),
            ('<!DOCTYPE HealthData SYSTEM "file:///invented-private-never-read"><HealthData/>', 'XML_EXTERNAL_RESOURCE'),
            ('<!DOCTYPE HealthData [<!ENTITY ext SYSTEM "file:///invented-private-never-read">]><HealthData>&ext;</HealthData>', 'XML_EXTERNAL_RESOURCE'),
            ('<?xml version="1.0" encoding="not-a-real-encoding"?><HealthData/>', 'XML_INVALID'),
        ]
        for xml, refusal in examples:
            with self.subTest(refusal=refusal, xml=xml):
                with zipfile.ZipFile(path, 'w') as output:
                    output.writestr('export.xml', xml)
                raw = path.read_bytes()
                identity = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'xml_member': 'export.xml'}
                measured = Measurement([], [])
                if refusal:
                    with self.assertRaisesRegex(Refusal, '^' + refusal + '$'):
                        read_measurements(path, identity, measured)
                else:
                    read_measurements(path, identity, measured)
                    self.assertEqual(measured.ordinal, 2)
                    self.assertEqual(measured.archive, partitions(excluded=counts(1, bad_time=0), archive=True))

    def test_every_catalogue_family_is_validated_and_corrupt_digest_rejected(self):
        fixture = self.fixture([record(), record(type='HKCategoryTypeIdentifierSleepAnalysis', source='sleep')])
        content = fixture.expected([day(selected=counts(1))], counts(1, bad_time=0), counts(1))
        fixture.verify()
        cat = copy.deepcopy(fixture.cat)
        cat['families']['sleep'][0]['record_count'] = 2
        fixture.catalogue.write_bytes(wire({'catalogue': cat, 'catalogue_sha256': sha(cat)}))
        with self.assertRaisesRegex(Refusal, '^BINDING_AND_ARCHIVE_INVALID$'):
            fixture.verify()
        fixture, content = self.basic()
        fixture.report.write_bytes(wire({'report': content, 'report_sha256': '0'*64}))
        with self.assertRaisesRegex(Refusal, '^REPORT_INVALID$'):
            fixture.verify()

    def test_import_restores_search_path_on_success_and_dependency_failure(self):
        script = r"""
import builtins
from pathlib import Path
import runpy
import sys

module_path = Path(sys.argv[1])
sys.path.insert(0, str(module_path.parent))
original_path_object = sys.path
original_entries = sys.path[:]
original_import = builtins.__import__


def synthetic_import(name, *args, **kwargs):
    if name == 'verify_catalogue' and sys.argv[2] == 'failure':
        raise ImportError('invented dependency import failure')
    return original_import(name, *args, **kwargs)


builtins.__import__ = synthetic_import
try:
    namespace = runpy.run_path(str(module_path), run_name='invented_import_probe')
except ImportError as error:
    assert sys.argv[2] == 'failure', str(error)
    assert str(error) == 'invented dependency import failure'
else:
    assert sys.argv[2] == 'success'
    assert callable(namespace['verify_catalogue'])
finally:
    builtins.__import__ = original_import
assert sys.path is original_path_object
assert sys.path == original_entries, (original_entries, sys.path)
"""
        for outcome in ('success', 'failure'):
            with self.subTest(outcome=outcome):
                command = subprocess.run([sys.executable, '-c', script, str(Path(verify_steps.__file__)), outcome], capture_output=True, text=True)
                self.assertEqual(command.returncode, 0, command.stderr)
                self.assertEqual(command.stdout, '')
                self.assertEqual(command.stderr, '')

    def test_public_cli_pass_fixed_schema(self):
        fixture, _ = self.basic()
        command = subprocess.run([sys.executable, str(Path(verify_steps.__file__)), '--archive', str(fixture.archive), '--catalogue', str(fixture.catalogue), '--binding', str(fixture.binding), '--report', str(fixture.report)], capture_output=True, text=True)
        self.assertEqual(command.returncode, 0, command.stdout)
        self.assertEqual(command.stderr, '')
        self.assertEqual(json.loads(command.stdout), {'status': 'PASS', 'verification_scope': 'selected_steps_report_exact_recomputation', 'checks': ['binding_and_archive', 'pinned_timezone_boundaries', 'daily_steps_and_diagnostics', 'report_identity'], 'diagnostics': []})


if __name__ == '__main__':
    unittest.main()
