"""Validate every historical chain independently of declared listings."""
from . import contracts as c

def evidence(directories, entries, read):
    c.layout(directories, entries)
    bypath = {x['path']: x for x in entries}
    manifest = c.json_bytes(read('manifest.json'))
    c.d2.validate_manifest(manifest)
    c.require(bypath['writer.lock']['size'] == 0)
    intents = {}
    results = {}
    old_ids = set()
    batches = {}
    listed = {}
    for path in bypath:
        if not path.startswith('attempts/'):
            continue
        aid = path.split('/')[1][:-12]
        old_ids.add(aid)
        value = c.json_bytes(read(path))
        if path.endswith('.intent.json'):
            c.d2.validate_intent(value, manifest=manifest, attempt_id=aid)
            intents[aid] = value
            old_ids.add(value['verification']['attempt_id'])
        else:
            results[aid] = value
    for aid, result in results.items():
        intent = intents.get(aid)
        batch = None
        if result.get('verification') is not None:
            old_ids.add(result['verification']['attempt_id'])
        if result.get('outcome') == 'COMMITTED' and intent is not None:
            bid = intent['batch_id']
            batch = c.json_bytes(read('batches/' + bid + '.json'))
            c.d1.validate_batch(batch)
            c.require(batch['batch_id'] == bid)
            archive = batch['archive']
            item = bypath['archives/' + archive['sha256'] + '.zip']
            c.require((item['sha256'], item['size']) == (archive['sha256'], archive['bytes']))
            batches[bid] = batch
            entry = listed.setdefault(bid, dict(batch_id=bid, definition_id=batch['definition_id'], archive_sha256=archive['sha256'], report_sha256=batch['report']['report_sha256'], committed_attempt_ids=[]))
            entry['committed_attempt_ids'].append(aid)
        c.d2.validate_result(result, manifest=manifest, attempt_id=aid, intent=intent, batch=batch)
    for item in listed.values():
        item['committed_attempt_ids'].sort()
    listing = dict(schema_version='health-observation-list-v1', status='PASS', operation='list', store_id=manifest['store_id'], definition_id=manifest['definition_id'], batches=[listed[k] for k in sorted(listed)])
    c.d2.validate_listing(listing, manifest=manifest)
    return (manifest, listing, batches, old_ids, intents, results)
