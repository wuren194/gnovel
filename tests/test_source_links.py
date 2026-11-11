from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_links import query_source_links
from scripts.core.structure_store import StructureError, validate_structure
from tests.fixtures_source import source_fixture


class SourceLinkTraversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = source_fixture()
        source['relations'] = [
            {'relationId': 'relation-ALPHA-BETA', 'fromEntity': 'character-ALPHA', 'toEntity': 'character-BETA', 'kind': 'protects'},
            {'relationId': 'relation-BETA-ALPHA', 'fromEntity': 'character-BETA', 'toEntity': 'character-ALPHA', 'kind': 'trusts'},
        ]
        validate_structure(source); write_json(self.root / '.govel/structure.json', source)

    def test_typed_dependencies_and_entity_relations_are_distinct(self):
        result = query_source_links(self.root, recordId='attribute-HEIGHT', direction='out', depth=1)
        self.assertEqual({row['kind'] for row in result['items']}, {'content_binding'})
        self.assertEqual({row['neighborId'] for row in result['items']}, {'fact-HEIGHT', 'fact-LOOK', 'fact-MIXED'})
        relations = query_source_links(self.root, recordId='character-ALPHA', edgeKinds=['entity_relation'], depth=2)
        self.assertEqual({row['relationKind'] for row in relations['items']}, {'protects', 'trusts'})
        self.assertGreater(relations['coverage']['cycleEdgeCount'], 0)

    def test_depth_boundary_and_pagination_are_explicit(self):
        first = query_source_links(self.root, recordId='field-HEIGHT', direction='out', depth=1, limit=1)
        self.assertTrue(first['hasMore'])
        self.assertTrue(first['coverage']['truncatedByDepth'])
        self.assertEqual(first['coverage']['nextTraversal']['depth'], 2)
        second = query_source_links(self.root, recordId='field-HEIGHT', direction='out', depth=1,
                                    limit=1, cursor=first['nextCursor'])
        self.assertNotEqual(first['items'][0]['edgeId'], second['items'][0]['edgeId'])

    def test_unknown_record_and_excess_depth_fail_closed(self):
        with self.assertRaises(StructureError):
            query_source_links(self.root, recordId='MISSING')
        with self.assertRaises(StructureError) as caught:
            query_source_links(self.root, recordId='attribute-HEIGHT', depth=999)
        self.assertEqual(caught.exception.code, 'QUERY_BUDGET_INVALID')


if __name__ == '__main__':
    unittest.main()
