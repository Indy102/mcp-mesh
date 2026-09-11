# Private Health observation storage and recovery — D2

Status: design for implementation; no storage acceptance claimed by this document.
Binding architecture: [Health observation adapter boundary](2026-09-10-health-observation-adapter-design.md).
D1 base: `f60c4e8b24e8ce0860329a3a125b9ff9846792dc`.
This fixes D2's wire layout and recovery semantics. RC2, D1, all existing source
probes, their contracts and tests remain unchanged. Only invented public fixtures.

## 1. Architecture and alternatives

Use a separate `observation_store` package. It imports D1's public preparation and
contract APIs, never its private helpers, and never RC2's store/collector/report.
No new MCP tool, coverage promotion, latest-day view, source selection or D3 UI.

| Choice | Result | Decision |
|---|---|---|
| Immutable files, one successful receipt as visibility point | Fits the binding design; explicit recovery and file validation | Adopt |
| SQLite/transaction database plus external archives | Still needs a cross-file commit protocol and changes the chosen storage boundary | Defer |
| Reuse RC2 store/generation machinery | Couples unknown-coverage observations to existing production consumers | Reject |

D2 owns fresh `prepare_observations(InputSet, expected_definition)` inside every new
import operation. It has no public commit/prepared/PASS argument. D1 prepared
objects and schemas are not execution attestations. Retain D1's immutable batch
bytes unchanged and embed its fresh verification receipt in D2's attempt evidence.
D1 destroys its scratch before returning: D2 copies the source archive afresh and
requires the copied bytes' SHA-256 and length to equal the verified batch. It never
rereads catalogue/binding/report locators to construct a different batch.

## 2. Files, identities and strict contracts

One explicit store directory has this layout:

```text
STORE/                         # 0700; never a code checkout or RC2 directory
  writer.lock                  # 0600; permanent inode, never replaced
  manifest.json                # 0600; immutable initialization visibility point
  archives/<archive_sha>.zip    # raw retained archive bytes, immutable
  batches/<batch_id>.json       # exact canonical D1 batch, immutable
  attempts/<uuid>.intent.json   # immutable verified-but-not-committed intent
  attempts/<uuid>.result.json   # one immutable terminal result
  staging/<uuid>/...            # uncommitted objects; never read as results
```

All directories are 0700 and files 0600; validate types, permissions and owner.
Managed entries must be regular files/directories, never symlinks, FIFOs or devices.
Reject unexpected hard-linked managed files rather than following external aliases.
All managed regular files require exactly one link.
Use checked directory handles and relative no-follow operations to avoid redirecting
writes through swapped descendants. Canonicalize the user locator, reject code
checkout/RC2 ancestors and preserve its opened root identity through operations.
`current.json`/RC2 generation files are not Health store metadata. Initialization
requires a nonexistent destination under an existing private parent; never adopt,
chmod, repair or overwrite an existing directory, including an empty one.

Digests use D1 compact sorted-key ASCII canonical JSON, finite values, no duplicate
keys and lowercase 64-hex SHA-256. UUIDs are canonical UUID4. Schemas have closed
fields and exact scalar types; semantic validators enforce identity relationships.
The module exposes schemas/validators for manifest, intent, result, metadata list,
recovery and safe failure. JSON validation is not replay or authenticated provenance.

Manifest fields: `schema_version=health-observation-store-v1`, caller-supplied
`store_id`, full `definition`, `definition_id`, and
`flush_profile=fsync-files-and-directories-v1`. No mutable current batch or active
normalization. Store ID and definition never change. Unknown versions are refused.

Intent fields: `schema_version=health-observation-intent-v1`, `store_id`,
`attempt_id`, `definition_id`, `batch_id`, `batch_sha256`, `identities` (D1's exact
batch identity preimage), and `verification` (the whole fresh D1 receipt).
The D1 verification attempt UUID remains distinct from D2's caller operation UUID.
The intent's canonical digest is referenced by its terminal result. Standalone
intent validation uses public D1 `validate_receipt(receipt, batch=None)` and
cross-checks its identities/store/definition/attempt linkage without requiring any
orphan archive or batch. COMMITTED validation additionally supplies the batch.

