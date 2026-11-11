from __future__ import annotations
from scripts.core.chapter_receipt import chapter_receipt

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifacts import file_sha256
from scripts.core.continuity import complete_continuity, continuity_preview
from scripts.core.finalization import finalize_chapter
from scripts.core.jsonio import read_json, write_json
from scripts.core.project import continuation_policy
from scripts.core.settings import settings_schema
from scripts.core.structure_store import load_structure, save_structure
from scripts.core.task_prepare import prepare_task
from tests.fixtures_current import create_current_project
from tests.fixtures_workflow import review_ready_project

ROOT = Path(__file__).resolve().parents[1]


class TaskPrepareTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures_knowledge import staged_reference
        self.enterContext(staged_reference())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_current_project(self.root, chapter_count=2)
        self.chapter_id = self.fixture["chapterIds"][0]

    def codes(self, task="writer", **kwargs):
        report = prepare_task(self.root, task, chapter_id=kwargs.pop("chapter_id", self.chapter_id), **kwargs)
        return report, {row["code"] for row in report["missing"]}

    def fill_source(self, entity_id=None):
        entity_id = entity_id or self.fixture['locationId']
        source = load_structure(self.root)
        source['fieldDefinitions'].append({'fieldId': 'field-PREPARE-DETAIL', 'key': 'location.detail', 'label': '地点详情',
                                          'valueType': 'text', 'appliesTo': ['location']})
        source['attributes'].append({'attributeId': 'attribute-PREPARE-DETAIL', 'ownerId': entity_id, 'fieldId': 'field-PREPARE-DETAIL',
                                     'values': [{'valueId': 'value-PREPARE-DETAIL', 'value': '仓库位于东侧'}]})
        source['references'].append({'sourceId': self.chapter_id, 'targetId': entity_id, 'kind': 'located_in'})
        write_json(self.root/'.govel/structure.json', source)
        return source

    def test_read_list_references_current_source_for_scope_entities(self):
        from scripts.core.source_model import snapshot_hash
        source = self.fill_source()
        report, codes = self.codes('writer')
        row = next(item for item in report['readList'] if item.get('kind') == 'source-query')
        self.assertEqual(row['sourceHash'], snapshot_hash(source))
        self.assertIn(self.fixture['locationId'], report['scope']['entities'])
        self.assertTrue(report['readiness']['scopeReady'])
        self.assertTrue(report['reading']['sourceReads'])

    def test_prepare_publishes_a_source_reference_not_redundant_content(self):
        self.fill_source()
        report, _ = self.codes('writer')
        self.assertNotIn('settingAtoms', report)
        row = next(item for item in report['readList'] if item.get('kind') == 'source-query')
        self.assertEqual(row['tool'], 'query_source_chapter_plan')
        self.assertNotIn('仓库位于东侧', json.dumps(row, ensure_ascii=False))
        self.assertTrue(row['sourcePlanHash'])

    def test_no_markdown_card_is_needed_for_current_source_scope(self):
        report, codes = self.codes('writer')
        self.assertTrue(report['readiness']['scopeReady'])
        self.assertNotIn('CHAPTER_ENTITY_LINK_MISSING', codes)
        self.assertNotIn('SETTING_CARD_MISSING', codes)
        self.assertFalse(any(row.get('path', '').startswith('设定/') for row in report['readList']))

    def test_chapter_references_scope_current_entities(self):
        source = load_structure(self.root)
        source['entities'].append({'entityId': 'character-NEW', 'entityType': 'character', 'name': '新角色', 'aliases': [], 'status': 'active'})
        source['references'].append({'sourceId': self.chapter_id, 'targetId': 'character-NEW', 'kind': 'features'})
        write_json(self.root/'.govel/structure.json', source)
        report, _ = self.codes('writer')
        self.assertIn('character-NEW', report['scope']['entities'])
        self.assertTrue(report['reading']['sourceReads'])

    def test_context_status_is_reported_and_flags_pov_scoping(self):
        """上下文状态必须进报告；不传 --pov-character 时明确报未做 POV 过滤。"""
        report, _ = self.codes("writer")
        self.assertTrue(report["context"]["applicable"])
        self.assertFalse(report["context"]["povScoped"], "不传视角即不做 POV 过滤，须显式报出以免同对话串味")
        report, _ = self.codes("writer", pov_character=self.fixture["characterId"])
        self.assertTrue(report["context"]["povScoped"])

    def test_editor_prepare_uses_editor_context_budget(self):
        report, _ = self.codes("editor")
        self.assertTrue(report["context"]["applicable"])
        self.assertEqual(report["context"]["budget"]["total"],
                         continuation_policy()["memoryBudgets"]["editor"])

    def test_prepare_loads_versioned_reference_knowledge(self):
        report = prepare_task(self.root, 'planner', chapter_id=self.chapter_id, chapter_goal='关系推进')
        self.assertEqual(report['knowledgePack']['role'], 'REFERENCE KNOWLEDGE')
        item = report['knowledgePack']['items'][0]
        self.assertEqual(item['assetId'], 'KN-RELATIONSHIP-TRANSITION')
        self.assertIn('可观察', item['excerpt'])
        self.assertEqual(item['sha256'], report['knowledgeRoute']['items'][0]['sha256'])

    def test_prepare_consumes_the_unified_builder_without_rerouting_knowledge(self):
        """准备清单只消费 Builder：同输入下 hash/pack/manifest 必须与直接 Builder 完全一致。"""
        from scripts.core import context_builder
        from scripts.core.knowledge_router import route_knowledge
        report = prepare_task(self.root, 'planner', chapter_id=self.chapter_id, chapter_goal='关系推进')
        direct = context_builder.build_station_context(
            self.root, self.chapter_id, task_type='planner', chapter_goal='关系推进',
            view='station', read_only=True)
        self.assertEqual(report['context']['contextHash'], direct['contextHash'])
        self.assertEqual(report['knowledgePack'], direct['knowledgePack'])
        self.assertEqual(report['knowledgeRoute'], direct['knowledgeRoute'])
        self.assertEqual(report['context']['dependencyManifest'], direct['dependencyManifest'])
        self.assertEqual(report['context']['layers'], direct['layers'])
        # 旧实现会在准备清单里二次 route 一份完整结果（含 coverage.index 等重字段）；
        # 现在是 Builder 的轻量投影，形状与直接调用一致。
        self.assertNotIn('coverage', report['knowledgeRoute'])
        self.assertEqual(report['knowledgePack']['role'], 'REFERENCE KNOWLEDGE')

    def test_prepare_reports_station_chapter_placement(self):
        """当前章节真源是 placement；volumeId 只能作为派生的 derivedVolumeId 出现。"""
        from scripts.core.source_placement import chapter_placement, chapter_volume_id
        report = prepare_task(self.root, 'planner', chapter_id=self.chapter_id)
        chapter = report['chapter']
        raw = next(row for row in load_structure(self.root)['chapters']
                   if row['chapterId'] == self.chapter_id)
        self.assertNotIn('volumeId', chapter, 'volumeId 不能再看起来像当前章节真源字段')
        self.assertIn('placement', chapter)
        self.assertEqual(chapter['placement'], chapter_placement(raw, load_structure(self.root)['schemaVersion']))
        self.assertEqual(chapter['derivedVolumeId'],
                         chapter_volume_id(raw, load_structure(self.root)['schemaVersion']))
        self.assertEqual(chapter['derivedVolumeId'], load_structure(self.root)['volumes'][0]['volumeId'])

    def test_prepare_knowledge_empty_without_chapter_goal(self):
        report = prepare_task(self.root, 'planner', chapter_id=self.chapter_id)
        self.assertEqual(report['knowledgePack']['items'], [])
        self.assertEqual(report['knowledgePack']['mode'], 'curated')
        self.assertIsNone(report['knowledgeRoute'])

    def test_context_status_is_absent_for_tasks_that_do_not_consume_it(self):
        report, _ = self.codes("titler")
        self.assertFalse(report["context"]["applicable"])
        self.assertNotIn("povScoped", report["context"])

    def test_stage_tasks_require_their_own_materials(self):
        writer, writer_codes = self.codes("writer")
        self.assertFalse(writer["readiness"]["taskReady"])
        self.assertNotIn("SOURCE_CHAPTER_PLAN_MISSING", writer_codes)
        self.assertIn("STAGE_A_NOT_APPROVED", writer_codes)
        planner, planner_codes = self.codes("planner")
        self.assertNotIn("OUTLINE_MISSING", planner_codes, "规划任务正在产出推演，不应被缺推演拦住")
        self.assertNotIn("CANDIDATE_PROSE_MISSING", planner_codes)
        editor, editor_codes = self.codes("editor")
        self.assertIn("CANDIDATE_PROSE_MISSING", editor_codes)

    def test_unreferenced_entity_does_not_expand_chapter_scope(self):
        self.fill_source()
        source = load_structure(self.root)
        source['entities'].append({'entityId': 'character-UNRELATED', 'entityType': 'character', 'name': '路人', 'aliases': [], 'status': 'active'})
        write_json(self.root/'.govel/structure.json', source)
        report, _ = self.codes('writer')
        self.assertIn(self.fixture['locationId'], report['scope']['entities'])
        self.assertNotIn('character-UNRELATED', report['scope']['entities'])

    def test_source_read_credential_refuses_changed_source(self):
        from scripts.core.operations import call_operation
        self.fill_source()
        report, _ = self.codes('writer')
        read_id = report['reading']['sourceReads'][0]['readId']
        source = load_structure(self.root); source['attributes'][-1]['values'][0]['value'] = '仓库位于西侧'
        write_json(self.root/'.govel/structure.json', source)
        with self.assertRaises(ValueError):
            call_operation('task.read-batch', self.root, {'credential': report['reading']['credential'], 'readIds': [read_id]})

    def test_stale_stage_a_binding_blocks_writer(self):
        fixture = review_ready_project(self.root, chapter_count=1)
        chapter = read_json(self.root / ".govel/structure.json")["chapters"][0]
        report, codes = self.codes("writer", chapter_id=fixture["chapterIds"][0])
        self.assertNotIn("STAGE_A_OUTLINE_MISMATCH", codes)
        source = load_structure(self.root)
        next(f for f in source['fragments'] if f['ownerId'] == chapter['chapterId'])['text'] += '临时改动的推演。'
        write_json(self.root/'.govel/structure.json', source)
        report, codes = self.codes("writer", chapter_id=fixture["chapterIds"][0])
        self.assertIn("STAGE_A_OUTLINE_MISMATCH", codes)
        self.assertFalse(report["readiness"]["taskReady"])

    def test_unfinished_predecessor_continuity_blocks_next_chapter(self):
        fixture = review_ready_project(self.root, chapter_count=2)
        first, second = fixture["chapterIds"]
        finalize_chapter(self.root, first, skip_title=True, binding_generation=chapter_receipt(self.root, first)['bindingGeneration'])
        report, codes = self.codes("writer", chapter_id=second)
        self.assertIn("CONTINUITY_REQUIRED", codes)
        self.assertIn(first, [row["chapterId"] for row in report["pendingContinuity"]])
        preview = continuity_preview(self.root, first)
        complete_continuity(self.root, first, batch_hash=preview["batchHash"], approved=True, confirmation={
            "decisions": [{"itemId": row["itemId"], "resolution": "no_change", "reason": "作者确认无需更新", "evidence": []}
                          for row in preview["batch"]["items"]],
            "summary": {"resolution": "not_required", "reason": "样本无需摘要"},
        })
        report, codes = self.codes("writer", chapter_id=second)
        self.assertNotIn("CONTINUITY_REQUIRED", codes)
        self.assertTrue(report["readiness"]["authorizationReady"])

    def test_pov_hard_constraints_are_reported_as_task_gap(self):
        from scripts.core import context_builder

        class Boom(ValueError):
            code = "POV_HARD_CONSTRAINTS_UNAVAILABLE"

        with patch.object(context_builder, "build_station_context", side_effect=Boom("hidden")):
            report, codes = self.codes("writer", pov_character=self.fixture["characterId"])
        self.assertIn("POV_HARD_CONSTRAINTS_UNAVAILABLE", codes)
        self.assertFalse(report["readiness"]["contextReady"])

    def test_prepare_is_read_only_and_reports_loading_plan(self):
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        report, _ = self.codes("planner")
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(report["promptPlan"]["contracts"])
        self.assertEqual(report["promptPlan"]["contracts"][0]["readRanges"][0]["startLine"], 1)
        self.assertFalse(any(isinstance(row, dict) and "files" in row for row in report["promptPlan"]["onDemandReferences"]))

    def test_cli_exit_code_reflects_readiness(self):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "prepare",
                               "--project-root", str(self.root), "--task", "writer", "--chapter-id", self.chapter_id],
                              cwd=ROOT, capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(payload["code"], "TASK_NOT_READY")
        self.assertEqual(payload["station"], "govel-writer")
        self.assertTrue(payload["blocking"])

    def test_unknown_task_and_selector_fail_closed(self):
        with self.assertRaises(ValueError):
            prepare_task(self.root, "unknown-task", chapter_id=self.chapter_id)
        report, codes = self.codes("planner", entities=["不存在的实体"])
        self.assertIn("ENTITY_UNKNOWN", codes)


if __name__ == "__main__":
    unittest.main()
