"""Evidence references eliminate machine-field transcription without implicit approval."""
from __future__ import annotations
from scripts.core.chapter_receipt import chapter_receipt

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.evidence_pack import pack_evidence
from scripts.core.operations import call_operation
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_review import _receipts, submit_review
from scripts.core.structure_store import StructureError
from scripts.core.transport_cache import get, _directory
from scripts.core.query import _size
from tests.fixtures_source import source_fixture


class EvidenceReferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        base = self.source['fragments'][0]
        self.rows = [{**deepcopy(base), 'fragmentId': f'fact-EFF{i:03d}', 'text': f'第{i}项明确的测试事实。'} for i in range(40)]
        self.source['fragments'] = self.rows
        write_json(self.root / '.govel/structure.json', self.source)

    def prepared(self, count=40):
        return call_operation('evidence.prepare', self.root, {'kind': 'review',
            'recordIds': [row['fragmentId'] for row in self.rows[:count]]})

    def decisions(self, count=40):
        return [{'recordId': row['fragmentId'], 'decision': 'compatible', 'reason': '逐项核对所选原文。',
                 'quotes': [{'text': row['text']}]} for row in self.rows[:count]]

    def packed(self, count=40, request='batch', **extra):
        return call_operation('evidence.pack', self.root, {'snapshotRef': self.prepared(count)['snapshotRef'],
            'decisions': self.decisions(count), 'requestId': request, **extra})

    def test_model_references_are_small_and_pack_is_readonly(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        prepared = self.prepared(20)
        self.assertNotIn('snapshot', prepared)
        self.assertLess(len(prepared['snapshotRef']), 80)
        result = self.packed(20)
        self.assertNotIn('arguments', result)
        self.assertLess(len(result['submissionRef']), 80)
        from scripts.core.project import TOOLBOX_ROOT
        budgets = read_json(TOOLBOX_ROOT / 'tests/fixtures/model_efficiency_budgets.json')
        self.assertLessEqual(_size({'snapshotRef': prepared['snapshotRef'], 'decisions': self.decisions(20),
                                   'requestId': 'batch'}), budgets['evidenceReviewRequestBytes'])
        self.assertLessEqual(_size(result), budgets['evidencePackResponseBytes'])
        self.assertFalse(result['submitted']); self.assertFalse(result['approved'])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        saved = get(self.root, result['submissionRef'], kind='evidence-submission')
        self.assertEqual(len(saved['arguments']['reviews']), 20)
        committed = call_operation('evidence.submit', self.root, {'submissionRef': result['submissionRef']})
        self.assertTrue(committed['submitted']); self.assertEqual(committed['recordedCount'], 20)
        self.assertFalse(committed['approved'])

    def test_large_batch_splits_by_actual_bytes_and_recovers_without_replaying(self):
        result = self.packed(maxBytes=2500)
        self.assertGreater(result['batchCount'], 1)
        bound = get(self.root, result['submissionRef'], kind='evidence-submission')
        for batch in bound['batches']:
            self.assertLessEqual(_size(batch['arguments']), 2500)
        calls = []
        def fail_second(root, **arguments):
            calls.append(arguments['requestId'])
            if len(calls) == 2:
                raise OSError('injected second batch failure')
            return submit_review(root, **arguments)
        with patch('scripts.core.source_review.submit_review', side_effect=fail_second):
            partial = call_operation('evidence.submit', self.root, {'submissionRef': result['submissionRef']})
        self.assertFalse(partial['ok']); self.assertEqual(partial['completedBatches'], 1)
        first = calls[0]
        (_directory() / (result['submissionRef'].replace(':', '-') + '.json')).unlink()
        def record(root, **arguments):
            self.assertNotEqual(arguments['requestId'], first)
            return submit_review(root, **arguments)
        with patch('scripts.core.source_review.submit_review', side_effect=record):
            resumed = call_operation('evidence.submit', self.root, {'requestId': 'batch'})
        self.assertTrue(resumed['submitted']); self.assertEqual(resumed['recordedCount'], 40)
        with patch('scripts.core.source_review.submit_review', side_effect=AssertionError('replayed write')):
            again = call_operation('evidence.submit', self.root, {'requestId': 'batch'})
        self.assertTrue(again['submitted'])
        self.assertEqual(len(_receipts(self.root)['receipts']), result['batchCount'])
        cursor = None; rows = []
        while True:
            page = call_operation('evidence.status', self.root, {'requestId': 'batch', 'maxBytes': 3000,
                **({'cursor': cursor} if cursor else {})})
            rows.extend(page['items'])
            if not page['hasMore']: break
            cursor = page['nextCursor']
        self.assertEqual({row['itemId'] for row in rows}, {row['fragmentId'] for row in self.rows})
        self.assertTrue(all(row['status'] == 'recorded' for row in rows))

    def test_changed_inputs_block_pending_work_but_completed_history_survives(self):
        result = self.packed(count=2, maxBytes=800)
        original = submit_review
        def first_then_change(root, **arguments):
            answer = original(root, **arguments)
            source = read_json(root / '.govel/structure.json')
            source['fragments'][1]['text'] = '准备之后改动。'
            write_json(root / '.govel/structure.json', source)
            return answer
        with patch('scripts.core.source_review.submit_review', side_effect=first_then_change):
            partial = call_operation('evidence.submit', self.root, {'submissionRef': result['submissionRef']})
        self.assertFalse(partial['submitted'])
        self.assertEqual(partial['recordedCount'], 1)
        self.assertEqual(partial['error']['code'], 'EVIDENCE_SNAPSHOT_STALE')
        again = call_operation('evidence.submit', self.root, {'requestId': 'batch'})
        self.assertEqual(again['recordedCount'], 1)
        self.assertFalse(again['ok'])

    def test_single_long_item_and_duplicate_or_changed_requests_are_not_truncated(self):
        prepared = self.prepared(1)
        decisions = self.decisions(1); decisions[0]['reason'] = '完整理由' * 6000
        result = call_operation('evidence.pack', self.root, {'snapshotRef': prepared['snapshotRef'],
            'decisions': decisions, 'requestId': 'long'})
        self.assertEqual(result['oversizedItemsByReference'], 1)
        submitted = call_operation('evidence.submit', self.root, {'submissionRef': result['submissionRef']})
        self.assertTrue(submitted['submitted'])
        receipt = next(iter(_receipts(self.root)['receipts'].values()))
        self.assertEqual(receipt['reviews'][0]['reason'], decisions[0]['reason'])
        changed = self.packed(1, request='long')
        with self.assertRaises(StructureError) as caught:
            call_operation('evidence.submit', self.root, {'submissionRef': changed['submissionRef']})
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')
        with self.assertRaises(StructureError):
            call_operation('evidence.pack', self.root, {'snapshotRef': prepared['snapshotRef'],
                'decisions': self.decisions(1) * 2, 'requestId': 'duplicate'})

    def test_late_source_change_rolls_back_only_the_current_batch(self):
        result = self.packed(count=2, maxBytes=800)
        writes = 0
        def change_during_write(phase, relative=None):
            nonlocal writes
            if phase == 'write' and relative == '.govel/source-review-receipts.json':
                writes += 1
                if writes == 3:
                    source = read_json(self.root / '.govel/structure.json')
                    source['fragments'][1]['text'] = '提交窗口内的外部修改。'
                    write_json(self.root / '.govel/structure.json', source)
        with patch('scripts.core.write_sets.checkpoint', side_effect=change_during_write):
            partial = call_operation('evidence.submit', self.root, {'submissionRef': result['submissionRef']})
        self.assertFalse(partial['ok']); self.assertEqual(partial['recordedCount'], 1)
        self.assertEqual(len(_receipts(self.root)['receipts']), 1)
        self.assertEqual(read_json(self.root / '.govel/structure.json')['fragments'][1]['text'], '提交窗口内的外部修改。')

    def test_invalid_or_unrenderable_submit_budget_has_no_writes(self):
        packed = self.packed(count=1)
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass  # Stable infrastructure remains part of the byte comparison.
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        for budget in (0, -1, True, 1):
            with self.subTest(budget=budget), self.assertRaises(StructureError):
                call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef'], 'maxBytes': budget})
            self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes()
                                     for path in self.root.rglob('*') if path.is_file()})
        with self.assertRaises(StructureError):
            self.packed(count=1, view='full', maxBytes=0)
        full_small = self.packed(count=1, view='full', maxBytes=1)
        self.assertIn('submissionRef', full_small)
        self.assertNotIn('arguments', full_small)
        self.assertEqual(full_small['oversizedItemsByReference'], 1)

    def test_long_failure_returns_progress_and_full_error_reference_within_budget(self):
        packed = self.packed(count=2, maxBytes=800)
        calls = 0
        message = 'failure while saving second batch: ' + 'x' * 3000
        def fail_second(root, **arguments):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(message)
            return submit_review(root, **arguments)
        with patch('scripts.core.source_review.submit_review', side_effect=fail_second):
            result = call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef'], 'maxBytes': 1500})
        self.assertFalse(result['ok']); self.assertEqual(result['recordedCount'], 1)
        self.assertEqual(result['pendingCount'], 1)
        self.assertLessEqual(_size(result), 1500)
        self.assertEqual(get(self.root, result['errorRef'], kind='evidence-error')['message'], message)
        self.assertEqual(result['detailsOperation'], 'evidence.status')
        self.assertTrue(call_operation('evidence.submit', self.root, {'requestId': 'batch'})['submitted'])

    def test_cli_mcp_declarations_keep_pack_readonly_and_submit_explicit(self):
        from scripts.core.operations import operations
        from scripts.mcp_server import handle
        specs = {spec.id: spec for spec in operations()}
        self.assertTrue(specs['evidence.pack'].tool()['annotations']['readOnlyHint'])
        self.assertFalse(specs['evidence.submit'].tool()['annotations']['readOnlyHint'])
        packed = self.packed(count=1)
        result = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'submit_packed_evidence', 'arguments': {'projectRoot': str(self.root),
                'submissionRef': packed['submissionRef']}}})['result']
        self.assertFalse(result['isError']); self.assertTrue(result['structuredContent']['submitted'])

    def test_continuity_cannot_be_submitted_without_the_original_approval_boundary(self):
        from tests.fixtures_workflow import review_ready_project
        from scripts.core.finalization import finalize_chapter
        from scripts.core.continuity import continuity_preview
        from tests.test_continuity_receipts import confirmation_for
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            finalize_chapter(root, chapter, skip_title=True, binding_generation=chapter_receipt(root, chapter)['bindingGeneration'])
            prepared = call_operation('evidence.prepare', root, {'kind': 'continuity', 'chapterId': chapter})
            decisions = confirmation_for(continuity_preview(root, chapter))
            packed = call_operation('evidence.pack', root, {'snapshotRef': prepared['snapshotRef'], 'decisions': decisions})
            before = (root / '.govel/workflow.json').read_bytes()
            with self.assertRaises(StructureError):
                call_operation('evidence.submit', root, {'submissionRef': packed['submissionRef']})
            with self.assertRaises(StructureError):
                call_operation('evidence.confirm-continuity', root, {'submissionRef': packed['submissionRef'], 'approve': False})
            self.assertEqual(before, (root / '.govel/workflow.json').read_bytes())
            result = call_operation('evidence.confirm-continuity', root, {'submissionRef': packed['submissionRef'], 'approve': True})
            self.assertTrue(result['readyForContinuation'])

    def test_prose_duplicates_cannot_hide_across_batches(self):
        from tests.test_source_evidence import ProseEvidenceTests
        fixture = ProseEvidenceTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        prepared = call_operation('evidence.prepare', fixture.root, {'kind': 'prose', 'chapterId': 'chapter-ALPHA'})
        text = prepared['materials'][0]['text']
        decisions = [{'quote': {'text': text}, 'targetIds': ['attribute-HEIGHT'], 'kind': 'depends_on', 'reason': '明确引用'}]
        with self.assertRaises(StructureError):
            call_operation('evidence.pack', fixture.root, {'snapshotRef': prepared['snapshotRef'],
                'decisions': decisions * 2, 'requestId': 'duplicate-prose', 'maxBytes': 800})
        packed = call_operation('evidence.pack', fixture.root, {'snapshotRef': prepared['snapshotRef'],
            'decisions': decisions, 'requestId': 'prose'})
        self.assertTrue(call_operation('evidence.submit', fixture.root, {'submissionRef': packed['submissionRef']})['submitted'])

    def test_completed_receipts_remain_queryable_after_source_changes(self):
        result = self.packed(count=1)
        self.assertTrue(call_operation('evidence.submit', self.root, {'submissionRef': result['submissionRef']})['submitted'])
        source = read_json(self.root / '.govel/structure.json')
        source['fragments'][0]['text'] = '随后发生的明确修改。'
        write_json(self.root / '.govel/structure.json', source)
        with patch('scripts.core.source_review.submit_review', side_effect=AssertionError('must not replay history')):
            history = call_operation('evidence.submit', self.root, {'requestId': 'batch'})
        self.assertTrue(history['submitted']); self.assertTrue(history['historicalReceipt'])
        self.assertFalse(history['semanticReviewComplete'])

    def test_partial_selection_keeps_each_unprocessed_identity(self):
        prepared = self.prepared(2)
        packed = call_operation('evidence.pack', self.root, {'snapshotRef': prepared['snapshotRef'],
            'decisions': self.decisions(1), 'requestId': 'partial'})
        self.assertFalse(packed['selectionComplete'])
        result = call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef']})
        self.assertEqual(result['recordedCount'], 1); self.assertEqual(result['notSelectedCount'], 1)
        self.assertEqual({row['itemId']: row['status'] for row in result['items']},
                         {'fact-EFF000': 'recorded', 'fact-EFF001': 'not_selected'})

    def test_snapshot_reference_is_project_bound_and_explicit_snapshot_remains_callable(self):
        prepared = self.prepared(1)
        with self.assertRaises(StructureError):
            call_operation('evidence.pack', self.root / 'other', {'snapshotRef': prepared['snapshotRef'],
                'decisions': self.decisions(1), 'requestId': 'other'})
        snapshot = get(self.root, prepared['snapshotRef'], kind='evidence-snapshot')
        packed = pack_evidence(self.root, snapshot=snapshot, decisions=self.decisions(1), requestId='explicit-snapshot')
        self.assertIn('arguments', packed)
        self.assertTrue(call_operation(packed['operation'], self.root, packed['arguments'])['ok'])


if __name__ == '__main__':
    unittest.main()
