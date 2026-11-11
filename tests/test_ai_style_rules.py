"""Original neutral controls for style evidence, scope and non-blocking review."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.ai_cliche_linter import build_report
from scripts.core import data_provider
from scripts.core.reports import gate_passed
from scripts.core.writing_checks import check_paragraph, rules_card
from scripts.text.ai_patterns import load_text_patterns, scan_ai_patterns
from scripts.text.prose_spans import mask_prose
from scripts.text.validator import build_checks, check_ai_patterns

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = '他深吸一口气。\n\n眼中闪过疑虑。\n\n嘴角勾起一道笑意。\n\n空气似乎凝固。\n\n话里透着几分不屑。'


class StyleEvidenceTests(unittest.TestCase):
    def override_patterns(self, registry):
        original = data_provider.registry
        return patch('scripts.core.data_provider.registry',
                     side_effect=lambda name, *args, **kwargs: registry if name == 'text-patterns' else original(name, *args, **kwargs))

    def hits(self, text, rule_id):
        return [row for row in build_report(text)['findings'] if row['ruleId'] == rule_id]

    def test_physical_sensation_and_plain_cognition_are_not_emotion_templates(self):
        for text in ('她感到手腕刺痛，解开了表带。', '他意识到钥匙还在门外，起身去取。', '他明白了锁扣的用法。'):
            with self.subTest(text=text):
                self.assertEqual(build_report(text)['findings'], [])

    def test_style_evidence_does_not_identify_an_author_or_order_a_rewrite(self):
        text = '他不是来收信，而是来退件。说实话，她感到一阵不安。'
        report = build_report(text)
        self.assertEqual(report['recommendation'], 'review_advisory')
        self.assertTrue(gate_passed(report['recommendation']))
        self.assertEqual(report['calibrationStatus'], 'UNCALIBRATED')
        self.assertIn('no-ai-origin-proof', report['limitations'])
        self.assertGreaterEqual(len(report['findings']), 3)
        for finding in report['findings']:
            self.assertEqual((finding['review'], finding['severity'], finding['gate']), ('semantic', 'advisory', False))
            self.assertNotIn('BODY-FIRST', finding['ruleId'])

    def test_structural_candidates_have_contextual_rewrite_guidance(self):
        cases = {
            'VOLUME-CONTRAST': '话音不高，却使整排座位都安静下来。',
            'NEGATION-PARADE': '没有招呼，没有解释，只有一张回执。',
            'ENDING-PREVIEW': '新的篇章即将开始。',
            'FUTURE-NARRATION': '无人知道，这封信日后会送进博物馆。',
        }
        for rule_id, text in cases.items():
            with self.subTest(rule_id=rule_id):
                finding, = self.hits(text, rule_id)
                self.assertGreater(len(finding['rewriteHint']), 20)
                self.assertFalse(finding['gate'])
                self.assertEqual(text[finding['start']:finding['end']], finding['matched'])

    def test_ordinary_time_information_and_a_single_breath_are_not_preview_or_cluster(self):
        for text in ('几年以后，铺子搬到了河对岸。', '无人知道钥匙放在哪，他只好等同事回来。',
                     '她深吸一口气，潜入池底。', '齿轮转动起来，传送带停在标线前。'):
            with self.subTest(text=text):
                self.assertEqual(build_report(text)['findings'], [])

    def test_cluster_pools_first_hits_across_paragraphs_and_keeps_each_location(self):
        finding, = self.hits(CLUSTER, 'CLICHE-CLUSTER')
        self.assertEqual(finding['density']['count'], 5)
        self.assertEqual([row['line'] for row in finding['occurrences']], [1, 3, 5, 7, 9])
        for row in finding['occurrences']:
            self.assertEqual(CLUSTER[row['start']:row['end']], row['matched'])
        member_ids = {row['ruleId'] for row in finding['occurrences']}
        self.assertFalse(member_ids & {row['ruleId'] for row in build_report(CLUSTER)['findings']})

    def test_cluster_is_local_and_quoted_padding_cannot_dilute_it(self):
        self.assertTrue(self.hits(CLUSTER + '\n\n' + '走廊里放着木箱。' * 200, 'CLICHE-CLUSTER'))
        original, = self.hits(CLUSTER, 'CLICHE-CLUSTER')
        quoted, = self.hits(CLUSTER + '\n\n“' + '字' * 2000 + '”', 'CLICHE-CLUSTER')
        self.assertEqual(original['density'], quoted['density'])
        self.assertFalse(self.hits('\n\n'.join(CLUSTER.split('\n\n')[:3]), 'CLICHE-CLUSTER'))

    def test_ending_window_and_mode_are_consumed_by_all_entrypoints(self):
        text = '新的篇章即将开始。\n\n' + '走廊里放着木箱。' * 80
        self.assertFalse(self.hits(text, 'ENDING-PREVIEW'))
        registry = deepcopy(load_text_patterns())
        next(r for r in registry['ai']['regex'] if r['ruleId'] == 'NOT-A-BUT-B')['exemptModes'] = ['peakScene']
        text = '夹层里不是账本，而是一本菜谱。'
        with self.override_patterns(registry):
            self.assertTrue(build_report(text, mode='plot')['findings'])
            self.assertFalse(build_report(text, mode='peakScene')['findings'])
            self.assertFalse(check_paragraph(text=text, mode='peakScene', intensityLevel=1)['findings'])
            card = rules_card(mode='peakScene', intensityLevel=1)
            self.assertNotIn('NOT-A-BUT-B', {row['ruleId'] for row in card['rules']})
            _, checks, _ = build_checks('peakScene', False, 1)
            result = next(check(text) for name, check in checks if name == 'AI句式套路')
            self.assertEqual(result, (True, ''))

    def test_count_and_density_are_independent_requirements(self):
        registry = deepcopy(load_text_patterns())
        rule = next(r for r in registry['ai']['regex'] if r['ruleId'] == 'NOT-A-BUT-B')
        rule.update(pattern='提示信号', minMatches=2, minPerKHan=20)
        with self.override_patterns(registry):
            self.assertFalse(self.hits('提示信号。', rule['ruleId']))
            self.assertFalse(self.hits('提示信号。提示信号。' + '字' * 120, rule['ruleId']))
            self.assertEqual(len(self.hits('提示信号。提示信号。', rule['ruleId'])), 2)

    def test_quotes_metadata_and_crlf_preserve_positions_without_waiving_prose(self):
        text = '“不是来收信，\r\n而是来退件。”\r\n<!-- 去味:跳过 -->\r\n夹层里不是账本，而是菜谱。'
        finding, = self.hits(text, 'NOT-A-BUT-B')
        self.assertEqual((finding['line'], finding['column']), (4, 4))
        self.assertEqual(text[finding['start']:finding['end']], finding['matched'])
        self.assertEqual(len(mask_prose(text)), len(text))
        self.assertEqual([i for i, c in enumerate(text) if c in '\r\n'],
                         [i for i, c in enumerate(mask_prose(text)) if c in '\r\n'])
        for wrapper in ('```text\n{}\n```', '~~~text\n{}\n~~~', '---\nexample: {}\n---', '<!-- {} -->'):
            self.assertFalse(build_report(wrapper.format('他感到一阵不安。'))['findings'])

    def test_parallel_dialogue_is_a_semantic_candidate_not_an_automatic_veto(self):
        text = '“没有招呼，没有解释，只有回执。”'
        self.assertTrue(self.hits(text, 'NEGATION-PARADE'))
        self.assertTrue(check_ai_patterns(text)[0])

    def test_explicit_mechanical_provider_policy_still_blocks(self):
        registry = deepcopy(load_text_patterns())
        rule = next(r for r in registry['ai']['regex'] if r['ruleId'] == 'NOT-A-BUT-B')
        rule.update(review='mechanical', severity='blocking', gate=True)
        text = '夹层里不是账本，而是菜谱。'
        with self.override_patterns(registry):
            self.assertEqual(build_report(text)['recommendation'], 'surgical_edit')
            self.assertFalse(check_ai_patterns(text)[0])
            self.assertEqual(next(row for row in rules_card(mode='plot', intensityLevel=1)['rules']
                                  if row['ruleId'] == 'S-10')['severity'], 'error')
        rule['review'] = 'semantic'
        with self.override_patterns(registry):
            self.assertEqual(build_report(text)['recommendation'], 'review_advisory')
            self.assertTrue(check_ai_patterns(text)[0])

    def test_invalid_thresholds_and_members_fail_closed(self):
        for field, value in (('minMatches', True), ('windowHan', 0), ('minPerKHan', float('nan')),
                             ('members', ['missing-rule'])):
            registry = deepcopy(load_text_patterns())
            registry['ai']['clusters'][0][field] = value
            with self.subTest(field=field), self.override_patterns(registry):
                with self.assertRaises(ValueError):
                    scan_ai_patterns(CLUSTER)

    def test_aggregate_exposes_semantic_evidence_without_failing_mechanical_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'body.md'
            path.write_text('夹层里不是账本，而是一本菜谱。')
            proc = subprocess.run([sys.executable, str(ROOT / 'scripts/aggregate_diagnostics.py'), str(path),
                                   '--mode', 'plot', '--intensity-level', '1', '--json'],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertTrue(report['passed'])
            self.assertEqual(report['aggregatedRecommendation'], 'review_advisory')
            finding = next(row for row in report['findings'] if row['ruleId'] == 'NOT-A-BUT-B')
            self.assertEqual(finding['review'], 'semantic')
            self.assertFalse(finding['gate'])
            self.assertEqual(finding['column'], 4)


if __name__ == '__main__':
    unittest.main()
