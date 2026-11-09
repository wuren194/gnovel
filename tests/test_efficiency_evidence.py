from scripts.core.chapter_receipt import chapter_receipt
"""Real temporary-project checks for freshness, replay and bound evidence reads."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import sha256_text
from scripts.core.evidence_pack import prepare_evidence, pack_evidence
from scripts.core.evidence_quotes import quote_span
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import run_diagnostics, evidence_current
from scripts.core.operations import call_operation
from scripts.core.reading_batch import read_batch
from scripts.core.repair_context import repair_context
from scripts.core.structure_store import StructureError
from scripts.core.task_prepare import prepare_task
from tests.fixtures_workflow import review_ready_project
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


class WorkflowEfficiencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def prose(self, text, request=None):
        (self.root / '工作台/prose.txt').write_text(text, encoding='utf-8')
        return pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose',
                             input='工作台/prose.txt', requestId=request)

    def test_request_replay_preserves_approval_and_rejects_changed_content_or_dependencies(self):
        first = self.prose('第一版正文。', 'one')
        call_operation('project.workflow.artifact-approve', self.root, {'chapterId': self.chapter,
                       'artifact': ['@' + str(self.root / first['artifactPath'])]})
        before = (self.root / '.govel/workflow.json').read_bytes()
        with patch('scripts.core.artifacts.utc_now', return_value='2099-01-01T00:00:00Z'):
            replay = self.prose('第一版正文。', 'one')
        self.assertEqual(first['artifactSha256'], replay['artifactSha256'])
        self.assertTrue(replay['replayed']); self.assertTrue(replay['approved'])
        self.assertEqual(before, (self.root / '.govel/workflow.json').read_bytes())
        with self.assertRaises(StructureError) as error:
            self.prose('另一稿。', 'one')
        self.assertEqual(error.exception.code, 'ARTIFACT_REQUEST_REUSED')
        self.prose('第一版正文。')
        source = read_json(self.root / '.govel/structure.json'); source['revision'] += 1
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError) as error:
            self.prose('第一版正文。', 'one')
        self.assertEqual(error.exception.code, 'ARTIFACT_REQUEST_STALE')

    def test_failed_report_is_fresh_reused_and_new_run_keeps_baseline(self):
        self.prose('他' + '推开沉重的门' * 25 + '。')
        first = run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        self.assertFalse(first['evidence']['passed'])
        status = evidence_current(self.root, self.chapter)
        self.assertTrue(status['fresh']); self.assertFalse(status['passed']); self.assertFalse(status['current'])
        before = (self.root / first['artifactPath']).read_bytes()
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must reuse')):
            repeat = run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        self.assertTrue(repeat['reused'])
        repair = repair_context(self.root, chapterId=self.chapter)
        self.assertTrue(any(row['location']['spans'] for row in repair['items']))
        self.assertEqual(repair['draftSha256'], sha256_text('他' + '推开沉重的门' * 25 + '。'))
        self.prose('他把钥匙放回桌上。')
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['artifacts'][self.chapter]['diagnostics']['invalidatedBy'] = {'reason': 'candidate activation'}
        write_json(self.root / '.govel/workflow.json', workflow)
        self.assertFalse(evidence_current(self.root, self.chapter)['fresh'])
        second = run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        self.assertFalse(second['reused']); self.assertIsNotNone(second['diff'])
        self.assertEqual(before, (self.root / first['artifactPath']).read_bytes())
        self.assertIsNotNone(repair_context(self.root, chapterId=self.chapter)['diff'])

    def test_explicit_changed_parameters_run_again_through_executor(self):
        run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        result = call_operation('task.execute', self.root, {'task': 'editor', 'chapterId': self.chapter,
                                'authorization': 'candidate', 'diagnostics': {'mode': 'plot', 'intensityLevel': 1, 'sceneParagraphs': '1', 'register': True}})
        row = next(row for row in result['executed'] if row['operation'] == 'diagnostics.aggregate')
        self.assertTrue(row['ok'], row); self.assertFalse(row['result']['reused'])

    def test_incomplete_or_crashed_check_cannot_be_registered(self):
        self.prose('管理员换了新的登记本。')
        for result in (subprocess.CompletedProcess([], 3, '', 'crash'),
                       subprocess.CompletedProcess([], 0, json.dumps({'passed': False}), '')):
            before = (self.root / '.govel/workflow.json').read_bytes()
            with patch('scripts.core.mechanical_evidence.subprocess.run', return_value=result):
                with self.assertRaises(StructureError):
                    run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
            self.assertEqual(before, (self.root / '.govel/workflow.json').read_bytes())

    def test_selected_ranges_merge_and_keep_every_origin(self):
        prepared = prepare_task(self.root, 'editor', chapter_id=self.chapter)
        row = prepared['promptPlan']['contracts'][0]
        scope = row['readRanges'][0]
        request = {'root': 'toolbox', 'path': row['path'], 'sha256': row['sha256'],
                   'startLine': scope['startLine'], 'endLine': scope['endLine']}
        result = read_batch(self.root, task='editor', chapterId=self.chapter, requests=[request, request])
        self.assertEqual(len(result['mappings']), 2)
        self.assertTrue(all(item['requestIndexes'] == [0, 1] for item in result['items']))
        self.assertEqual(''.join(item['text'] for item in result['items']),
                         ''.join((ROOT/row['path']).read_text().splitlines(keepends=True)[scope['startLine']-1:scope['endLine']]))
        with self.assertRaises(StructureError):
            read_batch(self.root, task='editor', chapterId=self.chapter,
                       requests=[{**request, 'sha256': '0'*64}])

    def test_continuity_hashes_assemble_then_existing_approval_gate_applies(self):
        from scripts.core.finalization import finalize_chapter
        from scripts.core.continuity import complete_continuity, ContinuityError
        finalize_chapter(self.root, self.chapter, skip_title=True,
                         binding_generation=chapter_receipt(self.root, self.chapter)['bindingGeneration'])
        prepared = call_operation('summary.prepare', self.root, {})
        summary = call_operation('summary.commit-delta', self.root, {'prepareRef': prepared['prepareRef'],
            'storySummary': '值班员归还钥匙，登记交班结果。',
            'plotLine': [{'op': 'add', 'chapterId': self.chapter, 'summary': '交班完成。'}]})
        snapshot = prepare_evidence(self.root, kind='continuity', chapterId=self.chapter,
                                    paths=[summary['path']])['snapshot']
        decision = {'decisions': [], 'summary': {'resolution': 'refreshed', 'reason': '核对本章正文后记录交班'}}
        result = pack_evidence(self.root, snapshot=snapshot, decisions=decision)
        confirmation = result['arguments']['confirmation']
        self.assertEqual(confirmation['summary']['sha256'], snapshot['documents'][0]['sha256'])
        with self.assertRaises(ContinuityError):
            complete_continuity(self.root, self.chapter, batch_hash=snapshot['batchHash'], confirmation=confirmation, approved=False)
        document = self.root/summary['path']; document.write_text(document.read_text()+'\n')
        with self.assertRaises(StructureError): pack_evidence(self.root, snapshot=snapshot, decisions=decision)


    def test_unchanged_failed_report_is_not_rerun_by_group_executor(self):
        self.prose('他' + '推开沉重的门' * 25 + '。')
        run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        with patch('scripts.core.mechanical_evidence.subprocess.run', side_effect=AssertionError('must not rerun')):
            result = call_operation('task.execute', self.root, {'task': 'editor', 'chapterId': self.chapter,
                                    'authorization': 'candidate'})
        self.assertFalse(any(row['operation'] == 'diagnostics.aggregate' for row in result['executed']))
        self.assertFalse(result['approvalsPerformed'])
        self.assertTrue(evidence_current(self.root, self.chapter)['fresh'])

    def test_current_report_has_complete_reusable_inputs(self):
        status = evidence_current(self.root, self.chapter)
        self.assertNotIn('legacy', status); self.assertTrue(status['fresh']); self.assertTrue(status['passed'])

    def test_request_receipt_rolls_back_with_artifact_and_workflow(self):
        (self.root / '工作台/prose.txt').write_text('待包装正文。')
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        def fail(phase, relative=None):
            if phase == 'write' and relative.startswith('.govel/artifact-requests/'):
                raise RuntimeError('interrupted receipt')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(RuntimeError):
            pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose',
                          input='工作台/prose.txt', requestId='atomic')
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        from tests.fixtures_transaction import assert_preserved_rollback
        assert_preserved_rollback(self, self.root, before, after)

    def test_read_batch_cursor_budget_and_scope_cannot_be_reused_after_changes(self):
        prepared = prepare_task(self.root, 'editor', chapter_id=self.chapter)
        contract = max(prepared['promptPlan']['contracts'], key=lambda row:
                       row['readRanges'][0]['endLine'] - row['readRanges'][0]['startLine'])
        request = {'root': 'toolbox', 'path': contract['path'], 'sha256': contract['sha256'],
                   'startLine': contract['readRanges'][0]['startLine'], 'endLine': contract['readRanges'][0]['endLine']}
        page = read_batch(self.root, task='editor', chapterId=self.chapter, requests=[request], limit=1, maxBytes=4000)
        self.assertTrue(page['hasMore']); self.assertEqual(page['returned'], 1)
        next_page = read_batch(self.root, task='editor', chapterId=self.chapter, requests=[request],
                               limit=1, maxBytes=4000, cursor=page['nextCursor'])
        self.assertEqual(page['items'][0]['id'], next_page['items'][0]['id'])
        self.assertEqual(page['items'][0]['end'], next_page['items'][0]['start'])
        with self.assertRaises(StructureError):
            read_batch(self.root, task='editor', chapterId=self.chapter, requests=[request],
                       limit=2, cursor=page['nextCursor'])
        with self.assertRaises(StructureError):
            read_batch(self.root, task='editor', chapterId=self.chapter, requests=[request], maxBytes=1)
        source = read_json(self.root / '.govel/structure.json'); source['revision'] += 1
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError):
            read_batch(self.root, task='editor', chapterId=self.chapter, requests=[request],
                       limit=1, cursor=page['nextCursor'])

    def test_operation_report_view_uses_existing_body_resolver(self):
        from scripts.core.review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
        text = '他把钥匙放回桌上，合上了登记本。'
        payload = {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': {
                   slot: {'conclusion': '已核对动作', 'evidence': [{'text': text}]} for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
                   'findings': [{'severity': 'S3', 'ruleId': 'R-TEST', 'summary': '核对动作', 'location': text}]}
        write_json(self.root / '工作台/review.json', payload)
        pack_artifact(self.root, chapterId=self.chapter, artifactType='stage_b_review', input='工作台/review.json')
        result = call_operation('review.report-view', self.root, {'chapterId': self.chapter})
        self.assertEqual(result['localizedAgainst']['kind'], 'candidate_prose')
        self.assertEqual(result['localization'][0]['anchor']['state'], 'unique')

    def test_crashed_line_detail_evidence_is_never_complete(self):
        from scripts.core.mechanical_evidence import complete_evidence
        report = run_diagnostics(self.root, chapter_id=self.chapter, mode='plot', intensity_level=1, register=True)
        payload = copy.deepcopy(report['evidence'])
        payload['lineDetails']['__returncode'] = 3
        self.assertFalse(complete_evidence(payload))
        payload = copy.deepcopy(report['evidence']); payload['checksPresent'] = None
        self.assertFalse(complete_evidence(payload))



class SourceEvidenceAssemblyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source = source_fixture(self.root)

    def test_review_snapshot_contains_read_text_and_payload_submits_without_transcription(self):
        prepared = prepare_evidence(self.root, kind='review', recordIds=['SF-HEIGHT'])
        snapshot = prepared['snapshot']
        self.assertNotIn('text', snapshot['items'][0])
        decisions = [{'recordId': 'SF-HEIGHT', 'decision': 'compatible', 'reason': '核对原文',
                      'quotes': [{'text': prepared['materials'][0]['text']}]}]
        result = pack_evidence(self.root, snapshot=snapshot, decisions=decisions, requestId='review')
        self.assertFalse(result['submitted']); self.assertFalse(result['approved'])
        self.assertTrue(call_operation(result['operation'], self.root, result['arguments'])['ok'])
        source = read_json(self.root / '.govel/structure.json'); source['revision'] += 1
        write_json(self.root / '.govel/structure.json', source)
        with self.assertRaises(StructureError):
            pack_evidence(self.root, snapshot=snapshot, decisions=decisions, requestId='review')

    def test_prose_snapshot_preserves_crlf_and_assembled_citation_is_accepted(self):
        from tests.test_source_evidence import ProseEvidenceTests
        fixture = ProseEvidenceTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        prepared = prepare_evidence(fixture.root, kind='prose', chapterId='CH-ALPHA')
        snapshot = prepared['snapshot']
        self.assertNotIn('text', snapshot)
        self.assertIn('\r\n', prepared['materials'][0]['text'])
        decisions = [{'quote': {'text': prepared['materials'][0]['text']}, 'targetIds': ['AT-HEIGHT'],
                      'kind': 'depends_on', 'reason': '明确引用身高'}]
        result = pack_evidence(fixture.root, snapshot=snapshot, decisions=decisions, requestId='cite')
        self.assertTrue(call_operation(result['operation'], fixture.root, result['arguments'])['ok'])
        fixture.prose.write_text('全新正文')
        with self.assertRaises(StructureError):
            pack_evidence(fixture.root, snapshot=snapshot, decisions=decisions, requestId='cite')

    def test_ambiguous_missing_and_wrong_explicit_quote_are_rejected(self):
        for quote in ({'text': '同句'}, {'text': '没有'}, {'text': '同句', 'start': 1, 'end': 3}):
            with self.assertRaises(StructureError): quote_span('同句。同句。', quote)
        self.assertEqual(quote_span('同句。同句。', {'text': '同句', 'start': 3, 'end': 5})['start'], 3)

    def test_cli_and_mcp_expose_the_same_new_read_operation(self):
        args = {'kind': 'review', 'recordIds': ['SF-HEIGHT']}
        expected = call_operation('evidence.prepare', self.root, args)
        self.assertIn('snapshotRef', expected)
        cli = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'evidence', 'prepare',
                              '--project-root', str(self.root), '--kind', 'review', '--record-ids', 'SF-HEIGHT'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        self.assertEqual(json.loads(cli.stdout), expected)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                   'name': 'prepare_task_evidence', 'arguments': {**args, 'projectRoot': str(self.root)}}}
        mcp = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')],
                              input=json.dumps(request)+'\n', capture_output=True, text=True)
        self.assertEqual(json.loads(json.loads(mcp.stdout)['result']['content'][0]['text']), expected)
