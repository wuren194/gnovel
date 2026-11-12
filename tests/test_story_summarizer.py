from __future__ import annotations

from scripts.core.project import current_schema_version

import json
import tempfile
import unittest
from pathlib import Path

from scripts import state_cache
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

    def test_prepare_excludes_quarantined_chapters_from_summary_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            structure = fixture['structure']
            structure['chapters'][0].update(status='quarantined', quarantineReason='待复核内容边界',
                                             quarantineCategory='content_boundary')
            from scripts.core.structure_store import save_structure
            save_structure(root, structure)
            state_cache.rebuild(root)
            ctx = _prepare_context(root)
            self.assertEqual(ctx['coveredChapterIds'], [fixture['chapterIds'][1]])
            self.assertEqual([row['chapterId'] for row in ctx['chapters']], [fixture['chapterIds'][1]])
            self.assertEqual(ctx['asOfChapterId'], fixture['chapterIds'][1])

    def test_quarantine_change_withholds_stale_summary_until_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            original = fake_summary(root, fixture)
            source = root / 'summary.json'
            source.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
            _commit_summary(root, str(source))
            structure = fixture['structure']
            structure['chapters'][0].update(status='quarantined', quarantineReason='隔离边界',
                                             quarantineCategory='content_boundary')
            from scripts.core.structure_store import save_structure
            save_structure(root, structure)
            state_cache.rebuild(root)
            target = fixture['chapterIds'][-1]
            context = build_continuation_context(root, target)
            self.assertNotIn('storySummary', context)
            prepared = _prepare_context(root)
            self.assertEqual(prepared['excludedChapterIds'], [fixture['chapterIds'][0]])
            self.assertTrue(prepared['refresh']['fullRefresh'])

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
