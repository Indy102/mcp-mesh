import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
from observation_store import store as s
from observation_store import __main__ as cli
from observation_store.contracts import canonical
from observation_store.errors import StoreError
from helpers import fixture, ROOT, tree


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.source.name)
        cls.inputs, cls.definition = fixture(cls.folder)
        cls.definition_path = cls.folder / 'definition.json'
        cls.definition_path.write_bytes(canonical(cls.definition))

    @classmethod
    def tearDownClass(cls):
        cls.source.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'store'
        self.sid, self.attempt = str(uuid.uuid4()), str(uuid.uuid4())
        s.initialize(self.path, self.sid, self.definition)
        self.output = Path(self.temp.name) / 'output.json'

    def arguments(self):
        return ['import', '--store', str(self.path), '--store-id', self.sid, '--output', str(self.output), '--definition', str(self.definition_path), '--attempt-id', self.attempt, '--archive', str(self.inputs.archive), '--catalogue', str(self.inputs.catalogue), '--binding', str(self.inputs.binding), '--timezone', 'Etc/UTC', '--as-of', '2026-01-02', '--days', '1']

    def test_subprocess_fixed_envelope_and_private_output(self):
        run = subprocess.run([sys.executable, '-B', '-m', 'observation_store', *self.arguments()], cwd=ROOT, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertEqual(run.stderr, b'')
        self.assertEqual(json.loads(run.stdout), dict(status='PASS', operation='import', error=None))
        self.assertEqual(json.loads(self.output.read_bytes())['outcome'], 'COMMITTED')
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_malformed_arguments_private(self):
        run = subprocess.run([sys.executable, '-B', '-m', 'observation_store', 'secret-private-argument'], cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, b'')
        self.assertEqual(json.loads(run.stdout), dict(status='FAIL', operation='observation_store', error='CLI_USAGE'))

    def test_invalid_output_is_write_free(self):
        self.output = self.path / 'disallowed.json'
        before = tree(self.path)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(self.arguments()), 2)
        self.assertEqual(tree(self.path), before)
        self.assertEqual(json.loads(output.getvalue())['error'], 'OUTPUT_INVALID')

    def test_output_failure_after_commit_uncertain(self):
        with patch.object(cli, '_write', side_effect=StoreError('STORAGE_ERROR')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(self.arguments()), 2)
        self.assertEqual(json.loads(output.getvalue())['error'], 'PUBLICATION_UNKNOWN')
        self.assertEqual(json.loads(s.recover_attempt(self.path, self.sid, self.definition, self.attempt))['outcome'], 'COMMITTED')

    def test_failed_result_is_private_and_exit_failure(self):
        from adapters.health_observations import PreparationError
        with patch.object(s, 'prepare_observations', side_effect=PreparationError('INPUT_INVALID')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(self.arguments()), 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'FAIL')
        self.assertEqual(json.loads(self.output.read_bytes())['outcome'], 'FAILED')

    def test_other_cli_commands_and_failure_output(self):
        def run(operation, *extra):
            output = Path(self.temp.name) / (operation + '.json')
            arguments = [operation, '--store', str(self.path), '--store-id', self.sid, '--output', str(output), *extra]
            with contextlib.redirect_stdout(io.StringIO()) as public:
                code = cli.main(arguments)
            self.assertEqual(code, 0, public.getvalue())
            self.assertEqual(set(json.loads(public.getvalue())), {'status', 'operation', 'error'})
            return json.loads(output.read_bytes())
        run('status')
        result = json.loads(s.import_observations(self.path, self.sid, self.inputs, self.definition, self.attempt))
        self.assertEqual(run('read', '--batch-id', result['batch_id'])['batch_id'], result['batch_id'])
        self.assertEqual(len(run('list')['batches']), 1)
        self.assertEqual(run('recover', '--definition', str(self.definition_path), '--attempt-id', self.attempt)['outcome'], 'COMMITTED')
        failure = Path(self.temp.name) / 'failure.json'
        with contextlib.redirect_stdout(io.StringIO()) as public:
            self.assertEqual(cli.main([*self.arguments(), '--failure-output', str(failure)]), 2)
        self.assertEqual(json.loads(failure.read_bytes())['error'], 'ATTEMPT_EXISTS')
        self.path = Path(self.temp.name) / 'new-store'
        self.assertTrue(run('init', '--definition', str(self.definition_path))['initialized'])


if __name__ == '__main__':
    unittest.main()
