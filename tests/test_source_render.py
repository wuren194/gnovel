from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_query import query_source, read_fragment
from scripts.core.source_render import render_source, export_source
from scripts.core.structure_store import StructureError
from tests.fixtures_source import source_fixture


class SourceRenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture(self.root)

    def test_query_and_render_use_identical_selection_without_rewriting(self):
        selection = {'ownerIds': ['C-ALPHA'], 'excludeFields': ['appearance.height', 'appearance.skin']}
        query = query_source(self.root, selection=selection, view='detail')
        rendered = render_source(self.root, selection=selection)
        self.assertEqual(rendered['recordIds'], [r['id'] for r in query['items']])
        self.assertEqual(rendered['snapshot'], query['snapshot'])
        self.assertIn('初次守门时，他一米九的身形挡住了门。', rendered['markdown'])
        self.assertNotIn('他身高一米九。', rendered['markdown'])
        self.assertNotIn('contentBindings', rendered['markdown'])
        self.assertNotIn('AT-HEIGHT', rendered['markdown'])
        self.assertEqual(list(self.root.rglob('*.md')), [])

    def test_long_fragment_is_read_completely_at_character_boundaries(self):
        content = ('他说：“你好。”\n' * 4000) + '末尾标志'
        self.source['fragments'][0]['text'] = content
        write_json(self.root / '.gnovel/structure.json', self.source)
        page = read_fragment(self.root, fragmentId='SF-HEIGHT', maxBytes=1000)
        chunks = [page['text']]
        while page['hasMore']:
            page = read_fragment(self.root, fragmentId='SF-HEIGHT', offset=page['endOffset'],
                                 sourceHash=page['sourceHash'], maxBytes=1000)
            chunks.append(page['text'])
        self.assertEqual(''.join(chunks), content)
        self.assertEqual(page['contentHash'], hashlib.sha256(content.encode()).hexdigest())
        with self.assertRaises(StructureError): read_fragment(self.root, fragmentId='SF-HEIGHT', offset=1)

    def test_export_is_complete_even_when_inline_response_cannot_fit(self):
        content = '长段内容。' * 12000
        self.source['fragments'][0]['text'] = content
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(StructureError) as error:
            render_source(self.root)
        self.assertEqual(error.exception.code, 'SOURCE_EXPORT_REQUIRED')
        before = (self.root / '.gnovel/structure.json').read_bytes()
        receipt = export_source(self.root, output='工作台/阅读.md')
        self.assertIn(content, Path(receipt['output']).read_text())
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)
        self.assertFalse(receipt['maintained'])
        Path(receipt['output']).unlink()
        self.assertTrue(query_source(self.root)['ok'])

    def test_existing_snapshot_requires_explicit_hash_bound_overwrite(self):
        receipt = export_source(self.root, output='工作台/阅读.md')
        target = Path(receipt['output']); target.write_text('人工修改')
        with self.assertRaises(StructureError) as error:
            export_source(self.root, output='工作台/阅读.md')
        self.assertEqual(error.exception.code, 'SOURCE_EXPORT_EXISTS')
        with self.assertRaises(StructureError):
            export_source(self.root, output='工作台/阅读.md', overwrite=True, expectedFileHash=receipt['sha256'])
        export_source(self.root, output='工作台/阅读.md', overwrite=True,
                      expectedFileHash=hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertIn('初次守门', target.read_text())

    def test_export_cannot_target_source_or_prose(self):
        for output in ('.gnovel/structure.json', '正文/第一章.md', '设定/人物设定.md', '../outside.md'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                export_source(self.root, output=output)

    def test_unreviewed_content_is_not_presented_as_all_reviewed(self):
        with self.assertRaises(StructureError) as error:
            render_source(self.root, requireReviewed=True)
        self.assertEqual(error.exception.code, 'SOURCE_REVIEW_REQUIRED')

    def test_explicit_not_applicable_is_filled_and_can_be_exported_as_reviewed(self):
        from scripts.core.source_fields import query_settings_source
        self.source['fragments'] = []
        value = self.source['attributes'][1]['values'][0]
        value.pop('value')
        selection = {'includeAttributes': ['AT-SKIN']}
        for state, permitted in [('not_applicable', True), ('unknown', False), ('unfilled', False)]:
            with self.subTest(state=state):
                value['state'] = state
                write_json(self.root / '.gnovel/structure.json', self.source)
                settings = query_settings_source(self.root, self.source, selector='C-ALPHA')['matches'][0]
                self.assertEqual('appearance.skin' not in settings['unfilled'], permitted)
                if permitted:
                    result = render_source(self.root, selection=selection, requireReviewed=True)
                    self.assertIn('肤色：不适用', result['markdown'])
                    receipt = export_source(self.root, output='工作台/不适用.md', selection=selection,
                                            requireReviewed=True)
                    self.assertIn('肤色：不适用', Path(receipt['output']).read_text())
                else:
                    with self.assertRaises(StructureError) as error:
                        render_source(self.root, selection=selection, requireReviewed=True)
                    self.assertEqual(error.exception.code, 'SOURCE_REVIEW_REQUIRED')

    def test_pending_transaction_cannot_be_queried_or_exported(self):
        write_json(self.root / '.gnovel/transactions/TX-PENDING.json', {'phase': 'prepared'})
        with self.assertRaises(StructureError) as error:
            query_source(self.root)
        self.assertEqual(error.exception.code, 'TRANSACTION_INCOMPLETE')
        with self.assertRaises(StructureError): read_fragment(self.root, fragmentId='SF-HEIGHT')
        with self.assertRaises(StructureError): export_source(self.root, output='工作台/阅读.md')

    def test_explicit_unknown_time_is_distinct_from_an_all_periods_export(self):
        for unknown_time in ('include', 'exclude'):
            choice = {'asOf': None, 'unknownTime': unknown_time}
            query = query_source(self.root, selection=choice, view='detail')
            result = render_source(self.root, selection=choice)
            self.assertEqual(result['recordIds'], [row['id'] for row in query['items']])
            self.assertIn('故事时点：尚未确定', result['markdown'])
            self.assertGreater(result['coverage']['unknownTimeCount'], 0)
            self.assertEqual('FV-HEIGHT' in result['recordIds'], unknown_time == 'include')
        self.assertIn('FV-HEIGHT', render_source(self.root)['recordIds'])


class PerspectiveMetadataTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        self.choice = {'knowledgeMode': 'perspective', 'perspectiveIds': ['C-ALPHA']}
        self.source['bookName'] = 'SECRET-book'
        next(r for r in self.source['entities'] if r['entityId'] == 'C-BETA')['name'] = 'SECRET-person'
        next(r for r in self.source['entities'] if r['entityId'] == 'C-BETA')['aliases'] = ['SECRET-alias']
        next(r for r in self.source['entities'] if r['entityId'] == 'TA-START')['name'] = 'SECRET-time'
        self.source['fieldDefinitions'][0]['label'] = 'SECRET-field'
        self.save()

    def save(self):
        write_json(self.root/'.gnovel/structure.json', self.source)

    def add_belief(self):
        self.source['fragments'].append({'fragmentId': 'SF-VISIBLE-BELIEF', 'ownerId': 'C-BETA',
            'text': '甲记得门旁的人留下了一把钥匙。', 'order': 1, 'contentRole': 'belief',
            'perspectiveId': 'C-ALPHA', 'contentBindings': ['AT-BETA-HEIGHT'],
            'validDuring': {'fromId': 'TA-START'}})
        self.save()

    def test_no_perspective_fragments_means_no_owner_inventory(self):
        from scripts.core.source_selection import select_content
        result = query_source(self.root, selection=self.choice)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['totalMatching'], 0)
        selected = select_content(self.source, self.choice)
        self.assertEqual(selected['owners'], [])
        self.assertEqual(selected['records'], [])

    def test_author_only_names_do_not_leak_across_query_render_export_search(self):
        import json
        from scripts.core.source_selection import select_project_content
        from scripts.core.source_search import search_source
        self.add_belief()
        for view in ('overview', 'list', 'detail'):
            result = query_source(self.root, selection=self.choice, view=view)
            self.assertNotIn('SECRET', json.dumps(result, ensure_ascii=False))
        overview = query_source(self.root, selection=self.choice)
        self.assertEqual([(r['id'], r['label']) for r in overview['items']], [('C-BETA', 'C-BETA')])
        selected = select_project_content(self.root, self.choice)
        self.assertNotIn('SECRET', json.dumps(selected, ensure_ascii=False))
        self.assertEqual(selected['referenceLabels']['C-BETA'], 'C-BETA')
        self.assertEqual(selected['timeLabels']['TA-START'], 'TA-START')
        for operation in (lambda: render_source(self.root, selection=self.choice)['markdown'],
                          lambda: Path(export_source(self.root, output='工作台/视角阅读.md', selection=self.choice)['output']).read_text()):
            text = operation()
            self.assertIn('甲记得门旁的人留下了一把钥匙。', text)
            self.assertIn('TA-START起', text)
            self.assertNotIn('SECRET', text)
        found = search_source(self.root, terms=['钥匙'], selection=self.choice)
        self.assertEqual(found['items'][0]['ownerLabel'], 'C-BETA')
        self.assertNotIn('SECRET', json.dumps(found, ensure_ascii=False))
        with self.assertRaises(StructureError) as caught:
            search_source(self.root, terms=['钥匙'], selection=self.choice, ownerSelectors=['SECRET-alias'])
        self.assertEqual(caught.exception.code, 'SOURCE_SEARCH_OWNER_NOT_FOUND')

    def test_visible_metadata_is_exposed_only_for_selected_owners_and_denials_win(self):
        from scripts.core.source_search import search_source
        self.add_belief()
        beta = next(r for r in self.source['entities'] if r['entityId'] == 'C-BETA')
        beta.update(visibility='public', visibleTo=['C-BETA'])
        self.save()
        self.assertEqual(query_source(self.root, selection=self.choice)['items'][0]['label'], 'C-BETA')
        beta['visibleTo'] = ['C-ALPHA']; self.save()
        self.assertEqual(query_source(self.root, selection=self.choice)['items'][0]['label'], 'SECRET-person')
        found = search_source(self.root, terms=['钥匙'], selection=self.choice, ownerSelectors=['SECRET-alias'])
        self.assertEqual(found['items'][0]['ownerLabel'], 'SECRET-person')
        self.assertEqual(len(query_source(self.root, selection=self.choice)['items']), 1)
        # Author reads keep their explicit complete metadata inventory.
        author = query_source(self.root)
        self.assertIn('SECRET-person', [row['label'] for row in author['items']])

    def test_perspective_detail_withholds_author_provenance_but_keeps_authored_text(self):
        import json
        from scripts.core.source_reading import source_materials
        self.add_belief()
        fragment = self.source['fragments'][-1]
        fragment['sourceRefs'] = [{'path': 'AUTHOR-PROVENANCE/结局.md', 'note': 'AUTHOR-PROVENANCE'}]
        fragment['dependsOn'] = ['FV-HEIGHT']
        self.save()
        before = (self.root / '.gnovel/structure.json').read_bytes()
        result = query_source(self.root, selection=self.choice, view='detail')
        content = result['items'][0]['content']
        self.assertEqual(content['text'], fragment['text'])
        self.assertNotIn('sourceRefs', content)
        self.assertNotIn('dependsOn', content)
        self.assertNotIn('AUTHOR-PROVENANCE', json.dumps(result, ensure_ascii=False))
        chunks, _ = source_materials(self.root,
            {'tool': 'query_source', 'selection': self.choice},
            {'readingScope': {'povCharacter': 'C-ALPHA'}}, 0)
        self.assertNotIn('AUTHOR-PROVENANCE', json.dumps(chunks, ensure_ascii=False))
        author = query_source(self.root, selection={'roles': ['belief']}, view='detail')
        self.assertEqual(next(row['content'] for row in author['items']
                              if row['id'] == fragment['fragmentId']), fragment)
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)
