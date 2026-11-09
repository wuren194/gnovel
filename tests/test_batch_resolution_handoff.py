"""Batch resolution hands exact machine bindings to the original planning gate."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT, query_policy
from scripts.core.source_content_proposals import resolve_record
from scripts.core.source_resolution_batch import resolve_content_batch
from scripts.core.source_review import source_snapshot
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, validate_structure
from scripts.core.transport_cache import _directory
from tests.fixtures_current import initialize_project


class BatchResolutionHandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = initialize_project(self.root)
        self.source['fieldDefinitions'].append({'fieldId': 'FD-PLAN', 'key': 'plot.plan', 'label': 'Plan',
                                                'valueType': 'topic', 'appliesTo': ['project']})
        self.source['attributes'].append({'attributeId': 'AT-PLAN', 'ownerId': self.source['projectId'],
                                          'fieldId': 'FD-PLAN', 'values': []})
        self.ids = ['SF-PLAN-A', 'SF-PLAN-B']
        self.source['fragments'] = [{'fragmentId': rid, 'ownerId': self.source['projectId'], 'order': i,
                                    'contentRole': 'plan', 'contentBindings': ['AT-PLAN'],
                                    'text': 'Alpha guards the gate.\r\n' + 'Original paragraph.\r\n' * 1200}
                                   for i, rid in enumerate(self.ids)]
        validate_structure(self.source); write_json(self.root / '.gnovel/structure.json', self.source)

    def resolve(self, **options):
        return call_operation('source.content-resolve-batch', self.root,
                              {'kind': 'planning', 'recordIds': self.ids, **options})

    def changes(self):
        return [{'action': 'patch', 'collection': 'fragments', 'id': rid,
                 'patch': [{'op': 'text', 'path': ['text'], 'original': 'guards', 'replacement': 'leaves'}]}
                for rid in self.ids]

    def preview(self, reference, **options):
        return call_operation('source.change-preview', self.root, {'requestId': 'batch-edit',
            'changes': self.changes(), 'reason': 'Explicit fixture edit', 'resolutionRef': reference, **options})

    def test_one_source_snapshot_and_cursor_reuses_frozen_bindings(self):
        with patch('scripts.core.source_resolution_batch.source_snapshot', wraps=source_snapshot) as reader:
            first = self.resolve(limit=1, maxBytes=3000)
            second = self.resolve(limit=1, maxBytes=3000, cursor=first['nextCursor'])
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(first['resolutionRef'], second['resolutionRef'])
        self.assertFalse(first['approvalGranted'])
        for row in first['items'] + second['items']:
            single = call_operation('source.content-resolve', self.root, {
                'recordId': row['recordId'], 'kind': 'planning', 'view': 'model'})
            self.assertEqual({k: single[k] for k in row}, row)
        for page in (first, second):
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, indent=2, sort_keys=True).encode()) + 1, 3000)

    def test_reference_replaces_revision_and_hash_copying_but_not_approval(self):
        resolved = self.resolve()
        before = (self.root / '.gnovel/structure.json').read_bytes()
        plan = self.preview(resolved['resolutionRef'])
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)
        with self.assertRaises(StructureError):
            commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=False)
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        for old, new in zip(self.source['fragments'], read_json(self.root / '.gnovel/structure.json')['fragments']):
            self.assertEqual(new, {**old, 'text': old['text'].replace('guards', 'leaves')})

    def test_retry_after_cache_loss_uses_existing_request_not_a_new_binding(self):
        reference = self.resolve()['resolutionRef']; plan = self.preview(reference)
        (_directory() / (reference.replace(':', '-') + '.json')).unlink()
        self.assertEqual(self.preview(reference)['planHash'], plan['planHash'])
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertTrue(self.preview(reference)['alreadyCommitted'])
        with self.assertRaises(StructureError):
            self.preview(reference, reason='Different request')
        with self.assertRaises(StructureError):
            self.preview(reference, requestId='new-request')

    def test_unknown_wrong_kind_or_unselected_targets_do_not_gain_bindings(self):
        result = self.resolve(recordIds=[self.ids[0], 'SF-MISSING'])
        self.assertFalse(result['allResolved'])
        self.assertNotIn('recordHash', result['items'][1])
        with self.assertRaises(StructureError):
            self.preview(result['resolutionRef'])
        other = self.resolve(kind='fact')
        with self.assertRaises(StructureError):
            self.preview(other['resolutionRef'])
        reference = self.resolve()['resolutionRef']
        for options in ({'baseRevision': 100}, {'changes': [{'action': 'delete', 'collection': 'fragments', 'id': self.ids[0]}]},
                        {'changes': [{**self.changes()[0], 'expectedRecordHash': 'wrong'}]}):
            with self.subTest(options=options), self.assertRaises(StructureError):
                self.preview(reference, **options)

    def test_source_authorization_query_and_budget_drift_are_rejected(self):
        first = self.resolve(limit=1)
        for options in ({'limit': 2}, {'recordIds': list(reversed(self.ids))}):
            with self.assertRaises(StructureError):
                self.resolve(cursor=first['nextCursor'], **options)
        agents = self.root / 'AGENTS.md'
        agents.write_text((agents.read_text() if agents.exists() else '') + '\nChanged instruction.\n')
        with self.assertRaises(StructureError):
            self.preview(first['resolutionRef'])
        with self.assertRaises(StructureError):
            self.resolve(limit=1, cursor=first['nextCursor'])

    def test_late_source_change_during_resolution_never_returns_a_mixed_snapshot(self):
        changed = False
        def racing(*args, **kwargs):
            nonlocal changed
            result = resolve_record(*args, **kwargs)
            if not changed:
                changed = True
                self.source['bookName'] += ' external'
                write_json(self.root / '.gnovel/structure.json', self.source)
            return result
        with patch('scripts.core.source_resolution_batch.resolve_record', side_effect=racing), self.assertRaises(StructureError):
            self.resolve()
        self.assertTrue(read_json(self.root / '.gnovel/structure.json')['bookName'].endswith(' external'))

    def test_batch_bound_is_a_real_policy_consumer(self):
        policy = deepcopy(query_policy()); policy['maxLimit'] = 1
        with patch('scripts.core.source_resolution_batch.query_policy', return_value=policy), self.assertRaises(StructureError):
            resolve_content_batch(self.root, kind='planning', recordIds=self.ids)
        for options in ({'recordIds': self.ids * 2}, {'recordIds': []}, {'maxBytes': 1}, {'limit': True}):
            with self.subTest(options=options), self.assertRaises(StructureError):
                self.resolve(**options)

    def test_foreign_reference_and_unknown_cache_format_are_rejected(self):
        result = self.resolve(limit=1)
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp).resolve()
            initialize_project(other)
            with self.assertRaises(StructureError):
                call_operation('source.change-preview', other, {'requestId': 'foreign', 'resolutionRef': result['resolutionRef'],
                               'changes': self.changes(), 'reason': 'Not transferable'})
        with patch('scripts.core.source_resolution_batch.get', return_value={'version': 2}), self.assertRaises(StructureError) as caught:
            self.resolve(limit=1, cursor=result['nextCursor'])
        self.assertEqual(caught.exception.code, 'CONTENT_RESOLUTION_INVALID')

    def test_discovery_fits_existing_budget_and_intent_is_consumed(self):
        from scripts.core.project import tool_discovery_policy
        from scripts.mcp_server import handle
        visible = {row['name'] for row in handle({'id': 1, 'method': 'tools/list'})['result']['tools']}
        for query in ('batch resolve records', '批量解析规划片段绑定', '批量解析多个规划片段'):
            result = call_operation('capabilities.list', self.root, {'query': query})
            found = {row['id']: row for row in result['operations']}
            self.assertIn('source.content-resolve-batch', found)
            self.assertEqual(result['operations'][0]['id'], 'source.content-resolve-batch',
                             'explicit batch resolution must precede generic change/resolve matches within the budget')
            self.assertEqual(found['source.content-resolve-batch']['inputSchema'],
                call_operation('capabilities.list', self.root, {'operation': 'source.content-resolve-batch'})['operations'][0]['inputSchema'])
            self.assertIn(found['source.content-resolve-batch']['invocation']['tool'], visible)
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, indent=2).encode()) + 1,
                                 query_policy()['maxBytes']['detail'])
        policy = deepcopy(tool_discovery_policy())
        del policy['intents']['source.content-resolve-batch']
        with patch('scripts.core.tool_catalog.tool_discovery_policy', return_value=policy):
            result = call_operation('capabilities.list', self.root, {'query': 'batch resolve records'})
        self.assertEqual(result['matched'], 0)

    def test_preflight_requires_explicit_hashes_only_without_reference(self):
        from scripts.core.source_review import fingerprint
        args = {'requestId': 'preflight', 'reason': 'Schema only', 'changes': self.changes()}
        for bindings, valid in (({}, False), ({'baseRevision': self.source['revision']}, False),
                                ({'resolutionRef': 'not-a-real-reference'}, True)):
            result = call_operation('capabilities.validate', self.root, {
                'operation': 'source.change-preview', 'arguments': {**args, **bindings}})
            self.assertIs(result['schemaValid'], valid)
            self.assertFalse(result['executed'])
        explicit = [{**row, 'expectedRecordHash': fingerprint(self.source['fragments'][i])}
                    for i, row in enumerate(self.changes())]
        result = call_operation('capabilities.validate', self.root, {
            'operation': 'source.change-preview', 'arguments': {**args, 'changes': explicit,
                                                             'baseRevision': self.source['revision']}})
        self.assertTrue(result['schemaValid'])
        with self.assertRaises(StructureError):
            self.preview('not-a-real-reference')

    def test_reference_does_not_weaken_original_patch_guards(self):
        reference = self.resolve()['resolutionRef']
        for path, value in ((['ownerId'], 'other'), (['contentRole'], 'fact')):
            changes = self.changes()
            changes[0]['patch'] = [{'op': 'set', 'path': path, 'value': value}]
            with self.subTest(path=path), self.assertRaises(StructureError):
                self.preview(reference, changes=changes)
        with self.assertRaises(StructureError):
            self.preview(reference, changes=self.changes() * 2)
        self.assertFalse(list((self.root / '.gnovel/plans').glob('OP-*.json')))

    def test_cli_reference_can_be_consumed_by_mcp(self):
        from scripts.mcp_server import handle
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source',
                              'content-resolve-batch', '--project-root', str(self.root), '--kind', 'planning',
                              '--record-ids', self.ids[0], '--record-ids', self.ids[1]], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        reference = json.loads(cli.stdout)['resolutionRef']
        reply = handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'preview_source_change', 'arguments': {
            'projectRoot': str(self.root), 'resolutionRef': reference, 'requestId': 'from-cli',
            'changes': self.changes(), 'reason': 'Explicit transport test'}}})['result']
        self.assertFalse(reply['isError'], reply)
