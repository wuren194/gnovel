from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.ai_cliche_linter import build_report, check_intensity_level_consistency


class LinterTests(unittest.TestCase):
    def test_valid_body_reaction_is_not_a_cliche_by_itself(self) -> None:
        report = build_report("他喉结滚了一下，掌心的汗蹭在裤缝上。")
        self.assertEqual(report["recommendation"], "pass")

    def test_abstract_emotion_without_body_is_actionable(self) -> None:
        report = build_report("他感到一阵羞耻。")
        rule_ids = {finding["ruleId"] for finding in report["findings"]}
        self.assertIn("ABSTRACT-EMOTION", rule_ids)
        self.assertIn("BODY-FIRST", rule_ids)
        self.assertEqual(report["recommendation"], "surgical_edit")

    def test_dash_and_transitions_are_not_linter_business(self) -> None:
        # 破折号、转折词与功能词密度的唯一真源是 validate-text.py S-13/S-17/S-18；
        # linter 不得平行判定，避免与机械层给出矛盾结论。
        narrative = build_report("他伸手去抓——门已经关上了，但是他没有停，然而已经晚了。")
        rule_ids = {item["ruleId"] for item in narrative["findings"]}
        self.assertNotIn("NARRATIVE-EM-DASH", rule_ids)
        self.assertNotIn("TRANSITION-DENSITY", rule_ids)

    def test_intensity_level_is_an_upper_bound_not_a_quota(self) -> None:
        self.assertEqual(check_intensity_level_consistency("他握住门把。", 9)["status"], "PASS")
        from scripts import intensity_registry
        with patch.object(intensity_registry, 'ambiguous_ceiling_words', return_value=('待核对词',)), \
             patch.object(intensity_registry, 'ceiling_violations', return_value=('待核对词', '边界示例词')), \
             patch.object(intensity_registry, 'extreme_markers', return_value=('边界示例词',)):
            self.assertEqual(check_intensity_level_consistency("文本含有待核对词。", 1)["status"], "REVIEW_REQUIRED")
            self.assertEqual(check_intensity_level_consistency("明确短语：边界示例词。", 1)["status"], "INCONSISTENT")

    def test_that_was_only_flags_explanatory_structure(self) -> None:
        report = build_report("那是某种意味着他已经输了的证据。那是某种代表关系已经改变的信号。")
        findings = [finding for finding in report["findings"] if finding["ruleId"] == "THAT-WAS"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "advisory")
        self.assertEqual(report["recommendation"], "pass")


if __name__ == "__main__":
    unittest.main()
