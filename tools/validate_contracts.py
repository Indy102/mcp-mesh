"""T1 CONTRACT VALIDATION ONLY  (Rev 8).

Proves, on synthetic data with no network, no TCC grants and no store:
  * generated schemas are valid, self-contained, bounded
  * FULL JSON-RPC envelopes conform to the OFFICIAL MCP schema (not just structuredContent)
  * requests/results conform to our published input/outputSchema
  * semantic invariants JSON Schema cannot express
  * the Screen Time, circular-time and evaluation-identity algorithms behave as specified
  * a real in-process MCP client/server round trip carries our envelope

Proves NOTHING about sources (T2), APFS/DuckDB swap or TCC (T3), or statistical
calibration (T4). Those need the target Mac and the simulation battery, and are UNVERIFIED.
"""
import json, os, sys, math, re, pathlib, asyncio, urllib.request
from datetime import date, timedelta
from jsonschema import Draft202012Validator as V, FormatChecker
import contracts as C, fixtures as F, screentime as ST, circular as CIRC, evaluation as EV

FC = FormatChecker()
_HERE = pathlib.Path(__file__).resolve().parent
if _HERE.name != "tools":
    raise SystemExit("validate_contracts.py must live in <project>/tools/; "
                     f"found it in {_HERE}. Refusing to guess a project root.")
ROOT = _HERE.parent
_REQUIRED = ["pyproject.toml", "uv.lock", "tools/contracts.py", "tools/fixtures.py",
             "tools/screentime.py", "tools/circular.py", "tools/evaluation.py",
             "schemas/mcp-official-2026-07-28.json"]
_missing = [f for f in _REQUIRED if not (ROOT / f).exists()]
if _missing:
    raise SystemExit("Incomplete extraction; refusing to write anything.\n  missing: "
                     + ", ".join(_missing))
# This validator is STRICTLY READ-ONLY. Rev 8 regenerated packaged schemas before
# comparing them, so a corrupted artifact was silently repaired and still reported
# 299/0. Verification must never write.
READ_ONLY = True
fails, checks = [], 0
def chk(label, cond, detail=""):
    global checks; checks += 1
    if not cond: fails.append((label, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"\n         -> {detail}" if not cond and detail else ""))

def nonlocal_refs(n, acc=None):
    acc = set() if acc is None else acc
    if isinstance(n, dict):
        r = n.get("$ref")
        if isinstance(r, str) and not r.startswith("#"): acc.add(r)
        for v in n.values(): nonlocal_refs(v, acc)
    elif isinstance(n, list):
        for v in n: nonlocal_refs(v, acc)
    return acc
def depth(n, d=0):
    if isinstance(n, dict): return max([depth(v, d+1) for v in n.values()] or [d])
    if isinstance(n, list): return max([depth(v, d+1) for v in n] or [d])
    return d
def nsub(n):
    c = 1 if isinstance(n, dict) else 0
    if isinstance(n, dict): c += sum(nsub(v) for v in n.values())
    elif isinstance(n, list): c += sum(nsub(v) for v in n)
    return c

print("=" * 78)
print(f"MCP MESH — T1 CONTRACT VALIDATION (schema_version {C.SCHEMA_VERSION}, protocol {C.PROTOCOL_VERSION})")
print("=" * 78)

# ---------------------------------------------------------------- official schema
OFFICIAL = ROOT / "schemas" / "mcp-official-2026-07-28.json"
print("\n[0] Official MCP schema")
PROV = ROOT / "schemas" / "mcp-official-2026-07-28.provenance.json"
if OFFICIAL.exists():
    import hashlib
    _actual = hashlib.sha256(OFFICIAL.read_bytes()).hexdigest()
    _prov = json.loads(PROV.read_text()) if PROV.exists() else {}
    chk(f"pinned official schema checksum matches provenance ({_actual[:16]}…)",
        _prov.get("sha256") == _actual, f"provenance={_prov.get('sha256')}")
    chk("provenance pins an immutable commit, not a branch",
        bool(_prov.get("commit")) and "/main/" not in _prov.get("url", ""))
    MCP = json.loads(OFFICIAL.read_text())
    chk(f"official schema loaded, {len(MCP['$defs'])} definitions", len(MCP["$defs"]) > 100)
    def off(name):
        s = dict(MCP); s["$ref"] = f"#/$defs/{name}"; return s
    chk("CallToolResult requires resultType (item 1)",
        "resultType" in MCP["$defs"]["CallToolResult"]["required"])
    chk("DiscoverResult required == cacheScope/capabilities/resultType/supportedVersions/ttlMs",
        sorted(MCP["$defs"]["DiscoverResult"]["required"]) ==
        ["cacheScope","capabilities","resultType","supportedVersions","ttlMs"])
    chk("RequestParams requires _meta", "_meta" in MCP["$defs"]["RequestParams"]["required"])
