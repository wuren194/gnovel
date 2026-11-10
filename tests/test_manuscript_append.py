"""Existing-project intake conserves original prose and shares both transports."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.manuscript_append import (commit_append, inspect_append, preview_append,
                                           read_append_text, validate_append_plan)
from scripts.core.prose_binding import observe_chapter
from scripts.core.structure_commands import _operation_paths
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.project import query_policy
from tests.fixtures_refactor import refactor_project


ROOT = Path(__file__).resolve().parents[1]


class ManuscriptAppendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / '现有项目'
        self.ids = refactor_project(self.root)
        self.source = self.base / '新增 原稿.txt'
        self.raw = '第一章 交班\r\n他把钥匙交给同伴。\r\n\r\n第二章 夜路\r\n雨停了。\r\n'.encode()
        self.source.write_bytes(self.raw)
        self.initial = load_structure(self.root)
        self.prose = {c['filePath']: (self.root / c['filePath']).read_bytes() for c in self.initial['chapters']}
        environment = patch.dict(os.environ, {'GOVEL_LOG_MODE': 'off'})
        environment.start()
        self.addCleanup(environment.stop)

    def arguments(self, **kwargs):
        return {'requestId': 'append-1', 'source': str(self.source), 'volumeId': self.ids['volumeA'],
                'primaryArcId': self.ids['arcMain'], **kwargs}

    def preview(self, **kwargs):
        return preview_append(self.root, **self.arguments(**kwargs))

    def commit(self, preview, **kwargs):
        return commit_append(self.root, operationId=preview['operationId'], planHash=preview['planHash'],
                             approve=True, **kwargs)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(StructureError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)

    def assert_existing_preserved(self):
        for relative, body in self.prose.items():
            self.assertEqual((self.root / relative).read_bytes(), body)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def read_plan(self, preview):
        path, _, _ = _operation_paths(self.root, preview['operationId'])
        return json.loads(path.read_text())

    def test_append_creates_managed_chapters_and_preserves_all_existing_content(self):
        preview = self.preview()
        self.assertEqual(load_structure(self.root), self.initial)
        self.assertEqual(preview['chapterCount'], 2)
        result = self.commit(preview)
        source = load_structure(self.root)
        added = source['chapters'][len(self.initial['chapters']):]
        self.assertEqual(len(added), 2)
        self.assertEqual(b''.join((self.root / c['filePath']).read_bytes() for c in added), self.raw)
        self.assertEqual(source['projectId'], self.initial['projectId'])
        self.assertEqual(source['revision'], self.initial['revision'] + 1)
        for collection in ('entities', 'attributes', 'fragments', 'relations', 'references', 'units'):
            self.assertEqual(source[collection], self.initial[collection])
        for chapter in added:
            self.assertEqual(chapter['status'], 'planning_required')
            self.assertEqual(observe_chapter(self.root, source, chapter)['state'], 'OWNED_CLEAN')
        self.assertFalse(result['semanticReviewComplete'])
        receipt = json.loads((self.root / result['receiptPath']).read_text())
        self.assertEqual([r['chapterId'] for r in receipt['appendManuscript']['chapters']], [c['chapterId'] for c in added])
        original = receipt['appendManuscript']['files'][0]
        self.assertEqual((self.root / original['evidencePath']).read_bytes(), self.raw)
        self.assert_existing_preserved()

    def test_insert_before_chapter_keeps_new_order_and_existing_bindings(self):
        preview = self.preview(beforeChapterId=self.ids['chapterTwo'])
        new_ids = [r['chapterId'] for r in preview['chapters']]
        self.commit(preview)
        chapters = load_structure(self.root)['chapters']
        self.assertEqual([c['chapterId'] for c in chapters], [self.ids['chapterOne'], *new_ids,
                                                           self.ids['chapterTwo'], self.ids['chapterThree']])
        self.assertEqual([c['displayOrdinal'] for c in chapters], list(range(1, 6)))
        old_bindings = {c['chapterId']: c['proseBinding'] for c in self.initial['chapters']}
        for chapter in chapters:
            if chapter['chapterId'] in old_bindings:
                self.assertEqual(chapter['proseBinding'], old_bindings[chapter['chapterId']])
        self.assert_existing_preserved()

    def test_project_scope_requires_global_arc(self):
        arguments = self.arguments(placement={'scope': 'project'})
        arguments.pop('volumeId')
        self.assert_code('ARC_VOLUME_MISMATCH', preview_append, self.root, **arguments)
        arguments['primaryArcId'] = self.ids['arcSide']
        preview = preview_append(self.root, **arguments)
        self.commit(preview)
        self.assertTrue(all(c['placement'] == {'scope': 'project'} for c in load_structure(self.root)['chapters'][3:]))

    def test_manual_boundaries_and_explicit_encoding_preserve_the_original_evidence(self):
        text = '序言\n\n第一章 交班\n交班完成。\n第二章 夜路\n雨停了。\n'
        self.raw = text.encode('gb18030')
        self.source.write_bytes(self.raw)
        preview = self.preview(encoding='gb18030', boundaries=[
            {'sourcePath': self.source.name, 'startLine': 1, 'title': '序言与交班'},
            {'sourcePath': self.source.name, 'startLine': 5, 'title': '夜路'}])
        self.commit(preview)
        added = load_structure(self.root)['chapters'][3:]
        self.assertEqual([c['title'] for c in added], ['序言与交班', '夜路'])
        self.assertEqual(b''.join((self.root / c['filePath']).read_bytes() for c in added), text.encode())
        self.assert_existing_preserved()

    def test_approval_is_required_and_wrong_plan_cannot_commit(self):
        preview = self.preview()
        self.assert_code('APPROVAL_REQUIRED', commit_append, self.root,
                         operationId=preview['operationId'], planHash=preview['planHash'])
        self.assert_code('PLAN_HASH_MISMATCH', commit_append, self.root,
                         operationId=preview['operationId'], planHash='0' * 64, approve=True)
        self.assertEqual(load_structure(self.root), self.initial)

    def test_same_request_reuses_ids_and_rejects_changed_inputs(self):
        first = self.preview()
        second = self.preview()
        self.assertEqual(first, second)
        self.assert_code('SOURCE_REQUEST_REUSED', self.preview, split='file')
        self.commit(first)
        before = (self.root / '.govel/structure.json').read_bytes()
        repeated = self.commit(first)
        self.assertTrue(repeated.get('replayed') or repeated.get('alreadyCommitted'))
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)
        self.assertTrue(self.preview()['alreadyCommitted'])

    def test_damaged_committed_journal_cannot_authorize_reads_replays_or_resume(self):
        preview = self.preview()
        self.commit(preview)
        before = (self.root / '.govel/structure.json').read_bytes()
        _, journal_path, _ = _operation_paths(self.root, preview['operationId'])
        journal = json.loads(journal_path.read_text())
        journal['afterRevision'] += 1
        journal_path.write_text(json.dumps(journal, ensure_ascii=False))
        arguments = {'operationId': preview['operationId'], 'planHash': preview['planHash']}
        self.assert_code('OPERATION_RECEIPT_INVALID', inspect_append, self.root, **arguments)
        self.assert_code('OPERATION_RECEIPT_INVALID', read_append_text, self.root,
                         **arguments, sourcePath=self.source.name)
        self.assert_code('OPERATION_RECEIPT_INVALID', self.commit, preview)
        self.assert_code('OPERATION_RECEIPT_INVALID', self.preview)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)

    def test_changed_original_and_replaced_original_object_are_rejected(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                preview = self.preview(requestId=f'append-{replace}')
                if replace:
                    before = self.source.stat()
                    payload = self.source.read_bytes()
                    # Allocate while the original exists so its inode cannot be recycled.
                    replacement = self.source.with_name(self.source.name + '.replacement')
                    replacement.write_bytes(payload)
                    os.replace(replacement, self.source)
                    after = self.source.stat()
                    self.assertNotEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
                    self.assertEqual(self.source.read_bytes(), payload)
                else:
                    self.source.write_bytes(self.raw + '补充。'.encode())
                self.assert_code('IMPORT_SOURCE_CHANGED', self.commit, preview)
                self.assertEqual(load_structure(self.root), self.initial)
                self.source.write_bytes(self.raw)

    def test_source_set_change_is_rejected_but_unrelated_files_are_ignored(self):
        folder = self.base / '稿件目录'
        folder.mkdir()
        (folder / '2.txt').write_bytes(self.raw)
        (folder / '10.md').write_text('独立章节。')
        preview = self.preview(source=str(folder))
        self.assertEqual([c['sourcePath'] for c in preview['chapters']], ['2.txt', '2.txt', '10.md'])
        (folder / 'notes.json').write_text('{}')
        self.commit(preview)
        preview = self.preview(requestId='append-next', source=str(folder))
        (folder / '3.txt').write_text('新出现的原稿。')
        self.assert_code('IMPORT_SOURCE_CHANGED', self.commit, preview)

    def test_source_change_during_transaction_rolls_back_only_owned_outputs(self):
        preview = self.preview()
        changed = self.raw + '外部新修改。'.encode()
        def interfere(phase, relative=None):
            if phase == 'write' and relative == '.govel/structure.json':
                self.source.write_bytes(changed)
        with patch('scripts.core.write_sets.checkpoint', side_effect=interfere):
            self.assert_code('TRANSACTION_GUARD_DRIFT', self.commit, preview)
        self.assertEqual(load_structure(self.root), self.initial)
        self.assertEqual(self.source.read_bytes(), changed)
        for row in preview['chapters']:
            self.assertFalse((self.root / row['filePath']).exists())

    def test_project_change_rejects_saved_append(self):
        preview = self.preview()
        path = self.root / '.govel/structure.json'
        edited = load_structure(self.root)
        edited['bookName'] = '作者改了书名'
        path.write_text(json.dumps(edited, ensure_ascii=False))
        self.assert_code('REVISION_CONFLICT', self.commit, preview)
        self.assertEqual(load_structure(self.root)['bookName'], '作者改了书名')

    def test_occupied_new_prose_slot_is_never_adopted_or_overwritten(self):
        preview = self.preview()
        path = self.root / preview['chapters'][0]['filePath']
        path.write_text('作者新放入的内容。')
        with self.assertRaises(StructureError):
            self.commit(preview)
        self.assertEqual(path.read_text(), '作者新放入的内容。')
        self.assertEqual(load_structure(self.root), self.initial)

    def test_domain_replay_rejects_unrelated_edits_and_forged_ranges(self):
        preview = self.preview()
        plan = self.read_plan(preview)
        changed = deepcopy(plan)
        changed['proposedStructure']['entities'][0]['name'] = '不属于追加的更名'
        self.assert_code('APPEND_PLAN_INVALID', validate_append_plan, self.root, self.initial, changed)
        changed = deepcopy(plan)
        changed['appendManuscript']['chapters'][0]['utf8ByteStart'] = 1
        self.assert_code('APPEND_PLAN_INVALID', validate_append_plan, self.root, self.initial, changed)
        changed = deepcopy(plan)
        changed['manuscriptMaterialization'][0]['sourcePath'] = self.initial['chapters'][0]['filePath']
        self.assert_code('APPEND_PLAN_INVALID', validate_append_plan, self.root, self.initial, changed)

    def test_frozen_original_or_chapter_tampering_is_rejected(self):
        preview = self.preview()
        plan = self.read_plan(preview)
        source = self.root / plan['appendManuscript']['files'][0]['snapshotPath']
        source.write_bytes(self.raw + b'changed')
        self.assert_code('IMPORT_SNAPSHOT_DAMAGED', self.commit, preview)
        preview = self.preview(requestId='append-new')
        plan = self.read_plan(preview)
        (self.root / plan['manuscriptMaterialization'][0]['sourcePath']).write_text('替换了章节。')
        with self.assertRaises(StructureError):
            self.commit(preview)
        self.assertEqual(load_structure(self.root), self.initial)

    def test_failure_mid_commit_preserves_existing_project_and_can_retry(self):
        preview = self.preview()
        def fail(phase, relative=None):
            if phase == 'write' and relative == '.govel/structure.json':
                raise RuntimeError('injected write failure')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.commit(preview)
        self.assertEqual(load_structure(self.root), self.initial)
        self.assert_existing_preserved()
        self.commit(preview)
        self.assertEqual(len(load_structure(self.root)['chapters']), 5)

    def test_reply_lost_after_commit_replays_the_original_receipt(self):
        preview = self.preview()
        def fail(phase, relative=None):
            if phase == 'committed':
                raise RuntimeError('injected lost response')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.commit(preview)
        result = self.commit(preview)
        self.assertTrue(result.get('replayed') or result.get('alreadyCommitted'))
        self.assertEqual(len(load_structure(self.root)['chapters']), 5)

    def test_symlinks_invalid_placement_and_overlapping_sources_are_rejected(self):
        link = self.base / 'linked.txt'
        link.symlink_to(self.source)
        self.assert_code('IMPORT_SOURCE_INVALID', self.preview, source=str(link))
        self.assert_code('PLACEMENT_ARGUMENT_INVALID', self.preview, placement={'scope': 'project'})
        self.assert_code('VOLUME_NOT_FOUND', self.preview, volumeId='VOL-MISSING')
        self.assert_code('APPEND_SOURCE_OVERLAP', self.preview, source=str(self.root))
        self.assert_code('APPEND_SOURCE_OVERLAP', self.preview, source=str(self.root / '正文'))
        self.assert_code('APPEND_SOURCE_OVERLAP', self.preview, source=str(self.root / '.govel'))

    def test_workbench_manuscript_can_be_appended_without_modifying_its_source(self):
        folder = self.root / '工作台' / '外部稿件'
        folder.mkdir()
        original = folder / '新章.txt'
        original.write_bytes(self.raw)
        preview = self.preview(source=str(folder))
        self.commit(preview)
        self.assertEqual(original.read_bytes(), self.raw)
        self.assertEqual(len(load_structure(self.root)['chapters']), 5)
        self.assert_existing_preserved()

    def test_bounded_inspection_and_text_reads_survive_source_deletion_after_commit(self):
        self.raw = ''.join(f'第{i}章 交班\n' + '他归还钥匙。' * 30 + '\n' for i in range(35)).encode()
        self.source.write_bytes(self.raw)
        preview = self.preview()
        rows, offset = [], 0
        while True:
            page = inspect_append(self.root, operationId=preview['operationId'], planHash=preview['planHash'], offset=offset, limit=5)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), query_policy()['maxBytes']['list'])
            rows.extend(page['chapters'])
            if page['nextOffset'] is None:
                break
            self.assertGreater(page['nextOffset'], offset)
            offset = page['nextOffset']
        self.assertEqual([row['ordinal'] for row in rows], list(range(1, 36)))
        self.commit(preview)
        self.source.unlink()
        chunks, offset = [], 0
        while True:
            page = read_append_text(self.root, operationId=preview['operationId'], planHash=preview['planHash'],
                                    sourcePath=self.source.name, offset=offset)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), query_policy()['maxBytes']['detail'])
            self.assertTrue(page['historicalReceipt'])
            chunks.append(page['text'])
            if page['nextOffset'] is None:
                break
            self.assertGreater(page['nextOffset'], offset)
            offset = page['nextOffset']
        self.assertEqual(''.join(chunks).encode(), self.raw)

    def test_real_cli_preview_and_mcp_commit_share_a_plan(self):
        environment = dict(os.environ, GOVEL_LOG_MODE='off', PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'import', 'append-preview',
            '--project-root', str(self.root), '--request-id', 'append-cli', '--source', str(self.source),
            '--volume-id', self.ids['volumeA'], '--primary-arc-id', self.ids['arcMain']],
            cwd=self.base, env=environment, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        preview = json.loads(process.stdout)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'commit_manuscript_append', 'arguments': preview['nextAction']['arguments']}}
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')], input=json.dumps(request) + '\n',
            cwd=self.base, env=environment, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent']['code'], 'MANUSCRIPT_APPENDED')
        self.assertEqual(len(load_structure(self.root)['chapters']), 5)


if __name__ == '__main__':
    unittest.main()
