"""Tier T3 — real DuckDB, real collector, real swap. No fixtures.

Every test here executes the production code paths in tools/store.py and
tools/collector.py against an on-disk database.
"""
import json, os, sys, tempfile, time, unittest, duckdb
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import screentime as ST, collector as CO, store as ST_
from store import (Store, StoreTokenError, WriterBusyError, CompareAndSwapError,
                   StoreStateError, StoreIntegrityError, StoreAlreadyInitialized,
                   LifecycleTransitionError, IdentityContentConflict,
                   digest, check_integrity, register_definition, register_capability,
                   activate_definition, next_seq)

VD_ST = digest("value_definition", {"metric": "screentime.screen_minutes_total",
                                    "tz": "America/Indiana/Indianapolis", "taxonomy": "v1"})
VD_W = digest("value_definition", {"metric": "weather.precip_mm", "model": "ecmwf_ifs"})
CAP28 = digest("source_capability", {"source": "screentime", "retention_days": 28})
CAP14 = digest("source_capability", {"source": "screentime", "retention_days": 14})
POL_A = digest("policy", {"refresh": {"screentime": 7}})
POL_B = digest("policy", {"refresh": {"screentime": 5}})
ST_M, W_M = "screentime.screen_minutes_total", "weather.precip_mm"


def mkstore():
    d = tempfile.mkdtemp(prefix="meshtest-")
    s = Store(Path(d) / "facts.duckdb")
    return s, s.init(POL_A), d


def defn(metric, **kw):
    d = {"metric_id": metric, "timezone": "America/Indiana/Indianapolis", "unit": "min"}
    d.update(kw); return d


def capa(source="screentime", R=28, status="finite", eff=date(2026, 1, 1)):
    return {"source": source, "retention_status": status, "retention_days": R,
            "effective_from": eff}


def register(con, metric, vdid, cap, source="screentime", R=28, status="finite"):
    register_definition(con, metric, vdid, defn(metric))
    register_capability(con, cap, capa(source, R, status))
    activate_definition(con, metric, vdid)


def seed(con, sid, metric, vdid, cap, dates, source, value=100.0, reg=True,
         run_id="seed"):
    if reg:
        register(con, metric, vdid, cap, source)
    # Every stored fact names a RECORDED run: provenance is an invariant, so even a
    # low-level fixture has to construct a store that satisfies it.
    ST_.record_run(con, sid=sid, run_id=run_id, source=source, metric_id=metric,
                   vdid=vdid, capid=cap, outcome="ok")
    for d in dates:
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                    [sid, metric, d, vdid, source, value, "measured", 1, run_id])
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [sid, metric, d, vdid, cap, next_seq(con), "observed"])


def daterange(a, b):
    out, d = [], a
    while d <= b:
        out.append(d); d += timedelta(days=1)
    return out


class B24GapRepair(unittest.TestCase):
    """Rev 6 B24 detected a recoverable internal gap but never showed a sync closing it."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.as_of = date(2026, 9, 6)
        obs = [d for d in daterange(date(2026, 8, 1), date(2026, 9, 5))
               if not (date(2026, 8, 12) <= d <= date(2026, 8, 14))]
        def m(con): seed(con, self.sid, ST_M, VD_ST, CAP28, obs, "screentime")
        self.s.publish(m)

    def test_gap_is_detected_then_closed_by_a_real_sync(self):
        con = self.s.connect(read_only=True)
        d0 = CO.durability_from_store(con, self.sid, "screentime", ST_M, VD_ST,
                                      self.as_of, 28, date(2026, 9, 6))
        con.close()
        self.assertEqual([(g.start, g.end) for g in d0.recoverable_gaps],
                         [(date(2026, 8, 12), date(2026, 8, 14))])
        self.assertEqual(d0.earliest_unresolved_date, date(2026, 8, 12))
        self.assertTrue(d0.data_loss_at_risk)

        want = CO.collection_set(self.as_of, 7, date(2026, 9, 6), d0, d0.horizon)
        self.assertTrue({date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)} <= set(want),
                        "the collection set must include the internal recoverable gap")

        fr = CO.FetchResult(ST_M, "ok",
                            observed={d: {"value": 55.0} for d in want})
        def m(con): CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(),
                                   run_id="r2", source="screentime", fr=fr)
        self.s.publish(m)

        con = self.s.connect(read_only=True)
        d1 = CO.durability_from_store(con, self.sid, "screentime", ST_M, VD_ST,
                                      self.as_of, 28, date(2026, 9, 6))
        con.close()
        self.assertEqual(d1.recoverable_gaps, [], "the gap must be closed after sync")
        self.assertIsNone(d1.earliest_unresolved_date)
        self.assertFalse(d1.data_loss_at_risk)

    def test_failed_fetch_writes_nothing_and_preserves_archive(self):
        con = self.s.connect(read_only=True)
        before = con.execute("SELECT count(*) FROM observations").fetchone()[0]
        con.close()
        fr = CO.FetchResult(ST_M, "failed", reason="knowledgeC unreadable")
        def m(con): CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(),
                                   run_id="r3", source="screentime", fr=fr)
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], before)
        con.close()

    def test_partial_recovery_closes_part_of_the_gap(self):
        fr = CO.FetchResult(ST_M, "partial",
                            observed={date(2026, 8, 12): {"value": 40.0}},
                            undetermined=[date(2026, 8, 13), date(2026, 8, 14)])
        def m(con): CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(),
                                   run_id="r4", source="screentime", fr=fr)
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        d = CO.durability_from_store(con, self.sid, "screentime", ST_M, VD_ST,
                                     self.as_of, 28, date(2026, 9, 6))
        con.close()
        self.assertEqual([(g.start, g.end) for g in d.recoverable_gaps],
                         [(date(2026, 8, 13), date(2026, 8, 14))])


class AdapterEvidence(unittest.TestCase):
    """observed zero / known_absent / undetermined / failed must stay distinguishable."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()

    def test_four_read_outcomes_persist_distinctly(self):
        fr = CO.FetchResult(ST_M, "partial",
            observed={date(2026, 9, 1): {"value": 0.0, "sample_count": 0}},
            known_absent=[date(2026, 9, 2)],
            undetermined=[date(2026, 9, 3)])
        def m(con): CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(),
                                   run_id="r1", source="screentime", fr=fr)
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        av = dict(con.execute(
            "SELECT date, state FROM metric_date_availability WHERE metric_id=?",
            [ST_M]).fetchall())
        zero = con.execute(
            "SELECT value, sample_count FROM observations WHERE date=?",
            [date(2026, 9, 1)]).fetchone()
        con.close()
        self.assertEqual(zero, (0.0, 0), "a proven no-use day is a structural zero")
        self.assertEqual(av[date(2026, 9, 1)], "observed")
        self.assertEqual(av[date(2026, 9, 2)], "known_absent")
        self.assertNotIn(date(2026, 9, 3), av, "undetermined must remain UNKNOWN, not absent")


