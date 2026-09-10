"""A real stdio MCP LEAF. It serves exactly the two tools it implements.

  fetch          stored facts for this leaf's source, under one verified store_token
  source_status  what the store records about this source's collection attempts

The JSON-RPC dispatch, the error ladder and the stated input limits live in
`mcp_stdio.py`, which the gateway server shares. Rev 10 advertised the entire specified
catalogue from this leaf, so `weekly_report` appeared in `tools/list` and then answered
`insufficient_evidence`. A leaf now advertises only what it serves.

stdout carries ONLY protocol frames. Every diagnostic goes to stderr.
"""
import sys, json, pathlib
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import contracts as C, fixtures as F, mcp_stdio as P

# ---- Re-exported so the transport suite can assert the stated limits at their source.
MAX_FRAME_BYTES = P.MAX_FRAME_BYTES
MAX_NESTING_DEPTH = P.MAX_NESTING_DEPTH
semantic_errors = C.semantic_errors

# ---- Store-backed mode. With --store the leaf answers from the SELECTED STORE and the
# recorded operation state; without it, it answers from fixtures (test infrastructure).
STORE_DIR = None
SOURCE = None
CONFIG_PATH = None
for _i, _a in enumerate(sys.argv):
    if _a == "--store" and _i + 1 < len(sys.argv):
        STORE_DIR = sys.argv[_i + 1]
    if _a == "--source" and _i + 1 < len(sys.argv):
        SOURCE = sys.argv[_i + 1]
    if _a == "--config" and _i + 1 < len(sys.argv):
        CONFIG_PATH = sys.argv[_i + 1]
STORE_BACKED = STORE_DIR is not None

IMPLEMENTED = ("fetch", "source_status")

# NO production tool requires a client capability: this leaf implements no elicitation,
# sampling or roots workflow. Rev 8 hard-coded weekly_report -> elicitation purely to
# make a test pass, which is a fabricated requirement. The -32021 path is still
# implemented and exercised, through a TEST-ONLY tool (see TEST_ONLY_TOOLS).
REQUIRES_CAPABILITY = {}

# ---- TEST INFRASTRUCTURE, not part of the served profile -----------------------
# These names are not advertised by tools/list and are not production behaviour.
TEST_ONLY_TOOLS = {
    "__boom__": "raises, to exercise the -32603 internal-fault path",
    "__needs_elicitation__": "declares a capability requirement, to exercise -32021",
}
TEST_ONLY_CAPABILITY = {"__needs_elicitation__": "elicitation"}

SERVER_INFO = {"name": "mesh-leaf", "version": "0.11.0"}
DATE_LIST_CAP = 50               # DEFS.DateList maxItems; the wire cap, stated once
DEFAULT_MAX_STALENESS_DAYS = 1


def config():
    if CONFIG_PATH:
        return json.loads(pathlib.Path(CONFIG_PATH).read_text())
    return {}


def _store():
    import store as _S
    return _S.Store(pathlib.Path(STORE_DIR) / "facts.duckdb")


def _token_error(supplied, current):
    if supplied.get("store_instance_id") != current["store_instance_id"]:
        return P.error_payload(
            "store_instance_mismatch", "store_token names a different store instance",
            "Re-read the pointer; the store was re-created.", False,
            {"current_store_token": current})
    if supplied.get("generation") != current["generation"]:
        return P.error_payload(
            "generation_mismatch",
            f"leaf holds generation {current['generation']}; the request "
            f"specified {supplied.get('generation')}",
            "Re-read the pointer and repeat the fan-out.", True,
            {"current_generation": current["generation"],
             "current_store_token": current})
    return None


class _TokenMismatch(Exception):
    def __init__(self, payload):
        self.payload = payload


class NoAnalysisTimezone(RuntimeError):
    """The store records no usable analysis timezone and configuration supplies none."""


class NormalizationVersionUnknown(RuntimeError):
    """A metric's active definition does not record its normalization version."""


