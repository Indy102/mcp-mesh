"""Rev 7 wire fixtures. content[0].text is the ACTUAL serialization of
structuredContent. Persisted copies under schemas/fixtures/ are REGENERATED from
this module by tools/emit_artifacts.py and verified equal by the validator."""
import json, math
from datetime import date, timedelta
import screentime as ST
import evaluation as EV
from contracts import SCHEMA_VERSION as SV, PROTOCOL_VERSION as PV, INPUTS, output_schema, input_schema

CLIENT_META = {"io.modelcontextprotocol/protocolVersion": PV,
               "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
               "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "2.4.1"}}
# A request carrying an optional field and a legal VENDOR extension key (item 4).
CLIENT_META_FULL = {**CLIENT_META,
                    "io.modelcontextprotocol/logLevel": "warning",
                    "progressToken": "pt-7741",
                    "com.example.mesh/trace-hint": {"run": "weekly"}}
GATEWAY_META = {"io.modelcontextprotocol/protocolVersion": PV,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "mesh-gateway", "version": "0.1.0"}}
SERVER_META = {"io.modelcontextprotocol/serverInfo": {"name": "mesh-gateway", "version": "0.1.0"}}
LEAF_META   = {"io.modelcontextprotocol/serverInfo": {"name": "mesh-leaf-health", "version": "0.1.0"}}

def ser(o): return json.dumps(o, sort_keys=True, separators=(",", ":"), allow_nan=False)
def req(i, tool, args, meta=CLIENT_META):
    p = {"name": tool, "arguments": args, "_meta": meta}
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": p}
def res(i, sc, is_err=False, meta=SERVER_META):
    return {"jsonrpc": "2.0", "id": i, "result": {"resultType": "complete",
            "content": [{"type": "text", "text": ser(sc)}],
            "structuredContent": sc, "isError": is_err, "_meta": meta}}

AS_OF, GEN = "2026-09-06", 41
FP = "3b1f0a77c4e2d9018b6a5f43ce70d21998aa4c6f0e5b7d3a1c8e2f4b6d9a0c17"
DDI, EVAL = "7c4e2d9018b6a5f43ce70d21998aa4c6", "a91d0e73bc48f512d6a03e7f91c4b820"
CAVEATS = [
 "Findings are observational associations in your own data and are not evidence of cause.",
 "Benjamini-Hochberg at q=0.10 controls the EXPECTED false-discovery proportion under its assumptions. It is not a guarantee about the realized proportion in any single report, and it does not validate the p-values it adjusts.",
 "FDR is controlled within a single evaluation. Weekly re-evaluation is not jointly controlled across weeks; no online-FDR procedure is applied."]

def cov(mid, req_d, pres, miss=(), perm=(), dst=(), stale=(), suff=True):
    return {"metric_id": mid, "requested_days": req_d, "present_days": pres,
            "missing_dates": list(miss), "permanently_missing_dates": list(perm),
            "dst_affected_dates": list(dst), "stale_dates": list(stale),
            "omitted_count": 0, "sufficient": suff,
            "threshold_applied": "min_days_present_per_week=5"}

# --- Screen Time: DERIVED by screentime.compute(), not typed (Rev6 item 7) ---------
AS_OF_D = date(2026, 9, 6)
R = 28
OBS_FROM, OBS_TO = date(2026, 6, 1), date(2026, 9, 2)
INTERNAL_GAP = (date(2026, 7, 10), date(2026, 7, 18))     # 9 days, older than horizon
def _obs():
    d, out = OBS_FROM, []
    while d <= OBS_TO:
        if not (INTERNAL_GAP[0] <= d <= INTERNAL_GAP[1]):
            out.append(d)
        d += timedelta(days=1)
    return out
OBSERVED = _obs()
LAST_SYNC_D = date(2026, 9, 3)
DUR = ST.compute(OBSERVED, AS_OF_D, R, LAST_SYNC_D)
perm_days = sum(g.days for g in DUR.permanent_gaps)
recov_days = sum(g.days for g in DUR.recoverable_gaps)

REGISTRY = [
 {"id": "H1", "driver": "screentime.screen_minutes_total", "outcome": "health.sleep_duration_min",
  "lag_days": 1, "expected_direction": "-", "min_practical_effect": None},
 {"id": "H5", "driver": "calendar.busy_minutes_union", "outcome": "health.sleep_duration_min",
  "lag_days": 0, "expected_direction": "-", "min_practical_effect": 10.0},
]
METHOD = {"method_version": 1, "covariates": ["weekday","trend","annual_harmonics"],
          "se": "newey_west", "hac_lag_rule": "ceil(4*(n/100)**(2/9))", "alpha": 0.05,
          "q": 0.10, "min_paired": 60, "min_residual_df": 50,
          "transforms": {"log1p": ["screentime.screen_minutes_total","git.commits_count",
                                   "weather.precip_mm"]}}
REG_DIG = EV.registry_digest(REGISTRY)
METH_DIG = EV.method_digest(METHOD)
SIID = "5f1c9a2e7b34d0864ae1f2c9b7d05e31"       # store_instance_id
TOKEN = {"store_instance_id": SIID, "generation": 41}
VDID = "7c4e2d9018b6a5f43ce70d21998aa4c6"       # value_definition_id
POLID = "a3b8e5147f92c60d4e8b1a37c5029df6"      # policy_id (retention/refresh policy)
FAMILY = [{"id": "H1", "eligible": False, "p_value": None},
          {"id": "H5", "eligible": True,  "p_value": 0.0061}]
