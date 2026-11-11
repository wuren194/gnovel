from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifacts import sha256_text
from scripts.core.prompt_plan import PromptPlanError, _read_ranges, resolve_prompt_plan
from scripts.core.structure_store import load_structure, save_structure
from tests.fixtures_current import create_current_project

ROOT = Path(__file__).resolve().parents[1]


class PromptViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_current_project(self.root, chapter_count=1)
        self.chapter_id = self.fixture["chapterIds"][0]

    def plan(self, station="govel-planner", **kwargs):
        return resolve_prompt_plan(self.root, station, chapter_id=self.chapter_id, **kwargs)

    def test_station_plan_does_not_expand_reference_trees(self):
        library = self.root / 'library'
        library.mkdir()
        (library / 'manifest.json').write_text('{}')
        for number in range(20):
            (library / f'note-{number}.md').write_text('普通参考资料。')
        manifest = {'promptPlan': {'govel-planner': {'always': [], 'conditional': []}},
                    'onDemandReferences': {'govel-planner': ['library']}}
        with patch('scripts.core.prompt_plan.TOOLBOX_ROOT', self.root), \
             patch('scripts.core.prompt_plan.toolbox_manifest', return_value=manifest):
            station_plan = resolve_prompt_plan(self.root, 'govel-planner', design={}, view='station')
            full = resolve_prompt_plan(self.root, 'govel-planner', design={}, view='full')
            self.assertFalse(any("files" in row for row in station_plan["onDemandReferences"]))
            reference = station_plan['onDemandReferences'][0]
            self.assertEqual(reference['entrypoints'][0]['path'], 'library/manifest.json')
            self.assertLess(len(json.dumps(station_plan)), len(json.dumps(full)) * 0.7)

    def test_selected_ranges_are_exact_and_content_addressed(self):
        plan = self.plan("govel-titler", view="station", decision_type="title")
        total = 0
        for contract in plan["contracts"]:
            lines = (ROOT / contract["path"]).read_text(encoding="utf-8").splitlines(keepends=True)
            for span in contract["readRanges"]:
                text = "".join(lines[span["startLine"] - 1:span["endLine"]])
                self.assertEqual(sha256_text(text), span["sha256"])
                total += len(text)
        self.assertEqual(total, plan["characterCounts"]["contractText"])
        self.assertLess(total, len((ROOT / "shared/contracts/G-MULTI-IDENTITY-REVIEW.md").read_text()) / 2)
        self.assertEqual(plan["characterCounts"]["response"], len(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2)))
        self.assertEqual(plan, self.plan("govel-titler", view="station", decision_type="title"))

    def test_heavy_chapter_metadata_reaches_review_compiler(self):
        structure = load_structure(self.root)
        structure["chapters"][0].update(heavyPlot=True, volumeEnd=True, newLocation=True)
        save_structure(self.root, structure)
        plan = self.plan(decision_type="chapter", review_class="ordinary")
        self.assertEqual(plan["reviewPlan"]["activeSeats"], list(range(1, 11)))

    def test_unknown_chapter_class_does_not_invent_absence_reasons(self):
        plan = self.plan(decision_type="chapter")
        self.assertEqual(plan["reviewPlan"]["activeSeats"], list(range(1, 11)))
        self.assertFalse(plan["reviewPlan"]["inactiveReasons"])
        ordinary = self.plan(decision_type="chapter", review_class="ordinary")
        self.assertEqual(ordinary["reviewPlan"]["activeSeats"], [1, 2, 4, 5, 6, 7, 10])

    def test_plan_does_not_publish_stale_cache(self):
        cache = self.root / ".govel/cache/story-state.json"
        cache.write_text("{}", encoding="utf-8")
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.plan(view="station")
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_unrelated_research_does_not_change_station_plan(self):
        library = self.root / "library"
        library.mkdir()
        (library / "manifest.json").write_text("{}", encoding="utf-8")
        (library / "research.md").write_text("old", encoding="utf-8")
        manifest = {"promptPlan": {"example": {"always": [], "conditional": []}}, "onDemandReferences": {"example": ["library"]}}
        with patch("scripts.core.prompt_plan.TOOLBOX_ROOT", self.root), patch("scripts.core.prompt_plan.toolbox_manifest", return_value=manifest):
            before = resolve_prompt_plan(self.root, "example", design={}, view="station")
            audit = resolve_prompt_plan(self.root, "example", design={}, view="full")
            (library / "research.md").write_text("new", encoding="utf-8")
            self.assertEqual(before, resolve_prompt_plan(self.root, "example", design={}, view="station"))
            self.assertNotEqual(audit["planHash"], resolve_prompt_plan(self.root, "example", design={}, view="full")["planHash"])

    def test_missing_or_overlapping_slice_fails_closed(self):
        source = self.root / "contract.md"
        source.write_text("<!-- prompt:a -->\nrule\n<!-- /prompt:a -->\n", encoding="utf-8")
        for selectors in (["missing"], ["a", "a"], []):
            with self.subTest(selectors=selectors), self.assertRaises(PromptPlanError):
                _read_ranges(source, selectors)


if __name__ == "__main__":
    unittest.main()
