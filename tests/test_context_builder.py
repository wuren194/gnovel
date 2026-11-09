"""P5 统一 Context Builder 契约测试。

覆盖：确定性 contextHash、POV/目标依赖、Canon/计划/知识/声纹变化失效、
认识层不互相冒充、预算驱逐顺序、只读性与依赖清单可审计。
"""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core import context_builder as builder
from scripts.core.context_health import assess_context
from scripts.core.jsonio import write_json
from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, validate_structure
from tests.fixtures_source import single_source_chapter, source_fixture

LAYER_ROLES = ["CANON FACTS", "CANON SEMANTIC CONTRACT", "CURRENT BELIEFS", "IN-WORLD TEXT",
               "APPROVED PLAN", "UNAPPROVED CANDIDATES", "AUTHOR DECISIONS", "REFERENCE KNOWLEDGE",
               "WORK VOICE PROFILE"]


def context_source(root: Path, *, fragments: tuple = (), candidates: tuple = (),
                   decisions: tuple = (), voice: dict | None = None, chapters: int = 2) -> dict:
    """Current single source with chapter-scoped fact/belief/in-world-text/plan fragments."""
    source = source_fixture()
    volume = source['volumes'][0]['volumeId']
    arc = source['arcs'][0]['arcId']
    source['chapters'] = [single_source_chapter(source, 'CH-AAA', volume_id=volume, ordinal=1)]
    source['chapterArcLinks'] = [{'chapterId': 'CH-AAA', 'arcId': arc, 'role': 'primary'}]
    if chapters > 1:
        source['chapters'].append(single_source_chapter(source, 'CH-BBB', volume_id=volume, ordinal=2))
        source['chapterArcLinks'].append({'chapterId': 'CH-BBB', 'arcId': arc, 'role': 'primary'})
    source['references'] = [{'sourceId': 'CH-AAA', 'targetId': 'C-ALPHA', 'kind': 'uses'}]
    source['fieldDefinitions'].append({'fieldId': 'FD-PLAN', 'key': 'plot.plan', 'label': '剧情计划',
                                      'valueType': 'topic', 'appliesTo': ['chapter']})
    source['attributes'].append({'attributeId': 'AT-PLAN', 'ownerId': 'CH-AAA', 'fieldId': 'FD-PLAN', 'values': []})
    source['fragments'].append({'fragmentId': 'SF-PLAN', 'ownerId': 'CH-AAA', 'text': '甲守城门。', 'order': 1,
                                'contentRole': 'plan', 'contentBindings': ['AT-PLAN'], 'dependsOn': ['FV-HEIGHT']})
    source['fragments'].append({'fragmentId': 'SF-BELIEF', 'ownerId': 'CH-AAA', 'text': '甲认为乙已死。', 'order': 2,
                                'contentRole': 'belief', 'perspectiveId': 'C-ALPHA', 'contentBindings': ['AT-PLAN']})
    source['fragments'].append({'fragmentId': 'SF-TEXT', 'ownerId': 'CH-AAA', 'text': '信上写着他将离开。', 'order': 3,
                                'contentRole': 'in_world_text', 'perspectiveId': 'C-BETA', 'contentBindings': ['AT-PLAN']})
    source['fragments'].extend(fragments)
    source['planningCandidates'] = list(candidates)
    source['authorDecisions'] = list(decisions)
    if voice is not None:
        source['workVoiceProfile'] = voice
    save_context_source(root, source)
    return source


def save_context_source(root: Path, source: dict) -> None:
    validate_structure(source)
    write_json(root / '.gnovel/structure.json', source)
    write_json(root / '.gnovel/project.json', {'schemaVersion': PROJECT_SCHEMA_VERSION,
                                                 'workflowVersion': WORKFLOW_VERSION,
                                                 'projectId': source['projectId']})


def build(root: Path, *, chapter: str = 'CH-AAA', **kwargs) -> dict:
    kwargs.setdefault('task_type', 'planner')
    kwargs.setdefault('view', 'station')
    kwargs.setdefault('read_only', True)
    return builder.build_station_context(str(root), chapter, **kwargs)


def layers_by_role(context: dict) -> dict:
    return {row['role']: row for row in context['layers']}


