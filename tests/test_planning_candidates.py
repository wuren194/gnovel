from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import read_json
from scripts.core.operations import call_operation
from scripts.core.planning_candidates import preview_candidate_change, query_planning_candidates, validate_candidates
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.source_model import content_defaults
from scripts.core.project import source_model_policy
from scripts.core.structure_store import save_structure
from tests.fixtures_current import create_current_project


class PlanningCandidatesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'book'
        create_current_project(self.root, chapter_count=1)

    def commit(self, preview):
        return commit_plan(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)

    def placement(self):
        source = load_structure(self.root)
        return {'title': '未来章节', 'summary': '待讨论的转折',
                'placement': {'scope': 'volume', 'volumeId': source['volumes'][0]['volumeId']},
                'primaryArcId': source['arcs'][0]['arcId'], 'secondaryArcIds': [], 'beforeChapterId': None}

    def test_candidate_lifecycle_does_not_allocate_chapter_until_materialized(self):
        source = load_structure(self.root)
        before_chapters = [row['chapterId'] for row in source['chapters']]
        preview = preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                           reason='计划一个未来转折', record=self.placement())
        self.assertEqual(before_chapters, [row['chapterId'] for row in load_structure(self.root)['chapters']])
        self.commit(preview)
        candidate = query_planning_candidates(self.root)['items'][0]
        self.assertEqual(candidate['status'], 'draft')
        self.assertNotIn('materializedChapterId', candidate)
        self.assertEqual(before_chapters, [row['chapterId'] for row in load_structure(self.root)['chapters']])
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='materialize', expectedRevision=load_structure(self.root)['revision'],
                                     reason='越权', candidateId=candidate['candidateId'])
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_NOT_APPROVED')
        preview = preview_candidate_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                           reason='作者确认', candidateId=candidate['candidateId'],
                                           approvalActor='author', approvalEvidence='作者本地确认记录')
        self.commit(preview)
        preview = preview_candidate_change(self.root, action='materialize', expectedRevision=load_structure(self.root)['revision'],
                                           reason='准备独立章纲', candidateId=candidate['candidateId'])
        chapter_id = preview['parameters']['chapterId']
        self.assertEqual(before_chapters, [row['chapterId'] for row in load_structure(self.root)['chapters']])
        self.commit(preview)
        source = load_structure(self.root)
        self.assertEqual(source['planningCandidates'][0]['materializedChapterId'], chapter_id)
        self.assertEqual(source['planningCandidates'][0]['status'], 'materialized')
        self.assertEqual(source['chapters'][-1]['status'], 'planning_required')
        self.assertEqual(len(source['chapters']), 2)
        self.assertFalse(source['planningCandidates'][0]['approval']['verifiedHuman'])
        retry = preview_candidate_change(self.root, action='materialize', expectedRevision=source['revision'] - 1,
                                         reason='重复提交重试', candidateId=candidate['candidateId'])
        self.assertTrue(retry['idempotent'])
        self.assertEqual(retry['chapterId'], chapter_id)
        self.assertEqual(len(load_structure(self.root)['chapters']), 2)

    def test_revision_and_anchor_fail_closed(self):
        source = load_structure(self.root)
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='create', expectedRevision=source['revision'] - 1,
                                     reason='旧输入', record=self.placement())
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        wrong = {**self.placement(), 'beforeChapterId': 'chapter-NOT-FOUND'}
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                     reason='错误锚点', record=wrong)
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_INVALID')
        self.assertNotIn('planningCandidates', read_json(self.root / '.govel/structure.json'))

    def test_cli_and_mcp_share_operation(self):
        source = load_structure(self.root)
        result = call_operation('source.planning-candidates', self.root, {})
        self.assertEqual(result['code'], 'PLANNING_CANDIDATES')
        preview = call_operation('source.candidate-preview', self.root,
                                 {'action': 'create', 'expectedRevision': source['revision'],
                                  'reason': '测试入口', 'record': self.placement()})
        self.assertEqual(preview['operation'], 'planning_candidate_create')

    def test_unplaced_future_node_does_not_invent_volume(self):
        source = load_structure(self.root)
        plan = preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                        reason='序章归属未定', record={'title': '序章候选', 'summary': '先保留位置未知'})
        self.commit(plan)
        candidate = query_planning_candidates(self.root)['items'][0]
        self.assertIsNone(candidate['placement'])
        self.assertFalse(candidate['placementReady'])
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                     reason='错误批准', candidateId=candidate['candidateId'],
                                     approvalActor='author', approvalEvidence='确认')
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_UNPLACED')
        plan = preview_candidate_change(self.root, action='revise', expectedRevision=load_structure(self.root)['revision'],
                                        reason='明确归属', candidateId=candidate['candidateId'], record=self.placement())
        self.commit(plan)
        self.assertTrue(query_planning_candidates(self.root)['items'][0]['placementReady'])

    def test_split_then_merge_preserves_lineage_without_allocating_chapters(self):
        source = load_structure(self.root)
        self.commit(preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                             reason='初步候选', record=self.placement()))
        original = query_planning_candidates(self.root)['items'][0]['candidateId']
        self.commit(preview_candidate_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                             reason='作者批准原候选', candidateId=original,
                                             approvalActor='author', approvalEvidence='已确认原候选'))
        part_a = {**self.placement(), 'title': '前半段'}
        part_b = {**self.placement(), 'title': '后半段'}
        preview = preview_candidate_change(self.root, action='split', expectedRevision=load_structure(self.root)['revision'],
                                           reason='拆分节奏', candidateId=original, record={'parts': [part_a, part_b]})
        created = preview['parameters']['newCandidateIds']
        self.assertEqual(len(created), 2)
        self.commit(preview)
        rows = {row['candidateId']: row for row in query_planning_candidates(self.root)['items']}
        self.assertEqual(rows[original]['status'], 'withdrawn')
        self.assertNotIn('approval', rows[original])
        self.assertEqual(rows[original]['replacedByCandidateIds'], created)
        self.assertTrue(all(rows[cid]['sourceCandidateIds'] == [original] for cid in created))
        self.assertTrue(all(rows[cid]['status'] == 'draft' for cid in created))
        self.assertEqual(len(load_structure(self.root)['chapters']), 1)
        preview = preview_candidate_change(self.root, action='merge', expectedRevision=load_structure(self.root)['revision'],
                                           reason='合并为一个转折', record={'sourceCandidateIds': created,
                                                                      'placement': self.placement()})
        merged = preview['parameters']['candidateId']
        self.commit(preview)
        rows = {row['candidateId']: row for row in query_planning_candidates(self.root)['items']}
        self.assertEqual(rows[merged]['sourceCandidateIds'], created)
        self.assertTrue(all(rows[cid]['replacedByCandidateIds'] == [merged] for cid in created))
        self.assertEqual(len(load_structure(self.root)['chapters']), 1)

    def test_split_rejects_invalid_size_without_retiring_source(self):
        source = load_structure(self.root)
        self.commit(preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                             reason='候选', record=self.placement()))
        cid = query_planning_candidates(self.root)['items'][0]['candidateId']
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='split', expectedRevision=load_structure(self.root)['revision'],
                                     reason='拆分', candidateId=cid, record={'parts': [self.placement()]})
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_SPLIT_INVALID')
        self.assertEqual(query_planning_candidates(self.root)['items'][0]['status'], 'draft')

    def test_lineage_cycle_is_rejected_at_structure_boundary(self):
        source = load_structure(self.root)
        first = {**self.placement(), 'candidateId': 'candidate-CYCLE-A', 'status': 'withdrawn',
                 'sourceCandidateIds': ['candidate-CYCLE-B'], 'replacedByCandidateIds': ['candidate-CYCLE-B']}
        second = {**self.placement(), 'candidateId': 'candidate-CYCLE-B', 'status': 'withdrawn',
                  'sourceCandidateIds': ['candidate-CYCLE-A'], 'replacedByCandidateIds': ['candidate-CYCLE-A']}
        source['planningCandidates'] = [first, second]
        self.assertIn('PLANNING_CANDIDATE_LINEAGE_CYCLE',
                      {row['code'] for row in validate_candidates(source)})


