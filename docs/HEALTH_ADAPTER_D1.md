# Health observation preparation (D1)

D1 provides pure preparation in `adapters.health_observations`. It does not implement
an observation store, durable publication, retention, readers, MCP wiring, RC2
registration, coverage promotion, calibration or inference. A prepared result is
not authorization for a later D2 writer to trust a prior PASS. Every future commit
requires its own admission and storage acceptance. Existing spike and RC2 files
are unchanged.

## Public interface and private CLI

```python
from pathlib import Path
from adapters.health_observations import InputSet, inspect_definition, prepare_observations
inputs = InputSet(Path('/private/input/archive.zip'), Path('/private/input/catalogue.json'),
                  Path('/private/input/binding.json'), 'Etc/UTC', '2026-01-03', 1)
definition = inspect_definition(inputs)  # full evaluation AND replay; review explicitly
prepared = prepare_observations(inputs, definition)
# immutable bytes: prepared.batch_bytes, prepared.receipt_bytes, prepared.private_bytes
# detached decoded copies: prepared.batch, prepared.receipt
```

The API requires explicit timezone/as_of/days and an explicit expected definition.
An optional `report=Path(...)` on InputSet accepts an existing report for fresh
replay. No receipt or verified boolean parameter exists. The frozen result contains
only immutable bytes; decoded accessors return defensive copies. Constructing a
result or validating a serialized receipt does not prove an execution occurred.

From the checkout root, using the project's locked Python environment:

```sh
python -m adapters.health_observations inspect-definition --archive /private/input/archive.zip --catalogue /private/input/catalogue.json --binding /private/input/binding.json --timezone Etc/UTC --as-of 2026-01-03 --days 1 --output /private/results/definition.json
python -m adapters.health_observations prepare --archive /private/input/archive.zip --catalogue /private/input/catalogue.json --binding /private/input/binding.json --timezone Etc/UTC --as-of 2026-01-03 --days 1 --expected-definition /private/results/definition.json --output /private/results/prepared.json --failure-output /private/results/failure.json
```

Both operations accept optional `--report /private/input/report.json`. Inspection
writes the bare derived definition and never publishes a prepared batch. Inspection
does not choose sources, establish user intent, or admit a batch. Review the existing
explicit binding and inspected content before configuring it as expected. Preparation
compares freshly derived content in full; it does not accept a supplied identity as
an assertion. For a conflict, retry with the original matching inputs or explicitly
review/configure a new definition. Future storage must use a new store for a new
definition; D1 implements no migration or activation.

Output parents must already exist with mode 0700 outside code checkouts. Files are
mode 0600. Existing outputs, symlink aliases to inputs, and checkout destinations
are refused. A complete temporary inode is linked exclusively to its destination;
no old file is overwritten. This is private artifact output, not a durability claim.
Optional `--failure-output` writes the private typed failure only on refusal. A
failure-output write failure is reported as OUTPUT_INVALID, never durable recording.
Public stdout contains only `{status, operation}` on success or
`{status, operation, error}` on failure. All values are fixed. Stderr carries no
private input/path/exception diagnostics. Exit 0 means completed preparation or
inspection; exit 2 means refusal. Argument parsing failures use operation
`health_observations`; otherwise operation is `inspect-definition` or `prepare`.

## Definition content and rule mapping

The definition has exactly these fields:

| Field | Value |
|---|---|
| schema_version | `health-observation-definition-v1` |
| metric | `selected_source_recorded_steps` |
| unit | `count` |
| observation_schema | `mesh-steps-report-v1` |
| source_scope | Verified binding steps selection's `app_stream` or `device` |
| selected_group_ids | Exact nonempty finite sorted steps group-ID set |
| catalogue_descriptor_version | `mesh-health-catalogue-v1` |
| descriptor_hash_version | `sha256_canonical_family_tagged_descriptor_v1` |
| timezone | `{key, tzif_sha256}` from the actual verified report |
| rules | Complete fixed mapping below |

Descriptor hashing means SHA-256 of compact ASCII-escaped sorted-key JSON
`{family, descriptor}`. Descriptor slots are the catalogue's tagged raw sourceName,
sourceVersion and device values, preserving missing versus present strings.

