"""Streaming occurrence accounting and exact selected-source observations."""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
import math
import re
from steps_time import StepsError, timestamp, format_utc

PARTITIONS = ('selected', 'excluded', 'unresolved')
DECIMAL = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE]([+-]?[0-9]+))?', re.ASCII)


def exact_value(raw):
    if raw is not None and len(raw) > 4096:
        raise StepsError('INPUT_RESOURCE_LIMIT')
    if raw is None or (match := DECIMAL.fullmatch(raw)) is None:
        return None
    exponent_token = match.group(1)
    exponent = 0
    if exponent_token:
        magnitude = exponent_token.lstrip('+-').lstrip('0') or '0'
        if len(magnitude) > 6 or int(magnitude) > 100000:
            raise StepsError('INPUT_RESOURCE_LIMIT')
        exponent = int(exponent_token)
    mantissa = re.split('[eE]', raw)[0]
    negative = mantissa.startswith('-')
    mantissa = mantissa.lstrip('+-')
    whole, dot, decimal = mantissa.partition('.')
    coefficient = int((whole + decimal) or '0')
    if negative and coefficient:
        return None
    scale = exponent - len(decimal)
    return Fraction(coefficient * 10 ** scale) if scale >= 0 else Fraction(coefficient, 10 ** (-scale))


def counts(archive=False):
    keys = ('occurrences', 'time_attributable', 'time_unattributable', 'invalid_occurrences',
            'invalid_time', 'invalid_value', 'invalid_unit') if archive else (
                'occurrences', 'invalid_occurrences', 'invalid_value', 'invalid_unit')
    return {partition: dict.fromkeys(keys, 0) for partition in PARTITIONS}


def count_record(target, partition, bad_time, bad_value, bad_unit):
    counter = target[partition]
    counter['occurrences'] += 1
    counter['invalid_occurrences'] += int(bad_time or bad_value or bad_unit)
    counter['invalid_value'] += int(bad_value)
    counter['invalid_unit'] += int(bad_unit)
    if 'invalid_time' in counter:
        counter['invalid_time'] += int(bad_time)
        counter['time_unattributable'] += int(bad_time)
        counter['time_attributable'] += int(not bad_time)


def collide(a, b):
    """Inputs are day-clipped nonempty intervals or in-day points."""
    s, e = a
    t, u = b
    if s == e:
        return s == t if t == u else t <= s < u
    if t == u:
        return s <= t < e
    return max(s, t) < min(e, u)


def extras(counter):
    return sum(n - 1 for n in counter.values())


