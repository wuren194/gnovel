from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys
import tempfile
import unittest

from scripts.core.jsonio import read_json, write_json
from scripts.core.source_changes import preview_source_change
from scripts.core.source_content_proposals import resolve_content_target, preview_content_change
from scripts.core.source_fact_patch import preview_fact_patch
from scripts.core.source_maintenance import check_maintenance
from scripts.core.source_maintenance_prepare import prepare_maintenance
from scripts.core.source_model import snapshot_hash
from scripts.core.source_reports import query_change_report
from scripts.core.source_requests import query_request_status
from scripts.core.source_review import query_reviews, submit_review
from scripts.core.source_selection import select_project_content
from scripts.core.structure_commands import _plan_hash
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit, undo
from scripts.core.write_sets import digest
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source_fixture(self.root)
        (self.root / '工作台').mkdir()
        self.document = self.root / '工作台/方案.md'
        self.document.write_text('Z-1：统一身高。\nZ-2：调整经历。')
        self.checklist = {'source': {'path': '工作台/方案.md', 'sha256': digest(self.document.read_bytes())}, 'items': [
            {'itemId': 'Z-1', 'quote': 'Z-1：统一身高。', 'recordIds': ['fact-HEIGHT'], 'operationIds': []},
            {'itemId': 'Z-2', 'quote': 'Z-2：调整经历。', 'recordIds': ['fact-LIFE'], 'operationIds': []}]}
        write_json(self.root / '工作台/清单.json', self.checklist)
        self.reference = {'checklistPath': '工作台/清单.json', 'itemIds': ['Z-1', 'Z-2']}
        self.args = {'sourceHash': snapshot_hash(load_structure(self.root)), 'patches': [
            {'recordId': 'fact-HEIGHT', 'replacements': [{'old': '一米九', 'new': '一米八'}], 'reason': '方案 Z-1'},
            {'recordId': 'fact-LIFE', 'replacements': [{'old': '独自', 'new': '与同伴一起'}], 'reason': '方案 Z-2'}],
            'approvalActor': 'author', 'approvalEvidence': '作者要求按定稿方案修订', 'requestId': 'batch-1',
            'maintenanceReference': self.reference}

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def commit(self, plan):
        return commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def check(self):
        return check_maintenance(self.root, checklistPath='工作台/清单.json')

    def test_prepare_is_read_only_and_returns_unenumerated_draft(self):
        before = self.snapshot()
        report = prepare_maintenance(self.root, sourcePath='工作台/方案.md')
        self.assertTrue(report['writeReady'])
        self.assertEqual(report['checklistDraft'], {'source': self.checklist['source'], 'items': []})
        self.assertFalse(report['coverage']['approvalGranted'])
        self.assertTrue(Path(report['cliPath']).is_file())
        self.assertEqual(self.snapshot(), before)
        write_json(self.root / '工作台/草稿.json', report['checklistDraft'])
        self.assertTrue(check_maintenance(self.root, checklistPath='工作台/草稿.json')['summary']['needsMapping'])
        source = load_structure(self.root); source['schemaVersion'] = 2
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError) as caught:
            prepare_maintenance(self.root, sourcePath='工作台/方案.md')
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_PROJECT_VERSION')

    def test_retry_resumes_prepared_committed_and_undone_without_reapplying(self):
        self.assertEqual(query_request_status(self.root, requestId='batch-1')['operationState'], 'not_started')
        plan = preview_fact_patch(self.root, **self.args)
        before = self.snapshot()
        self.assertEqual(preview_fact_patch(self.root, **self.args), plan)
        self.assertEqual(query_request_status(self.root, requestId='batch-1')['planHash'], plan['planHash'])
        self.assertEqual(self.snapshot(), before)
        self.commit(plan)
        before = self.snapshot()
        resumed = preview_fact_patch(self.root, **self.args)
        self.assertEqual(resumed['operationState'], 'committed')
        self.assertEqual(query_request_status(self.root, requestId='batch-1')['operationState'], 'committed')
        self.assertEqual(self.snapshot(), before)
        undo(self.root, plan['operationId'], actor='test')
        before = self.snapshot()
        self.assertEqual(preview_fact_patch(self.root, **self.args)['operationState'], 'undone')
        self.assertIsNone(query_request_status(self.root, requestId='batch-1')['planHash'])
        self.assertEqual(self.snapshot(), before)

    def test_same_request_id_cannot_change_payload_or_approval(self):
        preview_fact_patch(self.root, **self.args)
        for field, value in [('approvalActor', 'someone_else'), ('approvalEvidence', 'new approval'),
                             ('maintenanceReference', {'checklistPath': '工作台/清单.json', 'itemIds': ['Z-1']})]:
            args = deepcopy(self.args); args[field] = value
            with self.subTest(field=field), self.assertRaises(StructureError) as error:
                preview_fact_patch(self.root, **args)
            self.assertEqual(error.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_auto_linkage_is_in_receipt_without_checklist_rewrite(self):
        plan = preview_fact_patch(self.root, **self.args)
        checklist_bytes = (self.root / '工作台/清单.json').read_bytes()
        self.assertEqual(self.check()['summary']['submittedItemCount'], 0)
        receipt = self.commit(plan)
        self.assertEqual(receipt['requestId'], 'batch-1')
        self.assertEqual(len(receipt['maintenance']['items']), 2)
        self.assertNotIn('maintenanceGuards', receipt)
        self.assertEqual((self.root / '工作台/清单.json').read_bytes(), checklist_bytes)
        checked = self.check()
        self.assertEqual(checked['summary']['submittedItemCount'], 2)
        self.assertEqual(checked['summary']['reviewReadyItemCount'], 0)
        self.assertEqual(checked['items'][0]['automaticallyLinkedOperationIds'], [plan['operationId']])
        details = query_change_report(self.root, operationId=plan['operationId'], section='maintenance')
        self.assertEqual(details['totalMatching'], 2)
        self.assertEqual(details['items'][0]['source'], self.checklist['source'])

    def test_source_document_or_checklist_drift_refuses_commit(self):
        plan = preview_fact_patch(self.root, **self.args)
        for path in (self.document, self.root / '工作台/清单.json'):
            old = path.read_bytes(); path.write_bytes(old + b'\n')
            before = self.snapshot()
            status = query_request_status(self.root, requestId='batch-1')
            self.assertEqual(status['operationState'], 'stale')
            self.assertIsNone(status['planHash'])
            with self.assertRaises(StructureError): self.commit(plan)
            with self.assertRaises(StructureError) as error: preview_fact_patch(self.root, **self.args)
            self.assertEqual(error.exception.code, 'SOURCE_REQUEST_STALE')
            self.assertEqual(self.snapshot(), before)
            path.write_bytes(old)

    def test_guard_rechecks_late_changes_and_rolls_back_only_transaction(self):
        plan = preview_fact_patch(self.root, **self.args)
        source_bytes = (self.root / '.govel/structure.json').read_bytes()
        original = self.document.read_bytes()
        def mutate(stage, *_args):
            if stage == 'prepared': self.document.write_bytes(original + b'\nchanged externally')
        with patch('scripts.core.write_sets.checkpoint', side_effect=mutate), self.assertRaises(StructureError):
            self.commit(plan)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), source_bytes)
        self.assertEqual(self.document.read_bytes(), original + b'\nchanged externally')
        self.assertFalse((self.root / f'.govel/operations/{plan["operationId"]}.json').exists())

    def test_guard_rechecks_after_structure_write(self):
        plan = preview_fact_patch(self.root, **self.args)
        source_bytes = (self.root / '.govel/structure.json').read_bytes()
        original = self.document.read_bytes()
        def mutate(stage, *args):
            if stage == 'write' and args == ('.govel/structure.json',):
                self.document.write_bytes(original + b'\nchanged after write')
        with patch('scripts.core.write_sets.checkpoint', side_effect=mutate), self.assertRaises(StructureError):
            self.commit(plan)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), source_bytes)
        self.assertEqual(self.document.read_bytes(), original + b'\nchanged after write')
        self.assertFalse((self.root / f'.govel/operations/{plan["operationId"]}.json').exists())

    def test_expired_request_is_read_only_until_exact_preview_is_renewed(self):
        plan = preview_fact_patch(self.root, **self.args)
        before = self.snapshot()
        with patch('scripts.core.source_requests.time.time', return_value=plan['expiresAtEpoch'] + 1):
            status = query_request_status(self.root, requestId='batch-1')
            self.assertEqual(status['operationState'], 'stale')
            self.assertIsNone(status['planHash'])
            self.assertTrue(status['renewable'])
            self.assertEqual(status['staleReasons'], ['expired'])
            self.assertEqual(self.snapshot(), before)
            renewed = preview_fact_patch(self.root, **self.args)
            self.assertEqual(renewed['operationId'], plan['operationId'])
            self.assertNotEqual(renewed['planHash'], plan['planHash'])
            self.assertEqual(renewed['writeSet'], plan['writeSet'])
            self.assertEqual(renewed['parameters'], plan['parameters'])
            self.assertEqual(query_request_status(self.root, requestId='batch-1')['operationState'], 'prepared')
            with self.assertRaises(StructureError) as error:
                self.commit(plan)
            self.assertEqual(error.exception.code, 'PLAN_HASH_MISMATCH')
            self.commit(renewed)

    def test_rehashed_plan_cannot_omit_input_guards(self):
        plan = preview_fact_patch(self.root, **self.args)
        path = self.root / f'.govel/plans/{plan["operationId"]}.json'
        saved = read_json(path); saved.pop('maintenanceGuards')
        saved['planHash'] = _plan_hash({k: v for k, v in saved.items() if k != 'planHash'})
        write_json(path, saved)
        before = self.snapshot()
        with self.assertRaises(StructureError) as error: self.commit(saved)
        self.assertEqual(error.exception.code, 'MAINTENANCE_BINDING_STALE')
        self.assertEqual(self.snapshot(), before)

    def test_item_without_changed_target_is_refused(self):
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass
        args = deepcopy(self.args); args['patches'] = args['patches'][:1]
        before = self.snapshot()
        with self.assertRaises(StructureError) as error: preview_fact_patch(self.root, **args)
        self.assertEqual(error.exception.code, 'MAINTENANCE_SCOPE_MISMATCH')
        self.assertEqual(self.snapshot(), before)

    def test_same_ids_in_another_document_do_not_link(self):
        self.commit(preview_fact_patch(self.root, **self.args))
        second = self.root / '工作台/另一份方案.md'; second.write_bytes(self.document.read_bytes())
        self.checklist['source']['path'] = '工作台/另一份方案.md'
        write_json(self.root / '工作台/清单.json', self.checklist)
        self.assertEqual(self.check()['summary']['submittedItemCount'], 0)

    def test_undo_and_explicit_new_request_can_cover_same_item_again(self):
        plan = preview_fact_patch(self.root, **self.args); self.commit(plan)
        undo(self.root, plan['operationId'], actor='test')
        self.assertEqual(self.check()['summary']['submittedItemCount'], 0)
        args = deepcopy(self.args); args['requestId'] = 'batch-2'; args['sourceHash'] = snapshot_hash(load_structure(self.root))
        replacement = preview_fact_patch(self.root, **args); self.commit(replacement)
        self.assertEqual(self.check()['summary']['submittedItemCount'], 2)
        self.assertEqual(self.check()['items'][0]['undoneOperationIds'], [plan['operationId']])

    def test_generic_source_and_content_proposal_share_item_linkage(self):
        source = load_structure(self.root)
        record = deepcopy(source['fragments'][0]); record['fragmentId'] = 'plan-PLAN'; record['contentRole'] = 'plan'; record['order'] = 99
        self.checklist['items'][0]['recordIds'] = ['plan-PLAN']
        write_json(self.root / '工作台/清单.json', self.checklist)
        args = dict(changes=[{'action': 'put', 'collection': 'fragments', 'record': record}],
                    requestId='plan-1', baseRevision=source['revision'], reason='方案 Z-1',
                    maintenanceReference={'checklistPath': '工作台/清单.json', 'itemIds': ['Z-1']})
        plan = preview_source_change(self.root, **args); self.commit(plan)
        self.assertEqual(preview_source_change(self.root, **args)['operationState'], 'committed')
        self.assertEqual(self.check()['summary']['submittedItemCount'], 1)
        resolved = resolve_content_target(self.root, recordId='fact-LIFE', kind='fact')
        new = deepcopy(resolved['record']); new['text'] += '后来离开。'
        plan = preview_content_change(self.root, sessionRef=resolved['sessionRef'], expectedOldRecord=resolved['record'],
            newRecord=new, reason='方案 Z-2', evidence=[{'fragmentId': 'fact-LIFE', 'quote': resolved['record']['text']}],
            approvalActor='author', approvalEvidence='明确授权',
            maintenanceReference={'checklistPath': '工作台/清单.json', 'itemIds': ['Z-2']})
        self.commit(plan)
        self.assertEqual(self.check()['summary']['submittedItemCount'], 2)

    def test_review_status_stays_consistent_across_readers(self):
        self.commit(preview_fact_patch(self.root, **self.args))
        source = load_structure(self.root); fragments = {row['fragmentId']: row for row in source['fragments']}
        rows = query_reviews(self.root)['items']
        submit_review(self.root, requestId='review-all', reviews=[
            {k: row[k] for k in ('recordId', 'inputHash', 'contentHash')} | {
                'decision': 'compatible', 'reason': '核对当前输入', 'evidence': [{'start': 0,
                    'end': len(fragments[row['recordId']]['text']), 'text': fragments[row['recordId']]['text']}]}
            for row in rows])
        for mutate in (False, True):
            if mutate:
                source = load_structure(self.root); source['fragments'][0]['text'] += '外部变动。'
                write_json(self.root / '.govel/structure.json', source)
            expected = {row['recordId']: row['status'] for row in query_reviews(self.root)['items']}
            selected = {row['id']: row['reviewStatus'] for row in select_project_content(self.root)['records'] if row['kind'] == 'fragment'}
            self.assertEqual(selected, expected)
            for item in self.check()['items']:
                for row in item['currentReviews']:
                    self.assertEqual(row['status'], expected[row['recordId']])
        self.assertEqual(expected['fact-HEIGHT'], 'stale')

    def test_cli_and_mcp_preparation_agree(self):
        from scripts.mcp_server import handle
        before = self.snapshot()
        cli = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'source', 'maintenance-prepare',
            '--project-root', str(self.root), '--source-path', '工作台/方案.md'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        mcp = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'prepare_source_maintenance',
            'arguments': {'projectRoot': str(self.root), 'sourcePath': '工作台/方案.md'}}})['result']['structuredContent']
        self.assertEqual(json.loads(cli.stdout), mcp)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
