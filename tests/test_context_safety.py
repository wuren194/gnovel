"""POV isolation, mandatory evidence and budget behavior on current typed sources."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import state_cache
from scripts.core import context_builder as context
from scripts.core.project import continuation_policy
from scripts.core.source_query import query_source
from tests.test_context_builder import context_source, save_context_source


class ContextSafetyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = context_source(self.root)
        self.source['entities'].extend([
            {'entityId': 'location-DOCK', 'entityType': 'location', 'name': '旧港', 'aliases': ['码头'], 'status': 'active'},
            {'entityId': 'prop-KEY', 'entityType': 'prop', 'name': '旧钥匙', 'aliases': [], 'status': 'active'},
        ])
        self.source['references'] = [{'sourceId': 'chapter-BBB', 'targetId': 'character-ALPHA', 'kind': 'uses'},
                                     {'sourceId': 'chapter-BBB', 'targetId': 'location-DOCK', 'kind': 'located_in'}]
        self.source['attributes'].append({'attributeId': 'attribute-NEXT', 'ownerId': 'chapter-BBB', 'fieldId': 'field-PLAN', 'values': []})
        self.source['fragments'].append({'fragmentId': 'plan-NEXT', 'ownerId': 'chapter-BBB', 'order': 1, 'contentRole': 'plan',
                                         'contentBindings': ['attribute-NEXT'], 'text': '甲在旧港寻找钥匙。'})

    def build(self, *, summary=None, **kwargs):
        save_context_source(self.root, self.source)
        kwargs.setdefault('read_only', True); kwargs.setdefault('knowledge_mode', 'off')
        if summary is None:
            return context.build_station_context(self.root, 'chapter-BBB', **kwargs)
        with patch.object(context, '_load_story_summary', return_value=summary):
            return context.build_station_context(self.root, 'chapter-BBB', **kwargs)

    def assert_budget(self, result):
        pack = result['memoryPack']
        self.assertEqual(pack['budget']['usedCharacters'], len(context.serialize_context(pack)))
        self.assertLessEqual(pack['budget']['usedCharacters'], pack['budget']['total'])
        for layer in context.LAYERS:
            counts = pack['provenance'][layer]
            self.assertEqual(counts['candidates'], counts['retained'] + counts['omittedBudget'] + counts['omittedRelevance'])
            self.assertEqual(counts['retained'], len(pack[layer]))
        if result.get('view') == 'station':
            self.assertEqual(result['contextBudget']['usedCharacters'], len(context.serialize_context(result)))
            self.assertLessEqual(result['contextBudget']['usedCharacters'], result['contextBudget']['total'])
            self.assertEqual(result['contextHash'], context.sha256_json({k: v for k, v in result.items() if k != 'contextHash'}))

    def entity(self, rid):
        return next(row for row in self.source['entities'] if row['entityId'] == rid)

    def test_keyword_filter_does_not_iterate_a_list_as_a_term(self):
        self.assertTrue(context._chapter_keyword_filter('码头，调查钥匙去向', '在码头相遇'))
        self.assertFalse(context._chapter_keyword_filter('码头，调查钥匙去向', '森林中的小屋'))
        self.assertTrue(context._chapter_keyword_filter('', '任何记录'))
        self.assert_budget(self.build(chapter_goal='在码头调查钥匙去向', view='station'))

    def test_pov_applies_before_metadata_memory_and_typed_source_reads(self):
        self.entity('character-BETA')['name'] = 'SECRET-person'
        self.entity('location-DOCK')['summary'] = 'SECRET-location'
        self.entity('location-DOCK')['mandatory'] = True
        self.source['chapters'][0]['title'] = 'SECRET-title'
        self.source['fragments'].append({'fragmentId': 'belief-PRIVATE', 'ownerId': 'chapter-BBB', 'order': 2,
            'contentRole': 'belief', 'perspectiveId': 'character-BETA', 'contentBindings': ['attribute-NEXT'], 'text': 'SECRET-belief'})
        self.source['fragments'].append({'fragmentId': 'belief-KNOWN', 'ownerId': 'chapter-BBB', 'order': 3,
            'contentRole': 'belief', 'perspectiveId': 'character-ALPHA', 'contentBindings': ['attribute-NEXT'], 'text': '甲认为钥匙仍在旧港。'})
        summary = {'asOfChapterId': 'chapter-AAA', 'storySummary': 'SECRET-summary', 'characterSummaries': []}
        for view in ('full', 'station'):
            result = self.build(pov_character='character-ALPHA', summary=summary, view=view)
            self.assertNotIn('SECRET', context.serialize_context(result))
            self.assertFalse(result['readiness']['ready'])
            self.assertEqual(result['readiness']['unavailableMandatoryCount'], 1)
            self.assertNotIn('storySummary', result)
            self.assert_budget(result)
            source_read = query_source(self.root, selection=result['sourceContext']['contentQuery']['selection'], view='detail')
            text = context.serialize_context(source_read)
            self.assertIn('甲认为钥匙仍在旧港。', text)
            self.assertNotIn('SECRET-belief', text)
            self.assertNotIn('他身高一米九。', text, '客观事实不能冒充视角已知内容')
        state, _ = state_cache.project_projections(self.root)
        pack = context._build_memory_pack({'targetChapter': 'chapter-BBB', 'povCharacter': 'character-ALPHA'}, state)
        self.assertNotIn('SECRET', context.serialize_context(pack))

    def test_visibility_is_not_inferred_from_relevance_and_denials_win(self):
        self.entity('location-DOCK').update(summary='甲可见的码头', visibility='public')
        self.entity('character-BETA').update(summary='HIDDEN', visibleTo=['character-BETA'], visibility='public')
        for audience in (None, [], 'character-ALPHA'):
            self.assertFalse(context._explicitly_visible({'visibleTo': audience, 'visibility': 'public'}, 'character-ALPHA'))
        result = self.build(pov_character='character-ALPHA', chapter_goal='码头')
        self.assertIn('甲可见的码头', context.serialize_context(result['memoryPack']))
        self.assertNotIn('HIDDEN', context.serialize_context(result))

    def test_huge_optional_record_is_omitted_without_slicing_its_value(self):
        self.entity('character-ALPHA')['summary'] = '大' * 100000
        result = self.build(view='station')
        self.assert_budget(result)
        self.assertGreater(result['memoryPack']['provenance']['semantic']['omittedBudget'], 0)
        self.assertFalse(any(row['subject'] == 'character-ALPHA' and row['field'] == 'entity' for row in result['memoryPack']['semantic']))
        self.assertNotIn('大' * 100, context.serialize_context(result))

    def test_outer_envelope_is_bounded_even_when_recent_title_is_huge(self):
        self.source['chapters'][0]['title'] = '旧' * 100000
        result = self.build(view='station')
        self.assert_budget(result)
        self.assertEqual(result['recentChapterProvenance']['omittedBudget'], 1)
        self.assertEqual(result['recentChapters'][0]['chapterId'], 'chapter-BBB')

    def test_mandatory_metadata_and_required_typed_rules_are_never_silently_dropped(self):
        self.entity('location-DOCK').update(summary='不能进入封闭区域', mandatory=True)
        self.source['entities'].append({'entityId': 'rule-GATE', 'entityType': 'rule', 'name': '出入限制',
            'ruleSpec': {'version': '1', 'method': 'semantic', 'enforcement': 'required',
                         'scopeIds': ['chapter-BBB'], 'actions': ['prepare'], 'inputIds': ['plan-NEXT']}})
        result = self.build(chapter_goal='探索森林', view='station')
        mandatory = [r for layer in context.LAYERS for r in result['memoryPack'][layer] if r['mandatory']]
        self.assertIn('location-DOCK', {row['subject'] for row in mandatory})
        self.assertFalse(result['readiness']['ready'])
        self.assertEqual(result['sourceContext']['ruleSummary']['blockingRuleIds'], ['rule-GATE'])
        self.assert_budget(result)
        self.entity('location-DOCK')['summary'] = '巨' * 100000
        with self.assertRaisesRegex(context.ContextBuilderError, 'CONTEXT_BUDGET_IMPOSSIBLE'):
            self.build(view='station')
        self.entity('location-DOCK')['summary'] = '不能进入封闭区域'
        with self.assertRaisesRegex(context.ContextBuilderError, 'CONTEXT_BUDGET_IMPOSSIBLE'):
            self.build(chapter_goal='纲' * 100000)

    def test_explicit_chapter_references_and_aliases_select_registered_entities(self):
        result = self.build(chapter_goal='完全不同的长篇章节目标')
        subjects = {r['subject'] for r in result['memoryPack']['semantic']}
        self.assertTrue({'character-ALPHA', 'location-DOCK'} <= subjects)
        self.assertNotIn('character-BETA', subjects)
        self.source['references'] = []
        result = self.build(chapter_goal='本章必须前往码头寻找证据')
        self.assertIn('location-DOCK', {r['subject'] for r in result['memoryPack']['semantic']})

    def test_deterministic_output_and_registry_budgets_for_each_task(self):
        for task in continuation_policy()['memoryBudgets']:
            first = self.build(task_type=task, view='station')
            self.assertEqual(first, self.build(task_type=task, view='station'))
            self.assert_budget(first)
        first = self.build()['memoryPack']
        self.source['entities'].reverse()
        self.assertEqual(first, self.build()['memoryPack'])

    def test_recent_chapter_paths_exclude_archived_chapters(self):
        self.source['chapters'][0]['status'] = 'archived'
        result = self.build(view='station', recent=2)
        self.assertEqual([r['chapterId'] for r in result['recentChapters']], ['chapter-BBB'])
        self.assertEqual(result['recentChapterProvenance']['input'], 1)

    def test_registry_budget_changes_selection_and_impossible_envelope_fails(self):
        for i in range(20):
            self.source['entities'].append({'entityId': f'location-EXTRA-{i}', 'entityType': 'location', 'name': f'地点{i}',
                                           'summary': '文' * 500, 'status': 'active', 'aliases': []})
            self.source['references'].append({'sourceId': 'chapter-BBB', 'targetId': f'location-EXTRA-{i}', 'kind': 'uses'})
        policy = continuation_policy()
        with patch('scripts.core.project.workflow_policy', return_value={'continuation': policy}):
            policy['memoryBudgets']['planner'] = 10**9
            baseline = self.build(view='station')
            mandatory = deepcopy(baseline)
            for layer in context.LAYERS:
                mandatory['memoryPack'][layer] = [row for row in mandatory['memoryPack'][layer] if row['mandatory']]
            minimum = context._measure(mandatory, 'contextBudget')
            policy['memoryBudgets']['planner'] = minimum + 500
            small = self.build(view='station')
            policy['memoryBudgets']['planner'] = minimum + 7000
            large = self.build(view='station')
            self.assertGreater(large['memoryPack']['provenance']['semantic']['retained'], small['memoryPack']['provenance']['semantic']['retained'])
            self.assert_budget(small); self.assert_budget(large)
            policy['memoryBudgets']['planner'] = 1
            with self.assertRaisesRegex(context.ContextBuilderError, 'CONTEXT_BUDGET_IMPOSSIBLE'): self.build(view='station')

    def test_pov_denial_of_required_evidence_reports_not_ready(self):
        self.entity('character-ALPHA').update(mandatory=True, visibleTo=[])
        result = self.build(pov_character='character-ALPHA', view='station')
        self.assertFalse(result['readiness']['ready'])
        self.assertEqual(result['readiness']['unavailableMandatoryCount'], 1)
        self.assert_budget(result)

    def test_scoped_summary_excludes_other_character_summaries(self):
        summary = {'povCharacter': 'character-ALPHA', 'asOfChapterId': 'chapter-AAA',
                   'storySummary': '当前视角摘要', 'plotLine': [], 'characterSummaries': [
                       {'characterId': 'character-ALPHA', 'summary': '自身状态'},
                       {'characterId': 'character-BETA', 'summary': 'SECRET-other'}]}
        result = self.build(pov_character='character-ALPHA', summary=summary)
        self.assertEqual(result['storySummary']['storySummary'], '当前视角摘要')
        self.assertNotIn('SECRET', context.serialize_context(result))

    def test_required_station_envelope_overflow_is_not_silently_removed(self):
        baseline = self.build()
        policy = continuation_policy()
        policy['memoryBudgets']['planner'] = baseline['memoryPack']['budget']['usedCharacters'] + 50
        with patch('scripts.core.project.workflow_policy', return_value={'continuation': policy}):
            with self.assertRaisesRegex(context.ContextBuilderError, 'CONTEXT_BUDGET_IMPOSSIBLE'):
                self.build(view='station')

    def test_configuration_rejects_missing_tasks_bool_zero_and_wrong_units(self):
        policy = continuation_policy()
        for broken in ({**policy, 'memoryBudgets': {'planner': 10}},
                       {**policy, 'memoryBudgets': {**policy['memoryBudgets'], 'planner': True}},
                       {**policy, 'memoryBudgets': {**policy['memoryBudgets'], 'writer': 0}},
                       {**policy, 'memoryBudgetUnit': 'bytes'}):
            with patch('scripts.core.project.workflow_policy', return_value={'continuation': broken}):
                with self.assertRaisesRegex(context.ContextBuilderError, 'CONTINUATION_CONFIG_INVALID'):
                    self.build()

    def test_escaping_and_unicode_are_counted_as_serialized_characters(self):
        self.entity('character-ALPHA')['summary'] = '中文"\\\n测试🌙' * 30
        result = self.build(view='station')
        self.assert_budget(result)
        self.assertGreater(len(context.serialize_context(result).encode()), len(context.serialize_context(result)))


if __name__ == '__main__': unittest.main()
