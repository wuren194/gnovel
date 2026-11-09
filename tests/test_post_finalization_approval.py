"""Current review approval cannot erase or substitute for a finalization receipt."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.continuity import _batch_hash, complete_continuity, continuity_preview
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project_doctor import assess_project
from scripts.core.prose_binding import seal_binding
from scripts.core.structure_store import load_structure
from scripts.novelctl import main
from tests.fixtures_workflow import evidenced_slots
from tests.test_finalization_preflight import ready_project


class PostFinalizationApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid, self.artifacts = ready_project(self.root)
        self.workflow_path = self.root / '.gnovel/workflow.json'
        self.source_path = self.root / '.gnovel/structure.json'
        for domain in ('_tooling_domain', '_knowledge_domain'):
            probe = patch('scripts.core.project_doctor.' + domain,
                          return_value=({'applicable': False}, []))
            probe.start(); self.addCleanup(probe.stop)
        receipt = chapter_receipt(self.root, self.cid, title_candidate_id='title-1')
        self.cli_ok('finalize-chapter', '--title-candidate-id', 'title-1',
                    '--binding-generation', str(receipt['bindingGeneration']))
        preview = continuity_preview(self.root, self.cid)
        complete_continuity(self.root, self.cid, batch_hash=preview['batchHash'], approved=True,
            confirmation={'decisions': [], 'summary': {'resolution': 'not_required',
                          'reason': '中性单章回归已核对交班正文。'}})
        self.finalized = self.workflow()
        self.formal = deepcopy(load_structure(self.root)['chapters'][0])

    def workflow(self):
        return read_json(self.workflow_path)

    def cli(self, action, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['project', 'workflow', action, '--project-root', str(self.root),
                         '--chapter-id', self.cid, *arguments])
        return code, json.loads(output.getvalue())

    def cli_ok(self, action, *arguments):
        code, result = self.cli(action, *arguments)
        self.assertEqual(code, 0, result)
        return result

    def new_review(self):
        text = self.artifacts['candidate_prose']['payload']['text']
        packed = call_operation('workflow.artifact-pack', self.root, {
            'chapterId': self.cid, 'artifactType': 'stage_b_review', 'payload': {
                'verdict': 'POLISH_READY', 'slots': evidenced_slots(text), 'findings': [],
                'factAssessment': {'status': 'no_change'},
                'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}})
        self.cli_ok('artifact-approve', '--artifact', '@' + str(self.root / packed['artifactPath']))
        return packed

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def doctor(self):
        before = self.inventory()
        report = assess_project(self.root)
        self.assertEqual(self.inventory(), before, 'Doctor must not repair or restamp history')
        return report

    def codes(self, report):
        return {r['code'] for r in report['issues'] if r['severity'] == 'blocking'}

    def test_new_current_b_preserves_finalization_and_title_decision(self):
        original_b = self.root / '工作台/stage_b_review.json'
        original_bytes = original_b.read_bytes()
        packed = self.new_review()
        approval = self.workflow()['approvals'][self.cid]
        self.assertTrue(approval.get('finalized'), approval)
        self.assertEqual(approval.get('titleDecision'), self.finalized['approvals'][self.cid]['titleDecision'])
        self.assertTrue(approval['approved'])
        self.assertEqual(approval['artifactHashes']['stage_b_review'], packed['artifactSha256'])
        self.assertNotEqual(packed['artifactSha256'], self.finalized['continuity'][self.cid]['factReview']['artifactSha256'])
        self.assertEqual(self.workflow()['continuity'], self.finalized['continuity'])
        self.assertEqual(load_structure(self.root)['chapters'][0], self.formal)
        self.assertEqual(original_b.read_bytes(), original_bytes)
        receipt = chapter_receipt(self.root, self.cid)
        self.assertEqual(receipt['titleDecision'], approval['titleDecision'])
        self.assertTrue(receipt['readyForContinuation'])
        report = self.doctor()
        self.assertTrue(report['healthy'], report['issues'])

    def test_existing_lost_flags_are_recognized_from_sealed_evidence_read_only(self):
        self.new_review()
        workflow = self.workflow()
        # Model the exact pre-fix approval replacement, not a repair of the project.
        workflow['approvals'][self.cid].pop('finalized', None)
        workflow['approvals'][self.cid].pop('titleDecision', None)
        write_json(self.workflow_path, workflow)
        report = self.doctor()
        self.assertTrue(report['healthy'], report['issues'])
        self.assertNotIn('finalized', self.workflow()['approvals'][self.cid])
        self.assertNotIn('titleDecision', self.workflow()['approvals'][self.cid])

    def test_missing_finalization_proof_is_reported_once(self):
        self.new_review()
        workflow = self.workflow()
        workflow['continuity'].pop(self.cid)
        workflow['approvals'][self.cid].pop('finalized', None)
        write_json(self.workflow_path, workflow)
        report = self.doctor()
        rows = [r for r in report['issues'] if r['code'] == 'STABLE_CHAPTER_APPROVAL_MISSING']
        self.assertEqual(len(rows), 1, rows)
        self.assertIn('CONTINUITY_REQUIRED', self.codes(report))

    def test_naked_finalized_flag_cannot_replace_bound_history(self):
        self.new_review()
        valid = self.workflow()
        for defect in ('missing_review', 'wrong_generation', 'wrong_prose', 'wrong_state'):
            with self.subTest(defect=defect):
                workflow = deepcopy(valid)
                workflow['approvals'][self.cid]['finalized'] = True
                record = workflow['continuity'][self.cid]
                if defect == 'missing_review':
                    record.pop('factReview')
                elif defect == 'wrong_generation':
                    record['factReview']['bindingGeneration'] += 1
                elif defect == 'wrong_prose':
                    record['factReview']['proseSha256'] = '0' * 64
                else:
                    record['stateArtifactSha256'] = '0' * 64
                record['batchHash'] = _batch_hash(record)
                write_json(self.workflow_path, workflow)
                self.assertIn('STABLE_CHAPTER_APPROVAL_MISSING', self.codes(self.doctor()))

    def test_project_integrity_reports_each_finding_once_without_collapsing_codes(self):
        workflow = self.workflow()
        workflow['approvals']['CH-UNKNOWN'] = {}
        workflow['artifacts']['CH-UNKNOWN'] = {}
        write_json(self.workflow_path, workflow)
        report = self.doctor()
        rows = [r for r in report['issues'] if r['code'] == 'WORKFLOW_UNKNOWN_CHAPTER']
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({r['evidence']['field'] for r in rows}, {'artifacts', 'approvals'})

    def test_history_damage_and_external_prose_remain_blocking(self):
        self.new_review()
        valid = self.workflow_path.read_bytes()
        workflow = self.workflow()
        workflow['continuity'][self.cid]['confirmationHash'] = 'damaged'
        write_json(self.workflow_path, workflow)
        self.assertIn('CONTINUITY_STATE_INVALID', self.codes(self.doctor()))
        self.workflow_path.write_bytes(valid)
        old_b = self.root / '工作台/stage_b_review.json'
        old_bytes = old_b.read_bytes()
        old_b.write_text('{}', encoding='utf-8')
        self.assertIn('STABLE_CHAPTER_APPROVAL_MISSING', self.codes(self.doctor()))
        old_b.write_bytes(old_bytes)
        prose = self.root / self.formal['filePath']
        prose.write_text(prose.read_text() + '外部追加。', encoding='utf-8')
        self.assertIn('PROSE_EXTERNAL_MODIFICATION', self.codes(self.doctor()))

    def test_current_generation_cannot_borrow_previous_finalization(self):
        source = load_structure(self.root)
        chapter = source['chapters'][0]
        binding = chapter['proseBinding']
        chapter['proseBinding'] = seal_binding(source['projectId'], self.cid, chapter['filePath'],
            state='managed', generation=binding['generation'] + 1,
            expected_content_digest=binding['expectedContentDigest'])
        write_json(self.source_path, source)
        self.new_review()
        report = self.doctor()
        self.assertEqual(chapter_receipt(self.root, self.cid)['ownership']['state'], 'OWNED_CLEAN')
        self.assertIn('STABLE_CHAPTER_APPROVAL_MISSING', self.codes(report))

    def test_invalid_current_binding_cannot_use_historical_proof(self):
        self.new_review()
        source = load_structure(self.root)
        source['chapters'][0]['proseBinding']['integrityDigest'] = 'damaged'
        write_json(self.source_path, source)
        report = self.doctor()
        self.assertFalse(report['healthy'])
        self.assertIn('OWNERSHIP_BINDING_INVALID', self.codes(report))

    def test_source_invalidation_does_not_revive_current_approval_or_old_b(self):
        packed = self.new_review()
        source = load_structure(self.root)
        fragment = deepcopy(next(r for r in source['fragments'] if r['ownerId'] == self.cid))
        fragment['text'] += '\n核对钥匙归还后的登记顺序。'
        plan = call_operation('source.change-preview', self.root, {
            'changes': [{'action': 'put', 'collection': 'fragments', 'record': fragment}],
            'baseRevision': source['revision'], 'requestId': 'review-source-change',
            'reason': '中性夹具测试已批准的计划修订'})
        call_operation('source.commit', self.root, {'operationId': plan['operationId'],
            'planHash': plan['planHash'], 'approve': True})
        invalidated = self.workflow()['approvals'][self.cid]
        self.assertFalse(invalidated['approved'])
        self.assertTrue(invalidated.get('finalized'))
        report = self.doctor()
        self.assertNotIn('STABLE_CHAPTER_APPROVAL_MISSING', self.codes(report))
        self.assertIn('FACT_INVENTORY_UNAPPROVED', self.codes(report))
        self.assertFalse(self.workflow()['approvals'][self.cid]['approved'])
        for path in (self.root / '工作台/stage_b_review.json', self.root / packed['artifactPath']):
            for bindings in ((), ('--bind', 'sourcePlanHash')):
                with self.subTest(path=path.name, bindings=bindings):
                    before = self.inventory()
                    code, result = self.cli('artifact-register', '--artifact', str(path), *bindings)
                    self.assertEqual(code, 1, result)
                    # This stable chapter has no newly reviewed candidate after
                    # the source edit: registration rejects that dependency first.
                    # test_invalidated_artifact_approval also exercises the replay
                    # guard after fresh A/candidate/diagnostics are supplied.
                    self.assertEqual(result['code'], 'REVIEW_CANDIDATE_REQUIRED')
                    self.assertEqual(self.inventory(), before)
        before = self.inventory()
        code, result = self.cli('artifact-approve', '--artifact', '@' + str(self.root / packed['artifactPath']))
        self.assertEqual((code, result['code']), (1, 'ARTIFACT_SOURCE_CHANGED'))
        self.assertEqual(self.inventory(), before)
        titles = call_operation('workflow.artifact-pack', self.root, {'chapterId': self.cid,
            'artifactType': 'title_candidates', 'payload': {'candidates': [
                {'candidateId': 'later-title', 'title': '交班之后', 'reason': '中性夹具新的标题备选'}]}})
        self.cli_ok('artifact-approve', '--artifact', '@' + str(self.root / titles['artifactPath']))
        approval = self.workflow()['approvals'][self.cid]
        self.assertEqual(approval['artifactHashes'], {'title_candidates': titles['artifactSha256']})
        self.assertTrue(approval.get('finalized'))
        self.assertEqual(approval.get('titleDecision'), self.finalized['approvals'][self.cid]['titleDecision'])
        self.assertIn('FACT_INVENTORY_UNAPPROVED', self.codes(self.doctor()))


if __name__ == '__main__':
    unittest.main()
