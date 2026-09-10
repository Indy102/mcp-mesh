"""Public CLI workflow on invented input; oracle unit fixtures remain separate."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]


class PublicStepsWorkflowTests(unittest.TestCase):
    def test_selected_steps_gaps_and_read_only_rehashed_tamper(self):
        with tempfile.TemporaryDirectory(prefix="steps-interop-") as directory:
            folder = Path(directory)
            archive = folder / "synthetic.zip"
            catalogue = folder / "catalogue.json"
            request = folder / "request.json"
            binding = folder / "binding.json"
            report = folder / "report.json"

            def record(source, start, end, value):
                return (
                    '<Record type="HKQuantityTypeIdentifierStepCount" '
                    f'sourceName="{source}" sourceVersion="1" device="Invented device" '
                    f'startDate="{start}" endDate="{end}" unit="count" value="{value}"/>'
                )

            xml = "<HealthData>" + "".join([
                record("Invented Watch", "2026-01-02T10:00:00Z", "2026-01-02T10:10:00Z", "100"),
                record("Invented Watch", "2026-01-02T10:10:00Z", "2026-01-02T10:20:00Z", "80"),
                record("Invented Watch", "2026-01-03T10:00:00Z", "2026-01-03T10:10:00Z", "100"),
                record("Invented Watch", "2026-01-03T10:05:00Z", "2026-01-03T10:15:00Z", "80"),
                record("Invented Phone", "2026-01-04T10:00:00Z", "2026-01-04T11:00:00Z", "50"),
                record("Invented Watch", "2026-01-05T00:00:00Z", "2026-01-05T00:00:00Z", "-0"),
                # This excluded interval crosses a day boundary; it cannot block the watch.
                record("Invented Phone", "2026-01-01T23:00:00Z", "2026-01-02T01:00:00Z", "900"),
                '<Record type="UnrelatedType" sourceName="Invented Other"/>',
            ]) + "</HealthData>"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("apple_health_export/export.xml", xml)

            def run(script, *args, expected=0):
                outcome = subprocess.run(
                    [sys.executable, str(ROOT / script), *map(str, args)],
                    cwd=ROOT, capture_output=True, text=True, timeout=180,
                )
                self.assertEqual(outcome.returncode, expected, outcome.stdout + outcome.stderr)
                self.assertEqual(outcome.stderr, "")
                for private in (directory, "Invented Watch", "Invented Phone", "Invented device"):
                    self.assertNotIn(private, outcome.stdout)
                result = json.loads(outcome.stdout)
                self.assertEqual(result["status"], "PASS" if expected == 0 else "FAIL")
                return result

            run("spikes/health_catalogue/catalogue.py", "build", "--archive", archive,
                "--output", catalogue)
            envelope = json.loads(catalogue.read_text())
            groups = envelope["catalogue"]["families"]["steps"]
            selected = [group["group_id"] for group in groups
                        if group["descriptor"][0] == ["present", "Invented Watch"]]
            self.assertEqual(len(selected), 1)
            request.write_text(json.dumps({
                "selection_schema": "mesh-health-selection-request-v1",
                "archive_sha256": envelope["catalogue"]["archive_sha256"],
                "catalogue_sha256": envelope["catalogue_sha256"],
                "selections": {"steps": {
                    "selection_handle": "00000000-0000-4000-8000-000000000001",
                    "group_ids": sorted(selected), "source_scope": "app_stream",
                    "user_choice": "Invented Watch for this synthetic test.",
                    "identity_evidence": "Invented archive-local source; no hardware identity claim.",
                }},
            }))
            os.chmod(request, 0o600)
            run("spikes/health_catalogue/catalogue.py", "bind", "--archive", archive,
                "--catalogue", catalogue, "--request", request, "--output", binding)
            originals = {path: path.read_bytes() for path in (archive, catalogue, request, binding)}
            run("spikes/health_steps/evaluate.py", "--archive", archive,
                "--catalogue", catalogue, "--binding", binding, "--timezone", "Etc/UTC",
                "--as-of", "2026-01-06", "--days", "4", "--output", report)
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            result = json.loads(report.read_text())
            days = result["report"]["days"]
            self.assertEqual([row["date"] for row in days],
                             ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"])
            self.assertEqual([row["calculation_state"] for row in days],
                             ["computed", "blocked", "no_records", "computed"])
            self.assertEqual([row["value"] for row in days], [180.0, None, None, 0.0])
            self.assertEqual([row["reasons"] for row in days],
                             [[], ["selected_quantity_overlap"], [], []])
            self.assertEqual([row["counts"]["selected"]["occurrences"] for row in days],
                             [2, 2, 0, 1])
            for row in days:
                self.assertEqual(row["observation_completeness"], "unknown")
                self.assertIsNone(row["valid_metric_day"])
                self.assertIs(row["inference_eligible"], False)
            verification = run("spikes/health_steps/verify_steps.py", "--archive", archive,
                               "--catalogue", catalogue, "--binding", binding, "--report", report)
            self.assertEqual(verification["diagnostics"], [])
            self.assertEqual(verification["verification_scope"],
                             "selected_steps_report_exact_recomputation")

            result["report"]["days"][0]["value"] = 181.0
            result["report_sha256"] = hashlib.sha256(json.dumps(
                result["report"], ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("ascii")).hexdigest()
            report.write_text(json.dumps(result))
            tampered = report.read_bytes()
            run("spikes/health_steps/verify_steps.py", "--archive", archive,
                "--catalogue", catalogue, "--binding", binding, "--report", report, expected=2)
            self.assertEqual(report.read_bytes(), tampered)
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
