"""Rev 7 store: DDL, identity-qualified predicates, durable swap.

Identity model (Rev 7 §6):
  store_instance_id  - a specific physical store; new on every destroy/re-create.
  generation         - monotonically increasing per store instance.
  store_token        - the PAIR (store_instance_id, generation). generation alone is
                       NOT a token: a re-created store restarts at 1.
  value_definition_id- PER METRIC (not global), covering only inputs that change what
                       that metric's number MEANS.
  policy_id          - collection policy only (windows, cadence). Never invalidates values.
  source_capability_id - the MEASURED source capability (notably retention R). A change
                       in R changes whether a missing date is recoverable, so it is a
                       versioned capability, not a mutable setting.
"""
import contextlib, duckdb, fcntl, hashlib, json, os, threading, time, uuid
from pathlib import Path

SCHEMA_VERSION = 7


def _jsonable(o):
    """Dates and datetimes serialize as ISO-8601 strings; nothing else is coerced, so an
    unexpected type still raises rather than silently changing a preimage."""
    import datetime as _dt
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    raise TypeError(f"{type(o).__name__} is not canonically serializable")


def canon(obj) -> str:
    """Canonical serialization for every hashed identity: JSON, sorted keys, no NaN,
    explicit field names. `sha256(a,b,c)` is not an encoding; this is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      ensure_ascii=False, default=_jsonable)


def digest(kind: str, payload: dict, n: int = 32) -> str:
    """Domain-separated: the kind is part of the preimage, so two different identity
    kinds with identical payloads cannot collide."""
    pre = canon({"kind": kind, "payload": payload}).encode("utf-8")
    return hashlib.sha256(pre).hexdigest()[:n]


DDL = """
CREATE TABLE IF NOT EXISTS store_manifest (
    id                 INTEGER   NOT NULL PRIMARY KEY,
    store_instance_id  VARCHAR   NOT NULL,
    generation         BIGINT    NOT NULL,
    schema_version     INTEGER   NOT NULL,
    policy_id          VARCHAR   NOT NULL,
    created_at         TIMESTAMP NOT NULL,
    CHECK (id = 1), CHECK (generation >= 1)
);

-- PER-METRIC active value definition. A weather change cannot shadow Screen Time.
CREATE TABLE IF NOT EXISTS metric_definitions (
    metric_id            VARCHAR NOT NULL,
    value_definition_id  VARCHAR NOT NULL,
    effective_from       TIMESTAMP NOT NULL,
    definition_json      VARCHAR NOT NULL,
    PRIMARY KEY (metric_id, value_definition_id)
);

-- Exactly one active definition per metric, enforced by the PRIMARY KEY itself
-- rather than by a boolean column that permits two rows with active = TRUE.
CREATE TABLE IF NOT EXISTS metric_active_definition (
    metric_id            VARCHAR NOT NULL PRIMARY KEY,
    value_definition_id  VARCHAR NOT NULL,
    activated_at         TIMESTAMP NOT NULL
);