| Rule field | Exact value | Interpretation |
|---|---|---|
| policy_version | `mesh-selected-steps-v1` | Only the existing steps policy is supported |
| timestamp_grammar | `ascii_offset_required_microseconds_v1` | Exact ASCII offset-required grammar in HEALTH_STEPS_CONTRACT, at most six fractional digits; checked integer UTC microseconds |
| civil_day | `pinned_tzif_contiguous_utc_preimage_v1` | Exact pinned-rule UTC preimage; empty skipped dates retained, disjoint or unrepresentable windows refused |
| point_interval | `half_open_intervals_points_at_start_v1` | Positive intervals [start,end); zero-duration point at start |
| day_allocation | `whole_interval_single_day_no_proration_v1` | Whole interval must fit a single day; cross-day selected quantities block assigned days |
| unresolved_source | `archive_family_block_v1` | Any unresolved steps source blocks ordinary dates archive-wide |
| invalid_record | `unattributable_family_bad_quantity_assigned_days_v1` | Selected unattributable time blocks family; bad selected value/unit blocks assigned days, defects counted independently |
| repeat_key | `family_descriptor_utc_endpoints_tagged_raw_unit_value_v1` | Creation time excluded; raw unit/value preserve missing and exact parsed strings; endpoints compare UTC instants |
| repeat_policy | `retain_all_block_daily_extras_v1` | No removal or representative; extras count and block each affected day |
| collision_policy | `all_selected_intervals_points_block_no_precedence_v1` | All selected groups and bad quantities participate; interval/point collisions block; point at excluded end does not collide |
| estimator | `sum_selected_allocatable_valid_quantities_v1` | Sum only otherwise computable selected quantities; retain all reasons, counts and diagnostic uncertainty |
| numeric_rule | `exact_rational_sum_binary64_nearest_even_positive_zero_range_guard_v1` | Exact accumulation, final ties-even binary64; explicit zero positive; positive-to-zero underflow and infinite result block, finite subnormals valid |

The full authoritative details remain [HEALTH_STEPS_CONTRACT.md](HEALTH_STEPS_CONTRACT.md).
The adapter does not reimplement numeric or timezone evaluation. Unknown rule versions
or modified rule mappings are invalid definitions. Timezone key must match the request;
actual pinned bytes are replayed, and their digest becomes definition content. Supplied
reports may intentionally pin rules different from the host database; their explicit
expected definition must match those actual bytes. No post-replay host TZif read
substitutes for the report's provenance.

Definition identity is SHA-256 of canonical JSON
`{"kind":"health-observation-definition-v1","payload":definition}`. Canonical JSON
is compact, sorted-key, ASCII-escaped, finite, with no duplicate keys. Every digest
is 64 lowercase hexadecimal characters. Paths, window, archive, unrelated catalogue
groups, handles, resource limits and execution timing are excluded from definition.

## Wire contracts and validation