def _open_verified(s, supplied):
    """Open the snapshot AND prove it is the one the caller asked for.

    Rev 10 compared the supplied token against `s.token()` and then called
    `open_snapshot()` with no expectation. A publication between those two operations
    produced a successful, non-error response carrying a DIFFERENT generation, which
    the gateway then accepted. The expectation is now passed INTO the operation that
    opens and verifies the snapshot, so a mismatch cannot be reported as success.
    """
    import store as _S
    try:
        current = s.token()
    except (FileNotFoundError, OSError, ValueError) as exc:
        # No pointer: this store directory was never initialised. An ordinary state
        # (the gateway started before `mesh init`), and an answer, not a fault.
        raise _TokenMismatch(P.error_payload(
            "storage_error", f"this store has not been initialised: {exc}",
            "run `mesh init --config <config>`", False,
            {"cause": "manifest_missing", "store_path_id": s.path_id()}))
    err = _token_error(supplied, current)
    if err:
        raise _TokenMismatch(err)
    try:
        return s.open_snapshot(expect_token=supplied)
    except _S.StoreTokenError:
        current = s.token()
        raise _TokenMismatch(_token_error(supplied, current) or P.error_payload(
            "generation_mismatch",
            "the store was published while this snapshot was being opened",
            "Re-read the pointer and repeat the fan-out.", True,
            {"current_generation": current["generation"],
             "current_store_token": current}))
    except Exception as exc:
        # The pointed-to generation could not be OPENED. Measured: a read-only connect
        # to a missing file raises rather than creating an empty database, so a
        # reclaimed generation can never become a silently empty store — but the
        # condition must be reported as a storage answer, not as an internal fault.
        # `storage_error` REQUIRES details.store_path_id and details.cause.
        raise _TokenMismatch(P.error_payload(
            "storage_error", f"the current generation could not be opened: {exc}",
            "Re-read the pointer and repeat the fan-out.", True,
            {"cause": "open_failed", "store_path_id": s.path_id(),
             "current_store_token": s.token()}))


# ------------------------------------------------------------------ source_status
def _normalization_versions(con, metric_ids):
    """From the ACTIVE definition of each metric. A literal here would misreport any
    metric whose recorded definition names a different version."""
    out = {}
    for m in metric_ids:
        row = con.execute(
            "SELECT d.definition_json FROM metric_active_definition a "
            "JOIN metric_definitions d ON d.metric_id=a.metric_id "
            "AND d.value_definition_id=a.value_definition_id WHERE a.metric_id=?",
            [m]).fetchone()
        v = json.loads(row[0]).get("normalization_version") if row else None
        if not isinstance(v, int) or v < 1:
            raise NormalizationVersionUnknown(
                f"{m}: its active value definition records no normalization_version")
        out[m] = v
    return out


def _analysis_timezone(con, cfg):
    """The timezone the stored numbers were actually computed in.

    The active value definitions RECORD it, because it changes the values. Rev 10
    emitted a hardcoded literal that contradicted the definition on file. Configuration
    is the fallback for a store that records none; a disagreement is reported, never
    silently resolved in favour of the configuration.
    """
    zones = set()
    for (body,) in con.execute(
            "SELECT d.definition_json FROM metric_active_definition a "
            "JOIN metric_definitions d ON d.metric_id=a.metric_id "
            "AND d.value_definition_id=a.value_definition_id").fetchall():
        tz = json.loads(body).get("analysis_timezone")
        if tz:
            zones.add(tz)
    if len(zones) == 1:
        return zones.pop(), "recorded_value_definition", None
    cfg_tz = cfg.get("analysis_timezone")
    if not cfg_tz:
        # Not an internal fault: a store that has been initialised but never synced
        # records no definitions, and this leaf has nothing to say about a timezone.
        raise NoAnalysisTimezone(
            "no metric is active in this store and no configuration supplies an "
            "analysis timezone" if not zones else
            f"active value definitions disagree ({sorted(zones)}) and no configuration "
            f"resolves them")
    note = (f"active value definitions record {sorted(zones)}; reporting the configured "
            f"{cfg_tz}") if zones else None
    return cfg_tz, "configuration", note


def _attempts(con, sid, source):
    """Attempt outcomes come from the recorded runs, not from retained facts."""
    import store as _S
    last = con.execute(
        "SELECT recorded_at, outcome, reason FROM collection_runs "
        "WHERE store_instance_id=? AND source=? ORDER BY seq DESC LIMIT 1",
        [sid, source]).fetchone()
    ok = con.execute(
        "SELECT recorded_at FROM collection_runs WHERE store_instance_id=? AND source=? "
        "AND outcome IN ('ok','empty','partial','truncated') ORDER BY seq DESC LIMIT 1",
        [sid, source]).fetchone()
    return {"last_attempt_at": _S.as_utc_iso(last[0]) if last else None,
            "last_attempt_outcome": last[1] if last else None,
            "last_attempt_reason": last[2] if last else None,
            "last_successful_sync": _S.as_utc_iso(ok[0]) if ok else None,
            "last_success_dt": ok[0] if ok else None}


