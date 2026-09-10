"""T6 — the git descriptive milestone through the SUPPORTED PUBLIC WORKFLOW.

    real temporary git repositories
      -> `mesh sync`   (the documented command, run as a subprocess)
      -> immutable store
      -> `mesh report` / the gateway MCP server
      -> real stdio leaf subprocesses
      -> one complete, schema-valid, typed report

Nothing here assembles a production response, injects a source-status list, or reads
the database to build a report. Tests create input data and inject failures only.

Rev 10's A1 called the calculator directly, A3 handed in a hand-built report and a
hand-written availability list, and A4 advanced the store BEFORE starting the gateway
so no retry was ever exercised. Those are replaced.
"""
import json, os, statistics, subprocess, sys, tempfile, unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from jsonschema import Draft202012Validator as V, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import contracts as C, collector as CO, git_source as GS, report as RP, store as ST
import gateway as GW, mesh_config as MC
from store import Store, digest

FC = FormatChecker()
LEAF = ROOT / "tools" / "stdio_leaf.py"
MESH = ROOT / "tools" / "mesh.py"
TZ = "America/Indiana/Indianapolis"
ME = "dev@example.test"
OTHER = "someone-else@example.test"
AS_OF = date(2026, 9, 7)          # a Monday; the default week is Aug 31 - Sep 6
MIN_DELTA = 1.0


def git(repo, *args, env=None, check=True):
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env=e, timeout=60)
    if check:
        assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r


def make_repo(path, commits, author=ME):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", author)
    git(path, "config", "user.name", "Dev")
    for i, (when, msg) in enumerate(commits):
        (path / f"f{i}.txt").write_text(msg)
        git(path, "add", ".")
        stamp = when.isoformat()
        git(path, "commit", "-q", "-m", msg,
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
                 "GIT_AUTHOR_EMAIL": author, "GIT_COMMITTER_EMAIL": author})
    return path


def at(d, hour=12):
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def mesh(*args, cwd=None, check=True):
    """Run the documented command exactly as a user would."""
    r = subprocess.run([sys.executable, str(MESH), *args], capture_output=True,
                       text=True, cwd=str(cwd or ROOT), timeout=180)
    if check:
        assert r.returncode == 0, f"mesh {args} -> {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


# --------------------------------------------------------------- independent maths
def weekly_means(values_by_date, week_start, min_days=5):
    """An INDEPENDENT weekly mean over present days, written here, not imported."""
    days = [values_by_date[week_start + timedelta(days=i)]
            for i in range(7) if (week_start + timedelta(days=i)) in values_by_date]
    if len(days) < min_days:
        return None
    return sum(days) / len(days)


def independent_change(values_by_date, week_start, min_delta):
    """Recompute every reported number from the stored daily values, independently."""
    wv = weekly_means(values_by_date, week_start)
    baseline = [v for v in (weekly_means(values_by_date, week_start - timedelta(weeks=k))
                            for k in range(1, 9)) if v is not None]
    med = statistics.median(baseline)
    mad = 1.4826 * statistics.median([abs(w - med) for w in baseline])
    delta = wv - med
    z = None if mad == 0 else delta / mad
    return {"week_value": wv, "baseline": baseline, "baseline_median": med,
            "baseline_mad": mad, "delta": delta, "delta_z": z,
            "meaningful": bool(z is not None and abs(z) >= 1.0
                               and abs(delta) >= min_delta)}


class MilestoneBase(unittest.TestCase):
    """Constant baseline: 1 commit/weekday for 9 prior weeks, 2/weekday in the target
    week. Every baseline week is identical, so MAD is zero by construction."""
    COMMITS_PER_BASELINE_DAY = 1
    COMMITS_PER_TARGET_DAY = 2

    def _commit_plan(self):
        commits = []
        for wk in range(1, 10):
            start = date(2026, 8, 31) - timedelta(weeks=wk)
            for i in range(5):
                for n in range(self.COMMITS_PER_BASELINE_DAY):
                    commits.append((at(start + timedelta(days=i), 9 + n),
                                    f"w{wk}d{i}n{n}"))
        for i in range(5):
            d = date(2026, 8, 31) + timedelta(days=i)
            for n in range(self.COMMITS_PER_TARGET_DAY):
                commits.append((at(d, 8 + n), f"t{i}n{n}"))
        return commits

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mesh-milestone-")
        base = Path(self.tmp.name)
        self.repo = make_repo(base / "repo1",
                              sorted(self._commit_plan(), key=lambda c: c[0]))
        # a merge commit and a foreign author, both of which must be excluded
        git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "side.txt").write_text("x"); git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "side",
            env={"GIT_AUTHOR_DATE": at(date(2026, 9, 1)).isoformat(),
                 "GIT_COMMITTER_DATE": at(date(2026, 9, 1)).isoformat(),
                 "GIT_AUTHOR_EMAIL": OTHER, "GIT_COMMITTER_EMAIL": OTHER})
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "side", "-m", "merge side",
            env={"GIT_AUTHOR_DATE": at(date(2026, 9, 2)).isoformat(),
                 "GIT_COMMITTER_DATE": at(date(2026, 9, 2)).isoformat()})
        self.clone = base / "clone1"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(self.clone)],
                       check=True, timeout=60)
        self.repos = [self.repo, self.clone]
        self.base = base
        self.config_path = base / "mesh.json"
        self.write_config()
        self.cfg = MC.load(self.config_path)
        self.store = Store(Path(self.cfg["store_dir"]) / "facts.duckdb")
        self.defn = GS.value_definition(author_emails=[ME], repos=self.cfg["sources"]
                                        ["git"]["repos"], analysis_timezone=TZ)
        self.vdid = digest("value_definition", self.defn)

    def write_config(self, repos=None):
        self.config_path.write_text(json.dumps({
            "store_dir": "data", "analysis_timezone": TZ,
            "max_staleness_days": 2, "min_meaningful_delta": MIN_DELTA,
            "sources": {
                "git": {"enabled": True,
                        "repos": [str(r) for r in (repos or self.repos)],
                        "author_emails": [ME]},
                "health": {"enabled": False,
                           "unavailable_reason": "no Health export adapter is built"}}},
            indent=1))

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------- workflow helpers
    def sync(self, check=True):
        return mesh("sync", "--config", str(self.config_path),
                    "--as-of", AS_OF.isoformat(), check=check)

    def report_via_cli(self):
        r = mesh("report", "--config", str(self.config_path),
                 "--as-of", AS_OF.isoformat())
        return json.loads(r.stdout)

    def report_via_gateway(self, gw=None):
        gw = gw or self.gateway()
        return GW.assemble_report(gw, as_of=AS_OF,
                                  min_meaningful_delta=MIN_DELTA,
                                  unavailable_sources=MC.unavailable_sources(self.cfg))

    def gateway(self, leaf=None):
        return GW.Gateway(self.store, leaf or LEAF, ["git"], config=self.config_path)

    def stored_values(self):
        """Daily values straight out of the store, for INDEPENDENT recomputation."""
        con, _ = self.store.open_snapshot()
        try:
            return {r[0]: float(r[1]) for r in con.execute(
                "SELECT date, value FROM observations_current WHERE metric_id=? "
                "ORDER BY date", [GS.METRIC]).fetchall()}
        finally:
            con.close()

    def assert_valid_report(self, rep):
        errs = list(V(C.output_schema("weekly_report"), format_checker=FC)
                    .iter_errors(rep))
        self.assertFalse(errs, "\n".join(e.message[:200] for e in errs[:4]))


