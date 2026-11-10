"""Public help and recovery must replace caller-side project/schema inspection."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.command_help import command_help
from scripts.core.operations import operations, validate_arguments, call_operation
from scripts.core.project import query_policy, TOOLBOX_ROOT
from scripts.core.structure_store import StructureError
from scripts.core.tool_catalog import list_capabilities
from scripts.mcp_server import handle
from scripts.novelctl import build_parser


class CommandHelpTests(unittest.TestCase):
    def test_help_does_not_require_a_project_or_configured_provider(self):
        with patch('scripts.core.structure_store.load_structure', side_effect=AssertionError('project read')):
            with patch('scripts.core.data_provider.binding', side_effect=AssertionError('provider read')):
                report = command_help()
                self.assertEqual(report['start']['arguments'], {'task': 'continue', 'view': 'model'})
                self.assertTrue(Path(report['help']['cliScript']).is_file())
                self.assertIn('workflow', [row['group'] for row in report['groups']])
                self.assertEqual(command_help(query='续写这个')['operations'][0]['id'], 'task.prepare')

    def test_all_public_commands_can_be_found_by_group_without_reading_schema_files(self):
        ids = set()
        for group in command_help()['groups']:
            offset = 0
            while True:
                result = command_help(commandGroup=group['group'], offset=offset, limit=3)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), query_policy()['maxBytes']['list'])
                for row in result['operations']:
                    self.assertNotIn(row['id'], ids)
                    ids.add(row['id'])
                    self.assertEqual(row['helpAction']['arguments']['operation'], row['id'])
                if result['nextOffset'] is None:
                    break
                self.assertGreater(result['nextOffset'], offset)
                offset = result['nextOffset']
        self.assertEqual(ids, {spec.id for spec in operations()})

    def test_exact_help_accepts_all_three_public_names(self):
        expected = command_help(operation='task.prepare')['operations']
        for name in ('prepare_task', 'project task prepare', 'task prepare'):
            self.assertEqual(command_help(operation=name)['operations'], expected)
        self.assertEqual(expected[0]['inputSchema'], next(s for s in operations() if s.id == 'task.prepare').input_schema)

    def test_cli_examples_in_help_are_parseable_and_keep_actual_parameters(self):
        parser = build_parser()
        for spec in operations():
            report = command_help(operation=spec.id)
            for example, command in zip(spec.examples, report['cliExamples']):
                with self.subTest(operation=spec.id, example=example):
                    validate_arguments(spec.input_schema, example)
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args(shlex.split(command)[2:])

    def test_common_author_requests_have_help_routes(self):
        cases = {
            '续写这个': 'task.prepare', '接着写': 'task.prepare', '继续上次': 'task.prepare',
            '继续写这个小说': 'task.prepare', '润色这一章': 'task.prepare',
            '看看写到哪了': 'project.overview', '现在有什么没做完': 'project.overview',
            '读一下第三章': 'project.prose', '看看章节计划': 'source.chapter-plan',
            '查看审校意见': 'review.report-view', '查人物设定': 'project.settings.show',
            '给小说改名': 'project.structure.book-rename-preview',
            '给这一章换标题': 'project.structure.chapter-rename-preview',
            '撤销刚才的修改': 'project.structure.operations',
            '新增一个人物': 'project.structure.entity-create-preview',
            '导入这个txt': 'import.preview', '怎么用': 'capabilities.help',
        }
        for text, operation in cases.items():
            with self.subTest(text=text):
                result = command_help(query=text)
                self.assertEqual(result['operations'][0]['id'], operation)
                self.assertNotIn(result['operations'][0]['effects'], {'protected-write', 'approval', 'undo'})
        unknown = command_help(query='量子纠缠量测')
        self.assertEqual(unknown['operations'], [])
        self.assertEqual(unknown['noMatchAction'], {'tool': 'get_tool_help', 'arguments': {}})

    def test_help_rejects_conflicting_filters_and_invalid_pages(self):
        for args in ({'operation': 'task.prepare', 'query': '续写'}, {'commandGroup': 'not-a-group'},
                     {'commandGroup': 'task', 'offset': -1}, {'commandGroup': 'task', 'limit': 0}, {'commandGroup': 'task', 'offset': 100000}):
            with self.subTest(args=args), self.assertRaises(StructureError):
                command_help(**args)
        with self.assertRaises(StructureError) as caught:
            command_help(operation='os.system')
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_FOUND')

    def test_action_arguments_render_to_cli_without_execution(self):
        args = {'projectRoot': '/tmp/书 with space', 'view': 'model'}
        result = command_help(operation='project.doctor', arguments=args)
        self.assertFalse(result['executionPerformed'])
        self.assertIn('--project-root', result['cliArgv'])
        self.assertNotIn('--projectRoot', result['cliArgv'])
        build_parser().parse_args(result['cliArgv'][2:])

    def test_camelcase_cli_error_suggests_actual_flag(self):
        result = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'),
            'project', 'doctor', '--projectRoot', '/tmp/example'], text=True, capture_output=True,
            env={**os.environ, 'GOVEL_LOG_MODE': 'off'})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['details']['flagSuggestions'], {'--projectRoot': '--project-root'})

    def test_explicit_false_is_not_silently_replaced_by_domain_default(self):
        with self.assertRaises(StructureError) as caught:
            command_help(operation='workflow.closeout', arguments={'requestId': 'existing-closeout', 'prepareNext': False})
        self.assertEqual(caught.exception.code, 'CLI_EXPLICIT_FALSE_UNSUPPORTED')
        self.assertIs(caught.exception.details['mcpAction']['arguments']['prepareNext'], False)
        self.assertFalse(caught.exception.details['executionPerformed'])

    def test_handshake_and_default_tool_list_expose_help(self):
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})['result']
        self.assertIn('get_tool_help', response['instructions'])
        self.assertIn('task=continue', response['instructions'])
        tools = handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
        self.assertIn('get_tool_help', {row['name'] for row in tools})
        help_result = handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                              'params': {'name': 'get_tool_help', 'arguments': {'operation': 'prepare_task'}}})['result']
        self.assertFalse(help_result['isError'], help_result)
        self.assertEqual(help_result['structuredContent'], command_help(operation='prepare_task'))

    def test_help_runs_outside_repository_even_with_invalid_private_provider_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {**os.environ, 'GOVEL_PROVIDER_CONFIG': str(Path(temporary) / 'missing.json'), 'GOVEL_LOG_MODE': 'off'}
            for arguments in (['--help'], ['project', 'task', 'prepare', '--help'],
                              ['project', 'tools', 'help', '--query', '续写这个']):
                result = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), *arguments],
                                        cwd=temporary, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(result.stdout.strip())

    def test_continue_without_authorization_returns_working_read_actions(self):
        from tests.fixtures_current import initialize_project
        from scripts.core.task_prepare import prepare_task, TaskPrepareError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            initialize_project(root)
            structure = (root / '.govel/structure.json').read_bytes()
            with self.assertRaises(TaskPrepareError) as caught:
                prepare_task(root, 'continue')
            self.assertEqual(caught.exception.code, 'WORKFLOW_NO_NEXT_ACTION')
            recovery = caught.exception.details['recovery']
            self.assertFalse(recovery['automatic'])
            for action in recovery['readActions']:
                spec = next(s for s in operations() if s.id == action['operation'])
                self.assertEqual(spec.effects, 'read')
                validate_arguments(spec.input_schema, action['arguments'])
                try:
                    result = call_operation(spec.id, root, action['arguments'])
                    self.assertIsInstance(result, dict)
                except StructureError as exc:
                    self.assertEqual(exc.code, 'DOCTOR_UNHEALTHY')
                    self.assertIn('domains', exc.details)
            self.assertEqual((root / '.govel/structure.json').read_bytes(), structure)


if __name__ == '__main__':
    unittest.main()
