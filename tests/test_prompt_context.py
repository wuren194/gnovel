from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.project import toolbox_manifest
from scripts.core.prompt_plan import PromptPlanError, resolve_prompt_plan
from scripts.core.structure_store import save_structure
from tests.fixtures_current import create_schema5_project

ROOT = Path(__file__).resolve().parents[1]


class ChapterPromptContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_schema5_project(self.root, chapter_count=1)
        self.chapter_id = self.fixture["chapterIds"][0]
        self.entry = toolbox_manifest()["promptPlan"]["govel-editor"]["conditional"][0]

    def test_real_project_can_match_chapter_condition(self):
        structure = self.fixture["structure"]
        structure["arcs"][0]["tags"] = [self.entry["when"]["value"]]
        save_structure(self.root, structure)
        plan = resolve_prompt_plan(self.root, "govel-editor", chapter_id=self.chapter_id)
        self.assertTrue(plan["complete"])
        self.assertTrue(plan["conditional"][0]["matched"])
        self.assertIn(self.entry["contract"], [Path(row["path"]).name for row in plan["contracts"]])
        self.assertEqual(plan["chapterId"], self.chapter_id)

    def test_explicit_chapter_can_exclude_condition(self):
        plan = resolve_prompt_plan(self.root, "govel-editor", chapter_id=self.chapter_id)
        self.assertTrue(plan["complete"])
        self.assertFalse(plan["conditional"][0]["matched"])
        self.assertEqual(plan["conditional"][0]["evidence"]["status"], "not_matched")

    def test_missing_chapter_is_unresolved_not_not_applicable(self):
        plan = resolve_prompt_plan(self.root, "govel-editor")
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["warnings"][0]["code"], "PROMPT_CHAPTER_REQUIRED")
        self.assertEqual(plan["conditional"][0]["evidence"]["status"], "unresolved")

    def test_unknown_chapter_fails_closed(self):
        with self.assertRaises(PromptPlanError) as caught:
            resolve_prompt_plan(self.root, "govel-editor", chapter_id="CH-NOTFOUND")
        self.assertEqual(caught.exception.code, "CHAPTER_NOT_FOUND")

    def test_cli_accepts_chapter_id(self):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "prompt-plan", "--project-root", str(self.root), "--station", "govel-editor", "--chapter-id", self.chapter_id], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["chapterId"], self.chapter_id)

    def test_composer_template_is_hashed(self):
        plan = resolve_prompt_plan(self.root, "govel-composer")
        row = next(row for row in plan["onDemandReferences"] if row["path"].endswith("design-candidate-template.md"))
        self.assertEqual(len(row["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