class A_PublicWorkflow(MilestoneBase):
    def test_A1_the_documented_commands_produce_a_complete_valid_report(self):
        out = json.loads(self.sync().stdout)
        self.assertEqual(out["sources"]["git"]["status"], "ok", out)
        rep = self.report_via_cli()
        self.assert_valid_report(rep)
        self.assertEqual(rep["payload_type"], "report")
        self.assertEqual(rep["week"], {"start": "2026-08-31", "end": "2026-09-06"})
        self.assertEqual(rep["store_instance_id"], self.store.token()["store_instance_id"])
        self.assertEqual(rep["generation"], self.store.token()["generation"])
        self.assertEqual(rep["value_definition_id"], self.vdid)
        self.assertFalse(rep["recommendations_enabled"])
        self.assertEqual(rep["associations"], [])
        # the disabled second source is represented through the typed contract
        self.assertEqual(
            [s["source"] for s in rep["source_status"] if not s["available"]],
            ["health"])
        self.assertEqual([s["reason"] for s in rep["source_status"]
                          if s["source"] == "health"],
                         ["no Health export adapter is built"])

    def test_A2_every_reported_number_is_independently_recomputable(self):
        self.sync()
        rep = self.report_via_gateway()
        self.assert_valid_report(rep)
        ch = [c for c in rep["observed_changes"]
              if c["metric_id"] == GS.METRIC][0]
        self.assertEqual(ch["status"], "computed", ch)
        exp = independent_change(self.stored_values(), date(2026, 8, 31), MIN_DELTA)
        self.assertEqual(len(exp["baseline"]), 8, "eight baseline weeks")
        for field in ("week_value", "baseline_median", "baseline_mad", "delta"):
            self.assertAlmostEqual(ch[field], exp[field], places=9, msg=field)
        self.assertEqual(ch["delta_z"], exp["delta_z"])
        self.assertEqual(ch["meaningful"], exp["meaningful"])
        # this scenario is the zero-dispersion case, stated explicitly
        self.assertEqual(exp["baseline_mad"], 0.0)
        self.assertEqual(ch["undefined_reason"], "zero_dispersion")
        self.assertIsNone(ch["delta_z"])
        self.assertFalse(ch["meaningful"])

    def test_A3_policy_holds_end_to_end(self):
        """Merges, foreign authors and duplicated clones are excluded by the policy,
        and a fully-read day with no commits is a structural zero, not a gap."""
        self.sync()
        vals = self.stored_values()
        weekday_total = sum(v for d, v in vals.items()
                            if date(2026, 8, 31) <= d <= date(2026, 9, 6)
                            and d.isoweekday() <= 5)
        self.assertEqual(weekday_total, 10.0,
                         "2 commits/day x 5 weekdays; merge, other author and the "
                         "clone's duplicates all excluded")
        self.assertEqual(vals[date(2026, 9, 5)], 0.0, "a read day with no commits")
        con, _ = self.store.open_snapshot()
        try:
            sample = con.execute(
                "SELECT sample_count FROM observations WHERE metric_id=? AND date=?",
                [GS.METRIC, date(2026, 9, 5)]).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(sample, 0)

    def test_A4_the_gateway_serves_weekly_report_over_stdio(self):
        """The client-facing entry point: one tool, no store_token from the client."""
        self.sync()
        p = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "gateway_server.py"),
             "--config", str(self.config_path)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        m = {"io.modelcontextprotocol/protocolVersion": C.PROTOCOL_VERSION,
             "io.modelcontextprotocol/clientCapabilities": {}}
        try:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                      "params": {"_meta": m}}) + "\n")
            p.stdin.flush()
            listed = json.loads(p.stdout.readline())["result"]
            self.assertEqual([t["name"] for t in listed["tools"]], ["weekly_report"])
            self.assertNotIn("store_token",
                             listed["tools"][0]["inputSchema"].get("properties", {}),
                             "the client does not hold the pointer; the gateway does")
            p.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "weekly_report",
                            "arguments": {"week_of": "2026-08-31"}, "_meta": m}}) + "\n")
            p.stdin.flush()
            res = json.loads(p.stdout.readline())["result"]
        finally:
            try:
                p.stdin.close(); p.wait(timeout=10)
            except Exception:
                p.kill()
            for h in (p.stdout, p.stderr):
                try: h.close()
                except Exception: pass
        self.assertFalse(res["isError"], res["structuredContent"])
        self.assert_valid_report(res["structuredContent"])
        self.assertEqual(res["structuredContent"]["week"],
                         {"start": "2026-08-31", "end": "2026-09-06"})


