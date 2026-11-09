"""Guards 1-3: registry consumers, transport/schema agreement, real task wiring."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core import tool_plan
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import new_operations, operations, validate_arguments
from scripts.novelctl import build_parser
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.task_prepare import prepare_task
from scripts.core.tool_catalog import load_catalog
from scripts.core.tool_plan import build_tool_plan, execute_task_actions
from scripts.core.workflow import WorkflowError
from tests.fixtures_workflow import review_ready_project

ROOT = Path(__file__).resolve().parents[1]


class RegistryConsumerGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = review_ready_project(self.root)
        self.chapter = self.fixture['chapterIds'][0]

    def action_status(self, plan, capability):
        return next(row['status'] for row in plan['toolActions'] if row['capabilityId'] == capability)

    def test_catalog_validates_and_bindings_resolve(self):
        catalog = load_catalog()
        self.assertEqual(catalog['schemaVersion'], 3)
        specs = {s.id for s in operations()}
        for cap in catalog['capabilities']:
            with self.subTest(capability=cap['id']):
                if cap['binding']['kind'] == 'operation':
                    self.assertIn(cap['binding']['ref'], specs)
                else:
                    self.assertEqual(cap['binding']['kind'], 'skill')

    def test_positive_negative_unknown_samples_move_the_plan(self):
        from scripts.core.mechanical_evidence import run_diagnostics
        result = run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        plan = build_tool_plan(self.root, 'editor', chapter_id=self.chapter)
        # 正样本：证据当前（通过与否另计） → 条件不成立，不产生动作
        self.assertEqual(plan['facts']['diagnostics.current'], 'true')
        self.assertNotIn('chapter.mechanical', [row['capabilityId'] for row in plan['toolActions']])
        # 负样本：候选正文变化后机械证据失效 → 必要动作出现
        artifact = read_json(self.root / result['artifactPath'])
        artifact['payload']['draftSha256'] = 'f' * 64
        write_json(self.root / result['artifactPath'], artifact)
        plan = build_tool_plan(self.root, 'editor', chapter_id=self.chapter)
        self.assertEqual(self.action_status(plan, 'chapter.mechanical'), 'action-required')
        self.assertTrue(next(row['necessary'] for row in plan['toolActions'] if row['capabilityId'] == 'chapter.mechanical'))
        # unknown 样本：事实不可读时既不适用也不放行
        with patch.object(tool_plan, 'evidence_current', side_effect=OSError('unreadable')):
            plan = build_tool_plan(self.root, 'editor', chapter_id=self.chapter)
        self.assertEqual(plan['facts']['diagnostics.current'], 'unknown')
        self.assertTrue(plan['unresolved'], '必要事实 unknown 必须显式列出，不能静默放行')

    def test_policy_perturbation_changes_plan_or_fails_closed(self):
        catalog = deepcopy(load_catalog())
        mechanical = next(c for c in catalog['capabilities'] if c['id'] == 'chapter.mechanical')
        mechanical['requiredFor'] = []
        with patch.object(tool_plan, 'load_catalog', return_value=catalog):
            plan = build_tool_plan(self.root, 'editor', chapter_id=self.chapter)
        self.assertNotIn('chapter.mechanical', [r['capabilityId'] for r in plan['toolActions'] if r['necessary']],
                         'requiredFor 必须真正决定必要性，而不是装饰')
        broken = deepcopy(load_catalog())
        next(c for c in broken['capabilities'] if c['id'] == 'chapter.mechanical')['when']['fact'] = 'unknown.fact'
        with patch('scripts.core.tool_catalog.cached_json', return_value=broken):
            with self.assertRaises(StructureError):
                load_catalog()

    def test_registry_lists_only_runnable_or_classified_scripts(self):
        catalog = load_catalog()
        for row in catalog['tools']:
            self.assertIn(row['kind'], ('operation', 'internal', 'maintenance'))
            self.assertTrue((ROOT / row['path']).is_file())
        self.assertTrue(any(row['kind'] == 'internal' and row.get('parent') for row in catalog['tools']))

    def test_station_cards_are_generated_from_the_catalogue(self):
        from scripts.core import tool_cards
        self.assertTrue(tool_cards.check_station_cards()['ok'], '工位条件段必须与能力目录零漂移')
        editor = tool_cards.render_card('editor')
        self.assertIn('chapter.mechanical', editor)
        self.assertIn('chapter.finalize', editor)
        self.assertIn('govel-editor/SKILL.md', editor)
        self.assertNotIn('summary.commit', editor, '候选写入类能力不进语义工位的条件段')
        # 每个出现的能力 ID 都必须能在目录里解析（工位入口的可达性）
        catalog = {cap['id'] for cap in load_catalog()['capabilities']}
        for line in editor.splitlines():
            if line.startswith('| ') and not line.startswith('| 能力 ID'):
                self.assertIn(line.split('|')[1].strip(), catalog)
        # 扰动：目录变化必须让卡片漂移（否则生成物是死副本）
        original = tool_cards.list_capabilities
        def without_timeline(*args, **kwargs):
            data = original(*args, **kwargs)
            data['capabilities'] = [c for c in data['capabilities'] if c['id'] != 'story.timeline']
            return data
        with patch.object(tool_cards, 'list_capabilities', side_effect=without_timeline):
            result = tool_cards.check_station_cards()
        self.assertFalse(result['ok'])
        self.assertTrue(any('editor' in path for path in result['drift']), result)


class TransportAlignmentGuards(unittest.TestCase):
    def test_every_operation_cli_generates_a_parsable_invocation(self):
        parser = build_parser()
        for spec in new_operations():
            with self.subTest(operation=spec.id):
                argv = ['project', *spec.cli]
                for key, field in spec.input_schema['properties'].items():
                    if key == 'projectRoot':
                        if key in spec.input_schema.get('required', []):
                            argv += ['--project-root', '/tmp/project']
                        continue
                    flag = '--' + ''.join(('-' + c.lower()) if c.isupper() else c for c in key).lstrip('-')
                    if field.get('type') == 'boolean': argv.append(flag)
                    elif field.get('type') == 'integer': argv += [flag, '1']
                    elif field.get('type') == 'array': argv += [flag, json.dumps({}) if field.get('items', {}).get('type') == 'object' else '"x"']
                    elif field.get('type') == 'object': argv += [flag, '{}']
                    else: argv += [flag, field.get('enum', ['x'])[0] if field.get('enum') else 'x']
                parsed = parser.parse_args(argv)
                self.assertEqual(parsed.operation_binding, spec.id)

    def test_arguments_validator_rejects_missing_unknown_and_wrong_types(self):
        schema = {'type': 'object', 'properties': {'projectRoot': {'type': 'string'}, 'depth': {'type': 'integer'}},
                  'required': ['projectRoot'], 'additionalProperties': False}
        validate_arguments(schema, {'projectRoot': '/x'})
        for bad in ({}, {'projectRoot': '/x', 'oops': 1}, {'projectRoot': '/x', 'depth': 'two'}, {'projectRoot': 5}):
            with self.subTest(arguments=bad), self.assertRaises(StructureError):
                validate_arguments(schema, bad)

    def test_mcp_workflow_writes_hold_the_project_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root)
            chapter = fixture['chapterIds'][0]
            def call(name, arguments):
                proc = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                                      input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                        'params': {'name': name, 'arguments': arguments}}) + '\n',
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)['result']
            result = call('workflow_set_next', {'projectRoot': str(root), 'chapterId': chapter,
                                                'nextAction': 'editor', 'reason': 'test'})
            self.assertFalse(result['isError'], result)
            self.assertEqual(json.loads((root / '.govel/workflow.json').read_text())['nextAction']['action'], 'editor')
            from scripts.core.locks import project_lock, project_lock_observation
            self.assertFalse(project_lock_observation(root)['locked'], '写入结束必须释放锁')
            # 竞争样本：锁被活进程持有时 MCP 写入必须拒绝且不动 workflow
            before = (root / '.govel/workflow.json').read_bytes()
            with project_lock(root):
                result = call('workflow_set_next', {'projectRoot': str(root), 'chapterId': chapter,
                                                    'nextAction': 'writer', 'reason': 'contended'})
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['code'], 'PROJECT_LOCKED')
            self.assertEqual((root / '.govel/workflow.json').read_bytes(), before)

    def test_skill_and_operation_bindings_are_different_types(self):
        catalog = load_catalog()
        spec_names = {s.name for s in operations()}
        for cap in catalog['capabilities']:
            with self.subTest(capability=cap['id']):
                binding = cap['binding']
                if binding['kind'] == 'skill':
                    self.assertTrue(binding['ref'].endswith('SKILL.md'))
                    self.assertNotIn(binding['ref'], spec_names)
                else:
                    self.assertIn(binding['ref'], {s.id for s in operations()})

    def test_mcp_surface_covers_task_chain_without_maintenance_tools(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                              input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'view': 'audit'}}) + '\n',
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = {tool['name'] for tool in json.loads(proc.stdout)['result']['tools']}
        for required in ('query_logic', 'get_tool_plan', 'run_mechanical_diagnostics', 'prepare_task',
                         'read_continuation_context', 'prepare_story_summary', 'commit_story_summary',
                         'workflow_chapter_receipt', 'workflow_continuity_complete', 'undo_structure_operation'):
            with self.subTest(tool=required):
                self.assertIn(required, names)
        for forbidden in ('state_rebuild', 'upgrade_apply', 'import_scan', 'install_antigravity', 'project_structure_gc'):
            with self.subTest(tool=forbidden):
                self.assertNotIn(forbidden, names)
        by_name = {tool['name']: tool for tool in json.loads(proc.stdout)['result']['tools']}
        self.assertTrue(by_name['run_mechanical_diagnostics']['annotations']['readOnlyHint'] is False)
        self.assertTrue(by_name['query_logic']['annotations']['readOnlyHint'] is True)

    def test_execution_never_approves_and_skips_protected_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root)
            workflow_before = (root / '.govel/workflow.json').read_bytes()
            result = execute_task_actions(root, 'editor', chapter_id=fixture['chapterIds'][0], authorization='read')
            self.assertFalse(result['approvalsPerformed'])
            self.assertEqual((root / '.govel/workflow.json').read_bytes(), workflow_before,
                             'read 授权不得改写批准状态')
            self.assertTrue(all(item.get('operation') != 'project.workflow.artifact-approve' for item in result['executed']))


class TaskEntryWiringGuards(unittest.TestCase):
    def test_doctor_and_receipt_surface_bounded_suggestions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root)
            from scripts.novelctl import project_doctor
            from scripts.core.chapter_receipt import chapter_receipt
            doctor = project_doctor(root)
            self.assertLessEqual(len(doctor['suggestedActions']), 4)
            catalog = {cap['id'] for cap in load_catalog()['capabilities']}
            for row in doctor['suggestedActions']:
                with self.subTest(capability=row['capabilityId']):
                    self.assertIn(row['capabilityId'], catalog | {'capability.unresolved'})
                    self.assertIn('reason', row)
            receipt = chapter_receipt(root, fixture['chapterIds'][0])
            self.assertIn('suggestedActions', receipt)
            # 反馈只建议不授权：不携带批准令牌，也不含执行结果
            self.assertTrue(all('approve' not in json.dumps(row).lower() or row.get('authorization') is None
                                for row in receipt['suggestedActions']))

    def test_prepare_reports_plan_action_and_cli_exposes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root)
            chapter_id = fixture['chapterIds'][0]
            # 制造倒挂：第二章的故事时间早于第一章
            structure = load_structure(root)
            from scripts.core.structure_store import save_structure
            structure['entities'] += [
                {'entityId': 'TA-EARLY', 'entityType': 'time_anchor', 'name': '早上', 'aliases': [], 'status': 'active', 'timeOrdinal': 1},
                {'entityId': 'TA-LATE', 'entityType': 'time_anchor', 'name': '晚上', 'aliases': [], 'status': 'active', 'timeOrdinal': 2},
            ]
            structure['chapters'][0]['storyTimeStartId'] = 'TA-LATE'
            structure['chapters'][1]['storyTimeStartId'] = 'TA-EARLY'
            save_structure(root, structure)
            report = prepare_task(root, 'planner', chapter_id=chapter_id)
            timeline = next(row for row in report['toolPlan']['toolActions'] if row['capabilityId'] == 'story.timeline')
            self.assertEqual(timeline['status'], 'recommended')
            self.assertEqual(timeline['observed'], 'true')
            cli = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'prepare',
                                  '--task', 'planner', '--chapter-id', chapter_id, '--project-root', str(root)],
                                 capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            payload = json.loads(cli.stdout)
            self.assertIn('toolPlan', payload)
            self.assertTrue(any(row['capabilityId'] == 'story.timeline' for row in payload['toolPlan']['toolActions']))

    def test_prepare_blocks_when_necessary_evidence_is_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root)
            chapter_id = fixture['chapterIds'][0]
            artifact_path = Path(read_json(root/'.govel/workflow.json')['artifacts'][chapter_id]['diagnostics']['artifactPath'])
            artifact = read_json(artifact_path)
            artifact['payload']['draftSha256'] = 'f' * 64
            write_json(artifact_path, artifact)
            report = prepare_task(root, 'editor', chapter_id=chapter_id)
            self.assertFalse(report['ok'])
            self.assertIn('TOOL_ACTION_REQUIRED', {row['code'] for row in report['blocking']})

    def test_advance_to_review_requires_current_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = review_ready_project(root, chapter_count=1)
            chapter_id = fixture['chapterIds'][0]
            from scripts.core.structure_store import save_structure
            structure = load_structure(root)
            structure['chapters'][0]['status'] = 'draft'
            save_structure(root, structure)
            # 机械证据指向不再匹配的输入（带指纹）→ 不再是「当前证据」
            artifact_path = Path(read_json(root/'.govel/workflow.json')['artifacts'][chapter_id]['diagnostics']['artifactPath'])
            artifact = read_json(artifact_path)
            artifact['payload']['inputHash'] = 'f' * 64
            artifact['payload']['run'] = {'mode': 'plot', 'intensityLevel': 1, 'sceneParagraphs': '', 'strictFloor': False}
            write_json(artifact_path, artifact)
            with self.assertRaises(WorkflowError) as caught:
                advance_chapter_status(root, chapter_id, 'review')
            self.assertEqual(caught.exception.code, 'DIAGNOSTICS_STALE')


if __name__ == '__main__':
    unittest.main()
