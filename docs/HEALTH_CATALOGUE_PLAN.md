# Health catalogue implementation plan

Spec: `docs/HEALTH_CATALOGUE_CONTRACT.md`. This follows the independently reviewed
September 10 source-selection policy. The user requested the private catalogue and
validated selection record. Actual source preferences remain a required operating
input, not something an implementer may choose. Wire contract review precedes code.

## Global Constraints

- Preserve `tools/`, `tests/`, `schemas/`, `pyproject.toml`, and `uv.lock` byte-for-byte.
- Work only in this isolated `health-source-catalogue` branch. Do not alter the
  accepted private repository, tags, input archive, or prior Health presence tools.
- Implement standalone standard-library catalogue/binding tools under
  `spikes/health_catalogue/`; no daily values, source-priority inference, adapter,
  fact-store writes, changed coverage, or inference enablement.
- Real source data, descriptors, catalogues, bindings, and measurements remain
  outside this checkout and public history. Use synthetic tests only here.
- Do not fabricate a real selection or claim human intent/device identity is
  software-verified. The user must choose actual sources before a real binding.
- Implementers do not spawn subagents. Root dispatches independent review.

## Task 1 — catalogue and binding commands

Read `docs/HEALTH_CATALOGUE_CONTRACT.md` as the exact wire/behavior authority and
the Global Constraints above. Implement `spikes/health_catalogue/catalogue.py`
and `test_catalogue.py`. Own only those files. Keep the module standalone; use
standard library Python 3.13 and streaming XML with bounded completed-element state.
Implement both build and bind commands, strict JSON structure/identity checks,
private output guard, deterministic content, and fixed safe CLI responses.

Tests must build small synthetic ZIPs and drive public commands as well as useful
unit boundaries. Require exact hand-derived catalogue counts and membership;
exercise tampered persisted catalogue and verify failure does not change it.
Use temporary paths outside the code checkout for private artifacts. Include
duplicate JSON keys/nonfinite numbers, unsupported encoding, external XML resource
refusal, symlink output aliases, preexisting outputs, and no leaked sentinel
descriptor/path tokens in expected failure stdout/stderr. No arbitrary memory/time
gate is claimed; inspect streaming ownership for completed nodes.

Run `python3 -m unittest discover -s spikes/health_catalogue -p test_catalogue.py -v`.
Self-review, commit only owned files, and report exact command/output/exit, commits,
and concerns. Do not run the real archive or choose sources; root handles those
after review. Primary tests must not import the future independent checker.

## Task 2 — independent catalogue/binding verification

Read `docs/HEALTH_CATALOGUE_CONTRACT.md` and Global Constraints as requirements.
Implement `spikes/health_catalogue/verify_catalogue.py` and
`test_verify_catalogue.py`. Own only these files. Do not read/import primary
implementation, tests, or fixtures to derive logic/expected output. A separate
streaming parse/accumulator must independently recompute every group and count and
validate catalogue and optional binding. Shared standard-library parsing machinery
is allowed if recorded; no shared project helper or producer repair is allowed.

Use hand-authored expected JSON and synthetic ZIPs. Test strict read-only failure
for changed group count/descriptor/order/digest, wrong archive, wrong/duplicate/
cross-family selection, extra fields, unsafe JSON, malformed/unsupported XML,
external-resource refusal, and fixed diagnostic privacy. Validate the binding's
false attestation flags and declared limited scope. Passing an optional binding
does not validate a user's actual choice or device identity.

Run `python3 -m unittest discover -s spikes/health_catalogue -p test_verify_catalogue.py -v`.
Self-review, commit owned files, report exact test evidence and parser choice. Do
not run the real archive or open private descriptors. Primary/independent CLI
interoperation is root's later check against synthetic inputs and the real
catalogue; the independent tests cannot generate expected results using Task 1.

## Root integration and delivery

After each task, review its requirements and committed diff independently; fix
substantive findings through the implementer and perform scoped re-review. Root
adds concise usage documentation and the standalone test command to CI, preserving
all accepted-core files. Run combined standalone tests, a synthetic public-command
build/bind/verify workflow, and a broad final branch review.

Only after code review, produce a private catalogue from the supplied archive and
run the independent verifier on it. Show actual source options to the user locally;
if choices arrive, record them verbatim, build the real binding, independently
verify it and retain evidence. If choices remain absent or ambiguous, finish all
independent work with the catalogue verified and the real selection explicitly
pending. Do not label a request template a validated selection.

Preserve immutable RC2 hashes/core. Report executed verification separately from
future policy examples and unfinished Calendar/weather/platform/statistical gates.
Public publication, if performed under existing authorization, contains generic
code/docs and synthetic fixtures only; never private clone history or evidence.