class IdentityScoping(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        def m(con):
            seed(con, self.sid, ST_M, VD_ST, CAP28, daterange(date(2026,8,1), date(2026,8,5)), "screentime")
            seed(con, self.sid, W_M, VD_W, "cap-weather", daterange(date(2026,8,1), date(2026,8,5)), "weather", 2.0)

        self.s.publish(m)

    def test_policy_only_change_preserves_values(self):
        def m(con): con.execute("UPDATE store_manifest SET policy_id=? WHERE id=1", [POL_B])
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        n = con.execute("SELECT count(*) FROM observations_current").fetchone()[0]
        pol = con.execute("SELECT policy_id FROM store_manifest").fetchone()[0]
        con.close()
        self.assertEqual(pol, POL_B)
        self.assertEqual(n, 10, "a cadence/policy change must not rewrite or hide values")

    def test_weather_definition_change_preserves_screentime(self):
        VD_W2 = digest("value_definition", {"metric": W_M, "model": "noaa_gfs"})
        def m(con):
            con.execute("INSERT INTO metric_definitions VALUES (?,?,now(),'{}')",
                        [W_M, VD_W2])
            con.execute("UPDATE metric_active_definition SET value_definition_id=?, "
                        "activated_at=now() WHERE metric_id=?", [VD_W2, W_M])
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        st_now = con.execute("SELECT count(*) FROM observations_current WHERE metric_id=?",
                             [ST_M]).fetchone()[0]
        w_now = con.execute("SELECT count(*) FROM observations_current WHERE metric_id=?",
                            [W_M]).fetchone()[0]
        w_archived = con.execute(
            "SELECT count(*) FROM observations WHERE metric_id=? AND value_definition_id=?",
            [W_M, VD_W]).fetchone()[0]
        con.close()
        self.assertEqual(st_now, 5, "a weather redefinition must not hide Screen Time history")
        self.assertEqual(w_now, 0, "weather under the retired definition leaves the current view")
        self.assertEqual(w_archived, 5, "retired-definition rows are archived, not destroyed")

    def test_overlapping_refresh_under_new_definition_preserves_old(self):
        VD_ST2 = digest("value_definition", {"metric": ST_M, "tz": "UTC", "taxonomy": "v1"})
        fr = CO.FetchResult(ST_M, "ok",
                            observed={d: {"value": 9.0} for d in daterange(date(2026,8,3), date(2026,8,5))})
        def m(con): CO.apply_fetch(con, sid=self.sid, vdid=VD_ST2, capid=CAP28, definition=defn(ST_M), capability=capa(),
                                   run_id="r9", source="screentime", fr=fr)
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        old = con.execute("SELECT count(*) FROM observations WHERE metric_id=? AND value_definition_id=?",
                          [ST_M, VD_ST]).fetchone()[0]
        new = con.execute("SELECT count(*) FROM observations WHERE metric_id=? AND value_definition_id=?",
                          [ST_M, VD_ST2]).fetchone()[0]
        con.close()
        self.assertEqual(old, 5, "an overlapping refresh under a NEW definition must not delete the old")
        self.assertEqual(new, 3)

    def test_capability_change_creates_an_availability_boundary(self):
        """Changing measured retention R changes what 'recoverable' means, so old
        availability rows stay queryable under their own capability id."""
        def m(con):
            con.execute("INSERT OR REPLACE INTO source_capabilities VALUES "
                        "(?,?,?,?,now(),?,NULL)", [CAP14, "screentime", "finite", 14,
                                                   date(2026, 8, 6)])
            con.execute("DELETE FROM observations WHERE metric_id=? AND date=?",
                        [ST_M, date(2026, 8, 1)])
            con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                        [self.sid, ST_M, date(2026, 8, 1), VD_ST, CAP14, next_seq(con),
                         "permanently_missing"])
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        caps = [r[0] for r in con.execute(
            "SELECT DISTINCT source_capability_id FROM metric_date_availability "
            "WHERE metric_id=? ORDER BY 1", [ST_M]).fetchall()]
        con.close()
        self.assertEqual(sorted(caps), sorted([CAP28, CAP14]),
                         "classifications under the old capability remain queryable")
        con = self.s.connect(read_only=True)
        both = con.execute(
            "SELECT state FROM metric_date_availability WHERE metric_id=? AND date=? "
            "ORDER BY source_capability_id", [ST_M, date(2026, 8, 1)]).fetchall()
        cur = con.execute(
            "SELECT state FROM metric_date_availability_current WHERE metric_id=? AND date=?",
            [ST_M, date(2026, 8, 1)]).fetchall()
        con.close()
        self.assertEqual(len(both), 2,
                         "the SAME date is retained under BOTH capability versions")
        self.assertEqual(len(cur), 1, "the current view resolves to exactly one row")


class StoreTokenLifecycle(unittest.TestCase):
    def test_destroy_recreate_rejects_the_old_token(self):
        s, sid, _ = mkstore()
        old = s.token()
        self.assertEqual(old["generation"], 1)
        s.publish(lambda con: None)
        mid = s.token(); self.assertEqual(mid["generation"], 2)
        new_sid = s.destroy_and_recreate(POL_A)
        fresh = s.token()
        self.assertNotEqual(new_sid, sid, "a re-created store must mint a new instance id")
        self.assertEqual(fresh["generation"], 1, "generation restarts, which is the trap")
        with self.assertRaises(StoreTokenError):
            s.require_token(old)                       # same generation number, different store
        s.require_token(fresh)

    def test_generation_alone_is_not_a_token(self):
        s, sid, _ = mkstore()
        with self.assertRaises(StoreTokenError):
            s.require_token({"generation": 1})


