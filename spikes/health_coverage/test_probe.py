import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr


PROBE = Path(__file__).with_name("probe.py")
STEP = "HKQuantityTypeIdentifierStepCount"
HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
ASLEEP = "HKCategoryValueSleepAnalysisAsleep"
ASLEEP_CORE = "HKCategoryValueSleepAnalysisAsleepCore"
ASLEEP_DEEP = "HKCategoryValueSleepAnalysisAsleepDeep"
ASLEEP_REM = "HKCategoryValueSleepAnalysisAsleepREM"
ASLEEP_UNSPECIFIED = "HKCategoryValueSleepAnalysisAsleepUnspecified"
IN_BED = "HKCategoryValueSleepAnalysisInBed"
AWAKE = "HKCategoryValueSleepAnalysisAwake"


def record(record_type, start=None, end=None, **attributes):
    values = {"type": record_type, **attributes}
    if start is not None:
        values["startDate"] = start
    if end is not None:
        values["endDate"] = end
    encoded = " ".join(f"{key}={quoteattr(str(value))}" for key, value in values.items())
    return f"  <Record {encoded}/>"


def export_xml(*records):
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<HealthData>\n" + "\n".join(records) + "\n</HealthData>\n")


class ProbeCliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def make_archive(self, xml, members=None):
        path = self.root / "health.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("apple_health_export/export.xml", xml)
            for name, contents in members or []:
                archive.writestr(name, contents)
        return path

    def make_named_archive(self, name, xml):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("apple_health_export/export.xml", xml)
        return path

    def run_probe(self, archive, *, timezone="America/New_York", as_of="2024-01-04",
                  days="3"):
        return subprocess.run(
            [sys.executable, str(PROBE), "--archive", str(archive),
             "--timezone", timezone, "--as-of", as_of, "--days", str(days)],
            text=True,
            capture_output=True,
            timeout=10,
        )

    def measured(self, archive, **kwargs):
        completed = self.run_probe(archive, **kwargs)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    @staticmethod
    def by_date(report, family):
        return {entry["date"]: entry for entry in report["families"][family]["daily"]}

    def test_contract_timezone_conversion_points_and_current_day_exclusion(self):
        xml = export_xml(
            record(STEP, "2024-01-02 03:30:00 +0000", "2024-01-02 03:30:00 +0000",
                   sourceName="Private Phone", sourceVersion="Private Version",
                   device="Private Device", unit="count", value="10"),
            record(HRV, "2024-01-02 05:30:00 +0000", "2024-01-02 05:30:00 +0000",
                   sourceName="Private Watch", unit="ms", value="42"),
            record(STEP, "2024-01-04 12:00:00 -0500", "2024-01-04 12:00:00 -0500",
                   sourceName="Private Phone", unit="count", value="20"),
        )
        archive = self.make_archive(xml)
        archive_before = archive.read_bytes()
        report = self.measured(archive, as_of="2024-01-04", days="3")

        self.assertEqual(archive.read_bytes(), archive_before)
        self.assertEqual(report["spike_schema"], "mesh-health-presence-v1")
        self.assertEqual(report["status"], "MEASURED")
        self.assertEqual(report["archive_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(report["archive_bytes"], archive.stat().st_size)
        self.assertEqual(report["parameters"], {
            "timezone": "America/New_York", "as_of": "2024-01-04", "days": 3,
        })
        self.assertEqual(report["window"], {"start": "2024-01-01", "end": "2024-01-03"})
        self.assertIn("captured_at", report)
        self.assertGreaterEqual(report["measurement"]["runtime_seconds"], 0)
        self.assertGreaterEqual(report["measurement"]["peak_traced_memory_bytes"], 0)
        self.assertTrue(report["measurement"]["scope"])
        self.assertTrue(report["limitations"])
        self.assertEqual(set(report["families"]), {
            "steps", "hrv_sdnn", "sleep_asleep", "sleep_in_bed", "sleep_awake",
        })

        steps = self.by_date(report, "steps")
        hrv = self.by_date(report, "hrv_sdnn")
        self.assertEqual(steps["2024-01-01"]["time_valid_record_count"], 1)
        self.assertEqual(hrv["2024-01-02"]["time_valid_record_count"], 1)
        self.assertEqual(sum(day["time_valid_record_count"] for day in steps.values()), 1)
        self.assertEqual(report["families"]["steps"]["whole_export_record_count"], 2)
        self.assertEqual(report["families"]["steps"]["records_intersecting_window"], 1)
        self.assertEqual(report["families"]["steps"]["valid_metric_days"], None)
        self.assertEqual(report["families"]["steps"]["days_with_records"], 1)
        self.assertEqual(report["families"]["steps"]["no_record_days"], 2)
        self.assertNotIn("Private Phone", json.dumps(report))
        self.assertNotIn("Private Watch", json.dumps(report))
        self.assertNotIn("Private Version", json.dumps(report))
        self.assertNotIn("Private Device", json.dumps(report))
        for family in report["families"].values():
            self.assertEqual(family["days_with_records"] + family["no_record_days"], 3)

    def test_interval_end_at_midnight_is_half_open(self):
        archive = self.make_archive(export_xml(
            record(STEP, "2024-01-01 23:00:00 -0500", "2024-01-02 00:00:00 -0500",
                   sourceName="A", unit="count", value="2"),
            record(STEP, "2024-01-02 00:00:00 -0500", "2024-01-02 01:00:00 -0500",
                   sourceName="A", unit="count", value="3"),
        ))
        report = self.measured(archive, as_of="2024-01-03", days="2")
        days = self.by_date(report, "steps")
        self.assertEqual(days["2024-01-01"]["time_valid_record_count"], 1)
        self.assertEqual(days["2024-01-02"]["time_valid_record_count"], 1)
        self.assertFalse(days["2024-01-01"]["within_source_overlap"])
        self.assertFalse(days["2024-01-02"]["within_source_overlap"])

    def test_spring_and_fall_dst_attribution_uses_instants(self):
        spring = self.make_archive(export_xml(
            record(STEP, "2024-03-10 04:30:00 +0000", "2024-03-10 07:30:00 +0000",
                   sourceName="A", unit="count", value="1"),
        ))
        spring_report = self.measured(spring, as_of="2024-03-11", days="2")
        spring_days = self.by_date(spring_report, "steps")
        self.assertEqual(spring_days["2024-03-09"]["time_valid_record_count"], 1)
        self.assertEqual(spring_days["2024-03-10"]["time_valid_record_count"], 1)

        fall = self.root / "fall.zip"
        with zipfile.ZipFile(fall, "w") as archive:
            archive.writestr("export.xml", export_xml(
                record(HRV, "2024-11-03 05:30:00 +0000", "2024-11-03 05:30:00 +0000",
                       sourceName="A", unit="ms", value="10"),
                record(HRV, "2024-11-03 06:30:00 +0000", "2024-11-03 06:30:00 +0000",
                       sourceName="A", unit="ms", value="11"),
            ))
        fall_report = self.measured(fall, as_of="2024-11-04", days="1")
        day = self.by_date(fall_report, "hrv_sdnn")["2024-11-03"]
        self.assertEqual(day["time_valid_record_count"], 2)
        self.assertIsNone(day["within_source_overlap"])
        self.assertIsNone(day["cross_source_overlap"])

    def test_skipped_civil_date_remains_empty_during_interval_attribution(self):
        archive = self.make_archive(export_xml(
            record(STEP, "2011-12-29 12:00:00 -1000", "2011-12-31 12:00:00 +1400",
                   sourceName="A", unit="count", value="1"),
        ))
        report = self.measured(
            archive, timezone="Pacific/Apia", as_of="2012-01-01", days="3",
        )
        days = self.by_date(report, "steps")
        self.assertEqual(days["2011-12-29"]["time_valid_record_count"], 1)
        self.assertEqual(days["2011-12-30"]["time_valid_record_count"], 0)
        self.assertEqual(days["2011-12-31"]["time_valid_record_count"], 1)

    def test_unrepresentable_point_local_date_is_counted_invalid_without_traceback(self):
        archive = self.make_archive(export_xml(
            record(HRV, "0001-01-01 00:00:00 +0000", "0001-01-01 00:00:00 +0000",
                   sourceName="Boundary Source", unit="ms", value="1"),
        ))
        completed = self.run_probe(
            archive, timezone="America/New_York", as_of="0001-01-02", days="1",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        report = json.loads(completed.stdout)
        family = report["families"]["hrv_sdnn"]
        self.assertEqual(family["whole_export_record_count"], 1)
        self.assertEqual(family["invalid_time_record_count"], 1)
        self.assertEqual(family["records_intersecting_window"], 0)
        self.assertEqual(family["daily"][0]["time_valid_record_count"], 0)

    def test_duplicate_candidates_and_overlap_diagnostics(self):
        first = record(STEP, "2024-01-01 09:00:00 +0000", "2024-01-01 11:00:00 +0000",
                       sourceName="A", unit="count", value="2")
        archive = self.make_archive(export_xml(
            first,
            first,
            record(STEP, "2024-01-01 10:00:00 +0000", "2024-01-01 12:00:00 +0000",
                   sourceName="A", unit="count", value="3"),
            record(STEP, "2024-01-01 10:30:00 +0000", "2024-01-01 11:30:00 +0000",
                   sourceName="B", unit="count", value="4"),
            record(STEP, "2024-01-02 09:00:00 +0000", "2024-01-02 10:00:00 +0000",
                   sourceName="A", unit="count", value="5"),
            record(STEP, "2024-01-02 10:00:00 +0000", "2024-01-02 11:00:00 +0000",
                   sourceName="B", unit="count", value="6"),
        ))
        report = self.measured(archive, timezone="UTC", as_of="2024-01-03", days="2")
        days = self.by_date(report, "steps")
        self.assertEqual(days["2024-01-01"]["time_valid_record_count"], 4)
        self.assertEqual(days["2024-01-01"]["duplicate_candidate_count"], 1)
        self.assertTrue(days["2024-01-01"]["within_source_overlap"])
        self.assertTrue(days["2024-01-01"]["cross_source_overlap"])
        self.assertEqual(days["2024-01-02"]["duplicate_candidate_count"], 0)
        self.assertFalse(days["2024-01-02"]["within_source_overlap"])
        self.assertFalse(days["2024-01-02"]["cross_source_overlap"])

    def test_sleep_categories_stay_separate_and_unknown_values_are_counted(self):
        records = [
            record(SLEEP, "2024-01-01 01:00:00 +0000", "2024-01-01 02:00:00 +0000",
                   sourceName="Sleep Source", value=value)
            for value in (ASLEEP, ASLEEP_UNSPECIFIED, ASLEEP_CORE, ASLEEP_DEEP, ASLEEP_REM)
        ]
        records.extend([
            record(SLEEP, "2024-01-01 00:00:00 +0000", "2024-01-01 08:00:00 +0000",
                   sourceName="Sleep Source", value=IN_BED),
            record(SLEEP, "2024-01-01 03:00:00 +0000", "2024-01-01 03:30:00 +0000",
                   sourceName="Sleep Source", value=AWAKE),
            record(SLEEP, "2024-01-01 04:00:00 +0000", "2024-01-01 04:30:00 +0000",
                   sourceName="Sleep Source", value="private-new-category"),
            record(SLEEP, "2024-01-01 05:00:00 +0000", "2024-01-01 05:30:00 +0000",
                   sourceName="Sleep Source"),
        ])
        report = self.measured(self.make_archive(export_xml(*records)), timezone="UTC",
                               as_of="2024-01-02", days="1")
        self.assertEqual(report["families"]["sleep_asleep"]["whole_export_record_count"], 5)
        self.assertEqual(report["families"]["sleep_in_bed"]["whole_export_record_count"], 1)
        self.assertEqual(report["families"]["sleep_awake"]["whole_export_record_count"], 1)
        self.assertEqual(report["unknown_sleep_value_record_count"], 2)
        self.assertEqual(self.by_date(report, "sleep_asleep")["2024-01-01"]
                         ["time_valid_record_count"], 5)
        self.assertNotIn("private-new-category", json.dumps(report))

    def test_sources_are_anonymous_stable_by_encounter_and_missing_is_explicit(self):
        archive = self.make_archive(export_xml(
            record(STEP, "broken", "2024-01-01 00:30:00 +0000",
                   sourceName="Invalid Secret", unit="count", value="1"),
            record(STEP, "2024-01-01 01:00:00 +0000", "2024-01-01 01:00:00 +0000",
                   sourceName="Secret B", unit="count", value="1"),
            record(STEP, "2024-01-01 02:00:00 +0000", "2024-01-01 02:00:00 +0000",
                   unit="count", value="1"),
            record(STEP, "2024-01-01 02:30:00 +0000", "2024-01-01 02:30:00 +0000",
                   sourceName="", unit="count", value="1"),
            record(STEP, "2024-01-01 03:00:00 +0000", "2024-01-01 03:00:00 +0000",
                   sourceName="Secret A", unit="count", value="1"),
            record(STEP, "2024-01-01 04:00:00 +0000", "2024-01-01 04:00:00 +0000",
                   sourceName="Secret B", unit="count", value="1"),
        ))
        report = self.measured(archive, timezone="UTC", as_of="2024-01-02", days="1")
        day = self.by_date(report, "steps")["2024-01-01"]
        self.assertEqual(day["source_counts"], {
            "source-002": 2, "unknown-source": 2, "source-003": 1,
        })
        rendered = json.dumps(report)
        self.assertNotIn("Invalid Secret", rendered)
        self.assertNotIn("Secret A", rendered)
        self.assertNotIn("Secret B", rendered)

    def test_nested_metadata_is_detached_without_losing_record_attributes(self):
        nested = (
            f"  <Record type={quoteattr(STEP)} "
            "startDate=\"2024-01-01 01:00:00 +0000\" "
            "endDate=\"2024-01-01 01:00:00 +0000\" "
            "sourceName=\"Synthetic Source\" unit=\"count\" value=\"1\">\n"
            "    <MetadataEntry key=\"Private Note\" value=\"Private Location\"/>\n"
            "  </Record>"
        )
        report = self.measured(self.make_archive(export_xml(nested)), timezone="UTC",
                               as_of="2024-01-02", days="1")
        day = self.by_date(report, "steps")["2024-01-01"]
        self.assertEqual(day["time_valid_record_count"], 1)
        rendered = json.dumps(report)
        self.assertNotIn("Private Note", rendered)
        self.assertNotIn("Private Location", rendered)

    def test_bad_times_and_zero_length_sleep_are_invalid(self):
        archive = self.make_archive(export_xml(
            record(STEP, None, "2024-01-01 01:00:00 +0000", unit="count", value="1"),
            record(STEP, "2024-01-01 02:00:00", "2024-01-01 03:00:00 +0000",
                   unit="count", value="1"),
            record(STEP, "broken", "2024-01-01 03:00:00 +0000", unit="count", value="1"),
            record(STEP, "2024-01-01 04:00:00 +0000", "2024-01-01 03:00:00 +0000",
                   unit="count", value="1"),
            record(HRV, "2024-01-01 04:00:00 +0000", None, unit="ms", value="1"),
            record(SLEEP, "2024-01-01 05:00:00 +0000", "2024-01-01 05:00:00 +0000",
                   value=ASLEEP),
        ))
        report = self.measured(archive, timezone="UTC", as_of="2024-01-02", days="1")
        self.assertEqual(report["families"]["steps"]["invalid_time_record_count"], 4)
        self.assertEqual(report["families"]["hrv_sdnn"]["invalid_time_record_count"], 1)
        self.assertEqual(report["families"]["sleep_asleep"]["invalid_time_record_count"], 1)
        for family in report["families"].values():
            self.assertEqual(family["records_intersecting_window"], 0)

    def test_quantity_issues_count_once_per_record_per_day_without_filtering_presence(self):
        archive = self.make_archive(export_xml(
            record(STEP, "2024-01-01 01:00:00 +0000", "2024-01-01 01:00:00 +0000",
                   unit="meters", value="1"),
            record(STEP, "2024-01-01 02:00:00 +0000", "2024-01-01 02:00:00 +0000",
                   unit="count"),
            record(STEP, "2024-01-01 03:00:00 +0000", "2024-01-01 03:00:00 +0000",
                   unit="count", value="not-a-number"),
            record(STEP, "2024-01-01 04:00:00 +0000", "2024-01-01 04:00:00 +0000",
                   unit="count", value="NaN"),
            record(STEP, "2024-01-01 05:00:00 +0000", "2024-01-01 05:00:00 +0000",
                   unit="wrong", value="-1"),
            record(HRV, "2024-01-01 06:00:00 +0000", "2024-01-01 06:00:00 +0000",
                   unit="ms", value="2"),
        ))
        report = self.measured(archive, timezone="UTC", as_of="2024-01-02", days="1")
        steps = self.by_date(report, "steps")["2024-01-01"]
        self.assertEqual(steps["time_valid_record_count"], 5)
        self.assertEqual(steps["quantity_issue_count"], 5)
        self.assertEqual(self.by_date(report, "hrv_sdnn")["2024-01-01"]
                         ["quantity_issue_count"], 0)
        self.assertEqual(self.by_date(report, "sleep_asleep")["2024-01-01"]
                         ["quantity_issue_count"], 0)

    def test_empty_dates_have_explicit_zero_and_family_specific_overlap_values(self):
        report = self.measured(self.make_archive(export_xml()), timezone="UTC",
                               as_of="2024-01-03", days="2")
        for name, family in report["families"].items():
            self.assertEqual(family["days_with_records"], 0)
            self.assertEqual(family["no_record_days"], 2)
            for day in family["daily"]:
                self.assertEqual(day["time_valid_record_count"], 0)
                self.assertEqual(day["source_counts"], {})
                self.assertEqual(day["duplicate_candidate_count"], 0)
                self.assertEqual(day["quantity_issue_count"], 0)
                expected = None if name == "hrv_sdnn" else False
                self.assertEqual(day["within_source_overlap"], expected)
                self.assertEqual(day["cross_source_overlap"], expected)

    def test_streaming_parser_memory_does_not_scale_with_whole_export_elements(self):
        def unrelated_export(count):
            return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<HealthData>\n" +
                    "  <Record type=\"UnrelatedSyntheticType\"/>\n" * count +
                    "</HealthData>\n")

        small = self.make_named_archive("small.zip", unrelated_export(5_000))
        large = self.make_named_archive("large.zip", unrelated_export(50_000))
        small_peak = self.measured(small, timezone="UTC", as_of="2024-01-02", days="1")[
            "measurement"]["peak_traced_memory_bytes"]
        large_peak = self.measured(large, timezone="UTC", as_of="2024-01-02", days="1")[
            "measurement"]["peak_traced_memory_bytes"]

        self.assertLess(
            large_peak,
            small_peak * 2,
            f"peak traced memory scaled from {small_peak} to {large_peak} bytes",
        )

    def test_invalid_arguments_fail_without_success_json(self):
        archive = self.make_archive(export_xml())
        cases = [
            {"days": "0"},
            {"days": "True"},
            {"as_of": "2024-1-2"},
            {"as_of": "2024-02-30"},
            {"timezone": "Not/A_Zone"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                completed = self.run_probe(archive, **kwargs)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")

    def test_missing_or_ambiguous_export_member_fails(self):
        missing = self.root / "missing.zip"
        with zipfile.ZipFile(missing, "w") as archive:
            archive.writestr("other.xml", export_xml())
        ambiguous = self.root / "ambiguous.zip"
        with zipfile.ZipFile(ambiguous, "w") as archive:
            archive.writestr("one/export.xml", export_xml())
            archive.writestr("two/export.xml", export_xml())

        for path in (missing, ambiguous):
            with self.subTest(path=path.name):
                completed = self.run_probe(path)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")

    def test_bad_zip_and_malformed_xml_fail_without_partial_success(self):
        bad_zip = self.root / "bad.zip"
        bad_zip.write_bytes(b"not a zip")
        malformed = self.make_archive(
            "<HealthData>" +
            record(STEP, "2024-01-01 01:00:00 +0000", "2024-01-01 01:00:00 +0000",
                   sourceName="Must Not Leak", unit="count", value="99")
        )

        for path in (bad_zip, malformed):
            with self.subTest(path=path.name):
                completed = self.run_probe(path)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn("Must Not Leak", completed.stderr)
                self.assertNotIn('"status": "MEASURED"', completed.stderr)

    def test_unsupported_xml_encoding_fails_without_leaking_parser_details(self):
        encoding_token = "unsupported-synthetic-encoding"
        archive = self.make_archive(
            f'<?xml version="1.0" encoding="{encoding_token}"?><HealthData/>'
        )

        completed = self.run_probe(archive)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "probe failed: export.xml is malformed XML\n")
        self.assertNotIn(encoding_token, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