SCOPE = EV.family_scope(FAMILY)
# p-values, adjusted values, membership and counts are derived from ONE source of truth.
_ELIG = [h for h in FAMILY if h["eligible"]]
_QS = EV.benjamini_hochberg([h["p_value"] for h in _ELIG])
QVALUES = {h["id"]: q for h, q in zip(_ELIG, _QS)}
H5_P, H5_Q = 0.0061, QVALUES["H5"]
METRIC_IDS = {
 "calendar.busy_minutes_union": {"value_definition_id": "b1" * 16,
                                 "source_capability_id": "c1" * 16},
 "health.sleep_duration_min":   {"value_definition_id": "b2" * 16,
                                 "source_capability_id": "c2" * 16}}
MID_DIG = EV.metric_identity_digest(METRIC_IDS)
EVAL = EV.evaluation_id(store_instance_id=SIID, generation=41, value_definition_id=VDID,
                        policy_id=POLID, registry_dig=REG_DIG,
                        eligibility_policy_version=1, method_dig=METH_DIG,
                        window_start="2025-09-29", window_end="2026-08-23",
                        metric_identity_dig=MID_DIG)
as_of, cadence, correction = AS_OF_D, DUR.cadence_days, DUR.correction_days
last_obs, last_sync = OBS_TO, LAST_SYNC_D
durable_since = DUR.durable_since
days_until_loss = DUR.days_until_loss
H1_PAIRS = 31
h1_earliest = AS_OF_D + timedelta(days=max(0, 60 - H1_PAIRS))

# --- practical effects, computed not asserted ------------------------------
h5_beta, h5_x0, h5_delta = -0.118, 310.0, 120.0
h5_effect = round(h5_beta * h5_delta, 3)                       # identity link both sides
ex_beta, ex_x0, ex_delta, ex_mu0 = 0.0091, 18.4, 2.0, math.log1p(3.0)
ex_effect = round(math.expm1(ex_mu0 + ex_beta * ex_delta) - math.expm1(ex_mu0), 4)

def st_src(**kw):
    base = {"source": "screentime", "reachable": True, "reason": None,
            "last_observation_date": last_obs.isoformat(),
            "last_successful_sync": f"{last_sync.isoformat()}T08:14:00Z", "stale": True,
            "staleness_days": (AS_OF_D - last_sync).days,
            "last_attempt_at": f"{last_sync.isoformat()}T08:14:00Z",
            "last_attempt_outcome": "ok", "last_attempt_reason": None,
            "max_staleness_days": 1,
            "durable_since": durable_since.isoformat() if durable_since else None,
            "permanent_gap_days": perm_days, "recoverable_gap_days": recov_days,
            "source_history_limit_days": R, "retention_days": R,
            "unbounded_retention": DUR.unbounded_retention,
            "retention_status": "unbounded" if DUR.unbounded_retention else "finite",
            "last_recoverable_date": DUR.last_recoverable_date.isoformat()
                                     if DUR.last_recoverable_date else None,
            "loss_deadline_date": DUR.loss_deadline_date.isoformat()
                                  if DUR.loss_deadline_date else None,
            "earliest_unresolved_date": DUR.earliest_unresolved_date.isoformat()
                                        if DUR.earliest_unresolved_date else None,
            "sync_overdue": DUR.sync_overdue, "data_loss_at_risk": DUR.data_loss_at_risk,
            "days_until_loss": days_until_loss,
            "low_retention_mode": DUR.low_retention_mode, "cadence_days": cadence,
            "correction_days": correction,
            "canonical_metric_id": "screentime.screen_minutes_total",
            "normalization_version": 1}
    base.update(kw); return base

def simple_src(name, canon, **kw):
    base = {"source": name, "reachable": True, "reason": None,
            "last_observation_date": "2026-09-05",
            "last_successful_sync": "2026-09-06T07:02:00Z", "stale": False,
            "staleness_days": (AS_OF_D - date(2026, 9, 6)).days,
            "last_attempt_at": "2026-09-06T07:02:00Z",
            "last_attempt_outcome": "ok", "last_attempt_reason": None,
            "max_staleness_days": 1, "durable_since": None, "permanent_gap_days": 0,
            "source_history_limit_days": None, "retention_days": None,
            "unbounded_retention": True, "retention_status": "unbounded",
            "earliest_unresolved_date": None, "last_recoverable_date": None,
            "loss_deadline_date": None,
            "sync_overdue": None, "data_loss_at_risk": False, "days_until_loss": None,
            "recoverable_gap_days": 0,
            "low_retention_mode": None, "cadence_days": None, "correction_days": None,
            "canonical_metric_id": canon, "normalization_version": 1}
    base.update(kw); return base

WEATHER_SPEC = {"id": "weather.precip_mm", "unit": "mm", "kind": "total", "value_min": 0,
  "value_max": 500, "direction": "neutral", "min_meaningful_delta": 2.0,
  "min_days_present_per_week": 5, "emits_structural_zero": True, "transform": "log1p",
  "normalization_version": 1, "description": "Daily total precipitation"}
