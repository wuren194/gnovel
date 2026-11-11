"""Review triage: cluster outstanding reviews by origin, layer by relation.

聚类只改变呈现顺序：行级保留逐条复核义务，簇摘要只汇总数量。来源已撤销或
journal 不可达的簇显式标注；无来源操作的记录不归簇。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.source_fact_patch import preview_fact_patch
from scripts.core.source_review_triage import triage_reviews
from scripts.core.source_model import snapshot_hash
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit, undo
from tests.fixtures_content_proposal import attribute_value_proposal
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


def patch_args(root: Path, request_id: str) -> dict:
    return {'sourceHash': snapshot_hash(load_structure(root)),
            'patches': [{'recordId': 'fact-HEIGHT', 'replacements': [{'old': '一米九', 'new': '一米八'}], 'reason': '方案 Z-1'}],
            'approvalActor': 'author', 'approvalEvidence': '作者要求按定稿方案修订', 'requestId': request_id}


def all_clusters(root: Path, **filters) -> list[dict]:
    """Read every cluster through the paged clusters view, as a model client would."""
    rows, cursor = [], None
    while True:
        page = triage_reviews(root, view='clusters', **filters, **({'cursor': cursor} if cursor else {}))
        rows.extend(page['items'])
        if not page['hasMore']:
            return rows
        cursor = page['nextCursor']


class ReviewTriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source_fixture(self.root)

    def test_attribute_change_puts_dependents_in_dependent_layer(self):
        """改属性值：changedIds 只含 attribute-HEIGHT；依赖片段 pending 且全部是 dependent 层。"""
        plan = attribute_value_proposal(self.root, 'attribute-HEIGHT', 185)
        result = commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'],
                        approved=True, actor='test')
        self.assertTrue(result['ok'], result)
        triage = triage_reviews(self.root)
        rows = {row['recordId']: row for row in triage['items']}
        # 依赖 FV-HEIGHT 的四个片段全部进入待处理，且都不是直接修改目标
        for rid in ('fact-HEIGHT', 'fact-LOOK', 'fact-MIXED', 'fact-LIFE'):
            self.assertEqual(rows[rid]['status'], 'pending', rid)
            self.assertEqual(rows[rid]['layer'], 'dependent', rid)
            self.assertEqual(rows[rid]['operationId'], result['operationId'], rid)
        clusters = all_clusters(self.root)
        cluster = next(c for c in clusters if c['operationId'] == result['operationId'])
        self.assertEqual(cluster['recordCount'], 4)
        self.assertEqual(cluster['layer'], 'dependent')
        # 簇摘要只汇总数量，逐条行仍在
        self.assertEqual(sum(c['recordCount'] for c in clusters),
                         len(triage['items']))
        self.assertTrue(triage['coverage']['perItemReviewObligationPreserved'])

    def test_newer_operation_replaces_earlier_attribution(self):
        """改值后片段 pending 归属属性操作（dependent）；随后改文本覆盖归属为
        patch 操作（direct）。review state 每片段只保留一条当前记录——归属指向
        最近一次让它待复核的操作，簇摘要反映当前状态。"""
        plan = attribute_value_proposal(self.root, 'attribute-HEIGHT', 185)
        attr_result = commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'],
                             approved=True, actor='test')
        before = triage_reviews(self.root)
        self.assertEqual({row['recordId'] for row in before['items']},
                         {'fact-HEIGHT', 'fact-LOOK', 'fact-MIXED', 'fact-LIFE'})
        self.assertTrue(all(row['layer'] == 'dependent' for row in before['items']))
        patch = preview_fact_patch(self.root, **patch_args(self.root, 'patch-1'))
        patch_result = commit(self.root, operation_id=patch['operationId'], plan_hash=patch['planHash'],
                              approved=True, actor='test')
        after = triage_reviews(self.root)
        rows = {row['recordId']: row for row in after['items']}
        self.assertEqual(rows['fact-HEIGHT']['layer'], 'direct')
        self.assertEqual(rows['fact-HEIGHT']['operationId'], patch_result['operationId'])
        for rid in ('fact-LOOK', 'fact-MIXED', 'fact-LIFE'):
            self.assertEqual(rows[rid]['layer'], 'dependent', rid)
            self.assertEqual(rows[rid]['operationId'], attr_result['operationId'], rid)
        # 直接层簇与依赖层簇分列
        layers = {c['layer'] for c in all_clusters(self.root)}
        self.assertEqual(layers, {'direct', 'dependent'})

    def test_unattributed_reviews_are_not_forced_into_a_cluster(self):
        """初始项目没有任何 review 记录：全部 unreviewed 且 operationId 为 null。"""
        triage = triage_reviews(self.root)
        self.assertTrue(triage['items'])
        self.assertTrue(all(row['operationId'] is None for row in triage['items']))
        self.assertTrue(all(row['layer'] == 'unattributed' for row in triage['items']))
        self.assertTrue(all(row['status'] == 'unreviewed' for row in triage['items']))
        cluster = all_clusters(self.root)[0]
        self.assertIsNone(cluster['operationId'])
        self.assertEqual(cluster['layer'], 'unattributed')

    def test_runtime_operation_reference_is_not_loaded_as_journal(self):
        """undo 后 review state 里的运行时操作 ID（非 OP- 格式）不触发 journal 加载崩溃。"""
        plan = attribute_value_proposal(self.root, 'attribute-HEIGHT', 185)
        result = commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'],
                        approved=True, actor='test')
        undo(self.root, result['operationId'], actor='test')
        triage = triage_reviews(self.root, statuses=['pending'])
        # 撤销把源恢复到提交前；撤销期间 review_outputs 重写过的 pending 引用若指向
        # 非 OP- 格式的运行时操作，按 receipt_missing 呈现而非崩溃
        for row in triage['items']:
            if row['layer'] == 'receipt_missing':
                self.assertIsNone(row['operationId'])
        for cluster in all_clusters(self.root, statuses=['pending']):
            if cluster['layer'] == 'receipt_missing':
                self.assertIsNone(cluster['operationId'])

    def test_status_and_owner_filters_and_validation(self):
        plan = attribute_value_proposal(self.root, 'attribute-HEIGHT', 185)
        commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')
        triage = triage_reviews(self.root, statuses=['unreviewed'])
        self.assertEqual(triage['items'], [])
        triage = triage_reviews(self.root, ownerIds=['character-ALPHA'])
        self.assertTrue(triage['items'])
        self.assertEqual({row['ownerId'] for row in triage['items']}, {'character-ALPHA'})
        with self.assertRaises(Exception):
            triage_reviews(self.root, statuses=['resolved'])

    def test_clusters_are_paged_and_summary_carries_counts_only(self):
        """真实项目 65 个簇各带全书章节清单，内联进 summary 后约 140KB，任何预算都放不下，
        review-triage 整体 FIELD_TOO_LARGE。簇改为与逐条记录一样按预算分页。"""
        plan = attribute_value_proposal(self.root, 'attribute-HEIGHT', 185)
        commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')
        patch = preview_fact_patch(self.root, **patch_args(self.root, 'patch-1'))
        commit(self.root, operation_id=patch['operationId'], plan_hash=patch['planHash'], approved=True, actor='test')
        items = triage_reviews(self.root)
        first = triage_reviews(self.root, view='clusters', limit=1)
        for page in (items, first):
            self.assertEqual(set(page['summary']), {'outstandingCount', 'clusterCount', 'unattributedCount'})
        self.assertEqual(items['summary'], first['summary'])
        self.assertEqual((first['view'], first['returned'], first['totalMatching']), ('clusters', 1, 2))
        self.assertTrue(first['hasMore'])
        clusters = all_clusters(self.root)
        self.assertEqual(len(clusters), first['summary']['clusterCount'])
        self.assertEqual(sum(c['recordCount'] for c in clusters), items['summary']['outstandingCount'])
        # 游标绑定视图：簇的续读游标不能拿去续读逐条记录。
        with self.assertRaises(StructureError) as caught:
            triage_reviews(self.root, cursor=first['nextCursor'], limit=1)
        self.assertEqual(caught.exception.code, 'CURSOR_QUERY_MISMATCH')
        with self.assertRaises(StructureError) as caught:
            triage_reviews(self.root, view='summary')
        self.assertEqual(caught.exception.code, 'QUERY_VIEW_INVALID')

    def test_cli_cluster_view(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'source', 'review-triage',
                               '--project-root', str(self.root), '--view', 'clusters'], capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['view'], 'clusters')
        self.assertEqual([(row['layer'], row['recordCount']) for row in payload['items']], [('unattributed', 4)])

    def test_cli_empty_triage_does_not_claim_consistency(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project',
                               'source', 'review-triage', '--project-root', str(self.root)],
                              capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['ok'], payload)
        self.assertEqual(payload['summary']['outstandingCount'], 4)
        self.assertIn('not that the text is consistent', payload['coverage']['absenceMeaning'])


if __name__ == '__main__':
    unittest.main()
