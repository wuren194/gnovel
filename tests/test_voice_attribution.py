"""Speaker evidence must not turn a mentioned character into a certain speaker."""
from __future__ import annotations

from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_voice_lexicon as voice


PEOPLE = [
    {'characterId': 'C-A', 'name': '林舟', 'bannedPhrases': ['随便']},
    {'characterId': 'C-B', 'name': '顾行', 'bannedPhrases': ['一定']},
    {'characterId': 'C-C', 'name': '韩师傅', 'bannedPhrases': ['全部']},
]


class SpeakerEvidenceTests(unittest.TestCase):
    def report(self, text, characters=PEOPLE, global_banned=()):
        return voice.build_report(text, deepcopy(characters), list(global_banned))

    def test_explicit_speaker_with_named_listener_wins(self):
        for text in ('林舟对顾行说：“随便。”', '林舟问顾行：“随便？”',
                     '“随便。”林舟答道。'):
            with self.subTest(text=text):
                report = self.report(text)
                self.assertEqual(len(report['findings']), 1)
                finding = report['findings'][0]
                self.assertEqual((finding['characterId'], finding['severity']), ('C-A', 'error'))
                self.assertTrue(finding['speakerCertain'])

    def test_each_quote_on_one_line_has_its_own_speaker(self):
        report = self.report('林舟说：“随便。”顾行答：“一定。”')
        self.assertCountEqual([(r['characterId'], r['phrase']) for r in report['findings']],
                              [('C-A', '随便'), ('C-B', '一定')])
        self.assertEqual([r['characterId'] for r in report['coverage']['dialogueAttributions']], ['C-A', 'C-B'])
        self.assertTrue(all(r['speakerCertain'] for r in report['findings']))

    def test_explicit_phone_speech_is_not_attributed_to_addressee(self):
        report = self.report('电话那头的顾行对林舟说：“一定。”')
        finding = report['findings'][0]
        self.assertEqual((finding['characterId'], finding['speakerConfidence']), ('C-B', 'line'))
        self.assertTrue(finding['speakerCertain'])

    def test_separate_speech_tag_is_certain_but_action_only_is_advisory(self):
        explicit = self.report('林舟低声问：\n\n“随便？”')
        self.assertEqual(explicit['findings'][0]['speakerConfidence'], 'speech-tag')
        self.assertEqual(explicit['findings'][0]['severity'], 'error')
        action = self.report('林舟把杯子推过去。\n“随便。”')
        self.assertEqual(action['findings'][0]['characterId'], 'C-A')
        self.assertFalse(action['findings'][0]['speakerCertain'])
        self.assertEqual(action['findings'][0]['severity'], 'warning')
        self.assertEqual(action['recommendation'], 'review_advisory')

    def test_same_line_action_is_not_certain_speech(self):
        report = self.report('林舟放下杯子：“随便。”')
        self.assertEqual(report['findings'][0]['severity'], 'warning')
        self.assertFalse(report['findings'][0]['speakerCertain'])

    def test_a_name_mentioned_in_dialogue_is_never_the_next_speaker(self):
        report = self.report('“韩师傅明天来。”\n“全部到齐了？”')
        self.assertEqual(report['findings'], [])
        last = report['coverage']['dialogueAttributions'][-1]
        self.assertIsNone(last['characterId'])
        self.assertFalse(last['speakerCertain'])

    def test_absent_or_possessive_name_is_not_speech_evidence(self):
        for text in ('顾行还没来。\n“一定。”', '“随便。”林舟的名字写在纸上。',
                     '林舟没有说：\n“随便。”'):
            with self.subTest(text=text):
                report = self.report(text)
                self.assertEqual(report['findings'], [])
                self.assertIsNone(report['coverage']['dialogueAttributions'][0]['characterId'])

    def test_lookback_never_skips_an_intervening_dialogue_turn(self):
        report = self.report('林舟把杯子推过去。\n“先检查。”\n“随便。”')
        self.assertEqual(report['findings'], [])
        self.assertIsNone(report['coverage']['dialogueAttributions'][-1]['characterId'])

    def test_two_established_turns_allow_only_tentative_alternation(self):
        text = '林舟问：“检查了吗？”\n顾行答：“检查了。”\n“随便放在哪里？”'
        report = self.report(text)
        finding = report['findings'][0]
        self.assertEqual((finding['characterId'], finding['speakerConfidence']), ('C-A', 'turn-taking'))
        self.assertEqual(finding['severity'], 'warning')
        self.assertFalse(finding['speakerCertain'])
        self.assertEqual(report['recommendation'], 'review_advisory')

    def test_phone_pair_allows_tentative_reply_but_third_speaker_stops_it(self):
        before = '电话那头的顾行说：“检查好了。”\n林舟问：“什么时候取？”\n'
        report = self.report(before + '“一定按时。”')
        self.assertEqual([(r['characterId'], r['speakerConfidence'], r['severity']) for r in report['findings']],
                         [('C-B', 'turn-taking', 'warning')])
        report = self.report(before + '韩师傅答：“明天。”\n“随便。”')
        self.assertEqual(report['findings'], [])
        self.assertIsNone(report['coverage']['dialogueAttributions'][-1]['characterId'])

    def test_narrative_after_explicit_tag_clears_prior_turn_pair(self):
        for ending in ('顾行答：“检查了。”电话挂断了。',
                       '“检查了。”顾行答道，电话挂断了。'):
            with self.subTest(ending=ending):
                report = self.report('林舟问：“检查了吗？”\n' + ending + '\n“随便。”')
                self.assertTrue(report['coverage']['dialogueAttributions'][1]['speakerCertain'])
                self.assertEqual(report['findings'], [])
                self.assertIsNone(report['coverage']['dialogueAttributions'][-1]['characterId'])

    def test_unclear_third_party_or_scene_boundary_resets_turn_context(self):
        before = '林舟问：“检查了吗？”\n顾行答：“检查了。”\n'
        for boundary in ('韩师傅的名字写在封面上。', '## 第二天', '电话挂断了。'):
            with self.subTest(boundary=boundary):
                report = self.report(before + boundary + '\n“随便。”')
                self.assertEqual(report['findings'], [])
                self.assertIsNone(report['coverage']['dialogueAttributions'][-1]['characterId'])

    def test_conflicting_explicit_tags_do_not_select_either_speaker(self):
        report = self.report('林舟说：“随便。”顾行答道。')
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['coverage']['dialogueAttributions'][0]['confidence'], 'ambiguous')

    def test_global_rule_remains_error_with_unknown_or_tentative_speaker(self):
        for text in ('“该死。”', '林舟放下杯子。\n“该死。”'):
            with self.subTest(text=text):
                report = self.report(text, global_banned=['该死'])
                self.assertEqual([r['severity'] for r in report['findings']], ['error'])
                self.assertEqual(report['recommendation'], 'surgical_edit')

    def test_no_private_bans_does_not_fabricate_findings_or_hide_coverage(self):
        characters = [{**person, 'bannedPhrases': []} for person in PEOPLE]
        report = self.report('林舟放下杯子。\n“随便。”\n“全部？”', characters)
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['recommendation'], 'pass')
        self.assertEqual(report['metrics']['charactersWithBannedLexicon'], 0)
        self.assertEqual(len(report['coverage']['dialogueAttributions']), 2)
        self.assertIsNone(report['coverage']['dialogueAttributions'][-1]['characterId'])

    def test_scan_and_report_use_the_same_attribution_rows(self):
        text = '林舟说：“随便。”\n顾行把杯子放下。\n“一定。”'
        report = self.report(text)
        self.assertEqual(report['findings'], [f.as_dict() for f in voice.scan(text, PEOPLE, [])])
        by_quote = {(r['line'], r['quote']): r for r in report['coverage']['dialogueAttributions']}
        for finding in report['findings']:
            row = by_quote[finding['line'], finding['quote']]
            self.assertEqual((finding['characterId'], finding['speakerConfidence'], finding['speakerCertain']),
                             (row['characterId'], row['confidence'], row['speakerCertain']))

    def test_each_attribution_grammar_field_changes_actual_behavior(self):
        original = json.loads(voice.TEXT_PATTERNS_PATH.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'patterns.json'
            cases = (
                ('speechVerbs', '测试说', '林舟说', '林舟测试说', 'error'),
                ('speechModifiers', '测试轻声', '林舟低声说', '林舟测试轻声说', 'error'),
                ('speakerPrefixes', '来电里的', '电话那头的林舟说', '来电里的林舟说', 'error'),
                ('listenerPrefixes', '面向', '林舟对顾行说', '林舟面向顾行说', 'error'),
                ('actionPrefixes', '撂下', '林舟放下杯子', '林舟撂下杯子', 'warning'),
            )
            for key, token, old, new, severity in cases:
                with self.subTest(key=key):
                    modified = deepcopy(original)
                    modified['voice']['attribution'][key] = [token]
                    path.write_text(json.dumps(modified, ensure_ascii=False))
                    with patch.object(voice, 'TEXT_PATTERNS_PATH', path):
                        self.assertEqual(self.report(old + '：“随便。”')['findings'], [])
                        finding = self.report(new + '：“随便。”')['findings'][0]
                        self.assertEqual(finding['severity'], severity)
                        self.assertEqual(finding['speakerCertain'], severity == 'error')

    def test_invalid_attribution_registry_fails_closed(self):
        original = json.loads(voice.TEXT_PATTERNS_PATH.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'patterns.json'
            for invalid in (None, {}, {'speechVerbs': ['说']}, '说'):
                with self.subTest(invalid=invalid):
                    modified = deepcopy(original)
                    modified['voice']['attribution'] = invalid
                    path.write_text(json.dumps(modified, ensure_ascii=False))
                    with patch.object(voice, 'TEXT_PATTERNS_PATH', path), self.assertRaises(voice.VoiceLexiconError):
                        self.report('林舟说：“随便。”')

    def test_cli_returns_fail_closed_when_attribution_configuration_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            patterns, prose = Path(tmp) / 'patterns.json', Path(tmp) / 'prose.md'
            patterns.write_text(json.dumps({'voice': {'globalBannedInterjections': []}}))
            prose.write_text('“检查好了。”')
            stderr = io.StringIO()
            with patch.object(voice, 'TEXT_PATTERNS_PATH', patterns), patch.object(sys, 'argv',
                    ['check_voice_lexicon.py', str(prose), '--json']), redirect_stderr(stderr):
                self.assertEqual(voice.main(), voice.EXIT_FAIL_CLOSED)
            self.assertIn('attribution', stderr.getvalue())
            self.assertNotIn('Traceback', stderr.getvalue())

    def test_speech_tag_between_two_fragments_preserves_explicit_speaker(self):
        report = self.report('“随便放着。”林舟说，“随便找个地方。”')
        self.assertEqual(len(report['findings']), 2)
        self.assertTrue(all(r['speakerCertain'] and r['characterId'] == 'C-A' for r in report['findings']))

    def test_non_subject_names_do_not_turn_reported_speech_into_direct_speech(self):
        for prefix in ('林舟的同事说', '顾行听见林舟说', '林舟没有说', '并非林舟说'):
            with self.subTest(prefix=prefix):
                self.assertEqual(self.report(prefix + '：“随便。”')['findings'], [])


if __name__ == '__main__':
    unittest.main()
