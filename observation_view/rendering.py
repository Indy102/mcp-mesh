"""Private pure renderer. Its input is validated by create_view, never a PASS flag."""
from datetime import datetime, timezone
from html import escape
import unicodedata
from adapters.health_observations.contracts import canonical

_REASONS = {
    'selection_unresolved': 'Some source occurrences cannot be assigned to the selected source.',
    'selected_time_unattributable': 'A selected occurrence cannot be assigned to a date.',
    'invalid_selected_record': 'A selected record has an invalid value or unit.',
    'cross_day_quantity_unallocated': 'A quantity crosses a day boundary and is not allocated between days.',
    'selected_quantity_overlap': 'Selected quantity intervals overlap.',
    'selected_repeat_candidate': 'A selected record may be repeated; it has not been deduplicated.',
    'numeric_result_out_of_range': 'The calculation cannot be represented as a finite stored value.',
    'no_civil_day': 'This local date does not exist in the pinned timezone.',
}
_STATES = {
    'computed': 'Recorded quantity from the selected source; daily completeness is unknown.',
    'no_records': 'No selected occurrences assigned to this date in this archive. This is not zero activity.',
    'blocked': 'Calculation withheld. The listed reasons are preserved; no other batch supplies a value.',
    'no_civil_day': 'Skipped local date: null UTC boundaries and zero duration.',
}
_CSS = """
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: #152b35; background: #f3f5f3; line-height: 1.55; }
* { box-sizing: border-box; }
body { margin: 0; padding: 40px 24px 64px; }
main { max-width: 1180px; margin: auto; }
header { border-top: 5px solid #246453; padding-top: 22px; margin-bottom: 28px; }
.eyebrow { letter-spacing: .12em; font-size: .76rem; font-weight: 750; text-transform: uppercase; color: #395b50; }
h1 { font-size: clamp(1.8rem, 4vw, 2.7rem); line-height: 1.15; margin: .5rem 0 1rem; }
h2 { font-size: 1.35rem; margin: 0 0 1rem; } h3 { font-size: 1rem; }
p { margin: .65rem 0; } .muted { color: #475d67; }
section { background: #fff; border: 1px solid #d3ddd8; border-radius: 12px; padding: 24px; margin: 20px 0; min-width: 0; }
.notice { background: #fff7e7; border-left: 5px solid #92651b; padding: 16px 20px; border-radius: 6px; }
.notice strong { color: #634715; } .grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 20px; }
dl { margin: 0; } dt { font-weight: 650; margin-top: .7rem; } dd { margin: .1rem 0 .7rem; overflow-wrap: anywhere; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .84rem; }
code { overflow-wrap: anywhere; } pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f4f6f5; padding: 14px; border-radius: 6px; max-width: 100%; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: .94rem; }
caption { text-align: left; margin-bottom: 14px; color: #475d67; }
th { text-align: left; background: #ecf1ee; font-size: .8rem; } th, td { padding: 13px 10px; border-bottom: 1px solid #dce3df; vertical-align: top; overflow-wrap: anywhere; }
th:nth-child(1) { width: 17%; } th:nth-child(2) { width: 16%; } th:nth-child(3) { width: 19%; }
.value { font-variant-numeric: tabular-nums; font-weight: 700; }
.day-details td { padding: 4px 10px 14px; } .day-details { background: #fafbf9; }
details { min-width: 0; margin: .4rem 0; } summary { cursor: pointer; font-weight: 650; padding: 6px 0; color: #245143; }
summary:focus-visible { outline: 3px solid #93620b; outline-offset: 4px; }
ul { padding-left: 1.25rem; } .reasons ul { margin: 0; } .reason-description { display: block; font-size: .84rem; margin-top: .2rem; color: #475d67; }
footer { padding-top: 10px; color: #475d67; font-size: .88rem; }
@media (max-width: 600px) { body { padding: 20px 12px 36px; } section { padding: 16px 12px; } .grid { grid-template-columns: 1fr; gap: 4px; } table { table-layout: auto; } th, td { padding: 9px 5px; font-size: .8rem; } th { overflow-wrap: normal; word-break: normal; } code { font-size: .76rem; } th:nth-child(1), th:nth-child(2), th:nth-child(3) { width: auto; } }
@media print { body { background: #fff; padding: 0; } section { border-radius: 0; box-shadow: none; } header { border-top-width: 2px; } tr { break-inside: avoid; } summary { color: #152b35; } }
"""


def _text(value):
    # Labels must not use control/directional formatting to impersonate UI text.
    text = str(value)
    visible = ''.join(('\\u%04x' % ord(c)) if unicodedata.category(c) in ('Cc', 'Cf') else c for c in text)
    return escape(visible, quote=True)


def _json(value):
    return escape(canonical(value).decode('ascii'), quote=True)


def _fields(pairs):
    return '<dl>' + ''.join(f'<dt>{_text(k)}</dt><dd><code>{_text(v)}</code></dd>' for k, v in pairs) + '</dl>'


def _reasons(codes):
    if not codes:
        return 'None recorded'
    return '<ul>' + ''.join(f'<li><code>{_text(c)}</code> <span class="reason-description">{_text(_REASONS[c])}</span></li>\n' for c in codes) + '</ul>'


