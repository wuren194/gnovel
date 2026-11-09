"""Voice attribution certainty survives the public diagnostics CLI and gate."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VoiceDiagnosticsAttributionTests(unittest.TestCase):
    def report(self, text):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            draft, design = root / 'draft.md', root / 'design.json'
            draft.write_text(text, encoding='utf-8')
            design.write_text(json.dumps({'characters': [{
                'characterId': 'C-A', 'name': '林舟',
                'voiceProfile': {'bannedPhrasesForRole': ['随便']},
            }]}, ensure_ascii=False), encoding='utf-8')
            process = subprocess.run([sys.executable, str(ROOT / 'scripts/aggregate_diagnostics.py'),
                str(draft), '--design', str(design), '--intensity-level', '1', '--mode', 'plot', '--json'],
                text=True, capture_output=True, check=False)
            self.assertIn(process.returncode, (0, 1), process.stderr)
            report = json.loads(process.stdout)
            voice = [row for row in report['findings'] if row['source'] == 'check_voice_lexicon']
            return report, voice

    def test_uncertain_role_hit_is_visible_without_blocking_or_asserting_a_speaker(self):
        for text in ('林舟放下杯子。\n“随便。”', '林舟抬起手：“随便。”'):
            with self.subTest(text=text):
                report, findings = self.report(text)
                self.assertEqual(len(findings), 1)
                finding = findings[0]
                self.assertEqual(finding['ruleId'], 'OOC_VOICE_DRIFT')
                self.assertEqual(finding['severity'], 'warning')
                self.assertFalse(finding['raw']['speakerCertain'])
                self.assertIn('归属待复核', finding['message'])
                self.assertEqual(report['aggregatedRecommendation'], 'review_advisory')
                self.assertTrue(report['passed'])

    def test_explicit_speech_tag_keeps_the_role_lexicon_gate(self):
        report, findings = self.report('林舟说：“随便。”')
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['severity'], 'error')
        self.assertTrue(findings[0]['raw']['speakerCertain'])
        self.assertIn('明确发言标记', findings[0]['message'])
        self.assertEqual(report['aggregatedRecommendation'], 'surgical_edit')
        self.assertFalse(report['passed'])

    def test_global_hit_is_independent_of_unknown_speaker(self):
        report, findings = self.report('“该死。”')
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['ruleId'], 'VOICE-GLOBAL-INTERJECTION')
        self.assertEqual(findings[0]['severity'], 'error')
        self.assertIsNone(findings[0]['raw']['characterId'])
        self.assertFalse(findings[0]['raw']['speakerCertain'])
        self.assertIn('不依赖说话人归属', findings[0]['message'])
        self.assertEqual(report['aggregatedRecommendation'], 'surgical_edit')
        self.assertFalse(report['passed'])


if __name__ == '__main__':
    unittest.main()
