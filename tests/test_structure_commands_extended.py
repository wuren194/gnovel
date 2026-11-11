from __future__ import annotations

from scripts.core.project import current_schema_version

import tempfile
import unittest
from pathlib import Path

from scripts.core.commands import list_foreshadowing, project_overview, story_timeline
from scripts.core.entity_registry import register_entity
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_commands import (
    commit_plan, gc_project, list_operations, list_pending_plans,
    preview_archive_chapter, preview_create_time_anchor, preview_move_chapter,
    preview_set_chapter_timespan,
)
from scripts.core.structure_store import StructureError, load_structure, new_id, save_structure
from tests.test_structure_commands import StructureCommandTests


class ExtendedStructureCommandTests(unittest.TestCase):
    def _project(self, root: Path) -> dict:
        helper = StructureCommandTests()
        data = helper._project(root)
        register_entity(data, {"entityId": "prop-LAMP", "entityType": "prop", "name": "旧灯", "aliases": ["台灯"]})
        data["references"].append({"referenceId": "relation-REF1", "sourceId": data["chapters"][0]["chapterId"], "targetId": "prop-LAMP", "kind": "mentions"})
        save_structure(root, data)
        return data

    def test_move_preview_preserves_ids_and_commit_rebuilds_cache_for_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self._project(root)
            write_json(root / ".govel/project.json", {"schemaVersion": current_schema_version(), "workflowVersion": '6.0', "projectId": "project-1", "bookName": "测试"})
            first, second = data["chapters"]
            preview = preview_move_chapter(root, chapter_id=first["chapterId"], before_chapter_id=None)
            result = commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            updated = load_structure(root)
            self.assertEqual({c["chapterId"] for c in updated["chapters"]}, {first["chapterId"], second["chapterId"]})
            self.assertEqual(updated["chapters"][-1]["chapterId"], first["chapterId"])
            self.assertEqual(result["code"], "STRUCTURE_COMMITTED")



class ArchiveChapterTests(unittest.TestCase):
    def test_archive_renumbers_following_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            first, second = data["chapters"]
            preview = preview_archive_chapter(root, chapter_id=first["chapterId"], reason="重写第一章")
            self.assertEqual(preview["impact"]["renumberedChapters"][0]["chapterId"], second["chapterId"])
            commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            updated = load_structure(root)
            archived = next(item for item in updated["chapters"] if item["chapterId"] == first["chapterId"])
            active = next(item for item in updated["chapters"] if item["chapterId"] == second["chapterId"])
            self.assertEqual(archived["status"], "archived")
            self.assertEqual(active["displayCode"], "CH001")

    def test_archive_twice_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            first = data["chapters"][0]
            preview = preview_archive_chapter(root, chapter_id=first["chapterId"])
            commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            with self.assertRaises(StructureError) as ctx:
                preview_archive_chapter(root, chapter_id=first["chapterId"])
            self.assertEqual(ctx.exception.code, "CHAPTER_ALREADY_ARCHIVED")




