"""Domain aggregates count existing states without manufacturing review credit."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.core.aggregate_queries import query_aggregates
from scripts.core.author_decisions import preview_decision_change, query_author_decisions
from scripts.core.jsonio import read_json, write_json
from scripts.core.query import _size
from scripts.core.source_impact import fragment_review_status, ReviewInputs, read_review
from scripts.core.source_review import query_reviews, submit_review
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from tests.fixtures_content_proposal import attribute_value_proposal
from tests.test_source_plan import planning_source


class AggregateQueryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source, self.chapter = planning_source(self.root)
        self.cid = self.chapter['chapterId']

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def test_character_burden_reuses_current_review_statuses(self):
        report = query_aggregates(self.root, kind='character-reviews', characterId='C-ALPHA')
        self.assertEqual(report['items'][0]['byStatus'], {'unreviewed': 4})
        selected = query_reviews(self.root, recordIds=['SF-HEIGHT'])['items'][0]
        quote = self.source['fragments'][0]['text']
        submit_review(self.root, requestId='review-height', reviews=[{
            'recordId': selected['recordId'], 'inputHash': selected['inputHash'],
            'contentHash': selected['contentHash'], 'decision': 'compatible', 'reason': '逐项核对',
            'evidence': [{'start': 0, 'end': len(quote), 'text': quote}]}])
        report = query_aggregates(self.root, kind='character-reviews', characterId='C-ALPHA')
        self.assertEqual(report['items'][0]['itemCount'], 3)
        source = load_structure(self.root)
        source['attributes'][0]['values'][0]['value']['number'] = 185
        write_json(self.root / '.gnovel/structure.json', source)
        report = query_aggregates(self.root, kind='character-reviews', view='items')
        actual = {row['recordId']: row['status'] for row in report['items']}
        inputs, review = ReviewInputs(source), read_review(self.root)
        self.assertEqual(actual['SF-HEIGHT'], fragment_review_status(inputs, review, 'SF-HEIGHT'))
        self.assertEqual(actual['SF-HEIGHT'], 'stale')
        self.assertFalse(report['coverage']['semanticReviewComplete'])
        self.assertNotIn('passed', report)

    def test_chapter_pending_scope_is_plan_closure_not_whole_catalog(self):
        report = query_aggregates(self.root, kind='chapter-issues', chapterId=self.cid, view='items')
        review_ids = {row['recordId'] for row in report['items'] if row['kind'] == 'source-review'}
        self.assertEqual(review_ids, {'SF-PLAN'})
        self.source['chapters'][0]['status'] = 'review'
        write_json(self.root / '.gnovel/structure.json', self.source)
        report = query_aggregates(self.root, kind='chapter-issues', chapterId=self.cid, view='items')
        from scripts.core.chapter_status import chapter_status
        expected = {row['code'] for row in chapter_status(self.root, self.cid)['blockers']}
        self.assertEqual({row['code'] for row in report['items'] if row['kind'] == 'workflow-blocker'}, expected)

    def test_perspective_scope_never_leaks_fact_or_other_recognition(self):
        for rid, perspective in [('SF-ALPHA-BELIEF', 'C-ALPHA'), ('SF-BETA-BELIEF', 'C-BETA')]:
            self.source['fragments'].append({'fragmentId': rid, 'ownerId': 'C-ALPHA', 'text': '独立认识内容。',
                'order': len(self.source['fragments']) + 1, 'contentBindings': ['AT-HEIGHT'],
                'contentRole': 'belief', 'perspectiveId': perspective})
        validate_structure(self.source); write_json(self.root / '.gnovel/structure.json', self.source)
        selection = {'knowledgeMode': 'perspective', 'perspectiveIds': ['C-ALPHA']}
        report = query_aggregates(self.root, kind='character-reviews', view='items', selection=selection)
        self.assertEqual([row['recordId'] for row in report['items']], ['SF-ALPHA-BELIEF'])
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('SF-BETA-BELIEF', encoded)
        self.assertNotIn('SF-HEIGHT', encoded)
        chapter = query_aggregates(self.root, kind='chapter-issues', chapterId=self.cid, selection=selection)
        self.assertTrue(chapter['coverage']['authorDomainsOmitted'])
        for kind in ('setting-impact', 'decision-plans'):
            with self.assertRaises(StructureError) as caught:
                query_aggregates(self.root, kind=kind, selection=selection)
            self.assertEqual(caught.exception.code, 'AGGREGATE_AUTHOR_SCOPE_REQUIRED')

    def test_setting_impact_keeps_recorded_scope_and_is_historical(self):
        plan = attribute_value_proposal(self.root, 'AT-HEIGHT', 185)
        self.commit(plan)
        report = query_aggregates(self.root, kind='setting-impact', operationId=plan['operationId'])
        counts = {key: len(value) for key, value in plan['impact'].items() if isinstance(value, list)}
        self.assertEqual(report['items'][0]['counts'], counts)
        self.assertTrue(report['items'][0]['historicalSnapshot'])
        items = query_aggregates(self.root, kind='setting-impact', operationId=plan['operationId'], view='items')
        self.assertTrue(all(row['detailsOperation'] == 'source.change-report' for row in items['items']))
        self.assertFalse(items['coverage']['approvalGranted'])
        self.assertNotIn('proposedStructure', json.dumps(items))

    def test_setting_impact_cursor_binds_undo_receipt_membership(self):
        plan = attribute_value_proposal(self.root, 'AT-HEIGHT', 185)
        self.commit(plan)
        arguments = {'kind': 'setting-impact', 'view': 'items', 'operationId': plan['operationId'], 'limit': 1}
        first = query_aggregates(self.root, **arguments)
        self.assertTrue(first['hasMore'])
        receipt = self.root / f'.gnovel/undo-receipts/{plan["operationId"]}-2.json'
        write_json(receipt, {'operationId': plan['operationId']})
        with self.assertRaises(StructureError) as caught:
            query_aggregates(self.root, **arguments, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')
        current = query_aggregates(self.root, **arguments)
        self.assertTrue(current['items'][0]['sourceOperationUndone'])
        receipt.unlink()
        with self.assertRaises(StructureError) as caught:
            query_aggregates(self.root, **arguments, cursor=current['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def create_decision(self, scopes, dependencies=()):
        plan = preview_decision_change(self.root, action='create', expectedRevision=load_structure(self.root)['revision'],
            reason='规划', statement='由作者决定保留因果', rationale='关系变化', scopeIds=scopes, dependsOn=list(dependencies))
        self.commit(plan)
        return plan['parameters']['decisionId']

    def test_decision_plans_reuse_global_and_registered_dependency_scopes(self):
        first = self.create_decision(['AT-HEIGHT'])
        second = self.create_decision([], [first])
        for did in (first, second):
            plan = preview_decision_change(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                reason='明确确认', decisionId=did, approvalActor='author', approvalEvidence='测试确认')
            self.commit(plan)
        for did in (first, second):
            report = query_aggregates(self.root, kind='decision-plans', decisionId=did)
            row = report['items'][0]
            self.assertEqual(row['chapterCount'], 1)
            self.assertEqual(row['stale'], query_author_decisions(self.root, decisionId=did)['items'][0]['stale'])
        details = query_aggregates(self.root, kind='decision-plans', decisionId=second, view='items')
        self.assertTrue(details['items'][0]['globalScope'])
        source = load_structure(self.root)
        source['attributes'][0]['values'][0]['value']['number'] = 175
        write_json(self.root / '.gnovel/structure.json', source)
        self.assertTrue(query_aggregates(self.root, kind='decision-plans', decisionId=first)['items'][0]['stale'])

    def test_bounded_pages_and_cursor_refuse_input_drift(self):
        first = query_aggregates(self.root, kind='character-reviews', view='items', limit=1, maxBytes=1800)
        self.assertLessEqual(_size(first), 1800)
        self.assertTrue(first['nextCursor'])
        rows, cursor = first['items'], first['nextCursor']
        while cursor:
            page = query_aggregates(self.root, kind='character-reviews', view='items', limit=1, maxBytes=1800, cursor=cursor)
            self.assertLessEqual(_size(page), 1800)
            rows.extend(page['items']); cursor = page['nextCursor']
        self.assertEqual(len({row['recordId'] for row in rows}), 4)
        self.source['fragments'][0]['text'] += '内容变化。'
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(StructureError) as caught:
            query_aggregates(self.root, kind='character-reviews', view='items', limit=1, maxBytes=1800, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_unknown_filters_rejected_and_all_queries_leave_business_files_unchanged(self):
        for arguments in ({'kind': 'unknown'}, {'kind': 'character-reviews', 'chapterId': self.cid},
                          {'kind': 'character-reviews', 'characterId': 'missing'},
                          {'kind': 'decision-plans', 'decisionId': 'missing'},
                          {'kind': 'chapter-issues', 'chapterId': 'missing'},
                          {'kind': 'character-reviews', 'maxBytes': True}):
            with self.assertRaises(StructureError):
                query_aggregates(self.root, **arguments)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for kind in ('chapter-issues', 'character-reviews', 'setting-impact', 'decision-plans'):
            query_aggregates(self.root, kind=kind)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_chapter_issues_load_one_structure_snapshot_for_every_chapter(self):
        """逐章聚合只加载一次结构：曾经每章各自重读四次（状态板、入库预检、两个状态投影），
        真实规模 119 章时一次聚合加载 421 次、耗时 60–79 秒。入库预检只对 review/stale 章跑。"""
        from unittest.mock import patch
        from scripts.core import aggregate_queries, chapter_status, structure_store
        second = deepcopy(self.chapter)
        second.update({'chapterId': 'CH-NEXT', 'title': '换岗', 'displayOrdinal': 2, 'filePath': '正文/换岗.md'})
        self.source['chapters'].append(second)
        self.source['chapterArcLinks'].append({'chapterId': 'CH-NEXT', 'arcId': self.source['arcs'][0]['arcId'],
                                               'role': 'primary'})
        self.source['chapters'][0]['status'] = 'review'
        validate_structure(self.source)
        write_json(self.root / '.gnovel/structure.json', self.source)
        loaded = []

        def recording_load(root):
            value = load_structure(root)
            loaded.append((value, deepcopy(value)))
            return value

        with patch.object(structure_store, 'validate_structure', wraps=structure_store.validate_structure) as validated, \
                patch.object(aggregate_queries, 'load_structure', side_effect=recording_load), \
                patch.object(chapter_status, 'preflight_finalization',
                             wraps=chapter_status.preflight_finalization) as preflight:
            report = query_aggregates(self.root, kind='chapter-issues', view='items')
        self.assertEqual(report['summary']['groupCount'], 2)
        self.assertEqual(validated.call_count, 1)
        self.assertEqual([call.args[1] for call in preflight.call_args_list], [self.cid])
        # 所有章节共享同一份快照，因此任何一章都不得改写它。
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0][0], loaded[0][1])
        self.assertTrue(any(row['kind'] == 'workflow-blocker' and row['chapterId'] == self.cid
                            for row in report['items']))


if __name__ == '__main__':
    unittest.main()