def real_source_status(args):
    import store as _S, collector as _CO, screentime as _ST
    s = _store()
    try:
        con, tok = _open_verified(s, args.get("store_token") or {})
    except _TokenMismatch as e:
        return P.payload_result(e.payload, True)
    cfg = config()
    max_stale = int(cfg.get("max_staleness_days", DEFAULT_MAX_STALENESS_DAYS))
    sid = tok["store_instance_id"]
    try:
        try:
            tz, tz_source, tz_note = _analysis_timezone(con, cfg)
        except NoAnalysisTimezone as e:
            return P.payload_result(P.error_payload(
                "insufficient_evidence", str(e),
                "run `mesh sync`, or start the leaf with --config naming an "
                "analysis_timezone", False), True)
        except NormalizationVersionUnknown as e:
            return P.payload_result(P.error_payload(
                "storage_error", str(e),
                "re-register the metric with a complete value definition", False,
                {"cause": "definition_incomplete", "store_path_id": s.path_id()}), True)
        # `as_of` and every timestamp comparison happen in the ANALYSIS timezone. A
        # UTC-recorded run compared against a LOCAL today() produced staleness of -1.
        zone = ZoneInfo(tz)
        as_of = datetime.now(timezone.utc).astimezone(zone).date()
        state = {r[0]: r for r in con.execute(
            "SELECT source, retention_status, retention_days, canonical_metric_id "
            "FROM source_state").fetchall()}
        # A source that has only ever FAILED has no source_state row; it must still be
        # reported, or an unreadable source looks like a source nobody configured.
        run_only = {r[0]: r[1] for r in con.execute(
            "SELECT source, max(metric_id) FROM collection_runs "
            "WHERE store_instance_id=? GROUP BY source", [sid]).fetchall()}
        sources = []
        for src in sorted(set(state) | set(run_only)):
            if src in state:
                _, retention_status, days, canon_metric = state[src]
            else:
                retention_status, days, canon_metric = "unknown", None, run_only[src]
            att = _attempts(con, sid, src)
            row = con.execute(
                "SELECT value_definition_id FROM metric_active_definition WHERE metric_id=?",
                [canon_metric]).fetchone()
            obs = _CO.observed_dates(con, sid, canon_metric, row[0]) if row else []
            last_ok = att.pop("last_success_dt")
            d = _ST.compute(obs, as_of, days,
                            last_ok.date() if last_ok else None,
                            retention_status=retention_status)
            if last_ok is None:
                staleness, stale = None, True
            else:
                # The recorded instant is UTC; the civil day is the analysis zone's.
                local_day = last_ok.replace(tzinfo=timezone.utc).astimezone(zone).date()
                staleness = max(0, (as_of - local_day).days)
                stale = staleness > max_stale
            outcome = att["last_attempt_outcome"]
            reachable = outcome in ("ok", "empty", "partial", "truncated")
            if reachable:
                reason = None
            elif outcome is None:
                reason = "no collection attempt is recorded for this source"
            else:
                reason = att["last_attempt_reason"] or f"last attempt was {outcome!r}"
            sources.append({
                "source": src, "reachable": reachable, "reason": reason,
                "last_observation_date": max(obs).isoformat() if obs else None,
                "stale": stale, "staleness_days": staleness,
                "max_staleness_days": max_stale,
                "durable_since": d.durable_since.isoformat() if d.durable_since else None,
                "permanent_gap_days": sum(g.days for g in d.permanent_gaps),
                "recoverable_gap_days": sum(g.days for g in d.recoverable_gaps),
                "source_history_limit_days": days, "retention_days": days,
                "retention_status": retention_status,
                "unbounded_retention": retention_status == "unbounded",
                "earliest_unresolved_date": (d.earliest_unresolved_date.isoformat()
                                             if d.earliest_unresolved_date else None),
                "last_recoverable_date": (d.last_recoverable_date.isoformat()
                                          if d.last_recoverable_date else None),
                "loss_deadline_date": (d.loss_deadline_date.isoformat()
                                       if d.loss_deadline_date else None),
                "sync_overdue": d.sync_overdue,
                "data_loss_at_risk": d.data_loss_at_risk,
                "days_until_loss": d.days_until_loss,
                "low_retention_mode": d.low_retention_mode,
                "cadence_days": d.cadence_days, "correction_days": d.correction_days,
                "canonical_metric_id": canon_metric,
                "normalization_version": _normalization_versions(
                    con, [canon_metric])[canon_metric] if row else 1,
                **att})
    finally:
        con.close()
    payload = {"schema_version": C.SCHEMA_VERSION, "payload_type": "status",
               "generation": tok["generation"], "as_of_date": as_of.isoformat(),
               "analysis_timezone": tz, "analysis_timezone_source": tz_source,
               "store_instance_id": sid,
               "redaction_level": "aggregate", "sources": sources}
    if tz_note:
        payload["notes"] = [tz_note]
    return P.payload_result(payload, False)


