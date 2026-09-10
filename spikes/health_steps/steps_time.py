"""Pinned timezone rules and exact UTC coordinates for steps observations."""
from __future__ import annotations

import base64
import hashlib
import io
import re
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
import zoneinfo


class StepsError(Exception):
    """Expected refusal with a fixed, non-private public code."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
SECOND = timedelta(seconds=1)
DAY_US = 86_400_000_000
STAMP = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))? ?(Z|[+-][0-9]{2}:?[0-9]{2})', re.ASCII)
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', re.ASCII)


def coordinate(value):
    delta = value - EPOCH
    return delta.days * DAY_US + delta.seconds * 1_000_000 + delta.microseconds


def timestamp(raw):
    """Parse admitted grammar; return None for record-level time defects."""
    if not isinstance(raw, str) or (match := STAMP.fullmatch(raw)) is None:
        return None
    d, h, m, s, fraction, offset = match.groups()
    try:
        day = date.fromisoformat(d)
        local = datetime(day.year, day.month, day.day, int(h), int(m), int(s),
                         int((fraction or '').ljust(6, '0')), tzinfo=timezone.utc)
        minutes = 0
        if offset != 'Z':
            token = offset[1:].replace(':', '')
            oh, om = int(token[:2]), int(token[2:])
            if oh > 23 or om > 59:
                return None
            minutes = (oh * 60 + om) * (1 if offset[0] == '+' else -1)
        return coordinate(local - timedelta(minutes=minutes))
    except (ValueError, OverflowError):
        return None


def valid_zone_key(key):
    return (isinstance(key, str) and bool(key) and not key.startswith('/')
            and '\\' not in key and '\x00' not in key
            and all(part not in ('', '.', '..') for part in key.split('/')))


def pinned_zone(key):
    if not valid_zone_key(key):
        raise StepsError('TIMEZONE_INVALID')
    blob = None
    try:
        for root in zoneinfo.TZPATH:
            path = Path(root).joinpath(*key.split('/'))
            if path.is_file():
                blob = path.read_bytes()
                break
        if blob is None:
            blob = resources.files('tzdata.zoneinfo').joinpath(*key.split('/')).read_bytes()
        zone = zoneinfo.ZoneInfo.from_file(io.BytesIO(blob), key=key)
    except (OSError, ValueError, ImportError, EOFError):
        raise StepsError('TIMEZONE_INVALID') from None
    return zone, {'key': key, 'tzif_base64': base64.b64encode(blob).decode('ascii'),
                  'tzif_sha256': hashlib.sha256(blob).hexdigest()}


def window_dates(as_of, days):
    if type(days) is not int or days <= 0 or not isinstance(as_of, str) or not DATE.fullmatch(as_of):
        raise StepsError('WINDOW_UNREPRESENTABLE')
    try:
        stop = date.fromisoformat(as_of)
        start = stop - timedelta(days=days)
        # The complete scan envelope must itself be representable.
        start - timedelta(days=1)
        stop + timedelta(days=1)
        dates = [start + timedelta(days=i) for i in range(days)]
    except (ValueError, OverflowError):
        raise StepsError('WINDOW_UNREPRESENTABLE') from None
    return dates, {'as_of': as_of, 'days': days, 'start': start.isoformat(),
                   'end': dates[-1].isoformat()}


def format_utc(value):
    dt = EPOCH + timedelta(microseconds=value)
    return f'{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond:06d}Z'


def day_intervals(dates, zone):
    """Find every civil-date run, without assuming that midnight exists."""
    runs = {d: [] for d in dates}
    try:
        nominal = datetime.combine(dates[0], datetime.min.time(), timezone.utc)
        utc = nominal - timedelta(days=1)
        end = datetime.combine(dates[-1], datetime.min.time(), timezone.utc) + timedelta(days=2)
        previous = None
        run_start = None
        while utc < end:
            local_date = utc.astimezone(zone).date()
            current = local_date if local_date in runs else None
            if current != previous:
                if previous is not None:
                    runs[previous].append((run_start, coordinate(utc)))
                run_start = coordinate(utc) if current is not None else None
                previous = current
            utc += SECOND
        if previous is not None:
            # A requested date reaching the padded boundary is unresolved.
            raise StepsError('WINDOW_UNREPRESENTABLE')
    except (ValueError, OverflowError):
        raise StepsError('WINDOW_UNREPRESENTABLE') from None
    result = []
    for day in dates:
        if len(runs[day]) > 1:
            raise StepsError('WINDOW_UNREPRESENTABLE')
        bounds = runs[day][0] if runs[day] else None
        result.append((day.isoformat(), bounds))
    return result