GIT_SPEC = {"id": "git.commits_count", "unit": "count", "kind": "count", "value_min": 0,
  "value_max": None, "direction": "neutral", "min_meaningful_delta": 1.0,
  "min_days_present_per_week": 5, "emits_structural_zero": True, "transform": "log1p",
  "normalization_version": 1, "description": "Commits authored across configured repositories"}

FIXTURES = {}

# B1 tools/list - the COMPLETE catalogue. Every advertised tool, deterministic
# (sorted) order, and outputSchema on all of them: it is a v1 invariant.
FIXTURES["B1_tools_list"] = {
 "request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
             "params": {"_meta": CLIENT_META}},
 "response": {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete", "tools": [
    {"name": t, "title": t.replace("_", " ").title(),
     "description": f"MCP Mesh {t}", "inputSchema": input_schema(t),
     "outputSchema": output_schema(t)}
    for t in sorted(INPUTS)],
    "ttlMs": 300000, "cacheScope": "private", "_meta": SERVER_META}}}

# B.2 describe_metrics (leaf)
FIXTURES["B2_describe_metrics"] = {
 "request": req(2, "describe_metrics", {"store_token": TOKEN}),
 "response": res(2, {"schema_version": SV, "payload_type": "metric_catalog",
   "generation": GEN, "source": "weather", "metrics": [WEATHER_SPEC]}, meta=LEAF_META)}

# B.3 fetch — partial with undetermined dates (Y16)
FIXTURES["B3_fetch_partial"] = {
 "request": req(3, "fetch", {"store_token": TOKEN, "start": "2026-08-24", "end": "2026-08-30",
   "metrics": ["health.sleep_duration_min", "health.hrv_sdnn_ms", "health.vo2max"]}),
 "response": res(3, {"schema_version": SV, "payload_type": "fetch", "source": "health",
   "generation": GEN, "store_token": TOKEN, "requested": {"start": "2026-08-24", "end": "2026-08-30"},
   "observations": [{"date": "2026-08-24", "metric_id": "health.sleep_duration_min",
     "value": 412.0, "quality": "measured", "sample_count": 1, "normalization_version": 2}],
   "metric_status": [
     {"metric_id": "health.sleep_duration_min", "value_definition_id": VDID,
      "status": "partial",
      "covered": {"start": "2026-08-24", "end": "2026-08-30"},
      "observed_dates": ["2026-08-24","2026-08-25","2026-08-26","2026-08-28","2026-08-29","2026-08-30"],
      "known_absent_dates": [], "undetermined_dates": ["2026-08-27"], "present_dates": 6,
      "permanently_missing_dates": [], "omitted_dates_count": 0,
      "reason": "2026-08-27 not determined; its prior observations are retained"},
     {"metric_id": "health.hrv_sdnn_ms", "value_definition_id": None,
      "status": "failed", "covered": None,
      "observed_dates": [], "known_absent_dates": [], "undetermined_dates": [],
      "present_dates": 0, "permanently_missing_dates": [], "omitted_dates_count": 0,
      "reason": "HRV series absent from this export; re-export from Health.app"},
     {"metric_id": "health.vo2max", "value_definition_id": None,
      "status": "unsupported", "covered": None,
      "observed_dates": [], "known_absent_dates": [], "undetermined_dates": [],
      "present_dates": 0, "permanently_missing_dates": [], "omitted_dates_count": 0,
      "reason": "not offered by the health adapter at normalization_version 2"}],
   "status": "partial",
   "truncation": {"truncated": False, "omitted_count": 0, "truncation_reason": None},
   "snapshot_id": "sha256:9f2c1d84e0b7a35c", "normalization_versions": {"health": 2}},
   meta=LEAF_META)}

# B.4 health_check (leaf) and B.5 source_status (gateway, previously omitted — Y6)
FIXTURES["B4_health_check"] = {
 "request": req(4, "health_check", {}),
 "response": res(4, {"schema_version": SV, "payload_type": "status", "generation": GEN,
   "as_of_date": AS_OF, "analysis_timezone": "America/Indiana/Indianapolis",
   "analysis_timezone_source": "recorded_value_definition",
   "store_instance_id": SIID, "redaction_level": "aggregate",
   "sources": [st_src()]}, meta=LEAF_META)}

FIXTURES["B5_source_status"] = {
 "request": req(5, "source_status", {"store_token": TOKEN}),
 "response": res(5, {"schema_version": SV, "payload_type": "status", "generation": GEN,
   "as_of_date": AS_OF, "analysis_timezone": "America/Indiana/Indianapolis",
   "analysis_timezone_source": "recorded_value_definition",
   "store_instance_id": SIID, "redaction_level": "aggregate", "sources": [
     simple_src("health", "health.sleep_duration_min", stale=True, max_staleness_days=7,
                last_observation_date="2026-08-30",
                last_successful_sync="2026-08-31T19:40:00Z",
                staleness_days=(AS_OF_D - date(2026, 8, 31)).days,
                last_attempt_at="2026-08-31T19:40:00Z", normalization_version=2),
     simple_src("calendar", "calendar.busy_minutes_union"),
     st_src(),
     simple_src("weather", "weather.temp_mean_c"),
     simple_src("git", "git.commits_count"),
   ]})}