def _render(batch, store_id):
    report = batch['report']['report']
    definition = batch['definition']
    window = report['window']
    generated = datetime.now(timezone.utc).isoformat()
    sources = {g['group_id']: g for g in batch['catalogue']['catalogue']['families']['steps']}
    sections = []
    for gid in definition['selected_group_ids']:
        descriptor = sources[gid]['descriptor']
        pairs = [('Group ID', gid)]
        for label, slot in zip(('sourceName', 'sourceVersion', 'device'), descriptor):
            pairs.append((label, 'missing' if slot == ['missing'] else ('present (empty string)' if slot[1] == '' else 'present: ' + slot[1])))
        sections.append('<article>' + _fields(pairs) + '</article>')
    rows = []
    for day in report['days']:
        state = day['calculation_state']
        rows.append('<tr class="observation-day">'
                    f'<td><time>{_text(day["date"])}</time></td>'
                    f'<td class="value">{_json(day["value"])}</td>'
                    f'<td class="state">{_text(state)}</td>'
                    f'<td class="reasons">{_reasons(day["reasons"])}</td></tr>')
        rows.append('<tr class="day-details"><td colspan="4"><details>'
                    f'<summary>Details for {_text(day["date"])}</summary>'
                    f'<p>{_text(_STATES[state])}</p>'
                    '<p>Exact daily fields, including UTC boundaries, duration, source counts, repeat candidates, overlap diagnostics and eligibility:</p>'
                    f'<pre>{_json(day)}</pre></details></td></tr>')
    scope = {key: report[key] for key in ('archive_counts', 'window_counts', 'archive_repeat_candidate_extras', 'window_repeat_candidate_extras')}
    identities = [('Store ID', store_id), ('Batch ID', batch['batch_id']), ('Definition ID', batch['definition_id']),
                  ('Archive SHA-256', batch['archive']['sha256']), ('Archive bytes', batch['archive']['bytes']),
                  ('XML member', batch['archive']['xml_member']), ('Catalogue SHA-256', batch['catalogue']['catalogue_sha256']),
                  ('Binding SHA-256', batch['binding']['binding_sha256']), ('Report SHA-256', batch['report']['report_sha256']),
                  ('Report schema', report['schema_version']), ('Policy version', report['policy_version']),
                  ('Producer version', report['producer_version']), ('Pinned timezone SHA-256', report['timezone']['tzif_sha256'])]
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Private Health observations</title><style>{_CSS}</style></head><body><main>
<header><div class="eyebrow">Private snapshot · Health observations</div>
<h1>Selected-source recorded steps</h1>
<p class="muted">One explicitly selected stored batch. Values describe the selected source in this archive.</p>
<div class="notice"><strong>Coverage is unknown.</strong><p><code>observation_completeness="unknown"</code> · <code>valid_metric_day=null</code> · <code>inference_eligible=false</code></p>
<p>Recorded quantities do not establish complete daily activity. Null is not zero. Comparisons, associations and recommendations are unavailable.</p></div></header>
<section><h2>Observation window</h2><div class="grid">{_fields([('First date', window['start']), ('Last date', window['end']), ('as_of (excluded)', window['as_of'])])}{_fields([('Pinned timezone', report['timezone']['key']), ('Unit', report['unit']), ('View generated at (UTC)', generated)])}</div></section>
<section><h2>Daily observations</h2><table><caption>Stored order and exact numeric values. Missing, blocked and skipped dates retain null.</caption>
<thead><tr><th scope="col">Local date</th><th scope="col">Recorded steps</th><th scope="col">Calculation state</th><th scope="col">Reasons</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section><h2>Family reasons</h2><p>These apply to the selected source family; they do not assign an unattributable occurrence to a date.</p>{_reasons(report['family_reasons'])}</section>
<section><h2>Selected source descriptors</h2>{_fields([('Source scope', definition['source_scope']), ('Selection handle', report['selection_handle'])])}
<p>These labels come from the export. Physical-device identity, user intent and Health source priority are not independently verified.</p>
<p><code>device_identity_verified=false</code> · <code>user_intent_verified=false</code> · <code>health_source_priority_verified=false</code></p>{''.join(sections)}</section>
<section><h2>Evidence and limits</h2><p><strong>D2 validated the committed batch and linked evidence when this view was generated.</strong></p>
<p>Rendering does not rerun independent source replay. Execution receipts, replay timestamps and verifier runtime identities are unavailable through this reader. The generation time above describes this view only.</p>
<p>This static file does not refresh or revalidate the backing store when opened. Generate a new view to check the store again. Evidence validation does not authenticate device identity, establish daily completeness, or verify backup or power-loss safety.</p>
<details><summary>Batch and evidence identities</summary>{_fields(identities)}</details>
<details><summary>Archive and window counts</summary><p>Archive counts describe the entire archive; window counts describe the requested window. Repeat candidate extras keep the same separate scopes.</p><pre>{_json(scope)}</pre></details>
<details><summary>Exact measurement definition</summary><pre>{_json(definition)}</pre></details>
<details><summary>Exact report evidence</summary><p>The complete canonical report, including pinned timezone bytes. This preserves the report; it does not replace the immutable D2 batch.</p><pre id="report-json">{_json(report)}</pre></details></section>
<footer>This file contains private Health information. Store it privately. D3 adds presentation only; the project remains DRAFT.</footer>
</main></body></html>'''
    return html.encode('utf-8')
