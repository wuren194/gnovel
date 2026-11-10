"""Split/merge conservation, current ownership, transactional failure and both facades."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core.chapter_reshape import (commit_reshape, preview_merge, preview_split, query_lineage,
                                         read_reshape, validate_reshape_plan)
from scripts.core.chapter_reshape_operations import chapter_reshape_operations
from scripts.core.jsonio import read_json, write_json
from scripts.core.prose_binding import OWNED_CLEAN, observe_chapter, seal_binding
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.write_sets import digest
from tests.fixtures_refactor import refactor_project


class ChapterReshapeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / '书'
        self.info = refactor_project(self.root)
        self.first, self.second = self.info['chapterOne'], self.info['chapterTwo']
        self.initial = load_structure(self.root)
        self.originals = {row['filePath']: (self.root / row['filePath']).read_bytes()
                          for row in self.initial['chapters']}

    def split(self, **changes):
        return preview_split(self.root, **{'requestId': 'split-1', 'chapterId': self.first,
            'cutOffsets': [7], 'titles': ['河口', '钥匙'], 'reason': '作者在场景转折处分章', **changes})

    def merge(self, **changes):
        return preview_merge(self.root, **{'requestId': 'merge-1', 'chapterIds': [self.first, self.second],
            'title': '归来', 'reason': '作者将连续场景合并', **changes})

    def commit(self, preview):
        return commit_reshape(self.root, operationId=preview['operationId'], planHash=preview['planHash'], approve=True)

    def plan(self, preview):
        return read_json(self.root / '.govel/plans' / f"{preview['operationId']}.json")

    def outputs(self, preview):
        if 'chapterReshape' in preview['impact']:
            return preview['impact']['chapterReshape']
        from scripts.core.source_reports import query_change_report
        rows, cursor = [], None
        while True:
            result = query_change_report(self.root, operationId=preview['operationId'], section='impact', cursor=cursor)
            rows.extend({key: value for key, value in row.items() if key != 'category'}
                        for row in result['items'] if row['category'] == 'chapterReshape')
            if not result['hasMore']:
                return rows
            cursor = result['nextCursor']

    def assert_originals(self):
        for path, payload in self.originals.items():
            self.assertEqual((self.root / path).read_bytes(), payload, path)

    def test_split_preserves_all_bytes_and_owns_new_prose_without_old_plan_or_approval(self):
        preview = self.split()
        self.assertEqual(load_structure(self.root), self.initial)
        self.assertTrue(preview['requiresApproval'])
        self.assertEqual(preview['nextAction']['operation'], 'chapter.reshape-commit')
        rows = self.outputs(preview)
        self.assertEqual(len(rows), 2)
        self.assertFalse(any((self.root / row['filePath']).exists() for row in rows))
        self.commit(preview)
        source = load_structure(self.root)
        self.assertEqual(source['revision'], self.initial['revision'] + 1)
        self.assertEqual(b''.join((self.root / row['filePath']).read_bytes() for row in rows),
                         self.originals[f'正文/{self.first}.md'])
        self.assert_originals()
        by_id = {row['chapterId']: row for row in source['chapters']}
        self.assertEqual(by_id[self.first]['status'], 'archived')
        for row in rows:
            chapter = by_id[row['chapterId']]
            self.assertEqual(chapter['status'], 'planning_required')
            self.assertEqual(observe_chapter(self.root, source, chapter)['state'], OWNED_CLEAN)
            self.assertEqual(chapter['proseBinding']['generation'], source['revision'])
            self.assertFalse(any(fragment['ownerId'] == chapter['chapterId'] for fragment in source['fragments']))
            self.assertIsNone(chapter['storyTimeStartId'])
        self.assertEqual([m['memberId'] for m in source['units'][0]['members']],
                         [rows[0]['chapterId'], rows[1]['chapterId'], self.second])
        self.assertEqual([row['chapterId'] for row in source['chapters'] if row['status'] != 'archived'][:2],
                         [row['chapterId'] for row in rows])

    def test_merge_uses_explicit_order_and_retains_archived_source_identities(self):
        preview = self.merge(chapterIds=[self.second, self.first], separator='\n--\n')
        self.commit(preview)
        row = self.outputs(preview)[0]
        expected = self.originals[f'正文/{self.second}.md'] + b'\n--\n' + self.originals[f'正文/{self.first}.md']
        self.assertEqual((self.root / row['filePath']).read_bytes(), expected)
        source = load_structure(self.root)
        self.assertTrue(all(next(c for c in source['chapters'] if c['chapterId'] == cid)['status'] == 'archived'
                            for cid in (self.first, self.second)))
        self.assert_originals()
        self.assertEqual(preview['impact']['reshapeSummary']['addedSeparatorCharacters'], 4)
        self.assertEqual(preview['impact']['reshapeSummary']['removedCharacters'], 0)
        self.assertEqual(preview['impact']['reshapeSummary']['target']['primaryArcId'], self.info['arcMain'])

    def test_default_merge_inserts_nothing_and_explicit_target_detaches_unit(self):
        preview = self.merge(target={'unitId': None})
        self.commit(preview)
        row = self.outputs(preview)[0]
        self.assertEqual((self.root / row['filePath']).read_bytes(),
                         self.originals[f'正文/{self.first}.md'] + self.originals[f'正文/{self.second}.md'])
        self.assertEqual(load_structure(self.root)['units'][0]['members'], [])

    def test_unicode_offsets_preserve_crlf_and_supplementary_characters(self):
        body = '甲😀\r\n乙。\n丙'
        structure = deepcopy(self.initial)
        chapter = next(row for row in structure['chapters'] if row['chapterId'] == self.first)
        (self.root / chapter['filePath']).write_bytes(body.encode('utf-8'))
        chapter['proseBinding'] = seal_binding(structure['projectId'], self.first, chapter['filePath'],
            state='managed', generation=structure['revision'], expected_content_digest='sha256:' + digest(body.encode('utf-8')))
        write_json(self.root / '.govel/structure.json', structure)
        preview = self.split(cutOffsets=[4])
        anchored = self.split(requestId='unicode-anchor', cutOffsets=None, cutBefore=['乙。\n'])
        self.assertEqual([row['contentDigest'] for row in self.outputs(anchored)],
                         [row['contentDigest'] for row in self.outputs(preview)])
        self.assertEqual(self.outputs(anchored)[1]['sources'][0]['start'], 4)
        first, second = self.outputs(preview)
        text = read_reshape(self.root, operationId=preview['operationId'], planHash=preview['planHash'], chapterId=first['chapterId'])
        self.assertEqual(''.join(item['text'] for item in text['items']), '甲😀\r\n')
        self.commit(preview)
        self.assertEqual((self.root / first['filePath']).read_bytes(), '甲😀\r\n'.encode('utf-8'))
        self.assertEqual((self.root / second['filePath']).read_bytes(), '乙。\n丙'.encode('utf-8'))

    def test_exact_source_fragments_split_without_caller_supplied_offsets(self):
        preview = self.split(cutOffsets=None, cutBefore=['艾琳握着', '旧钥匙'], titles=None)
        arguments = self.plan(preview)['parameters']['reshapeArguments']
        self.assertEqual(arguments['cutBefore'], ['艾琳握着', '旧钥匙'])
        self.assertNotIn('cutOffsets', arguments)
        self.assertTrue(validate_reshape_plan(self.root, self.initial, self.plan(preview)))
        self.commit(preview)
        self.assertEqual([(self.root / row['filePath']).read_bytes() for row in self.outputs(preview)],
                         [text.encode('utf-8') for text in ('白塔立在河口。', '艾琳握着', '旧钥匙。\n')])
        self.assert_originals()

    def test_source_fragment_boundaries_reject_ambiguity_missing_empty_start_and_wrong_order(self):
        cases = [([], 'CHAPTER_SPLIT_ANCHOR_INVALID'), ([''], 'CHAPTER_SPLIT_ANCHOR_INVALID'),
                 ([True], 'CHAPTER_SPLIT_ANCHOR_INVALID'), (['没有这段原文'], 'CHAPTER_SPLIT_ANCHOR_NOT_FOUND'),
                 (['。'], 'CHAPTER_SPLIT_ANCHOR_AMBIGUOUS'), (['白塔'], 'CHAPTER_SPLIT_ANCHOR_ORDER_INVALID'),
                 (['旧钥匙', '艾琳'], 'CHAPTER_SPLIT_ANCHOR_ORDER_INVALID'),
                 (['艾琳', '艾琳'], 'CHAPTER_SPLIT_ANCHOR_ORDER_INVALID')]
        for index, (anchors, code) in enumerate(cases):
            with self.subTest(anchors=anchors), self.assertRaises(StructureError) as error:
                self.split(requestId=f'bad-anchor-{index}', cutOffsets=None, cutBefore=anchors, titles=None)
            self.assertEqual(error.exception.code, code)
        for arguments in ({'cutOffsets': None}, {'cutBefore': ['艾琳']}):
            with self.subTest(arguments=arguments), self.assertRaises(StructureError) as error:
                self.split(requestId='select-one-' + str(arguments), **arguments)
            self.assertEqual(error.exception.code, 'CHAPTER_SPLIT_BOUNDARY_REQUIRED')
        self.assertEqual(load_structure(self.root), self.initial)
        self.assert_originals()

    def test_review_text_pages_reassemble_frozen_result(self):
        preview = self.split()
        row = self.outputs(preview)[1]
        response = read_reshape(self.root, operationId=preview['operationId'], planHash=preview['planHash'], chapterId=row['chapterId'])
        self.assertTrue(response['historical'])
        self.assertFalse(response['coverage']['semanticReviewComplete'])
        self.assertEqual(''.join(item['text'] for item in response['items']), self.info['bodies'][self.first][7:])
        self.commit(preview)
        self.assertTrue(read_reshape(self.root, operationId=preview['operationId'], planHash=preview['planHash'],
                                   chapterId=row['chapterId'])['committed'])

    def test_request_retries_reuse_preview_and_committed_result(self):
        preview = self.split()
        self.assertEqual(self.split()['planHash'], preview['planHash'])
        first = self.commit(preview)
        revision = load_structure(self.root)['revision']
        again = self.commit(preview)
        self.assertEqual(again['operationId'], first['operationId'])
        self.assertEqual(load_structure(self.root)['revision'], revision)
        historical = self.split()
        self.assertTrue(historical['historical'])
        with self.assertRaises(StructureError) as error:
            self.split(titles=['不同标题', '第二部分'])
        self.assertEqual(error.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_invalid_offsets_and_source_selection_fail_without_structure_change(self):
        for cuts in ([], [0], [9999], [7, 7], [8, 3], [True]):
            with self.subTest(cuts=cuts), self.assertRaises(StructureError):
                self.split(requestId='invalid-' + str(cuts), cutOffsets=cuts)
        for ids in ([self.first], [self.first, self.first], ['CH-MISSING', self.second]):
            with self.subTest(ids=ids), self.assertRaises(StructureError):
                self.merge(requestId='invalid-' + str(ids), chapterIds=ids)
        self.assertEqual(load_structure(self.root), self.initial)
        self.assert_originals()

    def test_no_approval_refuses_and_unbound_or_external_source_refuses(self):
        preview = self.split()
        with self.assertRaises(StructureError):
            commit_reshape(self.root, operationId=preview['operationId'], planHash=preview['planHash'], approve=False)
        (self.root / f'正文/{self.first}.md').write_text('作者新改的内容。', encoding='utf-8')
        with self.assertRaises(StructureError) as error:
            self.split(requestId='different-source')
        self.assertEqual(error.exception.code, 'PROSE_EXTERNAL_MODIFICATION')
        self.assertEqual(load_structure(self.root), self.initial)

    def test_source_replacement_with_same_bytes_rejects_stale_commit(self):
        preview = self.split()
        path = self.root / f'正文/{self.first}.md'
        payload = path.read_bytes()
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(payload)
        replacement.replace(path)
        with self.assertRaises(StructureError) as error:
            self.commit(preview)
        self.assertIn(error.exception.code, {'CHAPTER_RESHAPE_SOURCE_CHANGED', 'TRANSACTION_GUARD_DRIFT'})
        self.assertEqual(load_structure(self.root), self.initial)
        self.assert_originals()

    def test_request_status_and_retry_report_changed_source_as_stale(self):
        from scripts.core.source_requests import query_request_status
        self.split()
        path = self.root / f'正文/{self.first}.md'
        path.write_text('作者后来修改了这一章。', encoding='utf-8')
        status = query_request_status(self.root, requestId='split-1')
        self.assertEqual(status['operationState'], 'stale')
        self.assertIn('TRANSACTION_GUARD_DRIFT', status['staleReasons'])
        self.assertFalse(status['renewable'])
        with self.assertRaises(StructureError) as error:
            self.split()
        self.assertEqual(error.exception.code, 'SOURCE_REQUEST_STALE')
        self.assertEqual(load_structure(self.root), self.initial)

    def test_occupied_output_and_changed_frozen_material_refuse_without_overwrite(self):
        preview = self.split()
        row = self.outputs(preview)[0]
        target = self.root / row['filePath']
        target.write_bytes(b'outside work')
        with self.assertRaises(StructureError):
            self.commit(preview)
        self.assertEqual(target.read_bytes(), b'outside work')
        self.assertEqual(load_structure(self.root), self.initial)
        target.unlink()
        frozen = self.root / self.plan(preview)['manuscriptMaterialization'][0]['sourcePath']
        frozen.write_bytes(b'changed frozen result')
        with self.assertRaises(StructureError):
            self.commit(preview)
        self.assertFalse(target.exists())
        self.assertEqual(load_structure(self.root), self.initial)

    def test_validator_rejects_out_of_scope_data_and_forged_lineage(self):
        preview = self.split()
        plan = self.plan(preview)
        plan['proposedStructure']['bookName'] = '夹带改名'
        with self.assertRaises(StructureError) as error:
            validate_reshape_plan(self.root, self.initial, plan)
        self.assertEqual(error.exception.code, 'CHAPTER_RESHAPE_SCOPE_INVALID')
        plan = self.plan(preview)
        plan['impact']['chapterReshape'][0]['sources'][0]['start'] = 2
        with self.assertRaises(StructureError) as error:
            validate_reshape_plan(self.root, self.initial, plan)
        self.assertEqual(error.exception.code, 'CHAPTER_RESHAPE_LINEAGE_INVALID')

    def test_failure_after_business_write_rolls_back_all_outputs(self):
        preview = self.split()
        failed = False
        def fail(stage, *args):
            nonlocal failed
            if stage == 'write' and not failed:
                failed = True
                raise RuntimeError('synthetic interruption')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
                self.commit(preview)
        self.assertTrue(failed)
        self.assertEqual(load_structure(self.root), self.initial)
        self.assert_originals()
        self.assertFalse(any((self.root / row['filePath']).exists() for row in self.outputs(preview)))
        self.commit(preview)
        self.assertEqual(load_structure(self.root)['revision'], self.initial['revision'] + 1)

    def test_external_source_edit_during_commit_is_preserved_and_rolls_back_new_chapters(self):
        preview = self.split()
        changed = False
        path = self.root / f'正文/{self.first}.md'
        def edit(stage, *args):
            nonlocal changed
            if stage == 'write' and not changed:
                changed = True
                path.write_bytes(b'external author edit')
        with patch('scripts.core.write_sets.checkpoint', side_effect=edit):
            with self.assertRaises(StructureError) as error:
                self.commit(preview)
        self.assertEqual(error.exception.code, 'TRANSACTION_GUARD_DRIFT')
        self.assertEqual(path.read_bytes(), b'external author edit')
        self.assertEqual(load_structure(self.root), self.initial)
        self.assertFalse(any((self.root / row['filePath']).exists() for row in self.outputs(preview)))

    def test_lineage_only_reports_committed_results_and_finds_both_ends(self):
        preview = self.split()
        self.assertEqual(query_lineage(self.root, chapterId=self.first)['items'], [])
        self.commit(preview)
        source_rows = query_lineage(self.root, chapterId=self.first)['items']
        self.assertEqual(len(source_rows), 2)
        target_id = self.outputs(preview)[0]['chapterId']
        result_rows = query_lineage(self.root, chapterId=target_id)['items']
        self.assertEqual(len(result_rows), 1)
        self.assertEqual(result_rows[0]['sources'][0]['chapterId'], self.first)
        self.assertFalse(result_rows[0]['operationUndone'])


class ChapterReshapeTransportTests(unittest.TestCase):
    def test_cut_before_string_arrays_use_repeated_cli_flags_and_the_same_mcp_schema(self):
        from scripts.core.operations import validate_arguments
        spec = next(row for row in chapter_reshape_operations() if row.id == 'chapter.split-preview')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            environment = {**os.environ, 'GOVEL_LOG_DIR': str(base / 'logs'), 'GOVEL_LOG_MODE': 'off'}
            cli_root, mcp_root = base / 'cli-book', base / 'mcp-book'
            fixture = refactor_project(cli_root)
            refactor_project(mcp_root)
            arguments = {'requestId': 'quote-boundaries', 'chapterId': fixture['chapterOne'],
                         'cutBefore': ['艾琳握着', '旧钥匙'], 'titles': ['河口', '握钥', '旧物'], 'reason': '按作者选定原文分章'}
            validate_arguments(spec.input_schema, arguments)
            for invalid in ({**arguments, 'cutOffsets': [7]}, {k: v for k, v in arguments.items() if k != 'cutBefore'}):
                with self.assertRaises(StructureError):
                    validate_arguments(spec.input_schema, invalid)
            cli = subprocess.run([sys.executable, 'scripts/novelctl.py', 'project', 'structure', 'chapter-split-preview',
                '--project-root', str(cli_root), '--request-id', arguments['requestId'], '--chapter-id', fixture['chapterOne'],
                '--cut-before', '艾琳握着', '--cut-before', '旧钥匙', '--titles', '河口', '--titles', '握钥', '--titles', '旧物',
                '--reason', arguments['reason']], capture_output=True, text=True, env=environment)
            self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            cli_preview = json.loads(cli.stdout)
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'preview_chapter_split', 'arguments': {'projectRoot': str(mcp_root), **arguments}}}
            mcp = subprocess.run([sys.executable, 'scripts/mcp_server.py'], input=json.dumps(request) + '\n',
                                 capture_output=True, text=True, env=environment)
            self.assertEqual(mcp.returncode, 0, mcp.stderr)
            result = json.loads(mcp.stdout)['result']
            self.assertFalse(result['isError'], result)
            mcp_preview = result['structuredContent']
            def read_allocations(preview):
                if 'chapterReshape' in preview['impact']:
                    return preview['impact']['chapterReshape']
                action = preview['readAction']['chapterIdsAction']
                rows, cursor = [], None
                while True:
                    params = {**action['arguments'], **({'cursor': cursor} if cursor else {})}
                    request = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                               'params': {'name': action['tool'], 'arguments': params}}
                    proc = subprocess.run([sys.executable, 'scripts/mcp_server.py'], input=json.dumps(request) + '\n',
                                          capture_output=True, text=True, env=environment)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    result = json.loads(proc.stdout)['result']
                    self.assertFalse(result['isError'], result)
                    page = result['structuredContent']
                    rows.extend(row for row in page['items'] if row['category'] == 'chapterReshape')
                    if not page['hasMore']:
                        return rows
                    cursor = page['nextCursor']
            compare = lambda rows: [(row['title'], row['contentDigest'], row['sources']) for row in rows]
            self.assertEqual(compare(read_allocations(cli_preview)), compare(read_allocations(mcp_preview)))
            commit = subprocess.run([sys.executable, 'scripts/novelctl.py', 'project', 'structure', 'chapter-reshape-commit',
                '--project-root', str(mcp_root), '--operation-id', mcp_preview['operationId'],
                '--plan-hash', mcp_preview['planHash'], '--approve'], capture_output=True, text=True, env=environment)
            self.assertEqual(commit.returncode, 0, commit.stdout + commit.stderr)
            self.assertEqual([r['title'] for r in load_structure(mcp_root)['chapters'] if r['status'] != 'archived'][:3],
                             arguments['titles'])

    def test_operation_definitions_have_the_same_typed_facades(self):
        from scripts.core.operations import operations
        known = {spec.id: spec for spec in operations()}
        for declared in chapter_reshape_operations():
            self.assertIn(declared.id, known)
            self.assertEqual(known[declared.id].input_schema, declared.input_schema)
            self.assertTrue(known[declared.id].mcp)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '书'
            fixture = refactor_project(root)
            cli = subprocess.run([sys.executable, 'scripts/novelctl.py', 'project', 'structure', 'chapter-split-preview',
                '--project-root', str(root), '--request-id', 'facade-1', '--chapter-id', fixture['chapterOne'],
                '--cut-offsets', '[7]', '--reason', '作者要求分章'], capture_output=True, text=True, check=False)
            self.assertEqual(cli.returncode, 0, cli.stderr + cli.stdout)
            preview = json.loads(cli.stdout)
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'commit_chapter_reshape', 'arguments': {'projectRoot': str(root),
                    'operationId': preview['operationId'], 'planHash': preview['planHash'], 'approve': True}}}
            mcp = subprocess.run([sys.executable, 'scripts/mcp_server.py'], input=json.dumps(request) + '\n',
                                 capture_output=True, text=True, check=False)
            self.assertEqual(mcp.returncode, 0, mcp.stderr)
            result = json.loads(mcp.stdout)
            self.assertFalse(result['result'].get('isError', False), result)
            chapters = [row for row in load_structure(root)['chapters'] if row['status'] != 'archived']
            self.assertEqual(len(chapters), 4)


if __name__ == '__main__':
    unittest.main()
