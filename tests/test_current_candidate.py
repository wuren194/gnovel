"""GN-04: explicit selected identity shared by every execution consumer."""
from __future__ import annotations

from copy import deepcopy
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import envelope
from scripts.core.authoring_forms import prepare_state_candidate, review_form
from scripts.core.authoring_submission import submit_artifacts
from scripts.core.candidate_versions import activate_candidate_version, save_candidate_version
from scripts.core.current_candidate import prepare_candidate_handoff, selected_candidate
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import candidate_prose, evidence_current, run_diagnostics
from scripts.core.operations import call_operation
from scripts.core.review_localization import review_report_view
from scripts.core.structure_store import load_structure
from scripts.core.task_prepare import prepare_task
from scripts.core.workflow import approve_artifacts, record_artifact
from scripts.dashboard_server import DashboardHandler
from tests.fixtures_workflow import evidenced_slots, review_ready_project

ROOT = Path(__file__).resolve().parents[1]
OLD = '他把钥匙放回桌上，合上了登记本。'
NEW = '他把钥匙放进抽屉，关好柜门，走出了仓库。'
REVIEW = {'planApplicabilityReason': '已逐句核对当前计划及阶段A；仅调整已定动作的展开，未增加事实或改变人物责任。'}


class CurrentCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='current-candidate-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = review_ready_project(self.root)
        self.cid = self.fixture['chapterIds'][0]
        self.source = load_structure(self.root)

    def workflow(self):
        return read_json(self.root / '.gnovel/workflow.json')

    def activate(self, text=NEW, *, base=None, token=0, request='selection'):
        result = save_candidate_version(self.root, self.cid, body=text, request_id=request, base_candidate_version=base)
        activate_candidate_version(self.root, self.cid, candidate_version=result['version'], expected_active_token=token)
        return result

    def handoff(self, **changes):
        frozen = prepare_candidate_handoff(self.root, chapterId=self.cid)
        return frozen, pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
            text=frozen['text'], selectionRef=frozen['selectionRef'], selectionReview={**REVIEW, **changes},
            requestId='handoff-registration')

    def approve(self, *kinds):
        workflow = self.workflow()
        approve_artifacts(workflow, self.cid, artifact_hashes={kind: workflow['artifacts'][self.cid][kind]['artifactSha256'] for kind in kinds},
                          approval_mode='manual', project_root=self.root)
        write_json(self.root / '.gnovel/workflow.json', workflow)

    def test_saving_unselected_candidate_keeps_current_execution_and_approvals(self):
        before = self.workflow()
        evidence = evidence_current(self.root, self.cid)
        form = review_form(self.root, chapterId=self.cid, artifactType='stage_b_review', view='full')
        save_candidate_version(self.root, self.cid, body=NEW, request_id='not-active', base_candidate_version=None)
        self.assertEqual(self.workflow(), before)
        self.assertEqual(candidate_prose(self.root, self.cid)['text'], OLD)
        self.assertEqual(evidence_current(self.root, self.cid), evidence)
        self.assertTrue(preflight_finalization(self.root, self.cid, skip_title=True).ready)
        from scripts.core.transport_cache import get
        from scripts.core.prepare_dependencies import validate_dependencies
        frozen = get(self.root, form['formRef'], kind='review-form')
        validate_dependencies(self.root, frozen['dependencies'], code='REVIEW_FORM_STALE')

    def test_relocation_keeps_registered_selected_identity_but_not_a_later_activation(self):
        from scripts.core.project_relocate import preview_relocate_project, commit_relocate_project
        self.activate(OLD)
        _, packed = self.handoff()
        run_diagnostics(self.root, chapter_id=self.cid, register=True)
        before = self.workflow()
        destination = tempfile.TemporaryDirectory(prefix='selected-move-'); self.addCleanup(destination.cleanup)
        target = Path(destination.name).resolve() / 'moved'
        plan = preview_relocate_project(self.root, target=str(target))
        commit_relocate_project(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.root = target
        self.assertEqual(self.workflow(), before)
        self.assertEqual(candidate_prose(self.root, self.cid)['text'], OLD)
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])
        review_form(self.root, chapterId=self.cid, artifactType='stage_b_review')
        self.activate(OLD, base=1, token=1, request='after-move')
        with self.assertRaises(ValueError) as caught:
            candidate_prose(self.root, self.cid)
        self.assertEqual(caught.exception.code, 'CURRENT_CANDIDATE_UNALIGNED')

    def test_activation_blocks_diagnostics_forms_state_approval_and_finalize_of_old_artifact(self):
        self.activate()
        source = load_structure(self.root)
        generation = source['chapters'][0]['proseBinding']['generation']
        operations = [
            lambda: candidate_prose(self.root, self.cid),
            lambda: run_diagnostics(self.root, chapter_id=self.cid, mode='plot', intensity_level=1, register=True),
            lambda: review_form(self.root, chapterId=self.cid, artifactType='stage_b_review'),
            lambda: prepare_state_candidate(self.root, chapterId=self.cid, requestId='blocked-state'),
            lambda: self.approve('candidate_prose'),
            lambda: pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=OLD),
        ]
        before = self.workflow()
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(ValueError) as caught:
                operation()
            self.assertEqual(caught.exception.code, 'CURRENT_CANDIDATE_UNALIGNED')
            self.assertEqual(caught.exception.details['recovery']['readActions'][0]['operation'], 'workflow.candidate-handoff')
        self.assertEqual(self.workflow(), before)
        prep = prepare_task(self.root, task='editor', chapter_id=self.cid)
        self.assertIn('CURRENT_CANDIDATE_UNALIGNED', json.dumps(prep))
        check = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertFalse(check.ready)
        self.assertIn('CURRENT_CANDIDATE_UNALIGNED', str(check.blockers))
        with self.assertRaises(ValueError):
            finalize_chapter(self.root, self.cid, skip_title=True, binding_generation=generation)
        self.assertFalse((self.root / source['chapters'][0]['filePath']).exists())
        history = review_report_view(self.root, self.cid)
        self.assertFalse(history['canAnnotate'])
        self.assertEqual(history['versionBinding']['state'], 'historical')

    def test_real_handoff_review_approval_and_finalization_publish_only_selected_text(self):
        self.activate()
        frozen, packed = self.handoff()
        artifact = read_json(self.root / packed['artifactPath'])
        self.assertEqual(artifact['payload']['candidateSelection'], frozen['selection'])
        self.assertFalse(packed['approved'])
        self.assertEqual(candidate_prose(self.root, self.cid)['text'], NEW)
        diagnostic = run_diagnostics(self.root, chapter_id=self.cid, mode='plot', intensity_level=1, register=True)
        self.assertTrue(diagnostic['evidence']['passed'])
        pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', payload={
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(NEW), 'findings': [],
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
            'factAssessment': {'status': 'no_change'}})
        prepare_state_candidate(self.root, chapterId=self.cid, requestId='selected-state')
        self.approve('candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate')
        source = load_structure(self.root)
        self.assertTrue(preflight_finalization(self.root, self.cid, skip_title=True).ready)
        result = finalize_chapter(self.root, self.cid, skip_title=True, binding_generation=source['chapters'][0]['proseBinding']['generation'])
        self.assertEqual(result['code'], 'CHAPTER_FINALIZED')
        self.assertEqual((self.root / source['chapters'][0]['filePath']).read_text(), NEW)

    def test_hashes_without_current_approval_cannot_authorize_selection_handoff(self):
        self.activate()
        frozen = prepare_candidate_handoff(self.root, chapterId=self.cid)
        workflow = self.workflow()
        workflow['approvals'][self.cid]['approved'] = False
        write_json(self.root / '.gnovel/workflow.json', workflow)
        current = prepare_candidate_handoff(self.root, chapterId=self.cid)
        self.assertFalse(current['stageAApproved'])
        for reference in (frozen['selectionRef'], current['selectionRef']):
            with self.assertRaises(ValueError) as caught:
                pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=NEW,
                              selectionRef=reference, selectionReview=REVIEW)
            self.assertEqual(caught.exception.code, 'CANDIDATE_STAGE_A_STALE')
        self.assertEqual(self.workflow(), workflow)

    def test_same_bytes_new_identity_aba_and_cross_project_refs_never_borrow_selection(self):
        self.activate(OLD)
        frozen, packed = self.handoff()
        self.assertEqual(candidate_prose(self.root, self.cid)['text'], OLD)
        self.activate(OLD, base=1, token=1, request='same-body-new-identity')
        with self.assertRaises(ValueError) as caught:
            candidate_prose(self.root, self.cid)
        self.assertEqual(caught.exception.code, 'CURRENT_CANDIDATE_UNALIGNED')
        activate_candidate_version(self.root, self.cid, candidate_version=1, expected_active_token=2)
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=OLD,
                selectionRef=frozen['selectionRef'], selectionReview=REVIEW, requestId='handoff-registration')
        self.assertEqual(caught.exception.code, 'CANDIDATE_HANDOFF_STALE')
        with tempfile.TemporaryDirectory() as other:
            from scripts.core.transport_cache import get
            with self.assertRaises(ValueError) as caught:
                get(Path(other), frozen['selectionRef'], kind='candidate-handoff')
            self.assertEqual(caught.exception.code, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')
        # A low-level full envelope also needs the matching explicit selected identity.
        candidate = read_json(self.root / packed['artifactPath'])
        candidate['payload'].pop('candidateSelection')
        candidate = envelope('candidate_prose', candidate['payload'], project_id=self.source['projectId'], chapter_id=self.cid)
        with self.assertRaises(ValueError) as caught:
            record_artifact(self.workflow(), self.cid, candidate, structure=load_structure(self.root), project_root=self.root)
        self.assertEqual(caught.exception.code, 'CURRENT_CANDIDATE_UNALIGNED')

    def test_frozen_handoff_preserves_explicit_note_disposition_and_replay_identity(self):
        notes = {'deviations': [{'id': 'N-1', 'kind': 'intensityLevel', 'intensityLevel': 1,
                                'reason': '已按当前计划采用最低层级，没有新增升级情节。'}], 'newFacts': []}
        pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=OLD, writingNotes=notes)
        self.activate()
        frozen = prepare_candidate_handoff(self.root, chapterId=self.cid)
        before = self.workflow()
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=NEW,
                selectionRef=frozen['selectionRef'], selectionReview=REVIEW)
        self.assertEqual(caught.exception.code, 'WRITING_NOTES_REQUIRED')
        self.assertEqual(self.workflow(), before)
        review = {**REVIEW, 'notesDisposition': 'retained', 'notesReason': '逐条检查原写作说明，修改后的动作仍适用同一层级要求。'}
        args = {'chapterId': self.cid, 'artifactType': 'candidate_prose', 'text': NEW,
                'selectionRef': frozen['selectionRef'], 'selectionReview': review, 'writingNotes': notes, 'requestId': 'notes-handoff'}
        first = pack_artifact(self.root, **args)
        again = pack_artifact(self.root, **args)
        self.assertTrue(again['replayed'])
        self.assertEqual(first['artifactSha256'], again['artifactSha256'])
        self.assertEqual(read_json(self.root / first['artifactPath'])['payload']['writingNotes'], notes)
        save_candidate_version(self.root, self.cid, body='未激活旁支。', request_id='unselected-later', base_candidate_version=1)
        self.assertTrue(pack_artifact(self.root, **args)['replayed'])
        with self.assertRaises(ValueError):
            pack_artifact(self.root, **{**args, 'selectionReview': {**review, 'notesReason': '新理由不得改绑原请求。'}})

    def test_submission_freezes_ref_and_resumes_same_artifact(self):
        self.activate()
        frozen = prepare_candidate_handoff(self.root, chapterId=self.cid)
        result = submit_artifacts(self.root, requestId='selected-submit', chapterId=self.cid,
            artifacts=[{'artifactType': 'candidate_prose', 'text': NEW,
                        'selectionRef': frozen['selectionRef'], 'selectionReview': REVIEW}])
        self.assertTrue(result['ok'], result)
        expected = self.workflow()['artifacts'][self.cid]['candidate_prose']['artifactSha256']
        again = submit_artifacts(self.root, requestId='selected-submit')
        self.assertTrue(again['ok'], again)
        self.assertEqual(self.workflow()['artifacts'][self.cid]['candidate_prose']['artifactSha256'], expected)
        self.activate(NEW, base=1, token=1, request='after-submit')
        refused = submit_artifacts(self.root, requestId='selected-submit')
        self.assertFalse(refused['ok'])
        self.assertEqual(self.workflow()['artifacts'][self.cid]['candidate_prose']['artifactSha256'], expected)

    def test_corrupt_selected_body_and_interrupted_registration_do_not_fall_back(self):
        saved = self.activate()
        frozen = prepare_candidate_handoff(self.root, chapterId=self.cid)
        original = self.workflow()
        def interrupt(phase, path=None):
            if phase == 'write' and path == '.gnovel/workflow.json':
                raise RuntimeError('injected after publication before commit')
        with patch('scripts.core.write_sets.checkpoint', side_effect=interrupt), self.assertRaises(RuntimeError):
            pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', text=NEW,
                selectionRef=frozen['selectionRef'], selectionReview=REVIEW, requestId='interrupted-handoff')
        self.assertEqual(self.workflow(), original)
        body = self.root / '.gnovel/workbench/candidates' / self.cid / saved['fileName']
        self.assertEqual(body.read_text(), NEW)
        body.write_text('外改候选的字节。')
        for operation in (lambda: candidate_prose(self.root, self.cid), lambda: prepare_candidate_handoff(self.root, chapterId=self.cid)):
            with self.assertRaises(ValueError) as caught:
                operation()
            self.assertEqual(caught.exception.code, 'CANDIDATE_VERSION_CORRUPT')
        self.assertEqual(self.workflow(), original)

    def test_cli_mcp_and_http_handoff_share_frozen_identity(self):
        self.activate()
        expected = call_operation('workflow.candidate-handoff', self.root, {'chapterId': self.cid})
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'workflow', 'candidate-handoff',
                                  '--project-root', str(self.root), '--chapter-id', self.cid], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        cli = json.loads(process.stdout)
        from scripts.mcp_server import call_tool
        mcp = call_tool('prepare_candidate_handoff', {'projectRoot': str(self.root), 'chapterId': self.cid})
        server = ThreadingHTTPServer(('127.0.0.1', 0), DashboardHandler)
        server.daemon_threads = True
        server.project_root = str(self.root)
        server.mutation_token = 'current-candidate-http'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('GET', '/api/candidate/handoff?chapterId=' + self.cid,
                               headers={'X-gnovel-Token': server.mutation_token})
            response = connection.getresponse()
            http_result = json.loads(response.read())
            self.assertEqual(response.status, 200, http_result)
            connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        for response in (cli, mcp, http_result):
            self.assertEqual(response['selectionRef'], expected['selectionRef'])
            self.assertEqual(response['selection'], expected['selection'])
            self.assertFalse(response['approved'])


if __name__ == '__main__':
    unittest.main()