# B.6 list_metrics
FIXTURES["B6_list_metrics"] = {
 "request": req(6, "list_metrics", {"source": "git"}),
 "response": res(6, {"schema_version": SV, "payload_type": "metric_catalog",
   "generation": GEN, "source": "git", "metrics": [GIT_SPEC]})}

# B.7 weekly_report
FIXTURES["B7_weekly_report"] = {
 "request": req(7, "weekly_report", {"week_of": "2026-08-26"}),
 "response": res(7, {"schema_version": SV, "payload_type": "report", "generation": GEN,
   "store_instance_id": SIID, "value_definition_id": VDID, "policy_id": POLID,
   "recommendations_enabled": False,
   "recommendations_disabled_reason": "thresholds_not_prespecified", "as_of_date": AS_OF,
   "week": {"start": "2026-08-24", "end": "2026-08-30"},
   # No confirmatory hypothesis was eligible this week, so no family evaluation ran and
   # there is no evaluation identity to report.
   "evaluation_id": None,
   "observed_changes": [
     {"status": "computed", "metric_id": "health.sleep_duration_min", "week_value": 389.0,
      "baseline_median": 436.0, "baseline_mad": 22.2, "delta": -47.0, "delta_z": -2.117,
      "meaningful": True, "undefined_reason": None, "flags": ["daily_mean_of_total"],
      "coverage": cov("health.sleep_duration_min", 7, 6, miss=["2026-08-27"])},
     {"status": "insufficient_data", "metric_id": "screentime.screen_minutes_total",
      "reason": "target_week_incomplete", "present_days": 3, "required_days": 5,
      "valid_baseline_weeks": 4, "required_baseline_weeks": 6,
      "coverage": cov("screentime.screen_minutes_total", 7, 3,
        miss=["2026-08-28","2026-08-29","2026-08-30"], perm=["2026-08-28"], suff=False)}],
   "associations": [
     {"status": "ineligible", "hypothesis_id": "H1",
      "driver_id": "screentime.screen_minutes_total",
      "outcome_id": "health.sleep_duration_min", "lag_days": 1,
      "reasons": ["insufficient_durable_history"], "usable_paired_dates": H1_PAIRS,
      "evaluation_family": "confirmatory",
      "earliest_possible_date": h1_earliest.isoformat(), "assumes_perfect_cadence": True}],
   "recommendations": [],
   "source_status": [
     {"source": "health", "available": True, "reason": None,
      "metrics_contributed": 1, "last_observation_date": "2026-08-30"},
     {"source": "screentime", "available": True, "reason": None,
      "metrics_contributed": 1, "last_observation_date": last_obs.isoformat()},
     {"source": "calendar", "available": False,
      "reason": "EventKit access not granted", "metrics_contributed": 0,
      "last_observation_date": None},
     {"source": "weather", "available": True, "reason": None,
      "metrics_contributed": 1, "last_observation_date": "2026-09-05"},
     {"source": "git", "available": True, "reason": None,
      "metrics_contributed": 1, "last_observation_date": "2026-09-05"}],
   "notes": [f"screentime: {perm_days} permanently missing days before {durable_since}",
             f"H1 needs {60 - H1_PAIRS} more paired days; estimate assumes uninterrupted collection"],
   "truncation": {"truncated": False, "omitted_count": 0, "truncation_reason": None},
   "redaction_level": "aggregate",
   "summary_text": "Week of 2026-08-24. Mean nightly sleep was 389 min, 47 min below your 8-week baseline of 436 min (6 of 7 nights recorded). No associations were eligible this week.",
   "method_caveats": CAVEATS})}

# B.8 compare_periods — dedicated PeriodComparison (Y27)
FIXTURES["B8_compare_periods"] = {
 "request": req(8, "compare_periods", {"period_a": {"start": "2026-07-01", "end": "2026-07-31"},
                                       "period_b": {"start": "2026-08-01", "end": "2026-08-31"}}),
 "response": res(8, {"schema_version": SV, "payload_type": "comparison", "generation": GEN,
   "period_a": {"start": "2026-07-01", "end": "2026-07-31"},
   "period_b": {"start": "2026-08-01", "end": "2026-08-31"},
   "comparisons": [{"status": "computed", "metric_id": "git.commits_count",
     "a_value": 4.1, "b_value": 2.9, "a_present_days": 31, "b_present_days": 31,
     "delta": -1.2, "pooled_mad": 1.3, "delta_z": -0.923, "meaningful": False,
     "flags": ["daily_mean_of_total"],
     "coverage_a": cov("git.commits_count", 31, 31),
     "coverage_b": cov("git.commits_count", 31, 31)}],
   "truncation": {"truncated": False, "omitted_count": 0, "truncation_reason": None},
   "redaction_level": "aggregate",
   "summary_text": "August vs July: mean daily commits 2.9 vs 4.1 (delta -1.2, z -0.92; not meaningful, |z| < 1.0). Dispersion is pooled across both periods; neither is a baseline."})}

