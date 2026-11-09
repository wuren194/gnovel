"""Review projections must expose JSON type changes that Python treats as equal."""
from copy import deepcopy
import unittest

from scripts.core.source_diff import source_diff
from scripts.core.source_impact import identity_changes
from scripts.core.structure_store import validate_structure
from tests.fixtures_source import source_fixture


class SourceDiffValueTypeTests(unittest.TestCase):
    def test_boolean_and_numeric_changes_remain_visible_in_diff_and_identity_impact(self):
        for old_value, new_value, new_type in ((1, True, 'boolean'), (190, 190.0, 'number')):
            with self.subTest(new_type=new_type, new_value=new_value):
                old = source_fixture()
                field = next(row for row in old['fieldDefinitions'] if row['fieldId'] == 'FD-SKIN')
                field['valueType'] = 'number'
                attribute = next(row for row in old['attributes'] if row['attributeId'] == 'AT-SKIN')
                attribute['values'][0]['value'] = old_value
                new = deepcopy(old)
                next(row for row in new['fieldDefinitions'] if row['fieldId'] == 'FD-SKIN')['valueType'] = new_type
                next(row for row in new['attributes'] if row['attributeId'] == 'AT-SKIN')['values'][0]['value'] = new_value
                validate_structure(old)
                validate_structure(new)
                values = [row for row in source_diff(old, new) if row['recordId'] == 'AT-SKIN']
                self.assertEqual(len(values), 1)
                self.assertEqual(values[0]['path'], ['values', 0, 'value'])
                self.assertIs(type(values[0]['before']), type(old_value))
                self.assertIs(type(values[0]['after']), type(new_value))
                self.assertTrue({'AT-SKIN', 'FV-SKIN'} <= set(identity_changes(old, new)['changedIds']))

    def test_dictionary_key_order_does_not_create_a_false_change(self):
        old = source_fixture()
        new = deepcopy(old)
        new['attributes'][0] = dict(reversed(list(new['attributes'][0].items())))
        self.assertEqual(source_diff(old, new), [])
        self.assertEqual(identity_changes(old, new)['changedIds'], [])


if __name__ == '__main__':
    unittest.main()
