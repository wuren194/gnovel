"""End-to-end regressions for prepared reading, long text and scoped repairs."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifact_pack import pack_artifact
from scripts.core.candidate_patch import patch_candidate
from scripts.core.evidence_pack import prepare_evidence, pack_evidence
from scripts.core.evidence_quotes import quote_span
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.query import _size
from scripts.core.project import query_policy
from scripts.core.reading_batch import read_batch
from scripts.core.structure_store import StructureError
from scripts.core.tool_catalog import list_capabilities
from scripts.story_summarizer import _prepare_context, _commit_summary, StorySummarizerError
from tests.fixtures_current import create_schema5_project
from tests.fixtures_workflow import review_ready_project


class PreparedReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def test_default_model_report_is_sufficient_for_read_batch(self):
        prepared = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        row = next(row for row in prepared['readList'] if row.get('readId'))
        self.assertNotIn('sha256', row)
        result = call_operation('task.read-batch', self.root, {'credential': prepared['reading']['credential'],
                                'readIds': [row['readId'], row['readId']]})
        self.assertTrue(result['ok'])
        self.assertTrue(all(item['requestIndexes'] == [0, 1] for item in result['items']))
        self.assertEqual(prepared['readingTools']['workflow.repair-context'], 'read_repair_context')

    def test_mcp_model_handoff_and_cli_use_the_same_credential(self):
        from scripts.mcp_server import handle
        response = handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'prepare_task',
            'arguments': {'projectRoot': str(self.root), 'task': 'editor', 'chapterId': self.chapter}}})['result']
        report = response['structuredContent']
        row = report['contractReads'][0]
        result = read_batch(self.root, credential=report['reading']['credential'], readIds=row['readIds'])
        self.assertEqual(result['items'][0]['path'], row['path'])
        proc = subprocess.run([sys.executable, 'scripts/novelctl.py', 'project', 'task', 'read-batch',
            '--project-root', str(self.root), '--credential', report['reading']['credential'],
            '--read-ids', *row['readIds']], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout), result)

    def test_changed_file_cannot_be_rebound_to_latest_hash(self):
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        row = next(row for row in report['readList'] if row.get('readId') and row.get('path'))
        path = self.root / row['path']; path.write_bytes(path.read_bytes() + b'\nchanged')
        with self.assertRaises(StructureError) as error:
            read_batch(self.root, credential=report['reading']['credential'], readIds=[row['readId']])
        self.assertEqual(error.exception.code, 'READ_BATCH_STALE')

    def test_credential_is_project_and_scope_bound(self):
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        token = report['reading']['credential']; ids = report['contractReads'][0]['readIds']
        with self.assertRaises(StructureError):
            read_batch(self.root, credential=token, readIds=ids, task='writer')
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other).resolve(); review_ready_project(other_root, chapter_count=1)
            with self.assertRaises((StructureError, ValueError)):
                read_batch(other_root, credential=token, readIds=ids)

    def test_on_demand_file_or_directory_entrypoint_is_bound_at_prepare(self):
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        reference = next(row for row in report['reading']['referenceReads'] if row.get('readId'))
        result = read_batch(self.root, credential=report['reading']['credential'], readIds=[reference['readId']])
        self.assertEqual(result['items'][0]['path'], reference['path'])
        self.assertFalse(any(row.get('available') is False for row in report['reading']['referenceReads']))


class LongEvidenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_evidence import ProseEvidenceTests as ProseFixture
        fixture = ProseFixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.root, self.prose = fixture.root, fixture.prose
        self.text = '甲' * 4550 + '独特引文😀\r\n' + '乙' * 4550
        self.prose.write_bytes(self.text.encode())

    def test_long_prose_pages_reassemble_every_character_and_pack_absolute_quote(self):
        pages, cursor = [], None
        while True:
            result = prepare_evidence(self.root, kind='prose', chapterId='CH-ALPHA', cursor=cursor)
            self.assertLessEqual(_size(result), query_policy()['maxBytes']['detail'])
            pages.extend(result['materials'])
            self.assertFalse(result['semanticReviewComplete'])
            if not result['hasMore']:
                self.assertTrue(result['coverage']['deliveryComplete']); break
            self.assertGreater(result['coverage']['remainingMaterials'], 0)
            cursor = result['nextCursor']
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(row['text'] for row in pages), self.text)
        self.assertEqual([(a['end'], b['start']) for a, b in zip(pages, pages[1:])],
                         [(a['end'], a['end']) for a in pages[:-1]])
        selected = next(row for row in pages if '独特引文' in row['text'])
        locator = {k: selected[k] for k in ('start', 'end', 'chunkHash')}
        report = pack_evidence(self.root, snapshot=result['snapshot'], requestId='long', decisions=[{
            'quote': {'text': '独特引文', 'page': locator}, 'targetIds': ['AT-HEIGHT'], 'kind': 'depends_on', 'reason': '核对原文'}])
        citation = report['arguments']['citations'][0]
        self.assertEqual(citation['start'], 4550)
        self.assertTrue(call_operation(report['operation'], self.root, report['arguments'])['ok'])

    def test_changed_prose_or_budget_rejects_continuation(self):
        page = prepare_evidence(self.root, kind='prose', chapterId='CH-ALPHA')
        with self.assertRaises(StructureError) as error:
            prepare_evidence(self.root, kind='prose', chapterId='CH-ALPHA', cursor=page['nextCursor'], maxBytes=8000)
        self.assertEqual(error.exception.code, 'CURSOR_QUERY_MISMATCH')
        self.prose.write_bytes((self.text + '改').encode())
        with self.assertRaises(StructureError) as error:
            prepare_evidence(self.root, kind='prose', chapterId='CH-ALPHA', cursor=page['nextCursor'])
        self.assertEqual(error.exception.code, 'CURSOR_STALE')

    def test_multi_record_reading_retains_remaining_work(self):
        source = read_json(self.root / '.govel/structure.json')
        source['fragments'][0]['text'] = '甲' * 9100
        write_json(self.root / '.govel/structure.json', source)
        first = prepare_evidence(self.root, kind='review', recordIds=['SF-HEIGHT', 'SF-LOOK'], limit=1)
        self.assertEqual(first['coverage']['remainingMaterials'], 2)
        texts = []
        page = first
        while True:
            texts.extend((row['recordId'], row['text']) for row in page['materials'])
            if not page['hasMore']: break
            page = prepare_evidence(self.root, kind='review', recordIds=['SF-HEIGHT', 'SF-LOOK'], limit=1, cursor=page['nextCursor'])
        self.assertEqual(''.join(text for rid, text in texts if rid == 'SF-HEIGHT'), '甲' * 9100)
        self.assertEqual(texts[-1], ('SF-LOOK', source['fragments'][1]['text']))

    def test_page_quotes_reject_wrong_page_and_ambiguity(self):
        page = {'start': 0, 'end': 2, 'chunkHash': 'wrong'}
        with self.assertRaises(StructureError): quote_span('同句。同句。', {'text': '同句', 'page': page})

    def test_partial_review_pack_keeps_unprocessed_record_ids(self):
        prepared = prepare_evidence(self.root, kind='review', recordIds=['SF-HEIGHT', 'SF-LOOK'])
        report = pack_evidence(self.root, snapshot=prepared['snapshot'], requestId='partial', decisions=[{
            'recordId': 'SF-HEIGHT', 'decision': 'compatible', 'reason': '复核本项', 'quotes': [{'text': '他身高一米九。'}]}])
        self.assertEqual(report['pendingRecordIds'], ['SF-LOOK']); self.assertFalse(report['selectionComplete'])
        self.assertEqual(len(report['arguments']['reviews']), 1)


class SummaryIncrementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = create_schema5_project(self.root, chapter_count=12)
        self.initial = _prepare_context(self.root)
        self.commit(self.initial)

    def commit(self, context):
        summary = {k: context[k] for k in ('projectId', 'asOfChapterId', 'structureRevision', 'coveredChapterIds', 'inputSnapshot')}
        summary.update(schemaVersion=5, storySummary='临时测试摘要', plotLine=[], characterSummaries=[])
        path = self.root / 'summary.json'; write_json(path, summary)
        return _commit_summary(self.root, str(path))

    def mutate(self, callback):
        path = self.root / '.govel/structure.json'; source = read_json(path)
        callback(source); source['revision'] += 1; write_json(path, source)

    def test_unlinked_anchor_does_not_require_twelve_chapter_reread(self):
        self.mutate(lambda source: source['entities'].append({'entityId': 'TA-UNRELATED', 'entityType': 'time_anchor',
                      'name': '未关联时间', 'timeOrdinal': 9000}))
        result = _prepare_context(self.root)
        self.assertTrue(result['upToDate']); self.assertEqual(result['chapters'], [])

    def test_old_body_change_reads_only_changed_body_with_prior_summary(self):
        source = read_json(self.root / '.govel/structure.json'); row = source['chapters'][3]
        path = self.root / row['filePath']; path.write_text(path.read_text() + '改动')
        result = _prepare_context(self.root)
        self.assertTrue(result['incremental'])
        self.assertEqual([row['chapterId'] for row in result['chapters']], [source['chapters'][3]['chapterId']])
        self.assertIn('priorSummary', result)
        self.assertEqual(result['asOfChapterId'], source['chapters'][-1]['chapterId'])

    def test_append_reads_only_new_chapter(self):
        def append(source):
            chapter = copy.deepcopy(source['chapters'][-1]); chapter.update(chapterId='CH-APPENDED', title='新章',
                displayOrdinal=13, displayCode='CH013', filePath='正文/CH-APPENDED.md')
            source['chapters'].append(chapter)
            source['chapterArcLinks'].append({'chapterId': chapter['chapterId'], 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
            (self.root / chapter['filePath']).write_text('新章节正文。')
        self.mutate(append)
        result = _prepare_context(self.root)
        self.assertTrue(result['incremental'], result['refresh'])
        self.assertEqual([row['chapterId'] for row in result['chapters']], ['CH-APPENDED'])

    def test_reorder_and_unscoped_setting_change_expand_review_keep_prior(self):
        def reorder(source):
            source['chapters'][0]['displayOrdinal'], source['chapters'][1]['displayOrdinal'] = 2, 1
        self.mutate(reorder)
        result = _prepare_context(self.root)
        self.assertFalse(result['incremental']); self.assertEqual(len(result['chapters']), 12)
        self.assertIn('priorSummary', result)

    def test_unscoped_setting_change_expands_review(self):
        self.mutate(lambda source: source['entities'].append({'entityId': 'P-UNKNOWN', 'entityType': 'prop', 'name': '未知影响道具'}))
        result = _prepare_context(self.root)
        self.assertTrue(result['refresh']['fullRefresh']); self.assertEqual(len(result['chapters']), 12)

    def test_commit_cannot_seal_changed_body_with_same_structure_revision(self):
        source = read_json(self.root / '.govel/structure.json')
        path = self.root / source['chapters'][0]['filePath']; path.write_text('准备后正文已变。')
        with self.assertRaises(StorySummarizerError): self.commit(self.initial)

    def test_new_summary_freshness_detects_old_body_change_without_revision_change(self):
        from scripts.core.summary_status import summary_freshness
        from scripts.core.context_builder import build_station_context
        source = read_json(self.root / '.govel/structure.json')
        path = self.root / source['chapters'][2]['filePath']; path.write_text(path.read_text() + '改写')
        freshness = summary_freshness(self.root, source)
        self.assertTrue(freshness['stale'])
        self.assertEqual(freshness['changedCoveredChapterIds'], [source['chapters'][2]['chapterId']])
        context = build_station_context(self.root, source['chapters'][-1]['chapterId'])
        self.assertTrue(context['summaryFreshness']['stale'])

    def test_generated_setting_view_does_not_invalidate_authored_inputs(self):
        (self.root / '设定/临时派生.md').write_text('> GENERATED: 测试派生视图\n\n仅显示布局变化。')
        self.assertTrue(_prepare_context(self.root)['upToDate'])

    def test_damaged_summary_without_dependencies_requires_conservative_refresh(self):
        path = self.root / '.govel/aux/story-summary.json'
        old = read_json(path); old.pop('inputSnapshot'); write_json(path, old)
        report = _prepare_context(self.root)
        self.assertFalse(report['incremental']); self.assertEqual(len(report['chapters']), 12)
        self.assertIn('priorSummary', report)


class ScopedSummarySourceTests(unittest.TestCase):
    def test_registered_setting_change_only_selects_its_dependent_chapter(self):
        from scripts.core.summary_inputs import summary_inputs, summary_delta
        from tests.fixtures_source import source_fixture, single_source_chapter
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source = source_fixture(root)
            ids = ['CH-ONE', 'CH-TWO']
            source['chapters'] = [single_source_chapter(source, cid, ordinal=i + 1) for i, cid in enumerate(ids)]
            source['chapterArcLinks'] = [{'chapterId': cid, 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'} for cid in ids]
            source['references'] = [{'sourceId': ids[0], 'targetId': 'SF-HEIGHT', 'kind': 'uses'}]
            (root / '正文').mkdir()
            for chapter in source['chapters']:
                (root / chapter['filePath']).write_text('临时正文。')
            before = summary_inputs(root, source)
            source['attributes'][0]['values'][0]['value']['number'] = 185
            delta = summary_delta(before, summary_inputs(root, source), ids)
            self.assertFalse(delta['fullRefresh'])
            self.assertEqual(delta['affectedChapterIds'], [ids[0]])

    def test_invalid_prior_dependency_snapshot_expands_review(self):
        from scripts.core.summary_inputs import summary_delta
        current = {'version': 1, 'chapterOrder': ['CH-ONE'], 'chapters': {}, 'records': {}, 'globalHash': 'x'}
        result = summary_delta({'version': 1}, current, [])
        self.assertTrue(result['fullRefresh']); self.assertEqual(result['affectedChapterIds'], ['CH-ONE'])


class CandidatePatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.text = '开头\r\n甲推开门。\r\n原样😀\r\n乙合上书。\r\n结尾'
        (self.root / '工作台/base.txt').write_bytes(self.text.encode())
        self.base = pack_artifact(self.root, chapterId=self.chapter, artifactType='candidate_prose', input='工作台/base.txt')

    def run_patch(self, patches):
        return patch_candidate(self.root, chapterId=self.chapter, candidateSha256=self.base['artifactSha256'], patches=patches)

    def test_batch_uses_single_baseline_preserves_unmodified_bytes_and_invalidates_evidence(self):
        rows = [{'issueId': 'one', 'original': {'text': '甲推开门。'}, 'replacement': '甲推开窗。'},
                {'issueId': 'two', 'original': {'text': '乙合上书。'}, 'replacement': '乙放下笔。'}]
        result = self.run_patch(rows)
        text = read_json(self.root / result['artifactPath'])['payload']['text']
        self.assertEqual(text, self.text.replace('甲推开门。', '甲推开窗。').replace('乙合上书。', '乙放下笔。'))
        self.assertEqual(read_json(self.root / self.base['artifactPath'])['payload']['text'], self.text)
        self.assertFalse(result['approvalPerformed']); self.assertTrue(result['mechanicalChecksRequired'])
        with self.assertRaises(StructureError): self.run_patch(rows)

    def test_overlap_ambiguity_and_wrong_anchor_leave_no_partial_writes(self):
        invalids = [[{'issueId': 'one', 'original': {'text': '甲推开门。'}, 'replacement': '新句'},
                     {'issueId': 'two', 'original': {'text': '推开门'}, 'replacement': '推窗'}],
                    [{'issueId': 'one', 'original': {'text': '。'}, 'replacement': '！'}],
                    [{'issueId': 'one', 'original': {'text': '不存在'}, 'replacement': '！'}]]
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for rows in invalids:
            with self.assertRaises(StructureError): self.run_patch(rows)
            self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_write_interruption_rolls_back_input_candidate_and_workflow(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        def fail(phase, relative=None):
            if phase == 'write' and relative.endswith('.txt'): raise RuntimeError('interrupted')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(RuntimeError):
            self.run_patch([{'issueId': 'one', 'original': {'text': '甲推开门。'}, 'replacement': '新句'}])
        from tests.fixtures_transaction import assert_preserved_rollback
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        assert_preserved_rollback(self, self.root, before, after)

    def test_model_preparation_credential_binds_patch_without_hash_assembly(self):
        report = call_operation('task.prepare', self.root, {'task': 'repair', 'chapterId': self.chapter, 'view': 'model'})
        args = {'credential': report['reading']['credential'], 'patches': [
            {'issueId': 'one', 'original': {'text': '甲推开门。'}, 'replacement': '甲打开窗。'}]}
        result = call_operation('workflow.patch-candidate', self.root, args)
        self.assertEqual(result['baseCandidateSha256'], self.base['artifactSha256'])
        self.assertIn('甲打开窗。', read_json(self.root / result['artifactPath'])['payload']['text'])
        with self.assertRaises(StructureError): call_operation('workflow.patch-candidate', self.root, args)

    def test_writer_credential_cannot_be_used_as_repair_scope(self):
        report = call_operation('task.prepare', self.root, {'task': 'writer', 'chapterId': self.chapter, 'view': 'model'})
        with self.assertRaises(StructureError):
            patch_candidate(self.root, credential=report['reading']['credential'], patches=[
                {'issueId': 'one', 'original': {'text': '甲推开门。'}, 'replacement': '甲打开窗。'}])


class ScopedDiscoveryTests(unittest.TestCase):
    def test_writer_catalog_filters_operations_and_scripts(self):
        model = list_capabilities(audience='writer', task='writer')
        audit = list_capabilities(view='audit')
        self.assertEqual(len(model['capabilities']), 3)
        self.assertFalse(model['scripts'])
        self.assertLess(len(model['operations']), len(audit['operations']))
        ids = {row['id'] for row in model['operations']}
        self.assertIn('task.read-batch', ids); self.assertNotIn('workflow.patch-candidate', ids)
        self.assertNotIn('diagnostics.aggregate', ids)

    def test_mcp_default_is_compact_audit_and_compatibility_calls_remain_available(self):
        from scripts.mcp_server import handle, TOOLS
        model = handle({'id': 1, 'method': 'tools/list'})['result']['tools']
        writer = handle({'id': 1, 'method': 'tools/list', 'params': {'audience': 'writer', 'task': 'writer'}})['result']['tools']
        audit = handle({'id': 1, 'method': 'tools/list', 'params': {'view': 'audit'}})['result']['tools']
        self.assertEqual(audit, TOOLS)
        self.assertLess(len(writer), len(model)); self.assertLess(len(model), len(audit))
        self.assertLess(_size(writer), _size(audit))