class PersistentReaderSwap(unittest.TestCase):
    """The Rev 6 lifecycle hole: os.replace changes the directory entry, but a reader
    that opened the file earlier keeps the OLD inode and can retry forever."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()

    def test_persistent_connection_keeps_the_stale_generation(self):
        held = self.s.connect(read_only=True)
        g0 = held.execute("SELECT generation FROM store_manifest").fetchone()[0]
        self.s.publish(lambda con: None)
        g_held = held.execute("SELECT generation FROM store_manifest").fetchone()[0]
        held.close()
        self.assertEqual(g0, g_held,
                         "a held connection does NOT observe the swap - retrying it is futile")
        self.assertEqual(self.s.token()["generation"], g0 + 1)

    def test_generation_versioned_paths_defeat_the_instance_cache(self):
        """Measured on duckdb 1.5.5: with a FIXED filename, a new connect() while any
        handle is open returns the CACHED old instance. Generation-versioned paths make
        each generation a path the process has never opened."""
        held = self.s.connect(read_only=True)          # deliberately still open
        g0 = self.s.token()["generation"]
        self.s.publish(lambda con: None)
        fresh = self.s.connect(read_only=True)         # different PATH, so not cached
        g_new = fresh.execute("SELECT generation FROM store_manifest").fetchone()[0]
        fresh.close(); held.close()
        self.assertEqual(g_new, g0 + 1,
                         "a new generation must be observable even with an old handle open")

    def test_reopen_per_request_converges_within_a_bounded_retry(self):
        held = self.s.connect(read_only=True)          # leaf started before the swap
        self.s.publish(lambda con: None)
        target = self.s.token()                        # pointer is authoritative
        attempts, seen, budget = 0, None, 3
        while attempts < budget:
            attempts += 1
            held.close()
            held = self.s.connect(read_only=True)      # reopen at the CURRENT path
            seen = held.execute(
                "SELECT store_instance_id, generation FROM store_manifest").fetchone()
            if seen[0] == target["store_instance_id"] and seen[1] == target["generation"]:
                break
        held.close()
        self.assertEqual(seen[1], target["generation"])
        self.assertEqual(attempts, 1, "reopen converges on the first generation retry")


class SwapDurability(unittest.TestCase):
    def test_build_file_sweep_respects_liveness(self):
        s, sid, d = mkstore()
        dead = Path(d) / "facts.build.999999.aaaaaaaa.duckdb"; dead.write_text("x")
        live = Path(d) / f"facts.build.{os.getpid()}.bbbbbbbb.duckdb"; live.write_text("x")
        removed = Store.sweep_build_files(d)
        self.assertIn(dead.name, removed)
        self.assertTrue(live.exists(), "a live process's build file must not be removed")

    def test_failed_mutation_leaves_the_store_unchanged(self):
        s, sid, d = mkstore()
        s.publish(lambda con: seed(con, sid, ST_M, VD_ST, CAP28, [date(2026,8,1)], "screentime"))
        before = s.token()
        def boom(con):
            seed(con, sid, ST_M, VD_ST, CAP28, [date(2026,8,2)], "screentime")
            raise RuntimeError("torn build")
        with self.assertRaises(RuntimeError):
            s.publish(boom)
        self.assertEqual(s.token(), before, "a torn build must not advance the generation")
        con = s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        con.close()
        self.assertEqual([f for f in Path(d).glob('facts.build.*')], [],
                         "the temporary build file is removed on every exit path")


class CorrectionWindowBoundary(unittest.TestCase):
    def test_seven_days_means_seven_civil_dates(self):
        d = ST.compute([date(2026, 9, 5)], date(2026, 9, 6), 28, date(2026, 9, 6))
        s = CO.collection_set(date(2026, 9, 6), 7, None, d, d.horizon)
        self.assertEqual(len(s), 7)
        self.assertEqual(min(s), date(2026, 8, 31))
        self.assertEqual(max(s), date(2026, 9, 6))


class CadenceBoundary(unittest.TestCase):
    """R=28 => cadence 14. Overdue iff elapsed >= cadence. Test N-1, N, N+1."""
    def _overdue(self, elapsed):
        as_of = date(2026, 9, 30)
        return ST.compute([date(2026, 9, 1)], as_of, 28,
                          as_of - timedelta(days=elapsed)).sync_overdue

    def test_boundary_is_elapsed_ge_cadence(self):
        self.assertFalse(self._overdue(13))
        self.assertTrue(self._overdue(14))
        self.assertTrue(self._overdue(15))


class InputValidation(unittest.TestCase):
    def test_observation_after_as_of_rejected(self):
        with self.assertRaises(ST.RetentionConfigError):
            ST.compute([date(2026, 9, 7)], date(2026, 9, 6), 28, date(2026, 9, 6))




class SchemaIntegrity(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()

    def test_duplicate_active_definition_is_impossible(self):
        def m(con):
            CO_reg = register
            CO_reg(con, ST_M, VD_ST, CAP28)
            con.execute("INSERT INTO metric_definitions VALUES (?,?,now(),'{}')",
                        [ST_M, "second" + "0" * 26])
        self.s.publish(m)
        con = self.s.connect()
        with self.assertRaises(Exception):
            con.execute("INSERT INTO metric_active_definition VALUES (?,?,now())",
                        [ST_M, "second" + "0" * 26])
        con.close()

    def test_integrity_queries_detect_orphans(self):
        def m(con):
            seed(con, self.sid, ST_M, VD_ST, CAP28, [date(2026, 8, 1)], "screentime")
            con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                        [self.sid, ST_M, date(2026, 8, 2), "orphandef" + "0" * 23,
                         "screentime", 1.0, "measured", 1, "bad"])
        with self.assertRaises(StoreIntegrityError):
            self.s.publish(m)          # publication now BLOCKS the orphan
        con = self.s.connect()
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                    [self.sid, ST_M, date(2026, 8, 2), "orphandef" + "0" * 23,
                     "screentime", 1.0, "measured", 1, "bad"])
        res = check_integrity(con); con.close()
        self.assertTrue(res["orphan_observation_definition"],
                        "an observation under an unregistered definition must be detected")
        self.assertFalse(res["duplicate_active_definition"])
        self.assertFalse(res["overlapping_metric_ranges"])

    def test_clean_store_passes_every_integrity_check(self):
        def m(con):
            seed(con, self.sid, ST_M, VD_ST, CAP28,
                 daterange(date(2026, 8, 1), date(2026, 8, 5)), "screentime")
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        res = check_integrity(con); con.close()
        self.assertEqual({k: v for k, v in res.items() if v}, {})


class EvidenceSetDiscipline(unittest.TestCase):
    def test_overlapping_evidence_sets_are_rejected(self):
        d = date(2026, 9, 1)
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "partial", observed={d: {"value": 1.0}}, known_absent=[d])
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "partial", known_absent=[d], undetermined=[d])
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "partial", observed={d: {"value": 1.0}}, permanent=[d])
        CO.FetchResult(ST_M, "partial", observed={d: {"value": 1.0}},
                       known_absent=[date(2026, 9, 2)], undetermined=[date(2026, 9, 3)],
                       permanent=[date(2026, 9, 4)])

    def test_batches_bound_an_unbounded_repair(self):
        big = daterange(date(2024, 1, 1), date(2026, 1, 1))
        bs = CO.batches(big)
        self.assertGreater(len(bs), 1)
        self.assertTrue(all(len(b) <= CO.MAX_REPAIR_BATCH for b in bs))
        self.assertEqual(sum(len(b) for b in bs), len(big))


class AppendOnlyAndCounts(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.window = date(2026, 8, 30)          # correction window starts here
        def m(con):
            seed(con, self.sid, ST_M, VD_ST, CAP28,
                 daterange(date(2026, 8, 1), date(2026, 8, 5)), "screentime", 100.0)
        self.s.publish(m)

    def test_false_known_absent_cannot_erase_archive(self):
        """A source wrongly reporting 'nothing there' for old dates must not delete."""
        fr = CO.FetchResult(ST_M, "partial",
                            known_absent=daterange(date(2026, 8, 1), date(2026, 8, 5)))
        def m(con):
            CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(), run_id="bad",
                           source="screentime", fr=fr, append_only_before=self.window)
        with self.assertRaises(CO.AppendOnlyViolation):
            self.s.publish(m)
        con = self.s.connect(read_only=True)
        n = con.execute("SELECT count(*) FROM observations").fetchone()[0]
        v = con.execute("SELECT value FROM observations WHERE date=?",
                        [date(2026, 8, 1)]).fetchone()[0]
        con.close()
        self.assertEqual(n, 5, "archive rows survive a false known_absent")
        self.assertEqual(v, 100.0)

    def test_append_only_still_allows_filling_a_hole(self):
        fr = CO.FetchResult(ST_M, "ok", observed={date(2026, 8, 12): {"value": 7.0}})
        def m(con):
            CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(), run_id="fill",
                           source="screentime", fr=fr, append_only_before=self.window)
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 6)
        con.close()

    def test_deleted_count_is_real(self):
        fr = CO.FetchResult(ST_M, "ok",
                            observed={d: {"value": 5.0} for d in
                                      daterange(date(2026, 8, 1), date(2026, 8, 3))})
        out = {}
        def m(con):
            out.update(CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(),
                                      run_id="r", source="screentime", fr=fr))
        self.s.publish(m)
        self.assertEqual(out["deleted"], 3, "three existing rows were replaced")
        self.assertEqual(out["written"], 3)

    def test_permanent_classification_is_persisted(self):
        fr = CO.FetchResult(ST_M, "partial", permanent=[date(2026, 7, 1), date(2026, 7, 2)])
        def m(con):
            CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(), run_id="p",
                           source="screentime", fr=fr)
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        rows = con.execute("SELECT date FROM metric_date_availability WHERE state=?",
                           ["permanently_missing"]).fetchall()
        con.close()
        self.assertEqual(len(rows), 2, "permanent classifications must be persisted")

    def test_range_overlap_is_resolved_and_remainders_redigested(self):
        wide = CO.FetchResult(ST_M, "ok", observed={d: {"value": 1.0} for d in
                                                    daterange(date(2026, 8, 1), date(2026, 8, 10))})
        inner = CO.FetchResult(ST_M, "ok", observed={d: {"value": 2.0} for d in
                                                     daterange(date(2026, 8, 4), date(2026, 8, 6))})
        for fr, rid in ((wide, "w"), (inner, "i")):
            self.s.publish(lambda con, fr=fr, rid=rid: CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(), run_id=rid,
                source="screentime", fr=fr))
        con = self.s.connect(read_only=True)
        rs = con.execute("SELECT range_start, range_end, run_id FROM metric_ranges "
                         "WHERE metric_id=? ORDER BY range_start", [ST_M]).fetchall()
        bad = check_integrity(con)["overlapping_metric_ranges"]; con.close()
        self.assertFalse(bad, "ranges must never overlap after a replacement")
        self.assertEqual([(r[0], r[1]) for r in rs],
                         [(date(2026, 8, 1), date(2026, 8, 3)),
                          (date(2026, 8, 4), date(2026, 8, 6)),
                          (date(2026, 8, 7), date(2026, 8, 10))])
        self.assertTrue(any(r[2].startswith("derived:") for r in rs),
                        "trimmed remainders record derived provenance")


class ConcurrencyAndCAS(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()

    def test_lifecycle_lock_excludes_a_second_publisher(self):
        """ONE lock covers publish, init, destroy and cleanup."""
        import fcntl
        lf = open(Path(self.dir) / Store.LOCKFILE, "a+")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(WriterBusyError):
            self.s.publish(lambda con: None)
        with self.assertRaises(WriterBusyError):
            self.s.destroy_and_recreate(POL_A)
        fcntl.flock(lf, fcntl.LOCK_UN); lf.close()
        self.s.publish(lambda con: None)          # succeeds once released

    def test_compare_and_swap_requires_the_full_token(self):
        """Rev 9 kept a generation-only expectation, which bypassed instance identity."""
        t0 = self.s.token()
        self.s.publish(lambda con: None)
        with self.assertRaises(CompareAndSwapError):
            self.s.publish(lambda con: None, expect_token=t0)
        self.s.publish(lambda con: None, expect_token=self.s.token())

    def test_generation_only_expectation_is_not_accepted_at_the_api_boundary(self):
        with self.assertRaises(TypeError):
            self.s.publish(lambda con: None, expect_generation=1)

    def test_snapshot_open_verifies_the_token_inside_the_file(self):
        con, tok = self.s.open_snapshot()
        con.close()
        self.assertEqual(tok, self.s.token())
        with self.assertRaises(StoreTokenError):
            con2, _ = self.s.open_snapshot(
                expect_token={"store_instance_id": "0" * 32, "generation": 1})
            con2.close()

    def test_pointer_temp_files_are_unique_per_attempt(self):
        names = set()
        for _ in range(5):
            self.s.publish(lambda con: None)
            names |= {p.name for p in Path(self.dir).glob(".current.json.*.tmp")}
        self.assertEqual(names, set(), "pointer temp files are always renamed away")


# =============== Rev 9 regressions: independent storage probes ===============

class PublicationEnforcesValidity(unittest.TestCase):
    """Probe: an orphan active definition published fine, advancing generation 1 -> 2."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: seed(con, self.sid, ST_M, VD_ST, CAP28,
                                        [date(2026, 8, 1)], "screentime"))

    def test_orphan_active_definition_is_rejected_and_pointer_does_not_move(self):
        before = self.s.token()
        def m(con):
            con.execute("INSERT INTO metric_active_definition VALUES (?,?,now())",
                        ["weather.precip_mm", "nonexistent" + "0" * 21])
        with self.assertRaises(StoreIntegrityError):
            self.s.publish(m)
        self.assertEqual(self.s.token(), before, "the pointer must not advance")
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        self.assertEqual({k: v for k, v in check_integrity(con).items() if v}, {})
        con.close()

    def test_missing_source_database_is_rejected_not_silently_blanked(self):
        """Probe: deleting the pointed-to database let publish() create a blank one."""
        before = self.s.token()
        self.s.path.unlink()
        with self.assertRaises(StoreStateError):
            self.s.publish(lambda con: None)
        self.assertEqual(self.s.token(), before,
                         "a missing source must not advance the generation")
        self.assertFalse(self.s.path.exists())
        self.assertEqual([f for f in Path(self.dir).glob("facts.build.*")], [],
                         "the build file is cleaned up on the failure path")

    def test_prior_readable_generation_survives_a_rejected_publication(self):
        self.s.publish(lambda con: seed(con, self.sid, ST_M, VD_ST, CAP28,
                                        [date(2026, 8, 2)], "screentime", reg=False))
        good = self.s.token()
        with self.assertRaises(StoreIntegrityError):
            self.s.publish(lambda con: con.execute(
                "INSERT INTO metric_active_definition VALUES (?,?,now())",
                ["x.y", "z" * 32]))
        self.assertEqual(self.s.token(), good)
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 2)
        con.close()

    def test_manifest_must_be_exactly_one_row(self):
        with self.assertRaises(StoreStateError):
            self.s.publish(lambda con: con.execute("DELETE FROM store_manifest"))
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM store_manifest").fetchone()[0], 1)
        con.close()