Executable Draft 2020-12 schemas are exported as `contracts.SCHEMAS` for `definition`,
`batch`, `receipt`, `failure`, and `prepared`; `Validator` uses exact Python integer
types (JSON Schema's usual acceptance of `1.0` as integer is insufficient). Public
semantic validators are `validate_definition`, `validate_batch`, `validate_receipt`,
`validate_failure`, `validate_prepared`. `strict_load` rejects duplicate keys and
nonfinite JSON numbers. These validators return their input after validation and
raise fixed `ValueError` on invalid content. They are structural/relationship gates,
not independent archive replay and never proof of admission.

Prepared private output is exactly `{schema_version:"health-observation-prepared-v1",
batch, receipt}`. Batch fields are `schema_version`, `batch_id`, `definition_id`,
`definition`, `archive`, `catalogue`, `binding`, `report`. The last three preserve
complete private envelopes with their canonical digest fields. Archive is exactly
`{sha256, bytes, xml_member}`. No duplicate daily projection or operational fields
are embedded. The report retains every date, float/null, count, reason, boundary and
uncertainty flag. For every date completeness is unknown, valid_metric_day null,
and inference_eligible false. All-blocked or all-absent reports can be valid batches.

Batch identity is SHA-256 of canonical JSON with kind
`health-observation-batch-v1` and payload containing exactly `definition_id`,
`archive_sha256`, `catalogue_sha256`, `binding_sha256`, `report_sha256`. Catalogue,
binding and report digests are canonical content identities, not source-file byte
hashes. The latter serve only mutation detection. Reformatting identical JSON or
moving identical files leaves deterministic batch bytes unchanged. New provenance
or window changes batch identity. There is no merge, latest-day view or biological
record identity.

Receipt fields are `schema_version` (`health-observation-verification-receipt-v1`),
`attempt_id` (fresh UUID4), `batch_id`, `batch_sha256` (canonical full body),
`identities` (the exact batch-identity payload above), `outcome` (`PASS`),
`verification_scope` (`selected_steps_report_exact_recomputation`), `checks`
(`["binding_and_archive","pinned_timezone_boundaries","daily_steps_and_diagnostics","report_identity"]`),
`started_at`, `finished_at` (UTC ISO timestamps), `input_mode` (`evaluated` or
`supplied_report`), `code_sha256`, `adapter_sha256`, `runtime`, `resource_envelope`.
`code_sha256` maps eight repository-relative names to observed content hashes:
`health_catalogue/catalogue.py`, `health_catalogue/verify_catalogue.py`,
`health_steps/evaluate.py`, `health_steps/steps_core.py`, `health_steps/steps_time.py`,
`health_steps/verify_steps.py`, `health_steps/oracle_core.py`, `health_steps/oracle_time.py`.
`adapter_sha256` hashes the canonical filename-to-hash map of this package's Python
files. Runtime has `implementation`, `version`, `executable_sha256`, `expat_version`, `jsonschema_version`.
Resource envelope records `subprocess_timeout_seconds` (300),
`value_token_characters` (string `4096`), `exponent_magnitude` (string `100000`).
These are operational metadata, never coverage criteria. Code/runtime hashes are
observed local identities, not authenticated provenance or an invented commit.

A typed failure is `{schema_version:"health-observation-failure-v1", status:"FAIL",
error, conflict}`. Normally conflict is null. DEFINITION_CONFLICT has a private
`conflict` object containing `actual_definition_id`, `expected_definition_id`, and
sorted `differing_fields` using only fixed top-level definition field names. The
public error contains none of these private identities. Malformed or unsupported
expected definitions produce DEFINITION_INVALID. Failure never contains a prior batch.

## Input coherence and limitations

Every operation makes private 0700 scratch with exclusive 0600 snapshots, cleaned
on every success/refusal. Originals are not rewritten or chmodded. Each original
regular file is hashed through one open handle; inode, size, mode, mtime,
resolved target and symlink identity are checked across capture and admission.
Change-time (ctime) and access-time are not content identities: filesystem metadata
updates can change them while bytes, locator and the retained attributes stay
identical. Such metadata-only updates do not abort preparation; content SHA-256
is still rechecked, including when size and mtime are preserved.
Original locators must remain unchanged. Snapshot identities are checked across
evaluation and verification. Bytes captured before replay are the only bytes used
for assembly; changes to the report after replay cause refusal. Requested full
window/as_of/days and timezone key must independently match even if standalone
replay would accept a different report. Hash/stat checks detect observed changes;
this is not an OS-enforced lock against hostile same-user concurrent processes.

The adapter invokes the unchanged evaluator and independent verifier as subprocesses
with the running interpreter, avoiding sibling-module pollution. It captures their
fixed responses privately and returns only fixed failure codes. Semantic validators
check exact scalar types, envelope digests, selected membership, catalogue descriptor
hashes/order, definitions, input relationships, count bounds, date sequence, states,
coverage invariants and receipt relationships. Fresh verifier replay independently
recomputes pinned boundaries, source partitioning, count unions, collision/repeat
logic, quantities and all report fields; schemas/hashes cannot replace that work.

The separate result type supplies no observed-value map, RC2 coverage or report
payload. Existing collectors/configuration remain incompatible. No statement here
claims D2 crash recovery, backup/restore, durable idempotency, shared-history
continuity, physical identity, consent, source priority, complete measurement,
statistical calibration or inferential eligibility.

Synthetic test command:

```sh
python -m unittest discover -s tests/health_observations -p 'test_*.py' -v
```
