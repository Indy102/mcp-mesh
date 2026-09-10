#!/usr/bin/env python3
"""Build and validate private Apple Health source-selection metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PurePosixPath
from xml.parsers import expat


CATALOGUE_SCHEMA = "mesh-health-catalogue-v1"
SELECTION_SCHEMA = "mesh-health-selection-request-v1"
BINDING_SCHEMA = "mesh-health-selection-binding-v1"
VALIDATION_SCOPE = "archive_integrity_and_selection_membership"
FAMILY_TYPES = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep",
}
FAMILIES = ("steps", "hrv_sdnn", "sleep")
SOURCE_ATTRIBUTES = ("sourceName", "sourceVersion", "device")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
CHECKOUT = Path(__file__).resolve().parents[2]
CHUNK_SIZE = 1024 * 1024


class CatalogueError(Exception):
    """An expected failure represented publicly by a fixed code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser whose errors never echo private arguments."""

    def error(self, message: str) -> None:
        del message
        raise CatalogueError("CLI_USAGE")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _is_exact_int(value: object) -> bool:
    return type(value) is int


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def _require_exact_keys(value: object, keys: frozenset[str], code: str) -> dict:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise CatalogueError(code)
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogueError("JSON_INVALID")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> object:
    del value
    raise CatalogueError("JSON_INVALID")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CatalogueError("JSON_INVALID")
    return parsed


def read_strict_json(path: Path, failure_code: str) -> object:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(
                stream,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_constant,
                parse_float=_parse_finite_float,
            )
    except CatalogueError:
        raise CatalogueError(failure_code) from None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise CatalogueError(failure_code) from None


def _hash_open_stream(stream) -> tuple[str, int]:
    stream.seek(0)
    digest = hashlib.sha256()
    byte_count = 0
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
        byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _descriptor(attributes: dict[str, str]) -> list[list[str]]:
    return [
        ["missing"] if name not in attributes else ["present", attributes[name]]
        for name in SOURCE_ATTRIBUTES
    ]


class _ExportAccumulator:
    """Expat handlers retaining only nesting depth and descriptor counters."""

    def __init__(self) -> None:
        self.depth = 0
        self.root_seen = False
        self.record_count = 0
        self.groups = {family: Counter() for family in FAMILIES}

    def start_element(self, name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        if self.depth == 1:
            self.root_seen = True
            if name != "HealthData":
                raise CatalogueError("XML_INVALID")
        if name != "Record":
            return
        self.record_count += 1
        family = FAMILY_TYPES.get(attributes.get("type"))
        if family is not None:
            descriptor = tuple(tuple(slot) for slot in _descriptor(attributes))
            self.groups[family][descriptor] += 1

    def end_element(self, name: str) -> None:
        del name
        self.depth -= 1

    @staticmethod
    def start_doctype(
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: bool,
    ) -> None:
        del name, has_internal_subset
        if system_id is not None or public_id is not None:
            raise CatalogueError("EXTERNAL_XML_RESOURCE")

    @staticmethod
    def external_entity(
        context: str | None,
        base: str | None,
        system_id: str,
        public_id: str | None,
    ) -> int:
        del context, base, system_id, public_id
        raise CatalogueError("EXTERNAL_XML_RESOURCE")


def _parse_export(member) -> _ExportAccumulator:
    state = _ExportAccumulator()
    parser = expat.ParserCreate(namespace_separator="\x1f")
    parser.StartElementHandler = state.start_element
    parser.EndElementHandler = state.end_element
    parser.StartDoctypeDeclHandler = state.start_doctype
    parser.ExternalEntityRefHandler = state.external_entity
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_ALWAYS)
    try:
        while chunk := member.read(CHUNK_SIZE):
            parser.Parse(chunk, False)
        parser.Parse(b"", True)
    except CatalogueError:
        raise
    except (expat.ExpatError, LookupError, UnicodeError, ValueError):
        raise CatalogueError("XML_INVALID") from None
    if not state.root_seen or state.depth != 0:
        raise CatalogueError("XML_INVALID")
    return state


def _family_groups(family: str, counts: Counter) -> list[dict]:
    descriptors = [
        [list(slot) for slot in descriptor]
        for descriptor in counts
    ]
    descriptors.sort(key=canonical_json)
    return [
        {
            "group_id": sha256_json({"family": family, "descriptor": descriptor}),
            "descriptor": descriptor,
            "record_count": counts[tuple(tuple(slot) for slot in descriptor)],
        }
        for descriptor in descriptors
    ]


def compute_catalogue(archive_path: Path) -> dict:
    try:
        with archive_path.open("rb") as archive_stream:
            initial_hash, initial_size = _hash_open_stream(archive_stream)
            archive_stream.seek(0)
            with zipfile.ZipFile(archive_stream, "r") as archive:
                matches = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and PurePosixPath(info.filename).name == "export.xml"
                ]
                if len(matches) != 1:
                    raise CatalogueError("ARCHIVE_MEMBER_INVALID")
                member_info = matches[0]
                with archive.open(member_info, "r") as member:
                    state = _parse_export(member)
            final_hash, final_size = _hash_open_stream(archive_stream)
    except CatalogueError:
        raise
    except (
        OSError,
        EOFError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ):
        raise CatalogueError("ARCHIVE_INVALID") from None

    if (initial_hash, initial_size) != (final_hash, final_size):
        raise CatalogueError("ARCHIVE_CHANGED")
    return {
        "catalogue_schema": CATALOGUE_SCHEMA,
        "archive_sha256": initial_hash,
        "archive_bytes": initial_size,
        "xml_member": member_info.filename,
        "record_count_all_types": state.record_count,
        "families": {
            family: _family_groups(family, state.groups[family])
            for family in FAMILIES
        },
    }


def make_catalogue_envelope(catalogue: dict) -> dict:
    return {
        "catalogue": catalogue,
        "catalogue_sha256": sha256_json(catalogue),
    }


def _validate_descriptor(value: object) -> list[list[str]]:
    if not isinstance(value, list) or len(value) != 3:
        raise CatalogueError("CATALOGUE_INVALID")
    for slot in value:
        if not isinstance(slot, list):
            raise CatalogueError("CATALOGUE_INVALID")
        if slot == ["missing"]:
            continue
        if len(slot) != 2 or slot[0] != "present" or not isinstance(slot[1], str):
            raise CatalogueError("CATALOGUE_INVALID")
    return value


def validate_catalogue_envelope(value: object) -> tuple[dict, str]:
    envelope = _require_exact_keys(
        value,
        frozenset(("catalogue", "catalogue_sha256")),
        "CATALOGUE_INVALID",
    )
    catalogue = _require_exact_keys(
        envelope["catalogue"],
        frozenset((
            "catalogue_schema",
            "archive_sha256",
            "archive_bytes",
            "xml_member",
            "record_count_all_types",
            "families",
        )),
        "CATALOGUE_INVALID",
    )
    if catalogue["catalogue_schema"] != CATALOGUE_SCHEMA:
        raise CatalogueError("CATALOGUE_INVALID")
    if not _is_hash(catalogue["archive_sha256"]):
        raise CatalogueError("CATALOGUE_INVALID")
    if not _is_exact_int(catalogue["archive_bytes"]) or catalogue["archive_bytes"] < 0:
        raise CatalogueError("CATALOGUE_INVALID")
    if (
        not isinstance(catalogue["xml_member"], str)
        or PurePosixPath(catalogue["xml_member"]).name != "export.xml"
    ):
        raise CatalogueError("CATALOGUE_INVALID")
    if (
        not _is_exact_int(catalogue["record_count_all_types"])
        or catalogue["record_count_all_types"] < 0
    ):
        raise CatalogueError("CATALOGUE_INVALID")

    families = _require_exact_keys(
        catalogue["families"], frozenset(FAMILIES), "CATALOGUE_INVALID"
    )
    classified_count = 0
    all_group_ids = set()
    for family in FAMILIES:
        groups = families[family]
        if not isinstance(groups, list):
            raise CatalogueError("CATALOGUE_INVALID")
        previous_descriptor = None
        descriptors = set()
        for value_group in groups:
            group = _require_exact_keys(
                value_group,
                frozenset(("group_id", "descriptor", "record_count")),
                "CATALOGUE_INVALID",
            )
            descriptor = _validate_descriptor(group["descriptor"])
            descriptor_bytes = canonical_json(descriptor)
            descriptor_key = tuple(tuple(slot) for slot in descriptor)
            if descriptor_key in descriptors:
                raise CatalogueError("CATALOGUE_INVALID")
            if previous_descriptor is not None and descriptor_bytes <= previous_descriptor:
                raise CatalogueError("CATALOGUE_INVALID")
            descriptors.add(descriptor_key)
            previous_descriptor = descriptor_bytes
            expected_id = sha256_json({"family": family, "descriptor": descriptor})
            if group["group_id"] != expected_id or group["group_id"] in all_group_ids:
                raise CatalogueError("CATALOGUE_INVALID")
            all_group_ids.add(group["group_id"])
            if not _is_exact_int(group["record_count"]) or group["record_count"] <= 0:
                raise CatalogueError("CATALOGUE_INVALID")
            classified_count += group["record_count"]
    if classified_count > catalogue["record_count_all_types"]:
        raise CatalogueError("CATALOGUE_INVALID")

    digest = envelope["catalogue_sha256"]
    if not _is_hash(digest) or digest != sha256_json(catalogue):
        raise CatalogueError("CATALOGUE_INVALID")
    return catalogue, digest


def _valid_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def validate_selection_request(
    value: object,
    catalogue: dict,
    catalogue_digest: str,
) -> dict:
    request = _require_exact_keys(
        value,
        frozenset(("selection_schema", "archive_sha256", "catalogue_sha256", "selections")),
        "REQUEST_INVALID",
    )
    if request["selection_schema"] != SELECTION_SCHEMA:
        raise CatalogueError("REQUEST_INVALID")
    if request["archive_sha256"] != catalogue["archive_sha256"]:
        raise CatalogueError("REQUEST_INVALID")
    if request["catalogue_sha256"] != catalogue_digest:
        raise CatalogueError("REQUEST_INVALID")
    selections = request["selections"]
    if (
        not isinstance(selections, dict)
        or not selections
        or not set(selections).issubset(FAMILIES)
    ):
        raise CatalogueError("REQUEST_INVALID")

    handles = set()
    for family, selection_value in selections.items():
        selection = _require_exact_keys(
            selection_value,
            frozenset((
                "selection_handle",
                "group_ids",
                "source_scope",
                "user_choice",
                "identity_evidence",
            )),
            "REQUEST_INVALID",
        )
        handle = selection["selection_handle"]
        if not _valid_uuid4(handle) or handle in handles:
            raise CatalogueError("REQUEST_INVALID")
        handles.add(handle)
        group_ids = selection["group_ids"]
        if (
            not isinstance(group_ids, list)
            or not group_ids
            or not all(_is_hash(group_id) for group_id in group_ids)
            or group_ids != sorted(group_ids)
            or len(group_ids) != len(set(group_ids))
        ):
            raise CatalogueError("REQUEST_INVALID")
        available = {
            group["group_id"]: group
            for group in catalogue["families"][family]
        }
        for group_id in group_ids:
            if group_id not in available:
                raise CatalogueError("REQUEST_INVALID")
            source_slot = available[group_id]["descriptor"][0]
            if source_slot[0] != "present" or not source_slot[1].strip():
                raise CatalogueError("REQUEST_INVALID")
        if selection["source_scope"] not in ("app_stream", "device"):
            raise CatalogueError("REQUEST_INVALID")
        for field in ("user_choice", "identity_evidence"):
            if not isinstance(selection[field], str) or not selection[field].strip():
                raise CatalogueError("REQUEST_INVALID")
    return request


def _resolve_output(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=False)
        checkout = CHECKOUT.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise CatalogueError("OUTPUT_INVALID") from None
    if resolved == checkout or checkout in resolved.parents:
        raise CatalogueError("OUTPUT_NOT_PRIVATE")
    if os.path.lexists(path):
        raise CatalogueError("OUTPUT_EXISTS")
    try:
        parent = resolved.parent
        if not parent.exists() or not parent.is_dir():
            raise CatalogueError("OUTPUT_PARENT_INVALID")
    except (OSError, RuntimeError, ValueError):
        raise CatalogueError("OUTPUT_PARENT_INVALID") from None
    return resolved


def write_new_private(path: Path, value: object) -> None:
    resolved = _resolve_output(path)
    payload = canonical_json(value) + b"\n"
    descriptor = None
    created = False
    try:
        descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.close(descriptor)
        descriptor = None
    except FileExistsError:
        raise CatalogueError("OUTPUT_EXISTS") from None
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            try:
                resolved.unlink()
            except OSError:
                pass
        raise CatalogueError("OUTPUT_WRITE_FAILED") from None


def build_command(archive_path: Path, output_path: Path) -> None:
    _resolve_output(output_path)
    catalogue = compute_catalogue(archive_path)
    write_new_private(output_path, make_catalogue_envelope(catalogue))


def bind_command(
    archive_path: Path,
    catalogue_path: Path,
    request_path: Path,
    output_path: Path,
) -> None:
    _resolve_output(output_path)
    persisted_value = read_strict_json(catalogue_path, "CATALOGUE_INVALID")
    persisted_catalogue, persisted_digest = validate_catalogue_envelope(persisted_value)
    recomputed_catalogue = compute_catalogue(archive_path)
    if persisted_catalogue != recomputed_catalogue:
        raise CatalogueError("CATALOGUE_MISMATCH")
    request_value = read_strict_json(request_path, "REQUEST_INVALID")
    request = validate_selection_request(
        request_value, recomputed_catalogue, persisted_digest
    )
    binding = {
        "binding_schema": BINDING_SCHEMA,
        "catalogue": recomputed_catalogue,
        "catalogue_sha256": persisted_digest,
        "selection_request": request,
        "validation_scope": VALIDATION_SCOPE,
        "device_identity_verified": False,
        "user_intent_verified": False,
        "health_source_priority_verified": False,
    }
    write_new_private(
        output_path,
        {"binding": binding, "binding_sha256": sha256_json(binding)},
    )


def _parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(add_help=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    build = commands.add_parser("build", add_help=False)
    build.add_argument("--archive", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    bind = commands.add_parser("bind", add_help=False)
    bind.add_argument("--archive", required=True, type=Path)
    bind.add_argument("--catalogue", required=True, type=Path)
    bind.add_argument("--request", required=True, type=Path)
    bind.add_argument("--output", required=True, type=Path)
    return parser


def _emit(status: str, operation: str, error: str | None = None) -> None:
    value = {"operation": operation, "status": status}
    if error is not None:
        value["error"] = error
    sys.stdout.write(canonical_json(value).decode("ascii") + "\n")


def main(arguments: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    operation = argv[0] if argv and argv[0] in ("build", "bind") else "unknown"
    try:
        options = _parser().parse_args(argv)
        operation = options.operation
        if operation == "build":
            build_command(options.archive, options.output)
        else:
            bind_command(options.archive, options.catalogue, options.request, options.output)
    except CatalogueError as exc:
        _emit("FAIL", operation, exc.code)
        return 2
    except Exception:
        _emit("FAIL", operation, "INTERNAL_ERROR")
        return 1
    _emit("PASS", operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