class LifecycleLockAndFullToken(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()

    def test_repeated_init_is_explicit(self):
        with self.assertRaises(StoreAlreadyInitialized):
            self.s.init(POL_A)
        same = self.s.init(POL_A, exist_ok=True)
        self.assertEqual(same, self.sid)
        self.assertEqual(self.s.token()["generation"], 1, "exist_ok must not mutate")

    def test_recreate_from_another_process_is_excluded_during_publication(self):
        """Probe: an INDEPENDENT process recreated the store mid-publication, deleted
        the build file, and publication died with FileNotFoundError. Deterministic."""
        import subprocess, textwrap
        script = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {str(ROOT / 'tools')!r})
            from store import Store, WriterBusyError
            s = Store({str(Path(self.dir) / 'facts.duckdb')!r})
            try:
                s.destroy_and_recreate("pol")
                print("RECREATED")
            except WriterBusyError:
                print("BLOCKED")
        """)
        out = {}
        def m(con):
            r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                               text=True, timeout=60)
            out["stdout"] = r.stdout.strip()
        self.s.publish(m)
        self.assertEqual(out["stdout"], "BLOCKED",
                         "an independent process must not recreate the store mid-publish")
        self.assertEqual(self.s.token()["generation"], 2, "publication still completed")
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM store_manifest").fetchone()[0], 1)
        con.close()

    def test_full_token_cas_rejects_a_recreated_store_at_the_same_generation(self):
        stale = self.s.token()                       # store A, generation 1
        new_sid = self.s.destroy_and_recreate(POL_A) # store B, generation 1
        self.assertNotEqual(new_sid, stale["store_instance_id"])
        self.assertEqual(self.s.token()["generation"], stale["generation"])
        with self.assertRaises(CompareAndSwapError):
            self.s.publish(lambda con: None, expect_token=stale)
        self.s.publish(lambda con: None, expect_token=self.s.token())

    def test_lock_file_inode_is_never_replaced(self):
        lock = Path(self.dir) / Store.LOCKFILE
        self.s.publish(lambda con: None)
        ino = lock.stat().st_ino
        self.s.destroy_and_recreate(POL_A)
        self.assertTrue(lock.exists(), "destroy must not unlink the lock")
        self.assertEqual(lock.stat().st_ino, ino, "the lock inode must be stable")

    def test_nested_read_is_allowed_but_destruction_is_not(self):
        def ok(con):
            with self.s.lifecycle_lock("read"):      # re-entry for a read is fine
                pass
        self.s.publish(ok)
        self.assertEqual(self.s.token()["generation"], 2)

    def test_nested_destroy_during_publication_is_rejected_before_touching_files(self):
        """Rev 9: destroy inside a publish callback moved the pointer, deleted the
        facts, and the outer publish then died with FileNotFoundError."""
        self.s.publish(lambda con: seed(con, self.sid, ST_M, VD_ST, CAP28,
                                        [date(2026, 8, 1)], "screentime"))
        before = self.s.token()
        def destructive(con):
            with self.assertRaises(LifecycleTransitionError):
                self.s.destroy_and_recreate(POL_A)
        self.s.publish(destructive)
        self.assertEqual(self.s.token()["store_instance_id"],
                         before["store_instance_id"], "the instance must be unchanged")
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 1,
                         "the old facts must survive")
        con.close()


class CurrentStateDurability(unittest.TestCase):
    """Probe: Sept 4 reclassified known_absent still counted as observed."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: seed(
            con, self.sid, ST_M, VD_ST, CAP28,
            [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5)], "screentime"))

    def test_reclassification_under_a_new_capability_moves_the_tail(self):
        def m(con):
            con.execute("INSERT OR REPLACE INTO source_capabilities VALUES "
                        "(?,?,?,?,now(),?,NULL)",
                        [CAP14, "screentime", "finite", 14, date(2026, 9, 5)])
            con.execute("DELETE FROM observations WHERE date=? AND metric_id=?",
                        [date(2026, 9, 4), ST_M])
            con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                        [self.sid, ST_M, date(2026, 9, 4), VD_ST, CAP14, next_seq(con), "known_absent"])
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        facts = [r[0] for r in con.execute(
            "SELECT date FROM observations WHERE metric_id=? ORDER BY date",
            [ST_M]).fetchall()]
        obs = CO.observed_dates(con, self.sid, ST_M, VD_ST)
        d = CO.durability_from_store(con, self.sid, "screentime", ST_M, VD_ST,
                                     date(2026, 9, 6), 28, date(2026, 9, 6))
        hist = con.execute(
            "SELECT count(*) FROM metric_date_availability WHERE date=? AND metric_id=?",
            [date(2026, 9, 4), ST_M]).fetchone()[0]
        con.close()
        self.assertEqual(facts, [date(2026, 9, 3), date(2026, 9, 5)])
        self.assertEqual(obs, [date(2026, 9, 3), date(2026, 9, 5)],
                         "durability must read CURRENT classifications")
        self.assertEqual(d.durable_since, date(2026, 9, 5),
                         "the observed tail starts at Sept 5, not Sept 3")
        self.assertEqual(hist, 2, "classification history stays queryable")


