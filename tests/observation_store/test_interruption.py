import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
import uuid
from observation_store import store as s
from observation_store.errors import StoreError
from observation_store.contracts import canonical
from helpers import fixture, ROOT


class InterruptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name)
        _, cls.definition = fixture(cls.folder)
        (cls.folder / 'definition.json').write_bytes(canonical(cls.definition))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def kill_at(self, operation, phase, path, store_id, attempt):
        child = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tests/observation_store/worker.py'), operation, phase, str(path), store_id, attempt, str(self.folder)], cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)}, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(30), 'worker checkpoint timed out')
                self.assertEqual(child.stdout.readline(), b'READY\n', child.stderr.read() if child.poll() is not None else b'')
            if operation == 'import':
                with self.assertRaises(StoreError) as error:
                    s.recover_attempt(path, store_id, self.definition, attempt)
                self.assertEqual(error.exception.code, 'WRITER_BUSY')
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_every_initialization_checkpoint(self):
        for phase in ('initialized-root', 'before-manifest', 'after-manifest-publish'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                path, sid = Path(directory) / 'store', str(uuid.uuid4())
                self.kill_at('init', phase, path, sid, str(uuid.uuid4()))
                if phase == 'after-manifest-publish':
                    self.assertTrue(json.loads(s.status(path, sid))['initialized'])
                else:
                    with self.assertRaises(StoreError) as error:
                        s.status(path, sid)
                    self.assertEqual(error.exception.code, 'UNINITIALIZED')
                with self.assertRaises(StoreError) as error:
                    s.initialize(path, sid, self.definition)
                self.assertEqual(error.exception.code, 'STORE_EXISTS')

    def test_every_import_checkpoint_no_orphan_promotion(self):
        for phase in ('prepared', 'after-intent', 'after-archive', 'after-batch', 'before-result', 'after-result-publish', 'after-result-flush'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                path, sid, attempt = Path(directory) / 'store', str(uuid.uuid4()), str(uuid.uuid4())
                s.initialize(path, sid, self.definition)
                self.kill_at('import', phase, path, sid, attempt)
                recovery = json.loads(s.recover_attempt(path, sid, self.definition, attempt))
                expected = 'NOT_RECORDED' if phase == 'prepared' else ('COMMITTED' if phase.startswith('after-result') else 'INTERRUPTED')
                self.assertEqual(recovery['outcome'], expected)
                listing = json.loads(s.list_batches(path, sid))
                self.assertEqual(len(listing['batches']), int(expected == 'COMMITTED'))
                if expected == 'COMMITTED':
                    s.read_batch(path, sid, recovery['batch_id'])


if __name__ == '__main__':
    unittest.main()
