#!/usr/bin/env python3
"""Synthetic black-box tests for the independent presence verifier."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


HERE = Path(__file__).resolve().parent
VERIFIER = HERE / "verify_presence.py"
FAMILIES = (
    "steps",
    "hrv_sdnn",
    "sleep_asleep",
    "sleep_in_bed",
    "sleep_awake",
)
STEP = "HKQuantityTypeIdentifierStepCount"
HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP_CORE = "HKCategoryValueSleepAnalysisAsleepCore"
ASLEEP_VALUES = (
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    ASLEEP_CORE,
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
)
IN_BED = "HKCategoryValueSleepAnalysisInBed"
AWAKE = "HKCategoryValueSleepAnalysisAwake"


def record(record_type: str, start: str, end: str, **attributes: str) -> str:
    fields = {
        "type": record_type,
        "sourceName": "fixture-private-source",
        "startDate": start,
        "endDate": end,
        **attributes,
    }
    encoded = " ".join(
        f'{key}="{value.replace("&", "&amp;").replace(chr(34), "&quot;")}"'
        for key, value in fields.items()
    )
    return f"  <Record {encoded}/>"


class PresenceVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_archive(
        self,
        records: list[str],
        *,
        xml_tail: str = "</HealthData>",
        members: tuple[str, ...] = ("apple_health_export/export.xml",),
    ) -> Path:
        archive = self.root / "health.zip"
        xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<HealthData>\n"
        xml += "\n".join(records)
        xml += f"\n{xml_tail}"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for member in members:
                output.writestr(member, xml)
        return archive

    def report_for(
        self,
        archive: Path,
        counts: dict[str, list[int]] | None = None,
        *,
        timezone: str = "America/New_York",
        as_of: str = "2024-03-12",
        days: int = 3,
    ) -> dict[str, object]:
        date_sets = {
            ("0001-01-02", 1): ["0001-01-01"],
            ("2012-01-01", 3): ["2011-12-29", "2011-12-30", "2011-12-31"],
            ("2024-03-12", 3): ["2024-03-09", "2024-03-10", "2024-03-11"],
            ("2024-11-05", 3): ["2024-11-02", "2024-11-03", "2024-11-04"],
        }
        dates = date_sets[(as_of, days)]
        family_counts = counts or {family: [0] * days for family in FAMILIES}
        payload = archive.read_bytes()
        return {
            "spike_schema": "mesh-health-presence-v1",
            "status": "MEASURED",
            "archive_sha256": hashlib.sha256(payload).hexdigest(),
            "archive_bytes": len(payload),
            "parameters": {"timezone": timezone, "as_of": as_of, "days": days},
            # The report's civil-date range is inclusive even though record
            # interval intersection uses half-open [start, end) semantics.
            "window": {"start": dates[0], "end": dates[-1]},
            "families": {
                family: {
                    "daily": [
                        {"date": day, "time_valid_record_count": count}
                        for day, count in zip(dates, family_counts[family], strict=True)
                    ]
                }
                for family in FAMILIES
            },
        }

    def write_report(self, report: object) -> Path:
        path = self.root / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def run_verifier(self, archive: Path, report: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        completed = subprocess.run(
            [sys.executable, str(VERIFIER), "--archive", str(archive), "--report", str(report)],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            output = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"verifier stdout was not one JSON document: {completed.stdout!r}; {error}")
        self.assertEqual(completed.stderr, "")
        return completed, output

    def assert_failure(self, completed: subprocess.CompletedProcess[str], output: dict[str, object], code: str) -> None:
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(output["status"], "FAIL")
        self.assertIn(code, [item["code"] for item in output["diagnostics"]])

    def test_passes_hand_built_spring_dst_and_midnight_counts(self) -> None:
        archive = self.write_archive(
            [
                record(STEP, "2024-03-09 23:59:00 -0500", "2024-03-10 00:00:00 -0500", unit="count", value="1"),
                record(STEP, "2024-03-10 23:59:00 -0400", "2024-03-11 00:00:00 -0400", unit="count", value="2"),
                record(STEP, "2024-03-11 12:00:00 -0400", "2024-03-11 12:00:00 -0400", unit="count", value="0"),
                record(STEP, "2024-03-12 00:00:00 -0400", "2024-03-12 01:00:00 -0400", unit="count", value="8"),
                record(STEP, "2024-03-10 10:00:00 -0400", "2024-03-10 09:00:00 -0400", unit="count", value="99"),
                record(HRV, "2024-03-09 20:00:00 -0500", "2024-03-09 20:01:00 -0500", unit="ms", value="30"),
                # The explicit UTC offset maps this start instant to March 9
                # in the requested reporting timezone.
                record(HRV, "2024-03-10 03:30:00 +0000", "2024-03-10 03:31:00 +0000", unit="ms", value="29"),
                record(HRV, "2024-03-10 20:00:00 -0400", "2024-03-10 20:01:00 -0400", unit="ms", value="31"),
                record(HRV, "2024-03-11 20:00:00 -0400", "not-a-time", unit="ms", value="32"),
                record(SLEEP, "2024-03-09 23:00:00 -0500", "2024-03-10 03:30:00 -0400", value=ASLEEP_CORE),
                record(SLEEP, "2024-03-10 20:00:00 -0400", "2024-03-11 06:00:00 -0400", value=IN_BED),
                record(SLEEP, "2024-03-11 07:00:00 -0400", "2024-03-11 07:05:00 -0400", value=AWAKE),
                record(SLEEP, "2024-03-10 08:00:00 -0400", "2024-03-10 09:00:00 -0400", value="HKCategoryValueSleepAnalysisFutureValue"),
                record(SLEEP, "2024-03-10 10:00:00 -0400", "2024-03-10 10:00:00 -0400", value=AWAKE),
            ]
        )
        counts = {
            "steps": [1, 1, 1],
            "hrv_sdnn": [2, 1, 0],
            "sleep_asleep": [1, 1, 0],
            "sleep_in_bed": [0, 1, 1],
            "sleep_awake": [0, 0, 1],
        }
        report = self.write_report(self.report_for(archive, counts))

        completed, output = self.run_verifier(archive, report)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(output["status"], "PASS")
        self.assertEqual(output["diagnostics"], [])
        self.assertEqual(output["checks"], {
            "report_structure": "PASS",
            "archive_integrity": "PASS",
            "daily_counts": "PASS",
        })
        rendered = json.dumps(output)
        self.assertNotIn("fixture-private-source", rendered)
        self.assertNotIn("FutureValue", rendered)
        self.assertIn("time-valid daily record counts only", rendered)

    def test_assigns_every_known_asleep_category_to_the_asleep_family(self) -> None:
        archive = self.write_archive(
            [
                record(
                    SLEEP,
                    f"2024-03-10 {10 + index:02d}:00:00 -0400",
                    f"2024-03-10 {10 + index:02d}:30:00 -0400",
                    value=category,
                )
                for index, category in enumerate(ASLEEP_VALUES, start=1)
            ]
        )
        counts = {
            "steps": [0, 0, 0],
            "hrv_sdnn": [0, 0, 0],
            "sleep_asleep": [0, 5, 0],
            "sleep_in_bed": [0, 0, 0],
            "sleep_awake": [0, 0, 0],
        }
        report = self.write_report(self.report_for(archive, counts))

        completed, output = self.run_verifier(archive, report)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(output["status"], "PASS")

    def test_passes_fall_fold_and_inclusive_metadata_with_half_open_records(self) -> None:
        archive = self.write_archive(
            [
                record(STEP, "2024-11-02 23:59:00 -0400", "2024-11-03 00:00:00 -0400", unit="count", value="1"),
                record(HRV, "2024-11-03 01:30:00 -0500", "2024-11-03 01:31:00 -0500", unit="ms", value="20"),
                # Wall-clock end appears earlier during the fold, but the UTC
                # interval is positive and belongs to November 3.
                record(SLEEP, "2024-11-03 01:30:00 -0400", "2024-11-03 01:15:00 -0500", value=AWAKE),
            ]
        )
        counts = {
            "steps": [1, 0, 0],
            "hrv_sdnn": [0, 1, 0],
            "sleep_asleep": [0, 0, 0],
            "sleep_in_bed": [0, 0, 0],
            "sleep_awake": [0, 1, 0],
        }
        report = self.write_report(
            self.report_for(archive, counts, as_of="2024-11-05", days=3)
        )

        completed, output = self.run_verifier(archive, report)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(output["status"], "PASS")

    def test_rejects_changed_report_count_with_machine_readable_comparison(self) -> None:
        archive = self.write_archive([])
        payload = self.report_for(archive)
        payload["families"]["steps"]["daily"][1]["time_valid_record_count"] = 7
        report = self.write_report(payload)

        completed, output = self.run_verifier(archive, report)

        self.assert_failure(completed, output, "daily_count_mismatch")
        mismatch = output["diagnostics"][0]
        self.assertEqual(mismatch["family"], "steps")
        self.assertEqual(mismatch["date"], "2024-03-10")
        self.assertEqual(mismatch["reported_count"], 7)
        self.assertEqual(mismatch["recomputed_count"], 0)

    def test_rejects_changed_hash_and_byte_size(self) -> None:
        archive = self.write_archive([])
        for field, changed, code in (
            ("archive_sha256", "0" * 64, "archive_hash_mismatch"),
            ("archive_bytes", archive.stat().st_size + 1, "archive_size_mismatch"),
        ):
            with self.subTest(field=field):
                payload = self.report_for(archive)
                payload[field] = changed
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, code)

    def test_rejects_archive_tampering(self) -> None:
        archive = self.write_archive([])
        report = self.write_report(self.report_for(archive))
        with archive.open("ab") as output:
            output.write(b"tamper")

        completed, output = self.run_verifier(archive, report)

        self.assert_failure(completed, output, "archive_hash_mismatch")
        self.assertEqual(output["checks"]["daily_counts"], "NOT_RUN")

    def test_rejects_missing_duplicate_and_unexpected_report_dates(self) -> None:
        archive = self.write_archive([])
        mutations = {
            "missing": lambda daily: daily.pop(),
            "duplicate": lambda daily: daily.__setitem__(2, copy.deepcopy(daily[1])),
            "unexpected": lambda daily: daily[2].__setitem__("date", "2024-03-12"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                payload = self.report_for(archive)
                mutate(payload["families"]["steps"]["daily"])
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, "invalid_report_dates")
                self.assertEqual(output["checks"]["archive_integrity"], "NOT_RUN")

    def test_rejects_non_integer_negative_and_boolean_counts(self) -> None:
        archive = self.write_archive([])
        for bad_count in (-1, 1.5, True, "0"):
            with self.subTest(count=bad_count):
                payload = self.report_for(archive)
                payload["families"]["hrv_sdnn"]["daily"][0][
                    "time_valid_record_count"
                ] = bad_count
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, "invalid_report_count")

    def test_rejects_nonpositive_noninteger_and_boolean_days(self) -> None:
        archive = self.write_archive([])
        for bad_days in (0, -1, 3.5, True, "3"):
            with self.subTest(days=bad_days):
                payload = self.report_for(archive)
                payload["parameters"]["days"] = bad_days
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, "invalid_report_parameters")

    def test_rejects_wrong_window_and_nonexact_family_set(self) -> None:
        archive = self.write_archive([])
        mutations = {
            "window_start": lambda payload: payload["window"].__setitem__("start", "2024-03-10"),
            "window_end": lambda payload: payload["window"].__setitem__("end", "2024-03-12"),
            "missing_family": lambda payload: payload["families"].pop("sleep_awake"),
            "extra_family": lambda payload: payload["families"].__setitem__("other", {"daily": []}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                payload = self.report_for(archive)
                mutate(payload)
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                expected_code = "invalid_report_window" if name.startswith("window") else "invalid_report_families"
                self.assert_failure(completed, output, expected_code)

    def test_rejects_malformed_json_and_invalid_report_header(self) -> None:
        archive = self.write_archive([])
        malformed = self.root / "report.json"
        malformed.write_text("{", encoding="utf-8")
        completed, output = self.run_verifier(archive, malformed)
        self.assert_failure(completed, output, "malformed_report_json")

        for field, value in (("spike_schema", "other"), ("status", "PARTIAL")):
            with self.subTest(field=field):
                payload = self.report_for(archive)
                payload[field] = value
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, "invalid_report_header")

    def test_rejects_partial_malformed_xml_without_using_partial_counts(self) -> None:
        valid = record(STEP, "2024-03-10 12:00:00 -0400", "2024-03-10 12:01:00 -0400", unit="count", value="1")
        archive = self.write_archive([valid], xml_tail="<Record")
        payload = self.report_for(archive)
        payload["families"]["steps"]["daily"][1]["time_valid_record_count"] = 1
        report = self.write_report(payload)

        completed, output = self.run_verifier(archive, report)

        self.assert_failure(completed, output, "malformed_export_xml")
        self.assertEqual(output["checks"]["archive_integrity"], "PASS")
        self.assertEqual(output["checks"]["daily_counts"], "FAIL")

    def test_rejects_unsupported_xml_encoding_without_leaking_parser_details(self) -> None:
        encoding_token = "unsupported-synthetic-encoding"
        archive = self.root / "health.zip"
        xml = f'<?xml version="1.0" encoding="{encoding_token}"?><HealthData/>'
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("apple_health_export/export.xml", xml)
        report = self.write_report(self.report_for(archive))

        completed, output = self.run_verifier(archive, report)

        self.assert_failure(completed, output, "malformed_export_xml")
        self.assertEqual(output["checks"]["archive_integrity"], "PASS")
        self.assertEqual(output["checks"]["daily_counts"], "FAIL")
        self.assertEqual(
            output["diagnostics"],
            [{"code": "malformed_export_xml", "message": "export.xml is malformed."}],
        )
        self.assertNotIn(encoding_token, completed.stdout)

    def test_rejects_missing_or_ambiguous_export_member(self) -> None:
        for members, code in (
            (("other.xml",), "missing_export_xml"),
            (("one/export.xml", "two/export.xml"), "ambiguous_export_xml"),
        ):
            with self.subTest(code=code):
                archive = self.write_archive([], members=members)
                report = self.write_report(self.report_for(archive))
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, code)

    def test_rejects_invalid_zip_after_integrity_check(self) -> None:
        archive = self.root / "health.zip"
        archive.write_bytes(b"not a zip archive")
        report = self.write_report(self.report_for(archive))

        completed, output = self.run_verifier(archive, report)

        self.assert_failure(completed, output, "invalid_archive_zip")
        self.assertEqual(output["checks"]["archive_integrity"], "PASS")

    def test_rejects_invalid_cli_arguments_with_structured_failure(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(VERIFIER), "--unexpected"],
            text=True,
            capture_output=True,
            check=False,
        )

        output = json.loads(completed.stdout)
        self.assert_failure(completed, output, "invalid_cli_arguments")
        self.assertEqual(completed.stderr, "")

    def test_rejects_bad_timezone_date_and_unzoned_record_times(self) -> None:
        archive = self.write_archive(
            [record(STEP, "2024-03-10 12:00:00", "2024-03-10 13:00:00", unit="count", value="1")]
        )
        report = self.write_report(self.report_for(archive))
        completed, output = self.run_verifier(archive, report)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(output["status"], "PASS")

        for field, value in (("timezone", "Not/AZone"), ("as_of", "2024-02-30")):
            with self.subTest(field=field):
                payload = self.report_for(archive)
                payload["parameters"][field] = value
                report = self.write_report(payload)
                completed, output = self.run_verifier(archive, report)
                self.assert_failure(completed, output, "invalid_report_parameters")

    def test_treats_timestamp_conversion_overflow_as_an_invalid_record(self) -> None:
        cases = (
            # Converting this fixed-offset instant to UTC would underflow year 1.
            ("utc_underflow", "0001-01-01 00:00:00 +1400"),
            # UTC conversion succeeds, but conversion to New York underflows.
            ("report_timezone_underflow", "0001-01-01 00:00:00 +0000"),
        )
        for name, timestamp in cases:
            with self.subTest(name=name):
                archive = self.write_archive(
                    [record(HRV, timestamp, timestamp, unit="ms", value="20")]
                )
                report = self.write_report(self.report_for(archive))

                completed, output = self.run_verifier(archive, report)

                self.assertEqual(completed.returncode, 0)
                self.assertEqual(output["status"], "PASS")
                self.assertEqual(output["checks"]["daily_counts"], "PASS")

    def test_skipped_civil_date_has_no_interval_presence(self) -> None:
        archive = self.write_archive(
            [
                record(
                    STEP,
                    "2011-12-29 00:00:00 -1000",
                    "2011-12-31 23:00:00 +1400",
                    unit="count",
                    value="10",
                )
            ]
        )
        counts = {
            "steps": [1, 0, 1],
            "hrv_sdnn": [0, 0, 0],
            "sleep_asleep": [0, 0, 0],
            "sleep_in_bed": [0, 0, 0],
            "sleep_awake": [0, 0, 0],
        }
        report = self.write_report(
            self.report_for(
                archive,
                counts,
                timezone="Pacific/Apia",
                as_of="2012-01-01",
                days=3,
            )
        )

        completed, output = self.run_verifier(archive, report)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(output["status"], "PASS")

    def test_unrepresentable_reporting_day_is_a_structured_failure(self) -> None:
        archive = self.write_archive([])
        report = self.write_report(
            self.report_for(
                archive,
                timezone="Etc/GMT-14",
                as_of="0001-01-02",
                days=1,
            )
        )

        completed, output = self.run_verifier(archive, report)

        self.assert_failure(completed, output, "unrepresentable_report_day")
        self.assertEqual(output["checks"]["archive_integrity"], "PASS")
        self.assertEqual(output["checks"]["daily_counts"], "FAIL")

    def test_rejects_missing_archive_or_report_with_structured_failure(self) -> None:
        missing_archive = self.root / "missing.zip"
        missing_report = self.root / "missing.json"
        archive = self.write_archive([])
        report = self.write_report(self.report_for(archive))

        completed, output = self.run_verifier(missing_archive, report)
        self.assert_failure(completed, output, "archive_read_error")

        completed, output = self.run_verifier(archive, missing_report)
        self.assert_failure(completed, output, "report_read_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
