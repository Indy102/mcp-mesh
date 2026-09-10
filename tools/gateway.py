"""Gateway: MCP server to the client, MCP CLIENT to its leaves, over real stdio.

The gateway reads the store pointer ONCE per report and passes that one `store_token`
to every leaf call, so a report can never be composed from two generations. A leaf that
holds a different generation returns `generation_mismatch`; the gateway re-reads and
retries the WHOLE fan-out, bounded.

Rev 10 trusted `isError == false`. It does not verify identity, so a leaf that opened a
different snapshot between its token pre-check and its real `open_snapshot()` was
accepted. Every SUCCESSFUL response is now checked against the token the attempt was
made under, before any of its data is used:

  same instance, different generation -> mismatch, bounded whole-attempt retry
  different store instance            -> StoreIdentityMismatch, never retried
  no identity in the payload          -> refused; an unverifiable answer is not data
"""
import json, subprocess, sys, threading, queue
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report as RP
from store import digest

PROTOCOL_VERSION = "2026-07-28"
GENERATION_RETRY_BUDGET = 2
READ_TIMEOUT = 15.0
GATEWAY_VERSION = "0.11.0"


class LeafTimeout(RuntimeError):
    pass


class GenerationUnstable(RuntimeError):
    """The store advanced more times than the retry budget allows.

    Carries the LAST OBSERVED current token, so the public error can name it. Rev 11
    RC discarded it and emitted a `generation_mismatch` with no `details`, which the
    unchanged ErrorPayload contract requires for that error_type.
    """
    def __init__(self, message, observed=None):
        super().__init__(message)
        self.observed = observed


class StoreIdentityMismatch(RuntimeError):
    """A leaf answered from a DIFFERENT store instance. Never a retry case.

    Carries the observed store token for the same reason.
    """
    def __init__(self, message, observed=None):
        super().__init__(message)
        self.observed = observed


class LeafProtocolError(RuntimeError):
    """A leaf returned a JSON-RPC error, or an answer whose identity is unverifiable."""


class NoSourcesConfigured(RuntimeError):
    """A report was requested for a mesh that collects nothing."""


def _meta():
    return {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "mesh-gateway",
                                                   "version": GATEWAY_VERSION}}


class Leaf:
    """One stdio leaf subprocess."""
    def __init__(self, source, leaf_path, store_dir, env=None, config=None):
        self.source = source
        cmd = [sys.executable, str(leaf_path), "--source", source,
               "--store", str(store_dir)]
        if config:
            cmd += ["--config", str(config)]
        self.p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        self._id = 0

    def _readline(self):
        q = queue.Queue(maxsize=1)
        threading.Thread(target=lambda: q.put(self.p.stdout.readline()),
                         daemon=True).start()
        try:
            return q.get(timeout=READ_TIMEOUT)
        except queue.Empty:
            self.p.kill()
            raise LeafTimeout(f"{self.source}: no reply within {READ_TIMEOUT}s")

    def call(self, tool, args):
        self._id += 1
        frame = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                 "params": {"name": tool, "arguments": args, "_meta": _meta()}}
        self.p.stdin.write(json.dumps(frame) + "\n"); self.p.stdin.flush()
        return json.loads(self._readline())

    def close(self):
        try:
            self.p.stdin.close(); self.p.wait(timeout=5)
        except Exception:
            self.p.kill()
        for h in (self.p.stdout, self.p.stderr):
            try: h.close()
            except Exception: pass


def response_token(payload):
    """The identity a successful payload claims for the snapshot it actually used.

    `fetch` carries it as `store_token`; `source_status` carries the same pair as two
    top-level fields. Neither is a token ADDED to satisfy the gateway: both name the
    snapshot the numbers came from. A payload that names none cannot be verified.
    """
    tok = payload.get("store_token")
    if isinstance(tok, dict) and {"store_instance_id", "generation"} <= set(tok):
        return {"store_instance_id": tok["store_instance_id"],
                "generation": tok["generation"]}
    if "store_instance_id" in payload and "generation" in payload:
        return {"store_instance_id": payload["store_instance_id"],
                "generation": payload["generation"]}
    return None


class _Mismatch(Exception):
    def __init__(self, current):
        self.current = current


