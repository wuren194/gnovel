from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import envelope, sha256_text
from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_impact import review_outputs, source_impact
from scripts.core.source_model import snapshot_hash
from scripts.core.source_plan import chapter_plan
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.structure_store import load_structure, validate_structure, StructureError
from scripts.core.structure_transaction import commit
from scripts.core.workflow import approve_artifacts, default_workflow, WorkflowError
from scripts.novelctl import main
from tests.fixtures_current import create_schema5_project
from tests.fixtures_workflow import (evidenced_slots, register_and_approve,
                                     review_ready_project, run_and_approve_diagnostics)
from tests.test_time_dependency_closure import time_plan_source, move_anchor


class InvalidatedArtifactApprovalTests(unittest.TestCase):
    def test_approval_rejects_invalidated_artifacts_before_mutating_workflow(self):
        for kind in ('stage_a_review', 'stage_b_review', 'candidate_prose', 'diagnostics', 'state_candidate'):
            with self.subTest(kind=kind):
                workflow = default_workflow()
                workflow['artifacts']['CH-TEST'] = {kind: {
                    'artifactSha256': 'old', 'invalidatedBy': 'source_dependencies_changed'}}
                before = deepcopy(workflow)
                with self.assertRaises(WorkflowError) as caught:
                    approve_artifacts(workflow, 'CH-TEST', artifact_hashes={kind: 'old'}, approval_mode='manual')
                self.assertEqual(caught.exception.code, 'ARTIFACT_SOURCE_CHANGED')
                self.assertEqual(workflow, before)

    def test_approval_merge_preserves_only_still_approved_current_hashes(self):
        for previously_approved in (True, False):
            with self.subTest(previously_approved=previously_approved):
                workflow = default_workflow()
                workflow['artifacts']['CH-TEST'] = {
                    'stage_a_review': {'artifactSha256': 'a'},
                    'stage_b_review': {'artifactSha256': 'b', 'invalidatedBy': 'source_dependencies_changed'},
                    'diagnostics': {'artifactSha256': 'new-d'},
                    'state_candidate': {'artifactSha256': 'state'},
                }
                workflow['approvals']['CH-TEST'] = {'approved': previously_approved,
                    'artifactHashes': {'stage_a_review': 'a', 'stage_b_review': 'b',
                                       'diagnostics': 'old-d', 'title_candidates': 'removed'}}
                approve_artifacts(workflow, 'CH-TEST', artifact_hashes={'state_candidate': 'state'},
                                  approval_mode='manual')
                self.assertEqual(workflow['approvals']['CH-TEST']['artifactHashes'],
                                 {'state_candidate': 'state', **({'stage_a_review': 'a'} if previously_approved else {})})

    def test_source_invalidation_rejects_malformed_history_without_writes(self):
        before = time_plan_source()
        after = move_anchor(before, 'TA-START', 2)
        for history in (None, {}, ['not a record']):
            with self.subTest(history=history), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workflow = default_workflow()
                workflow['artifacts']['CH-TIME'] = {'stage_b_review': {
                    'artifactSha256': 'prior', 'repairHistory': history}}
                path = root / '.gnovel/workflow.json'
                write_json(path, workflow)
                original = path.read_bytes()
                with self.assertRaises(StructureError) as caught:
                    review_outputs(root, before, after, source_impact(before, after))
                self.assertEqual(caught.exception.code, 'REPAIR_HISTORY_INVALID')
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((root / '.gnovel/source-review.json').exists())

    def test_time_change_cannot_reapprove_or_rebind_old_review_but_new_review_can_finalize(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = create_schema5_project(root, chapter_count=2)
            source, chapter_id, other_id = fixture['structure'], *fixture['chapterIds']
            source['entities'].extend([
                {'entityId': 'TA-REVIEW', 'entityType': 'time_anchor', 'name': '行程起点', 'timeOrdinal': 0},
                {'entityId': 'E-REVIEW', 'entityType': 'event', 'name': '出发',
                 'timeSpec': {'kind': 'relative', 'anchorId': 'TA-REVIEW', 'offset': 3}},
            ])
            source['fragments'][0]['dependsOn'] = ['E-REVIEW']
            source['fieldDefinitions'].append({'fieldId': 'FD-TIME-EVIDENCE', 'key': 'time.evidence',
                'label': '时间依据', 'valueType': 'topic', 'appliesTo': ['time_anchor']})
            source['attributes'].append({'attributeId': 'AT-TIME-EVIDENCE', 'ownerId': 'TA-REVIEW',
                'fieldId': 'FD-TIME-EVIDENCE', 'values': []})
            source['fragments'].append({'fragmentId': 'SF-TIME-EVIDENCE', 'ownerId': 'TA-REVIEW',
                'text': '已核对日期。', 'order': 1, 'contentRole': 'fact', 'contentBindings': ['AT-TIME-EVIDENCE']})
            validate_structure(source)
            write_json(root / '.gnovel/structure.json', source)
            review_ready_project(root)
            workflow_path = root / '.gnovel/workflow.json'

            def cli(action, *args, cid=None):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main(['project', 'workflow', action, '--project-root', str(root),
                                      '--chapter-id', cid or chapter_id, *args])
                return exit_code, json.loads(output.getvalue())

            def approve(kind, artifact_hash):
                return cli('artifact-approve', '--artifact-hash', kind + '=' + artifact_hash)

            def stage_a(cid):
                return {'decision': 'adopt', 'activeSeats': list(range(1, 11)),
                    'sourcePlanHash': chapter_plan(load_structure(root), cid)['sourcePlanHash'],
                    'consensus': [], 'disagreements': [], 'openItems': []}

            text = '他把钥匙放回桌上，合上了登记本。'
            # Current B reports bind the plan explicitly. Source invalidation must
            # still reject both the old file and attempts to rebind its old identity.
            review_payload = {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': evidenced_slots(text),
                              'findings': [], 'finalProseSha256': sha256_text(text)}
            with patch('scripts.core.artifacts.utc_now', return_value='2026-11-04T23:59:00Z'):
                historical_b = envelope('stage_b_review', {**review_payload,
                    'sourcePlanHash': chapter_plan(load_structure(root), chapter_id)['sourcePlanHash']},
                                        project_id=source['projectId'], chapter_id=chapter_id)
            historical_b_path = root / '工作台/B0.json'
            write_json(historical_b_path, historical_b)
            self.assertEqual(cli('artifact-register', '--artifact', str(historical_b_path))[0], 0)
            self.assertEqual(approve('stage_b_review', historical_b['artifactSha256'])[0], 0)
            with patch('scripts.core.artifacts.utc_now', return_value='2026-11-05T00:00:00Z'):
                old_b = register_and_approve(root, chapter_id, 'stage_b_review', review_payload)
            register_and_approve(root, other_id, 'stage_a_review', stage_a(other_id))
            register_and_approve(root, other_id, 'candidate_prose', {'text': text})
            other_b = envelope('stage_b_review', {**review_payload,
                'sourcePlanHash': chapter_plan(load_structure(root), other_id)['sourcePlanHash']},
                               project_id=source['projectId'], chapter_id=other_id, producer='first-review')
            other_b_path = root / '工作台/other-B0.json'
            write_json(other_b_path, other_b)
            self.assertEqual(cli('artifact-register', '--artifact', str(other_b_path), cid=other_id)[0], 0)
            self.assertEqual(cli('artifact-approve', '--artifact-hash',
                                'stage_b_review=' + other_b['artifactSha256'], cid=other_id)[0], 0)
            register_and_approve(root, other_id, 'stage_b_review', review_payload)
            workflow_before = read_json(workflow_path)
            history_before = workflow_before['artifacts'][chapter_id]['stage_b_review']['repairHistory']
            self.assertIn(historical_b['artifactSha256'], {row['artifactSha256'] for row in history_before})
            self.assertTrue(all(not row.get('invalidatedBy') for row in history_before))
            old_a = workflow_before['artifacts'][chapter_id]['stage_a_review']
            old_b_path = Path(workflow_before['artifacts'][chapter_id]['stage_b_review']['artifactPath'])
            historical_bytes = {path: path.read_bytes() for path in (historical_b_path, old_b_path, other_b_path,
                Path(workflow_before['artifacts'][other_id]['stage_b_review']['artifactPath']))}
            self.assertTrue(preflight_finalization(root, chapter_id, skip_title=True).ready)
            source = load_structure(root)
            unrelated_hash = chapter_plan(source, other_id)['sourcePlanHash']
            proposal = preview_proposal_batch(root, sourceHash=snapshot_hash(source), items=[{
                'kind': 'entity', 'recordId': 'TA-REVIEW',
                'patch': [{'op': 'set', 'path': ['timeOrdinal'], 'value': 2}], 'reason': '核对日期',
                'evidence': [{'fragmentId': 'SF-TIME-EVIDENCE', 'quote': '已核对日期。'}],
            }], requestId='move-time', approvalActor='test', approvalEvidence='中性夹具批准')
            receipt = commit(root, operation_id=proposal['operationId'], plan_hash=proposal['planHash'],
                             approved=True, actor='test')
            self.assertEqual(receipt['impact']['approvalInvalidations'], [chapter_id])
            self.assertEqual(chapter_plan(load_structure(root), other_id)['sourcePlanHash'], unrelated_hash)
            for key in ('artifacts', 'approvals'):
                self.assertEqual(read_json(workflow_path)[key][other_id], workflow_before[key][other_id])
            history_after = read_json(workflow_path)['artifacts'][chapter_id]['stage_b_review']['repairHistory']
            self.assertEqual(history_after, [{**row, 'invalidatedBy': 'source_dependencies_changed'}
                                            for row in history_before])
            self.assertEqual({path: path.read_bytes() for path in historical_bytes}, historical_bytes)
            for kind, artifact_hash in (('stage_a_review', old_a['artifactSha256']),
                                        ('stage_b_review', old_b['artifactSha256'])):
                before = workflow_path.read_bytes()
                code, result = approve(kind, artifact_hash)
                self.assertEqual((code, result['code']), (1, 'ARTIFACT_SOURCE_CHANGED'))
                self.assertEqual(workflow_path.read_bytes(), before)
            with self.assertRaises(WorkflowError):
                advance_chapter_status(root, chapter_id, 'draft')

            # Rebuild the other materials, retaining identical prose and the old B.
            register_and_approve(root, chapter_id, 'stage_a_review', stage_a(chapter_id))
            register_and_approve(root, chapter_id, 'candidate_prose', {'text': text})
            run_and_approve_diagnostics(root, chapter_id)
            state = pack_artifact(root, chapterId=chapter_id, artifactType='state_candidate', payload={'changes': []})
            self.assertEqual(approve('state_candidate', state['artifactSha256'])[0], 0)
            self.assertNotIn('stage_b_review', read_json(workflow_path)['approvals'][chapter_id]['artifactHashes'])
            self.assertFalse(preflight_finalization(root, chapter_id, skip_title=True).ready)
            with self.assertRaises(WorkflowError):
                finalize_chapter(root, chapter_id, skip_title=True,
                                 binding_generation=chapter_receipt(root, chapter_id)['bindingGeneration'])
            def reject_old_reviews():
                for old_path in (historical_b_path, old_b_path):
                    for bindings in ((), ('--bind', 'sourcePlanHash')):
                        with self.subTest(path=old_path.name, bindings=bindings):
                            before = workflow_path.read_bytes(), old_path.read_bytes()
                            code, result = cli('artifact-register', '--artifact', str(old_path), *bindings)
                            self.assertEqual(code, 1)
                            self.assertEqual(result['code'], 'ARTIFACT_SOURCE_CHANGED' if bindings
                                             else 'CAUSALITY_REVIEW_PLAN_STALE')
                            self.assertEqual((workflow_path.read_bytes(), old_path.read_bytes()), before)
            reject_old_reviews()

            # An explicitly resubmitted new envelope is legal even when the review's
            # conclusions and quotations are unchanged. The tool cannot prove the
            # author really repeated a semantic review.
            with patch('scripts.core.artifacts.utc_now', return_value='2026-11-05T00:01:00Z'):
                new_b = envelope('stage_b_review', {**review_payload,
                    'sourcePlanHash': chapter_plan(load_structure(root), chapter_id)['sourcePlanHash']},
                                 project_id=source['projectId'], chapter_id=chapter_id)
            self.assertEqual({k: v for k, v in new_b['payload'].items() if k != 'sourcePlanHash'},
                             {k: v for k, v in old_b['payload'].items() if k != 'sourcePlanHash'})
            self.assertNotEqual(new_b['payload']['sourcePlanHash'], old_b['payload']['sourcePlanHash'])
            self.assertNotEqual(new_b['artifactSha256'], old_b['artifactSha256'])
            new_b_path = root / '工作台/复审.json'
            write_json(new_b_path, new_b)
            self.assertEqual(cli('artifact-register', '--artifact', str(new_b_path))[0], 0)
            self.assertEqual(approve('stage_b_review', new_b['artifactSha256'])[0], 0)
            reject_old_reviews()
            for key in ('artifacts', 'approvals'):
                self.assertEqual(read_json(workflow_path)[key][other_id], workflow_before[key][other_id])
            self.assertEqual({path: path.read_bytes() for path in historical_bytes}, historical_bytes)
            self.assertTrue(preflight_finalization(root, chapter_id, skip_title=True).ready)
            finalized = finalize_chapter(root, chapter_id, skip_title=True,
                                        binding_generation=chapter_receipt(root, chapter_id)['bindingGeneration'])
            self.assertEqual(finalized['code'], 'CHAPTER_FINALIZED')


if __name__ == '__main__':
    unittest.main()
