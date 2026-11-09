"""Content packaging reuses binding, registration, path and write-set guarantees."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import file_sha256, sha256_text
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS, validate_review_artifact
from scripts.core.structure_store import StructureError
from tests.fixtures_workflow import review_ready_project, evidenced_slots


class ArtifactPackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def workflow(self):
        return read_json(self.root / '.gnovel/workflow.json')

    def pack(self, kind, payload):
        path = self.root / '工作台/input.json'
        write_json(path, payload)
        return pack_artifact(self.root, chapterId=self.chapter, artifactType=kind, input='工作台/input.json')

    def stage_b(self):
        return {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': evidenced_slots('他把钥匙放回桌上，合上了登记本。'), 'findings': []}

    def test_prose_bytes_are_preserved_and_can_be_approved_by_file(self):
        raw = '第一行。\r\n第二行。\r\n'.encode()
        (self.root / '工作台/prose.txt').write_bytes(raw)
        before = self.workflow()['approvals']
        result = call_operation('workflow.artifact-pack', self.root, {
            'chapterId': self.chapter, 'artifactType': 'candidate_prose', 'input': '工作台/prose.txt'})
        artifact = read_json(self.root / result['artifactPath'])
        self.assertEqual(artifact['payload']['text'].encode(), raw)
        self.assertTrue(result['registered'])
        self.assertFalse(result['approved'])
        self.assertEqual(self.workflow()['approvals'], before)
        entry = self.workflow()['artifacts'][self.chapter]['candidate_prose']
        self.assertEqual(entry['artifactFileSha256'], file_sha256(self.root / result['artifactPath']))
        call_operation('project.workflow.artifact-approve', self.root,
                       {'chapterId': self.chapter, 'artifact': ['@' + str(self.root / result['artifactPath'])]})
        self.assertEqual(self.workflow()['approvals'][self.chapter]['artifactHashes']['candidate_prose'], artifact['artifactSha256'])

    def test_review_content_is_unchanged_and_binding_is_computed(self):
        payload = self.stage_b()
        before = self.workflow()['approvals']
        result = self.pack('stage_b_review', payload)
        artifact = read_json(self.root / result['artifactPath'])
        validate_review_artifact(artifact)
        self.assertEqual({k: v for k, v in artifact['payload'].items() if k not in {'finalProseSha256', 'diagnosticsArtifactSha256', 'sourcePlanHash', 'proseBindingGeneration', 'candidateArtifactSha256'}}, payload)
        self.assertEqual(artifact['payload']['candidateArtifactSha256'],
                         self.workflow()['artifacts'][self.chapter]['candidate_prose']['artifactSha256'])
        from scripts.core.source_plan import chapter_plan
        self.assertEqual(artifact['payload']['sourcePlanHash'],
                         chapter_plan(read_json(self.root / '.gnovel/structure.json'), self.chapter)['sourcePlanHash'])
        self.assertEqual(artifact['payload']['diagnosticsArtifactSha256'], self.workflow()['artifacts'][self.chapter]['diagnostics']['artifactSha256'])
        self.assertEqual(artifact['payload']['finalProseSha256'], sha256_text('他把钥匙放回桌上，合上了登记本。'))
        self.assertEqual(artifact['payload']['proseBindingGeneration'],
                         read_json(self.root / '.gnovel/structure.json')['chapters'][0]['proseBinding']['generation'])
        self.assertEqual(self.workflow()['approvals'], before)

    def test_missing_review_slot_is_not_filled_with_a_pass(self):
        payload = self.stage_b(); del payload['slots']['organization']
        before = self.workflow()
        with self.assertRaises(ValueError) as caught:
            self.pack('stage_b_review', payload)
        self.assertEqual(caught.exception.code, 'REVIEW_SLOTS_MISSING')
        self.assertEqual(self.workflow(), before)

    def test_explicit_stale_binding_is_not_silently_rebound(self):
        payload = self.stage_b(); payload['finalProseSha256'] = '0' * 64
        with self.assertRaises(StructureError) as caught:
            self.pack('stage_b_review', payload)
        self.assertEqual(caught.exception.code, 'ARTIFACT_BINDING_STALE')

    def test_tampered_registered_prose_is_rejected(self):
        path = Path(self.workflow()['artifacts'][self.chapter]['candidate_prose']['artifactPath'])
        artifact = read_json(path); artifact['payload']['text'] = '外部改稿'; write_json(path, artifact)
        before = self.workflow()
        with self.assertRaises(ValueError):
            self.pack('stage_b_review', self.stage_b())
        self.assertEqual(self.workflow(), before)

    def test_title_ids_and_state_bindings_are_assembled_without_conclusions(self):
        titles = self.pack('title_candidates', {'candidates': [{'title': '门后', 'reason': '悬念'}]})
        self.assertEqual(read_json(self.root / titles['artifactPath'])['payload']['candidates'],
                         [{'title': '门后', 'reason': '悬念', 'candidateId': 'title-1'}])
        state = self.pack('state_candidate', {'changes': []})
        artifact = read_json(self.root / state['artifactPath'])
        validate_review_artifact(artifact)
        self.assertEqual(artifact['payload']['changes'], [])
        self.assertEqual(set(state['bindings']), {'baseStateSha256', 'designSha256', 'sourcePlanHash', 'finalProseSha256'})

    def test_stage_a_reports_missing_required_seats(self):
        with self.assertRaises(StructureError) as caught:
            self.pack('stage_a_review', {'decision': 'adopt', 'activeSeats': [1]})
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_MISSING')
        result = self.pack('stage_a_review', {'decision': 'pending', 'activeSeats': list(range(1, 11))})
        self.assertEqual(read_json(self.root / result['artifactPath'])['payload']['decision'], 'pending')

    def test_rejects_manufactured_diagnostics_and_non_workbench_inputs(self):
        with self.assertRaises(StructureError):
            self.pack('diagnostics', {'passed': True})
        with self.assertRaises(StructureError):
            pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose', input='.gnovel/workflow.json')

    def test_transaction_failure_restores_workflow_and_removes_new_artifact(self):
        write_json(self.root / '工作台/input.json', self.stage_b())
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

        def fail(phase, relative=None):
            if phase == 'write' and relative.startswith('工作台/'):
                raise RuntimeError('injected interruption')

        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError):
                pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review', input='工作台/input.json')
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        from tests.fixtures_transaction import assert_preserved_rollback
        assert_preserved_rollback(self, self.root, before, after)

    def test_changed_input_vetoes_commit_without_restoring_external_edit(self):
        input_path = self.root / '工作台/input.json'
        write_json(input_path, self.stage_b())
        before = self.workflow()

        def change(phase, relative=None):
            if phase == 'prepared':
                input_path.write_text('外部改动', encoding='utf-8')

        with patch('scripts.core.write_sets.checkpoint', side_effect=change):
            with self.assertRaises(StructureError):
                pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review', input='工作台/input.json')
        self.assertEqual(input_path.read_text(), '外部改动')
        self.assertEqual(self.workflow(), before)

    def test_stage_a_binding_uses_authored_source_plan(self):
        from tests.current_lifecycle import CurrentLifecycleFixture
        helper = CurrentLifecycleFixture(); helper.setUp(); self.addCleanup(helper.doCleanups)
        root, cid = helper.read_ready_schema5()
        before = (root/'.gnovel/structure.json').read_bytes()
        result = pack_artifact(root, chapterId=cid, artifactType='stage_a_review',
            payload={'decision': 'pending', 'activeSeats': list(range(1, 11))})
        self.assertEqual(set(result['bindings']), {'sourcePlanHash'})
        self.assertEqual((root/'.gnovel/structure.json').read_bytes(), before)
