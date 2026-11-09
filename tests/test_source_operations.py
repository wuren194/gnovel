from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.operations import new_operations, call_operation
from scripts.core.source_review import query_reviews
from scripts.core.structure_store import StructureError
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


class SourceOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source_fixture(self.root)

    def test_runtime_mutations_and_receipt_fields_exposed_to_clients(self):
        operations = {s.id: s for s in new_operations()}
        for name in ('source.review-submit', 'source.evidence-register', 'source.rule-issue-submit'):
            self.assertEqual(operations[name].effects, 'runtime-write')
            self.assertFalse(operations[name].tool()['annotations']['readOnlyHint'])
        schema = operations['source.review-submit'].input_schema['properties']['reviews']['items']
        self.assertIn('inputHash', schema['required'])
        self.assertIn('evidence', schema['required'])
        with self.assertRaises(StructureError):
            call_operation('source.review-submit', self.root, {'requestId': 'bad', 'reviews': [{'reviewed': True}]})

    def test_cli_and_stdio_mcp_return_same_review_material_without_writes(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        cli = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'source', 'reviews',
                              '--project-root', str(self.root)], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'query_source_reviews', 'arguments': {'projectRoot': str(self.root)}}}
        mcp = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                             input=json.dumps(request) + '\n', capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        output = json.loads(mcp.stdout.splitlines()[-1])
        actual = json.loads(output['result']['content'][0]['text'])
        self.assertEqual(actual, json.loads(cli.stdout))
        self.assertEqual(actual, query_reviews(self.root))
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
