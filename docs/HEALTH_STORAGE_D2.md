# Private Health observation storage (D2)

D2 is a separate, explicit macOS-only store for D1 observations. It does not change
RC2, select a source, merge days, promote coverage, or replace old computed values
with a newer batch. Every new import runs real D1 preparation and replay while
holding the existing store writer lock. Coverage remains unknown.

The Python entry points are `initialize(store_dir, store_id, expected_definition)`,
`status(store_dir, store_id)`,
`import_observations(store_dir, store_id, input_set, expected_definition, attempt_id)`,
`read_batch(store_dir, store_id, batch_id)`, `list_batches(store_dir, store_id)` and
`recover_attempt(store_dir, store_id, expected_definition, attempt_id)` from
`observation_store`. Results are immutable canonical JSON bytes. Init/status return
status objects; import returns its terminal result; read returns the exact D1 batch;
list returns deduplicated metadata; recovery returns its observed outcome.
`StoreError.code` and `StoreError.failure_bytes` contain fixed safe failures.
An import returning `FAILED` bytes is a failed operation with recorded private
terminal evidence. A raised exception does not imply a terminal record exists.
Definition conflicts/invalid definitions never write an attempt record. There is
no public commit/prepared-object admission, reset, cleanup, repair, or destroy API.

Create a private 0700 parent outside any code checkout or RC2 directory. The store
must not exist, including as an empty directory or symlink. Choose and retain your
own canonical UUID4 store ID and a fresh canonical UUID4 attempt ID for every new
import. Store ID and the complete D1 definition are immutable. Supply the definition
JSON obtained through D1 inspection; D2 compares its complete content with the
stored and freshly derived definitions. A different definition requires a new
nonexistent store directory. Initialization never adopts or repairs partial roots.

The private CLI is `python -m observation_store` (use the project's Python runtime).
All commands require `--store PATH --store-id UUID --output NEW_PRIVATE_FILE`.
Each supports optional `--failure-output NEW_PRIVATE_FILE`. Additional arguments:

| Command | Additional required arguments |
| --- | --- |
| `init` | `--definition FILE` |
| `status` | None |
| `import` | `--definition FILE --attempt-id UUID --archive ZIP --catalogue FILE --binding FILE --timezone ZONE --as-of YYYY-MM-DD --days N`; optional `--report FILE` |
| `read` | `--batch-id SHA256` |
| `list` | None |
| `recover` | `--definition FILE --attempt-id UUID` |

Public stdout contains exactly `status`, `operation`, and `error`: success uses
`PASS` and `error=null`; failure uses `FAIL` and a fixed error code. Exit 0 means the
command and private output publication completed; exit 2 means failure. Malformed
arguments and unexpected exceptions follow the same policy, with no argument,
identifier, path, exception text, or Health payload printed to stdout/stderr.
A successful recovery command can return a private `FAILED`, `INTERRUPTED`, or
`NOT_RECORDED` outcome; inspect that outcome to distinguish it from a committed
import. An import with a recorded `FAILED` result writes it to `--output` and exits 2.

Outputs must be new files under an existing private 0700 parent outside store
roots, code checkouts, RC2 roots and input aliases. Both output locators are checked
before store mutation. Files are published atomically, exclusively, and with mode
0600. Existing paths are never overwritten. Optional failure evidence contains
only safe IDs, fixed codes and allowed definition conflict field names. Keep the
original caller attempt ID independently: output failure after terminal store
publication reports `PUBLICATION_UNKNOWN`, and that ID is required for recovery.
Failure to publish the optional failure file cannot erase that uncertainty.

The store retains `manifest.json`, permanent `writer.lock`, raw archives, unchanged
D1 batches, per-attempt intent/result files and uncommitted staging objects. Every
managed file requires mode 0600, current ownership, one link and regular-file type;
directories require 0700. Symlinks and unexpected hardlinks are refused. Root and
child identities are checked through directory handles. File and directory fsync
calls and macOS `renameatx_np(RENAME_EXCL)` implement the declared flush profile.
There is no ordinary rename or hardlink fallback on other platforms/filesystems.

One immutable `COMMITTED` result is the visibility point. Readers validate its
intent, fresh D1 receipt, full batch and retained archive before returning data.
Listing captures attempt names once and validates every discovered terminal record;
a concurrent later publication may be omitted. A corrupt record causes an explicit
failure rather than a skipped entry or fallback. Reusing a verified batch with a
new attempt runs D1 again and records a distinct D1 verification receipt; it adds
no quantities. Copies are hash/length checked against D1, with a fresh source
recheck immediately before publication. Archive-sized memory and additional staging
space are currently required; staging and orphan objects are preserved.

For `PUBLICATION_UNKNOWN`, recover the original attempt. A valid committed chain
is revalidated and reflushed; a valid intent without a terminal result becomes
`INTERRUPTED`, without promoting orphan objects. `NOT_RECORDED` means observed
absence under the lock, not historical proof against deletion or disk loss. Recovery
does not rerun admission. A fresh import requires a new attempt ID. An existing
attempt returns `ATTEMPT_EXISTS`, even if it failed. Concurrent writers return
`WRITER_BUSY`; same-thread nested mutation returns `NESTED_OPERATION`.

A killed initializer before manifest publication leaves `UNINITIALIZED`; use a new
nonexistent directory. A post-manifest flush failure is `INITIALIZATION_UNKNOWN`;
`status` validates the complete initialization layout and reflushing under the
existing lock can resolve it. Malformed layouts fail with `INTEGRITY_ERROR`.
No automatic deletion, adoption or repair is provided.

Tests use invented fixtures, real D1 replay, faults and bounded process kills at
all ten fixed publication checkpoints. These tests do **not** pass T8/APFS
power-loss, crash/corruption or independent backup/restore gates. Successful fsync
calls are not measured power-loss durability. An archive copy is not a backup;
keep original exports and independently validated backups. Advisory writer locks
serialize cooperating writers, not hostile same-user edits. Checksums detect
observed corruption, not authenticated rewriting of all linked evidence.
