"""Independent integer-time parsing and exhaustive pinned civil-date replay."""
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import base64
import hashlib
import re
from zoneinfo import ZoneInfo


class Refusal(Exception):
    """Only fixed, public diagnostic codes cross the command boundary."""


UTC = timezone.utc
ORIGIN = datetime(1, 1, 1, tzinfo=UTC)
STAMP = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))? ?(Z|[+-][0-9]{2}:?[0-9]{2})', re.ASCII)
DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', re.ASCII)


def micros(value):
    delta = value - ORIGIN
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def timestamp(token):
    if type(token) is not str:
        return None
    match = STAMP.fullmatch(token)
    if match is None:
        return None
    d, hour, minute, second, fraction, offset = match.groups()
    try:
        parsed = datetime.fromisoformat(d).replace(hour=int(hour), minute=int(minute), second=int(second), microsecond=int((fraction or '').ljust(6, '0')), tzinfo=UTC)
        if offset != 'Z':
            digits = offset[1:].replace(':', '')
            h, m = int(digits[:2]), int(digits[2:])
            if h > 23 or m > 59:
                return None
            parsed -= timedelta(minutes=(h * 60 + m) * (1 if offset[0] == '+' else -1))
        return micros(parsed)
    except (ValueError, OverflowError):
        return None


def utc_text(value):
    dt = ORIGIN + timedelta(microseconds=value)
    return f'{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond:06d}Z'


def pinned_zone(info):
    if type(info) is not dict or set(info) != {'key', 'tzif_base64', 'tzif_sha256'}:
        raise Refusal('TIMEZONE_INVALID')
    key = info['key']
    if type(key) is not str or not key or key.startswith('/') or '\\' in key or '\x00' in key or any(part in ('', '.', '..') for part in key.split('/')):
        raise Refusal('TIMEZONE_INVALID')
    try:
        token = info['tzif_base64']
        if type(token) is not str:
            raise ValueError
        raw = base64.b64decode(token, validate=True)
        if base64.b64encode(raw).decode('ascii') != token or hashlib.sha256(raw).hexdigest() != info['tzif_sha256']:
            raise ValueError
        return ZoneInfo.from_file(BytesIO(raw), key=key)
    except (ValueError, TypeError, UnicodeError, EOFError, OSError):
        raise Refusal('TIMEZONE_INVALID') from None


def civil_days(as_of, days, zone):
    if type(as_of) is not str or not DATE.fullmatch(as_of) or type(days) is not int or days <= 0:
        raise Refusal('WINDOW_UNREPRESENTABLE')
    try:
        closing = date.fromisoformat(as_of)
        opening = closing - timedelta(days=days)
        dates = [opening + timedelta(days=i) for i in range(days)]
        low = datetime.combine(opening - timedelta(days=1), datetime.min.time(), UTC)
        high = datetime.combine(closing + timedelta(days=1), datetime.min.time(), UTC)
        runs = {day: [] for day in dates}
        previous = None
        cursor = low
        step = timedelta(seconds=1)
        while cursor < high:
            local = cursor.astimezone(zone).date()
            current = local if local in runs else None
            if current != previous:
                boundary = micros(cursor)
                if previous is not None:
                    runs[previous][-1][1] = boundary
                if current is not None:
                    if runs[current]:
                        raise Refusal('WINDOW_UNREPRESENTABLE')
                    runs[current].append([boundary, None])
                previous = current
            cursor += step
        if previous is not None:
            raise Refusal('WINDOW_UNREPRESENTABLE')
        result = [(d.isoformat(), *(runs[d][0] if runs[d] else (None, None))) for d in dates]
        return {'as_of': as_of, 'days': days, 'start': opening.isoformat(), 'end': (closing - timedelta(days=1)).isoformat()}, result
    except (ValueError, OverflowError):
        raise Refusal('WINDOW_UNREPRESENTABLE') from None
