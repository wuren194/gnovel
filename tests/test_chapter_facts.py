"""Chapter facts use real finalization, source commit, and evidence receipts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.chapter_facts import complete_chapter_facts, preview_chapter_facts
from scripts.core.continuity import complete_continuity, continuity_preview
from scripts.core.finalization import finalize_chapter
from scripts.core.jsonio import read_json, write_json
from scripts.core.project import query_policy
from scripts.core.source_changes import preview_source_change
from scripts.core.source_evidence import EVIDENCE_PATH, query_evidence, register_evidence
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit
from tests.test_phase2_ownership_cutover import binding_project, _walk_to_review


class ChapterFactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prose = '他关上了城门。\r\n他把钥匙交给门房。'
        source = binding_project(self.root)
        self.cid = source['chapters'][0]['chapterId']
        self.prose_path = self.root / source['chapters'][0]['filePath']
        source['entities'] = [
            {'entityId': 'C-FACT-OWNER', 'entityType': 'character', 'name': '守门人', 'aliases': [], 'status': 'active'},
            {'entityId': 'C-FACT-NEW', 'entityType': 'character', 'name': '门房', 'aliases': [], 'status': 'active'},
        ]
        source['fieldDefinitions'] += [
            {'fieldId': 'FD-FACT-TOPIC', 'key': 'history.gates', 'label': '守门经历', 'valueType': 'topic',
             'appliesTo': ['character', 'chapter']},
            {'fieldId': 'FD-FACT-NUMBER', 'key': 'history.count', 'label': '次数', 'valueType': 'number',
             'appliesTo': ['character']},
        ]
        source['attributes'].append({'attributeId': 'AT-FACT-TOPIC', 'ownerId': 'C-FACT-OWNER',
                                     'fieldId': 'FD-FACT-TOPIC', 'values': []})
        write_json(self.root / '.govel/structure.json', source)
        _walk_to_review(self.root, self.cid, self.prose)
        finalized = finalize_chapter(self.root, self.cid, skip_title=True,
                                     binding_generation=source['chapters'][0]['proseBinding']['generation'])
        self.assertTrue(finalized['ok'])
        preview = continuity_preview(self.root, self.cid)
        complete_continuity(self.root, self.cid, batch_hash=preview['batchHash'], approved=True,
            confirmation={'decisions': [], 'summary': {'resolution': 'not_required',
                          'reason': '本测试的短篇单章不需要独立全书摘要。'}})

    def fact(self, **overrides):
        return {'ownerId': 'C-FACT-OWNER', 'field': 'history.gates', 'statement': '守门人关闭了城门。',
                'quote': '他关上了城门。', 'reason': '正文明确写出关门动作。', **overrides}

    def prepare(self, facts=None, request='facts-1'):
        return preview_chapter_facts(self.root, requestId=request, chapterId=self.cid,
                                     facts=[self.fact()] if facts is None else facts)

    def commit(self, report):
        action = report['nextAction']['arguments']
        return commit(self.root, operation_id=action['operationId'], plan_hash=action['planHash'],
                      approved=True, actor='test-author')

    def test_preview_approval_commit_and_exact_evidence_are_separate(self):
        original = (self.root / '.govel/structure.json').read_bytes()
        report = self.prepare()
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), original)
        self.assertEqual(report['facts'][0]['attributeId'], 'AT-FACT-TOPIC')
        self.assertFalse(report['committed'])
        self.assertFalse(report['complete'])
        args = report['nextAction']['arguments']
        with self.assertRaises(StructureError) as caught:
            commit(self.root, operation_id=args['operationId'], plan_hash=args['planHash'], approved=False, actor='test')
        self.assertEqual(caught.exception.code, 'APPROVAL_REQUIRED')
        self.assertFalse(complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)['committed'])
        self.assertFalse((self.root / EVIDENCE_PATH).exists())
        self.commit(report)
        ready = complete_chapter_facts(self.root, requestId='facts-1')
        self.assertTrue(ready['readyToRegisterEvidence'])
        self.assertEqual(ready['pendingEvidenceCount'], 1)
        self.assertFalse((self.root / EVIDENCE_PATH).exists())
        finished = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertTrue(finished['complete'])
        self.assertFalse(finished['semanticReviewComplete'])
        self.assertFalse(finished['approvalGranted'])
        self.assertFalse(finished['coverage']['allChapterFactsIdentified'])
        citation = query_evidence(self.root, chapterId=self.cid)['items'][0]
        self.assertEqual(citation['targetIds'], [report['facts'][0]['fragmentId']])
        self.assertEqual(citation['kind'], 'depends_on')
        self.assertEqual(citation['locator']['start'], 0)
        self.assertEqual(self.prose_path.read_bytes().decode(), self.prose)
        replay = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertTrue(replay['complete'])
        self.assertEqual(len(query_evidence(self.root, chapterId=self.cid)['items']), 1)

    def test_new_topic_attribute_allocated_once_and_request_resumes_without_facts(self):
        facts = [self.fact(ownerId='C-FACT-NEW'), self.fact(ownerId='C-FACT-NEW',
            statement='门房收到钥匙。', quote='他把钥匙交给门房。')]
        report = self.prepare(facts)
        identities = report['facts']
        self.assertEqual(identities[0]['attributeId'], identities[1]['attributeId'])
        self.assertNotEqual(identities[0]['fragmentId'], identities[1]['fragmentId'])
        self.assertEqual(preview_chapter_facts(self.root, requestId='facts-1')['facts'], identities)
        self.commit(report)
        replay = preview_chapter_facts(self.root, requestId='facts-1')
        self.assertEqual(replay['operationState'], 'committed')
        self.assertEqual(replay['facts'], identities)
        source = load_structure(self.root)
        self.assertEqual(len([row for row in source['attributes'] if row['ownerId'] == 'C-FACT-NEW']), 1)
        self.assertEqual(len([row for row in source['fragments'] if row['fragmentId'] in
                              {item['fragmentId'] for item in identities}]), 2)
        with self.assertRaises(StructureError) as caught:
            self.prepare([self.fact(statement='不同的事实。')])
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_closeout_checks_declared_fact_receipts_before_next_handoff(self):
        from scripts.core.chapter_closeout import prepare_closeout, closeout_chapter
        report = self.prepare()
        self.commit(report)
        prepared = prepare_closeout(self.root, chapterId=self.cid, factRequestIds=['facts-1'])
        result = closeout_chapter(self.root, requestId='fact-handoff-1', prepareRef=prepared['prepareRef'],
                                 prepareNext=False)
        self.assertEqual(result['code'], 'CLOSEOUT_INPUT_REQUIRED')
        self.assertFalse(result['readyForContinuation'])
        self.assertNotIn('nextPrepareAction', result)
        self.assertTrue(result['factsAssessed'])
        complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        # Domain writes require a fresh inspected closeout snapshot; an old
        # request cannot silently accept new evidence as its original input.
        with self.assertRaises(StructureError):
            closeout_chapter(self.root, requestId='fact-handoff-1')
        prepared = prepare_closeout(self.root, chapterId=self.cid, factRequestIds=['facts-1'])
        result = closeout_chapter(self.root, requestId='fact-handoff-2', prepareRef=prepared['prepareRef'],
                                 prepareNext=False)
        self.assertEqual(result['code'], 'CLOSEOUT_COMPLETED')
        self.assertTrue(result['readyForContinuation'])
        self.assertEqual(result['completedStages'], [])
        self.assertFalse(result['approvalPerformed'])

    def test_field_owner_dependency_and_quote_are_not_inferred(self):
        cases = [({'field': 'history.count'}, 'CHAPTER_FACTS_TOPIC_REQUIRED'),
                 ({'field': 'invented.field'}, 'CHAPTER_FACTS_FIELD_INVALID'),
                 ({'ownerId': 'C-UNKNOWN'}, 'CHAPTER_FACTS_OWNER_INVALID'),
                 ({'dependsOn': ['SF-UNKNOWN']}, 'CHAPTER_FACTS_DEPENDENCY_INVALID'),
                 ({'quote': '正文中没有这句话。'}, 'EVIDENCE_QUOTE_MISSING')]
        original = (self.root / '.govel/structure.json').read_bytes()
        for index, (overrides, code) in enumerate(cases):
            with self.subTest(code=code), self.assertRaises(StructureError) as caught:
                self.prepare([self.fact(**overrides)], request=f'bad-{index}')
            self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), original)
        self.assertFalse((self.root / EVIDENCE_PATH).exists())

    def test_pending_continuity_returns_its_real_action_and_does_not_confirm(self):
        path = self.root / '.govel/workflow.json'
        workflow = read_json(path)
        workflow['continuity'][self.cid]['status'] = 'pending'
        write_json(path, workflow)
        original = path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'CHAPTER_FACTS_CONTINUITY_REQUIRED')
        self.assertEqual(caught.exception.details['nextAction']['operation'], 'project.workflow.continuity-preview')
        self.assertFalse(caught.exception.details['automaticConfirmationAllowed'])
        self.assertEqual(path.read_bytes(), original)

    def test_unfinalized_chapter_is_rejected_even_if_candidate_prose_exists(self):
        source = load_structure(self.root)
        source['chapters'][0]['status'] = 'review'
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'CHAPTER_FACTS_STAGE_REQUIRED')
        self.assertEqual(caught.exception.details['nextAction']['operation'], 'project.workflow.chapter-receipt')

    def test_ordinary_source_commit_rechecks_unregistered_formal_prose(self):
        report = self.prepare()
        original = (self.root / '.govel/structure.json').read_bytes()
        self.prose_path.write_text('正文在预览后已改变。')
        with self.assertRaises(StructureError):
            self.commit(report)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), original)
        self.assertFalse((self.root / EVIDENCE_PATH).exists())

    def test_commit_marker_guard_rolls_back_fact_when_prose_races(self):
        report = self.prepare()
        original = (self.root / '.govel/structure.json').read_bytes()
        raced = False

        def race(phase, path=None):
            nonlocal raced
            if phase == 'write' and path == '.govel/structure.json' and not raced:
                raced = True
                self.prose_path.write_text('另一个编辑器更改了正文。')

        with patch('scripts.core.write_sets.checkpoint', side_effect=race), self.assertRaises(StructureError):
            self.commit(report)
        self.assertTrue(raced)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), original)
        self.assertEqual(self.prose_path.read_text(), '另一个编辑器更改了正文。')
        self.assertFalse((self.root / '.govel/operations' / (report['operationId'] + '.json')).exists())

    def test_postcommit_prose_drift_blocks_evidence_without_new_writes(self):
        report = self.prepare()
        self.commit(report)
        self.prose_path.write_text('另一份正文。')
        status = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertFalse(status['complete'])
        self.assertFalse(status['readyToRegisterEvidence'])
        self.assertTrue(status['blockers'])
        self.assertFalse((self.root / EVIDENCE_PATH).exists())

    def test_postcommit_source_drift_requires_rereading_before_evidence(self):
        report = self.prepare()
        self.commit(report)
        source = load_structure(self.root)
        unrelated = preview_source_change(self.root, requestId='another-source-request', baseRevision=source['revision'],
            reason='添加独立主题字段。', changes=[{'action': 'put', 'collection': 'fieldDefinitions', 'record': {
                'fieldId': '$other', 'key': 'other.topic', 'label': '另一主题', 'valueType': 'topic', 'appliesTo': ['character']}}])
        commit(self.root, operation_id=unrelated['operationId'], plan_hash=unrelated['planHash'], approved=True, actor='test')
        status = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertFalse(status['complete'])
        self.assertIn('CHAPTER_FACTS_SOURCE_CHANGED', [row['code'] for row in status['blockers']])
        self.assertEqual(status['nextAction']['operation'], 'evidence.prepare')
        self.assertFalse((self.root / EVIDENCE_PATH).exists())
        # An explicit new reading and ordinary evidence submission can finish the
        # original fact request; its frozen source request is never rebound.
        from scripts.core.evidence_pack import prepare_evidence, pack_evidence
        from scripts.core.evidence_submission import submit_evidence
        prepared = prepare_evidence(self.root, kind='prose', chapterId=self.cid, view='model')
        packed = pack_evidence(self.root, snapshotRef=prepared['snapshotRef'], requestId='reread-facts', view='model',
            decisions=[{'quote': {'text': '他关上了城门。'}, 'targetIds': [report['facts'][0]['fragmentId']],
                        'kind': 'depends_on', 'reason': self.fact()['reason']}])
        submit_evidence(self.root, submissionRef=packed['submissionRef'])
        completed = complete_chapter_facts(self.root, requestId='facts-1')
        self.assertTrue(completed['complete'])
        self.assertFalse(completed['sourceSnapshotCurrent'])
        self.assertFalse(completed['evidenceStarted'])

    def test_chapter_owner_can_invalidate_approvals_without_guarding_its_own_output(self):
        report = self.prepare([self.fact(ownerId=self.cid)])
        self.assertIn(self.cid, report['sourcePreview']['impact']['approvalInvalidations'])
        self.commit(report)
        self.assertFalse(read_json(self.root / '.govel/workflow.json')['approvals'][self.cid]['approved'])
        status = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertTrue(status['complete'])
        self.assertTrue(continuity_preview(self.root, self.cid)['readyForContinuation'])

    def test_partial_evidence_retries_only_unfinished_batches_from_domain_receipts(self):
        report = self.prepare([self.fact(reason='明确正文证据。' * 260), self.fact(
            statement='守门人把钥匙交给门房。', quote='他把钥匙交给门房。', reason='另一个明确正文证据。' * 260)])
        self.commit(report)
        policy = deepcopy(query_policy())
        policy['maxBytes']['detail'] = 6500
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('synthetic interruption between evidence batches')
            return register_evidence(*args, **kwargs)

        with patch('scripts.core.evidence_submission.query_policy', return_value=policy), \
                patch('scripts.core.source_evidence.register_evidence', side_effect=fail_second):
            first = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertFalse(first['complete'])
        self.assertEqual(first['pendingEvidenceCount'], 1)
        self.assertEqual(first['evidenceSubmission']['completedBatches'], 1)
        # Resumption needs the source journal and evidence ledger, not a live transport cache.
        with tempfile.TemporaryDirectory() as empty_cache, patch(
                'scripts.core.transport_cache._directory', return_value=Path(empty_cache)):
            second = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertTrue(second['complete'])
        self.assertEqual(len(read_json(self.root / EVIDENCE_PATH)['batches']), 2)
        self.assertEqual(len(query_evidence(self.root, chapterId=self.cid)['items']), 2)

    def test_empty_selection_never_means_no_new_facts(self):
        with self.assertRaises(StructureError) as caught:
            self.prepare([])
        self.assertEqual(caught.exception.code, 'CHAPTER_FACTS_INPUT_INVALID')

    def test_unrelated_current_citation_does_not_complete_explicit_fact_request(self):
        report = self.prepare()
        self.commit(report)
        from scripts.core.evidence_pack import prepare_evidence, pack_evidence
        from scripts.core.evidence_submission import submit_evidence
        prepared = prepare_evidence(self.root, kind='prose', chapterId=self.cid, view='model')
        packed = pack_evidence(self.root, snapshotRef=prepared['snapshotRef'], requestId='mention-only', view='model',
            decisions=[{'quote': {'text': '他关上了城门。'}, 'targetIds': [report['facts'][0]['fragmentId']],
                        'kind': 'mentions', 'reason': self.fact()['reason']}])
        submit_evidence(self.root, submissionRef=packed['submissionRef'])
        completed = complete_chapter_facts(self.root, requestId='facts-1')
        self.assertFalse(completed['complete'])
        self.assertEqual(completed['pendingEvidenceCount'], 1)

    def test_completed_evidence_becomes_pending_when_fact_content_changes(self):
        report = self.prepare()
        self.commit(report)
        self.assertTrue(complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)['complete'])
        from scripts.core.source_content_proposals import resolve_content_target, preview_content_change
        source = load_structure(self.root)
        fid = report['facts'][0]['fragmentId']
        fragment = next(row for row in source['fragments'] if row['fragmentId'] == fid)
        resolved = resolve_content_target(self.root, recordId=fid, kind='fact')
        changed = preview_content_change(self.root, sessionRef=resolved['sessionRef'], expectedOldRecord=fragment,
            newRecord={**fragment, 'text': '守门人关闭了侧门。'}, reason='明确订正门的位置。',
            evidence=[{'fragmentId': fid, 'quote': fragment['text']}],
            approvalActor='测试作者', approvalEvidence='作者明确批准订正。')
        commit(self.root, operation_id=changed['operationId'], plan_hash=changed['planHash'], approved=True, actor='test')
        status = complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)
        self.assertFalse(status['complete'])
        self.assertFalse(status['readyToRegisterEvidence'])
        self.assertIn('CHAPTER_FACTS_TARGET_CHANGED', [row['code'] for row in status['blockers']])
        self.assertEqual(status['facts'][0]['evidenceStatus'], 'needs_review')

    def test_public_operations_accept_bound_quotes_and_request_only_resume(self):
        from scripts.core.operations import call_operation
        report = call_operation('source.chapter-facts-preview', self.root, {
            'requestId': 'public-facts', 'chapterId': self.cid,
            'facts': [self.fact(quote={'text': '他关上了城门。'})]})
        resumed = call_operation('source.chapter-facts-preview', self.root, {'requestId': 'public-facts'})
        self.assertEqual(resumed['operationId'], report['operationId'])
        call_operation('source.commit', self.root, {
            key: value for key, value in report['nextAction']['arguments'].items() if key != 'projectRoot'})
        complete = call_operation('source.chapter-facts-complete', self.root, {
            'requestId': 'public-facts', 'registerEvidence': True})
        self.assertTrue(complete['complete'])

    def test_large_preview_keeps_commit_and_completion_actions_with_bounded_audit(self):
        from scripts.core.query import _size
        from scripts.core.transport_cache import get
        report = self.prepare([self.fact(statement='可完整复核的事实陈述。' * 2500)])
        self.assertLessEqual(_size(report), query_policy()['maxBytes']['detail'])
        self.assertEqual(report['nextAction']['operation'], 'source.commit')
        self.assertEqual(report['statusAction']['operation'], 'source.chapter-facts-complete')
        # Source reports may compact first; if the outer envelope still exceeds
        # budget its complete audit remains accessible through task.snapshot.
        if report.get('responseCompacted'):
            saved = get(self.root, report['reportRef'], kind='authoring-report')
            self.assertEqual(saved['facts'][0]['fragmentId'][:3], 'SF-')
            self.assertEqual(report['detailsAction']['operation'], 'task.snapshot')
        self.commit(report)
        self.assertTrue(complete_chapter_facts(self.root, requestId='facts-1', registerEvidence=True)['complete'])


if __name__ == '__main__':
    unittest.main()
