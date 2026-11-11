"""Current source identity types remain stable through actual source operations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.catalog_commands import preview_rename_entity
from scripts.core.project import source_model_policy
from scripts.core.source_changes import apply_changes
from scripts.core.source_ids import identity_pattern, identity_prefixes, new_id
from scripts.core.source_identity import source_identities
from scripts.core.structure_store import StructureError, validate_structure
from scripts.core.structure_transaction import commit
from tests.fixtures_source import source_fixture


class SourceIdentityTypesTests(unittest.TestCase):
    def test_registry_allocates_all_declared_types_with_unique_opaque_suffixes(self):
        identities = [new_id(kind) for kind in identity_prefixes()]
        self.assertEqual(len(identities), len(set(identities)))
        for kind in identity_prefixes():
            identity = new_id(kind)
            self.assertRegex(identity, identity_pattern(kind))
            self.assertEqual(len(identity.rsplit('-', 1)[-1]), source_model_policy()['generatedSuffixLength'])
        for role, prefix in source_model_policy()['fragmentPrefixes'].items():
            identity = new_id('fragment', content_role=role)
            self.assertTrue(identity.startswith(prefix + '-'))
            self.assertRegex(identity, identity_pattern('fragment'))

    def test_prefix_and_entity_type_must_agree(self):
        source = source_fixture()
        source['entities'][0]['entityType'] = 'prop'
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        self.assertIn('ENTITY_ID_TYPE_MISMATCH', {row['code'] for row in caught.exception.details['errors']})

    def test_invalid_identity_types_return_boundary_diagnostics(self):
        for identity in ({}, [], False, 7, None):
            with self.subTest(identity=identity):
                source = source_fixture()
                source['entities'][0]['entityId'] = identity
                with self.assertRaises(StructureError) as caught:
                    validate_structure(source)
                self.assertIn('INVALID_ID', {row['code'] for row in caught.exception.details['errors']})

    def test_fragment_role_cannot_change_under_its_identity(self):
        source = source_fixture()
        fragment = source['fragments'][0]
        fragment.update(contentRole='belief', perspectiveId='character-ALPHA')
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        self.assertIn('SOURCE_ID_ROLE_MISMATCH', {row['code'] for row in caught.exception.details['errors']})

    def test_english_role_ids_and_forward_references_are_tool_allocated(self):
        source = source_fixture()
        changes = [
            {'action': 'put', 'collection': 'fragments', 'record': {'fragmentId': '$knowledge',
             'ownerId': 'character-ALPHA', 'contentRole': 'belief', 'perspectiveId': 'character-ALPHA',
             'text': '他猜想这是一本旧书。', 'order': 12, 'contentBindings': ['attribute-LIFE'],
             'cognition': {'holderId': 'character-ALPHA', 'targetId': '$claim', 'attitude': 'suspects',
                           'acquiredAtId': '$event', 'basisIds': ['$claim', '$event']}}},
            {'action': 'put', 'collection': 'entities', 'record': {'entityId': '$event',
             'entityType': 'event', 'name': '查看封皮', 'timeOrdinal': 4}},
            {'action': 'put', 'collection': 'fragments', 'record': {'fragmentId': '$claim',
             'ownerId': 'character-ALPHA', 'contentRole': 'in_world_text', 'perspectiveId': 'character-ALPHA',
             'text': '旧诗集', 'order': 13, 'contentBindings': ['attribute-LIFE']}},
        ]
        proposed, identities = apply_changes(source, changes)
        self.assertTrue(identities['$knowledge'].startswith('belief-'))
        self.assertTrue(identities['$claim'].startswith('text-'))
        self.assertTrue(identities['$event'].startswith('event-'))
        record = next(row for row in proposed['fragments'] if row['fragmentId'] == identities['$knowledge'])
        self.assertEqual(record['cognition']['targetId'], identities['$claim'])
        self.assertEqual(record['cognition']['basisIds'], [identities['$claim'], identities['$event']])

    def test_entity_rename_keeps_every_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_fixture(root)
            before = source_identities(source)
            plan = preview_rename_entity(root, selector='character-ALPHA', new_name='林舟', keep_old_name_as_alias=True)
            commit(root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')
            from scripts.core.structure_store import load_structure
            renamed = load_structure(root)
            self.assertEqual(source_identities(renamed), before)
            self.assertEqual(renamed['entities'][0]['name'], '林舟')

    def test_absent_value_has_no_literal_but_zero_remains_a_known_value(self):
        source = source_fixture()
        value = source['attributes'][0]['values'][0]
        value.pop('value')
        value['state'] = 'absent'
        validate_structure(source)
        bad = deepcopy(source)
        bad['attributes'][0]['values'][0]['value'] = 0
        with self.assertRaises(StructureError):
            validate_structure(bad)
        value.update(state='known', value={'number': 0, 'unit': 'cm'})
        validate_structure(source)


if __name__ == '__main__':
    unittest.main()
