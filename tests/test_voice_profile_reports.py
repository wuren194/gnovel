"""Voice changes use the same frozen, approval-bound source reports as other plans."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import query_policy
from scripts.core.query import _size
from scripts.core.source_diff import markdown_change
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_source import source_fixture


class VoiceProfileReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        self.axes = {'narrativeDistance': '贴近人物所见', 'sentenceVariation': '长短交替',
                     'dialogueExposition': '对白承担信息', 'emotionLabeling': '少用情绪标签',
                     'somaticExpression': '动作带出感受', 'sceneTransition': '以动作衔接'}

    def preview(self, action='set', **kwargs):
        if action == 'set':
            kwargs = {'axes': self.axes, 'examples': ['他把钥匙放回桌上。'], 'applicability': '全书', **kwargs}
        else:
            kwargs = {'approvalActor': '测试作者', 'approvalEvidence': '已逐项核对本版六轴、示例及范围', **kwargs}
        return call_operation('source.voice-profile-preview', self.root, {
            'action': action, 'expectedRevision': load_structure(self.root)['revision'],
            'reason': '声纹差异回归', **kwargs})

    def commit(self, plan):
        return call_operation('source.commit', self.root, plan['nextAction']['arguments'])

    def diff(self, plan, **kwargs):
        return call_operation('source.change-report', self.root, {
            'operationId': plan['operationId'], 'section': 'diff', **kwargs})

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_set_and_approve_show_content_and_declaration_bound_to_plan(self):
        plan = self.preview()
        self.assertNotIn('proposedStructure', plan)
        self.assertEqual(plan['diffSummary'], {'changeCount': 1, 'recordCount': 1})
        row = plan['diff'][0]
        self.assertEqual(row['recordId'], self.source['projectId'])
        self.assertEqual(row['path'], ['workVoiceProfile'])
        self.assertIsNone(row['before'])
        self.assertEqual(row['after'], {'axes': self.axes, 'examples': ['他把钥匙放回桌上。'],
                                       'applicability': '全书', 'status': 'draft'})
        page = self.diff(plan)
        self.assertEqual(page['snapshot']['planHash'], plan['planHash'])
        self.assertEqual(page['items'], plan['diff'])
        self.commit(plan)
        approved = self.preview('approve')
        changes = {tuple(r['path']): r for r in self.diff(approved)['items']}
        self.assertEqual(changes[('workVoiceProfile', 'status')]['before'], 'draft')
        self.assertEqual(changes[('workVoiceProfile', 'status')]['after'], 'active')
        declaration = changes[('workVoiceProfile', 'approval')]
        self.assertIsNone(declaration['before'])
        self.assertEqual(declaration['after']['actorId'], '测试作者')
        self.assertEqual(declaration['after']['evidence'], '已逐项核对本版六轴、示例及范围')
        self.assertIs(declaration['after']['verifiedHuman'], False)
        self.commit(approved)
        current = call_operation('source.voice-profile', self.root, {})
        self.assertEqual(declaration['after']['profileHash'], current['profileHash'])
        self.assertEqual(self.diff(approved)['snapshot']['planHash'], approved['planHash'])
        self.assertEqual(self.diff(approved)['items'], approved['diff'])

    def test_edit_removes_approval_and_unchanged_draft_has_empty_diff(self):
        self.commit(self.preview())
        no_change = self.preview()
        self.assertEqual(no_change['diffSummary']['changeCount'], 0)
        self.assertEqual(self.diff(no_change)['items'], [])
        self.commit(self.preview('approve'))
        before = load_structure(self.root)['workVoiceProfile']
        changed = self.preview(axes={**self.axes, 'emotionLabeling': '通过选择表现情绪'},
                               examples=['他没有拿起钥匙。', '门外的雨停了。'], applicability='第一卷')
        rows = self.diff(changed)['items']
        by_path = {tuple(row['path']): row for row in rows}
        self.assertEqual(by_path[('workVoiceProfile', 'approval')]['before'], before['approval'])
        self.assertIsNone(by_path[('workVoiceProfile', 'approval')]['after'])
        self.assertEqual(by_path[('workVoiceProfile', 'status')]['after'], 'draft')
        self.assertIn(('workVoiceProfile', 'axes', 'emotionLabeling'), by_path)
        self.assertIn(('workVoiceProfile', 'examples'), by_path)
        self.assertIn(('workVoiceProfile', 'applicability'), by_path)
        self.commit(changed)
        self.assertEqual(call_operation('source.voice-profile', self.root, {})['epistemicRole'], 'unapproved_candidate')

    def test_repeated_reads_and_undo_keep_frozen_diff_without_reapproving(self):
        from scripts.core.structure_transaction import undo
        self.commit(self.preview())
        approval = self.preview('approve'); self.commit(approval)
        expected = deepcopy(approval['diff'])
        before = self.inventory()
        for _ in range(2):
            self.assertEqual(self.diff(approval)['items'], expected)
        self.assertEqual(before, self.inventory())
        undo(self.root, approval['operationId'], actor='test')
        self.assertEqual(call_operation('source.voice-profile', self.root, {})['profile']['status'], 'draft')
        self.assertEqual(self.diff(approval)['items'], expected)

    def test_long_profile_pages_losslessly_in_text_and_markdown(self):
        plan = self.preview(examples=['细节\r\n' * 1600])
        self.assertTrue(plan['responseCompacted'])
        self.assertLessEqual(_size(plan), query_policy()['maxBytes']['detail'])
        frozen = read_json(self.root / f'.govel/plans/{plan["operationId"]}.json')['diff']
        for view in ('text', 'markdown'):
            cursor, parts = None, []
            while True:
                page = self.diff(plan, view=view, limit=1, maxBytes=2500, **({'cursor': cursor} if cursor else {}))
                self.assertLessEqual(_size(page), 2500)
                self.assertEqual(page['snapshot']['planHash'], plan['planHash'])
                parts.extend(item['text'] for item in page['items'])
                if not page['hasMore']:
                    break
                cursor = page['nextCursor']
            from scripts.core.structure_store import canonical_json
            expected = ''.join(markdown_change(row) if view == 'markdown' else canonical_json(row) for row in frozen)
            self.assertEqual(''.join(parts), expected)

    def test_diff_tampering_and_absent_commit_approval_are_rejected(self):
        plan = self.preview()
        before = self.inventory()
        with self.assertRaises(StructureError):
            call_operation('source.commit', self.root, {**plan['nextAction']['arguments'], 'approve': False})
        self.assertEqual(before, self.inventory())
        path = self.root / f'.govel/plans/{plan["operationId"]}.json'
        stored = read_json(path)
        stored['diff'] = []
        write_json(path, stored)
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.diff(plan)
        self.assertEqual(caught.exception.code, 'PLAN_HASH_MISMATCH')
        with self.assertRaises(StructureError) as caught:
            self.commit(plan)
        self.assertEqual(caught.exception.code, 'PLAN_HASH_MISMATCH')
        self.assertEqual(before, self.inventory())
