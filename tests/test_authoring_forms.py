"""Direct forms and mechanical state preparation retain material and approval gates."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifact_pack import artifact_request_path, pack_artifact
from scripts.core.artifact_templates import write_review_skeleton
from scripts.core.artifacts import sha256_json, sha256_text
from scripts.core.authoring_forms import prepare_state_candidate, review_form
from scripts.core.jsonio import read_json, write_json
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS
from scripts.core.review_plan import resolve_effective_review_plan
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import StructureError
from tests.fixtures_workflow import evidenced_slots, review_ready_project


class AuthoringFormsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = review_ready_project(self.root, chapter_count=2)
        self.cid, self.other = fixture['chapterIds']

    def workflow(self):
        return read_json(self.root / '.govel/workflow.json')

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file() and p.name != 'structure.lock'}

    def test_stage_a_form_uses_dynamic_seats_and_frozen_plan_without_writing_files(self):
        source = read_json(self.root / '.govel/structure.json')
        chapter = source['chapters'][0]
        chapter.update(reviewClass='ordinary', newLocation=True)
        write_json(self.root / '.govel/structure.json', source)
        before = self.files()
        result = review_form(self.root, chapterId=self.cid, artifactType='stage_a_review', view='full')
        payload = result['payload']
        expected = resolve_effective_review_plan('govel-planner', 'chapter', chapter=chapter)['activeSeats']
        self.assertEqual(payload['activeSeats'], expected)
        self.assertEqual(set(payload['seatReviews']), {str(seat) for seat in expected})
        self.assertEqual(payload['sourcePlanHash'], chapter_plan(source, self.cid)['sourcePlanHash'])
        self.assertEqual(payload['decision'], 'pending')
        self.assertTrue(all(row == {'conclusion': '', 'evidence': []} for row in payload['seatReviews'].values()))
        self.assertNotIn('_guide', payload)
        self.assertFalse(result['registered'])
        self.assertFalse(result['approved'])
        self.assertEqual(self.files(), before)

    def test_stage_b_form_pins_body_and_diagnostics_and_cannot_pass_with_blank_conclusions(self):
        before = self.workflow()
        result = review_form(self.root, chapterId=self.cid, artifactType='stage_b_review', view='full')
        payload = result['payload']
        self.assertEqual(set(payload['slots']), set(STAGE_B_SLOTS) | set(AUXILIARY_GATES))
        self.assertEqual(payload['verdict'], 'INCOMPLETE')
        self.assertEqual(payload['finalProseSha256'], sha256_text('他把钥匙放回桌上，合上了登记本。'))
        self.assertEqual(payload['diagnosticsArtifactSha256'], before['artifacts'][self.cid]['diagnostics']['artifactSha256'])
        payload['verdict'] = 'POLISH_READY'
        with self.assertRaises(ValueError):
            pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', payload=payload)
        self.assertEqual(self.workflow(), before)

    def test_form_cannot_silently_attach_review_to_a_changed_candidate(self):
        payload = review_form(self.root, chapterId=self.cid, artifactType='stage_b_review', view='full')['payload']
        payload.update(verdict='POLISH_READY', slots=evidenced_slots('他把钥匙放回桌上，合上了登记本。'))
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text='他收起登记本，退回门外。')
        before = self.workflow()
        with self.assertRaises(StructureError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', payload=payload)
        self.assertEqual(caught.exception.code, 'ARTIFACT_BINDING_STALE')
        self.assertEqual(self.workflow(), before)

    def test_form_rejects_changed_plan_during_preparation(self):
        from scripts.core import artifact_bindings
        original = artifact_bindings.bind_fields

        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            source = read_json(self.root / '.govel/structure.json')
            source['fragments'][0]['text'] += '材料发生变化。'
            write_json(self.root / '.govel/structure.json', source)
            return result

        with patch.object(artifact_bindings, 'bind_fields', side_effect=change):
            with self.assertRaises(StructureError) as caught:
                review_form(self.root, chapterId=self.cid, artifactType='stage_a_review')
        self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')

    def test_file_skeleton_and_inline_form_have_the_same_blank_seat_payload(self):
        for kind in ('stage_a_review', 'stage_b_review'):
            result = review_form(self.root, chapterId=self.cid, artifactType=kind, view='full')
            saved = write_review_skeleton(self.root, chapterId=self.cid, artifactType=kind,
                                          output=f'工作台/{kind}-blank.json')
            payload = read_json(self.root / saved['path'])
            field = 'seatReviews' if kind == 'stage_a_review' else 'slots'
            self.assertEqual(payload[field], result['payload'][field])
            self.assertEqual(payload['_guide'], result['guide'])

    def test_state_prepare_owns_hashes_and_does_not_grant_approval_or_fact_assessment(self):
        before = self.workflow()['approvals']
        result = prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-first')
        state = read_json(self.root / result['artifactPath'])
        self.assertEqual(state['payload']['changes'], [])
        self.assertEqual(state['payload']['finalProseSha256'], sha256_text('他把钥匙放回桌上，合上了登记本。'))
        self.assertEqual(set(result['bindings']), {'baseStateSha256', 'designSha256', 'sourcePlanHash', 'finalProseSha256'})
        self.assertEqual(self.workflow()['approvals'], before)
        self.assertFalse(result['approvalPerformed'])
        self.assertFalse(result['semanticReviewComplete'])
        self.assertEqual(result['factsAssessment'], 'not_performed')
        self.assertTrue(result['approvalAction']['requiresExplicitApproval'])
        self.assertEqual(result['resumeAction']['arguments'], {'requestId': 'state-first'})

    def test_state_request_only_retry_reuses_receipt_and_does_not_write_again(self):
        result = prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-retry')
        before = self.files()
        retry = prepare_state_candidate(self.root, requestId='state-retry')
        self.assertTrue(retry['replayed'])
        self.assertEqual(retry['artifactSha256'], result['artifactSha256'])
        self.assertEqual(self.files(), before)

    def test_state_request_rejects_other_chapter_and_changed_dependencies(self):
        prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-pinned')
        with self.assertRaises(StructureError) as caught:
            prepare_state_candidate(self.root, chapterId=self.other, requestId='state-pinned')
        self.assertEqual(caught.exception.code, 'ARTIFACT_REQUEST_REUSED')
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text='他收好钥匙。')
        before = self.files()
        with self.assertRaises(StructureError) as caught:
            prepare_state_candidate(self.root, requestId='state-pinned')
        self.assertEqual(caught.exception.code, 'ARTIFACT_REQUEST_STALE')
        self.assertEqual(self.files(), before)

    def test_state_request_failure_leaves_no_partial_registration_and_can_retry(self):
        before = self.files()

        def fail(phase, relative=None):
            if phase == 'write' and relative.startswith('.govel/artifact-requests/'):
                raise RuntimeError('state write interrupted')

        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError):
                prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-interrupted')
        from tests.fixtures_transaction import assert_preserved_rollback
        assert_preserved_rollback(self, self.root, before, self.files())
        with self.assertRaises(StructureError) as caught:
            prepare_state_candidate(self.root, requestId='state-interrupted')
        self.assertEqual(caught.exception.code, 'STATE_RESUME_UNAVAILABLE')
        self.assertTrue(prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-interrupted')['registered'])

    def test_replay_cannot_restore_an_invalidated_state_artifact(self):
        prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-invalidated')
        workflow = self.workflow()
        workflow['artifacts'][self.cid]['state_candidate']['invalidatedBy'] = 'later reviewed operation'
        write_json(self.root / '.govel/workflow.json', workflow)
        before = self.files()
        with self.assertRaises(StructureError) as caught:
            prepare_state_candidate(self.root, requestId='state-invalidated')
        self.assertEqual(caught.exception.code, 'STATE_REQUEST_SUPERSEDED')
        self.assertEqual(self.files(), before)

    def test_state_request_tampering_and_missing_candidate_are_rejected(self):
        prepare_state_candidate(self.root, chapterId=self.cid, requestId='state-tamper')
        source = read_json(self.root / '.govel/structure.json')
        path = self.root / artifact_request_path(source['projectId'], 'state-prepare:state-tamper')
        data = read_json(path)
        data['inputIdentity']['chapterId'] = self.other
        write_json(path, data)
        with self.assertRaises(StructureError) as caught:
            prepare_state_candidate(self.root, requestId='state-tamper')
        self.assertEqual(caught.exception.code, 'STATE_REQUEST_INVALID')
        data['inputIdentity'] = []
        data['requestHash'] = sha256_json({key: value for key, value in data.items() if key != 'requestHash'})
        write_json(path, data)
        with self.assertRaises(StructureError) as caught:
            prepare_state_candidate(self.root, requestId='state-tamper')
        self.assertEqual(caught.exception.code, 'STATE_REQUEST_INVALID')
        before = self.files()
        with self.assertRaises(ValueError):
            prepare_state_candidate(self.root, chapterId=self.other, requestId='state-missing-candidate')
        self.assertEqual(self.files(), before)


if __name__ == '__main__':
    unittest.main()