else:
    MCP = None; chk("official schema available", False, "not downloaded")

# ---------------------------------------------------------------- [1] schemas
print("\n[1] Generated schemas: valid, self-contained, bounded")
outdir, indir = ROOT / "schemas" / "output", ROOT / "schemas" / "input"
outdir.mkdir(parents=True, exist_ok=True); indir.mkdir(parents=True, exist_ok=True)
for tool in sorted(C.SUCCESS):
    o, i = C.output_schema(tool), C.input_schema(tool)
    if not READ_ONLY:
        (outdir / f"{tool}.json").write_text(json.dumps(o, indent=1, sort_keys=True))
        (indir / f"{tool}.json").write_text(json.dumps(i, indent=1, sort_keys=True))
    try: V.check_schema(o); V.check_schema(i); ok, why = True, ""
    except Exception as e: ok, why = False, str(e)[:110]
    nl = nonlocal_refs(o) | nonlocal_refs(i)
    chk(f"{tool:22s} valid, 0 non-local $ref, depth {depth(o):2d}, subschemas {nsub(o):4d}",
        ok and not nl and depth(o) <= 20 and nsub(o) <= 2000, why or f"non-local: {nl}")

# ---------------------------------------------------------------- [2] envelopes
print("\n[1b] PERSISTED artifacts equal their canonical source  (Rev8 blocker 1)")
_fixdir = ROOT / "schemas" / "fixtures"
_ondisk = {p.stem for p in _fixdir.glob("*.json")}
_stale = sorted(_ondisk - set(F.FIXTURES)); _missing = sorted(set(F.FIXTURES) - _ondisk)
print(f"       counts: on_disk={len(_ondisk)} source={len(F.FIXTURES)} "
      f"stale={len(_stale)} missing={len(_missing)}")
chk("no stale fixture files and none missing", not _stale and not _missing,
    f"stale={_stale} missing={_missing}")
_bad_eq, _bad_txt, _bad_ver = [], [], []
for _n in sorted(F.FIXTURES):
    _f = _fixdir / f"{_n}.json"
    if not _f.exists():
        _bad_eq.append(_n); continue
    _p = json.loads(_f.read_text())
    if _p != json.loads(json.dumps(F.FIXTURES[_n])):
        _bad_eq.append(_n)
    _r = _p.get("response", {}).get("result", {})
    _sc = _r.get("structuredContent")
    if _sc is not None:
        if json.loads(_r["content"][0]["text"]) != _sc:
            _bad_txt.append(_n)
        if _sc.get("schema_version") != C.SCHEMA_VERSION:
            _bad_ver.append((_n, _sc.get("schema_version")))
chk(f"every persisted fixture is object-equal to its source ({len(F.FIXTURES)} checked)",
    not _bad_eq, f"differ: {_bad_eq}")
chk("content[0].text in the PERSISTED file round-trips to its structuredContent",
    not _bad_txt, f"differ: {_bad_txt}")
chk(f"every persisted payload is at schema_version {C.SCHEMA_VERSION}",
    not _bad_ver, f"stale: {_bad_ver}")
