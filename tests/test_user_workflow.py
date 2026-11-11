"""User aliases share the existing workflow, evidence and approval boundaries."""
from __future__ import annotations
from scripts.core.chapter_receipt import chapter_receipt

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts.core.chapter_state import advance_chapter_status
from scripts.core.continuity import continuity_preview, complete_continuity
from scripts.core.finalization import finalize_chapter, reconcile_workflow
from scripts.core.jsonio import read_json, write_json
from scripts.core.project import user_workflow_policy, workflow_policy, repair_policy
from scripts.core.structure_store import load_structure, save_structure
from scripts.core.task_prepare import prepare_task, TASK_STATIONS, TaskPrepareError
from scripts.core.workflow import increment_repair_round, WorkflowError
from tests.fixtures_current import create_current_project
from tests.fixtures_workflow import review_ready_project, register_and_approve, run_and_approve_diagnostics

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class UserWorkflowMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=2)
        self.chapter = self.fixture['chapterIds'][0]

    def test_aliases_equal_internal_reports_and_do_not_write(self):
        before = snapshot(self.root)
        for alias, task in [('design', 'planner'), ('write', 'writer'), ('revise', 'editor')]:
            with self.subTest(alias=alias):
                expected = prepare_task(self.root, task, chapter_id=self.chapter)
                actual = prepare_task(self.root, alias, chapter_id=self.chapter)
                self.assertEqual(actual, expected)
                self.assertEqual(actual['task'], task)
                self.assertEqual(actual['userStage'], alias)
                self.assertEqual(actual['station'], TASK_STATIONS[task])
        self.assertEqual(snapshot(self.root), before)

    def test_cli_and_mcp_use_the_same_prepare_boundary(self):
        from scripts.mcp_server import handle
        for alias, task in [('design', 'planner'), ('write', 'writer'), ('revise', 'editor')]:
            with self.subTest(alias=alias):
                proc = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'prepare',
                    '--project-root', str(self.root), '--task', alias, '--chapter-id', self.chapter, '--view', 'model'],
                    capture_output=True, text=True)
                cli = json.loads(proc.stdout)
                mcp = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                    'name': 'prepare_task', 'arguments': {'projectRoot': str(self.root), 'task': alias, 'chapterId': self.chapter}}})['result']
                actual = mcp['structuredContent']
                self.assertEqual(cli['task'], task)
                self.assertEqual(actual['task'], task)
                self.assertEqual(cli, actual)
                self.assertEqual(proc.returncode, 0 if cli['ok'] else 1)
                self.assertIn('contractReads', actual)
                self.assertIn('toolPlan', actual)
                if actual['modelContext']:
                    self.assertIn('contextProofRef', mcp['_meta'])

    def test_alias_preserves_missing_approval_and_stale_plan_blockers(self):
        path = self.root / '.govel/workflow.json'
        workflow = read_json(path)
        workflow['approvals'][self.chapter]['artifactHashes'].pop('stage_a_review')
        write_json(path, workflow)
        report = prepare_task(self.root, 'write', chapter_id=self.chapter)
        self.assertFalse(report['ok'])
        self.assertIn('STAGE_A_NOT_APPROVED', {r['code'] for r in report['blocking']})
        source = load_structure(self.root)
        next(f for f in source['fragments'] if f['ownerId'] == self.chapter)['text'] += '\n改动\n'
        write_json(self.root/'.govel/structure.json', source)
        report = prepare_task(self.root, 'write', chapter_id=self.chapter)
        self.assertIn('STAGE_A_OUTLINE_MISMATCH', {r['code'] for r in report['blocking']})
        self.assertIsNone(report['nextAction'])
        self.assertFalse(report['actionReadiness']['executable'])
        self.assertIn('STAGE_A_OUTLINE_MISMATCH', report['actionReadiness']['blockingCodes'])
        self.assertEqual(report['workflowNextAction'], read_json(path)['nextAction'])

    def test_registry_values_have_real_consumers(self):
        policy = deepcopy(workflow_policy())
        policy['userWorkflow']['stages']['design']['label'] = '策划展示'
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            report = prepare_task(self.root, 'design', chapter_id=self.chapter)
        self.assertIn('策划展示', report['notes'][0])
        policy = deepcopy(workflow_policy())
        policy['userWorkflow']['stages']['outline'] = policy['userWorkflow']['stages'].pop('design')
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            self.assertEqual(prepare_task(self.root, 'outline', chapter_id=self.chapter)['task'], 'planner')
            with self.assertRaises(TaskPrepareError):
                prepare_task(self.root, 'design', chapter_id=self.chapter)

    def test_stage_mutations_are_detected_by_independent_oracle(self):
        # Remove new aliases or restore the old six-name user menu: either breaks the contract.
        for mutate in [lambda p: p['stages'].pop('write'),
                       lambda p: p['stages'].update({'router': {'label': '路由', 'task': 'editor', 'subtasks': []}}),
                       lambda p: p['stages']['revise']['subtasks'].remove('repair')]:
            policy = deepcopy(workflow_policy())
            mutate(policy['userWorkflow'])
            with patch('scripts.core.project.workflow_policy', return_value=policy):
                with self.assertRaises(ValueError):
                    prepare_task(self.root, 'write', chapter_id=self.chapter)


class ContinueRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=2)
        self.chapter = self.fixture['chapterIds'][0]

    def test_continue_is_deterministic_read_only_without_router_persona(self):
        run_and_approve_diagnostics(self.root, self.chapter)
        before = snapshot(self.root)
        route = reconcile_workflow(self.root, persist=False)['nextAction']
        a = prepare_task(self.root, 'continue')
        b = prepare_task(self.root, 'continue')
        self.assertEqual(a, b)
        self.assertEqual(a['nextAction'], route)
        self.assertEqual(a['task'], 'editor')
        self.assertEqual(a['chapterId'], route['chapterId'])
        self.assertEqual(snapshot(self.root), before)

    def test_conflicting_state_does_not_guess(self):
        structure = load_structure(self.root)
        structure['chapters'][1]['status'] = 'planned'
        save_structure(self.root, structure)
        before = snapshot(self.root)
        with self.assertRaises(TaskPrepareError) as caught:
            prepare_task(self.root, 'continue')
        self.assertEqual(caught.exception.code, 'WORKFLOW_AMBIGUOUS')
        self.assertEqual(snapshot(self.root), before)

    def test_no_authorized_next_action_is_explicit(self):
        path = self.root / '.govel/workflow.json'
        workflow = read_json(path)
        workflow['approvedChapterIds'] = []
        write_json(path, workflow)
        with self.assertRaises(TaskPrepareError) as caught:
            prepare_task(self.root, 'continue')
        self.assertEqual(caught.exception.code, 'WORKFLOW_NO_NEXT_ACTION')

    def test_continue_cannot_override_machine_chapter(self):
        with self.assertRaises(TaskPrepareError) as caught:
            prepare_task(self.root, 'continue', chapter_id=self.fixture['chapterIds'][1])
        self.assertEqual(caught.exception.code, 'WORKFLOW_CHAPTER_MISMATCH')

    def test_next_action_mapping_change_affects_route(self):
        policy = deepcopy(workflow_policy())
        policy['userWorkflow']['actionTasks']['editor'] = 'repair'
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            report = prepare_task(self.root, 'continue')
        self.assertEqual(report['task'], 'repair')
        self.assertEqual(report['userStage'], 'revise')

    def test_approval_action_cannot_be_prepared_or_auto_approved(self):
        action = {'action': 'commit', 'chapterId': self.chapter, 'needsApproval': True}
        with patch('scripts.core.finalization.reconcile_workflow', return_value={'ok': True, 'nextAction': action}):
            before = snapshot(self.root)
            with self.assertRaises(TaskPrepareError) as caught:
                prepare_task(self.root, 'continue')
            self.assertEqual(caught.exception.code, 'WORKFLOW_ACTION_REQUIRED')
            self.assertEqual(snapshot(self.root), before)


class StationResponsibilityTests(unittest.TestCase):
    def test_only_three_user_stages_and_six_internal_tasks(self):
        policy = user_workflow_policy()
        self.assertEqual(set(policy['stages']), {'design', 'write', 'revise'})
        self.assertEqual(set(TASK_STATIONS), {'planner', 'writer', 'editor', 'repair', 'titler', 'continuity'})
        self.assertEqual(TASK_STATIONS['repair'], TASK_STATIONS['editor'])
        self.assertEqual(TASK_STATIONS['continuity'], 'govel-router')
        self.assertEqual(policy['stages']['revise']['subtasks'], ['repair', 'titler', 'continuity'])
        manifest = read_json(ROOT / 'toolbox.manifest.json')
        self.assertEqual(len(manifest['skills']), 6)
        self.assertNotIn('govel-composer', TASK_STATIONS.values())
        self.assertEqual(set(manifest['skills']), {key for key in manifest['promptPlan'] if not key.startswith('_')})

    def test_wrappers_have_no_workflow_business_rules(self):
        from scripts.generate_wrappers import expected_files
        # 准备模式由根 Skill 拥有，包装只委托入口选择。
        self.assertIn('--view model', (ROOT / 'SKILL.md').read_text())
        for path, text in expected_files().items():
            self.assertEqual(path.read_text(), text)
            body = tomllib.loads(text)["developer_instructions"] if path.suffix == ".toml" else text.split("---", 2)[-1]
            for rule in ['不强制注入', '读与不读', '按工位换窗', '阶段 A', 'finalize-chapter']:
                self.assertNotIn(rule, body)
            self.assertIn('MCP／CLI 选择按根 Skill 执行', body)
            self.assertNotIn('novelctl project prepare', body)


