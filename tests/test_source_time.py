from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_selection import select_content
from scripts.core.source_time import story_time_states, query_timeline
from scripts.core.structure_store import validate_structure, StructureError
from tests.fixtures_source import source_fixture
from tests.test_source_plan import planning_source


class SourceTimeTests(unittest.TestCase):
    def time_source(self):
        source = source_fixture()
        source['entities'].extend([
            {'entityId': 'E-REL', 'entityType': 'event', 'name': '三日后',
             'timeSpec': {'kind': 'relative', 'anchorId': 'TA-START', 'offset': 3}},
            {'entityId': 'E-RANGE', 'entityType': 'event', 'name': '中间某日',
             'timeSpec': {'kind': 'range', 'afterId': 'TA-START', 'beforeId': 'TA-LATER'}},
            {'entityId': 'E-UNKNOWN', 'entityType': 'event', 'name': '日期未知',
             'timeSpec': {'kind': 'unknown'}},
            {'entityId': 'TA-WINTER', 'entityType': 'time_anchor', 'name': '某个冬季',
             'timeSpec': {'kind': 'coarse', 'label': '冬季'}},
        ])
        validate_structure(source)
        return source

    def test_relative_range_coarse_and_unknown_remain_distinct(self):
        states = story_time_states(self.time_source())
        self.assertEqual(states['E-REL']['lower'], 3)
        self.assertEqual(states['E-RANGE']['status'], 'range')
        self.assertEqual((states['E-RANGE']['lower'], states['E-RANGE']['upper']), (0, 10))
        self.assertEqual(states['E-UNKNOWN']['status'], 'unknown')
        self.assertEqual(states['TA-WINTER'], {'status': 'coarse', 'label': '冬季',
                                               'reason': 'coarse story phase has no invented numeric date'})

    def test_resolved_impossible_range_is_rejected(self):
        source = self.time_source()
        event = next(row for row in source['entities'] if row['entityId'] == 'E-RANGE')
        event['timeSpec'] = {'kind': 'range', 'afterId': 'TA-LATER', 'beforeId': 'TA-START'}
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        self.assertIn('SOURCE_TIME_CONFLICT', {row['code'] for row in caught.exception.details['errors']})

    def test_nonzero_relative_cycle_conflicts_but_zero_cycle_is_unknown(self):
        source = source_fixture()
        source['entities'].extend([
            {'entityId': 'E-CYCLE-A', 'entityType': 'event', 'name': '循环甲',
             'timeSpec': {'kind': 'relative', 'anchorId': 'E-CYCLE-B', 'offset': 1}},
            {'entityId': 'E-CYCLE-B', 'entityType': 'event', 'name': '循环乙',
             'timeSpec': {'kind': 'relative', 'anchorId': 'E-CYCLE-A', 'offset': 1}},
        ])
        with self.assertRaises(StructureError): validate_structure(source)
        source['entities'][-1]['timeSpec']['offset'] = -1
        validate_structure(source)
        self.assertEqual(story_time_states(source)['E-CYCLE-A']['status'], 'unknown')

    def test_strict_before_after_cycle_is_rejected_without_dates(self):
        source = source_fixture()
        source['entities'].extend([
            {'entityId': 'E-BEFORE-A', 'entityType': 'event', 'name': '先后甲',
             'timeSpec': {'kind': 'range', 'beforeId': 'E-BEFORE-B'}},
            {'entityId': 'E-BEFORE-B', 'entityType': 'event', 'name': '先后乙',
             'timeSpec': {'kind': 'range', 'beforeId': 'E-BEFORE-A'}},
        ])
        with self.assertRaises(StructureError) as caught: validate_structure(source)
        self.assertIn('SOURCE_TIME_CONFLICT', {row['code'] for row in caught.exception.details['errors']})

    def test_absolute_and_relative_authority_cannot_coexist(self):
        source = self.time_source()
        event = next(row for row in source['entities'] if row['entityId'] == 'E-REL')
        event['timeOrdinal'] = 3
        with self.assertRaises(StructureError) as caught: validate_structure(source)
        self.assertIn('SOURCE_TIME_AUTHORITY_DUPLICATE', {row['code'] for row in caught.exception.details['errors']})

    def test_time_filter_resolves_relative_anchor_and_reports_unknown(self):
        source = self.time_source()
        source['attributes'][1]['values'][0]['validDuring'] = {'fromId': 'E-REL'}
        validate_structure(source)
        at_relative = select_content(source, {'ownerIds': ['C-ALPHA'], 'asOf': 'E-REL'})
        skin = next(row for row in at_relative['records'] if row['id'] == 'FV-SKIN')
        self.assertEqual(skin['timeMatch'], 'inside')
        uncertain = select_content(source, {'ownerIds': ['C-ALPHA'], 'asOf': 'E-RANGE',
                                            'unknownTime': 'exclude'})
        self.assertGreater(uncertain['coverage']['excludedByTimeCount'], 0)
        self.assertNotIn('FV-SKIN', {row['id'] for row in uncertain['records']})

    def test_reader_order_uses_presentations_not_duplicate_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, chapter = planning_source(root)
            source['entities'].extend([
                {'entityId': 'E-EARLY', 'entityType': 'event', 'name': '已揭示往事', 'timeOrdinal': -10},
                {'entityId': 'E-LATE', 'entityType': 'event', 'name': '后续揭示', 'timeOrdinal': -20},
            ])
            source['chapters'].append({'chapterId': 'CH-LATER', 'title': '后章', 'displayOrdinal': 2,
                'placement': chapter['placement'], 'filePath': '正文/后章.md', 'status': 'planning_required'})
            source['chapterArcLinks'].append({'chapterId': 'CH-LATER',
                'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
            source['presentations'].extend([
                {'presentationId': 'PV-EARLY', 'eventId': 'E-EARLY', 'chapterId': chapter['chapterId'],
                 'kind': 'reveal', 'order': 1},
                {'presentationId': 'PV-LATE', 'eventId': 'E-LATE', 'chapterId': 'CH-LATER',
                 'kind': 'flashback', 'order': 1},
            ])
            validate_structure(source); write_json(root / '.gnovel/structure.json', source)
            result = query_timeline(root, readerChapterId=chapter['chapterId'])
            rows = {row['timeId']: row for row in result['items']}
            self.assertEqual(rows['E-EARLY']['readerState'], 'revealed')
            self.assertEqual(rows['E-LATE']['readerState'], 'unrevealed')
            self.assertEqual(len([row for row in result['items'] if row['timeId'] == 'E-EARLY']), 1)


class SourceKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.source = source_fixture()
        self.source['entities'].append({'entityId': 'FC-ARCHIVE', 'entityType': 'faction',
                                        'name': '档案馆', 'aliases': []})
        self.source['fragments'].extend([
            {'fragmentId': 'SF-BELIEF', 'ownerId': 'C-ALPHA', 'text': '甲误以为城门永不关闭。',
             'order': 10, 'contentBindings': ['AT-LIFE'], 'contentRole': 'belief',
             'perspectiveId': 'C-ALPHA'},
            {'fragmentId': 'SF-LEGEND', 'ownerId': 'FC-ARCHIVE', 'text': '档案声称城门来自神明。',
             'order': 11, 'contentBindings': ['AT-LIFE'], 'contentRole': 'in_world_text',
             'perspectiveId': 'FC-ARCHIVE'},
        ])
        validate_structure(self.source)

    def test_author_and_perspective_views_preserve_information_identity(self):
        author = select_content(self.source)
        self.assertIn('SF-BELIEF', {row['id'] for row in author['records']})
        character = select_content(self.source, {'knowledgeMode': 'perspective',
                                                  'perspectiveIds': ['C-ALPHA']})
        self.assertEqual({row['id'] for row in character['records']}, {'SF-BELIEF'})
        self.assertGreater(character['coverage']['excludedByKnowledgeCount'], 0)
        archive = select_content(self.source, {'knowledgeMode': 'perspective',
                                                'perspectiveIds': ['FC-ARCHIVE']})
        self.assertEqual({row['id'] for row in archive['records']}, {'SF-LEGEND'})

    def test_unmarked_belief_and_perspective_on_fact_are_rejected(self):
        self.source['fragments'][-2].pop('perspectiveId')
        with self.assertRaises(StructureError): validate_structure(self.source)
        self.source['fragments'][-2]['perspectiveId'] = 'C-ALPHA'
        self.source['fragments'][0]['perspectiveId'] = 'C-ALPHA'
        with self.assertRaises(StructureError): validate_structure(self.source)

    def test_perspective_view_requires_explicit_identity(self):
        with self.assertRaises(StructureError):
            select_content(self.source, {'knowledgeMode': 'perspective'})


if __name__ == '__main__':
    unittest.main()