class SchemaThreePlanningTests(PlanningCandidatesTests):
    """Placement-aware candidate lifecycle: project candidates, unit membership."""

    def make_global_arc(self) -> str:
        source = load_structure(self.root)
        source['arcs'][0]['volumeIds'] = []  # global arc is bindable by a project-scope chapter
        save_structure(self.root, source)
        return source['arcs'][0]['arcId']

    def project_placement(self):
        return {'title': '序卷章', 'summary': '不属于任何一卷',
                'placement': {'scope': 'project'}, 'primaryArcId': self.make_global_arc(),
                'secondaryArcIds': [], 'beforeChapterId': None}

    def create_and_approve(self, record):
        source = load_structure(self.root)
        self.commit(preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                             reason='建立候选', record=record))
        cid = query_planning_candidates(self.root)['items'][-1]['candidateId']
        self.commit(preview_candidate_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                             reason='作者批准', candidateId=cid,
                                             approvalActor='author', approvalEvidence='确认'))
        return cid

    def test_project_candidate_approval_and_materialization(self):
        cid = self.create_and_approve(self.project_placement())
        preview = preview_candidate_change(self.root, action='materialize',
                                           expectedRevision=load_structure(self.root)['revision'],
                                           reason='物化项目章', candidateId=cid)
        chapter_id = preview['parameters']['chapterId']
        self.commit(preview)
        source = load_structure(self.root)
        chapter = next(c for c in source['chapters'] if c['chapterId'] == chapter_id)
        self.assertEqual(chapter['placement'], {'scope': 'project'})
        self.assertNotIn('volumeId', chapter)

    def test_volume_candidate_materialization_copies_placement(self):
        cid = self.create_and_approve(self.placement())
        volume = load_structure(self.root)['volumes'][0]['volumeId']
        preview = preview_candidate_change(self.root, action='materialize',
                                           expectedRevision=load_structure(self.root)['revision'],
                                           reason='物化', candidateId=cid)
        chapter_id = preview['parameters']['chapterId']
        self.commit(preview)
        chapter = next(c for c in load_structure(self.root)['chapters'] if c['chapterId'] == chapter_id)
        self.assertEqual(chapter['placement'], {'scope': 'volume', 'volumeId': volume})

    def test_project_candidate_volume_only_arc_is_rejected(self):
        source = load_structure(self.root)
        # The default arc is volume-scoped; a project candidate cannot bind it.
        record = {'title': '序卷章', 'summary': 's', 'placement': {'scope': 'project'},
                  'primaryArcId': source['arcs'][0]['arcId'], 'secondaryArcIds': [], 'beforeChapterId': None}
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                     reason='越权', record=record)
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_INVALID')

    def _unit_with_candidate(self, cid, *, scope='project'):
        source = load_structure(self.root)
        placement = ({'scope': 'volume', 'volumeId': source['volumes'][0]['volumeId']}
                     if scope == 'volume' else {'scope': 'project'})
        source['units'] = [{'unitId': 'unit-HOLDER', 'title': '第八日', 'ordinal': 1, 'status': 'active',
                            'placement': placement,
                            'members': [{'memberType': 'planning_candidate', 'memberId': cid, 'order': 3}]}]
        save_structure(self.root, source)

    def test_unit_pc_to_ch_atomic_conversion_preserves_order(self):
        cid = self.create_and_approve(self.placement())
        self._unit_with_candidate(cid, scope='volume')
        preview = preview_candidate_change(self.root, action='materialize',
                                           expectedRevision=load_structure(self.root)['revision'],
                                           reason='物化并转换单元成员', candidateId=cid)
        chapter_id = preview['parameters']['chapterId']
        self.commit(preview)
        unit = load_structure(self.root)['units'][0]
        self.assertEqual(unit['members'], [{'memberType': 'chapter', 'memberId': chapter_id, 'order': 3}])

    def test_split_of_unit_member_candidate_fails_closed(self):
        cid = self.create_and_approve(self.placement())
        self._unit_with_candidate(cid, scope='volume')
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='split', expectedRevision=load_structure(self.root)['revision'],
                                     reason='拆分单元成员候选', candidateId=cid,
                                     record={'parts': [self.placement(), self.placement()]})
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_UNIT_MEMBERSHIP_CONFLICT')

    def test_withdraw_of_unit_member_candidate_fails_closed(self):
        source = load_structure(self.root)
        self.commit(preview_candidate_change(self.root, action='create', expectedRevision=source['revision'],
                                             reason='候选', record=self.placement()))
        cid = query_planning_candidates(self.root)['items'][-1]['candidateId']
        self._unit_with_candidate(cid, scope='volume')
        with self.assertRaises(StructureError) as caught:
            preview_candidate_change(self.root, action='withdraw', expectedRevision=load_structure(self.root)['revision'],
                                     reason='撤回单元成员候选', candidateId=cid)
        self.assertEqual(caught.exception.code, 'PLANNING_CANDIDATE_UNIT_MEMBERSHIP_CONFLICT')