def inventory(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        from tests.fixtures_knowledge import staged_reference
        self.enterContext(staged_reference())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / '正文').mkdir()
        self.source = context_source(self.root)

    def save(self, source: dict | None = None):
        save_context_source(self.root, source if source is not None else self.source)

    def approve_decision(self, **kwargs):
        from scripts.core.author_decisions import preview_decision_change
        from scripts.core.structure_commands import commit_plan
        from scripts.core.structure_store import load_structure
        revision = load_structure(self.root)['revision']
        create = preview_decision_change(self.root, action='create', expectedRevision=revision,
                                        reason='规划控制', **kwargs)
        decision_id = create['parameters']['decisionId']
        commit_plan(self.root, operation_id=create['operationId'], plan_hash=create['planHash'], approved=True)
        approve = preview_decision_change(self.root, action='approve',
                                         expectedRevision=load_structure(self.root)['revision'],
                                         reason='作者确认', decisionId=decision_id,
                                         approvalActor='author', approvalEvidence='本地确认')
        commit_plan(self.root, operation_id=approve['operationId'], plan_hash=approve['planHash'], approved=True)
        return decision_id

    # --- 确定性与依赖 ---

    def test_same_inputs_produce_same_context_hash(self):
        first = build(self.root, chapter_goal='关系推进')
        second = build(self.root, chapter_goal='关系推进')
        self.assertEqual(first['contextHash'], second['contextHash'])
        self.assertEqual(first['dependencyManifest'], second['dependencyManifest'])
        self.assertEqual(first, second)

    def test_pov_change_changes_context_hash(self):
        plain = build(self.root)['contextHash']
        alpha = build(self.root, pov_character='C-ALPHA')['contextHash']
        beta = build(self.root, pov_character='C-BETA')['contextHash']
        self.assertEqual(len({plain, alpha, beta}), 3)
        manifest = build(self.root, pov_character='C-ALPHA')['dependencyManifest']
        self.assertEqual(manifest['povCharacter'], 'C-ALPHA')

    def test_chapter_goal_changes_knowledge_and_context_hash(self):
        related = build(self.root, chapter_goal='关系推进')
        unrelated = build(self.root, chapter_goal='completely-unrelated-query')
        empty = build(self.root)
        self.assertEqual(related['knowledgePack']['items'][0]['assetId'], 'KN-RELATIONSHIP-TRANSITION')
        self.assertEqual(unrelated['knowledgeRoute']['items'], [])
        self.assertEqual(unrelated['knowledgePack']['items'], [])
        self.assertEqual(empty['knowledgePack']['items'], [])
        self.assertEqual(empty['knowledgePack']['mode'], 'curated')
        self.assertEqual(len({related['contextHash'], unrelated['contextHash'], empty['contextHash']}), 3)
        self.assertNotEqual(related['dependencyManifest']['chapterGoalHash'],
                            unrelated['dependencyManifest']['chapterGoalHash'])
        # 知识路由只发生一次：pack 与 route 报告同一份选择结果。
        self.assertEqual([row['assetId'] for row in related['knowledgeRoute']['items']],
                         [row['assetId'] for row in related['knowledgePack']['items']])

    def test_canon_change_invalidates_old_context(self):
        before = build(self.root)
        self.source['entities'][0]['name'] = '改名后的人物甲'
        self.save()
        after = build(self.root)
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        self.assertNotEqual(before['dependencyManifest']['sourceHash'],
                            after['dependencyManifest']['sourceHash'])
        health = assess_context(before, expectedHash=after['contextHash'])
        self.assertFalse(health['ready'])
        self.assertIn('CONTEXT_SNAPSHOT_STALE', {row['code'] for row in health['issues']})

    def test_source_plan_change_invalidates_old_context(self):
        before = build(self.root)
        plan_fragment = next(row for row in self.source['fragments'] if row['fragmentId'] == 'SF-PLAN')
        plan_fragment['text'] = '甲弃门而去。'
        self.save()
        after = build(self.root)
        self.assertNotEqual(before['sourceContext']['sourcePlanHash'], after['sourceContext']['sourcePlanHash'])
        self.assertNotEqual(before['contextHash'], after['contextHash'])

    def test_unrelated_source_change_conservatively_invalidates_context(self):
        """sourceHash 是**整份 source snapshot** 的身份：这是保守失效，不是精确依赖。

        与本章无关、且没有被装进上下文的改动，仍会让 sourceHash / contextHash 变化。
        sourcePlanHash 与记录级 hash 才提供局部 provenance。
        """
        before = build(self.root)
        self.approve_decision(statement='只约束人物乙', rationale='与本章无关',
                              scopeIds=['C-BETA'], dependsOn=[])
        after = build(self.root)
        # 没被注入：决定不在闭包内，层与记忆包完全不变。
        self.assertEqual(after['authorDecisions'], [])
        self.assertEqual(after['dependencyManifest']['decisions'], [])
        self.assertEqual(before['layers'], after['layers'])
        self.assertEqual(before['memoryPack'], after['memoryPack'])
        # 局部 provenance 证明本章计划输入没变……
        self.assertEqual(before['sourceContext']['sourcePlanHash'], after['sourceContext']['sourcePlanHash'])
        # ……但整份快照身份仍然变化，旧上下文被保守判为过期。
        self.assertNotEqual(before['dependencyManifest']['sourceHash'],
                            after['dependencyManifest']['sourceHash'])
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        health = assess_context(before, expectedHash=after['contextHash'])
        self.assertFalse(health['ready'])
        self.assertIn('CONTEXT_SNAPSHOT_STALE', {row['code'] for row in health['issues']})

    def test_dependency_coverage_declares_full_source_snapshot(self):
        coverage = build(self.root)['sourceContext']['coverage']
        self.assertTrue(coverage['fullSourceSnapshot'])
        self.assertTrue(coverage['conservativeInvalidation'])

    def test_recent_prose_change_changes_dependency_snapshot(self):
        prose = self.root / '正文/CH-AAA.md'
        prose.write_text('第一章正文。', encoding='utf-8')
        before = build(self.root, chapter='CH-BBB')
        row = next(item for item in before['dependencyManifest']['recentChapters']
                   if item['chapterId'] == 'CH-AAA')
        self.assertIsNotNone(row['sha256'])
        prose.write_text('第一章正文，已改写。', encoding='utf-8')
        after = build(self.root, chapter='CH-BBB')
        changed = next(item for item in after['dependencyManifest']['recentChapters']
                       if item['chapterId'] == 'CH-AAA')
        self.assertNotEqual(row['sha256'], changed['sha256'])
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        prose.unlink()
        missing = build(self.root, chapter='CH-BBB')
        self.assertIsNone(next(item for item in missing['dependencyManifest']['recentChapters']
                               if item['chapterId'] == 'CH-AAA')['sha256'])

    # --- 认识层 ---

    def test_layers_cover_every_role_in_a_stable_order(self):
        context = build(self.root)
        self.assertEqual([row['role'] for row in context['layers']], LAYER_ROLES)
        for row in context['layers']:
            self.assertIn('epistemicRole', row)
            self.assertIsInstance(row['authoritative'], bool)
            self.assertIsInstance(row['contentInjected'], bool)

    def test_field_definitions_are_semantic_contract_not_canon_fact(self):
        """FD-* 规定属性怎么解释，属于 Canon 语义契约，不是世界事实本身。"""
        roles = layers_by_role(build(self.root))
        contract = roles['CANON SEMANTIC CONTRACT']
        self.assertEqual(contract['epistemicRole'], 'canon_semantic_contract')
        self.assertTrue(contract['authoritative'])
        self.assertFalse(contract['contentInjected'])
        self.assertIn('FD-PLAN', [row['id'] for row in contract['items']])
        # 语义契约绝不混进事实、计划、参考知识或信念层。
        contract_ids = {row['id'] for row in contract['items']}
        for role in ('CANON FACTS', 'APPROVED PLAN', 'REFERENCE KNOWLEDGE',
                     'CURRENT BELIEFS', 'IN-WORLD TEXT', 'UNAPPROVED CANDIDATES'):
            other = {row['id'] for row in roles[role]['items'] if 'id' in row}
            self.assertFalse(contract_ids & other, f'{role} 不得收纳 FieldDefinition')

    def test_attribute_value_and_fact_fragment_stay_canon_facts(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / '正文').mkdir()
        context_source(root, fragments=[{'fragmentId': 'SF-FACT-HERE', 'ownerId': 'CH-AAA',
                                         'text': '甲守着城门，未曾退开半步。', 'order': 5,
                                         'contentRole': 'fact', 'contentBindings': ['AT-PLAN']}])
        roles = layers_by_role(build(root))
        canon = {row['id'] for row in roles['CANON FACTS']['items']}
        self.assertIn('AT-PLAN', canon)        # Attribute 仍是 Canon fact
        self.assertIn('FV-HEIGHT', canon)      # Value 仍是 Canon fact
        self.assertIn('SF-FACT-HERE', canon)   # fact 片段仍是 Canon fact
        self.assertNotIn('FD-PLAN', canon)
        # belief / in_world_text / plan 三层不受新层影响。
        self.assertIn('SF-BELIEF', {row['id'] for row in roles['CURRENT BELIEFS']['items']})
        self.assertIn('SF-TEXT', {row['id'] for row in roles['IN-WORLD TEXT']['items']})
        self.assertIn('SF-PLAN', {row['id'] for row in roles['APPROVED PLAN']['items']})
        health = assess_context(build(root))
        self.assertTrue(health['ready'], health['issues'])

    def test_semantic_contract_layer_reports_provenance_without_prose(self):
        context = build(self.root)
        contract = layers_by_role(context)['CANON SEMANTIC CONTRACT']
        self.assertTrue(contract['items'])
        for row in contract['items']:
            self.assertEqual(row['kind'], 'field')
            self.assertEqual(len(row['contentHash']), 64)
            self.assertNotIn('label', row)   # 只报出处，不注入定义正文
            self.assertNotIn('key', row)

    def test_fact_belief_and_in_world_text_have_distinct_roles(self):
        roles = layers_by_role(build(self.root))
        self.assertIn('SF-PLAN', [row['id'] for row in roles['APPROVED PLAN']['items']])
        self.assertIn('SF-BELIEF', [row['id'] for row in roles['CURRENT BELIEFS']['items']])
        self.assertIn('SF-TEXT', [row['id'] for row in roles['IN-WORLD TEXT']['items']])
        self.assertTrue(roles['CANON FACTS']['authoritative'])
        self.assertFalse(roles['CURRENT BELIEFS']['authoritative'])
        self.assertFalse(roles['IN-WORLD TEXT']['authoritative'])
        # 一个角色相信 X 不等于 Canon 说 X；两者绝不互串。
        canon_ids = {row['id'] for row in roles['CANON FACTS']['items']}
        self.assertFalse({'SF-BELIEF', 'SF-TEXT', 'SF-PLAN'} & canon_ids)
        for row in roles['CANON FACTS']['items']:
            self.assertNotIn(row.get('contentRole'), {'belief', 'in_world_text', 'plan'})

    def test_belief_disagreement_with_canon_is_not_false_conflict(self):
        context = build(self.root)
        health = assess_context(context)
        self.assertTrue(health['ready'], health['issues'])
        self.assertNotIn('CONTEXT_VALUE_CONFLICT', {row['code'] for row in health['issues']})

    def test_approved_plan_layer_carries_source_plan_hash_and_active_decisions(self):
        decision_id = self.approve_decision(statement='秘密不得过早公开', rationale='延迟揭示',
                                            scopeIds=['CH-AAA'], dependsOn=[])
        context = build(self.root)
        plan_layer = layers_by_role(context)['APPROVED PLAN']
        self.assertEqual(plan_layer['sourcePlanHash'], context['sourceContext']['sourcePlanHash'])
        self.assertEqual(plan_layer['activeDecisionIds'], [decision_id])

    def test_unapproved_candidate_is_not_approved_plan(self):
        self.source['planningCandidates'] = [{
            'candidateId': 'PC-DRAFT-ONE', 'title': '待定未来章', 'summary': '',
            'placement': None, 'primaryArcId': None, 'secondaryArcIds': [],
            'beforeChapterId': None, 'status': 'draft'}]
        self.save()
        planner = layers_by_role(build(self.root, task_type='planner'))
        self.assertIn('PC-DRAFT-ONE', [row['id'] for row in planner['UNAPPROVED CANDIDATES']['items']])
        self.assertNotIn('PC-DRAFT-ONE', [row['id'] for row in planner['APPROVED PLAN']['items']])
        writer = layers_by_role(build(self.root, task_type='writer'))
        self.assertEqual(writer['UNAPPROVED CANDIDATES']['items'], [])
        self.assertTrue(writer['UNAPPROVED CANDIDATES']['withheldForStation'])

    def test_knowledge_pack_keeps_reference_knowledge_role(self):
        context = build(self.root, chapter_goal='关系推进')
        self.assertEqual(context['knowledgePack']['role'], 'REFERENCE KNOWLEDGE')
        layer = layers_by_role(context)['REFERENCE KNOWLEDGE']
        self.assertFalse(layer['authoritative'])
        self.assertIn('KN-RELATIONSHIP-TRANSITION', [row['assetId'] for row in layer['items']])
        canon_ids = {row['id'] for row in layers_by_role(context)['CANON FACTS']['items']}
        canon_assets = {row.get('assetId') for row in layers_by_role(context)['CANON FACTS']['items']}
        self.assertNotIn('KN-RELATIONSHIP-TRANSITION', canon_ids | canon_assets)

    # --- 预算 ---

    def test_budget_evicts_optional_knowledge_before_mandatory_context(self):
        full = build(self.root, chapter_goal='关系推进', view='full')
        payload = deepcopy(full)
        for item in payload['knowledgePack']['items']:
            item['excerpt'] = item['excerpt'] + '知' * 2000
        probe = deepcopy(payload)
        probe['knowledgePack']['items'] = []
        probe['dependencyManifest']['knowledge'] = []
        for layer in builder.LAYERS:
            probe['memoryPack'][layer] = [row for row in probe['memoryPack'][layer] if row['mandatory']]
        probe['contextBudget'] = {'total': 10 ** 9, 'unit': 'serializedCharacters',
                                  'scope': 'station', 'usedCharacters': 0}
        mandatory_size = builder._measure(probe, 'contextBudget')
        payload['contextBudget'] = {'total': mandatory_size + 200, 'unit': 'serializedCharacters',
                                    'scope': 'station', 'usedCharacters': 0}
        payload['memoryPack']['budget']['total'] = mandatory_size + 200
        station = builder._station_view(payload)
        self.assertEqual(station['knowledgePack']['items'], [])
        self.assertEqual(station['dependencyManifest']['knowledge'], [])
        self.assertEqual(station['knowledgePack']['omittedBudget'], len(full['knowledgePack']['items']))
        # 认识层必须报告工位真正收到的内容，不能继续声称已装载被驱逐的知识。
        self.assertEqual(layers_by_role(station)['REFERENCE KNOWLEDGE']['items'], [])
        self.assertEqual(layers_by_role(station)['REFERENCE KNOWLEDGE']['itemCount'], 0)
        mandatory = [row for layer in builder.LAYERS for row in station['memoryPack'][layer] if row['mandatory']]
        self.assertTrue(mandatory, '被预算驱逐的只能是 optional 材料')
        self.assertLessEqual(station['contextBudget']['usedCharacters'], station['contextBudget']['total'])

    def test_mandatory_context_over_budget_fails_closed(self):
        full = build(self.root, chapter_goal='关系推进', view='full')
        payload = deepcopy(full)
        payload['memoryPack']['budget']['total'] = 1
        with self.assertRaises(builder.ContextBuilderError) as caught:
            builder._station_view(payload)
        self.assertEqual(caught.exception.code, 'CONTEXT_BUDGET_IMPOSSIBLE')

    # --- 只读 ---

    def test_builder_does_not_write_project_files(self):
        before = inventory(self.root)
        build(self.root, chapter_goal='关系推进', view='full')
        build(self.root, task_type='writer', pov_character='C-ALPHA')
        self.assertEqual(before, inventory(self.root))


