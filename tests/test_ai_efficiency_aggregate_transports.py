"""Aggregate/history declarations drive discovery, CLI and MCP together."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.core.operations import call_operation, operations, validate_arguments
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.tool_catalog import list_capabilities
from scripts.mcp_server import handle
from tests.test_source_plan import planning_source


class AggregateTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source, self.chapter = planning_source(self.root)

    def test_cli_and_mcp_equal_domain_operations(self):
        for operation, arguments, flags in (
            ('query.aggregate', {'kind': 'character-reviews', 'characterId': 'character-ALPHA'},
             ['--kind', 'character-reviews', '--character-id', 'character-ALPHA']),
            ('history.query', {'kind': 'evidence'}, ['--kind', 'evidence']),
            ('history.index-state', {}, []),
        ):
            spec = next(row for row in operations() if row.id == operation)
            expected = call_operation(operation, self.root, arguments)
            cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'),
                'project', *spec.cli, '--project-root', str(self.root), *flags], capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr + cli.stdout)
            self.assertEqual(json.loads(cli.stdout), expected)
            result = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': spec.name, 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
            self.assertFalse(result['isError'], result)
            self.assertEqual(result['structuredContent'], expected)

    def test_discovery_and_nested_schema_have_no_separate_transport_copy(self):
        for operation in ('query.aggregate', 'history.query', 'history.index-state', 'history.index-rebuild'):
            spec = next(row for row in operations() if row.id == operation)
            catalog = list_capabilities(operation=operation)['operations'][0]
            self.assertEqual(catalog['inputSchema'], spec.input_schema)
            self.assertEqual(catalog['effects'], 'derived-write' if operation.endswith('rebuild') else 'read')
            for example in catalog['examples']:
                validate_arguments(spec.input_schema, example)
            listed = handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {'operation': operation}})
            self.assertEqual(listed['result']['tools'], [spec.tool()])
        writer = list_capabilities(audience='writer', task='writer')
        self.assertFalse({'query.aggregate', 'history.query'} & {row['id'] for row in writer['operations']})

    def test_mcp_rejects_raw_sql_and_invalid_perspective_array(self):
        cases = [('query_project_history', {'sql': 'SELECT * FROM entries'}, 'arguments'),
                 ('query_domain_aggregates', {'kind': 'character-reviews',
                    'selection': {'knowledgeMode': 'perspective', 'perspectiveIds': [False]}},
                  'arguments.selection.perspectiveIds[0]')]
        for name, arguments, error_path in cases:
            result = handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': name, 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['code'], 'ARGUMENTS_INVALID')
            self.assertEqual(result['structuredContent']['details']['path'], error_path)

    def test_index_rebuild_is_explicit_and_never_writes_a_domain_receipt(self):
        from scripts.core.history_index import INDEX_PATH
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass  # Include the permanent lock anchor in the unchanged baseline.
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
            'name': 'rebuild_project_history_index', 'arguments': {'projectRoot': str(self.root)}}})['result']
        self.assertFalse(result['isError'], result)
        self.assertTrue(result['structuredContent']['derivedOnly'])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual({k: v for k, v in after.items() if k != Path(INDEX_PATH)}, before)
        self.assertTrue(call_operation('history.index-state', self.root, {})['current'])


if __name__ == '__main__':
    unittest.main()
