"""Independent streaming measurement pass; no primary evaluator dependencies.

Shared standard-library primitives are Expat, SHA-256/JSON, datetime/ZoneInfo,
integer arithmetic, Fraction, and Python's correctly rounded Fraction-to-float.
"""
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import zipfile
from xml.parsers import expat

from oracle_time import Refusal, civil_days, pinned_zone, timestamp, utc_text

PARTITIONS = ('selected', 'excluded', 'unresolved')
DECIMAL = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE]([+-]?[0-9]+))?', re.ASCII)
STEP_TYPE = 'HKQuantityTypeIdentifierStepCount'


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (ValueError, TypeError, UnicodeError):
        raise Refusal('JSON_INVALID') from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refusal('JSON_INVALID')
            result[key] = value
        return result

    def reject(_):
        raise Refusal('JSON_INVALID')

    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=reject)
        canonical(value)  # Reject overflowed JSON numeric literals, too.
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise Refusal('JSON_INVALID') from None


def unchanged(path, expected):
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise Refusal('INPUT_CHANGED')
    except OSError:
        raise Refusal('INPUT_CHANGED') from None


def exact_value(token):
    if token is None:
        return None
    if len(token) > 4096:
        raise Refusal('INPUT_RESOURCE_LIMIT')
    match = DECIMAL.fullmatch(token)
    if match is None:
        return None
    exponent = int(match.group(1) or '0')
    if abs(exponent) > 100000:
        raise Refusal('INPUT_RESOURCE_LIMIT')
    significand = re.split('[eE]', token)[0]
    negative = significand.startswith('-')
    significand = significand.lstrip('+-')
    whole, dot, tail = significand.partition('.')
    integer = int((whole + tail) or '0')
    if negative and integer:
        return None
    power = exponent - len(tail)
    if power < 0:
        return Fraction(integer, 10 ** -power)
    return Fraction(integer * 10 ** power, 1)


def empty_counts(archive=False):
    keys = ('occurrences', 'invalid_occurrences', 'invalid_value', 'invalid_unit')
    if archive:
        keys += ('time_attributable', 'time_unattributable', 'invalid_time')
    return {p: dict.fromkeys(keys, 0) for p in PARTITIONS}


def increment(counts, partition, bad_time, bad_value, bad_unit, archive=False):
    row = counts[partition]
    row['occurrences'] += 1
    row['invalid_occurrences'] += int(bad_time or bad_value or bad_unit)
    row['invalid_value'] += int(bad_value)
    row['invalid_unit'] += int(bad_unit)
    if archive:
        row['time_attributable'] += int(not bad_time)
        row['time_unattributable'] += int(bad_time)
        row['invalid_time'] += int(bad_time)


def extras(keys):
    return sum(count - 1 for count in keys.values())


def collision(events, between=False):
    """Sweep clipped intervals and points. Endpoints are excluded for intervals."""
    farthest = {p: -1 for p in ('selected', 'excluded')}
    last_point = {p: -1 for p in ('selected', 'excluded')}
    for start, end, partition in sorted(events):
        other = ('excluded' if partition == 'selected' else 'selected') if between else partition
        if farthest[other] > start or last_point[other] == start:
            return True
        if start == end:
            last_point[partition] = start
        else:
            farthest[partition] = max(farthest[partition], end)
    return False