class Accumulator:
    def __init__(self, intervals, selected_ids, group_digest):
        self.intervals = intervals
        self.selected_ids = set(selected_ids)
        self.group_digest = group_digest
        self.archive_counts = counts(True)
        self.window_counts = counts()
        self.archive_repeats = Counter()
        self.window_repeats = Counter()
        self.family_reasons = set()
        self.ordinal = 0
        self.rows = []
        for label, bounds in intervals:
            self.rows.append({'date': label, 'bounds': bounds, 'counts': counts(),
                              'repeats': Counter(), 'selected': [], 'excluded': [],
                              'reasons': set(), 'sum': Fraction(0)})

    def record(self, attrs):
        self.ordinal += 1
        if attrs.get('type') != 'HKQuantityTypeIdentifierStepCount':
            return
        descriptor = [['present', attrs[name]] if name in attrs else ['missing']
                      for name in ('sourceName', 'sourceVersion', 'device')]
        group_id = self.group_digest({'family': 'steps', 'descriptor': descriptor})
        partition = ('unresolved' if not attrs.get('sourceName', '').strip() else
                     'selected' if group_id in self.selected_ids else 'excluded')
        start, end = timestamp(attrs.get('startDate')), timestamp(attrs.get('endDate'))
        bad_time = start is None or end is None or end < start
        value = exact_value(attrs.get('value'))
        bad_value, bad_unit = value is None, attrs.get('unit') != 'count'
        count_record(self.archive_counts, partition, bad_time, bad_value, bad_unit)
        if partition == 'unresolved':
            self.family_reasons.add('selection_unresolved')
        if bad_time:
            if partition == 'selected':
                self.family_reasons.add('selected_time_unattributable')
            return
        repeat_key = None
        if partition == 'selected':
            tagged = lambda name: ('present', attrs[name]) if name in attrs else ('missing',)
            repeat_key = ('steps', group_id, start, end, tagged('unit'), tagged('value'))
            self.archive_repeats[repeat_key] += 1
        assigned = False
        for row in self.rows:
            if row['bounds'] is None:
                continue
            lo, hi = row['bounds']
            point = start == end
            if not (lo <= start < hi if point else max(start, lo) < min(end, hi)):
                continue
            assigned = True
            count_record(row['counts'], partition, False, bad_value, bad_unit)
            if partition in ('selected', 'excluded'):
                row[partition].append((start, end) if point else (max(start, lo), min(end, hi)))
            if partition == 'selected':
                row['repeats'][repeat_key] += 1
                if bad_value or bad_unit:
                    row['reasons'].add('invalid_selected_record')
                allocatable = point or lo <= start < end <= hi
                if not allocatable:
                    row['reasons'].add('cross_day_quantity_unallocated')
                if allocatable and not bad_value and not bad_unit:
                    row['sum'] += value
        if assigned:
            count_record(self.window_counts, partition, False, bad_value, bad_unit)
            if repeat_key is not None:
                self.window_repeats[repeat_key] += 1

    def finish(self):
        output = []
        ac = self.archive_counts
        selected_unknown = bool(ac['selected']['time_unattributable'] or ac['unresolved']['occurrences'])
        excluded_unknown = selected_unknown or bool(ac['excluded']['time_unattributable'])
        for row in self.rows:
            bounds = row['bounds']
            repeat_extras = extras(row['repeats'])
            reasons = row['reasons'] | self.family_reasons
            overlap = any(collide(a, b) for i, a in enumerate(row['selected']) for b in row['selected'][i+1:])
            cross = any(collide(a, b) for a in row['selected'] for b in row['excluded'])
            value = None
            if bounds is None:
                reasons = {'no_civil_day'}
                state = 'no_civil_day'
                diagnostics = {'selected_quantity_overlap': False, 'selected_excluded_overlap': False}
            else:
                if overlap:
                    reasons.add('selected_quantity_overlap')
                if repeat_extras:
                    reasons.add('selected_repeat_candidate')
                diagnostics = {'selected_quantity_overlap': True if overlap else None if selected_unknown else False,
                               'selected_excluded_overlap': True if cross else None if excluded_unknown else False}
                if reasons:
                    state = 'blocked'
                elif row['counts']['selected']['occurrences'] == 0:
                    state = 'no_records'
                else:
                    try:
                        value = float(row['sum'])
                    except OverflowError:
                        value = math.inf
                    if not math.isfinite(value) or (row['sum'] > 0 and value == 0):
                        reasons.add('numeric_result_out_of_range')
                        state, value = 'blocked', None
                    else:
                        state = 'computed'
                        value = 0.0 if value == 0 else value
            output.append({'date': row['date'], 'utc_start': format_utc(bounds[0]) if bounds else None,
                           'utc_end': format_utc(bounds[1]) if bounds else None,
                           'utc_duration_seconds': (bounds[1] - bounds[0]) // 1_000_000 if bounds else 0,
                           'calculation_state': state, 'value': value, 'reasons': sorted(reasons),
                           'observation_completeness': 'unknown', 'valid_metric_day': None,
                           'inference_eligible': False, 'counts': row['counts'],
                           'repeat_candidate_extras': repeat_extras, 'diagnostics': diagnostics})
        return {'family_reasons': sorted(self.family_reasons), 'archive_counts': self.archive_counts,
                'window_counts': self.window_counts, 'archive_repeat_candidate_extras': extras(self.archive_repeats),
                'window_repeat_candidate_extras': extras(self.window_repeats), 'days': output}
