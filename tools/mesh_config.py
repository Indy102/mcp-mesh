"""The mesh configuration file: the ONE place a deployment states what it collects.

    {
      "store_dir": "data",
      "analysis_timezone": "America/Indiana/Indianapolis",
      "max_staleness_days": 2,
      "min_meaningful_delta": 1.0,
      "sources": {
        "git":    {"enabled": true,  "repos": ["/path/to/repo"],
                   "author_emails": ["me@example.com"]},
        "health": {"enabled": false, "unavailable_reason": "no adapter is built"}
      }
    }

Paths are resolved relative to the configuration file. Nothing here has a value-changing
default: a source that does not state its measurement inputs is a configuration error,
never a silently-assumed setting.
"""
import json, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import git_source as _git

REQUIRED = ("store_dir", "analysis_timezone", "sources")
SOURCE_INPUTS = {"git": ("repos", "author_emails")}

# The adapter registry. A source without an adapter cannot be enabled.
ADAPTERS = {"git": _git}


class ConfigError(ValueError):
    pass


class MeasurementDefinitionChanged(ConfigError):
    """The configured MEASUREMENT definition differs from the one the store's facts
    were collected under.

    This is not a settings change: `author_emails`, `repos` and `analysis_timezone`
    determine what every stored number MEANS. Rev 11 RC registered the new definition
    but never activated it, so a changed-author sync reported success while every
    report kept using the old author's numbers. For this pilot the change is REFUSED
    before anything is written; rederiving history under a new definition is a
    migration feature that is deliberately not in this milestone.
    """
    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__("; ".join(
            f"{c['metric_id']}: stored definition {c['stored'][:12]}… != configured "
            f"{c['configured'][:12]}… (differs in {', '.join(c['differing_fields'])})"
            for c in conflicts))


def load(path):
    if not path:
        raise ConfigError("no --config was supplied")
    p = pathlib.Path(path).resolve()
    cfg = json.loads(p.read_text())
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ConfigError(f"{p}: missing {missing}")
    if not cfg["sources"]:
        raise ConfigError(f"{p}: `sources` is empty; a mesh that collects nothing "
                          f"cannot produce a report")
    cfg["_dir"] = p.parent
    cfg["store_dir"] = str((p.parent / cfg["store_dir"]).resolve())
    for name, sc in cfg["sources"].items():
        if not sc.get("enabled"):
            if not sc.get("unavailable_reason"):
                raise ConfigError(
                    f"{p}: disabled source {name!r} must state unavailable_reason")
            continue
        for k in SOURCE_INPUTS.get(name, ()):
            if not sc.get(k):
                raise ConfigError(f"{p}: enabled source {name!r} must state {k}")
        if name == "git":
            sc["repos"] = [str((p.parent / r).resolve()) for r in sc["repos"]]
    return cfg


def enabled_sources(cfg):
    return sorted(n for n, sc in cfg["sources"].items() if sc.get("enabled"))


def unavailable_sources(cfg):
    return [(n, sc["unavailable_reason"])
            for n, sc in sorted(cfg["sources"].items()) if not sc.get("enabled")]


def identities(cfg, source):
    """(definition, value_definition_id, capability, source_capability_id)."""
    import store as ST
    from datetime import date
    sc = cfg["sources"][source]
    adapter = ADAPTERS.get(source)
    if adapter is None:
        raise ConfigError(f"no adapter is built for source {source!r}")
    definition = adapter.value_definition(
        author_emails=sc["author_emails"], repos=sc["repos"],
        analysis_timezone=cfg["analysis_timezone"])
    capability = {"source": source, "retention_status": "unbounded",
                  "retention_days": None,
                  "effective_from": date.fromisoformat(
                      sc.get("capability_effective_from", "2000-01-01"))}
    return (definition, ST.digest("value_definition", definition),
            capability, ST.digest("source_capability", capability))


def definition_conflicts(store, cfg):
    """Compare the CONFIGURED measurement definition against the store's ACTIVE one.

    Report-only settings (`min_meaningful_delta`, `max_staleness_days`) are a separate
    policy and never appear here: they do not enter the value definition, so they
    cannot produce a conflict.

    Returns [] when the store is uninitialised or holds no active definition for a
    configured metric — a first sync is not a change.
    """
    import json as _json
    if not store.pointer.exists():
        return []
    con, _ = store.open_snapshot()
    try:
        active = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT a.metric_id, a.value_definition_id, d.definition_json "
            "FROM metric_active_definition a JOIN metric_definitions d "
            "ON d.metric_id=a.metric_id "
            "AND d.value_definition_id=a.value_definition_id").fetchall()}
    finally:
        con.close()
    conflicts = []
    for source in enabled_sources(cfg):
        definition, vdid, _, _ = identities(cfg, source)
        metric = ADAPTERS[source].METRIC
        if metric not in active:
            continue
        stored_id, stored_json = active[metric]
        if stored_id == vdid:
            continue
        stored = _json.loads(stored_json)
        differing = sorted(k for k in set(stored) | set(definition)
                           if stored.get(k) != definition.get(k))
        conflicts.append({"source": source, "metric_id": metric, "stored": stored_id,
                          "configured": vdid, "differing_fields": differing,
                          "stored_definition": stored, "configured_definition": definition})
    return conflicts


def require_matching_definitions(store, cfg):
    """Raise BEFORE any write or any success-looking report."""
    conflicts = definition_conflicts(store, cfg)
    if conflicts:
        raise MeasurementDefinitionChanged(conflicts)


def wire(cfg):
    """(store, leaf_path, enabled_sources, unavailable_sources)."""
    import store as ST
    root = pathlib.Path(__file__).resolve().parent
    return (ST.Store(pathlib.Path(cfg["store_dir"]) / "facts.duckdb"),
            root / "stdio_leaf.py",
            enabled_sources(cfg), unavailable_sources(cfg))
