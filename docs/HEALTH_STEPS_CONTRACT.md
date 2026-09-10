# Standalone selected-source steps contract

Version: `mesh-selected-steps-v1`. Implements the steps portion of the reviewed
[Health policy](HEALTH_SOURCE_SELECTION_DESIGN.md). This adds private export
observations outside the RC2 store. It does not implement HRV, sleep, completeness,
production ingestion, comparisons, associations, or recommendations.

## Inputs and validation

Python 3.13, standard library only. Input is one local ZIP, the existing catalogue
JSON, its existing selection binding, an explicit IANA timezone key, `as_of`
(`YYYY-MM-DD`), and a positive integer `days`. Reporting dates are inclusively
`as_of - days` through `as_of - 1`; no current-day inclusion or default window.

Validate the entire existing catalogue and binding, not just the steps subset,
against the complete archive under `HEALTH_CATALOGUE_CONTRACT.md`. Require a
`steps` selection; otherwise fail the run with `STEPS_NOT_SELECTED`. Other selected
families may exist in the binding but are not evaluated. Preserve the exact finite
steps group IDs and selection handle. No source selection, fallback, migration,
deduplication, or binding rewrite occurs here.

The primary may reuse the public functions in `health_catalogue/catalogue.py`
for strict JSON, catalogue/request validation and archive recomputation, with a
small evaluator-local validator for the binding envelope and its exact-false flags.
The oracle may use the public `health_catalogue/verify_catalogue.py:verify` gate,
then its own strict reader and evaluation. It must not import/read the primary
evaluator, primary catalogue helpers, primary unit fixtures, or generated expected
results. Do not call private helper APIs in either existing module. Existing
catalogue, presence, and RC2 files are unchanged.

Snapshot the interpreted binding/catalogue bytes once. Compare their hashes after
validation/evaluation, including across any verification call that rereads them.
Every archive pass uses one handle for full before/after hashes, both equal to the
binding's expected archive hash and byte count. A stable second pass of a different
archive is not acceptable. ZIP/member/XML/encoding/external-resource failure,
changed input, bad binding, or unrepresentable window aborts the whole run. Read
the entire XML before publishing any success, even after family failure is known.

## Time and pinned civil days

Timestamp grammar is an ASCII date and time:
`YYYY-MM-DD[ T]HH:MM:SS` followed optionally by `.` and 1–6 ASCII digits,
then either `Z` or a signed offset `HHMM`/`HH:MM`. One ASCII space between the
time and offset is also allowed for Apple's export form. No leading/trailing
whitespace, missing offset, leap seconds, or extra precision. The complete lexical
regex is `[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})? ?(?:Z|[+-][0-9]{2}:?[0-9]{2})`,
matched against the entire string. The optional ASCII space also permits ` Z`.
Years 1–9999,
valid calendar components, and offset hour 00–23/minute 00–59 are required.
Convert to integer UTC microseconds without floating timestamps. Conversion
overflow, missing/empty times, or end before start is time-unattributable.

Resolve the named zone from the first file in `zoneinfo.TZPATH`, with the standard
`tzdata` package resource fallback if installed. Reject absolute/traversing/empty
keys. Read the actual TZif bytes once and construct `ZoneInfo.from_file` from those
bytes. Embed them as canonical base64 and their SHA-256 in the private report.
The oracle reconstructs the zone from those pinned bytes rather than substituting
its host's current rules. It validates key syntax, not equality with a currently
installed file for that key; the pinned bytes are the authoritative replay input.
A timezone label is not proof of where a person was.

A civil day's preimage in UTC determines its boundaries: every instant whose
conversion has that local calendar date. One nonempty contiguous interval is an
ordinary day; an empty preimage is `no_civil_day`. Multiple disjoint intervals,
unrepresentable UTC bounds, or inability to resolve the preimage aborts the run
with `WINDOW_UNREPRESENTABLE`; do not fill the gap with a single invented interval.
Midnight attachment alone is not a proof. Ambiguous midnight uses the earliest
valid boundary; a midnight gap can start the date later; a skipped date is empty.

An exact, simple implementation is allowed: scan integer UTC seconds over the
nominal reporting window padded by 24 hours on each side, classify each second
with the pinned zone, and collect runs of requested dates. TZif offset/transition
units and the UTC boundary grid are integral seconds. The padding bounds possible
UTC offsets (strictly less than 24 hours). Detect disjoint runs; do not merely take
their minimum/maximum. Checked arithmetic must fail if the necessary UTC envelope
cannot be represented. More efficient implementations must preserve these same
semantics. This correctness-first method can cost more than ordinary midnight
conversion; there is no fabricated performance pass. Retain integer microseconds
for record times within these second-aligned boundaries.

