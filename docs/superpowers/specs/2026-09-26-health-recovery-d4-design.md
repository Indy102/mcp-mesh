# D4: verified private Health backup and observation recovery

Status: implementation design; no acceptance or off-device protection claim.
Base: `19e564ffdeb485cf19ec62815e1f2384fefc88e7` (D3).

## Purpose and boundary

Preserve the complete private D2 managed tree as an independently verifiable snapshot, and recover all its committed observations into a new D2 store by fresh D1 replay. D4 is a separate `observation_backup` package. RC2, D1, D2, D3, prior tests/spikes and dependency files remain byte-identical. Only new package/tests/docs and additive README/CI changes are permitted. No personal fixtures, paths, values or identifiers in Git or public process output. Synthetic implementation evidence only; real private demonstration follows independent acceptance.

This is an architectural extension. The alternatives considered were a direct writable clone (duplicates immutable store identity and enables divergent writers), a committed-batches-only export (loses partial-attempt evidence), and the chosen full evidence snapshot plus new-identity observation recovery. No original receipt or store identity is rewritten. The new store has new attempts and freshly executed verification receipts; old attempts remain historical evidence in the backup. This is not continuation of the original operational history.

No new completeness, aggregation, inference, recommendation, source selection, MCP or RC2 integration. `observation_completeness="unknown"`, `valid_metric_day=null`, `inference_eligible=false` remain exact. No encryption/key management, scheduling, garbage collection, pruning, exact old-store writable restoration, APFS power-loss acceptance, or whole-project acceptance. A same-disk run is local restoration evidence only. Independent storage location and device-loss protection must be separately verified; never infer them from a copy.

## Interfaces

Successful public Python functions return canonical ASCII JSON bytes with exactly `status`, `operation`, `error`: PASS/null or FAIL/fixed code. No IDs or paths in these acknowledgements. Failures raise `BackupError` (the CLI prints its failure bytes); exceptions are `BackupError` with `code` and safe `failure_bytes`. Operations are `backup`, `verify`, `recover`, `check-recovery`.

- `create_backup(store_dir, store_id, backup_dir, backup_id)`
- `verify_backup(backup_dir, backup_id)`
- `recover_backup(backup_dir, backup_id, recovery_dir, new_store_id)`
- `check_recovery(backup_dir, backup_id, recovery_dir, new_store_id)`

CLI `python -m observation_backup <operation>` takes explicit `--store`, `--store-id` for backup; all commands require `--backup`, `--backup-id`; backup targets a nonexistent directory; recover/check-recovery also require `--recovery`, `--new-store-id`. Exit 0/2, exactly one safe acknowledgement on stdout, empty stderr, no argument echo/traceback/help containing input. Wrong args refuse. UUIDs are canonical UUID4. No latest/implicit identity, overwrite, resume, repair or deletion option.

Fixed errors: INPUT_INVALID, CLI_USAGE, LOCATION_INVALID, DESTINATION_EXISTS, NOT_SEALED, INTEGRITY_ERROR, STORE_ID_MISMATCH, WRITER_BUSY, NESTED_OPERATION, STORAGE_ERROR, PLATFORM_UNSUPPORTED, REPLAY_FAILED, RECOVERY_INCOMPLETE, PUBLICATION_UNKNOWN, INTERNAL_ERROR. Store/Preparation exceptions map to this closed set without exception strings. A failure after potential seal/completion publication returns PUBLICATION_UNKNOWN; verify/check-recovery resolves by full validation and required reflushing, not by presence alone.

## Complete snapshot and integrity

A sealed backup directory is owner-private 0700, containing exactly `objects/`, `staging/`, `manifest.json`. Files are owner-private 0600, current owner, single-link regular files; no symlinks, hardlinks, FIFO, device files or borrowed locks. `staging/` is empty in a sealed backup. Objects are `<sha256>.bin` containing exact original file bytes, deduplicated only by equal full bytes; original source paths are inventory data, never directly extracted. A backup is not a D2 store and is never opened as a writable clone. No TAR/ZIP extraction is added.

Canonical closed manifest fields:
- `schema_version`: `health-observation-backup-v1`
- `backup_id`: caller UUID4
- `captured_at`: UTC ISO timestamp, snapshot capture time, not data time
- `scope`: `complete-d2-managed-tree-v1`
- `source_manifest`: exact validated D2 manifest, including original store ID/full definition/flush profile
- `directories`: sorted unique relative POSIX directory paths (root represented by `""`)
- `files`: sorted unique entries `{path, size, sha256}` for every original regular file, including empty writer.lock, orphans and staging
- `listing`: exact derived D2 listing of unique committed batches and committed attempt IDs

