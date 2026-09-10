# Health export observation adapter boundary

Status: **PROPOSED DESIGN — no adapter or storage implementation accepted**.
Applies to the steps observation path reviewed at `46b217a`. Accepted RC2, its
archives and tags, and all existing production behavior remain unchanged.
Real export records and investigation evidence are private and are not examples
or fixtures in this document.

## 1. Decision and why the current interface cannot be reused

Introduce a `HealthExportObservationAdapter` that produces a verified, immutable
`HealthObservationBatch`. Persist and read those batches through a separate
private observation-store interface. Do not adapt them into `FetchResult`, register
them as RC2 production metrics, or pass their numeric rows to `weekly_report`.

This preserves useful selected-source recorded values without claiming complete
days. The existing implementation ties these concepts together:

- [`FetchResult` and `apply_fetch`](../../../tools/collector.py) map supplied
  `observed` values into numeric facts and `observed` availability. `undetermined`
  is not persisted as a daily observation and cannot retain a blocked day's reasons.
- [`observations`](../../../tools/store.py) requires a non-null number, recognizes
  measured/derived/imputed quality, and has no calculation-state or completeness
  contract. Adding a quality label does not supply the missing semantics.
- [`stdio_leaf`](../../../tools/stdio_leaf.py) counts observed facts as present;
  [`report.py`](../../../tools/report.py) uses numeric present days toward its
  weekly validity threshold. It does not inspect Health completeness flags.
- [`mesh_config`](../../../tools/mesh_config.py) registers only the Git adapter;
  its definition construction and measured capability are Git-specific.

Consequently, inserting a finite Health value and attaching `unknown` somewhere
in metadata would still allow that value to count toward a valid comparison.
The boundary must be enforced by incompatible types, storage paths, and consumers.

| Approach | Consequence | Decision |
|---|---|---|
| Reuse existing facts with extra metadata | Current consumers count numbers before evaluating Health coverage; null states lose detail | Reject |
| Add observation tables to a new version of the main store and update all consumers | Can be correct, but requires coordinated schema, migration, registration, gateway, and report changes | Defer to a separately reviewed integration |
| Separate typed batches and private observation storage | Existing consumer eligibility remains untouched; explicit batch reads avoid implicit history stitching | Adopt for the next bounded milestone |

The extra boundary is deliberate. It costs a later integration step and a
separate storage contract; it is not evidence that multiple databases or processes
are intrinsically necessary for the final product.

```mermaid
flowchart LR
    I[Private archive, catalogue, binding] --> E[Standalone evaluator]
    E --> V[Independent full replay]
    I --> V
    V --> A[Adapter admission and definition check]
    A --> B[Immutable observation batch]
    B --> S[Private observation store]
    S --> R[Explicit single-batch reader]
```

These nodes are responsibilities, not required separate processes. There is no
edge from this path into the current numeric-fact collector or weekly reporter.

## 2. Meaning of a batch

The adapter's input is the local archive, full catalogue, explicit steps binding,
and operating window/timezone inputs used by the standalone contract. It has no
source-priority setting, fallback, inferred source selector, deduplication switch,
coverage threshold, or recommendation threshold. Only the steps report is produced;
other catalogue/binding families remain preserved context, not implemented HRV or
sleep metrics. Unsupported report metrics or versions are refused.

`prepare_observations(input_set, expected_definition)` conceptually returns either
one prepared import (a `HealthObservationBatch` plus its verification receipt) or
a run-level failure. These names define the
interface responsibility, not a claim that callable functions exist today.

A batch contains:

- The entire unchanged `mesh-steps-report-v1` report envelope, including every
  date, null, reason, count, boundary, and uncertainty flag. Preserve the catalogue
  and binding privately so its source membership can be reproduced.
- A complete value definition and its domain-separated digest.
- Immutable input identities and the report digest. Implementation/runtime identity,
  timing, and verifier results belong to the separate execution receipt; they are
  not embedded as mutable fields in the deterministic batch document.
