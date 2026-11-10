"""Adversarial manuscript-plan cases must not weaken guards or invent commit success."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.chapter_reshape import preview_split, query_lineage
from scripts.core.jsonio import read_json, write_json
from scripts.core.manuscript_materialization import commit_manuscript_plan
from scripts.core.operation_receipts import require_committed_operation
from scripts.core.prose_binding import OWNED_CLEAN, observe_chapter
from scripts.core.structure_commands import _plan_hash, undo_operation
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_refactor import refactor_project


class ManuscriptMaterializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'book'
        self.fixture = refactor_project(self.root)
        self.initial = load_structure(self.root)
        self.chapter = self.fixture['chapterOne']

    def preview(self, root=None, **arguments):
        return preview_split(root or self.root, **{'requestId': 'materialization-1', 'chapterId': self.chapter,
            'cutOffsets': [7], 'reason': '作者在场景交接处分章', **arguments})

    def commit(self, preview, root=None):
        return commit_manuscript_plan(root or self.root, operationId=preview['operationId'],
                                      planHash=preview['planHash'], approve=True)

    def rewrite(self, preview, transform):
        path = self.root / '.govel/plans' / (preview['operationId'] + '.json')
        plan = read_json(path)
        transform(plan)
        plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
        write_json(path, plan)
        return plan

    def test_deleting_either_guard_set_cannot_remove_commit_protection(self):
        for field in ('manuscriptGuards', 'manuscriptGuardGroups'):
            with self.subTest(field=field):
                preview = self.preview(requestId='missing-' + field)
                plan = self.rewrite(preview, lambda plan: plan.pop(field))
                writes = []
                def edit(stage, *args):
                    if stage == 'write':
                        writes.append(args)
                        (self.root / f'正文/{self.chapter}.md').write_bytes(b'concurrent external edit')
                with patch('scripts.core.write_sets.checkpoint', side_effect=edit):
                    with self.assertRaises(StructureError) as caught:
                        self.commit(plan)
                self.assertEqual(caught.exception.code, 'MANUSCRIPT_INPUT_CHANGED')
                self.assertEqual(writes, [])
                self.assertEqual(load_structure(self.root), self.initial)
                for row in preview['impact']['chapterReshape']:
                    self.assertFalse((self.root / row['filePath']).exists())

    def test_empty_forged_guard_list_is_not_a_new_observation(self):
        preview = self.preview()
        plan = self.rewrite(preview, lambda plan: plan.update(manuscriptGuards=[]))
        with self.assertRaises(StructureError) as caught:
            self.commit(plan)
        self.assertEqual(caught.exception.code, 'MANUSCRIPT_INPUT_CHANGED')
        self.assertEqual(load_structure(self.root), self.initial)

    def test_three_field_journal_does_not_claim_success(self):
        preview = self.preview()
        write_json(self.root / '.govel/operations' / (preview['operationId'] + '.json'),
                   {key: preview[key] for key in ('operation', 'operationId', 'planHash')})
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'OPERATION_RECEIPT_INVALID')
        self.assertEqual(load_structure(self.root), self.initial)
        with self.assertRaises(StructureError) as caught:
            query_lineage(self.root, chapterId=self.chapter)
        self.assertEqual(caught.exception.code, 'OPERATION_RECEIPT_INVALID')

    def test_well_formed_journal_copied_without_its_transaction_witness_is_rejected(self):
        preview = self.preview()
        other = self.base / 'other-book'
        refactor_project(other)
        other_preview = self.preview(other)
        self.commit(other_preview, other)
        foreign = read_json(other / '.govel/operations' / (other_preview['operationId'] + '.json'))
        forged = {**deepcopy(foreign), 'operationId': preview['operationId'], 'planHash': preview['planHash']}
        write_json(self.root / '.govel/operations' / (preview['operationId'] + '.json'), forged)
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'OPERATION_RECEIPT_INVALID')
        self.assertEqual(load_structure(self.root), self.initial)

    def test_damaged_commit_witness_does_not_become_success_but_current_ownership_stays_readable(self):
        preview = self.preview()
        self.commit(preview)
        new_chapters = [row for row in load_structure(self.root)['chapters']
                        if row['chapterId'] in {item['chapterId'] for item in preview['impact']['chapterReshape']}]
        terminal = next(path for path in (self.root / '.govel/transactions').glob('TX-*/completed.json')
                        if read_json(path).get('operation') == 'chapter_split')
        (terminal.parent / 'after/.govel/structure.json').write_bytes(b'corrupt after image')
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'OPERATION_RECEIPT_INVALID')
        current = load_structure(self.root)
        for chapter in new_chapters:
            self.assertEqual(observe_chapter(self.root, current, chapter)['state'], OWNED_CLEAN)

    def test_valid_receipt_remains_historical_after_later_work(self):
        preview = self.preview()
        self.commit(preview)
        next_preview = self.preview(requestId='materialization-2', chapterId=self.fixture['chapterTwo'])
        self.commit(next_preview)
        current = load_structure(self.root)
        receipt = self.commit(preview)
        self.assertTrue(receipt['alreadyCommitted'])
        self.assertTrue(receipt['historical'])
        self.assertEqual(load_structure(self.root), current)
        self.assertEqual(require_committed_operation(self.root, preview['operationId'],
                         plan_hash=preview['planHash'])['afterRevision'], self.initial['revision'] + 1)

    def test_new_identity_receipt_does_not_offer_impossible_undo(self):
        preview = self.preview()
        receipt = self.commit(preview)
        self.assertFalse(receipt['reversible'])
        self.assertEqual(receipt['recovery']['mode'], 'preserve-identities')
        self.assertEqual(set(receipt['recovery']['newChapterIds']),
                         {row['chapterId'] for row in preview['impact']['chapterReshape']})
        current = load_structure(self.root)
        with self.assertRaises(StructureError) as caught:
            undo_operation(self.root, preview['operationId'])
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_REVERSIBLE')
        self.assertEqual(load_structure(self.root), current)

    def test_unreadable_new_directory_cannot_hide_manuscript_membership_changes(self):
        from scripts.core.write_sets import tree_membership, validate_tree_guards
        manuscripts = self.base / 'manuscripts'
        hidden = manuscripts / 'hidden'
        hidden.mkdir(parents=True)
        (manuscripts / 'first.txt').write_text('第一份原稿。', encoding='utf-8')
        guard = tree_membership(self.base, ['manuscripts'], suffixes=['.txt', '.md'])
        (hidden / 'later.txt').write_text('扫描之后新增的原稿。', encoding='utf-8')
        scandir = os.scandir
        def deny_new_directory(path):
            if Path(path) == hidden:
                raise PermissionError(13, 'permission denied', str(hidden))
            return scandir(path)
        with patch('scripts.core.write_sets.os.scandir', side_effect=deny_new_directory):
            with self.assertRaises(StructureError) as caught:
                validate_tree_guards(self.base, [guard], stage='before-committed')
        self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')


if __name__ == '__main__':
    unittest.main()
