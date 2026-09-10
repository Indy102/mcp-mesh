"""Tier T4 — REAL stdio transport against a REAL subprocess.

Frames are written to and read from the child's pipes, so every assertion is about
bytes on the wire. Results are validated against the OFFICIAL 2026-07-28 schema.
"""
import json, os, queue, subprocess, sys, threading, unittest
from pathlib import Path

READ_TIMEOUT = 10.0


def read_line(proc, timeout=READ_TIMEOUT):
    """Bounded read. `stdout.readline()` blocks forever; a teardown timeout does not
    rescue a hung suite. A missing reply must FAIL, not hang."""
    q: "queue.Queue[str]" = queue.Queue(maxsize=1)
    t = threading.Thread(target=lambda: q.put(proc.stdout.readline()), daemon=True)
    t.start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        proc.kill()
        raise AssertionError(f"no reply within {timeout}s (server exited "
                             f"{proc.poll()!r})")
from jsonschema import Draft202012Validator as V

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import contracts as C

LEAF = ROOT / "tools" / "stdio_leaf.py"
PV = C.PROTOCOL_VERSION
MCP = json.loads((ROOT / "schemas" / "mcp-official-2026-07-28.json").read_text())
TOK = {"store_instance_id": "5f1c9a2e7b34d0864ae1f2c9b7d05e31", "generation": 41}


def official(name):
    s = dict(MCP); s["$ref"] = f"#/$defs/{name}"; return s


def meta(caps=None, version=PV):
    return {"io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientCapabilities": caps if caps is not None else {},
            "io.modelcontextprotocol/clientInfo": {"name": "raw-wire-test", "version": "1.0"}}


class StdioProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = subprocess.Popen([sys.executable, str(LEAF)], cwd=str(ROOT),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, bufsize=1,
                                 env={**os.environ, "PYTHONUNBUFFERED": "1"})
        cls.sent = []

    @classmethod
    def tearDownClass(cls):
        try:
            cls.p.stdin.close(); cls.p.wait(timeout=10)
        except Exception:
            cls.p.kill()

    def raw(self, line, expect_reply=True):
        self.p.stdin.write(line + "\n"); self.p.stdin.flush()
        if not expect_reply:
            return None
        out = read_line(self.p)
        self.assertTrue(out, "server closed stdout without replying")
        return json.loads(out)

    def rpc(self, obj, expect_reply=True):
        type(self).sent.append(obj)
        return self.raw(json.dumps(obj), expect_reply)

    # ---------------------------------------------------------------- profile
    def test_01_discover(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                      "params": {"_meta": meta()}})
        self.assertEqual(r["id"], 1)
        res = r["result"]
        self.assertEqual(res["resultType"], "complete")
        for f in ("supportedVersions", "capabilities", "ttlMs", "cacheScope"):
            self.assertIn(f, res)
        self.assertIn(PV, res["supportedVersions"])
        self.assertEqual(res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
                         "mesh-leaf")
        self.assertNotIn("serverInfo", res, "serverInfo belongs in _meta, not the result")
        self.assertNotIn("payload_type", res)
        self.assertFalse(list(V(official("DiscoverResult")).iter_errors(res)))

    def test_02_tools_list_is_exactly_what_this_server_implements(self):
        """Rev 10 advertised all nine specified tools from a leaf, so `weekly_report`
        appeared in the catalogue and then answered `insufficient_evidence`. A server
        advertises what it serves and nothing else."""
        import stdio_leaf
        r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                      "params": {"_meta": meta()}})
        res = r["result"]
        self.assertEqual(res["resultType"], "complete")
        names = [t["name"] for t in res["tools"]]
        self.assertEqual(names, sorted(stdio_leaf.IMPLEMENTED),
                         "exactly the implemented tools, deterministic order")
        self.assertEqual(names, ["fetch", "source_status"])
        self.assertNotIn("weekly_report", names,
                         "the leaf does not implement weekly_report; the gateway does")
        for t in res["tools"]:
            self.assertIsInstance(t["inputSchema"], dict)
            self.assertIn("outputSchema", t, "outputSchema is a v1 invariant")
            self.assertEqual(t["outputSchema"]["type"], "object")
        self.assertNotIn("nextCursor", res, "the fixed catalogue fits one page")
        self.assertNotIn("payload_type", res)
        self.assertFalse(list(V(official("ListToolsResult")).iter_errors(res)))

    def test_03_tool_call_success(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "source_status", "arguments": {"store_token": TOK}, "_meta": meta()}})
        res = r["result"]
        self.assertEqual(res["resultType"], "complete")
        self.assertIs(res["isError"], False)
        self.assertEqual(res["structuredContent"]["payload_type"], "status")
        self.assertEqual(json.loads(res["content"][0]["text"]), res["structuredContent"])
        self.assertFalse(list(V(official("CallToolResult")).iter_errors(res)))
        e = list(V(C.output_schema("source_status")).iter_errors(res["structuredContent"]))
        self.assertFalse(e, e[0].message[:200] if e else "")

    def test_04_tool_execution_error_is_a_result(self):
        """This leaf was started with no --store, so `fetch` cannot be served. That is
        a tool EXECUTION failure and must be a result with isError, not a -32xxx."""
        r = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "fetch",
                                 "arguments": {"store_token": TOK, "start": "2026-01-01",
                                               "end": "2026-01-31"},
                                 "_meta": meta()}})
        self.assertIn("result", r, "a tool failure must not be a protocol error")
        self.assertTrue(r["result"]["isError"])
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc["payload_type"], "error")
        # `invalid_request`, not `storage_error`: no store is selected, so there is no
        # store_path_id to name and `storage_error` REQUIRES one. This process cannot
        # serve a store-qualified request at all.
        self.assertEqual(sc["error_type"], "invalid_request")
        from jsonschema import FormatChecker as _FC
        errs = list(V(C.output_schema("fetch"), format_checker=_FC())
                    .iter_errors(sc))
        self.assertFalse(errs, "the emitted error payload must satisfy the contract")

    def test_04b_a_specified_but_unserved_tool_is_unknown(self):
        """`weekly_report` is specified, and served by the gateway. Asking a LEAF for it
        is an unknown tool, not a tool that answers `insufficient_evidence`."""
        for name in ("weekly_report", "compare_periods", "association"):
            r = self.rpc({"jsonrpc": "2.0", "id": 40, "method": "tools/call",
                          "params": {"name": name, "arguments": {}, "_meta": meta()}})
            self.assertNotIn("result", r, name)
            self.assertEqual(r["error"]["code"], -32602, name)
            self.assertIn("Unknown tool", r["error"]["message"], name)

    # ---------------------------------------------------------------- error ladder
    def test_05_parse_error_minus_32700_no_id(self):
        r = self.raw("{not json")
        self.assertEqual(r["error"]["code"], -32700)
        self.assertNotIn("id", r)
        self.assertNotIn("result", r)

    def test_06_invalid_request_minus_32600_no_id(self):
        r = self.raw(json.dumps({"jsonrpc": "2.0", "params": {}}))
        self.assertEqual(r["error"]["code"], -32600)
        self.assertNotIn("id", r)

    def test_07_unknown_method_minus_32601(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/summon",
                      "params": {"_meta": meta()}})
        self.assertEqual(r["error"]["code"], -32601)
        self.assertEqual(r["id"], 7)
        self.assertNotIn("result", r)

    def test_08_unknown_tool_minus_32602(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                      "params": {"name": "correlate", "arguments": {}, "_meta": meta()}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("correlate", r["error"]["message"])
        self.assertEqual(r["id"], 8)

    def test_09_bad_arguments_minus_32602(self):
        """A SERVED tool with schema-invalid arguments. (Using an unserved tool here
        would pass for the wrong reason: unknown tool is also -32602.)"""
        r = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                      "params": {"name": "fetch",
                                 "arguments": {"store_token": TOK, "start": "2026-01-01",
                                               "end": "2026-01-31", "max_rows": 0},
                                 "_meta": meta()}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("Invalid arguments for fetch", r["error"]["message"])

    def test_10_missing_meta_minus_32602(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(r["id"], 10)

    def test_11_unsupported_version_minus_32022_after_negotiation(self):
        """Structurally valid; must REACH negotiation rather than be rejected as malformed."""
        r = self.rpc({"jsonrpc": "2.0", "id": 11, "method": "server/discover",
                      "params": {"_meta": meta(version="1900-01-01")}})
        self.assertEqual(r["error"]["code"], -32022)
        self.assertEqual(r["error"]["data"]["requested"], "1900-01-01")
        self.assertIn(PV, r["error"]["data"]["supported"])

    def test_12_missing_capability_minus_32021(self):
        """Exercised through a TEST-ONLY tool: no production tool fabricates a
        capability requirement it has no workflow for."""
        r = self.rpc({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                      "params": {"name": "__needs_elicitation__", "arguments": {},
                                 "_meta": meta(caps={})}})
        self.assertEqual(r["error"]["code"], -32021)
        self.assertEqual(r["error"]["data"]["requiredCapabilities"], ["elicitation"])

    def test_13_capability_present_is_served(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                      "params": {"name": "__needs_elicitation__", "arguments": {},
                                 "_meta": meta(caps={"elicitation": {"form": {}}})}})
        self.assertIn("result", r)

    def test_14_internal_fault_minus_32603_redacted(self):
        r = self.rpc({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                      "params": {"name": "__boom__", "arguments": {}, "_meta": meta()}})
        self.assertEqual(r["error"]["code"], -32603)
        self.assertEqual(r["error"]["message"], "Internal error")
        self.assertRegex(r["error"]["data"]["incident_id"], r"^[0-9a-f]{16}$")
        blob = json.dumps(r)
        self.assertNotIn("Traceback", blob)
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("deliberate internal fault", blob)

    # ---------------------------------------------------------------- discipline
    def test_15_response_ids_echo_and_notifications_get_no_reply(self):
        for i in (21, "str-id", 23):
            r = self.rpc({"jsonrpc": "2.0", "id": i, "method": "tools/list",
                          "params": {"_meta": meta()}})
            self.assertEqual(r["id"], i)
        self.rpc({"jsonrpc": "2.0", "method": "notifications/whatever",
                  "params": {"_meta": meta()}}, expect_reply=False)
        r = self.rpc({"jsonrpc": "2.0", "id": 24, "method": "tools/list",
                      "params": {"_meta": meta()}})
        self.assertEqual(r["id"], 24, "a notification must not consume a response slot")

    def test_16_server_emits_resultType_itself(self):
        """No test may inject resultType; assert the raw frame already carried it."""
        raw = json.dumps({"jsonrpc": "2.0", "id": 16, "method": "tools/list",
                          "params": {"_meta": meta()}})
        self.p.stdin.write(raw + "\n"); self.p.stdin.flush()
        line = read_line(self.p)
        self.assertIn('"resultType": "complete"', line.replace('"resultType":"complete"',
                                                               '"resultType": "complete"'))

    def test_17_no_initialize_anywhere_in_the_session(self):
        methods = [m.get("method") for m in self.sent]
        self.assertNotIn("initialize", methods)
        self.assertFalse([m for m in methods if m and m.startswith("notifications/initialized")])
        for m in self.sent:
            mt = (m.get("params") or {}).get("_meta")
            if mt:
                self.assertIn("io.modelcontextprotocol/protocolVersion", mt)
                self.assertIn("io.modelcontextprotocol/clientCapabilities", mt)


# ============ Rev 9 regressions: independent protocol probes ============
# Every case asserts the CORRECTED behaviour AND that the server survives: a valid
# frame sent immediately afterwards must still receive its response.

class MalformedFrameRegressions(unittest.TestCase):
    """Each case runs in its own subprocess with a bounded timeout."""
    def _probe(self, first_line, expect_code, expect_id="absent"):
        p = subprocess.Popen([sys.executable, str(LEAF)], cwd=str(ROOT),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1,
                             env={**os.environ, "PYTHONUNBUFFERED": "1"})
        try:
            p.stdin.write(first_line + "\n"); p.stdin.flush()
            first = json.loads(read_line(p))
            follow = {"jsonrpc": "2.0", "id": 999, "method": "tools/list",
                      "params": {"_meta": meta()}}
            p.stdin.write(json.dumps(follow) + "\n"); p.stdin.flush()
            second = json.loads(read_line(p))
        finally:
            try:
                p.stdin.close(); p.wait(timeout=10)
            except Exception:
                p.kill()
        self.assertEqual(first.get("error", {}).get("code"), expect_code,
                         f"expected {expect_code}, got {first}")
        self.assertNotIn("result", first, "a protocol error is never a tool result")
        if expect_id == "absent":
            self.assertNotIn("id", first, "an unreadable id must be omitted, not guessed")
        else:
            self.assertEqual(first["id"], expect_id)
        self.assertEqual(second["id"], 999, "the server must survive and keep answering")
        self.assertIn("result", second)
        self.assertFalse(list(V(official("ListToolsResult")).iter_errors(second["result"])))

    def test_string_params_is_minus_32602_and_survives(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                "params": "nope"}), -32602, 1)

    def test_string_meta_is_minus_32602_and_survives(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                "params": {"_meta": "nope"}}), -32602, 1)

    def test_array_method_is_minus_32600_and_survives(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": ["tools/list"],
                                "params": {"_meta": meta()}}), -32600)

    def test_boolean_id_is_rejected(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": True, "method": "tools/list",
                                "params": {"_meta": meta()}}), -32600)

    def test_fractional_id_is_rejected(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1.5, "method": "tools/list",
                                "params": {"_meta": meta()}}), -32600)

    def test_explicit_null_id_is_rejected_not_treated_as_absent(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": None, "method": "tools/list",
                                "params": {"_meta": meta()}}), -32600)

    def test_array_arguments_is_rejected_not_coerced(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "source_status", "arguments": [],
                                           "_meta": meta()}}), -32602, 1)

    def test_string_client_capabilities_is_rejected(self):
        m = meta(); m["io.modelcontextprotocol/clientCapabilities"] = "nope"
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                "params": {"_meta": m}}), -32602, 1)

    def test_invalid_calendar_date_never_reaches_tool_execution(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "fetch", "arguments": {
                                    "store_token": {"store_instance_id": "a" * 32,
                                                    "generation": 1},
                                    "start": "2026-02-31", "end": "2026-03-01"},
                                    "_meta": meta()}}), -32602, 1)

    def test_arguments_error_precedes_capability_error(self):
        """Declared order is arguments BEFORE capability; Rev 8 returned -32021 first."""
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "__needs_elicitation__",
                                           "arguments": [], "_meta": meta(caps={})}}),
                    -32602, 1)

    def test_reversed_fetch_range_is_a_semantic_rejection(self):
        """SI-1 on a SERVED tool. The compare_periods SI-1/SI-2 rules are still
        declared and are exercised directly in T1c, since no server serves that tool."""
        r = self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "fetch", "arguments": {
                                        "store_token": {"store_instance_id": "a" * 32,
                                                        "generation": 1},
                                        "start": "2026-03-01", "end": "2026-01-01"},
                                        "_meta": meta()}}), -32602, 1)

    def test_explicit_null_arguments_is_rejected(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "source_status", "arguments": None,
                                           "_meta": meta()}}), -32602, 1)

    def test_explicit_null_params_is_rejected(self):
        self._probe(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                "params": None}), -32602, 1)

    def test_deeply_nested_frame_is_refused_and_the_server_survives(self):
        deep = "[" * 10000 + "]" * 10000
        self._probe('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":'
                    + deep + "}", -32600)

    def test_oversized_frame_is_refused_and_the_server_survives(self):
        from stdio_leaf import MAX_FRAME_BYTES
        big = "x" * (MAX_FRAME_BYTES + 10)
        self._probe('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{},'
                    f'"pad":"{big}"}}}}', -32600)

    def test_stated_limits_are_published_not_implied(self):
        from stdio_leaf import MAX_FRAME_BYTES, MAX_NESTING_DEPTH
        self.assertEqual(MAX_FRAME_BYTES, 1 << 20)
        self.assertEqual(MAX_NESTING_DEPTH, 100)

    def test_no_production_tool_requires_a_capability(self):
        import contracts as _C
        from stdio_leaf import REQUIRES_CAPABILITY, TEST_ONLY_TOOLS
        self.assertEqual(REQUIRES_CAPABILITY, {},
                         "capability requirements must map to a real workflow")
        self.assertTrue(set(TEST_ONLY_TOOLS).isdisjoint(_C.INPUTS),
                        "test-only tools are never advertised")