- A deterministic batch identity defined below. It represents one observation
  result under one input set; it is not a set of mergeable biological samples.

A fully verified report containing only blocked or absent dates is still a valid
observation batch; a run-level parser, identity, or verifier failure is not. Keep
these outcomes distinct. The adapter declares no measured live-source retention,
full export completeness, or unbounded source capability merely because a local ZIP
can be reread. It writes no RC2 source-state/capability row.

No duplicate daily projection is authoritative alongside the report. The first
reader returns the stored report as a whole. A later indexed projection must be
proved to reconstruct it exactly before it can become a source for any query.

The adapter must run the existing strict validation and independent recomputation
path. A digest, a caller-supplied `verified: true`, or an earlier PASS log does not
substitute for replay. Verification success means exact reproduction of this
observation contract, not validated physical-device identity, source priority,
complete measurement, or independence of shared Python primitives.

## 3. Coverage is an invariant, not a heuristic

Apply this table to every imported date. Do not drop noncomputed dates.

| Report state | Stored value | Preserved interpretation | RC2 mapping |
|---|---|---|---|
| `computed` | Same finite binary64 value, including explicit positive zero | Recorded quantity from the selected exported source | None |
| `no_records` | null | No selected occurrences assigned to this date in this archive | None; never `known_absent` or structural zero |
| `blocked` | null | Preserve every determinable reason, count, and diagnostic | None; do not fall back to a previously computed date |
| `no_civil_day` | null | Preserve pinned skipped-date boundaries and zero count structures | None; not source failure or permanent data loss |

For all states, enforce exactly:

```text
observation_completeness = "unknown"
valid_metric_day = null
inference_eligible = false
```

A positive count, long sample span, high daily value, many computed dates,
agreement of two implementations, or successful XML parsing cannot alter these
fields. The count of computed dates is not `present_days`, `valid_days`, coverage
percentage, or a sufficient-week test. An explicit recorded zero does not establish
a zero-activity day. No new day-validity cutoff is chosen in this design.

Repeated-record candidates remain blocked even when source, interval, unit, and
value match. A different creation timestamp does not license selecting the newest
record; equality of all exported attributes would not prove biological identity
either. Keep occurrence locators in private forensic evidence. Do not add a
production identity or deduplication rule based on one real-data investigation.

## 4. Definition identity versus input provenance

Define a separate `health-observation-definition-v1` identity. Do not reuse RC2's
Git definition builder or silently activate a Health definition in its registry.
The canonical definition includes all value-changing interpretation choices:

- Metric `selected_source_recorded_steps`, unit `count`, and observation schema.
- `source_scope` and the exact finite sorted selected descriptor group IDs, with
  the catalogue descriptor/hash scheme version. A source name alone is inadequate.
- Explicit timezone key and pinned TZif digest; the actual rules, not just the
  label, affect assignment.
- Steps policy version, timestamp grammar, point/interval and day-allocation rules,
  unresolved-source and invalid-record rules, repeat-key and collision policy,
  estimator, and exact-sum/final-rounding rule. The supported version maps to one
  complete documented rule set; unknown versions are refused.

Use SHA-256 of the canonical JSON object
`{"kind":"health-observation-definition-v1","payload":definition}`. Digests are
64 lowercase hex characters. Canonical encoding is the existing compact,
ASCII-escaped, sorted-key JSON with finite values and no duplicate keys. Validate
structure and semantic content before computing identity; a matching hash does not
make an unsupported definition acceptable.

The adapter derives the actual definition from the freshly verified steps binding,
the verified report's actual pinned timezone bytes/key, and a supported constant
rule-set mapping. It must not accept an arbitrary caller-supplied definition as an
assertion about those inputs. Require full canonical-content equality of derived
actual definition and configured expected definition, not just two supplied IDs.
In D2, require both to equal the immutable stored definition under the writer lock.
A rule-version/field mismatch or false claimed selector is a definition conflict
before any store write or success-shaped prepared-import output. Private scratch
used during evaluation is not a published batch and is cleaned on refusal.

