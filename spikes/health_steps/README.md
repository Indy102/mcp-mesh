# Selected-source recorded steps

These standalone tools calculate recorded steps from one explicitly bound source
in one Health export. They create a private report outside the mesh store. They
do not reproduce Health's merged totals or establish complete days of observation.

First build and verify the [private source catalogue and selection](../health_catalogue/README.md).
Use a binding that selects steps. Keep the archive, catalogue, binding, and reports
in a private directory outside this checkout. The output directory must exist;
the evaluator creates a new file with mode 0600 and refuses existing outputs.

Using Python 3.13, with an IANA timezone database available:

```sh
python spikes/health_steps/evaluate.py \
  --archive /absolute/private/export.zip \
  --catalogue /absolute/private/catalogue.json \
  --binding /absolute/private/selection-binding.json \
  --timezone America/New_York --as-of 2026-01-10 --days 7 \
  --output /absolute/private/steps-report.json

python spikes/health_steps/verify_steps.py \
  --archive /absolute/private/export.zip \
  --catalogue /absolute/private/catalogue.json \
  --binding /absolute/private/selection-binding.json \
  --report /absolute/private/steps-report.json
```

The date named by `--as-of` is excluded. The report preserves the timezone rule
bytes and actual UTC boundaries so the independent checker can replay those rules
even if its host has a different timezone database. Exact civil-day resolution can
be slower than attaching a timezone to midnight; it must handle gaps, repeated
times, and skipped dates correctly.

Each date is `computed`, `blocked`, `no_records`, or `no_civil_day`. Computed values
sum the selected source's unambiguous exported quantities, rounding only once at
final output. Overlaps, repeated observations, invalid selected data, and quantities
crossing day boundaries can block a day. Missing selected records produce null,
with no phone fallback. An explicit zero sample is distinct from missing data.
Every day's observation completeness remains unknown and inference remains disabled.

The checker independently recomputes every daily number, state, reason, count,
diagnostic, and boundary. It refuses altered reports even when their digests have
been recomputed, and never repairs an input. Both paths use Python numeric and
timezone primitives; independent application logic does not establish that those
shared libraries are infallible. Verification also does not establish user intent,
hardware identity, or equivalence with Health's app summaries.

Run invented-data tests without a personal export:

```sh
python -m unittest discover -s spikes/health_steps -p 'test_*.py' -v
```

Command success means a contract-valid report was produced or checked; a report
may still contain blocked or absent dates. Expected command failures exit 2 with
fixed safe diagnostics. Resource-limit refusal aborts the run without substituting
partial values. Full semantics and resource limits are in the
[steps contract](../../docs/HEALTH_STEPS_CONTRACT.md); this is the steps portion of
the [Health source policy](../../docs/HEALTH_SOURCE_SELECTION_DESIGN.md), with no
production adapter, HRV/sleep calculation, or inference integration.
