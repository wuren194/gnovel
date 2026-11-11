from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_model import SourceIndex, source_schema
from scripts.core.source_query import query_source
from scripts.core.source_selection import select_content
from scripts.core.structure_store import StructureError, empty_structure, validate_structure
from tests.fixtures_source import single_source_chapter, source_fixture

ROOT = Path(__file__).resolve().parents[1]


class SourceModelTests(unittest.TestCase):
    def setUp(self):
        self.source = source_fixture()

    def assert_invalid(self, source, code):
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        self.assertIn(code, [r['code'] for r in caught.exception.details['errors']])

    def test_identity_index_and_duplicate_authority_rejection(self):
        index = SourceIndex.build(self.source)
        self.assertEqual(index.owner('value-HEIGHT'), 'character-ALPHA')
        validate_structure(empty_structure('current', '当前格式'))
        self.source['entities'][0]['fields'] = {'身高': '190cm'}
        self.assert_invalid(self.source, 'SOURCE_DUPLICATE_AUTHORITY')

    def test_schema_declares_every_mutable_source_collection(self):
        schema = source_schema()
        self.assertEqual(schema['mutableCollections']['relations']['idField'], 'relationId')
        self.assertEqual(schema['mutableCollections']['fragments']['idField'], 'fragmentId')
        self.assertEqual(schema['mutationPolicy']['relations']['existing'], 'relation_proposal')
        fields = schema['mutationPolicy']['referencedFieldDefinitions']
        self.assertEqual(fields['existing'], 'field_definition_proposal')
        self.assertEqual(fields['metadataFields'], ['label', 'description'])
        self.assertEqual(fields['semanticFields'], ['key', 'valueType', 'appliesTo'])
        self.assertEqual(schema['mutationPolicy']['presentations']['role'], 'narrative_presentation')

    def test_each_binding_is_validated_at_write_boundary(self):
        cases = [
            ('contentBindings', [], 'SOURCE_BINDINGS_REQUIRED'),
            ('contentBindings', ['attribute-GHOST'], 'SOURCE_BINDING_INVALID'),
            ('contentBindings', ['value-HEIGHT'], 'SOURCE_BINDING_INVALID'),
            ('dependsOn', ['fact-GHOST'], 'SOURCE_DEPENDENCY_INVALID'),
            ('ownerId', 'character-GHOST', 'SOURCE_OWNER_INVALID'),
            ('validDuring', {'fromId': 'character-ALPHA'}, 'SOURCE_TIME_REFERENCE_INVALID'),
        ]
        for key, value, code in cases:
            with self.subTest(key=key, value=value):
                source = deepcopy(self.source); source['fragments'][0][key] = value
                self.assert_invalid(source, code)

    def test_fields_are_instances_and_have_one_owner_slot(self):
        source = deepcopy(self.source)
        row = deepcopy(source['attributes'][2]); row['attributeId'] = 'attribute-DUPLICATE'
        source['attributes'].append(row)
        self.assert_invalid(source, 'SOURCE_ATTRIBUTE_DUPLICATE')
        self.source['attributes'][0]['ownerId'] = self.source['volumes'][0]['volumeId']
        self.assert_invalid(self.source, 'SOURCE_ATTRIBUTE_SCOPE_INVALID')

    def test_known_values_and_unknowns_do_not_share_an_ambiguous_empty_string(self):
        value = self.source['attributes'][0]['values'][0]
        value['state'] = 'unknown'; value.pop('value')
        validate_structure(self.source)
        value['value'] = ''
        self.assert_invalid(self.source, 'SOURCE_UNKNOWN_VALUE_PRESENT')

    def test_temporal_change_keeps_earlier_value_and_rejects_overlap(self):
        values = self.source['attributes'][0]['values']
        values[0]['validDuring']['toId'] = 'time-LATER'
        values.append({'valueId': 'value-NEW-HEIGHT', 'value': {'number': 185, 'unit': 'cm'}, 'validDuring': {'fromId': 'time-LATER'}})
        validate_structure(self.source)
        selected = select_content(self.source, {'includeAttributes': ['attribute-HEIGHT'], 'asOf': 'time-LATER'})
        self.assertEqual([r['id'] for r in selected['records'] if r['kind'] == 'value'], ['value-NEW-HEIGHT'])
        values[0]['validDuring'].pop('toId')
        self.assert_invalid(self.source, 'SOURCE_VALUE_PERIOD_OVERLAP')

    def test_dependency_cycles_are_legal_and_not_ownership_cycles(self):
        self.source['fragments'][0]['dependsOn'] = ['fact-MIXED']
        self.source['fragments'][2]['dependsOn'] = ['fact-HEIGHT']
        validate_structure(self.source)
        self.source['fragments'][0]['ownerId'] = 'fact-MIXED'
        self.assert_invalid(self.source, 'SOURCE_OWNER_INVALID')

    def test_global_identity_collision_in_nested_values(self):
        self.source['attributes'][3]['values'][0]['valueId'] = 'value-HEIGHT'
        self.assert_invalid(self.source, 'GLOBAL_ID_COLLISION')

    def test_bad_shapes_are_diagnostics_not_tracebacks(self):
        for collection in ('fragments', 'attributes', 'entities', 'chapters', 'fieldDefinitions'):
            for payload in (None, {}, ['bad']):
                with self.subTest(collection=collection, payload=payload):
                    source = deepcopy(self.source); source[collection] = payload
                    with self.assertRaises(StructureError): validate_structure(source)
        for field, payload in [('ownerId', {}), ('contentRole', []), ('perspectiveId', {})]:
            source = deepcopy(self.source)
            source['fragments'][0]['contentRole'] = 'belief'
            source['fragments'][0][field] = payload
            with self.assertRaises(StructureError): validate_structure(source)


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.source = source_fixture()

    def ids(self, choice):
        return [r['id'] for r in select_content(self.source, choice)['records'] if r['kind'] == 'fragment']

    def test_shared_filter_truth_table(self):
        cases = [
            ({}, ['fact-HEIGHT', 'fact-LOOK', 'fact-MIXED', 'fact-LIFE']),
            ({'excludeFields': ['appearance.height']}, ['fact-LOOK', 'fact-MIXED', 'fact-LIFE']),
            ({'excludeFields': ['appearance.height', 'appearance.skin']}, ['fact-MIXED', 'fact-LIFE']),
            ({'includeFields': ['appearance.height'], 'excludeFields': ['appearance.height']}, []),
            ({'includeFields': ['biography.experience']}, ['fact-MIXED', 'fact-LIFE']),
            ({'includeFields': []}, []),
            ({'includeAttributes': []}, []),
        ]
        for choice, expected in cases:
            with self.subTest(choice=choice): self.assertEqual(self.ids(choice), expected)

    def test_selection_is_not_text_redaction_or_dependency_expansion(self):
        selected = select_content(self.source, {'includeAttributes': ['attribute-LIFE']})
        self.assertIn('一米九', selected['records'][0]['content']['text'])
        self.assertFalse(any(r['kind'] == 'value' for r in selected['records']))
        self.assertEqual(self.ids({'ownerIds': ['character-BETA']}), [])

    def test_many_matching_topics_deduplicate_content(self):
        ids = self.ids({'includeAttributes': ['attribute-HEIGHT', 'attribute-LIFE']})
        self.assertEqual(ids.count('fact-MIXED'), 1)

    def test_unknown_time_is_reported_and_can_be_excluded(self):
        self.source['fragments'][0]['validDuring'] = {'fromId': 'time-UNKNOWN'}
        result = select_content(self.source, {'asOf': 'time-LATER'})
        self.assertEqual(result['coverage']['unknownTimeCount'], 1)
        self.assertIn('fact-HEIGHT', [r['id'] for r in result['records']])
        self.assertNotIn('fact-HEIGHT', self.ids({'asOf': 'time-LATER', 'unknownTime': 'exclude'}))

    def test_unknown_selection_keys_and_ids_fail_instead_of_looking_empty(self):
        for selection in ({'field': []}, {'ownerIds': ['character-GHOST']}, {'includeFields': ['typo']}, {'includeArchived': 'false'}):
            with self.subTest(selection=selection), self.assertRaises(StructureError):
                select_content(self.source, selection)

    def test_selection_is_pure(self):
        before = deepcopy(self.source)
        result = select_content(self.source)
        result['records'][0]['content']['value'] = 'changed'
        self.assertEqual(self.source, before)


class SourceQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture(self.root)

    def test_pages_cover_each_selected_record_once_and_reject_changed_queries(self):
        first = query_source(self.root, view='detail', limit=2)
        ids = [r['id'] for r in first['items']]
        page = first
        while page['hasMore']:
            page = query_source(self.root, view='detail', limit=2, cursor=page['nextCursor'])
            ids.extend(r['id'] for r in page['items'])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), first['totalMatching'])
        with self.assertRaises(StructureError) as error:
            query_source(self.root, view='list', limit=2, cursor=first['nextCursor'])
        self.assertEqual(error.exception.code, 'CURSOR_QUERY_MISMATCH')
        self.source['revision'] += 1
        write_json(self.root / '.govel/structure.json', self.source)
        with self.assertRaises(StructureError) as error:
            query_source(self.root, view='detail', limit=2, cursor=first['nextCursor'])
        self.assertEqual(error.exception.code, 'CURSOR_STALE')

    def test_oversized_record_is_never_silently_truncated(self):
        with self.assertRaises(StructureError) as error:
            query_source(self.root, view='detail', maxBytes=100)
        self.assertEqual(error.exception.code, 'FIELD_TOO_LARGE')

    def test_real_cli_query_is_read_only(self):
        path = self.root / '.govel/structure.json'; before = path.read_bytes()
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'source', 'query',
                               '--project-root', str(self.root), '--view', 'detail', '--selection',
                               json.dumps({'includeAttributes': ['attribute-LIFE']})], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual([r['id'] for r in payload['items']], ['fact-MIXED', 'fact-LIFE'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.rglob('*.md')), [])


