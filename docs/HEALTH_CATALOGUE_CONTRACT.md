# Health source catalogue and selection contract

Version: `mesh-health-catalogue-v1`. This is a standalone, private-metadata tool.
It implements only the catalogue/binding portion of the Health policy, not values,
coverage, source priority, device identity, or production adapter integration.

## Catalogue

Read one local Health ZIP through its unique non-directory member whose basename
is exactly `export.xml`. Reject duplicate/ambiguous members and malformed XML.
Require the root `HealthData`. Count every exact `Record` element, including other
types, in `record_count_all_types`. Classify by the exact `type` attribute:

- `HKQuantityTypeIdentifierStepCount` -> `steps`
- `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` -> `hrv_sdnn`
- `HKCategoryTypeIdentifierSleepAnalysis` -> `sleep`, before reading any category

Never inspect dates/values/units to decide catalogue membership. Every matching
record, including invalid or missing category/time/value, enters its descriptor
group. Copy only the three source attributes; no other record content is retained.
The descriptor is an array in `sourceName, sourceVersion, device` order. Each slot
is `["missing"]` if absent or `["present", exact_xml_parsed_string]` if present.
Preserve empty strings, case, whitespace, and Unicode without normalization.
Names/device descriptions are exported metadata, not verified device identities.

Canonical JSON bytes are `json.dumps(value, ensure_ascii=True, sort_keys=True,
separators=(",", ":"), allow_nan=False).encode("ascii")`. Strings use that escaping;
all counts are integers, never booleans/floats. Canonical content has exact keys:

```json
{
  "catalogue_schema": "mesh-health-catalogue-v1",
  "archive_sha256": "64 lowercase hex characters",
  "archive_bytes": 123,
  "xml_member": "apple_health_export/export.xml",
  "record_count_all_types": 3,
  "families": {
    "steps": [],
    "hrv_sdnn": [],
    "sleep": []
  }
}
```

Each family list contains groups with exact keys `group_id`, `descriptor`, and
`record_count`. `record_count` is positive. `group_id` is the lowercase SHA-256 of
canonical `{"family": family_name, "descriptor": descriptor}`. Sort each list by
canonical descriptor bytes. No duplicate descriptors or group IDs. Family counts
sum to at most `record_count_all_types`. All three families always exist, possibly
empty. The source-name slot may be missing/empty in the catalogue.

The on-disk envelope has exactly `catalogue` (the content above) and
`catalogue_sha256` (SHA-256 of canonical content). No timestamp or encounter-order
alias enters either identity. File formatting may be indented; JSON readers reject
duplicate object keys and NaN/Infinity before structural or digest checks. Unknown
fields are rejected at every defined object level.

No external XML resources may be fetched. Reject an external document type or an
unresolved external entity reference; an internal Health export DTD is allowed.
Malformed/unsupported encoding is a controlled failure. Memory grows with distinct
descriptor groups and current XML nesting, not the number of completed records.
Read via one open archive handle; compare full archive hashes and size before and
after parsing and reject observable input changes. This detects changes; it is not
a claim of an operating-system snapshot against a concurrent malicious writer.

## Selection request and validated record

A request is a private JSON file with these exact keys:

```json
{
  "selection_schema": "mesh-health-selection-request-v1",
  "archive_sha256": "64 lowercase hex characters",
  "catalogue_sha256": "64 lowercase hex characters",
  "selections": {
    "steps": {
      "selection_handle": "00000000-0000-4000-8000-000000000001",
      "group_ids": ["64 lowercase hex characters"],
      "source_scope": "app_stream",
      "user_choice": "The user's explicit preference, recorded locally",
      "identity_evidence": "What local metadata supports this grouping"
    }
  }
}
```