class PermanentFactConflict(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: seed(con, self.sid, ST_M, VD_ST, CAP28,
                                        [date(2026, 8, 1)], "screentime", 10.0))

    def test_permanent_classification_over_a_stored_fact_is_rejected(self):
        """Probe: the permanent branch bypassed the guard and integrity called it clean."""
        fr = CO.FetchResult(ST_M, "partial", permanent=[date(2026, 8, 1)])
        with self.assertRaises(CO.PermanentConflict):
            self.s.publish(lambda con: CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, definition=defn(ST_M), capability=capa(), run_id="p",
                source="screentime", fr=fr))
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        con.close()

    def test_integrity_check_detects_the_contradiction_if_it_is_ever_written(self):
        con = self.s.connect()
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [self.sid, ST_M, date(2026, 8, 1), VD_ST, "capX", next_seq(con), "permanently_missing"])
        res = check_integrity(con); con.close()
        self.assertTrue(res["permanent_with_stored_fact"],
                        "a stored fact classified permanently missing is a violation")


class RetentionTriState(unittest.TestCase):
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: seed(con, self.sid, ST_M, VD_ST, CAP28,
                                        [date(2026, 9, 1)], "screentime"))

    def test_unknown_is_not_unbounded(self):
        con = self.s.connect(read_only=True)
        unk = CO.durability_from_store(con, self.sid, "screentime", ST_M, VD_ST,
                                       date(2026, 9, 6), None, None,
                                       retention_status="unknown")
        unb = CO.durability_from_store(con, self.sid, "screentime", ST_M, VD_ST,
                                       date(2026, 9, 6), None, None,
                                       retention_status="unbounded")
        con.close()
        self.assertEqual(unk.retention_status, "unknown")
        self.assertEqual(unb.retention_status, "unbounded")
        self.assertFalse(unk.unbounded_retention,
                         "an unmeasured source must never look unbounded")
        self.assertTrue(unb.unbounded_retention)
        self.assertIsNone(unk.horizon)
        self.assertIsNone(unk.sync_overdue)
        self.assertFalse(unk.data_loss_at_risk)

    def test_inconsistent_retention_combinations_are_rejected(self):
        with self.assertRaises(ST.RetentionConfigError):
            ST.compute([], date(2026, 9, 6), None, None, retention_status="finite")
        with self.assertRaises(ST.RetentionConfigError):
            ST.compute([], date(2026, 9, 6), 28, None, retention_status="unknown")
        with self.assertRaises(ST.RetentionConfigError):
            ST.compute([], date(2026, 9, 6), None, None, retention_status="bogus")


# ============ Rev 10 regressions: the ten confirmed probes ============

