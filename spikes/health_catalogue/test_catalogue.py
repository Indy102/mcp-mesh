import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr


TOOL = Path(__file__).with_name("catalogue.py")
CHECKOUT = Path(__file__).resolve().parents[2]
STEP = "HKQuantityTypeIdentifierStepCount"
HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
FAMILIES = ("steps", "hrv_sdnn", "sleep")

SUBJECT_SPEC = importlib.util.spec_from_file_location("health_catalogue_subject", TOOL)
SUBJECT = importlib.util.module_from_spec(SUBJECT_SPEC)
SUBJECT_SPEC.loader.exec_module(SUBJECT)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def group_id(family, descriptor):
    return hashlib.sha256(canonical({"family": family, "descriptor": descriptor})).hexdigest()


def record(record_type, **attributes):
    values = {"type": record_type, **attributes}
    encoded = " ".join(f"{key}={quoteattr(str(value))}" for key, value in values.items())
    return f"  <Record {encoded}/>"


def export_xml(*records):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<HealthData>\n"
        + "\n".join(records)
        + "\n</HealthData>\n"
    )


class CatalogueCommandTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        self.assertNotEqual(self.root, CHECKOUT)
        self.assertNotIn(CHECKOUT, self.root.parents)

    def tearDown(self):
        self.tempdir.cleanup()

    def make_archive(self, xml, *, name="health.zip", member="apple_health_export/export.xml",
                     extra_members=(), compression=zipfile.ZIP_DEFLATED):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            archive.writestr(member, xml)
            for extra_name, contents in extra_members:
                archive.writestr(extra_name, contents)
        return path

    def corrupt_compressed_member(self, path):
        contents, compressed_start, info = self.compressed_member_details(path)
        contents[compressed_start] = 0x07  # final block with reserved DEFLATE type
        path.write_bytes(contents)

    def compressed_member_details(self, path):
        with zipfile.ZipFile(path, "r") as archive:
            info = archive.getinfo("apple_health_export/export.xml")
        contents = bytearray(path.read_bytes())
        header = info.header_offset
        filename_size = int.from_bytes(contents[header + 26:header + 28], "little")
        extra_size = int.from_bytes(contents[header + 28:header + 30], "little")
        compressed_start = header + 30 + filename_size + extra_size
        self.assertGreater(info.compress_size, 0)
        return contents, compressed_start, info

    def run_tool(self, *arguments):
        return subprocess.run(
            [sys.executable, str(TOOL), *map(str, arguments)],
            text=True,
            capture_output=True,
            timeout=15,
        )

    def assert_pass(self, completed, operation):
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {"operation": operation, "status": "PASS"},
        )

    def assert_safe_failure(self, completed, operation, *, forbidden=()):
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        message = json.loads(completed.stdout)
        self.assertEqual(message["operation"], operation)
        self.assertEqual(message["status"], "FAIL")
        self.assertRegex(message["error"], r"^[A-Z_]+$")
        rendered = completed.stdout + completed.stderr
        self.assertNotIn("Traceback", rendered)
        for token in forbidden:
            self.assertNotIn(str(token), rendered)
        return message["error"]

    def build(self, archive, output=None):
        output = output or self.root / "catalogue.json"
        completed = self.run_tool("build", "--archive", archive, "--output", output)
        return completed, output

    def build_catalogue(self, archive, output=None):
        completed, output = self.build(archive, output)
        self.assert_pass(completed, "build")
        return json.loads(output.read_text(encoding="ascii")), output

    def write_request(self, envelope, selections, *, name="request.json"):
        request = {
            "selection_schema": "mesh-health-selection-request-v1",
            "archive_sha256": envelope["catalogue"]["archive_sha256"],
            "catalogue_sha256": envelope["catalogue_sha256"],
            "selections": selections,
        }
        path = self.root / name
        path.write_bytes(canonical(request))
        return request, path

    @staticmethod
    def selection(group_ids, *, handle="00000000-0000-4000-8000-000000000001",
                  scope="app_stream", choice="Explicit synthetic choice",
                  evidence="Synthetic metadata reviewed locally"):
        return {
            "selection_handle": handle,
            "group_ids": group_ids,
            "source_scope": scope,
            "user_choice": choice,
            "identity_evidence": evidence,
        }

    def test_build_exact_catalogue_membership_counts_and_determinism(self):
        missing = [["missing"], ["missing"], ["missing"]]
        empty = [["present", ""], ["present", ""], ["present", ""]]
        alpha = [["present", "Alpha"], ["present", "1.0"], ["present", "Phone A"]]
        unicode_case = [["present", "Wätch"], ["present", "V2"], ["present", "DEVICE"]]
        lower_case = [["present", "wätch"], ["present", "V2"], ["present", "device"]]
        spaced = [["present", " Wätch "], ["present", " V2 "], ["present", " DEVICE "]]
        records = (
            record(STEP, sourceName="Alpha", sourceVersion="1.0", device="Phone A",
                   value="SENTINEL_RAW_VALUE"),
            record(STEP, sourceName="Alpha", sourceVersion="1.0", device="Phone A"),
            record(STEP),
            record(STEP, sourceName="", sourceVersion="", device=""),
            record(HRV, sourceName="Wätch", sourceVersion="V2", device="DEVICE"),
            record(HRV, sourceName="wätch", sourceVersion="V2", device="device"),
            record(HRV, sourceName=" Wätch ", sourceVersion=" V2 ", device=" DEVICE "),
            record(SLEEP, sourceName="Alpha", sourceVersion="1.0", device="Phone A",
                   value="unknown synthetic sleep category"),
            record("HKQuantityTypeIdentifierUnrelated", sourceName="Ignored"),
        )
        archive = self.make_archive(export_xml(*records))
        archive_before = archive.read_bytes()
        envelope, output = self.build_catalogue(archive)

        self.assertEqual(archive.read_bytes(), archive_before)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(set(envelope), {"catalogue", "catalogue_sha256"})
        catalogue = envelope["catalogue"]
        self.assertEqual(catalogue["catalogue_schema"], "mesh-health-catalogue-v1")
        self.assertEqual(catalogue["archive_sha256"], hashlib.sha256(archive_before).hexdigest())
        self.assertEqual(catalogue["archive_bytes"], len(archive_before))
        self.assertEqual(catalogue["xml_member"], "apple_health_export/export.xml")
        self.assertEqual(catalogue["record_count_all_types"], 9)
        self.assertEqual(set(catalogue["families"]), set(FAMILIES))

        expected = {
            "steps": {tuple(map(tuple, alpha)): 2, tuple(map(tuple, missing)): 1,
                      tuple(map(tuple, empty)): 1},
            "hrv_sdnn": {
                tuple(map(tuple, unicode_case)): 1,
                tuple(map(tuple, lower_case)): 1,
                tuple(map(tuple, spaced)): 1,
            },
            "sleep": {tuple(map(tuple, alpha)): 1},
        }
        for family in FAMILIES:
            groups = catalogue["families"][family]
            self.assertEqual(
                [canonical(group["descriptor"]) for group in groups],
                sorted(canonical(group["descriptor"]) for group in groups),
            )
            actual = {tuple(map(tuple, group["descriptor"])): group["record_count"]
                      for group in groups}
            self.assertEqual(actual, expected[family])
            for group in groups:
                self.assertEqual(group["group_id"], group_id(family, group["descriptor"]))
        self.assertEqual(
            envelope["catalogue_sha256"], hashlib.sha256(canonical(catalogue)).hexdigest()
        )
        self.assertNotIn("SENTINEL_RAW_VALUE", output.read_text(encoding="ascii"))
        self.assertNotIn("unknown synthetic sleep category", output.read_text(encoding="ascii"))

        second = self.root / "catalogue-second.json"
        second_envelope, _ = self.build_catalogue(archive, second)
        self.assertEqual(envelope, second_envelope)
        self.assertEqual(output.read_bytes(), second.read_bytes())

    def test_grouping_is_independent_of_record_order(self):
        first = record(STEP, sourceName="B")
        second = record(STEP, sourceName="A")
        left = self.make_archive(export_xml(first, second, first), name="left.zip")
        right = self.make_archive(export_xml(second, first, first), name="right.zip")
        left_catalogue, _ = self.build_catalogue(left, self.root / "left.json")
        right_catalogue, _ = self.build_catalogue(right, self.root / "right.json")
        self.assertEqual(
            left_catalogue["catalogue"]["families"],
            right_catalogue["catalogue"]["families"],
        )
        self.assertEqual(left_catalogue["catalogue"]["record_count_all_types"], 3)
        self.assertEqual(right_catalogue["catalogue"]["record_count_all_types"], 3)

    def test_build_rejects_malformed_ambiguous_root_encoding_and_external_resources(self):
        sentinel_path = self.root / "SENTINEL_PRIVATE_PATH"
        cases = {
            "malformed": b"<HealthData><Record></HealthData>",
            "wrong-root": b"<NotHealthData/>",
            "unsupported-encoding": (
                b'<?xml version="1.0" encoding="SENTINEL-ENCODING"?><HealthData/>'
            ),
            "external-doctype": (
                f'<!DOCTYPE HealthData SYSTEM "file://{sentinel_path}"><HealthData/>'
                .encode("utf-8")
            ),
            "external-entity": (
                f'<!DOCTYPE HealthData [<!ENTITY ext SYSTEM "file://{sentinel_path}">]>'
                "<HealthData>&ext;</HealthData>"
            ).encode("utf-8"),
            "external-parameter-entity": (
                f'<!DOCTYPE HealthData [<!ENTITY % ext SYSTEM "file://{sentinel_path}">'
                "%ext;]><HealthData/>"
            ).encode("utf-8"),
        }
        for index, (name, xml) in enumerate(cases.items()):
            with self.subTest(name=name):
                archive = self.make_archive(xml, name=f"bad-{index}.zip")
                output = self.root / f"bad-{index}.json"
                completed, _ = self.build(archive, output)
                self.assert_safe_failure(
                    completed,
                    "build",
                    forbidden=(sentinel_path, "SENTINEL-ENCODING", archive, output),
                )
                self.assertFalse(output.exists())

        ambiguous = self.make_archive(
            export_xml(),
            name="ambiguous.zip",
            extra_members=(("other/export.xml", export_xml()),),
        )
        completed, output = self.build(ambiguous, self.root / "ambiguous.json")
        self.assert_safe_failure(completed, "build", forbidden=(ambiguous, output))
        self.assertFalse(output.exists())

        no_export = self.root / "no-export.zip"
        with zipfile.ZipFile(no_export, "w") as archive:
            archive.writestr("not-export.xml", export_xml())
        completed, output = self.build(no_export, self.root / "no-export.json")
        self.assert_safe_failure(completed, "build", forbidden=(no_export, output))
        self.assertFalse(output.exists())

        internal = self.make_archive(
            (
                f'<!DOCTYPE HealthData [<!ENTITY source "Internal Source">]>'
                f'<HealthData><Record type="{STEP}" sourceName="&source;"/></HealthData>'
            ).encode("utf-8"),
            name="internal-dtd.zip",
        )
        internal_envelope, _ = self.build_catalogue(
            internal, self.root / "internal-dtd.json"
        )
        self.assertEqual(
            internal_envelope["catalogue"]["families"]["steps"][0]["descriptor"][0],
            ["present", "Internal Source"],
        )

    def test_build_rejects_corrupt_compressed_member_as_fixed_expected_failure(self):
        archive = self.make_archive(export_xml(
            record(STEP, sourceName="SENTINEL_CORRUPT_MEMBER_SOURCE"),
        ))
        self.corrupt_compressed_member(archive)
        output = self.root / "SENTINEL_CORRUPT_MEMBER_OUTPUT.json"
        completed, _ = self.build(archive, output)
        error = self.assert_safe_failure(
            completed,
            "build",
            forbidden=(archive, output, "SENTINEL_CORRUPT_MEMBER_SOURCE"),
        )
        self.assertEqual(error, "ARCHIVE_INVALID")
        self.assertFalse(output.exists())

    def test_build_rejects_corrupt_lzma_properties_and_body_as_expected_failures(self):
        corruptions = (("properties", 4, 0xFF), ("body", 9, None))
        for index, (label, offset, replacement) in enumerate(corruptions):
            with self.subTest(label=label):
                sentinel = f"SENTINEL_LZMA_{label.upper()}_SOURCE"
                archive = self.make_archive(
                    export_xml(record(STEP, sourceName=sentinel)),
                    name=f"lzma-{index}.zip",
                    compression=zipfile.ZIP_LZMA,
                )
                contents, compressed_start, info = self.compressed_member_details(archive)
                self.assertGreater(info.compress_size, offset)
                position = compressed_start + offset
                if replacement is None:
                    contents[position] ^= 0xFF
                else:
                    contents[position] = replacement
                archive.write_bytes(contents)

                output = self.root / f"lzma-{index}-output.json"
                completed, _ = self.build(archive, output)
                error = self.assert_safe_failure(
                    completed,
                    "build",
                    forbidden=(archive, output, sentinel),
                )
                self.assertEqual(error, "ARCHIVE_INVALID")
                self.assertFalse(output.exists())

    def test_xml_namespaces_do_not_match_exact_unqualified_elements(self):
        foreign_root = self.make_archive(
            (
                '<HealthData xmlns="urn:not-health">'
                f'<Record type="{STEP}" sourceName="SENTINEL_FOREIGN_ROOT"/>'
                "</HealthData>"
            ).encode("utf-8"),
            name="foreign-root.zip",
        )
        foreign_output = self.root / "foreign-root.json"
        completed, _ = self.build(foreign_root, foreign_output)
        self.assert_safe_failure(
            completed,
            "build",
            forbidden=(foreign_root, foreign_output, "SENTINEL_FOREIGN_ROOT"),
        )
        self.assertFalse(foreign_output.exists())

        mixed = self.make_archive(
            (
                '<HealthData xmlns:foreign="urn:not-health">'
                f'<foreign:Record type="{STEP}" sourceName="SENTINEL_FOREIGN_RECORD"/>'
                f'<Record type="{STEP}" sourceName="Local"/>'
                "</HealthData>"
            ).encode("utf-8"),
            name="foreign-record.zip",
        )
        envelope, output = self.build_catalogue(mixed, self.root / "foreign-record.json")
        catalogue = envelope["catalogue"]
        self.assertEqual(catalogue["record_count_all_types"], 1)
        self.assertEqual(len(catalogue["families"]["steps"]), 1)
        self.assertEqual(
            catalogue["families"]["steps"][0]["descriptor"][0],
            ["present", "Local"],
        )
        self.assertNotIn("SENTINEL_FOREIGN_RECORD", output.read_text(encoding="ascii"))

    def test_output_guard_rejects_checkout_alias_existing_and_missing_parent(self):
        archive = self.make_archive(export_xml(record(STEP, sourceName="Safe")))

        alias = self.root / "checkout-alias"
        alias.symlink_to(CHECKOUT / "spikes" / "health_catalogue", target_is_directory=True)
        alias_output = alias / "SENTINEL_ALIAS_OUTPUT.json"
        completed, _ = self.build(archive, alias_output)
        self.assert_safe_failure(completed, "build", forbidden=(archive, alias_output))
        self.assertFalse((CHECKOUT / "spikes" / "health_catalogue" /
                          "SENTINEL_ALIAS_OUTPUT.json").exists())

        existing = self.root / "existing.json"
        existing.write_text("SENTINEL_EXISTING_CONTENT", encoding="utf-8")
        completed, _ = self.build(archive, existing)
        self.assert_safe_failure(completed, "build", forbidden=(archive, existing))
        self.assertEqual(existing.read_text(encoding="utf-8"), "SENTINEL_EXISTING_CONTENT")

        missing_parent = self.root / "SENTINEL_MISSING_PARENT" / "result.json"
        completed, _ = self.build(archive, missing_parent)
        self.assert_safe_failure(completed, "build", forbidden=(archive, missing_parent))
        self.assertFalse(missing_parent.exists())

    def test_bind_validates_request_and_preserves_unselected_groups_and_false_attestations(self):
        archive = self.make_archive(export_xml(
            record(STEP, sourceName="Phone", sourceVersion="1", device="Device P"),
            record(HRV, sourceName="Watch", sourceVersion="2", device="Device W"),
            record(SLEEP, sourceName="Sleep App", sourceVersion="3", device="Device S"),
            record(SLEEP),
        ))
        envelope, catalogue_path = self.build_catalogue(archive)
        catalogue = envelope["catalogue"]
        step_id = catalogue["families"]["steps"][0]["group_id"]
        sleep_id = next(
            group["group_id"] for group in catalogue["families"]["sleep"]
            if group["descriptor"][0] == ["present", "Sleep App"]
        )
        selections = {
            "steps": self.selection([step_id], choice="Bogus but explicit assertion"),
            "sleep": self.selection(
                [sleep_id],
                handle="00000000-0000-4000-8000-000000000002",
                scope="device",
            ),
        }
        request, request_path = self.write_request(envelope, selections)
        output = self.root / "binding.json"
        completed = self.run_tool(
            "bind", "--archive", archive, "--catalogue", catalogue_path,
            "--request", request_path, "--output", output,
        )
        self.assert_pass(completed, "bind")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        result = json.loads(output.read_text(encoding="ascii"))
        self.assertEqual(set(result), {"binding", "binding_sha256"})
        binding = result["binding"]
        self.assertEqual(binding, {
            "binding_schema": "mesh-health-selection-binding-v1",
            "catalogue": catalogue,
            "catalogue_sha256": envelope["catalogue_sha256"],
            "selection_request": request,
            "validation_scope": "archive_integrity_and_selection_membership",
            "device_identity_verified": False,
            "user_intent_verified": False,
            "health_source_priority_verified": False,
        })
        self.assertIs(binding["device_identity_verified"], False)
        self.assertIs(binding["user_intent_verified"], False)
        self.assertIs(binding["health_source_priority_verified"], False)
        self.assertIn("hrv_sdnn", binding["catalogue"]["families"])
        self.assertEqual(len(binding["catalogue"]["families"]["sleep"]), 2)
        self.assertEqual(result["binding_sha256"], hashlib.sha256(canonical(binding)).hexdigest())

        repeat = self.root / "binding-repeat.json"
        second = self.run_tool(
            "bind", "--archive", archive, "--catalogue", catalogue_path,
            "--request", request_path, "--output", repeat,
        )
        self.assert_pass(second, "bind")
        self.assertEqual(output.read_bytes(), repeat.read_bytes())

    def test_bind_rejects_tampered_catalogue_read_only_without_output(self):
        archive = self.make_archive(export_xml(record(STEP, sourceName="Private Source")))
        envelope, catalogue_path = self.build_catalogue(archive)
        group = envelope["catalogue"]["families"]["steps"][0]
        _, request_path = self.write_request(
            envelope, {"steps": self.selection([group["group_id"]])}
        )
        tampered = json.loads(catalogue_path.read_text(encoding="ascii"))
        tampered["catalogue"]["record_count_all_types"] = 2
        tampered["catalogue"]["families"]["steps"][0]["record_count"] = 2
        tampered["catalogue_sha256"] = hashlib.sha256(
            canonical(tampered["catalogue"])
        ).hexdigest()
        catalogue_path.write_bytes(canonical(tampered))
        before = catalogue_path.read_bytes()
        output = self.root / "must-not-exist.json"
        completed = self.run_tool(
            "bind", "--archive", archive, "--catalogue", catalogue_path,
            "--request", request_path, "--output", output,
        )
        self.assert_safe_failure(
            completed, "bind", forbidden=(archive, catalogue_path, request_path,
                                           output, "Private Source"),
        )
        self.assertEqual(catalogue_path.read_bytes(), before)
        self.assertFalse(output.exists())

    def test_bind_rejects_duplicate_keys_and_nonfinite_numbers_without_leaks(self):
        archive = self.make_archive(export_xml(record(STEP, sourceName="Private Source")))
        envelope, catalogue_path = self.build_catalogue(archive)
        group_id_value = envelope["catalogue"]["families"]["steps"][0]["group_id"]
        valid_selection = canonical({"steps": self.selection([group_id_value])}).decode("ascii")
        request_prefix = (
            '{"selection_schema":"mesh-health-selection-request-v1",'
            f'"archive_sha256":"{envelope["catalogue"]["archive_sha256"]}",'
            f'"catalogue_sha256":"{envelope["catalogue_sha256"]}",'
        )
        payloads = (
            request_prefix + f'"selections":{valid_selection},"selections":{valid_selection}}}',
            request_prefix + f'"selections":{valid_selection},"extra":NaN}}',
            request_prefix + f'"selections":{valid_selection},"extra":Infinity}}',
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                request_path = self.root / f"unsafe-{index}.json"
                request_path.write_text(payload, encoding="utf-8")
                output = self.root / f"unsafe-{index}-binding.json"
                completed = self.run_tool(
                    "bind", "--archive", archive, "--catalogue", catalogue_path,
                    "--request", request_path, "--output", output,
                )
                self.assert_safe_failure(
                    completed, "bind", forbidden=(archive, catalogue_path, request_path,
                                                   output, "Private Source"),
                )
                self.assertFalse(output.exists())

    def test_bind_rejects_invalid_selection_shapes_identity_membership_and_source(self):
        archive = self.make_archive(export_xml(
            record(STEP, sourceName="Phone"),
            record(STEP, sourceName="Tablet"),
            record(HRV, sourceName="Watch"),
            record(SLEEP),
            record(SLEEP, sourceName=" \t"),
        ))
        envelope, catalogue_path = self.build_catalogue(archive)
        step_ids = [
            group["group_id"]
            for group in envelope["catalogue"]["families"]["steps"]
        ]
        steps = step_ids[0]
        hrv = envelope["catalogue"]["families"]["hrv_sdnn"][0]["group_id"]
        sleep_groups = envelope["catalogue"]["families"]["sleep"]
        missing_sleep = next(
            group["group_id"] for group in sleep_groups
            if group["descriptor"][0] == ["missing"]
        )
        whitespace_sleep = next(
            group["group_id"] for group in sleep_groups
            if group["descriptor"][0] == ["present", " \t"]
        )
        base = {"steps": self.selection([steps])}
        cases = {
            "empty selections": {},
            "empty group ids": {"steps": self.selection([])},
            "duplicate group ids": {"steps": self.selection([steps, steps])},
            "unsorted group ids": {"steps": self.selection(sorted(step_ids, reverse=True))},
            "cross family": {"steps": self.selection([hrv])},
            "unknown group": {"steps": self.selection(["0" * 64])},
            "missing source": {"sleep": self.selection([missing_sleep])},
            "whitespace source": {"sleep": self.selection([whitespace_sleep])},
            "wrong scope": {"steps": self.selection([steps], scope="anything")},
            "blank choice": {"steps": self.selection([steps], choice=" \t")},
            "blank evidence": {"steps": self.selection([steps], evidence="\n")},
            "non-v4 uuid": {"steps": self.selection(
                [steps], handle="00000000-0000-1000-8000-000000000001")},
            "uppercase uuid": {"steps": self.selection(
                [steps], handle="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")},
            "duplicate handles": {
                "steps": self.selection([steps]),
                "hrv_sdnn": self.selection([hrv]),
            },
            "unknown family": {**base, "other": self.selection([steps])},
            "selection extra field": {
                "steps": {**self.selection([steps]), "extra": "field"}
            },
            "wrong group list type": {"steps": self.selection(steps)},
        }
        for index, (name, selections) in enumerate(cases.items()):
            with self.subTest(name=name):
                _, request_path = self.write_request(
                    envelope, selections, name=f"invalid-{index}.json"
                )
                output = self.root / f"invalid-{index}-binding.json"
                completed = self.run_tool(
                    "bind", "--archive", archive, "--catalogue", catalogue_path,
                    "--request", request_path, "--output", output,
                )
                self.assert_safe_failure(completed, "bind", forbidden=(request_path, output))
                self.assertFalse(output.exists())

        stale_request, stale_path = self.write_request(envelope, base, name="stale.json")
        stale_request["archive_sha256"] = "f" * 64
        stale_path.write_bytes(canonical(stale_request))
        completed = self.run_tool(
            "bind", "--archive", archive, "--catalogue", catalogue_path,
            "--request", stale_path, "--output", self.root / "stale-binding.json",
        )
        self.assert_safe_failure(completed, "bind", forbidden=(stale_path, archive))

        wrong_digest, wrong_digest_path = self.write_request(
            envelope, base, name="wrong-digest.json"
        )
        wrong_digest["catalogue_sha256"] = "e" * 64
        wrong_digest_path.write_bytes(canonical(wrong_digest))
        completed = self.run_tool(
            "bind", "--archive", archive, "--catalogue", catalogue_path,
            "--request", wrong_digest_path,
            "--output", self.root / "wrong-digest-binding.json",
        )
        self.assert_safe_failure(completed, "bind", forbidden=(wrong_digest_path, archive))

        extra_request, extra_request_path = self.write_request(
            envelope, base, name="extra-request-field.json"
        )
        extra_request["extra"] = "field"
        extra_request_path.write_bytes(canonical(extra_request))
        completed = self.run_tool(
            "bind", "--archive", archive, "--catalogue", catalogue_path,
            "--request", extra_request_path,
            "--output", self.root / "extra-request-binding.json",
        )
        self.assert_safe_failure(completed, "bind", forbidden=(extra_request_path, archive))

    def test_catalogue_validation_rejects_bool_float_counts_and_unknown_fields(self):
        archive = self.make_archive(export_xml(record(STEP, sourceName="Phone")))
        envelope, catalogue_path = self.build_catalogue(archive)
        group_id_value = envelope["catalogue"]["families"]["steps"][0]["group_id"]
        _, request_path = self.write_request(
            envelope, {"steps": self.selection([group_id_value])}
        )
        mutations = (
            lambda data: data["catalogue"].__setitem__("archive_bytes", True),
            lambda data: data["catalogue"].__setitem__("record_count_all_types", 1.0),
            lambda data: data["catalogue"]["families"]["steps"][0].__setitem__(
                "record_count", True),
            lambda data: data["catalogue"].__setitem__("unknown", "field"),
        )
        original = catalogue_path.read_bytes()
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                altered = json.loads(original)
                mutate(altered)
                catalogue_path.write_bytes(canonical(altered))
                before = catalogue_path.read_bytes()
                output = self.root / f"typed-{index}.json"
                completed = self.run_tool(
                    "bind", "--archive", archive, "--catalogue", catalogue_path,
                    "--request", request_path, "--output", output,
                )
                self.assert_safe_failure(completed, "bind", forbidden=(catalogue_path, output))
                self.assertEqual(catalogue_path.read_bytes(), before)
                self.assertFalse(output.exists())
        catalogue_path.write_bytes(original)

    def test_cli_usage_and_missing_inputs_are_fixed_safe_failures(self):
        sentinel = self.root / "SENTINEL_NONEXISTENT_ARCHIVE"
        completed = self.run_tool("build", "--archive", sentinel, "--output",
                                  self.root / "unused.json")
        self.assert_safe_failure(completed, "build", forbidden=(sentinel, "unused.json"))

        completed = self.run_tool("build", "--archive", "SENTINEL_ARGUMENT")
        self.assert_safe_failure(completed, "build", forbidden=("SENTINEL_ARGUMENT",))

        completed = self.run_tool("build", "--help")
        error = self.assert_safe_failure(completed, "build")
        self.assertEqual(error, "CLI_USAGE")

        completed = self.run_tool("--help")
        error = self.assert_safe_failure(completed, "unknown")
        self.assertEqual(error, "CLI_USAGE")

    def test_unexpected_programmer_failure_is_fixed_failure_not_success(self):
        output = io.StringIO()
        original = SUBJECT.build_command
        try:
            def fail_unexpectedly(archive, destination):
                del archive, destination
                raise RuntimeError("SENTINEL_EXCEPTION_TEXT")

            SUBJECT.build_command = fail_unexpectedly
            with contextlib.redirect_stdout(output):
                result = SUBJECT.main([
                    "build", "--archive", "SENTINEL_ARCHIVE_PATH",
                    "--output", "SENTINEL_OUTPUT_PATH",
                ])
        finally:
            SUBJECT.build_command = original
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "error": "INTERNAL_ERROR", "operation": "build", "status": "FAIL",
        })
        self.assertNotIn("SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