class B_NonconstantBaseline(MilestoneBase):
    """A varying baseline (so MAD > 0) AND a baseline week with missing days."""
    def _commit_plan(self):
        commits = []
        for wk in range(1, 10):
            start = date(2026, 8, 31) - timedelta(weeks=wk)
            for i in range(5):
                for n in range(1 + (wk % 3)):        # 1, 2 or 3 commits per weekday
                    commits.append((at(start + timedelta(days=i), 8 + n),
                                    f"w{wk}d{i}n{n}"))
        for i in range(5):
            d = date(2026, 8, 31) + timedelta(days=i)
            for n in range(4):                        # a clearly elevated target week
                commits.append((at(d, 7 + n), f"t{i}n{n}"))
        return commits

    def test_B1_nonconstant_baseline_reproduces_median_mad_delta_and_z(self):
        self.sync()
        rep = self.report_via_gateway()
        self.assert_valid_report(rep)
        ch = [c for c in rep["observed_changes"] if c["metric_id"] == GS.METRIC][0]
        exp = independent_change(self.stored_values(), date(2026, 8, 31), MIN_DELTA)
        self.assertGreater(exp["baseline_mad"], 0.0, "this scenario must NOT be zero-MAD")
        self.assertGreater(len(set(exp["baseline"])), 1, "baseline weeks must differ")
        self.assertEqual(ch["status"], "computed", ch)
        for field in ("week_value", "baseline_median", "baseline_mad", "delta",
                      "delta_z"):
            self.assertAlmostEqual(ch[field], exp[field], places=9, msg=field)
        self.assertEqual(ch["meaningful"], exp["meaningful"])
        self.assertIsNone(ch["undefined_reason"])

    def test_B2_missing_days_shrink_the_denominator_and_the_baseline(self):
        """The earliest baseline week is only PARTLY inside the collected window, so it
        has fewer present days. The denominator is present days, never seven."""
        self.sync()
        # Re-sync a narrower window: the append-only store keeps the earlier facts, so
        # narrow the evidence by deleting the earliest days through a normal fetch that
        # reports them as undetermined... instead, collect from a later start.
        cut = date(2026, 7, 8)          # a Wednesday inside the earliest baseline week
        fr, prov = GS.fetch_result(
            repos=self.cfg["sources"]["git"]["repos"], author_emails=[ME],
            analysis_timezone=TZ, start=cut, end=AS_OF, FetchResult=CO.FetchResult)
        self.assertEqual(fr.status, "ok")
        vals = {d: v["value"] for d, v in fr.observed.items()}
        first_week = date(2026, 7, 6)
        present = [d for d in vals if first_week <= d <= first_week + timedelta(days=6)]
        self.assertEqual(len(present), 5, "Wed-Sun of the earliest baseline week")
        mean = sum(vals[d] for d in present) / len(present)
        self.assertAlmostEqual(weekly_means(vals, first_week), mean, places=12)
        self.assertNotEqual(len(present), 7, "the denominator is NOT seven")
        # and a week with fewer than five present days is not a valid baseline week
        self.assertIsNone(weekly_means(vals, first_week - timedelta(weeks=1)))


class C_SnapshotIdentity(MilestoneBase):
    """Item 2. A publication is scheduled at a deterministic point INSIDE the leaf's
    real `open_snapshot()`, using the reviewer's own seam. No sleeps, no polling."""

    def racing_leaf(self, *, fire_at=1, mode="advance", policy_id=None):
        """A thin wrapper around the REAL leaf. It changes no response field; it only
        decides WHEN a publication happens relative to the snapshot being opened."""
        path = self.base / f"racing_leaf_{fire_at}_{mode}.py"
        sentinel = self.base / f"fired_{fire_at}_{mode}"
        path.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(ROOT / 'tools')!r})\n"
            "import store, stdio_leaf\n"
            f"SENTINEL = pathlib.Path({str(sentinel)!r})\n"
            f"FIRE_AT, MODE, POLICY = {fire_at}, {mode!r}, {policy_id!r}\n"
            "calls = 0\n"
            "original = store.Store.open_snapshot\n"
            "def interleaved(self, *a, **kw):\n"
            "    global calls\n"
            "    calls += 1\n"
            "    if MODE == 'always' or (calls == FIRE_AT and not SENTINEL.exists()):\n"
            "        SENTINEL.write_text('fired')\n"
            "        if MODE == 'recreate':\n"
            "            self.destroy_and_recreate(POLICY)\n"
            "        else:\n"
            "            self.publish(lambda c: None)\n"
            "    return original(self, *a, **kw)\n"
            "store.Store.open_snapshot = interleaved\n"
            "stdio_leaf.main()\n")
        return path

    def test_C1_a_publication_inside_open_snapshot_is_not_reported_as_success(self):
        """Rev 10 returned isError=false carrying a DIFFERENT generation."""
        self.sync()
        before = self.store.token()
        leaf = GW.Leaf("git", self.racing_leaf(), self.store.dir,
                       config=self.config_path)
        try:
            r = leaf.call("fetch", {"store_token": before, "start": "2026-07-06",
                                    "end": AS_OF.isoformat()})
            res = r["result"]
        finally:
            leaf.close()
        sc = res["structuredContent"]
        self.assertTrue(res["isError"], sc)
        self.assertEqual(sc["error_type"], "generation_mismatch")
        self.assertEqual(sc["details"]["current_store_token"], self.store.token())
        self.assertNotEqual(self.store.token(), before, "the race really happened")

    def test_C2_the_gateway_enters_the_retry_path_and_converges(self):
        self.sync()
        gw = self.gateway(leaf=self.racing_leaf(fire_at=1))
        rep = GW.assemble_report(gw, as_of=AS_OF, min_meaningful_delta=MIN_DELTA)
        self.assertEqual(gw.attempt_count, 2, "the FIRST attempt must have failed")
        self.assertEqual(len(gw.mismatches), 1, "exactly one mismatch was observed")
        self.assert_valid_report(rep)
        token = self.store.token()
        self.assertEqual(rep["generation"], token["generation"],
                         "every value came from the accepted token")
        self.assertEqual(rep["store_instance_id"], token["store_instance_id"])

    def test_C3_a_publication_BETWEEN_contributing_calls_retries_the_whole_fan_out(self):
        """source_status succeeds, then the store advances before `fetch` opens. A
        report must never be composed half from one generation and half from another."""
        self.sync()
        gw = self.gateway(leaf=self.racing_leaf(fire_at=2))
        rep = GW.assemble_report(gw, as_of=AS_OF, min_meaningful_delta=MIN_DELTA)
        self.assertEqual(gw.attempt_count, 2)
        self.assertEqual(len(gw.mismatches), 1)
        self.assert_valid_report(rep)
        self.assertEqual(rep["generation"], self.store.token()["generation"])

    def test_C4_retry_exhaustion_is_reported_not_papered_over(self):
        self.sync()
        gw = self.gateway(leaf=self.racing_leaf(mode="always"))
        with self.assertRaises(GW.GenerationUnstable):
            GW.assemble_report(gw, as_of=AS_OF, min_meaningful_delta=MIN_DELTA)
        self.assertEqual(gw.attempt_count, GW.GENERATION_RETRY_BUDGET + 1)
        self.assertEqual(len(gw.mismatches), GW.GENERATION_RETRY_BUDGET + 1)

    def test_C5_a_store_recreated_mid_report_is_never_retried_into(self):
        self.sync()
        policy_id = digest("policy", RP.report_policy())
        gw = self.gateway(leaf=self.racing_leaf(mode="recreate", policy_id=policy_id))
        with self.assertRaises(GW.StoreIdentityMismatch):
            GW.assemble_report(gw, as_of=AS_OF, min_meaningful_delta=MIN_DELTA)
        self.assertEqual(gw.attempt_count, 1, "a different store is not a retry case")

    def test_C6_an_unverifiable_success_is_refused(self):
        """The gateway does not trust isError=false; it checks the claimed identity."""
        self.assertIsNone(GW.response_token({"payload_type": "fetch"}))
        tok = {"store_instance_id": "a" * 32, "generation": 3}
        self.assertEqual(GW.response_token({"store_token": tok}), tok)
        self.assertEqual(GW.response_token({"store_instance_id": "a" * 32,
                                            "generation": 3}), tok)
        gw = self.gateway()
        with self.assertRaises(GW.LeafProtocolError):
            gw._verify("git", tok, {"isError": False,
                                    "structuredContent": {"payload_type": "fetch"}})


