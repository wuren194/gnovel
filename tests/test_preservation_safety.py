"""Real lock holders, external saves, old descriptors, and crash checkpoints."""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core import write_sets
from scripts.core.candidate_versions import save_candidate_version, list_candidate_versions, read_candidate_version
from scripts.core.consistency import require_no_pending
from scripts.core.finalization import pending_transactions, recover_transactions
from scripts.core.jsonio import read_json
from scripts.core.locks import current_thread_owns_lock, project_lock, project_lock_reentrant, project_lock_observation
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project

REPO = Path(__file__).resolve().parents[1]


class PreservationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve(); self.root = self.base/'book'
        self.fixture = create_schema5_project(self.root, chapter_count=1)
        self.chapter = self.fixture['chapterIds'][0]
        self.relative = '正文/guarded.md'; self.path = self.root/self.relative
        self.path.write_bytes(b'old')

    def run_child(self, code, *arguments):
        return subprocess.run([sys.executable, '-c', code, str(self.root), *arguments],
                              cwd=REPO, capture_output=True, text=True, timeout=30)

    def test_thread_owner_reentrancy_and_pending_reads(self):
        pending = self.root/'.gnovel/transactions/TX-THREAD.json'; outcomes = []
        with project_lock(self.root):
            with project_lock_reentrant(self.root):
                self.assertTrue(current_thread_owns_lock(self.root))
            with self.assertRaises(StructureError) as caught:
                with project_lock(self.root): pass
            self.assertEqual(caught.exception.code, 'PROJECT_LOCKED')
            pending.write_text('{}'); require_no_pending(self.root)
            with self.assertRaises(StructureError) as pending_write:
                with project_lock_reentrant(self.root): pass
            self.assertEqual(pending_write.exception.code, 'TRANSACTION_INCOMPLETE')
            def competitor():
                for action in (lambda: project_lock_reentrant(self.root).__enter__(),
                               lambda: require_no_pending(self.root),
                               lambda: list_candidate_versions(self.root, self.chapter)):
                    try: action()
                    except StructureError as exc: outcomes.append(exc.code)
            worker = threading.Thread(target=competitor); worker.start(); worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, ['PROJECT_LOCKED', 'TRANSACTION_INCOMPLETE', 'TRANSACTION_INCOMPLETE'])
            pending.unlink()
        self.assertFalse(project_lock_observation(self.root)['locked'])

    def test_replaced_anchor_stops_holder_and_other_thread_cannot_release(self):
        from scripts.core import locks
        lock = self.root/'.gnovel/structure.lock'
        with project_lock(self.root):
            owner = next(owner for owner in locks._owners.values() if owner.path == lock); result = []
            def release():
                try: locks._release(owner)
                except StructureError as exc: result.append(exc.code)
            worker = threading.Thread(target=release); worker.start(); worker.join(5)
            self.assertEqual(result, ['PROJECT_LOCK_NOT_OWNED'])
            self.assertTrue(project_lock_observation(self.root)['locked'])
        with self.assertRaises(StructureError) as caught:
            with project_lock(self.root):
                lock.rename(self.base/'held-anchor'); lock.write_text('new object')
                write_sets.apply_write_set(self.root, {self.relative: b'new'}, operation='test')
        self.assertEqual(caught.exception.code, 'PROJECT_LOCK_REPLACED')
        self.assertEqual(self.path.read_bytes(), b'old')

    def test_pid_text_does_not_grant_lock_or_trigger_unlink(self):
        lock = self.root/'.gnovel/structure.lock'; lock.write_text('999999999'); inode = lock.stat().st_ino
        with self.assertRaises(StructureError) as caught:
            with project_lock_reentrant(self.root): pass
        self.assertEqual(caught.exception.code, 'PROJECT_LOCK_PROTOCOL_UNSUPPORTED')
        self.assertEqual(lock.read_text(), '999999999'); self.assertEqual(lock.stat().st_ino, inode)

    def test_real_dead_holder_and_two_contenders_have_one_winner(self):
        holder = subprocess.Popen([sys.executable, '-c', '''
from pathlib import Path
import sys,time
from scripts.core.locks import project_lock
with project_lock(Path(sys.argv[1])):
 print('held',flush=True)
 time.sleep(30)
''', str(self.root)], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        inode = (self.root/'.gnovel/structure.lock').stat().st_ino
        holder.kill(); holder.communicate(timeout=10)
        code = '''
from pathlib import Path
import sys
from scripts.core.locks import project_lock
from scripts.core.structure_store import StructureError
try:
 with project_lock(Path(sys.argv[1])):
  print('won',flush=True)
  sys.stdin.readline()
except StructureError as exc: print(exc.code,flush=True)
'''
        contenders = [subprocess.Popen([sys.executable, '-c', code, str(self.root)], cwd=REPO,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for proc in contenders: self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
        results = [proc.stdout.readline().strip() for proc in contenders]
        self.assertEqual(sorted(results), ['PROJECT_LOCKED', 'won'])
        for proc in contenders: proc.communicate('\n', timeout=10)
        self.assertEqual((self.root/'.gnovel/structure.lock').stat().st_ino, inode)
        self.assertFalse(project_lock_observation(self.root)['locked'])

    def test_fork_cannot_inherit_reentry_or_unlock_parent(self):
        with project_lock(self.root):
            read_fd, write_fd = os.pipe(); pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    try:
                        with project_lock_reentrant(self.root): result = b'WRONGLY_ACQUIRED'
                    except StructureError as exc: result = exc.code.encode()
                    os.write(write_fd, result)
                finally: os._exit(0)
            os.close(write_fd); result = os.read(read_fd, 100); os.close(read_fd); os.waitpid(pid, 0)
            self.assertEqual(result, b'PROJECT_LOCKED'); self.assertTrue(current_thread_owns_lock(self.root))

    def test_external_process_late_save_is_preserved_not_claimed(self):
        def hook(stage, relative=None):
            if stage == 'publish-before-preserve' and relative == self.relative:
                proc = self.run_child('''
from pathlib import Path
import os,sys
path=Path(sys.argv[1])/sys.argv[2]; foreign=path.with_name('external.md')
foreign.write_bytes(b'author-late-save'); os.replace(foreign,path)
''', self.relative)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        with project_lock(self.root), patch.object(write_sets, 'checkpoint', side_effect=hook):
            with self.assertRaises((OSError, StructureError)):
                write_sets.apply_write_set(self.root, {self.relative: b'approved'}, operation='late_external')
        self.assertEqual(self.path.read_bytes(), b'author-late-save'); self.assertTrue(pending_transactions(self.root))
        self.assertEqual(next((self.root/'.gnovel/transactions').glob('TX-*/before/正文/guarded.md')).read_bytes(), b'old')
        with self.assertRaises(StructureError): recover_transactions(self.root, apply=True)
        self.assertEqual(self.path.read_bytes(), b'author-late-save')

    def test_equal_bytes_replaced_inode_is_not_accepted(self):
        def hook(stage, relative=None):
            if stage == 'publish-before-preserve' and relative == self.relative:
                replacement = self.base/'equal.md'; replacement.write_bytes(b'old'); os.replace(replacement, self.path)
        with project_lock(self.root), patch.object(write_sets, 'checkpoint', side_effect=hook):
            with self.assertRaises((OSError, StructureError)):
                write_sets.apply_write_set(self.root, {self.relative: b'approved'}, operation='same_bytes')
        self.assertEqual(self.path.read_bytes(), b'old'); self.assertTrue(pending_transactions(self.root))

    def test_frozen_expected_object_is_not_rebound_between_validation_and_image_capture(self):
        from scripts.core.file_preservation import observation
        outputs = {self.relative: b'new'}
        expected = write_sets.inventory(self.root, outputs)
        expected[0]['expectedObject'] = observation(self.path)[0]
        validate = write_sets.validate_inventory
        def checked_then_saved(root, rows, **kwargs):
            validate(root, rows, **kwargs)
            if rows is expected:
                replacement = self.base/'same-byte-new-object'
                replacement.write_bytes(b'old'); os.replace(replacement, self.path)
        with project_lock(self.root), patch.object(write_sets, 'validate_inventory', side_effect=checked_then_saved):
            with self.assertRaises(StructureError) as caught:
                write_sets.apply_write_set(self.root, outputs, expected=expected, operation='frozen_object')
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertEqual(self.path.read_bytes(), b'old')
        self.assertNotEqual(self.path.stat().st_ino, expected[0]['expectedObject']['ino'])
        self.assertFalse(pending_transactions(self.root))

    def test_old_fd_survives_cleanup_gc_and_immutable_images_are_independent(self):
        fd = os.open(self.path, os.O_RDWR)
        try:
            old_ino = os.fstat(fd).st_ino
            with project_lock(self.root):
                write_sets.apply_write_set(self.root, {self.relative: b'approved'}, operation='held_fd')
            tx = next(p for p in (self.root/'.gnovel/transactions').glob('TX-*')
                if (p/'completed.json').is_file() and read_json(p/'completed.json')['operation'] == 'held_fd')
            displaced = tx/'displaced/forward/0'; self.assertEqual(displaced.stat().st_ino, old_ino)
            self.assertNotEqual((tx/'after'/self.relative).stat().st_ino, self.path.stat().st_ino)
            os.ftruncate(fd, 0); os.write(fd, b'author-through-old-fd'); os.fsync(fd)
            from scripts.core.structure_commands import gc_project
            gc_project(self.root, keep_backups=0)
            self.assertEqual(displaced.read_bytes(), b'author-through-old-fd')
            self.assertEqual((tx/'before'/self.relative).read_bytes(), b'old')
            self.assertEqual((tx/'after'/self.relative).read_bytes(), b'approved')
            self.assertEqual(self.path.read_bytes(), b'approved'); self.assertFalse(pending_transactions(self.root))
        finally: os.close(fd)

    def test_empty_slot_occupation_retains_both_objects(self):
        def hook(stage, relative=None):
            if stage == 'publish-before-install' and relative == self.relative: self.path.write_bytes(b'new occupant')
        with project_lock(self.root), patch.object(write_sets, 'checkpoint', side_effect=hook):
            with self.assertRaises(OSError):
                write_sets.apply_write_set(self.root, {self.relative: b'approved'}, operation='occupied')
        self.assertEqual(self.path.read_bytes(), b'new occupant'); tx = pending_transactions(self.root)[0]
        row = next(r for r in tx['files'] if r['path'] == self.relative)
        directory = self.root/'.gnovel/transactions'/tx['transactionId']
        self.assertEqual((directory/row['preservedSlot']).read_bytes(), b'old')
        self.assertEqual((directory/row['publishSlot']).read_bytes(), b'approved')

    def test_create_rollback_late_window_never_unlinks_foreign_file(self):
        self.path.unlink()
        def hook(stage, relative=None):
            if stage == 'write' and relative == self.relative: raise RuntimeError('rollback requested')
            if stage == 'rollback-before-preserve' and relative == self.relative:
                replacement = self.base/'foreign.md'; replacement.write_bytes(b'author-before-delete'); os.replace(replacement, self.path)
        with project_lock(self.root), patch.object(write_sets, 'checkpoint', side_effect=hook):
            with self.assertRaises(OSError):
                write_sets.apply_write_set(self.root, {self.relative: b'approved'}, operation='create_rollback')
        self.assertEqual(self.path.read_bytes(), b'author-before-delete'); self.assertTrue(pending_transactions(self.root))

    def test_crash_native_and_transaction_boundaries(self):
        stages = ('images-synced', 'before-prepared', 'prepared', 'publish-before-preserve', 'publish-preserved', 'publish-before-install',
                  'publish-installed', 'write', 'before-committed', 'committed', 'before-archive', 'archived',
                  'native-renamed', 'native-source-synced', 'native-target-synced')
        code = '''
from pathlib import Path
import os,sys,signal
from scripts.core import write_sets,file_preservation
from scripts.core.locks import project_lock
root=Path(sys.argv[1]);stage=sys.argv[2]
def hook(at,path=None):
 if at==stage: os.kill(os.getpid(),signal.SIGKILL)
write_sets.checkpoint=hook;file_preservation.checkpoint=hook
with project_lock(root): write_sets.apply_write_set(root,{'正文/guarded.md':b'new'},operation='kill_boundary')
'''
        for stage in stages:
            with self.subTest(stage=stage):
                self.path.write_bytes(b'old'); proc = self.run_child(code, stage)
                self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr)
                expected = b'new' if stage in {'committed', 'before-archive', 'archived'} else b'old'
                recover_transactions(self.root, apply=True)
                self.assertEqual(self.path.read_bytes(), expected); self.assertFalse(pending_transactions(self.root))

    def test_native_move_and_fsync_boundaries_for_publish_rollback_and_archive(self):
        code = '''
from pathlib import Path
import os,sys,signal
from scripts.core import write_sets,file_preservation
from scripts.core.locks import project_lock
root=Path(sys.argv[1]);role=sys.argv[2];stage=sys.argv[3];rolling=False

def business(at,path=None):
 global rolling
 if role.startswith('rollback') and at=='write':
  rolling=True
  raise RuntimeError('enter rollback')

def native(at,path):
 match=(role=='preserve' and '/displaced/forward/' in path or
        role=='install' and path.endswith('/正文/guarded.md') and not rolling or
        role=='archive' and path.endswith('/completed.json') or
        role=='rollback-preserve' and '/displaced/rollback/' in path or
        role=='rollback-install' and path.endswith('/正文/guarded.md') and rolling)
 if at==stage and match: os.kill(os.getpid(),signal.SIGKILL)
write_sets.checkpoint=business;file_preservation.checkpoint=native
with project_lock(root): write_sets.apply_write_set(root,{'正文/guarded.md':b'new'},operation='native_matrix')
'''
        for role in ('preserve', 'install', 'archive', 'rollback-preserve', 'rollback-install'):
            for stage in ('native-before-rename', 'native-renamed', 'native-source-synced', 'native-target-synced'):
                with self.subTest(role=role, stage=stage):
                    self.path.write_bytes(b'old'); proc = self.run_child(code, role, stage)
                    self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr)
                    recover_transactions(self.root, apply=True)
                    self.assertEqual(self.path.read_bytes(), b'new' if role == 'archive' else b'old')
                    self.assertFalse(pending_transactions(self.root))

    def test_crash_during_each_rollback_boundary(self):
        code = '''
from pathlib import Path
import os,sys,signal
from scripts.core import write_sets
from scripts.core.locks import project_lock
root=Path(sys.argv[1]);stage=sys.argv[2]
def hook(at,path=None):
 if at=='write': raise RuntimeError('start rollback')
 if at==stage: os.kill(os.getpid(),signal.SIGKILL)
write_sets.checkpoint=hook
with project_lock(root): write_sets.apply_write_set(root,{'正文/guarded.md':b'new'},operation='kill_rollback')
'''
        for stage in ('rollback-prepared', 'rollback-before-preserve', 'rollback-preserved', 'rollback-before-install', 'rollback'):
            with self.subTest(stage=stage):
                self.path.write_bytes(b'old'); proc = self.run_child(code, stage)
                self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr)
                recover_transactions(self.root, apply=True)
                self.assertEqual(self.path.read_bytes(), b'old'); self.assertFalse(pending_transactions(self.root))

    def test_candidate_body_index_request_crash_and_original_request_retry(self):
        code = '''
from pathlib import Path
import os,sys,signal
from scripts.core import write_sets
from scripts.core.candidate_versions import save_candidate_version
root=Path(sys.argv[1]);chapter=sys.argv[2]
def hook(stage,relative=None):
 if stage=='write' and relative.endswith('index.json'): os.kill(os.getpid(),signal.SIGKILL)
write_sets.checkpoint=hook
save_candidate_version(root,chapter,body='frozen body',request_id='R1',base_candidate_version=None)
'''
        proc = self.run_child(code, self.chapter); self.assertEqual(proc.returncode, -signal.SIGKILL, proc.stderr)
        with self.assertRaises(StructureError): list_candidate_versions(self.root, self.chapter)
        recover_transactions(self.root, apply=True)
        first = save_candidate_version(self.root, self.chapter, body='frozen body', request_id='R1', base_candidate_version=None)
        replay = save_candidate_version(self.root, self.chapter, body='frozen body', request_id='R1', base_candidate_version=None)
        self.assertEqual(first['version'], 1); self.assertEqual(replay['version'], 1); self.assertTrue(replay['replayed'])
        self.assertEqual(read_candidate_version(self.root, self.chapter, 1)['body'], 'frozen body')
        listing = list_candidate_versions(self.root, self.chapter)
        self.assertEqual(len(listing['versions']), 1); self.assertTrue(listing['recovery']['consistent'])
        with self.assertRaises(StructureError) as caught:
            save_candidate_version(self.root, self.chapter, body='frozen body', request_id='R1', base_candidate_version=1)
        self.assertEqual(caught.exception.code, 'CANDIDATE_REQUEST_CONFLICT')

    def test_commit_marker_failure_uses_durable_phase_not_mutated_memory(self):
        persist = write_sets._persist
        def fail_commit(root, manifest):
            if manifest['phase'] == 'committed':
                raise OSError('commit marker unavailable before replace')
            return persist(root, manifest)
        with project_lock(self.root), patch.object(write_sets, '_persist', side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, 'commit marker unavailable'):
                write_sets.apply_write_set(self.root, {self.relative: b'new'}, operation='commit_marker_failure')
        self.assertEqual(self.path.read_bytes(), b'old')
        self.assertFalse(pending_transactions(self.root))

    def test_public_finalization_late_save_and_public_recovery_preserve_author_bytes(self):
        from tests.fixtures_workflow import review_ready_project
        from scripts.core.finalization import finalize_chapter
        book = self.base/'finalize'; fixture = review_ready_project(book)
        chapter = fixture['structure']['chapters'][0]; path = book/chapter['filePath']
        relative = chapter['filePath']
        def hook(stage, target=None):
            if stage == 'publish-before-install' and target == relative:
                proc = subprocess.run([sys.executable, '-c',
                    'from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b"author-late-finalize")',
                    str(path)], cwd=REPO, capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        with patch.object(write_sets, 'checkpoint', side_effect=hook):
            with self.assertRaises(OSError):
                finalize_chapter(book, chapter['chapterId'], skip_title=True,
                                 binding_generation=chapter['proseBinding']['generation'])
        self.assertEqual(path.read_bytes(), b'author-late-finalize')
        with self.assertRaises(StructureError): recover_transactions(book, apply=True)
        self.assertEqual(path.read_bytes(), b'author-late-finalize')
        self.assertTrue(pending_transactions(book))

    def test_two_real_http_saves_cannot_share_a_version_or_lose_receipt(self):
        import http.client
        import json
        from http.server import ThreadingHTTPServer
        from scripts.dashboard_server import DashboardHandler
        server = ThreadingHTTPServer(('127.0.0.1', 0), DashboardHandler)
        server.project_root = str(self.root); server.mutation_token = 'test-frozen-token'
        service = threading.Thread(target=server.serve_forever, daemon=True); service.start()
        entered, proceed = threading.Event(), threading.Event()
        results = {}
        def request(request_id, body):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            payload = json.dumps({'chapterId': self.chapter, 'body': body, 'requestId': request_id,
                                  'baseCandidateVersion': None})
            connection.request('POST', '/api/candidate/save', body=payload, headers={
                'Content-Type': 'application/json', 'X-gnovel-Token': 'test-frozen-token'})
            response = connection.getresponse()
            results[request_id] = (response.status, json.loads(response.read()))
            connection.close()
        def hook(stage, relative=None):
            if stage == 'prepared' and threading.current_thread().name != 'MainThread':
                entered.set()
                if not proceed.wait(10): raise RuntimeError('HTTP barrier timed out')
        try:
            with patch.object(write_sets, 'checkpoint', side_effect=hook):
                first = threading.Thread(target=request, args=('A', 'body A')); first.start()
                self.assertTrue(entered.wait(10))
                second = threading.Thread(target=request, args=('B', 'body B')); second.start(); second.join(10)
                self.assertFalse(second.is_alive())
                proceed.set(); first.join(10); self.assertFalse(first.is_alive())
            self.assertEqual(results['A'][0], 200, results)
            self.assertNotEqual(results['B'][0], 200, results)
            self.assertEqual(results['B'][1]['code'], 'PROJECT_LOCKED')
            listing = list_candidate_versions(self.root, self.chapter)
            self.assertEqual([row['version'] for row in listing['versions']], [1])
            self.assertEqual(read_candidate_version(self.root, self.chapter, 1)['body'], 'body A')
            request('A', 'body A')
            self.assertTrue(results['A'][1]['replayed'])
            self.assertEqual(results['A'][1]['version'], 1)
        finally:
            proceed.set(); server.shutdown(); server.server_close(); service.join(5)

    def test_bad_or_old_source_never_creates_lock(self):
        import json
        lock = self.root/'.gnovel/structure.lock'; lock.unlink()
        source = self.root/'.gnovel/structure.json'; original = source.read_bytes()
        for content in ('not json', json.dumps({**read_json(source), 'schemaVersion': 4})):
            source.write_text(content)
            before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            with self.assertRaises(StructureError):
                save_candidate_version(self.root, self.chapter, body='x', request_id='bad', base_candidate_version=None)
            self.assertFalse(lock.exists())
            self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
            source.write_bytes(original)


if __name__ == '__main__': unittest.main()