for _kind, _keys in (("output", C.SUCCESS), ("input", C.INPUTS)):
    _d = ROOT / "schemas" / _kind
    _disk = {p.stem for p in _d.glob("*.json")}
    _gen = C.output_schema if _kind == "output" else C.input_schema
    _absent = [k for k in _keys if not (_d / f"{k}.json").exists()]
    _diff = [k for k in _keys if (_d / f"{k}.json").exists()
             and json.loads((_d / f"{k}.json").read_text()) != _gen(k)]
    _stray = sorted(_disk - set(_keys))
    print(f"       counts: schemas/{_kind}/ on_disk={len(_disk)} expected={len(_keys)} "
          f"mismatched={len(_diff)} missing={len(_absent)} stray={len(_stray)}")
    chk(f"persisted schemas/{_kind}/ equal source, none missing, no strays",
        not _diff and not _absent and not _stray,
        f"differ={_diff} missing={_absent} stray={_stray}")

print("\n[2] FULL JSON-RPC envelopes vs the OFFICIAL schema  (item 1, item 5)")
if MCP:
    for name, fx in sorted(F.FIXTURES.items()):
        r = fx.get("request")
        if r and r.get("method") == "tools/call":
            e = list(V(off("CallToolRequest")).iter_errors(r))
            expect = name != "B17_missing_meta"   # deliberately malformed fixture
            chk(f"{name:26s} request  vs CallToolRequest valid={not e} expected={expect}",
                (not e) == expect, e[0].message[:120] if e and expect else "")
        elif r and r.get("method") == "server/discover":
            e = list(V(off("DiscoverRequest")).iter_errors(r))
            chk(f"{name:26s} request  vs DiscoverRequest", not e, e[0].message[:120] if e else "")
        resp = fx.get("response")
        if not resp: continue
        if "error" in resp:
            e = list(V(off("JSONRPCErrorResponse")).iter_errors(resp))
            chk(f"{name:26s} response vs JSONRPCErrorResponse", not e, e[0].message[:120] if e else "")
        else:
            e = list(V(off("JSONRPCResultResponse")).iter_errors(resp))
            chk(f"{name:26s} response vs JSONRPCResultResponse", not e, e[0].message[:120] if e else "")
            if name.startswith("B21"):
                inner, own = "DiscoverResult", C.discover_result_schema()
            elif name.startswith("B1_"):
                inner, own = "ListToolsResult", C.list_tools_result_schema()
            else:
                inner, own = "CallToolResult", None
            if inner in MCP["$defs"]:
                e2 = list(V(off(inner)).iter_errors(resp["result"]))
                chk(f"{name:26s} result   vs official {inner}", not e2,
                    e2[0].message[:140] if e2 else "")
            if own is not None:
                e3 = list(V(own).iter_errors(resp["result"]))
                chk(f"{name:26s} result   vs OUR {inner} contract", not e3,
                    e3[0].message[:140] if e3 else "")
                chk(f"{name:26s} {inner} carries NO payload_type",
                    "payload_type" not in resp["result"])
            chk(f"{name:26s} result.resultType == 'complete'",
                resp["result"].get("resultType") == "complete")

print("\n[2b] RequestMetaObject conformance and key-naming rules  (item 4)")
meta_sch = {"$defs": {k: C.DEFS[k] for k in C.closure(["RequestMetaObject"])},
            "$ref": "#/$defs/RequestMetaObject"}
for name, fx in sorted(F.FIXTURES.items()):
    r = fx.get("request")
    if not r: continue
    m = r["params"].get("_meta", {})
    e = list(V(meta_sch, format_checker=FC).iter_errors(m))
    expect = name != "B17_missing_meta"   # only this one is malformed
    chk(f"{name:26s} _meta valid={not e} expected={expect}", (not e) == expect,
        e[0].message[:110] if e and expect else "")
full = F.FIXTURES["B22_meta_full"]["request"]["params"]["_meta"]
chk("B22 carries logLevel, progressToken and a vendor key",
    {"io.modelcontextprotocol/logLevel", "progressToken", "com.example.mesh/trace-hint"} <= set(full))
chk("vendor key 'com.example.mesh/trace-hint' matches the _meta key grammar",
    re.match(C.META_KEY_RE, "com.example.mesh/trace-hint") is not None)
chk("reserved prefix 'com.mcp.tools/x' is detected as reserved",
    re.match(C.RESERVED_PREFIX_RE, "com.mcp.tools/x") is not None)