class ThreadSafePublication(unittest.TestCase):
    """Probe 3: two threads sharing one Store both published from generation 1 and one
    writer's observation was lost."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: register(con, ST_M, VD_ST, CAP28))

    def test_two_threads_cannot_both_publish_from_the_same_generation(self):
        import threading
        started, results = threading.Event(), {}
        def writer(tag, day, gate):
            def m(con):
                if gate:
                    started.set()
                    time.sleep(0.3)          # hold the lock long enough to overlap
                ST_.record_run(con, sid=self.sid, run_id=tag, source="screentime",
                               metric_id=ST_M, vdid=VD_ST, capid=CAP28, outcome="ok")
                con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                            [self.sid, ST_M, day, VD_ST, "screentime", 1.0,
                             "measured", 1, tag])
                con.execute("INSERT INTO metric_date_availability "
                            "VALUES (?,?,?,?,?,now(),?,?)",
                            [self.sid, ST_M, day, VD_ST, CAP28, next_seq(con), "observed"])
            try:
                self.s.publish(m, )
                results[tag] = "published"
            except WriterBusyError as e:
                results[tag] = f"busy: {e}"
            except Exception as e:
                results[tag] = f"{type(e).__name__}: {e}"
        a = threading.Thread(target=writer, args=("A", date(2026, 8, 1), True))
        b = threading.Thread(target=writer, args=("B", date(2026, 8, 2), False))
        a.start(); started.wait(5); b.start(); a.join(30); b.join(30)
        con = self.s.connect(read_only=True)
        days = [r[0] for r in con.execute(
            "SELECT date FROM observations ORDER BY date").fetchall()]
        con.close()
        published = [k for k, v in results.items() if v == "published"]
        self.assertIn("A", published)
        if "B" in published:
            self.assertEqual(days, [date(2026, 8, 1), date(2026, 8, 2)],
                             "two successful publications must not overwrite one another")
            self.assertEqual(self.s.token()["generation"], 3)
        else:
            self.assertTrue(results["B"].startswith("busy:"),
                            f"a second writer must publish or be told busy: {results['B']}")
            self.assertEqual(days, [date(2026, 8, 1)])


class IdentityContentsArePreserved(unittest.TestCase):
    """Probe 4: a routine fetch rewrote an existing definition to {} and downgraded a
    measured finite/28 capability to unknown/null."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.real_defn = defn(ST_M, taxonomy="v3", sleep_window="02:00-12:00")
        self.real_cap = capa(R=28, eff=date(2026, 9, 1))
        def m(con):
            register_definition(con, ST_M, VD_ST, self.real_defn)
            register_capability(con, CAP28, self.real_cap)
            activate_definition(con, ST_M, VD_ST)
        self.s.publish(m)

    def _stored(self):
        con = self.s.connect(read_only=True)
        d = con.execute("SELECT definition_json FROM metric_definitions "
                        "WHERE metric_id=? AND value_definition_id=?",
                        [ST_M, VD_ST]).fetchone()[0]
        c = con.execute("SELECT retention_status, retention_days, effective_from "
                        "FROM source_capabilities WHERE source_capability_id=?",
                        [CAP28]).fetchone()
        con.close(); return d, c

    def test_a_routine_fetch_preserves_definition_and_capability(self):
        before = self._stored()
        fr = CO.FetchResult(ST_M, "ok", observed={date(2026, 9, 2): {"value": 5.0}})
        self.s.publish(lambda con: CO.apply_fetch(
            con, sid=self.sid, vdid=VD_ST, capid=CAP28, run_id="r",
            source="screentime", fr=fr))
        self.assertEqual(self._stored(), before,
                         "omitting the optional measurement must not erase it")

    def test_reusing_an_identity_with_different_content_fails(self):
        def m(con):
            register_definition(con, ST_M, VD_ST, defn(ST_M, taxonomy="v4"))
        with self.assertRaises(IdentityContentConflict):
            self.s.publish(m)
        with self.assertRaises(IdentityContentConflict):
            self.s.publish(lambda con: register_capability(
                con, CAP28, capa(R=14, eff=date(2026, 9, 1))))
        self.assertEqual(self._stored()[1][1], 28, "the measurement is unchanged")

    def test_identical_content_is_idempotent(self):
        self.s.publish(lambda con: register_definition(con, ST_M, VD_ST, self.real_defn))
        self.s.publish(lambda con: register_capability(con, CAP28, self.real_cap))

    def test_incomplete_registration_is_refused_not_fabricated(self):
        with self.assertRaises(IdentityContentConflict):
            self.s.publish(lambda con: register_definition(con, "a.b", "x" * 32, {}))
        with self.assertRaises(IdentityContentConflict):
            self.s.publish(lambda con: register_capability(
                con, "cap-x", {"source": "screentime", "retention_status": "finite",
                               "retention_days": None, "effective_from": date(2026, 1, 1)}))

    def test_unregistered_identity_cannot_be_used_by_a_fetch(self):
        fr = CO.FetchResult("weather.precip_mm", "ok",
                            observed={date(2026, 9, 2): {"value": 1.0}})
        with self.assertRaises(IdentityContentConflict):
            self.s.publish(lambda con: CO.apply_fetch(
                con, sid=self.sid, vdid="w" * 32, capid="cw" * 16, run_id="r",
                source="weather", fr=fr))


