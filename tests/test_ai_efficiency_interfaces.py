"""Phase-one acceptance: one contract, precise lookup, and equal transport validation."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.operations import call_operation, dispatch_new, operations, validate_arguments
from scripts.core.project import TOOLBOX_ROOT, current_schema_version, query_policy
from scripts.core.query import _size
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.summary_contract import load_summary
from scripts.core.summary_status import summary_freshness
from scripts.core.tool_catalog import list_capabilities
from scripts.novelctl import build_parser
from scripts.story_summarizer import _commit_summary, _prepare_context, StorySummarizerError
from scripts.mcp_server import handle
from tests.fixtures_current import create_schema5_project


class InterfaceConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_summary_prepare_commit_all_readers_agree_and_preserve_old_on_bad_versions(self):
        fixture = create_schema5_project(self.root)
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        prepared = _prepare_context(self.root, view='model')
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        payload = deepcopy(prepared['inputSkeleton'])
        self.assertEqual(payload['schemaVersion'], current_schema_version())
        payload.update(storySummary='因果摘要', plotLine=[{'chapterId': fixture['chapterIds'][0], 'summary': '推进'}])
        path = self.root / 'summary.json'; path.write_text(json.dumps(payload))
        _commit_summary(self.root, str(path))
        saved = (self.root / '.govel/aux/story-summary.json').read_bytes()
        self.assertTrue(_prepare_context(self.root)['upToDate'])
        self.assertFalse(summary_freshness(self.root, load_structure(self.root))['stale'])
        from scripts.core.context_builder import _load_story_summary
        store = type('Store', (), {'root': self.root})()
        self.assertEqual(_load_story_summary(store), load_summary(self.root))
        for version in (0, -1, True, str(current_schema_version()), float(current_schema_version())):
            with self.subTest(version=version):
                path.write_text(json.dumps({**payload, 'schemaVersion': version}))
                with self.assertRaises(StorySummarizerError):
                    _commit_summary(self.root, str(path))
                self.assertEqual(saved, (self.root / '.govel/aux/story-summary.json').read_bytes())

    def test_exact_operation_lookup_is_bounded_and_matches_registered_schema(self):
        spec = next(s for s in operations() if s.id == 'workflow.patch-candidate')
        result = list_capabilities(operation=spec.id, view='audit')
        self.assertEqual(len(result['operations']), 1)
        row = result['operations'][0]
        self.assertEqual(row['inputSchema'], spec.input_schema)
        self.assertEqual(row['effects'], 'candidate-write')
        self.assertFalse(row['annotations']['readOnlyHint'])
        self.assertTrue(row['examples'])
        for example in row['examples']:
            validate_arguments(spec.input_schema, example)
        budgets = json.loads((TOOLBOX_ROOT / 'tests/fixtures/model_efficiency_budgets.json').read_text())
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), budgets['singleOperationCharacters'])
        self.assertEqual(result['scripts'], [])
        with self.assertRaises(StructureError) as caught:
            list_capabilities(operation='not.a.real.operation')
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_FOUND')

    def test_single_operation_cli_and_mcp_equal(self):
        arguments = {'operation': 'workflow.patch-candidate'}
        expected = list_capabilities(**arguments)
        proc = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project',
                               'tools', 'list', '--operation', arguments['operation']], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), expected)
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'list_capabilities', 'arguments': arguments}})['result']
        self.assertFalse(response['isError'])
        self.assertEqual(response['structuredContent'], expected)

    def test_field_migration_remains_discoverable_within_the_original_response_budget(self):
        spec = next(s for s in operations() if s.id == 'source.proposal-batch-preview')
        budget = query_policy()['maxBytes']['detail']
        for query in ('批量修订设定', 'batch update canon', spec.id):
            with self.subTest(query=query):
                result = list_capabilities(query=query)
                self.assertLessEqual(_size(result), budget)
                row = next(row for row in result['operations'] if row['id'] == spec.id)
                self.assertEqual(row['inputSchema'], spec.input_schema)
        exact = list_capabilities(operation=spec.id)
        self.assertLessEqual(_size(exact), budget)
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'list_capabilities', 'arguments': {'query': spec.id}}})['result']
        self.assertFalse(response['isError'])
        self.assertEqual(response['structuredContent'], list_capabilities(query=spec.id))

    def test_nested_invalid_fields_rejected_before_handler_with_item_paths(self):
        spec = next(s for s in operations() if s.id == 'workflow.patch-candidate')
        arguments = {'patches': [{'issueId': 'one', 'original': {'text': 'old'}, 'replacement': 'new'}]}
        validate_arguments(spec.input_schema, arguments)
        for value in ({'issueId': 'one', 'original': {'text': 'old'}},
                      {'issueId': 'one', 'original': {'text': 'old', 'start': 2}, 'replacement': 'new'},
                      {'issueId': 'one', 'original': {'text': 'old'}, 'replacement': False}):
            with self.subTest(value=value), self.assertRaises(StructureError) as caught:
                validate_arguments(spec.input_schema, {'patches': [value]})
            self.assertIn('arguments.patches[0]', str(caught.exception))
            self.assertEqual(caught.exception.code, 'ARGUMENTS_INVALID')

    def test_cli_distinguishes_explicit_null_and_missing(self):
        spec = next(s for s in operations() if s.id == 'source.content-proposal-preview')
        argv = ['project', *spec.cli, '--session-ref', '{}', '--reason', 'reason', '--evidence', '[]',
                '--approval-actor', 'author', '--approval-evidence', 'explicit', '--new-record', 'null']
        args = build_parser().parse_args(argv)
        from dataclasses import replace
        capture = replace(spec, handler=lambda root, arguments: arguments)
        with patch('scripts.core.operations.new_operations', return_value=[capture]):
            result = dispatch_new(args)
        self.assertIn('newRecord', result)
        self.assertIsNone(result['newRecord'])
        self.assertNotIn('expectedOldRecord', result)

    def test_mcp_rejects_invalid_outer_arguments_consistently(self):
        for arguments in ([], {'projectRoot': 12, 'task': 'editor'}):
            response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                               'params': {'name': 'prepare_task', 'arguments': arguments}})['result']
            self.assertTrue(response['isError'])
            self.assertEqual(response['structuredContent']['code'], 'ARGUMENTS_INVALID')

    def test_combinators_bounds_and_array_indexes_have_real_consumers(self):
        schema = {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                  'items': {'oneOf': [{'type': 'integer', 'minimum': 1}, {'type': 'string', 'minLength': 2}]}}
        validate_arguments(schema, [1, 'ab'])
        for value in ([], [0], ['a'], [True], [1, 1]):
            with self.subTest(value=value), self.assertRaises(StructureError):
                validate_arguments(schema, value)


if __name__ == '__main__':
    unittest.main()
