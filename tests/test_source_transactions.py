from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.source_changes import preview_source_change
from scripts.core.source_impact import REVIEW_PATH
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit, undo
from tests.fixtures_source import source_fixture
from tests.fixtures_content_proposal import attribute_value_proposal


class SourceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture(self.root)
        (self.root / '正文').mkdir()
        (self.root / '正文/稿件.md').write_text('一米九的男人守在门口。')
        (self.root / '设定').mkdir()
        (self.root / '设定/人物设定.md').write_text('作者曾经保存的阅读文件。')

    def preview(self, *, key='change-1', height=185, base=None):
        return attribute_value_proposal(self.root, 'AT-HEIGHT', height)

    def commit(self, plan):
        return commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def test_source_and_pending_impact_commit_together_without_touching_prose_or_views(self):
        original = {p: p.read_bytes() for p in self.root.rglob('*.md')}
        plan = self.preview()
        self.assertEqual({r['path'] for r in plan['writeSet']}, {'.gnovel/structure.json', REVIEW_PATH})
        self.assertFalse((self.root / REVIEW_PATH).exists())
        self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], 190)
        self.assertIn('SF-HEIGHT', plan['impact']['reviewFragmentIds'])
        result = self.commit(plan)
        self.assertEqual(result['afterRevision'], 1)
        self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], 185)
        self.assertEqual(read_json(self.root / REVIEW_PATH)['items']['SF-HEIGHT']['status'], 'pending')
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*.md')}, original)
        self.assertFalse(result['impact']['proseWrite'])
        self.assertFalse(result['impact']['coverage']['proseScanned'])

    def test_idempotent_preview_and_commit_retry_keep_one_identity(self):
        changes = [{'action': 'put', 'collection': 'fragments', 'record': {
            'fragmentId': '$new-fragment', 'ownerId': 'C-ALPHA', 'text': '$new-fragment',
            'order': 5, 'contentRole': 'fact', 'contentBindings': ['AT-LIFE']}}]
        args = dict(changes=changes, requestId='new-fragment', baseRevision=0, reason='新增经历')
        plan = preview_source_change(self.root, **args)
        replay = preview_source_change(self.root, **args)
        self.assertEqual(replay['planHash'], plan['planHash'])
        rid = plan['parameters']['identityMap']['$new-fragment']
        self.commit(plan)
        receipt = preview_source_change(self.root, **args)
        self.assertTrue(receipt['alreadyCommitted'])
        actual = [r for r in load_structure(self.root)['fragments'] if r['fragmentId'] == rid]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]['text'], '$new-fragment', 'literal prose must not be interpolated as an ID')
        with self.assertRaises(StructureError) as error:
            preview_source_change(self.root, **{**args, 'reason': '改了请求内容'})
        self.assertEqual(error.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_idempotent_delete_retry_is_valid_after_target_is_gone(self):
        self.source['fragments'].append({'fragmentId': 'SF-DELETABLE-PLAN', 'ownerId': 'C-ALPHA',
                                         'text': '尚未实施的设想。', 'order': 9, 'contentRole': 'plan',
                                         'contentBindings': ['AT-LIFE']})
        write_json(self.root / '.gnovel/structure.json', self.source)
        args = dict(changes=[{'action': 'delete', 'collection': 'fragments', 'id': 'SF-DELETABLE-PLAN'}],
                    requestId='delete-fragment', baseRevision=0, reason='删除经历')
        plan = preview_source_change(self.root, **args)
        self.commit(plan)
        self.assertTrue(preview_source_change(self.root, **args)['alreadyCommitted'])

    def test_same_batch_forward_references_allocate_every_id_once(self):
        changes = [
            {'action': 'put', 'collection': 'fragments', 'record': {'fragmentId': '$text', 'ownerId': 'C-ALPHA',
             'text': '他喜欢独处。', 'order': 5, 'contentRole': 'fact', 'contentBindings': ['$attribute']}},
            {'action': 'put', 'collection': 'attributes', 'record': {'attributeId': '$attribute', 'ownerId': 'C-ALPHA',
             'fieldId': '$field', 'values': []}},
            {'action': 'put', 'collection': 'fieldDefinitions', 'record': {'fieldId': '$field', 'key': 'personality.preference',
             'label': '偏好', 'valueType': 'topic', 'appliesTo': ['character']}},
        ]
        plan = preview_source_change(self.root, changes=changes, requestId='batch', baseRevision=0, reason='明确偏好')
        self.assertEqual(len(plan['parameters']['identityMap']), 3)
        self.commit(plan)
        self.assertEqual(load_structure(self.root)['fragments'][-1]['contentBindings'],
                         [plan['parameters']['identityMap']['$attribute']])

    def test_same_batch_entity_relation_returns_typed_endpoint_impact(self):
        changes = [
            {'action': 'put', 'collection': 'relations', 'record': {
                'relationId': '$relationship', 'fromEntity': 'C-ALPHA',
                'toEntity': '$new-person', 'kind': 'protects'}},
            {'action': 'put', 'collection': 'entities', 'record': {
                'entityId': '$new-person', 'entityType': 'character', 'name': '新人物',
                'aliases': [], 'status': 'active'}},
        ]
        plan = preview_source_change(self.root, changes=changes, requestId='new-relation',
                                     baseRevision=0, reason='登记人物关系')
        identity = plan['parameters']['identityMap']
        self.assertEqual(plan['impact']['relationChanges'][0]['after'], {
            'fromEntity': 'C-ALPHA', 'toEntity': identity['$new-person'], 'kind': 'protects'})
        self.commit(plan)
        relation = load_structure(self.root)['relations'][0]
        self.assertEqual(relation['relationId'], identity['$relationship'])

    def test_dangling_delete_and_revision_race_write_nothing(self):
        before = (self.root / '.gnovel/structure.json').read_bytes()
        with self.assertRaises(StructureError):
            preview_source_change(self.root, changes=[{'action': 'delete', 'collection': 'attributes', 'id': 'AT-HEIGHT'}],
                                  requestId='bad-delete', baseRevision=0, reason='删除属性')
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)
        plan = self.preview()
        self.source['revision'] += 1; write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(StructureError) as error: self.commit(plan)
        self.assertEqual(error.exception.code, 'REVISION_CONFLICT')
        self.assertFalse((self.root / REVIEW_PATH).exists())

    def test_undo_restores_same_write_set(self):
        plan = self.preview(); self.commit(plan)
        undo(self.root, plan['operationId'], actor='test')
        source = load_structure(self.root)
        self.assertEqual(source['attributes'][0]['values'][0]['value']['number'], 190)
        self.assertEqual(source['revision'], 2)
        self.assertEqual(read_json(self.root / REVIEW_PATH)['items']['SF-HEIGHT']['status'], 'pending')
        self.assertTrue(list((self.root / '.gnovel/undo-receipts').glob('*.json')))
        self.assertEqual((self.root / '设定/人物设定.md').read_text(), '作者曾经保存的阅读文件。')

    def test_mid_write_failure_restores_source_and_pending_items(self):
        before = (self.root / '.gnovel/structure.json').read_bytes()
        plan = self.preview()
        def fail(phase, relative=None):
            if phase == 'write' and relative == '.gnovel/structure.json':
                raise OSError('simulated write failure')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(OSError): self.commit(plan)
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)
        self.assertFalse((self.root / REVIEW_PATH).exists())

    def test_batch_cannot_bypass_arc_graph_invariants(self):
        arc = deepcopy(self.source['arcs'][0]); arc['parentArcId'] = arc['arcId']
        with self.assertRaises(StructureError):
            preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'arcs', 'record': arc}],
                                  requestId='cycle', baseRevision=0, reason='非法父弧')
        self.assertEqual(load_structure(self.root)['arcs'][0]['parentArcId'], None)

    def test_malformed_batch_fields_return_diagnostics(self):
        for change in ({'action': 'put', 'collection': []}, {'action': {}, 'collection': 'fragments'},
                       {'action': 'put', 'collection': 'entities', 'record': {'entityType': {}}}):
            with self.subTest(change=change), self.assertRaises(StructureError):
                preview_source_change(self.root, changes=[change], requestId='bad-shape', baseRevision=0, reason='非法输入')

    def test_retry_does_not_return_plan_after_direct_source_edit(self):
        args = dict(changes=[{'action': 'put', 'collection': 'fragments', 'record': {
            'fragmentId': '$retry', 'ownerId': 'C-ALPHA', 'text': '新增经历。', 'order': 9,
            'contentRole': 'fact', 'contentBindings': ['AT-LIFE']}}],
            requestId='stale-retry', baseRevision=0, reason='新增经历')
        preview_source_change(self.root, **args)
        self.source['entities'][0]['name'] = '修改了人物名称'
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(StructureError) as error:
            preview_source_change(self.root, **args)
        self.assertEqual(error.exception.code, 'SOURCE_REQUEST_STALE')
