from __future__ import annotations

import unittest

from scripts.core.review_plan import ReviewPlanError, resolve_effective_review_plan


class ReviewPlanTests(unittest.TestCase):
    def test_chapter_uses_core_seven_and_optional_seats(self) -> None:
        plan = resolve_effective_review_plan("gnovel-planner", "chapter", chapter={"reviewClass": "ordinary"})
        self.assertEqual(plan["activeSeats"], [1, 2, 4, 5, 6, 7, 10])
        expanded = resolve_effective_review_plan("gnovel-planner", "chapter", chapter={"reviewClass": "ordinary", "newPlay": True, "hookRisk": True})
        self.assertIn(3, expanded["activeSeats"])
        self.assertIn(8, expanded["activeSeats"])
        self.assertNotIn(9, expanded["activeSeats"])

    def test_title_and_stage_b_have_contract_shapes(self) -> None:
        title = resolve_effective_review_plan("gnovel-titler", "title")
        self.assertEqual(title["activeSeats"], [1, 2, 3, 6, 10])
        stage_b = resolve_effective_review_plan("gnovel-editor", "stage_b")
        self.assertEqual(stage_b["activeSeats"], list(range(1, 11)))
        self.assertEqual(stage_b["auxiliaryGates"], ["cognition", "organization"])
        self.assertFalse(stage_b["semanticRulesEmbedded"])
        self.assertTrue(stage_b["sourceContract"]["sha256"])

    def test_unknown_decision_type_is_rejected(self) -> None:
        with self.assertRaises(ReviewPlanError):
            resolve_effective_review_plan("gnovel-planner", "unknown")


if __name__ == "__main__":
    unittest.main()