# B.9 gateway -> leaf tools/call. The gateway is an MCP CLIENT here and sends its own _meta.
FIXTURES["B9_gateway_to_leaf"] = {
 "request": req(9, "fetch", {"store_token": TOKEN, "start": "2026-06-29", "end": "2026-08-23",
                             "metrics": ["calendar.busy_minutes_union"]}, meta=GATEWAY_META),
 "response": res(9, {"schema_version": SV, "payload_type": "fetch", "source": "calendar",
   "generation": GEN, "store_token": TOKEN, "requested": {"start": "2026-06-29", "end": "2026-08-23"},
   "observations": [{"date": "2026-06-29", "metric_id": "calendar.busy_minutes_union",
     "value": 285.0, "quality": "measured", "sample_count": 6, "normalization_version": 1}],
   "metric_status": [{"metric_id": "calendar.busy_minutes_union", "value_definition_id": VDID,
     "status": "truncated",
     "covered": {"start": "2026-06-29", "end": "2026-08-20"},
     "observed_dates": ["2026-06-29"], "known_absent_dates": [], "undetermined_dates": [],
     "present_dates": 1, "permanently_missing_dates": [], "omitted_dates_count": 52,
     "reason": "row budget reached before end of requested range"}],
   "status": "partial",
   "truncation": {"truncated": True, "omitted_count": 52, "truncation_reason": "row_budget"},
   "snapshot_id": "ekchange:8841fa02", "normalization_versions": {"calendar": 1}},
   meta={"io.modelcontextprotocol/serverInfo": {"name": "mesh-leaf-calendar", "version": "0.1.0"}})}

# B.10 association, confirmatory
FIXTURES["B10_association_confirmatory"] = {
 "request": req(10, "association", {"hypothesis_id": "H5"}),
 "response": res(10, {"schema_version": SV, "payload_type": "association", "generation": GEN,
   "association": {"status": "estimated", "hypothesis_id": "H5",
     "driver_id": "calendar.busy_minutes_union", "outcome_id": "health.sleep_duration_min",
     "lag_days": 0, "beta": h5_beta,
     "beta_interpretation": "minutes of sleep per additional busy minute; both untransformed",
     "beta_standardized": -0.244, "ci_low": -0.201, "ci_high": -0.035,
     "p_value": H5_P, "q_value": H5_Q, "usable_paired_dates": 214,
     "parameter_count": 11, "residual_df": 203, "direction_as_expected": True,
     "driver_reference_value": h5_x0, "driver_delta_raw": h5_delta,
     "practical_effect_raw": h5_effect, "practical_effect_scale": "raw_outcome",
     "min_practical_effect": 10.0, "practical_effect_met": abs(h5_effect) >= 10.0,
     "back_transform": "identity", "back_transform_note": None, "extrapolated": False,
     "evaluation_family": "confirmatory", "evaluation_id": EVAL, "registry_version": 1,
     "registry_digest": REG_DIG, "method_version": 1, "method_digest": METH_DIG,
     "eligibility_policy_version": 1, "m_total": SCOPE["m_total"],
     "m_eligible": SCOPE["m_eligible"], "threshold_source": "prespecified_user",
     "flags": [],
     "estimation_window": {"start": "2025-09-29", "end": "2026-08-23"},
     "computed_at": "2026-09-06T18:22:41Z"},
   "method_caveats": CAVEATS[:2],
   "summary_text": f"Over 214 paired days, each additional busy calendar minute is associated with {abs(h5_beta)} fewer minutes of sleep (95% CI -0.201 to -0.035). For a {h5_delta:.0f}-minute increase above your typical {h5_x0:.0f} minutes, this predicts {h5_effect} minutes of sleep."})}

# B.11 association, arbitrary pair -> always exploratory, q_value null (Y9/Y24)
FIXTURES["B11_association_exploratory"] = {
 "request": req(11, "association", {"driver": "weather.temp_mean_c",
                                    "outcome": "git.commits_count", "lag_days": 2}),
 "response": res(11, {"schema_version": SV, "payload_type": "association", "generation": GEN,
   "association": {"status": "estimated", "hypothesis_id": None,
     "driver_id": "weather.temp_mean_c", "outcome_id": "git.commits_count", "lag_days": 2,
     "beta": ex_beta, "beta_interpretation": "log1p(commits) per degC; outcome log1p-transformed",
     "beta_standardized": 0.061, "ci_low": -0.0143, "ci_high": 0.0325,
     "p_value": 0.4462, "q_value": None, "usable_paired_dates": 389,
     "parameter_count": 11, "residual_df": 378, "direction_as_expected": False,
     "driver_reference_value": ex_x0, "driver_delta_raw": ex_delta,
     "practical_effect_raw": ex_effect, "practical_effect_scale": "raw_outcome",
     "min_practical_effect": 1.0, "practical_effect_met": False,   # threshold_source == "absent" (item 10)
     "back_transform": "expm1",
     "back_transform_note": "Outcome is log1p-transformed; the back-transformed value is a median-scale prediction, not a mean.",
     "extrapolated": False, "evaluation_family": "exploratory", "evaluation_id": None,
     "registry_version": None, "registry_digest": None, "method_version": 1,
     "method_digest": METH_DIG, "eligibility_policy_version": 1,
     "m_total": None, "m_eligible": None, "threshold_source": "absent",
     "flags": ["below_practical_threshold"],
     "estimation_window": {"start": "2025-08-01", "end": "2026-08-23"},
     "computed_at": "2026-09-06T18:23:02Z"},
   "method_caveats": [
     "This pair is not in the pre-registered registry; it is exploratory, no correction applies, and q_value is null.",
     "Exploratory results never enter a weekly report and never generate a recommendation."],
   "summary_text": f"Exploratory: no supported association between mean temperature and commits two days later (n=389, p=0.446, 95% CI -0.0143 to 0.0325). Predicted effect of a {ex_delta} degC rise is {ex_effect} commits."})}

