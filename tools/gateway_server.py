"""The CLIENT-FACING MCP server. It serves ONE tool: `weekly_report`.

Rev 10 described a gateway but shipped only a Python class, so a client had no entry
point and `weekly_report` was advertised by leaves that do not implement it. The split
is now explicit:

  leaf     `fetch`, `source_status` — store-token-qualified, one source each. Internal.
  gateway  `weekly_report`          — owns the pointer, reads it once, fans out to the
                                      leaves under that one token, and assembles the
                                      report. The client supplies no store_token: the
                                      gateway is the only component that reads the
                                      pointer.

Run:  python tools/gateway_server.py --config mesh.json
"""
import sys, pathlib
from datetime import date
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import contracts as C, mcp_stdio as P, gateway as GW, mesh_config as MC

SERVER_INFO = {"name": "mesh-gateway", "version": GW.GATEWAY_VERSION}
CONFIG_PATH = None
for _i, _a in enumerate(sys.argv):
    if _a == "--config" and _i + 1 < len(sys.argv):
        CONFIG_PATH = sys.argv[_i + 1]


def _weekly_report(args):
    cfg = MC.load(CONFIG_PATH)
    store, leaf, enabled, unavailable = MC.wire(cfg)
    try:
        MC.require_matching_definitions(store, cfg)
    except MC.MeasurementDefinitionChanged as e:
        # A success-looking report must never be produced from facts collected under a
        # different measurement definition.
        return P.payload_result(P.error_payload(
            "invalid_request",
            f"the configured measurement definition does not match the stored facts: {e}",
            "Point store_dir at a new store for the changed configuration, or restore "
            "the previous measurement settings.", False,
            {"metric_id": e.conflicts[0]["metric_id"]}), True)
    gw = GW.Gateway(store, leaf, enabled, config=CONFIG_PATH)
    week_of = date.fromisoformat(args["week_of"]) if args.get("week_of") else None
    try:
        payload = GW.assemble_report(
            gw, as_of=cfg.get("as_of_date") and date.fromisoformat(cfg["as_of_date"])
            or date.today(),
            week_of=week_of, min_meaningful_delta=cfg.get("min_meaningful_delta"),
            unavailable_sources=unavailable)
    except GW.GenerationUnstable as e:
        # `details` is REQUIRED for this error_type, with the current generation and
        # token. Rev 11 RC discarded the observed token and emitted neither.
        observed = e.observed or store.token()
        return P.payload_result(P.error_payload(
            "generation_mismatch", str(e),
            "The store is being published faster than a report can be assembled; "
            "retry when sync is idle.", True,
            {"current_generation": observed["generation"],
             "current_store_token": observed}), True)
    except GW.StoreIdentityMismatch as e:
        observed = e.observed or store.token()
        return P.payload_result(P.error_payload(
            "store_instance_mismatch", str(e),
            "The store was re-created during the report; start a new report.", False,
            {"current_store_token": observed}), True)
    except GW.NoSourcesConfigured as e:
        return P.payload_result(P.error_payload(
            "invalid_request", str(e),
            "Enable at least one source, or declare it disabled with a reason.", False),
            True)
    return P.payload_result(payload, False)


def call_tool_result(name, args):
    if name == "weekly_report":
        return _weekly_report(args)
    raise RuntimeError(f"unreachable: {name}")


SERVER = P.Server(
    name=SERVER_INFO["name"], version=SERVER_INFO["version"],
    instructions=("Descriptive weekly report over the configured local sources. "
                  "Inference, associations and recommendations are DISABLED pending "
                  "calibration. Numbers are means over present days only."),
    implemented=("weekly_report",), call=call_tool_result)


def handle(msg):
    return P.handle(msg, SERVER)


def main():
    P.main(SERVER)


if __name__ == "__main__":
    main()