Boundary output uses fixed `YYYY-MM-DDTHH:MM:SS.ffffffZ` strings, with four-digit
years, and integer `utc_duration_seconds`. Skipped dates have null start/end and
duration 0. Recompute every boundary independently from the pinned rules.

References: [Python ZoneInfo](https://docs.python.org/3.13/library/zoneinfo.html)
documents file-backed rules and the search path; [TZif RFC 9636](https://www.rfc-editor.org/rfc/rfc9636.html)
defines integer-second transition/offset data. These references do not verify this
implementation or its performance.

## Records and exact numbers

Use exact unqualified `HealthData` and `Record` expanded names. The audit ordinal
is one-based across all Record occurrences. Evaluate only
`HKQuantityTypeIdentifierStepCount`; construct its exact three-slot descriptor
and family group ID under the catalogue contract. Count other Record types only
for the full-archive integrity check; do not interpret their values.

Partition every steps occurrence into:

- `selected`: its group ID is in the verified steps selection;
- `unresolved`: sourceName is missing or whitespace-only (empty included);
- `excluded`: any other named steps source.

Never normalize the descriptor itself. A recognized source with missing version or
device is still a named stream; the binding's app-stream/device assertion is not
promoted to verified hardware identity. Unresolved sources cause the family reason
`selection_unresolved`, regardless of apparent date. A selected record with
unattributable times causes the family reason `selected_time_unattributable`, even
if a different field looks outside the window. Keep reading and diagnosing other
attributable records.

Decimal lexical grammar is ASCII
`[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?`, with no surrounding
whitespace or underscores. Unit must be exactly `count`; value must be finite and
nonnegative. Negative zero is allowed as exact zero. Missing/empty/invalid value
is `invalid_value`; missing/wrong unit is `invalid_unit`. Keep both defect counts.

Operational guard: a steps value token longer than 4096 characters, or a
syntactically valid decimal with an explicit exponent magnitude above 100000,
aborts the entire operation with `INPUT_RESOURCE_LIMIT`. It is not a bad-value
classification, a null daily observation, or an inference criterion. Check before
allocating exponent-sized integers. This explicit guard preserves exact arithmetic
for admitted inputs; it does not round, truncate, or silently exclude large inputs.
The same guard applies to all steps partitions because their defects are counted.

Keep exact decimal values/rationals through summation. No finite Decimal context
may round an intermediate sum. Convert only the final sum to nearest binary64,
ties to even. A positive exact sum rounded to zero, or a result that would become
infinite, yields `numeric_result_out_of_range`. Finite nonzero subnormals are valid;
normalize numeric negative zero to positive zero. The oracle uses independently
implemented accumulation and compares the exact binary64 result, without tolerance.
It may share Python numeric primitives, but never an evaluator helper.

## Assignment, collisions, and repeats

For every partition's time-attributable positive intervals use `[start, end)`. Assign the occurrence
once to each ordinary reporting day it intersects positively. An interval wholly
inside one day, including end equal to closing midnight, is allocatable. Otherwise
every intersected reporting day gets `cross_day_quantity_unallocated` for a selected
record only, even when
part of the interval lies outside the requested window. Do not prorate or clip the
quantity to make it fit. Excluded and unresolved intervals retain their assignments
and counts but cannot add that selected-quantity reason. A zero-duration steps record is a point assigned to its
start's local date.
Use the precomputed UTC day intervals for membership. Valid UTC endpoints entirely
outside the reporting window must not become time-invalid because an unnecessary
conversion to an extreme out-of-range local calendar date overflows.

For selected attributable records, two positive intervals collide when their
intersection inside the day's UTC interval has positive duration. Two points at
the same instant collide; a point at an interval's start or interior collides;
a point at its excluded end does not. Any collision gives
`selected_quantity_overlap`. Include all explicitly selected descriptor groups and
time-valid records with invalid quantities in these checks. Never choose one writer,
subtract overlap, or take a maximum to resolve it.

Repeat key: steps family, exact descriptor group ID, parsed UTC start/end, tagged
raw unit, tagged raw value. Tags preserve missing versus empty. Do not normalize raw
attributes: raw here means the exact XML-parsed string before semantic conversion,
not the original entity/character-reference spelling in the XML byte stream.
Do not normalize raw
number spelling; equivalent time spellings do compare by their parsed UTC instants.
For each scope, `repeat_candidate_extras` is sum(max(group_count - 1, 0)) over
selected time-attributable occurrences. Invalid quantities still participate.
Any daily extras give `selected_repeat_candidate`, alongside overlap and other
independently determinable reasons. Never remove an occurrence from a count or sum
one representative of a repeat group.

Unselected records do not change the selected numeric value. Preserve a separate
selected-versus-excluded collision diagnostic, using the same point/interval rules.
Unresolved records do not enter that named-source pair check; their source ambiguity
is reflected in family state and diagnostic unknowns below.

## Counts, states, and diagnostic uncertainty

Archive counts for each partition have exact keys:
`occurrences`, `time_attributable`, `time_unattributable`, `invalid_occurrences`,
`invalid_time`, `invalid_value`, `invalid_unit`.
Invalid occurrence count is the union of time/value/unit defects, not their sum.
`invalid_time == time_unattributable`; attributable + unattributable == occurrences.
The three archive occurrence counts sum to the catalogue's entire steps count.

Window and daily counts for each partition have exact keys:
`occurrences`, `invalid_occurrences`, `invalid_value`, `invalid_unit`.
Only time-attributable occurrences enter these scopes. Window counts count a record
once if assigned to any reporting date; daily counts count it once per assigned date.
No unattributable record gets a fabricated daily/window count. Window records are a
union, so a cross-day record can contribute to several daily counts but once to the
window. In these scopes invalid occurrence count is the union of value/unit defects.
Skipped dates have zero daily occurrence/defect counts in all partitions.

For each day, overlap diagnostics are true if a known attributable pair witnesses
the collision. Without a witness, `selected_quantity_overlap` is null if there is
any archive-wide selected time-unattributable occurrence or unresolved-source
occurrence, otherwise false. Without a witness, `selected_excluded_overlap` is null
if any selected/excluded occurrence is time-unattributable or any unresolved-source
occurrence exists, otherwise false. This preserves real witnesses despite other
unknown inputs; false is never used to hide an unavailable input. On skipped dates
both diagnostics are false because no instant can belong to that date.

Daily reasons accumulate all determinable conditions, not just the first:

1. A skipped date always has `no_civil_day`, null value, exactly that reason, and
   complete zero-valued count structures, even if the family is blocked. Family reasons remain in the report.
2. On ordinary dates start with the family reasons. Add `invalid_selected_record`
   for selected attributable bad values/units, `cross_day_quantity_unallocated`,
   `selected_quantity_overlap`, and `selected_repeat_candidate` where established.
3. Any reasons mean `blocked` and null value. Otherwise zero selected occurrences
   mean `no_records` and null value. Otherwise compute the exact sum.
4. Range-check only a sum otherwise eligible to compute. Range failure adds
   `numeric_result_out_of_range`, state blocked, value null. Do not manufacture an
   overflow diagnosis by adding records whose allocation or values are ambiguous.

Every daily result has `observation_completeness: "unknown"`,
`valid_metric_day: null`, `inference_eligible: false`. Explicit selected zero values
can compute 0.0 with a positive occurrence count, but absence never becomes zero.
There is no contributing-count field or second success/eligibility flag.

## Exact report wire format

Canonical JSON and strict loading are as in the catalogue contract. Reject duplicate
keys, NaN/Infinity, unknown fields, incorrect types, wrong array order, and corrupt
digests. The output envelope has only `report` and `report_sha256`, the SHA-256 of
canonical report content. The report has exactly these keys:

```text
schema_version: "mesh-steps-report-v1"
policy_version: "mesh-selected-steps-v1"
producer_version: "mesh-steps-evaluator-v1"
metric: "selected_source_recorded_steps"
unit: "count"
archive: {sha256, bytes, xml_member}
binding_sha256: canonical binding digest
selection_handle: the steps UUID from the binding
window: {as_of, days, start, end}             # start=as_of-days; end=as_of-1, both inclusive
timezone: {key, tzif_base64, tzif_sha256}      # canonical base64, no paths
family_reasons: sorted unique reason strings
archive_counts: {selected, excluded, unresolved}
window_counts: {selected, excluded, unresolved}
archive_repeat_candidate_extras: integer
window_repeat_candidate_extras: integer
days: chronological daily rows, exactly one per requested civil date
```

Each daily row has exactly:
`date`, `utc_start`, `utc_end`, `utc_duration_seconds`, `calculation_state`, `value`,
`reasons`, `observation_completeness`, `valid_metric_day`, `inference_eligible`,
`counts`, `repeat_candidate_extras`, `diagnostics`.
`counts` maps the three partitions to the daily-count structure.
`diagnostics` has only `selected_quantity_overlap` and `selected_excluded_overlap`.
All counts, days, bytes, and durations are actual integers, never bools/floats.
Computed values are finite binary64 JSON numbers emitted as Python float values;
the canonical comparison distinguishes negative zero and integer-looking counts
from floats. Other values are null. No timestamps of execution, runtime duration,
raw quantities, source/device descriptors, or filesystem paths enter the report.
The local run record separately retains the exact code commit and commands.

The oracle validates the envelope/digest and recomputes every substantive field
from the archive/binding plus report's explicit window and pinned timezone bytes.
It does not trust supplied day boundaries, counts, reasons, or values. Compare
canonical content exactly. PASS validates this supplied input/window/rule set,
not the user's location, intent, Health app equivalence, or a claimed executable
identity. Runtime versions/commit evidence remain separately recorded by the caller.

## Commands and artifact safety

```text
python spikes/health_steps/evaluate.py --archive ZIP --catalogue JSON --binding JSON --timezone IANA --as-of DATE --days N --output JSON
python spikes/health_steps/verify_steps.py --archive ZIP --catalogue JSON --binding JSON --report JSON
```

Primary stdout is fixed JSON: `{status: PASS, operation: evaluate}` on a successful
report, including reports with blocked/no-record days. This means the report was
produced under the contract, not that all days are computable. Expected failure is
`{status: FAIL, operation: evaluate, error: FIXED_CODE}`, exit 2; unexpected programmer
failures use `INTERNAL_ERROR`, exit 1. No arbitrary input/path/exception text reaches
stdout/stderr. Machine CLI help/usage follows fixed error JSON, as catalogue tools do.
Existing output, checkout output (including symlink aliases), or missing parent is
refused. Create output exclusively with mode 0600; delete a new incomplete artifact
on write failure. Never rewrite inputs or an old report.

Oracle stdout has only `status`, `verification_scope`, `checks`, `diagnostics`.
Scope is `selected_steps_report_exact_recomputation`; PASS exit 0 has checks
`["binding_and_archive", "pinned_timezone_boundaries", "daily_steps_and_diagnostics", "report_identity"]`
and empty diagnostics. Failure exit 2 has empty checks and one fixed diagnostic.
It never writes/repairs the report. It rejects a supplied rehashed but incorrect
report, including unchanged total steps with changed daily distribution/diagnostics.

Both commands may use fixed error codes that distinguish operational classes;
`STEPS_NOT_SELECTED`, `WINDOW_UNREPRESENTABLE`, and `INPUT_RESOURCE_LIMIT` are the
named mandatory run refusals where applicable. Other run-level input failures use
fixed class codes chosen consistently within each tool. A record-level defect
is not a command exception unless it breaches the explicit resource guard.

## Acceptance scope

Use independent synthetic fixtures and expected results in each implementation.
Cover at least: adjacent/overlapping intervals; both point endpoints and duplicate
points; repeats across offset-equivalent timestamps and raw value distinctions;
several bound version groups; selected versus excluded collisions; missing/empty/
whitespace sources; selected bad time apparently outside the window; invalid
selected quantities inside versus outside; full interval crossing the requested
window edge; exact closing midnight; no-record versus explicit zero; multiple
defects plus repeat extras (HSP-25); skipped dates and family errors together;
23/25-hour days and midnight gaps; a disjoint civil-date preimage refusal; exact
fractional sums, binary64 midpoint, 1e400 overflow, 5e-324 subnormal and 1e-400
underflow; grammar/resource limits; whole-document malformed tail and ZIP formats;
stale/mutated inputs and bindings without steps; output safety; deterministic rerun;
read-only verification refusing rehashed incorrect value/state/count/boundary/reason.

Root integration drives actual public commands through one invented archive and
compares the complete report with the independent oracle, then demonstrates tamper
refusal. Only reviewed code runs on the private supplied archive. The initial real
window reuses the earlier private presence investigation's dates and timezone;
record those operating inputs in private evidence, not this public contract. They
are not eligibility thresholds. Keep all real results and selection metadata local.
