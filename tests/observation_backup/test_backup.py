"""Public D4 integration with invented real D1/D2 evidence only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from tests.observation_store.helpers import fixture, tree
import observation_store as store
from observation_store import filesystem as fs
from observation_backup import create_backup,verify_backup,recover_backup,check_recovery,BackupError
from observation_backup import backup, contracts as c

class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve(); self.home.chmod(0o700)
        self.inputs,self.definition=fixture(self.home)
        self.source=self.home/'source'; self.dest=self.home/'backup'; self.recovery=self.home/'recovery'
        self.sid=str(uuid.uuid4()); self.bid=str(uuid.uuid4()); self.nid=str(uuid.uuid4())
        store.initialize(self.source,self.sid,self.definition)
    def imported(self):
        aid=str(uuid.uuid4()); result=json.loads(store.import_observations(self.source,self.sid,self.inputs,self.definition,aid)); self.assertEqual(result['outcome'],'COMMITTED'); return aid
    def capture(self): return create_backup(self.source,self.sid,self.dest,self.bid)
    def assertCode(self,code,fn,*args):
        with self.assertRaises(BackupError) as cm: fn(*args)
        self.assertEqual(cm.exception.code,code)
        self.assertEqual(set(json.loads(cm.exception.failure_bytes)),{'status','operation','error'})

class BackupTests(Fixture):
    def test_empty_end_to_end(self):
        before=tree(self.source); self.assertEqual(json.loads(self.capture())['status'],'PASS'); self.assertEqual(tree(self.source),before)
        verify_backup(self.dest,self.bid); recover_backup(self.dest,self.bid,self.recovery,self.nid); check_recovery(self.dest,self.bid,self.recovery,self.nid)
        self.assertEqual(json.loads(store.list_batches(self.recovery/'store',self.nid))['batches'],[])
    def test_repeated_batches_source_unavailable_exact_fresh_replay(self):
        old={self.imported(),self.imported()}; before=tree(self.source); self.capture(); self.assertEqual(tree(self.source),before)
        expected=json.loads(store.list_batches(self.source,self.sid))['batches']; raw=store.read_batch(self.source,self.sid,expected[0]['batch_id'])
        shutil.rmtree(self.source)
        for p in (self.inputs.archive,self.inputs.catalogue,self.inputs.binding): p.unlink()
        recover_backup(self.dest,self.bid,self.recovery,self.nid); check_recovery(self.dest,self.bid,self.recovery,self.nid)
        self.assertEqual(store.read_batch(self.recovery/'store',self.nid,expected[0]['batch_id']),raw)
        listed=json.loads(store.list_batches(self.recovery/'store',self.nid))['batches']; self.assertEqual(len(listed),1)
        self.assertEqual(len(listed[0]['committed_attempt_ids']),1); self.assertTrue(old.isdisjoint(listed[0]['committed_attempt_ids']))
    def test_opaque_orphans_staging_preserved(self):
        (self.source/'archives'/('a'*64+'.zip')).write_bytes(b'opaque not zip'); (self.source/'archives'/('a'*64+'.zip')).chmod(0o600)
        (self.source/'staging'/'partial').mkdir(mode=0o700)
        (self.source/'staging'/'partial'/'chunk').write_bytes(b'not json'); (self.source/'staging'/'partial'/'chunk').chmod(0o600)
        before=tree(self.source); self.capture(); verify_backup(self.dest,self.bid); self.assertEqual(tree(self.source),before)
        recover_backup(self.dest,self.bid,self.recovery,self.nid)
        self.assertEqual(json.loads(store.list_batches(self.recovery/'store',self.nid))['batches'],[])
    def test_not_sealed_no_clobber_and_ids(self):
        self.assertCode('INPUT_INVALID',create_backup,self.source,'bad',self.dest,self.bid)
        self.capture(); old=tree(self.dest)
        self.assertCode('DESTINATION_EXISTS',create_backup,self.source,self.sid,self.dest,self.bid); self.assertEqual(tree(self.dest),old)
        self.assertCode('STORE_ID_MISMATCH',verify_backup,self.dest,str(uuid.uuid4()))
        self.assertCode('INPUT_INVALID',recover_backup,self.dest,self.bid,self.recovery,self.sid)
    def test_before_after_seal_failure(self):
        def fail(name):
            if name=='before-backup-seal': raise BackupError('STORAGE_ERROR')
        with patch.object(backup,'_checkpoint',fail): self.assertCode('STORAGE_ERROR',self.capture)
        self.assertCode('NOT_SEALED',verify_backup,self.dest,self.bid)
        self.dest=self.home/'backup2'
        def after(name):
            if name=='after-backup-seal': raise BackupError('STORAGE_ERROR')
        with patch.object(backup,'_checkpoint',after): self.assertCode('PUBLICATION_UNKNOWN',self.capture)
        verify_backup(self.dest,self.bid)
    def test_before_after_result_failure(self):
        self.capture()
        def fail(name):
            if name=='before-recovery-result': raise BackupError('STORAGE_ERROR')
        with patch.object(backup,'_checkpoint',fail): self.assertCode('STORAGE_ERROR',recover_backup,self.dest,self.bid,self.recovery,self.nid)
        self.assertCode('RECOVERY_INCOMPLETE',check_recovery,self.dest,self.bid,self.recovery,self.nid)
        self.recovery=self.home/'recovery2'; self.nid=str(uuid.uuid4())
        def after(name):
            if name=='after-recovery-result': raise BackupError('STORAGE_ERROR')
        with patch.object(backup,'_checkpoint',after): self.assertCode('PUBLICATION_UNKNOWN',recover_backup,self.dest,self.bid,self.recovery,self.nid)
        check_recovery(self.dest,self.bid,self.recovery,self.nid)
    def test_nested_and_thread_contention(self):
        import concurrent.futures
        with fs.CheckedDir.open(self.source) as root,fs.writer_lock(root):
            self.assertCode('NESTED_OPERATION',self.capture)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future=pool.submit(self.capture)
                with self.assertRaises(BackupError) as cm: future.result()
                self.assertEqual(cm.exception.code,'WRITER_BUSY')
        self.assertFalse(self.dest.exists())
    def test_tamper_objects_manifest_and_types(self):
        self.capture(); manifest=(self.dest/'manifest.json').read_bytes()
        obj=next((self.dest/'objects').iterdir()); raw=obj.read_bytes(); obj.write_bytes(raw+b'x')
        self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid); obj.write_bytes(raw)
        (self.dest/'manifest.json').write_bytes(manifest+b'\n'); self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid)
        (self.dest/'manifest.json').write_bytes(manifest); obj.chmod(0o644); self.assertCode('INTEGRITY_ERROR',verify_backup,self.dest,self.bid)
    def test_cli_safe_usage_and_success(self):
        self.capture()
        for args,code in ((['verify','--backup',str(self.dest),'--backup-id',self.bid],0),(['PRIVATE-SECRET'],2),(['verify','--help','PRIVATE-SECRET'],2)):
            p=subprocess.run([sys.executable,'-m','observation_backup',*args],capture_output=True)
            self.assertEqual(p.returncode,code); self.assertEqual(p.stderr,b''); self.assertEqual(set(json.loads(p.stdout)),{'status','operation','error'}); self.assertNotIn(b'PRIVATE',p.stdout)

if __name__=='__main__': unittest.main()