Archive bytes/hash/member, catalogue and binding content/digests, selection handle,
and requested window belong to immutable input provenance. Producer/verifier code
identities, runtime/dependency identities, operational resource envelope, timing,
and results belong to separate execution receipts. A
path, UUID, code commit, or presentation label alone does not define the statistic.
Changing a verified catalogue's unrelated groups must not change the steps value
definition. Changing selected group membership or timezone rule bytes must.

An observation store is initialized for one complete definition. Before any attempt
or batch write, compare the configured definition with that stored definition.
A mismatch produces a typed `definition_conflict` with both identifiers and field
names; the store remains unchanged. Reverting the inputs or using a new
observation-store directory is the supported remediation. No migration, activation,
normalization overwrite, or fallback to an earlier definition is implemented.

A new archive always requires its own catalogue and explicitly reviewed binding.
Never infer continuity from the old source name. If it has the exact same selected
group set and interpretation rules, it may share a definition, but its observations
still form a separate batch. New selected descriptor variants change the definition;
that conservatism is intentional until source continuity is separately designed.

## 5. Deterministic result identity and immutable retention

Use a batch identity independent of collection time and local file paths:

```text
batch_id = SHA256(canonical({
  "kind": "health-observation-batch-v1",
  "payload": {
    "definition_id": definition_id,
    "archive_sha256": archive_sha256,
    "catalogue_sha256": catalogue_sha256,
    "binding_sha256": binding_sha256,
    "report_sha256": report_sha256
  }
}))
```

The report digest includes its complete window, pinned timezone rules, fixed
producer/policy versions, counts, states, and values. The same admitted result is
idempotent: a second import of the same batch never adds quantities or daily rows.
A repeated verification can add a separate attempt receipt without changing the
batch or its identity. Operational receipt fields are excluded from the immutable
batch document, not merely from its digest. The batch document contains only its
version, identity, complete definition, archive identity, catalogue and binding
envelopes, and report envelope. These contents are deterministic under the identity
inputs above. A different immutable body with the same proposed identity is an
integrity failure, not an overwrite opportunity.

Retain whole batches, including overlapping windows and archives, independently.
There is no automatic latest-per-day view, cross-archive concatenation, backfill,
archive replacement, or precedence by creation timestamp. Reads require an explicit
`(observation_store_id, batch_id)` and return exactly one batch. Listing is metadata
only and must not select or merge a result for the caller. These handles are not
MCP sessions and confer no authenticity by themselves.

An older computed date and a newer blocked date remain visible in their respective
batches. Reading the newer batch cannot borrow the older value. A future coherent
series view requires an explicit replacement/continuity contract and independent
acceptance; it is outside this design.

## 6. Storage and publication boundary

Use a new private `observation_store_dir`, distinct from every RC2 store and from
code checkouts, including symlink aliases. The initial storage interface supports
initialize, commit a verified batch, list batch metadata, and read one exact batch.
It never calls `Store.publish`, `apply_fetch`, metric registration, or Git sync.
Do not widen `ADAPTERS` or enable Health in current `mesh sync` for this milestone.

The first implementation should use immutable batch documents and a small immutable
store-definition manifest. The manifest has a random store-instance identifier and
complete bound definition; it is created once. There is no mutable current-batch
pointer. Each batch document contains its unchanged report and private reproduction
metadata; any auxiliary input storage must be identity-addressed and immutable.
Before publishing a batch, retain and hash-check a store-owned immutable copy of
its archive, keyed by the archive digest, along with the embedded catalogue, binding,
and pinned rule bytes. Reuse an existing archive object only after byte-identity
validation; this is archive-byte reuse, not sample deduplication. External archive
paths are input locators, not promises of retention or integrity. Missing/corrupt
linked evidence is an explicit integrity failure. No committed inputs or batches
are automatically reclaimed in this milestone.

