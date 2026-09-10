"""Independent regression probes for the supplied Rev 6 Screen Time module.

Run with the standard library only:
    python3 -B rev6_screentime_regressions.py /path/to/screentime.py

The tests fail against the attachment reviewed on 2026-09-06. They assert
Rev 6's stated definitions, not a proposed change to those definitions.
The target module is loaded read-only; bytecode generation is disabled.
"""

import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
if len(sys.argv) != 2:
    raise SystemExit("Usage: python3 -B rev6_screentime_regressions.py /path/to/screentime.py")
module_path = Path(sys.argv.pop()).resolve()
spec = importlib.util.spec_from_file_location("reviewed_screentime", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class LongTrailingGapRegression(unittest.TestCase):
    def setUp(self):
        self.result = module.compute(
            observed_dates=[date(2026, 8, 1), date(2026, 8, 2)],
            as_of=date(2026, 9, 6),
            retention_days=28,
            last_successful_sync_date=date(2026, 8, 2),
        )

    def test_unresolved_date_is_still_recoverable(self):
        # Horizon is Aug 10. Aug 3–9 are already permanent gaps.
        # The earliest recoverable unresolved date must therefore be Aug 10.
        self.assertEqual(self.result.horizon, date(2026, 8, 10))
        self.assertEqual(
            self.result.earliest_unresolved_date,
            date(2026, 8, 10),
            "A permanently missing date must not be reintroduced as recoverable.",
        )

    def test_durable_since_starts_at_beginning_of_observed_tail(self):
        # Rev 6 defines the tail as ending at the MOST RECENT OBSERVATION.
        # The observations Aug 1 and Aug 2 are contiguous. Gaps after Aug 2
        # do not change where that observed tail begins.
        self.assertEqual(
            self.result.durable_since,
            date(2026, 8, 1),
            "Later unobserved dates must not truncate the observed tail.",
        )

    def test_loss_deadline_is_first_unavailable_day(self):
        # Aug 10 is obtainable through Sep 6 (Aug 10 + R - 1). It first
        # becomes unavailable on Sep 7, so one day remains at this as_of date.
        self.assertEqual(
            self.result.days_until_loss,
            1,
            "The deadline is the first unavailable day, not the last recoverable day.",
        )


class PermanentOnlyRegression(unittest.TestCase):
    def test_no_recoverable_date_is_not_reported_as_unresolved(self):
        # With R=1, an observation on Sep 1 and as_of Sep 6 leaves Sep 2–5
        # permanently unavailable. There is no recoverable unresolved date.
        result = module.compute(
            observed_dates=[date(2026, 9, 1)],
            as_of=date(2026, 9, 6),
            retention_days=1,
            last_successful_sync_date=date(2026, 9, 1),
        )
        self.assertIsNone(
            result.earliest_unresolved_date,
            "A permanent gap must not be reported as an unresolved recoverable date.",
        )
        self.assertFalse(
            result.data_loss_at_risk,
            "There is no recoverable data left to lose.",
        )


class UnboundedStatusRegression(unittest.TestCase):
    def test_unbounded_retention_has_no_cadence_overdue_signal(self):
        # R=None explicitly means no retention horizon and no cadence. A
        # never-synced source therefore cannot be marked overdue under a
        # cadence that does not exist; the API should use null/unknown (the
        # recommended representation) rather than true.
        result = module.compute(
            observed_dates=[],
            as_of=date(2026, 9, 6),
            retention_days=None,
            last_successful_sync_date=None,
        )
        self.assertIsNone(
            result.sync_overdue,
            "Unbounded retention must not manufacture a cadence-based overdue flag.",
        )

    def test_empty_finite_source_has_no_data_loss_risk(self):
        # A source with no observations is due for collection, but it has no
        # archived value that can be destroyed. Keep collection urgency and
        # data-loss risk independent.
        result = module.compute(
            observed_dates=[],
            as_of=date(2026, 9, 6),
            retention_days=28,
            last_successful_sync_date=None,
        )
        self.assertFalse(
            result.data_loss_at_risk,
            "No observations means there is no stored data to lose.",
        )


class RetentionTypeRegression(unittest.TestCase):
    def test_fractional_retention_is_rejected(self):
        # The storage contract models R as an INTEGER number of civil days. A
        # fractional value must not silently flow into // arithmetic and date
        # calculations with a different meaning.
        with self.assertRaises(module.RetentionConfigError):
            module.compute(
                observed_dates=[date(2026, 9, 1)],
                as_of=date(2026, 9, 6),
                retention_days=1.5,
                last_successful_sync_date=date(2026, 9, 6),
            )

    def test_boolean_retention_is_rejected(self):
        # bool is an int subclass in Python, but True/False are not valid
        # retention-day configuration values.
        with self.assertRaises(module.RetentionConfigError):
            module.compute(
                observed_dates=[date(2026, 9, 1)],
                as_of=date(2026, 9, 6),
                retention_days=True,
                last_successful_sync_date=date(2026, 9, 6),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