# B.12 explore_associations
FIXTURES["B12_explore"] = {
 "request": req(12, "explore_associations", {"outcome": "health.sleep_duration_min",
                                             "lag_days": 1, "max_pairs": 50}),
 "response": res(12, {"schema_version": SV, "payload_type": "exploration", "generation": GEN,
   "evaluation_family": "exploratory", "pairs_tested": 17, "results": [],
   "truncation": {"truncated": True, "omitted_count": 3, "truncation_reason": "item_budget"},
   "method_caveats": [
     "Exploratory scan: pairs were selected after seeing the data, so these are hypothesis-generating only.",
     "NO multiple-testing correction is applied to exploratory scans. q_value is null for every result and NO error rate is controlled or claimed."],
   "summary_text": "Scanned 17 eligible drivers against sleep duration at lag 1. No correction was applied and no error rate is claimed; nothing here may enter a report or a recommendation."})}

# B.13-B.16 tool execution errors
FIXTURES["B13_generation_mismatch"] = {"response": res(13, {"schema_version": SV,
  "payload_type": "error", "error_type": "generation_mismatch",
  "message": "Leaf holds generation 42; the request specified 41.",
  "remediation": "Re-read store_manifest.generation and repeat the fan-out.",
  "retryable": True, "details": {"current_generation": 42,
   "current_store_token": {"store_instance_id": SIID, "generation": 42}}}, is_err=True, meta=LEAF_META)}

FIXTURES["B14_storage_error"] = {"response": res(14, {"schema_version": SV,
  "payload_type": "error", "error_type": "storage_error",
  "message": "Generation advanced during fan-out; retries exhausted.",
  "remediation": "A collector is swapping frequently. Retry in a few seconds.",
  "retryable": True,
  "details": {"store_path_id": "9c1a7f30b8d54e62", "cause": "concurrent_swap",
              "current_generation": 44,
              "current_store_token": {"store_instance_id": SIID, "generation": 44}}},
  is_err=True)}

FIXTURES["B15_unknown_hypothesis"] = {
 "request": req(15, "association", {"hypothesis_id": "H99"}),
 "response": res(15, {"schema_version": SV, "payload_type": "error",
  "error_type": "invalid_request", "message": "Unknown hypothesis_id 'H99'.",
  "remediation": "Use one of the registered ids, or pass driver and outcome for an exploratory estimate.",
  "retryable": False,
  "details": {"unknown_hypothesis_id": "H99",
              "valid_hypothesis_ids": ["H1","H2","H3","H4","H5","H6","H7","H8"]}}, is_err=True)}

FIXTURES["B16_insufficient_evidence"] = {"response": res(16, {"schema_version": SV,
  "payload_type": "error", "error_type": "insufficient_evidence",
  "message": "No source produced data for the requested week.",
  "remediation": "Run `mesh sync`. Calendar and Screen Time additionally require Full Disk Access.",
  "retryable": False, "details": {"sources_unavailable": [
    {"source": "health", "reason": "no export archive configured"},
    {"source": "calendar", "reason": "EventKit access not granted"},
    {"source": "screentime", "reason": "knowledgeC.db unreadable: Full Disk Access not granted"},
    {"source": "weather", "reason": "no completed range covers the requested week"},
    {"source": "git", "reason": "no configured repositories"}]}}, is_err=True)}

# B.17-B.20 JSON-RPC protocol errors (no structuredContent at all)
FIXTURES["B17_missing_meta"] = {
 "request": {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
   "params": {"name": "source_status", "arguments": {"store_token": TOKEN},
     "_meta": {"io.modelcontextprotocol/clientCapabilities": {}}}},
 "response": {"jsonrpc": "2.0", "id": 17, "error": {"code": -32602,
   "message": "Missing required _meta field io.modelcontextprotocol/protocolVersion"}}}

FIXTURES["B18_lag_override"] = {
 "request": req(18, "association", {"hypothesis_id": "H5", "lag_days": 3}),
 "response": {"jsonrpc": "2.0", "id": 18, "error": {"code": -32602,
   "message": "Invalid arguments for association: lag_days may not accompany hypothesis_id; the registry owns the lag"}}}

FIXTURES["B19_unknown_tool"] = {
 "response": {"jsonrpc": "2.0", "id": 19, "error": {"code": -32601,
   "message": "Unknown tool: correlate"}}}

FIXTURES["B20_missing_capability"] = {
 "response": {"jsonrpc": "2.0", "id": 20, "error": {"code": -32021,
   "message": "Required client capability not declared",
   "data": {"requiredCapabilities": ["elicitation"]}}}}

# ============================ Rev 6 additions =================================