class D_HistoryCompleteness(MilestoneBase):
    """Item 4. A successful `git log` is not evidence that the history is present."""

    def test_D1_a_shallow_clone_is_refused_instead_of_emitting_false_zeros(self):
        """The independent case, reproduced exactly: one commit on Sep 1 and one on
        Sep 2, read through a depth-1 clone."""
        source = make_repo(self.base / "two-commits",
                           [(at(date(2026, 9, 1)), "c1"), (at(date(2026, 9, 2)), "c2")])
        shallow = self.base / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", source.as_uri(),
                        str(shallow)], check=True, timeout=60)
        kwargs = dict(repos=[shallow], author_emails=[ME], analysis_timezone="UTC",
                      start=date(2026, 9, 1), end=date(2026, 9, 2),
                      FetchResult=CO.FetchResult)
        fr, prov = GS.fetch_result(**kwargs)
        self.assertEqual(fr.status, "failed",
                         "Rev 10 reported status='ok' and a MEASURED zero for a day "
                         "whose commits the clone simply did not contain")
        self.assertIn("shallow", fr.reason)
        self.assertEqual(fr.observed, {})
        self.assertIsNone(prov)
        # The independent expectation is preserved exactly: once the missing local
        # history is present, September 1 measures 1 commit.
        git(shallow, "fetch", "-q", "--unshallow")
        fr2, prov2 = GS.fetch_result(**kwargs)
        self.assertEqual(fr2.status, "ok")
        self.assertEqual(fr2.observed[date(2026, 9, 1)],
                         {"value": 1.0, "sample_count": 1, "quality": "measured"})
        self.assertTrue(prov2["history_completeness"][0]["complete"])

    def test_D2_an_empty_repository_is_complete_and_contributes_nothing(self):
        empty = self.base / "empty"
        empty.mkdir(); git(empty, "init", "-q", "-b", "main")
        fr, prov = GS.fetch_result(repos=[empty], author_emails=[ME],
                                   analysis_timezone=TZ, start=date(2026, 9, 1),
                                   end=date(2026, 9, 2), FetchResult=CO.FetchResult)
        self.assertEqual(fr.status, "ok",
                         "a repository with no commits has COMPLETE history "
                         "containing zero commits; Rev 10 called it 'failed'")
        self.assertEqual(len(fr.observed), 2)
        self.assertEqual(fr.observed[date(2026, 9, 1)]["value"], 0.0)
        self.assertIsNone(prov["heads"][str(empty)], "an unborn HEAD is recorded as None")

    def test_D3_a_sync_persists_provenance_that_survives_HEAD_moving(self):
        self.sync()
        con, _ = self.store.open_snapshot()
        try:
            row = con.execute(
                "SELECT run_id, snapshot_id, provenance_json, outcome FROM "
                "collection_runs WHERE source='git'").fetchone()
        finally:
            con.close()
        self.assertIsNotNone(row, "the sync path must record its run")
        run_id, snapshot_id, prov_json, outcome = row
        prov = json.loads(prov_json)
        self.assertEqual(outcome, "ok")
        self.assertEqual(snapshot_id, prov["snapshot_id"])
        self.assertEqual(sorted(prov["heads"]),
                         sorted(self.cfg["sources"]["git"]["repos"]))
        self.assertTrue(all(e["complete"] for e in prov["history_completeness"]))
        self.assertEqual(prov["value_definition"]["analysis_timezone"], TZ)
        heads_then = dict(prov["heads"])
        configured = self.cfg["sources"]["git"]["repos"][0]

        # HEAD moves. The recorded origin of the existing values must not change.
        (self.repo / "later.txt").write_text("later")
        git(self.repo, "add", "."); git(self.repo, "commit", "-q", "-m", "later")
        self.assertNotEqual(GS.capture_heads([configured])[configured],
                            heads_then[configured])
        con, _ = self.store.open_snapshot()
        try:
            still = json.loads(con.execute(
                "SELECT provenance_json FROM collection_runs WHERE run_id=?",
                [run_id]).fetchone()[0])
        finally:
            con.close()
        self.assertEqual(still["heads"], heads_then)

    def test_D4_every_stored_fact_names_a_recorded_run(self):
        self.sync()
        con, _ = self.store.open_snapshot()
        try:
            orphans = con.execute(
                "SELECT o.metric_id, o.run_id FROM observations o "
                "LEFT JOIN collection_runs r ON r.store_instance_id=o.store_instance_id "
                "AND r.run_id=o.run_id AND r.metric_id=o.metric_id "
                "WHERE r.run_id IS NULL").fetchall()
        finally:
            con.close()
        self.assertEqual(orphans, [])


