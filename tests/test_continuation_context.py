from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.continuation_context import ContinuationContextError, build_continuation_context
from scripts.core.context_builder import _entities, _plot_line, _relations
from scripts.core.structure_store import load_structure, save_structure
from tests.fixtures_current import create_current_project


class ContinuationContextTests(unittest.TestCase):
    def test_context_uses_stable_ids_and_arc_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            ctx = build_continuation_context(root, fixture["chapterIds"][0])
            self.assertEqual(ctx["targetChapter"], fixture["chapterIds"][0])
            self.assertEqual(ctx["targetDisplayCode"], "CH001")
            self.assertEqual(ctx["targetVolumeId"], fixture["structure"]["volumes"][0]["volumeId"])
            self.assertEqual(ctx["targetArcLinks"][0]["role"], "primary")
            self.assertEqual(ctx["plotLine"][0]["chapterId"], fixture["chapterIds"][0])
            self.assertEqual(ctx["committedChapters"], 2)
            structure = load_structure(root)
            structure["chapters"][1]["status"] = "planning_required"
            save_structure(root, structure)
            (root / f"正文/{fixture['chapterIds'][1]}.md").unlink()
            changed = build_continuation_context(root, fixture["chapterIds"][0])
            self.assertEqual(changed["committedChapters"], 1)

    def test_entity_projection_includes_character_prop_location_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            state, _ = __import__("scripts.state_cache", fromlist=["load"]).load(root)
            ids = {item["entityId"] for item in _entities(state)}
            self.assertTrue({fixture["characterId"], fixture["propId"], fixture["locationId"], fixture["hookId"]}.issubset(ids))


    def test_plot_line_is_display_order_not_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            state, _ = __import__("scripts.state_cache", fromlist=["load"]).load(root)
            state["chapters"] = list(reversed(state["chapters"]))
            line = _plot_line(state)
            self.assertEqual([item["displayCode"] for item in line], ["CH001", "CH002"])

    def test_quarantined_chapter_is_absent_from_plot_and_recent_prose(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root, chapter_count=3)
            structure = load_structure(root)
            hidden_id = fixture['chapterIds'][1]
            structure['chapters'][1].update(status='quarantined', quarantineReason='隔离敏感插入',
                                             quarantineCategory='content_boundary')
            save_structure(root, structure)
            from scripts import state_cache
            state_cache.rebuild(root)
            context = build_continuation_context(root, fixture['chapterIds'][2])
            self.assertNotIn(hidden_id, [row['chapterId'] for row in context['plotLine']])
            self.assertNotIn(hidden_id, [row['chapterId'] for row in context['recentChapters']])
            self.assertEqual(context['committedChapters'], 2)

    def test_quarantined_target_cannot_build_continuation_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            structure = load_structure(root)
            chapter_id = fixture['chapterIds'][0]
            structure['chapters'][0].update(status='quarantined', quarantineReason='隔离复核',
                                             quarantineCategory='content_boundary')
            save_structure(root, structure)
            with self.assertRaises(ContinuationContextError) as caught:
                build_continuation_context(root, chapter_id)
            self.assertIn('CHAPTER_QUARANTINED', str(caught.exception))

    def test_unknown_pov_does_not_fallback_to_all_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root)
            with self.assertRaises(ContinuationContextError) as caught:
                build_continuation_context(root, fixture["chapterIds"][0], pov_character="character-NOT-FOUND")
            self.assertIn("POV_CHARACTER_NOT_FOUND", str(caught.exception))


    def test_station_view_omits_unbounded_collections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root, chapter_count=2)
            ctx = build_continuation_context(
                root, fixture["chapterIds"][1], task_type="writer", view="station"
            )
            self.assertEqual(ctx["view"], "station")
            self.assertIn("memoryPack", ctx)
            self.assertIn("recentChapters", ctx)
            for key in ("entities", "relations", "characterStates", "knowledgeStates", "plotLine"):
                self.assertNotIn(key, ctx)

    def test_invalid_view_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_current_project(root, chapter_count=1)
            with self.assertRaises(ContinuationContextError):
                build_continuation_context(root, fixture["chapterIds"][0], view="oversized")
