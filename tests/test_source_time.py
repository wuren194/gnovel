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
            {'entityId': 'event-REL', 'entityType': 'event', 'name': '三日后',
             'timeSpec': {'kind': 'relative', 'anchorId': 'time-START', 'offset': 3}},
            {'entityId': 'event-RANGE', 'entityType': 'event', 'name': '中间某日',
             'timeSpec': {'kind': 'range', 'afterId': 'time-START', 'beforeId': 'time-LATER'}},
            {'entityId': 'event-UNKNOWN', 'entityType': 'event', 'name': '日期未知',
             'timeSpec': {'kind': 'unknown'}},
            {'entityId': 'time-WINTER', 'entityType': 'time_anchor', 'name': '某个冬季',
             'timeSpec': {'kind': 'coarse', 'label': '冬季'}},
        ])
        validate_structure(source)
        return source

    def test_relative_range_coarse_and_unknown_remain_distinct(self):
        states = story_time_states(self.time_source())
        self.assertEqual(states['event-REL']['lower'], 3)
        self.assertEqual(states['event-RANGE']['status'], 'range')
        self.assertEqual((states['event-RANGE']['lower'], states['event-RANGE']['upper']), (0, 10))
        self.assertEqual(states['event-UNKNOWN']['status'], 'unknown')
        self.assertEqual(states['time-WINTER'], {'status': 'coarse', 'label': '冬季',
                                               'reason': 'coarse story phase has no invented numeric date'})

    def test_resolved_impossible_range_is_rejected(self):
        source = self.time_source()
        event = next(row for row in source['entities'] if row['entityId'] == 'event-RANGE')
        event['timeSpec'] = {'kind': 'range', 'afterId': 'time-LATER', 'beforeId': 'time-START'}
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        self.assertIn('SOURCE_TIME_CONFLICT', {row['code'] for row in caught.exception.details['errors']})

    def test_nonzero_relative_cycle_conflicts_but_zero_cycle_is_unknown(self):
        source = source_fixture()
        source['entities'].extend([
            {'entityId': 'event-CYCLE-A', 'entityType': 'event', 'name': '循环甲',
             'timeSpec': {'kind': 'relative', 'anchorId': 'event-CYCLE-B', 'offset': 1}},
            {'entityId': 'event-CYCLE-B', 'entityType': 'event', 'name': '循环乙',
             'timeSpec': {'kind': 'relative', 'anchorId': 'event-CYCLE-A', 'offset': 1}},
        ])
        with self.assertRaises(StructureError): validate_structure(source)
        source['entities'][-1]['timeSpec']['offset'] = -1
        validate_structure(source)
        self.assertEqual(story_time_states(source)['event-CYCLE-A']['status'], 'unknown')

    def test_strict_before_after_cycle_is_rejected_without_dates(self):
        source = source_fixture()
        source['entities'].extend([
            {'entityId': 'event-BEFORE-A', 'entityType': 'event', 'name': '先后甲',
             'timeSpec': {'kind': 'range', 'beforeId': 'event-BEFORE-B'}},
            {'entityId': 'event-BEFORE-B', 'entityType': 'event', 'name': '先后乙',
             'timeSpec': {'kind': 'range', 'beforeId': 'event-BEFORE-A'}},
        ])
        with self.assertRaises(StructureError) as caught: validate_structure(source)
        self.assertIn('SOURCE_TIME_CONFLICT', {row['code'] for row in caught.exception.details['errors']})

    def test_absolute_and_relative_authority_cannot_coexist(self):
        source = self.time_source()
        event = next(row for row in source['entities'] if row['entityId'] == 'event-REL')
        event['timeOrdinal'] = 3
        with self.assertRaises(StructureError) as caught: validate_structure(source)
        self.assertIn('SOURCE_TIME_AUTHORITY_DUPLICATE', {row['code'] for row in caught.exception.details['errors']})

    def test_time_filter_resolves_relative_anchor_and_reports_unknown(self):
        source = self.time_source()
        source['attributes'][1]['values'][0]['validDuring'] = {'fromId': 'event-REL'}
        validate_structure(source)
        at_relative = select_content(source, {'ownerIds': ['character-ALPHA'], 'asOf': 'event-REL'})
        skin = next(row for row in at_relative['records'] if row['id'] == 'value-SKIN')
        self.assertEqual(skin['timeMatch'], 'inside')
        uncertain = select_content(source, {'ownerIds': ['character-ALPHA'], 'asOf': 'event-RANGE',
                                            'unknownTime': 'exclude'})
        self.assertGreater(uncertain['coverage']['excludedByTimeCount'], 0)
        self.assertNotIn('value-SKIN', {row['id'] for row in uncertain['records']})

    def test_reader_order_uses_presentations_not_duplicate_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, chapter = planning_source(root)
            source['entities'].extend([
                {'entityId': 'event-EARLY', 'entityType': 'event', 'name': '已揭示往事', 'timeOrdinal': -10},
                {'entityId': 'event-LATE', 'entityType': 'event', 'name': '后续揭示', 'timeOrdinal': -20},
            ])
            source['chapters'].append({'chapterId': 'chapter-LATER', 'title': '后章', 'displayOrdinal': 2,
                'placement': chapter['placement'], 'filePath': '正文/后章.md', 'status': 'planning_required'})
            source['chapterArcLinks'].append({'chapterId': 'chapter-LATER',
                'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
            source['presentations'].extend([
                {'presentationId': 'presentation-EARLY', 'eventId': 'event-EARLY', 'chapterId': chapter['chapterId'],
                 'kind': 'reveal', 'order': 1},
                {'presentationId': 'presentation-LATE', 'eventId': 'event-LATE', 'chapterId': 'chapter-LATER',
                 'kind': 'flashback', 'order': 1},
            ])
            validate_structure(source); write_json(root / '.govel/structure.json', source)
            result = query_timeline(root, readerChapterId=chapter['chapterId'])
            rows = {row['timeId']: row for row in result['items']}
            self.assertEqual(rows['event-EARLY']['readerState'], 'revealed')
            self.assertEqual(rows['event-LATE']['readerState'], 'unrevealed')
            self.assertEqual(len([row for row in result['items'] if row['timeId'] == 'event-EARLY']), 1)


class SourceKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.source = source_fixture()
        self.source['entities'].append({'entityId': 'faction-ARCHIVE', 'entityType': 'faction',
                                        'name': '档案馆', 'aliases': []})
        self.source['fragments'].extend([
            {'fragmentId': 'belief-BELIEF', 'ownerId': 'character-ALPHA', 'text': '甲误以为城门永不关闭。',
             'order': 10, 'contentBindings': ['attribute-LIFE'], 'contentRole': 'belief',
             'perspectiveId': 'character-ALPHA'},
            {'fragmentId': 'text-LEGEND', 'ownerId': 'faction-ARCHIVE', 'text': '档案声称城门来自神明。',
             'order': 11, 'contentBindings': ['attribute-LIFE'], 'contentRole': 'in_world_text',
             'perspectiveId': 'faction-ARCHIVE'},
        ])
        validate_structure(self.source)

    def test_author_and_perspective_views_preserve_information_identity(self):
        author = select_content(self.source)
        self.assertIn('belief-BELIEF', {row['id'] for row in author['records']})
        character = select_content(self.source, {'knowledgeMode': 'perspective',
                                                  'perspectiveIds': ['character-ALPHA']})
        self.assertEqual({row['id'] for row in character['records']}, {'belief-BELIEF'})
        self.assertGreater(character['coverage']['excludedByKnowledgeCount'], 0)
        archive = select_content(self.source, {'knowledgeMode': 'perspective',
                                                'perspectiveIds': ['faction-ARCHIVE']})
        self.assertEqual({row['id'] for row in archive['records']}, {'text-LEGEND'})

    def test_unmarked_belief_and_perspective_on_fact_are_rejected(self):
        self.source['fragments'][-2].pop('perspectiveId')
        with self.assertRaises(StructureError): validate_structure(self.source)
        self.source['fragments'][-2]['perspectiveId'] = 'character-ALPHA'
        self.source['fragments'][0]['perspectiveId'] = 'character-ALPHA'
        with self.assertRaises(StructureError): validate_structure(self.source)

    def test_perspective_view_requires_explicit_identity(self):
        with self.assertRaises(StructureError):
            select_content(self.source, {'knowledgeMode': 'perspective'})


if __name__ == '__main__':
    unittest.main()