class E_HonestStatus(MilestoneBase):
    """Item 5. Attempt outcomes come from recorded runs, not from retained facts."""

    def leaf_status(self):
        leaf = GW.Leaf("git", LEAF, self.store.dir, config=self.config_path)
        try:
            r = leaf.call("source_status", {"store_token": self.store.token()})
        finally:
            leaf.close()
        return r["result"]

    def assert_valid_status(self, sc):
        errs = list(V(C.output_schema("source_status"), format_checker=FC)
                    .iter_errors(sc))
        self.assertFalse(errs, "\n".join(e.message[:200] for e in errs[:4]))

    def test_E1_a_successful_sync_reports_a_reachable_fresh_source(self):
        self.sync()
        res = self.leaf_status()
        sc = res["structuredContent"]
        self.assertFalse(res["isError"], sc)
        self.assert_valid_status(sc)
        self.assertEqual(sc["analysis_timezone"], TZ)
        self.assertEqual(sc["analysis_timezone_source"], "recorded_value_definition")
        g = [s for s in sc["sources"] if s["source"] == "git"][0]
        self.assertTrue(g["reachable"])
        self.assertIsNone(g["reason"])
        self.assertEqual(g["last_attempt_outcome"], "ok")
        self.assertFalse(g["stale"])
        self.assertEqual(g["staleness_days"], 0)
        self.assertEqual(g["max_staleness_days"], 2, "from configuration, not a literal")
        # a NON-NULL timestamp must carry an explicit offset
        for field in ("last_successful_sync", "last_attempt_at"):
            self.assertIsNotNone(g[field])
            self.assertTrue(g[field].endswith("+00:00"), g[field])
            datetime.fromisoformat(g[field])          # parses, with a real offset

    def test_E2_a_failed_sync_preserves_facts_and_exposes_the_failed_attempt(self):
        self.sync()
        before = self.stored_values()
        self.assertTrue(before)
        # Inject a real OPERATIONAL failure: an already-configured repository that
        # becomes unreadable. (Adding a repository would change the measurement
        # definition and is refused separately — see H_MeasurementDefinitionChange.)
        import shutil
        shutil.rmtree(self.clone)
        r = self.sync(check=False)
        self.assertEqual(r.returncode, 2, "a failed source is a non-zero exit")
        self.assertEqual(json.loads(r.stdout)["sources"]["git"]["status"], "failed")

        self.assertEqual(self.stored_values(), before,
                         "a failed run deletes and rewrites nothing")
        sc = self.leaf_status()["structuredContent"]
        self.assert_valid_status(sc)
        g = [s for s in sc["sources"] if s["source"] == "git"][0]
        self.assertFalse(g["reachable"], "the LAST attempt failed")
        self.assertEqual(g["last_attempt_outcome"], "failed")
        self.assertIn(self.clone.name, g["last_attempt_reason"])
        self.assertIsNotNone(g["last_successful_sync"],
                             "the earlier success is still recorded")
        self.assertIn(g["last_attempt_reason"], [g["reason"]])

        # the report still carries the retained facts, and says why they are retained
        rep = self.report_via_gateway()
        self.assert_valid_report(rep)
        gs = [s for s in rep["source_status"] if s["source"] == "git"][0]
        self.assertTrue(gs["available"], "retained facts DID contribute")
        self.assertIn("last collection attempt failed", gs["reason"])

        # recovery: the repository comes back at the SAME path, so the measurement
        # definition is unchanged throughout.
        subprocess.run(["git", "clone", "-q", str(self.repo), str(self.clone)],
                       check=True, timeout=60)
        self.sync()
        g = [s for s in self.leaf_status()["structuredContent"]["sources"]
             if s["source"] == "git"][0]
        self.assertTrue(g["reachable"])
        self.assertEqual(g["last_attempt_outcome"], "ok")

    def test_E3_a_source_that_never_succeeded_reports_a_null_sync_and_is_stale(self):
        """The NULL-timestamp branch, which Rev 10's A5 never reached."""
        missing = self.base / "never-existed"
        self.write_config(repos=[missing])
        self.cfg = MC.load(self.config_path)
        r = self.sync(check=False)
        self.assertEqual(r.returncode, 2)
        sc = self.leaf_status()["structuredContent"]
        self.assert_valid_status(sc)
        g = [s for s in sc["sources"] if s["source"] == "git"][0]
        self.assertIsNone(g["last_successful_sync"])
        self.assertIsNone(g["staleness_days"])
        self.assertTrue(g["stale"], "never synced is stale by definition")
        self.assertFalse(g["reachable"])
        self.assertEqual(g["last_attempt_outcome"], "failed")

    def test_E4_old_facts_do_not_make_a_source_look_fresh(self):
        """Rev 10 hardcoded stale=false, so a store of 2025 facts looked healthy."""
        self.sync()
        con, _ = self.store.open_snapshot()
        try:
            sid = self.store.token()["store_instance_id"]
        finally:
            con.close()
        old = datetime(2025, 3, 1, 12)          # UTC-naive, the store convention

        def backdate(con):
            # Both the recorded run AND its denormalised copy move: an integrity check
            # forbids source_state from claiming a success newer than any recorded run,
            # and this test wants an OLD sync, not an inconsistent store.
            con.execute("UPDATE collection_runs SET recorded_at=? "
                        "WHERE store_instance_id=?", [old, sid])
            con.execute("UPDATE source_state SET last_successful_sync=? "
                        "WHERE store_instance_id=?", [old, sid])
        self.store.publish(backdate)
        sc = self.leaf_status()["structuredContent"]
        self.assert_valid_status(sc)
        g = [s for s in sc["sources"] if s["source"] == "git"][0]
        self.assertTrue(g["stale"])
        self.assertGreater(g["staleness_days"], 500)
        self.assertEqual(g["last_successful_sync"], "2025-03-01T12:00:00+00:00",
                         "the recorded instant, with an explicit offset")


class F_ReportEvidenceGating(MilestoneBase):
    """Item 6. A complete-looking baseline must never come from a partial fetch."""

    def test_F1_a_truncated_fetch_makes_the_change_unavailable(self):
        self.sync()
        gw = self.gateway()
        ws, we = RP.default_week(AS_OF)
        token, out = gw.fan_out_many({"git": [
            ("source_status", {}),
            ("fetch", {"start": RP.baseline_span(ws).isoformat(),
                       "end": we.isoformat(), "max_rows": 3})]})
        sc = out[("git", "fetch")]["structuredContent"]
        self.assertTrue(sc["truncation"]["truncated"])
        self.assertEqual(sc["metric_status"][0]["status"], "truncated")

        # The PRODUCTION assembler, given the same low budget, must refuse to compute
        # rather than emit a complete-looking baseline from a partial fetch.
        rep = GW.assemble_report(self.gateway(), as_of=AS_OF,
                                 min_meaningful_delta=MIN_DELTA, max_rows=3)
        self.assert_valid_report(rep)
        self.assertEqual(rep["observed_changes"],
                         [{"status": "unknown", "metric_id": GS.METRIC,
                           "reason": "evidence_truncated"}])
        git_status = [s for s in rep["source_status"] if s["source"] == "git"][0]
        self.assertFalse(git_status["available"])
        self.assertEqual(git_status["metrics_contributed"], 0)

    def test_F2_the_full_report_window_is_not_truncated(self):
        self.sync()
        rep = self.report_via_gateway()
        self.assert_valid_report(rep)
        self.assertEqual([c["status"] for c in rep["observed_changes"]], ["computed"])
        self.assertFalse(rep["truncation"]["truncated"])




