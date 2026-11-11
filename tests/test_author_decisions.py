from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.author_decisions import preview_decision_change, query_author_decisions
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.canon_proposals import resolve_canon_target, preview_canon_change
from tests.fixtures_source import source_fixture


class AuthorDecisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source_fixture(self.root)

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def change_canon_summary(self, value):
        ref = resolve_canon_target(self.root, selector='character-ALPHA')['sessionRef']
        self.commit(preview_canon_change(self.root, sessionRef=ref, fieldPath='summary',
                                         expectedOldExists=False, expectedOldValue=None, newValue=value,
                                         reason='作者确认事实摘要',
                                         evidence=[{'fragmentId': 'fact-LIFE', 'quote': '他独自守过城门。'}],
                                         approvalActor='author', approvalEvidence='本地确认'))

    def test_authorial_decision_stays_out_of_canon_and_detects_stale_scope(self):
        source = load_structure(self.root)
        plan = preview_decision_change(self.root, action='create', expectedRevision=source['revision'],
                                       reason='规划控制', statement='秘密不得过早公开', rationale='延迟揭示',
                                       scopeIds=['character-ALPHA'], dependsOn=[])
        did = plan['parameters']['decisionId']
        self.assertNotIn('authorDecisions', source)
        self.commit(plan)
        with self.assertRaises(StructureError) as caught:
            preview_decision_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                    reason='作者确认', decisionId=did)
        self.assertEqual(caught.exception.code, 'AUTHOR_DECISION_APPROVAL_INVALID')
        plan = preview_decision_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                       reason='作者确认', decisionId=did, approvalActor='author',
                                       approvalEvidence='本地确认')
        self.commit(plan)
        decision = query_author_decisions(self.root, decisionId=did)['items'][0]
        self.assertEqual(decision['epistemicRole'], 'authorial_planning_decision')
        self.assertFalse(decision['stale'])
        self.change_canon_summary('新摘要')
        self.assertTrue(query_author_decisions(self.root, decisionId=did)['items'][0]['stale'])

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(StructureError) as caught:
            preview_decision_change(self.root, action='create', expectedRevision=0,
                                    reason='错引用', statement='测试', rationale='',
                                    scopeIds=['character-NO-SUCH'], dependsOn=[])
        self.assertEqual(caught.exception.code, 'AUTHOR_DECISION_SCOPE_UNKNOWN')

    def test_decision_options_and_transitive_staleness(self):
        source = load_structure(self.root)
        first = preview_decision_change(self.root, action='create', expectedRevision=source['revision'],
                                        reason='选择', statement='先保留线索', rationale='因果铺垫',
                                        scopeIds=['character-ALPHA'], dependsOn=[],
                                        alternatives=[{'label': '保留线索', 'outcome': 'chosen', 'reason': '有前文证据'},
                                                      {'label': '提前揭示', 'outcome': 'rejected', 'reason': '破坏悬念'}],
                                        constraints=['不能伪造人物认知'], evidenceRefs=['作者规划记录'])
        first_id = first['parameters']['decisionId']; self.commit(first)
        approve = preview_decision_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                          reason='作者确认', decisionId=first_id, approvalActor='author',
                                          approvalEvidence='具体提案确认')
        self.commit(approve)
        second = preview_decision_change(self.root, action='create', expectedRevision=load_structure(self.root)['revision'],
                                         reason='依赖', statement='下一章延迟公布', rationale='承接决定',
                                         scopeIds=[], dependsOn=[first_id])
        second_id = second['parameters']['decisionId']; self.commit(second)
        approve = preview_decision_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                          reason='作者确认', decisionId=second_id, approvalActor='author',
                                          approvalEvidence='具体提案确认')
        self.commit(approve)
        decision = query_author_decisions(self.root, decisionId=first_id)['items'][0]
        self.assertEqual(decision['alternatives'][1]['outcome'], 'rejected')
        self.change_canon_summary('变更后的摘要')
        dependent = query_author_decisions(self.root, decisionId=second_id)['items'][0]
        self.assertTrue(dependent['stale'])
        self.assertEqual(dependent['staleDecisionIds'], [first_id])


