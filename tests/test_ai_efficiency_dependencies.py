"""Prepared pages depend on consumed inputs, never an unrelated workbench tree."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from scripts.core.transport_cache import get, _directory
from tests.fixtures_workflow import review_ready_project


class PreparationDependencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def prepare(self):
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        return report, {'credential': report['reading']['credential'], 'readIds': report['contractReads'][0]['readIds'],
                        'maxBytes': 5000}

    def test_unrelated_workbench_add_edit_delete_does_not_invalidate_page_or_receipt(self):
        report, args = self.prepare()
        first = call_operation('task.read-batch', self.root, args)
        note = self.root / '工作台/无关笔记.md'
        with patch('scripts.core.reading_batch.prepare_task', side_effect=AssertionError('must reuse bound preparation')):
            for action in (lambda: note.write_text('独立笔记'), lambda: note.write_text('改动独立笔记'), note.unlink):
                action()
                page = call_operation('task.read-batch', self.root, args)
                self.assertEqual(page['items'], first['items'])
            if first['hasMore']:
                call_operation('task.read-batch', self.root, {**args, 'cursor': first['nextCursor']})
        snapshot = get(self.root, report['reading']['credential'], kind='prepare')
        self.assertNotIn('工作台/无关笔记.md', snapshot['dependencies']['files'])

    def test_conventional_unregistered_candidate_is_bound_only_when_selected(self):
        from scripts.core.transport_cache import prepared
        relative = f'工作台/{self.chapter}-设定回写候选.md'
        note = self.root / relative
        titler = call_operation('task.prepare', self.root, {'task': 'titler', 'chapterId': self.chapter})
        credential = titler['reading']['credential']
        self.assertNotIn(relative, get(self.root, credential, kind='prepare')['dependencies']['files'])
        for action in (lambda: note.write_text('尚未登记的回写笔记'), lambda: note.write_text('编辑笔记'), note.unlink):
            action()
            prepared(self.root, credential)
        continuity = call_operation('task.prepare', self.root, {'task': 'continuity', 'chapterId': self.chapter})
        credential = continuity['reading']['credential']
        self.assertIsNone(get(self.root, credential, kind='prepare')['dependencies']['files'][relative]['state'])
        note.write_text('现在成为连续性任务的输入')
        with self.assertRaises(StructureError) as caught:
            prepared(self.root, credential)
        self.assertIn(relative, [row.get('path') for row in caught.exception.details['changedItems']])

    def test_registered_workbench_content_and_authorization_change_are_named(self):
        report, args = self.prepare()
        workflow = read_json(self.root / '.govel/workflow.json')
        candidate = self.root / workflow['artifacts'][self.chapter]['candidate_prose']['artifactPath']
        candidate.write_bytes(candidate.read_bytes() + b'\n')
        with self.assertRaises(StructureError) as caught:
            call_operation('task.read-batch', self.root, args)
        self.assertIn(candidate.relative_to(self.root).as_posix(),
                      [item.get('path') for item in caught.exception.details['changedItems']])
        _, args = self.prepare()
        workflow['approvedChapterIds'] = []
        write_json(self.root / '.govel/workflow.json', workflow)
        with self.assertRaises(StructureError) as caught:
            call_operation('task.read-batch', self.root, args)
        self.assertTrue(any(item['kind'] == 'authorization' and item['path'] == '.govel/workflow.json'
                            for item in caught.exception.details['changedItems']))

    def test_new_member_of_a_consumed_query_invalidates(self):
        _, args = self.prepare()
        path = self.root / '.govel/transactions/TX-test.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {'transactionId': 'TX-test', 'phase': 'prepared'})
        from scripts.core.transport_cache import prepared
        with self.assertRaises(StructureError) as caught:
            prepared(self.root, args['credential'])
        membership = next(item for item in caught.exception.details['changedItems'] if item['kind'] == 'query-membership')
        self.assertIn('.govel/transactions/TX-test.json', membership['added'])

    def test_creation_of_optional_project_rules_is_not_ignored(self):
        _, args = self.prepare()
        (self.root / 'AGENTS.md').write_text('新增项目约束。')
        with self.assertRaises(StructureError) as caught:
            call_operation('task.read-batch', self.root, args)
        self.assertTrue(any(row.get('path') == 'AGENTS.md' for row in caught.exception.details['changedItems']))

    def test_input_change_during_preparation_is_rejected(self):
        from scripts.core.task_prepare import _prepare_task, TaskPrepareError
        def mutate(*args, **kwargs):
            result = _prepare_task(*args, **kwargs)
            (self.root / 'AGENTS.md').write_text('准备窗口内更改。')
            return result
        with patch('scripts.core.task_prepare._prepare_task', side_effect=mutate), self.assertRaises(TaskPrepareError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'TASK_INPUT_CHANGED')
        self.assertTrue(caught.exception.details['changedItems'])


class EvidencePageDependencyTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures_source import source_fixture
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); source = source_fixture(self.root)
        self.record = source['fragments'][0]['fragmentId']
        source['fragments'][0]['text'] = '原始连续正文😀。\r\n' * 2000
        self.text = source['fragments'][0]['text']
        write_json(self.root / '.govel/structure.json', source)
        self.args = {'kind': 'review', 'recordIds': [self.record], 'maxBytes': 3000}

    def test_remaining_pages_do_not_prepare_again_and_keep_original_snapshot(self):
        first = call_operation('evidence.prepare', self.root, self.args)
        texts = [row['text'] for row in first['materials']]
        cursor = first['nextCursor']
        (self.root / '工作台').mkdir(exist_ok=True)
        (self.root / '工作台/无关.txt').write_text('无关笔记')
        with patch('scripts.core.evidence_pack._prepare_snapshot', side_effect=AssertionError('must reuse snapshot')):
            while cursor:
                page = call_operation('evidence.prepare', self.root, {**self.args, 'cursor': cursor,
                                      'snapshotRef': first['snapshotRef']})
                self.assertEqual(page['snapshotRef'], first['snapshotRef'])
                texts.extend(row['text'] for row in page['materials'])
                cursor = page['nextCursor']
        self.assertEqual(''.join(texts), self.text)
        self.assertFalse(page['semanticReviewComplete'])

    def test_changed_inputs_and_missing_page_cache_cannot_rebind(self):
        first = call_operation('evidence.prepare', self.root, self.args)
        source = read_json(self.root / '.govel/structure.json')
        source['fragments'][0]['text'] = 'changed'
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError) as caught:
            call_operation('evidence.prepare', self.root, {**self.args, 'cursor': first['nextCursor']})
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')
        self.assertTrue(caught.exception.details['changedItems'])
        from scripts.core.query import _cursor_decode
        reference = _cursor_decode(first['nextCursor'])['source']
        (_directory() / (reference.replace(':', '-') + '.json')).unlink()
        with self.assertRaises(StructureError) as caught:
            call_operation('evidence.prepare', self.root, {**self.args, 'cursor': first['nextCursor']})
        self.assertEqual(caught.exception.code, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