# ============ Rev 11: the fetch REQUEST and COVERAGE contract, over the wire ============
# The Rev 10 producer ignored `metrics` and `max_rows`, listed 50 of 60 observed dates
# while reporting zero omissions, and named a metric `<source>.unknown` when the store
# held none. These drive a REAL store through a REAL leaf subprocess and validate the
# complete response without repairing it.

import tempfile
from datetime import date, timedelta

sys.path.insert(0, str(ROOT / "tools"))
import collector as CO, store as STO
from jsonschema import FormatChecker

FC = FormatChecker()
M_A = "git.commits_count"
M_B = "git.lines_changed"


def _defn(metric):
    return {"metric_id": metric, "normalization_version": 1,
            "analysis_timezone": "UTC", "policy": "rev11-wire-test"}


class StoreBackedFetchContract(unittest.TestCase):
    LO = date(2026, 1, 1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mesh-fetch-wire-")
        self.dir = Path(self.tmp.name) / "data"
        self.store = STO.Store(self.dir / "facts.duckdb")
        self.sid = self.store.init(STO.digest("policy", {"p": "rev11"}))
        self.cap = {"source": "git", "retention_status": "unbounded",
                    "retention_days": None, "effective_from": date(2026, 1, 1)}
        self.capid = STO.digest("source_capability", self.cap)
        self.vd = {m: STO.digest("value_definition", _defn(m)) for m in (M_A, M_B)}

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, metric, days, first=None):
        first = first or self.LO
        fr = CO.FetchResult(metric, "ok", observed={
            first + timedelta(days=i): {"value": float(i), "sample_count": 1}
            for i in range(days)})
        self.store.publish(lambda con: CO.apply_fetch(
            con, sid=self.sid, vdid=self.vd[metric], capid=self.capid,
            run_id=f"seed-{metric}", source="git", fr=fr,
            definition=_defn(metric), capability=self.cap))

    def _call(self, args, tool="fetch"):
        p = subprocess.Popen(
            [sys.executable, str(LEAF), "--source", "git", "--store", str(self.dir)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        try:
            p.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": tool,
                            "arguments": {"store_token": self.store.token(), **args},
                            "_meta": meta()}}) + "\n")
            p.stdin.flush()
            return json.loads(read_line(p))["result"]
        finally:
            try:
                p.stdin.close(); p.wait(timeout=5)
            except Exception:
                p.kill()
            for h in (p.stdout, p.stderr):
                try: h.close()
                except Exception: pass

    def _valid(self, sc):
        errs = list(V(C.output_schema("fetch"), format_checker=FC).iter_errors(sc))
        self.assertFalse(errs, errs[0].message[:300] if errs else "")

    def test_row_limit_is_applied_and_truncation_is_reported(self):
        """Rev 10 answered max_rows=1 with 60 rows and truncated=false."""
        self._seed(M_A, 60)
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=59)).isoformat(),
                          "max_rows": 1})
        sc = res["structuredContent"]
        self._valid(sc)
        self.assertEqual(len(sc["observations"]), 1, "max_rows=1 means one row")
        self.assertTrue(sc["truncation"]["truncated"])
        self.assertEqual(sc["truncation"]["truncation_reason"], "row_budget")
        ms = sc["metric_status"][0]
        self.assertEqual(ms["status"], "truncated")
        self.assertEqual(ms["present_dates"], 60)

    def test_date_list_cap_is_counted_not_hidden(self):
        """60 observed dates; DateList holds 50. The other 10 must be COUNTED."""
        self._seed(M_A, 60)
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=59)).isoformat()})
        sc = res["structuredContent"]
        self._valid(sc)
        ms = sc["metric_status"][0]
        self.assertEqual(len(ms["observed_dates"]), 50)
        self.assertEqual(ms["present_dates"], 60)
        self.assertEqual(ms["omitted_dates_count"], 10,
                         "Rev 10 listed 50 of 60 and reported 0 omitted")
        self.assertTrue(sc["truncation"]["truncated"])
        self.assertEqual(sc["truncation"]["truncation_reason"], "date_list_cap")

    def test_requested_metrics_are_honoured(self):
        """Rev 10 returned git.commits_count for an explicit request for another id."""
        self._seed(M_A, 5)
        self._seed(M_B, 5)
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=4)).isoformat(),
                          "metrics": [M_B]})
        sc = res["structuredContent"]
        self._valid(sc)
        self.assertEqual(sorted({o["metric_id"] for o in sc["observations"]}), [M_B])
        self.assertEqual([m["metric_id"] for m in sc["metric_status"]], [M_B])

    def test_multiple_requested_metrics_each_report_their_own_status(self):
        self._seed(M_A, 5)
        self._seed(M_B, 5)
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=4)).isoformat(),
                          "metrics": [M_A, M_B]})
        sc = res["structuredContent"]
        self._valid(sc)
        self.assertEqual(sorted(m["metric_id"] for m in sc["metric_status"]), [M_A, M_B])
        for m in sc["metric_status"]:
            self.assertEqual(m["status"], "ok")
            self.assertEqual(m["value_definition_id"], self.vd[m["metric_id"]])

    def test_an_unknown_requested_metric_is_unsupported_not_invented(self):
        self._seed(M_A, 5)
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=4)).isoformat(),
                          "metrics": ["git.unrequested_metric"]})
        sc = res["structuredContent"]
        self._valid(sc)
        self.assertEqual(sc["observations"], [])
        self.assertEqual([m["metric_id"] for m in sc["metric_status"]],
                         ["git.unrequested_metric"])
        self.assertEqual(sc["metric_status"][0]["status"], "unsupported")
        self.assertIsNone(sc["metric_status"][0]["value_definition_id"])

    def test_uncollected_dates_are_undetermined_not_proven_absent(self):
        """5 collected days inside a 12-day request: the other 7 are UNDETERMINED."""
        self._seed(M_A, 5)
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=11)).isoformat()})
        sc = res["structuredContent"]
        self._valid(sc)
        ms = sc["metric_status"][0]
        self.assertEqual(ms["status"], "partial")
        self.assertEqual(ms["present_dates"], 5)
        self.assertEqual(len(ms["undetermined_dates"]), 7)
        self.assertEqual(ms["known_absent_dates"], [],
                         "an uncollected date is NOT proven absence")

    def test_normalization_version_comes_from_the_recorded_definition(self):
        """Rev 11 emitted a literal 1 on every observation and in the per-source map.
        For git that happened to be correct; for a metric whose definition records 2 it
        would have been a fabricated metadata value."""
        d = dict(_defn(M_A)); d["normalization_version"] = 2
        vd = STO.digest("value_definition", d)
        self.vd[M_A] = vd
        fr = CO.FetchResult(M_A, "ok", observed={
            self.LO + timedelta(days=i): {"value": float(i), "sample_count": 1}
            for i in range(3)})
        self.store.publish(lambda con: CO.apply_fetch(
            con, sid=self.sid, vdid=vd, capid=self.capid, run_id="nv2", source="git",
            fr=fr, definition=d, capability=self.cap))
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=2)).isoformat()})
        sc = res["structuredContent"]
        self._valid(sc)
        self.assertEqual({o["normalization_version"] for o in sc["observations"]}, {2})
        self.assertEqual(sc["normalization_versions"], {"git": 2})

    def test_a_definition_without_a_normalization_version_is_refused(self):
        """Reporting a default here would invent metadata the store does not record."""
        d = {"metric_id": M_A, "analysis_timezone": "UTC"}      # no version recorded
        vd = STO.digest("value_definition", d)
        fr = CO.FetchResult(M_A, "ok",
                            observed={self.LO: {"value": 1.0, "sample_count": 1}})
        self.store.publish(lambda con: CO.apply_fetch(
            con, sid=self.sid, vdid=vd, capid=self.capid, run_id="nv0", source="git",
            fr=fr, definition=d, capability=self.cap))
        res = self._call({"start": self.LO.isoformat(), "end": self.LO.isoformat()})
        self.assertTrue(res["isError"])
        sc = res["structuredContent"]
        self.assertEqual(sc["error_type"], "storage_error")
        self.assertIn("normalization_version", sc["message"])
        self.assertEqual(sc["details"]["cause"], "definition_incomplete")
        self.assertRegex(sc["details"]["store_path_id"], r"^[0-9a-f]{16}$")
        self._valid(sc)

    def test_an_empty_store_says_so_instead_of_inventing_a_metric(self):
        """Rev 10 answered `git.unknown`, a metric that does not exist."""
        res = self._call({"start": self.LO.isoformat(),
                          "end": (self.LO + timedelta(days=4)).isoformat()})
        sc = res["structuredContent"]
        self.assertTrue(res["isError"])
        self.assertEqual(sc["error_type"], "insufficient_evidence")
        self.assertNotIn("unknown", json.dumps(sc.get("details", {})))
        self._valid(sc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
