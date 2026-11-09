"""Current per-chapter continuity confirmations and exact summary handoffs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.continuity import (ContinuityError, complete_continuity, continuity_preview,
                                     pending_continuity, require_predecessors_ready)
from scripts.core.finalization import finalize_chapter, reconcile_workflow
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import load_structure
from tests.fixtures_current import create_schema5_project
from tests.fixtures_workflow import review_ready_project


def confirmation_for(preview: dict) -> dict:
    return {'decisions': [], 'summary': {'resolution': 'not_required', 'reason': '本合成样本不需要长卷摘要'}}


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = review_ready_project(self.root)
        self.chapter_id, self.next_id = fixture['chapterIds']
        receipt = chapter_receipt(self.root, self.chapter_id, skip_title=True)
        self.finalized = finalize_chapter(self.root, self.chapter_id, skip_title=True,
                                          binding_generation=receipt['bindingGeneration'])

    def complete(self, preview=None, confirmation=None, *, approved=True):
        preview = preview or continuity_preview(self.root, self.chapter_id)
        return complete_continuity(self.root, self.chapter_id, batch_hash=preview['batchHash'],
            confirmation=confirmation or confirmation_for(preview), approved=approved)

    def test_finalize_is_committed_but_continuity_is_pending(self):
        self.assertEqual(self.finalized['status'], 'stable')
        self.assertFalse(self.finalized['readyForContinuation'])
        receipt = chapter_receipt(self.root, self.chapter_id)
        self.assertEqual(receipt['continuity']['status'], 'pending')
        action = reconcile_workflow(self.root)['nextAction']
        self.assertEqual(action['action'], 'continuity')
        self.assertEqual(action['chapterId'], self.chapter_id)

    def test_next_chapter_is_blocked_until_confirmation(self):
        with self.assertRaises(ContinuityError) as caught:
            advance_chapter_status(self.root, self.next_id, 'planned')
        self.assertEqual(caught.exception.code, 'CONTINUITY_REQUIRED')
        result = self.complete()
        self.assertEqual(result['nextAction']['chapterId'], self.next_id)
        self.assertEqual(result['nextAction']['action'], 'planner')
        self.assertTrue(chapter_receipt(self.root, self.chapter_id)['readyForContinuation'])

    def test_completion_requires_explicit_approval_in_auto_mode(self):
        path = self.root/'.gnovel/project.json'; marker = read_json(path)
        marker['approvalMode'] = 'auto'; write_json(path, marker)
        with self.assertRaises(ContinuityError) as caught: self.complete(approved=False)
        self.assertEqual(caught.exception.code, 'CONTINUITY_APPROVAL_REQUIRED')

    def test_summary_refresh_is_hash_bound_and_never_edits_the_summary(self):
        prepare = call_operation('summary.prepare', self.root, {})
        saved = call_operation('summary.commit-delta', self.root, {'prepareRef': prepare['prepareRef'],
            'storySummary': '值班员将钥匙放回桌上，完成交班登记。',
            'plotLine': [{'op': 'add', 'chapterId': self.chapter_id, 'summary': '钥匙归还，登记完成。'}]})
        preview = continuity_preview(self.root, self.chapter_id)
        confirmation = {'decisions': [], 'summary': {'resolution': 'refreshed', 'reason': '已按本章正式正文重述交班结果',
                                                      'sha256': saved['sha256']}}
        path = self.root/saved['path']; before = path.read_bytes()
        self.complete(preview, confirmation)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(chapter_receipt(self.root, self.chapter_id)['readyForContinuation'])

    def test_changed_source_or_prose_does_not_confirm(self):
        preview = continuity_preview(self.root, self.chapter_id)
        source = load_structure(self.root)
        path = self.root/source['chapters'][0]['filePath']
        path.write_text(path.read_text()+'外部变动。')
        self.assertFalse(continuity_preview(self.root, self.chapter_id)['ok'])
        with self.assertRaises(ContinuityError) as caught: self.complete(preview)
        self.assertEqual(caught.exception.code, 'CONTINUITY_BLOCKED')

    def test_unknown_decision_and_empty_summary_reason_are_rejected(self):
        preview = continuity_preview(self.root, self.chapter_id)
        confirmation = confirmation_for(preview)
        confirmation['decisions'] = [{'itemId': 'not-in-batch', 'resolution': 'no_change', 'reason': '不属本章'}]
        with self.assertRaises(ContinuityError) as caught: self.complete(preview, confirmation)
        self.assertEqual(caught.exception.code, 'CONTINUITY_DECISIONS_INCOMPLETE')
        confirmation = confirmation_for(preview); confirmation['summary']['reason'] = ''
        with self.assertRaises(ContinuityError) as caught: self.complete(preview, confirmation)
        self.assertEqual(caught.exception.code, 'CONTINUITY_SUMMARY_REQUIRED')

    def test_completion_is_idempotent_and_cannot_be_overwritten(self):
        preview = continuity_preview(self.root, self.chapter_id); confirmation = confirmation_for(preview)
        self.complete(preview, confirmation)
        self.assertEqual(self.complete(preview, confirmation)['code'], 'CONTINUITY_IDEMPOTENT')
        changed = deepcopy(confirmation); changed['summary']['reason'] = '另一个理由'
        with self.assertRaises(ContinuityError): self.complete(preview, changed)

    def test_failure_restores_receipt(self):
        path = self.root/'.gnovel/workflow.json'; before = path.read_bytes()
        def fail(phase, relative=None):
            if phase == 'write' and relative == '.gnovel/workflow.json': raise RuntimeError('injected')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(RuntimeError): self.complete()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list((self.root/'.gnovel/transactions').glob('*.json')))

    def test_preview_is_read_only(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        continuity_preview(self.root, self.chapter_id)
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_zero_changes_still_record_summary_disposition(self):
        preview = continuity_preview(self.root, self.chapter_id)
        self.assertEqual(preview['batch']['items'], [])
        result = self.complete(preview)
        self.assertEqual(result['summaryDisposition']['resolution'], 'not_required')
        self.assertTrue(result['summaryFreshness']['stale'])


class MissingContinuityTests(unittest.TestCase):
    def test_missing_receipts_are_not_backfilled_or_bundled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); fixture = create_schema5_project(root, chapter_count=3)
            source = load_structure(root); workflow = read_json(root/'.gnovel/workflow.json')
            before = (root/'.gnovel/workflow.json').read_bytes()
            pending = pending_continuity(source, workflow)
            self.assertEqual([r['chapterId'] for r in pending], fixture['chapterIds'])
            self.assertTrue(all(r['status'] == 'receipt_missing' for r in pending))
            for cid in fixture['chapterIds']:
                with self.assertRaises(ContinuityError) as caught: continuity_preview(root, cid)
                self.assertEqual(caught.exception.code, 'CONTINUITY_RECEIPT_MISSING')
            with self.assertRaises(ContinuityError):
                require_predecessors_ready(source, workflow, {'chapterId': 'CH-NEXT', 'displayOrdinal': 4})
            self.assertEqual((root/'.gnovel/workflow.json').read_bytes(), before)
