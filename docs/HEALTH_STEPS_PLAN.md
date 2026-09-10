# Selected-source steps implementation plan

Spec: `docs/HEALTH_STEPS_CONTRACT.md`; underlying policy:
`docs/HEALTH_SOURCE_SELECTION_DESIGN.md`. The user chose the named exported watch
stream for steps, its binding is verified, and requested the next step after the
daily-calculation/overlap-check milestone was described. Implement this scope;
do not stop for renewed permission on that already authorized work.

## Global Constraints

- Work only in the isolated `health-steps-evaluator` branch. Preserve all existing
  RC2, catalogue, and presence code/tests/dependencies byte-for-byte. New code lives
  under `spikes/health_steps/`; no store writes, production adapter, or inference.
- Keep real input, source metadata, bindings, values, reports, and evidence outside
  this public checkout. Use invented unit fixtures; no external network calls.
- The frozen steps contract governs exact values, fields, limits, privacy, and
  state/count/diagnostic semantics. Do not invent source choices or relax policy.
- Primary and oracle logic/tests are independently authored. Oracle must not read
  or import primary evaluator modules, primary unit tests, generated expected
  reports, or primary catalogue helpers. Standard-library primitives may be shared.
- Implementers do not dispatch subagents. Root dispatches independent reviews.
- Own only assigned files. Commits are serialized by root after empty-index checks;
  do not stage another task's files. Report READY_TO_COMMIT before committing unless
  root has explicitly assigned the current commit slot.

## Task 1 — primary standalone steps evaluator

Read `docs/HEALTH_STEPS_CONTRACT.md` and the Global Constraints above. Own:
`spikes/health_steps/evaluate.py`, `spikes/health_steps/steps_core.py`,
`spikes/health_steps/steps_time.py`, and `spikes/health_steps/test_evaluate.py`.
Keep CLI/binding/output glue, observation arithmetic/diagnostics, and time/rule
handling separated by responsibility. Do not create additional modules without
explaining the concrete need to root. Existing catalogue helpers may be imported
only through their public functions as the contract allows; no catalogue edits.

Implement complete public evaluation, strict run-level input validation, frozen
timezone provenance and real day boundaries, streaming record processing, exact
arithmetic, all counts/reasons/diagnostics, and private exclusive output. Retain
only metadata counters and records needed for scoped evaluation/repeat diagnostics;
do not accumulate unrelated XML elements. Hash identity across every archive pass.

Use synthetic inputs and hand-derived expected values. Test the contract acceptance
cases through public CLI plus focused core helpers as appropriate. Include actual
DST/skipped dates, a synthetic TZif with disjoint date segments, exact rounding,
every collision boundary, HSP-25 counts, full XML failure, and no data leakage.
Reuse no oracle fixtures or expected results. For synthetic binding setup, primary
catalogue helpers are allowed; expected daily statistics must be independent of the
evaluation under test. Do not run the real archive.

Run `python3 -m unittest discover -s spikes/health_steps -p test_evaluate.py -v`
once after focused iteration. Preserve full output and exit status in the report.
Self-review for exact semantics and complete-file parsing. Report files changed,
tests and their evidence, any concerns, and READY_TO_COMMIT. When assigned the
commit slot, commit only owned files and update the opening report status to DONE
with the exact base/head. Do not leave contradictory pre-commit status at its top.

## Task 2 — independent steps report oracle

Read `docs/HEALTH_STEPS_CONTRACT.md` and the Global Constraints above. Own:
`spikes/health_steps/verify_steps.py`, `spikes/health_steps/oracle_core.py`,
`spikes/health_steps/oracle_time.py`, and `spikes/health_steps/test_verify_steps.py`.
Implement separate strict input/report loading, pinned-zone replay, time parsing,
record assignment, exact arithmetic, partitions/repeats/collisions/reasons, and
complete canonical comparison. All report fields must be reconstructed or validated
against supplied input identity, including flags, types, order, and fixed versions.

The existing independent catalogue verifier's public `verify` function is allowed
for binding/catalogue integrity. Inspect that module only as needed for its public
interface. Do not import/read the primary evaluator or catalogue implementation,
their tests, or generated expected reports. Independently stream the measurement
pass and ensure the input hashes still equal the binding across validation/evaluation
passes. The supplied report's timezone bytes are a pinned replay input; day boundaries
and other outputs must be derived from them, never accepted as expected results.

Build independent synthetic ZIP/catalogue/binding fixtures by the frozen contracts.
Hand-author expected daily fields; do not generate expected results with the primary
or your evaluator itself. Include every critical state/math/time rule and reject
rehashes of wrong daily distributions, values, counts, reasons, flags, boundaries,
and schema fields. Assert all verification paths are read-only and ordinary errors
are fixed safe JSON. No tolerance comparisons. No real input or network.

Run `python3 -m unittest discover -s spikes/health_steps -p test_verify_steps.py -v`
once after focused iteration; preserve full output/exit. Self-review and report
files/tests/concerns with READY_TO_COMMIT. Wait for root's serialized commit slot,
then commit only owned files and update the report's opening status to DONE.

## Root integration and private execution

Review the wire contract before implementation. Independently review each committed
task against the brief/spec and its diff, route substantive findings through the
implementer, then perform one broad final branch review. Root owns
`spikes/health_steps/test_interoperation.py`, its README, root README, and CI updates.
The interoperation test drives public evaluate/verify on an invented archive,
asserts expected daily numbers and gaps, and proves a rehashed changed report fails
verification without being rewritten. Add only the standalone suite to CI; preserve
all existing checks and all 80 accepted core hashes.

After review, run the primary and independent oracle on the real supplied archive
and verified steps binding in a new private output directory. Use the prior
completed-day analysis window and timezone recorded in private evidence. Compare every emitted field, preserve exact source
and code identities, and retain local evidence. Source selection is already given;
do not repeat that question or select HRV/sleep. Computed numbers remain recorded
counts from one export source, with completeness unknown and inference disabled.

Publish only generic code/docs and invented tests through a separate follow-on PR
under the existing public-repository authorization. Check publication scope and
wait for CI. Do not merge earlier PRs or modify their branches. Summarize real
results privately, distinguishing computed, blocked, and absent dates, and identify
the next bounded work without reclassifying unfinished project gates.