Serialize initialization and writes with a store-scoped process/thread lock; reject
nested lifecycle mutation. Validate the complete candidate before making it visible.
Publish via a same-filesystem staging path and atomic no-clobber publication under
the writer lock. Never replace an existing batch. Readers consume one complete file
through one open handle and verify its identity and content relationships. No
multi-batch report is produced, so a generation token for fan-out is not needed.
Do not claim crash durability from atomic namespace visibility alone.

Every attempt under the matching definition has a distinct attempt ID and explicit
outcome. Keep acquisition/replay failure separate from existing successful batches.
A failed attempt cannot return old data as if it were its result. Commit accepts
only a prepared import verified during this operation, never a caller-provided
PASS receipt; copied inputs must still match the verified bytes. All writer-side
manifest/definition checks are repeated while holding the publication lock.

The atomic publication of one immutable successful-attempt receipt is the commit
point. That receipt names the store, batch, canonical body digest, linked input
identities, and exact verifier execution. All named objects must already be complete,
verified, and flushed in their final immutable locations. Only a batch named by a
valid committed receipt may appear in list/read results; the mere existence of a
batch file is insufficient. Readers validate the receipt and all named identity
relationships. A second import reuses identical batch/input objects and publishes
a new receipt, not another daily quantity. This ordering avoids claiming that two
independent file renames form one atomic transaction.

Files staged or copied before that receipt are uncommitted objects. They cannot be
read as successful imports or automatically adopted during recovery. An interruption
can leave such objects; recovery verifies/reuses their bytes only during a new full
admission attempt. Committed objects are never cleaned up. Initialization and receipt
publication require no-clobber writer serialization and directory-flush handling;
the exact wire layout and OS-specific operations must be fixed and tested in D2.

If publication outcome is uncertain, recovery resolves the attempt ID against its
committed receipt and linked objects before retry; return `publication_unknown`
rather than claiming that nothing was written. If writing a failure receipt also
fails, report storage failure and do not claim durable recording. Definition
conflicts are refusals before all store writes, so they have a private caller-side
receipt, not a stored attempt. A safe refusal during recovery may remain unresolved
until the original inputs are available; never reinterpret it as a successful run.

Code/receipts/logs must expose no raw source names, device attributes, raw record
content, filesystem paths, or arbitrary exception text in public output. Use fixed
error codes and private path identifiers. Private directories are mode0700, files
mode0600. Public examples and tests use invented data. A copied binding's human
assertions remain assertions, not software-verified identity or consent.

Implementation must specify and test initialization races, no-clobber behavior,
partial writes, locks, checksums, failed receipt writes, and cleanup/recovery before
claiming storage acceptance. Backup/restore and APFS crash recovery remain explicit
release gates. This design does not implement them or convert RC2's outstanding
platform gates into passes. The pure preparation deliverable below does not require
persistent publication and cannot claim it.

## 7. Consumer and promotion boundary

The observation reader is separate from the current leaf `fetch` contract. It
returns the unchanged observation report and definition/batch identity, never a
numeric `observed` map, RC2 `Coverage`, or `ReportPayload`. A future public MCP tool
for this reader requires its own input/output schema and wire acceptance tests;
no new tool is advertised by this design or its first implementation deliverable.

There is no promotion API in this milestone. A later production daily-metric path
must provide all of the following before any selected export observation can count
toward a comparison:

1. A reviewed per-metric completeness/valid-day contract with independent evidence
   for each admitted date. Sample presence and successful calculation are insufficient.
2. A separate value and coverage definition with explicit compatibility, provenance,
   read/write guards, and safe migration/new-store behavior.
3. Consumers that select only that definition and independently established valid
   dates; no path may treat numerical presence as sufficient coverage.
4. Adversarial tests proving unknown/blocked/absent/skipped observations cannot leak
   into valid-day counts, weekly baselines, deltas, or meaningful flags.

