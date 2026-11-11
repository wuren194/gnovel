from __future__ import annotations

from scripts.core.project import current_schema_version

import tempfile
import uuid
import unittest
from pathlib import Path

from scripts import state_cache
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_commands import commit_plan, preview_insert_chapter
from scripts.core.structure_store import new_id, save_structure

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


class StrictCacheTests(unittest.TestCase):
    def _project(self, root: Path) -> dict:
        from tests.fixtures_current import create_current_project
        return create_current_project(root, chapter_count=2)['structure']

    def test_new_structure_builds_current_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = self._project(root)
            state, design = state_cache.load(root)
            self.assertEqual(state["schemaVersion"], current_schema_version())
            self.assertEqual(state["chapters"][0]["chapterId"], structure["chapters"][0]["chapterId"])
            self.assertEqual(state["chapters"][0]["displayCode"], "CH001")
            self.assertEqual(design["schemaVersion"], current_schema_version())

    def test_structure_edit_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = self._project(root)
            self.assertTrue(state_cache.is_fresh(root))
            structure["bookName"] = "修改后"
            save_structure(root, structure)
            self.assertFalse(state_cache.is_fresh(root))
            state_cache.load(root)
            self.assertTrue(state_cache.is_fresh(root))

    def test_old_project_is_rejected_with_import_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".govel").mkdir()
            write_json(root / ".govel/project.json", {"schemaVersion": 4, "workflowVersion": "4.0"})
            with self.assertRaises(state_cache.StateCacheError) as caught:
                state_cache.load(root)
            self.assertIn("外部小说", str(caught.exception))

    def test_unregistered_reading_snapshot_does_not_invalidate_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._project(root)
            self.assertTrue(state_cache.is_fresh(root))
            snapshot = root / "设定" / "实体索引.md"
            snapshot.write_text("作者自行保存的阅读快照\n", encoding="utf-8")
            self.assertTrue(state_cache.is_fresh(root))

    def test_insert_changes_display_code_not_existing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = self._project(root)
            second_id = structure['chapters'][1]['chapterId']
            preview = preview_insert_chapter(root, title="中间章", volume_id=structure["volumes"][0]["volumeId"], primary_arc_id=structure["arcs"][0]["arcId"], before_chapter_id=second_id)
            commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            updated = read_json(root / ".govel/structure.json")
            self.assertEqual(next(item for item in updated["chapters"] if item["chapterId"] == second_id)["displayCode"], "CH003")
            self.assertEqual(next(item for item in updated["chapters"] if item["title"] == "中间章")["displayCode"], "CH002")


if __name__ == "__main__":
    unittest.main()
