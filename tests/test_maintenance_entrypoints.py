"""Real maintenance wording must reach callable tools, not duplicate scripts."""
import json
from pathlib import Path
import random
import shlex
import tempfile
import unittest

from scripts.core.cli_examples import command_example
from scripts.core.jsonio import write_json
from scripts.core.operations import call_operation, operations
from scripts.core.source_search_match import literal_hits
from scripts.core.structure_store import StructureError
from scripts.mcp_server import handle
from scripts.novelctl import build_parser
from tests.fixtures_source import source_fixture


class MaintenanceEntrypointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)

    def test_real_phrases_resolve_to_visible_invocations(self):
        visible = {row['name'] for row in handle({'id': 1, 'method': 'tools/list'})['result']['tools']}
        for query in ('复核回执', '把修改写回设定', '改设定里的一句话', '提交后核对改动结果', '检查批量输入参数'):
            result = call_operation('capabilities.list', self.root, {'query': query})
            self.assertTrue(result['operations'], query)
            for row in result['operations']:
                self.assertIn(row['invocation']['tool'], visible, row['id'])
        for operation in ('source.maintenance-checklist', 'source.proposal-batch-preview', 'capabilities.validate'):
            row = call_operation('capabilities.list', self.root, {'operation': operation})['operations'][0]
            self.assertIn(row['invocation']['tool'], visible)

    def test_array_example_roundtrips_through_real_parser(self):
        spec = next(s for s in operations() if s.id == 'source.search')
        command = command_example(spec, {'terms': ['门', '钥匙'], 'view': 'model'})
        args = build_parser().parse_args(shlex.split(command)[2:])
        self.assertEqual(args.terms, ['门', '钥匙'])
        self.assertEqual(command.count('--terms'), 2)
        review = next(s for s in operations() if s.id == 'source.review-submit')
        self.assertIn('evidence pack', review.description)

    def test_suspicious_array_requires_explicit_literal_intent(self):
        with self.assertRaises(StructureError) as caught:
            call_operation('source.search', self.root, {'terms': ['["门"]']})
        self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_TERMS_FORMAT')
        page = call_operation('source.search', self.root, {'terms': ['["门"]'], 'literalTerms': True})
        self.assertEqual(page['items'], [])

    def test_owner_filter_is_unique_and_never_widens_selection(self):
        args = {'terms': ['他'], 'ownerSelectors': ['人物甲']}
        report = call_operation('source.search', self.root, args)
        self.assertTrue(report['items'])
        self.assertEqual({row['ownerId'] for row in report['items']}, {'C-ALPHA'})
        with self.assertRaises(StructureError):
            call_operation('source.search', self.root, {**args, 'selection': {'ownerIds': ['C-BETA']}})
        self.source['volumes'][0]['title'] = '人物甲'
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(StructureError) as caught:
            call_operation('source.search', self.root, {'terms': ['他'], 'ownerSelectors': ['人物甲']})
        self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_OWNER_AMBIGUOUS')

    def test_compact_hits_keep_bindings_in_a_readable_snapshot(self):
        full = call_operation('source.search', self.root, {'terms': ['他']})
        small = call_operation('source.search', self.root, {'terms': ['他'], 'view': 'model'})
        from scripts.core.transport_cache import get
        saved = get(self.root, small['searchRef'], kind='source-search')
        self.assertEqual(saved['items'], full['items'])
        self.assertNotIn('locator', small['items'][0])
        self.assertEqual(small['snapshot'], full['snapshot'])
        self.assertEqual(small['detailsAction']['arguments']['reference'], small['searchRef'])

    def test_registered_intents_remain_a_real_consumer(self):
        from unittest.mock import patch
        from scripts.core.project import tool_discovery_policy
        policy = tool_discovery_policy()
        del policy['intents']['evidence.pack']
        with patch('scripts.core.tool_catalog.tool_discovery_policy', return_value=policy):
            result = call_operation('capabilities.list', self.root, {'query': '复核回执'})
        self.assertEqual(result['matched'], 0)

    def test_match_scope_and_pagination_parameters_are_bound(self):
        args = {'terms': ['他', '城门'], 'matchMode': 'all', 'limit': 1}
        page = call_operation('source.search', self.root, args)
        self.assertTrue(page['hasMore'])
        with self.assertRaises(StructureError):
            call_operation('source.search', self.root, {**args, 'matchMode': 'any', 'cursor': page['nextCursor']})
        for options in ({'matchMode': 'near'}, {'nearCharacters': 2}, {'matchMode': 'near', 'nearCharacters': True}):
            with self.assertRaises(StructureError):
                call_operation('source.search', self.root, {'terms': ['他'], **options})

    def test_near_matches_exhaustive_window_oracle(self):
        rng = random.Random(17)
        for _ in range(160):
            text = ''.join(rng.choice('abc') for _ in range(12))
            terms = rng.sample(['a', 'b', 'ab', 'bc', 'ca'], 3)
            width = rng.randrange(8)
            all_hits = literal_hits(text, terms, 'any', None)
            expected = set()
            for start in range(len(text)):
                selected = [hit for hit in all_hits if start <= hit[0] and hit[1] <= start + width]
                if {hit[2] for hit in selected} == set(terms):
                    expected.update(selected)
            self.assertEqual(set(literal_hits(text, terms, 'near', width)), expected, (text, terms, width))
