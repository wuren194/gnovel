"""Derived history queries never mutate source ledgers or confer approval."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.candidate_versions import save_candidate_version
from scripts.core.history_index import INDEX_PATH, history_index_state, query_history, rebuild_history_index
from scripts.core.jsonio import read_json, write_json
from scripts.core.query import _size
from scripts.core.structure_store import StructureError
from tests.fixtures_workflow import review_ready_project


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.save('first', '第一版候选正文，不应出现在历史元数据中。', None)

    def save(self, request, body, base):
        return save_candidate_version(self.root, self.cid, body=body, request_id=request,
                                      base_candidate_version=base)

    def test_read_only_fallback_and_indexed_queries_match(self):
        before = tree(self.root)
        fallback = query_history(self.root, limit=100)
        self.assertFalse(fallback['index']['used'])
        self.assertEqual(tree(self.root), before)
        self.assertTrue({'candidate', 'diagnostics', 'approval'} <= {r['kind'] for r in fallback['items']})
        rebuilt = rebuild_history_index(self.root)
        self.assertGreater(rebuilt['sourceFilesRead'], 0)
        self.assertTrue(history_index_state(self.root)['current'])
        result = query_history(self.root, limit=100)
        self.assertTrue(result['index']['used'])
        self.assertEqual(result['items'], fallback['items'])
        self.assertEqual({k: v for k, v in tree(self.root).items() if k != INDEX_PATH}, before)
        self.assertNotIn('第一版候选正文', json.dumps(result, ensure_ascii=False))
        self.assertFalse(result['coverage']['approvalGranted'])
        self.assertTrue(result['coverage']['currentStatusNotEvaluated'])

    def test_rebuild_reuses_unchanged_sources_and_reads_only_modified_ledger(self):
        rebuild_history_index(self.root)
        self.assertEqual(rebuild_history_index(self.root)['sourceFilesRead'], 0)
        self.save('second', '第二版', 1)
        self.assertFalse(history_index_state(self.root)['current'])
        fallback = query_history(self.root, requestId='second')
        self.assertEqual(fallback['items'][0]['version'], 2)
        self.assertEqual(rebuild_history_index(self.root)['sourceFilesRead'], 1)
        with patch('scripts.core.history_index._extract', side_effect=AssertionError('historical source reread')):
            result = query_history(self.root, requestId='second')
        self.assertEqual(result['items'], fallback['items'])

    def test_exact_filters_are_parameterized_and_paging_is_byte_bounded(self):
        for version in range(2, 9):
            self.save(f'version-{version}', f'候选 {version}', version - 1)
        rebuild_history_index(self.root)
        expected = query_history(self.root, kind='candidate', chapterId=self.cid, limit=100)['items']
        found, cursor = [], None
        while True:
            page = query_history(self.root, kind='candidate', chapterId=self.cid,
                                 limit=2, maxBytes=2200, cursor=cursor)
            self.assertLessEqual(_size(page), 2200)
            found.extend(page['items'])
            cursor = page['nextCursor']
            if cursor is None:
                break
        self.assertEqual(found, expected)
        self.assertEqual(query_history(self.root, requestId="' OR 1=1 --")['items'], [])
        with self.assertRaises(StructureError) as caught:
            query_history(self.root, maxBytes=1)
        self.assertEqual(caught.exception.code, 'FIELD_TOO_LARGE')
        for arguments in ({'kind': 'sql'}, {'chapterId': []}, {'limit': True}, {'limit': 0}, {'maxBytes': True}):
            with self.assertRaises(StructureError):
                query_history(self.root, **arguments)

    def test_cursor_rejects_new_receipt_and_different_filters(self):
        first = query_history(self.root, limit=1)
        with self.assertRaises(StructureError) as caught:
            query_history(self.root, limit=1, kind='candidate', cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_QUERY_MISMATCH')
        self.save('second', '新版本', 1)
        with self.assertRaises(StructureError) as caught:
            query_history(self.root, limit=1, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_corrupt_deleted_and_old_schema_index_falls_back_without_writes(self):
        expected = query_history(self.root, limit=100)['items']
        target = self.root / INDEX_PATH
        for change in ('corrupt', 'schema', 'entries', 'meta', 'delete'):
            rebuild_history_index(self.root)
            if change == 'delete':
                target.unlink()
            elif change == 'corrupt':
                target.write_bytes(b'not sqlite')
            else:
                with closing(sqlite3.connect(target)) as connection:
                    if change == 'schema':
                        connection.execute("UPDATE meta SET value='0' WHERE key='schemaVersion'")
                    elif change == 'meta':
                        connection.execute("UPDATE meta SET value='broken' WHERE key='recordCount'")
                    else:
                        connection.execute('DELETE FROM entries')
                    connection.commit()
            before = tree(self.root)
            result = query_history(self.root, limit=100)
            self.assertEqual(result['items'], expected, change)
            self.assertFalse(result['index']['used'], change)
            self.assertEqual(tree(self.root), before)
            self.assertTrue(rebuild_history_index(self.root)['ok'])

    def test_same_mtime_edit_stales_index_and_removal_drops_rows(self):
        rebuild_history_index(self.root)
        ledger = self.root / f'.govel/workbench/candidates/{self.cid}/index.json'
        state = ledger.stat()
        data = read_json(ledger); data['versions'][0]['source'] = 'external'
        write_json(ledger, data)
        os.utime(ledger, ns=(state.st_atime_ns, state.st_mtime_ns))
        self.assertFalse(history_index_state(self.root)['current'])
        self.assertEqual(rebuild_history_index(self.root)['sourceFilesRead'], 1)
        ledger.unlink()
        self.assertEqual(query_history(self.root, requestId='first')['items'], [])
        rebuild_history_index(self.root)
        self.assertEqual(query_history(self.root, requestId='first')['items'], [])

    def test_invalid_source_fails_closed_and_cannot_replace_old_index(self):
        rebuild_history_index(self.root)
        target = self.root / INDEX_PATH
        original = target.read_bytes()
        ledger = self.root / f'.govel/workbench/candidates/{self.cid}/index.json'
        ledger.write_text('{broken')
        for operation in (query_history, rebuild_history_index):
            with self.assertRaises(ValueError):
                operation(self.root)
        self.assertEqual(target.read_bytes(), original)

    def test_pending_transactions_and_index_symlinks_are_rejected(self):
        rebuild_history_index(self.root)
        pending = self.root / '.govel/transactions/pending.json'
        write_json(pending, {})
        for operation in (history_index_state, query_history, rebuild_history_index):
            with self.assertRaises(ValueError):
                operation(self.root)
        pending.unlink()
        target = self.root / INDEX_PATH
        destination = self.root / 'other.sqlite3'
        target.rename(destination); target.symlink_to(destination)
        for operation in (history_index_state, query_history, rebuild_history_index):
            with self.assertRaises(ValueError):
                operation(self.root)

    def test_project_copy_cannot_reuse_another_roots_index(self):
        rebuild_history_index(self.root)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        other = Path(temp.name).resolve() / 'copy'
        shutil.copytree(self.root, other)
        self.assertFalse(history_index_state(other)['current'])
        self.assertFalse(query_history(other)['index']['used'])
        self.assertTrue(rebuild_history_index(other)['ok'])
        self.assertTrue(history_index_state(other)['current'])

    def test_new_input_during_rebuild_keeps_previous_database(self):
        from scripts.core import history_index
        rebuild_history_index(self.root)
        before = (self.root / INDEX_PATH).read_bytes()
        self.save('second', '第二版', 1)
        extract = history_index._extract
        def mutate(*args):
            rows = extract(*args)
            if args[2] == 'candidate':
                self.save('third', '第三版', 2)
            return rows
        with patch.object(history_index, '_extract', side_effect=mutate), self.assertRaises(StructureError) as caught:
            rebuild_history_index(self.root)
        self.assertEqual(caught.exception.code, 'HISTORY_INPUT_CHANGED')
        self.assertEqual((self.root / INDEX_PATH).read_bytes(), before)
        self.assertFalse(list((self.root / INDEX_PATH).parent.glob('history-*.sqlite3')))

    def test_request_receipt_and_retained_artifact_are_discoverable(self):
        relative = '工作台/next-prose.txt'
        (self.root / relative).write_text('未批准的新候选。')
        packed = pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
                               input=relative, requestId='packed')
        saved_workflow = (self.root / '.govel/workflow.json').read_bytes()
        for indexed in (False, True):
            if indexed:
                rebuild_history_index(self.root)
            result = query_history(self.root, requestId='packed')
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['items'][0]['artifactSha256'], packed['artifactSha256'])
            self.assertEqual(result['items'][0]['recordType'], 'request_receipt')
            self.assertIn('retained_artifact', {r['recordType'] for r in query_history(
                self.root, kind='candidate', limit=100)['items']})
        self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), saved_workflow)

    def test_source_review_receipt_lookup_uses_existing_request_identity(self):
        from scripts.core.source_review import query_reviews, submit_review
        from tests.fixtures_source import source_fixture
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        source = source_fixture(root)
        row = query_reviews(root, recordIds=['SF-HEIGHT'])['items'][0]
        text = source['fragments'][0]['text']
        receipt = submit_review(root, requestId='review-once', reviews=[{
            **{key: row[key] for key in ('recordId', 'inputHash', 'contentHash')},
            'decision': 'compatible', 'reason': '明确复核',
            'evidence': [{'start': 0, 'end': len(text), 'text': text}]}])
        for indexed in (False, True):
            if indexed:
                rebuild_history_index(root)
            result = query_history(root, kind='evidence', requestId='review-once', recordId='SF-HEIGHT')
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['items'][0]['receiptId'], receipt['receiptId'])
            self.assertNotIn(text, json.dumps(result, ensure_ascii=False))
    def test_committed_operations_and_workflow_backups_keep_historical_meaning(self):
        from scripts.core.structure_commands import commit_plan
        from tests.fixtures_content_proposal import attribute_value_proposal
        from tests.test_source_plan import planning_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        _, chapter = planning_source(root)
        plan = attribute_value_proposal(root, 'AT-HEIGHT', 185)
        commit_plan(root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        for indexed in (False, True):
            if indexed:
                rebuild_history_index(root)
            rows = query_history(root, kind='operation', recordId='AT-HEIGHT')['items']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['id'], plan['operationId'])
            self.assertTrue(rows[0]['historicalSnapshot'])
        backup = self.root / '.govel/backups/OP-1234567890ABCDEF/.govel/workflow.json'
        write_json(backup, read_json(self.root / '.govel/workflow.json'))
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['approvals'][self.cid]['approved'] = False
        write_json(self.root / '.govel/workflow.json', workflow)
        rebuild_history_index(self.root)
        rows = query_history(self.root, kind='approval', chapterId=self.cid, limit=100)['items']
        self.assertTrue(any(row['recordedApproved'] and '/backups/' in row['path'] for row in rows))
        self.assertTrue(any(not row['recordedApproved'] and row['path'] == '.govel/workflow.json' for row in rows))

    def test_prose_evidence_uses_ledger_without_scanning_current_manuscript(self):
        from scripts.core.source_evidence import register_evidence
        from scripts.core.source_model import snapshot_hash
        from scripts.core.structure_store import validate_structure
        from scripts.core.workflow import default_workflow
        from scripts.core.write_sets import digest
        from tests.fixtures_source import source_fixture, single_source_chapter
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve(); source = source_fixture()
        chapter = single_source_chapter(source, 'CH-ALPHA', status='stable',
                                        volume_id=source['volumes'][0]['volumeId'])
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': 'CH-ALPHA', 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        validate_structure(source)
        write_json(root / '.govel/structure.json', source)
        write_json(root / '.govel/workflow.json', default_workflow())
        manuscript = root / chapter['filePath']; manuscript.parent.mkdir()
        text = '他一米九的身形挡住了城门。'; manuscript.write_text(text)
        receipt = register_evidence(root, chapterId='CH-ALPHA', fileHash=digest(manuscript.read_bytes()),
            sourceHash=snapshot_hash(source), requestId='prose-once', citations=[{
                'start': 0, 'end': len(text), 'text': text, 'targetIds': ['AT-HEIGHT'],
                'kind': 'depends_on', 'reason': '明确引证'}])
        manuscript.write_text('后来改稿，旧引证不能充当当前证据。')
        for indexed in (False, True):
            if indexed:
                rebuild_history_index(root)
            result = query_history(root, kind='evidence', requestId='prose-once', chapterId='CH-ALPHA')
            self.assertEqual(result['items'][0]['receiptId'], receipt['batchId'])
            self.assertTrue(result['coverage']['currentStatusNotEvaluated'])
            self.assertNotIn(text, json.dumps(result, ensure_ascii=False))
            self.assertNotIn('后来改稿', json.dumps(result, ensure_ascii=False))
    def test_wal_writer_cannot_hide_changed_index_behind_unchanged_main_file(self):
        rebuild_history_index(self.root)
        target = self.root / INDEX_PATH
        expected = query_history(self.root, limit=100)['items']
        with closing(sqlite3.connect(target)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('DELETE FROM entries')
            connection.commit()
            before = tree(self.root)
            result = query_history(self.root, limit=100)
            self.assertEqual(result['items'], expected)
            self.assertFalse(result['index']['used'])
            self.assertEqual(result['index']['reason'], 'busy')
            self.assertEqual(tree(self.root), before)
            with self.assertRaises(StructureError) as caught:
                rebuild_history_index(self.root)
            self.assertEqual(caught.exception.code, 'HISTORY_INDEX_BUSY')
        rebuild_history_index(self.root)
        self.assertTrue(history_index_state(self.root)['current'])

    def test_parent_request_filter_finds_existing_split_batch_receipts(self):
        from scripts.core.operations import call_operation
        from tests.test_ai_efficiency_evidence_refs import EvidenceReferenceTests
        fixture = EvidenceReferenceTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        packed = fixture.packed(count=4, request='parent-history', maxBytes=800)
        self.assertGreater(packed['batchCount'], 1)
        done = call_operation('evidence.submit', fixture.root, {'submissionRef': packed['submissionRef']})
        self.assertEqual(done['recordedCount'], 4)
        for indexed in (False, True):
            if indexed:
                rebuild_history_index(fixture.root)
            result = query_history(fixture.root, kind='evidence', requestId='parent-history')
            self.assertEqual(result['totalMatching'], 4)
            self.assertTrue(all('parent-history' in row['requestIds'] for row in result['items']))


if __name__ == '__main__':
    unittest.main()