All keys closed, types exact (bool is not integer), paths portable safe components without empty/dot/dotdot/NUL/backslash, no absolute paths, duplicate names or file/directory collisions. Paths are constrained to the original D2 layout: manifest.json, writer.lock, archives/*.zip, batches/*.json, attempts/*.intent.json / *.result.json and bounded D2 staging. Directories include root, four required D2 dirs and optional one-level staging child dirs. Archive/batch names require lowercase 64-hex, attempts require UUID4. Inventory hierarchy must be closed. Every object is referenced, every reference exists, no extra objects or hidden files; exact sizes/hashes are checked with bounded streaming I/O. JSON evidence is strict/canonical before semantic validation. A changed complete graph can be forged by a trusted owner; hashes are corruption detection, not authentication. Pin the manifest hash externally when retaining custody evidence.

A validator must validate *every* intent/result with public D2 contract validators, not only requested batches. Validate pending intents, FAILED with/without intent, INTERRUPTED and all COMMITTED chains; committed archives must match archive hash/length, batches their filename/content identity, full definition and D1 contract, result/intent linkage and receipt identity. Derive listing independently of manifest declarations and compare exactly. Unreferenced archive/batch bytes and partial staging are retained/hash-checked as opaque evidence; they are not committed or necessarily semantically valid. No public status call or shape validation substitutes for full chain validation. Use D2 public contract/filesystem APIs only, never import its underscore helpers.

## Capture, publication and concurrency

Before any mutation, validate explicit identities and nonexistent destination under an existing private parent, reject containment/aliasing with source, executing source distribution, checkouts, RC2 stores, any Health store/backup/recovery ancestor. Preserve original tree/bytes/modes on success and all failure paths. Source is required to be initialized/structurally and semantically valid.

Hold existing D2 writer_lock across source inventory, full chain validation, streaming capture and final inventory/hash/manifest recheck. Never create or replace source lock. Contending thread/process/alias must refuse with the established lock error. No recovery/terminalization of pending attempts. Pin handles and recheck path/inode/size/mtime/mode before/after read; detect source and destination replacement. Source paths/modes/bytes inventory must equal before seal. Cooperating writers are excluded; no protection claim against arbitrary privileged writers that perfectly undo mutations.

Reserve a new backup directory exclusively. Copy bytes into private objects using bounded chunks, handle short writes, flush every file and directory and verify copied bytes before sealing. Incomplete destinations are retained and never silently adopted. Stage canonical manifest then publish it with unchanged D2 exclusive file publication; manifest is the sole seal. Reflush directory and parent before PASS. Checkpoints before/after seal permit deterministic exception/process-kill tests. No cross-filesystem rename: each destination is built on its own volume, only local final file publication. Verify is read-only except filesystem flushes; it never repairs content or creates seal.

## Recovery through fresh admission

Verify full backup first. Require new store UUID distinct from original, new recovery directory outside source/backup/store/code trees under private parent. Refuse any existing destination before mutation. Pin canonical backup manifest hash and complete sorted committed batch set. Reserve recovery root, create work/, then publish/flush plan.json; let public D2 initialize exclusively create the nonexistent store/ path. The initialized layout is exactly `plan.json`, `store/`, `work/` and, only upon completion, `result.json`. Work may retain replay inputs on failure. Do not clean failure evidence automatically.

Closed canonical plan fields: `schema_version="health-observation-recovery-plan-v1"`, `backup_id`, `backup_manifest_sha256`, `original_store_id`, `new_store_id`, `definition_id`, `batches` sorted unique objects `{batch_id, batch_sha256, original_attempt_ids, new_attempt_id}`. Fresh UUID4 attempt IDs are distinct from all old D2 and D1 attempt IDs and each other; persist the immutable plan before initializing/importing. One new import per distinct committed batch, even when old batch had repeated successful attempts. Empty initialized stores are valid recovery inputs.

Initialize the new D2 store with the exact old definition. For each planned batch, materialize its embedded exact catalogue/binding/report envelopes in private work; retained backup archive is the archive input. Construct real D1 InputSet using supplied report and its pinned timezone/window. Invoke public D2 `import_observations`, never prepared-object admission. Require resulting public `read_batch` bytes to equal original bytes, and final `list_batches` to match exactly planned batches/new attempts. Fresh D1 receipt IDs must differ from old ones. Preserve historical failed/interrupted/pending attempts and orphans exclusively in backup; they never become recovered observations. No original export/catalogue/binding/store needed.

Before completion, acquire the existing new-store writer_lock and hold it across final store validation, backup revalidation, reflushing and result publication. Never hold this lock across public init/import/status calls that reacquire it. Reverify backup still matches pinned manifest and object inventory; validate new store definition and all planned observations/attempt links. Closed result fields: `schema_version="health-observation-recovery-result-v1"`, `status="PASS"`, `plan_sha256`, `backup_manifest_sha256`, `new_store_id`, `recovered_batch_ids` sorted unique exact plan. Stage and exclusively publish result.json last, flush root/parent. A later replay failure can leave a partially imported new D2 store; absence of validated completion means RECOVERY_INCOMPLETE, never complete success. No blind resume or overwrite. A new attempt uses a new directory and UUID.

`check_recovery` acquires the existing new-store writer_lock across the entire validation/reflush/PASS interval, revalidates backup, plan/result, full new D2 listing/read equality, definition and mapped attempts and reflushes durable files/dirs. It resolves uncertain completion only when the immutable result already exists and validates. It never creates completion or imports missing observations. Writes by an operator after completed recovery can make exact-plan check fail; this checks the recorded recovery state, not arbitrary future evolution. Private docs distinguish result completion, D2 visibility and future use of recovery/store.

## Acceptance

Meaningful synthetic tests through public APIs and CLI: empty, repeated and multiple distinct batches; all value/null/reason fields exact; pending/failed/interrupted/orphans/staging retained without promotion; source unchanged; entire inventory closure; wrong IDs/version/shape/canonical JSON; tamper each evidence class; missing/extra objects; unsafe paths/types/links/modes; bad destinations including marker-free code distributions and aliases; thread/process/nested source lock; source/destination mutation; partial writes/ENOSPC/fsync errors; before/after-seal and before/after-result process interruption, safe unknown states and verify/check resolution; actual fresh replay failure; source/export removed before recovery; all new batch bytes and receipt differences; partial recovery never complete. No fake PASS repairs, detached fixtures or weakening earlier tests.

Independent acceptance harness authored before implementation, task review, whole-branch review and clean source extraction. Run unchanged existing suites once per final relevant build. Verify core/base file hashes and RC2 artifacts. Generate D3 view from recovered synthetic store; private real demonstration only after reviewed code. Exact source-only reproducible archive and stacked unmerged PR, no personal evidence uploaded. Report capacity and destination limits honestly; no physical power-loss or off-device result from synthetic/local tests.

## Readiness clarifications

- source_manifest must equal the strict canonical object of inventoried manifest.json; writer.lock bytes must be empty. Successful recovery has exactly the planned fresh COMMITTED intent/result pairs and no extra pending/failed attempts, not merely the same listing. Original D1 and D2 attempt IDs are collected across all retained intents/results for new-ID exclusion.
- Refuse ancestors with Health writer.lock/manifest or D4 objects/staging or work/plan/store layout markers even if no completed seal exists. Also reject resolved and lexical containment, symlinks/hardlinks and marker-free executing distribution destinations. A completely empty interrupted root has no data; never adopt it as a destination. Unsupported portable staging names are an explicit D4 refusal even if D2 could read them. Empty root path is the only empty path exception.
- Copy/hash objects by streaming directly to their exclusive final SHA names; only the small manifest/result/plan uses the existing file-publication helper. No bounded-memory whole-recovery claim: unchanged D2 archive checks/publication may allocate entire archive bytes.
- Private checkpoint seam in observation_backup implementation exposes fixed names `before-backup-seal`, `after-backup-seal`, `before-recovery-result`, `after-recovery-result`. Tests may patch/kill at these seams; no CLI flag or production hook input. Extra internal seams allowed for focused fault tests.
- Verify flushes every referenced object, sealed manifest, empty staging, object directory, backup root and parent. check-recovery additionally flushes plan/result, all regular new-store/work files, every child directory bottom-up, recovery root and parent. Both perform full validation first and recheck pinned identity/content through the flush interval.
- D2 PUBLICATION_UNKNOWN inside an import is uncertain batch publication, not completed recovery. Return RECOVERY_INCOMPLETE unless D4 result publication itself may have happened. Any post-result failure returns PUBLICATION_UNKNOWN; check-recovery never supplies missing completion.
