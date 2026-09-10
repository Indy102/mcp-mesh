"""T1c — semantic contract regressions from the independent contract probes.

Each asserts the CORRECTED behaviour; the probes asserted the defect.
"""
import copy, itertools, json, sys, unittest
from pathlib import Path
from jsonschema import Draft202012Validator as V, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import contracts as C, fixtures as F, circular as CIRC, evaluation as EV

FC = FormatChecker()


def sub(defn):
    return {"$defs": {k: C.DEFS[k] for k in C.closure([defn])}, "$ref": f"#/$defs/{defn}"}


def valid(defn, inst):
    return not list(V(sub(defn), format_checker=FC).iter_errors(inst))


ASSOC = F.FIXTURES["B10_association_confirmatory"]["response"]["result"] \
         ["structuredContent"]["association"]
COV = F.cov("a.b", 7, 7)


class RequiredByState(unittest.TestCase):
    """Probe: deleting any of six identity fields still validated."""
    FIELDS = ["threshold_source", "registry_digest", "method_digest",
              "eligibility_policy_version", "m_total", "m_eligible",
              "evaluation_id", "registry_version"]

    def test_baseline_is_valid(self):
        self.assertTrue(valid("Association", ASSOC))

    def test_deleting_any_confirmatory_identity_field_is_rejected(self):
        for f in self.FIELDS:
            with self.subTest(field=f):
                bad = copy.deepcopy(ASSOC); bad.pop(f)
                self.assertFalse(valid("Association", bad),
                                 f"deleting {f} must invalidate a confirmatory estimate")

    def test_nulling_any_confirmatory_identity_field_is_rejected(self):
        for f in self.FIELDS:
            with self.subTest(field=f):
                bad = copy.deepcopy(ASSOC); bad[f] = None
                self.assertFalse(valid("Association", bad))


class ExplorationFamilyConsistency(unittest.TestCase):
    """Probe: an ExplorationPayload accepted a confirmatory result with a q-value."""
    def _payload(self, results):
        return {"schema_version": C.SCHEMA_VERSION, "payload_type": "exploration",
                "generation": 41, "evaluation_family": "exploratory",
                "pairs_tested": len(results), "results": results,
                "truncation": {"truncated": False, "omitted_count": 0,
                               "truncation_reason": None},
                "method_caveats": ["a", "b"], "summary_text": "x"}

    def test_confirmatory_result_inside_an_exploratory_payload_is_rejected(self):
        self.assertFalse(valid("ExplorationPayload", self._payload([ASSOC])))

    def test_exploratory_result_is_accepted(self):
        ex = copy.deepcopy(ASSOC)
        ex.update(evaluation_family="exploratory", hypothesis_id=None, q_value=None,
                  registry_version=None, registry_digest=None, evaluation_id=None,
                  m_total=None, m_eligible=None)
        self.assertTrue(valid("ExplorationPayload", self._payload([ex])))


class AntipodalComparison(unittest.TestCase):
    """Probe: the promised antipodal representation was rejected by the schema."""
    def test_antipodal_result_is_produced_and_validates(self):
        r = CIRC.compare_periods([1430.0, 10.0], [710.0, 730.0], 15.0,
                                 "screentime.last_use_local_time_min",
                                 coverage_a=F.cov("screentime.last_use_local_time_min", 2, 2),
                                 coverage_b=F.cov("screentime.last_use_local_time_min", 2, 2))
        self.assertIsNone(r["delta"])
        self.assertIsNone(r["delta_z"])
        self.assertFalse(r["meaningful"])
        self.assertEqual(r["undefined_reason"], "antipodal_circular_delta")
        self.assertGreater(r["pooled_mad"], 0, "dispersion is genuinely positive here")
        self.assertTrue(valid("PeriodComparison", r))

    def test_antipodal_reason_is_distinct_from_zero_dispersion(self):
        """A negative test must START from a payload that validates."""
        base = CIRC.compare_periods([1430.0, 10.0], [710.0, 730.0], 15.0, "a.b",
                                    coverage_a=COV, coverage_b=COV)
        self.assertTrue(valid("PeriodComparison", base), "baseline must be valid")
        bad = dict(base); bad["undefined_reason"] = "zero_dispersion"
        self.assertFalse(valid("PeriodComparison", bad),
                         "zero_dispersion requires pooled_mad == 0 and a numeric delta")

    def test_null_delta_requires_the_antipodal_reason(self):
        base = CIRC.compare_periods([600.0, 610.0], [700.0, 710.0], 5.0, "a.b",
                                    coverage_a=COV, coverage_b=COV)
        self.assertTrue(valid("PeriodComparison", base), "baseline must be valid")
        bad = dict(base); bad["delta"] = None
        self.assertFalse(valid("PeriodComparison", bad))