class Measurement:
    def __init__(self, selected_ids, intervals):
        self.selected_ids = set(selected_ids)
        self.intervals = intervals
        self.archive = empty_counts(True)
        self.window = empty_counts()
        self.daily = [empty_counts() for _ in intervals]
        self.reasons = [set() for _ in intervals]
        self.values = [Fraction(0) for _ in intervals]
        self.events = [[] for _ in intervals]
        self.daily_repeats = [Counter() for _ in intervals]
        self.archive_repeats = Counter()
        self.window_repeats = Counter()
        self.family = set()
        self.depth = 0
        self.root = False
        self.ordinal = 0

    def start(self, name, attrs):
        if self.depth == 0:
            if self.root or name != 'HealthData':
                raise Refusal('XML_INVALID')
            self.root = True
        self.depth += 1
        if name != 'Record':
            return
        self.ordinal += 1
        if attrs.get('type') != STEP_TYPE:
            return
        descriptor = [['present', attrs[k]] if k in attrs else ['missing'] for k in ('sourceName', 'sourceVersion', 'device')]
        group = digest({'family': 'steps', 'descriptor': descriptor})
        partition = 'selected' if group in self.selected_ids else ('excluded' if attrs.get('sourceName', '').strip() else 'unresolved')
        start, end = timestamp(attrs.get('startDate')), timestamp(attrs.get('endDate'))
        bad_time = start is None or end is None or end < start
        value = exact_value(attrs.get('value'))
        bad_value, bad_unit = value is None, attrs.get('unit') != 'count'
        increment(self.archive, partition, bad_time, bad_value, bad_unit, True)
        if partition == 'unresolved':
            self.family.add('selection_unresolved')
        if partition == 'selected' and bad_time:
            self.family.add('selected_time_unattributable')
        if bad_time:
            return
        assigned = [i for i, (_, left, right) in enumerate(self.intervals) if left is not None and (left <= start < right if start == end else start < right and end > left)]
        repeat = (group, start, end, tuple(['present', attrs['unit']] if 'unit' in attrs else ['missing']), tuple(['present', attrs['value']] if 'value' in attrs else ['missing']))
        if partition == 'selected':
            self.archive_repeats[repeat] += 1
        if assigned:
            increment(self.window, partition, False, bad_value, bad_unit)
            if partition == 'selected':
                self.window_repeats[repeat] += 1
        for index in assigned:
            increment(self.daily[index], partition, False, bad_value, bad_unit)
            _, left, right = self.intervals[index]
            if partition != 'unresolved':
                self.events[index].append((max(start, left), min(end, right), partition))
            if partition != 'selected':
                continue
            self.daily_repeats[index][repeat] += 1
            if bad_value or bad_unit:
                self.reasons[index].add('invalid_selected_record')
            allocatable = start == end or left <= start and end <= right
            if not allocatable:
                self.reasons[index].add('cross_day_quantity_unallocated')
            if not bad_value and not bad_unit and allocatable:
                self.values[index] += value

    def end(self, _):
        self.depth -= 1

    def rows(self):
        unknown_selected = bool(self.archive['selected']['time_unattributable'] or self.archive['unresolved']['occurrences'])
        unknown_pair = bool(unknown_selected or self.archive['excluded']['time_unattributable'])
        result = []
        for i, (day, left, right) in enumerate(self.intervals):
            repeat = extras(self.daily_repeats[i])
            overlap = collision([event for event in self.events[i] if event[2] == 'selected'])
            pair_overlap = collision(self.events[i], between=True)
            reasons = set(self.family) | self.reasons[i]
            value = None
            if left is None:
                state, reasons, overlap, pair_overlap = 'no_civil_day', {'no_civil_day'}, False, False
            else:
                if overlap:
                    reasons.add('selected_quantity_overlap')
                if repeat:
                    reasons.add('selected_repeat_candidate')
                overlap = True if overlap else (None if unknown_selected else False)
                pair_overlap = True if pair_overlap else (None if unknown_pair else False)
                if reasons:
                    state = 'blocked'
                elif self.daily[i]['selected']['occurrences'] == 0:
                    state = 'no_records'
                else:
                    total = self.values[i]
                    try:
                        value = float(total)
                    except OverflowError:
                        value = math.inf
                    if not math.isfinite(value) or total > 0 and value == 0:
                        state, value = 'blocked', None
                        reasons.add('numeric_result_out_of_range')
                    else:
                        state = 'computed'
                        if value == 0:
                            value = 0.0
            result.append({'date': day, 'utc_start': None if left is None else utc_text(left), 'utc_end': None if right is None else utc_text(right), 'utc_duration_seconds': 0 if left is None else (right-left)//1000000, 'calculation_state': state, 'value': value, 'reasons': sorted(reasons), 'observation_completeness': 'unknown', 'valid_metric_day': None, 'inference_eligible': False, 'counts': self.daily[i], 'repeat_candidate_extras': repeat, 'diagnostics': {'selected_quantity_overlap': overlap, 'selected_excluded_overlap': pair_overlap}})
        return result


def hash_handle(stream):
    stream.seek(0)
    digestor = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        digestor.update(block)
        size += len(block)
    return digestor.hexdigest(), size


def read_measurements(path, archive_identity, measurement):
    try:
        with path.open('rb') as raw:
            expected = (archive_identity['sha256'], archive_identity['bytes'])
            if hash_handle(raw) != expected:
                raise Refusal('INPUT_CHANGED')
            raw.seek(0)
            with zipfile.ZipFile(raw) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and PurePosixPath(m.filename).name == 'export.xml']
                if len(members) != 1 or members[0].filename != archive_identity['xml_member']:
                    raise Refusal('ARCHIVE_INVALID')
                parser = expat.ParserCreate(namespace_separator='}')
                parser.StartElementHandler = measurement.start
                parser.EndElementHandler = measurement.end

                def doctype(name, system, public, internal):
                    if system is not None:
                        raise Refusal('XML_EXTERNAL_RESOURCE')

                def external(*_):
                    raise Refusal('XML_EXTERNAL_RESOURCE')

                parser.StartDoctypeDeclHandler = doctype
                parser.ExternalEntityRefHandler = external
                parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_ALWAYS)
                with archive.open(members[0]) as member:
                    while block := member.read(65536):
                        parser.Parse(block, False)
                    parser.Parse(b'', True)
            if hash_handle(raw) != expected:
                raise Refusal('INPUT_CHANGED')
        if not measurement.root or measurement.depth:
            raise Refusal('XML_INVALID')
    except (expat.ExpatError, LookupError, UnicodeError):
        raise Refusal('XML_INVALID') from None
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise Refusal('ARCHIVE_INVALID') from None


