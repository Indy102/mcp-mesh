"""Circular time arithmetic on the 1440-minute day. Rev6 §9.

A clock_time metric is an angle, not a scalar: 23:50 and 00:10 are 20 minutes apart,
not 1420. Definitions used everywhere clock_time appears.

  theta(x)      = 2*pi*x/1440
  mean          = atan2( mean(sin theta), mean(cos theta) ) mapped back to [0,1440)
  resultant R   = hypot(mean sin, mean cos) in [0,1]; R==0 => mean UNDEFINED
  difference    = signed minimal arc. EXACT ANTIPODAL CONVENTION: at a separation of
                  exactly 720 minutes the minimal arc is genuinely ambiguous (+720 and
                  -720 are the same arc), so `difference` returns +720.0 by convention
                  and is therefore NOT antisymmetric at that single point. Any consumer
                  that needs a well-defined sign must use `signed_difference_or_none`,
                  which returns None at the antipode. Comparison uses that variant.
  dispersion    = circular MAD: median of |difference(x_i, median_direction)|
"""
import math

DAY = 1440.0


def _theta(x):
    return 2.0 * math.pi * (x % DAY) / DAY


def mean(values):
    """Circular mean in [0, 1440), or None when the resultant length is 0."""
    if not values:
        return None
    s = sum(math.sin(_theta(v)) for v in values) / len(values)
    c = sum(math.cos(_theta(v)) for v in values) / len(values)
    if math.hypot(s, c) < 1e-12:
        return None                      # antipodal/uniform: mean is undefined, not 0
    ang = math.atan2(s, c)
    return (ang * DAY / (2.0 * math.pi)) % DAY


def resultant_length(values):
    if not values:
        return 0.0
    s = sum(math.sin(_theta(v)) for v in values) / len(values)
    c = sum(math.cos(_theta(v)) for v in values) / len(values)
    return math.hypot(s, c)


def difference(a, b):
    """Signed minimal arc a - b, in (-720, 720]."""
    d = (a - b) % DAY
    if d > DAY / 2:
        d -= DAY
    return d


def is_antipodal(a, b, tol=1e-9):
    return abs(((a - b) % DAY) - DAY / 2) < tol


def signed_difference_or_none(a, b):
    """Signed minimal arc, or None when a and b are exactly antipodal (12h apart),
    where no sign is defined. Consumers must treat None as UNDEFINED, not as zero."""
    if is_antipodal(a, b):
        return None
    return difference(a, b)


def median_direction(values, tol=1e-9):
    """Direction minimising the sum of absolute circular differences.

    TIE RULE, specified so the result depends only on the MULTISET and never on input
    order: candidates are the DISTINCT values, evaluated in ascending order, and among
    costs within `tol` the SMALLEST representative angle in [0, 1440) wins.

    Deliberate limitation: this is permutation-invariant but NOT rotation-equivariant.
    Rotating every input can select a different representative, because "smallest angle"
    is defined on the [0,1440) representation. Reproducibility was preferred over
    rotation equivariance; a rotation-equivariant rule would need an arbitrary origin.
    """
    best, best_cost = None, None
    for cand in sorted({v % DAY for v in values}):
        cost = sum(abs(difference(v, cand)) for v in values)
        if best_cost is None or cost < best_cost - tol:
            best, best_cost = cand, cost
    return best


def mad(values):
    """Circular MAD, scaled by 1.4826. Permutation-invariant via `median_direction`."""
    if not values:
        return 0.0
    m = median_direction(values)
    devs = sorted(abs(difference(v, m)) for v in values)
    n = len(devs)
    med = devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2.0
    return 1.4826 * med


# ---------------------------------------------------------------------------
# The comparison-producing call site. Rev 8 shipped `signed_difference_or_none`
# with no consumer, so the promised antipodal representation was never produced.

def compare_periods(a_values, b_values, min_meaningful_delta, metric_id,
                    coverage_a=None, coverage_b=None):
    """Circular PeriodComparison. Returns the payload fragment defined by
    contracts.PeriodComparison, including the ANTIPODAL case."""
    a_mean, b_mean = mean(a_values), mean(b_values)
    pooled = mad(list(a_values) + list(b_values))
    flags = ["circular_metric"]
    if a_mean is None or b_mean is None:
        return {"status": "insufficient_data", "metric_id": metric_id,
                "reason": "both_incomplete" if a_mean is None and b_mean is None
                          else ("period_a_incomplete" if a_mean is None
                                else "period_b_incomplete"),
                "a_present_days": len(a_values), "b_present_days": len(b_values)}
    delta = signed_difference_or_none(b_mean, a_mean)
    if delta is None:
        flags.append("antipodal_circular_delta")
        return {"status": "computed", "metric_id": metric_id,
                "a_value": a_mean, "b_value": b_mean,
                "a_present_days": len(a_values), "b_present_days": len(b_values),
                "delta": None, "pooled_mad": pooled, "delta_z": None,
                "meaningful": False, "undefined_reason": "antipodal_circular_delta",
                "flags": flags, "coverage_a": coverage_a, "coverage_b": coverage_b}
    if pooled == 0:
        flags.append("zero_dispersion")
        return {"status": "computed", "metric_id": metric_id,
                "a_value": a_mean, "b_value": b_mean,
                "a_present_days": len(a_values), "b_present_days": len(b_values),
                "delta": delta, "pooled_mad": 0.0, "delta_z": None,
                # DECISION: `meaningful` is a STANDARDIZED judgement. With zero pooled
                # dispersion the standardized effect is undefined, so meaningfulness is
                # not asserted. The raw delta is still reported for the reader.
                "meaningful": False,
                "undefined_reason": "zero_dispersion", "flags": flags,
                "coverage_a": coverage_a, "coverage_b": coverage_b}
    z = delta / pooled
    return {"status": "computed", "metric_id": metric_id,
            "a_value": a_mean, "b_value": b_mean,
            "a_present_days": len(a_values), "b_present_days": len(b_values),
            "delta": delta, "pooled_mad": pooled, "delta_z": z,
            "meaningful": abs(z) >= 1.0 and abs(delta) >= min_meaningful_delta,
            "undefined_reason": None, "flags": flags,
            "coverage_a": coverage_a, "coverage_b": coverage_b}
