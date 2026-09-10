# Health source selection and overlap policy

Date: 2026-09-10. Status: **RECOMMENDED DESIGN, not implemented or accepted**.
Scope: XML-export analysis, before production Health adapter integration.

This document defines a reproducible, conservative interpretation of exported
records. It does not establish Apple Health summary equivalence, full-day
observation, valid sleep nights, or inference eligibility. Accepted RC2 remains
frozen. No existing metric, schema, report, threshold, or store is changed.

## 1. Decision and alternatives

Use **one explicitly selected source per metric family**, with no automatic
fallback or cross-source aggregation. The selected source may differ for steps,
HRV SDNN, and sleep. A source preference is an operating input; data quantity,
apparent plausibility, desired statistical result, and export order never select it.
All sleep categories share the same selection; do not mix an app's InBed data with
another source's asleep or awake records.

This next-milestone recommendation supersedes the unbuilt historical defaults of
Watch-first source priority, generic tuple deduplication, quantity prorating,
30-minute sleep-session joining, and automatic full-archive replacement. Those
rules are not inherited from earlier draft revisions. No accepted RC2 behavior
implemented them, and no old specification or evidence is edited.

| Approach | Result and trade-off | Decision |
|---|---|---|
| Explicit source in XML | Auditable observations from the supplied archive; can omit data that Health merges from other sources | Recommended for this export milestone |
| Priority-based merging of XML samples | Requires a new allocation algorithm, uncertain source identity, and an assumption about steps inside overlapping intervals | Do not implement in this milestone |
| Native HealthKit statistics queries | Uses Apple's supported statistical aggregation path; requires a separately authorized native integration and comparison evidence | Appropriate future path if Health-summary equivalence is required |

