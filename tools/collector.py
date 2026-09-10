"""Rev 7 collector: identity-qualified writes and gap repair.

Collection set (Rev 7 §5.4) = union of
   (a) the correction window  [as_of - (correction_days - 1) .. as_of]   INCLUSIVE,
       so "7 days" means SEVEN CIVIL DATES, not a 7-day difference;
   (b) newly elapsed dates since last_successful_sync;
   (c) recoverable unresolved dates from durability (INTERNAL gaps included),
capped by actual source availability (never earlier than the retention horizon).

Adapter evidence (Rev 7 §5.5). A no-use day is a structural zero ONLY when the source
query proves the day was read. Four outcomes are distinct and are persisted distinctly:
   observed        - a value (including a proven 0)
   known_absent    - read, and the source proves there is nothing
   undetermined    - inside the window, could not be determined -> stays UNKNOWN
   failed          - nothing determined for the metric -> no write, no delete
"""
from datetime import date, timedelta
import screentime as ST
import store as ST_
from store import digest


def collection_set(as_of, correction_days, last_sync_date, durability, horizon=None):
    want = set()
    if correction_days:
        for i in range(correction_days):                 # inclusive cardinality
            want.add(as_of - timedelta(days=i))
    if last_sync_date:
        d = last_sync_date
        while d <= as_of:
            want.add(d); d += timedelta(days=1)
    for g in durability.recoverable_gaps:                # (c) internal gaps too
        d = g.start
        while d <= g.end:
            want.add(d); d += timedelta(days=1)
    if horizon is not None:
        want = {d for d in want if d >= horizon}
    return sorted(d for d in want if d <= as_of)


MAX_REPAIR_BATCH = 90          # an unbounded repair is split into bounded runs


class FetchSetError(ValueError):
    """The four evidence sets must be pairwise disjoint."""


class AppendOnlyViolation(RuntimeError):
    """A write would have destroyed irreplaceable archive."""


class PermanentConflict(RuntimeError):
    """A date was classified permanently_missing while a fact for it is stored."""


class FetchResult:
    def __init__(self, metric_id, status, observed=None, known_absent=None,
                 undetermined=None, permanent=None, reason=None):
        self.metric_id, self.status = metric_id, status
        self.observed = observed or {}
        self.known_absent = list(known_absent or [])
        self.undetermined = list(undetermined or [])
        self.permanent = list(permanent or [])
        self.reason = reason
        self.reason_range = None
        o, k, u, pm = (set(self.observed), set(self.known_absent),
                       set(self.undetermined), set(self.permanent))
        # Status and evidence must agree, or an `ok` range can contain a known-absent
        # date that MetricFetchStatus explicitly forbids.
        if status in ("failed", "unsupported", "unknown"):
            if o or k or u or pm:
                raise FetchSetError(f"status={status!r} must carry no evidence")
        elif status == "ok":
            if k or u:
                raise FetchSetError(
                    f"status='ok' means every date is observed; got "
                    f"{len(k)} known_absent and {len(u)} undetermined")
        elif status == "empty":
            if o or u:
                raise FetchSetError(
                    f"status='empty' means nothing was observed and every date was "
                    f"determined; got {len(o)} observed and {len(u)} undetermined")
        elif status not in ("partial", "truncated"):
            raise FetchSetError(f"unknown status {status!r}")
        for a, b, na, nb in ((o, k, "observed", "known_absent"),
                             (o, u, "observed", "undetermined"),
                             (o, pm, "observed", "permanent"),
                             (k, u, "known_absent", "undetermined"),
                             (k, pm, "known_absent", "permanent"),
                             (u, pm, "undetermined", "permanent")):
            if a & b:
                raise FetchSetError(
                    f"{fmt_dates(sorted(a & b))} appears in both {na} and {nb}")


def fmt_dates(ds):
    return ", ".join(d.isoformat() for d in ds[:3]) + ("…" if len(ds) > 3 else "")


def batches(dates, size=MAX_REPAIR_BATCH):
    """Bounded repair runs: a multi-year backfill must not become one transaction."""
    dates = sorted(dates)
    return [dates[i:i + size] for i in range(0, len(dates), size)] or [[]]