def _codes(source):
    try:
        validate_structure(source)
        return []
    except StructureError as caught:
        return [row['code'] for row in caught.details['errors']]


class SchemaThreePlacementTests(unittest.TestCase):
    """schema-3 placement and typed unit membership invariants."""

    def base(self, *, volumes=1):
        source = source_fixture()
        first = source['volumes'][0]['volumeId']
        for ordinal in range(2, volumes + 1):
            source['volumes'].append({'volumeId': f'volume-EXTRA{ordinal:03d}', 'ordinal': ordinal,
                                      'title': f'第{ordinal}卷', 'status': 'planned'})
        source['arcs'][0]['volumeIds'] = [v['volumeId'] for v in source['volumes']]
        return source, first

    def add_chapter(self, source, chapter_id, *, volume_id, ordinal):
        source['chapters'].append(single_source_chapter(source, chapter_id, volume_id=volume_id, ordinal=ordinal))
        source['chapterArcLinks'].append({'chapterId': chapter_id, 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})

    def test_schema3_chapter_requires_placement(self):
        source, volume = self.base()
        chapter = single_source_chapter(source, 'chapter-AAA', volume_id=volume, ordinal=1)
        chapter.pop('placement')
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': 'chapter-AAA', 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        self.assertIn('CHAPTER_PLACEMENT_INVALID', _codes(source))

    def test_project_chapter_needs_no_volume(self):
        source, _ = self.base()
        source['arcs'][0]['volumeIds'] = []  # a global arc for the project-scope chapter
        self.add_chapter(source, 'chapter-PRJ', volume_id=None, ordinal=1)
        validate_structure(source)
        chapter = next(c for c in source['chapters'] if c['chapterId'] == 'chapter-PRJ')
        self.assertEqual(chapter['placement'], {'scope': 'project'})
        self.assertNotIn('volumeId', chapter)

    def test_legacy_chapter_fields_forbidden_in_schema3(self):
        source, volume = self.base()
        self.add_chapter(source, 'chapter-AAA', volume_id=volume, ordinal=1)
        chapter = next(c for c in source['chapters'] if c['chapterId'] == 'chapter-AAA')
        chapter['volumeId'] = volume
        self.assertIn('CHAPTER_LEGACY_VOLUME_FORBIDDEN', _codes(source))
        chapter.pop('volumeId')
        chapter['unitId'] = 'unit-XYZ'
        self.assertIn('CHAPTER_LEGACY_UNIT_FORBIDDEN', _codes(source))

    def unit(self, unit_id, *, scope='project', volume_id=None, members=(), ordinal=1, status='active'):
        placement = {'scope': 'volume', 'volumeId': volume_id} if scope == 'volume' else {'scope': 'project'}
        return {'unitId': unit_id, 'title': '单元', 'ordinal': ordinal, 'status': status,
                'placement': placement, 'members': list(members)}

    def test_volume_unit_same_volume_invariant(self):
        source, volume = self.base(volumes=2)
        second = source['volumes'][1]['volumeId']
        self.add_chapter(source, 'chapter-AAA', volume_id=volume, ordinal=1)
        self.add_chapter(source, 'chapter-BBB', volume_id=second, ordinal=2)
        source['units'] = [self.unit('unit-AAA', scope='volume', volume_id=volume,
                                     members=[{'memberType': 'chapter', 'memberId': 'chapter-BBB', 'order': 1}])]
        self.assertIn('SOURCE_UNIT_MEMBER_VOLUME_MISMATCH', _codes(source))
        source['units'][0]['members'] = [{'memberType': 'chapter', 'memberId': 'chapter-AAA', 'order': 1}]
        validate_structure(source)

    def test_project_unit_spans_volumes(self):
        source, volume = self.base(volumes=2)
        second = source['volumes'][1]['volumeId']
        self.add_chapter(source, 'chapter-AAA', volume_id=volume, ordinal=1)
        self.add_chapter(source, 'chapter-BBB', volume_id=second, ordinal=2)
        source['units'] = [self.unit('unit-PRJ', scope='project',
                                     members=[{'memberType': 'chapter', 'memberId': 'chapter-AAA', 'order': 1},
                                              {'memberType': 'chapter', 'memberId': 'chapter-BBB', 'order': 2}])]
        validate_structure(source)
        index = SourceIndex.build(source)
        self.assertEqual(sorted(index.units_for('chapter-AAA') + index.units_for('chapter-BBB')), ['unit-PRJ', 'unit-PRJ'])

    def test_project_unit_may_contain_planning_candidate(self):
        source, volume = self.base()
        source['planningCandidates'] = [{'candidateId': 'candidate-AAA', 'title': '候选', 'summary': 's',
                                         'placement': None, 'primaryArcId': None, 'secondaryArcIds': [],
                                         'beforeChapterId': None, 'status': 'draft'}]
        source['units'] = [self.unit('unit-PRJ', scope='project',
                                     members=[{'memberType': 'planning_candidate', 'memberId': 'candidate-AAA', 'order': 1}])]
        validate_structure(source)
        # A volume unit cannot hold the unplaced candidate.
        source['units'][0]['placement'] = {'scope': 'volume', 'volumeId': volume}
        self.assertIn('SOURCE_UNIT_MEMBER_VOLUME_MISMATCH', _codes(source))

    def test_unit_member_ordering_uniqueness(self):
        source, volume = self.base()
        self.add_chapter(source, 'chapter-AAA', volume_id=volume, ordinal=1)
        self.add_chapter(source, 'chapter-BBB', volume_id=volume, ordinal=2)
        source['units'] = [self.unit('unit-AAA', scope='volume', volume_id=volume,
                                     members=[{'memberType': 'chapter', 'memberId': 'chapter-AAA', 'order': 1},
                                              {'memberType': 'chapter', 'memberId': 'chapter-BBB', 'order': 1}])]
        self.assertIn('SOURCE_UNIT_MEMBER_ORDER_DUPLICATE', _codes(source))

    def test_unit_target_validation(self):
        source, volume = self.base()
        source['units'] = [self.unit('unit-AAA', scope='project',
                                     members=[{'memberType': 'chapter', 'memberId': 'chapter-GHOST', 'order': 1}])]
        self.assertIn('SOURCE_UNIT_MEMBER_TARGET_MISSING', _codes(source))

    def test_one_active_member_cannot_belong_to_two_active_units(self):
        source, volume = self.base()
        self.add_chapter(source, 'chapter-AAA', volume_id=volume, ordinal=1)
        member = [{'memberType': 'chapter', 'memberId': 'chapter-AAA', 'order': 1}]
        source['units'] = [self.unit('unit-ONE', scope='project', members=member, ordinal=1),
                           self.unit('unit-TWO', scope='project', members=member, ordinal=2)]
        self.assertIn('SOURCE_MEMBER_MULTIPLE_ACTIVE_UNITS', _codes(source))
        # Archiving one unit resolves the conflict.
        source['units'][1]['status'] = 'archived'
        validate_structure(source)

    pass

    def test_schema_reports_one_current_format(self):
        schema = source_schema()
        from scripts.core.project import current_schema_version
        self.assertEqual(schema['schemaVersion'], current_schema_version())
        self.assertNotIn('structureVersions', schema)
        self.assertNotIn('ownershipModels', schema)
        self.assertEqual(schema['proseBinding'], {'format': 'PROSE_BINDING_V1',
                                                  'states': ['unbound', 'managed', 'unresolved']})