# B21 server/discover — servers MUST implement it; clients MAY call it (item 2).
# Required DiscoverResult fields per the official schema: cacheScope, capabilities,
# resultType, supportedVersions, ttlMs. NOTE: serverInfo is NOT a DiscoverResult
# field in the official schema; server identity travels in _meta.
FIXTURES["B21_server_discover"] = {
 "request": {"jsonrpc": "2.0", "id": 21, "method": "server/discover",
             "params": {"_meta": CLIENT_META}},
 "response": {"jsonrpc": "2.0", "id": 21, "result": {
    "resultType": "complete",
    "supportedVersions": [PV],
    "capabilities": {"tools": {"listChanged": False},
                     "extensions": {}},
    "instructions": ("Local quantified-self store. Call source_status first to see which "
                     "sources are fresh and whether `mesh sync` is due. weekly_report is "
                     "descriptive; associations are observational and never causal."),
    "ttlMs": 300000,
    "cacheScope": "private",
    "_meta": {"io.modelcontextprotocol/serverInfo":
              {"name": "mesh-gateway", "version": "0.6.0"}}}}}

# B22 a request exercising every optional _meta field plus a legal vendor key (item 4).
FIXTURES["B22_meta_full"] = {
 "request": req(22, "source_status", {"store_token": TOKEN}, meta=CLIENT_META_FULL),
 "response": res(22, {"schema_version": SV, "payload_type": "status", "generation": GEN,
   "as_of_date": AS_OF, "analysis_timezone": "America/Indiana/Indianapolis",
   "analysis_timezone_source": "recorded_value_definition",
   "store_instance_id": SIID, "redaction_level": "aggregate", "sources": [st_src()]})}

# B23 rebuild_unavailable — a non-rebuildable source states so explicitly (item 6).
FIXTURES["B23_rebuild_unavailable"] = {
 "response": res(23, {"schema_version": SV, "payload_type": "error",
   "error_type": "rebuild_unavailable",
   "message": ("screentime cannot be re-derived: no raw payload is retained and the "
               "source has discarded the underlying records."),
   "remediation": ("Existing rows are retained under their original value_definition_id "
                   "and remain readable. Nothing further can be recovered."),
   "retryable": False,
   "details": {"source": "screentime", "rebuildable": False, "raw_retained": False,
               "affected_dates": ["2026-06-01", "2026-06-02"]}}, is_err=True)}

# B24 the two risk signals DIVERGING: collection is current, but an old internal gap is
# about to fall out of retention. Rev5's single `at_risk` could not express this.
_obs2 = [d for d in OBSERVED if d < date(2026, 8, 12) or d > date(2026, 8, 14)]
_obs2 = [d for d in _obs2 if d <= date(2026, 9, 5)] + [date(2026, 9, 5)]
DUR2 = ST.compute(sorted(set(_obs2)), AS_OF_D, R, date(2026, 9, 6))
FIXTURES["B24_signals_diverge"] = {
 "response": res(24, {"schema_version": SV, "payload_type": "status", "generation": GEN,
   "as_of_date": AS_OF, "analysis_timezone": "America/Indiana/Indianapolis",
   "analysis_timezone_source": "recorded_value_definition",
   "store_instance_id": SIID, "redaction_level": "aggregate",
   "sources": [st_src(last_observation_date="2026-09-05",
     last_successful_sync="2026-09-06T07:00:00Z",
     staleness_days=(AS_OF_D - date(2026, 9, 6)).days,
     last_attempt_at="2026-09-06T07:00:00Z",
     durable_since=DUR2.durable_since.isoformat() if DUR2.durable_since else None,
     permanent_gap_days=sum(g.days for g in DUR2.permanent_gaps),
     recoverable_gap_days=sum(g.days for g in DUR2.recoverable_gaps),
     earliest_unresolved_date=DUR2.earliest_unresolved_date.isoformat()
                              if DUR2.earliest_unresolved_date else None,
     sync_overdue=DUR2.sync_overdue, data_loss_at_risk=DUR2.data_loss_at_risk,
     days_until_loss=DUR2.days_until_loss)]})}

# B25 unbounded retention (R = None): loss signals must be null/false, never computed.
DUR_NONE = ST.compute(OBSERVED, AS_OF_D, None, LAST_SYNC_D)
FIXTURES["B25_unbounded_retention"] = {
 "response": res(25, {"schema_version": SV, "payload_type": "status", "generation": GEN,
   "as_of_date": AS_OF, "analysis_timezone": "America/Indiana/Indianapolis",
   "analysis_timezone_source": "recorded_value_definition",
   "store_instance_id": SIID, "redaction_level": "aggregate",
   "sources": [st_src(retention_days=None, source_history_limit_days=None,
     unbounded_retention=True, retention_status="unbounded",
     last_recoverable_date=None, loss_deadline_date=None, cadence_days=None, correction_days=None,
     low_retention_mode=False,
     durable_since=DUR_NONE.durable_since.isoformat() if DUR_NONE.durable_since else None,
     permanent_gap_days=sum(g.days for g in DUR_NONE.permanent_gaps),
     recoverable_gap_days=sum(g.days for g in DUR_NONE.recoverable_gaps),
     earliest_unresolved_date=DUR_NONE.earliest_unresolved_date.isoformat()
                              if DUR_NONE.earliest_unresolved_date else None,
     sync_overdue=DUR_NONE.sync_overdue, data_loss_at_risk=False,
     days_until_loss=None)]})}