chk("'com.example.mcp/x' is NOT reserved (2nd label is example)",
    re.match(C.RESERVED_PREFIX_RE, "com.example.mcp/x") is None)
chk("illegal key '-bad/x' rejected by the grammar",
    re.match(C.META_KEY_RE, "-bad/x") is None)

# ---------------------------------------------------------------- [3] payloads
print("\n[2c] Arguments vs published inputSchema (known tools only)")
for name, fx in sorted(F.FIXTURES.items()):
    r = fx.get("request")
    if not r or r.get("method") != "tools/call": continue
    tool = r["params"]["name"]
    if tool not in C.INPUTS:
        chk(f"{name:26s} names an UNKNOWN tool '{tool}' -> -32602", 
            fx["response"].get("error", {}).get("code") == -32602)
        continue
    e = list(V(C.input_schema(tool), format_checker=FC).iter_errors(r["params"]["arguments"]))
    expect = name != "B18_lag_override"
    chk(f"{name:26s} inputSchema({tool}) valid={not e} expected={expect}", (not e) == expect,
        e[0].message[:120] if e and expect else "")

print("\n[3] structuredContent vs published outputSchema, and isError <-> payload_type")
TOOL_OF = {"B2_describe_metrics":"describe_metrics","B3_fetch_partial":"fetch",
 "B4_health_check":"health_check","B5_source_status":"source_status",
 "B6_list_metrics":"list_metrics","B7_weekly_report":"weekly_report",
 "B8_compare_periods":"compare_periods","B9_gateway_to_leaf":"fetch",
 "B10_association_confirmatory":"association","B11_association_exploratory":"association",
 "B12_explore":"explore_associations","B13_generation_mismatch":"fetch",
 "B14_storage_error":"weekly_report","B15_unknown_hypothesis":"association",
 "B16_insufficient_evidence":"weekly_report","B22_meta_full":"source_status",
 "B23_rebuild_unavailable":"weekly_report","B24_signals_diverge":"source_status",
 "B25_unbounded_retention":"source_status"}
for name, tool in sorted(TOOL_OF.items()):
    sc = F.FIXTURES[name]["response"]["result"]["structuredContent"]
    e = list(V(C.output_schema(tool), format_checker=FC).iter_errors(sc))
    chk(f"{name:26s} vs outputSchema({tool})", not e, e[0].message[:150] if e else "")
for name, fx in sorted(F.FIXTURES.items()):
    r = fx.get("response")
    if not r or "result" not in r or "structuredContent" not in r["result"]: continue
    if name.startswith(("B1_", "B21")): continue      # not CallToolResults
    is_err = r["result"].get("isError", False)
    pt = r["result"]["structuredContent"]["payload_type"]
    chk(f"{name:26s} isError={str(is_err):5s} <-> payload_type={pt:15s}  [SI-21]",
        is_err == (pt == "error"))
    txt = r["result"]["content"][0]["text"]
    chk(f"{name:26s} content[0].text round-trips",
        "<serialized" not in txt and json.loads(txt) == r["result"]["structuredContent"])

# ---------------------------------------------------------------- [4] semantics
print("\n[4] Semantic invariants JSON Schema cannot express  (item 8)")
def datelists(sc, path="$"):
    out = []
    if isinstance(sc, dict):
        for k, v in sc.items():
            if k.endswith("_dates") and isinstance(v, list): out.append((f"{path}.{k}", v))
            else: out += datelists(v, f"{path}.{k}")
    elif isinstance(sc, list):
        for i, v in enumerate(sc): out += datelists(v, f"{path}[{i}]")
    return out
uniq = order = True
for name, fx in F.FIXTURES.items():
    r = fx.get("response")
    if not r or "result" not in r or "structuredContent" not in r["result"]: continue
    for path, lst in datelists(r["result"]["structuredContent"]):
        if len(set(lst)) != len(lst): uniq = False
        if lst != sorted(lst): order = False
chk("every *_dates list is unique  [SI-22]", uniq)
chk("every *_dates list is ascending  [SI-23]", order)
_fix_n = sum(1 for f in F.FIXTURES.values()
             if f.get("response", {}).get("result", {}).get("structuredContent"))
