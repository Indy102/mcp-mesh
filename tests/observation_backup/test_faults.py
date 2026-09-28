"""Fault, interruption, graph closure and semantic-corruption tests."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid
from unittest.mock import patch
import test_backup as support
from observation_backup import create_backup,verify_backup,recover_backup,check_recovery,BackupError
from observation_backup import backup,contracts as c,io
import observation_store as store
from observation_store import filesystem as fs

class FaultTests(support.Fixture):
    # Reuse setup helpers without inheriting the earlier test cases.
    def test_child_kills_around_both_publications(self):
        worker=Path(__file__).with_name('worker.py')
        for checkpoint in ('before-backup-seal','after-backup-seal'):
            self.dest=self.home/checkpoint
            p=subprocess.run([sys.executable,'-m','tests.observation_backup.worker','backup',checkpoint,str(self.source),self.sid,str(self.dest),self.bid],capture_output=True)
            self.assertEqual(p.returncode,-signal.SIGKILL)
            if checkpoint.startswith('before'): self.assertCode('NOT_SEALED',verify_backup,self.dest,self.bid)
            else: verify_backup(self.dest,self.bid)
        for checkpoint in ('before-recovery-result','after-recovery-result'):
            self.recovery=self.home/checkpoint; self.nid=str(uuid.uuid4())
            p=subprocess.run([sys.executable,'-m','tests.observation_backup.worker','recover',checkpoint,str(self.dest),self.bid,str(self.recovery),self.nid],capture_output=True)
            self.assertEqual(p.returncode,-signal.SIGKILL)
            if checkpoint.startswith('before'): self.assertCode('RECOVERY_INCOMPLETE',check_recovery,self.dest,self.bid,self.recovery,self.nid)
            else: check_recovery(self.dest,self.bid,self.recovery,self.nid)
    def test_short_writes_and_fsync_failure(self):
        real=os.write
        with patch.object(os,'write',lambda fd,blob:real(fd,blob[:max(1,len(blob)//3)])): self.capture()
        verify_backup(self.dest,self.bid); self.dest=self.home/'failed'
        with patch.object(os,'fsync',side_effect=OSError('disk-full')):
            with self.assertRaises(BackupError): self.capture()
        self.assertTrue(self.dest.exists()); self.assertFalse((self.dest/'manifest.json').exists())
    def test_zero_write_no_false_seal(self):
        with patch.object(os,'write',return_value=0): self.assertCode('STORAGE_ERROR',self.capture)
        self.assertTrue(self.dest.exists()); self.assertFalse((self.dest/'manifest.json').exists())
    def test_source_and_destination_replacement(self):
        old=self.home/'original'
        def mutate(name):
            if name=='before-backup-seal':
                self.source.rename(old); self.source.mkdir(mode=0o700)
        with patch.object(backup,'_checkpoint',mutate): self.assertCode('INTEGRITY_ERROR',self.capture)
        self.assertFalse((self.dest/'manifest.json').exists())
        self.source.rmdir(); old.rename(self.source); self.dest=self.home/'second'
        def replace(name):
            if name=='before-backup-seal': self.dest.rename(self.home/'moved'); self.dest.mkdir(mode=0o700)
        with patch.object(backup,'_checkpoint',replace): self.assertCode('INTEGRITY_ERROR',self.capture)
        self.assertFalse((self.dest/'manifest.json').exists()); self.assertFalse((self.home/'moved'/'manifest.json').exists())
    def test_process_writer_contention(self):
        code="from observation_store import filesystem as f; import sys; r=f.CheckedDir.open(sys.argv[1]); c=f.writer_lock(r); c.__enter__(); print('ready',flush=True); sys.stdin.read()"
        p=subprocess.Popen([sys.executable,'-c',code,str(self.source)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertEqual(p.stdout.readline(),b'ready\n'); self.assertCode('WRITER_BUSY',self.capture)
        finally:
            p.communicate(b'',timeout=10)
        self.assertEqual(p.returncode,0); self.assertFalse(self.dest.exists())
    def test_missing_extra_objects_and_unsafe_graph(self):
        self.capture(); path=self.dest/'manifest.json'; raw=path.read_bytes(); m=json.loads(raw)
        extra=self.dest/'objects'/('f'*64+'.bin'); extra.write_bytes(b''); extra.chmod(0o600)
        self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid); extra.unlink()
        obj=next((self.dest/'objects').iterdir()); held=obj.read_bytes(); obj.unlink()
        self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid); obj.write_bytes(held); obj.chmod(0o600)
        for mutate in (lambda x:x['files'][0].update(size=True),lambda x:x['files'][0].update(path='../escape'),lambda x:x['directories'].append('staging/deep/deeper'),lambda x:x.update(extra='private'),lambda x:x.update(schema_version='bad'),lambda x:x['files'].append(x['files'][0])):
            changed=json.loads(raw); mutate(changed); path.write_bytes(c.canonical(changed)); self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid)
        path.write_bytes(raw); verify_backup(self.dest,self.bid)
    def test_hardlink_symlink_fifo_modes_and_nesting(self):
        for kind in ('hardlink','symlink','fifo'):
            path=self.source/'staging'/'unsafe'
            if kind=='hardlink': os.link(self.source/'manifest.json',path)
            elif kind=='symlink': path.symlink_to(self.source/'manifest.json')
            else: os.mkfifo(path,0o600)
            self.assertCode('INTEGRITY_ERROR',self.capture); path.unlink(); self.assertFalse(self.dest.exists())
        for ancestor in (self.source,self.home/'incomplete'):
            if not ancestor.exists(): ancestor.mkdir(mode=0o700); (ancestor/'writer.lock').touch(mode=0o600)
            self.assertCode('LOCATION_INVALID',create_backup,self.source,self.sid,ancestor/'nested',self.bid)
        alias=self.home/'alias'; alias.symlink_to(self.source,target_is_directory=True)
        self.assertCode('LOCATION_INVALID',create_backup,alias,self.sid,self.dest,self.bid)
    def test_all_evidence_classes_and_listing_semantics(self):
        self.imported(); self.capture(); pristine=self.home/'pristine'; shutil.copytree(self.dest,pristine)
        original=json.loads((self.dest/'manifest.json').read_bytes())
        classes=('manifest.json','writer.lock','archives/','batches/','attempts/')
        selected=[x['path'] for x in original['files'] if any(x['path']==key or x['path'].startswith(key) for key in classes)]
        for path in selected:
            shutil.rmtree(self.dest); shutil.copytree(pristine,self.dest)
            m=json.loads((self.dest/'manifest.json').read_bytes()); entry=next(x for x in m['files'] if x['path']==path); old=entry['sha256']; blob=b'{}' if path.endswith('.json') else b'corrupt'; new=hashlib.sha256(blob).hexdigest()
            target=self.dest/'objects'/(new+'.bin'); target.write_bytes(blob); target.chmod(0o600); entry.update(sha256=new,size=len(blob))
            if not any(x['sha256']==old for x in m['files']): (self.dest/'objects'/(old+'.bin')).unlink()
            (self.dest/'manifest.json').write_bytes(c.canonical(m)); self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid)
        shutil.rmtree(self.dest); shutil.copytree(pristine,self.dest)
        m=json.loads((self.dest/'manifest.json').read_bytes()); m['listing']['batches']=[]; (self.dest/'manifest.json').write_bytes(c.canonical(m)); self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid)
    def test_pending_failed_interrupted_and_replay_failure(self):
        # A real preparation refusal leaves a FAILED result without an intent.
        original_catalogue=self.inputs.catalogue.read_bytes()
        self.inputs.catalogue.write_bytes(b'{}')
        no_intent=self.import_failure()
        self.assertFalse((self.source/'attempts'/(no_intent+'.intent.json')).exists())
        self.inputs.catalogue.write_bytes(original_catalogue)
        def fail(name):
            if name=='after-intent': raise store.StoreError('STORAGE_ERROR')
        with patch.object(store.store,'_checkpoint',fail):
            failed=self.import_failure()
        # Turn the valid failed-with-intent history into pending and interrupted chains.
        pending=str(uuid.uuid4())
        with patch.object(store.store,'_checkpoint',fail): self.import_failure(pending)
        (self.source/'attempts'/(pending+'.result.json')).unlink()
        interrupted=str(uuid.uuid4())
        with patch.object(store.store,'_checkpoint',fail): self.import_failure(interrupted)
        (self.source/'attempts'/(interrupted+'.result.json')).unlink()
        store.recover_attempt(self.source,self.sid,self.definition,interrupted)
        self.imported(); before={p:p.read_bytes() for p in (self.source/'attempts').iterdir()}; self.capture(); verify_backup(self.dest,self.bid)
        self.assertEqual(before,{p:p.read_bytes() for p in (self.source/'attempts').iterdir()})
        # Real preparation fails because an immutable input has changed after capture validation.
        original_import=store.import_observations
        def corrupt_materialized_input(store_path,store_id,inputs,definition,attempt_id):
            inputs.catalogue.write_bytes(b'{}')
            return original_import(store_path,store_id,inputs,definition,attempt_id)
        with patch.object(store,'import_observations',corrupt_materialized_input):
            self.assertCode('REPLAY_FAILED',recover_backup,self.dest,self.bid,self.recovery,self.nid)
        self.assertCode('RECOVERY_INCOMPLETE',check_recovery,self.dest,self.bid,self.recovery,self.nid)
        self.assertTrue((self.recovery/'plan.json').exists())
    def import_failure(self,aid=None):
        aid=aid or str(uuid.uuid4()); result=json.loads(store.import_observations(self.source,self.sid,self.inputs,self.definition,aid)); self.assertEqual(result['outcome'],'FAILED'); return aid
    def test_plan_result_and_extra_attempt_tampering(self):
        self.imported(); self.capture(); recover_backup(self.dest,self.bid,self.recovery,self.nid)
        for name in ('plan.json','result.json'):
            path=self.recovery/name; raw=path.read_bytes(); m=json.loads(raw); m['new_store_id']=str(uuid.uuid4()); path.write_bytes(c.canonical(m))
            self.assertCode('INTEGRITY_ERROR',check_recovery,self.dest,self.bid,self.recovery,self.nid); path.write_bytes(raw)
        store.import_observations(self.recovery/'store',self.nid,self.inputs,self.definition,str(uuid.uuid4()))
        self.assertCode('INTEGRITY_ERROR',check_recovery,self.dest,self.bid,self.recovery,self.nid)

    def test_multiple_distinct_batches_exact(self):
        self.imported(); folder=self.home/'other-input'; folder.mkdir(mode=0o700)
        inputs,definition=support.fixture(folder,repeated=True)
        self.assertEqual(definition,self.definition)
        result=json.loads(store.import_observations(self.source,self.sid,inputs,self.definition,str(uuid.uuid4()))); self.assertEqual(result['outcome'],'COMMITTED')
        listing=json.loads(store.list_batches(self.source,self.sid))['batches']; self.assertEqual(len(listing),2)
        expected={b['batch_id']:store.read_batch(self.source,self.sid,b['batch_id']) for b in listing}
        self.capture(); recover_backup(self.dest,self.bid,self.recovery,self.nid); check_recovery(self.dest,self.bid,self.recovery,self.nid)
        for bid,raw in expected.items(): self.assertEqual(store.read_batch(self.recovery/'store',self.nid,bid),raw)
    def test_source_only_distribution_refused_without_git_marker(self):
        from tests.observation_store.helpers import source_copy,ROOT
        folder=self.home/'distribution'; folder.mkdir(mode=0o700)
        source,alias=source_copy(folder)
        shutil.copytree(ROOT/'observation_backup',source/'observation_backup',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        self.assertFalse((source/'.git').exists())
        for parent in (source/'private',alias/'private'):
            output=parent/'backup'
            p=subprocess.run([sys.executable,'-m','observation_backup','backup','--store',str(self.source),'--store-id',self.sid,'--backup',str(output),'--backup-id',self.bid],cwd=source,capture_output=True)
            self.assertEqual(p.returncode,2); self.assertEqual(p.stderr,b''); self.assertEqual(json.loads(p.stdout)['error'],'LOCATION_INVALID'); self.assertFalse(output.exists())
    def test_check_lock_and_flush_corruption_rechecked(self):
        self.capture(); recover_backup(self.dest,self.bid,self.recovery,self.nid)
        with fs.CheckedDir.open(self.recovery/'store') as root,fs.writer_lock(root):
            self.assertCode('NESTED_OPERATION',check_recovery,self.dest,self.bid,self.recovery,self.nid)
        original=fs.CheckedDir.flush; changed=False
        flushed=[]
        def full_path(directory):
            if directory.path is not None: return directory.path
            return full_path(directory.parent)/directory.name
        def record_flush(directory):
            original(directory); flushed.append(full_path(directory))
        with patch.object(fs.CheckedDir,'flush',record_flush):
            check_recovery(self.dest,self.bid,self.recovery,self.nid)
        root_last=max(i for i,path in enumerate(flushed) if path==self.recovery)
        self.assertTrue(all(i<root_last for i,path in enumerate(flushed) if self.recovery in path.parents))
        def mutate(directory):
            nonlocal changed
            original(directory)
            if directory.path==self.dest and not changed:
                changed=True; object_path=next((self.dest/'objects').iterdir()); object_path.write_bytes(object_path.read_bytes()+b'changed')
        with patch.object(fs.CheckedDir,'flush',mutate): self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid)
    def test_recovered_store_d3_exact_report_and_rows(self):
        from observation_view import create_view
        from tests.observation_view.test_view import Evidence
        self.imported(); self.capture(); recover_backup(self.dest,self.bid,self.recovery,self.nid)
        batch_id=json.loads(store.list_batches(self.recovery/'store',self.nid))['batches'][0]['batch_id']
        batch=json.loads(store.read_batch(self.recovery/'store',self.nid,batch_id)); report=batch['report']['report']
        output=self.home/'recovered.html'
        self.assertEqual(json.loads(create_view(self.recovery/'store',self.nid,batch_id,output))['status'],'PASS')
        parsed=Evidence(output.read_text()); self.assertEqual(parsed.report_json,report)
        self.assertEqual(parsed.primary_rows,[{'date':day['date'],'value':c.canonical(day['value']).decode('ascii'),'state':day['calculation_state'],'reasons':set(day['reasons'])} for day in report['days']])
    def test_validated_store_inventory_pinned_through_flush(self):
        from observation_backup import recovery
        self.imported(); self.capture(); recover_backup(self.dest,self.bid,self.recovery,self.nid)
        original=recovery.validate_store
        batch=next((self.recovery/'store'/'batches').iterdir())
        for path in (batch,self.recovery/'result.json'):
            raw=path.read_bytes()
            def mutate_after_validation(*args):
                validated=original(*args)
                path.write_bytes(raw+b'\n')
                return validated
            with patch.object(recovery,'validate_store',mutate_after_validation):
                self.assertCode('INTEGRITY_ERROR',check_recovery,self.dest,self.bid,self.recovery,self.nid)
            path.write_bytes(raw)