def require_identities(con, metric_id, vdid, capid, definition=None, capability=None,
                       activate=False):
    """Identities must already NAME their content, or be registered here with COMPLETE
    content. Rev 9 used INSERT OR REPLACE, which silently rewrote an existing definition
    to `{}` and downgraded a measured `finite/28` capability to `unknown/null` whenever
    the optional argument was omitted. Nothing here fabricates content.
    """
    if definition is not None:
        ST_.register_definition(con, metric_id, vdid, definition)
    elif not con.execute("SELECT 1 FROM metric_definitions WHERE metric_id=? "
                         "AND value_definition_id=?", [metric_id, vdid]).fetchone():
        raise ST_.IdentityContentConflict(
            f"{metric_id}/{vdid} is not registered and no definition was supplied")
    if capability is not None:
        ST_.register_capability(con, capid, capability)
    elif not con.execute("SELECT 1 FROM source_capabilities WHERE source_capability_id=?",
                         [capid]).fetchone():
        raise ST_.IdentityContentConflict(
            f"capability {capid} is not registered and no measurement was supplied")
    if activate or not con.execute(
            "SELECT 1 FROM metric_active_definition WHERE metric_id=?",
            [metric_id]).fetchone():
        ST_.activate_definition(con, metric_id, vdid)


def apply_fetch(con, *, sid, vdid, capid, run_id, source, fr: FetchResult,
                append_only_before=None, definition=None, capability=None,
                provenance=None, snapshot_id=None):
    """Identity-qualified replacement.

    Deletion touches ONLY determined dates and ONLY this metric's value_definition_id.
    `append_only_before` (the start of the correction window) guards irreplaceable
    sources: a determined date older than it may be INSERTED where the archive has no
    row, but an existing row is never deleted or overwritten.

    EVERY attempt is recorded in `collection_runs`, including one that writes nothing,
    so a failed run is visible as a failed attempt rather than being indistinguishable
    from "no run happened". `provenance` carries whatever made these values
    reproducible (for git: the captured HEAD OIDs and the completeness evidence).
    """
    ST_.record_run(con, sid=sid, run_id=run_id, source=source, metric_id=fr.metric_id,
                   vdid=vdid, capid=capid, outcome=fr.status, reason=fr.reason,
                   snapshot_id=snapshot_id, provenance=provenance)
    if fr.status in ("failed", "unsupported"):
        return {"deleted": 0, "written": 0, "skipped": fr.status,
                "known_absent": 0, "undetermined": len(fr.undetermined), "permanent": 0}
    # The collector OWNS identity registration for what it writes: an observation may
    # never reference a definition or capability the store does not know about.
    require_identities(con, fr.metric_id, vdid, capid, definition, capability)
    determined = sorted(set(fr.observed) | set(fr.known_absent))
    if append_only_before is not None:
        protected = [d for d in determined if d < append_only_before]
        if protected:
            existing = {r[0] for r in con.execute(
                "SELECT date FROM observations WHERE store_instance_id=? AND metric_id=? "
                "AND value_definition_id=? AND date IN ({})".format(
                    ",".join(["?"] * len(protected))),
                [sid, fr.metric_id, vdid, *protected]).fetchall()}
            clash = sorted(existing)
            if clash:
                raise AppendOnlyViolation(
                    f"{fr.metric_id}: {fmt_dates(clash)} is archive older than the "
                    f"correction window ({append_only_before.isoformat()}) and cannot "
                    f"be replaced")
    deleted = 0
    for d in determined:
        deleted += con.execute(
            "SELECT count(*) FROM observations WHERE store_instance_id=? AND metric_id=? "
            "AND date=? AND value_definition_id=?",
            [sid, fr.metric_id, d, vdid]).fetchone()[0]
        con.execute(
            "DELETE FROM observations WHERE store_instance_id=? AND metric_id=? "
            "AND date=? AND value_definition_id=?", [sid, fr.metric_id, d, vdid])
    for d, v in sorted(fr.observed.items()):
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                    [sid, fr.metric_id, d, vdid, source, float(v["value"]),
                     v.get("quality", "measured"), int(v.get("sample_count", 1)), run_id])
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [sid, fr.metric_id, d, vdid, capid, ST_.next_seq(con), "observed"])
    for d in fr.known_absent:
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [sid, fr.metric_id, d, vdid, capid, ST_.next_seq(con), "known_absent"])
    if fr.permanent:
        # A date cannot be BOTH permanently missing and stored as a fact under the same
        # identity. Rev 8's permanent branch bypassed the archive guard and produced
        # exactly that contradiction, which every integrity check then called clean.
        held = [r[0] for r in con.execute(
            "SELECT date FROM observations WHERE store_instance_id=? AND metric_id=? "
            "AND value_definition_id=? AND date IN ({})".format(
                ",".join(["?"] * len(fr.permanent))),
            [sid, fr.metric_id, vdid, *fr.permanent]).fetchall()]
        if held:
            raise PermanentConflict(
                f"{fr.metric_id}: {fmt_dates(sorted(held))} is stored as a fact and "
                f"cannot also be classified permanently_missing")
    for d in fr.permanent:            # permanent classifications ARE persisted
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [sid, fr.metric_id, d, vdid, capid, ST_.next_seq(con),
                     "permanently_missing"])
    # undetermined: deliberately nothing written and nothing deleted -> remains unknown
    if fr.status == "empty" and not determined and fr.reason_range:
        _write_range(con, sid, fr, vdid, capid, run_id,
                     fr.reason_range[0], fr.reason_range[1], "empty")
    if determined:
        rows = con.execute(
            "SELECT date, value FROM observations WHERE store_instance_id=? AND metric_id=? "
            "AND value_definition_id=? AND date BETWEEN ? AND ? ORDER BY date",
            [sid, fr.metric_id, vdid, determined[0], determined[-1]]).fetchall()
        _write_range(con, sid, fr, vdid, capid, run_id, determined[0], determined[-1],
                     fr.status, rows)
    return {"deleted": deleted, "written": len(fr.observed),
            "known_absent": len(fr.known_absent), "undetermined": len(fr.undetermined),
            "permanent": len(fr.permanent)}


