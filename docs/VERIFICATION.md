# Public snapshot verification

The publication copy's 80 core files—those under `tools/`, `tests/`, and `schemas/`,
together with `pyproject.toml` and `uv.lock`—match accepted RC2 byte-for-byte. The per-file
SHA-256 values and original artifact identity are recorded in
[rc2-core-sha256.json](rc2-core-sha256.json). Public documentation and CI are new;
this repository is a new distribution, not the original private RC2 archive.

## Local publication run

On September 9, 2026 (Indianapolis local time), a fresh virtual environment was
created in the allowlisted publication copy. Python 3.13.7, uv 0.12.10,
macOS 26.5 arm64, and the unchanged `uv.lock` were used. The command was:

```sh
uv run --locked python tools/run_all.py
```

Exit status: **0**. This table transcribes the new run's tier summaries; it is not
a claim that the original private archive was rebuilt. The machine-specific command
paths in the local transcript are deliberately not published.

| Tier | New publication-copy result |
|---|---|
| T1 read-only contracts | 310 checks, 0 failures |
| T1b artifact integrity | 7 tests, OK |
| T1c semantic contracts | 34 tests, OK |
| T2 independent domain suite | 8 tests, OK |
| T3 DuckDB and collector | 79 tests, OK |
| T4 real stdio transport | 44 tests, OK |
| T5 byte-for-byte generation round-trip | 2 tests, OK |
| T6 public Git workflow | 37 tests, OK |
| T7 source feasibility | Not executed by this suite; incomplete in ongoing local work |
| T8 platform crash verification | Not run |
| T9 statistical calibration | Not run; not implemented |

The runner keeps its existing DRAFT status and unverified-gate wording. The tests
use synthetic fixtures and temporary Git repositories. They do not establish
personal-source access, backup/restore, APFS crash durability, statistical validity,
or compatibility with every MCP client.

## GitHub Actions

[The workflow](../.github/workflows/verify.yml) runs the same command on a
`macos-15` hosted runner with Python 3.13.7 and uv 0.12.10. Both actions are pinned
to immutable commits; the job has read-only repository permissions and checkout
does not persist credentials. See [third-party references](THIRD_PARTY.md).

The local result above is independent of hosted CI. Inspect the repository's
Actions page for the status of each actual workflow run; workflow configuration
alone is not a passing CI result.
