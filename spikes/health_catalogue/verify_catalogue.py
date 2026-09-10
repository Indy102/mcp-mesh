#!/usr/bin/env python3
"""Independently verify a Health source catalogue and optional binding."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import uuid
import zipfile
from xml.parsers import expat


CATALOGUE_SCHEMA = "mesh-health-catalogue-v1"
REQUEST_SCHEMA = "mesh-health-selection-request-v1"
BINDING_SCHEMA = "mesh-health-selection-binding-v1"
VERIFICATION_SCOPE = "archive_catalogue_and_optional_binding_membership"
BINDING_SCOPE = "archive_integrity_and_selection_membership"

FAMILIES = ("steps", "hrv_sdnn", "sleep")
TYPE_TO_FAMILY = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
}
SOURCE_ATTRIBUTES = ("sourceName", "sourceVersion", "device")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class VerificationFailure(Exception):
    """A controlled verification failure carrying only a safe code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise VerificationFailure("ARGUMENTS_INVALID")


def _fail(code: str) -> None:
    raise VerificationFailure(code)


def _canonical(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        _fail("CANONICALIZATION_FAILED")
    return rendered.encode("ascii")


def _sha256_canonical(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON_INVALID")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    _fail("JSON_INVALID")


def _load_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(
                stream,
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=_reject_json_constant,
            )
    except VerificationFailure:
        raise
    except (OSError, UnicodeError, ValueError):
        _fail("JSON_INVALID")


def _exact_keys(value: object, keys: set[str], code: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail(code)
    return value


def _is_hex64(value: object) -> bool:
    return type(value) is str and HEX64.fullmatch(value) is not None


def _descriptor(value: object, code: str) -> tuple[tuple[str, ...], ...]:
    if type(value) is not list or len(value) != len(SOURCE_ATTRIBUTES):
        _fail(code)
    result: list[tuple[str, ...]] = []
    for slot in value:
        if type(slot) is not list or not slot or type(slot[0]) is not str:
            _fail(code)
        if slot == ["missing"]:
            result.append(("missing",))
        elif len(slot) == 2 and slot[0] == "present" and type(slot[1]) is str:
            result.append(("present", slot[1]))
        else:
            _fail(code)
    return tuple(result)


def _validate_catalogue_content(value: object, code: str) -> dict[str, object]:
    catalogue = _exact_keys(
        value,
        {
            "catalogue_schema",
            "archive_sha256",
            "archive_bytes",
            "xml_member",
            "record_count_all_types",
            "families",
        },
        code,
    )
    if catalogue["catalogue_schema"] != CATALOGUE_SCHEMA:
        _fail(code)
    if not _is_hex64(catalogue["archive_sha256"]):
        _fail(code)
    archive_bytes = catalogue["archive_bytes"]
    total_records = catalogue["record_count_all_types"]
    if type(archive_bytes) is not int or archive_bytes < 0:
        _fail(code)
    if type(total_records) is not int or total_records < 0:
        _fail(code)
    if type(catalogue["xml_member"]) is not str:
        _fail(code)

    families = _exact_keys(catalogue["families"], set(FAMILIES), code)
    classified_count = 0
    all_group_ids: set[str] = set()
    for family in FAMILIES:
        groups = families[family]
        if type(groups) is not list:
            _fail(code)
        prior_descriptor_bytes: bytes | None = None
        descriptors: set[tuple[tuple[str, ...], ...]] = set()
        family_ids: set[str] = set()
        for raw_group in groups:
            group = _exact_keys(
                raw_group, {"group_id", "descriptor", "record_count"}, code
            )
            group_id = group["group_id"]
            record_count = group["record_count"]
            if not _is_hex64(group_id):
                _fail(code)
            if type(record_count) is not int or record_count <= 0:
                _fail(code)
            normalized_descriptor = _descriptor(group["descriptor"], code)
            descriptor_bytes = _canonical(group["descriptor"])
            if prior_descriptor_bytes is not None and descriptor_bytes <= prior_descriptor_bytes:
                _fail(code)
            prior_descriptor_bytes = descriptor_bytes
            if normalized_descriptor in descriptors or group_id in family_ids:
                _fail(code)
            descriptors.add(normalized_descriptor)
            family_ids.add(group_id)
            if group_id in all_group_ids:
                _fail(code)
            all_group_ids.add(group_id)
            expected_id = _sha256_canonical(
                {"family": family, "descriptor": group["descriptor"]}
            )
            if group_id != expected_id:
                _fail(code)
            classified_count += record_count
    if classified_count > total_records:
        _fail(code)
    return catalogue


def _validate_catalogue_envelope(value: object) -> tuple[dict[str, object], str]:
    envelope = _exact_keys(
        value, {"catalogue", "catalogue_sha256"}, "CATALOGUE_STRUCTURE_INVALID"
    )
    digest = envelope["catalogue_sha256"]
    if not _is_hex64(digest):
        _fail("CATALOGUE_STRUCTURE_INVALID")
    catalogue = _validate_catalogue_content(
        envelope["catalogue"], "CATALOGUE_STRUCTURE_INVALID"
    )
    if digest != _sha256_canonical(catalogue):
        _fail("CATALOGUE_DIGEST_MISMATCH")
    return catalogue, digest


def _digest_open_file(stream: object) -> tuple[str, int]:
    try:
        stream.seek(0)
        digest = hashlib.sha256()
        total = 0
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
        return digest.hexdigest(), total
    except (OSError, ValueError, TypeError):
        _fail("ARCHIVE_INVALID")


class _HealthAccumulator:
    def __init__(self) -> None:
        self.depth = 0
        self.saw_root = False
        self.record_count = 0
        self.groups: dict[str, dict[tuple[tuple[str, ...], ...], int]] = {
            family: {} for family in FAMILIES
        }

    def start(self, name: str, attrs: dict[str, str]) -> None:
        if self.depth == 0:
            if self.saw_root or name != "HealthData":
                _fail("XML_INVALID")
            self.saw_root = True
        self.depth += 1
        if name != "Record":
            return
        self.record_count += 1
        family = TYPE_TO_FAMILY.get(attrs.get("type"))
        if family is None:
            return
        descriptor = tuple(
            ("present", attrs[key]) if key in attrs else ("missing",)
            for key in SOURCE_ATTRIBUTES
        )
        self.groups[family][descriptor] = self.groups[family].get(descriptor, 0) + 1

    def end(self, name: str) -> None:
        del name
        self.depth -= 1

    def content(self, archive_digest: str, archive_bytes: int, member: str) -> dict[str, object]:
        if not self.saw_root or self.depth != 0:
            _fail("XML_INVALID")
        families: dict[str, list[dict[str, object]]] = {}
        for family in FAMILIES:
            groups: list[dict[str, object]] = []
            for descriptor, count in self.groups[family].items():
                serial_descriptor = [list(slot) for slot in descriptor]
                groups.append(
                    {
                        "group_id": _sha256_canonical(
                            {"family": family, "descriptor": serial_descriptor}
                        ),
                        "descriptor": serial_descriptor,
                        "record_count": count,
                    }
                )
            groups.sort(key=lambda group: _canonical(group["descriptor"]))
            families[family] = groups
        return {
            "catalogue_schema": CATALOGUE_SCHEMA,
            "archive_sha256": archive_digest,
            "archive_bytes": archive_bytes,
            "xml_member": member,
            "record_count_all_types": self.record_count,
            "families": families,
        }


def _parse_health_xml(stream: object) -> _HealthAccumulator:
    accumulator = _HealthAccumulator()
    parser = expat.ParserCreate()
    parser.StartElementHandler = accumulator.start
    parser.EndElementHandler = accumulator.end

    def doctype(
        name: str, system_id: str | None, public_id: str | None, has_internal: int
    ) -> None:
        del name, public_id, has_internal
        if system_id is not None:
            _fail("XML_EXTERNAL_RESOURCE")

    def external_entity(
        context: str | None,
        base: str | None,
        system_id: str,
        public_id: str | None,
    ) -> int:
        del context, base, system_id, public_id
        _fail("XML_EXTERNAL_RESOURCE")

    parser.StartDoctypeDeclHandler = doctype
    parser.ExternalEntityRefHandler = external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_ALWAYS)
    try:
        while True:
            block = stream.read(64 * 1024)
            if not block:
                break
            parser.Parse(block, False)
        parser.Parse(b"", True)
    except VerificationFailure:
        raise
    except (expat.ExpatError, LookupError, OSError, UnicodeError, ValueError):
        _fail("XML_INVALID")
    return accumulator


def _recompute_catalogue(archive: Path) -> dict[str, object]:
    try:
        with archive.open("rb") as raw_archive:
            before_digest, before_bytes = _digest_open_file(raw_archive)
            raw_archive.seek(0)
            with zipfile.ZipFile(raw_archive, "r") as health_zip:
                matches = [
                    info
                    for info in health_zip.infolist()
                    if not info.is_dir()
                    and PurePosixPath(info.filename).name == "export.xml"
                ]
                if len(matches) != 1:
                    _fail("ARCHIVE_XML_MEMBER_INVALID")
                member = matches[0]
                with health_zip.open(member, "r") as xml_stream:
                    accumulator = _parse_health_xml(xml_stream)
            after_digest, after_bytes = _digest_open_file(raw_archive)
    except VerificationFailure:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        _fail("ARCHIVE_INVALID")
    if (before_digest, before_bytes) != (after_digest, after_bytes):
        _fail("ARCHIVE_CHANGED")
    return accumulator.content(before_digest, before_bytes, member.filename)


def _nonblank(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _validate_request(
    value: object, catalogue: dict[str, object], catalogue_digest: str
) -> dict[str, object]:
    request = _exact_keys(
        value,
        {"selection_schema", "archive_sha256", "catalogue_sha256", "selections"},
        "BINDING_REQUEST_INVALID",
    )
    if request["selection_schema"] != REQUEST_SCHEMA:
        _fail("BINDING_REQUEST_INVALID")
    if not _is_hex64(request["archive_sha256"]) or not _is_hex64(
        request["catalogue_sha256"]
    ):
        _fail("BINDING_REQUEST_INVALID")
    if request["archive_sha256"] != catalogue["archive_sha256"]:
        _fail("BINDING_IDENTITY_MISMATCH")
    if request["catalogue_sha256"] != catalogue_digest:
        _fail("BINDING_IDENTITY_MISMATCH")

    selections = request["selections"]
    if type(selections) is not dict or not selections or not set(selections) <= set(FAMILIES):
        _fail("BINDING_REQUEST_INVALID")
    handles: set[str] = set()
    catalogue_families = catalogue["families"]
    for family, raw_selection in selections.items():
        selection = _exact_keys(
            raw_selection,
            {
                "selection_handle",
                "group_ids",
                "source_scope",
                "user_choice",
                "identity_evidence",
            },
            "BINDING_REQUEST_INVALID",
        )
        handle = selection["selection_handle"]
        if type(handle) is not str:
            _fail("BINDING_REQUEST_INVALID")
        try:
            parsed_handle = uuid.UUID(handle)
        except (ValueError, AttributeError):
            _fail("BINDING_REQUEST_INVALID")
        if (
            str(parsed_handle) != handle
            or parsed_handle.version != 4
            or parsed_handle.variant != uuid.RFC_4122
            or handle in handles
        ):
            _fail("BINDING_REQUEST_INVALID")
        handles.add(handle)

        group_ids = selection["group_ids"]
        if (
            type(group_ids) is not list
            or not group_ids
            or any(not _is_hex64(group_id) for group_id in group_ids)
            or group_ids != sorted(group_ids)
            or len(set(group_ids)) != len(group_ids)
        ):
            _fail("BINDING_REQUEST_INVALID")
        family_groups = {
            group["group_id"]: group for group in catalogue_families[family]
        }
        if any(group_id not in family_groups for group_id in group_ids):
            _fail("BINDING_MEMBERSHIP_INVALID")
        for group_id in group_ids:
            source_name_slot = family_groups[group_id]["descriptor"][0]
            if (
                source_name_slot[0] != "present"
                or not _nonblank(source_name_slot[1])
            ):
                _fail("BINDING_MEMBERSHIP_INVALID")
        if type(selection["source_scope"]) is not str or selection[
            "source_scope"
        ] not in {"app_stream", "device"}:
            _fail("BINDING_REQUEST_INVALID")
        if not _nonblank(selection["user_choice"]) or not _nonblank(
            selection["identity_evidence"]
        ):
            _fail("BINDING_REQUEST_INVALID")
    return request


def _validate_binding(
    value: object, catalogue: dict[str, object], catalogue_digest: str
) -> None:
    envelope = _exact_keys(
        value, {"binding", "binding_sha256"}, "BINDING_STRUCTURE_INVALID"
    )
    digest = envelope["binding_sha256"]
    if not _is_hex64(digest):
        _fail("BINDING_STRUCTURE_INVALID")
    binding = _exact_keys(
        envelope["binding"],
        {
            "binding_schema",
            "catalogue",
            "catalogue_sha256",
            "selection_request",
            "validation_scope",
            "device_identity_verified",
            "user_intent_verified",
            "health_source_priority_verified",
        },
        "BINDING_STRUCTURE_INVALID",
    )
    if binding["binding_schema"] != BINDING_SCHEMA:
        _fail("BINDING_STRUCTURE_INVALID")
    embedded = _validate_catalogue_content(
        binding["catalogue"], "BINDING_STRUCTURE_INVALID"
    )
    if embedded != catalogue:
        _fail("BINDING_IDENTITY_MISMATCH")
    if binding["catalogue_sha256"] != catalogue_digest:
        _fail("BINDING_IDENTITY_MISMATCH")
    if binding["validation_scope"] != BINDING_SCOPE:
        _fail("BINDING_SCOPE_INVALID")
    for flag in (
        "device_identity_verified",
        "user_intent_verified",
        "health_source_priority_verified",
    ):
        if type(binding[flag]) is not bool or binding[flag] is not False:
            _fail("BINDING_ATTESTATION_INVALID")
    _validate_request(binding["selection_request"], catalogue, catalogue_digest)
    if digest != _sha256_canonical(binding):
        _fail("BINDING_DIGEST_MISMATCH")


def verify(archive: Path, catalogue_path: Path, binding_path: Path | None) -> None:
    recomputed = _recompute_catalogue(archive)
    persisted, catalogue_digest = _validate_catalogue_envelope(
        _load_json(catalogue_path)
    )
    if persisted != recomputed:
        _fail("CATALOGUE_MISMATCH")
    if binding_path is not None:
        _validate_binding(_load_json(binding_path), persisted, catalogue_digest)


def _result(status: str, diagnostic: str | None, binding_requested: bool) -> dict[str, object]:
    checks = [
        "archive_integrity",
        "catalogue_structure_and_digest",
        "catalogue_exact_recomputation",
    ]
    if binding_requested:
        checks.append("binding_structure_and_membership")
    return {
        "status": status,
        "checks": checks,
        "diagnostics": [] if diagnostic is None else [diagnostic],
        "verification_scope": VERIFICATION_SCOPE,
    }


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--catalogue", required=True, type=Path)
    parser.add_argument("--binding", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    binding_requested = False
    try:
        args = _parser().parse_args(argv)
        binding_requested = args.binding is not None
        verify(args.archive, args.catalogue, args.binding)
    except VerificationFailure as error:
        print(
            json.dumps(
                _result("FAIL", error.code, binding_requested),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                _result("FAIL", "INTERNAL_ERROR", binding_requested),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(
        json.dumps(
            _result("PASS", None, binding_requested),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