# ------------------------------------------------------------------------- fetch
def _daterange(lo, hi):
    out, d = [], lo
    while d <= hi:
        out.append(d); d += timedelta(days=1)
    return out


def _capped(dates):
    """Return (listed, omitted) under the wire cap for a DateList."""
    iso = [d.isoformat() for d in dates]
    return iso[:DATE_LIST_CAP], max(0, len(iso) - DATE_LIST_CAP)


def real_fetch(args):
    s = _store()
    try:
        con, tok = _open_verified(s, args.get("store_token") or {})
    except _TokenMismatch as e:
        return P.payload_result(e.payload, True)
    sid = tok["store_instance_id"]
    lo, hi = date.fromisoformat(args["start"]), date.fromisoformat(args["end"])
    max_rows = int(args.get("max_rows", 2000))
    try:
        known = {r[0]: r[1] for r in con.execute(
            "SELECT metric_id, value_definition_id FROM metric_active_definition "
            "WHERE metric_id LIKE ?", [f"{SOURCE}.%"]).fetchall()}
        requested = args.get("metrics")
        if requested is None:
            selected, unsupported = sorted(known), []
        else:
            want = sorted(set(requested))
            selected = [m for m in want if m in known]
            unsupported = [m for m in want if m not in known]
        if selected:
            try:
                _normalization_versions(con, selected)
            except NormalizationVersionUnknown as e:
                return P.payload_result(P.error_payload(
                    "storage_error", str(e),
                    "re-register the metric with a complete value definition", False,
                    {"cause": "definition_incomplete", "store_path_id": s.path_id()}),
                    True)
        if not selected and not unsupported:
            # An empty store has no metrics to describe. `metric_status` requires at
            # least one entry, and inventing `<source>.unknown` would name a metric
            # that does not exist, so this is reported as an application error.
            return P.payload_result(P.error_payload(
                "insufficient_evidence",
                f"no metric for source {SOURCE!r} is active in this store",
                "run `mesh sync`", False), True)

        window = _daterange(lo, hi)
        nversions = _normalization_versions(con, selected)
        obs_out, metric_status, total_omitted_rows = [], [], 0
        budget = max_rows
        for m in selected:
            vd = known[m]
            evidence = {r[0]: r[1] for r in con.execute(
                "SELECT date, state FROM metric_date_availability_current "
                "WHERE store_instance_id=? AND metric_id=? AND value_definition_id=? "
                "AND date BETWEEN ? AND ? ORDER BY date",
                [sid, m, vd, lo, hi]).fetchall()}
            observed = [d for d in window if evidence.get(d) == "observed"]
            absent = [d for d in window if evidence.get(d) == "known_absent"]
            perm = [d for d in window if evidence.get(d) == "permanently_missing"]
            undet = [d for d in window if d not in evidence]

            rows = con.execute(
                "SELECT date, value, quality, sample_count FROM observations "
                "WHERE store_instance_id=? AND metric_id=? AND value_definition_id=? "
                "AND date BETWEEN ? AND ? ORDER BY date",
                [sid, m, vd, lo, hi]).fetchall()
            take = rows[:budget] if budget > 0 else []
            dropped = len(rows) - len(take)
            budget -= len(take)
            total_omitted_rows += dropped
            obs_out.extend({"date": r[0].isoformat(), "metric_id": m,
                            "value": float(r[1]), "quality": r[2],
                            "sample_count": int(r[3]),
                            "normalization_version": nversions[m]}
                           for r in take)

            o_list, o_om = _capped(observed)
            a_list, a_om = _capped(absent)
            p_list, p_om = _capped(perm)
            u_list, u_om = _capped(undet)
            if dropped:
                status = "truncated"
            elif observed and not (absent or perm or undet):
                status = "ok"
            elif not observed and not undet:
                status = "empty"
            else:
                status = "partial"
            metric_status.append({
                "metric_id": m, "value_definition_id": vd, "status": status,
                "covered": {"start": lo.isoformat(), "end": hi.isoformat()},
                "observed_dates": o_list, "known_absent_dates": a_list,
                "undetermined_dates": u_list, "permanently_missing_dates": p_list,
                "present_dates": len(observed),
                "omitted_dates_count": o_om + a_om + p_om + u_om,
                "reason": (f"{dropped} row(s) withheld by max_rows={max_rows}"
                           if dropped else None)})
        for m in unsupported:
            metric_status.append({
                "metric_id": m, "value_definition_id": None, "status": "unsupported",
                "covered": None, "observed_dates": [], "known_absent_dates": [],
                "undetermined_dates": [], "permanently_missing_dates": [],
                "present_dates": 0, "omitted_dates_count": 0,
                "reason": f"{m} is not an active metric for source {SOURCE!r}"})
    finally:
        con.close()

    date_cap_hit = any(ms["omitted_dates_count"] for ms in metric_status)
    truncated = bool(total_omitted_rows or date_cap_hit)
    reason = ("row_budget" if total_omitted_rows
              else ("date_list_cap" if date_cap_hit else None))
    kinds = {ms["status"] for ms in metric_status}
    if kinds == {"ok"} and not truncated:
        overall = "ok"
    elif kinds == {"empty"}:
        overall = "empty"
    else:
        overall = "partial"
    payload = {"schema_version": C.SCHEMA_VERSION, "payload_type": "fetch",
               "source": SOURCE, "generation": tok["generation"],
               "store_token": tok,
               "requested": {"start": args["start"], "end": args["end"]},
               "observations": obs_out, "metric_status": metric_status,
               "status": overall,
               "truncation": {"truncated": truncated,
                              "omitted_count": total_omitted_rows + sum(
                                  ms["omitted_dates_count"] for ms in metric_status),
                              "truncation_reason": reason},
               "snapshot_id": f"{sid}:{tok['generation']}",
               # Per SOURCE. When this leaf's metrics record different versions the
               # map cannot represent them individually, so it carries the highest and
               # each observation still names its own.
               "normalization_versions": {SOURCE: max(nversions.values(),
                                                      default=1)}}
    return P.payload_result(payload, False)


