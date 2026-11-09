"""Task execution reuses only the preparation baseline explicitly held by its caller."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import run_diagnostics
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT
from tests.fixtures_workflow import review_ready_project


def preparation(result):
    return next(row['result'] for row in result['executed'] if row['operation'] == 'task.prepare')


class TaskExecutionDeltaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        self.arguments = {'task': 'editor', 'chapterId': self.chapter, 'authorization': 'read'}

    def execute(self, **overrides):
        return call_operation('task.execute', self.root, {**self.arguments, **overrides})

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_held_prepare_is_compact_without_repeating_context_or_queries(self):
        full = preparation(self.execute())
        before = self.inventory()
        result = self.execute(heldPrepareRef=full['prepareRef'])
        delta = preparation(result)
        self.assertTrue(result['ok'], result)
        self.assertEqual(delta['delivery']['mode'], 'delta')
        self.assertEqual(delta['delivery']['baseRef'], full['prepareRef'])
        self.assertIn('modelContext', delta['delivery']['unchangedFields'])
        self.assertEqual(delta['readiness'], full['readiness'])
        self.assertEqual(delta['missing'], full['missing'])
        self.assertEqual([row['operation'] for row in result['executed']], ['task.prepare'])
        self.assertLess(len(json.dumps(delta)), len(json.dumps(full)))
        self.assertEqual(before, self.inventory())
        self.assertFalse(result['approvalsPerformed'])

    def test_no_implicit_baseline_is_used_on_repeat(self):
        self.execute()
        full = preparation(self.execute())
        self.assertNotIn('delivery', full)
        self.assertIn('modelContext', full)

    def test_unavailable_baseline_returns_full_materials(self):
        full = preparation(self.execute())
        with tempfile.TemporaryDirectory() as temp, patch('scripts.core.transport_cache._directory', return_value=Path(temp)):
            result = preparation(self.execute(heldPrepareRef=full['prepareRef']))
        self.assertEqual(result['delivery']['mode'], 'full')
        self.assertEqual(result['delivery']['reason'], 'baseline_snapshot_unavailable')
        self.assertIn('modelContext', result)

    def test_pov_change_does_not_reuse_author_side_materials(self):
        full = preparation(self.execute())
        result = preparation(self.execute(heldPrepareRef=full['prepareRef'], povCharacter='C-TEST-ALPHA'))
        self.assertEqual(result['delivery']['mode'], 'full')
        self.assertEqual(result['delivery']['reason'], 'task_or_pov_scope_changed')
        self.assertEqual(result['scope']['povCharacter'], 'C-TEST-ALPHA')

    def test_current_blockers_are_not_inherited_from_held_readiness(self):
        full = preparation(self.execute())
        path = self.root / '.gnovel/workflow.json'
        workflow = read_json(path)
        workflow['approvedChapterIds'] = []
        write_json(path, workflow)
        before = self.inventory()
        result = self.execute(heldPrepareRef=full['prepareRef'])
        delta = preparation(result)
        self.assertFalse(result['taskReady'])
        self.assertFalse(delta['readiness']['authorizationReady'])
        self.assertIn('CHAPTER_OUTSIDE_APPROVED_RANGE', [row['code'] for row in delta['missing']])
        self.assertIsNone(delta['nextAction'])
        self.assertEqual(before, self.inventory())

    def test_refresh_after_registration_uses_delivered_baseline_not_intermediate_prepare(self):
        full = preparation(self.execute())
        path = self.root / '.gnovel/workflow.json'
        workflow = read_json(path)
        approvals = workflow['approvals']
        del workflow['artifacts'][self.chapter]['diagnostics']
        write_json(path, workflow)
        from scripts.core import tool_plan
        original = tool_plan.call_operation
        prepare_args = []

        def observe(operation, root, arguments):
            if operation == 'task.prepare':
                prepare_args.append(dict(arguments))
            return original(operation, root, arguments)

        with patch.object(tool_plan, 'call_operation', side_effect=observe):
            result = self.execute(heldPrepareRef=full['prepareRef'], authorization='candidate',
                                  diagnostics={'mode': 'plot', 'intensityLevel': 1, 'register': True})
        self.assertEqual(len(prepare_args), 2)
        self.assertTrue(all(args['heldPrepareRef'] == full['prepareRef'] for args in prepare_args))
        self.assertEqual(preparation(result)['delivery']['baseRef'], full['prepareRef'])
        self.assertTrue(next(row for row in result['executed'] if row['operation'] == 'task.prepare')['refreshedAfterRegistration'])
        self.assertEqual(read_json(path)['approvals'], approvals)

    def test_cli_and_mcp_support_the_same_held_baseline(self):
        full = preparation(self.execute())
        arguments = {**self.arguments, 'projectRoot': str(self.root), 'heldPrepareRef': full['prepareRef']}
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'tools', 'execute',
                              '--project-root', str(self.root), '--task', 'editor', '--chapter-id', self.chapter,
                              '--authorization', 'read', '--held-prepare-ref', full['prepareRef']],
                             capture_output=True, text=True)
        from scripts.mcp_server import handle
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'execute_task_actions', 'arguments': arguments}})['result']
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertFalse(response['isError'])
        self.assertEqual(json.loads(cli.stdout), response['structuredContent'])


if __name__ == '__main__':
    unittest.main()
