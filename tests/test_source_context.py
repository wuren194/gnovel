from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import state_cache
from scripts.continuation_context import build_continuation_context
from scripts.core.jsonio import write_json
from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, validate_structure
from tests.test_source_plan import planning_source


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.chapter = planning_source(self.root); self.cid = self.chapter['chapterId']
        write_json(self.root / '.gnovel/project.json', {'schemaVersion': PROJECT_SCHEMA_VERSION,
            'workflowVersion': WORKFLOW_VERSION, 'projectId': self.source['projectId']})

    def save(self):
        validate_structure(self.source)
        write_json(self.root / '.gnovel/structure.json', self.source)

    def test_design_projection_declares_source_model_without_copying_reading_export(self):
        state, design = state_cache.project_projections(self.root)
        self.assertEqual(state['sourceModelVersion'], PROJECT_SCHEMA_VERSION)
        self.assertEqual(design['sourceModelVersion'], PROJECT_SCHEMA_VERSION)
        self.assertNotIn('sourceRecords', design)

    def test_station_context_routes_to_bounded_source_queries(self):
        context = build_continuation_context(self.root, self.cid, task_type='writer',
                                             pov_character='C-ALPHA', view='station', read_only=True)
        source = context['sourceContext']
        self.assertTrue(source['planReady'])
        self.assertEqual(source['planQuery']['tool'], 'query_source_chapter_plan')
        self.assertEqual(source['contentQuery']['selection'],
                         {'knowledgeMode': 'perspective', 'perspectiveIds': ['C-ALPHA'],
                          'unknownTime': 'exclude', 'asOf': None})
        self.assertEqual(source['coverage']['perspectiveTime']['status'], 'unavailable')
        self.assertFalse(source['coverage']['contentInjected'])
        self.assertFalse(source['coverage']['readingExportRequired'])
        self.assertNotIn('records', source)

    def test_prepared_pov_reads_only_beliefs_available_at_chapter_start(self):
        from scripts.core.source_query import query_source
        from scripts.core.source_reading import source_materials
        self.chapter['storyTimeStartId'] = 'TA-START'
        for suffix, period in [('NOW', {'fromId': 'TA-START'}),
                               ('FUTURE', {'fromId': 'TA-LATER'}),
                               ('UNRESOLVED', {'fromId': 'TA-UNKNOWN'})]:
            self.source['fragments'].append({'fragmentId': 'SF-KNOW-' + suffix,
                'ownerId': 'C-ALPHA', 'text': '认知材料-' + suffix, 'order': 1,
                'contentRole': 'belief', 'perspectiveId': 'C-ALPHA',
                'contentBindings': ['AT-LIFE'], 'validDuring': period,
                'sourceRefs': [{'note': 'AUTHOR-ONLY-EXPLANATION'}]})
        self.save()
        context = build_continuation_context(self.root, self.cid, task_type='writer',
                                             pov_character='C-ALPHA', view='station', read_only=True)
        query = context['sourceContext']['contentQuery']
        result = query_source(self.root, selection=query['selection'], view='detail')
        self.assertEqual([row['id'] for row in result['items']], ['SF-KNOW-NOW'])
        self.assertEqual(result['coverage']['unknownTimeCount'], 1)
        self.assertEqual(result['coverage']['excludedByTimeCount'], 2)
        chunks, coverage = source_materials(self.root, query,
            {'readingScope': {'povCharacter': 'C-ALPHA'}}, 0)
        text = '\n'.join(row['text'] for row in chunks)
        self.assertIn('认知材料-NOW', text)
        for hidden in ('FUTURE', 'UNRESOLVED', 'AUTHOR-ONLY-EXPLANATION'):
            self.assertNotIn(hidden, text)
        self.assertEqual(coverage['coverage']['temporalScope'], 'as_of')

    def test_undated_pov_keeps_unbounded_beliefs_without_changing_author_reads(self):
        from scripts.core.source_query import query_source
        self.source['fragments'].append({'fragmentId': 'SF-FUTURE-KNOWLEDGE',
            'ownerId': 'C-ALPHA', 'text': '未来才得知的钥匙位置。', 'order': 1,
            'contentRole': 'belief', 'perspectiveId': 'C-ALPHA',
            'contentBindings': ['AT-LIFE'], 'validDuring': {'fromId': 'TA-LATER'}})
        self.source['fragments'].append({'fragmentId': 'SF-UNBOUNDED-KNOWLEDGE',
            'ownerId': 'C-ALPHA', 'text': '他记得自己保管着门房钥匙。', 'order': 2,
            'contentRole': 'belief', 'perspectiveId': 'C-ALPHA', 'contentBindings': ['AT-LIFE']})
        self.save()
        context = build_continuation_context(self.root, self.cid, task_type='writer',
                                             pov_character='C-ALPHA', view='station', read_only=True)
        source = context['sourceContext']
        result = query_source(self.root, selection=source['contentQuery']['selection'], view='detail')
        self.assertEqual([row['id'] for row in result['items']], ['SF-UNBOUNDED-KNOWLEDGE'])
        self.assertEqual(result['coverage']['unknownTimeCount'], 1)
        self.assertEqual(result['coverage']['temporalScope'], 'undetermined')
        self.assertTrue(source['coverage']['perspectiveTime']['timeLimitedBeliefsWithheld'])
        author = query_source(self.root, selection={'ownerIds': ['C-ALPHA']}, view='detail')
        self.assertIn('SF-FUTURE-KNOWLEDGE', [row['id'] for row in author['items']])

    def test_pov_occurrence_window_uses_proven_validity_without_inventing_a_date(self):
        from scripts.core.source_query import query_source
        for number in (3, 5, 8, 12):
            self.source['entities'].append({'entityId': f'TA-DAY{number}', 'entityType': 'time_anchor',
                                           'name': f'第{number}天', 'timeOrdinal': number})
        self.source['entities'].append({'entityId': 'TA-WINDOW', 'entityType': 'time_anchor',
            'name': '抵达时点', 'timeSpec': {'kind': 'range', 'afterId': 'TA-DAY3', 'beforeId': 'TA-DAY5'}})
        self.chapter['storyTimeStartId'] = 'TA-WINDOW'
        self.source['fragments'].append({'fragmentId': 'SF-TIMED-BELIEF',
            'ownerId': 'C-ALPHA', 'text': '他认为门房钥匙放在抽屉里。', 'order': 1,
            'contentRole': 'belief', 'perspectiveId': 'C-ALPHA', 'contentBindings': ['AT-LIFE'],
            'validDuring': {'fromId': 'TA-START', 'toId': 'TA-LATER'}})
        for after_id, before_id, expected in [('TA-DAY3', 'TA-DAY5', 'inside'),
                                             ('TA-DAY8', 'TA-DAY12', 'unknown'),
                                             ('TA-LATER', 'TA-DAY12', 'outside')]:
            with self.subTest(expected=expected):
                self.source['entities'][-1]['timeSpec'].update(afterId=after_id, beforeId=before_id)
                self.save()
                context = build_continuation_context(self.root, self.cid, task_type='writer',
                    pov_character='C-ALPHA', view='station', read_only=True)
                selection = context['sourceContext']['contentQuery']['selection']
                result = query_source(self.root, selection=selection, view='detail')
                self.assertEqual(bool(result['items']), expected == 'inside')
                self.assertEqual(result['coverage']['unknownTimeCount'], int(expected == 'unknown'))
                if result['items']:
                    self.assertEqual(result['items'][0]['timeMatch'], expected)

    def test_relative_validity_does_not_need_an_absolute_date(self):
        from scripts.core.source_model import SourceIndex
        from scripts.core.source_selection import time_match
        self.source['entities'].extend([
            {'entityId': 'TA-RELSTART', 'entityType': 'time_anchor', 'name': '未定开始'},
            {'entityId': 'TA-RELEND', 'entityType': 'time_anchor', 'name': '十天后',
             'timeSpec': {'kind': 'relative', 'anchorId': 'TA-RELSTART', 'offset': 10}},
            {'entityId': 'TA-RELMID', 'entityType': 'time_anchor', 'name': '五天后',
             'timeSpec': {'kind': 'relative', 'anchorId': 'TA-RELSTART', 'offset': 5}}])
        self.save()
        record = {'validDuring': {'fromId': 'TA-RELSTART', 'toId': 'TA-RELEND'}}
        index = SourceIndex.build(self.source)
        for as_of, expected in [('TA-RELSTART', 'inside'), ('TA-RELMID', 'inside'),
                                 ('TA-RELEND', 'outside'), ('TA-UNKNOWN', 'unknown')]:
            with self.subTest(as_of=as_of):
                self.assertEqual(time_match(index, record, as_of), expected)
        self.assertEqual(index.time_states['TA-RELMID']['status'], 'unknown')
        from unittest.mock import patch
        with patch('scripts.core.source_time.time_point_order', side_effect=AssertionError('repeat solve')):
            self.assertEqual(time_match(index, record, 'TA-RELMID'), 'inside')

    def test_station_context_carries_knowledge_and_dependency_hashes(self):
        from tests.fixtures_knowledge import staged_reference
        with staged_reference():
            context = build_continuation_context(self.root, self.cid, task_type='planner',
                                                 chapter_goal='关系推进', view='station', read_only=True)
        self.assertEqual(context['knowledgePack']['role'], 'REFERENCE KNOWLEDGE')
        self.assertTrue(context['knowledgePack']['items'])
        self.assertEqual(context['knowledgePack']['items'][0]['assetId'], 'KN-RELATIONSHIP-TRANSITION')
        self.assertEqual(context['dependencyManifest']['knowledge'][0]['sha256'],
                         context['knowledgePack']['items'][0]['sha256'])
        self.assertTrue(context['dependencyManifest']['sourceHash'])

    def test_applicable_required_rule_blocks_context_for_its_action(self):
        self.source['entities'].append({'entityId': 'RU-CONTEXT', 'entityType': 'rule', 'name': '审校认知',
            'ruleSpec': {'version': '1', 'method': 'semantic', 'enforcement': 'required',
                         'scopeIds': [self.source['projectId']], 'actions': ['review'],
                         'inputIds': ['SF-PLAN']}})
        self.save()
        context = build_continuation_context(self.root, self.cid, task_type='editor',
                                             view='full', read_only=True)
        self.assertFalse(context['readiness']['ready'])
        self.assertIn('SOURCE_RULES_BLOCKED', context['readiness']['reasons'])
        self.assertEqual(context['sourceContext']['ruleSummary']['blockingRuleIds'], ['RU-CONTEXT'])


if __name__ == '__main__':
    unittest.main()
