"""Summary deltas preserve untouched stable identities and reject concurrent replacement."""
from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from scripts.core.summary_updates import SUMMARY_PATH
from scripts.story_summarizer import _prepare_context, _commit_summary
from tests.fixtures_current import create_schema5_project


class SummaryDeltaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = create_schema5_project(self.root, chapter_count=3)
        self.prepared = _prepare_context(self.root, view='model')
        self.ids = self.prepared['coveredChapterIds']
        self.character = self.prepared['characters'][0]['characterId']
        initial = deepcopy(self.prepared['inputSkeleton'])
        initial.update(storySummary='前因推动后果。',
            plotLine=[{'chapterId': cid, 'title': f'章 {i}', 'summary': f'原推进 {i}'} for i, cid in enumerate(self.ids)],
            characterSummaries=[{'characterId': self.character, 'summary': '原状态'}])
        write_json(self.root / 'summary.json', initial)
        _commit_summary(self.root, 'summary.json')

    def delta(self, **changes):
        prepared = _prepare_context(self.root, view='model')
        return {'prepareRef': prepared['prepareRef'], 'storySummary': '经复核的完整因果。', **changes}

    def test_one_changed_entry_preserves_all_other_entries_and_history(self):
        before = read_json(self.root / SUMMARY_PATH)
        raw = (self.root / SUMMARY_PATH).read_bytes()
        result = call_operation('summary.commit-delta', self.root, self.delta(
            plotLine=[{'op': 'replace', 'chapterId': self.ids[1], 'summary': '改写推进'}]))
        after = read_json(self.root / SUMMARY_PATH)
        self.assertEqual(after['plotLine'][0], before['plotLine'][0])
        self.assertEqual(after['plotLine'][2], before['plotLine'][2])
        self.assertEqual(after['characterSummaries'], before['characterSummaries'])
        self.assertEqual(after['plotLine'][1]['summary'], '改写推进')
        self.assertEqual(after['coveredChapterIds'], self.ids)
        self.assertIn('inputSnapshot', after)
        history = call_operation('summary.read', self.root, {'view': 'history'})
        self.assertEqual(history['totalMatching'], 1)
        old = call_operation('summary.read', self.root, {'version': result['previousSummaryVersion']})
        self.assertEqual(json.loads(old['materials'][0]['text']), json.loads(raw))
        self.assertFalse(result['approved'])

    def test_optional_summary_titles_and_collections_render_in_continuation(self):
        from scripts.continuation_context import _summarize, build_continuation_context
        call_operation('summary.commit-delta', self.root, self.delta(
            plotLine=[{'op': 'replace', 'chapterId': self.ids[1], 'summary': '省略可选标题仍可展示'}]))
        context = build_continuation_context(self.root, self.ids[-1], read_only=True)
        self.assertIn('省略可选标题仍可展示', _summarize(context))
        context['storySummary'].pop('plotLine', None)
        context['storySummary'].pop('characterSummaries', None)
        self.assertIn(context['storySummary']['storySummary'], _summarize(context))

    def test_add_delete_and_duplicate_updates_are_explicit(self):
        removed = call_operation('summary.commit-delta', self.root, self.delta(
            plotLine=[{'op': 'delete', 'chapterId': self.ids[1]}],
            characterSummaries=[{'op': 'delete', 'characterId': self.character}]))
        self.assertTrue(removed['ok'])
        call_operation('summary.commit-delta', self.root, self.delta(
            plotLine=[{'op': 'add', 'chapterId': self.ids[1], 'summary': '补充推进'}],
            characterSummaries=[{'op': 'add', 'characterId': self.character, 'summary': '新状态'}]))
        for updates in ([{'op': 'add', 'chapterId': self.ids[0], 'summary': '重复新增'}],
                        [{'op': 'replace', 'chapterId': 'missing', 'summary': '无身份'}],
                        [{'op': 'delete', 'chapterId': self.ids[0]}] * 2):
            before = (self.root / SUMMARY_PATH).read_bytes()
            with self.assertRaises(StructureError):
                call_operation('summary.commit-delta', self.root, self.delta(plotLine=updates))
            self.assertEqual((self.root / SUMMARY_PATH).read_bytes(), before)

    def test_same_inputs_do_not_allow_concurrent_summary_overwrite(self):
        arguments = self.delta()
        call_operation('summary.commit-delta', self.root, arguments)
        saved = (self.root / SUMMARY_PATH).read_bytes()
        with self.assertRaises(StructureError) as caught:
            call_operation('summary.commit-delta', self.root, {**arguments, 'storySummary': '另一会话的覆盖'})
        self.assertEqual(caught.exception.code, 'SUMMARY_VERSION_CONFLICT')
        self.assertEqual((self.root / SUMMARY_PATH).read_bytes(), saved)

    def test_body_change_and_late_transaction_change_are_rejected_without_restoring_source(self):
        arguments = self.delta()
        source = read_json(self.root / '.gnovel/structure.json')
        path = self.root / source['chapters'][0]['filePath']
        path.write_text('准备后改稿')
        before = (self.root / SUMMARY_PATH).read_bytes()
        with self.assertRaises(StructureError) as caught:
            call_operation('summary.commit-delta', self.root, arguments)
        self.assertEqual(caught.exception.code, 'SUMMARY_INPUT_CHANGED')
        self.assertEqual((self.root / SUMMARY_PATH).read_bytes(), before)
        arguments = self.delta()
        def mutate(phase, relative=None):
            if phase == 'write' and relative == SUMMARY_PATH:
                path.write_text('事务期间外部更改')
        with patch('scripts.core.write_sets.checkpoint', side_effect=mutate), self.assertRaises(StructureError):
            call_operation('summary.commit-delta', self.root, arguments)
        self.assertEqual((self.root / SUMMARY_PATH).read_bytes(), before)
        self.assertEqual(path.read_text(), '事务期间外部更改')

    def test_full_submission_remains_supported_and_prepared_full_has_cas(self):
        prepared = _prepare_context(self.root, view='model')
        full = deepcopy(prepared['inputSkeleton']); full.update(storySummary='完整重述')
        write_json(self.root / 'full.json', full)
        call_operation('summary.commit-delta', self.root, self.delta())
        with self.assertRaises(StructureError):
            _commit_summary(self.root, 'full.json')
        full.pop('prepareRef')
        write_json(self.root / 'full.json', full)
        self.assertTrue(_commit_summary(self.root, 'full.json')['ok'])

    def test_invalid_current_summary_and_delta_fail_closed(self):
        target = self.root / SUMMARY_PATH
        for content in ('{broken', '{}', '[]'):
            target.write_text(content)
            prepared = _prepare_context(self.root, view='model')
            with self.assertRaises(StructureError) as caught:
                call_operation('summary.read', self.root, {})
            self.assertEqual(caught.exception.code, 'SUMMARY_INVALID')
            with self.assertRaises(StructureError) as caught:
                call_operation('summary.commit-delta', self.root, {
                    'prepareRef': prepared['prepareRef'], 'storySummary': '不能把无效旧摘要当作空摘要'})
            self.assertEqual(caught.exception.code, 'SUMMARY_INVALID')
            self.assertEqual(target.read_text(), content)
        full = deepcopy(prepared['inputSkeleton']); full['storySummary'] = '完整重审后替换损坏摘要'
        write_json(self.root / 'restored.json', full)
        self.assertTrue(_commit_summary(self.root, 'restored.json')['ok'])

    def test_read_paging_and_current_version_binding(self):
        from scripts.core.query import _size
        story = '完整因果以及明确转折。\r\n' * 1000
        call_operation('summary.commit-delta', self.root, {**self.delta(), 'storySummary': story})
        expected = read_json(self.root / SUMMARY_PATH)
        parts, cursor = [], None
        while True:
            arguments = {'maxBytes': 2000, 'limit': 1}
            if cursor:
                arguments['cursor'] = cursor
            page = call_operation('summary.read', self.root, arguments)
            self.assertLessEqual(_size(page), 2000)
            parts.extend(item['text'] for item in page['materials'])
            cursor = page.get('nextCursor')
            if not cursor:
                break
        self.assertEqual(json.loads(''.join(parts)), expected)
        first = call_operation('summary.read', self.root, {'maxBytes': 2000, 'limit': 1})
        call_operation('summary.commit-delta', self.root, self.delta())
        with self.assertRaises(StructureError) as caught:
            call_operation('summary.read', self.root, {'maxBytes': 2000, 'limit': 1, 'cursor': first['nextCursor']})
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_read_filters_are_not_silently_ignored(self):
        for arguments in ({'limit': 0}, {'limit': True}, {'maxBytes': 0},
                          {'view': 'history', 'chapterId': self.ids[0]},
                          {'view': 'history', 'version': 'a' * 64},
                          {'chapterId': self.ids[0], 'characterId': self.character}):
            with self.assertRaises(StructureError):
                call_operation('summary.read', self.root, arguments)

    def test_empty_story_and_unknown_ids_cannot_change_saved_summary(self):
        before = (self.root / SUMMARY_PATH).read_bytes()
        for changes in ({'storySummary': '   '},
                        {'plotLine': [{'op': 'add', 'chapterId': 'CH-UNKNOWN', 'summary': '不存在'}]},
                        {'characterSummaries': [{'op': 'replace', 'characterId': self.character, 'summary': ' '}]},
                        {'plotLine': [{'op': 'delete', 'chapterId': self.ids[0], 'summary': '不可混用'}]}):
            with self.assertRaises(StructureError):
                call_operation('summary.commit-delta', self.root, self.delta(**changes))
            self.assertEqual((self.root / SUMMARY_PATH).read_bytes(), before)

    def test_one_item_delta_does_not_repeat_untouched_rows(self):
        prepared = _prepare_context(self.root, view='model')
        full = deepcopy(prepared['inputSkeleton'])
        prior = read_json(self.root / SUMMARY_PATH)
        full.update(storySummary='完整因果', plotLine=prior['plotLine'], characterSummaries=prior['characterSummaries'])
        delta = {'prepareRef': prepared['prepareRef'], 'storySummary': '完整因果',
                 'plotLine': [{'op': 'replace', 'chapterId': self.ids[1], 'summary': '修改后的推进'}]}
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(encode(delta)), len(encode(full)))
        self.assertNotIn(self.ids[0], encode(delta))
        self.assertNotIn(self.character, encode(delta))

    def test_current_id_selection_and_mcp_schema_match(self):
        from scripts.mcp_server import handle
        result = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'commit_story_summary_delta', 'arguments': {'projectRoot': str(self.root), **self.delta(
                characterSummaries=[{'op': 'replace', 'characterId': self.character, 'summary': '明确关系变化'}])}}})['result']
        self.assertFalse(result['isError'], result)
        selected = call_operation('summary.read', self.root, {'characterId': self.character})
        payload = json.loads(selected['materials'][0]['text'])
        self.assertEqual(payload['characterSummaries'], [{'characterId': self.character, 'summary': '明确关系变化'}])


if __name__ == '__main__':
    unittest.main()
