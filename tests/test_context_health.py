from __future__ import annotations

import tempfile
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core import knowledge_store
from scripts.core.context_health import assess_context, query_context_health
from scripts.core.jsonio import write_json
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.structure_store import load_structure
from tests.test_source_context import SourceContextTests


class ContextHealthTests(unittest.TestCase):
    def setUp(self):
        fixture = SourceContextTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.chapter = fixture.root, fixture.cid

    def test_live_health_and_stale_hash(self):
        result = query_context_health(self.root, chapterId=self.chapter, task='writer')
        self.assertEqual(result['code'], 'CONTEXT_HEALTH')
        self.assertTrue(result['ready'])
        self.assertTrue(result['coverage']['builtContextOnly'])
        stale = query_context_health(self.root, chapterId=self.chapter, task='writer', expectedHash='old-hash')
        self.assertFalse(stale['ready'])
        self.assertIn('CONTEXT_SNAPSHOT_STALE', {row['code'] for row in stale['issues']})

    def test_duplicate_and_conflict_are_distinct(self):
        from scripts.continuation_context import build_continuation_context
        context = build_continuation_context(str(self.root), self.chapter, task_type='writer', view='station', read_only=True)
        source_layer = next(layer for layer in ('working', 'episodic', 'semantic') if context['memoryPack'][layer])
        row = deepcopy(context['memoryPack'][source_layer][0])
        context['memoryPack'][source_layer].append(row)
        health = assess_context(context)
        self.assertIn('CONTEXT_DUPLICATE', {item['code'] for item in health['issues']})
        row['value'] = '冲突值'
        self.assertFalse(assess_context(context)['ready'])


