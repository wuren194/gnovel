"""CLI / MCP 的 context 通道只做序列化，不得改变统一 Builder 的结果。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.context_builder import build_station_context
from tests.cli_harness import run_cli as in_process_cli, clear_runtime_caches

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, process=False) -> dict:
    proc = (subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=60) if process else in_process_cli(['project', *args]))
    if proc.returncode not in (0, 1):
        raise AssertionError(proc.stdout + proc.stderr)
    return json.loads(proc.stdout)


def run_mcp(tool: str, arguments: dict, *, process=False) -> dict:
    # These tests compare the legacy machine interface. Default model text is
    # separately exercised by test_context_semantic_view.
    if tool in {'read_continuation_context', 'prepare_task'}:
        arguments = {'view': 'station', **arguments}
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': tool, 'arguments': arguments}}
    if not process:
        from scripts.mcp_server import handle
        clear_runtime_caches()
        try:
            return json.loads(handle(request)['result']['content'][0]['text'])
        finally:
            clear_runtime_caches()
    proc = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                          input=json.dumps(request) + '\n', cwd=ROOT, capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise AssertionError(proc.stderr)
    payload = json.loads(proc.stdout.splitlines()[-1])
    return json.loads(payload['result']['content'][0]['text'])


class ContextTransportTests(unittest.TestCase):
    def setUp(self):
        from tests.test_context_builder import context_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / '正文').mkdir()
        context_source(self.root)

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_cli_and_mcp_context_read_agree_with_direct_builder(self):
        before = self.inventory()
        cli = run_cli('task', 'context', '--project-root', str(self.root),
                      '--chapter-id', 'CH-AAA', '--task-type', 'planner', '--chapter-goal', '关系推进', process=True)
        mcp = run_mcp('read_continuation_context', {'projectRoot': str(self.root), 'chapterId': 'CH-AAA',
                                                   'taskType': 'planner', 'chapterGoal': '关系推进'}, process=True)
        direct = build_station_context(str(self.root), 'CH-AAA', task_type='planner',
                                       chapter_goal='关系推进', view='station', read_only=True)
        for payload in (cli, mcp):
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['contextHash'], direct['contextHash'])
            self.assertEqual(payload['dependencyManifest'], direct['dependencyManifest'])
            self.assertEqual(payload['knowledgePack'], direct['knowledgePack'])
            self.assertEqual([row['role'] for row in payload['layers']],
                             [row['role'] for row in direct['layers']])
            self.assertEqual(payload['contextBudget'], direct['contextBudget'])
        self.assertEqual({key: value for key, value in cli.items() if key != 'code'},
                         {key: value for key, value in mcp.items() if key != 'code'},
                         '两个通道只允许 code 包装不同，上下文本身必须一致')
        self.assertEqual(before, self.inventory(), 'context.read 不得写任何项目文件')

    def test_cli_and_mcp_context_health_agree(self):
        cli = run_cli('source', 'context-health', '--project-root', str(self.root),
                      '--chapter-id', 'CH-AAA', '--task', 'planner', '--chapter-goal', '关系推进')
        mcp = run_mcp('query_source_context_health', {'projectRoot': str(self.root), 'chapterId': 'CH-AAA',
                                                     'task': 'planner', 'chapterGoal': '关系推进'})
        self.assertEqual(cli, mcp)
        self.assertEqual(cli['code'], 'CONTEXT_HEALTH')
        self.assertTrue(cli['ready'])
        self.assertEqual([row for row in cli['issues'] if row['severity'] == 'blocking'], [])

    def test_cli_and_mcp_context_preheat_agree_and_cache_nothing(self):
        before = self.inventory()
        cli = run_cli('source', 'context-preheat', '--project-root', str(self.root),
                      '--after-chapter-id', 'CH-AAA')
        mcp = run_mcp('preheat_source_next_chapter', {'projectRoot': str(self.root), 'afterChapterId': 'CH-AAA'})
        self.assertEqual(cli, mcp)
        self.assertEqual(cli['metadata']['nextChapterId'], 'CH-BBB')
        self.assertFalse(cli['coverage']['futurePlotGenerated'])
        self.assertFalse(cli['coverage']['proseRead'])
        self.assertFalse(cli['coverage']['contextPackCached'])
        self.assertEqual(before, self.inventory(), 'preheat 不得写任何项目文件')

    def test_all_context_read_entries_write_nothing(self):
        """四个 Context read 入口清单：legacy facade CLI、直连 novelctl context、project task context、MCP context.read。

        全部必须 read_only：不得经 state_cache.load() → rebuild() 发布派生缓存。
        缓存重建只属于显式维护命令 novelctl project state rebuild。
        """
        before = self.inventory()
        legacy = subprocess.run([sys.executable, str(ROOT / 'scripts/continuation_context.py'),
                                 '--project-root', str(self.root), '--chapter-id', 'CH-AAA',
                                 '--task-type', 'planner', '--view', 'station'],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(legacy.returncode, 0, legacy.stdout + legacy.stderr)
        direct = run_cli('context', '--project-root', str(self.root), '--chapter-id', 'CH-AAA',
                         '--task-type', 'planner', '--view', 'station')
        task = run_cli('task', 'context', '--project-root', str(self.root), '--chapter-id', 'CH-AAA',
                       '--task-type', 'planner')
        mcp = run_mcp('read_continuation_context', {'projectRoot': str(self.root), 'chapterId': 'CH-AAA',
                                                    'taskType': 'planner'})
        prepare = run_cli('prepare', '--project-root', str(self.root), '--task', 'planner',
                          '--chapter-id', 'CH-AAA')
        health = run_cli('source', 'context-health', '--project-root', str(self.root),
                         '--chapter-id', 'CH-AAA', '--task', 'writer')
        self.assertEqual(self.inventory(), before, 'Context read 入口不得写任何项目文件（含派生缓存）')
        self.assertFalse([key for key in self.inventory() if '.govel/cache' in key],
                         '只读入口不得发布派生缓存')
        for payload in (json.loads(legacy.stdout), task, mcp):
            self.assertEqual(payload['contextHash'], mcp['contextHash'],
                             '只读装配的入口必须给出同一份上下文')
        # novelctl project context 走传输封装（ok/code 计入预算并重算 hash），
        # 因此 hash 可以不同，但依赖与认识层必须是同一份判定。
        self.assertEqual(direct['dependencyManifest'], mcp['dependencyManifest'])
        self.assertEqual(direct['layers'], mcp['layers'])
        self.assertTrue(prepare['context']['applicable'])
        self.assertEqual(prepare['context']['contextHash'], mcp['contextHash'],
                         'prepare 的上下文报告与 context.read 是同一份装配')
        self.assertEqual(health['code'], 'CONTEXT_HEALTH')

    def test_state_rebuild_is_the_only_cache_publishing_entry(self):
        """显式维护操作仍然发布派生缓存——只读收口不能把它一起关掉。"""
        self.assertFalse([key for key in self.inventory() if '.govel/cache' in key])
        proc = in_process_cli(['project', 'state', 'rebuild', '--project-root', str(self.root)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue([key for key in self.inventory() if '.govel/cache' in key],
                        'state rebuild 仍应发布派生缓存')
        after = self.inventory()
        read = run_cli('context', '--project-root', str(self.root), '--chapter-id', 'CH-AAA', '--view', 'station')
        self.assertTrue(read['ok'])
        self.assertEqual(after, self.inventory(), 'rebuild 之后的 context read 不得再写')

    def test_legacy_facade_exposes_one_implementation(self):
        from scripts import continuation_context
        from scripts.core import context_builder
        self.assertIs(continuation_context.build_continuation_context,
                      context_builder.build_station_context)
        self.assertIs(continuation_context.build_station_context,
                      context_builder.build_station_context)
        self.assertIs(continuation_context.bound_station_context,
                      context_builder.bound_station_context)
        self.assertIs(continuation_context.ContinuationContextError, context_builder.ContextBuilderError)
        # facade 不携带任何独立装配逻辑。
        self.assertFalse(hasattr(continuation_context, '_epistemic_layers'))


if __name__ == '__main__':
    unittest.main()
