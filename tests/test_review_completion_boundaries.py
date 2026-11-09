"""Complete-envelope entrypoints enforce actual seats without inventing reviews."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.artifacts import envelope, file_sha256
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.jsonio import write_json
from scripts.core.review_plan import chapter_stage_a_plan, require_stage_a_completion
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import StructureError
from scripts.core.workflow import approve_artifacts, default_workflow, record_artifact
from tests.fixtures_current import create_schema5_project
from tests.fixtures_workflow import assessed_review_payload


class ReviewCompletionBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = create_schema5_project(self.root, chapter_count=1)
        self.source = fixture['structure']; self.cid = fixture['chapterIds'][0]
        self.source['chapters'][0]['status'] = 'planning_required'
        write_json(self.root / '.gnovel/structure.json', self.source)
        self.payload = assessed_review_payload(self.source, self.cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)),
            'sourcePlanHash': chapter_plan(self.source, self.cid)['sourcePlanHash']})

    def historical(self, payload):
        artifact = envelope('stage_a_review', payload, project_id=self.source['projectId'], chapter_id=self.cid)
        path = self.root / '工作台/unproven-a.json'; write_json(path, artifact)
        workflow = default_workflow(); workflow['approvedChapterIds'] = [self.cid]
        workflow['artifacts'] = {self.cid: {'stage_a_review': {'artifactSha256': artifact['artifactSha256'],
            'artifactFileSha256': file_sha256(path), 'artifactPath': str(path)}}}
        return artifact, path, workflow

    def test_missing_seat_cannot_register_approve_or_advance(self):
        payload = deepcopy(self.payload); payload['activeSeats'] = [4]
        payload['seatReviews'] = {'4': payload['seatReviews']['4']}
        artifact, path, workflow = self.historical(payload)
        with self.assertRaises(StructureError) as caught:
            record_artifact(workflow, self.cid, artifact, structure=self.source,
                            artifact_path=str(path), project_root=self.root)
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_MISSING')
        with self.assertRaises(StructureError) as caught:
            approve_artifacts(workflow, self.cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']},
                              approval_mode='manual', project_root=self.root)
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_MISSING')
        workflow['approvals'] = {self.cid: {'approved': True, 'artifactHashes': {'stage_a_review': artifact['artifactSha256']}}}
        write_json(self.root / '.gnovel/workflow.json', workflow)
        with self.assertRaises(StructureError) as caught:
            advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_MISSING')

    def test_duplicate_empty_and_wrong_quote_fail_without_mutation(self):
        for kind in ('duplicate', 'conclusion', 'quote'):
            payload = deepcopy(self.payload)
            if kind == 'duplicate': payload['activeSeats'].append(1)
            if kind == 'conclusion': payload['seatReviews']['1']['conclusion'] = ''
            if kind == 'quote': payload['seatReviews']['1']['evidence'] = [{'text': 'This is not in the plan.'}]
            before = deepcopy(payload)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                require_stage_a_completion(self.source, self.cid, payload)
            self.assertEqual(payload, before)

    def test_pending_is_storable_but_cannot_advance(self):
        payload = deepcopy(self.payload); payload['decision'] = 'pending'; payload['activeSeats'] = [4]
        payload.pop('seatReviews')
        artifact, path, workflow = self.historical(payload)
        record_artifact(workflow, self.cid, artifact, structure=self.source, artifact_path=str(path), project_root=self.root)
        approve_artifacts(workflow, self.cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']},
                          approval_mode='manual', project_root=self.root)
        write_json(self.root / '.gnovel/workflow.json', workflow)
        with self.assertRaises(ValueError) as caught:
            advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(caught.exception.code, 'STAGE_A_NOT_ADOPTED')

    def test_ordinary_plan_allows_exact_current_reduced_seats(self):
        self.source['chapters'][0]['reviewClass'] = 'ordinary'
        seats = chapter_stage_a_plan(self.source, self.cid)['activeSeats']
        self.assertEqual(seats, [1, 2, 4, 5, 6, 7, 10])
        payload = deepcopy(self.payload); payload['activeSeats'] = seats
        payload['seatReviews'] = {str(s): payload['seatReviews'][str(s)] for s in seats}
        require_stage_a_completion(self.source, self.cid, payload)
        self.source['chapters'][0]['newLocation'] = True
        with self.assertRaises(StructureError) as caught:
            require_stage_a_completion(self.source, self.cid, payload)
        self.assertEqual(caught.exception.details['missing'], [9])

    def test_review_approval_requires_current_project_material(self):
        artifact, path, workflow = self.historical(self.payload)
        path.write_text('{"corrupt":true}')
        with self.assertRaises(ValueError) as caught:
            approve_artifacts(workflow, self.cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']},
                              approval_mode='manual')
        self.assertEqual(caught.exception.code, 'APPROVAL_MATERIAL_REQUIRED')
        self.assertNotIn(self.cid, workflow['approvals'])
        with self.assertRaises(ValueError):
            approve_artifacts(workflow, self.cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']},
                              approval_mode='manual', project_root=self.root)
        self.assertNotIn(self.cid, workflow['approvals'])

    def test_independent_seat_errors_are_aggregated_with_exact_fields(self):
        payload = deepcopy(self.payload)
        payload['seatReviews']['1']['conclusion'] = ''
        payload['seatReviews']['2']['evidence'] = []
        payload['seatReviews']['4']['evidence'] = [{'text': 'not the current plan'}]
        with self.assertRaises(StructureError) as caught:
            require_stage_a_completion(self.source, self.cid, payload)
        fields = {row['field'] for row in caught.exception.details['issues']}
        self.assertEqual(fields, {'seatReviews.1.conclusion', 'seatReviews.2.evidence', 'seatReviews.4.evidence[0]'})
        payload['activeSeats'] = [True]
        with self.assertRaises(StructureError) as caught:
            require_stage_a_completion(self.source, self.cid, payload)
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_INVALID')
        self.assertEqual(caught.exception.details['notChecked'], ['seatReviews'])