class WorkflowTransitionTests(unittest.TestCase):
    def test_design_write_revise_follow_existing_state_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = create_current_project(root, chapter_count=1)
            chapter = fixture['chapterIds'][0]
            structure = load_structure(root)
            structure['chapters'][0]['status'] = 'planning_required'
            save_structure(root, structure)
            workflow = read_json(root / '.govel/workflow.json')
            workflow['approvedChapterIds'] = [chapter]
            write_json(root / '.govel/workflow.json', workflow)
            self.assertEqual(prepare_task(root, 'continue')['task'], 'planner')
            with self.assertRaises(WorkflowError):
                advance_chapter_status(root, chapter, 'planned')
            # Fixture executes real register/approve/advance operations through review.
            review_ready_project(root, chapter_count=1)
            self.assertEqual(prepare_task(root, 'continue')['task'], 'editor')
            self.assertEqual(prepare_task(root, 'write', chapter_id=chapter)['task'], 'writer')
            self.assertEqual(prepare_task(root, 'revise', chapter_id=chapter)['task'], 'editor')

    def test_title_gate_continuity_and_next_chapter_survive_internalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = review_ready_project(root, chapter_count=2)
            first, second = fixture['chapterIds']
            with self.assertRaises(ValueError):
                finalize_chapter(root, first, binding_generation=chapter_receipt(root, first)['bindingGeneration'])
            register_and_approve(root, first, 'title_candidates', {'candidates': [
                {'candidateId': 'T-1', 'title': '留下钥匙', 'patternId': 'action', 'voice': 'active', 'reason': '合成标题'}]})
            self.assertEqual(prepare_task(root, 'titler', chapter_id=first)['userStage'], 'revise')
            finalize_chapter(root, first, title_candidate_id='T-1', binding_generation=chapter_receipt(root, first)['bindingGeneration'])
            report = prepare_task(root, 'continue')
            self.assertEqual(report['task'], 'continuity')
            self.assertEqual(report['userStage'], 'revise')
            self.assertFalse(report['ok'])
            self.assertIn('CONTINUITY_REQUIRED', {r['code'] for r in report['blocking']})
            self.assertFalse(prepare_task(root, 'write', chapter_id=second)['readiness']['authorizationReady'])
            preview = continuity_preview(root, first)
            complete_continuity(root, first, batch_hash=preview['batchHash'], approved=True, confirmation={
                'decisions': [{'itemId': row['itemId'], 'resolution': 'no_change', 'reason': '合成确认', 'evidence': []}
                              for row in preview['batch']['items']],
                'summary': {'resolution': 'not_required', 'reason': '合成无需摘要'}})
            self.assertEqual(prepare_task(root, 'continue')['task'], 'planner')
            self.assertEqual(load_structure(root)['chapters'][0]['title'], '留下钥匙')

    def test_repair_mode_preserves_round_limit_and_artifact_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = review_ready_project(root, chapter_count=1)
            chapter = fixture['chapterIds'][0]
            report = prepare_task(root, 'repair', chapter_id=chapter)
            self.assertEqual(report['userStage'], 'revise')
            self.assertEqual(report['station'], 'govel-editor')
            workflow = read_json(root / '.govel/workflow.json')
            maximum = repair_policy()['maxAutomaticRounds']
            for number in range(maximum):
                self.assertEqual(increment_repair_round(workflow, chapter, maximum=maximum), number + 1)
            with self.assertRaises(WorkflowError):
                increment_repair_round(workflow, chapter, maximum=maximum)
            self.assertEqual(report['upstream']['candidate_prose']['artifactSha256'],
                             workflow['artifacts'][chapter]['candidate_prose']['artifactSha256'])
