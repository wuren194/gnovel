"""Current source transactions and durable write-set failure/recovery guarantees."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core import write_sets
from scripts.core.catalog_commands import preview_create_entity
from scripts.core.finalization import pending_transactions, recover_transactions
from scripts.core.jsonio import read_json, write_json
from scripts.core.locks import project_lock
from scripts.core.settings import query_settings
from scripts.core.source_changes import preview_source_change
from scripts.core.structure_commands import commit_plan, undo_operation
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_current import create_schema5_project

ROOT = Path(__file__).resolve().parents[1]


class PropagationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = create_schema5_project(self.root, chapter_count=1)

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def plan(self, value='当前值'):
        source = load_structure(self.root)
        return preview_source_change(self.root, requestId='new-voice-field', baseRevision=source['revision'],
            reason='显式登记人物声纹约束', changes=[
                {'action': 'put', 'collection': 'fieldDefinitions', 'record': {
                    'fieldId': '$field', 'key': 'voice.ban', 'label': '声纹禁词', 'valueType': 'text', 'appliesTo': ['character']}},
                {'action': 'put', 'collection': 'attributes', 'record': {
                    'attributeId': '$attribute', 'ownerId': self.fixture['characterId'], 'fieldId': '$field',
                    'values': [{'valueId': '$value', 'value': value}]}},
            ])

    def business(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()
                and not set(p.relative_to(self.root).parts) & {'plans', 'backups', 'transactions', 'operations'}
                and p.name != 'structure.lock'}

    def test_source_fields_and_query_share_one_commit_without_markdown_mirrors(self):
        plan = self.plan()
        self.assertFalse(any(row['path'].startswith(('设定/', '规划/')) for row in plan['writeSet']))
        self.commit(plan)
        self.assertEqual(query_settings(self.root, selector=self.fixture['characterId'])['matches'][0]['fields']['voice.ban'], '当前值')
        self.assertFalse((self.root/'设定/人物设定.md').exists())
        self.assertFalse((self.root/'设定/实体索引.md').exists())

    def test_creation_and_undo_leave_no_extra_source_entity_or_views(self):
        plan = preview_create_entity(self.root, entity_type='character', name='新增人物')
        self.commit(plan)
        self.assertEqual(query_settings(self.root, selector='新增人物')['matchedCount'], 1)
        undo_operation(self.root, plan['operationId'])
        self.assertEqual(query_settings(self.root, selector='新增人物')['matchedCount'], 0)
        self.assertFalse((self.root/'设定/人物设定.md').exists())

    def test_authored_reading_files_remain_outside_commit_and_undo(self):
        authored = self.root/'设定/人物设定.md'; authored.write_text('作者内容')
        plan = self.plan(); self.commit(plan)
        authored.write_text('作者继续整理阅读资料')
        undo_operation(self.root, plan['operationId'])
        self.assertEqual(authored.read_text(), '作者继续整理阅读资料')
        self.assertNotIn('voice.ban', query_settings(self.root, selector=self.fixture['characterId'])['matches'][0]['fields'])

    def test_changed_source_refuses_commit_without_overwriting_external_edit(self):
        plan = self.plan(); path = self.root/'.govel/structure.json'
        source = read_json(path); source['revision'] += 1; write_json(path, source)
        before = self.business()
        with self.assertRaises(StructureError): self.commit(plan)
        self.assertEqual(before, self.business())

    def test_exception_restores_source_set_and_same_plan_can_retry(self):
        plan = self.plan(); before = self.business()
        def fail(phase, relative=None):
            if phase == 'write' and relative == '.govel/structure.json':
                raise RuntimeError('injected')
        with patch.object(write_sets, 'checkpoint', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'injected'): self.commit(plan)
        self.assertEqual(before, self.business())
        self.assertEqual(self.commit(plan)['code'], 'STRUCTURE_COMMITTED')

    def test_typed_fields_do_not_invent_or_overwrite_author_relations(self):
        source = load_structure(self.root)
        source['relations'] = [{'relationId': 'REL-AUTHOR', 'fromEntity': self.fixture['propId'],
            'toEntity': self.fixture['characterId'], 'kind': 'owns', 'origin': 'author'}]
        write_json(self.root/'.govel/structure.json', source)
        self.commit(self.plan())
        self.assertEqual(load_structure(self.root)['relations'], source['relations'])

    def test_crash_after_structure_is_detected_and_recoverable_via_cli(self):
        plan = self.plan(); before = self.business()
        script = """import os,sys
