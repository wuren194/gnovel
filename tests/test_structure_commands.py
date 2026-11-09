from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import write_json
from scripts.core.structure_commands import commit_plan, preview_insert_chapter, undo_operation
from scripts.core.structure_store import empty_structure, load_structure, new_id, save_structure


class StructureCommandTests(unittest.TestCase):
    def _project(self, root: Path) -> dict:
        from scripts.novelctl import _initialize
        _initialize(root, project_id='project-1', book_name='测试', idempotency_key='test', approval_mode='manual')
        for ordinal in (1, 2):
            source = load_structure(root)
            preview = preview_insert_chapter(root, title=f'第{ordinal}章',
                volume_id=source['volumes'][0]['volumeId'], primary_arc_id=source['arcs'][0]['arcId'])
            commit_plan(root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)
        return load_structure(root)

    def test_preview_is_side_effect_free_and_commit_preserves_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = self._project(root)
            before = root.joinpath(".govel/structure.json").read_bytes()
            second_id = original["chapters"][1]["chapterId"]
            preview = preview_insert_chapter(
                root,
                title="插入章",
                volume_id=original["volumes"][0]["volumeId"],
                primary_arc_id=original["arcs"][0]["arcId"],
                before_chapter_id=second_id,
            )
            self.assertEqual(root.joinpath(".govel/structure.json").read_bytes(), before)
            self.assertEqual(preview["impact"]["renumberedChapters"][0]["chapterId"], second_id)
            result = commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            updated = load_structure(root)
            self.assertEqual([item["displayCode"] for item in updated["chapters"]], ["CH001", "CH002", "CH003"])
            self.assertEqual(next(item for item in updated["chapters"] if item["chapterId"] == second_id)["displayCode"], "CH003")
            self.assertEqual(result["chapter"]["displayCode"], "CH002")

    def test_plan_is_single_use_and_undo_restores_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = self._project(root)
            preview = preview_insert_chapter(root, title="插入章", volume_id=original["volumes"][0]["volumeId"], primary_arc_id=original["arcs"][0]["arcId"])
            result = commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            from scripts.core.structure_store import StructureError
            with self.assertRaises(StructureError) as caught:
                undo_operation(root, result['operationId'], binding_generation=load_structure(root)['chapters'][-1]['proseBinding']['generation'])
            self.assertEqual(caught.exception.code, 'UNDO_UNSUPPORTED_OPERATION')
            self.assertEqual(len(load_structure(root)['chapters']), 3)
    def test_commit_failure_restores_structure_indexes_and_outline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            from scripts.core import write_sets
            root = Path(temp)
            original = self._project(root)
            watched = {
                ".govel/structure.json": (root / ".govel/structure.json").read_bytes(),
            }
            preview = preview_insert_chapter(
                root,
                title="失败章",
                volume_id=original["volumes"][0]["volumeId"],
                primary_arc_id=original["arcs"][0]["arcId"],
            )

            def fail(phase, relative=None):
                if phase == "write" and relative and relative == ".govel/structure.json":
                    raise RuntimeError("injected")

            with patch.object(write_sets, "checkpoint", side_effect=fail):
                with self.assertRaises(RuntimeError):
                    commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
            for relative, before in watched.items():
                self.assertEqual((root / relative).read_bytes(), before, relative)
            self.assertEqual(list((root / "规划").glob("CH-*-推演.md")), [])
            self.assertFalse(list((root / ".govel/transactions").glob("*.json")), "失败事务必须清理")
            # 同一份计划必须能原样重试成功（备份残留会在这里暴露 FileExistsError）
            self.assertEqual(commit_plan(root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)["code"], "STRUCTURE_COMMITTED")


if __name__ == "__main__":
    unittest.main()
