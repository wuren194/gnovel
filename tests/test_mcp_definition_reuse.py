"""Request-local spec reuse retains execution-time and next-request freshness."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import mcp_server
from scripts.core import static_cache

operations_module = importlib.import_module('scripts.core.operations')
ROOT = Path(__file__).resolve().parents[1]


class DefinitionReuseTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {'GOVEL_LOG_MODE': 'off'})
        patcher.start(); self.addCleanup(patcher.stop)
        static_cache._VALUES.clear()
        self.registry = ROOT / 'shared/registries/text-patterns.json'
        original = self.registry.read_bytes()
        self.addCleanup(self.registry.write_bytes, original)  # owned copy only

    def request(self, name='get_source_schema', arguments=None, request_id=1):
        return mcp_server.handle({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                                 'params': {'name': name, 'arguments': arguments or {}}})['result']

    def test_hot_request_has_one_presentation_scan_and_one_execution_scan(self):
        self.assertFalse(self.request()['isError'])
        with patch.object(static_cache, 'toolbox_signature', wraps=static_cache.toolbox_signature) as signature:
            second = self.request(request_id=2)
        self.assertFalse(second['isError'])
        self.assertEqual(signature.call_count, 2)

    def test_execution_uses_current_schema_even_when_presentation_spec_is_older(self):
        specs = operations_module.operations()
        chosen = next(spec for spec in specs if spec.name == 'get_source_schema')
        changed = replace(chosen, input_schema={**chosen.input_schema, 'required': ['newProof'],
            'properties': {**chosen.input_schema['properties'], 'newProof': {'type': 'string'}}})
        current = [changed if spec.id == chosen.id else spec for spec in specs]
        with patch.object(mcp_server, 'operations', return_value=specs), \
                patch.object(operations_module, 'operations', return_value=current):
            response = self.request()
        self.assertTrue(response['isError'])
        self.assertEqual(response['structuredContent']['code'], 'ARGUMENTS_INVALID')
        self.assertIn('newProof', json.dumps(response['structuredContent']))

    def test_non_mcp_spec_does_not_become_callable_by_reuse(self):
        # All public CLI operations are exposed. An explicit private fixture
        # still verifies that request-local reuse cannot grant MCP access.
        specs = operations_module.operations()
        handler = Mock()
        spec = replace(specs[0], id='test.private', name='private_test_tool',
                       cli=('internal', 'private'), mcp=False, handler=handler)
        private_catalog = [*specs, spec]
        with patch.object(mcp_server, 'operations', return_value=private_catalog), \
                patch.object(operations_module, 'operations', return_value=private_catalog):
            response = self.request(spec.name)
        self.assertTrue(response['isError'])
        self.assertEqual(response['structuredContent']['code'], 'TOOL_NOT_FOUND')
        handler.assert_not_called()

    def test_next_request_rebuilds_for_changed_bytes_restored_mtime_and_new_inode(self):
        with patch.object(operations_module, '_operations', wraps=operations_module._operations) as factory:
            self.assertFalse(self.request()['isError'])
            self.assertFalse(self.request()['isError'])
            self.assertEqual(factory.call_count, 1)
            state = self.registry.stat(); data = json.loads(self.registry.read_bytes())
            data['diagnosticPolicy']['proseQuality']['minScore'] = 71
            self.registry.write_text(json.dumps(data, ensure_ascii=False))
            os.utime(self.registry, ns=(state.st_atime_ns, state.st_mtime_ns))
            self.assertFalse(self.request()['isError'])
            self.assertEqual(factory.call_count, 2)
            # A separate inode with identical bytes is a definition identity change.
            replacement = self.registry.with_suffix('.temporary.json')
            replacement.write_bytes(self.registry.read_bytes()); os.replace(replacement, self.registry)
            self.assertFalse(self.request()['isError'])
            self.assertEqual(factory.call_count, 3)

    def test_build_drift_and_factory_failure_are_not_cached_as_success(self):
        original = operations_module._operations
        def drift():
            result = original()
            self.registry.write_bytes(self.registry.read_bytes() + b'\n')
            return result
        with patch.object(operations_module, '_operations', side_effect=drift):
            response = self.request()
        self.assertTrue(response['isError'])
        self.assertIn('changed while building', response['structuredContent']['error'])
        with patch.object(operations_module, '_operations', side_effect=ValueError('bad factory')) as factory:
            self.assertTrue(self.request()['isError']); self.assertTrue(self.request()['isError'])
            self.assertEqual(factory.call_count, 2)
        self.assertFalse(self.request()['isError'])

    def test_caller_mutation_and_concurrent_requests_do_not_share_mutable_specs(self):
        specs = operations_module.operations()
        spec = next(row for row in specs if row.name == 'get_source_schema')
        spec.input_schema['required'].append('injected')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda value: self.request(request_id=value), (1, 2)))
        self.assertTrue(all(not result['isError'] for result in results))
        self.assertEqual(results[0]['structuredContent'], results[1]['structuredContent'])


from tests.toolbox_isolation import install_isolation
install_isolation(globals())