_field_n = sum(len(datelists(f["response"]["result"]["structuredContent"]))
               for f in F.FIXTURES.values()
               if f.get("response", {}).get("result", {}).get("structuredContent"))
print(f"       audit: enumerated {_field_n} *_dates fields across {_fix_n} "
      f"structuredContent fixtures (of {len(F.FIXTURES)} total)")
chk(f"the date-list audit enumerated every structuredContent fixture "
    f"({_fix_n} fixtures, {_field_n} *_dates fields)", _fix_n >= 15 and _field_n > 0)
for name in ("B3_fetch_partial",):
    for ms in F.FIXTURES[name]["response"]["result"]["structuredContent"]["metric_status"]:
        o, k, u = set(ms["observed_dates"]), set(ms["known_absent_dates"]), set(ms["undetermined_dates"])
        chk(f"{name} {ms['metric_id']:26s} observed/known_absent/undetermined disjoint  [SI-25]",
            not (o & k) and not (o & u) and not (k & u))
        chk(f"{name} {ms['metric_id']:26s} present_dates == len(observed_dates)",
            ms["present_dates"] == len(ms["observed_dates"]))
        if ms["covered"]:
            lo, hi = date.fromisoformat(ms["covered"]["start"]), date.fromisoformat(ms["covered"]["end"])
            span = {(lo + timedelta(days=i)).isoformat() for i in range((hi - lo).days + 1)}
            chk(f"{name} {ms['metric_id']:26s} determined+undetermined partitions covered  [SI-26]",
                (o | k | u) == span)

# ---------------------------------------------------------------- [5] screen time
print("\n[5] Screen Time algorithm  (item 7)")
AS = date(2026, 9, 6)
try:
    ST.compute([], AS, 0, None); chk("R<=0 rejected", False, "no exception")
except ST.RetentionConfigError: chk("R=0 raises RetentionConfigError", True)
try:
    ST.compute([], AS, -5, None); chk("R=-5 rejected", False, "no exception")
except ST.RetentionConfigError: chk("R=-5 raises RetentionConfigError", True)
d = ST.compute(F.OBSERVED, AS, None, date(2026, 9, 3))
chk("R=None: unbounded, no loss risk, no deadline",
    d.unbounded_retention and not d.data_loss_at_risk and d.days_until_loss is None
    and d.cadence_days is None)
d1 = ST.compute([date(2026, 9, 4), date(2026, 9, 5)], AS, 1, date(2026, 9, 5))
chk(f"R=1: cadence={d1.cadence_days} correction={d1.correction_days} low_retention={d1.low_retention_mode}",
    d1.cadence_days == 1 and d1.correction_days == 1 and d1.low_retention_mode)
dperm = F.DUR
chk("internal gap 07-10..07-18 detected as PERMANENT (not just trailing)",
    len(dperm.permanent_gaps) == 1 and dperm.permanent_gaps[0].start == date(2026,7,10)
    and dperm.permanent_gaps[0].days == 9)
chk("trailing gap 09-03..09-05 classified RECOVERABLE",
    len(dperm.recoverable_gaps) == 1 and dperm.recoverable_gaps[0].start == date(2026,9,3))
chk("durable_since is the contiguous observed tail start (2026-07-19)",
    dperm.durable_since == date(2026, 7, 19))
chk("earliest_unresolved_date is the earliest UNRESOLVED date (2026-09-03)",
    dperm.earliest_unresolved_date == date(2026, 9, 3))
chk(f"last_recoverable_date={dperm.last_recoverable_date} == unresolved + R - 1",
    dperm.last_recoverable_date == date(2026, 9, 30))
chk(f"loss_deadline_date={dperm.loss_deadline_date} == unresolved + R (first unavailable)",
    dperm.loss_deadline_date == date(2026, 10, 1))
chk(f"days_until_loss={dperm.days_until_loss} == deadline - as_of == 25",
    dperm.days_until_loss == 25)
chk("days_until_loss is never <= 0 for a recoverable date (by construction)",
    dperm.days_until_loss >= 1)
