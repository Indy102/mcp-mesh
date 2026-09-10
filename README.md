# mcp-mesh

Local Git history becomes daily facts, versioned DuckDB snapshots, and descriptive
weekly reports served through an MCP gateway. The project addresses a practical
problem: a number is hard to trust when its source, measurement definition, missing
days, or collection failures are hidden.

**The Git-only descriptive milestone is accepted. The overall project is DRAFT.**
Git is the only implemented source adapter. Health, Calendar, Screen Time, and
weather adapters remain unbuilt; inference, associations, and recommendations are
disabled. See [project status](docs/STATUS.md) for the remaining gates.

This public portfolio repository starts with new history. Its core code, tests,
schemas, and dependency files are byte-identical to the accepted RC2 core. Public
documentation and distribution packaging are new; this snapshot is a new
distribution, not the original accepted archive. Private development history,
personal data, and local evidence are excluded. See the [public-copy verification](docs/VERIFICATION.md) and [third-party notices](docs/THIRD_PARTY.md).

A separate [Health observation preparation experiment](docs/HEALTH_ADAPTER_D1.md)
provides private, freshly replayed batches without production Health registration.

## Run the validation

From the repository root, use Git, `uv`, and Python 3.13, the accepted milestone's
runtime. Project metadata permits Python 3.11 and later; acceptance does not establish
compatibility with every interpreter or platform.

```sh
uv sync --locked --python 3.13
uv run --locked python tools/validate_contracts.py
uv run --locked python tools/run_all.py
```

`--locked` uses the checked-in dependency resolution and refuses a stale lockfile.
The contract validator checks delivered artifacts without regenerating or repairing
them. The full runner starts with that same check, then runs integrity, semantics,
database, transport, generation, and Git workflow tests. Tests use synthetic fixtures
and temporary repositories. T7 source/platform feasibility, T8 crash verification,
and T9 statistical calibration are outside this runner's executed tiers.

## Report on your local repository

Create `mesh.json` in this repository's root. Replace the fictional repository path
and email below. Paths resolve relative to the configuration file; `data` is the
local store directory. The example explicitly states the measurement inputs,
freshness policy, descriptive threshold, and unavailable sources.

```json
{
  "store_dir": "data",
  "analysis_timezone": "UTC",
  "max_staleness_days": 2,
  "min_meaningful_delta": 1.0,
  "sources": {
    "git": {
      "enabled": true,
      "repos": ["/absolute/path/to/your/repository"],
      "author_emails": ["developer@example.com"]
    },
    "health": {
      "enabled": false,
      "unavailable_reason": "Health adapter is not implemented"
    },
    "calendar": {
      "enabled": false,
      "unavailable_reason": "Calendar adapter is not implemented"
    },
    "screentime": {
      "enabled": false,
      "unavailable_reason": "Screen Time adapter is not implemented"
    },
    "weather": {
      "enabled": false,
      "unavailable_reason": "Weather adapter is not implemented"
    }
  }
}
```

The author allowlist uses exact email matching. Check the repository's author
spellings locally and include every address you intend to count:

```sh
git -C /absolute/path/to/your/repository log --format='%ae' | sort -u
```

Then initialize, collect, and report:

```sh
uv run --locked python tools/mesh.py init --config mesh.json
uv run --locked python tools/mesh.py sync --config mesh.json
uv run --locked python tools/mesh.py report --config mesh.json
```

`report` prints a JSON report assembled through the gateway and actual stdio leaf
transport. It includes coverage, availability, collection status, and snapshot
identity. The default target is the last complete ISO week before the run's `as_of`
date. `sync` and `report` accept `--as-of YYYY-MM-DD`; `report` also accepts
`--week-of YYYY-MM-DD`.

Changing `author_emails`, canonical repository paths, or `analysis_timezone` changes
the meaning of stored values. Once facts exist, a conflicting `sync` or `report`
refuses with exit code `3` before writing. Use a new `store_dir` for the changed
definition, or restore the previous settings. `max_staleness_days` and
`min_meaningful_delta` are report policies and do not cause that refusal.

