# D4 backup and observation recovery implementation plan

Spec: ../specs/2026-09-26-health-recovery-d4-design.md
Base: 19e564ffdeb485cf19ec62815e1f2384fefc88e7
Isolated branch health-recovery-d4; source-only stacked PR, no merge.

## Global Constraints

- RC2, D1, D2, D3, all previous tests/spikes and dependency files remain byte-identical; only new package/tests/docs and additive README/CI entries.
- Complete D2 managed-tree evidence snapshot, but recovery re-admits only committed observations into a different UUID through real public D2/D1 replay. Never rewrite original evidence or create a writable old-identity clone.
- Every original value/null/reason remains exact; coverage unknown, valid_metric_day null, inference false. No source choice, aggregation, inference, MCP or RC2 integration.
- No personal input reads in implementation/tests, no private identifiers/paths/data in Git or stdout/stderr. All committed fixtures synthetic. No automatic backup destination, cleanup, overwrite or resume.
- Public D2 contracts/filesystem/APIs only, no underscore helpers or admission bypass. Preserve partial/uncertain publication; no fabricated repair, detached pass claim or changed earlier expected values.
- Same-disk verification is local recovery evidence only; physical loss, external media protection, encryption and APFS power-loss acceptance remain separate unverified gates.

## Task 1: Verified snapshot and recovery with public commands

Read the binding spec first at docs/superpowers/specs/2026-09-26-health-recovery-d4-design.md; use its exact fields, signatures, CLI operations and error set. Implement observation_backup with small focused modules for contracts, checked object I/O, validation, snapshot, recovery and CLI as useful. Inspect unchanged observation_store/contracts.py, filesystem.py, store.py and adapters/health_observations public APIs. Do not duplicate whole D2 implementations or import private helpers. Shape validation must include semantic closure/linkage; bools rejected for integer fields; canonical JSON and derived listing comparisons are mandatory.

Implement streaming complete-tree capture under existing source writer_lock, deduplicated flat SHA objects, validated immutable manifest seal, full offline verify/reflush and deliberate safe refusal boundaries. Use checked handles to prevent path replacement or unsafe types. Treat full managed tree and partial-attempt retention exactly as spec; only committed batches have full semantic archive/batch validation, opaque unreferenced data is preserved but never promoted. Reserve private destinations exclusively and preserve incomplete outputs on failure. Files published within destination volume through unchanged D2 exclusive file primitive. Source tree unchanged including no recover/status writes. No unbounded read of archive objects just to hash/copy; D1/D2 existing full-json reads remain documented limits.

Recover with an immutable persisted plan and new store UUID; construct InputSet from backed-up archive and exact embedded envelopes using supplied report, then public D2 import and read/list comparisons. Do not copy D1 PASS receipts for admission. Enforce original-versus-new attempt distinction, exact definition/batch set and all batch bytes. Store writer lock encloses final verification/reflush/completion publication and check-recovery; never enclose init/import/status calls that reacquire it. Preserve incomplete recovery evidence, only a validated result is complete. No recovery mutation of backup/source.

Add tests/observation_backup synthetic public API/CLI/contract tests with real D1/D2 data. Include actual child-process kills around before/after seals and results, concurrency, tamper all evidence classes, short writes/fsync errors, no-clobber/root replacement and source-only distribution privacy boundaries. Reuse earlier synthetic helpers without editing them. Meaningful behavior assertions required. Document public commands, private output layouts, retained size/memory limits, external drive filesystem privacy requirements, source unavailability rehearsal, fresh identity/history limits, errors and unfinished gates in docs/HEALTH_RECOVERY_D4.md. Add README and CI steps only.

Run focused new tests while building; final task evidence runs complete D4 tests and unchanged D1/D2/D3 plus tools/run_all.py and three spike suites once using .venv/bin/python. No network or actual personal data. Self-review and commit task files. No subagents/push/merge. Write full task report to controller path with exact tests/counts/exits, interfaces, decisions and limitations; return compact status+commits. Controller independently reviews, runs authored-before-code acceptance, packages/extracts source, checks frozen baselines and runs real private demonstration only after review.