class PlanHousekeepingTests(unittest.TestCase):
    def test_pending_plans_and_gc(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            preview = preview_archive_chapter(root, chapter_id=data["chapters"][0]["chapterId"])
            plans = list_pending_plans(root)
            self.assertEqual([item["operationId"] for item in plans], [preview["operationId"]])
            self.assertFalse(plans[0]["expired"])
            plan_path = root / ".govel/plans" / f"{preview['operationId']}.json"
            plan = read_json(plan_path)
            plan["expiresAtEpoch"] = 1
            write_json(plan_path, plan)
            report = gc_project(root)
            self.assertEqual(report["removedPlans"], [preview["operationId"]])
            self.assertEqual(list_pending_plans(root), [])

    def test_operations_carry_undoable_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            first, second = data["chapters"]
            p1 = preview_archive_chapter(root, chapter_id=first["chapterId"])
            commit_plan(root, operation_id=p1["operationId"], plan_hash=p1["planHash"], approved=True)
            p2 = preview_archive_chapter(root, chapter_id=second["chapterId"])
            commit_plan(root, operation_id=p2["operationId"], plan_hash=p2["planHash"], approved=True)
            operations = list_operations(root)
            flags = {row["operationId"]: row["undoable"] for row in operations}
            self.assertTrue(flags[p2["operationId"]])
            self.assertFalse(flags[p1["operationId"]])


class OverviewEnrichmentTests(unittest.TestCase):
    def test_overview_reports_han_counts_and_length_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            first = data["chapters"][0]
            first["status"] = "draft"
            save_structure(root, data)
            prose = root / first["filePath"]
            prose.parent.mkdir(parents=True, exist_ok=True)
            prose.write_text("汉" * 100, encoding="utf-8")
            overview = project_overview(root)
            row = next(item for item in overview["chapters"] if item["chapterId"] == first["chapterId"])
            self.assertEqual(row["hanCharCount"], 100)
            self.assertEqual(row["lengthStatus"], "short")
            self.assertEqual(overview["chapterLength"]["minHanCharacters"], 8500)

    def test_foreshadowing_view_lists_unresolved_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            register_entity(data, {"entityId": "hook-SEED0001", "entityType": "hook", "name": "断簪", "status": "active"})
            register_entity(data, {"entityId": "hook-DONE0001", "entityType": "hook", "name": "旧信", "status": "archived"})
            data["references"].append({"referenceId": "REF-F1", "sourceId": data["chapters"][0]["chapterId"], "targetId": "hook-SEED0001", "kind": "plants"})
            save_structure(root, data)
            report = list_foreshadowing(root)
            self.assertEqual(report["unresolvedCount"], 1)
            first_row = report["foreshadowing"][0]
            self.assertEqual(first_row["entityId"], "hook-SEED0001")
            self.assertEqual(first_row["mentionChapters"][0]["chapterId"], data["chapters"][0]["chapterId"])


class StoryTimelineTests(unittest.TestCase):
    def _project_with_anchors(self, root: Path) -> dict:
        data = StructureCommandTests()._project(root)
        register_entity(data, {"entityId": "time-SPRING01", "entityType": "time_anchor", "name": "三年前的春天", "absoluteTime": "0844-03-01"})
        register_entity(data, {"entityId": "time-NYE00001", "entityType": "time_anchor", "name": "除夕夜", "absoluteTime": "0847-02-17 23:00"})
        save_structure(root, data)
        return data

    def test_timespan_preview_and_timeline_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self._project_with_anchors(root)
            first, second = data["chapters"]
            p = preview_set_chapter_timespan(root, chapter_id=first["chapterId"], start_anchor_id="time-SPRING01")
            commit_plan(root, operation_id=p["operationId"], plan_hash=p["planHash"], approved=True)
            p = preview_set_chapter_timespan(root, chapter_id=second["chapterId"], start_anchor_id="time-NYE00001")
            commit_plan(root, operation_id=p["operationId"], plan_hash=p["planHash"], approved=True)
            view = story_timeline(root)
            self.assertEqual([a["entityId"] for a in view["anchors"]], ["time-SPRING01", "time-NYE00001"])
            self.assertEqual(view["problems"], [])
            self.assertEqual(view["unmappedChapterCount"], 0)
            rows = {row["chapterId"]: row for row in view["chapters"]}
            self.assertEqual(rows[first["chapterId"]]["startIndex"], 0)
            self.assertEqual(rows[second["chapterId"]]["startIndex"], 1)

    def test_timeline_flags_inversion_and_unknown_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self._project_with_anchors(root)
            first, second = data["chapters"]
            first["storyTimeStartId"] = "time-NYE00001"
            first["storyTimeEndId"] = "time-NYE00001"
            second["storyTimeStartId"] = "time-SPRING01"
            second["storyTimeEndId"] = "time-GHOST001"
            with self.assertRaises(StructureError) as caught:
                save_structure(root, data)
            self.assertIn('SOURCE_TIME_MOUNT_INVALID',
                          {row['code'] for row in caught.exception.details['errors']})
            # The diagnostic projection can still explain raw damaged input;
            # persistence no longer accepts the dangling endpoint.
            view = story_timeline(root, structure=data)
            codes = {p["code"] for p in view["problems"]}
            self.assertIn("TIME_DISPLAY_ORDER_INVERSION", codes)
            self.assertIn("UNKNOWN_TIME_ANCHOR", codes)

    def test_anchors_sorted_by_absolute_time_not_list_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            register_entity(data, {"entityId": "time-LATER001", "entityType": "time_anchor", "name": "后来", "absoluteTime": "0850-01-01"})
            register_entity(data, {"entityId": "time-EARLY001", "entityType": "time_anchor", "name": "很早以前", "absoluteTime": "0840-06-01"})
            save_structure(root, data)
            view = story_timeline(root)
            self.assertEqual([a["entityId"] for a in view["anchors"]], ["time-EARLY001", "time-LATER001"])
            self.assertEqual(view["anchors"][0]["absoluteTime"], "0840-06-01")

    def test_anchor_without_absolute_time_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = StructureCommandTests()._project(root)
            register_entity(data, {"entityId": "time-VAGUE001", "entityType": "time_anchor", "name": "某天"})
            save_structure(root, data)
            view = story_timeline(root)
            self.assertIn("ANCHOR_MISSING_ABSOLUTE_TIME", {p["code"] for p in view["problems"]})

    def test_create_anchor_requires_absolute_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            StructureCommandTests()._project(root)
            with self.assertRaises(StructureError) as ctx:
                preview_create_time_anchor(root, name="某夜", absolute_time="  ")
            self.assertEqual(ctx.exception.code, "ABSOLUTE_TIME_REQUIRED")
            p = preview_create_time_anchor(root, name="除夕夜", absolute_time="0847-02-17 23:00", summary="夺印之夜")
            commit_plan(root, operation_id=p["operationId"], plan_hash=p["planHash"], approved=True)
            anchors = [e for e in load_structure(root)["entities"] if e["entityType"] == "time_anchor"]
            self.assertEqual(anchors[0]["absoluteTime"], "0847-02-17 23:00")

    def test_registered_entity_times_render_as_points_and_spans(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self._project_with_anchors(root)
            register_entity(data, {'entityId': 'character-HERO0001', 'entityType': 'character', 'name': '沈决',
                'timeStartId': 'time-SPRING01', 'timeEndId': 'time-NYE00001'})
            register_entity(data, {'entityId': 'hook-PIN0001', 'entityType': 'hook', 'name': '断簪',
                'timeStartId': 'time-SPRING01', 'timeEndId': 'time-SPRING01', 'status': 'active'})
            save_structure(root, data)
            tracks = story_timeline(root)['tracks']
            self.assertFalse(tracks['character'][0]['isPoint'])
            self.assertTrue(tracks['hook'][0]['isPoint'])

    def test_timespan_preview_rejects_unknown_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self._project_with_anchors(root)
            with self.assertRaises(StructureError) as ctx:
                preview_set_chapter_timespan(root, chapter_id=data["chapters"][0]["chapterId"], start_anchor_id="time-NOPE0001")
            self.assertEqual(ctx.exception.code, "TIME_ANCHOR_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
