"""Regression probes using disposable neutral manuscripts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifact_templates import artifact_template, validate_artifact_schema_declarations
from scripts.core.artifacts import envelope, file_sha256, sha256_text
from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.continuity import continuity_preview, complete_continuity
from scripts.core.derived_views import derived_outputs
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import review_policy
from scripts.core.review_artifacts import validate_review_artifact, validate_stage_b_completion
from scripts.core.structure_commands import preview_insert_chapter, commit_plan
from scripts.core.structure_store import load_structure
from scripts.core.summary_status import summary_freshness
from scripts.core.tool_catalog import list_capabilities
from scripts.novelctl import build_parser
from tests.fixtures_current import initialize_project
from tests.fixtures_workflow import review_ready_project, evidenced_slots
from tests.test_phase2_ownership_cutover import _walk_to_review, review_ready_schema5_project


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class CompletionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.text = '他把钥匙放回桌上，合上了登记本。'

    def payload(self):
        return {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': evidenced_slots(self.text),
                'findings': [], 'finalProseSha256': sha256_text(self.text)}

    def pack(self, payload):
        write_json(self.root / '工作台/current-review.json', payload)
        before = files(self.root)
        try:
            return pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review',
                                 input='工作台/current-review.json')
        except ValueError:
            self.assertEqual(files(self.root), before)
            raise

    def test_placeholder_and_empty_evidence_cannot_be_published(self):
        for placeholder in review_policy()['unfinishedConclusions']:
            payload = self.payload(); payload['slots']['logic']['conclusion'] = placeholder
            with self.subTest(placeholder=placeholder), self.assertRaises(ValueError) as caught:
                self.pack(payload)
            self.assertEqual(caught.exception.code, 'REVIEW_PLACEHOLDER')
        payload = self.payload(); payload['slots']['logic']['evidence'] = []
        with self.assertRaises(ValueError) as caught:
            self.pack(payload)
        self.assertEqual(caught.exception.code, 'REVIEW_EVIDENCE_REQUIRED')

    def test_missing_and_wrong_quotes_and_stale_body_fail(self):
        for quote in ({'text': '不存在的引文'}, {'text': self.text, 'start': 1, 'end': len(self.text) + 1}):
            payload = self.payload(); payload['slots']['logic']['evidence'] = [quote]
            with self.subTest(quote=quote), self.assertRaises(ValueError) as caught:
                self.pack(payload)
            self.assertIn(caught.exception.code, {'EVIDENCE_QUOTE_MISSING', 'EVIDENCE_QUOTE_MISMATCH'})
        payload = self.payload(); payload['finalProseSha256'] = '0' * 64
        with self.assertRaises(ValueError) as caught:
            self.pack(payload)
        self.assertEqual(caught.exception.code, 'ARTIFACT_BINDING_STALE')

    def test_ambiguous_unicode_quotes_need_exact_bounds(self):
        text = '甲。\r\n重复。\r\n重复。🙂'
        payload = self.payload(); payload['finalProseSha256'] = sha256_text(text)
        payload['slots'] = evidenced_slots(text)
        payload['slots']['logic']['evidence'] = [{'text': '重复。'}]
        with self.assertRaises(ValueError) as caught:
            validate_stage_b_completion(payload, text)
        self.assertEqual(caught.exception.code, 'EVIDENCE_QUOTE_AMBIGUOUS')
        start = text.rindex('重复。')
        payload['slots']['logic']['evidence'][0].update(start=start, end=start + len('重复。'))
        validate_stage_b_completion(payload, text)

    def test_explicit_not_applicable_is_recorded_without_approval(self):
        payload = self.payload()
        payload['slots']['continuity'] = {'conclusion': '本章没有前章依赖', 'notApplicableReason': '合成项目仅有首章，无前章可比对'}
        before = read_json(self.root / '.govel/workflow.json')['approvals']
        result = self.pack(payload)
        self.assertFalse(result['approvalPerformed'])
        self.assertEqual(read_json(self.root / '.govel/workflow.json')['approvals'], before)
        self.assertEqual(read_json(self.root / result['artifactPath'])['payload']['slots'], payload['slots'])
        payload['slots']['continuity']['notApplicableReason'] = '待填写'
        with self.assertRaises(ValueError): self.pack(payload)

    def test_passing_verdict_cannot_keep_a_blocker(self):
        payload = self.payload(); payload['slots']['logic'].update(blocking=True, location=self.text)
        with self.assertRaises(ValueError) as caught: self.pack(payload)
        self.assertEqual(caught.exception.code, 'REVIEW_COMPLETION_CONFLICT')
        payload = self.payload()
        payload['findings'] = [{'severity': 'S2', 'ruleId': 'TEST', 'summary': '未解决',
                                'location': self.text, 'repairSuggestion': '复核'}]
        with self.assertRaises(ValueError) as caught: self.pack(payload)
        self.assertEqual(caught.exception.code, 'REVIEW_COMPLETION_CONFLICT')

    def test_missing_review_evidence_cannot_finalize(self):
        workflow = read_json(self.root / '.govel/workflow.json')
        payload = self.payload()
        for row in payload['slots'].values(): row.pop('evidence')
        artifact = envelope('stage_b_review', payload,
                            project_id=load_structure(self.root)['projectId'], chapter_id=self.cid)
        validate_review_artifact(artifact)
        path = Path(workflow['artifacts'][self.cid]['stage_b_review']['artifactPath'])
        write_json(path, artifact)
        workflow['artifacts'][self.cid]['stage_b_review'].update(
            artifactSha256=artifact['artifactSha256'], artifactFileSha256=file_sha256(path))
        workflow['approvals'][self.cid]['artifactHashes']['stage_b_review'] = artifact['artifactSha256']
        write_json(self.root / '.govel/workflow.json', workflow)
        before = files(self.root)
        preflight = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertIn('REVIEW_EVIDENCE_REQUIRED', {row['code'] for row in preflight.blockers})
        with self.assertRaises(ValueError) as caught: finalize_chapter(self.root, self.cid, skip_title=True, binding_generation=chapter_receipt(self.root, self.cid)['bindingGeneration'])
        self.assertEqual(caught.exception.details['blockers'], preflight.blockers)
        self.assertEqual(files(self.root), before)


    def test_registry_controls_placeholder_rejection(self):
        with patch('scripts.core.project.workflow_policy', return_value={'review': {'unfinishedConclusions': ['CUSTOM']}}):
            payload = self.payload(); payload['slots']['logic']['conclusion'] = 'CUSTOM'
            with self.assertRaises(ValueError) as caught: self.pack(payload)
            self.assertEqual(caught.exception.code, 'REVIEW_PLACEHOLDER')
        for invalid in (None, [], [''], ['X', 'X']):
            with patch('scripts.core.project.workflow_policy', return_value={'review': {'unfinishedConclusions': invalid}}):
                with self.assertRaises(ValueError): review_policy()

    def test_model_receipt_preserves_blockers_but_does_not_echo_prose(self):
        before = files(self.root)
        full = chapter_receipt(self.root, self.cid, view='full', skip_title=True)
        model = call_operation('project.workflow.chapter-receipt', self.root,
                               {'chapterId': self.cid, 'skipTitle': True})
        for field in ('blockers', 'ownership', 'bindingGeneration', 'readyToFinalize', 'summaryFreshness', 'continuity'):
            self.assertEqual(model[field], full[field])
        self.assertNotIn(self.text, json.dumps(model, ensure_ascii=False))
        self.assertIn(self.text, json.dumps(full, ensure_ascii=False))
        self.assertTrue(all('payload' not in row for row in model['artifacts']))
        details = model['detailsOperation']
        reread = call_operation(details['operation'], self.root, details['arguments'])
        self.assertEqual(reread['artifacts'], full['artifacts'])
        self.assertEqual(files(self.root), before)

    def test_invalid_prose_payload_keeps_structured_blockers_in_model_view(self):
        workflow = read_json(self.root / '.govel/workflow.json')
        entry = workflow['artifacts'][self.cid]['candidate_prose']
        path = Path(entry['artifactPath'])
        for invalid in ([], None, 'invalid', 7):
            artifact = envelope('candidate_prose', invalid,
                project_id=load_structure(self.root)['projectId'], chapter_id=self.cid)
            write_json(path, artifact)
            entry.update(artifactSha256=artifact['artifactSha256'], artifactFileSha256=file_sha256(path))
            workflow['approvals'][self.cid]['artifactHashes']['candidate_prose'] = artifact['artifactSha256']
            write_json(self.root / '.govel/workflow.json', workflow)
            before = files(self.root)
            result = chapter_receipt(self.root, self.cid, skip_title=True, view='model')
            self.assertIn('PROSE_PAYLOAD_INVALID', {row['code'] for row in result['blockers']})
            from scripts.core.chapter_status import chapter_status
            board = chapter_status(self.root, self.cid)
            self.assertIn('PROSE_PAYLOAD_INVALID', {row['code'] for row in board['blockers']})
            self.assertEqual(board['writingNotes'], {})
            self.assertEqual(files(self.root), before)

    def test_historical_placeholder_stays_readable_but_fails_completion(self):
        payload = self.payload(); payload['slots']['logic']['conclusion'] = '待填写'
        validate_review_artifact(envelope('stage_b_review', payload))
        with self.assertRaises(ValueError) as caught:
            validate_stage_b_completion(payload, self.text)
        self.assertEqual(caught.exception.code, 'REVIEW_PLACEHOLDER')

    def test_registration_rejects_malformed_candidate_without_mutating_workflow(self):
        from copy import deepcopy
        from scripts.core.workflow import record_artifact
        source = load_structure(self.root)
        workflow = read_json(self.root / '.govel/workflow.json')
        entry = workflow['artifacts'][self.cid]['candidate_prose']
        path = Path(entry['artifactPath'])
        for invalid in ([], None, {}, {'text': []}, {'text': '  '}):
            candidate = envelope('candidate_prose', invalid, project_id=source['projectId'], chapter_id=self.cid)
            write_json(path, candidate)
            entry.update(artifactSha256=candidate['artifactSha256'], artifactFileSha256=file_sha256(path))
            artifact = envelope('stage_b_review', self.payload(), project_id=source['projectId'], chapter_id=self.cid)
            before = deepcopy(workflow), files(self.root)
            with self.assertRaises(ValueError) as caught:
                record_artifact(workflow, self.cid, artifact, structure=source, project_id=source['projectId'], project_root=self.root)
            self.assertEqual(caught.exception.code, 'PROSE_PAYLOAD_INVALID')
            self.assertEqual((workflow, files(self.root)), before)

    def test_skeleton_guidance_is_not_registered_as_review_content(self):
        result = call_operation('workflow.review-skeleton', self.root, {
            'chapterId': self.cid, 'artifactType': 'stage_b_review', 'output': '工作台/skeleton.json'})
        skeleton = read_json(self.root / result['path'])
        payload = self.payload(); payload['_guide'] = skeleton['_guide']
        packed = self.pack(payload)
        saved = read_json(self.root / packed['artifactPath'])
        self.assertNotIn('_guide', saved['payload'])
        self.assertFalse(packed['approvalPerformed'])

class WorkflowProjectionTests(unittest.TestCase):
    def test_create_and_finalize_keep_source_as_the_only_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_schema5_project(root)['chapters'][0]
            source = load_structure(root); before = files(root)
            outputs, owners = derived_outputs(root, source, current=source,
                                               extras={chapter['filePath']: b'prospective prose'})
            self.assertEqual(set(outputs), {'.govel/structure.json', chapter['filePath']})
            self.assertEqual(owners, {})
            self.assertEqual(files(root), before)
            receipt = chapter_receipt(root, chapter['chapterId'], skip_title=True)
            finalize_chapter(root, chapter['chapterId'], skip_title=True,
                             binding_generation=receipt['bindingGeneration'])
            self.assertTrue((root/chapter['filePath']).is_file())
            self.assertFalse((root/'规划/结构索引.md').exists())
            self.assertFalse((root/'设定/实体索引.md').exists())

    def test_summary_advisory_is_explicit_across_closeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            chapter = review_ready_schema5_project(root)['chapters'][0]
            cid = chapter['chapterId']
            generation = load_structure(root)['chapters'][0]['proseBinding']['generation']
            finalized = finalize_chapter(root, cid, skip_title=True, binding_generation=generation)
            self.assertTrue(finalized['summaryRefreshRecommended'])
            self.assertFalse(finalized['summaryRefreshRequired'])
            preview = continuity_preview(root, cid)
            self.assertTrue(preview['summaryFreshness']['stale'])
            confirmation = {'decisions': [], 'summary': {'resolution': 'not_required',
                'reason': '首章的本轮交接使用完整正文，暂不生成全书摘要'}}
            result = complete_continuity(root, cid, batch_hash=preview['batchHash'],
                                         confirmation=confirmation, approved=True)
            self.assertTrue(result['readyForContinuation'])
            self.assertTrue(result['summaryFreshness']['stale'])
            self.assertEqual(result['summaryDisposition'], confirmation['summary'])
            self.assertEqual(result['summaryFreshness'], summary_freshness(root, load_structure(root), target_chapter_id=cid))
            again = complete_continuity(root, cid, batch_hash=preview['batchHash'],
                                        confirmation=confirmation, approved=True)
            self.assertEqual(again['summaryFreshness'], result['summaryFreshness'])

    def test_stale_summary_cannot_be_claimed_refreshed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); fixture = review_ready_project(root, chapter_count=1, changes=[])
            cid = fixture['chapterIds'][0]
            finalize_chapter(root, cid, skip_title=True, binding_generation=chapter_receipt(root, cid)['bindingGeneration'])
            preview = continuity_preview(root, cid)
            for disposition, reason, code in [('refreshed', '已刷新', 'CONTINUITY_SUMMARY_STALE'),
                                               ('not_required', '待填写', 'CONTINUITY_SUMMARY_REQUIRED')]:
                before = files(root)
                with self.assertRaises(ValueError) as caught:
                    complete_continuity(root, cid, batch_hash=preview['batchHash'], approved=True,
                        confirmation={'decisions': [], 'summary': {'resolution': disposition, 'reason': reason}})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(files(root), before)

    def test_intents_and_templates_lead_to_existing_operations(self):
        for query, operation in [('创建章节', 'project.structure.insert-preview'),
                                  ('新建章节', 'project.structure.insert-preview'),
                                  ('定稿预检', 'project.workflow.chapter-receipt'),
                                  ('写下一章', 'task.prepare')]:
            result = list_capabilities(query=query)
            self.assertEqual(result['operations'][0]['id'], operation)
        next_chapter = list_capabilities(query='写下一章')['operations'][0]
        self.assertEqual(next_chapter['examples'][0]['task'], 'continue')
        args = build_parser().parse_args(['project', 'workflow', 'artifact-template', '--artifact-type', 'stage_b_review'])
        self.assertEqual(args.type, 'stage_b_review')
        guide = artifact_template('stage_b_review')['payload']['_guide']
        self.assertIn('POLISH_READY', guide['enums']['verdict'])
        self.assertTrue(validate_artifact_schema_declarations()['ok'])
