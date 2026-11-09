from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.source_changes import preview_source_change
from scripts.core.structure_commands import _base_plan, _write_plan
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_source import source_fixture


class CanonMutationGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source_fixture(self.root)

    def assert_blocked(self, proposed, code, operation='source_change', parameters=None):
        source = load_structure(self.root)
        plan = _base_plan(source, operation=operation, actor='app',
                          parameters=parameters or {}, impact={}, proposed=proposed)
        with self.assertRaises(StructureError) as caught:
            _write_plan(self.root, plan)
        self.assertEqual(caught.exception.code, code)

    def test_existing_entity_attribute_fact_and_relation_reject_handcrafted_plan(self):
        source = load_structure(self.root)
        source['relations'] = [{'relationId': 'REL-ALPHA-BETA', 'fromEntity': 'C-ALPHA',
                                'toEntity': 'C-BETA', 'kind': 'protects'}]
        from scripts.core.structure_store import save_structure
        save_structure(self.root, source)
        cases = [('entities', 'entityId', 'C-ALPHA', 'CANON_PROPOSAL_REQUIRED'),
                 ('attributes', 'attributeId', 'AT-HEIGHT', 'CONTENT_PROPOSAL_REQUIRED'),
                 ('fragments', 'fragmentId', 'SF-LIFE', 'CONTENT_PROPOSAL_REQUIRED'),
                 ('relations', 'relationId', 'REL-ALPHA-BETA', 'RELATION_PROPOSAL_REQUIRED')]
        for collection, key, rid, code in cases:
            for delete in (False, True):
                with self.subTest(collection=collection, delete=delete):
                    proposed = deepcopy(source)
                    rows = proposed[collection]
                    index = next(i for i, row in enumerate(rows) if row[key] == rid)
                    if delete:
                        rows.pop(index)
                    else:
                        rows[index]['status'] = 'archived'
                    self.assert_blocked(proposed, code)

    def test_fact_role_downgrade_and_delete_recreate_are_blocked(self):
        source = load_structure(self.root)
        proposed = deepcopy(source)
        proposed['fragments'][0]['contentRole'] = 'plan'
        self.assert_blocked(proposed, 'CONTENT_PROPOSAL_REQUIRED')
        proposed = deepcopy(source)
        proposed['fragments'] = [row for row in proposed['fragments'] if row['fragmentId'] != 'SF-LIFE']
        proposed['fragments'].append({**source['fragments'][-1], 'fragmentId': 'SF-REPLACEMENT'})
        self.assert_blocked(proposed, 'CONTENT_PROPOSAL_REQUIRED')

    def test_existing_plan_fragment_cannot_be_promoted_by_overwrite(self):
        source = load_structure(self.root)
        source['fragments'].append({'fragmentId': 'SF-PLANNED', 'ownerId': 'C-ALPHA',
                                    'text': '未来可能发生。', 'order': 7,
                                    'contentBindings': ['AT-LIFE'], 'contentRole': 'plan'})
        from scripts.core.structure_store import save_structure
        save_structure(self.root, source)
        proposed = deepcopy(source)
        proposed['fragments'][-1]['contentRole'] = 'fact'
        self.assert_blocked(proposed, 'FACT_PROMOTION_REVIEW_REQUIRED')
        with self.assertRaises(StructureError) as caught:
            preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'fragments',
                                                       'record': proposed['fragments'][-1]}],
                                  requestId='promote', baseRevision=source['revision'], reason='test')
        self.assertEqual(caught.exception.code, 'FACT_PROMOTION_REVIEW_REQUIRED')

    def test_referenced_field_definition_all_fields_fail_closed(self):
        source = load_structure(self.root)
        for field in ('key', 'valueType', 'appliesTo', 'label', 'description'):
            with self.subTest(field=field):
                proposed = deepcopy(source)
                proposed['fieldDefinitions'][0][field] = 'changed' if field != 'appliesTo' else ['prop']
                self.assert_blocked(proposed, 'FIELD_DEFINITION_MIGRATION_REQUIRED')
        proposed = deepcopy(source)
        proposed['fieldDefinitions'].pop(0)
        self.assert_blocked(proposed, 'FIELD_DEFINITION_MIGRATION_REQUIRED')

    def test_generic_preview_cannot_overwrite_relation_or_field_definition(self):
        source = load_structure(self.root)
        source['relations'] = [{'relationId': 'REL-ALPHA-BETA', 'fromEntity': 'C-ALPHA',
                                'toEntity': 'C-BETA', 'kind': 'protects'}]
        from scripts.core.structure_store import save_structure
        save_structure(self.root, source)
        changes = [({'action': 'put', 'collection': 'relations',
                     'record': {**source['relations'][0], 'kind': 'trusts'}}, 'RELATION_PROPOSAL_REQUIRED'),
                   ({'action': 'delete', 'collection': 'relations', 'id': 'REL-ALPHA-BETA'},
                    'RELATION_PROPOSAL_REQUIRED'),
                   ({'action': 'put', 'collection': 'fieldDefinitions',
                     'record': {**source['fieldDefinitions'][0], 'label': '改名'}},
                    'FIELD_DEFINITION_MIGRATION_REQUIRED'),
                   ({'action': 'delete', 'collection': 'fieldDefinitions', 'id': 'FD-HEIGHT'},
                    'FIELD_DEFINITION_MIGRATION_REQUIRED')]
        for i, (change, code) in enumerate(changes):
            with self.subTest(i=i), self.assertRaises(StructureError) as caught:
                preview_source_change(self.root, changes=[change], requestId=f'blocked-{i}',
                                      baseRevision=source['revision'], reason='test')
            self.assertEqual(caught.exception.code, code)

    def test_operation_name_cannot_hide_second_canon_change(self):
        source = load_structure(self.root)
        proposed = deepcopy(source)
        proposed['entities'][0]['summary'] = 'changed'
        proposed['entities'][1]['summary'] = 'also changed'
        self.assert_blocked(proposed, 'CANON_PROPOSAL_SCOPE_INVALID', operation='canon_change_proposal',
                            parameters={'canonProposal': {'entityId': 'C-ALPHA', 'fieldPath': 'summary',
                                'expectedOldExists': False, 'expectedOldValue': None, 'newValue': 'changed',
                                'reason': 'test', 'evidence': [{'fragmentId': 'SF-LIFE',
                                                              'quote': '他独自守过城门。'}],
                                'approval': {'actorId': 'author', 'evidence': 'declared',
                                             'verifiedHuman': False}}})

    def test_proposal_cannot_reorder_other_source_rows(self):
        source = load_structure(self.root)
        proposed = deepcopy(source)
        proposed['entities'][0]['summary'] = 'changed'
        proposed['attributes'].reverse()
        proposal = {'entityId': 'C-ALPHA', 'fieldPath': 'summary',
                    'expectedOldExists': False, 'expectedOldValue': None, 'newValue': 'changed',
                    'reason': 'test', 'evidence': [{'fragmentId': 'SF-LIFE',
                                                  'quote': '他独自守过城门。'}],
                    'approval': {'actorId': 'author', 'evidence': 'declared',
                                 'verifiedHuman': False}}
        self.assert_blocked(proposed, 'CANON_PROPOSAL_SCOPE_INVALID',
                            operation='canon_change_proposal', parameters={'canonProposal': proposal})

    def test_schema_downgrade_and_direct_save_do_not_bypass_guard(self):
        source = load_structure(self.root)
        proposed = deepcopy(source)
        proposed['schemaVersion'] = 1
        self.assert_blocked(proposed, 'UNSUPPORTED_PROJECT_VERSION')
        proposed = deepcopy(source)
        proposed['entities'][0]['summary'] = 'direct overwrite'
        from scripts.core.structure_store import save_structure
        with self.assertRaises(StructureError) as caught:
            save_structure(self.root, proposed)
        self.assertEqual(caught.exception.code, 'CANON_PROPOSAL_REQUIRED')


    def test_entity_refactor_exemption_stays_limited_to_display_fields(self):
        """The entity refactor exemption covers `name` / `aliases` / `status` and nothing else.

        A refactor operation name must not become a way past Canon: hiding behind
        `refactor_rename_entity`, every one of these still needs a reviewed proposal.
        """
        source = load_structure(self.root)
        source['relations'] = [{'relationId': 'REL-ALPHA-BETA', 'fromEntity': 'C-ALPHA',
                                'toEntity': 'C-BETA', 'kind': 'protects', 'status': 'active', 'notes': ''}]
        from scripts.core.structure_store import save_structure
        save_structure(self.root, source)

        def touch(collection, key, rid, field, value):
            proposed = deepcopy(source)
            row = next(row for row in proposed[collection] if row[key] == rid)
            row[field] = value
            return proposed

        cases = (
            ('attributes', 'attributeId', 'AT-HEIGHT', 'values', [{'valueId': 'FV-HEIGHT', 'value': '一米九二',
                                                                   'state': 'known'}]),
            ('fragments', 'fragmentId', 'SF-HEIGHT', 'text', '他身高两米。'),
            ('fragments', 'fragmentId', 'SF-HEIGHT', 'contentRole', 'plan'),
            ('relations', 'relationId', 'REL-ALPHA-BETA', 'kind', 'resents'),
            ('entities', 'entityId', 'C-ALPHA', 'summary', 'rewritten through a rename plan'),
            ('entities', 'entityId', 'C-ALPHA', 'entityType', 'prop'),
        )
        for collection, key, rid, field, value in cases:
            with self.subTest(collection=collection, field=field):
                self.assert_blocked(touch(collection, key, rid, field, value),
                                    'CANON_PROPOSAL_REQUIRED', operation='refactor_rename_entity')

        # A field definition that is still referenced by an attribute has its own,
        # stricter gate; an unreferenced one falls through to the Canon guard.
        self.assert_blocked(touch('fieldDefinitions', 'fieldId', 'FD-HEIGHT', 'label', '身高（改）'),
                            'FIELD_DEFINITION_MIGRATION_REQUIRED', operation='refactor_rename_entity')
        added = deepcopy(source)
        added['fieldDefinitions'].append({'fieldId': 'FD-NEW', 'key': 'appearance.new',
                                          'label': '新字段', 'valueType': 'text',
                                          'appliesTo': ['character']})
        self.assert_blocked(added, 'CANON_PROPOSAL_REQUIRED', operation='refactor_rename_entity')

    def test_entity_refactor_exemption_still_allows_what_it_grants(self):
        """The exemption is narrow, but it is real: a display rename must go through."""
        from scripts.core.refactor import commit_refactor, preview_rename_entity
        preview = preview_rename_entity(self.root, selector='C-ALPHA', new_name='人物壹',
                                        keep_old_name_as_alias=True)
        commit_refactor(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                        approved=True, actor='test')
        entity = next(row for row in load_structure(self.root)['entities'] if row['entityId'] == 'C-ALPHA')
        self.assertEqual((entity['name'], entity['aliases']), ('人物壹', ['人物甲']))


if __name__ == '__main__':
    unittest.main()