| Exit code | Meaning |
| --- | --- |
| `0` | Command succeeded. A report can still disclose insufficient data or an unavailable source. |
| `2` | A configured source failed during sync. Prior facts are preserved and the failed attempt is recorded. |
| `3` | A changed measurement definition was refused. |

## What is counted

The metric is `git.commits_count`: non-merge commits reachable from each configured
repository's HEAD object ID captured at collection time. Matching is by exact
author email. Commits are deduplicated by object ID across all configured
repositories, then assigned to a civil day using the **author date** converted to
the analysis timezone.

Collection requires full local history. Shallow/partial histories, grafts, and
replace refs are refused; the collector does not fetch from the network. If any
configured repository fails, the run produces no complete-looking subtotal. A
successfully observed day with no qualifying commits is zero; an unobserved day
remains missing. Commit counts describe this history and do not measure
productivity, effort, or quality.

A weekly value is the mean over present days. The denominator is the number of
present days: seven for a complete week, six for six present days, or five for five.
Missing days are excluded. A valid week needs at least five present days; its
baseline uses the preceding eight weeks and needs at least six valid weeks.

The report compares the week with the baseline median using `1.4826 × MAD`
dispersion. If dispersion is zero, `delta_z` is null and `meaningful` is false,
even when the raw change is large. This unchanged rule limits usefulness for
sparse histories. The `meaningful` field is a descriptive threshold result, not
statistical significance or a recommendation.

## Serve the MCP gateway

```sh
uv run --locked python tools/mesh.py serve --config mesh.json
```

The gateway advertises `weekly_report`. Its internal Git leaf advertises `fetch`
and `source_status`. The gateway selects a store token for each report attempt;
the client supplies no store token. Leaves also read the store pointer to verify
that they open the requested snapshot.

The tested transport is a stateless stdio profile pinned to `2026-07-28`, with
newline-delimited JSON-RPC frames and `server/discover`, `tools/list`, and
`tools/call`. Requests carry protocol version and client capabilities in
`params._meta`; this profile has no `initialize` handshake. See the
[protocol provenance](schemas/mcp-official-2026-07-28.provenance.json) and
[architecture](docs/ARCHITECTURE.md). The pin and tests cover this implemented
profile; they do not establish compatibility with every MCP client or conformance
to every MCP feature.

The store is local, single-machine, and single-writer. There is no authentication
layer or hosted service. [Architecture](docs/ARCHITECTURE.md) explains snapshot
consistency, provenance, and the boundaries between runtime tools and broader
contract fixtures.

## Source feasibility work

The standalone [Health record-presence probes](spikes/health_coverage/README.md)
investigate daily sample presence and overlap using synthetic tests and an
independent count checker. They do not produce Health metric totals or enable a
Health adapter. Real exports and investigation results stay outside this public
repository.

The [private Health source catalogue](spikes/health_catalogue/README.md) inventories
exact exported source descriptors and validates user-selected groups against one
archive. An independent checker verifies catalogue content and optional selection
membership. It does not establish device identity, choose sources, or calculate
Health values. Real catalogues and selection records stay outside this checkout.

The [standalone steps evaluator](spikes/health_steps/README.md) applies an explicit
source binding to recorded step quantities. It preserves ambiguity and missing
observations, and a separate checker recomputes each daily result from the archive
and pinned timezone rules. Its private reports do not establish complete activity
days or enable a production Health adapter or inference.

The [proposed Health adapter boundary](docs/superpowers/specs/2026-09-10-health-observation-adapter-design.md)
keeps verified export observations separate from complete-day metrics. It defines
immutable batches, explicit uncertainty, and admission checks before a future
adapter or observation store is built. This design does not enable Health in sync,
weekly comparisons, or inference.
