"""Neutral information-source regressions; no mechanical literary verdicts."""
from copy import deepcopy
from contextlib import redirect_stdout
from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import envelope, sha256_text
from scripts.core.authoring_forms import review_form
from scripts.core.authoring_submission import submit_artifacts
from scripts.core.cognition_review import scan_candidates, validate_review
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.prose_binding import seal_binding
from scripts.core.review_artifacts import ReviewArtifactError
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import StructureError, validate_structure
from scripts.core.workflow import default_workflow, record_artifact, approve_artifacts
from tests.fixtures_current import create_current_project
from tests.fixtures_workflow import evidenced_slots, register_and_approve, run_and_approve_diagnostics


class CognitionReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        fixture = create_current_project(self.root, chapter_count=3)
        self.prior, self.cid, self.later = fixture['chapterIds']
        source = fixture['structure']
        source['entities'][0].update(name='林舟', aliases=['小林'])
        self.actor = source['entities'][0]['entityId']
        self.person = 'character-GU'
        source['entities'].append({'entityId': self.person, 'entityType': 'character',
                                   'name': '顾行', 'aliases': ['老顾'], 'status': 'active'})
        chapter = source['chapters'][1]
        (self.root / chapter['filePath']).unlink()
        chapter['status'] = 'review'
        chapter['proseBinding'] = seal_binding(source['projectId'], self.cid, chapter['filePath'],
            state='unbound', generation=1, expected_content_digest=None)
        self.save(source)
        workflow = default_workflow()
        workflow['approvedChapterIds'] = fixture['chapterIds']
        write_json(self.root / '.govel/workflow.json', workflow)
        register_and_approve(self.root, self.cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': []})
        self.text = '来客介绍自己叫顾行，林舟听清了。\n林舟说：“顾行，请坐。”'
        self.set_candidate(self.text)

    def source(self):
        return read_json(self.root / '.govel/structure.json')

    def workflow(self):
        return read_json(self.root / '.govel/workflow.json')

    def save(self, source):
        validate_structure(source)
        write_json(self.root / '.govel/structure.json', source)

    def set_candidate(self, text):
        self.text = text
        return pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=text)

    def form(self, view='full'):
        return review_form(self.root, chapterId=self.cid, artifactType='stage_b_review', view=view)

    def completed(self):
        payload = self.form()['payload']
        payload.update(verdict='POLISH_READY', slots=evidenced_slots(self.text),
                       behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'},
                       factAssessment={'status': 'no_change'})
        return payload

    def support(self, row, source=None):
        row.update(characterId=self.actor, information='来客姓名是顾行', disposition='supported',
                   reason='林舟当面听到来客介绍，在称呼前已获知姓名。',
                   sources=[source or {'kind': 'current_prose', 'quote': {'text': '来客介绍自己叫顾行，林舟听清了。'}}])
        return row

    def pack(self, payload):
        return pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', payload=payload)

    def assert_code(self, code, call):
        with self.assertRaises((ReviewArtifactError, StructureError)) as caught:
            call()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def prior_body(self, text):
        source = self.source()
        chapter = source['chapters'][0]
        (self.root / chapter['filePath']).write_text(text)
        chapter['proseBinding'] = seal_binding(source['projectId'], self.prior, chapter['filePath'],
            state='managed', generation=chapter['proseBinding']['generation'] + 1,
            expected_content_digest='sha256:' + sha256_text(text))
        self.save(source)

    def test_scan_only_locates_names_and_keeps_attribution_uncertainty(self):
        source = self.source()
        source['entities'].append({'entityId': 'character-SAME', 'entityType': 'character', 'name': '顾行', 'status': 'active'})
        text = '顾行到了。\n林舟说：“顾行，请坐。”\n林舟说：“顾行，喝茶。”\n“老顾到了？”\n“老顾到了？”'
        rows = scan_candidates(source, self.cid, text)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['mentions'][0]['entityIds'], ['character-GU', 'character-SAME'])
        self.assertTrue(rows[0]['speakerHint']['certain'])
        self.assertTrue(all(row['characterId'] is None and row['disposition'] == 'unassessed' for row in rows))
        self.assertTrue(all(not row['speakerHint']['certain'] for row in rows[2:]))
        self.assertEqual(scan_candidates(source, self.cid, '旁白知道顾行已到门口。'), [])

    def test_form_blanks_are_model_visible_and_cannot_be_completed_by_generic_evidence(self):
        form = self.form('model')
        row = form['fields']['cognitionReviews'][0]
        self.assertEqual(row['sources'], [])
        self.assertEqual(row['reason'], '')
        self.assertFalse(form['semanticReviewComplete'])
        payload = self.completed()
        del payload['cognitionReviews']
        before = self.workflow()
        error = self.assert_code('COGNITION_REVIEW_REQUIRED', lambda: self.pack(payload))
        self.assertEqual(self.workflow(), before)
        self.assertEqual(error.details['recovery']['kind'], 'cognition_evidence_required')

    def test_same_chapter_source_and_reason_are_required(self):
        payload = self.completed()
        row = self.support(payload['cognitionReviews'][0])
        result = self.pack(payload)
        self.assertTrue(result['registered'])
        row['sources'] = []
        self.assert_code('COGNITION_SOURCE_REQUIRED', lambda: self.pack(payload))
        row['sources'] = [{'kind': 'current_prose', 'quote': deepcopy(row['use'])}]
        self.assert_code('COGNITION_SOURCE_ORDER_INVALID', lambda: self.pack(payload))
        row['sources'] = [{'kind': 'current_prose', 'quote': {'text': '不存在的介绍'}}]
        self.assert_code('EVIDENCE_QUOTE_MISSING', lambda: self.pack(payload))

    def test_scan_cannot_be_replaced_with_another_semantic_use_of_the_same_id(self):
        payload = self.completed()
        row = self.support(payload['cognitionReviews'][0])
        row.update(origin='semantic', use={'text': '来客介绍自己叫顾行，林舟听清了。'})
        self.assert_code('COGNITION_USE_STALE', lambda: self.pack(payload))

    def test_false_positive_requires_a_real_disposition_and_semantic_scope_is_not_inferred(self):
        self.set_candidate('门牌上印着“顾行”。')
        payload = self.completed()
        payload['cognitionReviews'][0].update(disposition='not_applicable',
            reason='这是门牌文字的直接引述，本句没有宣称某个角色已经识别姓名。')
        self.assertTrue(self.pack(payload)['registered'])
        self.set_candidate('林舟看着关上的门。')
        self.assertNotIn('cognitionReviews', self.form()['payload'])
        payload = self.completed()
        self.assertTrue(self.pack(payload)['registered'])
        self.assertIn('秘密', self.form()['guide']['cognitionReviews'])

    def test_semantic_append_uses_same_sources_without_fabricating_scan_coverage(self):
        self.set_candidate('来客先说门锁密码是七号。\n林舟输入七号密码。')
        form = self.form('model')
        edits = self.completed()
        for key in list(edits):
            if key not in form['fields']:
                edits.pop(key)
        edits['append'] = {'cognitionReviews': [{'id': 'semantic-password', 'origin': 'semantic',
            'use': {'text': '林舟输入七号密码。'}, 'characterId': self.actor,
            'information': '门锁密码', 'disposition': 'supported', 'reason': '来客当面告知后，林舟才输入。',
            'sources': [{'kind': 'current_prose', 'quote': {'text': '来客先说门锁密码是七号。'}}]}]}
        result = submit_artifacts(self.root, chapterId=self.cid, requestId='semantic-password', artifacts=[{
            'artifactType': 'stage_b_review', 'formRef': form['formRef'], 'edits': edits}])
        self.assertTrue(result['ok'], result)

    def test_form_edits_preserve_scanned_identity_and_submit_supported_use(self):
        form = self.form('model')
        fields = deepcopy(form['fields'])
        fields.update(verdict='POLISH_READY', slots=evidenced_slots(self.text),
            behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'},
            factAssessment={'status': 'no_change'})
        self.support(fields['cognitionReviews'][0])
        changed = deepcopy(fields)
        changed['cognitionReviews'][0]['use']['text'] = '改写扫描位置'
        self.assert_code('REVIEW_FORM_EDIT_FORBIDDEN', lambda: submit_artifacts(self.root, chapterId=self.cid,
            requestId='changed-scan', artifacts=[{'artifactType': 'stage_b_review', 'formRef': form['formRef'], 'edits': changed}]))
        result = submit_artifacts(self.root, chapterId=self.cid, requestId='supported-scan', artifacts=[{
            'artifactType': 'stage_b_review', 'formRef': form['formRef'], 'edits': fields}])
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['approvalsPerformed'])

    def test_prior_prose_is_bound_by_pack_but_direct_registration_cannot_omit_binding(self):
        self.prior_body('顾行向林舟作了自我介绍。')
        self.set_candidate('林舟说：“顾行，请坐。”')
        payload = self.completed()
        self.support(payload['cognitionReviews'][0], {'kind': 'prior_prose', 'chapterId': self.prior,
                                                    'quote': {'text': '顾行向林舟作了自我介绍。'}})
        artifact = envelope('stage_b_review', payload, project_id=self.source()['projectId'], chapter_id=self.cid)
        self.assert_code('COGNITION_SOURCE_STALE', lambda: record_artifact(self.workflow(), self.cid, artifact,
            structure=self.source(), project_id=self.source()['projectId'], project_root=self.root))
        incomplete = deepcopy(payload)
        incomplete['verdict'] = 'INCOMPLETE'
        artifact_without_root = envelope('stage_b_review', incomplete,
            project_id=self.source()['projectId'], chapter_id=self.cid)
        with self.assertRaises(ValueError) as caught:
            record_artifact(self.workflow(), self.cid, artifact_without_root,
                            structure=self.source(), project_id=self.source()['projectId'])
        self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_MATERIAL_REQUIRED')
        result = self.pack(payload)
        saved = read_json(self.root / result['artifactPath'])['payload']
        citation = saved['cognitionReviews'][0]['sources'][0]
        self.assertEqual(citation['proseDigest'], 'sha256:' + sha256_text('顾行向林舟作了自我介绍。'))
        self.assertEqual(citation['bindingGeneration'], self.source()['chapters'][0]['proseBinding']['generation'])
        self.prior_body('顾行向林舟作了自我介绍。随后离开。')
        self.assert_code('COGNITION_SOURCE_STALE', lambda: self.pack(saved))
        preflight = preflight_finalization(self.root, self.cid, skip_title=True, binding_generation=1)
        self.assertIn('COGNITION_SOURCE_STALE', [row['code'] for row in preflight.blockers])

    def test_finalization_guards_prior_prose_after_preflight(self):
        from scripts.core import finalization
        from scripts.core.continuity import complete_continuity, continuity_preview
        from tests.fixtures_workflow import review_ready_project
        # Produce the predecessor's accepted prose and continuity through the
        # real gates; a stable label alone is not predecessor evidence.
        review_ready_project(self.root, chapter_count=3)
        current_id, self.cid = self.cid, self.prior
        self.set_candidate('顾行向林舟作了自我介绍。')
        run_and_approve_diagnostics(self.root, self.cid)
        register_and_approve(self.root, self.cid, 'stage_b_review', {
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(self.text), 'findings': [],
            'finalProseSha256': sha256_text(self.text)})
        pack_artifact(self.root, chapterId=self.cid, artifactType='state_candidate', payload={'changes': []})
        workflow = self.workflow()
        approve_artifacts(workflow, self.cid, artifact_hashes={kind: entry['artifactSha256']
            for kind, entry in workflow['artifacts'][self.cid].items()}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.govel/workflow.json', workflow)
        finalization.finalize_chapter(self.root, self.cid, skip_title=True,
            binding_generation=self.source()['chapters'][0]['proseBinding']['generation'])
        continuity = continuity_preview(self.root, self.cid)
        complete_continuity(self.root, self.cid, batch_hash=continuity['batchHash'], approved=True,
            confirmation={'decisions': [], 'summary': {'resolution': 'not_required',
                'reason': '本合成场景直接核对完整前章介绍，不需要另建摘要。'}})
        self.cid = current_id
        source = self.source()
        source['chapters'][1]['status'] = 'review'
        self.save(source)
        register_and_approve(self.root, self.cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': []})
        self.set_candidate('林舟说：“顾行，请坐。”')
        run_and_approve_diagnostics(self.root, self.cid)
        payload = self.completed()
        self.support(payload['cognitionReviews'][0], {'kind': 'prior_prose', 'chapterId': self.prior,
                                                    'quote': {'text': '顾行向林舟作了自我介绍。'}})
        self.pack(payload)
        pack_artifact(self.root, chapterId=self.cid, artifactType='state_candidate', payload={'changes': []})
        workflow = self.workflow()
        approve_artifacts(workflow, self.cid, artifact_hashes={kind: entry['artifactSha256']
            for kind, entry in workflow['artifacts'][self.cid].items()}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.govel/workflow.json', workflow)
        preflight = preflight_finalization(self.root, self.cid, skip_title=True, binding_generation=1)
        self.assertTrue(preflight.ready, preflight.blockers)
        before_source = (self.root / '.govel/structure.json').read_bytes()
        before_workflow = (self.root / '.govel/workflow.json').read_bytes()
        prior_path = self.root / self.source()['chapters'][0]['filePath']
        from scripts.core import write_sets
        real_apply = write_sets.apply_write_set

        def race(root, outputs, **kwargs):
            prior_path.write_text('作者在预检后修改了前章。')
            return real_apply(root, outputs, **kwargs)

        with patch.object(write_sets, 'apply_write_set', side_effect=race):
            with self.assertRaises(StructureError) as caught:
                finalization.finalize_chapter(self.root, self.cid, skip_title=True, binding_generation=1)
        self.assertIn(caught.exception.code, {'TRANSACTION_GUARD_DRIFT', 'WRITE_SET_CONFLICT'})
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before_source)
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), before_workflow)
        self.assertFalse((self.root / self.source()['chapters'][1]['filePath']).exists())
        self.assertEqual(prior_path.read_text(), '作者在预检后修改了前章。')

    def test_cli_direct_registration_carries_source_guards_to_both_write_branches(self):
        from scripts import novelctl
        from scripts.core import write_sets
        self.prior_body('顾行向林舟作了自我介绍。')
        self.set_candidate('林舟说：“顾行，请坐。”')
        payload = self.completed()
        self.support(payload['cognitionReviews'][0], {'kind': 'prior_prose', 'chapterId': self.prior,
                                                    'quote': {'text': '顾行向林舟作了自我介绍。'}})
        artifact_path = self.root / self.pack(payload)['artifactPath']
        prior_path = self.root / self.source()['chapters'][0]['filePath']
        prior_raw = prior_path.read_bytes()
        real_apply = write_sets.apply_write_set
        for bind in ([], ['--bind', 'finalProseSha256']):
            with self.subTest(bind=bind):
                before = (self.root / '.govel/workflow.json').read_bytes()
                before_artifact = artifact_path.read_bytes()

                def race(root, outputs, **kwargs):
                    if kwargs.get('operation') in {'artifact_register', 'artifact_register_bind'}:
                        prior_path.write_text('在直接登记的写入边界修改前章。')
                    return real_apply(root, outputs, **kwargs)

                stream = io.StringIO()
                with patch.object(write_sets, 'apply_write_set', side_effect=race), redirect_stdout(stream):
                    code = novelctl.main(['project', 'workflow', 'artifact-register', '--project-root', str(self.root),
                        '--chapter-id', self.cid, '--artifact', str(artifact_path), *bind])
                response = json.loads(stream.getvalue())
                self.assertNotEqual(code, 0, response)
                self.assertEqual(response['code'], 'TRANSACTION_GUARD_DRIFT')
                self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), before)
                self.assertEqual(artifact_path.read_bytes(), before_artifact)
                self.assertEqual(prior_path.read_text(), '在直接登记的写入边界修改前章。')
                prior_path.write_bytes(prior_raw)

    def test_cli_direct_registration_preserves_concurrent_source_and_workflow_metadata(self):
        from scripts import novelctl
        from scripts.core import write_sets
        self.prior_body('顾行向林舟作了自我介绍。')
        self.set_candidate('林舟说：“顾行，请坐。”')
        payload = self.completed()
        self.support(payload['cognitionReviews'][0], {'kind': 'prior_prose', 'chapterId': self.prior,
                                                    'quote': {'text': '顾行向林舟作了自我介绍。'}})
        artifact_path = self.root / self.pack(payload)['artifactPath']
        real_apply = write_sets.apply_write_set
        for target in ('.govel/structure.json', '.govel/workflow.json'):
            for bind in ([], ['--bind', 'finalProseSha256']):
                with self.subTest(target=target, bind=bind):
                    path = self.root / target
                    original = path.read_bytes()
                    changed = json.loads(original)
                    if target.endswith('structure.json'):
                        changed['revision'] += 1
                        prior = changed['chapters'][0]
                        prior['proseBinding'] = seal_binding(changed['projectId'], self.prior, prior['filePath'],
                            state='managed', generation=prior['proseBinding']['generation'] + 1,
                            expected_content_digest=prior['proseBinding']['expectedContentDigest'])
                    else:
                        changed['approvals'] = {}
                    new_bytes = write_sets.json_bytes(changed)

                    def race(root, outputs, **kwargs):
                        if kwargs.get('operation') in {'artifact_register', 'artifact_register_bind'}:
                            path.write_bytes(new_bytes)
                        return real_apply(root, outputs, **kwargs)

                    stream = io.StringIO()
                    with patch.object(write_sets, 'apply_write_set', side_effect=race), redirect_stdout(stream):
                        code = novelctl.main(['project', 'workflow', 'artifact-register', '--project-root', str(self.root),
                            '--chapter-id', self.cid, '--artifact', str(artifact_path), *bind])
                    result = json.loads(stream.getvalue())
                    self.assertNotEqual(code, 0, result)
                    self.assertIn(result['code'], {'WRITE_SET_CONFLICT', 'TRANSACTION_GUARD_DRIFT'})
                    self.assertEqual(path.read_bytes(), new_bytes)
                    path.write_bytes(original)

    def test_prior_prose_external_changes_and_later_chapters_are_not_sources(self):
        payload = self.completed()
        self.support(payload['cognitionReviews'][0], {'kind': 'prior_prose', 'chapterId': self.later,
                                                    'quote': {'text': '第3章正文。'}})
        self.assert_code('COGNITION_PRIOR_CHAPTER_INVALID', lambda: self.pack(payload))
        source = self.source()
        (self.root / source['chapters'][0]['filePath']).write_text('外部改稿')
        payload['cognitionReviews'][0]['sources'][0].update(chapterId=self.prior, quote={'text': '外部改稿'})
        with self.assertRaises(ValueError) as caught:
            self.pack(payload)
        self.assertIn('OWNED_MODIFIED_EXTERNALLY', str(caught.exception))

    def test_prior_prose_cannot_come_from_provably_later_story_time(self):
        self.prior_body('顾行向林舟作了自我介绍。')
        baseline = self.source()
        cases = [
            [{'entityId': 'time-USE', 'entityType': 'time_anchor', 'name': '使用', 'status': 'active', 'timeOrdinal': 5},
             {'entityId': 'time-END', 'entityType': 'time_anchor', 'name': '上界', 'status': 'active', 'timeOrdinal': 10},
             {'entityId': 'time-LEARN', 'entityType': 'time_anchor', 'name': '获知', 'status': 'active',
              'timeSpec': {'kind': 'range', 'afterId': 'time-USE', 'beforeId': 'time-END'}}],
            [{'entityId': 'time-USE', 'entityType': 'time_anchor', 'name': '使用', 'status': 'active', 'timeSpec': {'kind': 'unknown'}},
             {'entityId': 'time-LEARN', 'entityType': 'time_anchor', 'name': '获知', 'status': 'active',
              'timeSpec': {'kind': 'relative', 'anchorId': 'time-USE', 'offset': 1}}],
        ]
        for entities in cases:
            with self.subTest(time=entities[-1]['timeSpec']):
                source = deepcopy(baseline)
                source['entities'].extend(entities)
                source['chapters'][0]['storyTimeStartId'] = 'time-LEARN'
                source['chapters'][1]['storyTimeEndId'] = 'time-USE'
                self.save(source)
                register_and_approve(self.root, self.cid, 'stage_a_review', {
                    'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': []})
                self.set_candidate('林舟说：“顾行，请坐。”')
                payload = self.completed()
                self.support(payload['cognitionReviews'][0], {'kind': 'prior_prose', 'chapterId': self.prior,
                                                            'quote': {'text': '顾行向林舟作了自我介绍。'}})
                self.assert_code('COGNITION_SOURCE_ORDER_INVALID', lambda: self.pack(payload))

    def test_large_scans_keep_all_rows_in_the_frozen_paged_form(self):
        from scripts.core.transport_cache import read_snapshot
        text = '\n'.join(f'林舟说：“顾行，第{index}份登记已放好。”' for index in range(24))
        self.set_candidate(text)
        form = self.form('model')
        self.assertTrue(form.get('responseCompacted'), form)
        self.assertFalse(form['fieldsIncluded'])
        chunks, cursor = [], None
        while True:
            page = read_snapshot(self.root, reference=form['formRef'], view='model', cursor=cursor)
            chunks.extend(row['text'] for row in page['materials'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        restored = json.loads(''.join(chunks))
        self.assertEqual(len(restored['fields']['cognitionReviews']), 24)
        self.assertTrue(all(row['disposition'] == 'unassessed' for row in restored['fields']['cognitionReviews']))

    def test_prior_source_change_expires_an_unsubmitted_form(self):
        form = self.form('model')
        prior_path = self.root / self.source()['chapters'][0]['filePath']
        prior_path.write_text('前章在取表后被改动。')
        self.assert_code('REVIEW_FORM_STALE', lambda: submit_artifacts(self.root, chapterId=self.cid,
            requestId='stale-prior-form', artifacts=[{'artifactType': 'stage_b_review', 'formRef': form['formRef'], 'edits': {}}]))

    def test_retained_pending_issue_cannot_disappear_or_be_erased_by_incomplete_review(self):
        payload = self.completed()
        row = payload['cognitionReviews'][0]
        row.update(disposition='needs_material', reason='尚未找到林舟得知姓名的来源。', findingId='F-NAME')
        payload.update(verdict='INCOMPLETE', findings=[{'findingId': 'F-NAME', 'severity': 'warning',
            'ruleId': 'cognition', 'summary': '姓名使用来源待核', 'location': row['use']['text']}])
        self.pack(payload)
        retained = self.form()['payload']['findings']
        self.assertEqual([r['findingId'] for r in retained], ['F-NAME'])
        incomplete = self.completed()
        incomplete.update(verdict='INCOMPLETE', findings=[])
        self.assert_code('COGNITION_FINDING_DROPPED', lambda: self.pack(incomplete))
        # A new source-backed resolution is explicit, and the former advisory
        # finding no longer needs to be carried into every later report.
        completed = self.completed()
        self.support(completed['cognitionReviews'][0])['findingId'] = 'F-NAME'
        completed['findings'] = []
        self.pack(completed)
        self.assertEqual(self.form()['payload']['findings'], [])

    def test_repair_required_needs_changed_prose_even_through_intermediate_reports(self):
        payload = self.completed()
        row = payload['cognitionReviews'][0]
        row.update(disposition='repair_required', reason='测试夹具：已确认此处称呼需要改写。', findingId='F-REPAIR')
        payload.update(verdict='INCOMPLETE', findings=[{'findingId': 'F-REPAIR', 'severity': 'warning',
            'ruleId': 'cognition', 'summary': '测试夹具认知缺陷', 'location': row['use']['text']}])
        self.pack(payload)
        next_payload = self.completed()
        self.support(next_payload['cognitionReviews'][0])['findingId'] = 'F-REPAIR'
        next_payload['verdict'] = 'INCOMPLETE'
        self.assert_code('COGNITION_REPAIR_REQUIRED', lambda: self.pack(next_payload))

    def test_explicit_repair_preserves_one_current_cognition_finding(self):
        from scripts.core.repair_context import repair_context
        payload = self.completed()
        row = payload['cognitionReviews'][0]
        row.update(disposition='needs_material', reason='尚未核实当前称呼的来源。', findingId='F-NAME')
        payload.update(verdict='INCOMPLETE', findings=[{'findingId': 'F-NAME', 'severity': 'warning',
            'ruleId': 'cognition', 'summary': '姓名来源待核', 'location': row['use']['text']}])
        self.pack(payload)
        material = repair_context(self.root, chapterId=self.cid)
        current = review_form(self.root, chapterId=self.cid, artifactType='stage_b_review',
                              repairRef=material['repairRef'], view='full')['payload']
        current['slots'] = evidenced_slots(self.text)
        current['findings'][0]['summary'] = '本轮已定位称呼，获知来源仍待核验。'
        result = self.pack(current)
        saved = read_json(self.root / result['artifactPath'])['payload']
        self.assertEqual([finding['findingId'] for finding in saved['findings']], ['F-NAME'])
        self.assertEqual(saved['findings'][0]['summary'], current['findings'][0]['summary'])
        self.assertEqual(saved['verdict'], 'INCOMPLETE')
        self.assertFalse(result['approvalPerformed'])
        self.assertEqual([finding['findingId'] for finding in self.form()['payload']['findings']], ['F-NAME'])

    def test_s1_cognition_repair_submits_through_ordinary_form_without_old_blocker(self):
        payload = self.completed()
        row = payload['cognitionReviews'][0]
        row.update(characterId=self.actor, information='来客姓名', disposition='repair_required',
                   reason='中性验收：当前称呼必须修正。', findingId='F-S1', sources=[])
        payload.update(verdict='REPAIR_REQUIRED', repairScope='local', findings=[{
            'findingId': 'F-S1', 'severity': 'S1', 'ruleId': 'cognition', 'summary': '称呼缺少依据',
            'location': row['use']['text'], 'repairSuggestion': '改成不包含身份的招呼。'}])
        self.pack(payload)
        self.set_candidate('来客站在门边。\n林舟说：“请坐。”')
        form = self.form(view='model')
        self.assertIn('repairDecisions', form['fields'])
        edits = form['fields']
        edits.update(verdict='POLISH_READY', slots=evidenced_slots(self.text),
                     behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'},
                     factAssessment={'status': 'no_change'})
        refused = submit_artifacts(self.root, chapterId=self.cid, requestId='cognition-unresolved',
            artifacts=[{'artifactType': 'stage_b_review', 'formRef': form['formRef'], 'edits': edits}])
        self.assertFalse(refused['ok'])
        edits['repairDecisions'] = [{'issueId': 'F-S1', 'decision': 'resolved',
            'reason': '已删除无依据的姓名，复核当前招呼不依赖该信息。', 'quotes': [{'text': '林舟说：“请坐。”'}]}]
        result = submit_artifacts(self.root, chapterId=self.cid, requestId='cognition-resolved',
            artifacts=[{'artifactType': 'stage_b_review', 'formRef': form['formRef'], 'edits': edits}])
        self.assertTrue(result['ok'], result)
        from scripts.core.mechanical_evidence import _registered
        current = _registered(self.root, self.source(), self.workflow(), self.cid, 'stage_b_review')['payload']
        self.assertEqual(current['verdict'], 'POLISH_READY')
        self.assertNotIn('F-S1', [row.get('findingId') for row in current['findings']])
        self.assertEqual(current['repairReview']['decisions'][0]['issueId'], 'F-S1')

    def test_reregistered_incomplete_report_reopens_its_retained_finding(self):
        from scripts import novelctl
        from scripts.core.repair_history import read_versions
        payload = self.completed()
        row = payload['cognitionReviews'][0]
        row.update(disposition='needs_material', reason='姓名来源尚待核对。', findingId='F-REOPEN')
        payload.update(verdict='INCOMPLETE', findings=[{'findingId': 'F-REOPEN', 'severity': 'warning',
            'ruleId': 'cognition', 'summary': '姓名使用来源待核', 'location': row['use']['text']}])
        original = self.pack(payload)
        ready = self.completed()
        self.support(ready['cognitionReviews'][0])['findingId'] = 'F-REOPEN'
        ready['findings'] = []
        self.pack(ready)
        self.assertEqual(self.form()['payload']['findings'], [])
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = novelctl.main(['project', 'workflow', 'artifact-register', '--project-root', str(self.root),
                '--chapter-id', self.cid, '--artifact', str(self.root / original['artifactPath'])])
        self.assertEqual(code, 0, stream.getvalue())
        versions, unavailable = read_versions(self.root, self.source(), self.workflow(), self.cid, 'stage_b_review')
        self.assertEqual(unavailable, [])
        self.assertTrue(versions[-1]['latest'])
        self.assertEqual(versions[-1]['report']['artifactSha256'], original['artifactSha256'])
        self.assertEqual([r['findingId'] for r in self.form()['payload']['findings']], ['F-REOPEN'])
        intermediate = self.completed()
        intermediate['verdict'] = 'INCOMPLETE'
        intermediate.pop('cognitionReviews')
        self.pack(intermediate)
        self.assertEqual([r['findingId'] for r in self.form()['payload']['findings']], ['F-REOPEN'])
        dropped = self.completed()
        self.support(dropped['cognitionReviews'][0])
        dropped['findings'] = []
        self.assert_code('COGNITION_FINDING_DROPPED', lambda: self.pack(dropped))

    def test_source_fragment_requires_perspective_current_time_and_plan_dependency(self):
        source = self.source()
        source['entities'].extend([
            {'entityId': 'time-NOW', 'entityType': 'time_anchor', 'name': '现在', 'status': 'active', 'timeOrdinal': 1},
            {'entityId': 'time-LATER', 'entityType': 'time_anchor', 'name': '之后', 'status': 'active', 'timeOrdinal': 2}])
        source['chapters'][1]['storyTimeStartId'] = 'time-NOW'
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.cid)
        belief = {'fragmentId': 'belief-NAME', 'ownerId': self.cid, 'order': 2, 'text': '林舟此前得知来客自称顾行。',
                  'contentRole': 'belief', 'perspectiveId': self.actor, 'contentBindings': plan['contentBindings'],
                  'validDuring': {'fromId': 'time-NOW'}}
        source['fragments'].append(belief)
        self.save(source)
        # Reapprove synthetic A against the changed source; no runtime auto-review.
        register_and_approve(self.root, self.cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': []})
        self.set_candidate('林舟说：“顾行，请坐。”')
        payload = self.completed()
        row = self.support(payload['cognitionReviews'][0], {'kind': 'source_fragment', 'fragmentId': 'belief-NAME',
                                                         'quote': {'text': belief['text']}})
        self.assertTrue(self.pack(payload)['registered'])
        for field, value, code in [('perspectiveId', self.person, 'COGNITION_SOURCE_PERSPECTIVE'),
                                   ('contentRole', 'fact', 'COGNITION_SOURCE_PERSPECTIVE'),
                                   ('validDuring', {'fromId': 'time-LATER'}, 'COGNITION_SOURCE_TIME_UNKNOWN')]:
            with self.subTest(field=field):
                modified = deepcopy(source)
                target = next(f for f in modified['fragments'] if f['fragmentId'] == 'belief-NAME')
                target[field] = value
                if field == 'contentRole':
                    target.pop('perspectiveId')
                changed = deepcopy(payload)
                changed['sourcePlanHash'] = chapter_plan(modified, self.cid)['sourcePlanHash']
                self.assert_code(code, lambda: validate_review(self.root, modified, self.workflow(), self.cid, changed, self.text))
        unbounded = deepcopy(source)
        next(f for f in unbounded['fragments'] if f['fragmentId'] == 'belief-NAME')['validDuring'] = {'fromId': None, 'toId': None}
        unbounded['chapters'][1]['storyTimeStartId'] = None
        changed = deepcopy(payload)
        changed['sourcePlanHash'] = chapter_plan(unbounded, self.cid)['sourcePlanHash']
        validate_review(self.root, unbounded, self.workflow(), self.cid, changed, self.text)

    def test_wrong_source_text_still_requires_semantic_review(self):
        # Exact source validation deliberately does not pretend to understand
        # entailment. The reviewer must provide a defensible semantic reason.
        payload = self.completed()
        self.support(payload['cognitionReviews'][0])
        payload['cognitionReviews'][0]['reason'] = ''
        self.assert_code('REVIEW_FIELD_REQUIRED', lambda: self.pack(payload))

    def test_cli_mcp_and_form_submit_share_the_information_use_gate(self):
        from scripts.core.operations import operations
        from scripts.mcp_server import handle
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run([sys.executable, str(root / 'scripts/novelctl.py'), 'project',
            'workflow', 'review-form', '--project-root', str(self.root), '--chapter-id', self.cid,
            '--artifact-type', 'stage_b_review'], capture_output=True, text=True, timeout=30,
            env={**os.environ, 'GOVEL_LOG_MODE': 'off', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        form = json.loads(process.stdout)
        self.assertEqual(form['fields']['cognitionReviews'], self.form('model')['fields']['cognitionReviews'])
        spec = next(row for row in operations() if row.id == 'workflow.artifact-pack')
        payload = self.completed()
        payload.pop('cognitionReviews')
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': spec.name, 'arguments': {'projectRoot': str(self.root), 'chapterId': self.cid,
                'artifactType': 'stage_b_review', 'payload': payload}}})['result']
        self.assertTrue(response['isError'])
        self.assertEqual(response['structuredContent']['code'], 'COGNITION_REVIEW_REQUIRED')
        edits = deepcopy(form['fields'])
        edits.update(verdict='POLISH_READY', slots=evidenced_slots(self.text),
            behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'},
            factAssessment={'status': 'no_change'})
        self.support(edits['cognitionReviews'][0])
        spec = next(row for row in operations() if row.id == 'workflow.submit')
        response = handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': spec.name, 'arguments': {'projectRoot': str(self.root), 'chapterId': self.cid,
                'requestId': 'mcp-cognition', 'artifacts': [{'artifactType': 'stage_b_review',
                    'formRef': form['formRef'], 'edits': edits}]}}})['result']
        self.assertFalse(response['isError'], response)
        self.assertTrue(response['structuredContent']['ok'])


if __name__ == '__main__':
    unittest.main()
