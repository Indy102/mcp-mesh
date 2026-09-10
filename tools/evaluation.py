"""Evaluation identity and BH scope. Rev6 §9."""
import hashlib, json, math


class PValueError(ValueError):
    pass


def _canon(kind, payload):
    """Canonical, FIELD-NAMED, DOMAIN-SEPARATED preimage. Positional concatenation is
    ambiguous: it cannot distinguish different field boundaries, and it can lose the
    metric a version belongs to."""
    return json.dumps({"kind": kind, "payload": payload}, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _d(kind, payload):
    return hashlib.sha256(_canon(kind, payload)).hexdigest()


def registry_digest(hypotheses):
    """Digest of the EXACT family, by field name."""
    return _d("registry", [{"id": h["id"], "driver": h["driver"], "outcome": h["outcome"],
                            "lag_days": h["lag_days"],
                            "expected_direction": h["expected_direction"],
                            "min_practical_effect": h.get("min_practical_effect")}
                           for h in sorted(hypotheses, key=lambda h: h["id"])])[:32]


def method_digest(method):
    """Digest of estimator settings that change a number, by field name."""
    keys = ["method_version", "covariates", "se", "hac_lag_rule", "alpha", "q",
            "min_paired", "min_residual_df", "transforms"]
    return _d("method", {k: method[k] for k in keys})[:32]


def metric_identity_digest(metric_identities):
    """Every participating metric's value-definition AND measured capability.

    A q-value depends on what each metric's number MEANS and on whether a date was
    classifiable, so both identities belong in the evaluation identity.
    `metric_identities` maps metric_id -> {"value_definition_id": ..., "source_capability_id": ...}
    """
    return _d("metric_identities",
              {m: {"value_definition_id": v["value_definition_id"],
                   "source_capability_id": v["source_capability_id"]}
               for m, v in sorted(metric_identities.items())})[:32]


def evaluation_id(*, store_instance_id, generation, value_definition_id, policy_id,
                  registry_dig, eligibility_policy_version, method_dig,
                  window_start, window_end, metric_identity_dig):
    """Every input that can change a q-value, named explicitly.

    `metric_identity_dig` is REQUIRED: it covers every participating metric's value
    definition and measured capability, including metrics considered for eligibility
    but not estimated, because eligibility is part of what produced the q-values.
    """
    if not metric_identity_dig:
        raise ValueError("metric_identity_dig is required for evaluation_id")
    return _d("evaluation_id", {
        "store_instance_id": store_instance_id, "generation": generation,
        "value_definition_id": value_definition_id, "policy_id": policy_id,
        "registry_digest": registry_dig,
        "eligibility_policy_version": eligibility_policy_version,
        "method_digest": method_dig, "window_start": window_start,
        "window_end": window_end, "metric_identity_digest": metric_identity_dig})[:32]


def benjamini_hochberg(pvalues, q=0.10):
    """BH-adjusted values over the SUPPLIED set only.

    Controls the EXPECTED false-discovery proportion under its assumptions. It says
    nothing about the realized FDP of any single evaluation.
    """
    for i, p in enumerate(pvalues):
        if p is None or isinstance(p, bool) or not isinstance(p, (int, float)):
            raise PValueError(f"p-value[{i}] is not a number: {p!r}")
        if math.isnan(p) or math.isinf(p):
            raise PValueError(f"p-value[{i}] is not finite: {p!r}")
        if p < 0.0 or p > 1.0:
            raise PValueError(f"p-value[{i}] outside [0,1]: {p!r}")
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, pvalues[i] * m / rank)
        adj[i] = val
        prev = val
    return adj


def family_scope(hypotheses_with_eligibility):
    """BH m policy: m counts ONLY hypotheses estimated in this evaluation.

    This is a RECORDED CHOICE, NOT A JUSTIFIED PROCEDURE. Eligibility is a
    data-dependent filter, and filtering can affect null validity, so eligible-only
    counting is an open question that the calibration battery must settle. Because m
    then depends on the data, `evaluation_id` pins the eligibility policy version and
    every participating metric's identity: two evaluations with different m are
    different evaluations and their q-values are not comparable.

    UNVERIFIED: no calibration has been run. No association may ship.
    """
    eligible = [h for h in hypotheses_with_eligibility if h["eligible"]]
    return {"m_total": len(hypotheses_with_eligibility),
            "m_eligible": len(eligible),
            "tested_ids": sorted(h["id"] for h in eligible)}