def call_tool_result(name, args):
    if name in TEST_ONLY_TOOLS:
        if name == "__boom__":
            raise RuntimeError("deliberate internal fault for testing")
        return P.payload_result(P.error_payload(
            "insufficient_evidence", "test-only tool", "n/a", False), True)
    if STORE_BACKED:
        return real_source_status(args) if name == "source_status" else real_fetch(args)
    if name == "source_status":
        return P.payload_result(
            F.FIXTURES["B5_source_status"]["response"]["result"]["structuredContent"],
            False)
    # No store is selected, so there is no store_path_id to name and `storage_error`
    # would have to invent one. This process cannot serve a store-qualified request at
    # all, which is what `invalid_request` says. (Production leaves are always started
    # with --store by the gateway; this is the test-infrastructure path.)
    return P.payload_result(P.error_payload(
        "invalid_request", f"{name} requires a leaf started with --store; this process "
        f"serves no store", "start the leaf with --store <dir>", False), True)


SERVER = P.Server(
    name=SERVER_INFO["name"], version=SERVER_INFO["version"],
    instructions=("Local quantified-self store, one source per leaf. `source_status` "
                  "reports what this store records about collection attempts; `fetch` "
                  "returns stored facts under one verified store_token. Reports are "
                  "assembled by the gateway, not here."),
    implemented=IMPLEMENTED, call=call_tool_result,
    requires_capability=REQUIRES_CAPABILITY, test_only_tools=TEST_ONLY_TOOLS,
    test_only_capability=TEST_ONLY_CAPABILITY)


def handle(msg):
    return P.handle(msg, SERVER)


def main():
    P.main(SERVER)


if __name__ == "__main__":
    main()
