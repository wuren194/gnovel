"""Public CLI leaves and MCP tools share schemas, author gates and real behavior."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.operations import operations
from scripts.core.structure_store import load_structure
from scripts.novelctl import build_parser
from tests.fixtures_refactor import refactor_project


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/novelctl.py'
MCP = ROOT / 'scripts/mcp_server.py'


def cli_leaves(parser, path=()):
    branches = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if not branches:
        yield path, parser
    for branch in branches:
        for name, child in branch.choices.items():
            yield from cli_leaves(child, (*path, name))


class CliMcpParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / '小说'
        self.environment = dict(os.environ)
        self.environment.pop('GOVEL_PROVIDER_CONFIG', None)
        self.environment.update({'GOVEL_DATA_HOME': str(self.base / 'empty-provider'),
                                 'GOVEL_LOG_DIR': str(self.base / 'logs'), 'GOVEL_LOG_MODE': 'off'})
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))

    def cli(self, *arguments, success=True, environment=None):
        proc = subprocess.run([sys.executable, str(CLI), 'project', *arguments], cwd=ROOT,
                              text=True, capture_output=True, env=environment or self.environment)
        if success:
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def exchange(self, requests, *, environment=None):
        proc = subprocess.run([sys.executable, str(MCP)], cwd=ROOT, text=True, capture_output=True,
            input=''.join(json.dumps(request, ensure_ascii=False) + '\n' for request in requests),
            env=environment or self.environment)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def mcp(self, tool, arguments, *, success=True, environment=None):
        response = self.exchange([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                   'params': {'name': tool, 'arguments': arguments}}], environment=environment)[0]
        self.assertNotIn('error', response, response)
        result = response['result']
        self.assertEqual(result['isError'], not success, result)
        return result['structuredContent']

    def tool_list(self):
        result = self.exchange([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list',
                                 'params': {'view': 'audit'}}])[0]['result']
        return {row['name']: row for row in result['tools']}

    def fixture(self):
        return refactor_project(self.root)

    def test_every_project_cli_leaf_has_one_registered_and_callable_mcp_schema(self):
        specs = operations()
        leaves = {path[1:]: parser for path, parser in cli_leaves(build_parser()) if path[0] == 'project'}
        by_cli = {spec.cli: spec for spec in specs}
        self.assertEqual(len(by_cli), len(specs), 'CLI paths must not resolve to competing operations')
        self.assertEqual(len({spec.name for spec in specs}), len(specs), 'MCP operation names must be unique')
        self.assertEqual(set(leaves), set(by_cli), 'public CLI and operation catalog must cover the same leaf set')
        tools = self.tool_list()
        for path, spec in by_cli.items():
            with self.subTest(cli=path, operation=spec.id):
                self.assertTrue(spec.mcp, 'a public CLI capability cannot silently disappear from MCP')
                self.assertIn(spec.name, tools)
                self.assertEqual(tools[spec.name]['inputSchema'], spec.input_schema)
                self.assertEqual(tools[spec.name]['annotations']['readOnlyHint'], spec.effects == 'read')
                declared = leaves[path].get_default('operation_binding')
                if declared is not None:
                    self.assertEqual(declared, spec.id)

    def test_author_intents_return_callable_tools_for_new_and_historical_gaps(self):
        expectations = [
            ('新建小说', 'project.create-preview'), ('导入外部小说', 'import.preview'),
            ('追加外部章节', 'import.append-preview'), ('拆分章节', 'chapter.split-preview'),
            ('合并章节', 'chapter.merge-preview'), ('备份项目', 'project.backup'),
            ('恢复项目', 'project.restore-preview'), ('外部修改', 'workflow.external-preview'),
            ('移动项目', 'project.relocate'), ('批量移动章节', 'structure.batch-preview'),
        ]
        requests = [{'jsonrpc': '2.0', 'id': index + 1, 'method': 'tools/call', 'params': {
            'name': 'list_capabilities', 'arguments': {'query': query}}} for index, (query, _) in enumerate(expectations)]
        tools = self.tool_list()
        for (query, expected), response in zip(expectations, self.exchange(requests)):
            with self.subTest(query=query):
                self.assertFalse(response['result']['isError'], response)
                result = response['result']['structuredContent']
                self.assertEqual(result['operations'][0]['id'], expected)
                invocation = result['operations'][0]['invocation']
                self.assertIn(invocation['tool'], tools)
                self.assertFalse(invocation.get('cliOnly', False))
                self.assertNotIn('shell', invocation)
                self.assertEqual(result['scripts'], [])

    def test_mcp_creation_preview_commits_via_cli_only_after_approval(self):
        preview = self.mcp('preview_project_creation', {'projectRoot': str(self.root),
            'bookName': '旧城来信', 'noAgentsMd': True})
        self.assertFalse(self.root.exists())
        arguments = {'projectRoot': str(self.root), 'planRef': preview['planRef'], 'planHash': preview['planHash']}
        self.mcp('commit_project_creation', {**arguments, 'approve': False}, success=False)
        self.assertFalse(self.root.exists())
        created = self.cli('create-commit', '--project-root', str(self.root), '--plan-ref', preview['planRef'],
                           '--plan-hash', preview['planHash'], '--approve')
        self.assertEqual(created['code'], 'PROJECT_CREATED')
        self.assertEqual(load_structure(self.root)['projectId'], preview['projectId'])
        replay = self.mcp('commit_project_creation', {**arguments, 'approve': True})
        self.assertTrue(replay['alreadyCommitted'])
        self.assertFalse((self.root / 'AGENTS.md').exists())

    def test_legacy_mcp_quick_init_and_cli_state_rebuild_share_the_current_project(self):
        self.mcp('quick_init', {'projectRoot': str(self.root), 'bookName': '晨间交班', 'noAgentsMd': True})
        before = (self.root / '.govel/structure.json').read_bytes()
        cli = self.cli('state', 'rebuild', '--project-root', str(self.root))
        mcp = self.mcp('state_rebuild', {'projectRoot': str(self.root)})
        self.assertEqual(cli['code'], 'STATE_CACHE_REBUILT')
        self.assertEqual(mcp['code'], cli['code'])
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)
        status = self.cli('state', 'status', '--project-root', str(self.root))
        self.assertEqual(status['code'], 'STATE_CACHE_STATUS')

    def test_recovery_report_and_explicit_apply_keep_clean_project_unchanged(self):
        self.fixture()
        before = (self.root / '.govel/structure.json').read_bytes()
        reported = self.cli('workflow', 'recover', '--project-root', str(self.root))
        applied = self.mcp('workflow_recover', {'projectRoot': str(self.root), 'apply': True})
        self.assertEqual(reported['code'], 'NO_PENDING_TRANSACTION')
        self.assertFalse(reported['applied'])
        self.assertEqual(applied['code'], reported['code'])
        self.assertEqual(applied['transactions'], [])
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)

    def test_mcp_batch_preview_cli_commit_keeps_author_gate_and_original_prose(self):
        fixture = self.fixture()
        initial = load_structure(self.root)
        originals = {row['filePath']: (self.root / row['filePath']).read_bytes() for row in initial['chapters']}
        preview = self.mcp('preview_structure_batch', {'projectRoot': str(self.root), 'steps': [
            {'op': 'move-chapter', 'chapterId': fixture['chapterTwo'], 'beforeChapterId': fixture['chapterOne']} ]})
        self.assertEqual(load_structure(self.root), initial)
        self.cli('structure', 'commit', '--project-root', str(self.root), '--operation-id', preview['operationId'],
                 '--plan-hash', preview['planHash'], success=False)
        self.assertEqual(load_structure(self.root), initial)
        self.cli('structure', 'commit', '--project-root', str(self.root), '--operation-id', preview['operationId'],
                 '--plan-hash', preview['planHash'], '--approve')
        self.assertEqual(load_structure(self.root)['chapters'][0]['chapterId'], fixture['chapterTwo'])
        for relative, body in originals.items():
            self.assertEqual((self.root / relative).read_bytes(), body)

    def test_cli_relocation_preview_commits_through_mcp_and_keeps_source_identity(self):
        self.fixture()
        initial = load_structure(self.root)
        originals = {row['filePath']: (self.root / row['filePath']).read_bytes() for row in initial['chapters']}
        target = self.base / '新位置'
        preview = self.cli('relocate', '--project-root', str(self.root), '--target', str(target))
        arguments = {'projectRoot': str(self.root), 'operationId': preview['operationId'], 'previewHash': preview['planHash']}
        denied = self.mcp('relocate', {**arguments, 'approve': False}, success=False)
        self.assertEqual(denied['code'], 'APPROVAL_REQUIRED')
        self.assertTrue(self.root.is_dir())
        self.assertFalse(target.exists())
        self.mcp('relocate', {**arguments, 'approve': True})
        self.assertFalse(self.root.exists())
        self.assertEqual(load_structure(target), initial)
        for relative, body in originals.items():
            self.assertEqual((target / relative).read_bytes(), body)
        overview = self.cli('overview', '--project-root', str(target))
        self.assertEqual(overview['code'], 'PROJECT_OVERVIEW')

    def test_mcp_log_configuration_and_pruning_use_the_same_temporary_user_store_as_cli(self):
        environment = dict(self.environment)
        environment.pop('GOVEL_LOG_MODE', None)
        configured = self.mcp('configure_call_logging', {'mode': 'summary'}, environment=environment)
        self.assertEqual(configured['configuredMode'], 'summary')
        status = self.cli('logs', 'status', environment=environment)
        self.assertEqual(status['mode'], 'summary')
        self.assertEqual(Path(status['directory']).resolve(), self.base / 'logs')
        pruned = self.mcp('prune_call_logs', {}, environment=environment)
        self.assertEqual(pruned['code'], 'CALL_LOG_PRUNED')
        self.cli('logs', 'configure', '--mode', 'off', environment=environment)
        self.assertEqual(self.mcp('query_call_logging_status', {}, environment=environment)['mode'], 'off')
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