class Gateway:
    def __init__(self, store, leaf_path, sources, config=None):
        self.store, self.leaf_path, self.sources = store, leaf_path, sources
        self.config = config
        # Observable retry evidence: a test must be able to prove the retry path was
        # ENTERED, rather than infer it from a fresh first attempt that happened to
        # land on the newer generation.
        self.attempt_count = 0
        self.mismatches = []

    def _verify(self, source, token, res):
        """Nothing from a response is used before its identity is checked."""
        sc = res.get("structuredContent") or {}
        if res.get("isError"):
            if sc.get("error_type") == "generation_mismatch":
                raise _Mismatch(sc.get("details", {}).get("current_store_token"))
            if sc.get("error_type") == "store_instance_mismatch":
                raise StoreIdentityMismatch(
                    f"{source}: {sc.get('message')}",
                    (sc.get("details") or {}).get("current_store_token"))
            return res
        got = response_token(sc)
        if got is None:
            raise LeafProtocolError(
                f"{source}: a successful payload carries no snapshot identity")
        if got["store_instance_id"] != token["store_instance_id"]:
            raise StoreIdentityMismatch(
                f"{source}: answered from store instance "
                f"{got['store_instance_id'][:8]}…, not {token['store_instance_id'][:8]}…",
                got)
        if got["generation"] != token["generation"]:
            # A SUCCESSFUL answer from the wrong generation. Rev 10 accepted this.
            raise _Mismatch(got)
        return res

    def _fan_out(self, leaves, token, calls):
        """One token for every leaf in one attempt."""
        out = {}
        for s, spec in calls.items():
            for tool, args in (spec if isinstance(spec, list) else [spec]):
                a = dict(args)
                a["store_token"] = token
                r = leaves[s].call(tool, a)
                res = r.get("result")
                if res is None:
                    # A routed JSON-RPC error is not data, but it is also not a reason
                    # to abandon the whole report: it is reported as an unavailable
                    # source, with the error as the stated reason.
                    out[(s, tool)] = {"error": r.get("error")}
                    continue
                out[(s, tool)] = self._verify(s, token, res)
        return out

    def fan_out_many(self, calls):
        """calls: {source: [(tool, args), ...]} -> (token, {(source, tool): result}).

        The leaf SUBPROCESSES are started once and reused across retry attempts. A leaf
        holds no per-attempt state — it re-reads the pointer and re-opens a snapshot on
        every call — so respawning would only cost time.
        """
        self.attempt_count, self.mismatches = 0, []
        leaves = {}
        try:
            for s in self.sources:
                leaves[s] = Leaf(s, self.leaf_path, self.store.dir, config=self.config)
            while True:
                token = self.store.token()
                self.attempt_count += 1
                try:
                    return token, self._fan_out(leaves, token, calls)
                except _Mismatch as m:
                    self.mismatches.append(m.current)
                    if self.attempt_count > GENERATION_RETRY_BUDGET:
                        observed = next((t for t in reversed(self.mismatches) if t), None)
                        raise GenerationUnstable(
                            f"generation advanced {len(self.mismatches)} times during "
                            f"one report; giving up after {self.attempt_count} attempts",
                            observed or self.store.token())
        finally:
            for lf in leaves.values():
                lf.close()

    def fan_out(self, calls):
        """Back-compatible single-call-per-source form."""
        token, out = self.fan_out_many({s: [c] for s, c in calls.items()})
        return token, {s: out[(s, tool)] for s, (tool, _) in calls.items()}


# ------------------------------------------------------------------ report assembly
CAVEATS = ["Descriptive only; no inference is enabled.",
           "Weekly values are means over PRESENT days; missing days are excluded, "
           "never imputed.",
           "Measures the configured sources under their recorded value definitions, "
           "not personal productivity."]


def _unavailable(source, reason):
    return {"source": source, "available": False, "reason": reason,
            "metrics_contributed": 0, "last_observation_date": None}


MAX_REPORT_ROWS = 2000          # FetchPayload.observations maxItems


