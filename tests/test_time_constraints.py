from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.commands import story_timeline
from scripts.core.source_changes import preview_source_change
from scripts.core.source_impact import source_impact
from scripts.core.source_plan import chapter_plan
from scripts.core.source_time import query_timeline, story_time_states, time_span_states, time_point_order
from scripts.core.structure_store import StructureError, validate_structure
from tests.fixtures_source import single_source_chapter, source_fixture
from tests.test_time_dependency_closure import add_plan


def event(rid, spec=None, **fields):
    return {'entityId': rid, 'entityType': 'event', 'name': rid,
            **({'timeSpec': spec} if spec else {}), **fields}


class TimeConstraintTests(unittest.TestCase):
    def assert_conflict(self, source):
        with self.assertRaises(StructureError) as caught:
            validate_structure(source)
        errors = caught.exception.details['errors']
        self.assertIn('SOURCE_TIME_CONFLICT', {row['code'] for row in errors})
        return next(row for row in errors if row['code'] == 'SOURCE_TIME_CONFLICT')

    def test_mixed_relative_and_strict_order_reject_without_numeric_dates(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-LEFT', {'kind': 'relative', 'anchorId': 'event-RIGHT', 'offset': 1}),
            event('event-RIGHT', {'kind': 'range', 'afterId': 'event-LEFT'}),
        ])
        conflict = self.assert_conflict(source)
        self.assertEqual(conflict['timeIds'], ['event-LEFT', 'event-RIGHT'])
        self.assertEqual(conflict['constraintPaths'],
                         ['entities.event-LEFT.timeSpec', 'entities.event-RIGHT.timeSpec.afterId'])
        source['entities'][-2]['timeSpec']['offset'] = -1
        validate_structure(source)
        self.assertEqual(story_time_states(source)['event-LEFT']['status'], 'unknown')

    def test_overlapping_occurrence_windows_keep_the_feasible_envelope(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-FIVE', timeOrdinal=5), event('event-FIFTEEN', timeOrdinal=15),
            event('event-LEFT', {'kind': 'range', 'afterId': 'time-START', 'beforeId': 'time-LATER'}),
            event('event-RIGHT', {'kind': 'range', 'afterId': 'event-FIVE', 'beforeId': 'event-FIFTEEN'}),
            event('event-BETWEEN', {'kind': 'range', 'afterId': 'event-LEFT', 'beforeId': 'event-RIGHT'}),
        ])
        validate_structure(source)
        state = story_time_states(source)['event-BETWEEN']
        self.assertEqual((state['lower'], state['upper']), (0, 15))
        self.assertTrue(state['lowerExclusive'] and state['upperExclusive'])
        self.assertEqual(state['meaning'], 'possible_occurrence_point')

    def test_strict_equality_conflicts_without_a_numeric_epsilon(self):
        for offset in (0, 1e-20):
            with self.subTest(offset=offset):
                source = source_fixture()
                source['entities'].extend([
                    event('event-LEFT', {'kind': 'relative', 'anchorId': 'event-RIGHT', 'offset': offset}),
                    event('event-RIGHT', {'kind': 'range', 'afterId': 'event-LEFT'}),
                ])
                self.assert_conflict(source)
                source['entities'][-2]['timeSpec']['offset'] = -1e-20
                validate_structure(source)

    def test_decimal_relative_zero_cycle_is_unknown(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-AA', {'kind': 'relative', 'anchorId': 'event-BB', 'offset': 0.1}),
            event('event-BB', {'kind': 'relative', 'anchorId': 'event-CC', 'offset': 0.2}),
            event('event-CC', {'kind': 'relative', 'anchorId': 'event-AA', 'offset': -0.3}),
        ])
        validate_structure(source)
        self.assertEqual(story_time_states(source)['event-AA']['status'], 'unknown')

    def test_reversed_entity_and_chapter_endpoints_are_rejected(self):
        for chapter in (False, True):
            with self.subTest(chapter=chapter):
                source = source_fixture()
                if chapter:
                    row = single_source_chapter(source, 'chapter-TIMESPAN')
                    row.update(storyTimeStartId='time-LATER', storyTimeEndId='time-START')
                    source['chapters'].append(row)
                    source['chapterArcLinks'].append({'chapterId': row['chapterId'],
                        'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
                else:
                    source['entities'].append(event('event-SPAN', timeStartId='time-LATER', timeEndId='time-START'))
                self.assert_conflict(source)

    def test_span_order_interacts_with_relative_and_strict_constraints(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-FIRST', {'kind': 'range', 'beforeId': 'event-LAST'}),
            event('event-LAST'),
            event('event-SPAN', timeStartId='event-LAST', timeEndId='event-FIRST'),
        ])
        conflict = self.assert_conflict(source)
        self.assertIn('entities.event-SPAN.timeEndId', conflict['constraintPaths'])
        source['entities'][-1].update(timeStartId='event-FIRST', timeEndId='event-LAST')
        validate_structure(source)
        self.assertEqual(story_time_states(source)['event-FIRST']['status'], 'unknown')

    def test_activity_endpoints_do_not_become_its_occurrence_point(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-ACTIVITY', timeStartId='time-START', timeEndId='time-LATER'),
            event('event-AMBIGUOUS', {'kind': 'relative', 'anchorId': 'event-ACTIVITY', 'offset': 1}),
            event('event-AFTER-END', {'kind': 'relative', 'anchorId': 'time-LATER', 'offset': 1}),
        ])
        validate_structure(source)
        states = story_time_states(source)
        self.assertEqual(states['event-ACTIVITY']['status'], 'unknown')
        self.assertEqual(states['event-AMBIGUOUS']['status'], 'unknown')
        self.assertEqual(states['event-AFTER-END']['lower'], 11)
        span = time_span_states(source)['event-ACTIVITY']
        self.assertEqual((span['startTime']['lower'], span['endTime']['lower']), (0, 10))
        self.assertFalse(span['ownerPointInferred'])

    def test_single_endpoint_and_coarse_time_remain_unresolved(self):
        source = source_fixture()
        source['entities'].append(event('event-ACTIVITY', timeStartId='time-START'))
        source['entities'].append(event('event-SEASON', {'kind': 'coarse', 'label': '秋季'}))
        validate_structure(source)
        span = time_span_states(source)['event-ACTIVITY']
        self.assertIsNone(span['endId'])
        self.assertEqual(span['endTime']['status'], 'unknown')
        self.assertEqual(story_time_states(source)['event-SEASON']['status'], 'coarse')

    def test_invalid_endpoints_and_nonfinite_numbers_fail_as_diagnostics(self):
        for value in ('time-MISSING', 'character-ALPHA', ['time-START'], ''):
            with self.subTest(value=value):
                source = source_fixture()
                source['entities'].append(event('event-ACTIVITY', timeStartId=value))
                with self.assertRaises(StructureError) as caught:
                    validate_structure(source)
                self.assertIn('SOURCE_TIME_MOUNT_INVALID',
                              {row['code'] for row in caught.exception.details['errors']})
        for value in (float('inf'), float('nan')):
            source = source_fixture()
            source['entities'].append(event('event-NUMBER', timeOrdinal=value))
            with self.assertRaises(StructureError):
                validate_structure(source)

    def test_preview_rejection_preserves_the_current_source_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_fixture(root)
            path = root / '.govel/structure.json'
            original = path.read_bytes()
            with self.assertRaises(StructureError):
                preview_source_change(root, changes=[{'action': 'put', 'collection': 'entities',
                    'record': event('event-SPAN', timeStartId='time-LATER', timeEndId='time-START')}],
                    requestId='reversed-span', reason='test explicit reversed endpoints')
            self.assertEqual(path.read_bytes(), original)

    def test_duration_query_keeps_the_declared_endpoints_and_unknown_owner_point(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = source_fixture()
            source['entities'].append(event('event-SPAN', timeStartId='time-START', timeEndId='time-LATER'))
            from scripts.core.jsonio import write_json
            write_json(root / '.govel/structure.json', source)
            row = next(row for row in query_timeline(root)['items'] if row['timeId'] == 'event-SPAN')
            self.assertEqual(row['storyTime']['status'], 'unknown')
            self.assertEqual(row['timeSpan']['startId'], 'time-START')
            self.assertEqual(row['timeSpan']['endId'], 'time-LATER')

    def test_chart_keeps_declared_direction_when_display_labels_sort_differently(self):
        source = source_fixture()
        source['entities'].extend([
            {'entityId': 'time-ZZ', 'entityType': 'time_anchor', 'name': '开始', 'absoluteTime': '冬季'},
            {'entityId': 'time-AA', 'entityType': 'time_anchor', 'name': '结束', 'absoluteTime': '下个春季'},
            event('event-SPAN', timeStartId='time-ZZ', timeEndId='time-AA'),
        ])
        validate_structure(source)
        view = story_timeline(Path('.'), structure=source)
        row = view['tracks']['event'][0]
        self.assertGreater(row['startIndex'], row['endIndex'])
        self.assertEqual((row['startAnchorId'], row['endAnchorId']), ('time-ZZ', 'time-AA'))
        self.assertEqual(row['timeSpan']['startTime']['status'], 'unknown')
        self.assertNotIn('TIME_ORDER_INVERSION', {p['code'] for p in view['problems']})

    def test_reverse_constraints_invalidate_their_actual_dependency_component_only(self):
        source = source_fixture()
        source['fieldDefinitions'] = [{'fieldId': 'field-PLAN', 'key': 'plot.plan', 'label': '计划',
                                       'valueType': 'topic', 'appliesTo': ['chapter']}]
        source['attributes'], source['fragments'] = [], []
        source['entities'].extend([
            event('event-POINT'), event('event-INDEPENDENT', timeOrdinal=100),
            event('event-LOWER', timeStartId='time-START', timeEndId='event-POINT'),
            event('event-UPPER', timeStartId='event-POINT', timeEndId='time-START'),
        ])
        add_plan(source, 'chapter-TIME', ['event-POINT'])
        add_plan(source, 'chapter-OTHER', ['event-INDEPENDENT'])
        validate_structure(source)
        self.assertEqual(story_time_states(source)['event-POINT']['status'], 'exact')
        after = deepcopy(source)
        next(row for row in after['entities'] if row['entityId'] == 'event-UPPER')['timeEndId'] = 'time-LATER'
        validate_structure(after)
        self.assertEqual(story_time_states(after)['event-POINT']['status'], 'range')
        self.assertNotEqual(chapter_plan(source, 'chapter-TIME')['sourcePlanHash'],
                            chapter_plan(after, 'chapter-TIME')['sourcePlanHash'])
        self.assertEqual(chapter_plan(source, 'chapter-OTHER')['sourcePlanHash'],
                         chapter_plan(after, 'chapter-OTHER')['sourcePlanHash'])
        self.assertEqual(source_impact(source, after)['approvalInvalidations'], ['chapter-TIME'])

    def test_five_hundred_point_chain_resolves_without_recursion_or_invented_dates(self):
        source = source_fixture()
        for number in range(500):
            source['entities'].append(event(f'event-{number:04}', {'kind': 'relative',
                'anchorId': 'time-START' if number == 0 else f'event-{number - 1:04}', 'offset': 1}))
        validate_structure(source)
        states = story_time_states(source)
        self.assertEqual(states['event-0499']['lower'], 500)
        self.assertEqual(states['time-UNKNOWN']['status'], 'unknown')

    def test_pair_order_resolves_relative_and_strict_bounds_without_absolute_dates(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-BASE'),
            event('event-LATER', {'kind': 'relative', 'anchorId': 'event-BASE', 'offset': 1}),
            event('event-EQUAL', {'kind': 'relative', 'anchorId': 'event-BASE', 'offset': 0}),
            event('event-BEFORE', {'kind': 'range', 'beforeId': 'event-BASE'}),
        ])
        validate_structure(source)
        self.assertEqual(story_time_states(source)['event-LATER']['status'], 'unknown')
        for left, right, expected in [('event-LATER', 'event-BASE', 'after'), ('event-BASE', 'event-LATER', 'before'),
                                      ('event-EQUAL', 'event-BASE', 'equal'), ('event-BEFORE', 'event-BASE', 'before'),
                                      ('event-BASE', 'time-START', 'unknown')]:
            with self.subTest(left=left, right=right):
                self.assertEqual(time_point_order(source, left, right)['relation'], expected)

    def test_pair_order_distinguishes_strict_open_bound_and_nonstrict_span(self):
        source = source_fixture()
        source['entities'].extend([
            event('event-AFTER', {'kind': 'range', 'afterId': 'time-START'}),
            event('event-UNKNOWN'),
            event('event-SPAN', timeStartId='time-START', timeEndId='event-UNKNOWN'),
        ])
        validate_structure(source)
        strict = time_point_order(source, 'event-AFTER', 'time-START')
        self.assertEqual(strict['relation'], 'after')
        self.assertEqual(strict['difference']['lower'], 0)
        self.assertTrue(strict['difference']['lowerExclusive'])
        nonstrict = time_point_order(source, 'event-UNKNOWN', 'time-START')
        self.assertEqual(nonstrict['relation'], 'unknown')
        self.assertEqual(nonstrict['difference']['lower'], 0)
        self.assertFalse(nonstrict['difference']['lowerExclusive'])
        with self.assertRaises(StructureError):
            time_point_order(source, 'character-ALPHA', 'time-START')


if __name__ == '__main__':
    unittest.main()
