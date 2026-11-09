"""Own explicit evidence submissions may advance progress, not change frozen reading."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import write_json, read_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from tests.fixtures_source import source_fixture


class EvidenceReadContinuationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source = source_fixture(self.root)
        self.args = {'kind': 'review', 'recordIds': ['SF-HEIGHT', 'SF-MIXED'], 'includeContext': True, 'limit': 1}

    def prepare(self, **extra):
        return call_operation('evidence.prepare', self.root, {**self.args, **extra})

    def submit(self, page, rid='SF-HEIGHT', request='first'):
        packed = call_operation('evidence.pack', self.root, {'snapshotRef': page['snapshotRef'], 'requestId': request,
            'decisions': [{'recordId': rid, 'decision': 'not_checked', 'reason': 'Explicitly deferred fixture', 'quotes': []}]})
        result = call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef']})
        self.assertEqual(result['recordedCount'], 1)
        return result

    def test_initial_and_existing_ledgers_resume_only_with_explicit_request(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                page = self.prepare()
                request = 'existing' if existing else 'initial'
                self.submit(page, request=request)
                with self.assertRaises(StructureError):
                    self.prepare(cursor=page['nextCursor'])
                next_page = self.prepare(cursor=page['nextCursor'], resumeRequestIds=[request])
                self.assertEqual(next_page['snapshotRef'], page['snapshotRef'])
                self.assertEqual(next_page['continuation']['recordedCount'], 1)
                self.assertTrue(next_page['continuation']['materialsAtPreparation'])
                self.assertFalse(next_page['approvalGranted'])

    def test_aggregate_scope_and_progress_can_resume_across_two_own_submissions(self):
        aggregate = call_operation('query.aggregate', self.root, {'kind': 'character-reviews', 'characterId': 'C-ALPHA'})
        self.args['aggregateRef'] = aggregate['selectionRef']
        page = self.prepare()
        self.submit(page)
        next_page = self.prepare(cursor=page['nextCursor'], resumeRequestIds=['first'])
        self.submit(page, 'SF-MIXED', 'second')
        resumed = self.prepare(cursor=next_page['nextCursor'], resumeRequestIds=['first', 'second'])
        self.assertEqual(resumed['continuation']['recordedCount'], 2)
        self.assertFalse(resumed['semanticReviewComplete'])

    def test_unrelated_review_changes_cannot_be_folded_into_the_baseline(self):
        page = self.prepare(); self.submit(page)
        other = call_operation('evidence.prepare', self.root, {'kind': 'review', 'recordIds': ['SF-LIFE']})
        self.submit(other, 'SF-LIFE', 'unrelated')
        for requests in (['first'], ['first', 'unrelated']):
            with self.assertRaises(StructureError):
                self.prepare(cursor=page['nextCursor'], resumeRequestIds=requests)

    def test_source_scope_budget_and_missing_history_remain_protected(self):
        page = self.prepare(); self.submit(page)
        for overrides in ({'recordIds': ['SF-HEIGHT']}, {'limit': 2}, {'selection': {'ownerIds': ['C-BETA']}},
                          {'resumeRequestIds': []}, {'resumeRequestIds': ['missing']}):
            with self.subTest(overrides=overrides), self.assertRaises(StructureError):
                self.prepare(cursor=page['nextCursor'], **{'resumeRequestIds': ['first'], **overrides})
        self.source['fragments'][0]['text'] += ' External change.'
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(StructureError):
            self.prepare(cursor=page['nextCursor'], resumeRequestIds=['first'])

    def test_tampered_request_and_replaced_aggregate_are_rejected(self):
        aggregate = call_operation('query.aggregate', self.root, {'kind': 'character-reviews', 'characterId': 'C-ALPHA'})
        self.args['aggregateRef'] = aggregate['selectionRef']
        page = self.prepare(); self.submit(page)
        other = call_operation('query.aggregate', self.root, {'kind': 'character-reviews', 'characterId': 'C-ALPHA'})
        with self.assertRaises(StructureError):
            self.prepare(cursor=page['nextCursor'], resumeRequestIds=['first'], aggregateRef=other['selectionRef'])
        path = self.root / '.gnovel/source-review-receipts.json'
        ledger = read_json(path)
        next(iter(ledger['requests'].values()))['snapshot']['sourceHash'] = 'tampered'
        write_json(path, ledger)
        with self.assertRaises(StructureError):
            self.prepare(cursor=page['nextCursor'], resumeRequestIds=['first'])