`selections` is a nonempty subset of the three families. A missing family is
unselected, never an implicit fallback. Each handle is a distinct canonical
lowercase UUID version 4. Each group list is nonempty, unique, and sorted by group
ID, and every member belongs to the specified family's recomputed catalogue.
Reject unknown/cross-family IDs, stale digests, extra fields, and wrong types.
Selected groups must have a nonempty (not whitespace-only) source name. Do not
infer a grouping from matching names or promote an empty preference into a choice.
`source_scope` is `app_stream` or `device`; `user_choice` and `identity_evidence`
must be nonblank strings. These are recorded human assertions, not machine proof.
Several groups in one selection require explicit human review that they represent
the intended single source scope; the validator cannot establish that assertion.

The validator re-reads the ZIP and recomputes the entire catalogue, then compares
it with the persisted catalogue read-only. It never repairs either input. Once the
request's structure, archive/catalogue identity, membership, and handle constraints
pass, the output has exact top-level keys `binding` and `binding_sha256`.
`binding` has exactly:

- `binding_schema`: `mesh-health-selection-binding-v1`
- `catalogue`: the verified full catalogue content, preserving local decision inputs
- `catalogue_sha256`: its canonical digest
- `selection_request`: the validated request exactly as parsed
- `validation_scope`: `archive_integrity_and_selection_membership`
- `device_identity_verified`: false
- `user_intent_verified`: false
- `health_source_priority_verified`: false

`binding_sha256` hashes canonical `binding`. This is a mechanically validated
selection record, not verified physical-source identity or proof of human consent.
The operator must have actually received the user's preference before producing a
real binding. A typed evidence sentence cannot enforce provenance or truth.
If the user's requested device remains ambiguous, leave that choice pending.
Unselected families and unresolved-source catalogue groups stay visible in the
embedded catalogue; the later evaluator must apply the policy's refusal rules.

A new archive requires a new catalogue and explicit reviewed request. An identical
input rerun produces identical substantive catalogue/binding content. No values are
combined across archives and no existing fact store is opened.

## Commands, private outputs, and verification

Python 3.13 standard library only. Proposed public commands:

```text
python spikes/health_catalogue/catalogue.py build --archive FILE --output FILE
python spikes/health_catalogue/catalogue.py bind --archive FILE --catalogue FILE --request FILE --output FILE
python spikes/health_catalogue/verify_catalogue.py --archive FILE --catalogue FILE [--binding FILE]
```

The builder and binder create a new private output file with mode 0600, fail if it
exists, and never overwrite input. Parent directory must already exist. Resolve
paths and refuse output inside the code checkout, including symlink aliases.
The same output guard applies to synthetic CLI usage: tests use temporary external
directories. Write no output artifact on parse/validation failure; remove a newly
created incomplete artifact on a write error. All input files remain read-only.
No source strings, paths, raw values, or arbitrary parser/OS exception text go to
stdout/stderr. Successful builder/binder stdout is fixed JSON with `status: PASS`
and `operation: build|bind`; failures use `status: FAIL` and fixed error codes.
These statuses attest only the operation named. Expected failures exit 2; success
exits 0. Unexpected programmer exceptions must not become successful results.

The independent verifier reads this contract and inputs, not primary code, helpers,
fixtures, or generated expected results. Use a separately implemented streaming
parser/accumulator and independently implement canonicalization, structure, and
membership checks. It must not import `catalogue.py`. It compares every descriptor
and count, archive identity, catalogue ordering/digests, and (if supplied) binding
content/membership/digest. It reads only; it cannot create or repair a binding.
Verifier stdout includes fixed `status`, `checks` and `diagnostics` fields plus
`verification_scope: archive_catalogue_and_optional_binding_membership`.
On any failed check it exits 2; on complete success it exits 0. Diagnostics expose
fixed codes only. PASS does not validate intent, physical device identity, Health
totals, coverage, overlap policy, or any production feature.

Synthetic tests must cover missing vs empty, case/Unicode distinctions, order
independence, unknown sleep categories, repeated occurrences, ignored unrelated
types, malformed/ambiguous inputs, no metadata leakage in failures, output safety,
read-only tamper detection, wrong counts/digests/membership, empty or duplicate
selections, unselected families, bogus human assertions not being attested, and
repeat runs. Test files are authored independently for the second implementation.
Real inputs/catalogues/requests/bindings/results are private evidence, never fixtures.
