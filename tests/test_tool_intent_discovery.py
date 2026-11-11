"""Intent discovery must lead to callable, bounded, effect-preserving operations."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.operations import operations, validate_arguments
from scripts.core.project import TOOLBOX_ROOT, query_policy, tool_discovery_policy
from scripts.core.query import _size
from scripts.core.structure_store import StructureError
from scripts.core.tool_catalog import list_capabilities
from scripts.core.jsonio import write_json
from scripts.mcp_server import handle
from scripts.novelctl import build_parser
from tests.fixtures_source import source_fixture


class ToolIntentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)

    def call(self, name, arguments):
        return handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': name, 'arguments': arguments}})['result']

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_common_intents_return_distinct_registered_definitions(self):
        for query, expected in (
            ('把全部设定导出成一个 Markdown', 'source.export'),
            ('导出包含归档的设定', 'source.export'),
            ('把成品小说合并导出', 'project.export'),
            ('查看人物资料', 'project.settings.show'),
            ('预览全部设定', 'source.render'),
            ('查询结构化设定', 'source.query'),
            ('搜索设定关键词', 'source.search'),
            ('查看返修问题', 'workflow.repair-context'),
            ('装配复核材料包', 'evidence.prepare'),
            ('差量执行任务工具', 'task.execute'),
            ('读取修订方案', 'source.maintenance-prepare'),
            ('装配覆盖清单', 'source.maintenance-checklist'),
            ('批量修订设定', 'source.proposal-batch-preview'),
            ('batch update canon', 'source.proposal-batch-preview'),
            ('提交', 'source.commit'),
            ('续用预览', 'source.request-renew'),
            ('扫描禁用词', 'source.scan-report'),
            ('读取变更明细', 'source.change-report'),
            ('预检批量输入参数', 'capabilities.validate'),
            ('preflight arguments', 'capabilities.validate'),
            ('save maintenance checklist', 'source.maintenance-checklist'),
            ('export all settings', 'source.export'),
            ('export the manuscript', 'project.export'),
            ('read character details', 'project.settings.show'),
            ('提交本章工件', 'workflow.submit'),
            ('恢复提交', 'workflow.submit'),
            ('建章并提交推演', 'workflow.chapter-preview'),
            ('准备阶段 A 审校', 'workflow.review-form'),
            ('准备阶段 B 审校', 'workflow.review-form'),
            ('给我本章审校输入', 'workflow.review-form'),
            ('生成阶段A表单', 'workflow.review-form'),
            ('生成阶段B表单', 'workflow.review-form'),
            ('提交审校填写', 'workflow.submit'),
            ('提交表单修改', 'workflow.submit'),
            ('定稿前检查', 'workflow.closeout-prepare'),
            ('检查为什么不能定稿', 'workflow.closeout-prepare'),
            ('正文归属冲突', 'workflow.closeout-prepare'),
        ):
            with self.subTest(query=query):
                before = self.files()
                result = list_capabilities(query=query)
                self.assertEqual(result['operations'][0]['id'], expected)
                self.assertLessEqual(len(result['operations']), tool_discovery_policy()['maxResults'])
                self.assertLessEqual(_size(result), query_policy()['maxBytes']['detail'])
                self.assertEqual(result['scripts'], [])
                self.assertEqual(self.files(), before)
                spec = next(row for row in operations() if row.id == expected)
                self.assertEqual(result['operations'][0]['inputSchema'], spec.input_schema)

    def test_author_phrasings_from_real_sessions_reach_the_right_first_operation(self):
        # 真实会话里作者的原话：设定审计、时间线、批量重排与按显示编号找章。
        for query, expected in (
            ('检查设定有没有冲突', 'source.search'),
            ('时间线矛盾', 'project.structure.timeline'),
            ('批量移动章节', 'structure.batch-preview'),
            ('把卷一 48 章全部移到卷二前面', 'structure.batch-preview'),
            ('批量登记实体', 'structure.batch-preview'),
            ('把第3章移到第1章前面', 'project.structure.move-preview'),
            ('CH001 是哪一章', 'project.workflow.chapter-status'),
        ):
            with self.subTest(query=query):
                self.assertEqual(list_capabilities(query=query)['operations'][0]['id'], expected)
        both = {row['id'] for row in list_capabilities(query='设定有没有冲突不一致的地方 尤其是时间线逻辑')['operations']}
        self.assertEqual(both, {'source.search', 'project.structure.timeline'})
        self.assertEqual(list_capabilities(query='全部帮我优化')['operations'], [])

    def test_each_registered_intent_has_valid_shared_examples(self):
        identities = tool_discovery_policy()['intents']
        for spec in operations():
            if spec.id not in identities:
                continue
            with self.subTest(operation=spec.id):
                self.assertTrue(spec.examples)
                for example in spec.examples:
                    validate_arguments(spec.input_schema, example)

    def test_explicit_identities_work_without_matching_generic_name_substrings(self):
        for query in ('source.export', '使用 source.export 保存', 'export_source_reading', 'project source export'):
            with self.subTest(query=query):
                self.assertEqual(list_capabilities(query=query)['operations'][0]['id'], 'source.export')
        self.assertEqual(list_capabilities(query='export')['operations'][0]['id'], 'project.export')
        self.assertEqual(list_capabilities(query='reexport settings')['operations'], [])
        for operation in ('capabilities.list', 'capabilities.read', 'capabilities.export'):
            row = list_capabilities(operation=operation)['operations'][0]
            self.assertEqual(row['invocation']['tool'], row['mcp'])
            self.assertNotIn('operation', row['invocation'])

    def test_unknown_or_ambiguous_intent_never_executes_or_invents_a_tool(self):
        before = self.files()
        unknown = list_capabilities(query='量子纠缠量测')
        self.assertEqual(unknown['operations'], [])
        multiple = list_capabilities(query='导出设定和成品小说')
        self.assertEqual({row['id'] for row in multiple['operations']}, {'source.export', 'project.export'})
        self.assertEqual(self.files(), before)
        for row in multiple['operations']:
            self.assertEqual(row['effects'], 'export-write')
            self.assertFalse(row['annotations']['readOnlyHint'])

    def test_policy_limits_aliases_and_unknown_operations_have_real_consumers(self):
        policy = deepcopy(tool_discovery_policy())
        policy['maxResults'] = 1
        with patch('scripts.core.tool_catalog.tool_discovery_policy', return_value=policy):
            result = list_capabilities(query='导出设定和成品小说')
        self.assertEqual(len(result['operations']), 1)
        self.assertTrue(result['hasMore'])
        policy['intents']['source.export'] = [['特殊代号'], ['阅读包']]
        with patch('scripts.core.tool_catalog.tool_discovery_policy', return_value=policy):
            self.assertEqual(list_capabilities(query='特殊代号阅读包')['operations'][0]['id'], 'source.export')
            self.assertEqual(list_capabilities(query='导出设定')['operations'], [])
        policy['intents']['nonexistent.operation'] = [['bad']]
        with patch('scripts.core.tool_catalog.tool_discovery_policy', return_value=policy):
            with self.assertRaises(StructureError):
                list_capabilities(query='bad')
        policy = deepcopy(tool_discovery_policy()); policy['maxQueryCharacters'] = 2
        with patch('scripts.core.tool_catalog.tool_discovery_policy', return_value=policy):
            with self.assertRaises(StructureError):
                list_capabilities(query='导出全部设定')

    def test_malformed_discovery_registry_is_rejected(self):
        catalog = json.loads((TOOLBOX_ROOT / 'shared/registries/tools.json').read_text())
        invalid = (None, {}, {**catalog['discovery'], 'maxResults': True},
                   {**catalog['discovery'], 'maxQueryCharacters': 0},
                   {**catalog['discovery'], 'intents': {'source.export': [[]]}},
                   {**catalog['discovery'], 'intents': {'source.export': [[' ']]}},
                   {**catalog['discovery'], 'unused': 1})
        from scripts.core.static_cache import cached_json
        for policy in invalid:
            with self.subTest(policy=policy), patch('scripts.core.project.cached_json', side_effect=lambda path:
                    {**catalog, 'discovery': policy} if path == TOOLBOX_ROOT / 'shared/registries/tools.json' else cached_json(path)):
                with self.assertRaises(ValueError):
                    tool_discovery_policy()

    def test_search_respects_actual_byte_budget_without_partial_definitions(self):
        policy = deepcopy(query_policy())
        single = list_capabilities(query='导出设定')['operations'][0]
        policy['maxBytes']['detail'] = _size(single) + 1024
        with patch('scripts.core.tool_catalog.query_policy', return_value=policy):
            result = list_capabilities(query='导出设定和成品小说')
        self.assertEqual(len(result['operations']), 1)
        self.assertTrue(result['hasMore'])
        self.assertLessEqual(_size(result), policy['maxBytes']['detail'])
        policy['maxBytes']['detail'] = 1
        with patch('scripts.core.tool_catalog.query_policy', return_value=policy), self.assertRaises(StructureError) as caught:
            list_capabilities(query='导出设定')
        self.assertEqual(caught.exception.code, 'OPERATION_DEFINITION_TOO_LARGE')
        with patch('scripts.core.tool_catalog.query_policy', return_value=policy), self.assertRaises(StructureError) as caught:
            list_capabilities(query='无匹配意图')
        self.assertEqual(caught.exception.code, 'QUERY_BUDGET_INVALID')

    def test_invalid_registry_returns_a_protocol_error_for_tools_list(self):
        with patch('scripts.core.tool_catalog.tool_discovery_policy', side_effect=ValueError('invalid discovery policy')):
            result = handle({'id': 1, 'method': 'tools/list'})
        self.assertEqual(result['error']['code'], -32602)
        self.assertNotIn('result', result)

    def test_invalid_query_combinations_fail_closed(self):
        for arguments in ({'query': ''}, {'query': ' '}, {'query': '查' * (tool_discovery_policy()['maxQueryCharacters'] + 1)},
                          {'query': '导出设定', 'operation': 'source.export'}, {'query': '导出设定', 'view': 'audit'}):
            with self.subTest(arguments=arguments), self.assertRaises(StructureError):
                list_capabilities(**arguments)
        self.assertEqual(list_capabilities(query='导出设定', audience='writer')['operations'], [])
        self.assertEqual(list_capabilities(query='导出设定', category='skill')['operations'], [])

    def test_default_mcp_client_discovers_and_exports_without_hidden_tool_calls(self):
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass  # The export writer reuses this permanent anchor.
        tools = handle({'id': 1, 'method': 'tools/list'})['result']['tools']
        visible = {row['name']: row for row in tools}
        self.assertIn('list_capabilities', visible)
        self.assertIn('read_discovered_operation', visible)
        self.assertIn('export_discovered_operation', visible)
        self.assertFalse(visible['export_discovered_operation']['annotations']['readOnlyHint'])
        before = self.files()
        discovered = self.call('list_capabilities', {'query': '把全部设定导出成一个Markdown'})
        self.assertFalse(discovered['isError'])
        definition = discovered['structuredContent']['operations'][0]
        invocation = definition['invocation']
        self.assertIn(invocation['tool'], visible)
        self.assertEqual(self.files(), before)
        arguments = {'projectRoot': str(self.root), 'operation': invocation['operation'],
                     'arguments': definition['examples'][0]}
        exported = self.call(invocation['tool'], arguments)
        self.assertFalse(exported['isError'], exported)
        result = exported['structuredContent']
        self.assertEqual(result['code'], 'SOURCE_EXPORTED')
        self.assertFalse(result['coverage']['semanticReviewComplete'])
        self.assertFalse(result['maintained'])
        self.assertEqual(self.files(), {**before, '工作台/设定与规划.md': Path(result['output']).read_bytes()})
        self.assertIn('他身高一米九。', Path(result['output']).read_text())
        self.assertFalse(result['selection']['includeArchived'])

    def test_default_mcp_client_uses_field_preview_and_manuscript_operations(self):
        from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION
        from tests.fixtures_source import single_source_chapter
        write_json(self.root / '.govel/project.json', {
            'schemaVersion': PROJECT_SCHEMA_VERSION, 'workflowVersion': WORKFLOW_VERSION,
            'projectId': self.source['projectId'], 'bookName': self.source['bookName'], 'approvalMode': 'manual'})
        self.source['volumes'] = [{'volumeId': 'volume-EXPORT', 'title': '卷一', 'ordinal': 1, 'status': 'planned'}]
        self.source['arcs'] = [{'arcId': 'arc-EXPORT', 'title': '主线', 'kind': 'main', 'status': 'planned',
                               'volumeIds': ['volume-EXPORT'], 'parentArcId': None}]
        chapter = single_source_chapter(self.source, 'chapter-EXPORT', status='stable', volume_id='volume-EXPORT')
        from scripts.core.prose_binding import seal_binding
        chapter['proseBinding'] = seal_binding(self.source['projectId'], chapter['chapterId'], chapter['filePath'],
                                               state='managed', generation=self.source['revision'],
                                               expected_content_digest='sha256:' + hashlib.sha256('已入库的章节正文。'.encode()).hexdigest())
        self.source['chapters'].append(chapter)
        self.source['chapterArcLinks'] = [{'chapterId': 'chapter-EXPORT', 'arcId': 'arc-EXPORT', 'role': 'primary'}]
        write_json(self.root / '.govel/structure.json', self.source)
        (self.root / '正文').mkdir()
        (self.root / chapter['filePath']).write_text('已入库的章节正文。', encoding='utf-8')
        visible = {row['name'] for row in handle({'id': 1, 'method': 'tools/list'})['result']['tools']}
        for query, expected in (('查看人物资料', 'project.settings.show'),
                                ('预览全部设定', 'source.render'), ('导出成品小说', 'project.export')):
            with self.subTest(query=query):
                definition = self.call('list_capabilities', {'query': query})['structuredContent']['operations'][0]
                self.assertEqual(definition['id'], expected)
                invocation = definition['invocation']
                self.assertIn(invocation['tool'], visible)
                arguments = {} if expected != 'project.export' else definition['examples'][0]
                direct = self.call(definition['mcp'], {'projectRoot': str(self.root), **arguments})
                bridged = self.call(invocation['tool'], {'projectRoot': str(self.root),
                                    'operation': expected, 'arguments': arguments})
                self.assertFalse(bridged['isError'], bridged)
                self.assertEqual(bridged, direct)
        self.assertTrue((self.root / '工作台/成品正文.md').is_file())

    def test_cli_bridge_requires_project_and_calls_existing_handler(self):
        process = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project',
                                  'tools', 'export', '--project-root', str(self.root), '--operation', 'source.export',
                                  '--arguments', json.dumps({'output': '工作台/cli.md'})], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout)['code'], 'SOURCE_EXPORTED')
        self.assertIn('他身高一米九。', (self.root / '工作台/cli.md').read_text())

    def test_bridge_rejects_wrong_effect_approval_nested_root_and_recursive_calls(self):
        before = self.files()
        for tool, operation, arguments in (
            ('read_discovered_operation', 'source.export', {'output': '工作台/no.md'}),
            ('export_discovered_operation', 'source.render', {}),
            ('read_discovered_operation', 'project.workflow.artifact-approve', {}),
            ('export_discovered_operation', 'project.structure.commit', {}),
            ('read_discovered_operation', 'knowledge.index-rebuild', {}),
            ('read_discovered_operation', 'capabilities.read', {}),
            ('read_discovered_operation', 'capabilities.list', {}),
            ('export_discovered_operation', 'capabilities.export', {}),
            ('read_discovered_operation', 'source.render', {'projectRoot': '/other'}),
            ('read_discovered_operation', 'os.system', {'command': 'true'}),
        ):
            with self.subTest(operation=operation):
                result = self.call(tool, {'projectRoot': str(self.root), 'operation': operation, 'arguments': arguments})
                self.assertTrue(result['isError'], result)
                self.assertEqual(self.files(), before)
        result = self.call('read_discovered_operation', {'operation': 'source.render', 'arguments': {}})
        self.assertTrue(result['isError'])

    def test_bridge_reuses_nested_validation_and_preserves_pov_selection(self):
        invalid = self.call('read_discovered_operation', {'projectRoot': str(self.root), 'operation': 'source.query',
                            'arguments': {'selection': {'ownerTypes': [3]}}})
        self.assertTrue(invalid['isError'])
        self.assertIn('arguments.selection.ownerTypes[0]', invalid['structuredContent']['error'])
        query = {'selection': {'knowledgeMode': 'perspective', 'perspectiveIds': ['character-ALPHA']}}
        spec = next(row for row in operations() if row.id == 'source.query')
        direct = self.call(spec.name, {'projectRoot': str(self.root), **query})
        bridged = self.call('read_discovered_operation', {'projectRoot': str(self.root),
                             'operation': spec.id, 'arguments': query})
        self.assertFalse(bridged['isError'], bridged)
        self.assertEqual(bridged, direct)
        self.assertNotIn('他身高一米九。', json.dumps(bridged, ensure_ascii=False))

    def test_export_bridge_retains_archive_review_path_and_overwrite_guards(self):
        self.source['fragments'][0]['status'] = 'archived'
        write_json(self.root / '.govel/structure.json', self.source)
        base = {'projectRoot': str(self.root), 'operation': 'source.export'}
        first = self.call('export_discovered_operation', {**base, 'arguments': {'output': '工作台/first.md'}})
        self.assertFalse(first['isError'], first)
        text = (self.root / '工作台/first.md').read_text()
        self.assertNotIn('他身高一米九。', text)
        included = self.call('export_discovered_operation', {**base,
            'arguments': {'output': '工作台/archived.md', 'selection': {'includeArchived': True}}})
        self.assertFalse(included['isError'], included)
        self.assertIn('他身高一米九。', (self.root / '工作台/archived.md').read_text())
        for arguments in ({'output': '设定/forbidden.md'}, {'output': '工作台/reviewed.md', 'requireReviewed': True}):
            before = self.files()
            self.assertTrue(self.call('export_discovered_operation', {**base, 'arguments': arguments})['isError'])
            self.assertEqual(self.files(), before)
        target = self.root / '工作台/existing.md'; target.write_bytes(b'author content')
        before = target.read_bytes()
        failed = self.call('export_discovered_operation', {**base, 'arguments': {'output': '工作台/existing.md'}})
        self.assertTrue(failed['isError'])
        self.assertEqual(target.read_bytes(), before)
        success = self.call('export_discovered_operation', {**base, 'arguments': {
            'output': '工作台/existing.md', 'overwrite': True, 'expectedFileHash': hashlib.sha256(before).hexdigest()}})
        self.assertFalse(success['isError'], success)

    def test_parser_facade_errors_are_structured_and_do_not_stop_mcp(self):
        for operation, name in (('project.settings.show', 'settings_show'), ('project.export', 'export')):
            with self.subTest(operation=operation):
                direct = self.call(name, {'projectRoot': str(self.root)})
                bridge = 'export_discovered_operation' if operation == 'project.export' else 'read_discovered_operation'
                bridged = self.call(bridge, {'projectRoot': str(self.root), 'operation': operation, 'arguments': {}})
                self.assertTrue(bridged['isError'])
                self.assertEqual(bridged['structuredContent']['code'], 'PROJECT_NOT_INITIALIZED')
                self.assertEqual(bridged, direct)
        self.assertFalse(self.call('list_capabilities', {'query': '导出设定'})['isError'])

    def test_cli_help_and_mcp_share_descriptions_and_examples(self):
        parser = build_parser()
        def leaf(path):
            node = parser
            for name in path:
                node = next(a for a in node._actions if isinstance(a, argparse._SubParsersAction)).choices[name]
            return node
        for identity in ('source.export', 'source.render', 'project.settings.show', 'project.export', 'project.doctor'):
            spec = next(row for row in operations() if row.id == identity)
            node = leaf(['project', *spec.cli])
            self.assertEqual(node.description, spec.description)
            for action in node._actions:
                if action.dest == 'help':
                    continue
                key = ''.join([action.dest.split('_')[0], *[word.title() for word in action.dest.split('_')[1:]]])
                if action.help:
                    self.assertEqual(spec.input_schema['properties'][key].get('description'), action.help)
            for example in spec.examples:
                validate_arguments(spec.input_schema, example)
        exported = leaf(['project', 'source', 'export']).format_help()
        self.assertIn('默认不含归档', exported)
        self.assertIn('expectedFileHash', exported)
        self.assertIn('不是成品正文', exported)
        self.assertIn('设定阅读文件用 project source export', leaf(['project', 'export']).format_help())

    def test_cli_and_stdio_mcp_intent_results_agree(self):
        expected = list_capabilities(query='查看人物资料')
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'tools', 'list',
                              '--query', '查看人物资料'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout), expected)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'list_capabilities', 'arguments': {'query': '查看人物资料'}}}
        process = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/mcp_server.py')],
                                 input=json.dumps(request) + '\n', capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['result']['structuredContent'], expected)


if __name__ == '__main__':
    unittest.main()
