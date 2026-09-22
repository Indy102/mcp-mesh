import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
from observation_store import store as s
from observation_store import filesystem as fs
from observation_store.errors import StoreError
from helpers import fixture, tree


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = tempfile.TemporaryDirectory()
        cls.inputs, cls.definition = fixture(Path(cls.source.name))

    @classmethod
    def tearDownClass(cls):
        cls.source.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'store'
        self.id = str(uuid.uuid4())
        s.initialize(self.path, self.id, self.definition)

    def attempt(self):
        return str(uuid.uuid4())

    def admit(self, attempt=None):
        return json.loads(s.import_observations(self.path, self.id, self.inputs, self.definition, attempt or self.attempt()))

    def test_roundtrip_fresh_receipts_and_source_independence(self):
        with patch.object(s, 'prepare_observations', wraps=s.prepare_observations) as replay:
            first, second = self.admit(), self.admit()
        self.assertEqual(replay.call_count, 2)
        self.assertEqual(first['outcome'], 'COMMITTED')
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertNotEqual(first['verification']['attempt_id'], second['verification']['attempt_id'])
        batch = json.loads(s.read_batch(self.path, self.id, first['batch_id']))
        self.assertEqual((self.path / 'archives' / (batch['archive']['sha256'] + '.zip')).read_bytes(), self.inputs.archive.read_bytes())
        listing = json.loads(s.list_batches(self.path, self.id))
        self.assertEqual(len(listing['batches']), 1)
        self.assertEqual(len(listing['batches'][0]['committed_attempt_ids']), 2)
        recovered = json.loads(s.recover_attempt(self.path, self.id, self.definition, first['attempt_id']))
        self.assertEqual(recovered['outcome'], 'COMMITTED')

    def test_definition_and_duplicate_refusals_write_free(self):
        altered = copy.deepcopy(self.definition)
        altered['timezone']['key'] = 'America/New_York'
        before = tree(self.path)
        with self.assertRaises(StoreError) as error:
            s.import_observations(self.path, self.id, self.inputs, altered, self.attempt())
        self.assertEqual(error.exception.code, 'DEFINITION_CONFLICT')
        self.assertEqual(tree(self.path), before)
        first = self.admit()
        before = tree(self.path)
        with self.assertRaises(StoreError) as error:
            self.admit(first['attempt_id'])
        self.assertEqual(error.exception.code, 'ATTEMPT_EXISTS')
        self.assertEqual(tree(self.path), before)

    def test_failure_and_failure_record_failure(self):
        from adapters.health_observations import PreparationError
        with patch.object(s, 'prepare_observations', side_effect=PreparationError('INPUT_INVALID')):
            failed = self.admit()
        self.assertEqual(failed['outcome'], 'FAILED')
        self.assertIsNone(failed['batch_id'])
        with patch.object(s, 'prepare_observations', side_effect=PreparationError('INPUT_INVALID')), patch.object(s, '_record', side_effect=StoreError('STORAGE_ERROR')):
            with self.assertRaises(StoreError) as error:
                self.admit()
        self.assertEqual(error.exception.code, 'STORAGE_ERROR')

    def test_unknown_success_recovery(self):
        attempt = self.attempt()
        original = fs.publish
        def fail_after(source, name, destination, target, **kwargs):
            result = original(source, name, destination, target, **kwargs)
            if target.endswith('.result.json'):
                raise fs.PublicationError('STORAGE_ERROR', linked_may_have_occurred=True)
            return result
        with patch.object(fs, 'publish', side_effect=fail_after):
            with self.assertRaises(StoreError) as error:
                self.admit(attempt)
        self.assertEqual(error.exception.code, 'PUBLICATION_UNKNOWN')
        self.assertEqual(json.loads(s.recover_attempt(self.path, self.id, self.definition, attempt))['outcome'], 'COMMITTED')

    def test_corruption_not_old_fallback(self):
        first = self.admit()
        batch = self.path / 'batches' / (first['batch_id'] + '.json')
        batch.write_bytes(b'{}')
        for fn in (lambda: s.read_batch(self.path, self.id, first['batch_id']), lambda: s.list_batches(self.path, self.id), lambda: s.recover_attempt(self.path, self.id, self.definition, first['attempt_id'])):
            with self.assertRaises(StoreError) as error:
                fn()
            self.assertEqual(error.exception.code, 'INTEGRITY_ERROR')
        self.assertEqual(batch.read_bytes(), b'{}')

    def test_real_thread_alias_and_nested_lock(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.path)
        entered, release = threading.Event(), threading.Event()
        failures = []
        def checkpoint(phase):
            if phase == 'prepared':
                with self.assertRaises(StoreError) as error:
                    s.recover_attempt(alias, self.id, self.definition, self.attempt())
                self.assertEqual(error.exception.code, 'NESTED_OPERATION')
                entered.set()
                if not release.wait(15):
                    raise AssertionError('bounded wait expired')
        def worker():
            try:
                self.admit()
            except BaseException as error:
                failures.append(error)
        with patch.object(s, '_checkpoint', side_effect=checkpoint):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(30))
                with self.assertRaises(StoreError) as error:
                    s.recover_attempt(alias, self.id, self.definition, self.attempt())
                self.assertEqual(error.exception.code, 'WRITER_BUSY')
                self.assertEqual(json.loads(s.list_batches(alias, self.id))['batches'], [])
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_partial_and_not_recorded(self):
        self.assertEqual(json.loads(s.recover_attempt(self.path, self.id, self.definition, self.attempt()))['outcome'], 'NOT_RECORDED')
        partial = Path(self.temp.name) / 'partial'
        partial.mkdir(mode=0o700)
        with self.assertRaises(StoreError) as error:
            s.status(partial, self.id)
        self.assertEqual(error.exception.code, 'UNINITIALIZED')

    def test_actual_definition_conflict_is_write_free(self):
        altered = copy.deepcopy(self.definition)
        altered['source_scope'] = 'device'
        other = Path(self.temp.name) / 'other'
        s.initialize(other, self.id, altered)
        before = tree(other)
        with self.assertRaises(StoreError) as error:
            s.import_observations(other, self.id, self.inputs, altered, self.attempt())
        self.assertEqual(error.exception.code, 'DEFINITION_CONFLICT')
        self.assertEqual(tree(other), before)
        before = tree(self.path)
        with self.assertRaises(StoreError) as error:
            s.import_observations(self.path, self.id, object(), self.definition, self.attempt())
        self.assertEqual(error.exception.code, 'INPUT_INVALID')
        self.assertEqual(tree(self.path), before)

    def test_source_removed_and_new_blocked_batch_no_fallback(self):
        first = self.admit()
        with tempfile.TemporaryDirectory() as source:
            inputs, definition = fixture(Path(source), repeated=True)
            self.assertEqual(definition, self.definition)
            newer = json.loads(s.import_observations(self.path, self.id, inputs, definition, self.attempt()))
            batch_id = newer['batch_id']
            archived = inputs.archive.read_bytes()
        self.assertNotEqual(first['batch_id'], batch_id)
        newer = json.loads(s.read_batch(self.path, self.id, batch_id))
        row = newer['report']['report']['days'][0]
        self.assertEqual(row['calculation_state'], 'blocked')
        self.assertIsNone(row['value'])
        self.assertEqual((self.path / 'archives' / (newer['archive']['sha256'] + '.zip')).read_bytes(), archived)
        prior = json.loads(s.read_batch(self.path, self.id, first['batch_id']))
        self.assertEqual(prior['report']['report']['days'][0]['value'], 14.0)

    def test_archive_change_after_preparation_and_before_result(self):
        for phase in ('prepared', 'before-result'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as source:
                inputs, definition = fixture(Path(source))
                def checkpoint(current):
                    if current == phase:
                        with inputs.archive.open('ab') as stream:
                            stream.write(b'changed')
                with patch.object(s, '_checkpoint', side_effect=checkpoint):
                    failed = json.loads(s.import_observations(self.path, self.id, inputs, definition, self.attempt()))
                self.assertEqual(failed['outcome'], 'FAILED')
                self.assertEqual(failed['error'], 'INPUT_CHANGED')
                self.assertEqual(json.loads(s.list_batches(self.path, self.id))['batches'], [])

    def test_failed_new_attempt_preserves_prior_batch(self):
        first = self.admit()
        before = s.read_batch(self.path, self.id, first['batch_id'])
        from adapters.health_observations import PreparationError
        with patch.object(s, 'prepare_observations', side_effect=PreparationError('INPUT_INVALID')):
            failed = self.admit()
        self.assertIsNone(failed['batch_id'])
        self.assertEqual(s.read_batch(self.path, self.id, first['batch_id']), before)
        self.assertEqual(json.loads(s.recover_attempt(self.path, self.id, self.definition, failed['attempt_id']))['outcome'], 'FAILED')

    def test_missing_modes_hardlinks_and_unknown_names(self):
        first = self.admit()
        batch = self.path / 'batches' / (first['batch_id'] + '.json')
        batch.chmod(0o644)
        with self.assertRaises(StoreError):
            s.list_batches(self.path, self.id)
        batch.chmod(0o600)
        alias = Path(self.temp.name) / 'hardlink'
        os.link(batch, alias)
        with self.assertRaises(StoreError):
            s.read_batch(self.path, self.id, first['batch_id'])
        alias.unlink()
        unknown = self.path / 'attempts' / 'unknown'
        unknown.write_bytes(b'')
        unknown.chmod(0o600)
        with self.assertRaises(StoreError):
            s.list_batches(self.path, self.id)
        unknown.unlink()
        batch.unlink()
        with self.assertRaises(StoreError):
            s.recover_attempt(self.path, self.id, self.definition, first['attempt_id'])

    def test_corrupt_immutable_reuse_records_failure_without_clobber(self):
        first = self.admit()
        batch = self.path / 'batches' / (first['batch_id'] + '.json')
        batch.write_bytes(b'corrupt')
        failed = self.admit()
        self.assertEqual(failed['outcome'], 'FAILED')
        self.assertEqual(failed['error'], 'INTEGRITY_ERROR')
        self.assertIsNone(failed['batch_id'])
        self.assertEqual(batch.read_bytes(), b'corrupt')

    def test_manifest_tampering_before_intent_never_commits(self):
        def checkpoint(phase):
            if phase == 'prepared':
                manifest = self.path / 'manifest.json'
                manifest.write_bytes(b'{}')
        with patch.object(s, '_checkpoint', side_effect=checkpoint):
            with self.assertRaises(StoreError):
                self.admit()
        self.assertEqual(list((self.path / 'attempts').iterdir()), [])

    def test_initialization_unknown_status_reflush(self):
        other = Path(self.temp.name) / 'unknown'
        original = fs.publish
        def fail_after(source, name, destination, target, **kwargs):
            result = original(source, name, destination, target, **kwargs)
            if target == 'manifest.json':
                raise fs.PublicationError('STORAGE_ERROR', linked_may_have_occurred=True)
            return result
        with patch.object(fs, 'publish', side_effect=fail_after):
            with self.assertRaises(StoreError) as error:
                s.initialize(other, self.id, self.definition)
        self.assertEqual(error.exception.code, 'INITIALIZATION_UNKNOWN')
        self.assertTrue(json.loads(s.status(other, self.id))['initialized'])

    def test_terminal_flush_failures_remain_unknown(self):
        attempt = self.attempt()
        original = fs.flush_file
        def broken(directory, name):
            if name.endswith('.result.json'):
                raise StoreError('STORAGE_ERROR')
            return original(directory, name)
        with patch.object(fs, 'flush_file', side_effect=broken):
            with self.assertRaises(StoreError) as error:
                self.admit(attempt)
            self.assertEqual(error.exception.code, 'PUBLICATION_UNKNOWN')
            with self.assertRaises(StoreError) as error:
                s.recover_attempt(self.path, self.id, self.definition, attempt)
            self.assertEqual(error.exception.code, 'PUBLICATION_UNKNOWN')
        self.assertEqual(json.loads(s.recover_attempt(self.path, self.id, self.definition, attempt))['outcome'], 'COMMITTED')

    def test_intent_write_failure_no_false_failure_evidence(self):
        import errno
        with patch.object(fs.os, 'write', side_effect=OSError(errno.ENOSPC, 'private value')):
            with self.assertRaises(StoreError) as error:
                self.admit()
        self.assertEqual(error.exception.code, 'STORAGE_ERROR')
        self.assertEqual(list((self.path / 'attempts').iterdir()), [])

    def test_published_intent_must_remain_exact_for_success_and_failure(self):
        for mutation in ('malformed', 'deleted', 'different_receipt'):
            for fallback in (False, True):
                with self.subTest(mutation=mutation, fallback=fallback):
                    attempt = self.attempt()
                    target = self.path / 'attempts' / (attempt + '.intent.json')
                    def checkpoint(phase):
                        if phase != 'after-intent':
                            return
                        before = target.stat()
                        if mutation == 'deleted':
                            target.unlink()
                        else:
                            if mutation == 'malformed':
                                blob = b'{}'
                            else:
                                altered = json.loads(target.read_bytes())
                                altered['verification']['attempt_id'] = self.attempt()
                                # Structurally valid and same batch, but not this fresh execution.
                                s.c.validate_intent(altered)
                                blob = s.c.canonical(altered)
                            target.write_bytes(blob)
                            self.assertEqual(target.stat().st_ino, before.st_ino)
                        if fallback:
                            raise StoreError('INPUT_CHANGED')
                    with patch.object(s, '_checkpoint', side_effect=checkpoint):
                        with self.assertRaises(StoreError) as error:
                            self.admit(attempt)
                    self.assertEqual(error.exception.code, 'STORAGE_ERROR')
                    self.assertFalse((self.path / 'attempts' / (attempt + '.result.json')).exists())
                    # Remove only this test's tampered intent to isolate the next probe.
                    if target.exists():
                        target.unlink()

    def test_intent_changed_during_result_staging_is_not_adopted(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                attempt = self.attempt()
                target = self.path / 'attempts' / (attempt + '.intent.json')
                original = fs.stage_bytes
                def changed(directory, name, blob):
                    original(directory, name, blob)
                    if directory.name == 'staging' and name.endswith('.json') and not name.endswith('.intent.json'):
                        altered = json.loads(target.read_bytes())
                        altered['verification']['attempt_id'] = self.attempt()
                        s.c.validate_intent(altered)
                        target.write_bytes(s.c.canonical(altered))
                def checkpoint(phase):
                    if fallback and phase == 'after-intent':
                        raise StoreError('INPUT_CHANGED')
                with patch.object(fs, 'stage_bytes', side_effect=changed), patch.object(s, '_checkpoint', side_effect=checkpoint):
                    with self.assertRaises(StoreError) as error:
                        self.admit(attempt)
                self.assertEqual(error.exception.code, 'STORAGE_ERROR')
                self.assertFalse((self.path / 'attempts' / (attempt + '.result.json')).exists())
                target.unlink()

    def test_intent_damage_with_linked_terminal_failure_stays_unknown(self):
        attempt = self.attempt()
        target = self.path / 'attempts' / (attempt + '.intent.json')
        def checkpoint(phase):
            if phase == 'after-result-publish':
                target.write_bytes(b'{}')
                raise StoreError('STORAGE_ERROR')
        with patch.object(s, '_checkpoint', side_effect=checkpoint):
            with self.assertRaises(StoreError) as error:
                self.admit(attempt)
        self.assertEqual(error.exception.code, 'PUBLICATION_UNKNOWN')
        result = json.loads((self.path / 'attempts' / (attempt + '.result.json')).read_bytes())
        self.assertEqual(result['outcome'], 'COMMITTED')
        self.assertEqual(len(list((self.path / 'attempts').glob('*.result.json'))), 1)
        with self.assertRaises(StoreError) as error:
            s.recover_attempt(self.path, self.id, self.definition, attempt)
        self.assertEqual(error.exception.code, 'INTEGRITY_ERROR')


if __name__ == '__main__':
    unittest.main()
