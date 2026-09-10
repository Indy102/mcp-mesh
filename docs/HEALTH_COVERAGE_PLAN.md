# Health record-presence feasibility check

This bounded investigation follows the user's authorization to proceed after the
Git-only publication. It adds standalone spike code and synthetic tests, not a
production Health adapter. Accepted RC2 core behavior remains unchanged.

## Scope and constraints

- Do not change `tools/`, `tests/`, `schemas/`, `pyproject.toml`, or `uv.lock`.
- Add standalone stdlib probes/tests under `spikes/health_coverage/` and generic
  documentation. Keep actual archive inputs and every real-source result outside
  this public checkout and its Git history. Never write to the input archive.
- Stream the unique `export.xml` member of a Health ZIP, without extraction or CDA
  ingestion. Record the archive checksum/size and the chosen reporting parameters.
- Emit record-presence diagnostics, never daily health-value totals, inference,
  source-priority decisions, or complete-observation claims. No missing date becomes
  a measured zero. A zero record count means no matching exported record was found.
- Names, devices, source versions, notes, locations, and raw measured values must
  not appear in output. Anonymous source groups are metadata, not stable identities.
- Fail on malformed XML, ambiguous archive membership, or invalid CLI arguments;
  do not emit a successful partial result. Invalid records are counted explicitly.

## Task 1 — streaming presence and overlap probe

Create `spikes/health_coverage/probe.py` and `test_probe.py`. CLI:

```text
python spikes/health_coverage/probe.py --archive FILE --timezone IANA --as-of YYYY-MM-DD --days 90
```

`days` is a positive integer. The inclusive reporting dates are `as_of - days`
through `as_of - 1 day`: only completed civil dates relative to the explicit
`as_of`. There is no inference from the current clock or export timestamp. Use
timezone-aware timestamps; compare instants in UTC, including during DST folds.

Five fixed families:

- `steps`: `HKQuantityTypeIdentifierStepCount`.
- `hrv_sdnn`: `HKQuantityTypeIdentifierHeartRateVariabilitySDNN`.
- `sleep_asleep`: SleepAnalysis values Asleep, AsleepUnspecified, AsleepCore,
  AsleepDeep, AsleepREM, each with its full `HKCategoryValueSleepAnalysis` prefix.
- `sleep_in_bed`: SleepAnalysis InBed.
- `sleep_awake`: SleepAnalysis Awake.

Unknown sleep values are counted explicitly without echoing their text or assigning
them to a known family. Sleep categories remain separate; expected InBed-versus-stage
overlap is not mixed into the asleep-family diagnostic.

Daily attribution is investigative, not the eventual sleep-session normalization:
HRV uses the start instant's local date. Positive-length steps and sleep intervals
contribute presence to each civil day intersecting `[start,end)`. A zero-length step
sample contributes presence on its start date; zero-length sleep is invalid. An end
before start or missing/unzoned/unparseable timestamp is invalid for every family.
Intervals ending exactly at midnight do not contribute to the following day.

Group by exact XML `sourceName`, assigning `source-001`, `source-002`, etc. in first
encounter order. Emit no reverse mapping or hash of names. Missing/empty source names
must be counted and assigned an explicit `unknown-source` group. These groups are not
device identity or an inferred priority order and may change on another export.

For each family, report whole-export record count, invalid-time record counts,
unique input records intersecting the window, and every reporting date with:
`date`, `time_valid_record_count`, `source_counts`, `duplicate_candidate_count`,
`within_source_overlap`, `cross_source_overlap`, `quantity_issue_count`.
`source_counts` maps anonymous group IDs to time-valid record counts and sums to the
daily count. HRV overlap fields are null (discrete start-time attribution); interval
families use booleans. Empty interval-family dates have false overlap flags, which
describes the empty exported record set rather than complete observation.

A duplicate candidate has equal family, source group, UTC start/end instants, unit
text, and raw value/category text. Count extra occurrences per day. This is candidate
equality, not established record identity. Remove these candidate repeats only for
overlap diagnostics; never rewrite source records. On remaining positive-length
intervals, strict positive overlap within one source and across different sources
is flagged separately per day. Equal endpoints are not overlap. Do not interpret
cross-source overlap as automatically erroneous or as proof of duplicate values.

Quantity issues (steps/HRV only): unexpected unit (`count` / `ms`), missing or
non-numeric/non-finite/negative value. Count a record once per day if any issue
applies; do not echo values or filter it out of time-valid presence. Sleep issue
count is zero/not-applicable. Presence therefore does not imply value validity.

Output JSON has `spike_schema: mesh-health-presence-v1`, `status: MEASURED`,
`archive_sha256`, `archive_bytes`, `parameters` (`timezone`, `as_of`, `days`),
`window` (`start`, `end`), `families` keyed by the five names with `daily` lists,
and explicit limitations. For each family include `valid_metric_days: null`,
`days_with_records`, and `no_record_days`; these two counts must sum to `days`.
Include capture timestamp and measured runtime/memory scope without a performance
acceptance verdict. No declared record coverage may be used to enable inference.

Focused synthetic ZIP tests must cover timezone offset conversion, midnight
half-open boundaries, spring/fall DST days, HRV and step point samples, current-day
exclusion, duplicate candidates, cross/within-source overlaps, separate sleep
categories, unknown/missing source handling, bad dates/values, and malformed ZIP/XML.
Test observable outcomes, not copies of helper implementation logic.

## Task 2 — independently recomputed daily counts

Create `spikes/health_coverage/verify_presence.py` and `test_verify_presence.py`.
CLI takes `--archive FILE --report FILE`. It must not import the probe or mesh
implementation. Use a separate streaming XML parser (SAX) and independent date
attribution implementation to recompute each family's time-valid daily record count
for every date in the report window. Use parameters in the report and validate that
the expected window and exact family/date sets are present with no duplicate dates.
Verify archive checksum and byte size, compare every date/count, and fail nonzero on
tampering, malformed input or any mismatch. Return structured PASS/FAIL diagnostics
without raw attributes/values or source names.

This checker independently verifies daily counts only, not overlap classification,
anonymous source grouping, quantity validation, metric completeness, or normalization.
State that boundary in its output. Tests use hand-built reports/ZIPs and require
rejection of a changed count/hash, malformed/missing/duplicate report dates, and
correct boundary/DST attribution. Do not import Task 1 to generate expectations.

## Review and real-source execution

Each task is independently reviewed against these criteria; root records any
interpretation decisions explicitly. After focused tests pass, run both probes on
the supplied archive using the agreed analysis timezone and 90 completed dates.
Store real results only in the separate local evidence workspace. Review all counts
and diagnostic limitations before deciding the next adapter work. A broad final
review and public-content check precede any push of code/documentation.

Sources informing the distinction between presence and completeness:
[Apple steps](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/stepcount),
[Apple sleep categories](https://developer.apple.com/documentation/healthkit/hkcategoryvaluesleepanalysis),
[Apple HRV SDNN](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/heartratevariabilitysdnn),
[Apple source priority](https://support.apple.com/en-us/108779).
