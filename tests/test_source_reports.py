from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.jsonio import read_json, write_json
from scripts.core.project import query_policy
from scripts.core.query import _size
from scripts.core.source_changes import preview_source_change
from scripts.core.source_reports import query_change_report
from scripts.core.source_review import query_reviews, query_receipt, submit_review, RECEIPTS_PATH
from scripts.core.structure_store import load_structure, StructureError
from scripts.core.structure_transaction import commit, undo
from tests.fixtures_source import source_fixture
from tests.fixtures_content_proposal import attribute_value_proposal


class SourceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); source_fixture(self.root)

    def preview(self, height=185, key='change'):
        return attribute_value_proposal(self.root, 'AT-HEIGHT', height)

    def commit(self, plan):
        return commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def test_large_report_is_bounded_but_all_impacts_can_be_read(self):
        source = load_structure(self.root)
        template = source['fragments'][0]
        source['fragments'].extend({**deepcopy(template), 'fragmentId': 'SF-LARGE-' + str(i), 'order': i + 10} for i in range(400))
        write_json(self.root / '.gnovel/structure.json', source)
        plan = self.preview()
        self.assertTrue(plan['responseCompacted'])
        self.assertLessEqual(_size(plan), query_policy()['maxBytes']['detail'])
        rows, cursor = [], None
        while True:
            page = query_change_report(self.root, operationId=plan['operationId'], limit=50, cursor=cursor)
            rows.extend(page['items']); cursor = page['nextCursor']
            if not cursor: break
        expected = read_json(self.root / f'.gnovel/plans/{plan["operationId"]}.json')['impact']
        self.assertEqual(len(rows), sum(len(value) for value in expected.values() if isinstance(value, list)))
        result = self.commit(plan)
        self.assertTrue(result['responseCompacted'])
        self.assertEqual(query_change_report(self.root, operationId=plan['operationId'])['totalMatching'], len(rows))

    def test_report_cursor_cannot_cross_commit(self):
        plan = self.preview()
        first = query_change_report(self.root, operationId=plan['operationId'], limit=1)
        self.commit(plan)
        with self.assertRaises(StructureError) as error:
            query_change_report(self.root, operationId=plan['operationId'], limit=1, cursor=first['nextCursor'])
        self.assertEqual(error.exception.code, 'CURSOR_STALE')

    def test_undo_preserves_review_history_and_recomputes_current_work(self):
        plan = self.preview(); self.commit(plan)
        item = next(r for r in query_reviews(self.root)['items'] if r['recordId'] == 'SF-HEIGHT')
        review = {k: item[k] for k in ('recordId', 'inputHash', 'contentHash')} | {
            'decision': 'conflict', 'reason': '原文一米九与新值不符。',
            'evidence': [{'start': 0, 'end': 7, 'text': '他身高一米九。'}]}
        receipt = submit_review(self.root, requestId='conflict', reviews=[review])
        history = (self.root / RECEIPTS_PATH).read_bytes()
        result = undo(self.root, plan['operationId'], actor='test')
        self.assertEqual((self.root / RECEIPTS_PATH).read_bytes(), history)
        self.assertFalse(query_receipt(self.root, receiptId=receipt['receiptId'])['items'][0]['inputsStillCurrent'])
        self.assertIn('SF-HEIGHT', result['impact']['reviewFragmentIds'])
        report = query_change_report(self.root, operationId=plan['operationId'], undoRevision=result['revision'])
        self.assertTrue(report['historical'])
        self.assertTrue(any(r.get('recordId') == 'SF-HEIGHT' for r in report['items']))

    def test_three_changes_and_undo_keep_latest_target_and_history(self):
        plans = []
        for number in (185, 180, 175):
            plan = self.preview(number, key=str(number)); self.commit(plan); plans.append(plan)
        for plan, expected in zip(reversed(plans), (180, 185, 190)):
            undo(self.root, plan['operationId'], actor='test')
            self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], expected)
            row = next(r for r in query_reviews(self.root)['items'] if r['recordId'] == 'SF-HEIGHT')
            self.assertEqual(row['status'], 'pending')
        self.assertEqual(load_structure(self.root)['revision'], 6)
        self.assertEqual(len(list((self.root / '.gnovel/undo-receipts').glob('*.json'))), 3)
