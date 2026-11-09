"""View adapter tests: placement/Unit normalization, bounded summary, honest impact."""

from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from scripts.core.view_adapters import chapter_view, plan_impact_view, project_summary
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project


class ChapterViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        self.fixture = create_schema5_project(self.root, chapter_count=2)
        self.structure = self.fixture["structure"]
        # Current placement is the only persisted structural parent.
        base = self.structure["chapters"][0]
        self.schema5_chapter = {"chapterId": base["chapterId"], "title": base["title"],
                                "displayCode": base["displayCode"], "displayOrdinal": base["displayOrdinal"],
                                "status": "draft", "placement": {"scope": "volume", "volumeId": base["placement"]["volumeId"]}}
        self.schema5_structure = {**self.structure, "schemaVersion": 5, "chapters": [self.schema5_chapter]}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema5_chapter_view_uses_placement_never_volume_id(self) -> None:
        view = chapter_view(self.schema5_structure, self.schema5_chapter)
        self.assertEqual(view["placement"]["scope"], "volume")
        self.assertEqual(view["derivedVolumeId"], self.schema5_chapter["placement"]["volumeId"])
        self.assertNotIn("volumeId", view)  # UI 层永不直接接触 legacy 字段
        self.assertEqual(view["chapterId"], self.schema5_chapter["chapterId"])
        self.assertEqual(view["displayOrdinal"], self.schema5_chapter["displayOrdinal"])

    def test_project_scope_chapter_has_null_volume(self) -> None:
        project_chapter = {**self.schema5_chapter, "placement": {"scope": "project"}}
        view = chapter_view(self.schema5_structure, project_chapter)
        self.assertIsNone(view["derivedVolumeId"])
        self.assertEqual(view["placement"], {"scope": "project"})

    def test_unit_membership_is_reported(self) -> None:
        structure = {**self.structure, "units": [{"unitId": "UNIT-1", "title": "序章组", "placement": {"scope": "project"},
                                                  "members": [{"memberType": "chapter", "memberId": self.structure["chapters"][0]["chapterId"], "order": 1}]}]}
        view = chapter_view(structure, structure["chapters"][0])
        self.assertEqual(view["unitMembership"], ["UNIT-1"])

    def test_unknown_placement_shape_fails_closed(self) -> None:
        broken = {**self.schema5_chapter, "placement": {"scope": "galaxy"}}
        with self.assertRaises(StructureError):
            chapter_view(self.schema5_structure, broken)


class BoundedSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        self.fixture = create_schema5_project(self.root, chapter_count=6)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_summary_is_bounded_and_cached_across_polls(self) -> None:
        first = project_summary(self.root)
        self.assertTrue(first["countsCache"]["bounded"])
        self.assertEqual(first["countsCache"]["filesReadThisCall"], 6)
        second = project_summary(self.root)
        self.assertEqual(second["countsCache"]["filesReadThisCall"], 0, "repeat poll must not re-read prose")
        self.assertEqual(first["counts"]["hanCharacters"], second["counts"]["hanCharacters"])
        # 修改一章后只重读一章
        chapter = self.fixture["structure"]["chapters"][0]
        path = self.root / chapter["filePath"]
        path.write_text("更新后的正文，字数不同。", encoding="utf-8")
        third = project_summary(self.root)
        self.assertEqual(third["countsCache"]["filesReadThisCall"], 1)

    def test_summary_surveys_all_chapters_with_view_fields(self) -> None:
        summary = project_summary(self.root)
        row = summary["chapters"][0]
        for field in ("chapterId", "title", "displayCode", "displayOrdinal", "status", "placement", "derivedVolumeId", "unitMembership", "arcLinks", "hanCharCount"):
            self.assertIn(field, row)

    def test_same_size_rewrite_with_preserved_mtime_recomputes_count(self):
        chapter = self.fixture['structure']['chapters'][0]
        path = self.root/chapter['filePath']
        path.write_text('甲乙丙')
        before = path.stat()
        first = project_summary(self.root)
        self.assertEqual(first['chapters'][0]['hanCharCount'], 3)
        path.write_text('abcdefghi')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed = project_summary(self.root)
        self.assertEqual(changed['chapters'][0]['hanCharCount'], 0)
        self.assertEqual(changed['countsCache']['filesReadThisCall'], 1)

    def test_same_size_path_replacement_is_not_the_cached_file_object(self):
        chapter = self.fixture['structure']['chapters'][0]
        path = self.root/chapter['filePath']; path.write_text('甲乙丙')
        before = path.stat(); project_summary(self.root)
        replacement = path.with_suffix('.replacement'); replacement.write_text('abcdefghi')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        changed = project_summary(self.root)
        self.assertEqual(changed['chapters'][0]['hanCharCount'], 0)
        self.assertEqual(changed['countsCache']['filesReadThisCall'], 1)

    def test_count_read_drift_reports_unknown_and_does_not_cache_mixed_version(self):
        from scripts.core import write_sets
        chapter = self.fixture['structure']['chapters'][0]
        path = (self.root/chapter['filePath']).resolve(); path.write_text('甲乙丙')
        observed = write_sets.guard_observation

        def drift(selected):
            state, raw = observed(selected)
            if selected == path:
                before = selected.stat()
                selected.write_text('abcdefghi')
                os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns))
            return state, raw

        with patch.object(write_sets, 'guard_observation', side_effect=drift):
            result = project_summary(self.root)
        self.assertIsNone(result['chapters'][0]['hanCharCount'])
        retry = project_summary(self.root)
        self.assertEqual(retry['chapters'][0]['hanCharCount'], 0)
        self.assertEqual(retry['countsCache']['filesReadThisCall'], 1)


class PlanImpactViewTests(unittest.TestCase):
    def test_empty_impact_is_unknown_never_zero(self) -> None:
        view = plan_impact_view({"operation": "canon_resolve", "impact": {}})
        self.assertFalse(view["known"])
        self.assertIsNone(view["renumberedChapters"])
        self.assertIn("尚未完整取得", view["note"])
        self.assertFalse(plan_impact_view({"operation": "x"})["known"])

    def test_real_impact_passes_through(self) -> None:
        view = plan_impact_view({"impact": {"renumberedChapters": [{"chapterId": "CH-B", "from": 2, "to": 3}],
                                            "referenceCount": 4, "staleCandidates": ["PC-1"]}})
        self.assertTrue(view["known"])
        self.assertEqual(view["renumberedChapters"][0]["chapterId"], "CH-B")
        self.assertEqual(view["staleCandidateCount"], 1)


if __name__ == "__main__":
    unittest.main()
