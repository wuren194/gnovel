"""Large planning bodies use existing patch gates without becoming Canon facts."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import write_json, read_json
from scripts.core.operations import call_operation
from scripts.core.source_review import fingerprint
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, validate_structure
from tests.fixtures_current import initialize_project


class PlanningPatchBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = initialize_project(self.root)
        self.source['fieldDefinitions'].append({'fieldId': 'FD-PLAN', 'key': 'plot.plan', 'label': 'Plan',
                                                'valueType': 'topic', 'appliesTo': ['project']})
        self.source['attributes'].append({'attributeId': 'AT-PLAN', 'ownerId': self.source['projectId'], 'fieldId': 'FD-PLAN', 'values': []})
        self.source['fragments'] = [{'fragmentId': 'SF-PLAN' + str(i), 'ownerId': self.source['projectId'],
            'text': 'Alpha guards the gate. ' + ('Long original paragraph.\r\n' * 1200 if i == 0 else ''),
            'order': i, 'contentRole': 'plan', 'contentBindings': ['AT-PLAN']} for i in range(2)]
        validate_structure(self.source); write_json(self.root / '.govel/structure.json', self.source)

    def changes(self):
        return [{'action': 'patch', 'collection': 'fragments', 'id': row['fragmentId'],
                 'expectedRecordHash': fingerprint(row),
                 'patch': [{'op': 'text', 'path': ['text'], 'original': 'guards', 'replacement': 'leaves'}]}
                for row in self.source['fragments']]

    def preview(self, changes=None):
        return call_operation('source.change-preview', self.root, {'changes': changes or self.changes(),
            'requestId': 'planning-batch', 'baseRevision': self.source['revision'], 'reason': 'Explicit synthetic update'})

    def test_large_resolver_returns_bound_reference_and_patch_without_full_record(self):
        with self.assertRaises(StructureError):
            call_operation('source.content-resolve', self.root, {'kind': 'planning', 'recordId': 'SF-PLAN0'})
        resolved = call_operation('source.content-resolve', self.root, {'kind': 'planning', 'recordId': 'SF-PLAN0', 'view': 'model'})
        self.assertNotIn('record', resolved)
        self.assertEqual(resolved['recordHash'], fingerprint(self.source['fragments'][0]))
        from scripts.core.transport_cache import get
        self.assertEqual(get(self.root, resolved['recordRef'], kind='source-record')['record'], self.source['fragments'][0])
        page = call_operation('task.snapshot', self.root, {'reference': resolved['recordRef'], 'maxBytes': 2000})
        self.assertTrue(page['hasMore'])

    def test_batch_matches_local_patches_and_preserves_other_bytes(self):
        preview = self.preview()
        with self.assertRaises(StructureError):
            commit_plan(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=False)
        commit_plan(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)
        current = read_json(self.root / '.govel/structure.json')
        for old, new in zip(self.source['fragments'], current['fragments']):
            self.assertEqual(new, {**old, 'text': old['text'].replace('guards', 'leaves')})

    def test_stale_duplicate_owner_and_fact_promotion_rejected(self):
        bad_hash = self.changes(); bad_hash[0]['expectedRecordHash'] = '0' * 64
        owner = self.changes(); owner[0]['patch'] = [{'op': 'set', 'path': ['ownerId'], 'value': 'other'}]
        fact = self.changes(); fact[0]['patch'] = [{'op': 'set', 'path': ['contentRole'], 'value': 'fact'}]
        for changes in (bad_hash, owner, fact, self.changes() * 2):
            with self.subTest(changes=changes[0]['patch']), self.assertRaises(StructureError):
                self.preview(changes)
        self.assertFalse(list((self.root / '.govel/plans').glob('OP-*.json')))
        self.source['fragments'][0]['contentRole'] = 'fact'
        write_json(self.root / '.govel/structure.json', self.source)
        with self.assertRaises(StructureError):
            self.preview()
