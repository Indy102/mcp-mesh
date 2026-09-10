#!/usr/bin/env python3
"""Independently verify daily record-presence counts in a Health report."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import NoReturn
import xml.sax
from xml.sax.handler import ContentHandler, feature_external_ges, feature_external_pes
import zipfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


VERIFICATION_SCHEMA = "mesh-health-presence-verification-v1"
REPORT_SCHEMA = "mesh-health-presence-v1"
FAMILIES = (
    "steps",
    "hrv_sdnn",
    "sleep_asleep",
    "sleep_in_bed",
    "sleep_awake",
)
STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
HRV_TYPE = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP_VALUES = frozenset(
    {
        "HKCategoryValueSleepAnalysisAsleep",
        "HKCategoryValueSleepAnalysisAsleepUnspecified",
        "HKCategoryValueSleepAnalysisAsleepCore",
        "HKCategoryValueSleepAnalysisAsleepDeep",
        "HKCategoryValueSleepAnalysisAsleepREM",
    }
)
IN_BED_VALUE = "HKCategoryValueSleepAnalysisInBed"
AWAKE_VALUE = "HKCategoryValueSleepAnalysisAwake"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
LIMITATIONS = (
    "Independently verifies time-valid daily record counts only.",
    "Does not verify overlap classification, anonymous source grouping, quantity validation, metric completeness, normalization, source priority, or measured quantities.",
)


class VerificationError(Exception):
    """Expected validation failure with a safe machine-readable diagnostic."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def diagnostic(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.details}


