"""Synthetic public-store integration and private presentation failure boundaries."""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
from html.parser import HTMLParser

from adapters.health_observations.contracts import canonical
import observation_store
from observation_store import filesystem as fs
from observation_store import store as store_impl
from observation_store.errors import StoreError
from observation_view import create_view, ViewError
from observation_view import rendering, view

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('d2_synthetic_helpers', ROOT / 'tests/observation_store/helpers.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Evidence(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags, self.attributes, self.pre, self.current = [], [], [], None
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)
        if tag == 'pre':
            self.current = []
    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)
    def handle_endtag(self, tag):
        if tag == 'pre':
            self.pre.append(json.loads(''.join(self.current)))
            self.current = None


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix='health-view-fixture-')
        base = Path(cls.shared.name)
        inputs_dir = base / 'inputs'; inputs_dir.mkdir(mode=0o700)
        cls.inputs, cls.definition = helper.fixture(inputs_dir)
        cls.sid = str(uuid.uuid4())
        cls.original = base / 'store'
        observation_store.initialize(cls.original, cls.sid, cls.definition)
        result = json.loads(observation_store.import_observations(cls.original, cls.sid, cls.inputs, cls.definition, str(uuid.uuid4())))
        cls.bid = result['batch_id']
        cls.batch = json.loads(observation_store.read_batch(cls.original, cls.sid, cls.bid))
        repeated = base / 'repeated'; repeated.mkdir(mode=0o700)
        cls.repeated, _ = helper.fixture(repeated, repeated=True)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='health-view-test-')
        self.parent = Path(self.temp.name)
        self.store = self.parent / 'store'
        shutil.copytree(self.original, self.store)
        self.output = self.parent / 'view.html'
        self.before = helper.tree(self.store)
        self.check_store_unchanged = True

    def tearDown(self):
        try:
            if self.check_store_unchanged:
                self.assertEqual(self.before, helper.tree(self.store))
        finally:
            self.temp.cleanup()

    def render(self, **options):
        return create_view(options.get('store', self.store), options.get('sid', self.sid),
                           options.get('bid', self.bid), options.get('output', self.output))

    def refusal(self, expected=None, **options):
        with self.assertRaises(ViewError) as raised:
            self.render(**options)
        if expected:
            self.assertEqual(raised.exception.code, expected)
        self.assertEqual(str(raised.exception), raised.exception.code)
        self.assertEqual(json.loads(raised.exception.failure_bytes),
                         {'status': 'FAIL', 'operation': 'view', 'error': raised.exception.code})
        return raised.exception.code

    def test_real_public_read_exact_report_and_private_publication(self):
        self.assertEqual(json.loads(self.render()), {'status': 'PASS', 'operation': 'view', 'error': None})
        parsed = Evidence(self.output.read_text())
        self.assertIn(self.batch['report']['report'], parsed.pre)
        self.assertIn(self.batch['report']['report']['days'][0], parsed.pre)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(self.output.stat().st_nlink, 1)
        self.assertNotIn('script', parsed.tags)

    def test_only_one_requested_read_and_no_store_mutators(self):
        with patch.object(observation_store, 'read_batch', wraps=observation_store.read_batch) as read:
            with patch.multiple(observation_store, initialize=None, status=None, import_observations=None,
                                list_batches=None, recover_attempt=None):
                self.render()
        read.assert_called_once_with(self.store, self.sid, self.bid)

    def test_invalid_ids_refused_before_read(self):
        with patch.object(observation_store, 'read_batch') as read:
            for sid, bid in [(None, self.bid), ('wrong', self.bid), (self.sid.upper(), self.bid),
                             (self.sid, True), (self.sid, self.bid.upper()), (self.sid, '../private')]:
                with self.subTest(sid=sid, bid=bid):
                    self.refusal('INPUT_INVALID', sid=sid, bid=bid)
            read.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_wrong_store_and_missing_batch_fail_without_output(self):
        self.refusal('STORE_ID_MISMATCH', sid=str(uuid.uuid4()))
        self.refusal('BATCH_NOT_COMMITTED', bid='0'*64)
        self.assertFalse(self.output.exists())

    def test_malformed_or_wrong_identity_read_result_is_rejected(self):
        for raw in (b'{"status":"PASS"}', canonical(self.batch)):
            with patch.object(observation_store, 'read_batch', return_value=raw):
                self.refusal('INTEGRITY_ERROR', bid='0'*64)
        self.assertFalse(self.output.exists())

    def test_corruption_has_no_fallback_or_store_repair(self):
        archive = next((self.store / 'archives').iterdir())
        archive.write_bytes(b'corrupt')
        self.before = helper.tree(self.store)
        self.refusal('INTEGRITY_ERROR')
        self.assertFalse(self.output.exists())

    def test_existing_regular_symlink_hardlink_and_directory_untouched(self):
        original = self.parent / 'sentinel'; original.write_bytes(b'untouched'); original.chmod(0o600)
        destinations = [self.parent / n for n in ('regular', 'symlink', 'hardlink', 'directory')]
        destinations[0].write_bytes(b'existing')
        destinations[1].symlink_to(self.parent / 'absent')
        os.link(original, destinations[2]); destinations[3].mkdir(mode=0o700)
        with patch.object(observation_store, 'read_batch') as read:
            for target in destinations:
                self.refusal('OUTPUT_EXISTS', output=target)
            read.assert_not_called()
        self.assertEqual(original.read_bytes(), b'untouched')
        self.assertEqual(destinations[0].read_bytes(), b'existing')
        self.assertTrue(destinations[1].is_symlink())

    def test_nonprivate_missing_and_nondirectory_parent_refused_before_read(self):
        bad = self.parent / 'public'; bad.mkdir(mode=0o755)
        file = self.parent / 'file'; file.write_bytes(b'x')
        with patch.object(observation_store, 'read_batch') as read:
            for parent in (bad, self.parent / 'missing', file):
                self.refusal('OUTPUT_NOT_PRIVATE', output=parent / 'view.html')
            read.assert_not_called()

    def test_selected_store_other_store_code_and_alias_outputs_refused(self):
        other = self.parent / 'other'; shutil.copytree(self.original, other)
        checkout = self.parent / 'checkout'; checkout.mkdir(mode=0o700); (checkout / '.git').mkdir()
        for i, parent in enumerate((self.store, other, ROOT, checkout)):
            alias = self.parent / ('alias'+str(i)); alias.symlink_to(parent, target_is_directory=True)
            for place in (parent, alias):
                with self.subTest(place=place), patch.object(observation_store, 'read_batch') as read:
                    self.refusal('OUTPUT_INVALID', output=place / 'private-new.html')
                    read.assert_not_called()

    def test_marker_free_executing_source_and_alias_refused(self):
        source = self.parent / 'source'; source.mkdir(mode=0o700)
        for name in ('observation_store', 'observation_view', 'adapters'):
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        private = source / 'private'; private.mkdir(mode=0o700)
        alias = self.parent / 'source-alias'; alias.symlink_to(source, target_is_directory=True)
        for output in (private / 'view.html', alias / 'private/view.html'):
            result = self.cli(output=output, cwd=source)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['error'], 'OUTPUT_INVALID')
            self.assertEqual(result.stderr, b'')
            self.assertFalse(output.exists())

    def test_directory_replacement_after_read_is_detected(self):
        target = self.parent / 'private'; target.mkdir(mode=0o700)
        def read(*args):
            raw = observation_store_read(*args)
            target.rename(self.parent / 'moved'); target.mkdir(mode=0o700)
            return raw
        observation_store_read = observation_store.read_batch
        with patch.object(observation_store, 'read_batch', side_effect=read):
            self.refusal('INTEGRITY_ERROR', output=target / 'view.html')
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list((self.parent / 'moved').iterdir()), [])

    def test_destination_reclassified_as_store_after_read_is_refused(self):
        parent = self.parent / 'output'; parent.mkdir(mode=0o700)
        read_batch = observation_store.read_batch
        def read(*args):
            raw = read_batch(*args)
            (parent / 'manifest.json').write_bytes(b'{}'); (parent / 'writer.lock').touch()
            return raw
        with patch.object(observation_store, 'read_batch', side_effect=read):
            self.refusal('OUTPUT_INVALID', output=parent / 'view.html')

    def test_stage_failure_no_success_or_destination(self):
        with patch.object(fs, 'stage_bytes', side_effect=StoreError('STORAGE_ERROR')):
            self.refusal('STORAGE_ERROR')
        self.assertFalse(self.output.exists())

    def test_short_writes_complete_without_truncation(self):
        write = os.write
        with patch.object(fs.os, 'write', side_effect=lambda fd, blob: write(fd, blob[:41])):
            self.render()
        self.assertIn(self.batch['report']['report'], Evidence(self.output.read_text()).pre)

    def test_zero_progress_write_leaves_no_published_output(self):
        with patch.object(fs.os, 'write', return_value=0):
            self.refusal('STORAGE_ERROR')
        self.assertFalse(self.output.exists())

    def test_before_link_publication_failure_is_not_unknown(self):
        with patch.object(fs, 'publish', side_effect=fs.PublicationError('STORAGE_ERROR', linked_may_have_occurred=False)):
            self.refusal('STORAGE_ERROR')
        self.assertFalse(self.output.exists())

    def test_real_link_then_flush_failure_reports_uncertainty(self):
        flush = fs.CheckedDir.flush
        def fault(parent):
            if self.output.exists():
                raise StoreError('STORAGE_ERROR')
            return flush(parent)
        with patch.object(fs.CheckedDir, 'flush', fault):
            self.refusal('OUTPUT_PUBLICATION_UNKNOWN')
        self.assertIn(self.batch['report']['report'], Evidence(self.output.read_text()).pre)

    def test_unexpected_error_around_publish_is_conservatively_unknown(self):
        with patch.object(fs, 'publish', side_effect=RuntimeError('/sensitive/argument')):
            self.refusal('OUTPUT_PUBLICATION_UNKNOWN')
        self.assertFalse(self.output.exists())

    def test_destination_race_has_one_winner_without_clobber(self):
        barrier = threading.Barrier(2)
        publish = fs.publish
        def race(*args, **kwargs):
            barrier.wait(timeout=10)
            return publish(*args, **kwargs)
        def run():
            try:
                self.render(); return 'PASS'
            except ViewError as error:
                return error.code
        with patch.object(fs, 'publish', side_effect=race), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            outcomes = [f.result(timeout=20) for f in futures]
        self.assertCountEqual(outcomes, ['PASS', 'OUTPUT_EXISTS'])
        self.assertIn(self.batch['report']['report'], Evidence(self.output.read_text()).pre)

    def test_read_older_batch_during_newer_publication_keeps_older_values(self):
        self.check_store_unchanged = False
        reached, release = threading.Event(), threading.Event()
        def checkpoint(phase):
            if phase == 'before-result':
                reached.set()
                if not release.wait(30):
                    raise RuntimeError('test synchronization timeout')
        with patch.object(store_impl, '_checkpoint', side_effect=checkpoint), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(observation_store.import_observations, self.store, self.sid,
                                 self.repeated, self.definition, str(uuid.uuid4()))
            try:
                self.assertTrue(reached.wait(30))
                self.render()
                self.assertIn(self.batch['report']['report'], Evidence(self.output.read_text()).pre)
            finally:
                release.set()
            newer = json.loads(future.result(timeout=30))
        self.assertNotEqual(newer['batch_id'], self.bid)
        other = self.parent / 'newer.html'
        self.render(bid=newer['batch_id'], output=other)
        report = json.loads(observation_store.read_batch(self.store, self.sid, newer['batch_id']))['report']['report']
        self.assertEqual(report['days'][0]['calculation_state'], 'blocked')
        self.assertIsNone(report['days'][0]['value'])
        self.assertIn(report, Evidence(other.read_text()).pre)

    def test_private_renderer_preserves_states_values_and_diagnostic_fields(self):
        batch = copy.deepcopy(self.batch)
        original = batch['report']['report']['days'][0]
        days = []
        for state, value in [('computed', 0.0), ('computed', 0.10000000000000002),
                             ('no_records', None), ('blocked', None), ('no_civil_day', None)]:
            day = copy.deepcopy(original);day.update(calculation_state=state, value=value)
            day['reasons'] = ['selected_quantity_overlap'] if state == 'blocked' else (['no_civil_day'] if state == 'no_civil_day' else [])
            day['diagnostics']['selected_excluded_overlap'] = None
            if state == 'no_civil_day':
                day.update(utc_start=None, utc_end=None, utc_duration_seconds=0)
            days.append(day)
        batch['report']['report']['days'] = days
        html = rendering._render(batch, self.sid).decode()
        parsed = Evidence(html)
        for day in days:
            self.assertIn(day, parsed.pre)
        self.assertIn('>0.10000000000000002</td>', html)
        self.assertIn('>0.0</td>', html)
        self.assertEqual(html.count('class="observation-day"'), 5)
        # Pure-renderer fixtures test presentation only; public API validates batches.

    def test_hostile_source_labels_stay_text_and_controls_are_visible(self):
        batch = copy.deepcopy(self.batch)
        group = batch['catalogue']['catalogue']['families']['steps'][0]
        group['descriptor'] = [['present', '<script src="https://invalid.test">x</script>\u202e\x00'], ['present', ''], ['missing']]
        html = rendering._render(batch, self.sid).decode()
        parsed = Evidence(html)
        self.assertNotIn('script', parsed.tags)
        self.assertFalse(any(k.startswith('on') or k in ('href', 'src') for k,v in parsed.attributes))
        self.assertIn('\\u202e\\u0000', html)
        self.assertIn('present (empty string)', html)
        self.assertIn('missing', html)

    def test_every_reason_is_explained_and_keeps_readable_code_boundary(self):
        parser = HTMLParser(convert_charrefs=True)
        texts = []; parser.handle_data = texts.append
        parser.feed(rendering._reasons(list(rendering._REASONS)))
        text = ''.join(texts)
        for reason in rendering._REASONS:
            self.assertIn(reason + ' ', text)

    def cli(self, *, output=None, extra=(), cwd=ROOT, args=None):
        arguments = args if args is not None else ['--store', str(self.store), '--store-id', self.sid,
                  '--batch-id', self.bid, '--output', str(output or self.output), *extra]
        return subprocess.run([sys.executable, '-B', '-m', 'observation_view', *arguments], cwd=cwd,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(cwd)},
                              capture_output=True, timeout=30)

    def test_cli_success_has_only_safe_ack(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'status': 'PASS', 'operation': 'view', 'error': None})
        self.assertEqual(result.stderr, b'')

    def test_cli_malformed_or_private_arguments_never_echo(self):
        for args in ([], ['--help'], ['--store', '/private/sensitive'], ['--verified', 'true'], ['--sto', '/private/sensitive']):
            result = self.cli(args=args)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout), {'status': 'FAIL', 'operation': 'view', 'error': 'CLI_USAGE'})
            self.assertEqual(result.stderr, b'')

    def test_unexpected_read_exception_has_fixed_safe_error(self):
        with patch.object(observation_store, 'read_batch', side_effect=RuntimeError('/sensitive/path')):
            self.refusal('INTERNAL_ERROR')
        self.assertFalse(self.output.exists())

    def test_platform_refusal_precedes_read_or_write(self):
        with patch.object(fs, 'require_platform', side_effect=StoreError('PLATFORM_UNSUPPORTED')), patch.object(observation_store, 'read_batch') as read:
            self.refusal('PLATFORM_UNSUPPORTED')
            read.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
