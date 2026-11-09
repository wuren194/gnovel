"""Display chapter codes resolve to stable IDs at the CLI/MCP boundary, never by guessing."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.chapter_refs import resolve_chapter_arguments, resolve_chapter_reference
from scripts.core.jsonio import write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError, load_structure
from tests.test_structure_commands import StructureCommandTests

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


def cli(*args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(NOVELCTL), *args], cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


class ChapterReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = StructureCommandTests()._project(self.root)
        write_json(self.root / ".govel/project.json", {"schemaVersion": 5, "workflowVersion": "5.0",
                                                         "projectId": "project-1", "bookName": "测试"})
        self.first, self.second = (row["chapterId"] for row in self.data["chapters"])

    def test_display_codes_resolve_case_insensitively_and_stable_ids_pass_through(self) -> None:
        for value, expected in (("CH001", self.first), ("ch2", self.second), ("CH0002", self.second),
                                (self.first, self.first), ("not-a-code", "not-a-code")):
            with self.subTest(value=value):
                self.assertEqual(resolve_chapter_reference(self.data, value), expected)

    def test_a_stable_id_that_looks_like_a_display_code_wins(self) -> None:
        data = load_structure(self.root)
        data["chapters"][1]["chapterId"] = "CH001"  # 旧项目的稳定 ID 可能就是 CH001
        self.assertEqual(resolve_chapter_reference(data, "CH001"), "CH001")

    def test_missing_ambiguous_and_archived_codes_fail_closed(self) -> None:
        with self.assertRaises(StructureError) as ctx:
            resolve_chapter_reference(self.data, "CH009")
        self.assertEqual(ctx.exception.code, "CHAPTER_REFERENCE_NOT_FOUND")
        self.assertEqual(ctx.exception.details["activeChapterCount"], 2)
        archived = load_structure(self.root)
        archived["chapters"][1]["status"] = "archived"
        archived["chapters"][1]["displayCode"] = "CH002"
        with self.assertRaises(StructureError) as ctx:
            resolve_chapter_reference(archived, "CH002")
        self.assertEqual(ctx.exception.details["archivedChapterIds"], [self.second])
        clash = load_structure(self.root)
        clash["chapters"][1]["displayOrdinal"] = 1
        with self.assertRaises(StructureError) as ctx:
            resolve_chapter_reference(clash, "CH001")
        self.assertEqual(ctx.exception.code, "CHAPTER_REFERENCE_AMBIGUOUS")

    def test_argument_lists_are_rewritten_and_reported(self) -> None:
        arguments = {"chapterIds": ["CH002", self.first], "chapterGoal": "CH001", "beforeChapterId": "CH001"}
        resolved = resolve_chapter_arguments(self.root, arguments)
        self.assertEqual(arguments, {"chapterIds": [self.second, self.first], "chapterGoal": "CH001",
                                     "beforeChapterId": self.first})
        self.assertEqual([row["reference"] for row in resolved], ["CH002", "CH001"])

    def test_no_structure_means_no_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            arguments = {"chapterId": "CH001"}
            self.assertEqual(resolve_chapter_arguments(Path(temp), arguments), [])
            self.assertEqual(arguments, {"chapterId": "CH001"})

    def test_cli_legacy_and_operation_entries_resolve_the_same_way(self) -> None:
        code, moved = cli("project", "structure", "move-preview", "--project-root", str(self.root),
                          "--chapter-id", "CH002", "--before-chapter-id", "CH001")
        self.assertEqual(code, 0, moved)
        self.assertEqual(moved["parameters"], {"chapterId": self.second, "beforeChapterId": self.first})
        self.assertEqual({row["field"] for row in moved["chapterReferences"]}, {"chapter_id", "before_chapter_id"})
        code, missing = cli("project", "workflow", "chapter-status", "--project-root", str(self.root), "--chapter-id", "CH404")
        self.assertEqual((code, missing["code"]), (1, "CHAPTER_REFERENCE_NOT_FOUND"))
        outline = call_operation("task.outline-check", self.root, {"chapterId": "CH001"})
        self.assertEqual(outline["chapterReferences"][0]["chapterId"], self.first)

    def test_preview_plans_store_only_stable_ids(self) -> None:
        code, moved = cli("project", "structure", "move-preview", "--project-root", str(self.root), "--chapter-id", "CH001")
        self.assertEqual(code, 0, moved)
        plan = json.loads((self.root / ".govel/plans" / f"{moved['operationId']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("CH001", json.dumps(plan["parameters"]))


if __name__ == "__main__":
    unittest.main()