class PeriodComparisonProducer(unittest.TestCase):
    """Positive producer coverage: every branch, with complete required fields."""
    def test_nonzero_dispersion(self):
        r = CIRC.compare_periods([600.0, 620.0, 590.0], [660.0, 700.0, 640.0], 15.0,
                                 "a.b", coverage_a=COV, coverage_b=COV)
        self.assertTrue(valid("PeriodComparison", r))
        self.assertIsNotNone(r["delta"]); self.assertIsNotNone(r["delta_z"])
        self.assertGreater(r["pooled_mad"], 0)
        self.assertIsNone(r["undefined_reason"])

    def test_zero_dispersion_is_not_meaningful(self):
        """Probe 7: the producer returned meaningful=True with delta_z null, which the
        contract forbids. `meaningful` is a STANDARDIZED judgement."""
        r = CIRC.compare_periods([600.0] * 7, [660.0] * 6, 15.0, "a.b",
                                 coverage_a=COV, coverage_b=COV)
        self.assertEqual(r["pooled_mad"], 0.0)
        self.assertIsNone(r["delta_z"])
        self.assertAlmostEqual(r["delta"], 60.0, places=6)
        self.assertFalse(r["meaningful"],
                         "an undefined standardized effect is not asserted meaningful")
        self.assertEqual(r["undefined_reason"], "zero_dispersion")
        self.assertTrue(valid("PeriodComparison", r))

    def test_antipodal(self):
        r = CIRC.compare_periods([1430.0, 10.0], [710.0, 730.0], 15.0, "a.b",
                                 coverage_a=COV, coverage_b=COV)
        self.assertIsNone(r["delta"]); self.assertFalse(r["meaningful"])
        self.assertEqual(r["undefined_reason"], "antipodal_circular_delta")
        self.assertTrue(valid("PeriodComparison", r))

    def test_insufficient_data(self):
        r = CIRC.compare_periods([], [600.0], 15.0, "a.b")
        self.assertEqual(r["status"], "insufficient_data")
        self.assertEqual(r["reason"], "period_a_incomplete")
        self.assertTrue(valid("PeriodComparison", r))


class RetentionDiscriminatedState(unittest.TestCase):
    """Probe 8: retention_status='finite' with null days and unbounded_retention=true."""
    def _status(self, **over):
        src = dict(F.st_src()); src.update(over)
        return {"schema_version": C.SCHEMA_VERSION, "payload_type": "status",
                "generation": 41, "as_of_date": "2026-09-06",
                "analysis_timezone": "America/Indiana/Indianapolis",
                "analysis_timezone_source": "recorded_value_definition",
                "store_instance_id": F.SIID,
                "redaction_level": "aggregate", "sources": [src]}

    def test_baseline_finite_status_is_valid(self):
        self.assertTrue(valid("StatusPayload", self._status()))

    def test_finite_with_null_days_and_unbounded_true_is_rejected(self):
        self.assertFalse(valid("StatusPayload", self._status(
            retention_status="finite", retention_days=None, unbounded_retention=True)))

    def test_finite_without_cadence_is_rejected(self):
        self.assertFalse(valid("StatusPayload", self._status(
            retention_status="finite", cadence_days=None)))

    def test_unknown_must_not_carry_days_or_loss_signals(self):
        self.assertFalse(valid("StatusPayload", self._status(
            retention_status="unknown", retention_days=28)))
        self.assertFalse(valid("StatusPayload", self._status(
            retention_status="unknown", retention_days=None, cadence_days=None,
            correction_days=None, unbounded_retention=False, sync_overdue=None,
            days_until_loss=5, data_loss_at_risk=False,
            loss_deadline_date=None, last_recoverable_date=None)))

    def test_unbounded_must_not_claim_a_deadline(self):
        self.assertFalse(valid("StatusPayload", self._status(
            retention_status="unbounded", retention_days=None, unbounded_retention=True,
            cadence_days=None, correction_days=None, sync_overdue=None,
            days_until_loss=3)))


