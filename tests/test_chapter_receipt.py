from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.jsonio import read_json, write_json
from tests.fixtures_current import create_current_project


class ChapterReceiptTests(unittest.TestCase):
    def test_project_receipt_lists_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root, chapter_count=2)
            receipt = chapter_receipt(root)
            self.assertEqual(receipt["projectId"], fixture["structure"]["projectId"])
            self.assertEqual(len(receipt["chapters"]), 2)

    def test_missing_artifacts_are_reported_as_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root, chapter_count=1)
            structure = read_json(root / ".govel/structure.json")
            structure["chapters"][0]["status"] = "review"
            write_json(root / ".govel/structure.json", structure)
            receipt = chapter_receipt(root, fixture["chapterIds"][0])
            codes = {item["code"] for item in receipt["blockers"]}
            self.assertIn("FINAL_ARTIFACTS_MISSING", codes)
            self.assertIn("CHAPTER_OUTSIDE_APPROVED_RANGE", codes)
            self.assertFalse(receipt["readyToFinalize"])

    def test_unknown_chapter_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            create_current_project(root, chapter_count=1)
            with self.assertRaises(ValueError):
                chapter_receipt(root, "chapter-GHOST")


if __name__ == "__main__":
    unittest.main()