-- Measured source capability, versioned with an effective range. A change in R changes
-- what "recoverable" means, so classifications are retained per capability version.
CREATE TABLE IF NOT EXISTS source_capabilities (
    source_capability_id VARCHAR NOT NULL PRIMARY KEY,
    source               VARCHAR NOT NULL,
    retention_status     VARCHAR NOT NULL,
    retention_days       INTEGER,
    measured_at          TIMESTAMP NOT NULL,
    effective_from       DATE     NOT NULL,
    effective_to         DATE,
    CHECK (retention_status IN ('unknown','finite','unbounded')),
    CHECK (retention_days IS NULL OR retention_days >= 1),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TABLE IF NOT EXISTS observations (
    store_instance_id     VARCHAR NOT NULL,
    metric_id             VARCHAR NOT NULL,
    date                  DATE    NOT NULL,
    value_definition_id   VARCHAR NOT NULL,
    source                VARCHAR NOT NULL,
    value                 DOUBLE  NOT NULL,
    quality               VARCHAR NOT NULL,
    sample_count          INTEGER NOT NULL,
    run_id                VARCHAR NOT NULL,
    PRIMARY KEY (store_instance_id, metric_id, date, value_definition_id),
    CHECK (sample_count >= 0),
    CHECK (isfinite(value)),
    CHECK (quality IN ('measured','derived','imputed'))
);

CREATE TABLE IF NOT EXISTS metric_ranges (
    store_instance_id    VARCHAR NOT NULL,
    metric_id            VARCHAR NOT NULL,
    value_definition_id  VARCHAR NOT NULL,
    range_start          DATE    NOT NULL,
    range_end            DATE    NOT NULL,
    status               VARCHAR NOT NULL,
    payload_digest       VARCHAR NOT NULL,
    source_capability_id VARCHAR NOT NULL,
    run_id               VARCHAR NOT NULL,
    stale                BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (store_instance_id, metric_id, value_definition_id, range_start),
    CHECK (range_end >= range_start),
    CHECK (status IN ('ok','empty','partial','truncated'))
);

-- Monotonic write sequence. Ordering classifications by capability id or wall clock is
-- wrong: two writes in one transaction share a timestamp, and a capability id is not an
-- order. This counter increases on every classification write, within and across
-- publications, so "current" is always the LAST write.
CREATE SEQUENCE IF NOT EXISTS classification_seq START 1;

CREATE TABLE IF NOT EXISTS metric_date_availability (
    store_instance_id    VARCHAR NOT NULL,
    metric_id            VARCHAR NOT NULL,
    date                 DATE    NOT NULL,
    value_definition_id  VARCHAR NOT NULL,
    source_capability_id VARCHAR NOT NULL,
    classified_at        TIMESTAMP NOT NULL,
    seq                  BIGINT  NOT NULL,
    state                VARCHAR NOT NULL,
    -- APPEND-ONLY EVENT LOG. `seq` is part of the key, so every classification is
    -- retained: the same date may be classified under different capabilities and may
    -- be re-classified under the same one. "Current" is the highest seq, never a
    -- capability id and never a wall-clock timestamp.
    PRIMARY KEY (store_instance_id, metric_id, date, value_definition_id,
                 source_capability_id, seq),
    CHECK (state IN ('observed','known_absent','permanently_missing'))
);

CREATE TABLE IF NOT EXISTS permanent_gaps (
    store_instance_id    VARCHAR NOT NULL,
    source               VARCHAR NOT NULL,
    source_capability_id VARCHAR NOT NULL,
    gap_start            DATE    NOT NULL,
    gap_end              DATE    NOT NULL,
    detected_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (store_instance_id, source, source_capability_id, gap_start),
    CHECK (gap_end >= gap_start)
);

-- COLLECTION PROVENANCE. Every attempt is recorded, successful or not, independently
-- of the facts a previous attempt left behind. `provenance_json` carries the evidence
-- that made the values reproducible at the time they were written — for git, the
-- captured HEAD OIDs and the history-completeness evidence — so a later movement of
-- HEAD cannot make the origin of a stored value unknowable.
CREATE TABLE IF NOT EXISTS collection_runs (
    store_instance_id    VARCHAR   NOT NULL,
    run_id               VARCHAR   NOT NULL,
    source               VARCHAR   NOT NULL,
    metric_id            VARCHAR   NOT NULL,
    value_definition_id  VARCHAR   NOT NULL,
    source_capability_id VARCHAR   NOT NULL,
    recorded_at          TIMESTAMP NOT NULL,   -- UTC, naive. See utcnow().
    seq                  BIGINT    NOT NULL,   -- total order; timestamps tie in one txn
    outcome              VARCHAR   NOT NULL,
    reason               VARCHAR,
    snapshot_id          VARCHAR,
    provenance_json      VARCHAR   NOT NULL,
    PRIMARY KEY (store_instance_id, run_id, source, metric_id),
    CHECK (outcome IN ('ok','empty','partial','truncated','failed','unsupported','unknown'))
);

CREATE TABLE IF NOT EXISTS source_state (
    store_instance_id    VARCHAR NOT NULL,
    source               VARCHAR NOT NULL,
    source_capability_id VARCHAR NOT NULL,
    retention_days       INTEGER,
    retention_status     VARCHAR NOT NULL,   -- unknown | finite | unbounded
    measured_at          TIMESTAMP,
    -- DENORMALISED convenience copy, UTC. `collection_runs` is the authority: status
    -- derives every attempt outcome from there. An integrity check forbids this column
    -- from claiming a success newer than any recorded successful run.
    last_successful_sync TIMESTAMP,
    canonical_metric_id  VARCHAR NOT NULL,
    PRIMARY KEY (store_instance_id, source),
    CHECK (retention_status IN ('unknown','finite','unbounded'))
);
"""

CURRENT_VIEW = """
CREATE OR REPLACE VIEW observations_current AS
SELECT o.*
FROM observations o
JOIN store_manifest m ON o.store_instance_id = m.store_instance_id
JOIN metric_active_definition a
  ON a.metric_id = o.metric_id AND a.value_definition_id = o.value_definition_id;

-- One row per (metric, date) under the ACTIVE definition and the MOST RECENT
-- capability classification for that date. Older capability rows stay queryable.
CREATE OR REPLACE VIEW metric_date_availability_current AS
SELECT v.*
FROM metric_date_availability v
JOIN metric_active_definition a
  ON a.metric_id = v.metric_id AND a.value_definition_id = v.value_definition_id
QUALIFY row_number() OVER (
    PARTITION BY v.store_instance_id, v.metric_id, v.date, v.value_definition_id
    ORDER BY v.seq DESC) = 1;
"""


INTEGRITY_CHECKS = {
 "duplicate_active_definition":
   "SELECT metric_id, count(*) c FROM metric_active_definition GROUP BY 1 HAVING c > 1",
 "active_definition_without_definition_row":
   "SELECT a.metric_id FROM metric_active_definition a LEFT JOIN metric_definitions d "
   "ON d.metric_id=a.metric_id AND d.value_definition_id=a.value_definition_id "
   "WHERE d.metric_id IS NULL",
 "orphan_observation_definition":
   "SELECT DISTINCT o.metric_id, o.value_definition_id FROM observations o "
   "LEFT JOIN metric_definitions d ON d.metric_id=o.metric_id "
   "AND d.value_definition_id=o.value_definition_id WHERE d.metric_id IS NULL",
 "orphan_availability_capability":
   "SELECT DISTINCT v.source_capability_id FROM metric_date_availability v "
   "LEFT JOIN source_capabilities c ON c.source_capability_id=v.source_capability_id "
   "WHERE c.source_capability_id IS NULL",
 "observation_without_availability":
   "SELECT o.metric_id, o.date FROM observations o LEFT JOIN metric_date_availability v "
   "ON v.store_instance_id=o.store_instance_id AND v.metric_id=o.metric_id "
   "AND v.date=o.date AND v.value_definition_id=o.value_definition_id "
   "WHERE v.metric_id IS NULL",
 "current_observed_without_fact":
   "SELECT v.metric_id, v.date FROM metric_date_availability_current v "
   "LEFT JOIN observations o ON o.store_instance_id=v.store_instance_id "
   "AND o.metric_id=v.metric_id AND o.date=v.date "
   "AND o.value_definition_id=v.value_definition_id "
   "WHERE v.state='observed' AND o.metric_id IS NULL",
 "fact_without_current_observed":
   "SELECT o.metric_id, o.date FROM observations o "
   "JOIN metric_active_definition a ON a.metric_id=o.metric_id "
   "AND a.value_definition_id=o.value_definition_id "
   "LEFT JOIN metric_date_availability_current v "
   "ON v.store_instance_id=o.store_instance_id AND v.metric_id=o.metric_id "
   "AND v.date=o.date AND v.value_definition_id=o.value_definition_id "
   "WHERE v.state IS DISTINCT FROM 'observed'",
 "permanent_with_stored_fact":
   "SELECT v.metric_id, v.date FROM metric_date_availability v JOIN observations o "
   "ON o.store_instance_id=v.store_instance_id AND o.metric_id=v.metric_id "
   "AND o.date=v.date AND o.value_definition_id=v.value_definition_id "
   "WHERE v.state='permanently_missing'",
 # Every stored fact names the collection run that produced it, and that run is
 # recorded. Without this, a value's provenance is a free-text label nothing resolves.
 "observation_without_run":
   "SELECT o.metric_id, o.run_id FROM observations o LEFT JOIN collection_runs r "
   "ON r.store_instance_id=o.store_instance_id AND r.run_id=o.run_id "
   "AND r.metric_id=o.metric_id WHERE r.run_id IS NULL",
 # `collection_runs` is the AUTHORITY on attempt outcomes; `source_state` keeps a
 # denormalised copy of the last successful sync for operators. It may lag, but it may
 # never claim a success newer than any recorded successful run — that is the exact
 # shape of the Rev 10 defect where retained facts implied a healthy source.
 "source_state_claims_a_sync_no_run_recorded":
   "SELECT s.source FROM source_state s WHERE s.last_successful_sync IS NOT NULL "
   "AND s.last_successful_sync > COALESCE((SELECT max(r.recorded_at) "
   "FROM collection_runs r WHERE r.store_instance_id=s.store_instance_id "
   "AND r.source=s.source AND r.outcome IN ('ok','empty','partial','truncated')), "
   "TIMESTAMP '0001-01-01 00:00:00')",
 "overlapping_metric_ranges":
   "SELECT a.metric_id FROM metric_ranges a JOIN metric_ranges b "
   "ON a.store_instance_id=b.store_instance_id AND a.metric_id=b.metric_id "
   "AND a.value_definition_id=b.value_definition_id AND a.range_start < b.range_start "
   "WHERE a.range_end >= b.range_start",
}


def check_integrity(con):
    """Every invariant a DuckDB constraint cannot express, as explicit queries."""
    return {k: con.execute(q).fetchall() for k, q in INTEGRITY_CHECKS.items()}


class StoreTokenError(RuntimeError):
    pass


class WriterBusyError(RuntimeError):
    """Another process holds the store lifecycle lock."""


class StoreStateError(RuntimeError):
    """The on-disk store is missing, unreadable, or internally inconsistent."""


class StoreIntegrityError(RuntimeError):
    """A mutation would have published a store that violates its own invariants."""


class StoreAlreadyInitialized(RuntimeError):
    """init() called on a store that already has a pointer."""


class LifecycleTransitionError(RuntimeError):
    """A lifecycle operation is not permitted from the currently active one."""


class IdentityContentConflict(RuntimeError):
    """An identity was reused with different canonical content."""


# Which nested lifecycle operations an active operation authorizes. Re-entrant LOCK
# ACQUISITION is not the same thing as an allowed lifecycle TRANSITION: a publication
# may re-enter the lock to READ, but it may never destroy, re-initialize, or PUBLISH
# on top of the store it is building on.
#
# Rev 10 permitted publish -> publish. Both calls read the same pointer, both choose
# generation n+1, and the outer `os.replace` then overwrote the file the inner call had
# already published: a successful publication silently disappeared. A nested publication
# has no correct next generation to choose, so it is refused outright, before mutation.
ALLOWED_NESTED = {
    "publish": {"read"},
    "init": {"read"},
    "destroy": {"init", "read"},
    "cleanup": {"read"},
}
DESTRUCTIVE = {"init", "destroy"}


class CompareAndSwapError(RuntimeError):
    """The store advanced since the caller last observed it."""


class Store:
    """Generation-VERSIONED database files behind an atomic pointer.

    FINDING (measured, duckdb 1.5.5): DuckDB caches the database instance by PATH for
    the life of the process. After an `os.replace` swap, a new `connect()` to the same
    path returns the CACHED instance bound to the old inode for as long as any other
    handle is open. "Reopen a read-only connection per request" therefore does NOT
    converge on its own, and neither does a bare retry.

    Rev 7 fixes this by making each generation a DIFFERENT PATH:
        data/facts.<store_instance_id>.<generation>.duckdb
    with `current.json` as the atomically-replaced pointer. A reader that observes a
    new generation opens a path it has never opened, so the instance cache cannot
    hand back stale data.

    RECLAMATION. Superseded generation files are removed by `reclaim()`, which every
    successful publication calls. The policy is stated, not hand-waved: keep the
    current generation plus the `KEEP_GENERATIONS` most recent older ones, and only
    remove a file whose mtime is older than `RECLAIM_GRACE_SECONDS`.

    Why a grace period rather than "no reader holds them": on POSIX a reader that has
    already OPENED a file keeps reading the unlinked inode, so an open reader is never
    harmed. The only exposed reader is one that has read the pointer but not yet opened
    the file it named. Two retained generations plus a grace window make that reader's
    file survive far longer than the gap between those two operations, and if it ever
    did lose the race it gets a clean open failure that the gateway's bounded retry
    resolves by re-reading the pointer.
    """
    POINTER = "current.json"
    LOCKFILE = "lifecycle.lock"
    KEEP_GENERATIONS = 2
    RECLAIM_GRACE_SECONDS = 300

    def __init__(self, path):
        p = Path(path)
        self.dir = p.parent
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pointer = self.dir / self.POINTER
        self._lock_depth = 0
        self._lock_fh = None
        # In-process exclusion. `_lock_depth` alone has no owner, so two threads sharing
        # one Store could both publish from the same generation and lose a write.
        self._thread_lock = threading.Lock()
        self._lock_owner = None
        self._active_op = None

    # ---- ONE lock for every lifecycle operation: init, publish, destroy, cleanup.
    # The lock file is created once and NEVER unlinked, so its inode cannot be
    # replaced underneath a holder. Re-entrant so nested calls cannot self-deadlock.
    @contextlib.contextmanager
    def lifecycle_lock(self, op="publish", blocking=False, timeout=10.0):
        me = threading.get_ident()
        if self._lock_depth > 0 and self._lock_owner == me:
            allowed = ALLOWED_NESTED.get(self._active_op, set())
            if op not in allowed:
                raise LifecycleTransitionError(
                    f"{op!r} is not permitted while {self._active_op!r} is active; "
                    f"allowed: {sorted(allowed)}")
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        # A DIFFERENT thread must wait or be told the store is busy; it must never
        # proceed concurrently just because the same object is shared.
        got = self._thread_lock.acquire(blocking, timeout if blocking else -1)
        if not got:
            raise WriterBusyError(
                "another thread in this process holds the store lifecycle lock")
        lf = open(self.dir / self.LOCKFILE, "a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not blocking or time.monotonic() > deadline:
                    lf.close(); self._thread_lock.release()
                    raise WriterBusyError(
                        "another process holds the store lifecycle lock")
                time.sleep(0.02)
        self._lock_fh, self._lock_depth = lf, 1
        self._lock_owner, self._active_op = me, op
        try:
            yield
        finally:
            self._lock_depth -= 1
            self._lock_owner, self._active_op = None, None
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            finally:
                lf.close()
                self._lock_fh = None
                self._thread_lock.release()

    def path_id(self):
        """A stable 16-hex identifier for THIS store location, for error details.
        The path itself is not disclosed."""
        return digest("store_path", {"dir": str(self.dir.resolve())}, 16)

    def _read_pointer(self):
        return json.loads(self.pointer.read_text())

    def _db_path(self, sid, gen):
        return self.dir / f"facts.{sid}.{gen}.duckdb"

    @property
    def path(self):
        p = self._read_pointer()
        return self._db_path(p["store_instance_id"], p["generation"])

    def connect(self, read_only=False):
        """Opens the CURRENT generation's own file. Callers must close promptly."""
        return duckdb.connect(str(self.path), read_only=read_only)

    # ---------------------------------------------------------------- lifecycle
    def _write_pointer(self, sid, gen):
        tmp = self.dir / f".{self.POINTER}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(canon({"store_instance_id": sid, "generation": gen,
                              "schema_version": SCHEMA_VERSION}))
        fd = os.open(tmp, os.O_RDONLY); os.fsync(fd); os.close(fd)
        os.replace(tmp, self.pointer)
        dfd = os.open(self.dir, os.O_RDONLY); os.fsync(dfd); os.close(dfd)

    def init(self, policy_id, store_instance_id=None, exist_ok=False):
        """Repeated initialization is EXPLICIT: by default a second init on a store
        that already has a pointer raises. `exist_ok=True` returns the existing
        instance id unchanged and does not touch the data.

        BACKLOG (deliberately not in this milestone): identity-shaped columns are not
        format-checked. Doing it for policy_id alone would be arbitrary — three other
        identity kinds are deliberately exercised with non-identity values by the
        integrity tests — so the uniform version is deferred rather than half-done.
        Nothing the store holds under `policy_id` reaches the wire.
        """
        with self.lifecycle_lock("init"):
            if self.pointer.exists():
                cur = self.token()
                if not exist_ok:
                    raise StoreAlreadyInitialized(
                        f"store already initialized as {cur['store_instance_id']} "
                        f"at generation {cur['generation']}; pass exist_ok=True")
                return cur["store_instance_id"]
            return self._init_locked(policy_id, store_instance_id)

    def _init_locked(self, policy_id, store_instance_id=None):
        sid = store_instance_id or uuid.uuid4().hex
        dbp = self._db_path(sid, 1)
        con = duckdb.connect(str(dbp))
        con.execute(DDL); con.execute(CURRENT_VIEW)
        con.execute("DELETE FROM store_manifest")
        con.execute("INSERT INTO store_manifest VALUES (1,?,1,?,?,now())",
                    [sid, SCHEMA_VERSION, policy_id])
        con.execute("CHECKPOINT"); con.close()
        fd = os.open(dbp, os.O_RDONLY); os.fsync(fd); os.close(fd)
        self._write_pointer(sid, 1)
        return sid

    def token(self):
        """Read from the pointer, not from a database handle: the pointer is the
        authority and cannot be served stale by the instance cache."""
        p = self._read_pointer()
        return {"store_instance_id": p["store_instance_id"], "generation": p["generation"]}

    def require_token(self, tok):
        """Both fields must match. A re-created store restarts generation at 1, so
        generation alone cannot distinguish it from the old store."""
        cur = self.token()
        if not isinstance(tok, dict) or "store_instance_id" not in tok or "generation" not in tok:
            raise StoreTokenError("store_token must carry store_instance_id and generation")
        if tok["store_instance_id"] != cur["store_instance_id"]:
            raise StoreTokenError(
                f"store_instance_mismatch: token {tok['store_instance_id'][:8]}… "
                f"is not this store {cur['store_instance_id'][:8]}…")
        if tok["generation"] != cur["generation"]:
            raise StoreTokenError(
                f"generation_mismatch: token {tok['generation']} != {cur['generation']}")
        return cur

    def destroy_and_recreate(self, policy_id):
        with self.lifecycle_lock("destroy"):
            for f in self.dir.glob("facts.*.duckdb"):
                f.unlink()
            if self.pointer.exists():
                self.pointer.unlink()
            # The lock file is deliberately NOT removed: unlinking it would replace the
            # inode a concurrent holder is waiting on.
            return self._init_locked(policy_id)

    # ------------------------------------------------------- durable publish (swap)
    def open_snapshot(self, expect_token=None):
        """Open the CURRENT generation and verify the token INSIDE the snapshot.

        The pointer can advance between reading it and opening the file, so the
        identity is re-read from the opened database and compared.
        """
        tok = self.token()
        con = duckdb.connect(str(self._db_path(tok["store_instance_id"], tok["generation"])),
                             read_only=True)
        inside = con.execute(
            "SELECT store_instance_id, generation FROM store_manifest WHERE id=1").fetchone()
        actual = {"store_instance_id": inside[0], "generation": inside[1]}
        if actual != tok:
            con.close()
            raise StoreTokenError(f"snapshot identity {actual} != pointer {tok}")
        if expect_token is not None and expect_token != actual:
            con.close()
            raise StoreTokenError(f"token {expect_token} != snapshot {actual}")
        return con, actual

    def publish(self, mutate, expect_token=None):
        """Copy -> mutate -> checkpoint -> fsync file AND parent dir -> atomic rename.

        Rename alone is atomic but not durable: without the fsyncs a crash can leave the
        directory entry pointing at data the filesystem never flushed.
        """
        with self.lifecycle_lock("publish"):
            return self._publish_locked(mutate, expect_token)

    def _publish_locked(self, mutate, expect_token=None):
        if not self.pointer.exists():
            raise StoreStateError("no pointer: the store is not initialized")
        cur = self.token()
        # CAS on the FULL token: a generation number alone cannot tell store A gen 1
        # from a recreated store B gen 1.
        if expect_token is not None and expect_token != cur:
            raise CompareAndSwapError(
                f"compare-and-swap failed: expected {expect_token}, found {cur}")
        sid, nxt = cur["store_instance_id"], cur["generation"] + 1
        tmp = self.dir / f"facts.build.{os.getpid()}.{uuid.uuid4().hex}.duckdb"
        final = self._db_path(sid, nxt)
        # A generation file is written ONCE. If the target already exists, some other
        # writer published it and this publication would destroy it; the pointer is
        # then behind, which is a state to report, not to overwrite.
        if final.exists():
            raise StoreStateError(
                f"generation {nxt} already exists at {final.name}; refusing to overwrite "
                f"a published generation (pointer is at {cur['generation']})")
        con = None
        try:
            # PRE-MUTATION VALIDATION. A missing or unreadable source must fail, not
            # silently become a blank database at the next generation.
            src = self._db_path(sid, cur["generation"])
            if not src.exists():
                raise StoreStateError(
                    f"source generation {cur['generation']} is missing at {src.name}; "
                    "refusing to publish a blank database")
            try:
                probe = duckdb.connect(str(src), read_only=True)
            except Exception as e:
                raise StoreStateError(f"source generation is unreadable: {e}") from e
            try:
                rows = probe.execute(
                    "SELECT store_instance_id, generation FROM store_manifest").fetchall()
            finally:
                probe.close()
            if len(rows) != 1:
                raise StoreStateError(
                    f"source has {len(rows)} manifest rows; exactly one is required")
            if {"store_instance_id": rows[0][0], "generation": rows[0][1]} != cur:
                raise StoreStateError(
                    f"source identity {rows[0]} disagrees with the pointer {cur}")
            import shutil; shutil.copy2(src, tmp)
            con = duckdb.connect(str(tmp))
            con.execute(DDL); con.execute(CURRENT_VIEW)
            con.execute("BEGIN")
            try:
                mutate(con)
                con.execute("UPDATE store_manifest SET generation = generation + 1 WHERE id=1")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK"); raise
            # POST-MUTATION ENFORCEMENT, before anything becomes visible.
            viol = {k: v for k, v in check_integrity(con).items() if v}
            if viol:
                raise StoreIntegrityError(
                    "integrity violations block publication: "
                    + "; ".join(f"{k}={len(v)}" for k, v in sorted(viol.items())))
            mrows = con.execute(
                "SELECT store_instance_id, generation FROM store_manifest").fetchall()
            if len(mrows) != 1:
                raise StoreStateError(
                    f"post-mutation manifest has {len(mrows)} rows; exactly one required")
            if mrows[0][0] != sid or mrows[0][1] != nxt:
                raise StoreStateError(
                    f"post-mutation identity {mrows[0]} != expected ({sid}, {nxt})")
            con.execute("CHECKPOINT")
            con.close(); con = None
            fd = os.open(tmp, os.O_RDONLY); os.fsync(fd); os.close(fd)
            os.replace(tmp, final)                      # new generation, NEW PATH
            dfd = os.open(self.dir, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
            self._write_pointer(sid, nxt)               # publish, atomically
            self.reclaim()                              # best-effort, never fatal
        finally:
            if con is not None:
                try: con.close()
                except Exception: pass
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
        return self.token()

    def reclaim(self, keep=None, grace_seconds=None):
        """Remove superseded generation files. Returns the names removed.

        Never removes the current generation, never removes a file belonging to another
        store instance, and never raises: a publication that succeeded must not be
        reported as failed because a stale file could not be unlinked.
        """
        keep = self.KEEP_GENERATIONS if keep is None else keep
        grace = (self.RECLAIM_GRACE_SECONDS if grace_seconds is None else grace_seconds)
        removed = []
        try:
            cur = self.token()
        except Exception:
            return removed
        sid, gen = cur["store_instance_id"], cur["generation"]
        now = time.time()
        older = []
        for f in self.dir.glob(f"facts.{sid}.*.duckdb"):
            try:
                n = int(f.name.rsplit(".", 2)[-2])
            except ValueError:
                continue
            if n < gen:
                older.append((n, f))
        for n, f in sorted(older, reverse=True)[keep:]:
            try:
                if now - f.stat().st_mtime < grace:
                    continue
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
        return removed

    @staticmethod
    def sweep_build_files(directory):
        """A torn build from a dead process is removed; a live process's is not."""
        removed = []
        for f in Path(directory).glob("*.build.*.duckdb"):
            try:
                pid = int(f.name.split(".build.")[1].split(".")[0])
            except (IndexError, ValueError):
                continue
            alive = True
            try: os.kill(pid, 0)
            except ProcessLookupError: alive = False
            except PermissionError: alive = True
            if not alive:
                f.unlink(); removed.append(f.name)
        return removed


# ---------------------------------------------------------------------------
# Identity registration. An identity NAMES canonical content; reusing it with
# different content is an error, never a silent overwrite.

def register_definition(con, metric_id, value_definition_id, definition: dict):
    """Idempotent for identical canonical content; raises otherwise."""
    if not isinstance(definition, dict) or not definition:
        raise IdentityContentConflict(
            f"a complete definition is required for {metric_id}/{value_definition_id}")
    body = canon(definition)
    row = con.execute(
        "SELECT definition_json FROM metric_definitions WHERE metric_id=? "
        "AND value_definition_id=?", [metric_id, value_definition_id]).fetchone()
    if row is None:
        con.execute("INSERT INTO metric_definitions VALUES (?,?,now(),?)",
                    [metric_id, value_definition_id, body])
    elif row[0] != body:
        raise IdentityContentConflict(
            f"{metric_id}/{value_definition_id} already names different content; "
            f"a changed definition needs a new value_definition_id")


def register_capability(con, source_capability_id, capability: dict):
    """capability: {source, retention_status, retention_days, effective_from}."""
    need = {"source", "retention_status", "effective_from"}
    if not isinstance(capability, dict) or not need <= set(capability):
        raise IdentityContentConflict(
            f"a complete capability is required for {source_capability_id}: {sorted(need)}")
    st, days = capability["retention_status"], capability.get("retention_days")
    if st == "finite" and not (isinstance(days, int) and not isinstance(days, bool) and days >= 1):
        raise IdentityContentConflict("retention_status='finite' requires retention_days >= 1")
    if st in ("unknown", "unbounded") and days is not None:
        raise IdentityContentConflict(f"retention_status={st!r} must not carry retention_days")
    row = con.execute(
        "SELECT source, retention_status, retention_days, effective_from, effective_to "
        "FROM source_capabilities WHERE source_capability_id=?",
        [source_capability_id]).fetchone()
    if row is None:
        con.execute("INSERT INTO source_capabilities VALUES (?,?,?,?,now(),?,?)",
                    [source_capability_id, capability["source"], st, days,
                     capability["effective_from"], capability.get("effective_to")])
        return
    existing = {"source": row[0], "retention_status": row[1], "retention_days": row[2],
                "effective_from": str(row[3]),
                "effective_to": str(row[4]) if row[4] else None}
    incoming = {"source": capability["source"], "retention_status": st,
                "retention_days": days, "effective_from": str(capability["effective_from"]),
                "effective_to": (str(capability["effective_to"])
                                 if capability.get("effective_to") else None)}
    if existing != incoming:
        raise IdentityContentConflict(
            f"capability {source_capability_id} already names different content "
            f"({existing} != {incoming}); a new measurement needs a new capability id")


def activate_definition(con, metric_id, value_definition_id):
    """Activation is a DELIBERATE decision, never a side effect of a fetch."""
    row = con.execute("SELECT 1 FROM metric_definitions WHERE metric_id=? "
                      "AND value_definition_id=?",
                      [metric_id, value_definition_id]).fetchone()
    if row is None:
        raise IdentityContentConflict(
            f"cannot activate unregistered {metric_id}/{value_definition_id}")
    con.execute("DELETE FROM metric_active_definition WHERE metric_id=?", [metric_id])
    con.execute("INSERT INTO metric_active_definition VALUES (?,?,now())",
                [metric_id, value_definition_id])


def next_seq(con):
    return con.execute("SELECT nextval('classification_seq')").fetchone()[0]


def utcnow():
    """Every timestamp this code writes is UTC, stored NAIVE.

    Measured: DuckDB's `now()` written into a TIMESTAMP column stores LOCAL civil time,
    and an aware Python datetime is converted to local before the tzinfo is dropped. A
    value written as 12:00Z came back as 07:00 and was then serialized with no offset,
    which is neither a valid RFC 3339 date-time nor the instant that was recorded. So
    the store never lets DuckDB choose: the caller supplies UTC.
    """
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def as_utc_iso(naive):
    """Serialize a stored (UTC, naive) timestamp with an explicit offset."""
    import datetime as _dt
    if naive is None:
        return None
    if naive.tzinfo is not None:
        naive = naive.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return naive.replace(tzinfo=_dt.timezone.utc).isoformat()


def record_run(con, *, sid, run_id, source, metric_id, vdid, capid, outcome,
               reason=None, snapshot_id=None, provenance=None):
    """Record ONE collection attempt for one metric.

    Recorded whether or not the attempt produced facts: a failed run must be visible
    as a failed attempt while the facts a previous run wrote remain untouched. The
    provenance object is stored canonically so it can be compared and re-digested.
    """
    con.execute("DELETE FROM collection_runs WHERE store_instance_id=? AND run_id=? "
                "AND source=? AND metric_id=?", [sid, run_id, source, metric_id])
    con.execute("INSERT INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [sid, run_id, source, metric_id, vdid, capid, utcnow(), next_seq(con),
                 outcome, reason, snapshot_id,
                 canon(provenance if provenance is not None else {})])
