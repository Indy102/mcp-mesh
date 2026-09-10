from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import verify_catalogue


SCRIPT = Path(__file__).with_name("verify_catalogue.py")
FAMILIES = ("steps", "hrv_sdnn", "sleep")


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def group(family: str, descriptor: list[list[str]], count: int) -> dict[str, object]:
    return {
        "group_id": digest({"family": family, "descriptor": descriptor}),
        "descriptor": descriptor,
        "record_count": count,
    }


def envelope(content: dict[str, object]) -> dict[str, object]:
    return {"catalogue": content, "catalogue_sha256": digest(content)}


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="health-verifier-")
        self.root = Path(self.temporary.name)
        self.archive = self.root / "private-secret-name.zip"
        self.catalogue_path = self.root / "catalogue.json"
        self.binding_path = self.root / "binding.json"
        self.xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<HealthData>"
            '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Fit" device=""/>'
            '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Fit" device="" value="999"/>'
            '<Record type="HKQuantityTypeIdentifierStepCount" sourceVersion="1"/>'
            '<Record type="HKQuantityTypeIdentifierStepCount" sourceName=""/>'
            '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="fit"/>'
            '<Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" '
            'sourceName="Δ Watch" sourceVersion=" 1 " device="腕"/>'
            '<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Bed" value="unknown"/>'
            '<Record type="HKQuantityTypeIdentifierBodyMass" sourceName="ignored"/>'
            "</HealthData>"
        ).encode("utf-8")
        self._write_zip(self.archive, [("apple_health_export/export.xml", self.xml)])
        self.content = self._expected_content(self.archive)
        self.catalogue = envelope(self.content)
        self._write_json(self.catalogue_path, self.catalogue)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_zip(self, path: Path, members: list[tuple[str, bytes]]) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in members:
                archive.writestr(name, data)

    def _write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def _expected_content(self, archive: Path) -> dict[str, object]:
        missing = ["missing"]
        groups = {
            "steps": [
                group("steps", [["present", "Fit"], missing, ["present", ""]], 2),
                group("steps", [missing, ["present", "1"], missing], 1),
                group("steps", [["present", ""], missing, missing], 1),
                group("steps", [["present", "fit"], missing, missing], 1),
            ],
            "hrv_sdnn": [
                group(
                    "hrv_sdnn",
                    [["present", "Δ Watch"], ["present", " 1 "], ["present", "腕"]],
                    1,
                )
            ],
            "sleep": [group("sleep", [["present", "Bed"], missing, missing], 1)],
        }
        for family in FAMILIES:
            groups[family].sort(key=lambda item: canonical(item["descriptor"]))
        raw = archive.read_bytes()
        return {
            "catalogue_schema": "mesh-health-catalogue-v1",
            "archive_sha256": hashlib.sha256(raw).hexdigest(),
            "archive_bytes": len(raw),
            "xml_member": "apple_health_export/export.xml",
            "record_count_all_types": 8,
            "families": groups,
        }

    def _binding(self) -> dict[str, object]:
        steps_group = next(
            item
            for item in self.content["families"]["steps"]
            if item["descriptor"][0] == ["present", "Fit"]
        )
        hrv_group = self.content["families"]["hrv_sdnn"][0]
        request = {
            "selection_schema": "mesh-health-selection-request-v1",
            "archive_sha256": self.content["archive_sha256"],
            "catalogue_sha256": self.catalogue["catalogue_sha256"],
            "selections": {
                "steps": {
                    "selection_handle": "00000000-0000-4000-8000-000000000001",
                    "group_ids": [steps_group["group_id"]],
                    "source_scope": "app_stream",
                    "user_choice": "Bogus assertion is retained, not proven",
                    "identity_evidence": "A typed sentence cannot prove identity",
                },
                "hrv_sdnn": {
                    "selection_handle": "00000000-0000-4000-8000-000000000002",
                    "group_ids": [hrv_group["group_id"]],
                    "source_scope": "device",
                    "user_choice": "Use the watch stream",
                    "identity_evidence": "Local exported label",
                },
            },
        }
        binding = {
            "binding_schema": "mesh-health-selection-binding-v1",
            "catalogue": copy.deepcopy(self.content),
            "catalogue_sha256": self.catalogue["catalogue_sha256"],
            "selection_request": request,
            "validation_scope": "archive_integrity_and_selection_membership",
            "device_identity_verified": False,
            "user_intent_verified": False,
            "health_source_priority_verified": False,
        }
        return {"binding": binding, "binding_sha256": digest(binding)}

    def _run(
        self,
        *,
        archive: Path | None = None,
        catalogue: Path | None = None,
        binding: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--archive",
            str(archive or self.archive),
            "--catalogue",
            str(catalogue or self.catalogue_path),
        ]
        if binding is not None:
            command.extend(("--binding", str(binding)))
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def _assert_pass(self, result: subprocess.CompletedProcess[str], binding: bool = False) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(
            payload["verification_scope"],
            "archive_catalogue_and_optional_binding_membership",
        )
        self.assertEqual(
            payload["checks"],
            [
                "archive_integrity",
                "catalogue_structure_and_digest",
                "catalogue_exact_recomputation",
            ]
            + (["binding_structure_and_membership"] if binding else []),
        )
        return payload

    def _assert_fail(
        self, result: subprocess.CompletedProcess[str], code: str | None = None
    ) -> dict[str, object]:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(len(payload["diagnostics"]), 1)
        if code is not None:
            self.assertEqual(payload["diagnostics"], [code])
        return payload

    def _rehash_catalogue(self, changed: dict[str, object]) -> None:
        changed["catalogue_sha256"] = digest(changed["catalogue"])
        self._write_json(self.catalogue_path, changed)

    def _write_binding(self, value: dict[str, object]) -> None:
        self._write_json(self.binding_path, value)

    def _rehash_binding(self, value: dict[str, object]) -> None:
        value["binding_sha256"] = digest(value["binding"])
        self._write_binding(value)

    def test_valid_catalogue_covers_exact_grouping_and_counts(self) -> None:
        self._assert_pass(self._run())
        self.assertEqual(self.content["record_count_all_types"], 8)
        steps = self.content["families"]["steps"]
        self.assertEqual(sum(item["record_count"] for item in steps), 5)
        self.assertEqual(len(steps), 4)
        self.assertEqual(
            [item["descriptor"] for item in steps],
            sorted((item["descriptor"] for item in steps), key=canonical),
        )

    def test_record_order_does_not_change_grouping(self) -> None:
        reordered = self.root / "reordered.zip"
        records = re.findall(r"<Record[^>]*/>", self.xml.decode("utf-8"))
        reordered_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>'
            + "".join(reversed(records))
            + "</HealthData>"
        ).encode("utf-8")
        self._write_zip(reordered, [("apple_health_export/export.xml", reordered_xml)])
        recomputed = verify_catalogue._recompute_catalogue(reordered)
        self.assertEqual(recomputed["families"], self.content["families"])
        self.assertEqual(recomputed["record_count_all_types"], 8)

    def test_repeat_runs_are_identical_and_inputs_remain_read_only(self) -> None:
        before_archive = self.archive.read_bytes()
        before_catalogue = self.catalogue_path.read_bytes()
        self.archive.chmod(0o400)
        self.catalogue_path.chmod(0o400)
        first = self._run()
        second = self._run()
        self._assert_pass(first)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(self.archive.read_bytes(), before_archive)
        self.assertEqual(self.catalogue_path.read_bytes(), before_catalogue)
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(self.catalogue_path.stat().st_mode), 0o400)

    def test_internal_dtd_is_allowed(self) -> None:
        archive = self.root / "internal-dtd.zip"
        xml = (
            b'<!DOCTYPE HealthData [<!ELEMENT HealthData ANY><!ELEMENT Record ANY>]>'
            b'<HealthData><Record type="HKQuantityTypeIdentifierStepCount" sourceName="A"/></HealthData>'
        )
        self._write_zip(archive, [("export.xml", xml)])
        raw = archive.read_bytes()
        descriptor = [["present", "A"], ["missing"], ["missing"]]
        content = {
            "catalogue_schema": "mesh-health-catalogue-v1",
            "archive_sha256": hashlib.sha256(raw).hexdigest(),
            "archive_bytes": len(raw),
            "xml_member": "export.xml",
            "record_count_all_types": 1,
            "families": {
                "steps": [group("steps", descriptor, 1)],
                "hrv_sdnn": [],
                "sleep": [],
            },
        }
        self._write_json(self.catalogue_path, envelope(content))
        self._assert_pass(self._run(archive=archive))

    def test_ambiguous_and_missing_export_members_are_rejected(self) -> None:
        for names in (("other.xml",), ("one/export.xml", "two/export.xml")):
            with self.subTest(names=names):
                archive = self.root / (str(len(names)) + "-members.zip")
                self._write_zip(archive, [(name, b"<HealthData/>") for name in names])
                self._assert_fail(
                    self._run(archive=archive), "ARCHIVE_XML_MEMBER_INVALID"
                )

    def test_malformed_wrong_root_and_unsupported_encoding_are_rejected(self) -> None:
        cases = (
            b"<HealthData><Record></HealthData>",
            b"<NotHealthData/>",
            b'<?xml version="1.0" encoding="X-UNKNOWN"?><HealthData/>',
        )
        for index, xml in enumerate(cases):
            with self.subTest(index=index):
                archive = self.root / f"bad-{index}.zip"
                self._write_zip(archive, [("export.xml", xml)])
                self._assert_fail(self._run(archive=archive), "XML_INVALID")

    def test_external_doctype_and_entity_are_refused(self) -> None:
        cases = (
            b'<!DOCTYPE HealthData SYSTEM "file:///private/secret.dtd"><HealthData/>',
            b'<!DOCTYPE HealthData [<!ENTITY leak SYSTEM "file:///private/secret">]><HealthData>&leak;</HealthData>',
        )
        for index, xml in enumerate(cases):
            with self.subTest(index=index):
                archive = self.root / f"external-{index}.zip"
                self._write_zip(archive, [("export.xml", xml)])
                result = self._run(archive=archive)
                self._assert_fail(result, "XML_EXTERNAL_RESOURCE")
                self.assertNotIn("secret", result.stdout + result.stderr)
                self.assertNotIn("file:", result.stdout + result.stderr)

    def test_json_duplicate_keys_and_nonfinite_values_are_rejected(self) -> None:
        unsafe_documents = (
            '{"catalogue":{},"catalogue":{},"catalogue_sha256":"x"}',
            '{"catalogue":NaN,"catalogue_sha256":"x"}',
            '{"catalogue":Infinity,"catalogue_sha256":"x"}',
            '{"catalogue":-Infinity,"catalogue_sha256":"x"}',
        )
        for document in unsafe_documents:
            with self.subTest(document=document):
                self.catalogue_path.write_text(document, encoding="utf-8")
                self._assert_fail(self._run(), "JSON_INVALID")

    def test_unknown_fields_are_rejected_at_catalogue_levels(self) -> None:
        mutations = []
        top = copy.deepcopy(self.catalogue)
        top["extra"] = True
        mutations.append(top)
        content = copy.deepcopy(self.catalogue)
        content["catalogue"]["extra"] = True
        mutations.append(content)
        families = copy.deepcopy(self.catalogue)
        families["catalogue"]["families"]["extra"] = []
        mutations.append(families)
        group_level = copy.deepcopy(self.catalogue)
        group_level["catalogue"]["families"]["steps"][0]["extra"] = True
        mutations.append(group_level)
        for changed in mutations:
            with self.subTest(level=len(json.dumps(changed))):
                self._rehash_catalogue(changed)
                self._assert_fail(self._run(), "CATALOGUE_STRUCTURE_INVALID")

    def test_changed_count_descriptor_order_member_and_digest_are_rejected(self) -> None:
        mutations: list[tuple[str, dict[str, object], bool]] = []
        count = copy.deepcopy(self.catalogue)
        count["catalogue"]["families"]["steps"][0]["record_count"] += 1
        mutations.append(("count", count, True))
        descriptor = copy.deepcopy(self.catalogue)
        selected = descriptor["catalogue"]["families"]["steps"][0]
        selected["descriptor"][0] = ["present", "Changed"]
        selected["group_id"] = digest(
            {"family": "steps", "descriptor": selected["descriptor"]}
        )
        descriptor["catalogue"]["families"]["steps"].sort(
            key=lambda item: canonical(item["descriptor"])
        )
        mutations.append(("descriptor", descriptor, True))
        order = copy.deepcopy(self.catalogue)
        order["catalogue"]["families"]["steps"][0:2] = reversed(
            order["catalogue"]["families"]["steps"][0:2]
        )
        mutations.append(("order", order, True))
        member = copy.deepcopy(self.catalogue)
        member["catalogue"]["xml_member"] = "export.xml"
        mutations.append(("member", member, True))
        bad_digest = copy.deepcopy(self.catalogue)
        bad_digest["catalogue_sha256"] = "0" * 64
        mutations.append(("digest", bad_digest, False))
        for label, changed, rehash in mutations:
            with self.subTest(label=label):
                if rehash:
                    self._rehash_catalogue(changed)
                else:
                    self._write_json(self.catalogue_path, changed)
                self._assert_fail(self._run())

    def test_wrong_archive_is_rejected(self) -> None:
        wrong = self.root / "wrong.zip"
        self._write_zip(wrong, [("export.xml", b"<HealthData/>")])
        self._assert_fail(self._run(archive=wrong), "CATALOGUE_MISMATCH")

    def test_counts_require_actual_integers(self) -> None:
        for field, value in (
            ("archive_bytes", True),
            ("archive_bytes", float(self.content["archive_bytes"])),
            ("record_count_all_types", True),
            ("record_count_all_types", 8.0),
        ):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.catalogue)
                changed["catalogue"][field] = value
                self._rehash_catalogue(changed)
                self._assert_fail(self._run(), "CATALOGUE_STRUCTURE_INVALID")
        for value in (True, 2.0):
            with self.subTest(group_count=value):
                changed = copy.deepcopy(self.catalogue)
                changed["catalogue"]["families"]["steps"][0]["record_count"] = value
                self._rehash_catalogue(changed)
                self._assert_fail(self._run(), "CATALOGUE_STRUCTURE_INVALID")

    def test_failure_does_not_repair_or_rewrite_inputs(self) -> None:
        changed = copy.deepcopy(self.catalogue)
        changed["catalogue"]["record_count_all_types"] = 9
        self._rehash_catalogue(changed)
        before_archive = self.archive.read_bytes()
        before_catalogue = self.catalogue_path.read_bytes()
        self._assert_fail(self._run(), "CATALOGUE_MISMATCH")
        self.assertEqual(self.archive.read_bytes(), before_archive)
        self.assertEqual(self.catalogue_path.read_bytes(), before_catalogue)

    def test_observable_archive_change_is_rejected(self) -> None:
        real_digest = verify_catalogue._digest_open_file
        calls = 0

        def changed_digest(stream: object) -> tuple[str, int]:
            nonlocal calls
            calls += 1
            value = real_digest(stream)
            if calls == 2:
                return ("0" * 64, value[1])
            return value

        output = io.StringIO()
        with mock.patch.object(verify_catalogue, "_digest_open_file", changed_digest):
            with contextlib.redirect_stdout(output):
                code = verify_catalogue.main(
                    ["--archive", str(self.archive), "--catalogue", str(self.catalogue_path)]
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["diagnostics"], ["ARCHIVE_CHANGED"])

    def test_valid_binding_preserves_catalogue_and_limits_attestations(self) -> None:
        binding = self._binding()
        self._write_binding(binding)
        self._assert_pass(self._run(binding=self.binding_path), binding=True)
        stored = json.loads(self.binding_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["binding"]["catalogue"], self.content)
        self.assertNotIn("sleep", stored["binding"]["selection_request"]["selections"])
        self.assertEqual(
            stored["binding"]["selection_request"]["selections"]["steps"]["user_choice"],
            "Bogus assertion is retained, not proven",
        )
        self.assertIs(stored["binding"]["device_identity_verified"], False)
        self.assertIs(stored["binding"]["user_intent_verified"], False)
        self.assertIs(stored["binding"]["health_source_priority_verified"], False)

    def test_binding_wrong_unknown_duplicate_and_cross_family_groups_are_rejected(self) -> None:
        valid = self._binding()
        steps = valid["binding"]["selection_request"]["selections"]["steps"]
        hrv_id = valid["binding"]["selection_request"]["selections"]["hrv_sdnn"]["group_ids"][0]
        cases = {
            "unknown": ["0" * 64],
            "duplicate": [steps["group_ids"][0], steps["group_ids"][0]],
            "cross-family": [hrv_id],
        }
        for label, ids in cases.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(valid)
                changed["binding"]["selection_request"]["selections"]["steps"]["group_ids"] = ids
                self._rehash_binding(changed)
                self._assert_fail(self._run(binding=self.binding_path))

    def test_binding_empty_selections_and_empty_group_list_are_rejected(self) -> None:
        for target in ("selections", "group_ids"):
            with self.subTest(target=target):
                changed = self._binding()
                selections = changed["binding"]["selection_request"]["selections"]
                if target == "selections":
                    selections.clear()
                else:
                    selections["steps"]["group_ids"] = []
                self._rehash_binding(changed)
                self._assert_fail(
                    self._run(binding=self.binding_path), "BINDING_REQUEST_INVALID"
                )

    def test_binding_rejects_selection_of_missing_or_blank_source_name(self) -> None:
        invalid_groups = [
            item
            for item in self.content["families"]["steps"]
            if item["descriptor"][0] in (["missing"], ["present", ""])
        ]
        for item in invalid_groups:
            with self.subTest(descriptor=item["descriptor"]):
                changed = self._binding()
                changed["binding"]["selection_request"]["selections"]["steps"]["group_ids"] = [item["group_id"]]
                self._rehash_binding(changed)
                self._assert_fail(
                    self._run(binding=self.binding_path), "BINDING_MEMBERSHIP_INVALID"
                )

    def test_binding_flags_must_be_actual_false_booleans(self) -> None:
        for flag in (
            "device_identity_verified",
            "user_intent_verified",
            "health_source_priority_verified",
        ):
            for value in (True, 0, 0.0, "false"):
                with self.subTest(flag=flag, value=value):
                    changed = self._binding()
                    changed["binding"][flag] = value
                    self._rehash_binding(changed)
                    self._assert_fail(
                        self._run(binding=self.binding_path),
                        "BINDING_ATTESTATION_INVALID",
                    )

    def test_binding_scope_extra_fields_stale_identity_and_digest_are_rejected(self) -> None:
        cases: list[tuple[str, dict[str, object], str, bool]] = []
        scope = self._binding()
        scope["binding"]["validation_scope"] = "broader_scope"
        cases.append(("scope", scope, "BINDING_SCOPE_INVALID", True))
        extra = self._binding()
        extra["binding"]["extra"] = True
        cases.append(("extra", extra, "BINDING_STRUCTURE_INVALID", True))
        request_extra = self._binding()
        request_extra["binding"]["selection_request"]["extra"] = True
        cases.append(("request-extra", request_extra, "BINDING_REQUEST_INVALID", True))
        selection_extra = self._binding()
        selection_extra["binding"]["selection_request"]["selections"]["steps"]["extra"] = True
        cases.append(("selection-extra", selection_extra, "BINDING_REQUEST_INVALID", True))
        stale_archive = self._binding()
        stale_archive["binding"]["selection_request"]["archive_sha256"] = "0" * 64
        cases.append(("stale-archive", stale_archive, "BINDING_IDENTITY_MISMATCH", True))
        stale_catalogue = self._binding()
        stale_catalogue["binding"]["selection_request"]["catalogue_sha256"] = "0" * 64
        cases.append(("stale-catalogue", stale_catalogue, "BINDING_IDENTITY_MISMATCH", True))
        bad_digest = self._binding()
        bad_digest["binding_sha256"] = "0" * 64
        cases.append(("digest", bad_digest, "BINDING_DIGEST_MISMATCH", False))
        for label, changed, code, rehash in cases:
            with self.subTest(label=label):
                if rehash:
                    self._rehash_binding(changed)
                else:
                    self._write_binding(changed)
                self._assert_fail(self._run(binding=self.binding_path), code)

    def test_duplicate_handles_unsorted_groups_bad_uuid_and_blank_assertions_are_rejected(self) -> None:
        base = self._binding()
        valid_step_id = base["binding"]["selection_request"]["selections"]["steps"]["group_ids"][0]
        other_step_id = next(
            item["group_id"]
            for item in self.content["families"]["steps"]
            if item["descriptor"][0] == ["present", "fit"]
        )
        cases = []
        duplicate_handle = copy.deepcopy(base)
        selections = duplicate_handle["binding"]["selection_request"]["selections"]
        selections["hrv_sdnn"]["selection_handle"] = selections["steps"]["selection_handle"]
        cases.append(duplicate_handle)
        unsorted = copy.deepcopy(base)
        unsorted["binding"]["selection_request"]["selections"]["steps"]["group_ids"] = sorted(
            [valid_step_id, other_step_id], reverse=True
        )
        cases.append(unsorted)
        bad_uuid = copy.deepcopy(base)
        bad_uuid["binding"]["selection_request"]["selections"]["steps"]["selection_handle"] = "00000000-0000-1000-8000-000000000001"
        cases.append(bad_uuid)
        blank_choice = copy.deepcopy(base)
        blank_choice["binding"]["selection_request"]["selections"]["steps"]["user_choice"] = " \t"
        cases.append(blank_choice)
        blank_evidence = copy.deepcopy(base)
        blank_evidence["binding"]["selection_request"]["selections"]["steps"]["identity_evidence"] = "\n"
        cases.append(blank_evidence)
        wrong_scope_type = copy.deepcopy(base)
        wrong_scope_type["binding"]["selection_request"]["selections"]["steps"]["source_scope"] = ["device"]
        cases.append(wrong_scope_type)
        for index, changed in enumerate(cases):
            with self.subTest(index=index):
                self._rehash_binding(changed)
                self._assert_fail(
                    self._run(binding=self.binding_path), "BINDING_REQUEST_INVALID"
                )

    def test_failure_diagnostics_do_not_leak_paths_source_metadata_or_parser_errors(self) -> None:
        secret_source = "PRIVATE_SOURCE_9426"
        secret_path = self.root / "PRIVATE_PATH_7351.zip"
        broken_xml = f'<HealthData><Record sourceName="{secret_source}">'.encode()
        self._write_zip(secret_path, [("export.xml", broken_xml)])
        result = self._run(archive=secret_path)
        payload = self._assert_fail(result, "XML_INVALID")
        rendered = result.stdout + result.stderr
        self.assertNotIn(secret_source, rendered)
        self.assertNotIn(secret_path.name, rendered)
        self.assertNotIn("mismatched", rendered.lower())
        self.assertEqual(
            set(payload), {"status", "checks", "diagnostics", "verification_scope"}
        )

    def test_invalid_arguments_have_fixed_private_diagnostic(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--unknown", "PRIVATE_ARGUMENT"],
            text=True,
            capture_output=True,
            check=False,
        )
        self._assert_fail(result, "ARGUMENTS_INVALID")
        self.assertNotIn("PRIVATE_ARGUMENT", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
