"""Repair identities, unresolved carry-forward and explicit review deltas."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.candidate_patch import patch_candidate
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.repair_review import review_input
from scripts.core.review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
from scripts.core.structure_store import StructureError
from scripts.core.workflow import require_approved_artifact, WorkflowError
from tests.fixtures_workflow import review_ready_project


class RepairContinuationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        # Scope this fixture to semantic repair findings, not setup diagnostics.
        workflow = read_json(self.root/'.govel/workflow.json')
        workflow['artifacts'][self.cid].pop('diagnostics', None)
        workflow['approvals'][self.cid]['artifactHashes'].pop('diagnostics', None)
        write_json(self.root/'.govel/workflow.json', workflow)
        self.text = 'Before.\r\nAlpha opens door.\r\nSame.\r\nSame.\r\nBeta closes book.\r\nAfter.'
        self.candidate(self.text)
        self.payload = {'verdict': 'REPAIR_REQUIRED', 'repairScope': 'local',
            'slots': {slot: {'conclusion': 'Explicit test review'} for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
            'findings': [{'severity': 'S2', 'ruleId': rule, 'summary': 'Review ' + rule,
                          'location': quote, 'repairSuggestion': 'Judge the revised action'}
                         for rule, quote in [('door', 'Alpha opens door.'), ('book', 'Beta closes book.')]]}
        self.report = self.register(self.payload)

    def candidate(self, text):
        (self.root / '工作台/repair-body.txt').write_bytes(text.encode())
        return pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', input='工作台/repair-body.txt')

    def register(self, payload, request=None):
        write_json(self.root / '工作台/repair-review.json', payload)
        return pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review',
                             input='工作台/repair-review.json', requestId=request)

    def material(self, **args):
        rows, cursor, first = [], None, None
        while True:
            page = call_operation('workflow.repair-context', self.root, {'chapterId': self.cid,
                **args, **({'cursor': cursor} if cursor else {})})
            first = first or page
            rows.extend(page['items'])
            if not page['hasMore']:
                return first, rows
            cursor = page['nextCursor']

    def issues(self):
        return {row['finding']['ruleId']: row for row in self.material()[1] if row['source'] == 'issuePreview'}

    def repair(self, rule='door', replacement='Alpha opens a much wider window.'):
        row = self.issues()[rule]
        return {'issueId': row['issueId'], 'anchorRef': row['location']['spans'][0]['anchorRef'], 'replacement': replacement}

    def delta(self):
        payload = review_input(self.material()[0]['repairRef'])
        payload['slots'] = deepcopy(self.payload['slots'])
        for slot in payload['slots'].values():
            slot['evidence'] = [{'text': 'Before.'}]
        return payload

    def decision(self, row, decision='resolved'):
        return {'issueId': row['issueId'], 'decision': decision, 'reason': 'Explicit current-version judgment',
                'quotes': [{'text': 'Before.'}]}

    def test_identity_survives_seat_notes_and_unrelated_reordering(self):
        before = self.issues()
        self.payload['slots']['reader']['conclusion'] += ' with additional explanation'
        self.payload['findings'].reverse()
        self.register(self.payload)
        after = self.issues()
        self.assertEqual({k: r['issueId'] for k, r in before.items()}, {k: r['issueId'] for k, r in after.items()})
        raw = [json.loads(row['text']) for row in self.material()[1] if row['source'] == 'stage_b_review']
        self.assertTrue(all(row['evidenceId'].startswith('stage_b_review:') for row in raw))

    def test_one_patch_retains_both_issues_and_exact_before_after(self):
        old = self.issues()
        patch_candidate(self.root, patches=[self.repair()])
        page, rows = self.material()
        self.assertEqual(page['coverage']['totalFindings'], 2)
        self.assertEqual(page['coverage']['historicalPendingCount'], 2)
        current = self.issues()
        self.assertEqual(current['book']['issueId'], old['book']['issueId'])
        self.assertEqual(current['book']['location']['spans'][0]['text'], 'Beta closes book.')
        evidence = [json.loads(row['text']) for row in rows if row['source'] == 'stage_b_review']
        door = next(row for row in evidence if row['finding']['ruleId'] == 'door')
        self.assertEqual(door['comparison']['before'][0]['text'], 'Alpha opens door.')
        self.assertEqual(door['comparison']['after'][0]['text'], 'Alpha opens a much wider window.')
        self.assertTrue(door['comparison']['patchReceipts'])
        patch_candidate(self.root, patches=[self.repair('book', 'Beta writes a note.')])
        self.assertEqual(self.material()[0]['coverage']['totalFindings'], 2)
        self.assertFalse(page['coverage']['semanticFixVerified'])

    def test_omitting_findings_from_later_report_does_not_silently_close_them(self):
        payload = deepcopy(self.payload); payload['findings'] = []
        self.register(payload)
        self.assertEqual(set(self.issues()), {'door', 'book'})

    def complete_payload(self):
        return {'verdict': 'POLISH_READY',
            'slots': {slot: {'conclusion': 'Explicit current-version assessment', 'evidence': [{'text': 'Before.'}]}
                      for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
            'factAssessment': {'status': 'no_change'}, 'findings': []}

    def test_fresh_empty_completed_report_cannot_drop_old_blocking_findings(self):
        from scripts.core.authoring_forms import review_form
        form = review_form(self.root, chapterId=self.cid, artifactType='stage_b_review', view='full')
        self.assertTrue(form['payload'].get('repairRef') or form['payload'].get('findings'))
        with self.assertRaises(StructureError) as caught:
            self.register(self.complete_payload())
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_INCOMPLETE')
        self.assertEqual(len(caught.exception.details['pendingIssueIds']), 2)

    def test_current_resolution_survives_unrelated_new_report(self):
        payload = self.delta()
        payload.update(self.complete_payload())
        payload['repairDecisions'] = [self.decision(row) for row in self.issues().values()]
        self.register(payload)
        self.assertEqual(self.issues(), {})
        fresh = self.complete_payload()
        fresh['slots']['reader']['conclusion'] = 'Additional reader observation with unchanged input'
        self.register(fresh)
        self.assertEqual(self.issues(), {})
        self.candidate(self.text)  # Same bytes, new artifact identity.
        self.assertEqual(set(self.issues()), {'door', 'book'})

    def test_s3_advice_is_retained_without_blocking_completion(self):
        payload = self.delta()
        payload.update(self.complete_payload())
        payload['repairDecisions'] = [self.decision(row) for row in self.issues().values()]
        payload['findings'] = [{'findingId': 'ADVICE-ONE', 'severity': 'S3', 'ruleId': 'reader',
                               'summary': 'Optional alternate wording', 'location': 'Before.'}]
        self.register(payload)
        review = self.delta()
        review.update(self.complete_payload())
        result = self.register(review)
        saved = read_json(self.root / result['artifactPath'])['payload']
        self.assertEqual([row['findingId'] for row in saved['findings']], ['ADVICE-ONE'])
        self.assertEqual(saved['repairReview']['pendingIssueIds'], [])
        self.assertEqual(saved['verdict'], 'POLISH_READY')

    def test_same_id_severity_downgrade_cannot_erase_prior_blocker(self):
        old = self.issues()
        payload = deepcopy(self.payload)
        for finding in payload['findings']:
            finding.update(findingId=old[finding['ruleId']]['issueId'], severity='S3')
        self.register(payload)
        self.assertEqual({row['finding']['severity'] for row in self.issues().values()}, {'S2'})
        with self.assertRaises(StructureError) as caught:
            self.register(self.complete_payload())
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_INCOMPLETE')

    def test_explicit_repair_finding_does_not_replace_required_disposition(self):
        old = self.issues()
        payload = self.delta()
        payload.update(self.complete_payload())
        payload['findings'] = [{**row['finding'], 'findingId': row['issueId'], 'severity': 'S3'}
                               for row in old.values()]
        before = (self.root / '.govel/workflow.json').read_bytes()
        with self.assertRaises(StructureError) as caught:
            self.register(payload)
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_INCOMPLETE')
        self.assertEqual(set(caught.exception.details['pendingIssueIds']), {row['issueId'] for row in old.values()})
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), before)

    def test_old_completed_envelope_cannot_resolve_later_reopened_issue(self):
        from scripts.core.structure_store import load_structure
        from scripts.core.workflow import record_artifact
        payload = self.delta(); payload.update(self.complete_payload())
        payload['repairDecisions'] = [self.decision(row) for row in self.issues().values()]
        result = self.register(payload)
        old_path = self.root / result['artifactPath']; old = read_json(old_path)
        reopened = deepcopy(self.payload)
        for finding in reopened['findings']:
            decision = next(row for row in old['payload']['repairReview']['decisions'] if row['finding']['ruleId'] == finding['ruleId'])
            finding['findingId'] = decision['issueId']
        self.register(reopened)
        workflow = read_json(self.root / '.govel/workflow.json')
        before = deepcopy(workflow)
        with self.assertRaises(WorkflowError) as caught:
            record_artifact(workflow, self.cid, old, structure=load_structure(self.root),
                            artifact_path=str(old_path), project_root=self.root)
        self.assertEqual(caught.exception.code, 'REPAIR_HISTORICAL_RESOLUTION')
        self.assertEqual(workflow, before)
        self.assertEqual(set(self.issues()), {'door', 'book'})

    def test_old_partial_resolution_cannot_erase_later_reopened_issue(self):
        from scripts.core.structure_store import load_structure
        from scripts.core.workflow import record_artifact
        payload = self.delta()
        payload['repairDecisions'] = [self.decision(self.issues()['door'])]
        result = self.register(payload)
        old_path = self.root / result['artifactPath']; old = read_json(old_path)
        self.assertEqual(old['payload']['verdict'], 'INCOMPLETE')
        reopened = deepcopy(self.payload)
        reopened['findings'][0]['findingId'] = old['payload']['repairReview']['decisions'][0]['issueId']
        self.register(reopened)
        workflow = read_json(self.root / '.govel/workflow.json')
        before = deepcopy(workflow)
        with self.assertRaises(WorkflowError) as caught:
            record_artifact(workflow, self.cid, old, structure=load_structure(self.root),
                            artifact_path=str(old_path), project_root=self.root)
        self.assertEqual(caught.exception.code, 'REPAIR_HISTORICAL_RESOLUTION')
        self.assertEqual(workflow, before)
        self.assertEqual(set(self.issues()), {'door', 'book'})

    def test_restoring_old_candidate_identity_does_not_revive_resolved_history(self):
        from scripts.core.structure_store import load_structure
        from scripts.core.workflow import record_artifact
        workflow_path = self.root / '.govel/workflow.json'
        original = read_json(workflow_path)['artifacts'][self.cid]['candidate_prose']
        candidate_path = Path(original['artifactPath'])
        candidate = read_json(candidate_path)
        resolved = self.delta(); resolved.update(self.complete_payload())
        resolved['repairDecisions'] = [self.decision(row) for row in self.issues().values()]
        self.register(resolved)
        # Archive the resolved report without changing its candidate inputs.
        self.register(self.complete_payload())
        self.assertEqual(self.issues(), {})
        self.candidate(self.text + ' Changed candidate.')
        workflow = read_json(workflow_path)
        record_artifact(workflow, self.cid, candidate, structure=load_structure(self.root),
                        artifact_path=str(candidate_path), project_root=self.root)
        write_json(workflow_path, workflow)
        self.assertEqual(set(self.issues()), {'door', 'book'})
        with self.assertRaises(StructureError) as caught:
            self.register(self.complete_payload())
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_INCOMPLETE')

    def test_two_subrange_patches_map_the_whole_original_finding(self):
        row = self.repair()
        patch_candidate(self.root, patches=[
            {**row, 'range': {'start': 0, 'end': 5}, 'replacement': 'A taller Alpha'},
            {**row, 'range': {'start': 12, 'end': 16}, 'replacement': 'window'}])
        evidence = [json.loads(item['text']) for item in self.material()[1] if item['source'] == 'stage_b_review']
        after = next(item for item in evidence if item['finding']['ruleId'] == 'door')['comparison']['after']
        self.assertEqual(after[0]['text'], 'A taller Alpha opens window.')

    def test_ambiguous_old_line_is_not_reused_as_a_current_locator(self):
        self.payload['findings'][0].update(location='Same.', line=3)
        self.register(self.payload)
        self.candidate('Different.\r\nSame.\r\nSame.\r\nBeta closes book.')
        current = self.issues()['door']
        self.assertEqual(current['location']['state'], 'needs_location')
        self.assertEqual(current['location']['spans'], [])

    def test_unlocated_carried_finding_does_not_gain_an_anchor_from_its_old_line(self):
        self.payload['findings'][0].update(severity='S3', location='', line=3)
        self.register(self.payload)
        self.candidate('First.\nSecond.\nWrong line.\nBeta closes book.')
        self.register(self.delta())
        current = self.issues()['door']
        self.assertEqual(current['location']['state'], 'needs_location')
        self.assertEqual(current['location']['spans'], [])

    def test_partial_review_packs_unaddressed_findings_without_manual_fields(self):
        payload = self.delta()
        payload['repairDecisions'] = [self.decision(self.issues()['door'])]
        result = self.register(payload)
        saved = read_json(self.root / result['artifactPath'])['payload']
        self.assertEqual([row['ruleId'] for row in saved['findings']], ['book'])
        self.assertEqual(len(saved['repairReview']['pendingIssueIds']), 1)
        self.assertFalse(result['approvalPerformed'])
        self.assertEqual(set(self.issues()), {'book'})

    def test_no_implicit_pass_and_all_seats_still_required(self):
        payload = self.delta(); payload['verdict'] = 'POLISH_READY'
        with self.assertRaises(StructureError) as caught:
            self.register(payload)
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_INCOMPLETE')
        payload['repairDecisions'] = [self.decision(row) for row in self.issues().values()]
        payload['slots'].pop('organization')
        with self.assertRaises(ValueError) as caught:
            self.register(payload)
        self.assertEqual(caught.exception.code, 'REVIEW_SLOTS_MISSING')

    def test_explicit_complete_review_is_not_approval_and_later_prose_reopens_evidence(self):
        payload = self.delta(); payload['verdict'] = 'POLISH_READY'
        payload['behaviorCoverage'] = {'relationship': 'not_applicable', 'living': 'not_applicable'}
        payload['factAssessment'] = {'status': 'no_change'}
        payload['repairDecisions'] = [self.decision(row) for row in self.issues().values()]
        result = self.register(payload, 'review-once')
        self.assertEqual(self.material()[0]['coverage']['totalFindings'], 0)
        self.assertFalse(result['approvalPerformed'])
        with self.assertRaises(WorkflowError):
            require_approved_artifact(read_json(self.root / '.govel/workflow.json'), self.cid, 'stage_b_review')
        from scripts.core.transport_cache import _directory
        (_directory() / (payload['repairRef'].replace(':', '-') + '.json')).unlink()
        replay = pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review',
                               input='工作台/repair-review.json', requestId='review-once')
        self.assertTrue(replay['replayed'])
        self.candidate(self.text + ' Changed.')
        self.assertEqual(self.material()[0]['coverage']['historicalPendingCount'], 2)

    def test_old_reference_refuses_new_candidate_and_changed_history(self):
        payload = self.delta()
        self.candidate(self.text + ' Changed.')
        with self.assertRaises(StructureError) as caught:
            self.register(payload)
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_STALE')
        path = self.root / self.report['artifactPath']; original = path.read_bytes()
        path.write_text('{}')
        files = lambda: {p.relative_to(self.root).as_posix(): p.read_bytes()
                         for p in self.root.rglob('*') if p.is_file() and p.name != 'structure.lock'}
        before = files()
        page, rows = self.material()
        self.assertEqual(files(), before)
        self.assertGreater(page['coverage']['unavailableReportCount'], 0)
        self.assertFalse(page['reviewInputAvailable'])
        self.assertNotIn('reviewInputRef', page)
        self.assertEqual(page['reviewInputBlocker'], 'REPAIR_HISTORY_UNAVAILABLE')
        missing = next(json.loads(row['text']) for row in rows if row['source'] == 'unavailableReport'
                       and json.loads(row['text'])['artifactSha256'] == self.report['artifactSha256'])
        from scripts.core.artifacts import sha256_bytes
        self.assertEqual(missing['restore'], {'path': path.relative_to(self.root).as_posix(),
                                             'expectedFileSha256': sha256_bytes(original)})
        with self.assertRaises(StructureError) as caught:
            call_operation('workflow.review-form', self.root, {'chapterId': self.cid,
                'artifactType': 'stage_b_review', 'repairRef': page['repairRef']})
        self.assertEqual(caught.exception.code, 'FACT_HISTORY_UNAVAILABLE')
        rejected = review_input(page['repairRef']); rejected['verdict'] = 'POLISH_READY'
        rejected['slots'] = deepcopy(self.payload['slots'])
        with self.assertRaises(StructureError) as caught: self.register(rejected)
        self.assertEqual(caught.exception.code, 'REPAIR_REVIEW_INCOMPLETE')
        path.write_bytes(original)
        action = page['recovery']['recheckAction']
        restored = call_operation(action['operation'], self.root, action['arguments'])
        self.assertEqual(restored['coverage']['unavailableReportCount'], 0)
        self.assertTrue(restored['reviewInputAvailable'])
        self.assertIn('reviewInputRef', restored)
        self.assertNotEqual(restored['repairRef'], page['repairRef'])

    def test_late_report_change_is_guarded_without_overwriting_external_edit(self):
        import scripts.core.artifact_pack as packing
        payload = self.delta()
        original = packing.record_artifact
        path = self.root / self.report['artifactPath']
        changed = path.read_bytes() + b'\n'
        before_workflow = (self.root / '.govel/workflow.json').read_bytes()

        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(changed)
            return result

        with patch.object(packing, 'record_artifact', side_effect=mutate), self.assertRaises(StructureError):
            self.register(payload)
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), before_workflow)

    def test_schema_decisions_and_explicit_finding_ids_match_runtime(self):
        from scripts.core.project import TOOLBOX_ROOT
        from scripts.core.repair_review import DECISIONS
        schema = read_json(TOOLBOX_ROOT / 'shared/schemas/artifacts.json')['$defs']['stage_b_review']['properties']
        self.assertEqual(set(schema['repairReview']['properties']['decisions']['items']['properties']['decision']['enum']), DECISIONS)
        for finding in self.payload['findings']:
            finding['findingId'] = 'duplicate'
        with self.assertRaises(ValueError) as caught:
            self.register(self.payload)
        self.assertEqual(caught.exception.code, 'REVIEW_FINDING_ID_DUPLICATE')

    def test_repair_skeleton_contains_no_inherited_seat_conclusions(self):
        page, _ = self.material()
        call_operation('workflow.review-skeleton', self.root, {'chapterId': self.cid, 'artifactType': 'stage_b_review',
            'repairRef': page['repairRef'], 'output': '工作台/empty-repair-review.json'})
        payload = read_json(self.root / '工作台/empty-repair-review.json')
        self.assertEqual(payload['repairRef'], page['repairRef'])
        self.assertEqual(payload['repairDecisions'], [])
        self.assertTrue(all(row['conclusion'] == '' for row in payload['slots'].values()))

    def test_request_only_resume_reuses_completed_candidate_and_does_not_need_anchor_cache(self):
        row = self.repair()
        with patch('scripts.core.mechanical_evidence.run_diagnostics', side_effect=OSError('temporary failure')):
            first = patch_candidate(self.root, patches=[row], requestId='resume-me', advanceLifecycle=True,
                                    diagnostics={'mode': 'plot', 'intensityLevel': 1})
        self.assertFalse(first['ok'])
        self.assertEqual(first['completedStages'], ['candidate-registered'])
        from scripts.core.transport_cache import _directory
        (_directory() / (row['anchorRef'].replace(':', '-') + '.json')).unlink()
        second = call_operation('workflow.patch-candidate', self.root, {'requestId': 'resume-me'})
        self.assertTrue(second['ok'], second)
        self.assertEqual(first['artifactSha256'], second['artifactSha256'])
        self.assertTrue(second['replayed'])
        self.assertFalse(second['approvalPerformed'])

    def test_missing_patch_receipt_retains_issues_but_never_guesses_changed_location(self):
        result = patch_candidate(self.root, patches=[self.repair()])
        (self.root / result['patchReceiptPath']).unlink()
        page, _ = self.material()
        self.assertEqual(page['coverage']['totalFindings'], 2)
        self.assertGreater(page['coverage']['unavailableReportCount'], 0)
        self.assertFalse(page['reviewInputAvailable'])
        self.assertNotIn('reviewInputRef', page)
        self.assertEqual(self.issues()['door']['location']['state'], 'needs_location')
        self.assertEqual(self.issues()['book']['location']['spans'][0]['text'], 'Beta closes book.')
