"""Frozen form references reduce mechanical input without changing review gates."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.authoring_forms import review_form
from scripts.core.authoring_submission import submit_artifacts
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.review_form_edits import apply_semantic_edits
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.transport_cache import get
from tests.fixtures_workflow import evidenced_slots, review_ready_project

BODY = '他把钥匙放回桌上，合上了登记本。'
COVERAGE = {'relationship': 'not_applicable', 'living': 'not_applicable'}


class ReviewFormEditsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = review_ready_project(self.root, chapter_count=2)
        self.cid, self.other = fixture['chapterIds']

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file() and p.name != 'structure.lock'}

    def form(self, kind='stage_b_review', **kwargs):
        return review_form(self.root, chapterId=self.cid, artifactType=kind, **kwargs)

    def edits(self, form):
        edits = deepcopy(form['fields'])
        edits['behaviorCoverage'] = deepcopy(COVERAGE)
        if form['artifactType'] == 'stage_b_review':
            edits.update(verdict='POLISH_READY', slots=evidenced_slots(BODY), factAssessment={'status': 'no_change'})
        else:
            source = load_structure(self.root)
            text = next(row['text'] for row in source['fragments']
                        if row['ownerId'] == self.cid and row['contentRole'] == 'plan')
            edits['decision'] = 'adopt'
            for row in edits['seatReviews'].values():
                row.update(conclusion='合成计划仅核对钥匙归还，未涉及承重关系或持续生活安排。', evidence=[{'text': text}])
        return edits

    def arguments(self, form, edits=None, request='form-submit'):
        return {'chapterId': self.cid, 'requestId': request, 'artifacts': [{
            'artifactType': form['artifactType'], 'formRef': form['formRef'],
            'edits': self.edits(form) if edits is None else edits}]}

    def assert_rejected_unchanged(self, args, code=None, root=None):
        before = self.files()
        with self.assertRaises(ValueError) as caught:
            submit_artifacts(root or self.root, **args)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.files(), before)

    def cache_path(self, reference):
        from scripts.core.transport_cache import _directory
        return _directory() / (reference.replace(':', '-') + '.json')

    def test_model_has_only_semantic_template_and_full_view_preserves_exact_base(self):
        before = self.files()
        for kind in ('stage_a_review', 'stage_b_review'):
            with self.subTest(kind=kind):
                form = self.form(kind)
                full = self.form(kind, view='full')
                self.assertEqual(full['formRef'], form['formRef'])
                self.assertNotIn('payload', form)
                self.assertNotIn('activeSeats', form['fields'])
                self.assertNotIn('sourcePlanHash', form['fields'])
                self.assertNotIn('fieldSchemas', form['guide'])
                if kind == 'stage_b_review':
                    self.assertNotIn('finalProseSha256', form['fields'])
                    self.assertEqual(len(form['fields']['slots']), 12)
                else:
                    self.assertEqual(set(form['fields']['seatReviews']), {str(n) for n in full['payload']['activeSeats']})
                self.assertEqual(form['fullAction'], {'operation': 'task.snapshot', 'arguments': {'reference': form['formRef']}})
                self.assertEqual(get(self.root, form['formRef'], kind='review-form')['report']['payload'], full['payload'])
                self.assertFalse(form['registered'])
                self.assertFalse(form['approved'])
                self.assertFalse(form['semanticReviewComplete'])
                self.assertLess(len(json.dumps(form, ensure_ascii=False).encode()),
                                len(json.dumps(full, ensure_ascii=False).encode()))
        self.assertEqual(self.files(), before)

    def test_full_snapshot_pages_preserve_payload_and_hide_dependencies(self):
        form = self.form()
        chunks, cursor = [], None
        while True:
            page = call_operation('task.snapshot', self.root, {'reference': form['formRef'], 'maxBytes': 1800,
                                  **({'cursor': cursor} if cursor else {})})
            chunks.extend(row['text'] for row in page['materials'])
            self.assertFalse(page['freshnessRevalidated'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        report = json.loads(''.join(chunks))
        snapshot = get(self.root, form['formRef'], kind='review-form')
        self.assertEqual(report, snapshot['report'])
        self.assertIn('payload', report)
        self.assertNotIn('dependencies', report)
        self.assertNotIn('inputs', report)
        self.assertFalse(report['semanticReviewComplete'])

    def test_large_model_form_pages_semantic_fields_without_repeating_or_stripping_bindings(self):
        from scripts.core.project import query_policy

        form = self.form()
        edits = self.edits(form)
        edits.update(factAssessment={'status': 'changes'}, append={'findings': [{
            'findingId': 'FACT-LARGE', 'severity': 'info', 'ruleId': 'CHAPTER-PIPELINE',
            'summary': '此合成事实说明用于验证完整分页。' * 900,
            'factChange': {'status': 'pending', 'requestId': 'large-fact',
                           'reason': '后续章节需要持续持有链。', 'evidence': [{'text': BODY}]}}]})
        self.assertTrue(submit_artifacts(self.root, **self.arguments(form, edits, request='large-source'))['ok'])
        policy = query_policy()
        before = self.files()
        compact = self.form()
        self.assertTrue(compact['responseCompacted'])
        self.assertFalse(compact['fieldsIncluded'])
        self.assertEqual(compact['readAction']['arguments'], {'reference': compact['formRef'], 'view': 'model'})
        chunks, cursor, count = [], None, 0
        first_cursor = None
        while True:
            page = call_operation('task.snapshot', self.root, {**compact['readAction']['arguments'],
                'maxBytes': policy['maxBytes']['detail'], **({'cursor': cursor} if cursor else {})})
            count += 1
            chunks.extend(row['text'] for row in page['materials'])
            first_cursor = first_cursor or page['nextCursor']
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        delivered = json.loads(''.join(chunks))
        self.assertGreater(count, 1)
        self.assertEqual(delivered['formRef'], compact['formRef'])
        self.assertNotIn('payload', delivered)
        self.assertEqual(delivered['fields']['findings'][0]['findingId'], 'FACT-LARGE')
        self.assertEqual(len(delivered['fields']['slots']), 12)
        self.assertNotIn('sourcePlanHash', delivered['fields'])
        self.assertNotIn('dependencies', delivered)
        self.assertEqual(self.files(), before)
        with self.assertRaises(StructureError) as caught:
            call_operation('task.snapshot', self.root, {'reference': compact['formRef'], 'view': 'full',
                           'cursor': first_cursor, 'maxBytes': policy['maxBytes']['detail']})
        self.assertEqual(caught.exception.code, 'CURSOR_QUERY_MISMATCH')
        # Follow only the pageable model form: no caller reads payload or removes
        # its hashes. Actual judgments still have to fill every offered seat.
        edits = self.edits(delivered)
        edits['factAssessment'] = {'status': 'changes'}
        result = submit_artifacts(self.root, **self.arguments(delivered, edits, request='large-reviewed'))
        self.assertTrue(result['submissionComplete'], result)

    def test_too_small_budget_rejects_metadata_instead_of_exceeding_the_limit(self):
        from scripts.core.project import query_policy
        policy = deepcopy(query_policy())
        policy['maxBytes']['detail'] = 256
        before = self.files()
        with patch('scripts.core.project.query_policy', return_value=policy), self.assertRaises(StructureError) as caught:
            self.form()
        self.assertEqual(caught.exception.code, 'AUTHORING_REPORT_BUDGET_INVALID')
        self.assertEqual(caught.exception.details['maxBytes'], 256)
        self.assertEqual(self.files(), before)

    def test_public_a_semantic_submission_keeps_actual_seats_and_plan_binding(self):
        source = load_structure(self.root)
        source['chapters'][0].update(reviewClass='ordinary', newLocation=True)
        write_json(self.root / '.govel/structure.json', source)
        form = call_operation('workflow.review-form', self.root, {'chapterId': self.cid, 'artifactType': 'stage_a_review'})
        frozen = get(self.root, form['formRef'], kind='review-form')['report']['payload']
        result = call_operation('workflow.submit', self.root, self.arguments(form))
        self.assertTrue(result['submissionComplete'], result)
        artifact = read_json(self.root / result['stages'][0]['artifactPath'])
        self.assertEqual(artifact['payload']['activeSeats'], frozen['activeSeats'])
        self.assertEqual(artifact['payload']['sourcePlanHash'], frozen['sourcePlanHash'])
        self.assertFalse(result['approvalsPerformed'])

    def test_b_semantic_submission_reuses_existing_gates_and_replays_without_cache(self):
        form = self.form()
        args = self.arguments(form)
        before_approvals = read_json(self.root / '.govel/workflow.json')['approvals']
        result = submit_artifacts(self.root, **args)
        self.assertTrue(result['submissionComplete'], result)
        frozen = get(self.root, form['formRef'], kind='review-form')['report']['payload']
        artifact = read_json(self.root / result['stages'][0]['artifactPath'])
        for key in ('finalProseSha256', 'sourcePlanHash', 'proseBindingGeneration', 'diagnosticsArtifactSha256'):
            self.assertEqual(artifact['payload'][key], frozen[key])
        before = self.files()
        self.cache_path(form['formRef']).unlink()
        for supplied in (args, {'requestId': args['requestId']}):
            replay = submit_artifacts(self.root, **supplied)
            self.assertTrue(replay['submissionComplete'], replay)
            self.assertTrue(replay['results'][0]['replayed'])
            self.assertEqual(self.files(), before)
        self.assertEqual(read_json(self.root / '.govel/workflow.json')['approvals'], before_approvals)
        changed = deepcopy(args)
        changed['artifacts'][0]['edits']['slots']['logic']['conclusion'] += 'changed'
        self.assert_rejected_unchanged(changed, 'SUBMISSION_REQUEST_REUSED')

    def test_failed_pack_keeps_frozen_inputs_and_can_resume_without_cache(self):
        form = self.form()
        args = self.arguments(form, request='interrupted')
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=RuntimeError('interrupted')):
            failed = submit_artifacts(self.root, **args)
        self.assertFalse(failed['submissionComplete'])
        self.cache_path(form['formRef']).unlink()
        result = submit_artifacts(self.root, requestId='interrupted')
        self.assertTrue(result['submissionComplete'], result)
        record_path = next((self.root / '.govel/artifact-submissions').glob('*.json'))
        record = read_json(record_path)
        self.assertNotIn('formRef', record['arguments']['artifacts'][0])
        self.assertNotIn('edits', record['arguments']['artifacts'][0])
        self.assertIn('payload', record['arguments']['artifacts'][0])
        self.assertIn('callerArgumentsSha256', record)

    def test_resuming_after_material_changes_does_not_rebind_stored_edits(self):
        form = self.form()
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=RuntimeError('interrupted')):
            submit_artifacts(self.root, **self.arguments(form, request='stale-resume'))
        self.cache_path(form['formRef']).unlink()
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text='他收起钥匙。')
        before = self.files()
        result = submit_artifacts(self.root, requestId='stale-resume')
        self.assertFalse(result['submissionComplete'])
        self.assertEqual(self.files(), before)

    def test_form_retains_historical_artifact_guards_before_submit_and_on_resume(self):
        previous = self.form()
        review = self.edits(previous)
        review['slots']['logic']['conclusion'] = '这一轮合成审校明确核对了交接动作。'
        self.assertTrue(submit_artifacts(self.root, **self.arguments(previous, review, request='history-create'))['ok'])
        workflow = read_json(self.root / '.govel/workflow.json')
        history = workflow['artifacts'][self.cid]['stage_b_review']['repairHistory']
        old_path = self.root / history[0]['artifactPath']
        form = self.form()
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=RuntimeError('interrupted')):
            failed = submit_artifacts(self.root, **self.arguments(form, request='history-resume'))
        self.assertFalse(failed['ok'])
        old_path.write_text('{}', encoding='utf-8')
        self.assert_rejected_unchanged(self.arguments(form, request='new-history-use'), 'REVIEW_FORM_STALE')
        before = self.files()
        resumed = submit_artifacts(self.root, requestId='history-resume')
        self.assertFalse(resumed['ok'])
        self.assertEqual(resumed['error']['code'], 'TRANSACTION_GUARD_DRIFT')
        self.assertEqual(self.files(), before)

    def test_failure_during_request_freeze_does_not_leave_submission_record(self):
        form = self.form()
        before = self.files()

        def fail(phase, relative=None):
            if phase == 'write' and relative.startswith('.govel/artifact-submissions/'):
                raise RuntimeError('interrupted before submission freeze committed')

        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(RuntimeError):
            submit_artifacts(self.root, **self.arguments(form))
        from tests.fixtures_transaction import assert_preserved_rollback
        assert_preserved_rollback(self, self.root, before, self.files())
        with self.assertRaises(StructureError) as caught:
            submit_artifacts(self.root, requestId='form-submit')
        self.assertEqual(caught.exception.code, 'SUBMISSION_RESUME_INVALID')

    def test_blank_missing_and_wrong_quote_never_become_completed_review(self):
        form = self.form()
        variants = [deepcopy(form['fields']), self.edits(form), self.edits(form), self.edits(form)]
        variants[0]['verdict'] = 'POLISH_READY'
        del variants[1]['slots']['organization']
        variants[2]['slots']['logic']['evidence'] = [{'text': '没有出现在当前正文中的引文。'}]
        variants[3]['behaviorCoverage'] = {'relationship': 'unassessed'}
        workflow = (self.root / '.govel/workflow.json').read_bytes()
        for index, edits in enumerate(variants):
            result = submit_artifacts(self.root, **self.arguments(form, edits, request=f'incomplete-{index}'))
            self.assertFalse(result['submissionComplete'], result)
            self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), workflow)
            self.assertNotEqual(result['error']['code'], 'REVIEW_FORM_STALE')

    def test_bindings_unknown_seats_and_forged_receipts_are_not_writable(self):
        form = self.form()
        for value in ({'sourcePlanHash': '0' * 64}, {'finalProseSha256': '0' * 64},
                      {'activeSeats': []}, {'repairRef': 'gn:repair:' + '0' * 32},
                      {'repairReview': {}}, {'slots': {'invented': {'conclusion': 'pass'}}},
                      {'slots': {'logic': {'artifactSha256': '0' * 64}}}, {'factAssessment': {'status': 'pass'}},
                      {'behaviorCoverage': {'relationship': 'skip'}}, {'slots': None}):
            with self.subTest(value=value):
                self.assert_rejected_unchanged(self.arguments(form, value))

    def test_payload_and_form_ref_variants_are_exclusive(self):
        form = self.form()
        args = self.arguments(form)
        for extra in ({'payload': {}}, {'text': BODY}, {'input': '工作台/report.json'}):
            wrong = deepcopy(args)
            wrong['artifacts'][0].update(extra)
            with self.subTest(extra=extra):
                self.assert_rejected_unchanged(wrong, 'ARGUMENTS_INVALID')
        wrong = deepcopy(args)
        wrong['artifacts'][0]['edits'] = [{'path': '/verdict', 'value': 'POLISH_READY'}]
        self.assert_rejected_unchanged(wrong, 'ARGUMENTS_INVALID')

    def test_reference_is_project_chapter_and_stage_scoped(self):
        form = self.form()
        args = self.arguments(form)
        wrong = deepcopy(args)
        wrong['chapterId'] = self.other
        self.assert_rejected_unchanged(wrong, 'REVIEW_FORM_SCOPE_INVALID')
        wrong = deepcopy(args)
        wrong['artifacts'][0]['artifactType'] = 'stage_a_review'
        self.assert_rejected_unchanged(wrong, 'REVIEW_FORM_SCOPE_INVALID')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            other_cid = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            wrong = deepcopy(args)
            wrong['chapterId'] = other_cid
            self.assert_rejected_unchanged(wrong, 'TRANSPORT_SNAPSHOT_UNAVAILABLE', root=root)

    def test_missing_and_tampered_references_never_rebind(self):
        for mutation in ('delete', 'tamper'):
            form = self.form()
            path = self.cache_path(form['formRef'])
            if mutation == 'delete':
                path.unlink()
            else:
                value = read_json(path)
                value['value']['report']['payload']['sourcePlanHash'] = '0' * 64
                write_json(path, value)
                path.chmod(0o600)
            self.assert_rejected_unchanged(self.arguments(form), 'TRANSPORT_SNAPSHOT_UNAVAILABLE')

    def test_plan_seat_authorization_and_candidate_drift_reject_original_form(self):
        def change_source(root):
            source = load_structure(root)
            source['fragments'][0]['text'] += '合成材料变更。'
            write_json(root / '.govel/structure.json', source)

        def change_seats(root):
            source = load_structure(root)
            source['chapters'][0].update(reviewClass='ordinary')
            write_json(root / '.govel/structure.json', source)

        def change_authorization(root):
            workflow = read_json(root / '.govel/workflow.json')
            workflow['approvals'][self.cid]['approved'] = False
            write_json(root / '.govel/workflow.json', workflow)

        def change_candidate(root):
            pack_artifact(root, chapterId=self.cid, artifactType='candidate_prose', text='他收起钥匙。')

        for mutate in (change_source, change_seats, change_authorization, change_candidate):
            with self.subTest(mutation=mutate.__name__):
                form = self.form()
                args = self.arguments(form)
                paths = [self.root / '.govel' / name for name in ('structure.json', 'workflow.json')]
                original = {path: path.read_bytes() for path in paths}
                try:
                    mutate(self.root)
                    self.assert_rejected_unchanged(args, 'REVIEW_FORM_STALE')
                finally:
                    for path, raw in original.items():
                        path.write_bytes(raw)

    def test_mutation_between_resolve_and_submission_freeze_is_rejected(self):
        from scripts.core import authoring_submission as module
        original = module._assert_current
        changed = []

        def mutate(root, record):
            source = load_structure(root)
            source['fragments'][0]['text'] += '冻结途中发生更改。'
            write_json(root / '.govel/structure.json', source)
            changed.append(self.files())
            return original(root, record)

        form = self.form()
        with patch.object(module, '_assert_current', side_effect=mutate), self.assertRaises(StructureError):
            submit_artifacts(self.root, **self.arguments(form))
        self.assertEqual(self.files(), changed[0])
        self.assertFalse((self.root / '.govel/artifact-submissions').exists())

    def test_existing_facts_cannot_be_deleted_renamed_or_passed_implicitly(self):
        form = self.form()
        fact = {'findingId': 'FACT-KEY', 'severity': 'info', 'ruleId': 'CHAPTER-PIPELINE',
                'summary': '钥匙交接将影响后续持有链。', 'factChange': {'status': 'pending',
                'requestId': 'key-fact', 'reason': '后续需要归还事实。', 'evidence': [{'text': BODY}]}}
        edits = self.edits(form)
        edits.update(factAssessment={'status': 'changes'}, append={'findings': [fact]})
        self.assertTrue(submit_artifacts(self.root, **self.arguments(form, edits, request='fact-first'))['ok'])
        form = self.form()
        self.assertEqual(form['fields']['findings'][0]['findingId'], 'FACT-KEY')
        for update in ([{'findingId': 'renamed', 'summary': 'new'}],
                       [{'findingId': 'FACT-KEY', 'ruleId': 'another-rule'}],
                       [{'findingId': 'FACT-KEY', 'summary': 'one'}, {'findingId': 'FACT-KEY', 'summary': 'two'}]):
            self.assert_rejected_unchanged(self.arguments(form, {'findings': update}))
        edits = self.edits(form)
        edits['findings'] = []
        result = submit_artifacts(self.root, **self.arguments(form, edits, request='fact-drop'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'FACT_ASSESSMENT_CONFLICT')
        edits = self.edits(form)
        edits['findings'] = [{'findingId': 'FACT-KEY', 'factChange': {'status': 'not_required',
            'reason': '合成复核明确该动作仅在本章使用。', 'evidence': [{'text': BODY}]}}]
        result = submit_artifacts(self.root, **self.arguments(form, edits, request='fact-explicit-disposition'))
        self.assertTrue(result['ok'], result)

    def test_b_new_behavior_is_explicit_and_cannot_disappear_from_later_form(self):
        form = self.form()
        row = {'id': 'LIFE-KEY', 'kind': 'living', 'origin': 'prose', 'disposition': 'needs_material',
               'conclusion': '现有短句不足以判断交接是否形成持续义务。', 'evidence': [{'text': BODY}], 'basisIds': []}
        edits = self.edits(form)
        edits.update(verdict='INCOMPLETE', append={'behaviorReviews': [row]})
        self.assertTrue(submit_artifacts(self.root, **self.arguments(form, edits, request='behavior-first'))['ok'])
        form = self.form()
        self.assertEqual(form['fields']['behaviorReviews'][0]['id'], 'LIFE-KEY')
        for update in ([{'id': 'LIFE-KEY', 'origin': 'plan'}], [{'id': 'LIFE-KEY', 'kind': 'relationship'}],
                       [{'id': 'LIFE-KEY', 'basisIds': ['invented']}], [{'id': 'new', 'conclusion': 'pass'}]):
            self.assert_rejected_unchanged(self.arguments(form, {'behaviorReviews': update}))
        edits = self.edits(form)
        edits['behaviorReviews'] = []
        result = submit_artifacts(self.root, **self.arguments(form, edits, request='behavior-drop'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'BEHAVIOR_DISPOSITION_PENDING')
        for append in ({'behaviorReviews': [row]}, {'slots': []},
                       {'behaviorReviews': [{**row, 'id': 'new', 'origin': 'plan'}]}):
            self.assert_rejected_unchanged(self.arguments(form, {'append': append}))

    def test_writing_decision_identity_and_required_verdict_are_preserved(self):
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=BODY,
                      writingNotes={'deviations': [{'id': 'D-LEVEL', 'kind': 'intensityLevel', 'intensityLevel': 1,
                                                   'reason': '合成短章只写钥匙归还。'}]})
        form = self.form()
        self.assertEqual(form['fields']['writingDecisions'], [{'id': 'D-LEVEL', 'decision': '', 'reason': ''}])
        self.assert_rejected_unchanged(self.arguments(form, {'writingDecisions': [{'id': 'other', 'decision': 'accept'}]}))
        edits = self.edits(form)
        edits.pop('writingDecisions')
        result = submit_artifacts(self.root, **self.arguments(form, edits, request='missing-note-decision'))
        self.assertFalse(result['ok'])
        edits['writingDecisions'] = [{'id': 'D-LEVEL', 'decision': 'accept', 'reason': '核对当前短章后接受该次降档。'}]
        result = submit_artifacts(self.root, **self.arguments(form, edits, request='explicit-note-decision'))
        self.assertTrue(result['ok'], result)

    def test_relationship_and_living_rows_use_frozen_ids_and_do_not_drop_untouched_rows(self):
        # Structural edit permissions are independent of the semantic validator.
        # This is a real current-form payload plus declared rows, not source text inspection.
        payload = self.form('stage_a_review', view='full')['payload']
        payload['relationshipCausalityReviews'] = [{'id': 'R-ONE', 'conclusion': '', 'evidence': []}]
        payload['behaviorReviews'] = [{'id': 'L-ONE', 'kind': 'living', 'origin': 'plan',
            'disposition': 'needs_material', 'conclusion': '', 'evidence': [], 'basisIds': ['C-TEST-ALPHA']}]
        result = apply_semantic_edits(payload, 'stage_a_review', {'relationshipCausalityReviews': [{
            'id': 'R-ONE', 'conclusion': '当前材料仍不足。'}], 'behaviorReviews': []})
        self.assertEqual(result['relationshipCausalityReviews'][0]['conclusion'], '当前材料仍不足。')
        self.assertEqual(result['behaviorReviews'], payload['behaviorReviews'])
        for edits in ({'relationshipCausalityReviews': [{'id': 'R-TWO', 'conclusion': 'pass'}]},
                      {'behaviorReviews': [{'id': 'L-ONE', 'basisIds': []}]}, {'append': {'findings': []}}):
            with self.assertRaises(StructureError):
                apply_semantic_edits(payload, 'stage_a_review', edits)

    def test_repair_form_uses_existing_issue_decisions_and_artifact_receipt(self):
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['artifacts'][self.cid].pop('diagnostics', None)
        workflow['approvals'][self.cid]['artifactHashes'].pop('diagnostics', None)
        write_json(self.root / '.govel/workflow.json', workflow)
        form = self.form()
        edits = self.edits(form)
        edits.update(verdict='REPAIR_REQUIRED', repairScope='local', append={'findings': [{
            'findingId': 'REPAIR-KEY', 'severity': 'S2', 'ruleId': 'synthetic-check',
            'summary': '测试用的待核钥匙交接。', 'location': BODY, 'repairSuggestion': '核对是否已交回。'}]})
        self.assertTrue(submit_artifacts(self.root, **self.arguments(form, edits, request='repair-base'))['ok'])
        material = call_operation('workflow.repair-context', self.root, {'chapterId': self.cid})
        form = self.form(repairRef=material['repairRef'])
        self.assertNotIn('repairRef', form['fields'])
        edits = self.edits(form)
        result = submit_artifacts(self.root, **self.arguments(form, edits, request='repair-omitted'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'REPAIR_REVIEW_INCOMPLETE')
        edits['repairDecisions'] = [{'issueId': 'REPAIR-KEY', 'decision': 'resolved',
            'reason': '实际复核合成短句已明确交还动作。', 'quotes': [{'text': BODY}]}]
        args = self.arguments(form, edits, request='repair-explicit')
        result = submit_artifacts(self.root, **args)
        self.assertTrue(result['ok'], result)
        artifact = read_json(self.root / result['stages'][0]['artifactPath'])
        self.assertEqual(artifact['payload']['repairReview']['pendingIssueIds'], [])
        self.assertEqual(artifact['payload']['repairReview']['decisions'][0]['issueId'], 'REPAIR-KEY')
        for reference in (form['formRef'], material['repairRef']):
            self.cache_path(reference).unlink()
        before = self.files()
        replay = submit_artifacts(self.root, **args)
        self.assertTrue(replay['ok'], replay)
        self.assertEqual(self.files(), before)


if __name__ == '__main__':
    unittest.main()