def assemble_report(gw, *, as_of, week_of=None, min_meaningful_delta=None,
                    unavailable_sources=(), max_rows=MAX_REPORT_ROWS):
    """The PRODUCTION report. Facts arrive over the leaf transport; nothing here reads
    the database directly, and nothing is supplied by a caller except the request.

    Every leaf answers under ONE verified token, so `generation` and
    `store_instance_id` in the report name the single snapshot the whole report came
    from.
    """
    # Refused BEFORE any work: a mesh that collects nothing cannot describe a source,
    # and a report describing none would be a schema-invalid payload dressed as a
    # result. A mesh whose sources are all DISABLED is legitimate — it reports them.
    if not gw.sources and not unavailable_sources:
        raise NoSourcesConfigured(
            "no source is configured, enabled or disabled: a report would describe "
            "nothing")
    week_start, week_end = (RP.iso_week_bounds(week_of) if week_of
                            else RP.default_week(as_of))
    lo, hi = RP.baseline_span(week_start), week_end
    calls = {s: [("source_status", {}),
                 ("fetch", {"start": lo.isoformat(), "end": hi.isoformat(),
                            "max_rows": max_rows})]
             for s in gw.sources}
    token, out = gw.fan_out_many(calls)

    # The report's policy_id names the WEEKLY NUMBER CONTRACT this report applied,
    # which the gateway owns. It is not the store's collection policy.
    policy_id = digest("policy", RP.report_policy())
    changes, source_status, vdids = [], [], set()
    for s in gw.sources:
        st = out[(s, "source_status")]
        fx = out[(s, "fetch")]
        st_sc = st.get("structuredContent") or {}
        fx_sc = fx.get("structuredContent") or {}
        if "error" in st or "error" in fx:
            err = st.get("error") or fx.get("error") or {}
            source_status.append(_unavailable(
                s, f"leaf protocol error {err.get('code')}: {err.get('message')}"))
            continue
        if st.get("isError"):
            source_status.append(_unavailable(s, st_sc.get("message", "status failed")))
            continue
        reachable = {r["source"]: r for r in st_sc["sources"]}.get(s)
        if fx.get("isError"):
            source_status.append(_unavailable(s, fx_sc.get("message", "fetch failed")))
            continue
        by_metric = {}
        for o in fx_sc["observations"]:
            by_metric.setdefault(o["metric_id"], {})[
                RP.date.fromisoformat(o["date"])] = o["value"]
        contributed = 0
        for ms in fx_sc["metric_status"]:
            m = ms["metric_id"]
            if ms["status"] == "unsupported":
                changes.append({"status": "unknown", "metric_id": m,
                                "reason": "no_metric_range"})
                continue
            if ms["status"] == "truncated":
                # A complete-looking baseline must never be computed from a fetch that
                # withheld rows.
                changes.append({"status": "unknown", "metric_id": m,
                                "reason": "evidence_truncated"})
                continue
            vdids.add(ms["value_definition_id"])
            changes.append(RP.observed_change_from_facts(
                by_metric.get(m, {}), metric_id=m, week_start=week_start,
                week_end=week_end, min_meaningful_delta=min_meaningful_delta))
            if by_metric.get(m):
                contributed += 1
        obs_dates = [d for v in by_metric.values() for d in v]
        # `available` says whether this source CONTRIBUTED facts to this report.
        # Current reachability and freshness are a different question, answered by the
        # leaf from recorded attempts; when they disagree with a contributing source,
        # the caveat is carried in `reason` rather than hidden.
        rs = reachable or {}
        available = contributed > 0
        if not available:
            reason = rs.get("reason") or "no metric contributed facts to this report"
        elif not rs.get("reachable", True):
            reason = ("facts from earlier runs are retained; the last collection "
                      f"attempt failed: {rs.get('reason')}")
        elif rs.get("stale"):
            reason = (f"data is stale: {rs.get('staleness_days')} day(s) since the last "
                      f"successful sync (limit {rs.get('max_staleness_days')})")
        else:
            reason = None
        source_status.append({
            "source": s, "available": available, "reason": reason,
            "metrics_contributed": contributed,
            "last_observation_date": max(obs_dates).isoformat() if obs_dates else None})
    for s, why in unavailable_sources:
        source_status.append(_unavailable(s, why))

    if not vdids:
        # Nothing contributed. Naming a definition here would be a fabricated identity.
        vdid = None
    elif len(vdids) == 1:
        vdid = next(iter(vdids))
    else:
        vdid = digest("report_value_definitions", {"ids": sorted(vdids)})
    return {
        "schema_version": 7, "payload_type": "report",
        "generation": token["generation"],
        "store_instance_id": token["store_instance_id"],
        "value_definition_id": vdid, "policy_id": policy_id,
        "recommendations_enabled": False,
        "recommendations_disabled_reason": "inference_disabled",
        "as_of_date": as_of.isoformat(),
        "week": {"start": week_start.isoformat(), "end": week_end.isoformat()},
        "evaluation_id": None, "observed_changes": changes,
        "source_status": source_status, "associations": [], "recommendations": [],
        "notes": [], "truncation": {"truncated": False, "omitted_count": 0,
                                    "truncation_reason": None},
        "redaction_level": "aggregate",
        "summary_text": (f"Descriptive weekly report for "
                         f"{week_start.isoformat()}..{week_end.isoformat()} from "
                         f"generation {token['generation']}."),
        "method_caveats": list(CAVEATS)}
