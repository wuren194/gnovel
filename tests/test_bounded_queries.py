from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import write_json
from scripts.core.project import query_policy
from scripts.core.query import query_project
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_current_project
from tests.source_query_fixture import declare_field, set_field

ROOT = Path(__file__).resolve().parents[1]


class BoundedQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = create_current_project(self.root, chapter_count=2)
        self.structure = self.fixture['structure']
        self.structure['entities'] += [
            {'entityId': 'rule-ONE', 'entityType': 'rule', 'name': '通行规则', 'aliases': ['通行规定'], 'status': 'active'},
            {'entityId': 'rule-TWO', 'entityType': 'rule', 'name': '备用规则', 'aliases': [], 'status': 'active'},
            *[{'entityId': f'time-0{n+1}', 'entityType': 'time_anchor', 'name': f'第{n}天', 'aliases': [], 'status': 'active', 'timeOrdinal': n} for n in range(4)],
        ]
        for key, value in {'适用范围': '全城', '规则陈述': '夜间登记', '备注': '长说明'}.items():
            set_field(self.structure, 'rule-ONE', key, value)
        self.structure['chapters'][0].update(storyTimeStartId='time-01', storyTimeEndId='time-02')
        self.structure['entities'][0]['timeStartId'] = 'time-03'
        self.structure['references'].append({'sourceId': self.fixture['chapterIds'][0], 'targetId': 'rule-ONE', 'kind': 'uses'})
        self.structure['relations'].append({'relationId': 'relation-ONE', 'fromEntity': 'rule-ONE', 'toEntity': self.fixture['characterId'], 'kind': 'constrains'})
        self.save()

    def save(self): write_json(self.root / '.govel/structure.json', self.structure)

    def test_four_dimensions_default_to_small_independent_overviews(self):
        for dimension in ('logic', 'timeline', 'attributes', 'nodes'):
            result = query_project(self.root, dimension)
            self.assertEqual(result['view'], 'overview'); self.assertNotIn('items', result)
            self.assertLessEqual(len((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode()), query_policy()['maxBytes']['overview'])
            self.assertFalse(result['coverage']['complete'])

    def test_standard_custom_fields_and_distinct_selector_search_filter_projection(self):
        result = query_project(self.root, 'attributes', view='list', kind='rule', field=['备注'])
        self.assertEqual(result['totalMatching'], 2)
        result = query_project(self.root, 'attributes', view='list', kind='rule', hasField='备注', field=['备注'])
        self.assertEqual(result['totalMatching'], 1); self.assertEqual(result['items'][0]['fields'], {'备注': '长说明'})
        self.assertEqual(query_project(self.root, 'logic', view='list', query='通行')['totalMatching'], 1)
        with self.assertRaises(StructureError): query_project(self.root, 'logic', selector='通行')
        self.assertEqual(query_project(self.root, 'logic', selector='通行规定')['items'][0]['id'], 'rule-ONE')

    def test_anchor_usage_counts_mounts_and_not_only_references(self):
        result = query_project(self.root, 'timeline', view='list', kind='time_anchor', unused=True)
        self.assertEqual([r['id'] for r in result['items']], ['time-04'])
        detail = query_project(self.root, 'timeline', selector='time-01')['items'][0]
        self.assertEqual(detail['referenceCount'], 0); self.assertEqual(detail['timeMountCount'], 1)
        self.assertIn('not proof', result['coverage']['absenceMeaning'])

    def test_typed_traversal_distinguishes_relations_references_and_structure(self):
        result = query_project(self.root, 'nodes', view='traverse', selector='rule-ONE')
        self.assertEqual({r['kind'] for r in result['items']}, {'reference', 'relation'})
        result = query_project(self.root, 'nodes', view='traverse', selector='rule-ONE', direction='out', edgeKind='relation')
        self.assertEqual(result['returned'], 1)
        self.assertEqual(result['items'][0]['neighbor']['id'], self.fixture['characterId'])
        result = query_project(self.root, 'timeline', view='traverse', selector='time-01', edgeKind='time-mount')
        self.assertEqual(result['items'][0]['neighbor']['id'], self.fixture['chapterIds'][0])

    def test_record_and_byte_limits_pagination_and_stale_cursor(self):
        first = query_project(self.root, 'nodes', view='list', limit=2)
        self.assertEqual(first['returned'], 2); self.assertTrue(first['hasMore'])
        second = query_project(self.root, 'nodes', view='list', limit=2, cursor=first['nextCursor'])
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
        self.structure['entities'][0]['name'] = '改名'; self.save()
        with self.assertRaises(StructureError) as caught:
            query_project(self.root, 'nodes', view='list', limit=2, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')
        result = query_project(self.root, 'nodes', view='list', limit=100, maxBytes=3500)
        self.assertTrue(result['hasMore'])
        self.assertLessEqual(len((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode()), 3500)

    def test_oversize_fields_are_explicit_errors_not_truncated(self):
        set_field(self.structure, 'rule-ONE', '备注', '文' * 20000)
        self.save()
        with self.assertRaises(StructureError) as caught:
            query_project(self.root, 'attributes', selector='rule-ONE')
        self.assertEqual(caught.exception.code, 'FIELD_TOO_LARGE')
        result = query_project(self.root, 'attributes', selector='rule-ONE', field=['规则陈述'])
        self.assertEqual(result['items'][0]['fields'], {'规则陈述': '夜间登记'})

    def test_numeric_range_retains_unknown_partition_and_schema_is_paged(self):
        result = query_project(self.root, 'timeline', view='list', fromAnchor='time-02', toAnchor='time-03')
        self.assertIn(self.fixture['chapterIds'][1], {r['id'] for r in result['items']})
        self.assertFalse(next(r for r in result['items'] if r['id'] == self.fixture['chapterIds'][1])['timeComparable'])
        result = query_project(self.root, 'attributes', view='schema', kind='rule', limit=2)
        self.assertEqual(result['returned'], 2); self.assertTrue(result['hasMore'])

    def test_policy_perturbation_changes_actual_default_page(self):
        policy = deepcopy(query_policy()); policy['defaultLimit'] = 1
        with patch('scripts.core.query.query_policy', return_value=policy):
            self.assertEqual(query_project(self.root, 'nodes', view='list')['returned'], 1)

    def test_canonical_field_filter_replaces_retired_display_scope_parameter(self):
        attr = set_field(self.structure, 'rule-ONE', 'rule.scope', '全城')
        next(field for field in self.structure['fieldDefinitions'] if field['fieldId'] == attr['fieldId'])['label'] = '作用区域'
        self.save()
        for key in ('rule.scope', '作用区域', attr['fieldId']):
            result = query_project(self.root, 'logic', view='list', fieldEq={key: '全城'})
            self.assertEqual([row['id'] for row in result['items']], ['rule-ONE'])
        with self.assertRaises(StructureError) as caught:
            query_project(self.root, 'logic', scope='全城')
        self.assertEqual(caught.exception.code, 'QUERY_ARGUMENT_INVALID')

    def test_every_schema_owner_kind_can_be_used_as_a_query_filter(self):
        from scripts.core.source_model import owner_types
        from scripts.core.operations import call_operation
        from scripts.core.source_selection import normalize_selection
        from scripts.core.source_model import SourceIndex
        schema = query_project(self.root, 'attributes', view='schema')
        kinds = {row['kind'] for row in schema['items']}
        self.assertIn('chapter', kinds)
        for kind in kinds:
            result = call_operation('query.attributes', self.root, {'view': 'schema', 'kind': kind})
            self.assertTrue(result['items'])
            self.assertTrue(all(row['kind'] == kind for row in result['items']))
        for kind in owner_types():
            normalize_selection({'ownerTypes': [kind]}, SourceIndex.build(self.structure))
            call_operation('query.nodes', self.root, {'view': 'overview', 'kind': kind})

    def test_cli_and_stdio_mcp_use_same_read_only_schema_and_result(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        cli = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'query', 'logic', '--project-root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'query_logic', 'arguments': {'projectRoot': str(self.root)}}}]
        mcp = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')], input='\n'.join(json.dumps(r) for r in requests) + '\n', capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        payload = json.loads(mcp.stdout)['result']['structuredContent']
        self.assertEqual(json.loads(cli.stdout), payload)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})


if __name__ == '__main__': unittest.main()
