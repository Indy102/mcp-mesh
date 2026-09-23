# Health presentation D3 implementation plan

Spec: ../specs/2026-09-22-health-presentation-d3-design.md
Base: 06392108e3cf815aa9098eb6ae83bfdd3dddad6e
User authorized D3. Isolated branch health-presentation-d3. No merge.

## Global Constraints

- Accepted RC2, D1, D2, existing spikes/tests/contracts and dependency files remain byte-identical; only new D3 package/tests/docs and additive README/CI entries.
- Exactly one explicitly requested batch through public D2 read_batch; no fallback, latest, store mutation, private store internals, aggregation, source selection, MCP or RC2 integration.
- Every original value/null/reason/diagnostic is preserved. observation_completeness="unknown", valid_metric_day=null, inference_eligible=false. No comparisons, associations or recommendations.
- Private output only, no personal inputs or evidence in Git/public output, synthetic committed fixtures only. No fresh replay or receipt metadata claims from the D2 batch alone.
- No JavaScript, server, external assets or network. Escape all untrusted text and publish new owner-private files without clobber. No APFS power-loss/backup/whole-project acceptance claims.

## Task 1: Offline presentation, private publication and synthetic acceptance

Read docs/superpowers/specs/2026-09-22-health-presentation-d3-design.md first; it is binding for exact interfaces, HTML inspection selectors, strings, errors and scope. Implement separate observation_view package with __init__.py, __main__.py, rendering and publication modules as appropriate. Read the existing D1 contracts and D2 public APIs/filesystem functions; do not change them or use their private helpers. Public interface create_view(store_dir, store_id, batch_id, output), ViewError, and explicit module CLI as specified. The only source read is observation_store.read_batch, exactly once after validating explicit IDs/output; strict validation and identity comparison precede rendering. All Global Constraints above apply verbatim.

Build attractive but restrained private offline HTML: obvious unknown-coverage restriction, selected-source recorded metric, explicit chosen identities/window, daily table, all states/reasons with exact number spellings/null, diagnostic details, scoped family/archive/window evidence, selected source descriptors only, evidence/identity limits. Full exact report in escaped canonical JSON pre#report-json. Use fixed row class observation-day, time/date text and value/state/reasons cell classes for independent comparison. Accessible desktop/mobile/print CSS only. No charts, aggregate statistics, menu/server/picker or style dependency. Treat text as untrusted and display controls visibly in labels; preserve underlying JSON reversibly.

Private output uses unchanged D2 public filesystem primitives, adds D3-specific selected/other-store guards, no raw filesystem path/exception in stdout/stderr. New explicit file under private parent, all source/repo/store aliases refused before read. Fail closed on invalid store, output race/directory replacement, source-only extraction and partial publication; fixed uncertain-after-link error. No personal source reads during implementation.

Add tests/observation_view with semantic DOM tests using stdlib HTMLParser and real D1/D2 synthetic fixture paths, CLI/privacy/publication/race/failure tests. Reuse synthetic test helpers if appropriate without modifying earlier tests; never production bypass. Cover spec section 7, including exact primary values and appendix equality, not only snapshots/HTML string presence. Add docs/HEALTH_PRESENTATION_D3.md including exact CLI, limits/error semantics, cost of D2 full-archive read and stale static snapshots; concise README and additive CI step.

Run focused new tests as development proceeds. Before commit run complete D3 suite, unchanged D2 and D1 suites, core tools/run_all.py and the three spike suites once, with .venv/bin/python. Record exact counts/commands/exits. No dependency changes. Self-review and commit only task files. No subagents/push/merge. Do not inspect the controller's independent harness. Full report to controller-provided task report path: changes, signatures/error semantics, test evidence, self-review and limits; return compact status/commits/tests. Controller performs task review, independent acceptance, browser rendering, real private run and whole-branch review.
