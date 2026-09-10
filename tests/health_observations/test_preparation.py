"""Invented public-command fixtures and real replay; no mocked verification PASS."""
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from adapters.health_observations import InputSet, PreparedObservations, PreparationError, inspect_definition, prepare_observations
from adapters.health_observations import contracts as c
from adapters.health_observations import preparation as p

ROOT = Path(__file__).resolve().parents[2]


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def sha(value):
    return hashlib.sha256(canon(value)).hexdigest()


def record(source='Invented Wrist', value='0', start='2026-01-01T12:00:00Z', end=None, extra=''):
    return '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="' + source + '" sourceVersion="1" device="Invented hardware" unit="count" value="' + value + '" startDate="' + start + '" endDate="' + (start if end is None else end) + '" ' + extra + '/>'


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='invented-health-d1-')
        cls.folder = Path(cls.temp.name)
        cls.archive, cls.cat, cls.binding = cls.fixture(cls.folder, [record(), record(source='Invented Phone', value='700', start='2026-01-02T12:00:00Z'), record(value='12', start='2026-01-03T12:00:00Z', extra='creationDate="2026-01-03T13:00:00Z"'), record(value='12', start='2026-01-03T12:00:00Z', extra='creationDate="2026-01-04T13:00:00Z"')])
        cls.inputs = InputSet(cls.archive, cls.cat, cls.binding, 'Etc/UTC', '2026-01-04', 3)
        cls.definition = inspect_definition(cls.inputs)
        cls.prepared = prepare_observations(cls.inputs, cls.definition)
        cls.report = cls.folder / 'original-report.json'
        cls.report.write_bytes(canon(cls.prepared.batch['report']))
        cls.supplied = replace(cls.inputs, report=cls.report)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def command(*args, expected=0):
        result = subprocess.run([sys.executable, '-B', *map(str, args)], cwd=ROOT, capture_output=True, timeout=180)
        if result.returncode != expected or result.stderr:
            raise AssertionError((result.returncode, result.stdout, result.stderr))
        return json.loads(result.stdout)

    @classmethod
    def fixture(cls, folder, records):
        archive, cat, request, binding = (folder / f for f in ('archive.zip', 'catalogue.json', 'selection.json', 'binding.json'))
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('export.xml', '<HealthData>' + ''.join(records) + '</HealthData>')
        cls.command(ROOT / 'spikes/health_catalogue/catalogue.py', 'build', '--archive', archive, '--output', cat)
        env = json.loads(cat.read_bytes())
        groups = [g['group_id'] for g in env['catalogue']['families']['steps'] if g['descriptor'][0] == ['present', 'Invented Wrist']]
        request.write_bytes(canon({'selection_schema': 'mesh-health-selection-request-v1', 'archive_sha256': env['catalogue']['archive_sha256'], 'catalogue_sha256': env['catalogue_sha256'], 'selections': {'steps': {'selection_handle': '00000000-0000-4000-8000-000000000010', 'group_ids': sorted(groups), 'source_scope': 'app_stream', 'user_choice': 'Invented explicit source selection.', 'identity_evidence': 'Invented source, no hardware claim.'}}}))
        cls.command(ROOT / 'spikes/health_catalogue/catalogue.py', 'bind', '--archive', archive, '--catalogue', cat, '--request', request, '--output', binding)
        return archive, cat, binding

    def test_exact_zero_phone_absence_repeats_and_coverage(self):
        rows = self.prepared.batch['report']['report']['days']
        self.assertEqual([r['calculation_state'] for r in rows], ['computed', 'no_records', 'blocked'])
        self.assertEqual([r['value'] for r in rows], [0.0, None, None])
        self.assertIs(type(rows[0]['value']), float)
        self.assertEqual(rows[2]['counts']['selected']['occurrences'], 2)
        self.assertEqual(rows[2]['repeat_candidate_extras'], 1)
        self.assertEqual(rows[2]['reasons'], ['selected_quantity_overlap', 'selected_repeat_candidate'])
        for row in rows:
            self.assertEqual(row['observation_completeness'], 'unknown')
            self.assertIsNone(row['valid_metric_day'])
            self.assertIs(row['inference_eligible'], False)

    def test_independent_domain_hashes_and_receipt_separation(self):
        batch = self.prepared.batch
        did = sha({'kind': 'health-observation-definition-v1', 'payload': batch['definition']})
        self.assertEqual(batch['definition_id'], did)
        payload = {'definition_id': did, 'archive_sha256': batch['archive']['sha256'], 'catalogue_sha256': batch['catalogue']['catalogue_sha256'], 'binding_sha256': batch['binding']['binding_sha256'], 'report_sha256': batch['report']['report_sha256']}
        self.assertEqual(batch['batch_id'], sha({'kind': 'health-observation-batch-v1', 'payload': payload}))
        self.assertEqual(set(batch), {'schema_version', 'batch_id', 'definition_id', 'definition', 'archive', 'catalogue', 'binding', 'report'})
        again = prepare_observations(self.supplied, self.definition)
        self.assertEqual(again.batch_bytes, self.prepared.batch_bytes)
        self.assertNotEqual(again.receipt_bytes, self.prepared.receipt_bytes)
        self.assertEqual(again.receipt['batch_sha256'], sha(batch))
        for name, h in again.receipt['code_sha256'].items():
            self.assertEqual(h, hashlib.sha256((ROOT/'spikes'/name).read_bytes()).hexdigest())
        c.validate_prepared(c.strict_load(again.private_bytes))

    def test_immutable_bytes_and_caller_copies(self):
        view = self.prepared.batch
        view['report']['report']['days'][0]['value'] = 1000.0
        self.assertEqual(self.prepared.batch['report']['report']['days'][0]['value'], 0.0)
        with self.assertRaises(FrozenInstanceError):
            self.prepared.batch_bytes = b'{}'
        with self.assertRaises(ValueError):
            PreparedObservations(bytearray(self.prepared.batch_bytes), self.prepared.receipt_bytes)
        with self.assertRaises(TypeError):
            prepare_observations(self.inputs, self.definition, verified=True)

    def test_rehashed_tampering_real_replay(self):
        mutators = [lambda r: r['days'][0].update(value=1.0), lambda r: r['days'][0].update(value=0),
                    lambda r: r['days'][0].update(value=-0.0), lambda r: r['days'][0].update(reasons=['invalid_selected_record']),
                    lambda r: r['days'][0]['counts']['selected'].update(occurrences=2),
                    lambda r: r['days'][0].update(utc_start='2026-01-01T00:00:01.000000Z'),
                    lambda r: r['days'][0].update(inference_eligible=True), lambda r: r['days'][0].update(valid_metric_day=False),
                    lambda r: r['days'][1]['diagnostics'].update(selected_quantity_overlap=0),
                    lambda r: r['days'][2].update(repeat_candidate_extras=0)]
        for i, mutate in enumerate(mutators):
            with self.subTest(i=i), tempfile.TemporaryDirectory() as directory:
                env = copy.deepcopy(self.prepared.batch['report'])
                mutate(env['report'])
                env['report_sha256'] = sha(env['report'])
                path = Path(directory)/'bad.json'; path.write_bytes(canon(env))
                before = path.read_bytes()
                with self.assertRaises(PreparationError) as raised:
                    prepare_observations(replace(self.inputs, report=path), self.definition)
                self.assertEqual(raised.exception.code, 'VERIFICATION_FAILED')
                self.assertEqual(path.read_bytes(), before)

    def test_expected_false_claims_conflict_and_safe_fields(self):
        for field in ('selected_group_ids', 'source_scope', 'timezone'):
            false = copy.deepcopy(self.definition)
            false[field] = {'selected_group_ids': ['0'*64], 'source_scope': 'device', 'timezone': {'key': 'Etc/UTC', 'tzif_sha256': '0'*64}}[field]
            with self.subTest(field=field), self.assertRaises(PreparationError) as raised:
                prepare_observations(self.supplied, false)
            self.assertEqual(raised.exception.code, 'DEFINITION_CONFLICT')
            failure = c.strict_load(raised.exception.failure_bytes)
            c.validate_failure(failure)
            self.assertEqual(failure['conflict']['differing_fields'], [field])
            self.assertEqual(failure['conflict']['actual_definition_id'], self.prepared.batch['definition_id'])
        for edit in (lambda d: d['rules'].update(policy_version='unsupported'), lambda d: d.update(extra='private'), lambda d: d.update(selected_group_ids=['A'*64]), lambda d: d.update(selected_group_ids=[True]), lambda d: d.update(selected_group_ids=['a'*64+'\n'])):
            d = copy.deepcopy(self.definition); edit(d)
            with self.assertRaises(PreparationError) as raised:
                prepare_observations(self.inputs, d)
            self.assertEqual(raised.exception.code, 'DEFINITION_INVALID')

    def test_requested_window_and_timezone_are_independent(self):
        for inputs in (replace(self.supplied, days=2), replace(self.supplied, as_of='2026-01-05'), replace(self.supplied, timezone='UTC')):
            with self.assertRaises(PreparationError) as raised:
                prepare_observations(inputs, self.definition)
            self.assertEqual(raised.exception.code, 'OPERATING_INPUT_MISMATCH')

    def test_old_receipt_and_changed_bytes_cannot_attest(self):
        receipt = self.prepared.receipt
        receipt['batch_sha256'] = '0'*64
        with self.assertRaises(ValueError):
            c.validate_receipt(receipt, self.prepared.batch)
        with self.assertRaises(TypeError):
            prepare_observations(self.inputs, self.definition, receipt=self.prepared.receipt)
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory)/'archive'; bad.write_bytes(self.archive.read_bytes() + b'change')
            with self.assertRaises(PreparationError):
                prepare_observations(replace(self.supplied, archive=bad), self.definition)

    def test_original_inputs_unchanged_and_module_namespace_isolation(self):
        before = {x: (x.read_bytes(), x.stat().st_mode, x.stat().st_mtime_ns) for x in (self.archive, self.cat, self.binding, self.report)}
        modules = {k: sys.modules.get(k) for k in ('catalogue', 'evaluate', 'verify_steps', 'oracle_core', 'steps_core')}
        prepare_observations(self.supplied, self.definition)
        self.assertEqual(modules, {k: sys.modules.get(k) for k in modules})
        self.assertEqual(before, {x: (x.read_bytes(), x.stat().st_mode, x.stat().st_mtime_ns) for x in before})

    def test_original_mutation_and_post_replay_race_refused(self):
        for target in ('original', 'snapshot_report'):
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                paths = {}
                for field in ('archive', 'catalogue', 'binding', 'report'):
                    path = folder/field; path.write_bytes(Path(getattr(self.supplied, field)).read_bytes()); paths[field] = path
                run = p._run
                def mutate(tool, args):
                    run(tool, args)
                    if tool.endswith('verify_steps.py'):
                        path = paths['catalogue'] if target == 'original' else Path(args[-1])
                        path.write_bytes(path.read_bytes() + b' ')
                with patch.object(p, '_run', side_effect=mutate), self.assertRaises(PreparationError) as raised:
                    prepare_observations(replace(self.supplied, **paths), self.definition)
                self.assertEqual(raised.exception.code, 'INPUT_CHANGED')

    def test_unrelated_provenance_and_window_keep_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.fixture(Path(directory), [record(), '<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Invented Pillow"/>'])
            other = InputSet(*paths, 'Etc/UTC', '2026-01-02', 1)
            result = prepare_observations(other, self.definition)
            self.assertEqual(result.batch['definition'], self.definition)
            self.assertNotEqual(result.batch['batch_id'], self.prepared.batch['batch_id'])
        absent = prepare_observations(replace(self.inputs, as_of='2026-01-06', days=1), self.definition)
        self.assertEqual(absent.batch['report']['report']['days'][0]['calculation_state'], 'no_records')
        blocked = prepare_observations(replace(self.inputs, days=1), self.definition)
        self.assertEqual(blocked.batch['report']['report']['days'][0]['calculation_state'], 'blocked')

    def test_skipped_date_plus_family_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.fixture(Path(directory), [record(start='invalid'), record(source='', start='2011-12-30T12:00:00Z')])
            inputs = InputSet(*paths, 'Pacific/Apia', '2011-12-31', 2)
            d = inspect_definition(inputs)
            result = prepare_observations(inputs, d)
            report = result.batch['report']['report']
            self.assertEqual(report['family_reasons'], ['selected_time_unattributable', 'selection_unresolved'])
            self.assertEqual(report['days'][0]['calculation_state'], 'blocked')
            skipped = report['days'][1]
            self.assertEqual(skipped['calculation_state'], 'no_civil_day')
            self.assertEqual(skipped['reasons'], ['no_civil_day'])
            self.assertIsNone(skipped['utc_start'])
            self.assertTrue(all(v == 0 for cnt in skipped['counts'].values() for v in cnt.values()))

    def test_cli_private_success_failure_no_overwrite_and_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            common = ['--archive', self.archive, '--catalogue', self.cat, '--binding', self.binding, '--report', self.report, '--timezone', 'Etc/UTC', '--as-of', '2026-01-04', '--days', '3']
            definition, output, failure = (folder/x for x in ('definition', 'output', 'failure'))
            got = self.command('-m', 'adapters.health_observations', 'inspect-definition', *common, '--output', definition)
            self.assertEqual(got, {'status': 'PASS', 'operation': 'inspect-definition'})
            self.assertEqual(json.loads(definition.read_bytes()), self.definition)
            self.assertEqual(definition.stat().st_mode & 0o777, 0o600)
            got = self.command('-m', 'adapters.health_observations', 'prepare', *common, '--expected-definition', definition, '--output', output)
            self.assertEqual(got, {'status': 'PASS', 'operation': 'prepare'})
            c.validate_prepared(json.loads(output.read_bytes()))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            old = output.read_bytes()
            got = self.command('-m', 'adapters.health_observations', 'prepare', *common, '--expected-definition', definition, '--output', output, expected=2)
            self.assertEqual(got['error'], 'OUTPUT_EXISTS'); self.assertEqual(output.read_bytes(), old)
            wrong = copy.deepcopy(self.definition); wrong['source_scope'] = 'device'; definition.write_bytes(canon(wrong))
            got = self.command('-m', 'adapters.health_observations', 'prepare', *common, '--expected-definition', definition, '--output', folder/'absent', '--failure-output', failure, expected=2)
            self.assertEqual(got, {'status': 'FAIL', 'operation': 'prepare', 'error': 'DEFINITION_CONFLICT'})
            self.assertFalse((folder/'absent').exists()); self.assertEqual(failure.stat().st_mode & 0o777, 0o600)
            c.validate_failure(json.loads(failure.read_bytes()))
            got = self.command('-m', 'adapters.health_observations', 'inspect-definition', *common, '--output', ROOT/'private-forbidden.json', expected=2)
            self.assertEqual(got['error'], 'OUTPUT_NOT_PRIVATE')
            self.assertFalse((ROOT/'private-forbidden.json').exists())

    def test_schema_strict_scalars_private_envelopes_and_corrupt_relationships(self):
        for mutate in (lambda b: b['archive'].update(bytes=1.0), lambda b: b['binding']['binding'].update(user_intent_verified=0),
                       lambda b: b['report']['report']['days'][0].update(value=0), lambda b: b['report']['report']['days'][0].update(inference_eligible=0),
                       lambda b: b.update(batch_id='0'*64), lambda b: b['definition']['rules'].update(estimator='new')):
            batch = self.prepared.batch; mutate(batch)
            with self.assertRaises(ValueError):
                c.validate_batch(batch)
        for blob in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e9999}'):
            with self.assertRaises(ValueError):
                c.strict_load(blob)

    def test_no_rc2_compatible_observed_protocol(self):
        # Exercise supported consumers with the actual prepared object in an isolated process.
        code = """
import sys, json
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path.cwd()/'tools'))
import collector, mesh_config, report
from adapters.health_observations import PreparedObservations
value = json.loads(sys.stdin.read())
prepared = PreparedObservations(json.dumps(value['batch']).encode(), json.dumps(value['receipt']).encode())
assert 'health' not in mesh_config.ADAPTERS
try:
    collector.apply_fetch(None, sid='x', vdid='x', capid='x', run_id='x', source='health', fr=prepared)
except AttributeError:
    pass
else:
    raise AssertionError('collector accepted Health preparation')
try:
    mesh_config.identities({'sources': {'health': {'enabled': True}}}, 'health')
except mesh_config.ConfigError:
    pass
else:
    raise AssertionError('Health registered')
try:
    report.observed_change_from_facts(prepared, metric_id='health', week_start=date(2026,1,1), week_end=date(2026,1,7), min_meaningful_delta=1.0)
except TypeError:
    pass
else:
    raise AssertionError('Health used as facts')
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, input=self.prepared.private_bytes)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_many_computed_days_cannot_promote_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            records = [record(value='1000000', start=f'2026-02-{d:02}T01:00:00Z', end=f'2026-02-{d:02}T23:00:00Z') for d in range(1, 9)]
            paths = self.fixture(Path(directory), records)
            result = prepare_observations(InputSet(*paths, 'Etc/UTC', '2026-02-09', 8), self.definition)
            rows = result.batch['report']['report']['days']
            self.assertEqual(len(rows), 8)
            for row in rows:
                self.assertEqual(row['value'], 1000000.0)
                self.assertEqual(row['observation_completeness'], 'unknown')
                self.assertIsNone(row['valid_metric_day'])
                self.assertIs(row['inference_eligible'], False)

    def test_scratch_modes_cleanup_and_unexpected_diagnostics(self):
        folders = []
        run = p._run
        def inspect_then_run(tool, args):
            folder = Path(args[1]).parent
            folders.append(folder)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            for file in folder.iterdir():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            run(tool, args)
        with patch.object(p, '_run', side_effect=inspect_then_run):
            prepare_observations(self.supplied, self.definition)
        self.assertTrue(folders)
        self.assertTrue(all(not folder.exists() for folder in folders))
        folders.clear()
        def fail(tool, args):
            folders.append(Path(args[1]).parent)
            raise RuntimeError('private path and invented device diagnostic')
        with patch.object(p, '_run', side_effect=fail), self.assertRaises(PreparationError) as caught:
            prepare_observations(self.supplied, self.definition)
        self.assertEqual(str(caught.exception), 'INTERNAL_ERROR')
        self.assertNotIn(b'private path', caught.exception.failure_bytes)
        self.assertTrue(all(not folder.exists() for folder in folders))

    def test_output_alias_and_partial_write_cleanup(self):
        from adapters.health_observations.__main__ import publish_private
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            alias = folder/'alias'
            alias.symlink_to(self.report)
            with self.assertRaises(PreparationError):
                publish_private(alias, b'secret', [self.report])
            with patch('adapters.health_observations.__main__.os.link', side_effect=OSError('private injected error')):
                with self.assertRaises(OSError):
                    publish_private(folder/'output', b'secret', [self.report])
            self.assertEqual(list(folder.iterdir()), [alias])
            alias.unlink()
            os.chmod(folder, 0o755)
            with self.assertRaises(PreparationError) as caught:
                publish_private(folder/'output', b'secret', [])
            self.assertEqual(caught.exception.code, 'OUTPUT_NOT_PRIVATE')

    def test_nonregular_inputs_refuse_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory)/'fifo'; os.mkfifo(fifo)
            with self.assertRaises(PreparationError) as caught:
                prepare_observations(replace(self.supplied, archive=fifo), self.definition)
            self.assertEqual(caught.exception.code, 'INPUT_INVALID')

    def test_metadata_only_ctime_changes_preserve_input_identity(self):
        # Same-mode chmod deterministically changes ctime without changing bytes,
        # inode, size, mtime or mode. This also models incidental filesystem flags.
        import contextlib
        import io
        from adapters.health_observations import __main__ as cli
        for target in ('catalogue', 'expected_definition'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                catalogue = folder/'catalogue'; catalogue.write_bytes(self.cat.read_bytes()); catalogue.chmod(0o600)
                expected = folder/'definition'; expected.write_bytes(canon(self.definition)); expected.chmod(0o600)
                changed = catalogue if target == 'catalogue' else expected
                before = changed.stat(); blob = changed.read_bytes()
                def metadata_change():
                    os.chmod(changed, before.st_mode & 0o777)
                    after = changed.stat()
                    self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
                    self.assertEqual((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode),
                                     (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode))
                    self.assertEqual(changed.read_bytes(), blob)
                if target == 'catalogue':
                    run = p._run
                    def with_metadata(tool, args):
                        run(tool, args)
                        if tool.endswith('verify_steps.py'):
                            metadata_change()
                    with patch.object(p, '_run', side_effect=with_metadata):
                        result = prepare_observations(replace(self.supplied, catalogue=catalogue), self.definition)
                    self.assertEqual(result.batch_bytes, self.prepared.batch_bytes)
                else:
                    def prepare_then_metadata(*args):
                        result = prepare_observations(*args)
                        metadata_change()
                        return result
                    argv = ['prepare', '--archive', str(self.archive), '--catalogue', str(catalogue), '--binding', str(self.binding),
                            '--timezone', 'Etc/UTC', '--as-of', '2026-01-04', '--days', '3', '--report', str(self.report),
                            '--expected-definition', str(expected), '--output', str(folder/'prepared')]
                    out = io.StringIO()
                    with patch.object(cli, 'prepare_observations', side_effect=prepare_then_metadata), contextlib.redirect_stdout(out):
                        status = cli.main(argv)
                    self.assertEqual(status, 0, out.getvalue())
                    self.assertEqual(json.loads((folder/'prepared').read_bytes())['batch'], self.prepared.batch)

    def test_same_size_same_mtime_byte_change_still_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            catalogue = Path(directory)/'catalogue'; catalogue.write_bytes(self.cat.read_bytes()); catalogue.chmod(0o600)
            before = catalogue.stat(); original = catalogue.read_bytes()
            run = p._run
            def mutate(tool, args):
                run(tool, args)
                if tool.endswith('verify_steps.py'):
                    changed = original.replace(b'Invented', b'Imagined')
                    self.assertNotEqual(changed, original)
                    self.assertEqual(len(changed), len(original))
                    catalogue.write_bytes(changed)
                    os.utime(catalogue, ns=(before.st_atime_ns, before.st_mtime_ns))
                    after = catalogue.stat()
                    self.assertEqual((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode),
                                     (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode))
            with patch.object(p, '_run', side_effect=mutate), self.assertRaises(PreparationError) as caught:
                prepare_observations(replace(self.supplied, catalogue=catalogue), self.definition)
            self.assertEqual(caught.exception.code, 'INPUT_CHANGED')

    def test_schema_documents_are_executable(self):
        for schema in c.SCHEMAS.values():
            c.Draft202012Validator.check_schema(schema)


if __name__ == '__main__':
    unittest.main()