class CircularDeterminism(unittest.TestCase):
    """Probe: mad([0,1,2,100]) != mad([100,2,1,0])."""
    def test_mad_is_permutation_invariant(self):
        vals = [0, 1, 2, 100]
        seen = {round(CIRC.mad(list(p)), 9) for p in itertools.permutations(vals)}
        self.assertEqual(len(seen), 1, f"MAD must depend only on the multiset: {seen}")

    def test_mad_is_permutation_invariant_on_a_wrapped_cluster(self):
        vals = [1430.0, 1435.0, 5.0, 10.0]
        seen = {round(CIRC.mad(list(p)), 9) for p in itertools.permutations(vals)}
        self.assertEqual(len(seen), 1)

    def test_median_direction_tie_rule_picks_the_smallest_angle(self):
        self.assertEqual(CIRC.median_direction([0.0, 720.0]), 0.0)

    def test_antipodal_difference_convention_is_documented_and_exact(self):
        self.assertEqual(CIRC.difference(0.0, 720.0), 720.0)
        self.assertEqual(CIRC.difference(720.0, 0.0), 720.0)
        self.assertIsNone(CIRC.signed_difference_or_none(0.0, 720.0))
        self.assertEqual(CIRC.signed_difference_or_none(10.0, 1430.0), 20.0)


class FamilyArithmetic(unittest.TestCase):
    """Probe: B10 declared m_eligible=1, p=0.0061 but carried q=0.0366."""
    def test_fixture_q_equals_an_independent_bh_computation(self):
        eligible = [h for h in F.FAMILY if h["eligible"]]
        expected = EV.benjamini_hochberg([h["p_value"] for h in eligible])
        self.assertEqual(len(eligible), ASSOC["m_eligible"])
        self.assertEqual(len(F.FAMILY), ASSOC["m_total"])
        self.assertAlmostEqual(ASSOC["q_value"], expected[0], places=12)
        self.assertAlmostEqual(ASSOC["q_value"], ASSOC["p_value"], places=12,
                               msg="BH on a single-member family returns p unchanged")

    def test_report_with_no_eligible_hypothesis_has_no_evaluation_identity(self):
        rep = F.FIXTURES["B7_weekly_report"]["response"]["result"]["structuredContent"]
        self.assertIsNone(rep["evaluation_id"])
        self.assertTrue(all(a["status"] != "estimated" for a in rep["associations"]))


class EvaluationIdentity(unittest.TestCase):
    BASE = dict(store_instance_id=F.SIID, generation=41, value_definition_id=F.VDID,
                policy_id=F.POLID, registry_dig=F.REG_DIG, eligibility_policy_version=1,
                method_dig=F.METH_DIG, window_start="2025-09-29",
                window_end="2026-08-23", metric_identity_dig=F.MID_DIG)

    def test_metric_identity_is_mandatory(self):
        args = dict(self.BASE); args.pop("metric_identity_dig")
        with self.assertRaises(TypeError):
            EV.evaluation_id(**args)
        with self.assertRaises(ValueError):
            EV.evaluation_id(**{**self.BASE, "metric_identity_dig": None})

    def test_all_ten_inputs_change_the_identity(self):
        base = EV.evaluation_id(**self.BASE)
        changes = {"generation": 42, "value_definition_id": "0" * 32,
                   "policy_id": "1" * 32, "registry_dig": "2" * 32,
                   "eligibility_policy_version": 2, "method_dig": "3" * 32,
                   "window_start": "2025-09-30", "window_end": "2026-08-24",
                   "store_instance_id": "4" * 32, "metric_identity_dig": "5" * 32}
        self.assertEqual(len(changes), 10)
        for k, v in changes.items():
            with self.subTest(input=k):
                self.assertNotEqual(EV.evaluation_id(**{**self.BASE, k: v}), base)

    def test_each_participating_metric_identity_changes_the_digest(self):
        base = EV.metric_identity_digest(F.METRIC_IDS)
        for metric in F.METRIC_IDS:
            for field in ("value_definition_id", "source_capability_id"):
                with self.subTest(metric=metric, field=field):
                    ids = copy.deepcopy(F.METRIC_IDS)
                    ids[metric][field] = "f" * 32
                    self.assertNotEqual(EV.metric_identity_digest(ids), base)

    def test_metric_boundaries_cannot_collide(self):
        a = EV.metric_identity_digest({"a.b": {"value_definition_id": "1" * 32,
                                               "source_capability_id": "2" * 32}})
        b = EV.metric_identity_digest({"a.b": {"value_definition_id": "2" * 32,
                                               "source_capability_id": "1" * 32}})
        self.assertNotEqual(a, b, "field-named canonical JSON must not be ambiguous")


class ListToolsInvariant(unittest.TestCase):
    """Probe: our own tools/list contract accepted a tool without outputSchema."""
    def test_missing_output_schema_is_rejected(self):
        res = copy.deepcopy(
            F.FIXTURES["B1_tools_list"]["response"]["result"])
        self.assertTrue(not list(V(C.list_tools_result_schema()).iter_errors(res)))
        res["tools"][0].pop("outputSchema")
        self.assertTrue(list(V(C.list_tools_result_schema()).iter_errors(res)),
                        "outputSchema is a v1 invariant")



