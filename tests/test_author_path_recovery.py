"""Follow advertised recovery actions through actual shared operations."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import novelctl
from scripts.core.chapter_creation import preview_chapter
from scripts.core.operations import call_operation
from scripts.core.source_reports import public_report
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_current import initialize_project, create_schema5_project


class AuthorPathRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_empty_new_book_can_follow_error_to_field_and_first_chapter(self):
        source = initialize_project(self.root)
        self.assertEqual(source['fieldDefinitions'], [])
        args = {'requestId': 'first-chapter', 'title': '交班', 'primaryArcId': source['arcs'][0]['arcId'],
                'volumeId': source['volumes'][0]['volumeId'], 'outlineText': '两人核对钥匙后交班。', 'reason': '建立首章'}
        with self.assertRaises(StructureError) as caught:
            preview_chapter(self.root, **args)
        self.assertEqual(caught.exception.code, 'CHAPTER_PLAN_FIELD_REQUIRED')
        detail = caught.exception.details
        self.assertEqual(detail['availablePlanFields'], [])
        action = deepcopy(detail['createFieldAction'])
        action['arguments'].update(requestId='field-1', reason='确认建立章节计划字段')
        action['arguments']['changes'][0]['record'].update(key='plot.plan', label='章节推演')
        field_preview = call_operation(action['operation'], self.root, action['arguments'])
        self.assertTrue(field_preview['ok'])
        self.assertNotIn('proposedStructure', field_preview)
        commit = field_preview['nextAction']; call_operation(commit['operation'], self.root, commit['arguments'])
        with self.assertRaises(StructureError) as caught:
            preview_chapter(self.root, **args, planField='wrong-key')
        available = caught.exception.details['availablePlanFields']
        self.assertEqual(len(available), 1)
        self.assertNotIn('createFieldAction', caught.exception.details)
        preview = preview_chapter(self.root, **args, planField=available[0]['fieldId'])
        commit = preview['nextAction']; call_operation(commit['operation'], self.root, commit['arguments'])
        self.assertEqual(len(load_structure(self.root)['chapters']), 1)
        self.assertTrue(preview_chapter(self.root, **args, planField=available[0]['fieldId'])['alreadyCommitted'])

    def test_external_preview_projection_never_advertises_closed_commit(self):
        for huge in (False, True):
            report = {'operationId': 'OP-EXTERNAL', 'operation': 'accept_external_modification',
                      'requiresApproval': True, 'planHash': 'frozen', 'parameters': {'chapterId': 'CH-TEST'},
                      'impact': {'changedIds': ['x' * 60 + str(i) for i in range(1000)] if huge else []},
                      'externalReviewMaterials': {'availability': 'missing_accepted_bytes',
                          'missingPrerequisites': ['accepted_prose_bytes'], 'adoptionExecutable': False}}
            result = public_report(report, root=self.root)
            self.assertTrue(result['ok'])
            self.assertFalse(result['execution']['executable'])
            self.assertIn('accepted_prose_bytes', result['execution']['missingPrerequisites'])
            self.assertNotIn('nextAction', result)
        error = public_report({'ok': False, 'code': 'FAILED', 'operationId': 'OP-ERR',
                               'planHash': 'frozen', 'requiresApproval': True})
        self.assertFalse(error['ok'])

    def test_chapter_approval_reports_replacement_and_removed_scope(self):
        fixture = create_schema5_project(self.root, chapter_count=2)
        def approve(chapters):
            out, err = io.StringIO(), io.StringIO()
            args = ['project', 'workflow', 'approve-chapters', '--project-root', str(self.root)]
            for chapter in chapters: args += ['--chapter-id', chapter]
            with redirect_stdout(out), redirect_stderr(err): code = novelctl.main(args)
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            return json.loads(out.getvalue())
        first = approve(fixture['chapterIds'])
        second = approve(fixture['chapterIds'][:1])
        self.assertEqual(first['scopeChange']['mode'], 'replace')
        self.assertEqual(second['scopeChange']['before'], fixture['chapterIds'])
        self.assertEqual(second['scopeChange']['after'], fixture['chapterIds'][:1])
        self.assertEqual(second['scopeChange']['removed'], fixture['chapterIds'][1:])
        self.assertEqual(second['scopeChange']['added'], [])

    def test_stage_a_preflight_lists_independent_plan_coverage_gaps(self):
        from scripts.core.authoring_forms import review_form
        from scripts.core.jsonio import write_json
        fixture = create_schema5_project(self.root, chapter_count=1)
        source = fixture['structure']
        source['fragments'][0]['chapterContract']['behaviorCoverage'] = {
            'relationship': 'applicable', 'living': 'applicable'}
        write_json(self.root / '.gnovel/structure.json', source)
        result = review_form(self.root, chapterId=fixture['chapterIds'][0], artifactType='stage_a_review', view='full')
        preflight = result['planPreflight']
        self.assertFalse(preflight['readyForCompletionReview'])
        self.assertEqual({row['domain'] for row in preflight['issues'] if 'domain' in row}, {'relationship', 'living'})
        self.assertEqual(result['payload']['decision'], 'pending')
        self.assertFalse(result['approved'])
        self.assertTrue(preflight['draftSaveAllowed'])

    def test_replacing_artifact_receipt_does_not_inherit_approval(self):
        from scripts.core.artifact_pack import pack_artifact
        from scripts.core.jsonio import read_json
        from tests.fixtures_workflow import review_ready_project
        cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        old = read_json(self.root / '.gnovel/workflow.json')['artifacts'][cid]['candidate_prose']['artifactSha256']
        result = pack_artifact(self.root, chapterId=cid, artifactType='candidate_prose', text='重新交班的中性测试正文。', requestId='replace')
        replacement = result['replacement']
        self.assertEqual(replacement['previousArtifactSha256'], old)
        self.assertEqual(replacement['newArtifactSha256'], result['artifactSha256'])
        self.assertTrue(replacement['previouslyApproved'])
        self.assertFalse(replacement['approvalInherited'])
        replay = pack_artifact(self.root, chapterId=cid, artifactType='candidate_prose', text='重新交班的中性测试正文。', requestId='replace')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['replacement'], replacement)

    def test_advance_recovery_only_returns_real_read_actions(self):
        from scripts.core.chapter_state import advance_chapter_status
        from scripts.core.operations import operations
        fixture = create_schema5_project(self.root, chapter_count=1)
        with self.assertRaises(ValueError) as caught:
            advance_chapter_status(self.root, fixture['chapterIds'][0], 'planned')
        recovery = caught.exception.details['recovery']
        self.assertFalse(recovery['automatic'])
        specs = {spec.id: spec for spec in operations()}
        for action in recovery['readActions']:
            self.assertIn(action['operation'], specs)
            self.assertNotIn('approve', action['arguments'])
