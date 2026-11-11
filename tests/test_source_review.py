from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.source_changes import preview_source_change
from scripts.core.source_impact import REVIEW_PATH
from scripts.core.source_query import read_fragment
from scripts.core.source_render import render_source
from scripts.core.source_review import query_reviews, submit_review, RECEIPTS_PATH
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit
from tests.fixtures_source import source_fixture
from tests.fixtures_content_proposal import attribute_value_proposal


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture(self.root)

    def review(self, rid='fact-HEIGHT', decision='compatible'):
        row = next(r for r in query_reviews(self.root)['items'] if r['recordId'] == rid)
        text = read_fragment(self.root, **row['locator'])['text']
        return {k: row[k] for k in ('recordId', 'inputHash', 'contentHash')} | {
            'decision': decision, 'reason': '逐项核对该片段的登记依据。',
            'evidence': [{'start': 0, 'end': len(text), 'text': text}]}

    def change(self, *, target='attribute-HEIGHT', value=185):
        plan = attribute_value_proposal(self.root, target, value)
        commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def test_real_locators_and_all_reviewed_export(self):
        reviews = [self.review(row['recordId']) for row in query_reviews(self.root)['items']]
        before = (self.root / '.govel/structure.json').read_bytes()
        submit_review(self.root, requestId='review-all', reviews=reviews)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)
        self.assertEqual({r['status'] for r in query_reviews(self.root)['items']}, {'compatible'})
        self.assertTrue(render_source(self.root, requireReviewed=True)['ok'])

    def test_registered_input_change_invalidates_only_related_review(self):
        old = self.review()
        submit_review(self.root, requestId='checked', reviews=[old])
        self.change(target='attribute-BETA-HEIGHT', value=175)
        self.assertEqual(next(r for r in query_reviews(self.root)['items'] if r['recordId'] == 'fact-HEIGHT')['status'], 'compatible')
        self.change()
        row = next(r for r in query_reviews(self.root)['items'] if r['recordId'] == 'fact-HEIGHT')
        self.assertEqual(row['status'], 'pending')
        with self.assertRaises(StructureError) as error:
            submit_review(self.root, requestId='old-inputs', reviews=[old])
        self.assertEqual(error.exception.code, 'SOURCE_REVIEW_STALE')

    def test_bad_quote_and_false_blanket_flag_rejected(self):
        review = self.review()
        for bad in ({**review, 'reviewed': True}, {**review, 'evidence': []},
                    {**review, 'evidence': [{'start': 0, 'end': 2, 'text': '假话'}]},
                    {**review, 'decision': []}):
            with self.subTest(bad=bad), self.assertRaises(StructureError):
                submit_review(self.root, requestId='bad', reviews=[bad])
        self.assertFalse((self.root / REVIEW_PATH).exists())

    def test_receipt_idempotency_history_and_stale_retry(self):
        review = self.review()
        first = submit_review(self.root, requestId='once', reviews=[review])
        repeat = submit_review(self.root, requestId='once', reviews=[review])
        self.assertEqual(first['receiptId'], repeat['receiptId'])
        self.assertTrue(repeat['inputsStillCurrent'])
        self.change()
        self.assertFalse(submit_review(self.root, requestId='once', reviews=[review])['inputsStillCurrent'])
        self.assertEqual(len(read_json(self.root / RECEIPTS_PATH)['receipts']), 1)
        with self.assertRaises(StructureError):
            submit_review(self.root, requestId='once', reviews=[{**review, 'reason': '另一判断'}])

    def test_explicit_not_checked_never_passes_export_gate(self):
        review = self.review(decision='not_checked'); review['evidence'] = []
        submit_review(self.root, requestId='unread', reviews=[review])
        with self.assertRaises(StructureError): render_source(self.root, requireReviewed=True)

    def test_two_runtime_files_rollback_together(self):
        review = self.review()
        def fail(phase, relative=None):
            if phase == 'write': raise OSError('interrupt receipt')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(OSError): submit_review(self.root, requestId='fail', reviews=[review])
        self.assertFalse((self.root / REVIEW_PATH).exists())
        self.assertFalse((self.root / RECEIPTS_PATH).exists())

    def test_runtime_cursor_bound_to_review_changes(self):
        page = query_reviews(self.root, limit=1)
        submit_review(self.root, requestId='change-status', reviews=[self.review()])
        with self.assertRaises(StructureError) as error:
            query_reviews(self.root, limit=1, cursor=page['nextCursor'])
        self.assertEqual(error.exception.code, 'CURSOR_STALE')

    def test_direct_source_edit_does_not_keep_old_review_fresh(self):
        submit_review(self.root, requestId='checked', reviews=[self.review()])
        self.source['attributes'][0]['values'][0]['value']['number'] = 185
        write_json(self.root / '.govel/structure.json', self.source)
        self.assertEqual(next(r for r in query_reviews(self.root)['items'] if r['recordId'] == 'fact-HEIGHT')['status'], 'stale')

    def test_compatible_state_without_matching_receipt_cannot_pass(self):
        row = self.review()
        write_json(self.root / REVIEW_PATH, {'items': {'fact-HEIGHT': {
            'recordId': 'fact-HEIGHT', 'inputHash': row['inputHash'], 'status': 'compatible'}}})
        with self.assertRaises(StructureError) as error: render_source(self.root, requireReviewed=True)
        self.assertEqual(error.exception.code, 'SOURCE_REVIEW_INVALID')

    def test_exact_restoration_reuses_receipts_in_query_render_and_triage(self):
        from scripts.core.source_review_triage import triage_reviews
        reviews = [self.review(row['recordId']) for row in query_reviews(self.root)['items']]
        original = submit_review(self.root, requestId='review-original', reviews=reviews)
        self.change(value=185)
        changed_reviews = [self.review(row['recordId']) for row in query_reviews(self.root)['items']]
        changed = submit_review(self.root, requestId='review-changed', reviews=changed_reviews)
        self.change(value=180)
        self.assertTrue(triage_reviews(self.root)['items'])
        for value, expected in [(185, changed), (190, original)]:
            self.change(value=value)
            rows = query_reviews(self.root)['items']
            self.assertEqual({row['status'] for row in rows}, {'compatible'})
            self.assertEqual({row['receiptId'] for row in rows}, {expected['receiptId']})
            self.assertEqual(triage_reviews(self.root)['items'], [])
            self.assertTrue(render_source(self.root, requireReviewed=True)['ok'])

    def test_latest_exact_judgment_wins_over_earlier_compatible_receipt(self):
        from scripts.core.source_review_triage import triage_reviews
        submit_review(self.root, requestId='initial-pass', reviews=[self.review()])
        for decision in ('conflict', 'needs_information', 'not_checked'):
            review = self.review(decision=decision)
            if decision == 'not_checked':
                review['evidence'] = []
            receipt = submit_review(self.root, requestId=decision, reviews=[review])
            self.change(value=185)
            self.change(value=190)
            row = next(row for row in query_reviews(self.root)['items'] if row['recordId'] == 'fact-HEIGHT')
            self.assertEqual((row['status'], row['receiptId']), (decision, receipt['receiptId']))
            self.assertIn('fact-HEIGHT', [row['recordId'] for row in triage_reviews(self.root)['items']])
            self.assertIn('fact-HEIGHT', [row['recordId'] for row in triage_reviews(self.root, statuses=[decision])['items']])
            with self.assertRaises(StructureError):
                render_source(self.root, selection={'includeAttributes': ['attribute-HEIGHT']}, requireReviewed=True)

    def test_latest_matching_receipt_with_bad_content_or_quote_cannot_resurrect_old_pass(self):
        from scripts.core.source_review import fingerprint, query_receipt
        first = submit_review(self.root, requestId='pass-first', reviews=[self.review()])
        latest = submit_review(self.root, requestId='pass-latest', reviews=[self.review()])
        pristine = read_json(self.root / RECEIPTS_PATH)
        for field in ('contentHash', 'evidence'):
            history = deepcopy(pristine)
            receipt = history['receipts'][latest['receiptId']]
            review = receipt['reviews'][0]
            review[field] = '0' * 64 if field == 'contentHash' else [{'start': 0, 'end': 1, 'text': '错'}]
            receipt['requestHash'] = fingerprint({'actor': receipt['actor'], 'reviews': receipt['reviews']})
            write_json(self.root / RECEIPTS_PATH, history)
            row = next(row for row in query_reviews(self.root)['items'] if row['recordId'] == 'fact-HEIGHT')
            self.assertEqual(row['status'], 'pending')
            self.assertNotEqual(row['receiptId'], first['receiptId'])
            self.assertFalse(query_receipt(self.root, receiptId=latest['receiptId'])['items'][0]['inputsStillCurrent'])

    def test_restored_semantic_review_never_restores_chapter_approval(self):
        from scripts.core.workflow import default_workflow, require_approved_artifact, WorkflowError
        from tests.test_source_plan import planning_source
        from tests.fixtures_workflow import register_and_approve, evidenced_slots
        from scripts.core.source_plan import chapter_plan
        from scripts.core.artifacts import sha256_text
        source, chapter = planning_source(self.root)
        cid = chapter['chapterId']; workflow = default_workflow()
        workflow['approvedChapterIds'] = [cid]
        write_json(self.root / '.govel/workflow.json', workflow)
        plan_hash = chapter_plan(source, cid)['sourcePlanHash']
        text = '他核对完记录，关上了门。'
        register_and_approve(self.root, cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'sourcePlanHash': plan_hash,
            'consensus': [], 'disagreements': [], 'openItems': []})
        register_and_approve(self.root, cid, 'candidate_prose', {'text': text})
        register_and_approve(self.root, cid, 'stage_b_review', {
            'verdict': 'POLISH_READY', 'sourcePlanHash': plan_hash,
            'finalProseSha256': sha256_text(text), 'slots': evidenced_slots(text), 'findings': []})
        submit_review(self.root, requestId='plan-reviewed', reviews=[self.review('plan-PLAN')])
        self.change(value=185)
        self.change(value=190)
        row = next(row for row in query_reviews(self.root)['items'] if row['recordId'] == 'plan-PLAN')
        self.assertEqual(row['status'], 'compatible')
        current = read_json(self.root / '.govel/workflow.json')
        self.assertFalse(current['approvals'][cid]['approved'])
        for kind in ('stage_a_review', 'stage_b_review'):
            with self.assertRaises(WorkflowError):
                require_approved_artifact(current, cid, kind)
