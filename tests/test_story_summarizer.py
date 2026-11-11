from __future__ import annotations

from scripts.core.project import current_schema_version

import json
import tempfile
import unittest
from pathlib import Path

from scripts.continuation_context import build_continuation_context
from scripts.story_summarizer import StorySummarizerError, _commit_summary, _prepare_context
from tests.fixtures_current import create_current_project


def fake_summary(root: Path, fixture: dict) -> dict:
    structure = fixture["structure"]
    return {
        "schemaVersion": current_schema_version(),
        "projectId": structure["projectId"],
        "structureRevision": structure["revision"],
        "coveredChapterIds": fixture["chapterIds"],
        "asOfChapterId": fixture["chapterIds"][-1],
        "asOfDisplayOrdinal": len(fixture["chapterIds"]),
        "sourceCommitId": None,
        "storySummary": "沈砚与秘密钥匙发生联系。",
        "plotLine": [{"chapterId": fixture["chapterIds"][0], "title": "第一章", "summary": "钥匙出现。"}],
        "characterSummaries": [{"characterId": fixture["characterId"], "summary": "保持戒备。"}],
    }


class StorySummarizerTests(unittest.TestCase):
    def test_prepare_uses_stable_ids_and_structure_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            ctx = _prepare_context(root)
            self.assertEqual(ctx["committedChapters"], 2)
            self.assertEqual(ctx["chapters"][0]["chapterId"], fixture["chapterIds"][0])
            self.assertEqual(ctx["structureRevision"], fixture["structure"]["revision"])
            self.assertEqual(ctx["coveredChapterIds"], fixture["chapterIds"])

    def test_commit_and_context_injection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            src = root / "summary.json"
            src.write_text(json.dumps(fake_summary(root, fixture), ensure_ascii=False), encoding="utf-8")
            report = _commit_summary(root, str(src))
            self.assertTrue(report["ok"])
            ctx = build_continuation_context(root, fixture["chapterIds"][-1])
            self.assertIn("storySummary", ctx)
            self.assertFalse(ctx["summaryFreshness"]["stale"])

    def test_commit_rejects_wrong_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            summary = fake_summary(root, fixture)
            summary["structureRevision"] = 99
            src = root / "bad.json"
            src.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(StorySummarizerError):
                _commit_summary(root, str(src))

    def test_missing_or_corrupt_summary_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            ctx = build_continuation_context(root, fixture["chapterIds"][0])
            self.assertNotIn("storySummary", ctx)
            (root / ".govel/aux/story-summary.json").write_text("bad", encoding="utf-8")
            ctx = build_continuation_context(root, fixture["chapterIds"][0])
            self.assertNotIn("storySummary", ctx)


if __name__ == "__main__":
    unittest.main()
