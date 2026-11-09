"""S-11 must not manufacture a personal subject for negative existence clauses."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts import debug_validation
from scripts.text import validator
from scripts.core.project import diagnostic_policy


ROOT = Path(__file__).resolve().parents[1]


def subject_runs(text):
    return [row for row in validator.subject_sentence_violations(text)
            if row['kind'] == 'subject_run']


class SubjectExistenceRegressionTests(unittest.TestCase):
    def test_impersonal_existence_breaks_the_personal_subject_run(self):
        for clause in (
            '没有新通知，只剩昨天的安排',
            '没有任何新通知',
            '没有通知传来',
            '没有新的消息传来',
            '没一点声音',
            '仍然没有任何新消息',
            '再没有脚步声响起',
            '没有人应门',
            '没有任何人回答',
        ):
            with self.subTest(clause=clause):
                self.assertEqual(validator._subject_key(clause), '')
                self.assertFalse(validator._omitted_subject_sentence(clause))
                self.assertEqual(subject_runs(f'他打开屏幕。{clause}。他关上电脑。'), [])

    def test_negated_actions_still_continue_a_subject(self):
        for clause in (
            '没拿钥匙', '没有收起文件', '没有回答问题', '没有做记录',
            '没有通知同事', '没有通知', '没看到新通知', '没有听见声音', '没有回信',
            '没有再去敲门', '仍然没有关上窗户', '没有想法也没有开口',
        ):
            with self.subTest(clause=clause):
                self.assertTrue(validator._omitted_subject_sentence(clause))
                self.assertEqual(len(subject_runs(f'他走进办公室。{clause}。他坐下。')), 1)

    def test_ambiguous_notification_keeps_the_established_action_subject(self):
        text = '他答应通知同事。没有通知，反而拔掉了电话线。他拿起外套。'
        rows = subject_runs(text)
        self.assertEqual(len(rows), 1)
        self.assertIn('含省略主语延续', rows[0]['detail'])
        self.assertEqual(subject_runs('他查看邮箱。没有新通知。他拿起外套。'), [])

    def test_explicit_subject_is_not_exempted_by_existential_predicate(self):
        rows = subject_runs('他查看屏幕。他没有新消息。他关上电脑。')
        self.assertEqual(len(rows), 1)
        self.assertNotIn('省略', rows[0]['detail'])

    def test_mixed_run_reports_omission_whichever_branch_finishes_it(self):
        cases = (
            '他打开文件。没有做记录。他关上电脑。',
            '他打开文件。他没有做记录。关上电脑。',
            '他打开文件。没有做记录。关上电脑。',
        )
        for text in cases:
            with self.subTest(text=text):
                rows = subject_runs(text)
                self.assertEqual(len(rows), 1)
                self.assertIn('含省略主语延续', rows[0]['detail'])
                self.assertNotIn('显式主语开头', rows[0]['detail'])

    def test_all_explicit_run_keeps_explicit_description(self):
        rows = subject_runs('他打开文件。他检查日期。他关上电脑。')
        self.assertEqual(len(rows), 1)
        self.assertIn('显式主语开头', rows[0]['detail'])
        self.assertNotIn('省略', rows[0]['detail'])

    def test_break_and_subject_change_reset_omission_description(self):
        for prefix in ('他打开文件。没有做记录。没有新消息。',
                       '他打开文件。没有做记录。“等一等。”',
                       '她打开文件。没有做记录。'):
            with self.subTest(prefix=prefix):
                rows = subject_runs(prefix + '他打开门。他迈进房间。他关上门。')
                self.assertEqual(len(rows), 1)
                self.assertNotIn('省略', rows[0]['detail'])

    def test_existence_pattern_is_consumed_from_the_registry(self):
        clause = '没有新消息'
        policy = deepcopy(diagnostic_policy())
        self.assertIn('impersonalExistencePattern', policy.get('subjectSentence', {}))
        self.assertFalse(validator._omitted_subject_sentence(clause))
        policy['subjectSentence']['impersonalExistencePattern'] = r'(?!)'
        with patch('scripts.text.validator.diagnostic_policy', return_value=policy):
            self.assertTrue(validator._omitted_subject_sentence(clause))

    def test_cli_and_debug_use_the_same_nonblocking_existence_result(self):
        text = '他打开屏幕。没有新通知，只剩昨天的安排。他关上电脑。'
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/validate-text.py'),
                              '--mode', 'plot', '--json'], input=text,
                             text=True, capture_output=True, check=False)
        self.assertIn(run.returncode, (0, 1))
        report = json.loads(run.stdout)
        check = next(row for row in report['checks'] if row['rule_id'] == 'S-11')
        self.assertTrue(check['pass'])
        self.assertEqual(debug_validation.subject_sentences_issues(validator, text), [])


if __name__ == '__main__':
    unittest.main()