class G_DegenerateStates(MilestoneBase):
    """Rev 11 RC audit. Four latent failures, each found by inspection rather than by a
    failing test, each an ordinary state a real deployment reaches."""

    def test_G1_status_on_a_never_synced_store_is_an_answer_not_an_internal_fault(self):
        """`mesh init` then `source_status` before the first sync returned
        -32603 Internal error, because no active definition records a timezone."""
        mesh("init", "--config", str(self.config_path))
        leaf = GW.Leaf("git", LEAF, self.store.dir)          # deliberately no --config
        try:
            r = leaf.call("source_status", {"store_token": self.store.token()})
        finally:
            leaf.close()
        self.assertIn("result", r, "an ordinary empty state is not a protocol error")
        res = r["result"]
        self.assertTrue(res["isError"])
        sc = res["structuredContent"]
        self.assertEqual(sc["error_type"], "insufficient_evidence")
        self.assertIn("no metric is active", sc["message"])
        self.assertNotIn("incident_id", json.dumps(sc.get("details", {})))
        errs = list(V(C.output_schema("source_status"), format_checker=FC).iter_errors(sc))
        self.assertFalse(errs, "the error payload still satisfies the contract")

    def test_G2_a_timezone_disagreement_is_reported_and_still_validates(self):
        """The leaf detected the disagreement and attached a `notes` array that
        StatusPayload forbade, so the branch emitted a schema-invalid payload."""
        import stdio_leaf
        mesh("init", "--config", str(self.config_path))

        def m(con):
            for i, z in enumerate(("UTC", "America/Chicago")):
                d = {"metric_id": f"git.m{i}", "analysis_timezone": z}
                vd = digest("value_definition", d)
                ST.register_definition(con, f"git.m{i}", vd, d)
                ST.activate_definition(con, f"git.m{i}", vd)
        self.store.publish(m)

        leaf = GW.Leaf("git", LEAF, self.store.dir, config=self.config_path)
        try:
            sc = leaf.call("source_status",
                           {"store_token": self.store.token()})["result"]["structuredContent"]
        finally:
            leaf.close()
        self.assertEqual(sc["analysis_timezone"], TZ)
        self.assertEqual(sc["analysis_timezone_source"], "configuration")
        self.assertEqual(len(sc["notes"]), 1)
        self.assertIn("America/Chicago", sc["notes"][0])
        self.assertIn("UTC", sc["notes"][0])
        errs = list(V(C.output_schema("source_status"), format_checker=FC).iter_errors(sc))
        self.assertFalse(errs, "\n".join(e.message[:200] for e in errs[:3]))

    def test_G3_a_report_that_established_nothing_names_no_value_definition(self):
        """Rev 11 emitted digest("report_value_definitions", {"ids": []}) — a
        well-formed identity naming a definition set that does not exist."""
        mesh("init", "--config", str(self.config_path))       # no sync: store is empty
        rep = self.report_via_gateway()
        self.assert_valid_report(rep)
        self.assertIsNone(rep["value_definition_id"], "no definition was established")
        self.assertEqual(rep["observed_changes"], [])
        git_status = [s for s in rep["source_status"] if s["source"] == "git"][0]
        self.assertFalse(git_status["available"])
        self.assertEqual(git_status["metrics_contributed"], 0)
        self.assertIn("no metric", git_status["reason"])

    def test_G3b_a_null_definition_with_a_computed_change_is_contradictory(self):
        """The nullable field is guarded: it may only be null when nothing was computed."""
        self.sync()
        rep = self.report_via_gateway()
        self.assert_valid_report(rep)
        self.assertIsNotNone(rep["value_definition_id"])
        rep["value_definition_id"] = None
        self.assertTrue(list(V(C.output_schema("weekly_report"), format_checker=FC)
                             .iter_errors(rep)),
                        "a computed change with no named definition must be rejected")

    def test_G4_a_mesh_that_collects_nothing_is_refused(self):
        """A report describing zero sources violates source_status minItems; it is
        refused at the configuration boundary and again at the assembler."""
        empty = self.base / "empty.json"
        empty.write_text(json.dumps({"store_dir": "data", "analysis_timezone": TZ,
                                     "sources": {}}))
        with self.assertRaises(MC.ConfigError) as e:
            MC.load(empty)
        self.assertIn("collects nothing", str(e.exception))
        r = mesh("report", "--config", str(empty), check=False)
        self.assertNotEqual(r.returncode, 0)
        with self.assertRaises(GW.NoSourcesConfigured):
            GW.assemble_report(GW.Gateway(self.store, LEAF, []), as_of=AS_OF,
                               min_meaningful_delta=MIN_DELTA)




