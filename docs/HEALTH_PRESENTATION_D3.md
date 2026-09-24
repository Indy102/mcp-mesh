# Private Health observation view (D3)

D3 presents one explicitly selected committed D2 batch as a private offline HTML
file. It reads through `observation_store.read_batch`, without mutating the store,
replaying the archive, merging batches or using RC2's report/MCP path. All stored
values, nulls, reasons, counts, timezone boundaries and uncertainty flags remain
unchanged. It displays selected-source recorded steps, not complete daily activity.

Use the existing D2 `list` command to obtain a batch ID, and choose it explicitly.
The store ID must be the canonical UUID retained when the store was initialized.
Use an existing current-user-owned 0700 output directory outside source checkouts,
the executing source distribution, RC2 directories and all Health stores. The
destination must be a new file. These example paths are placeholders:

```sh
uv run --locked python -m observation_view \
  --store /private/path/to/observation-store \
  --store-id YOUR_CANONICAL_UUID4 \
  --batch-id YOUR_BATCH_SHA256 \
  --output /private/path/to/views/new-snapshot.html
```

Open the resulting HTML locally in a browser. It uses no JavaScript, server,
external fonts, assets, requests or refresh. The CLI has no implicit latest batch
and the HTML has no store picker. Create a new file to present a different batch.
The API `observation_view.create_view(store_dir, store_id, batch_id, output)`
performs the same operation and returns canonical acknowledgement bytes. It does
not accept prepared batches, receipts, PASS assertions or verification overrides.

Success returns exit 0 and prints exactly:

```json
{"error":null,"operation":"view","status":"PASS"}
```

Failure returns exit 2 with the same three fields, status `FAIL` and a fixed error
code, and empty stderr. No paths, IDs, source labels, quantities or exception text
are printed. The API raises `ViewError`; `code`, its string form and
`failure_bytes` contain fixed safe errors. Malformed CLI input is `CLI_USAGE`;
malformed IDs are `INPUT_INVALID`. D2 read errors remain explicit, including
`STORE_ID_MISMATCH`, `BATCH_NOT_COMMITTED` and `INTEGRITY_ERROR`; there is no
older-batch fallback. Existing output is `OUTPUT_EXISTS`; prohibited locations are
`OUTPUT_INVALID`; an unsuitable parent is `OUTPUT_NOT_PRIVATE`.

Output publication reuses unchanged public D2 filesystem primitives, with D3-owned
store exclusion checks. It pins the private parent, stages a new 0600 file, flushes
it, and uses macOS exclusive rename; no overwrite or hardlink fallback is offered.
The writer is macOS-only. The completed HTML can be read offline on other platforms.
After a possibly completed publication, failure is `OUTPUT_PUBLICATION_UNKNOWN`.
A complete destination may remain: do not treat the failed command as success or
overwrite it. Preserve it as uncertain evidence and rerun to a different new file.
Uncommitted private staging files may remain; there is no automatic adoption or
cleanup. The store is unchanged regardless of presentation success or failure.

The visible table preserves original order and exact round-trip numeric spelling.
`computed` can include an explicit zero. `no_records`, `blocked` and `no_civil_day`
carry literal `null`, never zero-filled activity. Every reason has its original
code and a plain explanation. Daily details retain the exact UTC interval,
duration, source counts, repeat extras, nullable overlap diagnostics and eligibility
fields. A skipped local date has null UTC boundaries and zero duration.
Family reasons and archive/window counts keep their distinct scopes. The complete
report is also preserved in a canonical JSON evidence appendix. Selected source
descriptors are matched by exact group ID; unrelated HRV/sleep groups are omitted.
Missing and present-but-empty descriptor fields differ, and control/directional
characters in human labels are displayed as escapes.

Coverage remains `unknown`, `valid_metric_day` remains `null`, and
`inference_eligible` remains `false`. D3 computes no daily completeness, totals,
averages, comparisons, trends, associations or recommendations. An HTML view is
private Health information, not a public portfolio artifact.

A successful read means D2 validated the committed batch and linked evidence at
view generation time. Rendering does not execute fresh independent replay. D2's
public reader returns no execution receipts, replay timestamps or verifier runtime
identities; D3 does not invent them. The displayed timestamp is view generation
only. Opening the static file later does not verify its integrity or recheck the
store. Source labels are not authenticated physical-device identity; evidence
validation does not establish complete activity, source priority or user intent.

D2 reads hash complete retained archives, so rendering has that existing memory
and I/O cost plus an in-memory HTML snapshot. This milestone sets no large-export
capacity guarantee. Local file/directory flush and process/failure checks do not
establish power-loss durability or independent backup/restore. Those gates and the
unfinished source/statistical milestones remain open; the project remains DRAFT.

Synthetic tests are separate from the accepted core:

```sh
uv run --locked python -m unittest discover -s tests/observation_view -p 'test_*.py' -v
```

They cover the real D1/D2 read path, immutable store behavior, exact presentation,
hostile text, private output refusals, source-only distributions, concurrent output,
publication while reading an older explicit batch, and failures before/after final
linkage. Pure renderer fixtures test formatting only; they do not attest admission.
