"""Closeout explains independent failures without granting permission or taking prose."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.chapter_closeout import closeout_chapter, prepare_closeout
from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import run_diagnostics
from scripts.core.operations import call_operation, operations, validate_arguments
from scripts.core.project import query_policy
from scripts.core.structure_store import StructureError
from tests.fixtures_workflow import review_ready_project


class CloseoutGuidanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'book'
        environment = patch.dict(os.environ, {'GNOVEL_LOG_MODE': 'off'})
        environment.start()
        self.addCleanup(environment.stop)
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def inventory(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def workflow(self, change):
        path = self.root / '.gnovel/workflow.json'
        value = read_json(path)
        change(value)
        write_json(path, value)
        return value

    def actions(self, report, key='conditionalActions'):
        return [action for item in report['blockers']
                for action in item.get('recovery', {}).get(key, [])]

    def check_action_shapes(self, report):
        specs = {row.id: row for row in operations()}
        for key in ('readActions', 'conditionalActions'):
            for action in self.actions(report, key):
                spec = specs[action['operation']]
                self.assertEqual(action['effects'], spec.effects)
                if not action['missingInputs']:
                    validate_arguments(spec.input_schema, action['arguments'])
                if key == 'readActions':
                    self.assertEqual(spec.effects, 'read')
                    self.assertTrue(action['executable'])
                    self.assertEqual(action['execution'], 'read_only')
                else:
                    self.assertFalse(action['executable'])
                    self.assertTrue(action['when'])

    def occupy_target(self):
        source = read_json(self.root / '.gnovel/structure.json')
        chapter = next(row for row in source['chapters'] if row['chapterId'] == self.cid)
        entry = read_json(self.root / '.gnovel/workflow.json')['artifacts'][self.cid]['candidate_prose']
        candidate = read_json(Path(entry['artifactPath']))
        target = self.root / chapter['filePath']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(candidate['payload']['text'].encode())
        return chapter, target

    def refresh_diagnostics(self):
        previous = read_json(self.root / '.gnovel/workflow.json')['artifacts'][self.cid]['diagnostics']['artifactSha256']
        # A disposable invalidation forces the real runner to publish a new report.
        self.workflow(lambda value: value['artifacts'][self.cid]['diagnostics'].update(
            invalidatedBy={'reason': 'synthetic rerun requested'}))
        with patch('scripts.core.artifacts.utc_now', return_value='2099-01-01T00:00:00+00:00'):
            result = run_diagnostics(self.root, chapter_id=self.cid, register=True, view='model')
        self.assertFalse(result['approved'])
        self.assertNotEqual(result['artifactSha256'], previous)
        return previous, result

    def test_independent_missing_state_title_stale_diagnostics_and_unknown_target_are_all_visible(self):
        def remove_state(value):
            value['artifacts'][self.cid].pop('state_candidate')
            value['approvals'][self.cid]['artifactHashes'].pop('state_candidate')
        self.workflow(remove_state)
        source = read_json(self.root / '.gnovel/structure.json')
        source['bookName'] += '（合成变更）'
        write_json(self.root / '.gnovel/structure.json', source)
        chapter, target = self.occupy_target()
        before = self.inventory()
        result = prepare_closeout(self.root, chapterId=self.cid)
        codes = {row['code'] for row in result['blockers']}
        self.assertTrue({'FINAL_ARTIFACTS_MISSING', 'TITLE_DECISION_REQUIRED',
                         'DIAGNOSTICS_STALE', 'PROSE_TARGET_NOT_OWNED'} <= codes, result)
        missing = next(row for row in result['blockers'] if row['code'] == 'FINAL_ARTIFACTS_MISSING')
        self.assertIn('state_candidate', missing['details']['missing'])
        self.assertIn('title_candidates', missing['details']['missing'])
        self.assertIsNone(result['runAction'])
        self.assertEqual(result['guidance']['proseTarget']['path'], chapter['filePath'])
        self.assertEqual(result['guidance']['proseTarget']['state'], 'UNKNOWN_TARGET')
        target_issue = next(row for row in result['blockers'] if row['code'] == 'PROSE_TARGET_NOT_OWNED')
        self.assertTrue(target_issue['recovery']['needsAuthorDecision'])
        self.assertEqual(target_issue['recovery']['conditionalActions'], [])
        self.assertEqual(target_issue['details']['currentLocator'], chapter['filePath'])
        self.assertTrue(target.exists())
        self.check_action_shapes(result)
        self.assertEqual(self.inventory(), before)

    def test_generated_state_action_has_request_id_and_retries_without_approval(self):
        def remove_state(value):
            value['artifacts'][self.cid].pop('state_candidate')
            value['approvals'][self.cid]['artifactHashes'].pop('state_candidate')
        self.workflow(remove_state)
        before = self.inventory()
        prepared = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        action = next(row for row in self.actions(prepared) if row['operation'] == 'workflow.state-prepare')
        self.assertTrue(action['arguments']['requestId'])
        self.assertEqual(action['arguments']['chapterId'], self.cid)
        self.assertEqual(action['effects'], 'candidate-write')
        self.assertFalse(action['executable'])
        again = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        repeated = next(row for row in self.actions(again) if row['operation'] == 'workflow.state-prepare')
        self.assertEqual(repeated['arguments'], action['arguments'])
        self.assertEqual(self.inventory(), before)
        result = call_operation(action['operation'], self.root, action['arguments'])
        self.assertTrue(result['registered'])
        self.assertFalse(result['approved'])
        after = self.inventory()
        replay = call_operation(action['operation'], self.root, action['arguments'])
        self.assertEqual(replay['artifactSha256'], result['artifactSha256'])
        self.assertEqual(self.inventory(), after)
        with self.assertRaises(StructureError) as caught:
            closeout_chapter(self.root, requestId='old-closeout', prepareRef=prepared['prepareRef'])
        self.assertEqual(caught.exception.code, 'CLOSEOUT_INPUTS_STALE')

    def test_refreshed_diagnostics_approval_points_only_to_the_exact_new_report(self):
        previous, diagnostics = self.refresh_diagnostics()
        before = self.inventory()
        prepared = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        issue = next(row for row in prepared['blockers'] if row['code'] == 'ARTIFACT_APPROVAL_HASH_MISMATCH'
                     and row['details'].get('artifactType') == 'diagnostics')
        self.assertEqual(issue['details']['reason'], 'approved_version_changed')
        self.assertEqual(issue['details']['registeredArtifactSha256'], diagnostics['artifactSha256'])
        self.assertEqual(issue['details']['approvedArtifactSha256'], previous)
        self.assertEqual(Path(issue['details']['artifactPath']), self.root / diagnostics['artifactPath'])
        actions = issue['recovery']['conditionalActions']
        self.assertEqual(len(actions), 1)
        approval = actions[0]
        self.assertEqual(approval['operation'], 'project.workflow.artifact-approve')
        self.assertEqual(approval['arguments']['artifactHash'], ['diagnostics=' + diagnostics['artifactSha256']])
        self.assertTrue(approval['needsApproval'])
        self.assertFalse(approval['executable'])
        self.assertEqual(self.inventory(), before)
        call_operation(approval['operation'], self.root, approval['arguments'])
        current = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertNotIn('ARTIFACT_APPROVAL_HASH_MISMATCH', {row['code'] for row in current['blockers']})

    def test_stale_diagnostics_does_not_recommend_approving_its_old_report(self):
        self.refresh_diagnostics()
        source = read_json(self.root / '.gnovel/structure.json')
        source['bookName'] += '（输入已变）'
        write_json(self.root / '.gnovel/structure.json', source)
        prepared = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertIn('DIAGNOSTICS_STALE', {row['code'] for row in prepared['blockers']})
        self.assertFalse(any(row['operation'] == 'project.workflow.artifact-approve'
                             and any(item.startswith('diagnostics=') for item in row['arguments'].get('artifactHash', []))
                             for row in self.actions(prepared)))

    def test_stage_b_can_still_cover_its_exact_diagnostics_without_a_separate_approval(self):
        self.workflow(lambda value: value['approvals'][self.cid]['artifactHashes'].pop('diagnostics'))
        result = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertTrue(result['ok'], result['blockers'])
        row = next(row for row in result['receipt']['artifacts'] if row['artifactType'] == 'diagnostics')
        self.assertEqual(row['approvalCoveredBy'], 'stage_b_review')
        self.assertFalse(row['approvalRequired'])

    def test_invalidated_artifact_never_recovers_credit_from_its_old_matching_approval(self):
        value = self.workflow(lambda value: value['artifacts'][self.cid]['state_candidate'].update(
            invalidatedBy={'reason': 'synthetic source dependency invalidation'}))
        old_hash = value['approvals'][self.cid]['artifactHashes']['state_candidate']
        self.assertTrue(value['approvals'][self.cid]['approved'])
        self.assertEqual(old_hash, value['artifacts'][self.cid]['state_candidate']['artifactSha256'])
        before = self.inventory()
        prepared = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertFalse(prepared['ok'])
        issue = next(row for row in prepared['blockers'] if row['code'] == 'ARTIFACT_SOURCE_CHANGED'
                     and row['details'].get('artifactType') == 'state_candidate')
        self.assertEqual(issue['details']['reason'], 'dependencies_invalidated')
        state = next(row for row in prepared['receipt']['artifacts'] if row['artifactType'] == 'state_candidate')
        self.assertFalse(state['approved'])
        self.assertTrue(state['invalidated'])
        self.assertFalse(any(row['operation'] == 'project.workflow.artifact-approve'
                             and 'state_candidate=' + old_hash in row['arguments'].get('artifactHash', [])
                             for row in self.actions(prepared)))
        self.assertEqual(self.inventory(), before)

    def test_bad_stage_b_file_does_not_hide_other_closeout_failures(self):
        value = read_json(self.root / '.gnovel/workflow.json')
        Path(value['artifacts'][self.cid]['stage_b_review']['artifactPath']).write_text('{invalid json')
        self.occupy_target()
        before = self.inventory()
        result = prepare_closeout(self.root, chapterId=self.cid)
        self.assertFalse(result['ok'])
        self.assertIn('PROSE_TARGET_NOT_OWNED', {row['code'] for row in result['blockers']})
        self.assertIn('TITLE_DECISION_REQUIRED', {row['code'] for row in result['blockers']})
        self.assertTrue(any(row['details'].get('artifactType') == 'stage_b_review' for row in result['blockers']))
        self.assertEqual(result['factInventory']['status'], 'unknown')
        self.assertFalse(result['factsAssessed'])
        self.assertEqual(self.inventory(), before)

    def test_internal_closeout_receipt_keeps_all_blockers_when_public_receipt_is_paged(self):
        self.occupy_target()
        policy = deepcopy(query_policy())
        policy['maxBytes']['detail'] = 2500
        before = self.inventory()
        with patch('scripts.core.project.query_policy', return_value=policy):
            public = chapter_receipt(self.root, self.cid, view='model')
            self.assertTrue(public['responseCompacted'])
            prepared = prepare_closeout(self.root, chapterId=self.cid)
        self.assertIn('PROSE_TARGET_NOT_OWNED', {row['code'] for row in prepared['blockers']})
        self.assertIn('TITLE_DECISION_REQUIRED', {row['code'] for row in prepared['blockers']})
        self.assertIsNone(prepared['runAction'])
        self.assertEqual(self.inventory(), before)

    def test_receipt_and_preflight_share_the_same_precise_blockers(self):
        self.refresh_diagnostics()
        receipt = chapter_receipt(self.root, self.cid, skip_title=True)
        preflight = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertEqual(receipt['blockers'], preflight.blockers)
        self.check_action_shapes(receipt)

    def test_ready_run_action_has_request_id_and_uses_inline_continuity_without_target_sync(self):
        source = read_json(self.root / '.gnovel/structure.json')
        target = self.root / source['chapters'][0]['filePath']
        self.assertFalse(target.exists())
        prepared = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertTrue(prepared['ok'], prepared['blockers'])
        self.assertFalse(prepared['guidance']['continuity']['confirmationFileRequired'])
        self.assertTrue(prepared['guidance']['proseTarget']['finalizeWritesFormalProse'])
        action = prepared['runAction']
        self.assertTrue(action['arguments']['requestId'])
        self.assertFalse(action['executable'])
        self.assertFalse(action['approvalGranted'])
        result = call_operation(action['operation'], self.root, {
            **action['arguments'], 'summary': {'resolution': 'not_required', 'reason': '合成夹具直接核对正式正文，暂不更新摘要。'},
            'approveContinuity': True, 'prepareNext': False})
        self.assertTrue(result['readyForContinuation'], result)
        self.assertTrue(target.is_file())
        self.assertEqual(result['completedStages'], ['finalize', 'continuity'])
        self.assertFalse(result['artifactApprovalPerformed'])

    def test_noop_closeout_keeps_old_request_frozen_and_offers_unused_id_for_new_decisions(self):
        prepared = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        first_args = {**prepared['runAction']['arguments'], 'prepareNext': False}
        first = call_operation('workflow.closeout', self.root, first_args)
        self.assertEqual(first['completedStages'], ['finalize'])
        stable = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        no_op_args = {**stable['runAction']['arguments'], 'prepareNext': False}
        same = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertEqual(same['runAction']['arguments'], stable['runAction']['arguments'])
        no_op = call_operation('workflow.closeout', self.root, no_op_args)
        self.assertEqual(no_op['code'], 'CLOSEOUT_INPUT_REQUIRED')
        self.assertEqual(no_op['completedStages'], [])
        frozen = self.inventory()
        fresh = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        fresh_again = prepare_closeout(self.root, chapterId=self.cid, skipTitle=True)
        self.assertNotEqual(fresh['runAction']['arguments']['requestId'], no_op_args['requestId'])
        self.assertEqual(fresh_again['runAction']['arguments'], fresh['runAction']['arguments'])
        self.assertEqual(self.inventory(), frozen)
        decision = {'summary': {'resolution': 'not_required', 'reason': '合成复核已明确本次直接使用正式正文。'},
                    'approveContinuity': True, 'prepareNext': False}
        with self.assertRaises(StructureError) as caught:
            call_operation('workflow.closeout', self.root, {**no_op_args, **decision})
        self.assertEqual(caught.exception.code, 'CLOSEOUT_REQUEST_REUSED')
        replay = call_operation('workflow.closeout', self.root, {'requestId': no_op_args['requestId']})
        self.assertEqual(replay['completedStages'], [])
        self.assertFalse(replay['approvalPerformed'])
        self.assertEqual(self.inventory(), frozen)
        completed = call_operation('workflow.closeout', self.root, {**fresh['runAction']['arguments'], **decision})
        self.assertTrue(completed['readyForContinuation'], completed)
        self.assertEqual(completed['completedStages'], ['continuity'])
        for path, data in frozen.items():
            if path.startswith('.gnovel/closeout-requests/'):
                self.assertEqual((self.root / path).read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
