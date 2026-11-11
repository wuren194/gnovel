"""Model reading text preserves evidence and precise next-call parameters."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.core.model_text import _rows, _smaller, render_model_text


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class ModelTextTests(unittest.TestCase):
    def assert_smaller(self, text, payload):
        original = compact(payload)
        self.assertLess(len(text), len(original))
        self.assertLess(len(text.encode()), len(original.encode()))
        self.assertLess(len(compact(text)), len(compact(original)))
        self.assertLess(len(compact(text).encode()), len(compact(original).encode()))

    def test_real_preparation_is_shorter_and_preserves_material_and_action_values(self):
        from scripts.core.operations import call_operation
        from tests.fixtures_workflow import review_ready_project
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            chapter = review_ready_project(root, chapter_count=1)['chapterIds'][0]
            report = call_operation('task.prepare', root, {'task': 'editor', 'chapterId': chapter, 'view': 'model'})
            unchanged = deepcopy(report)
            text = render_model_text('task.prepare', report)
            self.assert_smaller(text, report)
            self.assertEqual(report, unchanged)
            self.assertIn('调用成功。', text)
            self.assertIn(report['prepareRef'], text)
            self.assertIn(report['reading']['credential'], text)
            self.assertIn('准备不授予批准。', text)
            self.assertIn('仍需工位判断。', text)
            self.assertIn('"pendingApprovals":[]', text)
            self.assertIn('已就绪:设定、上下文、操作授权条件、范围、任务。', text)
            self.assertIn(compact(report['reviewPlan']), text)
            for row in report['toolPlan']['toolActions']:
                self.assertIn(compact(row['binding']), text)
                self.assertIn(compact(row['reason']), text)
                self.assertIn(compact(row['authorization']), text)
            for row in report['contractReads']:
                self.assertIn(compact(row['path']), text)
                self.assertIn(compact(row['readIds']), text)
            for row in report['modelContext']['memories']:
                self.assertIn(compact(row['content']), text)
                self.assertIn(compact(row['role']), text)
            self.assertIn('空列表:', text)
            self.assertIn('缺项:无', text)

    def test_no_inference_from_call_success_or_unknown_readiness(self):
        payload = {'ok': True, 'code': 'CHAPTER_STATUS', 'readyToFinalize': False,
                   'readyForContinuation': None, 'semanticReviewComplete': False,
                   'approvalPerformed': False, 'readiness': {'taskReady': None, 'contextReady': False},
                   'authorization': {'preparationGrantsApproval': False,
                                     'pendingApprovals': [{'artifactType': 'stage_b_review', 'approved': False}]},
                   'blockers': [{'code': 'NOT_REVIEWED', 'message': '当前版本尚未审阅。'}],
                   'futureStatus': {'passed': None, 'checked': 'unknown'}}
        text = render_model_text('project.workflow.chapter-status', payload)
        self.assert_smaller(text, payload)
        self.assertIn('调用成功。', text)
        self.assertIn('定稿未就绪。', text)
        self.assertIn('语义审阅未完成。', text)
        self.assertIn('本次未批准。', text)
        self.assertIn('taskReady', text)
        self.assertIn('readyForContinuation', text)
        # A two-field partial state is shorter as JSON; null is not false.
        self.assertIn(compact(payload['readiness']), text)
        self.assertIn('准备不授予批准。', text)
        self.assertIn(compact(payload['authorization']['pendingApprovals']), text)
        self.assertIn(compact(payload['futureStatus']), text)
        self.assertNotIn('全部通过', text)

    def test_recovery_keeps_exact_tokens_conditions_and_prose(self):
        quote = '原句含有"引号"、\\路径\n以及第二行。'
        action = {'operation': 'workflow.closeout', 'arguments': {'projectRoot': '/tmp/有 空格',
                  'chapterId': 'chapter-EXAMPLE', 'prepareRef': 'gn:prepare:exact',
                  'requestId': 'retry-exact', 'bindingGeneration': 19, 'cursor': 'next:=+/'},
                  'needsApproval': True, 'executable': False, 'when': 'after_author_review'}
        payload = {'ok': False, 'code': 'CLOSEOUT_INPUT_REQUIRED', 'readyToFinalize': False,
                   'semanticReviewComplete': False, 'approvalPerformed': False,
                   'blockers': [{'code': 'AUTHOR_REQUIRED', 'message': quote}],
                   'runAction': action, 'newField': {'text': quote, 'nested': {'unknown': False}}}
        before = deepcopy(payload)
        text = render_model_text('workflow.closeout', payload)
        self.assert_smaller(text, payload)
        self.assertIn(compact(action), text)
        self.assertIn(compact(quote), text)
        self.assertIn(compact(payload['newField']), text)
        self.assertEqual(payload, before)

    def test_uniform_rows_preserve_all_columns_and_nonuniform_rows_keep_json(self):
        rows = [{'chapterId': f'chapter-{i}', 'title': '含\n换行和|竖线', 'unknownExtension': {'x': None}}
                for i in range(5)]
        text = render_model_text('project.overview', {'chapters': rows, 'bookName': '测试书'})
        self.assertIn('列' + compact(list(rows[0])), text)
        for row in rows:
            self.assertIn(compact(list(row.values())), text)
        rows[1].pop('unknownExtension')
        text = render_model_text('project.overview', {'chapters': rows, 'bookName': '测试书'})
        self.assertIn(compact(rows), text)

    def test_complete_views_unknown_operations_and_tiny_results_keep_json(self):
        payload = {'ok': True, 'code': 'TASK_READY', 'readiness': {'taskReady': True},
                   'modelContext': {'memories': [], 'authorDecisions': [], 'referenceKnowledge': []}}
        for view in ('full', 'station', 'audit'):
            self.assertEqual(render_model_text('task.prepare', payload, view=view), compact(payload))
        self.assertEqual(render_model_text('unknown.future', payload), compact(payload))
        self.assertEqual(render_model_text(None, payload), compact(payload))
        self.assertEqual(render_model_text('task.prepare', {'ok': True}), '{"ok":true}')
        self.assertEqual(render_model_text('task.prepare', ['text']), '["text"]')

    def test_credential_policy_keeps_non_string_secret_labels_but_allows_public_columns(self):
        from scripts.core.project import call_logging_policy
        from unittest.mock import patch
        for value in (735291, True, None, {}, [735291]):
            rows = [{'id': f'OP-{index}', 'password': value, 'ordinaryValue': value}
                    for index in range(4)]
            self.assertEqual(_rows(rows), compact(rows))
        public = [{'id': f'OP-{index}', 'authorization': {'type': 'string', 'enum': ['read', 'candidate']},
                   'secret': {'meaning': '普通文学内容'}} for index in range(4)]
        self.assertTrue(_rows(public).startswith('列['))
        policy = deepcopy(call_logging_policy())
        policy['credentialFields'].append('privatePin')
        rows = [{'id': f'OP-{index}', 'private_pin': 735291, 'ordinaryValue': 735291}
                for index in range(4)]
        with patch('scripts.core.model_text.call_logging_policy', return_value=policy):
            self.assertEqual(_rows(rows), compact(rows))

    def test_diagnostics_distinguish_mechanical_success_and_semantic_work(self):
        payload = {'ok': True, 'passed': True, 'semanticReviewComplete': False,
                   'approvalPerformed': False, 'findings': [], 'chapterContract': {'pov': {'status': 'not_checked'}},
                   'fullDetails': {'operation': 'task.snapshot', 'arguments': {'reference': 'gn:report:exact'}}}
        text = render_model_text('diagnostics.aggregate', payload)
        self.assert_smaller(text, payload)
        self.assertIn('机械检查通过。', text)
        self.assertIn('语义审阅未完成。', text)
        self.assertIn('本次未批准。', text)
        self.assertIn(compact(payload['chapterContract']), text)
        self.assertIn(compact(payload['fullDetails']), text)

    def test_readiness_and_authorization_keep_false_null_and_extensions_distinct(self):
        action = {'operation': 'workflow.closeout', 'arguments': {'prepareRef': 'exact-ref'},
                  'when': 'after_author_review', 'needsApproval': True, 'executable': False}
        payload = {'ok': True, 'code': 'TASK_READY', 'readiness': {
            'structureHealthy': False, 'settingsReady': False, 'contextReady': None,
            'authorizationReady': True, 'scopeReady': 'unknown', 'taskReady': False,
            'futureReady': True}, 'authorization': {'preparationGrantsApproval': False,
                'stationJudgmentsRequired': True, 'pendingApprovals': [action],
                'unknownAuthorization': {'approved': None}}, 'nextAction': None}
        untouched = deepcopy(payload)
        text = render_model_text('task.prepare', payload)
        self.assert_smaller(text, payload)
        self.assertEqual(payload, untouched)
        self.assertIn('结构不健康。', text)
        self.assertIn('已就绪:操作授权条件。', text)
        self.assertIn('未就绪:设定、任务。', text)
        self.assertIn('"contextReady":null', text)
        self.assertIn('"scopeReady":"unknown"', text)
        self.assertIn('"futureReady":true', text)
        self.assertIn('准备不授予批准。', text)
        self.assertIn('仍需工位判断。', text)
        self.assertIn(compact(action), text)
        self.assertIn('"unknownAuthorization":{"approved":null}', text)
        self.assertNotIn('已获作者批准', text)
        for value in (None, True, 'unknown'):
            variant = {**payload, 'authorization': {'preparationGrantsApproval': value,
                       'stationJudgmentsRequired': False, 'pendingApprovals': [action]}}
            result = render_model_text('task.prepare', variant)
            self.assertIn(compact(variant['authorization']), result)
            self.assertNotIn('准备不授予批准。', result)
            self.assertNotIn('仍需工位判断。', result)

    def test_text_must_also_shrink_after_mcp_json_string_escaping(self):
        # Three newlines are shorter before encoding but longer as a JSON string.
        self.assertFalse(_smaller('\n\n\n', 'aaaa'))
        self.assertFalse(_smaller('\\\\\\', 'aaaa'))
        self.assertTrue(_smaller('short', 'a longer plain string'))
        for text in ('\n' * 40, '"\\\t' * 40, '正文\n"原话"\\原路径' * 10):
            report = {'bookName': text, 'chapters': [
                {'chapterId': str(i), 'title': text, 'body': text} for i in range(4)]}
            shown = render_model_text('project.overview', report)
            baseline = compact(report)
            packet = lambda value: compact({'content': [{'type': 'text', 'text': value}],
                                           'structuredContent': report})
            self.assertLessEqual(len(packet(shown)), len(packet(baseline)))
            self.assertLessEqual(len(packet(shown).encode()), len(packet(baseline).encode()))

    def test_nested_and_top_level_actions_have_distinct_scoped_text(self):
        rows = [{'artifactType': f'artifact-{index}', 'registered': True, 'fresh': True,
                 'envelopeValid': True, 'artifactSha256': str(index) * 64} for index in range(10)]
        action = {'operation': 'workflow.closeout', 'arguments': {'prepareRef': 'exact-reference'},
                  'needsApproval': True, 'executable': False}
        outer = {'receipt': {'artifacts': rows}, 'runAction': action}
        inner = {'receipt': {'artifacts': rows, 'runAction': action}}
        outer_text = render_model_text('workflow.closeout-prepare', outer)
        inner_text = render_model_text('workflow.closeout-prepare', inner)
        self.assert_smaller(outer_text, outer)
        self.assert_smaller(inner_text, inner)
        self.assertNotEqual(outer_text, inner_text)
        self.assertIn('\n}\n执行动作:' + compact(action), outer_text)
        self.assertIn('\n执行动作:' + compact(action) + '\n}', inner_text)
        self.assertTrue(outer_text.startswith('回执:{\n'))
        self.assertTrue(inner_text.startswith('回执:{\n'))

    def test_help_single_operation_keeps_definition_and_extensions_in_scope(self):
        from scripts.core.command_help import command_help
        report = command_help(operation='task.prepare')
        report['operations'][0]['futureMetadata'] = {'unknown': None, 'message': '保留新字段。'}
        untouched = deepcopy(report)
        text = render_model_text('capabilities.help', report)
        self.assert_smaller(text, report)
        self.assertEqual(report, untouched)
        definition = report['operations'][0]
        self.assertIn('操作:[{\n', text)
        self.assertIn('工具:' + compact(definition['name']), text)
        self.assertIn('用途:' + compact(definition['description']), text)
        self.assertIn('效果:' + compact(definition['effects']), text)
        self.assertIn('参数:' + compact(definition['inputSchema']), text)
        self.assertIn('调用:' + compact(definition['invocation']), text)
        self.assertIn('"futureMetadata":' + compact(definition['futureMetadata']), text)
        self.assertIn('\n}]\n', text)
        self.assertEqual(render_model_text('capabilities.help', report, view='full'), compact(report))

    def test_known_empty_blockers_and_missing_are_stated_without_inferring_review(self):
        report = {'ok': True, 'code': 'TASK_READY', 'missing': [], 'blockers': [],
                  'semanticReviewComplete': False, 'pendingContinuity': [], 'nextAction': None}
        text = render_model_text('task.prepare', report)
        self.assert_smaller(text, report)
        self.assertIn('缺项:无', text)
        self.assertIn('阻断:无', text)
        self.assertIn('语义审阅未完成。', text)
        self.assertIn('空列表:pendingContinuity', text)
        self.assertIn('null:nextAction', text)


if __name__ == '__main__':
    unittest.main()
