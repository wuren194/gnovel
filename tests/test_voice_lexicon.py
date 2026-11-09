"""The mechanical slice of the voice-signature check.

The checker never claims to prove OOC; it proves lexical facts and reports how
confident its speaker attribution was, so the Editor can dismiss misattributed
hits rather than trust the scanner blindly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_voice_lexicon import build_report, load_characters, load_global_banned, scan


CHARACTERS = [
    {"characterId": "C01", "name": "沈砚", "bannedPhrases": ["人家", "讨厌"]},
    {"characterId": "C02", "name": "季野", "bannedPhrases": ["老子"]},
]


class GlobalInterjectionTests(unittest.TestCase):
    def test_registry_supplies_the_global_list(self) -> None:
        banned = load_global_banned()
        self.assertIn("该死", banned)
        self.assertIn("天哪", banned)

    def test_global_interjection_is_flagged_for_any_speaker(self) -> None:
        findings = scan('「该死。」', [], ["该死"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "VOICE-GLOBAL-INTERJECTION")
        self.assertIsNone(findings[0].character_id)

    def test_narrative_text_is_not_scanned(self) -> None:
        # 只有引号内的台词受声纹约束；叙述里出现同一词不算。
        self.assertEqual(scan("该死的门锁又卡住了。", [], ["该死"]), [])


class AttributionTests(unittest.TestCase):
    def test_same_line_speaker_wins(self) -> None:
        findings = scan('沈砚说：「人家不知道。」', CHARACTERS, [])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].character_id, "C01")
        self.assertEqual(findings[0].speaker_confidence, "line")

    def test_lookback_attribution_is_marked_as_such(self) -> None:
        text = "沈砚把杯子推过去。\n「人家不知道。」"
        findings = scan(text, CHARACTERS, [])
        self.assertEqual(findings[0].character_id, "C01")
        self.assertTrue(findings[0].speaker_confidence.startswith("lookback"))

    def test_two_names_on_one_line_is_ambiguous_and_skips_per_role_rules(self) -> None:
        findings = scan('沈砚看着季野：「人家不知道。」', CHARACTERS, [])
        self.assertEqual(findings, [])

    def test_unattributed_line_still_gets_global_rules(self) -> None:
        findings = scan('「该死。」', CHARACTERS, ["该死"])
        self.assertEqual([item.rule_id for item in findings], ["VOICE-GLOBAL-INTERJECTION"])
        self.assertEqual(findings[0].speaker_confidence, "unattributed")

    def test_banned_phrase_of_another_character_does_not_fire(self) -> None:
        findings = scan('沈砚说：「老子不管。」', CHARACTERS, [])
        self.assertEqual(findings, [])


class DesignLoadingTests(unittest.TestCase):
    def test_voice_profile_is_read_from_story_design(self) -> None:
        design = {
            "characters": [
                {"characterId": "C01", "name": "沈砚",
                 "voiceProfile": {"bannedPhrasesForRole": ["人家"]}},
                {"characterId": "C02", "name": "季野"},
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "design.json"
            path.write_text(json.dumps(design, ensure_ascii=False), encoding="utf-8")
            characters = load_characters(path)
        self.assertEqual(characters[0]["bannedPhrases"], ["人家"])
        self.assertEqual(characters[1]["bannedPhrases"], [])

    def test_no_design_means_global_rules_only(self) -> None:
        self.assertEqual(load_characters(None), [])


class ReportTests(unittest.TestCase):
    def test_clean_prose_passes(self) -> None:
        report = build_report('沈砚说：「听话。」', CHARACTERS, load_global_banned())
        self.assertEqual(report["recommendation"], "pass")
        self.assertEqual(report["findings"], [])

    def test_report_declares_its_limits(self) -> None:
        report = build_report("正文", CHARACTERS, [])
        self.assertIn("lexical-only", report["limitations"])
        self.assertIn("speaker-attribution-is-heuristic", report["limitations"])

    def test_confident_findings_are_counted_separately(self) -> None:
        text = "沈砚说：「人家不知道。」\n季野停了一下。\n「老子不管。」"
        report = build_report(text, CHARACTERS, [])
        self.assertEqual(report["metrics"]["findingCount"], 2)
        self.assertEqual(report["metrics"]["confidentFindingCount"], 1)
        self.assertEqual(report["recommendation"], "surgical_edit")


if __name__ == "__main__":
    unittest.main()
