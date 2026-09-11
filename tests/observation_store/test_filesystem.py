"""Disposable synthetic files; real macOS exclusive rename and bounded workers."""
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from observation_store import filesystem as f
from observation_store.errors import StoreError


@unittest.skipUnless(sys.platform == 'darwin', 'macOS interface')
class Filesystem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-d2-', dir='/tmp')
        self.path = Path(self.tmp.name)
        os.chmod(self.path, 0o700)
        self.parent, self.root = f.reserve_root(self.path / 'store')
        self.stage = self.root.mkdir('staging')
        self.dest = self.root.mkdir('archives')
        f.create_lock(self.root)

    def tearDown(self):
        for d in (self.dest,self.stage,self.root,self.parent):
            d.close()
        self.tmp.cleanup()

    def test_reserve_existing_and_private(self):
        with self.assertRaisesRegex(StoreError, 'STORE_EXISTS'):
            f.reserve_root(self.path / 'store')
        os.chmod(self.path, 0o755)
        with self.assertRaises(StoreError):
            f.reserve_root(self.path / 'new')
        self.assertFalse((self.path / 'new').exists())
        os.chmod(self.path, 0o700)

    def test_racing_initializers(self):
        barrier = threading.Barrier(2)
        results = []
        def run():
            barrier.wait(timeout=5)
            try:
                a,b = f.reserve_root(self.path / 'race')
                b.close(); a.close()
                results.append('OK')
            except StoreError as e:
                results.append(e.code)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10); self.assertFalse(t.is_alive())
        self.assertEqual(sorted(results), ['OK','STORE_EXISTS'])

    def test_modes_owner_hardlink_fifo_symlink(self):
        f.stage_bytes(self.stage, 'good', b'x')
        path = self.path / 'store/staging'
        os.link(path/'good', path/'hard')
        with self.assertRaises(StoreError): f.read_bytes(self.stage, 'good')
        (path/'hard').unlink()
        os.symlink(path/'good', path/'link')
        os.mkfifo(path/'fifo', 0o600)
        for name in ('link','fifo'):
            with self.assertRaises(StoreError): f.read_bytes(self.stage,name)
        os.chmod(path/'good',0o644)
        with self.assertRaises(StoreError): f.read_bytes(self.stage,'good')
        os.chmod(path/'good',0o600)
        with patch.object(f.os, 'getuid', return_value=os.getuid()+1):
            with self.assertRaises(StoreError): self.root.check()

    def test_code_rc2_aliases(self):
        outside = self.path/'repo'
        outside.mkdir(mode=0o700)
        (outside/'.git').mkdir()
        alias = self.path/'alias'
        alias.symlink_to(outside)
        for p in (outside/'new',alias/'new'):
            with self.assertRaisesRegex(StoreError,'OUTPUT_INVALID'): f.reserve_root(p)
        (outside/'.git').rmdir()
        (outside/'current.json').write_bytes(b'{}')
        with self.assertRaises(StoreError): f.reserve_root(alias/'new')

    def test_nested_thread_process_alias_lock(self):
        alias = self.path/'alias'
        alias.symlink_to(self.path/'store')
        with f.CheckedDir.open(alias) as other:
            with f.writer_lock(self.root):
                with self.assertRaisesRegex(StoreError,'NESTED_OPERATION'):
                    with f.writer_lock(other): pass
                errors=[]
                def run():
                    try:
                        with f.writer_lock(other): pass
                    except StoreError as e: errors.append(e.code)
                t=threading.Thread(target=run); t.start(); t.join(timeout=5)
                self.assertFalse(t.is_alive()); self.assertEqual(errors,['WRITER_BUSY'])
                code = 'from observation_store.filesystem import *; import sys\ntry:\n with CheckedDir.open(sys.argv[1]) as r:\n  with writer_lock(r): print("BAD")\nexcept StoreError as e: print(e.code)'
                p=subprocess.run([sys.executable,'-c',code,str(alias)],capture_output=True,timeout=10)
                self.assertEqual((p.returncode,p.stdout.strip(),p.stderr),(0,b'WRITER_BUSY',b''))
            with f.writer_lock(other): pass

    def test_missing_lock_not_recreated(self):
        (self.path/'store/writer.lock').unlink()
        with self.assertRaises(StoreError):
            with f.writer_lock(self.root): pass
        self.assertFalse((self.path/'store/writer.lock').exists())

    def test_no_clobber_reuse_and_conflict(self):
        f.stage_bytes(self.stage,'a',b'one')
        self.assertEqual(f.publish(self.stage,'a',self.dest,'x'),f.Publication(True,False))
        self.assertEqual(os.stat(self.path/'store/archives/x').st_nlink,1)
        self.assertFalse((self.path/'store/staging/a').exists())
        f.stage_bytes(self.stage,'b',b'one')
        self.assertEqual(f.publish(self.stage,'b',self.dest,'x',allow_reuse=True),f.Publication(False,True))
        f.stage_bytes(self.stage,'c',b'two')
        with self.assertRaises(f.PublicationError) as e: f.publish(self.stage,'c',self.dest,'x',allow_reuse=True)
        self.assertFalse(e.exception.linked_may_have_occurred)
        self.assertEqual(f.read_bytes(self.dest,'x'),b'one')
        with self.assertRaises(f.PublicationError): f.publish(self.stage,'b',self.dest,'x')

    def test_short_zero_enospc_permission_writes(self):
        original = os.write
        with patch.object(f.os,'write',side_effect=lambda fd,b: original(fd,b[:2])):
            f.stage_bytes(self.stage,'short',b'abcdef')
        self.assertEqual(f.read_bytes(self.stage,'short'),b'abcdef')
        for name,effect in [('zero',lambda *a:0),('full',OSError(errno.ENOSPC,'secret')),('permission',PermissionError('secret'))]:
            with patch.object(f.os,'write',side_effect=effect):
                with self.assertRaisesRegex(StoreError,'STORAGE_ERROR'): f.stage_bytes(self.stage,name,b'abc')

    def test_flush_linkage_uncertainty(self):
        f.stage_bytes(self.stage,'pre',b'one')
        with patch.object(f.os,'fsync',side_effect=OSError(errno.EIO,'secret')):
            with self.assertRaises(f.PublicationError) as e: f.publish(self.stage,'pre',self.dest,'x')
        self.assertFalse(e.exception.linked_may_have_occurred)
        original=f._rename_exclusive
        def rename(*args):
            original(*args)
            self.dest.flush = lambda: (_ for _ in ()).throw(StoreError('STORAGE_ERROR'))
        with patch.object(f,'_rename_exclusive',side_effect=rename):
            with self.assertRaises(f.PublicationError) as e: f.publish(self.stage,'pre',self.dest,'x')
        self.assertTrue(e.exception.linked_may_have_occurred)
        self.assertEqual(f.read_bytes(self.dest,'x'),b'one')

    def test_replaced_child_and_root(self):
        base=self.path/'store'
        (base/'staging').rename(base/'old-stage')
        (base/'staging').mkdir(mode=0o700)
        with self.assertRaises(StoreError): f.stage_bytes(self.stage,'blocked',b'x')
        self.assertFalse((base/'old-stage/blocked').exists())
        base.rename(self.path/'old-store')
        base.mkdir(mode=0o700)
        with self.assertRaises(StoreError): self.root.check()

    def test_archive_capture_and_mutation(self):
        source=self.path/'synthetic.zip'; source.write_bytes(b'abc')
        self.assertEqual(f.capture_archive(source,self.stage,'archive'),f.Capture(hashlib.sha256(b'abc').hexdigest(),3))
        original=f.write_all
        def mutate(fd,blob):
            original(fd,blob)
            source.write_bytes(b'different')
        with patch.object(f,'write_all',side_effect=mutate):
            with self.assertRaisesRegex(StoreError,'INPUT_CHANGED'): f.capture_archive(source,self.stage,'mutated')
        source.unlink(); os.mkfifo(source,0o600)
        with self.assertRaisesRegex(StoreError,'INPUT_INVALID'): f.capture_archive(source,self.stage,'fifo')

    def test_platform_before_mutation(self):
        with patch.object(f.sys,'platform','linux'):
            with self.assertRaisesRegex(StoreError,'PLATFORM_UNSUPPORTED'): f.reserve_root(self.path/'unsupported')
        self.assertFalse((self.path/'unsupported').exists())

    def test_lock_inode_replacement(self):
        with f.writer_lock(self.root): pass
        lock = self.path/'store/writer.lock'
        lock.rename(self.path/'old-lock')
        lock.write_bytes(b''); lock.chmod(0o600)
        with self.assertRaisesRegex(StoreError,'INTEGRITY_ERROR'):
            with f.writer_lock(self.root): pass

    def test_source_path_replacement_and_ctime_exception(self):
        source=self.path/'synthetic.zip'; source.write_bytes(b'abc')
        original=f.write_all
        def replace(fd,blob):
            original(fd,blob)
            source.rename(self.path/'previous-source')
            source.write_bytes(b'abc')
        with patch.object(f,'write_all',side_effect=replace):
            with self.assertRaisesRegex(StoreError,'INPUT_CHANGED'): f.capture_archive(source,self.stage,'replaced')
        def metadata(fd,blob):
            original(fd,blob)
            os.utime(source, ns=(source.stat().st_atime_ns,source.stat().st_mtime_ns))
        with patch.object(f,'write_all',side_effect=metadata):
            self.assertEqual(f.capture_archive(source,self.stage,'metadata').size,3)

    def test_unsupported_rename_and_file_flush(self):
        f.stage_bytes(self.stage,'unsupported',b'x')
        with patch.object(f,'_rename_exclusive',side_effect=OSError(errno.ENOTSUP,'private')):
            with self.assertRaises(f.PublicationError) as e: f.publish(self.stage,'unsupported',self.dest,'x')
        self.assertEqual(e.exception.code,'STORAGE_ERROR')
        self.assertFalse(e.exception.linked_may_have_occurred)
        self.assertFalse((self.path/'store/archives/x').exists())
        source=self.path/'synthetic.zip'; source.write_bytes(b'x')
        with patch.object(f.os,'fsync',side_effect=OSError(errno.EIO,'private')):
            with self.assertRaisesRegex(StoreError,'STORAGE_ERROR'): f.capture_archive(source,self.stage,'flush')

    def test_reserve_final_symlink_refused_parent_alias_allowed(self):
        target = self.path/'new-target'
        alias = self.path/'existing-alias'
        alias.symlink_to(target)
        with self.assertRaisesRegex(StoreError, 'STORE_EXISTS'):
            f.reserve_root(alias)
        self.assertTrue(alias.is_symlink())
        self.assertFalse(target.exists())
        parent_alias = self.path/'parent-alias'
        parent_alias.symlink_to(self.path)
        parent, root = f.reserve_root(parent_alias/'through-parent')
        try:
            info = (self.path/'through-parent').stat()
            self.assertEqual(root.identity, (info.st_dev, info.st_ino))
        finally:
            root.close(); parent.close()

    def test_capture_source_disappearance_and_read_error(self):
        source = self.path/'synthetic.zip'
        source.write_bytes(b'abc')
        original = f.write_all
        def remove(fd, blob):
            original(fd, blob)
            source.unlink()
        with patch.object(f, 'write_all', side_effect=remove):
            with self.assertRaisesRegex(StoreError, 'INPUT_CHANGED'):
                f.capture_archive(source, self.stage, 'removed')
        source.write_bytes(b'abc')
        with patch.object(f.os, 'read', side_effect=OSError(errno.EIO, 'private source')):
            with self.assertRaisesRegex(StoreError, 'INPUT_CHANGED'):
                f.capture_archive(source, self.stage, 'read-error')
        with patch.object(f.os, 'write', side_effect=OSError(errno.ENOSPC, 'private destination')):
            with self.assertRaisesRegex(StoreError, 'STORAGE_ERROR'):
                f.capture_archive(source, self.stage, 'write-error')