Any inferential use additionally requires the unchanged statistical calibration,
hypothesis-family, threshold/provenance, and release gates. Descriptive observation
browsing does not waive them. If this export cannot establish day completeness,
production complete-day metrics remain unavailable; storing useful observations
is still a valid outcome.

## 8. Independently verified deliverables and acceptance

This document defines the architecture. It contains no executed claim about the
future adapter or store. Implement in small deliverables:

**D1 — pure preparation and admission contract.** Specify executable JSON/type and
semantic contracts for the definition, batch, receipt, and safe failures. Wrap the
existing evaluator and independent replay without changing them. Return a verified
batch in memory/private result output, with no observation-store or RC2 writes.
Independently test input immutability, full-field preservation, identity separation,
and exclusion from existing consumers. This is the next implementation milestone.

**D2 — private immutable persistence.** Define the exact directory/manifest/receipt
wire layout and recovery protocol, then implement commit/read/list against it.
Require independent storage, concurrency, interruption, and failure acceptance.
Do not call mere successful file creation durability or backup/restore.

**D3 — observation presentation.** Add a separate reader/UI or MCP contract only
after D1/D2 acceptance. Display recorded quantities and null reasons; do not use
existing weekly comparison logic or silently select a batch.

Mandatory acceptance examples, built from invented records:

| ID | Trigger | Required result |
|---|---|---|
| HA-01 | Computed positive value or explicit recorded zero | Exact value retained; completeness unknown, valid day null, inference false |
| HA-02 | No selected records, with or without phone records | Null/no_records preserved; no known_absent, structural zero, or fallback |
| HA-03 | Same source/interval/unit/value with different creation times | Both occurrences retained; repeat and overlap reasons survive; null/blocked |
| HA-04 | Selected timestamps cannot be assigned; skipped civil date also in window | Family and daily precedence preserved exactly; no invented daily assignment |
| HA-05 | Change value, reason, count, boundary, flag, or type and recompute report hash | Fresh independent replay refuses; no batch publication |
| HA-06 | A matching old PASS receipt plus changed archive/binding/report | Refuse; previous receipt cannot attest new bytes |
| HA-07 | Same verified batch imported twice | One immutable batch; quantities not appended; attempts separately identified |
| HA-08 | New batch blocks a date computed by an earlier batch | Both retained; explicit new-batch read returns null, never old fallback |
| HA-09 | Change selector, rule version, or pinned timezone bytes; or submit an expected definition that describes different verified inputs | Derived actual definition must equal expected and stored content; conflict before store write/prepared-result success; new-store remediation is testable |
| HA-10 | Change only window/archive/path/receipt time/unrelated catalogue groups | Definition stable if selected groups/rules remain identical; batch identity follows provenance; path/time alone do not change it |
| HA-11 | Feed a batch to existing collector/report/leaf/config paths | Rejected/unregistered; no Health numeric fact, present-day count, baseline, or comparison produced |
| HA-12 | Many computed dates with high counts and long sample spans | Coverage flags remain unchanged; no threshold can enable eligibility |
| HA-13 | Partial write, invalid checksum, concurrent duplicate import, or nested initialization/destruction | No partial or replaced batch visible; errors and surviving prior content independently verified |
| HA-14 | Last acquisition fails, old successful batch exists | Attempt failure explicit; old batch accessible only by its separate identity |
| HA-15 | Read-only query concurrent with publication of another batch | Return one complete requested identity or explicit failure, never a mixture |
| HA-16 | Public files/logs or permission failure | No private payload/path leak; fixed failure; incomplete output not advertised |
| HA-17 | Core preservation and existing suites | RC2 artifacts/tags untouched; previously accepted semantics and existing tests preserved |

D1 covers HA-01–06, HA-09–12 at its preparation boundary, and HA-16–17.
D2 completes the persistence portions of HA-07–10 and HA-13–17.
D3 adds explicit reader/wire cases to HA-11–16. Every deliverable requires an
independent reviewer and a reproducible artifact before acceptance; a design table
or green unrelated tier is not executed evidence for any of these cases.
