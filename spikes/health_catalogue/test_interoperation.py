"""Public-command interoperation; the independent verifier's tests remain separate."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


HERE = Path(__file__).resolve().parent


class PublicWorkflowTests(unittest.TestCase):
    def test_build_bind_verify_and_read_only_tamper_refusal(self):
        with tempfile.TemporaryDirectory(prefix="health-catalogue-interop-") as directory:
            folder = Path(directory)
            archive = folder / "synthetic.zip"
            catalogue_path = folder / "catalogue.json"
            request_path = folder / "request.json"
            binding_path = folder / "binding.json"
            private_value = "SYNTHETIC_VALUE_MUST_NOT_BE_CATALOGUED"
            xml = f'''<HealthData>
<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Synthetic Watch" sourceVersion="1" device="Synthetic device" value="{private_value}"/>
<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Synthetic Watch" sourceVersion="1" device="Synthetic device"/>
<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Synthetic Phone"/>
<Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" sourceName="Synthetic Watch"/>
<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Watch" value="UnknownCategory"/>
<Record type="UnrelatedType" sourceName="Unrelated Source"/>
</HealthData>'''
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("apple_health_export/export.xml", xml)
            archive_before = archive.read_bytes()

            def run(script, *arguments, expected=0):
                result = subprocess.run(
                    [sys.executable, str(HERE / script), *map(str, arguments)],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                for secret in (private_value, "Synthetic Watch", "Synthetic device", directory):
                    self.assertNotIn(secret, result.stdout + result.stderr)
                response = json.loads(result.stdout)
                self.assertEqual(response["status"], "PASS" if expected == 0 else "FAIL")
                return response

            run("catalogue.py", "build", "--archive", archive, "--output", catalogue_path)
            self.assertEqual(catalogue_path.stat().st_mode & 0o777, 0o600)
            catalogue_bytes = catalogue_path.read_bytes()
            self.assertNotIn(private_value.encode(), catalogue_bytes)
            envelope = json.loads(catalogue_bytes)
            catalogue = envelope["catalogue"]
            self.assertEqual(catalogue["record_count_all_types"], 6)
            families = catalogue["families"]
            self.assertEqual({f: sum(g["record_count"] for g in groups)
                              for f, groups in families.items()},
                             {"steps": 3, "hrv_sdnn": 1, "sleep": 1})
            run("verify_catalogue.py", "--archive", archive, "--catalogue", catalogue_path)

            request = {
                "selection_schema": "mesh-health-selection-request-v1",
                "archive_sha256": catalogue["archive_sha256"],
                "catalogue_sha256": envelope["catalogue_sha256"],
                "selections": {},
            }
            for i, family in enumerate(("steps", "hrv_sdnn", "sleep"), 1):
                chosen = [g["group_id"] for g in families[family]
                          if g["descriptor"][0] == ["present", "Synthetic Watch"]]
                self.assertEqual(len(chosen), 1)
                request["selections"][family] = {
                    "selection_handle": f"00000000-0000-4000-8000-{i:012d}",
                    "group_ids": sorted(chosen),
                    "source_scope": "app_stream",
                    "user_choice": "Explicit synthetic selection for this test.",
                    "identity_evidence": "Invented source descriptors; no real identity asserted.",
                }
            request_path.write_text(json.dumps(request), encoding="utf-8")
            request_before = request_path.read_bytes()
            run("catalogue.py", "bind", "--archive", archive, "--catalogue", catalogue_path,
                "--request", request_path, "--output", binding_path)
            self.assertEqual(binding_path.stat().st_mode & 0o777, 0o600)
            binding_before = binding_path.read_bytes()
            binding = json.loads(binding_before)["binding"]
            for field in ("device_identity_verified", "user_intent_verified",
                          "health_source_priority_verified"):
                self.assertIs(binding[field], False)
            run("verify_catalogue.py", "--archive", archive, "--catalogue", catalogue_path,
                "--binding", binding_path)

            # Rehash the dishonest content: checking only its self-digest must not pass.
            envelope["catalogue"]["families"]["steps"][0]["record_count"] += 1
            envelope["catalogue_sha256"] = hashlib.sha256(json.dumps(
                envelope["catalogue"], ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("ascii")).hexdigest()
            catalogue_path.write_text(json.dumps(envelope), encoding="utf-8")
            tampered_before = catalogue_path.read_bytes()
            run("verify_catalogue.py", "--archive", archive, "--catalogue", catalogue_path,
                expected=2)
            refused_output = folder / "refused-binding.json"
            run("catalogue.py", "bind", "--archive", archive, "--catalogue", catalogue_path,
                "--request", request_path, "--output", refused_output, expected=2)
            self.assertFalse(refused_output.exists())
            self.assertEqual(catalogue_path.read_bytes(), tampered_before)
            self.assertEqual(archive.read_bytes(), archive_before)
            self.assertEqual(request_path.read_bytes(), request_before)
            self.assertEqual(binding_path.read_bytes(), binding_before)


if __name__ == "__main__":
    unittest.main()
