from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import sys
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_links import query_source_links
from scripts.core.structure_store import StructureError, validate_structure
from tests.fixtures_source import source_fixture


class SourceGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = source_fixture()
        source['planningCandidates'] = [{
            'candidateId': 'candidate-TEST-NODE', 'title': '待定章节', 'summary': '',
            'placement': None, 'primaryArcId': None, 'secondaryArcIds': [],
            'beforeChapterId': None, 'status': 'draft',
        }]
        source['authorDecisions'] = [{
            'decisionId': 'decision-TEST-DECISION', 'statement': '保留待定位置',
            'rationale': '尚无正式章节归属', 'scopeIds': ['candidate-TEST-NODE', 'character-ALPHA'],
            'dependsOn': [], 'status': 'draft',
        }]
        source['fragments'].append({'fragmentId': 'plan-PLAN-GRAPH', 'ownerId': 'character-ALPHA',
                                    'text': '未来章节尚未确定。', 'order': 5,
                                    'contentBindings': ['attribute-LIFE'], 'dependsOn': [],
                                    'contentRole': 'plan'})
        validate_structure(source)
        write_json(self.root / '.govel/structure.json', source)

    def test_planning_edges_do_not_enter_canon_view(self):
        planning = query_source_links(self.root, recordId='candidate-TEST-NODE', graph='planning', depth=1)
        self.assertEqual({row['sourceId'] for row in planning['items']}, {'decision-TEST-DECISION'})
        self.assertEqual({row['kind'] for row in planning['items']}, {'decision_scope'})
        self.assertTrue(all(row['epistemicRole'] == 'authorial_plan' for row in planning['items']))
        canon = query_source_links(self.root, recordId='character-ALPHA', graph='canon', depth=1)
        self.assertFalse(any(row['sourceId'].startswith('decision-') for row in canon['items']))
        self.assertTrue(canon['coverage']['planningTruthExcluded'])
        self.assertNotIn('plan-PLAN-GRAPH', {row['neighborId'] for row in
                                          query_source_links(self.root, recordId='attribute-LIFE', graph='canon', depth=1)['items']})
        with self.assertRaises(StructureError):
            query_source_links(self.root, recordId='candidate-TEST-NODE', graph='canon')

    def test_planning_cursor_is_bounded_to_selected_graph(self):
        first = query_source_links(self.root, recordId='decision-TEST-DECISION', graph='planning',
                                   direction='out', depth=1, limit=1)
        self.assertTrue(first['hasMore'])
        second = query_source_links(self.root, recordId='decision-TEST-DECISION', graph='planning',
                                    direction='out', depth=1, limit=1, cursor=first['nextCursor'])
        self.assertNotEqual(first['items'][0]['edgeId'], second['items'][0]['edgeId'])
        with self.assertRaises(StructureError):
            query_source_links(self.root, recordId='decision-TEST-DECISION', graph='registered',
                               direction='out', depth=1, limit=1, cursor=first['nextCursor'])

    def test_cli_and_mcp_expose_same_read_only_graph(self):
        project = Path(__file__).resolve().parents[1]
        before = (self.root / '.govel/structure.json').read_bytes()
        cli = subprocess.run([sys.executable, str(project / 'scripts/novelctl.py'),
                              'project', 'source', 'graph', '--project-root', str(self.root),
                              '--record-id', 'candidate-TEST-NODE', '--graph', 'planning'],
                             cwd=project, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'query_source_graph', 'arguments': {'projectRoot': str(self.root),
                                                       'recordId': 'candidate-TEST-NODE', 'graph': 'planning'}}}
        mcp = subprocess.run([sys.executable, str(project / 'scripts/mcp_server.py')],
                             input=json.dumps(request) + '\n', cwd=project, capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        actual = json.loads(json.loads(mcp.stdout.splitlines()[-1])['result']['content'][0]['text'])
        self.assertEqual(json.loads(cli.stdout), actual)
        self.assertEqual(before, (self.root / '.govel/structure.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
