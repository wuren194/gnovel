"""Three real candidate chapters, conditional inputs, partial reading and closeout."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import signal
from unittest.mock import patch
import tempfile
import unittest

from scripts.core.artifact_pack import pack_artifact
from scripts.core.authoring_forms import review_form, prepare_state_candidate
from scripts.core.candidate_batch import (prepare_candidate_batch, prepare_candidate_recipe, batch_progress,
                                         validate_recipe, require_conversion)
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.continuity import continuity_preview, complete_continuity
from scripts.core.finalization import finalize_chapter, reconcile_workflow
from scripts.core.jsonio import read_json, write_json
from scripts.core.manuscript_export import export_candidate_batch
from scripts.core.mechanical_evidence import evidence_current
from scripts.core.prose_binding import seal_binding
from scripts.core.structure_store import load_structure
from scripts.core.workflow import approve_artifacts, default_workflow
from tests.fixtures_current import create_schema5_project
from tests.fixtures_workflow import evidenced_slots, assessed_review_payload

ROOT = Path(__file__).resolve().parents[1]

TEXT = '他把钥匙放回桌上，合上了登记本。'


class CandidateBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='candidate-batch-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        fixture = create_schema5_project(self.root, chapter_count=4)
        self.ids = fixture['chapterIds']
        source = fixture['structure']
        for chapter in source['chapters']:
            chapter['status'] = 'planning_required'
            (self.root / chapter['filePath']).unlink()
            chapter['proseBinding'] = seal_binding(source['projectId'], chapter['chapterId'], chapter['filePath'],
                state='unbound', generation=source['revision'], expected_content_digest=None)
        write_json(self.root / '.govel/structure.json', source)
        workflow = default_workflow(); workflow['approvedChapterIds'] = self.ids
        write_json(self.root / '.govel/workflow.json', workflow)
        self.batch = prepare_candidate_batch(self.root, requestId='three-candidates', chapterIds=self.ids[:3])['batchRef']

    def workflow(self): return read_json(self.root / '.govel/workflow.json')

    def cli(self, action, **arguments):
        from scripts.core.catalog_api import snake_case
        command = [sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'workflow', action, '--project-root', str(self.root)]
        for key, value in arguments.items():
            command.append('--' + snake_case(key).replace('_', '-'))
            if value is True: continue
            command.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def mcp(self, name, **arguments):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': {'projectRoot': str(self.root), **arguments}}}
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')], input=json.dumps(request) + '\n',
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)['result']
        self.assertFalse(value['isError'], value)
        return value['structuredContent']

    def approve(self, cid, kinds):
        workflow = self.workflow()
        approve_artifacts(workflow, cid, artifact_hashes={kind: workflow['artifacts'][cid][kind]['artifactSha256'] for kind in kinds},
                          approval_mode='manual', project_root=self.root)
        write_json(self.root / '.govel/workflow.json', workflow)

    def chapter(self, cid, *, stop_after_a=False):
        recipe = prepare_candidate_recipe(self.root, batchRef=self.batch, chapterId=cid)
        form = review_form(self.root, chapterId=cid, artifactType='stage_a_review', batchRef=self.batch, view='full')
        payload = assessed_review_payload(load_structure(self.root), cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': form['payload']['activeSeats'], 'candidateRecipe': recipe['recipe'],
            'consensus': [], 'disagreements': [], 'openItems': []})
        pack_artifact(self.root, chapterId=cid, artifactType='stage_a_review', payload=payload, recipeRef=recipe['recipeRef'])
        self.approve(cid, ['stage_a_review'])
        advance_chapter_status(self.root, cid, 'planned')
        if stop_after_a: return recipe
        packed = pack_artifact(self.root, chapterId=cid, artifactType='candidate_prose', text=TEXT,
            advanceLifecycle=True, diagnostics={'mode': 'plot', 'intensityLevel': 1})
        self.assertTrue(packed['ok'], packed)
        pack_artifact(self.root, chapterId=cid, artifactType='stage_b_review', payload={
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(TEXT), 'findings': [{
                'severity': 'S3', 'ruleId': 'OPTIONAL', 'summary': '可选地调整末句停顿。',
                'location': TEXT, 'repairSuggestion': '现有完整动作也可以保留。'}],
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
            'factAssessment': {'status': 'no_change'}})
        return recipe

    def test_three_candidates_ready_without_formal_approval_and_partial_delivery(self):
        self.chapter(self.ids[0])
        second = self.chapter(self.ids[1], stop_after_a=True)
        report = export_candidate_batch(self.root, batchRef=self.batch, output='工作台/阅读稿/部分候选.md')
        self.assertFalse(report['complete'])
        reading = Path(report['output']).read_text()
        self.assertIn('部分交付', reading)
        self.assertIn(TEXT, reading)
        self.assertIn('可选地调整末句停顿', reading)
        self.assertIn('尚未开始', reading)
        with self.assertRaises(ValueError):
            prepare_candidate_recipe(self.root, batchRef=self.batch, chapterId=self.ids[3])
        # Resume B at its existing, approved A; neither chapter one nor two is finalized.
        result = pack_artifact(self.root, chapterId=self.ids[1], artifactType='candidate_prose', text=TEXT,
            advanceLifecycle=True, diagnostics={'mode': 'plot', 'intensityLevel': 1}, requestId='resume-second')
        self.assertTrue(result['ok'], result)
        pack_artifact(self.root, chapterId=self.ids[1], artifactType='stage_b_review', payload={
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(TEXT), 'findings': [],
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}})
        third = self.chapter(self.ids[2])
        progress = batch_progress(self.root, batchRef=self.batch)
        self.assertTrue(progress['complete'], progress)
        reconciled = reconcile_workflow(self.root)
        self.assertTrue(reconciled['ok'], reconciled)
        self.assertEqual(reconciled['nextAction']['action'], 'await_author')
        for cid in self.ids[:3]:
            chapter = next(c for c in load_structure(self.root)['chapters'] if c['chapterId'] == cid)
            self.assertFalse((self.root / chapter['filePath']).exists())
            self.assertNotIn('stage_b_review', self.workflow()['approvals'][cid]['artifactHashes'])
            with self.assertRaises(ValueError):
                finalize_chapter(self.root, cid, skip_title=True, binding_generation=chapter['proseBinding']['generation'])
        full = export_candidate_batch(self.root, batchRef=self.batch, output='工作台/阅读稿/整批候选.txt', fmt='txt')
        self.assertTrue(full['complete'])
        self.assertEqual(len(full['candidateIdentities']), 3)
        self.assertEqual(len(third['recipe']['inputs']), 2)
        self.assertEqual(second['recipe']['inputs'][0]['materialRole'], 'conditional-candidate-not-canon')

    def test_upstream_same_text_new_artifact_invalidates_only_dependent_recipe(self):
        self.chapter(self.ids[0])
        second = self.chapter(self.ids[1], stop_after_a=True)
        before_third = deepcopy(self.workflow()['artifacts'].get(self.ids[2]))
        pack_artifact(self.root, chapterId=self.ids[0], artifactType='candidate_prose', text=TEXT)
        with self.assertRaises(ValueError):
            validate_recipe(self.root, load_structure(self.root), self.workflow(), self.ids[1], second['recipe'])
        with self.assertRaises(ValueError):
            pack_artifact(self.root, chapterId=self.ids[1], artifactType='candidate_prose', text=TEXT)
        self.assertEqual(self.workflow()['artifacts'].get(self.ids[2]), before_third)
        self.assertEqual(prepare_candidate_batch(self.root, requestId='three-candidates')['batchRef'], self.batch)

    def test_candidate_continuation_status_and_public_next_action_share_predecessor_gate(self):
        from scripts.core.chapter_status import chapter_status
        from scripts.core.project_doctor import project_doctor
        from scripts.core.workflow import set_next_action, current_next_action
        self.chapter(self.ids[0]); self.chapter(self.ids[1], stop_after_a=True)
        board = chapter_status(self.root, self.ids[1])
        self.assertNotIn('PREDECESSOR_UNSTABLE', [row['code'] for row in board['blockers']])
        workflow, source = self.workflow(), load_structure(self.root)
        set_next_action(workflow, source, root=self.root, chapter_id=self.ids[1], action='writer')
        self.assertEqual(current_next_action(workflow, source, root=self.root)['chapterId'], self.ids[1])
        result = self.cli('set-next', chapterId=self.ids[1], nextAction='writer')
        self.assertTrue(result['ok'], result)
        report = project_doctor(self.root)
        self.assertNotIn('PREDECESSOR_UNSTABLE', [row['code'] for row in report.get('issues', [])])

    def test_repeated_project_relocation_preserves_frozen_batch_artifacts_and_form(self):
        from scripts.core.project_relocate import preview_relocate_project, commit_relocate_project
        from scripts.core.mechanical_evidence import run_diagnostics
        from scripts.core.review_form_edits import resolve_form
        from scripts.core.paths import stored_project_path, safe_project_path
        self.chapter(self.ids[0]); self.chapter(self.ids[1])
        third_form = review_form(self.root, chapterId=self.ids[2], artifactType='stage_a_review', batchRef=self.batch)
        workflow = self.workflow()
        originals = {entry['artifactPath']: Path(entry['artifactPath']).read_bytes()
                     for entries in workflow['artifacts'].values() for entry in entries.values()}
        destination = tempfile.TemporaryDirectory(prefix='relocated-candidates-')
        self.addCleanup(destination.cleanup)
        for name in ('first', 'second'):
            target = Path(destination.name).resolve() / name
            plan = preview_relocate_project(self.root, target=str(target))
            commit_relocate_project(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
            self.root = target
            self.assertEqual(self.workflow(), workflow)
            for pointer, raw in originals.items():
                self.assertEqual(stored_project_path(self.root, pointer).read_bytes(), raw)
            progress = batch_progress(self.root, batchRef=self.batch)
            self.assertFalse(progress['complete'])
            self.assertTrue(evidence_current(self.root, self.ids[1])['fresh'])
            reused = run_diagnostics(self.root, chapter_id=self.ids[1], register=True)
            self.assertTrue(reused['reused'])
            resolve_form(self.root, chapter_id=self.ids[2], artifact_type='stage_a_review',
                         reference=third_form['formRef'], edits={})
        first = self.ids[0]
        prepare_state_candidate(self.root, chapterId=first, requestId='state-after-move')
        self.approve(first, ['candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate'])
        chapter = load_structure(self.root)['chapters'][0]
        finalized = finalize_chapter(self.root, first, skip_title=True, binding_generation=chapter['proseBinding']['generation'])
        self.assertEqual(finalized['code'], 'CHAPTER_FINALIZED')
        preview = continuity_preview(self.root, first)
        complete_continuity(self.root, first, batch_hash=preview['batchHash'], approved=True,
            confirmation={'decisions': [], 'summary': {'resolution': 'not_required', 'reason': '中性搬家回归，无新增摘要事实。'}})
        self.assertTrue(evidence_current(self.root, self.ids[1])['fresh'])
        # An unrelated file newly occupying the old address is never read.
        pointer = next(iter(originals)); Path(pointer).parent.mkdir(parents=True, exist_ok=True)
        Path(pointer).write_text('unrelated replacement')
        self.assertEqual(stored_project_path(self.root, pointer).read_bytes(), originals[pointer])
        with self.assertRaises(ValueError):
            safe_project_path(self.root, pointer, allow_absolute=True)
        receipt_path = self.root / '.govel/project-locator.json'
        receipt = read_json(receipt_path); receipt['projectId'] = 'PRJ-OTHER'
        write_json(receipt_path, receipt)
        with self.assertRaises(ValueError):
            stored_project_path(self.root, pointer)

    def test_three_chapter_cli_and_stdio_mcp_submission_chain(self):
        for cid in self.ids[:3]:
            form = self.cli('review-form', chapterId=cid, artifactType='stage_a_review', batchRef=self.batch, view='full')
            payload = assessed_review_payload(load_structure(self.root), cid, 'stage_a_review', {
                'decision': 'adopt', 'activeSeats': form['payload']['activeSeats'], 'candidateRecipe': form['payload']['candidateRecipe'],
                'consensus': [], 'disagreements': [], 'openItems': []})
            self.cli('artifact-pack', chapterId=cid, artifactType='stage_a_review', payload=payload)
            self.cli('artifact-approve', chapterId=cid,
                     artifactHash='stage_a_review=' + self.workflow()['artifacts'][cid]['stage_a_review']['artifactSha256'])
            self.cli('advance', chapterId=cid, to='planned')
            self.mcp('submit_chapter_artifacts', requestId='batch-' + cid, chapterId=cid,
                     artifacts=[{'artifactType': 'candidate_prose', 'text': TEXT}], advanceLifecycle=True,
                     diagnostics={'mode': 'plot', 'intensityLevel': 1})
            self.mcp('submit_chapter_artifacts', requestId='review-' + cid, chapterId=cid,
                     artifacts=[{'artifactType': 'stage_b_review', 'payload': {
                         'verdict': 'POLISH_READY', 'slots': evidenced_slots(TEXT), 'findings': [],
                         'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
                         'factAssessment': {'status': 'no_change'}}}])
        progress = self.cli('candidate-progress', batchRef=self.batch)
        self.assertTrue(progress['complete'])
        exported = self.mcp('export_candidate_batch', batchRef=self.batch, output='工作台/传输候选.md')
        self.assertTrue(exported['complete'])
        self.assertEqual(len(exported['candidateIdentities']), 3)
        for chapter in load_structure(self.root)['chapters']:
            self.assertFalse((self.root / chapter['filePath']).exists())

    def test_conditional_facts_are_readable_but_never_canon_or_formal_evidence(self):
        self.chapter(self.ids[0])
        cid = self.ids[0]
        findings = [{'findingId': 'F-CANDIDATE-ONLY', 'severity': 'S3', 'ruleId': 'FACT-INVENTORY',
                     'summary': '钥匙已归还桌面，后章仅按该候选条件继续。', 'location': TEXT,
                     'repairSuggestion': '正式采用后按受审源提案处理。',
                     'factChange': {'status': 'pending', 'requestId': 'candidate-key-return',
                                    'reason': '候选中明确发生归还动作，尚未建立正式事实。',
                                    'evidence': [{'text': TEXT}]}}]
        before = (self.root / '.govel/structure.json').read_bytes()
        pack_artifact(self.root, chapterId=cid, artifactType='stage_b_review', payload={
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(TEXT), 'findings': findings,
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
            'factAssessment': {'status': 'changes'}})
        recipe = prepare_candidate_recipe(self.root, batchRef=self.batch, chapterId=self.ids[1])
        item = recipe['recipe']['inputs'][0]
        self.assertEqual(item['conditionalFacts'][0]['findingId'], 'F-CANDIDATE-ONLY')
        self.assertEqual(item['materialRole'], 'conditional-candidate-not-canon')
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)
        self.assertFalse((self.root / '.govel/source-prose-evidence.json').exists())
        from scripts.core.fact_inventory import inventory_status
        self.assertFalse(inventory_status(self.root, load_structure(self.root), self.workflow(), cid)['complete'])
        self.assertNotIn('stage_b_review', self.workflow()['approvals'][cid]['artifactHashes'])

    def test_real_sigkill_after_candidate_commit_resumes_original_submission_once(self):
        self.chapter(self.ids[0]); self.chapter(self.ids[1], stop_after_a=True)
        cid = self.ids[1]
        code = r'''
import json,os,signal,sys
from pathlib import Path
from scripts.core import write_sets
from scripts.core.authoring_submission import submit_artifacts
root=Path(sys.argv[1]); cid=sys.argv[2]
def checkpoint(stage, relative=None):
    if stage == 'committed':
        pending=[json.loads(p.read_text()) for p in (root/'.govel/transactions').glob('*.json')]
        if any(p.get('operation')=='artifact_pack' and p.get('phase')=='committed' for p in pending):
            os.kill(os.getpid(),signal.SIGKILL)
write_sets.checkpoint=checkpoint
submit_artifacts(root,requestId='interrupted-batch-second',chapterId=cid,
    artifacts=[{'artifactType':'candidate_prose','text':sys.argv[3]}],advanceLifecycle=True,
    diagnostics={'mode':'plot','intensityLevel':1})
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.root), cid, TEXT], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stdout + child.stderr)
        candidate = self.workflow()['artifacts'][cid]['candidate_prose']['artifactSha256']
        from scripts.core.finalization import recover_transactions
        recovered = recover_transactions(self.root, apply=True)
        self.assertTrue(recovered['ok'], recovered)
        from scripts.core.authoring_submission import submit_artifacts
        result = submit_artifacts(self.root, requestId='interrupted-batch-second')
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.workflow()['artifacts'][cid]['candidate_prose']['artifactSha256'], candidate)
        self.assertTrue(submit_artifacts(self.root, requestId='interrupted-batch-second')['ok'])
        self.assertEqual(self.workflow()['artifacts'][cid]['candidate_prose']['artifactSha256'], candidate)
        with self.assertRaises(ValueError) as caught:
            submit_artifacts(self.root, requestId='interrupted-batch-second', chapterId=cid,
                artifacts=[{'artifactType': 'candidate_prose', 'text': TEXT + '换载荷。'}], advanceLifecycle=True,
                diagnostics={'mode': 'plot', 'intensityLevel': 1})
        self.assertEqual(caught.exception.code, 'SUBMISSION_REQUEST_REUSED')

    def test_closeout_killed_after_finalize_resumes_original_request_without_rewriting_journal(self):
        from scripts.core.chapter_closeout import prepare_closeout, closeout_chapter
        from scripts.core.candidate_batch import committed_conversion_receipt
        from scripts.core.finalization import recover_transactions
        self.chapter(self.ids[0]); second = self.chapter(self.ids[1])
        cid = self.ids[0]
        prepare_state_candidate(self.root, chapterId=cid, requestId='kill-closeout-state')
        self.approve(cid, ['candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate'])
        preparation = prepare_closeout(self.root, chapterId=cid, skipTitle=True)
        code = r'''
import json,os,signal,sys
from pathlib import Path
from scripts.core import write_sets
from scripts.core.chapter_closeout import closeout_chapter
root=Path(sys.argv[1])
def checkpoint(stage,relative=None):
    if stage=='committed':
        pending=[json.loads(p.read_text()) for p in (root/'.govel/transactions').glob('*.json')]
        if any(p.get('operation')=='finalize_chapter' and p.get('phase')=='committed' for p in pending):
            os.kill(os.getpid(),signal.SIGKILL)
write_sets.checkpoint=checkpoint
closeout_chapter(root,requestId='killed-closeout',prepareRef=sys.argv[2],
    summary={'resolution':'not_required','reason':'中性测试实际核对无新增摘要内容。'},approveContinuity=True,prepareNext=False)
'''
        child = subprocess.run([sys.executable, '-c', code, str(self.root), preparation['prepareRef']], cwd=ROOT,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, -signal.SIGKILL, child.stdout + child.stderr)
        self.assertEqual(self.workflow()['continuity'][cid]['status'], 'pending')
        recover_transactions(self.root, apply=True)
        witness = self.workflow()['continuity'][cid]['candidateConversion']
        proof = committed_conversion_receipt(self.root, cid, witness, stage='finalize')
        original = {path: (self.root / path).read_bytes() for path in proof['proofPaths']}
        result = closeout_chapter(self.root, requestId='killed-closeout')
        self.assertTrue(result['readyForContinuation'], result)
        self.assertEqual({path: (self.root / path).read_bytes() for path in original}, original)
        self.assertTrue(closeout_chapter(self.root, requestId='killed-closeout')['readyForContinuation'])
        validate_recipe(self.root, load_structure(self.root), self.workflow(), self.ids[1], second['recipe'])

    def test_export_rejects_input_change_and_occupied_output_without_overwrite(self):
        self.chapter(self.ids[0])
        path = self.root / '工作台/阅读稿.md'
        path.write_text('已有作者阅读笔记。')
        with self.assertRaises(ValueError):
            export_candidate_batch(self.root, batchRef=self.batch, output='工作台/阅读稿.md')
        self.assertEqual(path.read_text(), '已有作者阅读笔记。')
        source_path = self.root / '.govel/workflow.json'
        def change(stage):
            if stage == 'before_publish':
                workflow = read_json(source_path)
                workflow['artifacts'][self.ids[0]]['candidate_prose']['invalidatedBy'] = {'reason': 'external test change'}
                write_json(source_path, workflow)
        with patch('scripts.core.manuscript_export.checkpoint', side_effect=change), self.assertRaises(ValueError):
            export_candidate_batch(self.root, batchRef=self.batch, output='工作台/未完成阅读稿.md')
        self.assertFalse((self.root / '工作台/未完成阅读稿.md').exists())

    def test_all_three_formal_acceptances_use_original_closeout_and_committed_witnesses(self):
        from scripts.core.chapter_closeout import prepare_closeout, closeout_chapter
        from scripts.core.candidate_batch import committed_conversion_receipt
        for cid in self.ids[:3]: self.chapter(cid)
        original = {cid: {kind: self.workflow()['artifacts'][cid][kind]['artifactSha256']
                         for kind in ('stage_a_review', 'candidate_prose', 'diagnostics', 'stage_b_review')} for cid in self.ids[:3]}
        for number, cid in enumerate(self.ids[:3]):
            prepare_state_candidate(self.root, chapterId=cid, requestId='adopt-state-' + cid)
            # A title choice that keeps the already authored title is a pure identity conversion.
            title = next(c['title'] for c in load_structure(self.root)['chapters'] if c['chapterId'] == cid)
            pack_artifact(self.root, chapterId=cid, artifactType='title_candidates', payload={
                'candidates': [{'candidateId': 'keep-title', 'title': title, 'reason': '明确保留作者已定标题。'}]})
            self.approve(cid, ['candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate', 'title_candidates'])
            preparation = prepare_closeout(self.root, chapterId=cid, titleCandidateId='keep-title')
            self.assertTrue(preparation['ok'], preparation)
            result = closeout_chapter(self.root, requestId='adopt-' + cid, prepareRef=preparation['prepareRef'],
                summary={'resolution': 'not_required', 'reason': '此中性候选已完整核对，暂无需新增的全书摘要内容。'},
                approveContinuity=True, prepareNext=False)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['readyForContinuation'], result)
            witness = self.workflow()['continuity'][cid]['candidateConversion']
            self.assertEqual(result['receipts']['finalize']['candidateConversionHash'], witness['witnessHash'])
            self.assertEqual(result['receipts']['continuity']['candidateConversionHash'], witness['witnessHash'])
            for stage in ('finalize', 'continuity'):
                committed_conversion_receipt(self.root, cid, witness, stage=stage)
            for later in self.ids[number + 1:3]:
                for kind, value in original[later].items():
                    self.assertEqual(self.workflow()['artifacts'][later][kind]['artifactSha256'], value)
                self.assertTrue(evidence_current(self.root, later)['current'])
        for cid in self.ids[:3]:
            chapter = next(c for c in load_structure(self.root)['chapters'] if c['chapterId'] == cid)
            self.assertEqual(chapter['status'], 'stable')
            self.assertEqual((self.root / chapter['filePath']).read_text(), TEXT)

    def test_formal_conversion_requires_actual_continuity_then_preserves_downstream_evidence(self):
        self.chapter(self.ids[0]); second = self.chapter(self.ids[1]); self.chapter(self.ids[2])
        workflow = self.workflow()
        retained = {cid: {kind: workflow['artifacts'][cid][kind]['artifactSha256'] for kind in ('stage_a_review', 'candidate_prose', 'diagnostics', 'stage_b_review')} for cid in self.ids[1:3]}
        first = self.ids[0]
        prepare_state_candidate(self.root, chapterId=first, requestId='accept-first-state')
        self.approve(first, ['candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate'])
        source = load_structure(self.root); chapter = source['chapters'][0]
        finalize_chapter(self.root, first, skip_title=True, binding_generation=chapter['proseBinding']['generation'])
        source = load_structure(self.root); chapter = source['chapters'][0]
        with self.assertRaises(ValueError) as caught:
            require_conversion(self.root, source, self.workflow(), chapter, second['recipe']['inputs'][0])
        self.assertEqual(caught.exception.code, 'CANDIDATE_CONVERSION_HANDOFF_PENDING')
        forged = self.workflow()
        forged_record = forged['continuity'][first]
        from scripts.core.artifacts import sha256_json
        forged_record.update(status='complete', confirmation={'decisions': [], 'summary': {'resolution': 'not_required', 'reason': '伪造提前完成。'}},
                             conversionReceipt=forged_record['candidateConversion']['witnessHash'])
        forged_record['confirmationHash'] = sha256_json(forged_record['confirmation'])
        with self.assertRaises(ValueError) as caught:
            require_conversion(self.root, source, forged, chapter, second['recipe']['inputs'][0])
        self.assertEqual(caught.exception.code, 'CANDIDATE_CONVERSION_COMMIT_UNPROVEN')
        preview = continuity_preview(self.root, first)
        complete_continuity(self.root, first, batch_hash=preview['batchHash'],
            confirmation={'decisions': [], 'summary': {'resolution': 'not_required', 'reason': '本次中性验收无新增跨章摘要信息，候选依赖单独保留。'}}, approved=True)
        validate_recipe(self.root, load_structure(self.root), self.workflow(), self.ids[1], second['recipe'])
        for cid in self.ids[1:3]:
            self.assertTrue(evidence_current(self.root, cid)['current'])
            for kind, expected in retained[cid].items():
                self.assertEqual(self.workflow()['artifacts'][cid][kind]['artifactSha256'], expected)
        self.assertEqual((self.root / chapter['filePath']).read_text(), TEXT)
        changed = self.workflow()
        changed['continuity'][first]['candidateConversion']['candidateArtifactSha256'] = '0' * 64
        with self.assertRaises(ValueError):
            require_conversion(self.root, load_structure(self.root), changed, chapter, second['recipe']['inputs'][0])
        from scripts.core.candidate_batch import committed_conversion_receipt
        witness = self.workflow()['continuity'][first]['candidateConversion']
        proof = committed_conversion_receipt(self.root, first, witness, stage='finalize')
        terminal = self.root / proof['proofPaths'][0]
        original_bytes = terminal.read_bytes()
        for phase in ('prepared', 'rolled_back'):
            tampered = json.loads(original_bytes); tampered['phase'] = phase
            write_json(terminal, tampered)
            with self.assertRaises(ValueError) as caught:
                require_conversion(self.root, load_structure(self.root), self.workflow(), chapter, second['recipe']['inputs'][0])
            self.assertEqual(caught.exception.code, 'CANDIDATE_CONVERSION_COMMIT_UNPROVEN')
            terminal.write_bytes(original_bytes)

    def test_changed_title_at_formal_acceptance_reopens_dependent_recipe(self):
        self.chapter(self.ids[0]); second = self.chapter(self.ids[1])
        first = self.ids[0]
        prepare_state_candidate(self.root, chapterId=first, requestId='new-title-state')
        pack_artifact(self.root, chapterId=first, artifactType='title_candidates', payload={
            'candidates': [{'candidateId': 'new-title', 'title': '归还之后', 'reason': '作者在正式采用时选择的新标题。'}]})
        self.approve(first, ['candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate', 'title_candidates'])
        chapter = load_structure(self.root)['chapters'][0]
        finalize_chapter(self.root, first, title_candidate_id='new-title', binding_generation=chapter['proseBinding']['generation'])
        with self.assertRaises(ValueError) as caught:
            validate_recipe(self.root, load_structure(self.root), self.workflow(), self.ids[1], second['recipe'])
        self.assertEqual(caught.exception.code, 'CANDIDATE_RECIPE_STALE')
        self.assertEqual(load_structure(self.root)['chapters'][0]['title'], '归还之后')

    def test_frozen_recipe_rejects_self_hashed_changed_scope_and_cross_chapter(self):
        self.chapter(self.ids[0]); second = self.chapter(self.ids[1], stop_after_a=True)
        from scripts.core.artifacts import sha256_json
        for field, value in [('chapterId', self.ids[2]), ('inputs', []), ('sourcePlanHash', '0' * 64)]:
            tampered = deepcopy(second['recipe']); tampered[field] = value
            tampered['effectivePlanHash'] = sha256_json({k: v for k, v in tampered.items() if k != 'effectivePlanHash'})
            with self.assertRaises(ValueError):
                validate_recipe(self.root, load_structure(self.root), self.workflow(), self.ids[1], tampered)
        workflow = self.workflow(); workflow['approvedChapterIds'].remove(self.ids[0])
        with self.assertRaises(ValueError) as caught:
            validate_recipe(self.root, load_structure(self.root), workflow, self.ids[1], second['recipe'])
        self.assertEqual(caught.exception.code, 'CHAPTER_OUTSIDE_APPROVED_RANGE')

    def test_reviewed_fact_change_invalidates_dependent_candidates_not_unrelated_chapter(self):
        source = load_structure(self.root)
        source['fieldDefinitions'].append({'fieldId': 'FD-KEY-STATE', 'key': 'key.state', 'label': '钥匙位置',
                                          'valueType': 'topic', 'appliesTo': ['prop']})
        source['attributes'].append({'attributeId': 'AT-KEY-STATE', 'ownerId': 'P-TEST-KEY', 'fieldId': 'FD-KEY-STATE', 'values': []})
        fact = {'fragmentId': 'SF-KEY-STATE', 'ownerId': 'P-TEST-KEY', 'text': '钥匙此前位于柜中。', 'order': 1,
                'contentRole': 'fact', 'contentBindings': ['AT-KEY-STATE'], 'dependsOn': []}
        source['fragments'].append(fact)
        next(f for f in source['fragments'] if f['ownerId'] == self.ids[0] and f['contentRole'] == 'plan')['dependsOn'] = ['SF-KEY-STATE']
        from scripts.core.structure_store import validate_structure
        validate_structure(source); write_json(self.root / '.govel/structure.json', source)
        self.batch = prepare_candidate_batch(self.root, requestId='fact-dependent-batch', chapterIds=self.ids[:3])['batchRef']
        # An actual unrelated registered/approved candidate proves the operation
        # preserves evidence, rather than comparing two absent artifact entries.
        other = self.ids[3]
        payload = assessed_review_payload(source, other, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)),
            'consensus': [], 'disagreements': [], 'openItems': []})
        pack_artifact(self.root, chapterId=other, artifactType='stage_a_review', payload=payload)
        self.approve(other, ['stage_a_review'])
        pack_artifact(self.root, chapterId=other, artifactType='candidate_prose', text=TEXT)
        from scripts.core.mechanical_evidence import run_diagnostics
        run_diagnostics(self.root, chapter_id=other, mode='plot', intensity_level=1, register=True)
        pack_artifact(self.root, chapterId=other, artifactType='stage_b_review', payload={
            'verdict': 'POLISH_READY', 'slots': evidenced_slots(TEXT), 'findings': [],
            'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'},
            'factAssessment': {'status': 'no_change'}})
        self.approve(other, ['candidate_prose', 'diagnostics', 'stage_b_review'])
        self.chapter(self.ids[0]); second = self.chapter(self.ids[1])
        unrelated = deepcopy(self.workflow().get('artifacts', {}).get(self.ids[3]))
        unrelated_approval = deepcopy(self.workflow()['approvals'][other])
        from scripts.core.source_content_proposals import resolve_content_target, preview_content_change
        from scripts.core.structure_commands import commit_plan
        resolved = resolve_content_target(self.root, recordId=fact['fragmentId'], kind='fact')
        changed = {**resolved['record'], 'text': '钥匙此前放在柜外的抽屉中。'}
        preview = preview_content_change(self.root, sessionRef=resolved['sessionRef'], expectedOldRecord=resolved['record'],
            newRecord=changed, reason='合成作者明确更正原钥匙位置。',
            evidence=[{'fragmentId': fact['fragmentId'], 'quote': fact['text']}],
            approvalActor='synthetic-author', approvalEvidence='fixture-only explicit fact change')
        commit_plan(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)
        with self.assertRaises(ValueError):
            validate_recipe(self.root, load_structure(self.root), self.workflow(), self.ids[1], second['recipe'])
        self.assertEqual(self.workflow().get('artifacts', {}).get(self.ids[3]), unrelated)
        self.assertEqual(self.workflow()['approvals'][other], unrelated_approval)
        self.assertTrue(evidence_current(self.root, other)['fresh'])


if __name__ == '__main__': unittest.main()