Result fields: `schema_version=health-observation-result-v1`, `store_id`,
`attempt_id`, `definition_id`, `outcome` (`COMMITTED`, `FAILED`, `INTERRUPTED`),
`intent_sha256` (null only for preparation failure before intent), `batch_id`,
`batch_sha256`, `verification` (D1 receipt), `error`, `recorded_at` (UTC timestamp),
`storage_version=health-observation-storage-v1`.
For COMMITTED: intent digest/batch IDs/full verification are non-null and match the
intent and batch exactly; error is null. For FAILED/INTERRUPTED: batch fields and
verification are null, error is a fixed code; intent digest is required when an
intent was published. A failed result never carries an old batch as its result.
These operational fields do not enter D1's deterministic batch or its identity.

Each new import requires an explicit caller-owned `attempt_id` before execution;
the caller keeps it for recovery. No implicit generation/reset of that ID mid-run.
An existing intent or result for it causes ATTEMPT_EXISTS before any new writes:
resolve it with recovery and use a new ID for a fresh import. Repeating the same
verified batch with a new attempt must run replay again, reuse only verified
byte-identical objects, and create a new result without adding daily quantities.

All file classes (manifest, intent, archive, batch, result and private CLI output)
use macOS `renameatx_np(..., RENAME_EXCL)` through checked directory handles for
atomic no-clobber publication. Local `rename(2)` documents EEXIST on an existing
destination; a disposable local probe verified collision preservation and nlink=1.
The supported implementation platform is macOS. Other platforms return
PLATFORM_UNSUPPORTED before initialization/import mutation; no ordinary-rename or
hard-link fallback. Unsupported filesystems fail explicitly. This avoids a
link-then-unlink interruption state entirely; process tests pause immediately after
the exclusive rename and before flush. Namespace publication is not power-loss
proof. Fixed flags come from the platform interface, not guessed portability.

## 3. Initialization and serialization

`initialize(store_dir, store_id, expected_definition)` validates definition, UUID
and private/non-repository destination before mutation. Exclusive mkdir reserves
the nonexistent root; a racing initializer gets STORE_EXISTS. Create the permanent
lock and managed subdirectories; flush completed files/directories and root before
publishing manifest exclusively. Manifest publication is initialization visibility.
Flush manifest, root and parent before returning initialized. Never replace an
existing manifest or lock. Handle all opens/writes/flushes with checked errors.

A killed initializer can leave an uninitialized directory. Other commands return
UNINITIALIZED when manifest is absent; they do not silently finish or delete it.
Use a new nonexistent directory to retry. This is explicit safe refusal, not data
recovery: no imports are accepted before the manifest exists. If manifest linkage
may have happened but its final flush failed, return INITIALIZATION_UNKNOWN;
explicit initialization-status verification validates the complete layout and can
retry required flushes under the existing writer lock before reporting initialized.
A malformed existing manifest/layout is INTEGRITY_ERROR, never a fresh empty store.

Writer operations acquire a process-local lock keyed by root device/inode, then
an exclusive nonblocking flock on the existing lock inode. All instances/aliases
share that domain. Another thread/process gets WRITER_BUSY; the same thread trying
a nested store mutation gets NESTED_OPERATION, never a deadlock or inner publish.
Hold the lock through preparation and publication; throughput is deliberately
secondary for this local milestone. Release on every normal/error path; kernel
process-death release permits recovery. No destroy, migration, garbage collection,
lock deletion or reentrant mutation API is introduced.

Read-only batch/list operations do not acquire the writer lock. They validate their
explicit store identity and committed chain; they never use pending files or a
current-batch pointer. Locking is advisory and serializes cooperating application
writers, not arbitrary same-user filesystem edits.

## 4. Import order and refusal boundary

`import_observations(store_dir, store_id, input_set, expected_definition, attempt_id)`:

1. Validate request and opened initialized store. Acquire writer lock. Revalidate
   root/manifest/store identity and compare complete expected and stored definition.
   Reject conflict before every store write, including staging/attempt files. The
   existing lock is opened without creation/truncation/chmod.
2. Refuse reused attempt IDs. Invoke D1 fresh using caller inputs and expected
   definition, with D1 scratch outside the store. D1 derives actual definition;
   require its returned full definition to equal expected and stored content under
   the still-held lock. Re-read/validate manifest immediately before the first write.
   A DEFINITION_CONFLICT/DEFINITION_INVALID yields only a caller-side safe failure;
   store bytes/tree stay unchanged. No pending record is written beforehand.