class CliArgumentError(Exception):
    """Raised instead of letting argparse emit unstructured error output."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliArgumentError(message)


@dataclass(frozen=True)
class ReportContract:
    timezone: ZoneInfo
    expected_dates: tuple[date, ...]
    archive_sha256: str
    archive_bytes: int
    reported_counts: dict[str, dict[date, int]]


def _result(
    status: str,
    checks: dict[str, str],
    diagnostics: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "verification_schema": VERIFICATION_SCHEMA,
        "status": status,
        "checks": checks,
        "diagnostics": diagnostics,
        "limitations": list(LIMITATIONS),
    }


def _checks() -> dict[str, str]:
    return {
        "report_structure": "NOT_RUN",
        "archive_integrity": "NOT_RUN",
        "daily_counts": "NOT_RUN",
    }


def _strict_nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_civil_date(value: object) -> date:
    if not isinstance(value, str) or DATE_PATTERN.fullmatch(value) is None:
        raise ValueError
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError
    return parsed


def _read_report(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as source:
            return json.load(source)
    except json.JSONDecodeError as error:
        raise VerificationError(
            "malformed_report_json", "Report is not valid JSON."
        ) from error
    except (OSError, UnicodeError) as error:
        raise VerificationError(
            "report_read_error", "Report could not be read."
        ) from error


def _validate_report(payload: object) -> ReportContract:
    if not isinstance(payload, dict):
        raise VerificationError("invalid_report_header", "Report header is invalid.")
    if payload.get("spike_schema") != REPORT_SCHEMA or payload.get("status") != "MEASURED":
        raise VerificationError("invalid_report_header", "Report header is invalid.")

    parameters = payload.get("parameters")
    try:
        if not isinstance(parameters, dict):
            raise ValueError
        timezone_name = parameters.get("timezone")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ValueError
        report_timezone = ZoneInfo(timezone_name)
        as_of = _parse_civil_date(parameters.get("as_of"))
        days = parameters.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError) as error:
        raise VerificationError(
            "invalid_report_parameters", "Report parameters are invalid."
        ) from error

    try:
        first_date = as_of - timedelta(days=days)
        last_date = as_of - timedelta(days=1)
        expected_dates = tuple(first_date + timedelta(days=index) for index in range(days))
    except (OverflowError, MemoryError) as error:
        raise VerificationError(
            "invalid_report_parameters", "Report parameters are invalid."
        ) from error

    window = payload.get("window")
    if not isinstance(window, dict) or window.get("start") != first_date.isoformat() or window.get("end") != last_date.isoformat():
        raise VerificationError(
            "invalid_report_window", "Report window does not match its parameters."
        )

    archive_sha256 = payload.get("archive_sha256")
    archive_bytes = payload.get("archive_bytes")
    if (
        not isinstance(archive_sha256, str)
        or SHA256_PATTERN.fullmatch(archive_sha256) is None
        or not _strict_nonnegative_integer(archive_bytes)
    ):
        raise VerificationError(
            "invalid_archive_metadata", "Report archive metadata is invalid."
        )

    families = payload.get("families")
    if not isinstance(families, dict) or set(families) != set(FAMILIES):
        raise VerificationError(
            "invalid_report_families", "Report family set is invalid."
        )

    expected_date_set = set(expected_dates)
    reported_counts: dict[str, dict[date, int]] = {}
    for family in FAMILIES:
        family_report = families[family]
        if not isinstance(family_report, dict) or not isinstance(family_report.get("daily"), list):
            raise VerificationError(
                "invalid_report_dates", "Report daily date set is invalid.", family=family
            )
        daily = family_report["daily"]
        dates_seen: set[date] = set()
        family_counts: dict[date, int] = {}
        for item in daily:
            if not isinstance(item, dict):
                raise VerificationError(
                    "invalid_report_dates", "Report daily date set is invalid.", family=family
                )
            try:
                item_date = _parse_civil_date(item.get("date"))
            except (ValueError, TypeError, OverflowError) as error:
                raise VerificationError(
                    "invalid_report_dates", "Report daily date set is invalid.", family=family
                ) from error
            if item_date in dates_seen:
                raise VerificationError(
                    "invalid_report_dates", "Report daily date set is invalid.", family=family
                )
            dates_seen.add(item_date)
            count = item.get("time_valid_record_count")
            if not _strict_nonnegative_integer(count):
                raise VerificationError(
                    "invalid_report_count",
                    "Report daily count is not a nonnegative integer.",
                    family=family,
                    date=item_date.isoformat(),
                )
            family_counts[item_date] = count
        if dates_seen != expected_date_set:
            raise VerificationError(
                "invalid_report_dates", "Report daily date set is invalid.", family=family
            )
        reported_counts[family] = family_counts

    return ReportContract(
        timezone=report_timezone,
        expected_dates=expected_dates,
        archive_sha256=archive_sha256,
        archive_bytes=archive_bytes,
        reported_counts=reported_counts,
    )


def _hash_archive(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
    except OSError as error:
        raise VerificationError(
            "archive_read_error", "Archive could not be read."
        ) from error
    return digest.hexdigest(), byte_count


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidates = (value, value[:-1] + "+00:00" if value.endswith("Z") else value)
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    return None


def _convert_timezone(value: datetime, target: timezone | ZoneInfo) -> datetime | None:
    try:
        return value.astimezone(target)
    except (OverflowError, ValueError):
        return None


def _family(record_type: str | None, category: str | None) -> str | None:
    if record_type == STEP_TYPE:
        return "steps"
    if record_type == HRV_TYPE:
        return "hrv_sdnn"
    if record_type != SLEEP_TYPE:
        return None
    if category in ASLEEP_VALUES:
        return "sleep_asleep"
    if category == IN_BED_VALUE:
        return "sleep_in_bed"
    if category == AWAKE_VALUE:
        return "sleep_awake"
    return None


class PresenceHandler(ContentHandler):
    """SAX handler that retains only aggregate counts for expected dates."""

    def __init__(self, contract: ReportContract) -> None:
        super().__init__()
        self.contract = contract
        self.counts = {
            family: {day: 0 for day in contract.expected_dates} for family in FAMILIES
        }
        utc = timezone.utc
        self.day_bounds = {
            day: (
                datetime.combine(day, time.min, contract.timezone).astimezone(utc),
                datetime.combine(day + timedelta(days=1), time.min, contract.timezone).astimezone(utc),
            )
            for day in contract.expected_dates
        }

    def startElement(self, name: str, attributes: xml.sax.xmlreader.AttributesImpl) -> None:
        if name != "Record":
            return
        family = _family(attributes.get("type"), attributes.get("value"))
        if family is None:
            return

        start = _parse_timestamp(attributes.get("startDate"))
        end = _parse_timestamp(attributes.get("endDate"))
        if start is None or end is None:
            return
        start_utc = _convert_timezone(start, timezone.utc)
        end_utc = _convert_timezone(end, timezone.utc)
        if start_utc is None or end_utc is None:
            return
        if end_utc < start_utc:
            return

        if family == "hrv_sdnn":
            local_start = _convert_timezone(start_utc, self.contract.timezone)
            if local_start is None:
                return
            local_date = local_start.date()
            if local_date in self.counts[family]:
                self.counts[family][local_date] += 1
            return

        if start_utc == end_utc:
            if family != "steps":
                return
            local_start = _convert_timezone(start_utc, self.contract.timezone)
            if local_start is None:
                return
            local_date = local_start.date()
            if local_date in self.counts[family]:
                self.counts[family][local_date] += 1
            return

        for day, (day_start, day_end) in self.day_bounds.items():
            if start_utc < day_end and end_utc > day_start:
                self.counts[family][day] += 1


def _new_sax_parser() -> xml.sax.xmlreader.XMLReader:
    parser = xml.sax.make_parser()
    for feature in (feature_external_ges, feature_external_pes):
        try:
            parser.setFeature(feature, False)
        except (xml.sax.SAXNotRecognizedException, xml.sax.SAXNotSupportedException):
            pass
    return parser


def _export_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    matches = [
        member
        for member in archive.infolist()
        if not member.is_dir() and PurePosixPath(member.filename).name == "export.xml"
    ]
    if not matches:
        raise VerificationError(
            "missing_export_xml", "Archive does not contain export.xml."
        )
    if len(matches) != 1:
        raise VerificationError(
            "ambiguous_export_xml", "Archive contains more than one export.xml member."
        )
    return matches[0]


def _recompute_counts(path: Path, contract: ReportContract) -> dict[str, dict[date, int]]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            member = _export_member(archive)
            handler = PresenceHandler(contract)
            parser = _new_sax_parser()
            parser.setContentHandler(handler)
            try:
                with archive.open(member, "r") as source:
                    parser.parse(source)
            except xml.sax.SAXException as error:
                raise VerificationError(
                    "malformed_export_xml", "export.xml is malformed."
                ) from error
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                raise VerificationError(
                    "archive_member_read_error", "export.xml could not be read."
                ) from error
            return handler.counts
    except VerificationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise VerificationError(
            "invalid_archive_zip", "Archive is not a readable ZIP file."
        ) from error


def verify_presence(archive_path: Path, report_path: Path) -> tuple[dict[str, object], int]:
    checks = _checks()
    try:
        payload = _read_report(report_path)
        contract = _validate_report(payload)
        checks["report_structure"] = "PASS"
    except VerificationError as error:
        checks["report_structure"] = "FAIL"
        return _result("FAIL", checks, [error.diagnostic()]), 1

    try:
        archive_sha256, archive_bytes = _hash_archive(archive_path)
    except VerificationError as error:
        checks["archive_integrity"] = "FAIL"
        return _result("FAIL", checks, [error.diagnostic()]), 1

    if archive_sha256 != contract.archive_sha256:
        checks["archive_integrity"] = "FAIL"
        error = VerificationError(
            "archive_hash_mismatch", "Archive SHA-256 does not match report."
        )
        return _result("FAIL", checks, [error.diagnostic()]), 1
    if archive_bytes != contract.archive_bytes:
        checks["archive_integrity"] = "FAIL"
        error = VerificationError(
            "archive_size_mismatch", "Archive byte size does not match report."
        )
        return _result("FAIL", checks, [error.diagnostic()]), 1
    checks["archive_integrity"] = "PASS"

    try:
        recomputed_counts = _recompute_counts(archive_path, contract)
    except VerificationError as error:
        checks["daily_counts"] = "FAIL"
        return _result("FAIL", checks, [error.diagnostic()]), 1

    diagnostics: list[dict[str, object]] = []
    for family in FAMILIES:
        for day in contract.expected_dates:
            reported = contract.reported_counts[family][day]
            recomputed = recomputed_counts[family][day]
            if reported != recomputed:
                diagnostics.append(
                    {
                        "code": "daily_count_mismatch",
                        "message": "Reported daily count does not match independent recomputation.",
                        "family": family,
                        "date": day.isoformat(),
                        "reported_count": reported,
                        "recomputed_count": recomputed,
                    }
                )
    if diagnostics:
        checks["daily_counts"] = "FAIL"
        return _result("FAIL", checks, diagnostics), 1

    checks["daily_counts"] = "PASS"
    return _result("PASS", checks, []), 0


def _argument_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _argument_parser().parse_args(argv)
    except CliArgumentError:
        checks = _checks()
        diagnostics = [
            {
                "code": "invalid_cli_arguments",
                "message": "CLI arguments are invalid.",
            }
        ]
        print(json.dumps(_result("FAIL", checks, diagnostics), sort_keys=True))
        return 1

    result, exit_code = verify_presence(arguments.archive, arguments.report)
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
