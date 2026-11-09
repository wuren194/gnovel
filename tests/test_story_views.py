from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.commands import entity_graph, graph_mermaid, story_timeline, timeline_mermaid
from scripts.core.entity_registry import register_entity
from scripts.core.structure_commands import (
    commit_plan, preview_create_time_anchor,
)
from scripts.core.structure_store import StructureError, load_structure, save_structure
from scripts.core.jsonio import write_json
from scripts.core.canon_proposals import resolve_canon_target, preview_canon_change
from tests.fixtures_current import create_schema5_project

ROOT = Path(__file__).resolve().parents[1]


class TimeOrdinalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root)

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=True)

    def anchors(self) -> dict:
        return {e["entityId"]: e for e in load_structure(self.root)["entities"] if e["entityType"] == "time_anchor"}

    def test_anchor_can_be_created_with_a_numeric_ordinal(self) -> None:
        plan = preview_create_time_anchor(self.root, name="温序转入", absolute_time="深秋", time_ordinal=-45)
        self.commit(plan)
        self.assertEqual(list(self.anchors().values())[0]["timeOrdinal"], -45)

    def test_non_numeric_ordinal_is_rejected(self) -> None:
        for bad in ("abc", True, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(StructureError) as ctx:
                    preview_create_time_anchor(self.root, name="x", absolute_time="y", time_ordinal=bad)
                self.assertEqual(ctx.exception.code, "INVALID_TIME_ORDINAL")


    def test_structure_validation_rejects_a_hand_edited_ordinal(self) -> None:
        structure = load_structure(self.root)
        structure["entities"].append({"entityId": "TA-BAD000001", "entityType": "time_anchor", "name": "坏锚",
                                      "aliases": [], "status": "active", "summary": "", "sourceRefs": [],
                                      "absoluteTime": "某时", "timeOrdinal": "45"})
        with self.assertRaises(StructureError) as ctx:
            save_structure(self.root, structure)
        self.assertEqual(ctx.exception.code, "STRUCTURE_INVALID")
        self.assertIn("INVALID_TIME_ORDINAL", json.dumps(ctx.exception.details, ensure_ascii=False))

    def test_reviewed_canon_proposal_sets_and_clears_ordinal(self):
        self.commit(preview_create_time_anchor(self.root, name='锚', absolute_time='某时'))
        anchor_id = list(self.anchors())[0]
        source = load_structure(self.root)
        source['fieldDefinitions'].append({'fieldId': 'FD-TIME-EVIDENCE', 'key': 'time.description',
            'label': '时点说明', 'valueType': 'topic', 'appliesTo': ['time_anchor']})
        source['attributes'].append({'attributeId': 'AT-TIME-EVIDENCE', 'ownerId': anchor_id,
            'fieldId': 'FD-TIME-EVIDENCE', 'values': []})
        source['fragments'].append({'fragmentId': 'SF-TIME-EVIDENCE', 'ownerId': anchor_id, 'order': 1,
            'contentRole': 'fact', 'contentBindings': ['AT-TIME-EVIDENCE'], 'text': '此时点由作者审定。'})
        write_json(self.root/'.gnovel/structure.json', source)
        for value in (7, None):
            current = self.anchors()[anchor_id]
            ref = resolve_canon_target(self.root, selector=anchor_id)['sessionRef']
            plan = preview_canon_change(self.root, sessionRef=ref, fieldPath='timeOrdinal',
                expectedOldExists='timeOrdinal' in current, expectedOldValue=current.get('timeOrdinal'),
                newValue=value, reason='明确调整时间序', evidence=[{'fragmentId': 'SF-TIME-EVIDENCE', 'quote': '此时点由作者审定。'}],
                approvalActor='author-test', approvalEvidence='已检查这一值的修改')
            self.commit(plan)
            self.assertEqual(self.anchors()[anchor_id].get('timeOrdinal'), value)



class TimelineOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root)
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": "TA-ZZZZ9999", "entityType": "time_anchor",
                                    "name": "最晚", "absoluteTime": "9999-12-31", "timeOrdinal": 0})
        register_entity(structure, {"entityId": "TA-AAAA0001", "entityType": "time_anchor",
                                    "name": "最早", "absoluteTime": "0001-01-01"})
        register_entity(structure, {"entityId": "TA-MMMM0002", "entityType": "time_anchor",
                                    "name": "居中", "absoluteTime": "5000-06-01", "timeOrdinal": -45})
        save_structure(self.root, structure)

    def test_ordinal_anchors_sort_numerically_before_lexical_fallback(self) -> None:
        view = story_timeline(self.root)
        self.assertEqual([a["entityId"] for a in view["anchors"]], ["TA-MMMM0002", "TA-ZZZZ9999", "TA-AAAA0001"])
        self.assertEqual(view["anchors"][0]["timeOrdinal"], -45)
        self.assertIsNone(view["anchors"][2]["timeOrdinal"])

    def test_ordinal_less_anchors_keep_the_lexical_order(self) -> None:
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": "TA-BBBB0003", "entityType": "time_anchor",
                                    "name": "次早", "absoluteTime": "1000-01-01"})
        save_structure(self.root, structure)
        view = story_timeline(self.root)
        self.assertEqual([a["entityId"] for a in view["anchors"]], ["TA-MMMM0002", "TA-ZZZZ9999", "TA-AAAA0001", "TA-BBBB0003"])

    def test_anchor_without_absolute_time_sorts_last(self) -> None:
        """空 absoluteTime 的字典序最小，不能因此跑到所有锚点之前。"""
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": "TA-NONE0004", "entityType": "time_anchor",
                                    "name": "无绝对时间", "absoluteTime": ""})
        save_structure(self.root, structure)
        view = story_timeline(self.root)
        self.assertEqual([a["entityId"] for a in view["anchors"]],
                         ["TA-MMMM0002", "TA-ZZZZ9999", "TA-AAAA0001", "TA-NONE0004"])
        self.assertEqual(view["anchors"][-1]["entityId"], "TA-NONE0004")


class ArchivedAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root)

    def _mount(self, anchor_id: str, *, status: str) -> str:
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": anchor_id, "entityType": "time_anchor",
                                    "name": "锚点", "absoluteTime": "1000-01-01", "status": status})
        first_chapter = self.fixture["chapterIds"][0]
        for chapter in structure["chapters"]:
            if chapter["chapterId"] == first_chapter:
                chapter["storyTimeStartId"] = anchor_id
        save_structure(self.root, structure)
        return first_chapter

    def test_timeline_flags_mounts_on_archived_anchors(self) -> None:
        """已归档锚点仍被挂载时必须给出信号（缺陷 8），而不是静默照旧解析。"""
        first_chapter = self._mount("TA-ARCH0001", status="archived")
        view = story_timeline(self.root)
        rows = [p for p in view["problems"] if p["code"] == "ARCHIVED_ANCHOR_REFERENCED"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chapterId"], first_chapter)
        self.assertEqual(rows[0]["anchorId"], "TA-ARCH0001")

    def test_timeline_accepts_mounts_on_active_anchors(self) -> None:
        self._mount("TA-LIVE0001", status="active")
        view = story_timeline(self.root)
        self.assertNotIn("ARCHIVED_ANCHOR_REFERENCED", [p["code"] for p in view["problems"]])


class TimelineMermaidTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root)
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": "TA-OPEN0001", "entityType": "time_anchor",
                                    "name": "转入昭华: 开篇", "absoluteTime": "深秋", "timeOrdinal": -45})
        register_entity(structure, {"entityId": "TA-LONE0002", "entityType": "time_anchor",
                                    "name": "无章节锚点", "absoluteTime": "冬季", "timeOrdinal": 0})
        for chapter in structure["chapters"]:
            chapter["storyTimeStartId"] = "TA-OPEN0001"
            chapter["storyTimeEndId"] = "TA-OPEN0001"
        save_structure(self.root, structure)

    def test_chapters_compress_into_ranges_and_colon_is_escaped(self) -> None:
        text = timeline_mermaid(story_timeline(self.root))
        self.assertTrue(text.startswith("timeline\n"))
        self.assertIn("title 测试书 · 故事时间线", text)
        self.assertIn("-45 · 转入昭华： 开篇 : 第01–02章", text)

    def test_anchor_without_chapters_renders_bare_label(self) -> None:
        text = timeline_mermaid(story_timeline(self.root))
        self.assertIn("    0 · 无章节锚点\n", text)

    def test_newline_in_a_label_cannot_break_the_timeline(self) -> None:
        """换行是 mermaid 的硬断点：一个含换行的名字会让整张图渲染失败。"""
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": "TA-BREAK001", "entityType": "time_anchor",
                                    "name": "第一行\n第二行", "absoluteTime": "某时"})
        save_structure(self.root, structure)
        text = timeline_mermaid(story_timeline(self.root))
        self.assertIn("    第一行 第二行\n", text)
        self.assertNotIn("第一行\n", text)


class GraphMermaidTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root)
        structure = load_structure(self.root)
        structure["references"].append({"sourceId": self.fixture["chapterIds"][0], "targetId": self.fixture["characterId"]})
        structure["relations"] = [
            {"relationId": "REL-PLAIN0001", "fromEntity": self.fixture["characterId"],
             "toEntity": self.fixture["propId"], "kind": "unspecified"},
            {"relationId": "REL-LABEL0002", "fromEntity": self.fixture["characterId"],
             "toEntity": self.fixture["locationId"], "kind": "住所"},
        ]
        save_structure(self.root, structure)

    def test_groups_by_entity_type_with_mention_counts_and_labels(self) -> None:
        text = graph_mermaid(entity_graph(self.root))
        self.assertTrue(text.startswith("flowchart LR\n"))
        self.assertIn('subgraph "人物"', text)
        self.assertIn('subgraph "道具"', text)
        self.assertIn('subgraph "地点"', text)
        self.assertIn('C_TEST_ALPHA["沈砚 ·1"]', text)
        self.assertIn("P_TEST_KEY[\"储物柜钥匙\"]", text)
        self.assertIn("C_TEST_ALPHA --> P_TEST_KEY", text)
        self.assertIn("C_TEST_ALPHA -->|住所| L_TEST_ROOM", text)

    def test_time_anchors_do_not_appear(self) -> None:
        structure = load_structure(self.root)
        register_entity(structure, {"entityId": "TA-HIDE0001", "entityType": "time_anchor", "name": "锚", "absoluteTime": "某时"})
        save_structure(self.root, structure)
        self.assertNotIn("TA_HIDE", graph_mermaid(entity_graph(self.root)))

    def test_newline_in_a_node_label_cannot_break_the_graph(self) -> None:
        structure = load_structure(self.root)
        entity = next(e for e in structure["entities"] if e["entityId"] == self.fixture["characterId"])
        entity["name"] = "沈\n砚"
        write_json(self.root/".gnovel/structure.json", structure)
        text = graph_mermaid(entity_graph(self.root))
        self.assertIn('C_TEST_ALPHA["沈 砚 ·1"]', text)

    def test_pipe_in_an_edge_label_is_escaped(self) -> None:
        """`|` 就是 `-->|label|` 的分隔符，留在标签里会把边拆坏。"""
        structure = load_structure(self.root)
        structure["relations"].append({"relationId": "REL-PIPE0003", "fromEntity": self.fixture["propId"],
                                      "toEntity": self.fixture["locationId"], "kind": "甲|乙"})
        save_structure(self.root, structure)
        text = graph_mermaid(entity_graph(self.root))
        self.assertIn("P_TEST_KEY -->|甲｜乙| L_TEST_ROOM", text)


class ViewCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "structure", *args,
                               "--project-root", str(self.root)], capture_output=True, text=True)

    def test_graph_mermaid_round_trip(self) -> None:
        result = self.cli("graph", "--format", "mermaid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["code"], "ENTITY_GRAPH_MERMAID")
        self.assertIn("flowchart LR", payload["mermaid"])
        plain = self.cli("graph")
        self.assertEqual(json.loads(plain.stdout)["code"], "ENTITY_GRAPH")

    def test_timeline_mermaid_writes_a_marked_view_file(self) -> None:
        result = self.cli("timeline", "--format", "mermaid", "--output", "工作台/时间线视图.md")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path = self.root / "工作台/时间线视图.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("阅读快照", text)
        self.assertIn("```mermaid", text)
        self.assertIn("timeline", text)
        again = self.cli("timeline", "--format", "mermaid", "--output", "工作台/时间线视图.md")
        self.assertEqual(json.loads(again.stdout)["output"]["written"], False, "内容未变不重写")

    def test_output_refuses_an_authored_file(self) -> None:
        path = self.root / "工作台/时间线视图.md"
        path.write_text("# 作者手写\n", encoding="utf-8")
        result = self.cli("timeline", "--format", "mermaid", "--output", "工作台/时间线视图.md")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "EXPORT_EXISTS")
        self.assertEqual(path.read_text(encoding="utf-8"), "# 作者手写\n")

    def test_json_format_does_not_silently_ignore_output(self) -> None:
        """`--format json --output X` 必须报错：静默忽略会让调用方以为文件已写出。"""
        for command, target in (("graph", "规划/图视图.md"), ("timeline", "工作台/时间线视图.md")):
            with self.subTest(command=command):
                result = self.cli(command, "--format", "json", "--output", target)
                payload = json.loads(result.stdout)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(payload["code"], "OUTPUT_REQUIRES_MERMAID")
                self.assertFalse((self.root / target).exists(), "不得创建文件")

    def test_anchor_preview_accepts_a_time_ordinal(self) -> None:
        result = self.cli("anchor-preview", "--name", "开篇", "--absolute-time", "深秋", "--time-ordinal", "-45")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["parameters"]["timeOrdinal"], -45)


if __name__ == "__main__":
    unittest.main()