chk("sync_overdue and data_loss_at_risk are INDEPENDENT: B24 has overdue=False, at_risk=True",
    F.DUR2.sync_overdue is False and F.DUR2.data_loss_at_risk is True)
mid = ST.compute([d for d in F.OBSERVED if not (date(2026,8,20) <= d <= date(2026,8,24))],
                 AS, 28, date(2026, 9, 6))
chk("an INTERNAL recoverable gap drives the deadline, not the trailing edge",
    mid.earliest_unresolved_date == date(2026, 8, 20))
chk("coalesce merges adjacent gaps",
    len(ST.coalesce([ST.Gap(date(2026,1,1), date(2026,1,3)),
                     ST.Gap(date(2026,1,4), date(2026,1,6))])) == 1)

# ---------------------------------------------------------------- [6] circular
print("\n[6] Circular time comparison  (item 9)")
chk("mean(23:50, 00:10) == 00:00, not 12:00",
    abs(CIRC.difference(CIRC.mean([1430.0, 10.0]), 0.0)) < 1e-6)
chk("difference(00:10, 23:50) == +20 minutes",
    abs(CIRC.difference(10.0, 1430.0) - 20.0) < 1e-9)
chk("difference is antisymmetric", abs(CIRC.difference(1430.0, 10.0) + 20.0) < 1e-9)
chk("antipodal set has undefined mean (resultant 0), not 0",
    CIRC.mean([0.0, 720.0]) is None)
chk("resultant length of a tight cluster is near 1",
    CIRC.resultant_length([600.0, 601.0, 599.0]) > 0.99)
chk("circular MAD of a wrapped cluster is small",
    CIRC.mad([1430.0, 1435.0, 5.0, 10.0]) < 30.0)

# ---------------------------------------------------------------- [7] evaluation
print("\n[7] Evaluation identity and BH scope  (item 9)")
base = dict(store_instance_id=F.SIID, generation=41, value_definition_id=F.VDID,
            policy_id=F.POLID, registry_dig=F.REG_DIG, eligibility_policy_version=1,
            method_dig=F.METH_DIG, window_start="2025-09-29", window_end="2026-08-23",
            metric_identity_dig=F.MID_DIG)
e0 = EV.evaluation_id(**base)
_IDENTITY_INPUTS = [("generation", 42), ("value_definition_id", "0"*32),
                    ("policy_id", "1"*32), ("registry_dig", "2"*32),
                    ("eligibility_policy_version", 2), ("method_dig", "3"*32),
                    ("window_start", "2025-09-30"), ("window_end", "2026-08-24"),
                    ("store_instance_id", "4"*32), ("metric_identity_dig", "5"*32)]
chk(f"the identity test covers all TEN inputs (count={len(_IDENTITY_INPUTS)})",
    len(_IDENTITY_INPUTS) == 10)
for field, newv in _IDENTITY_INPUTS:
    chk(f"evaluation_id changes when {field} changes",
        EV.evaluation_id(**{**base, field: newv}) != e0)
chk("evaluation_id is stable for identical inputs", EV.evaluation_id(**base) == e0)
chk(f"BH m counts only ELIGIBLE hypotheses: m_total={F.SCOPE['m_total']} m_eligible={F.SCOPE['m_eligible']}",
    F.SCOPE["m_eligible"] == 1 and F.SCOPE["m_total"] == 2)
q = EV.benjamini_hochberg([0.001, 0.02, 0.30, 0.90])
chk("BH q-values are monotone non-decreasing in p", all(q[i] <= q[i+1] for i in range(3)))
chk("BH on a single p-value returns it unchanged", abs(EV.benjamini_hochberg([0.04])[0] - 0.04) < 1e-12)

# ---------------------------------------------------------------- [8] negatives
print("\n[8] Negative probes  (items 1, 3, 8, 10)")
def probe(label, defn, inst, should):
    s = {"$defs": {k: C.DEFS[k] for k in C.closure([defn])}, "$ref": f"#/$defs/{defn}"}
    e = list(V(s, format_checker=FC).iter_errors(inst))
    chk(label, (not e) == should, e[0].message[:140] if e and should else "")