# ---- Error classification, corrected (item 3) --------------------------------
# Rev5 mapped an unknown TOOL to -32601. The specification's own example maps it to
# -32602; -32601 is for an unknown RPC METHOD.
FIXTURES["B19_unknown_tool"] = {
 "request": req(19, "correlate", {}),
 "response": {"jsonrpc": "2.0", "id": 19, "error": {"code": -32602,
   "message": "Unknown tool: correlate"}}}

FIXTURES["B26_unknown_method"] = {
 "request": {"jsonrpc": "2.0", "id": 26, "method": "tools/summon",
             "params": {"_meta": CLIENT_META}},
 "response": {"jsonrpc": "2.0", "id": 26, "error": {"code": -32601,
   "message": "Method not found: tools/summon"}}}

FIXTURES["B27_unsupported_version"] = {
 "request": {"jsonrpc": "2.0", "id": 27, "method": "server/discover",
   "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {}}}},
 "response": {"jsonrpc": "2.0", "id": 27, "error": {"code": -32022,
   "message": "Unsupported protocol version",
   "data": {"supported": [PV], "requested": "1900-01-01"}}}}

# ============================ Rev 7 additions =================================
SIID2 = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"           # a DIFFERENT store instance

# B28 destroy/re-create: same generation number, different store instance -> rejected.
FIXTURES["B28_store_instance_mismatch"] = {
 "request": req(28, "fetch", {"store_token": {"store_instance_id": SIID2, "generation": 41},
                              "start": "2026-08-24", "end": "2026-08-30"}),
 "response": res(28, {"schema_version": SV, "payload_type": "error",
   "error_type": "store_instance_mismatch",
   "message": "store_token names instance 0a1b2c3d…, this leaf serves 5f1c9a2e….",
   "remediation": "Re-read the pointer; the store was destroyed and re-created.",
   "retryable": False,
   "details": {"current_store_token": {"store_instance_id": SIID, "generation": 41}}},
   is_err=True, meta=LEAF_META)}

# B29 internal fault: stable redacted object, no traceback, no local path.
FIXTURES["B29_internal_error"] = {
 "response": {"jsonrpc": "2.0", "id": 29, "error": {"code": -32603,
   "message": "Internal error",
   "data": {"incident_id": "3f9c02a71b6d4e58"}}}}

# B30/B31 the rest of the protocol error ladder.
FIXTURES["B30_parse_error"] = {
 "response": {"jsonrpc": "2.0", "error": {"code": -32700,
   "message": "Parse error"}}}
FIXTURES["B31_invalid_request"] = {
 "response": {"jsonrpc": "2.0", "error": {"code": -32600,
   "message": "Invalid Request: missing method"}}}

# B32 missing capability, request AND response.
FIXTURES["B32_missing_capability"] = {
 "request": {"jsonrpc": "2.0", "id": 32, "method": "tools/call",
   "params": {"name": "weekly_report", "arguments": {},
     "_meta": {"io.modelcontextprotocol/protocolVersion": PV,
               "io.modelcontextprotocol/clientCapabilities": {}}}},
 "response": {"jsonrpc": "2.0", "id": 32, "error": {"code": -32021,
   "message": "Required client capability not declared",
   "data": {"requiredCapabilities": ["elicitation"]}}}}

# B27 is REPLACED: an unsupported version must be STRUCTURALLY VALID and reach
# negotiation. protocolVersion is a string in the official schema, not an enum.
FIXTURES["B27_unsupported_version"] = {
 "request": {"jsonrpc": "2.0", "id": 27, "method": "server/discover",
   "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo":
                            {"name": "legacy-probe", "version": "0.1"}}}},
 "response": {"jsonrpc": "2.0", "id": 27, "error": {"code": -32022,
   "message": "Unsupported protocol version",
   "data": {"supported": [PV], "requested": "1900-01-01"}}}}

# B33 antipodal circular comparison: the representation Rev 8 promised but could not emit.
import circular as CIRC
_ANTI = CIRC.compare_periods([1430.0, 10.0], [710.0, 730.0], 15.0,
                             "screentime.last_use_local_time_min",
                             coverage_a=cov("screentime.last_use_local_time_min", 2, 2),
                             coverage_b=cov("screentime.last_use_local_time_min", 2, 2))
FIXTURES["B33_antipodal_comparison"] = {
 "request": req(33, "compare_periods",
                {"period_a": {"start": "2026-08-01", "end": "2026-08-02"},
                 "period_b": {"start": "2026-08-03", "end": "2026-08-04"}}),
 "response": res(33, {"schema_version": SV, "payload_type": "comparison", "generation": GEN,
   "period_a": {"start": "2026-08-01", "end": "2026-08-02"},
   "period_b": {"start": "2026-08-03", "end": "2026-08-04"},
   "comparisons": [_ANTI],
   "truncation": {"truncated": False, "omitted_count": 0, "truncation_reason": None},
   "redaction_level": "aggregate",
   "summary_text": ("Last-use clock time is exactly 12 hours apart between the two "
                    "periods. The signed circular difference is undefined at the "
                    "antipode, so no delta, no z and no meaningfulness are reported.")})}