class H_MeasurementDefinitionChange(MilestoneBase):
    """Blocker A, reproduced by independent acceptance against the frozen RC.

    Changing the configured author allowlist and re-syncing returned SUCCESS and
    advanced the generation, while every report kept using the old author's numbers:
    `require_identities` registered the new definition but activated it only when none
    was active, and the sync path performed no deliberate transition.

    This pilot does not rederive history. The change is REFUSED before anything is
    written. All assertions go through the public commands.
    """
    def _two_author_repo(self):
        repo = self.base / "authors"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "a@example.test")
        git(repo, "config", "user.name", "A")
        for i, email in enumerate(["a@example.test", "a@example.test", "b@example.test"]):
            stamp = f"2026-09-01T{10 + i}:00:00+00:00"
            git(repo, "commit", "--allow-empty", "-q", "-m", f"c{i}",
                env={"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email,
                     "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
        return repo

    def _write(self, repo, emails):
        self.config_path.write_text(json.dumps({
            "store_dir": "data", "analysis_timezone": "UTC",
            "max_staleness_days": 2, "min_meaningful_delta": MIN_DELTA,
            "sources": {"git": {"enabled": True, "repos": [str(repo)],
                                "author_emails": emails},
                        "health": {"enabled": False,
                                   "unavailable_reason": "no adapter is built"}}}))

    def setUp(self):
        super().setUp()
        self.repo2 = self._two_author_repo()
        self._write(self.repo2, ["a@example.test"])
        self.cfg = MC.load(self.config_path)
        self.store = Store(Path(self.cfg["store_dir"]) / "facts.duckdb")

    def _sync_cli(self, check=True):
        return mesh("sync", "--config", str(self.config_path),
                    "--as-of", "2026-09-08", check=check)

    def _report_cli(self, check=True):
        return mesh("report", "--config", str(self.config_path),
                    "--as-of", "2026-09-08", check=check)

    def test_H1_the_first_sync_and_report_are_correct(self):
        self._sync_cli()
        rep = json.loads(self._report_cli().stdout)
        self.assert_valid_report(rep)
        self.assertAlmostEqual(rep["observed_changes"][0]["week_value"], 2 / 7, places=12)

    def test_H2_a_changed_author_allowlist_is_refused_before_any_write(self):
        self._sync_cli()
        before = self.store.token()
        before_rep = json.loads(self._report_cli().stdout)

        self._write(self.repo2, ["b@example.test"])
        r = self._sync_cli(check=False)
        self.assertEqual(r.returncode, 3, "a refused configuration is exit 3")
        out = json.loads(r.stdout)
        self.assertEqual(out["error"], "measurement_definition_changed")
        self.assertEqual(out["conflicts"][0]["differing_fields"], ["author_emails"])
        self.assertEqual(out["conflicts"][0]["metric_id"], GS.METRIC)
        self.assertTrue(out["store_unchanged"])
        self.assertIn("NEW store", out["remediation"])

        self.assertEqual(self.store.token(), before,
                         "the generation must NOT advance on a refusal")
        con, _ = self.store.open_snapshot()
        try:
            rows = con.execute("SELECT date, value FROM observations_current "
                               "ORDER BY date").fetchall()
            runs = con.execute("SELECT count(*) FROM collection_runs").fetchone()[0]
        finally:
            con.close()
        self.assertTrue(rows, "facts are preserved")
        self.assertEqual(runs, 1, "a refused sync records no attempt")

        # and the report is refused too, rather than returning stale numbers
        rr = self._report_cli(check=False)
        self.assertEqual(rr.returncode, 3)
        self.assertEqual(json.loads(rr.stdout)["error"],
                         "measurement_definition_changed")
        # nothing about the prior report changed
        self.assertEqual(json.loads(self._restore_and_report()), before_rep)

    def _restore_and_report(self):
        self._write(self.repo2, ["a@example.test"])
        return self._report_cli().stdout

    def test_H3_an_unchanged_configuration_still_syncs_and_reports(self):
        self._sync_cli()
        first = json.loads(self._report_cli().stdout)
        r = self._sync_cli()
        self.assertEqual(json.loads(r.stdout)["sources"]["git"]["status"], "ok")
        second = json.loads(self._report_cli().stdout)
        self.assertEqual(first["value_definition_id"], second["value_definition_id"])
        self.assertAlmostEqual(second["observed_changes"][0]["week_value"], 2 / 7,
                               places=12)

    def test_H4_report_only_settings_never_trigger_a_refusal(self):
        """min_meaningful_delta and max_staleness_days are a separate policy: they do
        not enter the value definition and must not read as a measurement change."""
        self._sync_cli()
        cfg = json.loads(self.config_path.read_text())
        cfg["min_meaningful_delta"] = 99.0
        cfg["max_staleness_days"] = 30
        self.config_path.write_text(json.dumps(cfg))
        self.assertEqual(self._sync_cli().returncode, 0)
        rep = json.loads(self._report_cli().stdout)
        self.assert_valid_report(rep)
        self.assertAlmostEqual(rep["observed_changes"][0]["week_value"], 2 / 7, places=12)

    def test_H5_a_new_store_accepts_the_changed_configuration(self):
        """The documented remediation actually works."""
        self._sync_cli()
        cfg = json.loads(self.config_path.read_text())
        cfg["sources"]["git"]["author_emails"] = ["b@example.test"]
        cfg["store_dir"] = "data-b"
        self.config_path.write_text(json.dumps(cfg))
        mesh("init", "--config", str(self.config_path))
        self.assertEqual(self._sync_cli().returncode, 0)
        rep = json.loads(self._report_cli().stdout)
        self.assert_valid_report(rep)
        self.assertAlmostEqual(rep["observed_changes"][0]["week_value"], 1 / 7, places=12,
                               msg="the B-only value the acceptance probe expected")

    def test_H6_the_public_MCP_endpoint_refuses_too(self):
        """`mesh serve` must not reopen the hole the CLI closed."""
        self._sync_cli()
        self._write(self.repo2, ["b@example.test"])
        res = self._call_gateway({"week_of": "2026-08-31"})
        sc = res["structuredContent"]
        self.assertTrue(res["isError"], sc)
        self.assertEqual(sc["error_type"], "invalid_request")
        self.assertIn("measurement definition", sc["message"])
        errs = list(V(C.output_schema("weekly_report"), format_checker=FC)
                    .iter_errors(sc))
        self.assertFalse(errs, "the refusal itself satisfies the contract")

    def _call_gateway(self, arguments, program=None):
        m = {"io.modelcontextprotocol/protocolVersion": C.PROTOCOL_VERSION,
             "io.modelcontextprotocol/clientCapabilities": {}}
        frame = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                 "params": {"name": "weekly_report", "arguments": arguments,
                            "_meta": m}}
        r = subprocess.run(
            [sys.executable, str(program or ROOT / "tools" / "gateway_server.py"),
             "--config", str(self.config_path)],
            input=json.dumps(frame) + "\n", capture_output=True, text=True,
            cwd=str(ROOT), timeout=120)
        return json.loads(r.stdout.splitlines()[0])["result"]




class I_PublicFailureResponses(MilestoneBase):
    """Blocker B, reproduced by independent acceptance against the frozen RC.

    Retry exhaustion and store recreation emitted `generation_mismatch` and
    `store_instance_mismatch` with NO `details`, which the unchanged ErrorPayload
    contract requires for both. The supplied C4/C5 tests stopped at the Python
    exceptions and never exercised the public response conversion.

    Every assertion here validates the response the PUBLIC endpoint actually emitted,
    unchanged.
    """
    def _racing_gateway(self, mode):
        """Wraps only the leaf's real snapshot-open scheduling; no response field is
        touched. Mirrors the independent probe's seam."""
        leaf = self.base / f"leaf_{mode}.py"
        leaf.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(ROOT / 'tools')!r})\n"
            "import store, stdio_leaf\n"
            f"MODE = {mode!r}\n"
            "original = store.Store.open_snapshot\n"
            "def interleaved(self, *a, **k):\n"
            "    if MODE == 'recreate':\n"
            "        self.destroy_and_recreate('synthetic-reset')\n"
            "    else:\n"
            "        self.publish(lambda c: None)\n"
            "    return original(self, *a, **k)\n"
            "store.Store.open_snapshot = interleaved\n"
            "stdio_leaf.main()\n")
        gw = self.base / f"gateway_{mode}.py"
        gw.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'tools')!r})\n"
            "import mesh_config, gateway_server\n"
            "original = mesh_config.wire\n"
            "def wire(cfg):\n"
            "    store, leaf, enabled, unavailable = original(cfg)\n"
            f"    return store, {str(leaf)!r}, enabled, unavailable\n"
            "mesh_config.wire = wire\n"
            "gateway_server.main()\n")
        return gw

    def _public_call(self, program):
        m = {"io.modelcontextprotocol/protocolVersion": C.PROTOCOL_VERSION,
             "io.modelcontextprotocol/clientCapabilities": {}}
        frame = {"jsonrpc": "2.0", "id": 831, "method": "tools/call",
                 "params": {"name": "weekly_report",
                            "arguments": {"week_of": "2026-08-31"}, "_meta": m}}
        r = subprocess.run([sys.executable, str(program), "--config",
                            str(self.config_path)],
                           input=json.dumps(frame) + "\n", capture_output=True,
                           text=True, cwd=str(ROOT), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        frames = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        self.assertEqual(len(frames), 1, frames)
        self.assertEqual(frames[0]["id"], 831)
        return frames[0]["result"]

    def _assert_contract(self, sc):
        errs = list(V(C.output_schema("weekly_report"), format_checker=FC)
                    .iter_errors(sc))
        self.assertFalse(errs, "\n".join(e.message[:200] for e in errs[:3]))

    def test_I1_retry_exhaustion_carries_the_required_generation_details(self):
        self.sync()
        res = self._public_call(self._racing_gateway("always"))
        sc = res["structuredContent"]
        self.assertTrue(res["isError"], sc)
        self.assertEqual(sc["error_type"], "generation_mismatch")
        self.assertTrue(sc["retryable"])
        self.assertIn("details", sc, "the contract REQUIRES details here")
        self.assertEqual(sc["details"]["current_generation"],
                         sc["details"]["current_store_token"]["generation"])
        self.assertEqual(sc["details"]["current_store_token"]["store_instance_id"],
                         self.store.token()["store_instance_id"])
        self._assert_contract(sc)

    def test_I2_store_recreation_carries_the_required_token_and_is_not_retryable(self):
        self.sync()
        res = self._public_call(self._racing_gateway("recreate"))
        sc = res["structuredContent"]
        self.assertTrue(res["isError"], sc)
        self.assertEqual(sc["error_type"], "store_instance_mismatch")
        self.assertFalse(sc["retryable"], "a different store is never a retry case")
        self.assertIn("details", sc)
        tok = sc["details"]["current_store_token"]
        self.assertEqual(set(tok), {"store_instance_id", "generation"})
        self.assertEqual(tok, self.store.token(),
                         "the token names the store that actually answered")
        self._assert_contract(sc)

    def test_I3_a_leaf_storage_error_carries_its_required_details(self):
        """The same class of defect: `storage_error` requires store_path_id and cause."""
        import stdio_leaf
        self.sync()
        leaf = GW.Leaf("git", LEAF, self.store.dir, config=self.config_path)
        try:
            def m(con):
                d = {"metric_id": "git.no_version", "analysis_timezone": TZ}
                vd = digest("value_definition", d)
                ST.register_definition(con, "git.no_version", vd, d)
                ST.activate_definition(con, "git.no_version", vd)
            self.store.publish(m)
            r = leaf.call("fetch", {"store_token": self.store.token(),
                                    "start": "2026-08-31", "end": "2026-09-06"})
        finally:
            leaf.close()
        sc = r["result"]["structuredContent"]
        self.assertTrue(r["result"]["isError"], sc)
        self.assertEqual(sc["error_type"], "storage_error")
        self.assertEqual(sc["details"]["cause"], "definition_incomplete")
        self.assertRegex(sc["details"]["store_path_id"], r"^[0-9a-f]{16}$")
        errs = list(V(C.output_schema("fetch"), format_checker=FC).iter_errors(sc))
        self.assertFalse(errs, "\n".join(e.message[:200] for e in errs[:3]))

    def test_I4_every_advertised_tool_error_payload_validates(self):
        """A sweep, so this class cannot regress silently: drive each leaf tool into an
        error and validate the emitted payload against its own output schema.

        The uninitialised-store case was found by writing this sweep: a leaf pointed at
        a directory with no pointer answered -32603 rather than a contract-valid
        storage_error."""
        leaf = GW.Leaf("git", LEAF, self.store.dir, config=self.config_path)
        try:
            for tool, args in (("fetch", {"store_token": {"store_instance_id": "0" * 32,
                                                          "generation": 1},
                                          "start": "2026-08-31", "end": "2026-09-06"}),
                               ("source_status", {"store_token": {
                                   "store_instance_id": "0" * 32, "generation": 1}})):
                sc = leaf.call(tool, args)["result"]["structuredContent"]
                self.assertEqual(sc["error_type"], "storage_error", tool)
                self.assertEqual(sc["details"]["cause"], "manifest_missing", tool)
                errs = list(V(C.output_schema(tool), format_checker=FC).iter_errors(sc))
                self.assertFalse(errs, f"uninitialised {tool}: " + "; ".join(
                    e.message[:160] for e in errs[:2]))
        finally:
            leaf.close()

        self.sync()
        leaf = GW.Leaf("git", LEAF, self.store.dir, config=self.config_path)
        try:
            stale = {"store_instance_id": "0" * 32, "generation": 1}
            for tool, args in (("fetch", {"store_token": stale, "start": "2026-08-31",
                                          "end": "2026-09-06"}),
                               ("source_status", {"store_token": stale})):
                sc = leaf.call(tool, args)["result"]["structuredContent"]
                self.assertEqual(sc["payload_type"], "error", tool)
                errs = list(V(C.output_schema(tool), format_checker=FC)
                            .iter_errors(sc))
                self.assertFalse(errs, f"{tool}: " + "; ".join(
                    e.message[:160] for e in errs[:2]))
        finally:
            leaf.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
