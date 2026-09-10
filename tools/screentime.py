"""Screen Time durability — Rev 7 normative implementation.

NORMATIVE DEFINITIONS (all fixed here; the spec quotes this module).

Dates
  * Civil dates in the configured `analysis_timezone`. Ranges are INCLUSIVE at both ends.
  * `as_of` is "today" in that timezone, injectable for tests.
  * Observation dates AFTER `as_of` are a configuration error, not a future tail.

Retention is a TRI-STATE, carried explicitly end to end:
  * "unknown"   => the source capability has not been MEASURED. No horizon, no cadence,
                   no loss deadline, and no claim either way. An unmeasured source must
                   never be silently treated as unbounded.
  * "finite"    => R is a strict int >= 1.
  * "unbounded" => the source never discards; no horizon and no cadence.
  `retention_status` defaults to "unbounded" when R is None and "finite" otherwise, so
  existing two-argument callers are unchanged.

Retention R
  * `R is None`  => retention UNBOUNDED and no collection cadence is derivable.
  * `R` finite   => a strict `int` (bool rejected) with R >= 1.
  * `horizon` is the EARLIEST civil date still obtainable, INCLUSIVE:
        horizon = as_of - (R - 1)
    so R=1 makes only `as_of` obtainable.

Deadlines — last recoverable vs first unavailable (distinct fields)
  * A date D is obtainable through `D + R - 1` (last_recoverable_date).
  * D first becomes unavailable on `D + R`  (loss_deadline_date).
  * `days_until_loss = (loss_deadline_date - as_of).days`, computed from the EARLIEST
    RECOVERABLE unresolved date. Because a recoverable date satisfies D >= horizon,
    `days_until_loss >= 1` always; it is never zero or negative by construction.

durable_since
  * The first date of the CONTIGUOUS OBSERVED TAIL that ends at the most recent
    observation. Computed from observations alone; later gaps cannot move it.

earliest_unresolved_date
  * The earliest date in a RECOVERABLE gap. A permanently missing date is never
    reported as unresolved: nothing can be done about it. If every missing date is
    already permanent, this is None and `data_loss_at_risk` is False.

The two independent signals
  * `sync_overdue`      — is collection behind cadence?  None when no cadence exists.
  * `data_loss_at_risk` — will recoverable archive be destroyed soon?
  Cadence boundary, stated exactly: overdue iff `elapsed_days >= cadence_days`.
  At elapsed = cadence-1 it is False; at cadence and cadence+1 it is True.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, List


class RetentionConfigError(ValueError):
    """Invalid retention configuration or observation input."""


@dataclass(frozen=True)
class Gap:
    start: date
    end: date                       # inclusive

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass
class Durability:
    retention_days: Optional[int]
    retention_status: str
    unbounded_retention: bool
    cadence_days: Optional[int]
    correction_days: Optional[int]
    horizon: Optional[date]
    durable_since: Optional[date]
    permanent_gaps: List[Gap] = field(default_factory=list)
    recoverable_gaps: List[Gap] = field(default_factory=list)
    earliest_unresolved_date: Optional[date] = None
    last_recoverable_date: Optional[date] = None
    loss_deadline_date: Optional[date] = None
    days_until_loss: Optional[int] = None
    sync_overdue: Optional[bool] = None
    data_loss_at_risk: bool = False
    low_retention_mode: bool = False


def validate_retention(R):
    """R is None (unbounded) or a strict positive int. bool and float are rejected."""
    if R is None:
        return None
    if isinstance(R, bool):
        raise RetentionConfigError("retention_days must not be a bool; got True/False")
    if not isinstance(R, int):
        raise RetentionConfigError(
            f"retention_days must be an int number of civil days or None; got "
            f"{type(R).__name__} ({R!r})")
    if R <= 0:
        raise RetentionConfigError(f"retention_days must be >= 1 or None; got {R}")
    return R


def coalesce(gaps: List[Gap]) -> List[Gap]:
    """Merge overlapping or adjacent gaps (next.start <= cur.end + 1 day)."""
    out: List[Gap] = []
    for g in sorted(gaps, key=lambda x: (x.start, x.end)):
        if out and g.start <= out[-1].end + timedelta(days=1):
            out[-1] = Gap(out[-1].start, max(out[-1].end, g.end))
        else:
            out.append(g)
    return out


def _gaps_between(observed: set, lo: date, hi: date) -> List[Gap]:
    runs, cur, d = [], None, lo
    while d <= hi:
        if d in observed:
            if cur is not None:
                runs.append(Gap(cur, d - timedelta(days=1)))
                cur = None
        elif cur is None:
            cur = d
        d += timedelta(days=1)
    if cur is not None:
        runs.append(Gap(cur, hi))
    return runs


def _observed_tail_start(observed_sorted: List[date]) -> date:
    """First date of the contiguous run ending at the LAST observation.

    Depends only on observations. Later unobserved dates cannot truncate it.
    """
    start = observed_sorted[-1]
    for prev in reversed(observed_sorted[:-1]):
        if prev == start - timedelta(days=1):
            start = prev
        else:
            break
    return start


def compute(observed_dates, as_of, retention_days, last_successful_sync_date,
            retention_status=None):
    """observed_dates: civil dates on which the CANONICAL metric is currently `observed`."""
    R = validate_retention(retention_days)
    if retention_status is None:
        retention_status = "unbounded" if R is None else "finite"
    if retention_status not in ("unknown", "finite", "unbounded"):
        raise RetentionConfigError(f"retention_status must be unknown|finite|unbounded; "
                                   f"got {retention_status!r}")
    if retention_status == "finite" and R is None:
        raise RetentionConfigError("retention_status='finite' requires retention_days")
    if retention_status in ("unknown", "unbounded") and R is not None:
        raise RetentionConfigError(
            f"retention_status={retention_status!r} must not carry retention_days")
    unbounded = R is None

    observed = set(observed_dates)
    future = sorted(d for d in observed if d > as_of)
    if future:
        raise RetentionConfigError(
            f"observed dates after as_of={as_of.isoformat()}: {future[0].isoformat()}"
            f" (+{len(future) - 1} more)" if len(future) > 1 else
            f"observed dates after as_of={as_of.isoformat()}: {future[0].isoformat()}")

    cadence = None if unbounded else max(1, R // 2)
    correction = None if unbounded else min(7, cadence)
    horizon = None if unbounded else as_of - timedelta(days=R - 1)

    d = Durability(retention_days=R, retention_status=retention_status,
                   unbounded_retention=(retention_status == "unbounded"),
                   cadence_days=cadence, correction_days=correction, horizon=horizon,
                   durable_since=None,
                   low_retention_mode=(not unbounded and R <= 3))

    # --- cadence signal (independent of any stored data) --------------------------
    if retention_status == "unknown":
        # Nothing is known about the source's horizon, so no cadence can be derived
        # and no loss claim may be made in EITHER direction.
        d.sync_overdue = None
    elif unbounded:
        # No retention horizon means no derivable cadence. Reporting `True` here would
        # fabricate a policy that does not exist; the status is UNKNOWN.
        d.sync_overdue = None
    elif last_successful_sync_date is None:
        d.sync_overdue = True
    else:
        d.sync_overdue = (as_of - last_successful_sync_date).days >= cadence

    if retention_status == "unknown" or not observed:
        # Unknown retention: no horizon, so gaps cannot be classified at all.
        # No observations: collection may be due, but nothing exists to destroy.
        d.data_loss_at_risk = False
        if observed and retention_status == "unknown":
            d.durable_since = _observed_tail_start(sorted(observed))
        return d

    obs_sorted = sorted(observed)
    last = obs_sorted[-1]
    d.durable_since = _observed_tail_start(obs_sorted)

    span_hi = as_of - timedelta(days=1)          # today is not yet expected complete
    gaps = _gaps_between(observed, obs_sorted[0], span_hi) if span_hi >= obs_sorted[0] else []

    perm, recov = [], []
    for g in gaps:
        if unbounded:
            recov.append(g)
            continue
        if g.start < horizon:
            perm.append(Gap(g.start, min(g.end, horizon - timedelta(days=1))))
        if g.end >= horizon:
            recov.append(Gap(max(g.start, horizon), g.end))
    d.permanent_gaps, d.recoverable_gaps = coalesce(perm), coalesce(recov)

    # Unresolved means RECOVERABLE-and-missing. Derived from the classified recoverable
    # gaps only, so a permanent date can never be reintroduced as a deadline driver.
    d.earliest_unresolved_date = d.recoverable_gaps[0].start if d.recoverable_gaps else None

    if unbounded or d.earliest_unresolved_date is None:
        d.last_recoverable_date = d.loss_deadline_date = d.days_until_loss = None
        d.data_loss_at_risk = False
    else:
        u = d.earliest_unresolved_date
        d.last_recoverable_date = u + timedelta(days=R - 1)   # still obtainable on this day
        d.loss_deadline_date = u + timedelta(days=R)          # first unavailable day
        d.days_until_loss = (d.loss_deadline_date - as_of).days
        d.data_loss_at_risk = d.days_until_loss <= cadence
    return d
