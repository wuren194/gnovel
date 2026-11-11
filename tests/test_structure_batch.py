"""Batch structure plans: one preview, one commit, one undo, no extra authority."""
from __future__ import annotations

from scripts.core.project import current_schema_version

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.project import structure_batch_max_steps, workflow_policy
from scripts.core.structure_batch import preview_structure_batch
from scripts.core.structure_commands import commit_plan, list_operations, undo_operation
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_source import source_fixture
from tests.test_structure_commands import StructureCommandTests

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


def cli(*args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(NOVELCTL), *args], cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def order(root: Path) -> list[str]:
    data = load_structure(root)
    return [row["chapterId"] for row in sorted((c for c in data["chapters"] if c.get("status") != "archived"),
                                                key=lambda c: c["displayOrdinal"])]


class StructureBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        StructureCommandTests()._project(self.root)
        write_json(self.root / ".govel/project.json", {"schemaVersion": current_schema_version(), "workflowVersion": '6.0',
                                                         "projectId": "project-1", "bookName": "测试"})
        data = load_structure(self.root)
        from scripts.core.structure_commands import preview_insert_chapter
        for ordinal in (3, 4):
            preview = preview_insert_chapter(self.root, title=f'第{ordinal}章',
                volume_id=data['volumes'][0]['volumeId'], primary_arc_id=data['arcs'][0]['arcId'])
            commit_plan(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)
        self.ids = order(self.root)

    def test_reverse_order_is_one_plan_one_commit_one_undo(self) -> None:
        before = read_json(self.root / ".govel/structure.json")
        steps = [{"op": "move-chapter", "chapterId": code, "beforeChapterId": "CH001"}
                 for code in ("CH004", "CH003", "CH002")]
        preview = preview_structure_batch(self.root, steps=steps)
        # 预览不写真源；显示编号按预览前结构解析成稳定 ID
        self.assertEqual(read_json(self.root / ".govel/structure.json"), before)
        self.assertEqual([row["chapterId"] for row in preview["parameters"]["steps"]], self.ids[:0:-1])
        self.assertEqual({row["beforeChapterId"] for row in preview["parameters"]["steps"]}, {self.ids[0]})
        self.assertEqual(preview["batch"]["stepCounts"], {"move-chapter": 3})
        self.assertEqual(preview["batch"]["renumberedCount"], 4)
        result = commit_plan(self.root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
        self.assertEqual(result["code"], "STRUCTURE_COMMITTED")
        self.assertEqual(order(self.root), list(reversed(self.ids)))
        self.assertEqual(len([row for row in list_operations(self.root) if row["operation"] == "structure_batch"]), 1)
        undo_operation(self.root, preview["operationId"])
        self.assertEqual(order(self.root), self.ids)

    def test_archive_and_create_entity_share_one_snapshot(self) -> None:
        preview = preview_structure_batch(self.root, steps=[
            {"op": "archive-chapter", "chapterId": "CH001", "reason": "合并进第二章"},
            # 归档后旧 CH002 变成 CH001，但步骤仍按预览前编号解析
            {"op": "move-chapter", "chapterId": "CH002"},
            {"op": "create-entity", "entityType": "location", "name": "藏书楼", "aliases": ["书库"]},
        ])
        self.assertEqual(preview["batch"]["archivedChapterIds"], [self.ids[0]])
        self.assertEqual(preview["batch"]["createdEntities"][0]["name"], "藏书楼")
        commit_plan(self.root, operation_id=preview["operationId"], plan_hash=preview["planHash"], approved=True)
        self.assertEqual(order(self.root), [self.ids[2], self.ids[3], self.ids[1]])
        names = {row["name"] for row in load_structure(self.root)["entities"]}
        self.assertIn("藏书楼", names)

    def test_failed_step_reports_its_index_and_writes_no_plan(self) -> None:
        plans = self.root / ".govel/plans"
        before = sorted(plans.glob("*.json")) if plans.is_dir() else []
        for steps, step_code in (
            ([{"op": "move-chapter", "chapterId": "CH002", "beforeChapterId": "CH001"},
              {"op": "archive-chapter", "chapterId": "CH099"}], "CHAPTER_REFERENCE_NOT_FOUND"),
            ([{"op": "archive-chapter", "chapterId": "CH001"},
              {"op": "archive-chapter", "chapterId": "CH001"}], "CHAPTER_ALREADY_ARCHIVED"),
            ([{"op": "update-entity", "selector": "不存在的实体", "changes": {"name": "x"}}], None),
        ):
            with self.subTest(step_code=step_code):
                with self.assertRaises(StructureError) as ctx:
                    preview_structure_batch(self.root, steps=steps)
                self.assertEqual(ctx.exception.code, "STRUCTURE_BATCH_STEP_FAILED")
                self.assertEqual(ctx.exception.details["stepIndex"], len(steps) - 1)
                if step_code:
                    self.assertEqual(ctx.exception.details["stepCode"], step_code)
        after = sorted(plans.glob("*.json")) if plans.is_dir() else []
        self.assertEqual(after, before)

    def test_noop_and_step_limit_are_rejected(self) -> None:
        with self.assertRaises(StructureError) as ctx:
            preview_structure_batch(self.root, steps=[{"op": "move-chapter", "chapterId": "CH001", "beforeChapterId": "CH002"}])
        self.assertEqual(ctx.exception.code, "STRUCTURE_BATCH_NOOP")
        policy = deepcopy(workflow_policy())
        policy["maintenance"]["structureBatchMaxSteps"] = 1
        with patch("scripts.core.project.workflow_policy", return_value=policy):
            self.assertEqual(structure_batch_max_steps(), 1)
            with self.assertRaises(StructureError) as ctx:
                preview_structure_batch(self.root, steps=[{"op": "move-chapter", "chapterId": "CH002"}] * 2)
        self.assertEqual(ctx.exception.code, "STRUCTURE_BATCH_INVALID")
        self.assertEqual(ctx.exception.details["maxSteps"], 1)

    def test_cli_accepts_steps_file_and_commits_through_structure_commit(self) -> None:
        steps = self.root / "工作台" / "batch.json"
        steps.parent.mkdir(exist_ok=True)
        steps.write_text(json.dumps([{"op": "move-chapter", "chapterId": "CH004", "beforeChapterId": "CH001"}]), encoding="utf-8")
        code, preview = cli("project", "structure", "batch-preview", "--project-root", str(self.root), "--steps", f"@{steps}")
        self.assertEqual(code, 0, preview)
        code, result = cli("project", "structure", "commit", "--project-root", str(self.root),
                           "--operation-id", preview["operationId"], "--plan-hash", preview["planHash"], "--approve")
        self.assertEqual(code, 0, result)
        self.assertEqual(order(self.root)[0], self.ids[3])
        code, bad = cli("project", "structure", "batch-preview", "--project-root", str(self.root),
                        "--steps", json.dumps([{"op": "rename-everything"}]))
        self.assertEqual(code, 1)
        self.assertEqual(bad["code"], "ARGUMENTS_INVALID")


class SingleSourceBatchTests(unittest.TestCase):
    def test_batch_cannot_edit_existing_canon_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_fixture(root)
            with self.assertRaises(StructureError) as ctx:
                preview_structure_batch(root, steps=[{"op": "update-entity", "selector": "character-ALPHA", "changes": {"name": "新名"}}])
            self.assertEqual(ctx.exception.details["stepCode"], "STRUCTURE_BATCH_OPERATION_INVALID")


if __name__ == "__main__":
    unittest.main()
