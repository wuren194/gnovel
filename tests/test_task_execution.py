"""Task execution carries usable inputs/results across real operation boundaries."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.tool_plan import execute_task_actions
from tests.fixtures_workflow import review_ready_project

ROOT = Path(__file__).resolve().parents[1]


class TaskExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        from scripts.core.mechanical_evidence import run_diagnostics
        run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def remove_diagnostics(self):
        path = self.root / '.gnovel/workflow.json'
        workflow = read_json(path)
        del workflow['artifacts'][self.chapter]['diagnostics']
        write_json(path, workflow)

    def test_read_executes_preparation_and_returns_query_content_once(self):
        before = self.inventory()
        result = execute_task_actions(self.root, 'editor', chapter_id=self.chapter)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['taskReady'])
        actions = {row['operation']: row for row in result['executed']}
        self.assertEqual(actions['task.prepare']['result']['modelContext']['task'], 'editor')
        for operation in ('query.logic', 'query.attributes', 'query.nodes'):
            self.assertNotIn(operation, actions)
        self.assertNotIn('context.read', actions)
        self.assertTrue(any(row.get('coveredBy') == 'chapter.prepare' for row in result['skipped']))
        self.assertEqual(before, self.inventory())

    def test_task_inputs_reach_model_context_and_pov_is_not_bypassed(self):
        result = call_operation('task.execute', self.root, {
            'task': 'editor', 'chapterId': self.chapter, 'authorization': 'read',
            'povCharacter': 'C-TEST-ALPHA', 'chapterGoal': '关系推进', 'knowledgeMode': 'curated'})
        prepared = next(row['result'] for row in result['executed'] if row['operation'] == 'task.prepare')
        self.assertEqual(prepared['scope']['povCharacter'], 'C-TEST-ALPHA')
        if prepared['modelContext'] is not None:
            self.assertEqual(prepared['modelContext']['task'], 'editor')
            self.assertEqual(prepared['modelContext']['pov'], 'C-TEST-ALPHA')
        self.assertFalse(any(row['operation'].startswith('query.') for row in result['executed']))
        self.assertTrue(any('POV' in row['reason'] for row in result['skipped']))

    def test_missing_semantic_inputs_are_named_and_top_level_fails(self):
        source = read_json(self.root/'.gnovel/structure.json')
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.chapter)
        plan['chapterContract'].pop('mode', None); plan['chapterContract'].pop('intensityLevel', None)
        write_json(self.root/'.gnovel/structure.json', source)
        from scripts.core.source_plan import chapter_plan
        from tests.fixtures_workflow import register_and_approve
        register_and_approve(self.root, self.chapter, 'stage_a_review', {'decision': 'adopt', 'activeSeats': list(range(1, 11)),
            'sourcePlanHash': chapter_plan(source, self.chapter)['sourcePlanHash'], 'consensus': [], 'disagreements': [], 'openItems': []})
        self.remove_diagnostics()
        before = self.inventory()
        result = execute_task_actions(self.root, 'editor', chapter_id=self.chapter, authorization='candidate')
        self.assertFalse(result['ok'])
        self.assertFalse(result['taskReady'])
        diagnostic = next(row for row in result['executed'] if row['operation'] == 'diagnostics.aggregate')
        self.assertEqual(diagnostic['code'], 'TOOL_ACTION_INPUT_REQUIRED')
        self.assertEqual(diagnostic['missing'], ['intensityLevel', 'mode'])
        self.assertEqual(before, self.inventory())

    def test_read_authorization_never_runs_a_candidate_action(self):
        self.remove_diagnostics()
        before = self.inventory()
        result = execute_task_actions(self.root, 'editor', chapter_id=self.chapter,
                                      diagnostics={'mode': 'plot', 'intensityLevel': 1, 'register': True})
        self.assertFalse(result['ok'])
        self.assertFalse(any(row['operation'] == 'diagnostics.aggregate' for row in result['executed']))
        self.assertFalse(result['approvalsPerformed'])
        self.assertEqual(before, self.inventory())

    def test_real_diagnostics_receives_explicit_parameters_and_refreshes_prepare(self):
        self.remove_diagnostics()
        approvals = read_json(self.root / '.gnovel/workflow.json')['approvals']
        result = call_operation('task.execute', self.root, {
            'task': 'editor', 'chapterId': self.chapter, 'authorization': 'candidate',
            'diagnostics': {'mode': 'plot', 'intensityLevel': 1, 'register': True}})
        diagnostic = next(row for row in result['executed'] if row['operation'] == 'diagnostics.aggregate')
        self.assertTrue(diagnostic['ok'], diagnostic)
        self.assertTrue(diagnostic['result']['registered'])
        saved = read_json(self.root / diagnostic['result']['artifactPath'])['payload']
        self.assertEqual(saved['run']['mode'], 'plot')
        self.assertEqual(saved['run']['intensityLevel'], 1)
        self.assertNotIn('evidence', diagnostic['result'])
        prepared = next(row for row in result['executed'] if row['operation'] == 'task.prepare')
        self.assertTrue(prepared['refreshedAfterRegistration'])
        self.assertEqual(read_json(self.root / '.gnovel/workflow.json')['approvals'], approvals)

    def test_handler_false_is_not_reported_as_success(self):
        from scripts.core import tool_plan
        original = tool_plan.call_operation

        def fail_one(operation, root, arguments):
            if operation == 'task.prepare':
                return {'ok': False, 'code': 'QUERY_INCOMPLETE', 'missing': ['evidence']}
            return original(operation, root, arguments)

        with patch.object(tool_plan, 'call_operation', side_effect=fail_one):
            result = execute_task_actions(self.root, 'editor', chapter_id=self.chapter)
        self.assertFalse(result['ok'])
        failed = next(row for row in result['executed'] if row['operation'] == 'task.prepare')
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['result']['missing'], ['evidence'])

    def test_cli_and_mcp_return_same_results(self):
        args = {'projectRoot': str(self.root), 'task': 'editor', 'chapterId': self.chapter, 'authorization': 'read'}
        cli = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'tools', 'execute',
                              '--project-root', str(self.root), '--task', 'editor', '--chapter-id', self.chapter,
                              '--authorization', 'read'], capture_output=True, text=True)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'execute_task_actions', 'arguments': args}}
        mcp = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                              input=json.dumps(request) + '\n', capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        self.assertEqual(json.loads(cli.stdout), json.loads(json.loads(mcp.stdout)['result']['content'][0]['text']))
