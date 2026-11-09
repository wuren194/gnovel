"""Neutral script-free authoring flows and interrupted submission recovery.

Synthetic review conclusions exercise transport/gates only, not literary quality.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.authoring_submission import submit_artifacts
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from tests.fixtures_current import initialize_project
from tests.fixtures_workflow import evidenced_slots, review_ready_project

ROOT = Path(__file__).resolve().parents[1]
BODY = '他把钥匙放回桌上，合上了登记本。'


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def commit(root, preview):
    action = preview['nextAction']
    return call_operation(action['operation'], root, action['arguments'])


class InlineArtifactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def test_text_notes_and_key_order_retries_need_no_input_file(self):
        text = '值班员把钥匙放在桌上。\r\n交班登记已经填好。\r\n'
        args = {'chapterId': self.cid, 'artifactType': 'candidate_prose', 'text': text,
                'writingNotes': {'newFacts': [], 'deviations': []}, 'requestId': 'inline-body'}
        before = read_json(self.root/'.govel/workflow.json')['approvals']
        packed = call_operation('workflow.artifact-pack', self.root, args)
        artifact = read_json(self.root/packed['artifactPath'])
        self.assertEqual(artifact['payload']['text'], text)
        self.assertEqual((self.root/packed['inputPath']).read_bytes(), text.encode())
        self.assertEqual(artifact['payload']['writingNotes'], args['writingNotes'])
        args['writingNotes'] = {'deviations': [], 'newFacts': []}
        after = inventory(self.root)
        replay = call_operation('workflow.artifact-pack', self.root, args)
        self.assertTrue(replay['replayed'])
        self.assertEqual(inventory(self.root), after)
        self.assertEqual(read_json(self.root/'.govel/workflow.json')['approvals'], before)

    def test_invalid_inline_review_rolls_back_all_generated_inputs(self):
        before = inventory(self.root)
        with self.assertRaises(ValueError):
            call_operation('workflow.artifact-pack', self.root, {'chapterId': self.cid,
                'artifactType': 'stage_b_review', 'payload': {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': {}}})
        self.assertEqual(inventory(self.root), before)

    def test_inline_input_choices_and_notes_are_not_ambiguous(self):
        for extra in ({'text': BODY, 'input': '工作台/in.txt'}, {'text': BODY, 'payload': {}},
                      {'text': BODY, 'writingNotes': {}, 'notes': '工作台/notes.json'}):
            before = inventory(self.root)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                call_operation('workflow.artifact-pack', self.root,
                    {'chapterId': self.cid, 'artifactType': 'candidate_prose', **extra})
            self.assertEqual(inventory(self.root), before)

    def test_tampered_tool_owned_input_is_never_overwritten_on_retry(self):
        args = {'chapterId': self.cid, 'artifactType': 'candidate_prose', 'text': BODY, 'requestId': 'inline-body'}
        packed = call_operation('workflow.artifact-pack', self.root, args)
        path = self.root/packed['inputPath']; path.write_text('作者在外部编辑了这个文件。')
        before = inventory(self.root)
        with self.assertRaises(StructureError) as caught:
            call_operation('workflow.artifact-pack', self.root, args)
        self.assertEqual(caught.exception.code, 'ARTIFACT_INPUT_OVERWRITE')
        self.assertEqual(inventory(self.root), before)

    def test_input_and_notes_appearing_before_inventory_are_not_overwritten(self):
        from scripts.core import artifact_pack as module
        real_inventory = module.inventory
        for suffix in ('.txt', '.json'):
            appeared = []
            sentinel = b'author content written after the initial existence check'

            def race(root, outputs, **kwargs):
                for relative in outputs:
                    if relative.startswith('工作台/inputs/') and relative.endswith(suffix):
                        path = root/relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(sentinel)
                        appeared.append(path)
                return real_inventory(root, outputs, **kwargs)

            workflow = (self.root/'.govel/workflow.json').read_bytes()
            with self.subTest(suffix=suffix), patch.object(module, 'inventory', side_effect=race):
                with self.assertRaises(StructureError) as caught:
                    pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
                        text='不同的合成候选正文。'+suffix, writingNotes={'deviations': []}, requestId='race-'+suffix)
                self.assertEqual(caught.exception.code, 'ARTIFACT_INPUT_OVERWRITE')
            self.assertTrue(appeared)
            self.assertTrue(all(path.read_bytes() == sentinel for path in appeared))
            self.assertEqual((self.root/'.govel/workflow.json').read_bytes(), workflow)
            for path in appeared:
                path.unlink()

    def test_large_inline_result_is_bounded_and_pageable_without_losing_notes(self):
        from scripts.core.project import query_policy
        from scripts.core.query import _size
        reason = '作者解释本章呈现取舍。' * 2000
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=BODY,
            writingNotes={'deviations': [{'id': 'D1', 'kind': 'intensityLevel', 'intensityLevel': 1, 'reason': reason}]})
        self.assertTrue(result['registered'])
        self.assertTrue(result['responseCompacted'])
        self.assertLessEqual(_size(result), query_policy()['maxBytes']['detail'])
        chunks, cursor = [], None
        while True:
            page = call_operation('task.snapshot', self.root, {'reference': result['reportRef'],
                                  **({'cursor': cursor} if cursor else {})})
            self.assertLessEqual(_size(page), query_policy()['maxBytes']['detail'])
            chunks.extend(row['text'] for row in page['materials'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        full = json.loads(''.join(chunks))
        self.assertEqual(full['writingNotes']['deviations'][0]['reason'], reason)


class SubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.reviews = [
            {'artifactType': 'title_candidates', 'payload': {'candidates': [{'title': '交班', 'reason': '章内事件'}]}},
            {'artifactType': 'state_candidate', 'payload': {'changes': []}},
            {'artifactType': 'stage_b_review', 'payload': {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY',
                'slots': evidenced_slots(BODY), 'findings': []}},
        ]

    def submit(self, **kwargs):
        return call_operation('workflow.submit', self.root,
            {'requestId': 'reviews-1', 'chapterId': self.cid, 'artifacts': self.reviews, **kwargs})

    def test_review_group_is_ordered_and_does_not_approve(self):
        before = read_json(self.root/'.govel/workflow.json')['approvals']
        result = self.submit()
        self.assertTrue(result['ok'], result)
        self.assertEqual([row['artifactType'] for row in result['stages']],
                         ['stage_b_review', 'state_candidate', 'title_candidates'])
        self.assertFalse(result['approvalsPerformed'])
        self.assertFalse(result['semanticReviewComplete'])
        self.assertEqual(read_json(self.root/'.govel/workflow.json')['approvals'], before)
        self.assertEqual(len(result['approvalAction']['arguments']['artifact']), 3)
        before_retry = inventory(self.root)
        replay = call_operation('workflow.submit', self.root, {'requestId': 'reviews-1'})
        self.assertTrue(replay['ok'], replay)
        self.assertTrue(all(row['replayed'] for row in replay['results']))
        self.assertEqual(inventory(self.root), before_retry)

    def test_lost_response_after_child_commit_resumes_without_duplicate_work(self):
        original = pack_artifact

        def interrupt(root, **kwargs):
            result = original(root, **kwargs)
            if kwargs['artifactType'] == 'state_candidate':
                raise RuntimeError('connection lost after the child committed')
            return result

        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=interrupt):
            partial = self.submit()
        self.assertFalse(partial['ok'])
        self.assertEqual([row['status'] for row in partial['stages']], ['registered', 'registered', 'pending'])
        self.assertNotIn('approvalAction', partial)
        restored = call_operation(partial['resumeAction']['operation'], self.root, partial['resumeAction']['arguments'])
        self.assertTrue(restored['ok'], restored)
        self.assertTrue(restored['results'][0]['replayed'])
        self.assertTrue(restored['results'][1]['replayed'])
        self.assertEqual([row['artifactSha256'] for row in restored['stages'][:2]],
                         [row['artifactSha256'] for row in partial['stages'][:2]])

    def test_same_request_cannot_change_payload_or_diagnostic_options(self):
        self.submit()
        self.reviews[0]['payload']['candidates'][0]['title'] = '另一个标题'
        before = inventory(self.root)
        with self.assertRaises(StructureError) as caught: self.submit()
        self.assertEqual(caught.exception.code, 'SUBMISSION_REQUEST_REUSED')
        self.assertEqual(inventory(self.root), before)
        with self.assertRaises(StructureError):
            call_operation('workflow.submit', self.root, {'requestId': 'reviews-1', 'diagnostics': {}})

    def test_resume_does_not_restore_a_superseded_member(self):
        self.submit()
        pack_artifact(self.root, chapterId=self.cid, artifactType='title_candidates',
                      payload={'candidates': [{'title': '新选题', 'reason': '作者另行修订'}]})
        before = inventory(self.root)
        result = call_operation('workflow.submit', self.root, {'requestId': 'reviews-1'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'SUBMISSION_SUPERSEDED')
        self.assertEqual(inventory(self.root), before)

    def test_external_candidate_change_blocks_unfinished_review_group(self):
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=OSError('temporary failure')):
            self.submit()
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text='他取走钥匙，走出了仓库。')
        before = inventory(self.root)
        result = call_operation('workflow.submit', self.root, {'requestId': 'reviews-1'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'SUBMISSION_INPUTS_STALE')
        self.assertEqual(inventory(self.root), before)

    def test_pending_candidate_cannot_overwrite_a_newer_request_on_resume(self):
        args = {'requestId': 'old-draft', 'chapterId': self.cid,
                'artifacts': [{'artifactType': 'candidate_prose', 'text': '旧候选稿。'}]}
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=OSError('before first child')):
            partial = call_operation('workflow.submit', self.root, args)
        self.assertFalse(partial['ok'])
        new = call_operation('workflow.submit', self.root, {'requestId': 'new-draft', 'chapterId': self.cid,
            'artifacts': [{'artifactType': 'candidate_prose', 'text': '作者新修订的候选稿。'}]})
        before = inventory(self.root)
        replay = call_operation('workflow.submit', self.root, {'requestId': 'old-draft'})
        self.assertFalse(replay['ok'])
        self.assertEqual(replay['error']['code'], 'SUBMISSION_SUPERSEDED')
        self.assertEqual(inventory(self.root), before)
        current = read_json(self.root/'.govel/workflow.json')['artifacts'][self.cid]['candidate_prose']
        self.assertEqual(current['artifactSha256'], new['stages'][0]['artifactSha256'])

    def test_partial_review_group_cannot_overwrite_a_newer_pending_title(self):
        def fail_title(root, **kwargs):
            if kwargs['artifactType'] == 'title_candidates':
                raise OSError('before title publication')
            return pack_artifact(root, **kwargs)
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=fail_title):
            partial = self.submit()
        self.assertEqual([row['status'] for row in partial['stages']], ['registered', 'registered', 'pending'])
        pack_artifact(self.root, chapterId=self.cid, artifactType='title_candidates',
                      payload={'candidates': [{'title': '作者另选新名', 'reason': '后续独立创作决定'}]})
        before = inventory(self.root)
        replay = call_operation('workflow.submit', self.root, {'requestId': 'reviews-1'})
        self.assertFalse(replay['ok'])
        self.assertEqual(replay['error']['code'], 'SUBMISSION_SUPERSEDED')
        self.assertEqual(inventory(self.root), before)

    def test_source_changed_after_freeze_never_rebinds_the_review(self):
        old_workflow = (self.root/'.govel/workflow.json').read_bytes()
        def race(root, **kwargs):
            source = read_json(root/'.govel/structure.json')
            next(f for f in source['fragments'] if f['ownerId'] == self.cid)['text'] = '作者改为离开仓库。'
            write_json(root/'.govel/structure.json', source)
            return pack_artifact(root, **kwargs)
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=race):
            result = call_operation('workflow.submit', self.root, {'requestId': 'freeze-race', 'chapterId': self.cid,
                'artifacts': [{'artifactType': 'stage_a_review', 'payload': {'decision': 'pending',
                    'activeSeats': list(range(1, 11)), 'consensus': ['只审查原来的留在仓库计划。'],
                    'disagreements': [], 'openItems': []}}]})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'TRANSACTION_GUARD_DRIFT')
        self.assertEqual((self.root/'.govel/workflow.json').read_bytes(), old_workflow)
        self.assertTrue(all(row['status'] == 'pending' for row in result['stages']))
        self.assertEqual(next(f for f in read_json(self.root/'.govel/structure.json')['fragments']
                             if f['ownerId'] == self.cid)['text'], '作者改为离开仓库。')

    def test_authorization_change_is_not_hidden_by_candidate_lifecycle_normalization(self):
        args = {'requestId': 'authorized-draft', 'chapterId': self.cid,
                'artifacts': [{'artifactType': 'candidate_prose', 'text': BODY}], 'advanceLifecycle': True}
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=OSError('before publication')):
            call_operation('workflow.submit', self.root, args)
        workflow = read_json(self.root/'.govel/workflow.json')
        workflow['approvedChapterIds'] = []
        write_json(self.root/'.govel/workflow.json', workflow)
        before = inventory(self.root)
        replay = call_operation('workflow.submit', self.root, {'requestId': 'authorized-draft'})
        self.assertFalse(replay['ok'])
        self.assertEqual(replay['error']['code'], 'SUBMISSION_INPUTS_STALE')
        self.assertEqual(inventory(self.root), before)

    def test_large_submission_response_preserves_actions_and_full_results_by_reference(self):
        from scripts.core.project import query_policy
        from scripts.core.query import _size
        from scripts.core.transport_cache import get
        reason = '本章呈现取舍由作者说明。' * 2000
        result = call_operation('workflow.submit', self.root, {'requestId': 'large-submission', 'chapterId': self.cid,
            'artifacts': [{'artifactType': 'candidate_prose', 'text': BODY, 'writingNotes': {'deviations': [
                {'id': 'D1', 'kind': 'intensityLevel', 'intensityLevel': 1, 'reason': reason}]}}]})
        self.assertTrue(result['ok'], result)
        self.assertLessEqual(_size(result), query_policy()['maxBytes']['detail'])
        self.assertTrue(result['responseCompacted'])
        self.assertEqual(result['stages'][0]['status'], 'registered')
        self.assertTrue(result['approvalAction']['requiresExplicitApproval'])
        self.assertFalse(result['approvalsPerformed'])
        full = get(self.root, result['reportRef'], kind='authoring-report')
        self.assertEqual(full['results'][0]['writingNotes']['deviations'][0]['reason'], reason)
        self.assertEqual(result['resumeAction'], full['resumeAction'])

    def test_combined_candidate_review_group_accepts_only_its_own_invalidations(self):
        args = {'requestId': 'complete-batch', 'chapterId': self.cid,
                'advanceLifecycle': True, 'diagnostics': {'mode': 'plot', 'intensityLevel': 1},
                'artifacts': [{'artifactType': 'candidate_prose', 'text': BODY}, *self.reviews]}
        response = call_operation('workflow.submit', self.root, args)
        self.assertTrue(response['ok'], response)
        self.assertEqual([row['status'] for row in response['stages']], ['registered'] * 4)
        before = inventory(self.root)
        replay = call_operation('workflow.submit', self.root, {'requestId': 'complete-batch'})
        self.assertTrue(replay['ok'], replay)
        self.assertTrue(all(row['replayed'] for row in replay['results']))
        self.assertEqual(inventory(self.root), before)

    def test_large_partial_error_remains_bounded_and_recoverable(self):
        from scripts.core.project import query_policy
        from scripts.core.query import _size
        from scripts.core.transport_cache import get
        message = '保存前发生可恢复的故障。' * 2000
        with patch('scripts.core.authoring_submission.pack_artifact', side_effect=OSError(message)):
            response = self.submit()
        self.assertFalse(response['ok'])
        self.assertLessEqual(_size(response), query_policy()['maxBytes']['detail'])
        full = get(self.root, response['reportRef'], kind='authoring-report')
        self.assertEqual(full['error']['message'], message)
        self.assertEqual(response['resumeAction'], full['resumeAction'])
        self.assertEqual([row['status'] for row in response['stages']], ['pending'] * 3)
        replay = call_operation('workflow.submit', self.root, response['resumeAction']['arguments'])
        self.assertTrue(replay['ok'], replay)

    def test_candidate_survives_diagnostics_failure_and_resumes(self):
        args = {'requestId': 'draft-1', 'chapterId': self.cid,
                'artifacts': [{'artifactType': 'candidate_prose', 'text': BODY, 'writingNotes': {}}],
                'advanceLifecycle': True, 'diagnostics': {'mode': 'plot', 'intensityLevel': 1}}
        with patch('scripts.core.mechanical_evidence.run_diagnostics', side_effect=OSError('runner interrupted')):
            partial = call_operation('workflow.submit', self.root, args)
        self.assertFalse(partial['ok'])
        self.assertEqual(partial['stages'][0]['status'], 'registered')
        repaired = call_operation('workflow.submit', self.root, {'requestId': 'draft-1'})
        self.assertTrue(repaired['ok'], repaired)
        self.assertTrue(repaired['results'][0]['replayed'])
        self.assertTrue(repaired['results'][0]['diagnostics']['registered'])
        self.assertEqual(len(repaired['approvalAction']['arguments']['artifact']), 2)
        before_retry = inventory(self.root)
        repeated = call_operation('workflow.submit', self.root, {'requestId': 'draft-1'})
        self.assertTrue(repeated['ok'], repeated)
        self.assertTrue(repeated['results'][0]['diagnostics']['reused'])
        self.assertEqual(inventory(self.root), before_retry)

    def test_forged_diagnostics_or_mixed_stage_a_never_start(self):
        for artifacts in ([{'artifactType': 'diagnostics', 'payload': {'passed': True}}],
                          [{'artifactType': 'stage_a_review', 'payload': {}}, self.reviews[0]],
                          [self.reviews[0], self.reviews[0]]):
            before = inventory(self.root)
            with self.subTest(artifacts=artifacts), self.assertRaises(ValueError):
                call_operation('workflow.submit', self.root, {'chapterId': self.cid, 'requestId': 'bad', 'artifacts': artifacts})
            self.assertEqual(inventory(self.root), before)

    def test_cli_and_fresh_mcp_accept_inline_submission_and_resume(self):
        args = {'requestId': 'transport-1', 'chapterId': self.cid, 'artifacts': [self.reviews[0]]}
        result = subprocess.run([sys.executable, str(ROOT/'scripts/novelctl.py'), 'project', 'workflow', 'submit',
            '--project-root', str(self.root), '--chapter-id', self.cid, '--request-id', args['requestId'],
            '--artifacts', json.dumps(args['artifacts'], ensure_ascii=False)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cli = json.loads(result.stdout)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'submit_chapter_artifacts',
            'arguments': {'projectRoot': str(self.root), 'requestId': args['requestId']}}}
        mcp = subprocess.run([sys.executable, str(ROOT/'scripts/mcp_server.py')], input=json.dumps(request)+'\n',
                             capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        response = json.loads(mcp.stdout)['result']['structuredContent']
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['stages'], cli['stages'])
        self.assertTrue(response['results'][0]['replayed'])


class ChapterCreationFlowTests(unittest.TestCase):
    def make_project(self, root, single):
        source = initialize_project(root)
        if single:
            preview = call_operation('source.change-preview', root, {'requestId': 'plan-field',
                'baseRevision': source['revision'], 'reason': '夹具声明章节规划字段', 'changes': [
                    {'action': 'put', 'collection': 'fieldDefinitions', 'record': {'fieldId': '$planField',
                     'key': 'plot.plan', 'label': '章节推演', 'valueType': 'topic', 'appliesTo': ['chapter']}}]})
            commit(root, preview)
        return source

    def chapter_args(self, source, single):
        return {'requestId': 'chapter-1', 'title': '交班', 'primaryArcId': source['arcs'][0]['arcId'],
                'volumeId': source['volumes'][0]['volumeId'], 'reason': '夹具提供完整章纲',
                'outlineText': '# 交班\n\n值班员交还钥匙，登记交班结果。\n',
                **({'planField': 'plot.plan', 'dependsOn': [],
                    'chapterContract': {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'targetRange': [10, 80], 'mode': 'plot', 'intensityLevel': 1}} if single else {})}

    def test_create_preview_and_retry_allocate_one_chapter_and_plan(self):
        for single in (True,):
            with self.subTest(single=single), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(); source = self.make_project(root, single)
                args = self.chapter_args(source, single)
                before = (root/'.govel/structure.json').read_bytes()
                preview = call_operation('workflow.chapter-preview', root, args)
                self.assertEqual((root/'.govel/structure.json').read_bytes(), before)
                replay = call_operation('workflow.chapter-preview', root, args)
                self.assertEqual(replay['operationId'], preview['operationId'])
                self.assertEqual(replay['identities'], preview['identities'])
                denied = deepcopy(preview['nextAction']['arguments']); denied['approve'] = False
                with self.assertRaises(ValueError): call_operation('source.commit', root, denied)
                commit(root, preview)
                current = read_json(root/'.govel/structure.json')
                self.assertEqual(len(current['chapters']), 1)
                cid = current['chapters'][0]['chapterId']
                self.assertEqual(cid, preview['identities']['chapterId'])
                self.assertEqual(current['chapters'][0]['status'], 'planning_required')
                if single:
                    plans = [f for f in current['fragments'] if f['ownerId'] == cid]
                    self.assertEqual(len(plans), 1)
                    self.assertEqual(plans[0]['text'], args['outlineText'])
                    self.assertEqual(plans[0]['dependsOn'], [])
                    self.assertNotIn('outlinePath', current['chapters'][0])
                before_retry = inventory(root)
                committed = call_operation('workflow.chapter-preview', root, args)
                self.assertTrue(committed['alreadyCommitted'])
                self.assertEqual(inventory(root), before_retry)
                with self.assertRaises(StructureError) as caught:
                    call_operation('workflow.chapter-preview', root, {**args, 'outlineText': '换了推演。'})
                self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_source_field_and_dependency_errors_leave_canon_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source = self.make_project(root, True)
            for bad in ({'planField': '不存在'}, {'dependsOn': ['SF-MISSING']},
                        {'chapterContract': {'targetRange': [0, 10]}}):
                before = inventory(root)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    call_operation('workflow.chapter-preview', root, {**self.chapter_args(source, True), **bad})
                self.assertEqual(inventory(root), before)

    def test_large_source_plan_keeps_identity_and_readable_frozen_diff(self):
        from scripts.core.project import query_policy
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source = self.make_project(root, True)
            args = {**self.chapter_args(source, True), 'outlineText': '完整章节计划。\n' * 4000}
            preview = call_operation('workflow.chapter-preview', root, args)
            self.assertTrue(preview['responseCompacted'])
            self.assertTrue(preview['identities']['chapterId'].startswith('CH-'))
            self.assertLessEqual(len(json.dumps(preview, ensure_ascii=False, separators=(',', ':')).encode()),
                                 query_policy()['maxBytes']['detail'])
            commit(root, preview)
            source = read_json(root/'.govel/structure.json')
            self.assertEqual(source['fragments'][0]['text'], args['outlineText'])

    def test_current_layouts_reach_finalization_and_closeout_without_caller_files(self):
        for single in (True,):
            with self.subTest(single=single), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(); source = self.make_project(root, single)
                preview = call_operation('workflow.chapter-preview', root, self.chapter_args(source, single))
                commit(root, preview); cid = preview['identities']['chapterId']
                call_operation('project.workflow.approve-chapters', root, {'chapterId': [cid], 'reason': '测试中的显式作者范围批准'})
                stage_a = call_operation('workflow.submit', root, {'requestId': 'stage-a', 'chapterId': cid,
                    'artifacts': [{'artifactType': 'stage_a_review', 'payload': {
                        'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [],
                        'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
                        'seatReviews': {str(seat): {'conclusion': '合成交班计划仅一次钥匙交接，没有承重关系或长期生活安排。',
                            'evidence': [{'text': '值班员交还钥匙，登记交班结果。'}]} for seat in range(1, 11)},
                        'disagreements': [], 'openItems': []}}]})
                self.assertTrue(stage_a['ok'], stage_a)
                with self.assertRaises(ValueError):
                    call_operation('project.workflow.advance', root, {'chapterId': cid, 'to': 'planned'})
                action = stage_a['approvalAction']; call_operation(action['operation'], root, action['arguments'])
                call_operation('project.workflow.advance', root, {'chapterId': cid, 'to': 'planned'})
                prose = call_operation('workflow.submit', root, {'requestId': 'body', 'chapterId': cid,
                    'artifacts': [{'artifactType': 'candidate_prose', 'text': BODY}],
                    'advanceLifecycle': True, 'diagnostics': {'mode': 'plot', 'intensityLevel': 1}})
                self.assertTrue(prose['ok'], prose)
                if single:
                    # Source truth must work without optional legacy projections.
                    for name in ('story-state.json', 'story-design.json'):
                        (root/'.govel/cache'/name).unlink(missing_ok=True)
                reviews = call_operation('workflow.submit', root, {'requestId': 'reviews', 'chapterId': cid, 'artifacts': [
                    {'artifactType': 'stage_b_review', 'payload': {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY',
                        'slots': evidenced_slots(BODY), 'findings': []}},
                    {'artifactType': 'state_candidate', 'payload': {'changes': []}},
                    {'artifactType': 'title_candidates', 'payload': {'candidates': [{'title': '交班', 'reason': '章内事件'}]}},
                ]})
                self.assertTrue(reviews['ok'], reviews)
                for result in (prose, reviews):
                    action = result['approvalAction']; call_operation(action['operation'], root, action['arguments'])
                receipt = call_operation('project.workflow.chapter-receipt', root, {'chapterId': cid, 'titleCandidateId': 'title-1'})
                self.assertTrue(receipt['readyToFinalize'], receipt['blockers'])
                action = receipt['finalizeAction']; finalized = call_operation(action['operation'], root, action['arguments'])
                self.assertFalse(finalized['readyForContinuation'])
                prepared = call_operation('summary.prepare', root, {})
                summary = call_operation('summary.commit-delta', root, {'prepareRef': prepared['prepareRef'],
                    'storySummary': '值班员已经交回钥匙并完成交班登记。',
                    'plotLine': [{'op': 'add', 'chapterId': cid, 'summary': '钥匙归位，交班完成。'}]})
                evidence = call_operation('evidence.prepare', root, {'kind': 'continuity', 'chapterId': cid,
                    'paths': [summary['path']]})
                packed = call_operation('evidence.pack', root, {'snapshotRef': evidence['snapshotRef'],
                    'decisions': {'decisions': [], 'summary': {'resolution': 'refreshed', 'reason': '逐字核对正式正文后重述交班结果'}}})
                with self.assertRaises(ValueError):
                    call_operation('evidence.confirm-continuity', root, {'submissionRef': packed['submissionRef'], 'approve': False})
                call_operation('evidence.confirm-continuity', root, {'submissionRef': packed['submissionRef'], 'approve': True})
                done = call_operation('project.workflow.chapter-receipt', root, {'chapterId': cid, 'view': 'model'})
                self.assertTrue(done['readyForContinuation'], done)
                self.assertEqual((root/done['chapter']['filePath']).read_text(), BODY)
                self.assertEqual(list(root.rglob('*.py')), [])
                if single:
                    self.source_fact_handoff(root, cid)

    def source_fact_handoff(self, root, cid):
        """Caller supplies semantics; existing tools own IDs, hashes and offsets."""
        source = read_json(root/'.govel/structure.json')
        changes = [
            {'action': 'put', 'collection': 'entities', 'record': {'entityId': '$event',
                'entityType': 'event', 'name': '交班完成', 'status': 'active', 'aliases': []}},
            {'action': 'put', 'collection': 'fieldDefinitions', 'record': {'fieldId': '$field',
                'key': 'event.result', 'label': '交班结果', 'valueType': 'topic', 'appliesTo': ['event']}},
            {'action': 'put', 'collection': 'attributes', 'record': {'attributeId': '$result',
                'ownerId': '$event', 'fieldId': '$field', 'values': []}},
            {'action': 'put', 'collection': 'fragments', 'record': {'fragmentId': '$fact',
                'ownerId': '$event', 'order': 1, 'contentRole': 'fact', 'contentBindings': ['$result'],
                'text': '钥匙已经放回桌上。', 'dependsOn': []}},
        ]
        preview = call_operation('source.change-preview', root, {'requestId': 'fact', 'baseRevision': source['revision'],
            'changes': changes, 'reason': '依据本章正式正文登记已发生结果'})
        identities = call_operation('source.change-report', root,
            {'operationId': preview['operationId'], 'section': 'identities'})
        fact_id = next(row['id'] for row in identities['items'] if row['handle'] == '$fact')
        commit(root, preview)
        read = call_operation('evidence.prepare', root, {'kind': 'review', 'recordIds': [fact_id]})
        pack = call_operation('evidence.pack', root, {'snapshotRef': read['snapshotRef'], 'requestId': 'fact-review',
            'decisions': [{'recordId': fact_id, 'decision': 'compatible', 'reason': '陈述只涵盖正文中归还钥匙的动作',
                           'quotes': [{'text': '钥匙已经放回桌上。'}]}]})
        saved = call_operation('evidence.submit', root, {'submissionRef': pack['submissionRef']})
        self.assertTrue(saved['submitted'], saved)
        read = call_operation('evidence.prepare', root, {'kind': 'prose', 'chapterId': cid})
        pack = call_operation('evidence.pack', root, {'snapshotRef': read['snapshotRef'], 'requestId': 'fact-prose',
            'decisions': [{'quote': {'text': '把钥匙放回桌上'}, 'targetIds': [fact_id],
                           'kind': 'mentions', 'reason': '当前正文直接描写了这一结果'}]})
        saved = call_operation('evidence.submit', root, {'submissionRef': pack['submissionRef']})
        self.assertTrue(saved['submitted'], saved)
        self.assertTrue(call_operation('evidence.submit', root, {'requestId': 'fact-prose'})['submitted'])
        prepared = call_operation('summary.prepare', root, {})
        summary = call_operation('summary.commit-delta', root, {'prepareRef': prepared['prepareRef'],
            'storySummary': '值班员已经交回钥匙并完成交班登记，钥匙留在桌上。',
            'plotLine': [{'op': 'replace', 'chapterId': cid, 'summary': '钥匙归位，交班完成。'}]})
        self.assertTrue(summary['ok'], summary)
        done = call_operation('project.workflow.chapter-receipt', root, {'chapterId': cid, 'view': 'full'})
        self.assertTrue(done['readyForContinuation'])
        self.assertFalse(done['summaryFreshness']['stale'])


if __name__ == '__main__':
    unittest.main()