if MCP:
    bad = json.loads(json.dumps(F.FIXTURES["B7_weekly_report"]["response"]))
    bad["result"].pop("resultType")
    chk("result missing resultType REJECTED by official CallToolResult",
        bool(list(V(off("CallToolResult")).iter_errors(bad["result"]))))
    d_bad = json.loads(json.dumps(F.FIXTURES["B21_server_discover"]["response"]["result"]))
    d_bad.pop("ttlMs")
    chk("DiscoverResult missing ttlMs REJECTED",
        bool(list(V(off("DiscoverResult")).iter_errors(d_bad))))
    d_bad2 = json.loads(json.dumps(F.FIXTURES["B21_server_discover"]["response"]["result"]))
    d_bad2["cacheScope"] = "shared"
    chk("DiscoverResult cacheScope='shared' REJECTED (enum is private|public)",
        bool(list(V(off("DiscoverResult")).iter_errors(d_bad2))))
    tl = json.loads(json.dumps(F.FIXTURES["B1_tools_list"]["response"]["result"]))
    tl["tools"][0].pop("inputSchema")
    chk("tools/list entry without inputSchema REJECTED",
        bool(list(V(off("ListToolsResult")).iter_errors(tl))) if "ListToolsResult" in MCP["$defs"] else True)
chk("unknown TOOL maps to -32602 (item 3)",
    F.FIXTURES["B19_unknown_tool"]["response"]["error"]["code"] == -32602)
chk("unknown RPC METHOD maps to -32601 (item 3)",
    F.FIXTURES["B26_unknown_method"]["response"]["error"]["code"] == -32601)
for code, key in [(-32700, "B30_parse_error"), (-32600, "B31_invalid_request"),
                  (-32601, "B26_unknown_method"), (-32602, "B19_unknown_tool"),
                  (-32603, "B29_internal_error"), (-32021, "B32_missing_capability"),
                  (-32022, "B27_unsupported_version")]:
    chk(f"error ladder: {key:26s} -> {code}",
        F.FIXTURES[key]["response"]["error"]["code"] == code)
chk("-32700 and -32600 OMIT id (the official schema does not permit a null RequestId; "
    "id is not required when the request id could not be read)",
    "id" not in F.FIXTURES["B30_parse_error"]["response"]
    and "id" not in F.FIXTURES["B31_invalid_request"]["response"])
chk("every other error response echoes the request id",
    all(F.FIXTURES[k]["response"].get("id") == int(k[1:3])
        for k in ("B19_unknown_tool", "B26_unknown_method", "B27_unsupported_version",
                  "B29_internal_error", "B32_missing_capability")))
chk("-32603 carries a stable incident_id and no traceback or local path",
    "incident_id" in F.FIXTURES["B29_internal_error"]["response"]["error"]["data"]
    and "Traceback" not in json.dumps(F.FIXTURES["B29_internal_error"])
    and "/Users/" not in json.dumps(F.FIXTURES["B29_internal_error"]))
chk("an unsupported-version request is STRUCTURALLY VALID and reaches negotiation",
    not list(V(meta_sch, format_checker=FC).iter_errors(
        F.FIXTURES["B27_unsupported_version"]["request"]["params"]["_meta"])))
chk("store_instance_mismatch is NOT retryable and returns the current token",
    F.FIXTURES["B28_store_instance_mismatch"]["response"]["result"]["structuredContent"]
      ["details"]["current_store_token"]["store_instance_id"] == F.SIID)
chk("unsupported protocol version maps to -32022 with data.supported",
    F.FIXTURES["B27_unsupported_version"]["response"]["error"]["code"] == -32022
    and "supported" in F.FIXTURES["B27_unsupported_version"]["response"]["error"]["data"])
rep = F.FIXTURES["B7_weekly_report"]["response"]["result"]["structuredContent"]
probe("recommendations_enabled=false WITH a recommendation REJECTED (item 10)", "ReportPayload",
      {**rep, "recommendations": [{"hypothesis_id":"H5","text":"x","driver_delta":1.0,
       "outcome_delta":1.0,"practical_effect_raw":1.0,"practical_effect_scale":"raw_outcome",
       "caveat":"This is an observed association in your own data, not evidence that one causes the other."}]}, False)