3. Other preparation/acquisition failure under a matching configured definition may
   publish a FAILED result with null intent and no batch. An interrupt during the
   write-free preparation phase can leave no attempt record; do not claim otherwise.
4. Publish and flush the complete immutable intent exclusively. Create private
   staging for this attempt. Copy the archive through one nonblocking regular-file
   handle, hash/count actual copied bytes and detect observed path/content mutation;
   require exact verified digest/length. Do this even if a matching retained archive
   exists. Copy canonical batch bytes from D1's immutable result, not new source reads.
5. Flush staged files; publish archive and batch via same-filesystem exclusive
   rename operations. If a destination exists, validate its regular/private type
   and exact bytes/hash/size; equal reuses, different is INTEGRITY_ERROR. Never
   overwrite. Successful rename consumes the staging name; no committed object is deleted. Flush destination directories.
6. Validate final archive and batch afresh from open handles, validate D1's entire
   batch/receipt and all intent relationships, and recheck root/manifest/definition.
   Flush final linked objects and relevant directories before success publication.
7. Publish one complete COMMITTED result exclusively. This linkage is the **only
   batch visibility commit point**. Then flush result and attempts directory before
   returning COMMITTED. Record a successful reply only after required calls succeed.

A single successful receipt names its complete batch, retained archive, full input
identities and exact verifier execution. Two file renames are never treated as a
transaction. File existence alone is not import success. Overlapping batches stay
independent; there is no archive stitching, numeric append or old-value fallback.

Expected failures before result linkage can publish one FAILED result (referencing
intent if present); preserve partial uncommitted objects for diagnosis/new admission.
Never mask an integrity or storage failure with old data. If failure-result recording
fails, raise STORAGE_ERROR without claiming durable failure evidence. If any terminal
result linkage may have happened, never publish a contradictory second result:
return PUBLICATION_UNKNOWN with the caller's attempt ID and resolve by recovery.
A flush unsupported/failing after linkage is not silently ignored. Initial intent
flush failure is a storage failure; it cannot expose a batch without COMMITTED result.

## 5. Reads, listings and recovery

`read_batch(store_dir, store_id, batch_id)` returns immutable canonical D1 batch bytes
only when at least one fully valid COMMITTED result names it. Validate the
manifest/store/definition, result filename UUID, matching intent digest, whole D1
receipt, exact batch body/hash/ID/definition, and retained archive hash/length/type.
Missing/corrupt named evidence is INTEGRITY_ERROR. Orphan batch without success
receipt is BATCH_NOT_COMMITTED. No alternative or older batch is substituted.

`list_batches(store_dir, store_id)` captures one directory-name enumeration and
validates all terminal records discovered, plus complete chains for every successful
batch before returning deduplicated, sorted metadata: batch_id, definition_id,
archive_sha256, report_sha256 and sorted committed attempt IDs. Fail the entire list
on malformed candidate records/corrupt linked evidence; never silently skip them.
Pending intents with no terminal record are excluded. Unknown managed names are
integrity failures. Concurrent listing may omit a later complete publication; it is
not an atomic snapshot of all store history. Each returned batch is independently
validated; no combined observation report is formed.

`recover_attempt(store_dir, store_id, expected_definition, attempt_id)` acquires the
same writer lock, validates definitions before writes, and follows this table:

| State while exclusively locked | Outcome |
|---|---|
| Valid COMMITTED result and complete valid linked evidence | Reflush required files/directories, then return COMMITTED and exact batch ID |
| Valid FAILED/INTERRUPTED result and valid intent link when non-null | Reflush result/intent directories as needed; return its recorded failure state |
| Valid intent, no terminal result | Publish a new immutable INTERRUPTED result; never adopt/promote staged or final orphan objects |
| No intent and no result | Return NOT_RECORDED; do not claim that historical publication never occurred |
| Malformed/conflicting records, wrong store, corrupt/missing linked committed data | Explicit integrity/identity failure, no repair or fallback |
| Flush/publication outcome still uncertain | PUBLICATION_UNKNOWN, no false success |