Apple documents user-configurable source priority. That setting does not tell us
the complete sample-merging algorithm or establish that a ZIP preserves it.
[Apple source management](https://support.apple.com/en-us/108779).
Apple DTS specifically recommends `HKStatisticsQuery` to avoid double-counting
steps from multiple sources. That guidance is for HealthKit queries, not a license
to claim equivalent results from summing XML samples.
[Apple DTS response](https://developer.apple.com/forums/thread/759709).

## 2. Source binding: make the selection reviewable

The existing probe's `source-001` labels are temporary name-equality groups. They
are not source IDs, device identities, priorities, or valid configuration keys.

The next deliverable must produce a **local source catalogue**, without measurement
values, grouping the entire archive's records by type into `steps`, `hrv_sdnn`,
and `sleep`, then by the exact parsed XML
attribute tuple `(sourceName, sourceVersion, device)`. Preserve missing versus empty
attributes and case. This is an exported descriptor group, not a claim that it
uniquely identifies hardware. Do not parse a device description as a stable Apple
identifier, strip parts to make devices appear equal, or infer identity from a
display name. The raw descriptor may include volatile text.

`steps` and `hrv_sdnn` use the same exact quantity type identifiers as the presence
probe. `sleep` includes **every** `HKCategoryTypeIdentifierSleepAnalysis` record,
before examining the category value, including unknown or missing values. It is
one selection family; the three output sleep statistics are not three bindings.
Other record types are outside this policy. The inventory includes time-invalid
and quantity-invalid records; it is not filtered by the analysis window or values.

Keep this catalogue, all descriptor values, and every binding outside the public
checkout. A user-facing local catalogue may display source/device descriptions to
the owner; no such strings belong in ordinary logs, public fixtures, or CI. Public
examples use invented descriptors. Hashing a real descriptor does not make it safe
to publish.

A binding records:

- Archive SHA-256 and member identity; catalogue version and digest.
- Metric family; an opaque, locally assigned selection handle.
- The explicit finite set of descriptor groups selected, with no wildcards.
- Whether the selection means one app's exported stream or one identified device;
  the user's stated choice and the local evidence supporting that interpretation.
- An explicit statement that no Health source-priority order or cross-export
  device continuity has been established by this catalogue.

Minimum binding-validation contract: the member identity is the archive digest
plus exact unique ZIP member path. Select a **nonempty** set of distinct descriptor
groups belonging to that family's catalogue. Reject unknown groups, duplicates,
cross-family entries, or a stale archive/catalogue identity before any calculation
in the requested run. No special absent-source or wildcard binding is supported.
An archive-known selected group may have zero records within the requested window.

Deliverable 1 must freeze a versioned canonical catalogue representation and digest
before implementation. Its canonical content must include archive/member identity,
family, descriptor tuple, and whole-archive occurrence count; it must exclude
capture time and encounter-order handles. Represent each attribute as either
`["missing"]` or `["present", "exact value"]`, so an empty value is distinct.
Deterministically sort groups by canonical descriptor content. The binding must
refer to and validate exact content, not trust a supplied digest without recomputing
the catalogue against the archive. The catalogue's wire encoding is deliverable 1's
small contract; no current schema is claimed to validate it.

Several descriptor groups, such as source-version variants, may form one selection
only after explicit review establishes that they fit the chosen source scope.
Do not silently merge names, OS versions, replacement devices, or app imports.
If the available metadata cannot distinguish the requested device, return
`selection_unresolved`; an app-group selection must not be labeled device-specific.
Identical descriptor tuples can hide distinct writers; the report retains this
identity limitation even after a choice is made.

The binding is **archive-scoped**. A new archive requires a newly reviewed binding;
do not automatically reuse ordinal labels, raw device strings, or an old archive
digest. This explicit friction is the limit of the first XML path, not the final
cross-export identity solution. A future native source registry requires separate
evidence. Apple exposes source, version, and device information as distinct concepts;
XML display text must not be promoted to those API identities.
[Apple sourceRevision](https://developer.apple.com/documentation/healthkit/hkobject/sourcerevision?language=objc).

There is one selection for an entire requested analysis window per family. No
day-by-day fallback, switching to whichever source has more records, or selecting
a source based on its measured values. A historical source change is a separately
identified series; cross-source comparisons need an explicit later design.

## 3. Input, time, and rejection boundaries

Operate on one immutable archive, its unique `export.xml`, an explicit IANA analysis
timezone, an explicit `as_of` date, and a positive integer `days`. Reporting dates
run inclusively from `as_of - days` through `as_of - 1`.
The entire XML document must parse successfully before any successful report is
published. Ambiguous ZIP membership, decoding failure, or malformed XML fails the
run; a partly parsed archive must not yield a successful partial report.

Select records before measurement aggregation. Records from unselected, fully
identified groups are excluded and counted; their overlap with selected records
is diagnostic only. Missing/empty `sourceName` in a recognized family causes
`selection_unresolved` for that family, rather than silently excluding an unknown
writer. Missing version/device attributes are explicit catalogue values, not
automatic failures for an app-group selection; a device-specific selection still
requires distinguishing evidence. A valid binding with subsequently discovered
unattributable/unknown-source records can fail one family while leaving another
family's separately stated result available, but
never invent an `ok` result for the failed family.

Parse timezone-aware timestamps and compare instants in UTC. An interval is
`[start, end)`: adjacency is not overlap. An end exactly at midnight belongs only
to the preceding day for interval attribution. Use actual local-day UTC boundaries,
including 23-hour, 25-hour, and skipped civil dates; never assume 86,400 seconds.
An unrepresentable analysis window fails the run. A skipped civil date has
`no_civil_day`, no number, and contributes no duration.

Accept calendar years 1–9999, seconds 00–59, an explicit UTC offset (or `Z`), and
zero to six fractional-second digits. Record timestamps may use Apple's space
separator/compact numeric offset or ISO `T`/colon offset. Reject excess precision,
leap-second syntax, and unsupported forms rather than silently truncating them.
Preserve accepted instants as integer microseconds. Pin and retain the timezone
rule bytes used to resolve the IANA zone, their digest, and each reporting date's
resolved UTC boundaries. Independent replay must verify these boundaries against
the pinned rules; the zone name alone is not sufficient provenance.

Selected steps and HRV quantities must be finite, nonnegative decimal numbers with
exact allowed units `count` and `ms`, respectively. No implicit conversion,
input rounding, clipping, outlier removal, or physiological threshold. Define sums
and means on exact decimal input values; define elapsed time on timestamp precision
preserved by the parser. Round once at numeric output to nearest IEEE-754 binary64,
ties to even. Normalize numeric negative zero to positive zero. If rounding would
produce infinity, or turn a strictly positive result into zero, block the daily
statistic with `numeric_result_out_of_range`. Finite nonzero subnormal output is
allowed. This tests the final rounded result, not exact representability of every
input decimal. An
independent oracle checks the exact arithmetic and this final conversion, without
a tunable comparison tolerance. Negative-duration
records are invalid; zero-duration steps and HRV are point observations, while
zero-duration sleep is invalid.

A selected record with attributable times but an invalid value/unit blocks the
affected daily statistic with `invalid_selected_record`; it is not silently dropped.
A selected record whose time cannot be reliably attributed blocks its whole family
for this report window. This includes missing, unzoned, malformed, out-of-range,
or reversed timestamps: there is no trustworthy interval to exclude as outside
the window. Otherwise, attributable records affect only reporting dates under
the metric's attribution rule; an out-of-window invalid quantity does not block
unrelated dates. Neither path changes an invalid or unknown day to zero.
An unknown selected sleep category blocks affected sleep-day calculations.
It blocks all three sleep statistics for every reporting day touched by the
record. A zero-duration selected sleep record blocks its start date as invalid;
if the time itself is invalid the whole-family rule applies. Unknown categories
on unselected descriptors are excluded and counted, not promoted to asleep or
allowed to block selected values.
Unselected records still contribute exclusion/invalid-input diagnostics, without
their raw content being logged.

## 4. Repeated records are not automatically duplicate measurements

Within an archive, assign each input record an audit locator using the member and
record ordinal. This identifies an occurrence, not a biological observation.
Reprocessing the same archive does not append or add its values a second time:
recompute a standalone result for the same inputs. Different archives are never
concatenated to build a measurement series in this milestone.

Adapt the existing probe's candidate tuple by using the full descriptor group in
place of its name-only alias: family, descriptor group, UTC start/end, unit,
and raw value/category. Equality defines a **repeat candidate**, not proven HealthKit UUID identity.
Do not deduplicate step or HRV values from tuple equality, nearest timestamps,
equal numbers, or similar device names. A selected repeated step/HRV candidate
makes the affected daily statistic ambiguous and null. A later identity-aware
deduplication policy requires its own evidence and version.

For sleep duration, interval union is inherently idempotent. It can tolerate two
equal assertions of asleep time without asserting that the input records are the
same measurement. Preserve both occurrence counts and flag the repeat candidate.

## 5. Metric-specific overlap rules

These are standalone **export-observation statistics**, not new entries in RC2's
MetricSpec registry. The labels below are intentionally distinct from planned
production daily metrics.

### Steps: recorded count from the selected source

Proposed statistic: `selected_source_recorded_steps`, unit `count`.
Sum the valid selected quantities only when the day's selected records have
unambiguous attribution and no internal collision:

1. A positive interval must fit wholly inside one completed local day. An interval
   ending at that day's closing midnight fits; an interval spanning multiple civil
   days blocks every intersected reporting day with `cross_day_quantity_unallocated`.
2. A zero-length record belongs to its start date. Two points at the same instant,
   or a point inside a selected positive interval, are collisions. A point at an
   interval's excluded end is adjacent, not overlapping.
3. Strict positive overlap between selected intervals, including across explicitly
   bound version groups, yields `selected_quantity_overlap` and a null daily value.
   A repeat candidate also blocks the day. Never sum, take the maximum, prorate,
   subtract, or choose the newest record to resolve this uncertainty.
4. Unselected phone/watch intervals cannot fill gaps or alter the selected number.

For example, selected records `[10:00,10:10)=100` and `[10:05,10:15)=80` produce an
ambiguous statistic, not 180 or 100 steps. Their quantities do not tell us where
within each interval the individual steps occurred. This is a deliberate model
boundary, not a claim that all overlap is erroneous.

With no selected records, the statistic is null. Explicit selected zero quantities
may yield a recorded count of zero with positive sample count, but do not prove a
zero-activity day. A positive count is also not proof of a complete day's activity.

### HRV SDNN: mean of selected exported samples

Proposed statistic: `selected_source_hrv_sample_mean_ms`. Assign each valid sample
to its start instant's local date, irrespective of interval end. Compute the
arithmetic mean of the day's selected values with equal weight per accepted record;
report input occurrence counts and sampling-time diagnostics. For a computed mean,
the contributing count is the selected occurrence count for that day. A blocked
mean has no contributing count (null), even when input counts are positive;
`no_records` has input and contributing counts of zero but a null value.
No duration weighting or sum.

Distinct records at the same start instant, including repeat candidates, cause
`selected_hrv_sample_collision` and a null daily statistic. Different-start
measurement intervals may overlap: preserve and flag that overlap, but do not
claim it proves duplicate measurements or independent samples. The statistic
describes those exported sample values only, not continuous or representative HRV
for the day. Sampling density and recording context can affect it. No resampling,
manual-entry override, or exclusion of unusually high/low values is inferred.
This mean is not SDNN recomputed from pooled heartbeat intervals.

One valid record is sufficient to calculate this descriptive sample mean; it is
not a valid-day or inference threshold. No selected records means null. HealthKit
defines this type as discrete SDNN values; its definition does not validate our
choice of daily summary or establish sampling representativeness.
[Apple HRV SDNN](https://developer.apple.com/documentation/healthkit/hkquantitytypeidentifier/heartratevariabilitysdnn?language=objc).

### Sleep: union of selected assertions, with explicit conflicts

Proposed statistics: selected-source `recorded_asleep_minutes`,
`recorded_in_bed_minutes`, and `recorded_awake_minutes`. Keep the categories
separate. Map only known Asleep/AsleepUnspecified/Core/Deep/REM values to asleep.

Clip intervals to each civil day's actual boundaries, then measure the union of
the selected intervals for each category group in UTC seconds / 60. Repeated or
partially overlapping assertions within a group contribute duration once. An
unspecified-asleep interval overlapping staged sleep contributes only its union,
never an extra layer of duration. Record overlap/repeat diagnostics.

InBed overlap with asleep or awake is expected and does not invalidate them. Any
selected awake-versus-asleep intersection makes the day's asleep and awake
statistics null with `contradictory_sleep_state`; an independently valid InBed
union remains available. Do not resolve the conflict by priority or subtraction.
Conflicting asleep stages leave the total asleep union available but prohibit
stage-specific values; stage totals are outside this milestone in all cases.

No records in a category means null, not zero. InBed-only data cannot produce asleep
duration. Missing Awake data cannot imply uninterrupted sleep. No gap filling,
joining nearby nights, maximum-session selection, or minimum-sleep threshold is
permitted. This output is observed duration by civil date, not sleep duration for
a night, valid-night count, sleep efficiency, bedtime, or wake time.

Apple documents intentional InBed/stage overlap and warns that awake samples can
omit the beginning/end of an InBed interval. These are reasons to keep the
categories distinct, not a completeness rule.
[Apple sleep categories](https://developer.apple.com/documentation/healthkit/hkcategoryvaluesleepanalysis?preferredLanguage=occ).

## 6. Coverage, diagnostics, and producer invariants

A future standalone evaluator must expose separate concepts per date/statistic:

| Concept | Allowed meaning |
|---|---|
| `calculation_state` | `computed`, `no_records`, `blocked`, or `no_civil_day` |
| `value` | Finite calculated number only for `computed`; null for all other states |
| `reasons` | Sorted, unique codes for all determinable blocking conditions; empty for computed/no_records, with nonblocking warnings kept in diagnostics |
| `observation_completeness` | Always `unknown` for this export path |
| `valid_metric_day` | Always null; never derive it from sample presence or span |
| `inference_eligible` | Always false |

Precedence is explicit:

1. Archive/ZIP/XML decoding or structural failure, invalid analysis-window/timezone
   inputs, or binding-integrity failure aborts the entire run. Emit a controlled
   failure with no daily results. An incomplete parse cannot promise exhaustive
   downstream diagnostics. Do not retain success-looking results from another family.
2. With structurally valid inputs, a family whose selection cannot be resolved or
   whose selected record times are unattributable is blocked. Preserve the reason
   at family level and on its ordinary reporting dates. Other families may succeed.
3. A skipped date always has `no_civil_day`, null value, and that reason; a family
   failure remains visible at family level, without relabeling the skipped date.
4. On other dates, determinable family or metric-specific blocking conditions take
   priority over `no_records`; otherwise calculate if records exist, or use
   `no_records`. Inspect all attributable records before finalizing a day's reasons.

Do not emit a second, inconsistent success flag. `computed` is a calculation status, not quality
assurance for the real-world measurement. `no_records` never maps to RC2's
`known_absent`, `present`, or a structural zero.

Counts use these fixed units; additional diagnostics require their own definition:

- The audit ordinal counts every `Record` element in XML encounter order, including
  types outside this policy; it is one-based within the member. One occurrence is
  counted once, regardless of how many defects or XML metadata children it has.
- For each family, partition whole-archive occurrences into selected, excluded,
  and unresolved-source counts. Include invalid records in their partition. Every
  source with missing/empty name goes into unresolved, not excluded. If binding
  integrity fails, this partition is not emitted as a successful result.
- Split each partition into time-attributable and time-unattributable counts.
  Unattributable membership in the reporting window is unknown: retain the archive
  count, do not insert it into a fabricated daily count or window total.
- Window counts are unique attributable occurrences assigned to at least one
  reporting date under that metric's rule. Daily counts count the occurrence once
  per assigned date. HRV uses start date; intervals use positive day intersection;
  zero-length step/invalid sleep records use start date. A midnight-crossing sleep
  record may contribute to two daily counts but only one window count.
- `invalid_occurrence_count` counts each selected record once in its declared scope
  if it has any validation defect. Per-reason invalid counts may overlap and do not
  sum to that total. Collision/conflict diagnostics are separate from input validity.
- `repeat_candidate_extras` is the sum of `max(group_size - 1, 0)` over selected
  time-attributable equal-tuple groups in the declared scope. It counts excess
  occurrences, not pairs or candidate groups. Retain invalid quantities/categories
  in this diagnostic, tagging missing raw attributes explicitly; invalid times
  cannot form such a tuple. Do not remove any occurrence from record counts.
- Overlap, same-start collision, stage-conflict, and contradictory-state diagnostics
  are booleans per applicable statistic/day, not pair counts. Quantity/categorical
  validity must not hide time-detectable overlap. A diagnostic that cannot be
  evaluated because its inputs are unavailable is null, never false. Validated
  empty input sets yield false. Reason-code lists are sorted and unique; only
  determinable conditions are promised.

A step day can have positive occurrence count and null value. No unsupported
sample-count or complete-day threshold is added.

Preserve archive identity, binding digest, explicit window/timezone, policy version,
and producer version in local provenance. Keep the private decision inputs so a
second implementation can reproduce the selection. A policy/binding change creates
a separately identified result and never overwrites historical evidence. A new
archive is new provenance, not automatically a new biological source, and this
milestone makes no cross-archive continuity claim either way.

For eventual production integration, every value-changing choice must enter a
per-metric value definition: source scope/selector, timezone/date attribution,
category mapping, validity, overlap/repeat rules, and estimator. Archive checksum
belongs to input provenance, not the measurement definition. A changed definition
must refuse reuse of old facts before publication/reporting; retain the existing
new-store remediation until a separate migration design is accepted. Operational
settings and report presentation must not masquerade as measurement changes.

## 7. Integration boundary and the next deliverables

The accepted production Health adapter does not exist. Existing fixture names such
as `health.hrv_sdnn_ms` and `health.sleep_duration_min` do not supply an implemented
normalization policy. The current accepted registry and value-definition guard are
Git-specific; do not insert these observations into them or disguise a sample mean
as a complete-day metric. A later integration needs explicit per-metric definition,
coverage, report eligibility, and migration contracts. No practical-effect threshold
or inference eligibility follows from this design.

Proceed in small deliverables, each reviewed independently:

1. **Source catalogue and binding validation.** Local-only real descriptors;
   synthetic public fixtures. Independently recount descriptor groups and reject
   stale/mismatched bindings. Obtain the user's actual source choices before a
   real-data calculation. This can finish without an adapter or new store.
2. **Standalone policy evaluator and independent oracle.** Implement the rules
   above using synthetic inputs first. Produce observations outside RC2's store.
   A separate implementation must recompute selections, conflicts, and values;
   the existing SAX presence checker cannot verify these new calculations.
3. **Private bound-archive verification.** Apply the chosen binding and compare
   every emitted result/diagnostic within the agreed window. Publish code and
   synthetic evidence only. Label unresolved and absent observations honestly.

Deliverable 1 is the next implementation scope. Deliverables 2 and 3 depend on its
reviewed contract and the actual source selection. Native HealthKit, production
Health ingestion, longitudinal identity, and sleep-night normalization require
their own subsequent designs, not silent additions to this work.

## 8. Acceptance examples for future executable verification

These are specified cases, **not tests already executed or passed**.

| ID | Input or change | Required result |
|---|---|---|
| HSP-01 | Reorder XML records, rebuild the archive/catalogue, and explicitly bind the same descriptors | Same descriptor membership and values; provenance/ordinals may change, and the old archive binding is rejected |
| HSP-02 | Wrong archive/catalogue digest, unknown selection, or a device choice that metadata cannot distinguish | Selection refused before calculations |
| HSP-03 | Archive-known selected watch has no records in the window; unselected phone does | Null selected-source statistic; no fallback |
| HSP-04 | Add an overlapping unselected source | Selected value unchanged; exclusion/overlap diagnostics disclose it |
| HSP-05 | Steps 100 and 80 in overlapping selected intervals | Null, `selected_quantity_overlap` |
| HSP-06 | Selected steps 100 and 80 in adjacent same-day intervals | Recorded count 180; completeness remains unknown |
| HSP-07 | Repeated selected step/HRV candidate versus a repeated asleep interval | Quantity statistic blocked; sleep union unchanged with repeat diagnostic |
| HSP-08 | Step interval crosses midnight; another ends exactly at midnight | Crossing interval blocks intersected days; midnight-ending interval belongs to preceding day |
| HSP-09 | Point steps collide with another point/interior interval versus equal excluded endpoint | Collision blocked; adjacency allowed |
| HSP-10 | HRV values 20 and 40 at distinct start times on one date | Sample mean 30 ms, count 2, no valid-day claim |
| HSP-11 | Two HRV values at same start versus distinct-start overlapping sample intervals | Same-start collision blocked; different starts retained with overlap diagnostic |
| HSP-12 | No records versus one explicit zero step/HRV sample | Null/no_records versus computed 0 with positive count; both completeness unknown |
| HSP-13 | Asleep 01:00–03:00 plus 02:00–04:00; all times UTC | Asleep union 180 minutes, not 240 |
| HSP-14 | InBed 00:00–05:00 plus the preceding asleep records | InBed 300 minutes and asleep 180, separate quantities |
| HSP-15 | Asleep and Awake intersect | Both state durations null; independently valid InBed duration survives |
| HSP-16 | Unspecified sleep overlaps staged sleep; two named stages disagree | Total asleep union only; stage conflict retained and no stage totals |
| HSP-17 | Spring/fall DST interval, skipped civil date, or unrepresentable window | Actual UTC durations; no_civil_day for skipped date; controlled run failure for unrepresentable window |
| HSP-18 | Selected invalid quantity, unknown sleep category, or unattributable timestamp | Affected days or whole family blocked as defined; other valid families may remain available |
| HSP-19 | Truncated XML, unsupported encoding, or ambiguous ZIP members | No successful partial report |
| HSP-20 | Repeat exact run; change binding, policy, or archive | Repeat has identical substantive output; changed inputs get distinct provenance and preserve earlier evidence |
| HSP-21 | Real source strings deliberately placed in an exception or synthetic publication fixture | Privacy check fails; no public publication of that content |
| HSP-22 | Valid records in one family followed by malformed XML; or valid XML with a family failure and skipped date | Malformed XML aborts all results; with valid XML the skipped date stays no_civil_day and family failure stays visible |
| HSP-23 | Empty/duplicate/cross-family selection set or changed canonical missing/empty descriptor content | Binding refused before any calculation; supplied digest alone cannot legitimize mismatched content |
| HSP-24 | A descriptor containing only unknown/missing SleepAnalysis categories is selected versus excluded | It is catalogued either way; selection blocks affected sleep days, exclusion does not block another selected source |
| HSP-25 | Three equal selected step occurrences on one day, all with negative value and wrong unit but valid times | Selected/window/day count 3, invalid count 3, two per-reason counts of 3, repeat extras 2; overlap is a separate boolean and value is null |
| HSP-26 | A selected record with an unattributable timestamp | Archive selected/unattributable/invalid counts each include it once; window membership is unknown, family blocked, no invented daily occurrence |
| HSP-27 | Mean of decimal values 0, 0, 1; binary64 midpoint 9007199254740993; overflowing final result 1e400 | Mean rounds once to 0.3333333333333333; midpoint rounds to 9007199254740992; overflow is blocked |
| HSP-28 | Positive result 5e-324 versus 1e-400; input negative zero | Nonzero subnormal allowed; positive-to-zero underflow blocked; exact zero normalized to +0 |
| HSP-29 | More than six timestamp fractional digits; changed timezone rules | Excess precision rejected, never truncated; replay uses/verifies pinned rules and UTC boundaries |

## 9. Unchanged project gates and required inputs

The user's actual device/app preference for each family and its unambiguous local
binding are still operating inputs. Defining this policy does not choose them.
If the requirement is instead equality with Health's merged totals, choose the
native integration approach before implementing a different XML merge heuristic.

Calendar remains pending a toolchain that can compile the modern EventKit read
request and a correctly identified permission-owning app. No grant follows from
Full Disk Access. Two matching weather captures do not establish a revision
horizon; it remains unknown. Screen Time device/usage attribution and retention
remain unfinished. Backup/restore and T8/T9 remain unfinished, inference disabled,
and the project DRAFT. None of these gates is weakened by this document.
