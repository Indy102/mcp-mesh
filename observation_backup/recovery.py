"""New identity recovery exclusively through fresh public D1/D2 admission."""
from contextlib import ExitStack
import uuid
from observation_store import filesystem as fs, StoreError
import observation_store as store
from adapters.health_observations import InputSet
from . import contracts as c, io, backup
from .validation import evidence

def validate_plan(plan, manifest, data, new_store_id):
    c.closed(plan, 'schema_version backup_id backup_manifest_sha256 original_store_id new_store_id definition_id batches')
    source = manifest['source_manifest']
    c.require(plan['schema_version'] == 'health-observation-recovery-plan-v1')
    c.require(plan['backup_id'] == manifest['backup_id'] and plan['backup_manifest_sha256'] == c.digest(manifest))
    c.require(plan['original_store_id'] == source['store_id'] and plan['new_store_id'] == new_store_id and (new_store_id != source['store_id']))
    c.d2.uuid4(plan['new_store_id'])
    c.require(plan['definition_id'] == source['definition_id'])
    c.require(type(plan['batches']) is list)
    old_ids = data[3]
    seen = set()
    expected = manifest['listing']['batches']
    c.require(len(plan['batches']) == len(expected))
    for item, entry in zip(plan['batches'], expected):
        c.closed(item, 'batch_id batch_sha256 original_attempt_ids new_attempt_id')
        c.require(item['batch_id'] == entry['batch_id'] and item['batch_sha256'] == c.digest(data[2][entry['batch_id']]))
        c.require(item['original_attempt_ids'] == entry['committed_attempt_ids'])
        aid = c.d2.uuid4(item['new_attempt_id'])
        c.require(aid not in old_ids | seen)
        seen.add(aid)
    return plan

def validate_result(result, plan):
    c.closed(result, 'schema_version status plan_sha256 backup_manifest_sha256 new_store_id recovered_batch_ids')
    c.require(result == dict(schema_version='health-observation-recovery-result-v1', status='PASS', plan_sha256=c.digest(plan), backup_manifest_sha256=plan['backup_manifest_sha256'], new_store_id=plan['new_store_id'], recovered_batch_ids=[b['batch_id'] for b in plan['batches']]))

def validate_store(root, plan, manifest, data, store_path):
    with io.tree(root) as (dirs, files):
        directories, entries = io.inventory(dirs, files)
        fresh = evidence(directories, entries, lambda p: fs.read_bytes(files[p][0], files[p][1]))
        expected = dict(manifest['source_manifest'], store_id=plan['new_store_id'])
        c.require(fresh[0] == expected)
        aids = {b['new_attempt_id'] for b in plan['batches']}
        c.require(set(fresh[4]) == aids and set(fresh[5]) == aids and (set(fresh[2]) == set(data[2])))
        receipts = set()
        for item in plan['batches']:
            aid = item['new_attempt_id']
            bid = item['batch_id']
            intent = fresh[4][aid]
            result = fresh[5][aid]
            c.require(result['outcome'] == 'COMMITTED' and intent['batch_id'] == bid)
            receipt = intent['verification']['attempt_id']
            c.require(receipt not in data[3] | aids | receipts)
            receipts.add(receipt)
            c.require(c.canonical(fresh[2][bid]) == c.canonical(data[2][bid]))
            c.require(store.read_batch(store_path, plan['new_store_id'], bid) == c.canonical(data[2][bid]))
        expected_listing = dict(manifest['listing'], store_id=plan['new_store_id'], batches=[dict(entry, committed_attempt_ids=[item['new_attempt_id']]) for item, entry in zip(plan['batches'], manifest['listing']['batches'])])
        c.require(fresh[1] == expected_listing and store.list_batches(store_path, plan['new_store_id']) == c.canonical(expected_listing))
        io.recheck(dirs, files)
        return sorted(dirs), {path: details[2:] for path, details in files.items()}

def final_check(root, store_root, backup_dir, backup_id, new_store_id, recovery_path, require_result=True):
    names = {'plan.json', 'work', 'store'} | ({'result.json'} if require_result else set())
    c.require(set(root.names()) == names)
    with backup.opened_backup(backup_dir, backup_id, True) as (_, objects, manifest, data, read, recheck):
        raw = fs.read_bytes(root, 'plan.json')
        plan = validate_plan(c.json_bytes(raw), manifest, data, new_store_id)
        result_raw = fs.read_bytes(root, 'result.json') if require_result else None
        if require_result:
            validate_result(c.json_bytes(result_raw), plan)
        store_directories, store_files = validate_store(
            store_root, plan, manifest, data, recovery_path / 'store')
        with io.tree(root) as (dirs, files):
            # Carry the semantically validated inventory through the flush interval.
            actual_directories = sorted(path[6:] for path in dirs if path.startswith('store/'))
            actual_directories.insert(0, '')
            actual_files = {path[6:]: details[2:] for path, details in files.items()
                            if path.startswith('store/')}
            c.require(actual_directories == store_directories and actual_files == store_files)
            io.recheck(dirs, files, True)
            recheck(True)
            with fs.CheckedDir.open(recovery_path.parent) as parent:
                parent.flush()
            io.recheck(dirs, files)
            recheck()
        c.require(fs.read_bytes(root, 'plan.json') == raw)
        if require_result:
            c.require(fs.read_bytes(root, 'result.json') == result_raw)
        return plan