NOT_RECORDED is observed absence under lock, not historical proof against deletion
or disk loss. No automatic retry occurs. A new full import with a new attempt ID can
reuse byte-verified orphan archive/batch objects; it runs D1 anew and publishes a new
success. Orphan files/staging are not automatically reclaimed. Committed objects
and attempt evidence are never cleaned up by D2. No backup restoration or inference
from filenames alone is implemented.

## 6. Public/private interfaces and evidence limits

Provide Python API plus `python -m observation_store` commands `init`, `status`,
`import`, `read`, `list`, `recover`. All identify an explicit store ID (and explicit
attempt/batch ID where relevant); no defaults choose data. Definition files are
explicit for init/import/recover. Import inputs/options match D1. Read/list/recover
write results only to explicit new private output files outside the store tree
(including aliases), code checkouts and original input locators. Validate both
result and failure output destinations before any store mutation; a refused output
gets only the fixed public envelope and cannot alter a write-free store refusal.
Public stdout/stderr use
fixed status/operation/error envelopes. Optional private failure outputs contain
fixed errors, safe IDs and definition conflict field names, never arbitrary exception
text. Do not route these through the existing MCP or mesh CLI.

Storage failures include STORE_EXISTS, UNINITIALIZED, INITIALIZATION_UNKNOWN,
STORE_ID_MISMATCH, DEFINITION_INVALID, DEFINITION_CONFLICT, WRITER_BUSY,
NESTED_OPERATION, ATTEMPT_EXISTS, BATCH_NOT_COMMITTED, INPUT_CHANGED,
PREPARATION_FAILED, INTEGRITY_ERROR, STORAGE_ERROR, PUBLICATION_UNKNOWN and fixed
CLI/output refusal codes. Define exact closed error/result schemas in implementation.
Every command/API failure is classified and cannot carry an old result. Output-file
failure after a committed import must report uncertainty with attempt ID, not imply
the store stayed unchanged; recovery resolves it. Private stdout policy also applies
to unexpected exceptions, permission failures and malformed arguments.

Use file fsync and directory fsync in the stated order; the profile names exactly
those calls. Their successful return is not measured power-loss durability. Process
kill/interruption tests are D2 evidence; APFS power-loss/crash/corruption verification
and independent backup/restore remain explicit unpassed release gates. D2 is not
permission to discard original exports or store irreplaceable data with no backup.
No new directory is a backup merely because it contains an archive copy.

Application immutability means D2 never overwrites retained objects. Checksums detect
observed corruption, not authenticated malicious rewriting of every linked object.
Input guards retain D1's distinction between ctime-only metadata drift and changed
bytes; no new OS-lock guarantee against hostile same-user processes is claimed.

## 7. Independent acceptance and deliverables

Implement in two internal units, reviewed separately: strict contracts/filesystem
primitives; then store orchestration/CLI and complete acceptance. Neither alone is
D2 completion. Public evidence uses synthetic input and preserves all previous tests.

Mandatory checks: definition mismatches have zero store-byte/tree changes; fabricated
prepared/PASS cannot be imported; unchanged batch/new attempt executes fresh replay;
archive mutation after D1 and during copy cannot produce mismatched commitment;
no-clobber identical reuse versus corruption; process/thread/alias writers; nested
mutation refusal; initialization races and partial uninitialized-state refusal;
short writes/ENOSPC/permission/flush failures; kill before/after every publication
boundary; unknown success resolved by attempt; interrupted intents never auto-adopt
orphans; failed failure-record writes do not claim evidence; source removal after
commit leaves full reproduction objects; read/list tamper detection; old computed
and new blocked batches stay separate; concurrent reads see one complete requested
identity or explicit failure; mode/alias/privacy checks; unchanged RC2/D1 and all
existing suites. The final independent harness uses real D1 replay and real files.

Private test-only checkpoint hooks may pause process workers at named phases for
kill/fault tests, but no production environment flag or public CLI bypass can enable
success, skip replay or import a fixture. Root phases must be fixed in the test
contract before tests: initialized-root, before-manifest, after-manifest-publish,
prepared, after-intent, after-archive, after-batch, before-result, after-result-publish,
after-result-flush. A normal hook does nothing; test workers replace it privately.
