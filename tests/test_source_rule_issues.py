from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_rule_issues import submit_rule_issue, query_rule_issues, query_rule_issue_receipt
from scripts.core.source_rules import evaluate_rules
from scripts.core.source_evidence import register_evidence
from scripts.core.source_model import snapshot_hash
from scripts.core.structure_store import validate_structure, StructureError
from scripts.core.write_sets import digest
from tests.test_source_plan import planning_source


class SourceRuleIssueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.chapter = planning_source(self.root); self.cid = self.chapter['chapterId']
        self.source['entities'].append({'entityId': 'RU-VOICE', 'entityType': 'rule', 'name': '认知边界',
            'ruleSpec': {'version': '1', 'method': 'semantic', 'enforcement': 'required',
                         'scopeIds': [self.source['projectId']], 'actions': ['review', 'publish'],
                         'inputIds': ['SF-PLAN']}})
        validate_structure(self.source); self.save()

    def save(self):
        write_json(self.root / '.gnovel/structure.json', self.source)

    def current(self, action='publish'):
        return evaluate_rules(self.source, self.cid, action, root=self.root)['items'][0]

    def submit(self, status, *, request='one', evidence=None, deferred=None):
        row = self.current()
        return submit_rule_issue(self.root, requestId=request, ruleId='RU-VOICE', chapterId=self.cid,
            action='publish', inputHash=row['inputHash'], status=status, reason='已逐段核对人物认知来源。',
            evidence=[{'kind': 'source_quote', 'recordId': 'SF-PLAN', 'start': 0, 'end': 5,
                       'text': '甲守城门。'}] if evidence is None else evidence,
            affectedIds=[self.cid, 'SF-PLAN'], deferredUntilScopeIds=deferred)

    def test_resolved_semantic_rule_passes_only_current_inputs(self):
        self.assertTrue(self.current()['blocking'])
        receipt = self.submit('resolved')
        self.assertEqual(receipt['code'], 'SOURCE_RULE_ISSUE_RECORDED')
        self.assertEqual(self.current()['status'], 'passed')
        self.assertFalse(self.current()['blocking'])
        self.source['fragments'][-1]['text'] = '甲离开城门。'; self.save()
        changed = self.current()
        self.assertEqual(changed['status'], 'pending')
        self.assertTrue(changed['blocking'])
        issues = query_rule_issues(self.root, chapterId=self.cid)
        publish = next(row for row in issues['items'] if row['action'] == 'publish')
        self.assertEqual(publish['effectiveStatus'], 'pending')
        self.assertEqual(publish['previousStatus'], 'resolved')

    def test_unrelated_edit_does_not_revoke_receipt(self):
        self.submit('resolved')
        self.source['entities'][1]['name'] = '无关人物改名'; self.save()
        self.assertEqual(self.current()['status'], 'passed')

    def test_issue_query_recomputes_dependency_hash_even_when_quote_is_unchanged(self):
        self.submit('resolved')
        self.source['attributes'][0]['values'][0]['value']['number'] = 185
        self.save()
        self.assertEqual(self.current()['status'], 'pending')
        for filters in ({}, {'chapterId': self.cid}, {'ruleId': 'RU-VOICE'}):
            with self.subTest(filters=filters):
                pending = query_rule_issues(self.root, statuses=['pending'], **filters)
                publish = next(row for row in pending['items'] if row['action'] == 'publish')
                self.assertEqual(publish['inputHash'], self.current()['inputHash'])
                self.assertEqual(publish['previousStatus'], 'resolved')
                self.assertEqual(query_rule_issues(self.root, statuses=['resolved'], **filters)['items'], [])

    def test_restoration_keeps_latest_negative_judgment_for_the_same_inputs(self):
        self.submit('resolved', request='original-pass')
        for status in ('confirmed', 'pending'):
            saved = self.submit(status, request=status, evidence=[] if status == 'pending' else None)
            self.source['attributes'][0]['values'][0]['value']['number'] = 185
            self.save()
            self.submit('pending', request=status + '-changed', evidence=[])
            self.source['attributes'][0]['values'][0]['value']['number'] = 190
            self.save()
            self.assertTrue(self.current()['blocking'])
            for filters in ({}, {'chapterId': self.cid}):
                row = next(row for row in query_rule_issues(self.root, **filters)['items']
                           if row['action'] == 'publish')
                self.assertEqual((row['effectiveStatus'], row['receiptId']), (status, saved['receiptId']))

    def test_archived_rule_issue_is_obsolete_instead_of_current_resolution(self):
        self.submit('resolved')
        self.source['entities'][-1]['status'] = 'archived'
        self.save()
        self.assertEqual(evaluate_rules(self.source, self.cid, 'publish', root=self.root)['items'], [])
        row = query_rule_issues(self.root, chapterId=self.cid)['items'][0]
        self.assertEqual(row['effectiveStatus'], 'obsolete')
        self.assertFalse(row['current'])

    def test_confirmed_and_deferred_required_issue_stay_blocking(self):
        self.submit('confirmed')
        self.assertEqual(self.current()['status'], 'conflict')
        self.source['volumes'].append({'volumeId': 'VOL-LATER', 'title': '后卷', 'ordinal': 2, 'status': 'planned'}); self.save()
        self.submit('deferred', request='defer', deferred=['VOL-LATER'])
        row = self.current()
        self.assertEqual(row['status'], 'deferred')
        self.assertTrue(row['blocking'])
        self.source['entities'][-1]['ruleSpec']['enforcement'] = 'advisory'; self.save()
        self.assertFalse(self.current()['blocking'])

    def test_exception_requires_declared_policy_and_current_evidence(self):
        with self.assertRaises(StructureError) as caught:
            self.submit('exception')
        self.assertEqual(caught.exception.code, 'SOURCE_RULE_EXCEPTION_FORBIDDEN')
        self.source['entities'][-1]['ruleSpec']['exceptionsAllowed'] = True; self.save()
        self.submit('exception', request='exception')
        self.assertEqual(self.current()['status'], 'passed')

    def test_checked_outcome_requires_exact_evidence(self):
        with self.assertRaises(StructureError) as caught:
            self.submit('resolved', evidence=[])
        self.assertEqual(caught.exception.code, 'SOURCE_RULE_EVIDENCE_REQUIRED')
        bad = [{'kind': 'source_quote', 'recordId': 'SF-PLAN', 'start': 0, 'end': 1, 'text': '乙'}]
        with self.assertRaises(StructureError) as caught:
            self.submit('resolved', evidence=bad)
        self.assertEqual(caught.exception.code, 'SOURCE_RULE_EVIDENCE_STALE')

    def test_idempotency_and_request_reuse(self):
        first = self.submit('resolved')
        again = self.submit('resolved')
        self.assertTrue(again['alreadyRecorded'])
        self.assertEqual(first['receiptId'], again['receiptId'])
        with self.assertRaises(StructureError) as caught:
            self.submit('confirmed')
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_historical_receipt_reports_current_validity(self):
        saved = self.submit('resolved')
        receipt = query_rule_issue_receipt(self.root, receiptId=saved['receiptId'])
        self.assertTrue(receipt['inputsStillCurrent'])
        self.source['fragments'][-1]['text'] = '甲离开城门。'; self.save()
        receipt = query_rule_issue_receipt(self.root, receiptId=saved['receiptId'])
        self.assertFalse(receipt['inputsStillCurrent'])

    def test_deferred_status_requires_future_scope(self):
        with self.assertRaises(StructureError):
            self.submit('deferred', deferred=[])
        with self.assertRaises(StructureError):
            self.submit('resolved', deferred=[self.cid])
        with self.assertRaises(StructureError):
            self.submit('deferred', deferred=[self.cid])

    def test_source_operation_provenance_must_exist(self):
        row = self.current()
        with self.assertRaises(StructureError):
            submit_rule_issue(self.root, requestId='bad-op', ruleId='RU-VOICE', chapterId=self.cid,
                action='publish', inputHash=row['inputHash'], status='pending', reason='等待检查。',
                evidence=[], affectedIds=[self.cid], sourceOperationId='OP-MISSING')

    def test_rule_version_change_obsoletes_old_receipt(self):
        self.submit('resolved')
        self.source['entities'][-1]['ruleSpec']['version'] = '2'; self.save()
        self.assertEqual(self.current()['status'], 'pending')

    def test_reverting_inputs_reactivates_matching_historical_receipt(self):
        self.submit('resolved', request='old')
        original = self.source['fragments'][-1]['text']
        self.source['fragments'][-1]['text'] = '甲离开城门。'; self.save()
        row = self.current()
        submit_rule_issue(self.root, requestId='new', ruleId='RU-VOICE', chapterId=self.cid,
            action='publish', inputHash=row['inputHash'], status='pending', reason='新版本尚未检查。',
            evidence=[], affectedIds=[self.cid])
        self.source['fragments'][-1]['text'] = original; self.save()
        self.assertEqual(self.current()['status'], 'passed')
        for filters in ({}, {'chapterId': self.cid}):
            row = next(row for row in query_rule_issues(self.root, statuses=['resolved'], **filters)['items']
                       if row['action'] == 'publish')
            self.assertTrue(row['current'])

    def test_pending_can_be_recorded_without_claimed_evidence(self):
        self.submit('pending', evidence=[])
        self.assertEqual(self.current()['status'], 'pending')

    def test_query_synthesizes_unreviewed_applicable_actions(self):
        result = query_rule_issues(self.root, chapterId=self.cid)
        self.assertEqual({row['action'] for row in result['items']}, {'review', 'publish'})
        self.assertTrue(all(row['effectiveStatus'] == 'pending' for row in result['items']))

    def test_prose_evidence_change_revokes_semantic_resolution(self):
        manuscript = self.root / '正文/守门.md'; manuscript.parent.mkdir(parents=True)
        manuscript.write_text('甲守城门。', encoding='utf-8')
        batch = register_evidence(self.root, chapterId=self.cid,
            fileHash=digest(manuscript.read_bytes()), sourceHash=snapshot_hash(self.source), requestId='prose',
            citations=[{'start': 0, 'end': 5, 'text': '甲守城门。', 'targetIds': ['SF-PLAN'],
                        'kind': 'depends_on', 'reason': '正文直接采用本章计划。'}])
        from scripts.core.source_evidence import read_evidence
        evidence_id = read_evidence(self.root)['batches'][batch['batchId']]['citations'][0]['evidenceId']
        self.submit('resolved', evidence=[{'kind': 'prose_evidence', 'evidenceId': evidence_id}])
        self.assertEqual(self.current()['status'], 'passed')
        manuscript.write_text('甲离开城门。', encoding='utf-8')
        self.assertEqual(self.current()['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