@backup.boundary('recover')
def recover_backup(backup_dir, backup_id, recovery_dir, new_store_id):
    backup.ids(backup_id, new_store_id)
    fs.require_platform()
    destination = io.location(recovery_dir, [backup_dir], True)
    with backup.opened_backup(backup_dir, backup_id, True) as (_, objects, manifest, data, read, recheck):
        source = manifest['source_manifest']
        if new_store_id == source['store_id']:
            raise c.BackupError('INPUT_INVALID')
        used = set(data[3])
        batches = []
        for entry in manifest['listing']['batches']:
            aid = str(uuid.uuid4())
            while aid in used:
                aid = str(uuid.uuid4())
            used.add(aid)
            bid = entry['batch_id']
            batches.append(dict(batch_id=bid, batch_sha256=c.digest(data[2][bid]), original_attempt_ids=entry['committed_attempt_ids'], new_attempt_id=aid))
        plan = dict(schema_version='health-observation-recovery-plan-v1', backup_id=backup_id, backup_manifest_sha256=c.digest(manifest), original_store_id=source['store_id'], new_store_id=new_store_id, definition_id=source['definition_id'], batches=batches)
        validate_plan(plan, manifest, data, new_store_id)
        parent, root = fs.reserve_root(destination)
        published = False
        try:
            with root.mkdir('work') as work:
                io.publish(root, work, 'plan.json', plan)
                parent.flush()
                # Public initialize/import own their writer lock; never nest it.
                root.check()
                try:
                    store.initialize(destination / 'store', new_store_id, source['definition'])
                except StoreError:
                    raise c.BackupError('RECOVERY_INCOMPLETE') from None
                root.check()
                for item in batches:
                    batch = data[2][item['batch_id']]
                    with work.mkdir(item['new_attempt_id']) as attempt:
                        for key in ('catalogue', 'binding', 'report'):
                            fs.stage_bytes(attempt, key + '.json', c.canonical(batch[key]))
                        path = destination / 'work' / item['new_attempt_id']
                        report = batch['report']['report']
                        inputs = InputSet(io.location(backup_dir) / 'objects' / (batch['archive']['sha256'] + '.bin'), path / 'catalogue.json', path / 'binding.json', report['timezone']['key'], report['window']['as_of'], report['window']['days'], path / 'report.json')
                        recheck()
                        root.check()
                        attempt.check()
                        try:
                            result = c.json_bytes(store.import_observations(destination / 'store', new_store_id, inputs, source['definition'], item['new_attempt_id']))
                            root.check()
                            attempt.check()
                            if result['outcome'] != 'COMMITTED':
                                raise c.BackupError('REPLAY_FAILED')
                        except StoreError as error:
                            raise c.BackupError('RECOVERY_INCOMPLETE' if error.code in ('PUBLICATION_UNKNOWN', 'STORAGE_ERROR') else 'REPLAY_FAILED') from None
                # Completion owns the same lock through validation, flush, and publication.
                with root.child('store') as sr, fs.writer_lock(sr):
                    final_check(root, sr, backup_dir, backup_id, new_store_id, destination, False)
                    result = dict(schema_version='health-observation-recovery-result-v1', status='PASS', plan_sha256=c.digest(plan), backup_manifest_sha256=c.digest(manifest), new_store_id=new_store_id, recovered_batch_ids=[b['batch_id'] for b in batches])
                    validate_result(result, plan)
                    backup._checkpoint('before-recovery-result')
                    final_check(root, sr, backup_dir, backup_id, new_store_id, destination, False)

                    def completed():
                        nonlocal published
                        published = True
                        backup._publication.set(True)
                        backup._checkpoint('after-recovery-result')
                    io.publish(root, work, 'result.json', result, completed)
                    parent.flush()
                    final_check(root, sr, backup_dir, backup_id, new_store_id, destination)
        except BaseException as error:
            if published or (isinstance(error, fs.PublicationError) and error.linked_may_have_occurred and ('result.json' in root.names())):
                raise c.BackupError('PUBLICATION_UNKNOWN') from None
            raise
        finally:
            root.close()
            parent.close()
    return c.acknowledgement('recover')

@backup.boundary('check-recovery')
def check_recovery(backup_dir, backup_id, recovery_dir, new_store_id):
    backup.ids(backup_id, new_store_id)
    fs.require_platform()
    path = io.location(recovery_dir, [backup_dir])
    with fs.CheckedDir.open(path) as root:
        if 'result.json' not in root.names():
            raise c.BackupError('RECOVERY_INCOMPLETE')
        with root.child('store') as sr, fs.writer_lock(sr):
            final_check(root, sr, backup_dir, backup_id, new_store_id, path)
    return c.acknowledgement('check-recovery')
