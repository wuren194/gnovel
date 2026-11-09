from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.canon_proposals import preview_canon_change, resolve_canon_target
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_source import source_fixture
from scripts.core.source_changes import preview_source_change


class CanonProposalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source_fixture(self.root)

    def proposal(self, ref, **changes):
        return preview_canon_change(self.root, sessionRef=ref, fieldPath='summary', expectedOldExists=False,
                                    expectedOldValue=None,
                                    newValue='曾经守过城门', reason='作者核对设定',
                                    evidence=[{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}],
                                    approvalActor='author', approvalEvidence='本地确认', **changes)

    def test_scoped_resolution_old_value_and_atomic_commit(self):
        resolved = resolve_canon_target(self.root, selector='人物甲', entityType='character')
        self.assertEqual(resolved['status'], 'resolved')
        ref = resolved['sessionRef']
        preview = self.proposal(ref)
        self.assertNotIn('summary', load_structure(self.root)['entities'][0])
        self.assertEqual(preview['parameters']['canonProposal']['expectedOldValue'], None)
        self.assertFalse(preview['parameters']['canonProposal']['approval']['verifiedHuman'])
        commit_plan(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)
        self.assertEqual(load_structure(self.root)['entities'][0]['summary'], '曾经守过城门')
        with self.assertRaises(StructureError) as caught:
            self.proposal(ref)
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')

    def test_unknown_target_and_wrong_old_value_fail_closed(self):
        self.assertEqual(resolve_canon_target(self.root, selector='不存在')['status'], 'not_found')
        self.assertNotIn('sessionRef', resolve_canon_target(self.root, selector='不存在'))
        ref = resolve_canon_target(self.root, selector='C-ALPHA')['sessionRef']
        with self.assertRaises(StructureError) as caught:
            preview_canon_change(self.root, sessionRef=ref, fieldPath='summary', expectedOldExists=False,
                                 expectedOldValue='错误旧值',
                                 newValue='新值', reason='测试', evidence=[{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}],
                                 approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'EXPECTED_OLD_VALUE_CONFLICT')
        with self.assertRaises(StructureError) as caught:
            preview_canon_change(self.root, sessionRef=ref, fieldPath='summary', expectedOldExists=True,
                                 expectedOldValue=None, newValue='新值', reason='测试',
                                 evidence=[{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}],
                                 approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'EXPECTED_OLD_VALUE_CONFLICT')
        with self.assertRaises(StructureError) as caught:
            preview_canon_change(self.root, sessionRef=ref, fieldPath='summary', expectedOldExists=False,
                                 expectedOldValue=None,
                                 newValue='新值', reason='测试', evidence=[{'fragmentId': 'SF-LIFE', 'quote': '不存在'}],
                                 approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'CANON_EVIDENCE_INVALID')
        from scripts.core.structure_store import save_structure
        source = load_structure(self.root)
        source['fragments'].append({'fragmentId': 'SF-UNRELATED', 'ownerId': 'C-BETA',
                                    'text': '乙走过城门。', 'order': 1, 'contentRole': 'fact',
                                    'contentBindings': ['AT-BETA-HEIGHT']})
        source['fragments'].append({'fragmentId': 'SF-PLAN-CLAIM', 'ownerId': 'C-ALPHA',
                                    'text': '将来可能守门。', 'order': 5, 'contentRole': 'plan',
                                    'contentBindings': ['AT-LIFE']})
        save_structure(self.root, source)
        new_ref = resolve_canon_target(self.root, selector='C-ALPHA')['sessionRef']
        with self.assertRaises(StructureError) as caught:
            preview_canon_change(self.root, sessionRef=new_ref, fieldPath='summary', expectedOldExists=False,
                                 expectedOldValue=None, newValue='新值', reason='测试',
                                 evidence=[{'fragmentId': 'SF-UNRELATED', 'quote': '乙走过城门。'}],
                                 approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'CANON_EVIDENCE_SCOPE_INVALID')
        with self.assertRaises(StructureError) as caught:
            preview_canon_change(self.root, sessionRef=new_ref, fieldPath='summary', expectedOldExists=False,
                                 expectedOldValue=None, newValue='新值', reason='测试',
                                 evidence=[{'fragmentId': 'SF-PLAN-CLAIM', 'quote': '将来可能守门。'}],
                                 approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'CANON_EVIDENCE_INVALID')

    def test_generic_source_change_cannot_bypass_entity_old_value_gate(self):
        source = load_structure(self.root)
        edited = {**source['entities'][0], 'name': '更名'}
        with self.assertRaises(StructureError) as caught:
            preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'entities', 'record': edited}],
                                  requestId='raw-rename', baseRevision=source['revision'], reason='绕过')
        self.assertEqual(caught.exception.code, 'CANON_PROPOSAL_REQUIRED')
        ref = resolve_canon_target(self.root, selector='C-ALPHA')['sessionRef']
        preview = preview_canon_change(self.root, sessionRef=ref, fieldPath='aliases', expectedOldExists=True,
                                       expectedOldValue=[],
                                       newValue=['甲'], reason='作者确认称呼',
                                       evidence=[{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}],
                                       approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(preview['operation'], 'canon_change_proposal')

    def test_chapter_scope_requires_registered_reference(self):
        from tests.test_source_plan import planning_source
        from scripts.core.structure_store import save_structure
        source, chapter = planning_source(self.root)
        source['references'] = [{'sourceId': chapter['chapterId'], 'targetId': 'C-ALPHA', 'kind': 'uses'}]
        save_structure(self.root, source)
        self.assertEqual(resolve_canon_target(self.root, selector='人物乙', chapterId=chapter['chapterId'])['status'], 'not_found')
        selected = resolve_canon_target(self.root, selector='人物甲', chapterId=chapter['chapterId'])
        self.assertEqual(selected['status'], 'resolved')
        self.assertIn('sessionRef', selected)
        preview = self.proposal(selected['sessionRef'])
        self.assertEqual(preview['parameters']['canonProposal']['sourceChapterId'], chapter['chapterId'])
        with self.assertRaises(StructureError) as caught:
            resolve_canon_target(self.root, selector='人物甲', chapterId='CH-MISSING')
        self.assertEqual(caught.exception.code, 'RESOLVER_SCOPE_UNKNOWN')