def _write_range(con, sid, fr, vdid, capid, run_id, lo, hi, status, rows=None):
    """Overlap rule: the incoming range REPLACES any overlapping portion; surviving
    left/right remainders are re-digested over what they actually still contain.
    Adjacent ranges coalesce only when provenance matches exactly."""
    over = con.execute(
        "SELECT range_start, range_end, status, source_capability_id, run_id "
        "FROM metric_ranges WHERE store_instance_id=? AND metric_id=? "
        "AND value_definition_id=? AND range_end >= ? AND range_start <= ?",
        [sid, fr.metric_id, vdid, lo, hi]).fetchall()
    for (rs, re_, st, cap, rid) in over:
        con.execute("DELETE FROM metric_ranges WHERE store_instance_id=? AND metric_id=? "
                    "AND value_definition_id=? AND range_start=?",
                    [sid, fr.metric_id, vdid, rs])
        for a, b in ((rs, lo - timedelta(days=1)), (hi + timedelta(days=1), re_)):
            if a <= b:
                _insert_range(con, sid, fr.metric_id, vdid, a, b, st, cap,
                              f"derived:{rid}")
    _insert_range(con, sid, fr.metric_id, vdid, lo, hi, status, capid, run_id)


def _insert_range(con, sid, metric_id, vdid, lo, hi, status, capid, run_id):
    rows = con.execute(
        "SELECT date, value FROM observations WHERE store_instance_id=? AND metric_id=? "
        "AND value_definition_id=? AND date BETWEEN ? AND ? ORDER BY date",
        [sid, metric_id, vdid, lo, hi]).fetchall()
    con.execute("INSERT INTO metric_ranges VALUES (?,?,?,?,?,?,?,?,?,?)",
                [sid, metric_id, vdid, lo, hi, status,
                 digest("range", {"metric_id": metric_id, "start": str(lo), "end": str(hi),
                                  "rows": [[str(r[0]), r[1]] for r in rows]}),
                 capid, run_id, False])


def observed_dates(con, sid, metric_id, vdid):
    """CURRENT classifications only.

    Rev 8 read every historical `state='observed'` row, so a date later reclassified
    `known_absent` under a NEW capability still counted as observed and durability
    reported a tail that current facts contradicted. This reads the current view, which
    resolves the active definition and the newest classification per date.
    """
    return [r[0] for r in con.execute(
        "SELECT date FROM metric_date_availability_current "
        "WHERE store_instance_id=? AND metric_id=? AND value_definition_id=? "
        "AND state='observed' ORDER BY date",
        [sid, metric_id, vdid]).fetchall()]


def durability_from_store(con, sid, source, metric_id, vdid, as_of, R, last_sync,
                          retention_status=None):
    """Retention tri-state is carried explicitly; `unknown` is not `unbounded`."""
    return ST.compute(observed_dates(con, sid, metric_id, vdid), as_of, R, last_sync,
                      retention_status=retention_status)