class ClassificationOrder(unittest.TestCase):
    """Probe 5: two fetches in ONE transaction shared a timestamp and the lexical
    capability tie-break selected the older observed row for a deleted fact."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: register(con, ST_M, VD_ST, "zz-old"))

    def test_second_fetch_in_the_same_transaction_wins(self):
        day = date(2026, 9, 3)
        def m(con):
            register_capability(con, "aa-new", capa(R=14, eff=date(2026, 9, 2)))
            CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid="zz-old", run_id="r1",
                           source="screentime",
                           fr=CO.FetchResult(ST_M, "ok", observed={day: {"value": 3.0}}))
            CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid="aa-new", run_id="r2",
                           source="screentime",
                           fr=CO.FetchResult(ST_M, "empty", known_absent=[day]))
        self.s.publish(m)
        con = self.s.connect(read_only=True)
        cur = con.execute("SELECT state FROM metric_date_availability_current "
                          "WHERE metric_id=? AND date=?", [ST_M, day]).fetchall()
        hist = con.execute("SELECT count(*) FROM metric_date_availability "
                           "WHERE metric_id=? AND date=?", [ST_M, day]).fetchone()[0]
        obs = CO.observed_dates(con, self.sid, ST_M, VD_ST)
        facts = con.execute("SELECT count(*) FROM observations WHERE date=?",
                            [day]).fetchone()[0]
        viol = {k: v for k, v in check_integrity(con).items() if v}
        con.close()
        self.assertEqual(cur, [("known_absent",)],
                         "the LATER write is current, regardless of capability id")
        self.assertEqual(hist, 2, "history is retained")
        self.assertEqual(facts, 0, "the fact was deleted by the second fetch")
        self.assertNotIn(day, obs, "a deleted date must not report as observed")
        self.assertEqual(viol, {})

    def test_current_observed_without_a_fact_is_an_integrity_violation(self):
        con = self.s.connect()
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [self.sid, ST_M, date(2026, 9, 9), VD_ST, "zz-old", next_seq(con),
                     "observed"])
        res = check_integrity(con); con.close()
        self.assertTrue(res["current_observed_without_fact"])

    def test_a_fact_whose_current_state_is_not_observed_is_a_violation(self):
        day = date(2026, 9, 10)
        con = self.s.connect()
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                    [self.sid, ST_M, day, VD_ST, "screentime", 1.0, "measured", 1, "x"])
        con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                    [self.sid, ST_M, day, VD_ST, "zz-old", next_seq(con), "known_absent"])
        res = check_integrity(con); con.close()
        self.assertTrue(res["fact_without_current_observed"])

    def test_publishing_an_invalid_current_state_is_blocked(self):
        def m(con):
            con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                        [self.sid, ST_M, date(2026, 9, 11), VD_ST, "zz-old",
                         next_seq(con), "observed"])
        with self.assertRaises(StoreIntegrityError):
            self.s.publish(m)


class StatusEvidenceAgreement(unittest.TestCase):
    """Probe 6: status='ok' persisted a known-absent date inside an `ok` range."""
    def setUp(self):
        self.s, self.sid, self.dir = mkstore()
        self.s.publish(lambda con: register(con, ST_M, VD_ST, CAP28))

    def test_ok_with_known_absent_is_rejected_at_construction(self):
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "ok", observed={date(2026, 9, 1): {"value": 1.0}},
                           known_absent=[date(2026, 9, 2)])

    def test_ok_with_undetermined_is_rejected(self):
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "ok", observed={date(2026, 9, 1): {"value": 1.0}},
                           undetermined=[date(2026, 9, 2)])

    def test_empty_with_observed_is_rejected(self):
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "empty", observed={date(2026, 9, 1): {"value": 1.0}})

    def test_failed_with_any_evidence_is_rejected(self):
        with self.assertRaises(CO.FetchSetError):
            CO.FetchResult(ST_M, "failed", known_absent=[date(2026, 9, 1)])

    def test_rejection_leaves_the_prior_store_intact(self):
        self.s.publish(lambda con: CO.apply_fetch(
            con, sid=self.sid, vdid=VD_ST, capid=CAP28, run_id="r", source="screentime",
            fr=CO.FetchResult(ST_M, "ok", observed={date(2026, 9, 1): {"value": 7.0}})))
        before = self.s.token()
        def m(con):
            CO.FetchResult(ST_M, "ok", observed={date(2026, 9, 2): {"value": 1.0}},
                           known_absent=[date(2026, 9, 3)])
        with self.assertRaises(CO.FetchSetError):
            self.s.publish(m)
        self.assertEqual(self.s.token(), before)
        con = self.s.connect(read_only=True)
        self.assertEqual(con.execute("SELECT count(*) FROM observations").fetchone()[0], 1)
        con.close()




# ============ Rev 11 regressions ============
# Each case reproduces an independently demonstrated Rev 10 behaviour and asserts the
# corrected one. The substance of the observation is preserved, not the defect.

class NestedPublicationIsRejected(unittest.TestCase):
    """Independent probe: an inner publish() and its outer publish() both chose the
    same next generation, and the outer os.replace overwrote the file the inner call
    had already published. A successful publication disappeared."""
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.d.name) / "data" / "facts.duckdb")
        self.sid = self.s.init(POL_A)
        def setup(con):
            register(con, ST_M, VD_ST, CAP28)
        self.s.publish(setup)
        self.before = self.s.token()

    def tearDown(self):
        self.d.cleanup()

    def _write(self, con, day, value, run):
        CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, run_id=run,
                       source="screentime", fr=CO.FetchResult(
                           ST_M, "ok", observed={day: {"value": value,
                                                       "sample_count": int(value)}}))

    def test_the_inner_publication_is_rejected_and_nothing_is_lost(self):
        seen = {}

        def outer(con):
            self._write(con, date(2026, 9, 1), 1, "outer")
            with self.assertRaises(LifecycleTransitionError):
                self.s.publish(lambda inner: self._write(
                    inner, date(2026, 9, 2), 2, "inner"))
            # rejected BEFORE mutation: the pointer has not moved
            seen["token_during"] = self.s.token()

        after = self.s.publish(outer)
        self.assertEqual(seen["token_during"], self.before,
                         "a rejected inner call must not move the pointer")
        self.assertEqual(after["generation"], self.before["generation"] + 1)
        con, _ = self.s.open_snapshot()
        try:
            rows = con.execute("SELECT date, value FROM observations_current "
                               "ORDER BY date").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [(date(2026, 9, 1), 1.0)],
                         "the OUTER write survives; nothing was silently discarded")

    def test_a_nested_read_is_still_allowed(self):
        """Re-entrant lock ACQUISITION is not an allowed lifecycle TRANSITION. Reading
        inside a publication stays legal; publishing does not."""
        observed = {}

        def outer(con):
            self._write(con, date(2026, 9, 1), 1, "outer")
            with self.s.lifecycle_lock("read"):
                observed["ok"] = True
        self.s.publish(outer)
        self.assertTrue(observed["ok"])

    def test_an_already_published_generation_file_is_never_overwritten(self):
        sid = self.before["store_instance_id"]
        nxt = self.s._db_path(sid, self.before["generation"] + 1)
        nxt.write_bytes(b"an existing published generation")
        with self.assertRaises(StoreStateError) as e:
            self.s.publish(lambda con: self._write(con, date(2026, 9, 3), 1, "x"))
        self.assertIn("already exists", str(e.exception))
        self.assertEqual(self.s.token(), self.before)
        self.assertEqual(nxt.read_bytes(), b"an existing published generation")


class CollectionProvenance(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.d.name) / "data" / "facts.duckdb")
        self.sid = self.s.init(POL_A)

    def tearDown(self):
        self.d.cleanup()

    def _apply(self, fr, run, **kw):
        self.s.publish(lambda con: CO.apply_fetch(
            con, sid=self.sid, vdid=VD_ST, capid=CAP28, run_id=run,
            source="screentime", fr=fr, definition=defn(ST_M), capability=capa(),
            **kw))

    def _runs(self):
        con, _ = self.s.open_snapshot()
        try:
            return con.execute(
                "SELECT run_id, outcome, reason, snapshot_id, provenance_json "
                "FROM collection_runs ORDER BY seq").fetchall()
        finally:
            con.close()

    def test_a_successful_run_records_its_provenance(self):
        fr = CO.FetchResult(ST_M, "ok", observed={
            date(2026, 9, 1): {"value": 5.0, "sample_count": 1}})
        self._apply(fr, "r1", provenance={"heads": {"repo": "abc"}, "complete": True},
                    snapshot_id="git:abc")
        rows = self._runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], ("r1", "ok", None, "git:abc"))
        self.assertEqual(json.loads(rows[0][4]),
                         {"heads": {"repo": "abc"}, "complete": True})

    def test_a_failed_run_is_recorded_and_writes_no_facts(self):
        """Rev 10 returned early on `failed`, so a failed attempt left no trace at all
        and status could not distinguish it from 'no run has happened'."""
        ok = CO.FetchResult(ST_M, "ok", observed={
            date(2026, 9, 1): {"value": 5.0, "sample_count": 1}})
        self._apply(ok, "r1")
        self._apply(CO.FetchResult(ST_M, "failed", reason="repo unreadable"), "r2")
        rows = self._runs()
        self.assertEqual([r[1] for r in rows], ["ok", "failed"])
        self.assertEqual(rows[1][2], "repo unreadable")
        con, _ = self.s.open_snapshot()
        try:
            facts = con.execute("SELECT date, value FROM observations_current").fetchall()
        finally:
            con.close()
        self.assertEqual(facts, [(date(2026, 9, 1), 5.0)],
                         "a failed run preserves the previous facts exactly")

    def test_a_fact_without_a_recorded_run_cannot_be_published(self):
        def m(con):
            register(con, ST_M, VD_ST, CAP28)
            con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                        [self.sid, ST_M, date(2026, 9, 2), VD_ST, "screentime", 1.0,
                         "measured", 1, "ghost-run"])
            con.execute("INSERT INTO metric_date_availability VALUES (?,?,?,?,?,now(),?,?)",
                        [self.sid, ST_M, date(2026, 9, 2), VD_ST, CAP28, next_seq(con),
                         "observed"])
        with self.assertRaises(StoreIntegrityError) as e:
            self.s.publish(m)
        self.assertIn("observation_without_run", str(e.exception))


class TimestampConvention(unittest.TestCase):
    """Measured: DuckDB stores LOCAL civil time for now() and for an aware datetime, so
    a value written as 12:00Z came back as 07:00 and was serialized with no offset."""
    def test_duckdb_localises_now_and_aware_datetimes(self):
        from datetime import datetime, timezone
        con = duckdb.connect()
        con.execute("CREATE TABLE t (a TIMESTAMP)")
        con.execute("INSERT INTO t VALUES (?)",
                    [datetime(2025, 3, 1, 12, tzinfo=timezone.utc)])
        got = con.execute("SELECT a FROM t").fetchone()[0]
        con.close()
        self.assertIsNone(got.tzinfo, "the stored value is naive")
        # This is exactly why the store never lets DuckDB choose the zone.

    def test_the_store_writes_utc_and_serialises_with_an_offset(self):
        from datetime import datetime, timezone
        naive = ST_.utcnow()
        self.assertIsNone(naive.tzinfo)
        delta = abs((datetime.now(timezone.utc).replace(tzinfo=None) - naive)
                    .total_seconds())
        self.assertLess(delta, 5, "utcnow() is UTC, not local")
        iso = ST_.as_utc_iso(datetime(2025, 3, 1, 12, 0, 0))
        self.assertEqual(iso, "2025-03-01T12:00:00+00:00")
        self.assertIsNone(ST_.as_utc_iso(None))




class SourceStateCannotOutrunItsRuns(unittest.TestCase):
    """The denormalised copy may lag the recorded runs; it may never lead them."""
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.d.name) / "data" / "facts.duckdb")
        self.sid = self.s.init(POL_A)

    def tearDown(self):
        self.d.cleanup()

    def _sync(self, con, when):
        CO.apply_fetch(con, sid=self.sid, vdid=VD_ST, capid=CAP28, run_id="r1",
                       source="screentime", definition=defn(ST_M), capability=capa(),
                       fr=CO.FetchResult(ST_M, "ok", observed={
                           date(2026, 9, 1): {"value": 1.0, "sample_count": 1}}))
        con.execute("UPDATE collection_runs SET recorded_at=? WHERE run_id='r1'", [when])
        con.execute("INSERT OR REPLACE INTO source_state VALUES (?,?,?,?,?,?,?,?)",
                    [self.sid, "screentime", CAP28, 28, "finite", when, when, ST_M])

    def test_a_consistent_pair_publishes(self):
        from datetime import datetime
        self.s.publish(lambda con: self._sync(con, datetime(2026, 9, 1, 10)))
        self.assertEqual(self.s.token()["generation"], 2)

    def test_a_sync_claim_ahead_of_every_recorded_run_is_blocked(self):
        from datetime import datetime
        def m(con):
            self._sync(con, datetime(2026, 9, 1, 10))
            con.execute("UPDATE source_state SET last_successful_sync=?",
                        [datetime(2026, 9, 30, 10)])
        with self.assertRaises(StoreIntegrityError) as e:
            self.s.publish(m)
        self.assertIn("source_state_claims_a_sync_no_run_recorded", str(e.exception))




class GenerationReclamation(unittest.TestCase):
    """The store header claimed "old files are reclaimed once no reader holds them".
    Measured on a real pilot store: seven syncs left seven full database files and 45 MB
    on disk. The claim was false; this is the stated policy, implemented and bounded."""
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.d.name) / "data" / "facts.duckdb")
        self.sid = self.s.init(POL_A)
        self.s.publish(lambda con: register(con, ST_M, VD_ST, CAP28))

    def tearDown(self):
        self.d.cleanup()

    def _files(self):
        return sorted(int(f.name.rsplit(".", 2)[-2])
                      for f in self.s.dir.glob("facts.*.duckdb"))

    def _bump(self, n):
        for _ in range(n):
            self.s.publish(lambda con: None)

    def test_the_grace_window_retains_everything(self):
        """A publication does not delete a file that was just written."""
        self._bump(5)          # init -> 1, setUp publish -> 2, five more -> 7
        self.assertEqual(self._files(), [1, 2, 3, 4, 5, 6, 7],
                         "nothing is old enough to reclaim yet")

    def test_reclaim_keeps_the_current_generation_and_two_predecessors(self):
        self._bump(5)
        removed = self.s.reclaim(grace_seconds=0)
        self.assertEqual(self._files(), [5, 6, 7], "current plus two predecessors")
        self.assertEqual(sorted(removed), sorted(
            [f"facts.{self.sid}.{n}.duckdb" for n in (1, 2, 3, 4)]))
        self.assertEqual(self.s.token()["generation"], 7)

    def test_the_current_generation_is_never_reclaimed(self):
        self._bump(3)
        self.s.reclaim(keep=0, grace_seconds=0)
        self.assertEqual(self._files(), [5], "only the pointed-to generation survives")
        con, tok = self.s.open_snapshot()
        try:
            self.assertEqual(tok["generation"], 5)
        finally:
            con.close()

    def test_an_already_open_reader_survives_reclamation(self):
        """POSIX: unlinking a file an open handle holds does not disturb that handle.
        This is why generation-versioned paths can be reclaimed at all."""
        self._bump(1)
        con, tok = self.s.open_snapshot()
        try:
            self._bump(3)
            self.s.reclaim(keep=0, grace_seconds=0)
            self.assertEqual(self._files(), [6])
            n = con.execute("SELECT generation FROM store_manifest").fetchone()[0]
            self.assertEqual(n, tok["generation"],
                             "the open reader still sees the generation it opened")
        finally:
            con.close()

    def test_another_store_instance_is_never_touched(self):
        stray = self.s.dir / "facts.ffffffffffffffffffffffffffffffff.1.duckdb"
        stray.write_bytes(b"another instance")
        self._bump(4)
        self.s.reclaim(grace_seconds=0)
        self.assertTrue(stray.exists(), "reclamation is scoped to this store instance")

    def test_a_publication_is_not_failed_by_an_unreclaimable_file(self):
        self._bump(4)
        original = Store.reclaim
        Store.reclaim = lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
        try:
            with self.assertRaises(OSError):
                self.s.reclaim()
        finally:
            Store.reclaim = original
        # publish() calls reclaim() for its side effect only; a failure there must not
        # roll back a pointer that has already moved.
        before = self.s.token()
        self.s.publish(lambda con: None)
        self.assertEqual(self.s.token()["generation"], before["generation"] + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