class StationContextPolicyTests(unittest.TestCase):
    """工位覆盖策略集中在一处，未知工位默认不拿上下文。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / '正文').mkdir()

    def test_declared_stations_get_a_full_context(self):
        self.assertEqual(builder.station_context_scope('planner'), 'full')
        self.assertEqual(builder.station_context_scope('writer'), 'full')
        self.assertEqual(builder.station_context_scope('editor'), 'full')
        self.assertEqual(builder.station_context_scope('query'), 'full')

    def test_stations_without_narrative_context_are_explicit(self):
        for task in ('repair', 'titler', 'continuity', 'composer'):
            with self.subTest(task=task):
                self.assertEqual(builder.station_context_scope(task), 'none')

    def test_unknown_station_defaults_to_no_context(self):
        self.assertEqual(builder.station_context_scope('not-a-station'), 'none')
        self.assertEqual(builder.station_context_scope(''), 'none')

    def test_task_prepare_uses_the_declared_policy(self):
        from scripts.core.task_prepare import _context
        not_applicable = _context(self.root, 'titler', None, None, '')
        self.assertFalse(not_applicable['applicable'])
        self.assertIsNone(not_applicable['ready'])
        self.assertEqual(not_applicable['reason'], '本任务不消费续写上下文')


if __name__ == '__main__':
    unittest.main()
