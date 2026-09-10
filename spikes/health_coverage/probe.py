#!/usr/bin/env python3
"""Stream an Apple Health export and report record-presence diagnostics.

This is an investigative spike.  Its output deliberately excludes source names,
raw values, device details, and daily health-value totals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import tracemalloc
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "mesh-health-presence-v1"
STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
HRV_TYPE = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

SLEEP_ASLEEP_VALUES = frozenset({
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
})
SLEEP_IN_BED_VALUE = "HKCategoryValueSleepAnalysisInBed"
SLEEP_AWAKE_VALUE = "HKCategoryValueSleepAnalysisAwake"

FAMILY_NAMES = (
    "steps",
    "hrv_sdnn",
    "sleep_asleep",
    "sleep_in_bed",
    "sleep_awake",
)
EXPECTED_UNITS = {"steps": "count", "hrv_sdnn": "ms"}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
POSITIVE_INTEGER_RE = re.compile(r"[1-9]\d*\Z")


class ProbeError(Exception):
    """An expected, safely reportable probe failure."""


@dataclass(frozen=True)
class PresenceRecord:
    family: str
    source_group: str
    start: datetime
    end: datetime
    unit_text: str | None
    raw_value_text: str | None
    quantity_issue: bool

    @property
    def candidate_key(self) -> tuple[object, ...]:
        return (
            self.family,
            self.source_group,
            self.start,
            self.end,
            self.unit_text,
            self.raw_value_text,
        )


@dataclass
class FamilyState:
    name: str
    dates: tuple[date, ...]
    whole_export_record_count: int = 0
    invalid_time_record_count: int = 0
    records_intersecting_window: int = 0
    records_by_date: dict[date, list[PresenceRecord]] = field(init=False)

    def __post_init__(self) -> None:
        self.records_by_date = {day: [] for day in self.dates}


class SourceAnonymizer:
    """Assign export-local labels without exposing or hashing source names."""

    def __init__(self) -> None:
        self._labels: dict[str, str] = {}

    def label(self, source_name: str | None) -> str:
        if source_name is None or not source_name.strip():
            return "unknown-source"
        label = self._labels.get(source_name)
        if label is None:
            label = f"source-{len(self._labels) + 1:03d}"
            self._labels[source_name] = label
        return label


def parse_as_of(text: str) -> date:
    if not DATE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid civil date") from exc


def parse_positive_days(text: str) -> int:
    if not POSITIVE_INTEGER_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(text)


def parse_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProbeError("timezone must be a valid IANA zone") from exc


def parse_timestamp(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def classify_record(attributes: dict[str, str]) -> str | None:
    record_type = attributes.get("type")
    if record_type == STEP_TYPE:
        return "steps"
    if record_type == HRV_TYPE:
        return "hrv_sdnn"
    if record_type != SLEEP_TYPE:
        return None
    value = attributes.get("value")
    if value in SLEEP_ASLEEP_VALUES:
        return "sleep_asleep"
    if value == SLEEP_IN_BED_VALUE:
        return "sleep_in_bed"
    if value == SLEEP_AWAKE_VALUE:
        return "sleep_awake"
    return "unknown_sleep"


def has_quantity_issue(family: str, attributes: dict[str, str]) -> bool:
    if family not in EXPECTED_UNITS:
        return False
    if attributes.get("unit") != EXPECTED_UNITS[family]:
        return True
    raw_value = attributes.get("value")
    if raw_value is None:
        return True
    try:
        value = float(raw_value)
    except ValueError:
        return True
    return not math.isfinite(value) or value < 0


def archive_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def reporting_dates(as_of: date, days: int) -> tuple[date, ...]:
    try:
        start = as_of - timedelta(days=days)
    except OverflowError as exc:
        raise ProbeError("reporting window is outside the supported date range") from exc
    return tuple(start + timedelta(days=offset) for offset in range(days))


def utc_day_bounds(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime_time.min, tzinfo=timezone)
    end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=timezone)
    return start.astimezone(UTC), end.astimezone(UTC)


def attributed_dates(record: PresenceRecord, state: FamilyState,
                     timezone: ZoneInfo) -> tuple[date, ...]:
    if record.family == "hrv_sdnn" or record.start == record.end:
        local_day = record.start.astimezone(timezone).date()
        return (local_day,) if local_day in state.records_by_date else ()

    matched = []
    for day in state.dates:
        day_start, day_end = utc_day_bounds(day, timezone)
        if record.start < day_end and record.end > day_start:
            matched.append(day)
    return tuple(matched)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def process_export(member, states: dict[str, FamilyState], timezone: ZoneInfo) -> int:
    sources = SourceAnonymizer()
    unknown_sleep_count = 0
    try:
        elements = ElementTree.iterparse(member, events=("end",))
        for _, element in elements:
            if local_name(element.tag) == "Record":
                attributes = element.attrib
                family = classify_record(attributes)
                if family == "unknown_sleep":
                    unknown_sleep_count += 1
                elif family is not None:
                    state = states[family]
                    state.whole_export_record_count += 1
                    source_group = sources.label(attributes.get("sourceName"))
                    start = parse_timestamp(attributes.get("startDate"))
                    end = parse_timestamp(attributes.get("endDate"))
                    invalid = (
                        start is None
                        or end is None
                        or end < start
                        or (family.startswith("sleep_") and end == start)
                    )
                    if invalid:
                        state.invalid_time_record_count += 1
                    else:
                        record = PresenceRecord(
                            family=family,
                            source_group=source_group,
                            start=start,
                            end=end,
                            unit_text=attributes.get("unit"),
                            raw_value_text=attributes.get("value"),
                            quantity_issue=has_quantity_issue(family, attributes),
                        )
                        dates = attributed_dates(record, state, timezone)
                        if dates:
                            state.records_intersecting_window += 1
                            for day in dates:
                                state.records_by_date[day].append(record)
            element.clear()
    except ElementTree.ParseError as exc:
        raise ProbeError("export.xml is malformed XML") from exc
    return unknown_sleep_count


def overlap_flags(records: list[PresenceRecord], day: date,
                  timezone: ZoneInfo) -> tuple[bool, bool]:
    day_start, day_end = utc_day_bounds(day, timezone)
    unique: dict[tuple[object, ...], PresenceRecord] = {}
    for record in records:
        unique.setdefault(record.candidate_key, record)

    clipped = []
    for record in unique.values():
        start = max(record.start, day_start)
        end = min(record.end, day_end)
        if start < end:
            clipped.append((start, end, record.source_group))
    clipped.sort(key=lambda interval: (interval[0], interval[1], interval[2]))

    within_source = False
    cross_source = False
    latest_end_by_source: dict[str, datetime] = {}
    for start, end, source in clipped:
        previous_same = latest_end_by_source.get(source)
        if previous_same is not None and start < previous_same:
            within_source = True
        if any(other != source and start < latest_end
               for other, latest_end in latest_end_by_source.items()):
            cross_source = True
        if previous_same is None or end > previous_same:
            latest_end_by_source[source] = end
    return within_source, cross_source


def render_family(state: FamilyState, timezone: ZoneInfo) -> dict[str, object]:
    daily = []
    for day in state.dates:
        records = state.records_by_date[day]
        source_counts = Counter(record.source_group for record in records)
        candidates = Counter(record.candidate_key for record in records)
        duplicate_count = sum(count - 1 for count in candidates.values())
        if state.name == "hrv_sdnn":
            within_source_overlap = None
            cross_source_overlap = None
        else:
            within_source_overlap, cross_source_overlap = overlap_flags(
                records, day, timezone,
            )
        daily.append({
            "date": day.isoformat(),
            "time_valid_record_count": len(records),
            "source_counts": dict(source_counts),
            "duplicate_candidate_count": duplicate_count,
            "within_source_overlap": within_source_overlap,
            "cross_source_overlap": cross_source_overlap,
            "quantity_issue_count": sum(record.quantity_issue for record in records),
        })

    days_with_records = sum(entry["time_valid_record_count"] > 0 for entry in daily)
    return {
        "whole_export_record_count": state.whole_export_record_count,
        "invalid_time_record_count": state.invalid_time_record_count,
        "records_intersecting_window": state.records_intersecting_window,
        "valid_metric_days": None,
        "days_with_records": days_with_records,
        "no_record_days": len(state.dates) - days_with_records,
        "daily": daily,
    }


def build_report(archive_path: Path, timezone_name: str, as_of: date,
                 days: int) -> dict[str, object]:
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ProbeError("days must be a positive integer")
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise ProbeError("as_of must be a civil date")

    timezone = parse_timezone(timezone_name)
    dates = reporting_dates(as_of, days)
    states = {name: FamilyState(name, dates) for name in FAMILY_NAMES}
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    started = time.perf_counter()
    tracemalloc.start()
    try:
        sha256, archive_bytes = archive_digest(archive_path)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = [
                    info for info in archive.infolist()
                    if not info.is_dir() and PurePosixPath(info.filename).name == "export.xml"
                ]
                if len(members) != 1:
                    raise ProbeError("archive must contain exactly one export.xml member")
                with archive.open(members[0], "r") as member:
                    unknown_sleep_count = process_export(member, states, timezone)
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ProbeError("archive is not a readable ZIP") from exc

        families = {
            name: render_family(states[name], timezone)
            for name in FAMILY_NAMES
        }
        runtime_seconds = time.perf_counter() - started
        _, peak_memory = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return {
        "spike_schema": SCHEMA,
        "status": "MEASURED",
        "captured_at": captured_at,
        "archive_sha256": sha256,
        "archive_bytes": archive_bytes,
        "parameters": {
            "timezone": timezone_name,
            "as_of": as_of.isoformat(),
            "days": days,
        },
        "window": {
            "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(),
        },
        "families": families,
        "unknown_sleep_value_record_count": unknown_sleep_count,
        "measurement": {
            "runtime_seconds": runtime_seconds,
            "peak_traced_memory_bytes": peak_memory,
            "scope": (
                "archive checksum, ZIP/member validation, streaming XML parse, "
                "window attribution, diagnostics, and report construction"
            ),
        },
        "limitations": [
            "Record presence does not establish complete observation or a measured zero.",
            "A zero count means no matching exported record was found for that date.",
            "Anonymous source groups are export-local metadata; they do not identify "
            "devices or establish source priority.",
            "Duplicate candidates and overlaps do not establish record identity, "
            "measurement error, or which source should be preferred.",
            "Daily attribution is investigative and is not sleep-session normalization.",
            "Quantity issues do not remove time-valid presence; metric-valid days are "
            "not assessed and no declared coverage may enable inference.",
        ],
    }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report Apple Health record presence for completed civil dates.",
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--as-of", required=True, type=parse_as_of)
    parser.add_argument("--days", required=True, type=parse_positive_days)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = argument_parser()
    arguments = parser.parse_args(argv)
    try:
        report = build_report(
            arguments.archive,
            arguments.timezone,
            arguments.as_of,
            arguments.days,
        )
    except (ProbeError, OSError) as exc:
        parser.exit(2, f"probe failed: {exc}\n")
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
