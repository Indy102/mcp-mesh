"""`mesh` — the runnable workflow. This is the supported entry point.

    python tools/mesh.py init   --config mesh.json
    python tools/mesh.py sync   --config mesh.json [--as-of YYYY-MM-DD]
    python tools/mesh.py report --config mesh.json [--as-of YYYY-MM-DD] [--week-of DATE]
    python tools/mesh.py serve  --config mesh.json      # client-facing MCP over stdio

`sync` runs the adapters and publishes ONE new generation containing every source's
result, recording each attempt — successful or failed — in `collection_runs`. A failed
source preserves the facts a previous run wrote; it never deletes them and never
publishes a complete-looking subtotal.

`report` goes through the SAME transport a client uses: it starts the leaves, reads the
pointer once, and assembles the report from what the leaves answer under that one token.
It does not read the database.
"""
import argparse, json, sys, pathlib, uuid
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import collector as CO, git_source as GS, gateway as GW, mesh_config as MC
import report as RP, store as ST


ADAPTERS = MC.ADAPTERS
EXIT_OK, EXIT_SOURCE_FAILED, EXIT_CONFIG_REFUSED = 0, 2, 3


def _identities(cfg, source):
    return MC.identities(cfg, source)


def _refuse_changed_measurement(store, cfg):
    """Print an actionable refusal and return the exit code, or None to proceed.

    Checked BEFORE any write and before any success-looking report. The store is left
    exactly as it was.
    """
    try:
        MC.require_matching_definitions(store, cfg)
    except MC.MeasurementDefinitionChanged as e:
        print(json.dumps({
            "error": "measurement_definition_changed",
            "message": str(e),
            "remediation": (
                "The stored facts were collected under a different measurement "
                "definition. This pilot does not rederive history: point `store_dir` at "
                "a NEW store for the changed configuration, or restore the previous "
                "measurement settings. Report-only settings "
                "(min_meaningful_delta, max_staleness_days) never trigger this."),
            "conflicts": [{k: c[k] for k in
                           ("source", "metric_id", "stored", "configured",
                            "differing_fields")} for c in e.conflicts],
            "store_unchanged": True,
            "token": store.token() if store.pointer.exists() else None},
            indent=1, default=str))
        return EXIT_CONFIG_REFUSED
    return None


def cmd_init(cfg, _args):
    store = ST.Store(pathlib.Path(cfg["store_dir"]) / "facts.duckdb")
    sid = store.init(ST.digest("policy", RP.report_policy()), exist_ok=True)
    print(json.dumps({"store_instance_id": sid, "token": store.token()}, default=str))
    return 0


def cmd_sync(cfg, args):
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    store = ST.Store(pathlib.Path(cfg["store_dir"]) / "facts.duckdb")
    store.init(ST.digest("policy", RP.report_policy()), exist_ok=True)
    refused = _refuse_changed_measurement(store, cfg)
    if refused is not None:
        return refused
    lo = RP.baseline_span(RP.default_week(as_of)[0])
    hi = as_of
    # UNIQUE PER ATTEMPT. A deterministic run id let a later failed attempt replace the
    # earlier successful one, erasing the evidence of the last successful sync.
    run_id = f"sync-{as_of.isoformat()}-{uuid.uuid4().hex[:12]}"
    outcomes = {}

    def mutate(con):
        for source in MC.enabled_sources(cfg):
            definition, vdid, capability, capid = _identities(cfg, source)
            sc = cfg["sources"][source]
            fr, provenance = ADAPTERS[source].fetch_result(
                repos=sc["repos"], author_emails=sc["author_emails"],
                analysis_timezone=cfg["analysis_timezone"], start=lo, end=hi,
                FetchResult=CO.FetchResult)
            CO.apply_fetch(
                con, sid=store.token()["store_instance_id"], vdid=vdid, capid=capid,
                run_id=run_id, source=source, fr=fr,
                definition=definition if fr.status != "failed" else None,
                capability=capability if fr.status != "failed" else None,
                provenance=provenance,
                snapshot_id=(provenance or {}).get("snapshot_id"))
            outcomes[source] = {"status": fr.status, "reason": fr.reason,
                                "snapshot_id": (provenance or {}).get("snapshot_id")}
            if fr.status != "failed":
                # The denormalised copy takes the RUN's own timestamp. Calling utcnow()
                # a second time makes it microseconds newer than every recorded run,
                # which is exactly the inconsistency the integrity check forbids.
                run_at = con.execute(
                    "SELECT recorded_at FROM collection_runs WHERE run_id=? AND "
                    "source=? AND metric_id=?",
                    [run_id, source, ADAPTERS[source].METRIC]).fetchone()[0]
                con.execute(
                    "INSERT OR REPLACE INTO source_state VALUES (?,?,?,?,?,?,?,?)",
                    [store.token()["store_instance_id"], source, capid, None,
                     "unbounded", run_at, run_at, ADAPTERS[source].METRIC])

    token = store.publish(mutate)
    reclaimed = store.reclaim()
    print(json.dumps({"run_id": run_id, "token": token, "reclaimed": len(reclaimed),
                      "window":
                      {"start": lo.isoformat(), "end": hi.isoformat()},
                      "sources": outcomes}, default=str))
    return (EXIT_OK if all(o["status"] != "failed" for o in outcomes.values())
            else EXIT_SOURCE_FAILED)


def cmd_report(cfg, args):
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    store, leaf, enabled, unavailable = MC.wire(cfg)
    refused = _refuse_changed_measurement(store, cfg)
    if refused is not None:
        return refused
    gw = GW.Gateway(store, leaf, enabled, config=args.config)
    payload = GW.assemble_report(
        gw, as_of=as_of,
        week_of=date.fromisoformat(args.week_of) if args.week_of else None,
        min_meaningful_delta=cfg.get("min_meaningful_delta"),
        unavailable_sources=unavailable)
    print(json.dumps(payload, indent=1, sort_keys=True))
    return 0


def cmd_serve(cfg, args):
    import gateway_server
    gateway_server.CONFIG_PATH = args.config
    gateway_server.main()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mesh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "sync", "report", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name in ("sync", "report"):
            p.add_argument("--as-of", dest="as_of", default=None)
        if name == "report":
            p.add_argument("--week-of", dest="week_of", default=None)
    args = ap.parse_args(argv)
    cfg = MC.load(args.config)
    return {"init": cmd_init, "sync": cmd_sync, "report": cmd_report,
            "serve": cmd_serve}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
