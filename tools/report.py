"""Weekly descriptive report computed FROM STORED FACTS. Rev 10 milestone.

WEEKLY NUMBER CONTRACT (defaults recorded in the value definition where they change
values; see `report_policy()`):

  week            ISO-8601, Monday-Sunday, in the analysis timezone
  default week    the last COMPLETE ISO week strictly before as_of
  daily value     the stored fact for that civil date
  weekly value    arithmetic MEAN over PRESENT days only. The denominator is the number
                  of present days, never 7: a missing day is excluded, never imputed.
  valid week      present_days >= min_days_present_per_week (default 5)
  baseline        the 8 ISO weeks immediately preceding the target week
  usable baseline >= 6 of those 8 weeks are valid
  median          median of the valid weekly values
  mad             1.4826 * median(|w - median|)
  delta           week_value - median            (raw, in metric units)
  delta_z         delta / mad                    (standardized)
  zero mad        delta_z is null, undefined_reason='zero_dispersion', meaningful=False
  no threshold    if min_meaningful_delta is unset, meaningful=False with
                  undefined_reason='no_threshold_configured'
  meaningful      |delta_z| >= 1.0 AND |delta| >= min_meaningful_delta
"""
import statistics
from datetime import date, timedelta

MIN_DAYS_PRESENT_PER_WEEK = 5
BASELINE_WEEKS = 8
MIN_VALID_BASELINE_WEEKS = 6


def report_policy():
    return {"week": "iso-8601-monday", "weekly_value": "mean_over_present_days",
            "min_days_present_per_week": MIN_DAYS_PRESENT_PER_WEEK,
            "baseline_weeks": BASELINE_WEEKS,
            "min_valid_baseline_weeks": MIN_VALID_BASELINE_WEEKS,
            "dispersion": "mad_1.4826", "standardized_threshold": 1.0}


def iso_week_bounds(d):
    start = d - timedelta(days=d.isoweekday() - 1)
    return start, start + timedelta(days=6)


def default_week(as_of):
    this_start, _ = iso_week_bounds(as_of)
    return iso_week_bounds(this_start - timedelta(days=1))


def _facts(con, sid, metric_id, vdid, lo, hi):
    return {r[0]: r[1] for r in con.execute(
        "SELECT o.date, o.value FROM observations o "
        "JOIN metric_active_definition a ON a.metric_id=o.metric_id "
        "AND a.value_definition_id=o.value_definition_id "
        "WHERE o.store_instance_id=? AND o.metric_id=? AND o.value_definition_id=? "
        "AND o.date BETWEEN ? AND ? ORDER BY o.date",
        [sid, metric_id, vdid, lo, hi]).fetchall()}


def _week_value(facts, lo, hi, min_days):
    days = [facts[d] for d in facts if lo <= d <= hi]
    if len(days) < min_days:
        return None, len(days)
    return sum(days) / len(days), len(days)


def observed_change(con, *, sid, metric_id, vdid, week_start, week_end,
                    min_meaningful_delta, min_days=MIN_DAYS_PRESENT_PER_WEEK):
    """Database-backed producer. Identical arithmetic to the transport-backed one:
    both delegate to `observed_change_from_facts`, so a report assembled by the gateway
    from fetched facts and a report computed in-process cannot diverge."""
    span_lo = week_start - timedelta(weeks=BASELINE_WEEKS)
    return observed_change_from_facts(
        _facts(con, sid, metric_id, vdid, span_lo, week_end),
        metric_id=metric_id, week_start=week_start, week_end=week_end,
        min_meaningful_delta=min_meaningful_delta, min_days=min_days)


def baseline_span(week_start):
    """The earliest date the baseline needs. A fetch that does not cover this cannot
    support a report."""
    return week_start - timedelta(weeks=BASELINE_WEEKS)


def observed_change_from_facts(facts, *, metric_id, week_start, week_end,
                               min_meaningful_delta,
                               min_days=MIN_DAYS_PRESENT_PER_WEEK):
    present = sorted(d for d in facts if week_start <= d <= week_end)
    missing = [(week_start + timedelta(days=i)).isoformat()
               for i in range(7)
               if (week_start + timedelta(days=i)) not in facts]
    cov = {"metric_id": metric_id, "requested_days": 7, "present_days": len(present),
           "missing_dates": missing, "permanently_missing_dates": [],
           "dst_affected_dates": [], "stale_dates": [], "omitted_count": 0,
           "sufficient": len(present) >= min_days,
           "threshold_applied": f"min_days_present_per_week={min_days}"}

    wv, ndays = _week_value(facts, week_start, week_end, min_days)
    weekly = []
    for k in range(1, BASELINE_WEEKS + 1):
        lo = week_start - timedelta(weeks=k)
        v, _ = _week_value(facts, lo, lo + timedelta(days=6), min_days)
        if v is not None:
            weekly.append(v)
    if wv is None:
        return {"status": "insufficient_data", "metric_id": metric_id,
                "reason": "target_week_incomplete", "present_days": ndays,
                "required_days": min_days, "valid_baseline_weeks": len(weekly),
                "required_baseline_weeks": MIN_VALID_BASELINE_WEEKS, "coverage": cov}
    if len(weekly) < MIN_VALID_BASELINE_WEEKS:
        return {"status": "insufficient_data", "metric_id": metric_id,
                "reason": "baseline_incomplete", "present_days": ndays,
                "required_days": min_days, "valid_baseline_weeks": len(weekly),
                "required_baseline_weeks": MIN_VALID_BASELINE_WEEKS, "coverage": cov}

    med = statistics.median(weekly)
    mad = 1.4826 * statistics.median([abs(w - med) for w in weekly])
    delta = wv - med
    flags = ["daily_mean_of_total"]
    if mad == 0:
        flags.append("zero_dispersion")
        return {"status": "computed", "metric_id": metric_id, "week_value": wv,
                "baseline_median": med, "baseline_mad": 0.0, "delta": delta,
                "delta_z": None, "meaningful": False,
                "undefined_reason": "zero_dispersion", "flags": flags, "coverage": cov}
    if min_meaningful_delta is None:
        flags.append("no_threshold_configured")
        return {"status": "computed", "metric_id": metric_id, "week_value": wv,
                "baseline_median": med, "baseline_mad": mad, "delta": delta,
                "delta_z": delta / mad, "meaningful": False,
                "undefined_reason": "no_threshold_configured", "flags": flags,
                "coverage": cov}
    z = delta / mad
    return {"status": "computed", "metric_id": metric_id, "week_value": wv,
            "baseline_median": med, "baseline_mad": mad, "delta": delta, "delta_z": z,
            "meaningful": abs(z) >= 1.0 and abs(delta) >= min_meaningful_delta,
            "undefined_reason": None, "flags": flags, "coverage": cov}