def reconstruct(archive_path, catalogue, binding_envelope, supplied):
    binding = binding_envelope['binding']
    selection = binding['selection_request']['selections'].get('steps')
    if selection is None:
        raise Refusal('STEPS_NOT_SELECTED')
    if type(supplied) is not dict or type(supplied.get('window')) is not dict:
        raise Refusal('REPORT_INVALID')
    zone = pinned_zone(supplied.get('timezone'))
    window, intervals = civil_days(supplied['window'].get('as_of'), supplied['window'].get('days'), zone)
    identity = {'sha256': catalogue['archive_sha256'], 'bytes': catalogue['archive_bytes'], 'xml_member': catalogue['xml_member']}
    measurement = Measurement(selection['group_ids'], intervals)
    read_measurements(archive_path, identity, measurement)
    if sum(measurement.archive[p]['occurrences'] for p in PARTITIONS) != sum(g['record_count'] for g in catalogue['families']['steps']):
        raise Refusal('INPUT_CHANGED')
    return {'schema_version': 'mesh-steps-report-v1', 'policy_version': 'mesh-selected-steps-v1', 'producer_version': 'mesh-steps-evaluator-v1', 'metric': 'selected_source_recorded_steps', 'unit': 'count', 'archive': identity, 'binding_sha256': digest(binding), 'selection_handle': selection['selection_handle'], 'window': window, 'timezone': supplied['timezone'], 'family_reasons': sorted(measurement.family), 'archive_counts': measurement.archive, 'window_counts': measurement.window, 'archive_repeat_candidate_extras': extras(measurement.archive_repeats), 'window_repeat_candidate_extras': extras(measurement.window_repeats), 'days': measurement.rows()}
