# Private verified Health backups and new-identity recovery (D4)

D4 snapshots the complete private D2 managed tree and freshly admits every unique
committed batch into a new D2 store. It preserves exact batch bytes, including
values, nulls, reasons, `observation_completeness="unknown"`,
`valid_metric_day=null`, and `inference_eligible=false`. It does not establish
complete activity days, source selection, aggregation, inference, or RC2 integration.

## Public commands

Use macOS, the pinned project Python environment, and explicit canonical UUID4
identities. All paths below are placeholders for private locations outside source
checkouts, source-only distributions, RC2 stores, and other Health outputs. Create
an owner-private parent first; each new destination must not exist. Directory modes
must be 0700 and regular file modes 0600, owned by the current user. Symlink aliases,
hardlinks, special files, unsafe names, and nested output locations are refused.
Use canonical paths: on macOS `/var` and `/tmp` can be aliases; their canonical
counterparts begin `/private/var` and `/private/tmp`. Synthetic execution can set
`TMPDIR=/private/tmp` when a canonical temporary parent is needed.

```sh
python -m observation_backup backup --store STORE --store-id STORE_UUID --backup NEW_BACKUP --backup-id BACKUP_UUID
python -m observation_backup verify --backup BACKUP --backup-id BACKUP_UUID
python -m observation_backup recover --backup BACKUP --backup-id BACKUP_UUID --recovery NEW_RECOVERY --new-store-id NEW_UUID
python -m observation_backup check-recovery --backup BACKUP --backup-id BACKUP_UUID --recovery RECOVERY --new-store-id NEW_UUID
```

Equivalent public Python functions are `create_backup(store_dir, store_id,
backup_dir, backup_id)`, `verify_backup(backup_dir, backup_id)`,
`recover_backup(backup_dir, backup_id, recovery_dir, new_store_id)`, and
`check_recovery(backup_dir, backup_id, recovery_dir, new_store_id)` in
`observation_backup`. Successful calls return canonical ASCII JSON bytes with
exactly `status`, `operation`, and `error`. Errors raise `BackupError` with `code`
and safe `failure_bytes`. The CLI emits exactly one acknowledgement plus a newline,
uses exit 0/2, and emits no stderr or input identifiers. No command accepts an
implicit latest identity, overwrite, resume, repair, extraction, or deletion option.

## Private layouts and completion

A sealed backup contains exactly `manifest.json`, `objects/`, and empty `staging/`.
The manifest records capture time, the original D2 manifest, directory inventory,
every file size/hash, and an independently derived committed listing. Object files
are flat `<sha256>.bin` files. Equal bytes are deduplicated; original paths remain
inventory data and are never extracted. Complete capture includes the empty writer
lock, pending/failed/interrupted attempts, orphan archives/batches, and bounded
partial staging. Only committed chains receive full archive/batch semantic checks;
unreferenced archive/batch and partial staging bytes remain opaque retained evidence.
D4 intentionally refuses nonportable staging names even when D2 can read them.

Capture holds the existing D2 writer lock through inventory, validation, streaming
copy, final recheck, and publication. It does not invoke status or recover attempts,
create a source lock, or mutate the source. `manifest.json` is the sole seal. An
interrupted output remains private evidence and cannot be reused as a destination.

A recovery root contains `plan.json`, `work/`, and `store/`; only completed recovery
adds `result.json`. The immutable plan pins the backup manifest hash, complete batch
set, original attempt mapping, fresh D2 attempts, and new store identity. Work
retains exact catalogue, binding, and report envelopes. The original backup archive
feeds a real InputSet and public D2 import, executing fresh D1 verification. Old
PASS receipts do not grant admission. Each distinct committed batch is imported
once, including when the original had repeated successful attempts.

Recovery has a new UUID and fresh D1/D2 attempts distinct from all retained old
attempt IDs. Historical failed/interrupted/pending attempts remain exclusively in
the backup. Recovery is not a writable continuation of the old store's history.
The exact original definition and every committed batch byte must match. Source
store, export archive, catalogue, and binding are unnecessary after backup sealing;
a rehearsal should make those original inputs unavailable before recovery.

D2 batches can be visible after an interrupted partial import. Their visibility is
not D4 completion. Only validated `result.json` establishes completion;
`check-recovery` revalidates full backup, immutable plan/result, exact recovered
batch set and attempt pairs, definition, receipt distinction, and public D2 reads.
It holds the new store's existing writer lock across validation and all required
reflushes. It never supplies missing completion or imports observations. Subsequent
operator writes to `RECOVERY/store` can invalidate this exact-plan check. A new
recovery attempt always uses a new directory and UUID.

## Failure and durability limits

The closed error set is `INPUT_INVALID`, `CLI_USAGE`, `LOCATION_INVALID`,
`DESTINATION_EXISTS`, `NOT_SEALED`, `INTEGRITY_ERROR`, `STORE_ID_MISMATCH`,
`WRITER_BUSY`, `NESTED_OPERATION`, `STORAGE_ERROR`, `PLATFORM_UNSUPPORTED`,
`REPLAY_FAILED`, `RECOVERY_INCOMPLETE`, `PUBLICATION_UNKNOWN`, and `INTERNAL_ERROR`.
Errors expose no private identifiers or exception details. Malformed stored evidence
refuses validation. Writer contention requires a later independent operation.
Existing or partial destinations are retained; choose a new destination deliberately.

Failure after a possible D4 seal/result publication returns `PUBLICATION_UNKNOWN`.
Run verify/check-recovery with the same explicit identities to resolve it; existence
alone is insufficient. D2 uncertain batch publication during import is
`RECOVERY_INCOMPLETE`, never completed recovery. Verification reflushes every object,
manifest, staging/object/root directory, and parent. Recovery checks also flush every
regular store/work file, all directories bottom-up, plan/result, root and parent,
with pinned identity/content rechecks through that interval.

Files publish exclusively on their own destination filesystem using unchanged D2
primitives; no cross-volume rename is attempted. External drive filesystems must
support owner/private POSIX permissions, safe regular-file checks, exclusive local
publication, advisory locking, and file/directory fsync. An external drive's physical
location and independence must be verified separately. No encryption, keys,
scheduling, garbage collection, pruning, or physical power-loss acceptance is added.
A local same-disk rehearsal proves local restoration only, not off-device or
device-loss protection.

Capture/object verification uses bounded 1 MiB chunks. Metadata, manifests, JSON
batches, and envelope validation still allocate complete JSON values. Unchanged
D1/D2 admission and archive publication can allocate whole archive bytes; there is
no bounded-memory whole-recovery claim. Storage includes all historical files and
partial staging, deduplicated full-byte objects, plus new store archive/staging and
replay envelopes. There is no capacity preflight or space reservation; exhaustion
retains partial evidence and fails safely. Keep adequate headroom on each volume.

Hashes detect corruption, not an authorized owner's deliberate rewriting of an
entire consistent graph. Pin the canonical backup manifest hash independently when
custody evidence matters. Cooperating writers are locked out; no protection is
claimed against privileged writers that perfectly restore modified bytes/metadata.
Public tests use only invented data and include real child-process kills. Process
interruption and successful fsync calls do not prove APFS power-loss behavior.

Independent acceptance, source-only distribution review, private real-data rehearsal,
and any external-drive/device-loss claim are separate gates. This implementation
and its synthetic suites do not constitute whole-project acceptance.