class DeclaredSemanticInvariants(unittest.TestCase):
    """SI-1 and SI-2 belong to the CONTRACT, not to one server.

    `compare_periods` is specified but served by nobody (inference is disabled), so its
    rules are no longer reachable over any wire. They are still declared, so they are
    exercised here directly. SI-1 for `fetch` is additionally asserted on the wire in T4.
    """
    def test_SI1_reversed_period_is_rejected(self):
        for label in ("period_a", "period_b"):
            args = {"period_a": {"start": "2026-01-01", "end": "2026-01-31"},
                    "period_b": {"start": "2026-02-01", "end": "2026-02-28"}}
            args[label] = {"start": "2026-03-01", "end": "2026-01-01"}
            msg = C.semantic_errors("compare_periods", args)
            self.assertIsNotNone(msg, label)
            self.assertIn("SI-1", msg)
            self.assertIn(label, msg)

    def test_SI2_overlapping_periods_are_rejected(self):
        msg = C.semantic_errors("compare_periods", {
            "period_a": {"start": "2026-01-01", "end": "2026-02-01"},
            "period_b": {"start": "2026-01-15", "end": "2026-03-01"}})
        self.assertIn("SI-2", msg or "")

    def test_disjoint_periods_are_accepted(self):
        self.assertIsNone(C.semantic_errors("compare_periods", {
            "period_a": {"start": "2026-01-01", "end": "2026-01-31"},
            "period_b": {"start": "2026-02-01", "end": "2026-02-28"}}))

    def test_SI1_applies_to_fetch(self):
        self.assertIn("SI-1", C.semantic_errors(
            "fetch", {"start": "2026-03-01", "end": "2026-01-01"}) or "")
        self.assertIsNone(C.semantic_errors(
            "fetch", {"start": "2026-01-01", "end": "2026-03-01"}))


class ReportEvidenceGating(unittest.TestCase):
    """A report may not compute a complete-looking baseline from partial evidence."""
    def test_evidence_truncated_is_an_expressible_unknown(self):
        import report as RP
        sch = {"$defs": {k: C.DEFS[k] for k in C.closure(["ObservedChange"])},
               "$ref": "#/$defs/ObservedChange"}
        for reason in ("evidence_truncated", "source_unavailable", "source_failed",
                       "no_metric_range"):
            ch = {"status": "unknown", "metric_id": "git.commits_count",
                  "reason": reason}
            self.assertTrue(V(sch, format_checker=FC).is_valid(ch), reason)

    def test_the_two_producers_agree(self):
        """The gateway assembles reports from FETCHED facts; the database producer is
        used for in-process recomputation. They must be the same arithmetic."""
        import report as RP, tempfile, collector as CO, store as STO
        from datetime import date, timedelta
        facts = {date(2026, 6, 1) + timedelta(days=i): float(i % 4) for i in range(70)}
        ws, we = date(2026, 8, 3), date(2026, 8, 9)
        a = RP.observed_change_from_facts(facts, metric_id="git.commits_count",
                                          week_start=ws, week_end=we,
                                          min_meaningful_delta=1.0)
        self.assertEqual(a["status"], "computed")
        with tempfile.TemporaryDirectory() as td:
            st = STO.Store(Path(td) / "d" / "facts.duckdb")
            sid = st.init(STO.digest("policy", {"p": 1}))
            defn = {"metric_id": "git.commits_count", "normalization_version": 1}
            vd = STO.digest("value_definition", defn)
            cap = {"source": "git", "retention_status": "unbounded",
                   "retention_days": None, "effective_from": date(2026, 1, 1)}
            capid = STO.digest("source_capability", cap)
            fr = CO.FetchResult("git.commits_count", "ok", observed={
                d: {"value": v, "sample_count": 1} for d, v in facts.items()})
            st.publish(lambda con: CO.apply_fetch(
                con, sid=sid, vdid=vd, capid=capid, run_id="r", source="git", fr=fr,
                definition=defn, capability=cap))
            con, _ = st.open_snapshot()
            try:
                b = RP.observed_change(con, sid=sid, metric_id="git.commits_count",
                                       vdid=vd, week_start=ws, week_end=we,
                                       min_meaningful_delta=1.0)
            finally:
                con.close()
        self.assertEqual(a, b, "one arithmetic, two entry points")


if __name__ == "__main__":
    unittest.main(verbosity=2)
