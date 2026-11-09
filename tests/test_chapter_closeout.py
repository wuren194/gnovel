"""Closeout preserves inspected inputs, explicit approval and atomic retry proof."""
from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.chapter_closeout import closeout_chapter, prepare_closeout, _load
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.summary_updates import SUMMARY_PATH
from tests.fixtures_workflow import review_ready_project


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file() and path.name != 'structure.lock'}


class ChapterCloseoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = review_ready_project(self.root, chapter_count=2)
        self.cid = self.fixture['chapterIds'][0]

    def prepare(self):
        return prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)

    def run_args(self, **kwargs):
        return {'requestId': 'close-1', 'prepareRef': self.prepare()['prepareRef'],
                'prepareNext': False, **kwargs}

    def summary(self):
        return {'resolution': 'not_required', 'reason': '当前交接直接核对完整正式正文，暂不刷新摘要'}

    def update(self):
        return {'storySummary': '他把钥匙放回桌面，随后合上登记本。',
                'reason': '已按当前候选中的动作顺序复核因果',
                'plotLine': [{'op': 'add', 'chapterId': self.cid, 'summary': '归还钥匙后合上登记本。'}]}

    def test_prepare_is_read_only_and_keeps_exact_ownership_and_title(self):
        before = files(self.root)
        result = call_operation('workflow.closeout-prepare', self.root, {'chapterId': self.cid, 'skipTitle': True})
        self.assertTrue(result['ok'], result)
        self.assertEqual(files(self.root), before)
        action = result['receipt']['finalizeAction']['arguments']
        self.assertEqual(action['bindingGeneration'], load_structure(self.root)['chapters'][0]['proseBinding']['generation'])
        self.assertTrue(action['skipTitle'])
        self.assertTrue(result['factsAssessed'])

    def test_finalize_does_not_supply_continuity_approval_or_summary(self):
        result = closeout_chapter(self.root, **self.run_args())
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'CLOSEOUT_INPUT_REQUIRED')
        self.assertEqual(result['completedStages'], ['finalize'])
        self.assertFalse(result['approvalPerformed'])
        self.assertFalse(result['readyForContinuation'])
        self.assertIn('missingInputs', result)
        workflow = read_json(self.root/'.govel/workflow.json')
        self.assertEqual(workflow['continuity'][self.cid]['status'], 'pending')

    def test_explicit_summary_disposition_completes_and_prepares_next(self):
        args = self.run_args(summary=self.summary(), approveContinuity=True, prepareNext=True)
        result = call_operation('workflow.closeout', self.root, args)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['readyForContinuation'], result)
        self.assertEqual(result['completedStages'], ['finalize', 'continuity'])
        self.assertFalse(result['artifactApprovalPerformed'])
        self.assertTrue(result['approvalPerformed'])
        self.assertEqual(result['nextPrepareAction']['arguments']['task'], 'continue')
        if result.get('responseCompacted'):
            from scripts.core.transport_cache import get
            result = get(self.root, result['reportRef'], kind='authoring-report')
        self.assertIn('nextPreparation', result)
        self.assertTrue(result['factsAssessed'])

    def test_summary_content_and_review_reason_flow_into_confirmation(self):
        result = closeout_chapter(self.root, **self.run_args(summaryUpdate=self.update(), approveContinuity=True))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['completedStages'], ['finalize', 'summary', 'continuity'])
        summary = read_json(self.root/SUMMARY_PATH)
        self.assertEqual(summary['storySummary'], self.update()['storySummary'])
        self.assertEqual(summary['plotLine'][0]['chapterId'], self.cid)
        record = read_json(self.root/'.govel/workflow.json')['continuity'][self.cid]
        self.assertEqual(record['confirmation']['summary']['reason'], self.update()['reason'])
        self.assertEqual(record['confirmation']['summary']['sha256'], result['receipts']['summary']['summaryVersion'])

    def test_resume_after_finalization_failure_uses_original_receipt(self):
        args = self.run_args(summaryUpdate=self.update(), approveContinuity=True)
        with patch('scripts.core.summary_updates.commit_delta', side_effect=OSError('temporary summary fault')):
            result = closeout_chapter(self.root, **args)
        self.assertFalse(result['ok'])
        self.assertEqual(result['completedStages'], ['finalize'])
        generation = load_structure(self.root)['chapters'][0]['proseBinding']['generation']
        with patch('scripts.core.finalization._finalize_chapter_locked', side_effect=AssertionError('must not finalize twice')):
            result = closeout_chapter(self.root, requestId='close-1')
        self.assertTrue(result['readyForContinuation'], result)
        self.assertEqual(load_structure(self.root)['chapters'][0]['proseBinding']['generation'], generation)

    def test_resume_after_summary_commit_does_not_rewrite_summary(self):
        args = self.run_args(summaryUpdate=self.update(), approveContinuity=True)
        with patch('scripts.core.continuity.complete_continuity', side_effect=OSError('temporary confirm fault')):
            result = closeout_chapter(self.root, **args)
        self.assertEqual(result['completedStages'], ['finalize', 'summary'])
        raw = (self.root/SUMMARY_PATH).read_bytes()
        with patch('scripts.core.summary_updates.commit_delta', side_effect=AssertionError('summary already committed')):
            result = closeout_chapter(self.root, requestId='close-1')
        self.assertTrue(result['readyForContinuation'], result)
        self.assertEqual((self.root/SUMMARY_PATH).read_bytes(), raw)

    def test_resume_after_confirmation_commit_recovers_lost_response(self):
        from scripts.core.continuity import complete_continuity
        def lose_response(*args, **kwargs):
            complete_continuity(*args, **kwargs)
            raise OSError('response lost after commit')
        with patch('scripts.core.continuity.complete_continuity', side_effect=lose_response):
            result = closeout_chapter(self.root, **self.run_args(summary=self.summary(), approveContinuity=True))
        self.assertEqual(result['completedStages'], ['finalize', 'continuity'])
        before = files(self.root)
        result = closeout_chapter(self.root, requestId='close-1')
        self.assertTrue(result['readyForContinuation'], result)
        self.assertEqual(files(self.root), before)

    def test_failure_before_domain_commit_leaves_no_success_receipt(self):
        with patch('scripts.core.finalization._finalize_chapter_locked', side_effect=OSError('not committed')):
            result = closeout_chapter(self.root, **self.run_args())
        self.assertEqual(result['completedStages'], [])
        self.assertEqual(load_structure(self.root)['chapters'][0]['status'], 'review')
        result = closeout_chapter(self.root, requestId='close-1')
        self.assertEqual(result['completedStages'], ['finalize'])

    def test_stale_preparation_and_resume_never_adopt_external_work(self):
        args = self.run_args()
        path = self.root/'.govel/workflow.json'
        workflow = read_json(path); workflow['approvedChapterIds'] = []
        write_json(path, workflow)
        before = files(self.root)
        with self.assertRaises(StructureError) as caught:
            closeout_chapter(self.root, **args)
        self.assertEqual(caught.exception.code, 'CLOSEOUT_INPUTS_STALE')
        self.assertEqual(files(self.root), before)

    def test_changed_prose_after_partial_success_blocks_resume(self):
        closeout_chapter(self.root, **self.run_args())
        prose = self.root/load_structure(self.root)['chapters'][0]['filePath']
        prose.write_text('作者在外部修改了正文。')
        before = files(self.root)
        with self.assertRaises(StructureError) as caught:
            closeout_chapter(self.root, requestId='close-1')
        self.assertEqual(caught.exception.code, 'CLOSEOUT_INPUTS_STALE')
        self.assertEqual(files(self.root), before)

    def test_new_preparation_cannot_claim_modified_formal_prose_is_ready(self):
        closeout_chapter(self.root, **self.run_args(summary=self.summary(), approveContinuity=True))
        prose = self.root/load_structure(self.root)['chapters'][0]['filePath']
        prose.write_text('已在外部替换的正式正文。')
        before = files(self.root)
        prepared = self.prepare()
        self.assertFalse(prepared['ok'])
        self.assertIn('PROSE_EXTERNAL_MODIFICATION', {row['code'] for row in prepared['blockers']})
        with self.assertRaises(StructureError) as caught:
            closeout_chapter(self.root, requestId='new-closeout', prepareRef=prepared['prepareRef'], prepareNext=False)
        self.assertEqual(caught.exception.code, 'CLOSEOUT_BLOCKED')
        self.assertEqual(files(self.root), before)

    def test_request_cannot_gain_approval_or_replace_frozen_text_on_retry(self):
        args = self.run_args()
        closeout_chapter(self.root, **args)
        before = files(self.root)
        with self.assertRaises(StructureError) as caught:
            closeout_chapter(self.root, **{**args, 'summary': self.summary(), 'approveContinuity': True})
        self.assertEqual(caught.exception.code, 'CLOSEOUT_REQUEST_REUSED')
        self.assertEqual(files(self.root), before)
        self.assertFalse(_load(self.root, 'close-1')['arguments']['approveContinuity'])

    def test_invalid_or_missing_summary_is_rejected_before_finalization(self):
        for summary in (None, {'resolution': 'not_required', 'reason': '待填写'}):
            args = self.run_args(summary=summary, approveContinuity=True)
            before = files(self.root)
            with self.assertRaises(StructureError): closeout_chapter(self.root, **args)
            self.assertEqual(files(self.root), before)

    def test_missing_artifact_approval_is_not_granted_by_closeout(self):
        path = self.root/'.govel/workflow.json'
        workflow = read_json(path)
        del workflow['approvals'][self.cid]['artifactHashes']['state_candidate']
        write_json(path, workflow)
        prepared = self.prepare()
        self.assertFalse(prepared['ok'])
        before = files(self.root)
        with self.assertRaises(StructureError) as caught:
            closeout_chapter(self.root, requestId='blocked', prepareRef=prepared['prepareRef'])
        self.assertEqual(caught.exception.code, 'CLOSEOUT_BLOCKED')
        self.assertEqual(files(self.root), before)

    def test_new_history_that_appears_after_absence_check_is_never_overwritten(self):
        from scripts.core.summary_updates import inventory
        from scripts.core.write_sets import digest
        closeout_chapter(self.root, **self.run_args(summaryUpdate=self.update()))
        old = (self.root/SUMMARY_PATH).read_bytes()
        target = self.root/f'.govel/aux/story-summary-history/{digest(old)}.json'
        update = deepcopy(self.update()); update['plotLine'][0]['op'] = 'replace'
        args = self.run_args(requestId='close-2', summaryUpdate=update)
        def race(root, outputs, **kwargs):
            if target.relative_to(self.root).as_posix() in outputs and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'external history')
            return inventory(root, outputs, **kwargs)
        with patch('scripts.core.summary_updates.inventory', side_effect=race):
            result = closeout_chapter(self.root, **args)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error']['code'], 'WRITE_SET_CONFLICT')
        self.assertEqual(target.read_bytes(), b'external history')
        self.assertEqual((self.root/SUMMARY_PATH).read_bytes(), old)
        self.assertEqual(result['completedStages'], [])
        self.assertEqual(_load(self.root, 'close-2')['receipts'], {})

    def test_existing_history_changed_after_validation_blocks_summary_without_restoring_it(self):
        from scripts.core.summary_updates import inventory
        from scripts.core.write_sets import digest
        closeout_chapter(self.root, **self.run_args(summaryUpdate=self.update()))
        old = (self.root/SUMMARY_PATH).read_bytes()
        target = self.root/f'.govel/aux/story-summary-history/{digest(old)}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old)
        workflow_before = (self.root/'.govel/workflow.json').read_bytes()
        update = deepcopy(self.update()); update['plotLine'][0]['op'] = 'replace'
        args = self.run_args(requestId='close-2', summaryUpdate=update)

        def race(root, outputs, **kwargs):
            # persist_summary has already checked these existing archive bytes.
            # They are a read guard, never an output or a rollback target.
            self.assertNotIn(target.relative_to(self.root).as_posix(), outputs)
            target.write_bytes(b'external edit after history validation')
            return inventory(root, outputs, **kwargs)

        with patch('scripts.core.summary_updates.inventory', side_effect=race):
            result = closeout_chapter(self.root, **args)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error']['code'], 'TRANSACTION_GUARD_DRIFT')
        self.assertEqual(target.read_bytes(), b'external edit after history validation')
        self.assertEqual((self.root/SUMMARY_PATH).read_bytes(), old)
        self.assertEqual((self.root/'.govel/workflow.json').read_bytes(), workflow_before)
        self.assertEqual(result['completedStages'], [])
        self.assertEqual(_load(self.root, 'close-2')['receipts'], {})

    def test_approval_withdrawn_after_finalization_preflight_prevents_prose_commit(self):
        from scripts.core.chapter_closeout import receipt_outputs
        workflow_path = self.root/'.govel/workflow.json'
        source_before = (self.root/'.govel/structure.json').read_bytes()
        chapter = load_structure(self.root)['chapters'][0]
        self.assertFalse((self.root/chapter['filePath']).exists())
        args = self.run_args(summary=self.summary(), approveContinuity=True)

        def revoke(root, request_id, stage, outputs, evidence, **kwargs):
            self.assertEqual(stage, 'finalize')
            # The real finalization preflight has finished and built its output
            # set, but the domain write has not committed anything yet.
            workflow = read_json(workflow_path)
            del workflow['approvals'][self.cid]['artifactHashes']['candidate_prose']
            write_json(workflow_path, workflow)
            return receipt_outputs(root, request_id, stage, outputs, evidence, **kwargs)

        with patch('scripts.core.chapter_closeout.receipt_outputs', side_effect=revoke):
            result = closeout_chapter(self.root, **args)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['error']['code'], 'CLOSEOUT_INPUTS_STALE')
        self.assertEqual(result['completedStages'], [])
        self.assertEqual(_load(self.root, 'close-1')['receipts'], {})
        self.assertEqual((self.root/'.govel/structure.json').read_bytes(), source_before)
        self.assertFalse((self.root/chapter['filePath']).exists())
        self.assertNotIn('candidate_prose', read_json(workflow_path)['approvals'][self.cid]['artifactHashes'])

    def test_large_receipt_preserves_missing_inputs_and_continuation_actions(self):
        summary = self.summary(); summary['reason'] *= 5000
        result = closeout_chapter(self.root, **self.run_args(summary=summary, approveContinuity=True))
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['responseCompacted'])
        self.assertTrue(result['readyForContinuation'])
        self.assertIn('nextPrepareAction', result)
        self.assertTrue(result['factsAssessed'])

    def test_end_of_authorized_range_is_completed_not_a_retry_failure(self):
        workflow_path = self.root/'.govel/workflow.json'
        workflow = read_json(workflow_path); workflow['approvedChapterIds'] = [self.cid]
        write_json(workflow_path, workflow)
        result = closeout_chapter(self.root, **self.run_args(summary=self.summary(), approveContinuity=True,
                                                           prepareNext=True))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'CLOSEOUT_COMPLETED')
        self.assertTrue(result['readyForContinuation'])
        self.assertTrue(result['waitingForAuthorization'])
        self.assertNotIn('nextPrepareAction', result)
        before = files(self.root)
        resumed = closeout_chapter(self.root, requestId='close-1')
        self.assertEqual(resumed['code'], 'CLOSEOUT_COMPLETED')
        self.assertEqual(files(self.root), before)

    def test_optional_next_preparation_failure_keeps_completed_closeout(self):
        with patch('scripts.core.task_prepare.prepare_task', side_effect=OSError('read failed')):
            result = closeout_chapter(self.root, **self.run_args(summary=self.summary(), approveContinuity=True,
                                                               prepareNext=True))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'CLOSEOUT_COMPLETED')
        self.assertEqual(result['nextPreparationError'], 'NEXT_PREPARATION_FAILED')
        self.assertEqual(result['nextPrepareAction']['arguments']['task'], 'continue')


if __name__ == '__main__':
    unittest.main()
