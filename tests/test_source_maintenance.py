from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys
import tempfile
import unittest

from scripts.core.canon_guard import require_canon_proposal
from scripts.core.jsonio import read_json, write_json
from scripts.core.project import query_policy
from scripts.core.source_changes import preview_source_change
from scripts.core.source_fact_patch import preview_fact_patch
from scripts.core.source_maintenance import check_maintenance
from scripts.core.source_model import snapshot_hash
from scripts.core.source_reports import query_change_report
from scripts.core.source_review import query_reviews, submit_review
from scripts.core.source_search import search_source
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit, undo
from scripts.core.write_sets import digest
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source_fixture(self.root)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def patches(self):
        return [{'recordId': 'SF-HEIGHT', 'replacements': [{'old': '一米九', 'new': '一米八'}], 'reason': '方案第一项'},
                {'recordId': 'SF-LIFE', 'replacements': [{'old': '独自', 'new': '与同伴一起'}], 'reason': '方案第二项'}]

    def preview(self, patches=None, source_hash=None):
        return preview_fact_patch(self.root, sourceHash=source_hash or snapshot_hash(load_structure(self.root)),
                                  patches=self.patches() if patches is None else patches,
                                  approvalActor='author', approvalEvidence='作者明确要求按方案修订')

    def commit(self, plan):
        return commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def test_search_pages_exact_unicode_positions_and_keeps_annotations(self):
        source = load_structure(self.root)
        source['fragments'][0]['text'] = '🙂禁止旧称；正文仍然使用旧称。'
        write_json(self.root / '.govel/structure.json', source)
        before = self.snapshot()
        page = search_source(self.root, terms=['旧称'], contextCharacters=3, limit=1)
        self.assertEqual(page['totalMatching'], 2)
        item = page['items'][0]
        self.assertEqual(source['fragments'][0]['text'][item['start']:item['end']], '旧称')
        self.assertIn('禁止', item['context']['text'])
        second = search_source(self.root, terms=['旧称'], contextCharacters=3, limit=1, cursor=page['nextCursor'])
        self.assertGreater(second['items'][0]['start'], item['start'])
        self.assertFalse(page['coverage']['semanticJudgmentMade'])
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(StructureError):
            search_source(self.root, terms=['禁止'], contextCharacters=3, limit=1, cursor=page['nextCursor'])
        self.commit(self.preview([self.patches()[1]]))
        with self.assertRaises(StructureError) as error:
            search_source(self.root, terms=['旧称'], contextCharacters=3, limit=1, cursor=page['nextCursor'])
        self.assertEqual(error.exception.code, 'CURSOR_STALE')

    def test_search_selection_and_registry_have_real_consumers(self):
        self.assertEqual(search_source(self.root, terms=['他'], selection={'ownerIds': ['C-BETA']})['totalMatching'], 0)
        policy = deepcopy(query_policy()); policy['searchContextCharacters'] = 1
        with patch('scripts.core.source_search.query_policy', return_value=policy):
            item = search_source(self.root, terms=['身高'])['items'][0]
        self.assertEqual(item['context']['text'], '他身高一')
        for terms in ([], [''], '他'):
            with self.assertRaises(StructureError):
                search_source(self.root, terms=terms)

    def test_batch_commit_preserves_identity_metadata_and_undo(self):
        source = load_structure(self.root)
        plan = self.preview()
        self.assertEqual(load_structure(self.root), source)
        result = self.commit(plan)
        self.assertEqual(result['afterRevision'], source['revision'] + 1)
        self.assertEqual(set(result['impact']['changedIds']), {'SF-HEIGHT', 'SF-LIFE'})
        changed = load_structure(self.root)
        self.assertEqual(changed['fragments'][0]['text'], '他身高一米八。')
        self.assertEqual(changed['fragments'][3]['text'], '他与同伴一起守过城门。')
        self.assertEqual(changed['fragments'][1:3], source['fragments'][1:3])
        self.assertEqual(query_change_report(self.root, operationId=plan['operationId'], section='rationale')['totalMatching'], 2)
        undo(self.root, plan['operationId'], actor='test')
        self.assertEqual(load_structure(self.root)['fragments'], source['fragments'])

    def test_invalid_patch_is_atomic_and_original_spans_do_not_cascade(self):
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass  # Retain and compare the stable anchor as well as domain bytes.
        before = self.snapshot()
        for replacement in ([{'old': '不存在', 'new': '新文'}],
                            [{'old': '他', 'new': '别人'}, {'old': '他身高', 'new': '新的'}]):
            changes = self.patches(); changes[0]['replacements'] = replacement
            with self.assertRaises(StructureError):
                self.preview(changes)
            self.assertEqual(self.snapshot(), before)
        changes = self.patches(); changes[1]['replacements'] = [{'old': '不存在', 'new': '新文'}]
        with self.assertRaises(StructureError): self.preview(changes)
        self.assertEqual(self.snapshot(), before)
        changes = self.patches()[:1]
        changes[0]['replacements'] = [{'old': '一米九', 'new': '他'}, {'old': '他', 'new': '此人'}]
        self.commit(self.preview(changes))
        self.assertEqual(load_structure(self.root)['fragments'][0]['text'], '此人身高他。')

    def test_repeated_match_duplicate_target_and_stale_snapshot_refused(self):
        source = load_structure(self.root); source['fragments'][0]['text'] = '一米九，一米九。'
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError) as error: self.preview()
        self.assertEqual(error.exception.code, 'SOURCE_PATCH_MATCH_INVALID')
        with self.assertRaises(StructureError): self.preview([self.patches()[1]] * 2)
        source_hash = snapshot_hash(source)
        self.commit(self.preview([self.patches()[1]]))
        with self.assertRaises(StructureError) as error: self.preview(source_hash=source_hash)
        self.assertEqual(error.exception.code, 'SESSION_REF_STALE')

    def test_batch_guard_replays_evidence_and_rejects_extra_changes(self):
        plan = self.preview()
        frozen = read_json(self.root / f'.govel/plans/{plan["operationId"]}.json')
        current = load_structure(self.root)
        require_canon_proposal(current, frozen['proposedStructure'], frozen)
        for kind in ('extra', 'metadata', 'evidence', 'approval', 'order'):
            corrupt = deepcopy(frozen)
            if kind == 'extra': corrupt['proposedStructure']['entities'][0]['name'] = '非法改名'
            if kind == 'metadata': corrupt['proposedStructure']['fragments'][0]['order'] = 999
            if kind == 'evidence': corrupt['parameters']['contentProposals'][0]['evidence'] = []
            if kind == 'approval': corrupt['parameters']['contentProposals'][0]['approval']['evidence'] = ''
            if kind == 'order': corrupt['proposedStructure']['fragments'].reverse()
            with self.subTest(kind=kind), self.assertRaises(StructureError):
                require_canon_proposal(current, corrupt['proposedStructure'], corrupt)

    def test_schema5_batch_rejects_stale_commit_without_partial_writes(self):
        source = load_structure(self.root)
        source['schemaVersion'] = 5
        write_json(self.root / '.govel/structure.json', source)
        stale = self.preview()
        competing = self.preview([self.patches()[1]])
        self.commit(competing)
        before = self.snapshot()
        with self.assertRaises(StructureError): self.commit(stale)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(load_structure(self.root)['fragments'][0]['text'], '他身高一米九。')

    def test_large_source_search_and_batch_coverage(self):
        source = load_structure(self.root)
        for i in range(1154):
            source['fragments'].append({**deepcopy(source['fragments'][0]),
                'fragmentId': f'SF-SCALE-{i}', 'order': i + 10, 'text': f'第{i}处旧称。'})
        write_json(self.root / '.govel/structure.json', source)
        result = search_source(self.root, terms=['旧称'], limit=1)
        self.assertEqual(result['totalMatching'], 1154)
        patches = [{'recordId': f'SF-SCALE-{i}', 'reason': f'方案第{i}项',
                    'replacements': [{'old': '旧称', 'new': '新称'}]} for i in range(25)]
        plan = self.preview(patches)
        self.commit(plan)
        self.assertEqual(search_source(self.root, terms=['旧称'], limit=1)['totalMatching'], 1129)
        self.assertEqual(search_source(self.root, terms=['新称'], limit=1)['totalMatching'], 25)

    def test_source_change_reason_survives_commit_and_retry(self):
        source = load_structure(self.root)
        record = deepcopy(source['fragments'][0]); record.pop('fragmentId'); record['contentRole'] = 'plan'; record['order'] = 99
        args = dict(changes=[{'action': 'put', 'collection': 'fragments', 'record': record}],
                    requestId='maintenance-reason', baseRevision=source['revision'], reason='方案 §2：修订依据')
        plan = preview_source_change(self.root, **args)
        before = query_change_report(self.root, operationId=plan['operationId'], section='rationale')['items']
        result = self.commit(plan)
        self.assertEqual(result['reason'], args['reason'])
        self.assertFalse((self.root / f'.govel/plans/{plan["operationId"]}.json').exists())
        self.assertEqual(query_change_report(self.root, operationId=plan['operationId'], section='rationale')['items'], before)
        self.assertEqual(preview_source_change(self.root, **args)['reason'], args['reason'])

    def checklist(self, operation_id):
        document = '第一项：调整身高。\n第二项：调整经历。'
        directory = self.root / '工作台'; directory.mkdir(exist_ok=True)
        (directory / '方案.md').write_text(document)
        checklist = {'source': {'path': '工作台/方案.md', 'sha256': digest(document.encode())}, 'items': [
            {'itemId': 'Z-1', 'quote': '第一项：调整身高。', 'recordIds': ['SF-HEIGHT'], 'operationIds': [operation_id]},
            {'itemId': 'Z-2', 'quote': '第二项：调整经历。', 'recordIds': ['SF-LOOK'], 'operationIds': [operation_id]}]}
        write_json(directory / '核对.json', checklist)
        return checklist

    def test_checklist_reports_omission_pending_and_undo_without_writing(self):
        plan = self.preview(); self.commit(plan); self.checklist(plan['operationId'])
        before = self.snapshot()
        result = check_maintenance(self.root, checklistPath='工作台/核对.json')
        self.assertEqual(self.snapshot(), before)
        from scripts.core.source_review_triage import triage_reviews
        triage = {row['recordId']: row for row in triage_reviews(self.root)['items']}
        for item in result['items']:
            for row in item['currentReviews']:
                self.assertEqual(row['layer'], triage[row['recordId']]['layer'])
                self.assertEqual(row['operationId'], triage[row['recordId']]['operationId'])
        self.assertEqual(result['summary']['submittedItemCount'], 1)
        self.assertEqual(result['summary']['reviewReadyItemCount'], 0)
        self.assertEqual(result['items'][1]['uncoveredRecordIds'], ['SF-LOOK'])
        self.assertFalse(result['coverage']['semanticReviewComplete'])
        undo(self.root, plan['operationId'], actor='test')
        result = check_maintenance(self.root, checklistPath='工作台/核对.json')
        self.assertEqual(result['summary']['submittedItemCount'], 0)
        self.assertEqual(result['items'][0]['undoneOperationIds'], [plan['operationId']])

    def test_checklist_binds_document_and_reviews_to_current_inputs(self):
        plan = self.preview(); self.commit(plan); self.checklist(plan['operationId'])
        reviews = query_reviews(self.root, recordIds=['SF-HEIGHT', 'SF-LIFE'])
        source = load_structure(self.root)
        by_id = {f['fragmentId']: f for f in source['fragments']}
        submission = [{key: row[key] for key in ('recordId', 'inputHash', 'contentHash')} | {
            'decision': 'compatible', 'reason': '逐项核对完成', 'evidence': [{'start': 0,
                'end': len(by_id[row['recordId']]['text']), 'text': by_id[row['recordId']]['text']}]}
            for row in reviews['items']]
        submit_review(self.root, requestId='maintenance-review', reviews=submission)
        result = check_maintenance(self.root, checklistPath='工作台/核对.json')
        self.assertTrue(result['items'][0]['currentReviewsReady'])
        changes = self.patches()[:1]; changes[0]['replacements'] = [{'old': '一米八', 'new': '一米七'}]
        self.commit(self.preview(changes))
        self.assertFalse(check_maintenance(self.root, checklistPath='工作台/核对.json')['items'][0]['currentReviewsReady'])
        (self.root / '工作台/方案.md').write_text('另一份同编号方案')
        with self.assertRaises(StructureError) as error: check_maintenance(self.root, checklistPath='工作台/核对.json')
        self.assertEqual(error.exception.code, 'MAINTENANCE_SOURCE_STALE')

    def test_checklist_rejects_wrong_quotes_and_tracks_uncommitted_operations(self):
        plan = self.preview()
        checklist = self.checklist(plan['operationId'])
        row = check_maintenance(self.root, checklistPath='工作台/核对.json')['items'][0]
        self.assertFalse(row['historicallySubmitted'])
        self.assertEqual(row['unavailableOperationIds'], [plan['operationId']])
        checklist['items'][0]['quote'] = '另一份文件的同编号条目'
        write_json(self.root / '工作台/核对.json', checklist)
        with self.assertRaises(StructureError): check_maintenance(self.root, checklistPath='工作台/核对.json')

    def test_cli_json_file_and_mcp_search_share_implementation(self):
        from scripts.mcp_server import handle
        args = [sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'source']
        cli = subprocess.run(args + ['search', '--project-root', str(self.root), '--terms', '一米九'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        mcp = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'search_source_fragments', 'arguments': {'projectRoot': str(self.root), 'terms': ['一米九'], 'view': 'full'}}})
        self.assertEqual(json.loads(cli.stdout), mcp['result']['structuredContent'])
        request = self.root / '补丁.json'; request.write_text(json.dumps(self.patches(), ensure_ascii=False))
        cli = subprocess.run(args + ['fact-patch-preview', '--project-root', str(self.root),
            '--source-hash', snapshot_hash(load_structure(self.root)), '--patches', '@' + str(request),
            '--approval-actor', 'author', '--approval-evidence', '按方案处理'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertEqual(json.loads(cli.stdout)['operation'], 'canon_fact_patch')


if __name__ == '__main__':
    unittest.main()