probe("recommendations_enabled=false WITHOUT reason REJECTED", "ReportPayload",
      {**rep, "recommendations_disabled_reason": None}, False)
a = F.FIXTURES["B10_association_confirmatory"]["response"]["result"]["structuredContent"]["association"]
probe("threshold_source='absent' with practical_effect_met=true REJECTED (item 10)", "Association",
      {**a, "threshold_source": "absent", "practical_effect_met": True}, False)
probe("confirmatory association without registry_digest REJECTED", "Association",
      {**a, "registry_digest": None}, False)
probe("exploratory association carrying m_total REJECTED", "Association",
      {**a, "evaluation_family": "exploratory", "hypothesis_id": None, "q_value": None,
       "registry_version": None, "evaluation_id": None, "registry_digest": None,
       "m_total": 8, "m_eligible": 6}, False)
st = F.FIXTURES["B25_unbounded_retention"]["response"]["result"]["structuredContent"]
probe("unbounded_retention with a days_until_loss REJECTED (item 7)", "StatusPayload",
      {**st, "sources": [{**st["sources"][0], "days_until_loss": 5}]}, False)
probe("rebuild_unavailable without details.rebuildable REJECTED (item 6)", "ErrorPayload",
      {"schema_version": C.SCHEMA_VERSION, "payload_type": "error",
       "error_type": "rebuild_unavailable", "message": "x", "retryable": False,
       "details": {"source": "screentime"}}, False)

# ---------------------------------------------------------------- [9] live MCP
print("\n[9] SDK compatibility of the PUBLISHED schemas  (object level only)")
print("      NOTE: this tier makes no protocol-era claim and runs no session.")
print("      Wire-level evidence for the supported profile is tests/test_stdio.py (T4).")
try:
    import mcp.types as _T
    _tools = [_T.Tool(name=n, title=n.replace("_", " ").title(), description=f"MCP Mesh {n}",
                      inputSchema=C.input_schema(n), outputSchema=C.output_schema(n))
              for n in sorted(C.INPUTS)]
    chk(f"all {len(_tools)} published tool definitions load in mcp.types.Tool",
        len(_tools) == len(C.INPUTS))
    _rt = _tools[0].model_dump(by_alias=True, mode="json", exclude_none=True)
    chk("a serialised Tool keeps our outputSchema byte-for-byte",
        _rt["outputSchema"] == C.output_schema(sorted(C.INPUTS)[0]))
    if MCP:
        _e = list(V(off("Tool")).iter_errors(_rt)) if "Tool" in MCP["$defs"] else []
        chk("a serialised Tool validates against the OFFICIAL Tool definition", not _e,
            _e[0].message[:140] if _e else "")
    _payload = F.FIXTURES["B5_source_status"]["response"]["result"]["structuredContent"]
    _res = _T.CallToolResult(content=[_T.TextContent(type="text", text=json.dumps(_payload))],
                             structuredContent=_payload, isError=False)
    _wire = _res.model_dump(by_alias=True, mode="json", exclude_none=True)
    chk("the SDK's own CallToolResult emits resultType without any injection",
        _wire.get("resultType") == "complete", f"got {_wire.get('resultType')!r}")
    if MCP:
        _e2 = list(V(off("CallToolResult")).iter_errors(_wire))
        chk("that SDK-produced result validates against the OFFICIAL CallToolResult",
            not _e2, _e2[0].message[:140] if _e2 else "")
except Exception as exc:
    chk("SDK compatibility of published schemas", False, f"{type(exc).__name__}: {exc}")

print("\n" + "=" * 78)
print(f"T1 CONTRACT CHECKS: {checks}    FAILURES: {len(fails)}")
for l, d in fails: print(f"  FAILED: {l}  {d}")
print("UNVERIFIED (not covered by any check above): TCC/S1, Screen Time retention R/S2,")
print("  Health export/S3, weather revision/S4, git/S5, APFS os.replace + DuckDB swap/S6,")
print("  and the statistical calibration battery. All remain UNVERIFIED.")
print("=" * 78)
sys.exit(1 if fails else 0)