from pathlib import Path
from scripts.core import write_sets
from scripts.core.structure_commands import commit_plan
write_sets.checkpoint=lambda phase, relative=None: os._exit(77) if phase=='write' and relative=='.govel/structure.json' else None
commit_plan(Path(sys.argv[1]), operation_id=sys.argv[2], plan_hash=sys.argv[3], approved=True)
"""
        result = subprocess.run([sys.executable, '-c', script, str(self.root), plan['operationId'], plan['planHash']], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr)
        with self.assertRaises(StructureError) as caught: query_settings(self.root)
        self.assertEqual(caught.exception.code, 'TRANSACTION_INCOMPLETE')
        result = subprocess.run([sys.executable, str(ROOT/'scripts/novelctl.py'), 'project', 'workflow', 'recover', '--apply',
            '--project-root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, self.business())
        self.commit(plan)


class DurableWriteSetTests(unittest.TestCase):
    def test_every_write_stage_and_recovery_reentry_preserve_before_images(self):
        for stop in range(1, 4):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                from tests.fixtures_current import create_schema5_project
                create_schema5_project(root, chapter_count=0)
                (root / 'update').write_text('before'); (root / 'delete').write_text('before')
                script = """import os,sys
from pathlib import Path
from scripts.core import write_sets
from scripts.core.locks import project_lock
n=0
def checkpoint(phase, relative=None):
 global n
 if phase=='write':
  n+=1
  if n==int(sys.argv[2]): os._exit(77)
write_sets.checkpoint=checkpoint
with project_lock(Path(sys.argv[1])):
 write_sets.apply_write_set(Path(sys.argv[1]), {'create':b'after','update':b'after','delete':None}, operation='test')
