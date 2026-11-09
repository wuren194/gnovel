"""Fact handoff reports expose the full reviewed quote set without approving it."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.core.structure_store import load_structure
from scripts.core.evidence_quotes import quote_span
from tests.fixtures_workflow import review_ready_project, run_and_approve_diagnostics
from tests import test_behavior_inventory as behavior_fixture


class FactInventoryEvidenceTests(unittest.TestCase):
    op = behavior_fixture.BehaviorInventoryTests.op
    full = behavior_fixture.BehaviorInventoryTests.full
    payload = behavior_fixture.BehaviorInventoryTests.payload
    record = behavior_fixture.BehaviorInventoryTests.record
    closeout = behavior_fixture.BehaviorInventoryTests.closeout

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root)['chapterIds'][0]
        self.body = '他把钥匙放回桌上，合上了登记本。'
        payload = self.payload()
        payload['factAssessment'] = {'status': 'changes'}
        self.quotes = [{'text': '他把钥匙放回桌上'}, {'text': '合上了登记本'}]
        payload['findings'] = [{'findingId': 'F-HANDOVER', 'severity': 'info',
            'ruleId': 'CHAPTER-PIPELINE', 'summary': '两项交接动作均需保留。',
            'factChange': {'status': 'pending', 'requestId': 'handover',
                           'reason': '两项动作共同说明交接已结束。', 'evidence': self.quotes}}]
        self.record(payload)
        run_and_approve_diagnostics(self.root, self.cid)
        state = self.op('workflow.state-prepare', chapterId=self.cid, requestId='state-handover')
        self.op('project.workflow.artifact-approve', chapterId=self.cid, artifact=['@' + state['artifactPath']])
        result = self.closeout()
        self.assertFalse(result['readyForContinuation'])
        self.field = next(row['fieldId'] for row in load_structure(self.root)['fieldDefinitions']
                          if 'chapter' in row['appliesTo'] and row['valueType'] == 'topic')

    def receipt(self):
        return self.op('project.workflow.chapter-receipt', chapterId=self.cid, view='model')

    def preview(self, quotes):
        return self.full(self.op('source.chapter-facts-preview', chapterId=self.cid,
            requestId='handover', facts=[{'findingId': 'F-HANDOVER', 'ownerId': self.cid,
                'field': self.field, 'statement': '交接记录：' + quote['text'], 'quote': quote,
                'reason': '按当前B逐条引用登记已经发生的动作。'} for quote in quotes]))

    def test_model_receipt_exposes_all_quotes_and_explains_an_incomplete_request(self):
        source_path = self.root / '.gnovel/structure.json'
        workflow_path = self.root / '.gnovel/workflow.json'
        before = source_path.read_bytes(), workflow_path.read_bytes()
        result = self.receipt()
        item = result['factInventory']['items'][0]
        expected = [quote_span(self.body, quote) for quote in self.quotes]
        self.assertEqual(item['requiredEvidence'], expected)
        self.assertEqual(item['requiredEvidenceCount'], 2)
        self.assertEqual(item['nextAction']['operation'], 'source.chapter-facts-preview')
        self.assertEqual((source_path.read_bytes(), workflow_path.read_bytes()), before)

        # The caller omits the second reviewed quote. Keep the original rejection;
        # only make its precise missing evidence visible through the same receipt.
        self.preview(expected[:1])
        before = source_path.read_bytes(), workflow_path.read_bytes()
        result = self.receipt()
        item = result['factInventory']['items'][0]
        self.assertFalse(result['readyForContinuation'])
        self.assertEqual(item['requestStatus'], 'stale')
        self.assertEqual(item['requiredEvidence'], expected)
        self.assertEqual(item['requestEvidence']['missingRequiredIndexes'], [1])
        self.assertEqual(item['requestEvidence']['providedEvidenceCount'], 1)
        self.assertEqual(item['requestEvidence']['matchedEvidenceCount'], 1)
        self.assertTrue(item['requestEvidence']['manuscriptMatches'])
        self.assertEqual(item['nextAction']['operation'], 'workflow.review-form')
        self.assertIn('FACT_REQUEST_BINDING_STALE', {row['code'] for row in result['factInventory']['blockers']})
        self.assertEqual((source_path.read_bytes(), workflow_path.read_bytes()), before)

    def test_full_set_can_be_reused_from_receipt_but_never_grants_source_approval(self):
        item = self.receipt()['factInventory']['items'][0]
        preview = self.preview(item['requiredEvidence'])
        pending = self.receipt()['factInventory']['items'][0]
        self.assertNotIn('requestStatus', pending)
        self.assertEqual(pending['requestEvidence']['missingRequiredIndexes'], [])
        self.assertEqual(pending['requestEvidence']['providedEvidenceCount'], 2)
        self.assertFalse(pending['complete'])
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)
        done = self.full(self.op('source.chapter-facts-complete', requestId='handover', registerEvidence=True))
        self.assertTrue(done['complete'])
        self.assertFalse(done['approvalGranted'])
        self.assertFalse(done['semanticReviewComplete'])
        result = self.receipt()
        self.assertTrue(result['factInventory']['complete'])
        self.assertTrue(result['readyForContinuation'])
        from scripts.core.finalization import reconcile_workflow
        from scripts.core.jsonio import read_json
        workflow = read_json(self.root / '.gnovel/workflow.json')
        self.assertEqual(workflow['nextAction']['action'], 'planner')
        self.assertNotEqual(workflow['nextAction']['chapterId'], self.cid)
        self.assertFalse(workflow['approvals'][self.cid]['approved'])
        self.assertTrue(reconcile_workflow(self.root)['ok'])
        continued = self.op('task.prepare', task='continue')
        self.assertEqual(continued['chapter']['chapterId'], workflow['nextAction']['chapterId'])
        self.assertEqual(continued['station'], 'gnovel-planner')

    def test_fact_completion_preserves_a_different_explicit_next_action(self):
        from scripts.core.jsonio import read_json, write_json
        preview = self.preview(self.receipt()['factInventory']['items'][0]['requiredEvidence'])
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)
        workflow_path = self.root / '.gnovel/workflow.json'
        workflow = read_json(workflow_path)
        selected = {'chapterId': self.cid, 'action': 'editor',
                    'reason': '作者另行要求复查本章。', 'needsApproval': False}
        workflow['nextAction'] = selected
        write_json(workflow_path, workflow)
        done = self.full(self.op('source.chapter-facts-complete', requestId='handover', registerEvidence=True))
        self.assertTrue(done['complete'])
        self.assertEqual(done['workflowHandoff']['code'], 'WORKFLOW_AMBIGUOUS')
        self.assertEqual(read_json(workflow_path)['nextAction'], selected)

    def test_end_of_scope_keeps_author_action_and_can_resume_derived_handoff(self):
        from scripts.core.finalization import pending_fact_action
        from scripts.core.jsonio import read_json, write_json
        preview = self.preview(self.receipt()['factInventory']['items'][0]['requiredEvidence'])
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)
        workflow_path = self.root / '.gnovel/workflow.json'
        workflow = read_json(workflow_path)
        workflow['approvedChapterIds'] = [self.cid]
        explicit = {'chapterId': self.cid, 'action': 'editor',
                    'reason': '作者要求范围结束后复查。', 'needsApproval': True}
        workflow['nextAction'] = explicit
        write_json(workflow_path, workflow)
        result = self.full(self.op('source.chapter-facts-complete', requestId='handover', registerEvidence=True))
        self.assertTrue(result['complete'])
        self.assertEqual(result['workflowHandoff']['code'], 'WORKFLOW_AMBIGUOUS')
        self.assertEqual(read_json(workflow_path)['nextAction'], explicit)
        # Simulate the stored tool handoff left by an interruption. Request replay
        # finishes only the mechanical action, with no new evidence or approval.
        workflow = read_json(workflow_path)
        workflow['nextAction'] = pending_fact_action(self.cid)
        write_json(workflow_path, workflow)
        result = self.full(self.op('source.chapter-facts-complete', requestId='handover', registerEvidence=True))
        self.assertTrue(result['workflowHandoff']['ok'])
        self.assertIsNone(read_json(workflow_path)['nextAction'])
        before = workflow_path.read_bytes()
        self.op('source.chapter-facts-complete', requestId='handover', registerEvidence=True)
        self.assertEqual(workflow_path.read_bytes(), before)

    def test_large_evidence_receipt_stays_bounded_and_all_quotes_remain_pageable(self):
        from scripts.core.project import query_policy
        from scripts.core.query import _size
        quotes = [{'text': self.body[start:end], 'start': start, 'end': end}
                  for start in range(len(self.body)) for end in range(start + 1, len(self.body) + 1)][:80]
        payload = self.payload()
        payload['factAssessment'] = {'status': 'changes'}
        payload['findings'] = [{'findingId': 'F-HANDOVER', 'severity': 'info',
            'ruleId': 'CHAPTER-PIPELINE', 'summary': '逐处保留当前交接引证。',
            'factChange': {'status': 'pending', 'requestId': 'handover',
                           'reason': '测试较多精确引文的交付。', 'evidence': quotes}}]
        self.record(payload)
        source_path = self.root / '.gnovel/structure.json'
        workflow_path = self.root / '.gnovel/workflow.json'
        before = source_path.read_bytes(), workflow_path.read_bytes()
        result = self.receipt()
        budget = query_policy()['maxBytes']['detail']
        self.assertTrue(result['responseCompacted'])
        self.assertLessEqual(_size({'ok': True, 'code': 'CHAPTER_RECEIPT', **result}), budget)
        self.assertEqual(result['chapter']['chapterId'], self.cid)
        self.assertEqual(result['bindingGeneration'], load_structure(self.root)['chapters'][0]['proseBinding']['generation'])
        self.assertFalse(result['readyToFinalize'])
        self.assertFalse(result['readyForContinuation'])
        self.assertFalse(result['factInventory']['complete'])
        self.assertIsNotNone(result['nextAction'])
        chunks, cursor = [], None
        while True:
            page = self.op('task.snapshot', reference=result['reportRef'], **({'cursor': cursor} if cursor else {}))
            self.assertLessEqual(_size(page), budget)
            chunks.extend(row['text'] for row in page['materials'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        full = json.loads(''.join(chunks))
        self.assertEqual(full['factInventory']['items'][0]['requiredEvidence'], quotes)
        self.assertEqual(full['factInventory']['items'][0]['requiredEvidenceCount'], 80)
        self.assertFalse(full['readyForContinuation'])
        overview = self.op('project.workflow.chapter-receipt', view='model')
        self.assertLessEqual(_size({'ok': True, 'code': 'CHAPTER_RECEIPT', **overview}), budget)
        full_overview = self.full(overview)
        chapter = next(row for row in full_overview['chapters'] if row['chapter']['chapterId'] == self.cid)
        self.assertEqual(chapter['continuity']['factInventory']['items'][0]['requiredEvidence'], quotes)
        self.assertEqual((source_path.read_bytes(), workflow_path.read_bytes()), before)

    def test_public_envelope_and_display_reference_are_inside_the_response_budget(self):
        from scripts.core.chapter_receipt import chapter_receipt
        from scripts.core.project import query_policy
        from scripts.core.query import _size
        budget = query_policy()['maxBytes']['detail']
        internal = chapter_receipt(self.root, self.cid, view='model')
        self.assertNotIn('responseCompacted', internal)
        # This summary occurs twice in the raw receipt. Put that receipt just
        # below the limit, where public status/reference fields cross it.
        padding = (budget - 2 - _size(internal)) // 2
        self.assertGreater(padding, 0)
        payload = self.payload()
        payload['factAssessment'] = {'status': 'changes'}
        payload['findings'] = [{'findingId': 'F-HANDOVER', 'severity': 'info',
            'ruleId': 'CHAPTER-PIPELINE', 'summary': '两项交接动作均需保留。' + 'a' * padding,
            'factChange': {'status': 'pending', 'requestId': 'handover',
                           'reason': '两项动作共同说明交接已结束。', 'evidence': self.quotes}}]
        self.record(payload)
        internal = chapter_receipt(self.root, self.cid, view='model')
        self.assertNotIn('responseCompacted', internal)
        self.assertLessEqual(_size(internal), budget)
        self.assertGreater(_size({'ok': True, 'code': 'CHAPTER_RECEIPT', **internal}), budget)
        for selector in (self.cid, 'CH001'):
            with self.subTest(selector=selector):
                result = self.op('project.workflow.chapter-receipt', chapterId=selector, view='model')
                self.assertLessEqual(_size(result), budget)
                self.assertTrue(result['responseCompacted'])
                self.assertEqual(result['chapter']['chapterId'], self.cid)
                full = self.full(result)
                self.assertEqual(full['factInventory']['items'][0]['requiredEvidenceCount'], 2)
                self.assertFalse(result['readyForContinuation'])
                if selector == 'CH001':
                    self.assertEqual(result['chapterReferences'][0]['chapterId'], self.cid)
        process = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/novelctl.py'),
            'project', 'workflow', 'chapter-receipt', '--project-root', str(self.root),
            '--chapter-id', 'CH001', '--view', 'model'], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertLessEqual(_size(result), budget)
        self.assertTrue(result['responseCompacted'])
        self.assertEqual(result['chapterReferences'][0]['chapterId'], self.cid)
        # unittest discovery adds scripts/ to sys.path. A fresh package client
        # must also work without that accidental top-level core import path.
        process = subprocess.run([sys.executable, '-c',
            "import json,sys; from pathlib import Path; "
            "from scripts.core.operations import call_operation; "
            "print(json.dumps(call_operation('project.workflow.chapter-receipt', Path(sys.argv[1]), "
            "{'chapterId':'CH001','view':'model'}),ensure_ascii=False))", str(self.root)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertLessEqual(_size(result), budget)
        self.assertTrue(result['responseCompacted'])
        self.assertEqual(result['chapterReferences'][0]['chapterId'], self.cid)


if __name__ == '__main__':
    unittest.main()
