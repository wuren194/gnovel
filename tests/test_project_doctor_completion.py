"""Finished chapters keep historical approvals while current closeout stays checked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.chapter_status import chapter_status
from scripts.core.continuity import complete_continuity, continuity_preview
from scripts.core.finalization import finalize_chapter
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project_doctor import assess_project
from scripts.core.structure_store import load_structure
from tests.fixtures_workflow import register_and_approve
from tests.test_finalization_preflight import ready_project


class DoctorCompletionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid, self.artifacts = ready_project(self.root)
        # These independent toolbox-wide probes are outside the chapter-scope regression.
        for domain in ('_tooling_domain', '_knowledge_domain'):
            mock = patch('scripts.core.project_doctor.' + domain, return_value=({'applicable': False}, []))
            mock.start(); self.addCleanup(mock.stop)

    def finalize(self, *, confirm=True):
        receipt = chapter_receipt(self.root, self.cid, title_candidate_id='title-1')
        result = finalize_chapter(self.root, self.cid, title_candidate_id='title-1',
                                  binding_generation=receipt['bindingGeneration'])
        self.assertTrue(result['ok'], result)
        if confirm:
            preview = continuity_preview(self.root, self.cid)
            complete_continuity(self.root, self.cid, batch_hash=preview['batchHash'], approved=True,
                confirmation={'decisions': [], 'summary': {'resolution': 'not_required',
                              'reason': '合成单章回归直接使用完整正文核对交接。'}})

    def assess(self, **kwargs):
        return assess_project(self.root, **kwargs)

    def codes(self, report):
        return {row['code'] for row in report['issues'] if row['severity'] == 'blocking'}

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_completed_chapter_does_not_become_default_writer_task(self):
        self.finalize()
        self.assertIsNone(read_json(self.root / '.govel/workflow.json')['nextAction'])
        receipt = chapter_receipt(self.root, self.cid)
        self.assertTrue(receipt['readyForContinuation'])
        before = self.inventory()
        for strict in (False, True):
            with patch('scripts.core.task_prepare.prepare_task', side_effect=AssertionError('no Writer task')):
                report = self.assess(strict=strict)
            self.assertTrue(report['healthy'], report['issues'])
            self.assertIsNone(report['scopedChapterId'])
            for domain in ('writerReadiness', 'context'):
                self.assertEqual(report['domains'][domain]['status'], 'not_applicable')
                self.assertFalse(report['domains'][domain]['applicable'])
            self.assertIsNone(report['domains']['writerReadiness']['taskReady'])
        self.assertEqual(before, self.inventory())
        board = chapter_status(self.root, self.cid)
        self.assertEqual(board['continuity']['status'], 'complete')
        self.assertTrue(board['readyForContinuation'])
        self.assertEqual(board['nextActions'], [])
        self.assertEqual(board['evidencePurpose'], 'historical publication evidence; rerun before a new revision')
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['nextAction'] = {'chapterId': self.cid, 'action': 'continuity',
                                  'reason': '残留的确认建议', 'needsApproval': True}
        write_json(self.root / '.govel/workflow.json', workflow)
        before = self.inventory()
        self.assertIsNone(chapter_status(self.root, self.cid)['nextAction'])
        self.assertIsNone(self.assess()['scopedChapterId'])
        self.assertEqual(before, self.inventory())

    def test_explicit_completed_chapter_still_requires_current_stage_a(self):
        from scripts.core.task_prepare import prepare_task
        self.finalize()
        before = self.inventory()
        report = self.assess(chapter_id=self.cid)
        prep = prepare_task(self.root, 'writer', chapter_id=self.cid)
        self.assertEqual(report['scopedChapterId'], self.cid)
        self.assertTrue(report['domains']['writerReadiness']['applicable'])
        self.assertEqual(report['domains']['writerReadiness']['blocking'], [row['code'] for row in prep['blocking']])
        self.assertIn('STAGE_A_OUTLINE_MISMATCH', self.codes(report))
        self.assertEqual(before, self.inventory())

    def test_pending_continuity_is_workflow_work_and_not_a_writer_failure(self):
        self.finalize(confirm=False)
        report = self.assess()
        self.assertIsNone(report['scopedChapterId'])
        self.assertEqual(report['domains']['writerReadiness']['status'], 'not_applicable')
        self.assertIn('CONTINUITY_REQUIRED', self.codes(report))
        self.assertNotIn('STAGE_A_OUTLINE_MISMATCH', self.codes(report))
        board = chapter_status(self.root, self.cid)
        self.assertTrue(any(r['operation'] == 'project.workflow.continuity-preview' for r in board['nextActions']))

    def test_modified_completed_prose_and_invalid_continuity_remain_blocking(self):
        self.finalize()
        prose = self.root / load_structure(self.root)['chapters'][0]['filePath']
        prose.write_text(prose.read_text() + '外部追加。')
        report = self.assess()
        self.assertIn('PROSE_EXTERNAL_MODIFICATION', self.codes(report))
        self.assertEqual(report['domains']['writerReadiness']['status'], 'not_applicable')
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['continuity'][self.cid]['confirmationHash'] = 'corrupt'
        write_json(self.root / '.govel/workflow.json', workflow)
        self.assertIn('CONTINUITY_STATE_INVALID', self.codes(self.assess()))

    def test_known_fact_handoff_remains_pending_after_confirmation_and_clears_with_evidence(self):
        payload = self.artifacts['stage_b_review']['payload']
        payload['factAssessment'] = {'status': 'changes'}
        payload['findings'] = [{'findingId': 'FACT-KEY', 'severity': 'info', 'ruleId': 'G-CAUSAL',
            'summary': '钥匙归还桌面需要登记',
            'factChange': {'status': 'pending', 'requestId': 'doctor-key-fact',
                           'reason': '下一章需要知道钥匙的去处', 'evidence': [{'text': '他把钥匙放回桌上'}]}}]
        register_and_approve(self.root, self.cid, 'stage_b_review', payload)
        self.finalize()
        report = self.assess()
        self.assertIn('CHAPTER_FACTS_PENDING', self.codes(report))
        self.assertNotIn('STAGE_A_OUTLINE_MISMATCH', self.codes(report))
        board = chapter_status(self.root, self.cid)
        self.assertEqual(board['continuity']['status'], 'complete')
        self.assertFalse(board['readyForContinuation'])
        self.assertFalse(any(r['operation'] == 'project.workflow.continuity-preview' for r in board['nextActions']))
        self.assertTrue(any(r['operation'] == 'project.workflow.chapter-receipt' for r in board['nextActions']))
        preview = call_operation('source.chapter-facts-preview', self.root, {
            'chapterId': self.cid, 'requestId': 'doctor-key-fact', 'facts': [{
                'findingId': 'FACT-KEY', 'ownerId': self.cid, 'field': 'plot.plan',
                'statement': '钥匙已放回桌上。', 'quote': {'text': '他把钥匙放回桌上'}, 'reason': '正文明确写出放回动作'}]})
        call_operation('source.commit', self.root, preview['nextAction']['arguments'])
        call_operation('source.chapter-facts-complete', self.root, {'requestId': 'doctor-key-fact', 'registerEvidence': True})
        self.assertTrue(self.assess()['healthy'], self.assess()['issues'])
        self.assertTrue(chapter_receipt(self.root, self.cid)['readyForContinuation'])
        workflow = read_json(self.root / '.govel/workflow.json')
        self.assertFalse(workflow['approvals'][self.cid]['approved'])
        self.assertTrue(workflow['approvals'][self.cid]['finalized'])

    def test_unfinished_chapter_is_selected_after_completed_one(self):
        from scripts.core.structure_commands import preview_insert_chapter, commit_plan
        self.finalize()
        source = load_structure(self.root)
        plan = preview_insert_chapter(self.root, title='续章', volume_id=source['volumes'][0]['volumeId'],
                                      primary_arc_id=source['arcs'][0]['arcId'])
        second = commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)['chapter']['chapterId']
        report = self.assess()
        self.assertEqual(report['scopedChapterId'], second)
        self.assertIn('STAGE_A_NOT_APPROVED', self.codes(report))
        self.assertIn('SOURCE_CHAPTER_PLAN_MISSING', self.codes(report))

    def test_stale_chapter_still_runs_writer_gate(self):
        self.finalize()
        source = load_structure(self.root)
        source['chapters'][0]['status'] = 'stale'
        write_json(self.root / '.govel/structure.json', source)
        report = self.assess()
        self.assertEqual(report['scopedChapterId'], self.cid)
        self.assertTrue(report['domains']['writerReadiness']['applicable'])
        self.assertIn('STAGE_A_OUTLINE_MISMATCH', self.codes(report))