"""
                result = subprocess.run([sys.executable, '-c', script, str(root), str(stop)], cwd=ROOT, capture_output=True)
                self.assertEqual(result.returncode, 77, result.stderr)
                crashed = False
                def interrupt(phase, relative=None):
                    nonlocal crashed
                    if phase == 'rollback' and not crashed:
                        crashed = True
                        raise RuntimeError('recovery interrupted')
                with patch.object(write_sets, 'checkpoint', side_effect=interrupt):
                    with self.assertRaises(RuntimeError):
                        recover_transactions(root, apply=True)
                recover_transactions(root, apply=True)
                self.assertFalse((root / 'create').exists())
                self.assertEqual((root / 'update').read_text(), 'before')
                self.assertEqual((root / 'delete').read_text(), 'before')

    def test_unknown_post_crash_edits_block_all_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            from tests.fixtures_current import create_schema5_project
            create_schema5_project(root, chapter_count=0)
            (root / 'a').write_text('before')
            def fail(phase, relative=None):
                if phase == 'write':
                    (root / 'a').write_text('author repair')
                    raise RuntimeError('stop')
            with project_lock(root), patch.object(write_sets, 'checkpoint', side_effect=fail):
                with self.assertRaisesRegex(OSError, 'rollback incomplete'):
                    write_sets.apply_write_set(root, {'a': b'after'}, operation='test')
            with self.assertRaises(StructureError):
                recover_transactions(root, apply=True)
            self.assertEqual((root / 'a').read_text(), 'author repair')

    # --- read-only guards ---------------------------------------------------------------
    # A guard row is a file the transaction compares but never owns. These tests pin the
    # difference against a normal writable row: same veto power over the commit marker, none
    # of the recovery authority.

    def _guard_root(self, temp: str) -> Path:
        root = Path(temp).resolve()
        (root / '.govel').mkdir()
        (root / 'business.json').write_text('old', encoding='utf-8')
        (root / 'authored.md').write_text('H1', encoding='utf-8')
        return root

    def _guard_race(self, at: str) -> StructureError:
        """Drift an authored file at one exact transaction point and keep what it proved."""
        with tempfile.TemporaryDirectory() as temp:
            root = self._guard_root(temp)
            guards = write_sets.capture_guards(root, ['authored.md'])

            def hook(phase, relative=None):
                if (at == 'prepared' and phase == 'prepared') or (at == 'last-write' and phase == 'write'):
                    (root / 'authored.md').write_text('H2 written by someone else', encoding='utf-8')
                    if at == 'last-write':
                        # the drift must not be an accident of ordering: prove the business file
                        # really did land before the guard was re-read
                        self.assertEqual((root / 'business.json').read_text(), 'new')

            with patch.object(write_sets, 'checkpoint', side_effect=hook):
                with self.assertRaises(StructureError) as caught:
                    write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                               guards=guards)
            self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
            self.assertEqual(caught.exception.details['stage'],
                             'after-prepared' if at == 'prepared' else 'before-committed')
            self.assertEqual((root / 'business.json').read_text(), 'old', 'COMPLETE OLD')
            self.assertEqual((root / 'authored.md').read_text(), 'H2 written by someone else',
                             'a guard is never restored to its before-image')
            self.assertEqual(pending_transactions(root), [], 'the vetoed manifest is gone')
            return caught.exception

    def test_a_guard_that_moves_between_manifest_and_first_write_vetoes_the_commit(self) -> None:
        self._guard_race('prepared')

    def test_a_guard_that_moves_after_the_last_write_still_vetoes_the_commit_marker(self) -> None:
        """The window that used to make an API failure sit on top of a committed transaction."""
        self._guard_race('last-write')

    def test_a_guard_that_already_moved_is_refused_before_anything_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._guard_root(temp)
            guards = write_sets.capture_guards(root, ['authored.md'])
            (root / 'authored.md').write_text('H2', encoding='utf-8')
            with self.assertRaises(StructureError) as caught:
                write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                           guards=guards)
            self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
            self.assertEqual(caught.exception.details['stage'], 'preparing')
            self.assertEqual((root / 'business.json').read_text(), 'old')
            self.assertFalse((root / '.govel' / 'transactions').exists(),
                             'not even a transaction directory was created')

    def test_identical_bytes_on_a_different_object_are_still_guard_drift(self) -> None:
        """Byte equality alone would let an outsider swap the file and keep the commit."""
        with tempfile.TemporaryDirectory() as temp:
            root = self._guard_root(temp)
            guards = write_sets.capture_guards(root, ['authored.md'])
            path = root / 'authored.md'
            payload = path.read_bytes()
            path.unlink()
            path.write_bytes(payload)
            with self.assertRaises(StructureError) as caught:
                write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                           guards=guards)
            details = caught.exception.details
            self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
            self.assertEqual(details['expected']['sha256'], details['observed']['sha256'],
                             'the bytes really are identical, which is why bytes are not enough')
            self.assertNotEqual(details['expected']['ino'], details['observed']['ino'],
                                'and the object under those bytes is a different one')
            self.assertEqual((root / 'business.json').read_text(), 'old')

    def test_a_deleted_or_replaced_guard_object_fails_closed(self) -> None:
        for shape in ('deleted', 'directory'):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as temp:
                root = self._guard_root(temp)
                guards = write_sets.capture_guards(root, ['authored.md'])
                path = root / 'authored.md'

                def hook(phase, relative=None):
                    if phase != 'prepared':
                        return
                    payload = path.read_bytes()
                    path.unlink()
                    if shape == 'directory':
                        path.mkdir()
                    else:
                        (root / 'moved-elsewhere.md').write_bytes(payload)

                with patch.object(write_sets, 'checkpoint', side_effect=hook):
                    with self.assertRaises(StructureError) as caught:
                        write_sets.apply_write_set(root, {'business.json': b'new'},
                                                   operation='test', guards=guards)
                details = caught.exception.details
                self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
                self.assertEqual(details['observed']['exists'], True if shape == 'directory' else False)
                self.assertIsNone(details['observed']['sha256'],
                                  'what sits there now is not the file that was observed')
                self.assertEqual((root / 'business.json').read_text(), 'old')
                if shape == 'directory':
                    path.rmdir()

    def test_a_drifted_guard_never_strands_an_interrupted_transaction(self) -> None:
        """Guard drift vetoes a commit; it must never block rolling metadata back."""
        with tempfile.TemporaryDirectory() as temp:
            root = self._guard_root(temp)
            guards = write_sets.capture_guards(root, ['authored.md'])

            def hook(phase, relative=None):
                if phase == 'write':
                    (root / 'authored.md').write_text('the author kept editing', encoding='utf-8')
                    raise OSError('injected crash')

            with patch.object(write_sets, 'checkpoint', side_effect=hook):
                with self.assertRaises(OSError):
                    write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                               guards=guards)
            self.assertEqual((root / 'business.json').read_text(), 'old')
            self.assertEqual((root / 'authored.md').read_text(), 'the author kept editing')
            self.assertEqual(pending_transactions(root), [])

    def test_guard_rows_are_recorded_as_guards_and_never_as_writable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._guard_root(temp)
            guards = write_sets.capture_guards(root, ['authored.md'])
            manifests, backed_up = [], []
            real_persist = write_sets._persist

            def recording(project_root, manifest):
                manifests.append(json.loads(json.dumps(manifest)))
                real_persist(project_root, manifest)

            def hook(phase, relative=None):
                if phase == 'prepared':
                    directory = root / '.govel' / 'transactions' / manifests[-1]['transactionId']
                    backed_up.append(sorted(str(p.relative_to(directory / 'before'))
                                            for p in (directory / 'before').rglob('*') if p.is_file()))

            with patch.object(write_sets, '_persist', side_effect=recording), \
                    patch.object(write_sets, 'checkpoint', side_effect=hook):
                write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                           guards=guards)
            prepared = next(manifest for manifest in manifests if manifest['phase'] == 'prepared')
            self.assertEqual([row['path'] for row in prepared['guards']], ['authored.md'])
            self.assertNotIn('authored.md', [row['path'] for row in prepared['files']])
            self.assertEqual(backed_up, [['business.json']],
                             'a guard carries no before-image, so it has nothing to restore')
            self.assertEqual((root / 'authored.md').read_text(), 'H1')
            self.assertEqual((root / 'business.json').read_text(), 'new')

    def test_a_committed_manifest_with_a_drifted_guard_only_clears_its_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._guard_root(temp)
            guards = write_sets.capture_guards(root, ['authored.md'])
            def interrupt(stage, relative=None):
                if stage == 'committed':
                    raise KeyboardInterrupt('committed interruption')
            with patch.object(write_sets, 'checkpoint', side_effect=interrupt), \
                    patch.object(write_sets, 'recover_write_set', side_effect=KeyboardInterrupt('stop cleanup')):
                with self.assertRaises(OSError):
                    write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test', guards=guards)
            manifest = pending_transactions(root)[0]
            (root / 'authored.md').write_text('edited after the commit', encoding='utf-8')
            actions = write_sets.recover_write_set(root, manifest)
            self.assertEqual(actions, [{'action': 'committed_cleanup'}])
            self.assertEqual((root / 'business.json').read_text(), 'new', 'COMPLETE NEW stands')
            self.assertEqual((root / 'authored.md').read_text(), 'edited after the commit')
            self.assertEqual(pending_transactions(root), [])

    # --- read-only tree guards ----------------------------------------------------------
    # Guard rows protect the paths that were enumerated when they were minted. A path that
    # did not exist then is invisible to every row, so membership is guarded as its own
    # claim about the roots: same veto over the commit marker, and the same absence of any
    # recovery authority over what it observed.

    def _tree_root(self, temp: str) -> Path:
        root = Path(temp).resolve()
        (root / '.govel').mkdir()
        (root / 'business.json').write_text('old', encoding='utf-8')
        (root / '设定').mkdir()
        (root / '设定' / 'a.md').write_text('A', encoding='utf-8')
        (root / '正文').mkdir()
        (root / '正文' / 'c.md').write_text('C', encoding='utf-8')
        return root

    def _tree_guard(self, root: Path) -> dict:
        return write_sets.tree_membership(root, ['正文', '设定'])

    def test_a_new_authored_path_is_membership_drift_at_every_check_point(self) -> None:
        """The case no per-file guard row can see: the path did not exist when rows were minted."""
        for at, stage, durable in (('before', 'preparing', False),
                                   ('prepared', 'after-prepared', True),
                                   ('write', 'before-committed', True)):
            with self.subTest(at=at), tempfile.TemporaryDirectory() as temp:
                root = self._tree_root(temp)
                guard = self._tree_guard(root)

                def hook(phase, relative=None, *, _at=at, _root=root):
                    if _at == 'prepared' and phase == 'prepared':
                        (_root / '设定' / 'appeared.md').write_text('new material', encoding='utf-8')
                    if _at == 'write' and phase == 'write':
                        (_root / '设定' / 'appeared.md').write_text('new material', encoding='utf-8')

                if at == 'before':
                    (root / '设定' / 'appeared.md').write_text('new material', encoding='utf-8')
                with patch.object(write_sets, 'checkpoint', side_effect=hook):
                    with self.assertRaises(StructureError) as caught:
                        write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                                   tree_guards=[guard])
                details = caught.exception.details
                self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
                self.assertEqual(details['stage'], stage)
                self.assertEqual(details['added'], ['设定/appeared.md'])
                self.assertEqual(details['removed'], [])
                self.assertEqual((root / 'business.json').read_text(), 'old', 'COMPLETE OLD')
                self.assertEqual((root / '设定' / 'appeared.md').read_text(), 'new material',
                                 'the author’s new file is not a recovery target')
                self.assertEqual(pending_transactions(root), [])
                if not durable:
                    self.assertFalse((root / '.govel' / 'transactions').exists(),
                                     'a membership veto that lands first costs no durable work')

    def test_a_rename_or_deletion_of_a_member_is_membership_drift(self) -> None:
        """Both directions of the member list: a disappearance and an equal-length swap."""
        for shape, added, removed in (('delete', [], ['正文/c.md']),
                                      ('rename', ['正文/renamed.md'], ['正文/c.md'])):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as temp:
                root = self._tree_root(temp)
                guard = self._tree_guard(root)

                def hook(phase, relative=None, *, _shape=shape, _root=root):
                    if phase != 'prepared':
                        return
                    if _shape == 'delete':
                        (_root / '正文' / 'c.md').unlink()
                    else:
                        (_root / '正文' / 'c.md').rename(_root / '正文' / 'renamed.md')

                with patch.object(write_sets, 'checkpoint', side_effect=hook):
                    with self.assertRaises(StructureError) as caught:
                        write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                                   tree_guards=[guard])
                details = caught.exception.details
                self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
                self.assertEqual(details['stage'], 'after-prepared')
                self.assertEqual(details['added'], added)
                self.assertEqual(details['removed'], removed)
                self.assertEqual((root / 'business.json').read_text(), 'old', 'COMPLETE OLD')
                if shape == 'delete':
                    self.assertFalse((root / '正文' / 'c.md').exists(),
                                     'a deleted member is never restored')
                else:
                    self.assertEqual((root / '正文' / 'renamed.md').read_text(), 'C',
                                     'a rename is never reversed')

    def test_membership_drift_never_strands_an_interrupted_transaction(self) -> None:
        """§9: recovery restores only the files rows, and a new path neither blocks nor joins it."""
        with tempfile.TemporaryDirectory() as temp:
            root = self._tree_root(temp)
            guard = self._tree_guard(root)

            def hook(phase, relative=None):
                if phase == 'write':
                    (root / '设定' / 'appeared.md').write_text('author kept going', encoding='utf-8')
                    raise OSError('injected crash')

            with patch.object(write_sets, 'checkpoint', side_effect=hook):
                with self.assertRaises(OSError):
                    write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                               tree_guards=[guard])
            self.assertEqual((root / 'business.json').read_text(), 'old', 'COMPLETE OLD')
            self.assertEqual((root / '设定' / 'appeared.md').read_text(), 'author kept going')
            self.assertEqual(pending_transactions(root), [])

    def test_tree_guard_rows_carry_membership_only_and_no_physical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = self._tree_root(temp)
            guard = self._tree_guard(root)
            manifests = []
            real_persist = write_sets._persist

            def recording(project_root, manifest):
                manifests.append(json.loads(json.dumps(manifest)))
                real_persist(project_root, manifest)

            with patch.object(write_sets, '_persist', side_effect=recording):
                write_sets.apply_write_set(root, {'business.json': b'new'}, operation='test',
                                           tree_guards=[guard])
            prepared = next(manifest for manifest in manifests if manifest['phase'] == 'prepared')
            self.assertEqual(set(prepared['treeGuards'][0]),
                             {'roots', 'members', 'membershipDigest'},
                             'a tree guard is membership, so it has neither dev/ino nor before/after images')
            self.assertEqual(prepared['treeGuards'][0]['members'], ['正文/c.md', '设定/a.md'])
            self.assertEqual(prepared['treeGuards'][0]['membershipDigest'], guard['membershipDigest'])
            self.assertEqual((root / 'business.json').read_text(), 'new')


if __name__ == '__main__':
    unittest.main()
