# Project status

**The Git-only descriptive milestone is accepted and frozen at RC2. The overall
project remains DRAFT.** Acceptance covers the bounded local Git workflow and its
existing criteria. It does not establish completion of a five-source mesh or
universal MCP client compatibility.

## Public distribution

This repository is a fresh public snapshot with new Git history. The contents of
`tools/`, `tests/`, `schemas/`, `pyproject.toml`, and `uv.lock` preserve the accepted
RC2 core byte-for-byte. Public-facing documentation and packaging are new. The
public snapshot is a new distribution, not the original accepted archive.

Private development history, personal source inputs, demonstration outputs, and
local feasibility evidence are not included. No artifact download or historical
acceptance tag is implied by the RC2 name in this repository.

## Implemented and accepted scope

| Area | Scope |
| --- | --- |
| Git source | Local full-history reads, captured HEAD scope, exact author-email matching, non-merge commits, author-date bucketing, and object-ID deduplication across repositories. |
| Daily facts | Provenance, measurement-definition identity, structural zeros after complete observation, coverage, and recorded collection attempts. |
| Storage | Versioned DuckDB generations, atomic pointer publication, snapshot identity checks, local writer locking, and bounded reclamation. |
| Reporting | Descriptive weekly means and baseline comparisons, availability and failure disclosure, and refusal of changed measurement definitions. |
| Runtime transport | A client-facing `weekly_report` gateway and an internal Git leaf serving `fetch` and `source_status`, using the tested pinned stdio profile. |

The frozen milestone's acceptance recorded the following results. These are
historical baseline results, not a claim of a fresh run on every public checkout:

| Tier | Recorded accepted result |
| --- | --- |
| T1: read-only contract verification | 310 checks, 0 failures |
| T1b: artifact integrity | 7/7 |
| T1c: semantic contracts | 34/34 |
| T2: independent domain-function suite | 8/8 |
| T3: database and collector | 79/79 |
| T4: stdio transport profile | 44/44 |
| T5: generation round-trip | 2/2 |
| T6: Git public workflow | 37/37 |

Run the [README validation commands](../README.md#run-the-validation) to verify
this checkout. The runner reports executed tiers separately from gates it does
not execute. Passing contract fixtures for a planned feature does not make that
feature available.

## Remaining work and unchanged limits

| Area | State |
| --- | --- |
| Health, Calendar, Screen Time, and weather adapters | Unbuilt. Git is the only production source adapter in this snapshot. |
| T7 source/platform feasibility | In progress in separate local work; the overall gate is incomplete. Evidence remains private and is not incorporated into the accepted core or its test totals. |
| Backup/restore | Not implemented. Retaining recent generations is not a restore workflow. |
| T8 filesystem crash verification | Unverified. Database publication tests do not establish APFS crash/corruption behavior. |
| T9 statistical calibration | Not implemented or run. Inference, associations, recommendations, and association caching remain disabled. |
| Zero-MAD behavior | Unchanged. Zero baseline dispersion yields a null standardized delta and `meaningful=false`, including on sparse histories with a large raw change. |

Source-feasibility progress does not promote an unbuilt adapter or incomplete
platform gate to accepted status. There is no completion-date claim for the full
multi-source project.

The accepted core remains frozen. Reopening its behavior requires a reproduced
failure of an existing acceptance criterion against the accepted build. A fix
would become a new candidate with a new identity; public documentation does not
retune the accepted rules or silently replace that core.
