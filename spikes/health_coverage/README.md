# Health record-presence investigation

These standalone probes investigate the shape of an Apple Health XML export. They
do not import data into the mesh store or produce daily Health metric values.
The production Health adapter remains unimplemented.

Use Python 3.13 with timezone data available. Run the synthetic tests without any
personal data:

```sh
python -m unittest discover -s spikes/health_coverage -p 'test_*.py' -v
```

For a local investigation, keep both the export and results outside this checkout.
Replace the example paths, timezone, and explicit analysis date:

```sh
python spikes/health_coverage/probe.py \
  --archive /absolute/path/outside/repository/export.zip \
  --timezone America/New_York --as-of 2026-01-10 --days 90 \
  > /absolute/path/outside/repository/presence.json

python spikes/health_coverage/verify_presence.py \
  --archive /absolute/path/outside/repository/export.zip \
  --report /absolute/path/outside/repository/presence.json \
  > /absolute/path/outside/repository/verification.json
```

The analysis date itself is excluded. `window.start` and `window.end` are inclusive
civil dates; each interval is separately interpreted as `[start, end)`. An interval
ending at midnight does not contribute to the following day. HRV uses the sample's
start date. Sleep categories are kept separate from time in bed.

The report counts records by day, groups sources under temporary anonymous aliases,
and flags candidate duplicates, interval overlaps, and invalid quantities. No raw
measurement values or source-name mapping are emitted. These aggregates remain
personal metadata; do not publish real reports or use them as public test fixtures.

A zero record count means no matching exported record was found. It is not a
measured zero for steps, HRV, or sleep, and sample presence does not establish
complete observation. `valid_metric_days` remains null. Source aliases do not
establish device identity or Health's source-priority ordering.

The second executable uses a separate SAX parser and date-attribution implementation.
It verifies archive identity, report shape, and every daily record count. Its PASS
does **not** verify overlap flags, source grouping, quantity diagnostics, metric
completeness, or equivalence to Health's displayed summaries.

See the [investigation contract](../../docs/HEALTH_COVERAGE_PLAN.md) for the exact
family definitions, boundary rules, and verification limits. These checks do not
change RC2 acceptance or enable inference.