class AuthorDecisionContextTests(unittest.TestCase):
    """作者决定必须真正进出统一 Context Builder，而不只是查询 API 上的标记。"""

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

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def create(self, **kwargs):
        source = load_structure(self.root)
        plan = preview_decision_change(self.root, action='create', expectedRevision=source['revision'],
                                       reason='规划控制', **kwargs)
        self.commit(plan)
        return plan['parameters']['decisionId']

    def approve(self, decision_id):
        plan = preview_decision_change(self.root, action='approve',
                                       expectedRevision=load_structure(self.root)['revision'],
                                       reason='作者确认', decisionId=decision_id,
                                       approvalActor='author', approvalEvidence='本地确认')
        self.commit(plan)

    def layers(self, context):
        return {row['role']: row for row in context['layers']}

    def loaded(self, context=None):
        """工位真正拿到的决定 ID（按稳定顺序）。"""
        return sorted(row['decisionId'] for row in (context or self.build())['authorDecisions'])

    def unit_with(self, *member_ids):
        """给本章挂一个单元，返回其 unitId（Decision 可以 scope 到单元）。"""
        source = load_structure(self.root)
        volume_id = source['volumes'][0]['volumeId']
        from scripts.core.source_placement import placement_from_volume
        unit_id = 'unit-%04d' % (len(source.get('units', [])) + 1)
        source['units'] = source.get('units', []) + [{
            'unitId': unit_id, 'placement': placement_from_volume(volume_id), 'title': '测试单元',
            'ordinal': 1, 'status': 'active',
            'members': [{'memberType': 'chapter', 'memberId': mid, 'order': index + 1}
                        for index, mid in enumerate(member_ids)]}]
        self.save(source)
        return unit_id

    def active_decision_on(self, scope_id, statement='规划控制'):
        decision_id = self.create(statement=statement, rationale='作者规划',
                                  scopeIds=[scope_id], dependsOn=[])
        self.approve(decision_id)
        return decision_id

    def manifest_ids(self, context):
        return sorted(row['decisionId'] for row in context['dependencyManifest']['decisions'])

    def test_active_approved_decision_enters_approved_plan_layer(self):
        decision_id = self.create(statement='秘密不得过早公开', rationale='延迟揭示',
                                  scopeIds=['chapter-AAA'], dependsOn=[])
        draft = self.build()
        self.assertEqual(draft['authorDecisions'], [])
        self.assertNotIn(decision_id, self.layers(draft)['APPROVED PLAN']['activeDecisionIds'])
        self.approve(decision_id)
        active = self.build()
        self.assertEqual([row['decisionId'] for row in active['authorDecisions']], [decision_id])
        self.assertEqual(active['authorDecisions'][0]['role'], 'AUTHOR DECISIONS')
        self.assertIn(decision_id, [row['id'] for row in self.layers(active)['AUTHOR DECISIONS']['items']])
        self.assertEqual(self.layers(active)['APPROVED PLAN']['activeDecisionIds'], [decision_id])
        self.assertEqual([row['decisionId'] for row in active['dependencyManifest']['decisions']], [decision_id])

    def test_draft_decision_is_not_station_guidance(self):
        decision_id = self.create(statement='草案控制', rationale='尚未确认',
                                  scopeIds=['chapter-AAA'], dependsOn=[])
        context = self.build()
        self.assertEqual(context['authorDecisions'], [])
        self.assertEqual(self.layers(context)['AUTHOR DECISIONS']['items'], [])
        self.assertEqual(self.manifest_ids(context), [])

    def test_stale_decision_is_excluded_from_active_guidance(self):
        decision_id = self.create(statement='依赖人物甲', rationale='规划控制',
                                  scopeIds=['chapter-AAA', 'character-ALPHA'], dependsOn=[])
        self.approve(decision_id)
        self.assertIn(decision_id, [row['decisionId'] for row in self.build()['authorDecisions']])
        changed = load_structure(self.root)
        changed['entities'][0]['name'] = '改名后的人物甲'
        self.save(changed)
        stale = self.build()
        self.assertEqual(stale['authorDecisions'], [])
        self.assertIn(decision_id, stale['staleDecisionIds'])
        self.assertEqual(self.layers(stale)['AUTHOR DECISIONS']['items'], [])
        self.assertEqual(self.manifest_ids(stale), [])

    def test_transitively_stale_dependent_decision_is_excluded(self):
        first = self.create(statement='先保留线索', rationale='因果铺垫',
                            scopeIds=['chapter-AAA', 'character-ALPHA'], dependsOn=[])
        self.approve(first)
        second = self.create(statement='下一章延迟公布', rationale='承接决定',
                             scopeIds=[], dependsOn=[first])
        self.approve(second)
        self.assertEqual(sorted(row['decisionId'] for row in self.build()['authorDecisions']), sorted([first, second]))
        changed = load_structure(self.root)
        changed['entities'][0]['name'] = '改名后的人物甲'
        self.save(changed)
        stale = self.build()
        self.assertEqual(stale['authorDecisions'], [])
        self.assertEqual(sorted(stale['staleDecisionIds']), sorted([first, second]))
        self.assertEqual(self.layers(stale)['APPROVED PLAN']['activeDecisionIds'], [])

    def test_decision_outside_chapter_scope_is_not_injected(self):
        decision_id = self.create(statement='只约束人物乙', rationale='与本章无关',
                                  scopeIds=['character-BETA'], dependsOn=[])
        self.approve(decision_id)
        context = self.build()
        self.assertEqual(context['authorDecisions'], [])
        self.assertEqual(context['staleDecisionIds'], [])
        self.assertEqual(self.manifest_ids(context), [])
        self.assertNotIn(decision_id, self.layers(context)['APPROVED PLAN']['activeDecisionIds'])

    def test_decision_change_changes_context_dependency_hash(self):
        before = self.build()
        decision_id = self.create(statement='新增规划控制', rationale='规划控制',
                                  scopeIds=['chapter-AAA'], dependsOn=[])
        self.approve(decision_id)
        after = self.build()
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        self.assertEqual([row['decisionId'] for row in after['dependencyManifest']['decisions']], [decision_id])
        self.assertEqual(self.layers(after)['AUTHOR DECISIONS']['items'], [{'id': decision_id}])

    # --- 适用性闭包：scopeIds 可以指向任何 SourceIndex record 或 Planning Candidate ---

    def test_decision_scoped_to_chapter_is_applicable(self):
        decision_id = self.active_decision_on('chapter-AAA')
        self.assertEqual(self.loaded(), [decision_id])

    def test_decision_scoped_to_arc_is_applicable(self):
        arc_id = self.source['arcs'][0]['arcId']
        decision_id = self.active_decision_on(arc_id)
        self.assertEqual(self.loaded(), [decision_id])

    def test_decision_scoped_to_volume_is_applicable(self):
        volume_id = self.source['volumes'][0]['volumeId']
        decision_id = self.active_decision_on(volume_id)
        self.assertEqual(self.loaded(), [decision_id])

    def test_decision_scoped_to_unit_is_applicable(self):
        unit_id = self.unit_with('chapter-AAA')
        decision_id = self.active_decision_on(unit_id)
        self.assertEqual(self.loaded(), [decision_id])

    def test_decision_scoped_to_project_is_applicable(self):
        decision_id = self.active_decision_on(self.source['projectId'])
        self.assertEqual(self.loaded(), [decision_id])

    def test_decision_scoped_to_plan_entity_is_applicable(self):
        """本章 Source Plan 闭包内的实体：决定 scope 到它必须被装进来。"""
        decision_id = self.active_decision_on('character-ALPHA')
        self.assertEqual(self.loaded(), [decision_id])

    def decision_loaded_for_scope_in_fresh_project(self, scope_id, statement):
        """在全新 fixture 项目上：给单个 scope 建并批准决定，返回工位拿到的 ID 列表。

        每个 scope 用独立项目，避免决定内容互相污染 stale 判定。
        """
        from scripts.core.context_builder import build_station_context
        from tests.test_context_builder import context_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / '正文').mkdir()
        context_source(root)
        decision_id = self.active_decision_on_root(root, scope_id, statement)
        context = build_station_context(str(root), 'chapter-AAA', task_type='planner', view='station', read_only=True)
        return [row['decisionId'] for row in context['authorDecisions']], decision_id

    def active_decision_on_root(self, root, scope_id, statement):
        plan = preview_decision_change(root, action='create',
                                       expectedRevision=load_structure(root)['revision'],
                                       reason='规划控制', statement=statement, rationale='作者规划',
                                       scopeIds=[scope_id], dependsOn=[])
        decision_id = plan['parameters']['decisionId']
        commit_plan(root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        approve = preview_decision_change(root, action='approve',
                                          expectedRevision=load_structure(root)['revision'],
                                          reason='作者确认', decisionId=decision_id,
                                          approvalActor='author', approvalEvidence='本地确认')
        commit_plan(root, operation_id=approve['operationId'], plan_hash=approve['planHash'], approved=True)
        return decision_id

    def test_decision_scoped_to_attribute_in_plan_closure_is_applicable(self):
        for scope_id in ('attribute-PLAN', 'value-HEIGHT'):
            with self.subTest(scope_id=scope_id):
                loaded, decision_id = self.decision_loaded_for_scope_in_fresh_project(scope_id, '约束属性')
                self.assertEqual(loaded, [decision_id])

    def test_decision_scoped_to_fact_fragment_is_applicable(self):
        """片段与字段定义都在本章计划闭包内，scope 到它们同样适用。"""
        for scope_id in ('plan-PLAN', 'field-PLAN'):
            with self.subTest(scope_id=scope_id):
                loaded, decision_id = self.decision_loaded_for_scope_in_fresh_project(scope_id, '约束片段')
                self.assertEqual(loaded, [decision_id])

    def test_global_decision_without_scope_is_applicable(self):
        decision_id = self.create(statement='全书通用规划纪律', rationale='作者规划', scopeIds=[], dependsOn=[])
        self.approve(decision_id)
        self.assertEqual(self.loaded(), [decision_id])

    def project_with_candidates(self, author):
        """新建项目，交给 ``author(source) -> candidates`` 补候选，返回根目录。"""
        from tests.test_context_builder import context_source, save_context_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / '正文').mkdir()
        source = context_source(root)
        source['planningCandidates'] = list(author(source))
        save_context_source(root, source)
        return root

    @staticmethod
    def add_second_scope(source):
        """另一卷 + 只属于该卷的弧线：本章闭包之外。"""
        source['volumes'].append({'volumeId': 'volume-OTHER', 'ordinal': 2, 'title': '第二卷', 'status': 'planned'})
        source['arcs'].append({'arcId': 'arc-OTHER', 'title': '支线', 'status': 'planned',
                               'volumeIds': ['volume-OTHER'], 'parentArcId': None, 'promise': '',
                               'resolutionTargetChapterId': None})

    def test_decision_scoped_to_relevant_planning_candidate_is_applicable(self):
        from scripts.core.context_builder import build_station_context
        from scripts.core.source_placement import placement_from_volume

        def author(source):
            return [{'candidateId': 'candidate-IN-SCOPE', 'title': '本弧待定章', 'summary': '',
                     'placement': placement_from_volume(source['volumes'][0]['volumeId']),
                     'primaryArcId': source['arcs'][0]['arcId'], 'secondaryArcIds': [],
                     'beforeChapterId': None, 'status': 'draft'}]

        root = self.project_with_candidates(author)
        decision_id = self.active_decision_on_root(root, 'candidate-IN-SCOPE', '约束待定章')
        context = build_station_context(str(root), 'chapter-AAA', task_type='planner', view='station', read_only=True)
        self.assertEqual([row['decisionId'] for row in context['authorDecisions']], [decision_id])
        self.assertIn('candidate-IN-SCOPE', [row['id'] for row in self.layers(context)['UNAPPROVED CANDIDATES']['items']])

    def test_decision_scoped_to_unrelated_planning_candidate_is_not_injected(self):
        from scripts.core.context_builder import build_station_context
        from scripts.core.source_placement import placement_from_volume

        def author(source):
            self.add_second_scope(source)
            return [{'candidateId': 'candidate-ELSEWHERE', 'title': '他处待定章', 'summary': '',
                     'placement': placement_from_volume('volume-OTHER'), 'primaryArcId': 'arc-OTHER',
                     'secondaryArcIds': [], 'beforeChapterId': 'chapter-BBB', 'status': 'draft'}]

        root = self.project_with_candidates(author)
        decision_id = self.active_decision_on_root(root, 'candidate-ELSEWHERE', '约束他处待定章')
        context = build_station_context(str(root), 'chapter-AAA', task_type='planner', view='station', read_only=True)
        self.assertEqual(context['authorDecisions'], [])
        self.assertEqual(context['staleDecisionIds'], [])
        self.assertEqual(self.manifest_ids(context), [])
        self.assertNotIn(decision_id, self.layers(context)['APPROVED PLAN']['activeDecisionIds'])
        self.assertNotIn('candidate-ELSEWHERE', [row['id'] for row in self.layers(context)['UNAPPROVED CANDIDATES']['items']])
        # 同一候选对它自己的章是相关的：排除来自本章闭包，不是候选被整体忽略。
        other = build_station_context(str(root), 'chapter-BBB', task_type='planner', view='station', read_only=True)
        self.assertIn('candidate-ELSEWHERE', [row['id'] for row in self.layers(other)['UNAPPROVED CANDIDATES']['items']])
        self.assertEqual([row['decisionId'] for row in other['authorDecisions']], [decision_id])

    def test_relevant_but_stale_decision_is_excluded_while_applicable(self):
        """适用性成立、但内容已过期：仍不注入，且报 stale。"""
        decision_id = self.active_decision_on('character-ALPHA')
        changed = load_structure(self.root)
        changed['entities'][0]['name'] = '改名后的人物甲'
        self.save(changed)
        context = self.build()
        self.assertEqual(context['authorDecisions'], [])
        self.assertIn(decision_id, context['staleDecisionIds'])
        self.assertEqual(self.layers(context)['AUTHOR DECISIONS']['items'], [])

    def test_decision_manifest_proves_loaded_version(self):
        from scripts.core.artifacts import sha256_json
        decision_id = self.active_decision_on('chapter-AAA')
        context = self.build()
        entries = context['dependencyManifest']['decisions']
        self.assertEqual([row['decisionId'] for row in entries], [decision_id])
        row = next(item for item in load_structure(self.root)['authorDecisions']
                   if item['decisionId'] == decision_id)
        self.assertEqual(entries[0]['sha256'], sha256_json(row))
        from scripts.core.context_health import assess_context
        self.assertTrue(assess_context(context)['ready'], assess_context(context)['issues'])
        stripped = deepcopy(context)
        stripped['dependencyManifest']['decisions'] = [{'decisionId': decision_id}]
        codes = {item['code'] for item in assess_context(stripped)['issues']}
        self.assertIn('CONTEXT_DECISION_PROVENANCE_INVALID', codes)
        missing = deepcopy(context)
        missing['dependencyManifest']['decisions'] = []
        codes = {item['code'] for item in assess_context(missing)['issues']}
        self.assertIn('CONTEXT_DECISION_PROVENANCE_INVALID', codes)

    def test_chapter_management_changes_keep_content_proof_and_plan(self):
        from scripts.core.author_decisions import _decision_stale
        from scripts.core.source_plan import chapter_plan
        decision_id = self.active_decision_on('chapter-AAA')
        source = load_structure(self.root)
        expected = chapter_plan(source, 'chapter-AAA')['sourcePlanHash']
        for status in ('planned', 'draft', 'review', 'stable'):
            changed = deepcopy(source)
            changed['revision'] += 1
            chapter = next(row for row in changed['chapters'] if row['chapterId'] == 'chapter-AAA')
            chapter.update(status=status, filePath='正文/新位置.md')
            chapter['proseBinding']['generation'] += 1
            by_id = {row['decisionId']: row for row in changed['authorDecisions']}
            self.assertFalse(_decision_stale(changed, by_id, decision_id), status)
            self.assertEqual(chapter_plan(changed, 'chapter-AAA')['sourcePlanHash'], expected, status)
            self.assertEqual(chapter_plan(changed, 'chapter-AAA')['authorInputs']['decisions'],
                             source['authorDecisions'])

    def test_new_decision_and_withdrawal_change_only_relevant_plans(self):
        from scripts.core.source_plan import chapter_plan
        from scripts.core.source_impact import source_impact
        before = load_structure(self.root)
        initial = {cid: chapter_plan(before, cid)['sourcePlanHash'] for cid in ('chapter-AAA', 'chapter-BBB')}
        did = self.create(statement='仅约束本章', rationale='作者规划', scopeIds=['chapter-AAA'], dependsOn=[])
        draft = load_structure(self.root)
        self.assertEqual(source_impact(before, draft)['approvalInvalidations'], [])
        self.approve(did)
        active = load_structure(self.root)
        self.assertNotEqual(chapter_plan(active, 'chapter-AAA')['sourcePlanHash'], initial['chapter-AAA'])
        self.assertEqual(chapter_plan(active, 'chapter-BBB')['sourcePlanHash'], initial['chapter-BBB'])
        self.assertEqual(source_impact(draft, active)['approvalInvalidations'], ['chapter-AAA'])
        plan = preview_decision_change(self.root, action='supersede', expectedRevision=active['revision'],
                                       reason='撤回当前决定', decisionId=did)
        self.commit(plan)
        withdrawn = load_structure(self.root)
        self.assertEqual(source_impact(active, withdrawn)['approvalInvalidations'], ['chapter-AAA'])

    def test_missing_or_unknown_scope_basis_is_unproven_without_resealing(self):
        from scripts.core.author_decisions import decision_validity
        from scripts.core.structure_store import canonical_json
        did = self.active_decision_on('chapter-AAA')
        source = load_structure(self.root)
        for basis in (None, 'unknown-basis'):
            changed = deepcopy(source)
            row = changed['authorDecisions'][0]
            if basis is None:
                row.pop('scopeSnapshotBasis')
            else:
                row['scopeSnapshotBasis'] = basis
            original = canonical_json(changed)
            result = decision_validity(changed, {did: row}, did)
            self.assertEqual(result['validity'], 'unproven')
            self.assertEqual(canonical_json(changed), original)
            self.save(changed)
            context = self.build()
            self.assertEqual(context['authorDecisions'], [])
            self.assertEqual(context['unavailableDecisions'][0]['validity'], 'unproven')
            from scripts.core.context_health import assess_context
            self.assertFalse(assess_context(context)['ready'])

    def test_repeated_plan_calculation_does_not_depend_on_itself(self):
        from scripts.core.source_plan import chapter_plan
        from scripts.core.author_decisions import validate_decisions
        first = self.active_decision_on('chapter-AAA')
        second = self.create(statement='由前项决定派生', rationale='保持因果', scopeIds=['chapter-AAA'], dependsOn=[first])
        self.approve(second)
        source = load_structure(self.root)
        plans = [chapter_plan(source, 'chapter-AAA') for _ in range(3)]
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(plans[1], plans[2])
        self.assertEqual({row['decisionId'] for row in plans[0]['authorInputs']['decisions']}, {first, second})
        changed = deepcopy(source)
        changed['authorDecisions'][0]['dependsOn'] = [second]
        self.assertIn({'code': 'AUTHOR_DECISION_DEPENDENCY_CYCLE'}, validate_decisions(changed))

    def test_unproven_applicable_decision_blocks_low_level_writing_binding(self):
        from scripts.core.source_plan import outline_binding
        did = self.active_decision_on('chapter-AAA')
        source = load_structure(self.root)
        next(row for row in source['authorDecisions'] if row['decisionId'] == did).pop('scopeSnapshotBasis')
        chapter = next(row for row in source['chapters'] if row['chapterId'] == 'chapter-AAA')
        with self.assertRaises(StructureError) as caught:
            outline_binding(self.root, source, chapter)
        self.assertEqual(caught.exception.code, 'AUTHOR_DECISIONS_REQUIRE_REVIEW')
        self.assertEqual(caught.exception.details['decisions'][0]['validity'], 'unproven')
        other = next(row for row in source['chapters'] if row['chapterId'] == 'chapter-BBB')
        source['attributes'].append({'attributeId': 'attribute-OTHER-PLAN', 'ownerId': 'chapter-BBB', 'fieldId': 'field-PLAN', 'values': []})
        source['fragments'].append({'fragmentId': 'plan-OTHER-PLAN', 'ownerId': 'chapter-BBB', 'text': '乙关好窗户。',
            'order': 1, 'contentRole': 'plan', 'contentBindings': ['attribute-OTHER-PLAN'], 'dependsOn': []})
        self.assertEqual(outline_binding(self.root, source, other)['kind'], 'source')


if __name__ == '__main__':
    unittest.main()
