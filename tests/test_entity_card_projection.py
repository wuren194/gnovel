"""Source-backed entity cards: typed API data and text-only browser consumption."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.core.commands import get_entity
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_fields import query_settings_source
from scripts.core.source_query import query_source
from scripts.core.structure_store import StructureError, validate_structure
from scripts.dashboard_server import DashboardHandler
from tests.fixtures_current import initialize_project
from tests.fixtures_source import source_fixture

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # Same optional browser dependency as test_browser_e2e.
    sync_playwright = None


JSON_TEXT = '{"state":"unknown","value":"这是原样文本"}'
MARKUP_TEXT = '<svg onload="window.__cardInjected=true">原样标记</svg>'


def make_card_project(root: Path) -> dict:
    """Neutral current-format fixture, also usable by text-only manual checks."""
    initial = initialize_project(root, book_name='对象卡投影验收')
    source = source_fixture()
    source.update(projectId=initial['projectId'], bookName='对象卡投影验收')
    source['entities'][0].update(aliases=['值班员'], summary='负责整理值班记录。')
    source['attributes'][0]['values'][0]['validDuring']['toId'] = 'time-LATER'
    source['attributes'][0]['values'].append({
        'valueId': 'value-HEIGHT-LATER', 'value': {'number': 191, 'unit': 'cm'},
        'validDuring': {'fromId': 'time-LATER'}})

    rows = [
        ('UNKNOWN', 'profile.origin', '籍贯', 'text', {'state': 'unknown'}),
        ('UNFILLED', 'profile.note', '备注', 'text', {'state': 'unfilled'}),
        ('NA', 'profile.rank', '职级', 'text', {'state': 'not_applicable'}),
        ('EMPTY', 'profile.empty', '空属性', 'text', None),
        ('BOOL', 'profile.onDuty', '正在值班', 'boolean', {'value': False}),
        ('ZERO', 'profile.count', '遗漏次数', 'number', {'value': 0}),
        ('TEXT', 'profile.literal', '原样文本', 'text', {'value': JSON_TEXT}),
        ('MARKUP', 'profile.markup', '原样标记', 'text', {'value': MARKUP_TEXT}),
        ('REF', 'profile.partner', '搭档', 'reference', {'value': 'character-BETA'}),
        ('PROTO', '__proto__', '自定义字段', 'text', {'value': '原型同名也只是字段'}),
    ]
    for suffix, key, label, kind, value in rows:
        source['fieldDefinitions'].append({
            'fieldId': 'field-CARD-' + suffix, 'key': key, 'label': label,
            'valueType': kind, 'appliesTo': ['character']})
        source['attributes'].append({
            'attributeId': 'attribute-CARD-' + suffix, 'ownerId': 'character-ALPHA',
            'fieldId': 'field-CARD-' + suffix,
            'values': [] if value is None else [{'valueId': 'value-CARD-' + suffix, **value}]})
    source['fieldDefinitions'].append({
        'fieldId': 'field-CARD-MISSING', 'key': 'profile.missing', 'label': '未登记项',
        'valueType': 'text', 'appliesTo': ['character']})
    source['fieldDefinitions'].append({
        'fieldId': 'field-CARD-CONSTRUCTOR', 'key': 'constructor', 'label': '未登记自定义项',
        'valueType': 'text', 'appliesTo': ['character']})
    source['fragments'].extend([
        {'fragmentId': 'belief-CARD-BELIEF', 'ownerId': 'character-BETA', 'order': 5,
         'text': '甲认为两人都熟悉这条路。', 'contentRole': 'belief', 'perspectiveId': 'character-ALPHA',
         'contentBindings': ['attribute-SKIN', 'attribute-BETA-HEIGHT'], 'validDuring': {'fromId': 'time-START'}},
        {'fragmentId': 'text-CARD-DOC', 'ownerId': 'character-ALPHA', 'order': 6,
         'text': '值班簿写着：周三由甲值班。', 'contentRole': 'in_world_text',
         'perspectiveId': 'character-ALPHA', 'contentBindings': ['attribute-LIFE']},
        {'fragmentId': 'plan-CARD-PLAN', 'ownerId': 'character-ALPHA', 'order': 7,
         'text': '下一次值班时核对钥匙数量。', 'contentRole': 'plan', 'contentBindings': ['attribute-LIFE']},
        {'fragmentId': 'fact-CARD-ARCHIVED', 'ownerId': 'character-ALPHA', 'order': 8,
         'text': 'ARCHIVED-旧记录', 'contentRole': 'fact', 'contentBindings': ['attribute-LIFE'],
         'status': 'archived'},
    ])
    validate_structure(source)
    write_json(root / '.govel/structure.json', source)
    return source


def inventory(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


class CardFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='govel-card-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = make_card_project(self.root)

    def save(self):
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)


class EntityCardProjectionTests(CardFixture, unittest.TestCase):
    def test_projection_matches_source_query_and_preserves_metadata_without_writes(self):
        before = inventory(self.root)
        result = get_entity(self.root, '值班员')
        projection = result['sourceProjection']
        queried = query_settings_source(self.root, self.source, selector='character-ALPHA')['matches'][0]
        self.assertEqual(result['entity'], self.source['entities'][0])
        self.assertEqual(result['references'], self.source['references'])
        self.assertEqual(projection['fields'], queried['fields'])
        self.assertEqual(projection['fragments'], queried['fragments'])
        self.assertEqual(projection['valueSource'], '.govel/structure.json')
        self.assertEqual(projection['knowledgeMode'], 'author')
        self.assertEqual(projection['timeScope'], 'all')
        self.assertNotIn('fields', result['entity'])
        self.assertEqual(inventory(self.root), before)
        self.assertTrue(all('fields' not in row for row in read_json(
            self.root / '.govel/structure.json')['entities']))

    def test_typed_states_empty_fields_and_validity_are_not_flattened(self):
        projection = get_entity(self.root, 'character-ALPHA')['sourceProjection']
        values = projection['fieldValues']
        for key, state in [('profile.origin', 'unknown'), ('profile.note', 'unfilled'),
                           ('profile.rank', 'not_applicable')]:
            with self.subTest(key=key):
                self.assertEqual(values[key][0]['content']['state'], state)
                self.assertNotIn('value', values[key][0]['content'])
        self.assertNotIn('profile.empty', values)
        self.assertNotIn('profile.missing', values)
        definitions = {field['key'] for field in projection['fieldDefinitions']}
        self.assertIn('profile.empty', definitions)
        self.assertIn('profile.missing', definitions)
        for key, expected in [('profile.onDuty', False), ('profile.count', 0),
                              ('profile.literal', JSON_TEXT), ('profile.partner', 'character-BETA')]:
            with self.subTest(key=key):
                self.assertEqual(values[key][0]['content']['value'], expected)
        heights = values['appearance.height']
        self.assertEqual([row['content']['value']['number'] for row in heights], [190, 191])
        self.assertEqual(heights[0]['content']['validDuring'], {'fromId': 'time-START', 'toId': 'time-LATER'})
        self.assertEqual(heights[1]['content']['validDuring'], {'fromId': 'time-LATER'})
        self.assertTrue(all(row['timeMatch'] == 'all' for row in heights))

    def test_shared_fragments_are_bound_to_each_card_once_and_archived_content_is_excluded(self):
        for selector, expected_keys in [('character-ALPHA', ['appearance.skin']),
                                        ('character-BETA', ['appearance.height'])]:
            with self.subTest(selector=selector):
                rows = get_entity(self.root, selector)['sourceProjection']['fragments']
                ids = [row['id'] for row in rows]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertNotIn('fact-CARD-ARCHIVED', ids)
                shared = next(row for row in rows if row['id'] == 'belief-CARD-BELIEF')
                self.assertEqual(shared['fieldKeys'], expected_keys)
                self.assertEqual(shared['content']['ownerId'], 'character-BETA')
                self.assertEqual(shared['content']['contentRole'], 'belief')

    def test_archived_entity_keeps_metadata_and_reports_exclusion_instead_of_missing_facts(self):
        self.source['entities'][0]['status'] = 'archived'
        self.save()
        before = inventory(self.root)
        result = get_entity(self.root, 'character-ALPHA')
        self.assertEqual(result['entity'], self.source['entities'][0])
        projection = result['sourceProjection']
        self.assertEqual(projection['excludedReason'], 'archived')
        self.assertEqual(projection['fields'], {})
        self.assertEqual(projection['fieldValues'], {})
        self.assertEqual(projection['fragments'], [])
        self.assertEqual(inventory(self.root), before)

    def test_source_changes_are_reflected_on_the_next_read_without_projection_storage(self):
        old = get_entity(self.root, 'character-ALPHA')['sourceProjection']
        self.source['attributes'][1]['values'][0]['value'] = '肤色记录已修订'
        self.source['fragments'][3]['text'] = '他曾整理过仓库的记录。'
        self.save()
        before = inventory(self.root)
        new = get_entity(self.root, 'character-ALPHA')['sourceProjection']
        self.assertNotEqual(new['fields']['appearance.skin'], old['fields']['appearance.skin'])
        self.assertIn('肤色记录已修订', new['fields']['appearance.skin'])
        self.assertEqual(next(row for row in new['fragments'] if row['id'] == 'fact-LIFE')['content']['text'],
                         '他曾整理过仓库的记录。')
        self.assertEqual(inventory(self.root), before)

    def test_author_card_does_not_change_perspective_selection_boundaries(self):
        self.source['entities'][1].update(name='SECRET-person', aliases=['SECRET-alias'])
        self.source['attributes'][1]['values'][0]['value'] = 'SECRET-canonical-value'
        self.source['fragments'][0]['text'] = 'SECRET-author-fact'
        self.source['fragments'].append({
            'fragmentId': 'belief-CARD-OTHER-POV', 'ownerId': 'character-BETA', 'order': 9,
            'contentBindings': ['attribute-BETA-HEIGHT'], 'contentRole': 'belief',
            'perspectiveId': 'character-BETA', 'text': 'SECRET-other-belief'})
        self.save()
        get_entity(self.root, 'character-ALPHA')
        result = query_source(self.root, selection={
            'knowledgeMode': 'perspective', 'perspectiveIds': ['character-ALPHA']}, view='detail')
        self.assertNotIn('SECRET', json.dumps(result, ensure_ascii=False))
        self.assertEqual({row['id'] for row in result['items']}, {'belief-CARD-BELIEF', 'text-CARD-DOC'})
        self.assertTrue(all(row['kind'] == 'fragment' for row in result['items']))

    def test_pending_transaction_blocks_entity_projection_without_writes(self):
        write_json(self.root / '.govel/transactions/TX-PENDING.json', {'phase': 'prepared'})
        before = inventory(self.root)
        with self.assertRaises(StructureError) as caught:
            get_entity(self.root, 'character-ALPHA')
        self.assertEqual(caught.exception.code, 'TRANSACTION_INCOMPLETE')
        self.assertEqual(inventory(self.root), before)


class CardServerFixture(CardFixture):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), DashboardHandler)
        self.server.daemon_threads = True
        self.server.project_root = str(self.root)
        self.server.mutation_token = 'card-test-token'
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, timeout=2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'


class EntityCardHTTPTests(CardServerFixture, unittest.TestCase):
    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        self.addCleanup(connection.close)
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body, {'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())

    def test_authenticated_http_detail_serializes_current_source_and_rejects_anonymous_read(self):
        before = inventory(self.root)
        status, _, payload = self.request('POST', '/api/entity', {'selector': 'character-ALPHA'})
        self.assertIn(status, {401, 403})
        self.assertIn(payload['code'], {'SESSION_REQUIRED', 'MUTATION_TOKEN_REQUIRED'})
        status, headers, _ = self.request('GET', '/api/config', headers={'Origin': self.base})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        status, _, payload = self.request('POST', '/api/entity', {'selector': 'character-ALPHA'},
                                          {'Cookie': cookie, 'Origin': self.base})
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['sourceProjection'], get_entity(self.root, 'character-ALPHA')['sourceProjection'])
        self.assertIs(payload['sourceProjection']['fieldValues']['profile.onDuty'][0]['content']['value'], False)
        self.assertNotIn('fields', payload['entity'])
        self.assertEqual(inventory(self.root), before)


CARD_TEST_HTML = '''<!doctype html><meta charset="utf-8"><title>对象卡文字验收</title>
<input id="card-filter"><select id="card-type"><option value="">全部</option></select>
<div id="object-list"></div><div id="object-detail"></div>
<script type="module">
import {api} from '/js/api.js';
import {state} from '/js/state.js';
import {initCards, renderCards} from '/js/cards.js';
state.entities = await api('/api/entities');
initCards(); renderCards(); window.__cardReady = true;
</script>'''




if __name__ == '__main__':
    unittest.main()