class ContextHealthContractTests(unittest.TestCase):
    """Context Health 检查统一 Builder 契约，而不是重新发明上下文。"""

    def setUp(self):
        from tests.test_context_builder import context_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / '正文').mkdir()
        self.source = context_source(self.root)

    def build(self, **kwargs):
        from scripts.core.context_builder import build_station_context
        kwargs.setdefault('task_type', 'planner')
        kwargs.setdefault('view', 'station')
        kwargs.setdefault('read_only', True)
        return build_station_context(str(self.root), 'chapter-AAA', **kwargs)

    def save(self, source):
        from tests.test_context_builder import save_context_source
        save_context_source(self.root, source)

    def codes(self, health):
        return {row['code'] for row in health['issues']}

    def severity(self, health, code):
        return {row['severity'] for row in health['issues'] if row['code'] == code}

    def status(self, health, code):
        return {row['status'] for row in health['issues'] if row['code'] == code}

    def layers(self, context):
        return {row['role']: row for row in context['layers']}

    def test_every_issue_carries_a_status_from_the_shared_vocabulary(self):
        """Context Health owns classification: every issue must carry a valid status."""
        from scripts.core.context_health import CONTEXT_ISSUE_STATUS
        valid = {'mechanical_error', 'stale', 'needs_rebuild', 'author_decision_required',
                 'semantic_review_required', 'informational'}
        self.assertTrue(set(CONTEXT_ISSUE_STATUS.values()) <= valid)
        context = self.build()
        for row in assess_context(context)['issues']:
            with self.subTest(code=row['code']):
                self.assertIn(row['status'], valid)
                self.assertIn('recommendedAction', row)

    def test_chapter_plan_missing_is_an_author_decision_not_a_rebuild(self):
        """缺章节计划是作者去补规划，不是引擎需要重建——这是本次修复的核心分类。"""
        changed = load_structure(self.root)
        changed['fragments'] = [row for row in changed.get('fragments', [])
                                if row.get('contentRole') != 'plan']
        self.save(changed)
        health = assess_context(self.build(task_type='writer'))
        self.assertIn('CONTEXT_CHAPTER_PLAN_MISSING', self.codes(health))
        self.assertEqual(self.status(health, 'CONTEXT_CHAPTER_PLAN_MISSING'), {'author_decision_required'})
        self.assertEqual(self.severity(health, 'CONTEXT_CHAPTER_PLAN_MISSING'), {'warning'})

    def test_summary_stale_is_classified_stale(self):
        health = assess_context(self.build())
        if 'CONTEXT_SUMMARY_STALE' in self.codes(health):
            self.assertEqual(self.status(health, 'CONTEXT_SUMMARY_STALE'), {'stale'})

    def test_dependency_missing_is_needs_rebuild_not_semantic_review(self):
        context = self.build()
        context['dependencyManifest'].pop('sourceHash')
        context['dependencyManifest'].pop('sourcePlanHash')
        health = assess_context(context)
        self.assertFalse(health['ready'])
        self.assertEqual(self.status(health, 'CONTEXT_DEPENDENCY_MISSING'), {'needs_rebuild'})

    def test_budget_omissions_are_informational_never_needs_rebuild(self):
        """预算省略是提示，不能让一个 ready 的上下文被误报成 needs_rebuild。"""
        with self._staged_asset():
            context = self.build(chapter_goal='关系推进')
        context['knowledgePack']['items'].pop()
        context['dependencyManifest']['knowledge'] = [
            {'assetId': row['assetId'], 'sha256': row['sha256']} for row in context['knowledgePack']['items']]
        context['knowledgePack']['omittedBudget'] = 1
        health = assess_context(context)
        self.assertTrue(health['ready'], health['issues'])
        self.assertEqual(self.status(health, 'CONTEXT_KNOWLEDGE_OMITTED'), {'informational'})
        for row in health['issues']:
            if row['code'] in {'CONTEXT_KNOWLEDGE_OMITTED', 'CONTEXT_OPTIONAL_OMITTED'}:
                self.assertNotEqual(row['status'], 'needs_rebuild')

    def test_contract_and_conflict_faults_are_mechanical_or_semantic(self):
        context = self.build()
        tampered = deepcopy(context)
        next(row for row in tampered['layers'] if row['role'] == 'CURRENT BELIEFS')['authoritative'] = True
        self.assertEqual(self.status(assess_context(tampered), 'CONTEXT_LAYER_CONTRACT_INVALID'),
                         {'mechanical_error'})
        conflict = deepcopy(context)
        source_layer = next(layer for layer in ('working', 'episodic', 'semantic') if conflict['memoryPack'][layer])
        row = deepcopy(conflict['memoryPack'][source_layer][0])
        row['value'] = {'conflicting': True}
        conflict['memoryPack'][source_layer].append(row)
        self.assertEqual(self.status(assess_context(conflict), 'CONTEXT_VALUE_CONFLICT'),
                         {'semantic_review_required'})
        pov = self.build(pov_character='character-ALPHA')
        pov.pop('filterProvenance')
        self.assertEqual(self.status(assess_context(pov), 'CONTEXT_POV_PROVENANCE_MISSING'),
                         {'mechanical_error'})

    def test_mandatory_unavailable_follows_reasons_not_a_blind_semantic_guess(self):
        from scripts.core.context_health import _mandatory_unavailable_status
        self.assertEqual(_mandatory_unavailable_status(['SOURCE_RULES_BLOCKED']),
                         'semantic_review_required')
        self.assertEqual(_mandatory_unavailable_status(['POV_HARD_CONSTRAINTS_UNAVAILABLE']),
                         'mechanical_error')
        self.assertEqual(_mandatory_unavailable_status(
            ['SOURCE_RULES_BLOCKED', 'POV_HARD_CONSTRAINTS_UNAVAILABLE']), 'mechanical_error')
        self.assertEqual(_mandatory_unavailable_status([]), 'mechanical_error')


    def test_health_accepts_current_builder_snapshot(self):
        context = self.build()
        health = assess_context(context)
        self.assertTrue(health['ready'], health['issues'])
        self.assertEqual(health['code'], 'CONTEXT_HEALTH')
        self.assertEqual(health['contextHash'], context['contextHash'])
        self.assertEqual([row for row in health['issues'] if row['severity'] == 'blocking'], [])
        self.assertEqual(self.codes(health), {'CONTEXT_SUMMARY_STALE'}, '无摘要只是警告')
        self.assertTrue(health['coverage']['builtContextOnly'])
        self.assertFalse(health['coverage']['semanticContradictionsExhaustive'])
        self.assertFalse(health['coverage']['implicitProseScanned'])

    def test_old_context_hash_is_stale_after_canon_change(self):
        before = self.build()
        self.assertTrue(assess_context(before)['ready'])
        changed = load_structure(self.root)
        changed['entities'][0]['name'] = '改名后的人物甲'
        self.save(changed)
        after = self.build()
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        stale = assess_context(before, expectedHash=after['contextHash'])
        self.assertFalse(stale['ready'])
        self.assertEqual(self.severity(stale, 'CONTEXT_SNAPSHOT_STALE'), {'blocking'})
        self.assertEqual(assess_context(after, expectedHash=after['contextHash'])['ready'], True)

    def test_old_context_is_stale_after_voice_change(self):
        from scripts.core.work_voice_profile import AXES, preview_voice_profile
        from scripts.core.structure_commands import commit_plan
        axes = {key: '适度变化' for key in AXES}
        for action, extra in (('set', {'axes': axes, 'examples': ['例证'], 'applicability': '全书'}),
                              ('approve', {'approvalActor': 'author', 'approvalEvidence': '审阅'})):
            plan = preview_voice_profile(self.root, action=action,
                                         expectedRevision=load_structure(self.root)['revision'],
                                         reason='声纹', **extra)
            commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        active = self.build(task_type='writer')
        self.assertIn('voiceProfileHash', active['dependencyManifest'])
        axes['emotionLabeling'] = '减少直接标签'
        plan = preview_voice_profile(self.root, action='set', expectedRevision=load_structure(self.root)['revision'],
                                     reason='调整', axes=axes, examples=['新例证'], applicability='全书')
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        draft = self.build(task_type='writer')
        self.assertNotEqual(active['contextHash'], draft['contextHash'])
        stale = assess_context(active, expectedHash=draft['contextHash'])
        self.assertFalse(stale['ready'])
        self.assertEqual(self.severity(stale, 'CONTEXT_SNAPSHOT_STALE'), {'blocking'})

    def test_old_context_is_stale_after_knowledge_change(self):
        with self._staged_asset() as assets:
            before = self.build(chapter_goal='关系推进')
            self.assertTrue(before['knowledgePack']['items'])
            self.assertEqual(before['knowledgePack']['items'][0]['assetId'], 'KN-RELATIONSHIP-TRANSITION')
            asset = assets / 'KN-RELATIONSHIP-TRANSITION.md'
            asset.write_text(asset.read_text(encoding='utf-8').replace('# 方法', '# 方法（改）'), encoding='utf-8')
            with patch.object(knowledge_store, 'ASSETS', assets):
                after = self.build(chapter_goal='关系推进')
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        self.assertNotEqual(before['dependencyManifest']['knowledge'][0]['sha256'],
                            after['dependencyManifest']['knowledge'][0]['sha256'])
        self.assertFalse(assess_context(before, expectedHash=after['contextHash'])['ready'])

    def test_old_context_is_stale_after_decision_change(self):
        from scripts.core.author_decisions import preview_decision_change
        from scripts.core.structure_commands import commit_plan
        before = self.build()
        plan = preview_decision_change(self.root, action='create',
                                       expectedRevision=load_structure(self.root)['revision'],
                                       reason='规划控制', statement='秘密不得过早公开', rationale='延迟揭示',
                                       scopeIds=['chapter-AAA'], dependsOn=[])
        decision_id = plan['parameters']['decisionId']
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        plan = preview_decision_change(self.root, action='approve',
                                       expectedRevision=load_structure(self.root)['revision'],
                                       reason='确认', decisionId=decision_id,
                                       approvalActor='author', approvalEvidence='本地确认')
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        after = self.build()
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        self.assertEqual([row['decisionId'] for row in after['dependencyManifest']['decisions']], [decision_id])
        self.assertTrue(after['dependencyManifest']['decisions'][0]['sha256'])
        self.assertFalse(assess_context(before, expectedHash=after['contextHash'])['ready'])

    def test_missing_mandatory_dependency_blocks(self):
        context = self.build()
        context['dependencyManifest'].pop('sourceHash')
        context['dependencyManifest'].pop('sourcePlanHash')
        health = assess_context(context)
        self.assertFalse(health['ready'])
        self.assertEqual(self.severity(health, 'CONTEXT_DEPENDENCY_MISSING'), {'blocking'})

    def test_knowledge_budget_omission_is_warning_not_blocking(self):
        with self._staged_asset():
            context = self.build(chapter_goal='关系推进')
        dropped = context['knowledgePack']['items'].pop()
        context['dependencyManifest']['knowledge'] = [
            {'assetId': row['assetId'], 'sha256': row['sha256']} for row in context['knowledgePack']['items']]
        context['knowledgePack']['omittedBudget'] = 1
        health = assess_context(context)
        self.assertTrue(health['ready'], health['issues'])
        self.assertEqual(self.severity(health, 'CONTEXT_KNOWLEDGE_OMITTED'), {'warning'})
        self.assertNotIn('CONTEXT_KNOWLEDGE_PROVENANCE_INVALID', self.codes(health))
        self.assertNotIn(dropped['assetId'], [row['assetId'] for row in context['knowledgePack']['items']])
        self.assertIn(dropped['assetId'], [row['assetId'] for row in
                                          self.layers(context)['REFERENCE KNOWLEDGE']['items']],
                      '层在构建时冻结；预算后的层同步由 Builder 在 _station_view 内完成')

    def test_applicable_stale_decision_requires_review_before_ready(self):
        from scripts.core.author_decisions import preview_decision_change
        from scripts.core.structure_commands import commit_plan
        plan = preview_decision_change(self.root, action='create',
                                       expectedRevision=load_structure(self.root)['revision'],
                                       reason='规划控制', statement='依赖人物甲', rationale='规划',
                                       scopeIds=['chapter-AAA', 'character-ALPHA'], dependsOn=[])
        decision_id = plan['parameters']['decisionId']
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        plan = preview_decision_change(self.root, action='approve',
                                       expectedRevision=load_structure(self.root)['revision'],
                                       reason='确认', decisionId=decision_id,
                                       approvalActor='author', approvalEvidence='本地确认')
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        changed = load_structure(self.root)
        changed['entities'][0]['name'] = '改名后的人物甲'
        self.save(changed)
        context = self.build()
        self.assertIn(decision_id, context['staleDecisionIds'])
        self.assertNotIn(decision_id, [row['decisionId'] for row in context['authorDecisions']])
        self.assertNotIn(decision_id, [row['id'] for row in self.layers(context)['AUTHOR DECISIONS']['items']])
        health = assess_context(context)
        self.assertFalse(health['ready'], '当前章仍适用的作者决定必须明确核对，不能过滤后假报就绪')
        self.assertEqual(self.severity(health, 'CONTEXT_DECISIONS_STALE'), {'blocking'})
        self.assertEqual(context['unavailableDecisions'][0]['statement'], '依赖人物甲')

    def test_pov_provenance_missing_is_blocking(self):
        context = self.build(pov_character='character-ALPHA')
        self.assertTrue(assess_context(context)['ready'], '带视角的上下文本身应带过滤证据')
        context.pop('filterProvenance')
        health = assess_context(context)
        self.assertFalse(health['ready'])
        self.assertEqual(self.severity(health, 'CONTEXT_POV_PROVENANCE_MISSING'), {'blocking'})

    def test_layer_contract_is_checked(self):
        context = self.build()
        self.assertNotIn('CONTEXT_LAYER_CONTRACT_INVALID', self.codes(assess_context(context)))
        tampered = deepcopy(context)
        next(row for row in tampered['layers'] if row['role'] == 'CURRENT BELIEFS')['authoritative'] = True
        health = assess_context(tampered)
        self.assertFalse(health['ready'])
        self.assertEqual(self.severity(health, 'CONTEXT_LAYER_CONTRACT_INVALID'), {'blocking'})
        # 语义契约层不得冒充世界事实层。
        demoted = deepcopy(context)
        next(row for row in demoted['layers']
             if row['role'] == 'CANON SEMANTIC CONTRACT')['epistemicRole'] = 'canon_fact'
        self.assertEqual(self.severity(assess_context(demoted), 'CONTEXT_LAYER_CONTRACT_INVALID'), {'blocking'})
        injected = deepcopy(context)
        next(row for row in injected['layers']
             if row['role'] == 'CANON SEMANTIC CONTRACT')['contentInjected'] = True
        self.assertEqual(self.severity(assess_context(injected), 'CONTEXT_LAYER_CONTRACT_INVALID'), {'blocking'})
        dropped = [row for row in deepcopy(context)['layers'] if row['role'] != 'CANON SEMANTIC CONTRACT']
        missing = deepcopy(context)
        missing['layers'] = dropped
        self.assertEqual(self.severity(assess_context(missing), 'CONTEXT_LAYER_CONTRACT_INVALID'), {'blocking'})

    def test_cross_epistemic_difference_is_not_context_conflict(self):
        context = self.build()
        roles = self.layers(context)
        self.assertTrue(roles['CANON FACTS']['items'])
        self.assertTrue(roles['CURRENT BELIEFS']['items'])
        health = assess_context(context)
        self.assertTrue(health['ready'])
        self.assertNotIn('CONTEXT_VALUE_CONFLICT', self.codes(health))
        self.assertNotIn('CONTEXT_DUPLICATE', self.codes(health))

    def test_same_epistemic_slot_conflict_is_blocking(self):
        context = self.build()
        source_layer = next(layer for layer in ('working', 'episodic', 'semantic') if context['memoryPack'][layer])
        row = deepcopy(context['memoryPack'][source_layer][0])
        row['value'] = {'conflicting': True}
        context['memoryPack'][source_layer].append(row)
        health = assess_context(context)
        self.assertFalse(health['ready'])
        self.assertEqual(self.severity(health, 'CONTEXT_VALUE_CONFLICT'), {'blocking'})

    def test_query_health_builds_current_snapshot_and_reports_stale(self):
        stale_hash = 'old-hash'
        result = query_context_health(self.root, chapterId='chapter-AAA', task='writer', expectedHash=stale_hash)
        self.assertFalse(result['ready'])
        self.assertEqual(self.severity(result, 'CONTEXT_SNAPSHOT_STALE'), {'blocking'})
        self.assertEqual(result['expectedHash'], stale_hash)
        fresh = query_context_health(self.root, chapterId='chapter-AAA', task='writer')
        self.assertTrue(fresh['ready'], fresh['issues'])

    def _staged_asset(self):
        from tests.fixtures_knowledge import staged_reference
        return staged_reference()
