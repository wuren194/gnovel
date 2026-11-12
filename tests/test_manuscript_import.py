"""Author-facing import, real manuscript bytes and publication failure boundaries."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.import_commit import commit_import
from scripts.core.manuscript_import import preview_import, inspect_import, read_import_text, plan_directory
from scripts.core.operations import call_operation
from scripts.core.prose_binding import observe_chapter
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.project import query_policy

ROOT = Path(__file__).resolve().parents[1]


class ManuscriptImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / '原 稿.txt'
        self.root = self.base / '新 书'
        self.raw = '书名与前言\r\n\r\n第一章 交班\r\n他归还了钥匙。\r\n\r\n第二章 夜路\r\n雨停后，他走出了门。\r\n'.encode()
        self.source.write_bytes(self.raw)
        environment = patch.dict(os.environ, {'GOVEL_LOG_MODE': 'off'})
        environment.start()
        self.addCleanup(environment.stop)

    def preview(self, **kwargs):
        return preview_import(self.root, source=str(self.source), **kwargs)

    def call(self, operation, **arguments):
        return call_operation(operation, self.root, arguments)

    def commit(self, preview):
        return commit_import(self.root, planId=preview['planId'], planHash=preview['planHash'], approve=True)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(StructureError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)

    def test_complete_import_preserves_all_bytes_owns_prose_and_does_not_fake_review(self):
        preview = self.preview()
        self.assertFalse(self.root.exists())
        self.assertEqual(preview['chapterCount'], 3)
        self.assertTrue(preview['warnings'])
        result = self.commit(preview)
        source = load_structure(self.root)
        self.assertEqual(b''.join((self.root / c['filePath']).read_bytes() for c in source['chapters']), self.raw)
        self.assertEqual(self.source.read_bytes(), self.raw)
        for chapter in source['chapters']:
            self.assertEqual(chapter['status'], 'planning_required')
            self.assertEqual(observe_chapter(self.root, source, chapter)['state'], 'OWNED_CLEAN')
        workflow = json.loads((self.root / '.govel/workflow.json').read_text())
        self.assertEqual(workflow['approvals'], {})
        self.assertEqual(workflow['approvedChapterIds'], [])
        self.assertEqual(workflow['artifacts'], {})
        self.assertEqual(source['entities'], [])
        self.assertFalse(result['semanticReviewComplete'])
        self.assertTrue((self.root / 'AGENTS.md').is_file())
        from scripts import state_cache
        self.assertTrue(state_cache.is_fresh(self.root))
        from scripts.core.manuscript_export import export_manuscript
        exported = export_manuscript(self.root, statuses=('planning_required',), output=self.base / '阅读稿.txt')
        self.assertEqual(exported['code'], 'MANUSCRIPT_EXPORTED')
        from scripts.core.project_doctor import project_doctor
        report = project_doctor(self.root)
        # Host registrations can intentionally point to a different checkout.
        # Import acceptance covers the project, not installation of this test clone.
        problems = [row for row in report['issues'] if row['domain'] != 'tooling' and row['status'] == 'mechanical_error']
        self.assertEqual(problems, [])

    def test_manual_boundaries_replace_auto_splits_without_ids_or_missing_text(self):
        boundaries = [{'sourcePath': self.source.name, 'startLine': 1, 'title': '开场与交班'},
                      {'sourcePath': self.source.name, 'startLine': 6, 'title': '夜路'}]
        original = deepcopy(boundaries)
        preview = self.preview(boundaries=boundaries)
        self.assertEqual(preview['chapterCount'], 2)
        self.commit(preview)
        source = load_structure(self.root)
        self.assertEqual([c['title'] for c in source['chapters']], ['开场与交班', '夜路'])
        self.assertEqual(b''.join((self.root / c['filePath']).read_bytes() for c in source['chapters']), self.raw)
        self.assertEqual(boundaries, original)

    def test_duplicate_heading_window_and_free_marker_pattern_merge_boundary(self):
        self.source.write_text('第一章 交班\n第一章 交班（本章免费）\n正文内容足够长，记录了完整的交班过程。\n第二章 夜路\n雨停后，他走出了门。\n')
        preview = self.preview()
        self.assertEqual(preview['chapterCount'], 2)
        self.assertEqual([row['title'] for row in preview['chapters']], ['第一章 交班', '第二章 夜路'])
        merged = [row for row in preview['anomalies'] if row['code'] == 'duplicate_heading_merged']
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['count'], 1)
        self.assertTrue(all(not row.get('warnings') for row in preview['chapters']))
        ignored = self.preview(ignoreHeadingPattern=r'（本章免费）$')
        self.assertEqual(ignored['chapterCount'], 2)
        filtered = inspect_import(self.root, planId=preview['planId'], planHash=preview['planHash'], anomaliesOnly=True)
        self.assertEqual(filtered['chapterCount'], 2)
        self.assertTrue(filtered['anomaliesOnly'])
        self.assertTrue(any(row['code'] == 'duplicate_heading_merged' for row in filtered['anomalies']))
        self.assertEqual(filtered['listedChapterCount'], 2)

    def test_special_headings_custom_prefixes_ocr_and_sequence_anomalies(self):
        self.source.write_text('引子 穿越\n引子正文展开，包含足够多的内容。\n一百零一章 初遇\n人物继续推进故事并留下清楚线索。\n第五百三十九掌 误字\n这一段正文也有足够内容。\n第二百三十六章 大结局\n终章正文完整结束。\n附录 资料\n附录正文。\n')
        preview = self.preview(headingPatterns=['附录'])
        self.assertEqual(preview['chapterCount'], 5)
        suspected = [row for row in preview['anomalies'] if row['code'] == 'suspected_heading']
        self.assertEqual([row['line'] for row in suspected], [3, 5])
        self.assertEqual(suspected[1]['suggestion'], '第五百三十九章 误字')
        sequence = [row for row in preview['anomalies'] if row['code'] == 'chapter_number_sequence']
        self.assertEqual(sequence[-1]['actual'], 236)
        self.assertTrue(any(row['title'].startswith('引子') for row in preview['chapters']))

    def test_empty_chapter_is_reported_as_an_anomaly(self):
        self.source.write_text('第一章 空章\n\n第二章 正常\n正文有内容。\n')
        preview = self.preview()
        empty = [row for row in preview['anomalies'] if row['code'] == 'short_chapter']
        self.assertGreaterEqual(len(empty), 1)
        self.assertEqual(empty[0]['chapterOrdinal'], 1)

    def test_inspect_exports_csv_or_jsonl_plan_without_overwriting(self):
        preview = self.preview()
        output = self.base / 'plan.jsonl'
        inspected = self.call('import.export-plan', planId=preview['planId'], planHash=preview['planHash'], exportPath=str(output))
        self.assertEqual(inspected['exportedPlan']['rowCount'], 3)
        self.assertEqual(len(output.read_text().splitlines()), 3)
        self.assert_code('IMPORT_EXPORT_EXISTS', self.call,
                         'import.export-plan',
                         planId=preview['planId'], planHash=preview['planHash'], exportPath=str(output))

    def test_inspect_searches_by_title_or_line_and_reports_anomaly_summary(self):
        self.source.write_text('第一章 交班\n正文有足够长度，写清楚两个人交班。\n第二章 夜路\n雨停后，他走出了门。\n')
        preview = self.preview()
        title_match = self.call('import.inspect', planId=preview['planId'], planHash=preview['planHash'], search='夜路')
        self.assertEqual([row['title'] for row in title_match['chapters']], ['第二章 夜路'])
        line_match = self.call('import.inspect', planId=preview['planId'], planHash=preview['planHash'], search='3')
        self.assertEqual([row['title'] for row in line_match['chapters']], ['第二章 夜路'])
        self.assertEqual(title_match['anomalySummary'], {'short_chapter': 2})

    def test_project_read_rejects_quarantined_chapter_with_reason(self):
        preview = self.preview()
        self.commit(preview)
        from scripts.core.commands import chapter_prose
        structure = load_structure(self.root)
        chapter = structure['chapters'][0]
        chapter.update(status='quarantined', quarantineReason='content_boundary', quarantineCategory='content_boundary')
        from scripts.core.jsonio import write_json
        write_json(self.root / '.govel/structure.json', structure)
        self.assert_code('CHAPTER_QUARANTINED', chapter_prose, self.root, chapter['chapterId'])

    def test_invalid_boundaries_fail_before_writes(self):
        for starts in ([1, 1], [3, 1], [1, 99]):
            with self.subTest(starts=starts):
                self.assert_code('IMPORT_BOUNDARIES_INVALID', self.preview, boundaries=[
                    {'sourcePath': self.source.name, 'startLine': start, 'title': '章节'} for start in starts])
        self.assert_code('IMPORT_BOUNDARIES_INVALID', self.preview, boundaries=[
            {'sourcePath': '../other', 'startLine': 1, 'title': '章节'}])
        self.assertFalse(self.root.exists())

    def test_bracket_headings_and_late_first_boundary_preserve_prefix(self):
        for heading in ('（一）交班', '（1）交班', '【一】交班', '一、交班', '(1) 交班'):
            self.source.write_text('前言\n' + heading + '\n正文。\n')
            result = self.preview()
            self.assertEqual(result['chapterCount'], 2)
        boundaries = [{'sourcePath': self.source.name, 'startLine': 2, 'title': '交班'}]
        before = deepcopy(boundaries)
        result = self.preview(boundaries=boundaries)
        self.assertEqual(result['chapters'][0]['startLine'], 1)
        self.assertEqual(result['chapterCount'], 2)
        self.assertEqual(boundaries, before)

    def test_explicit_encoding_and_bom_keep_original_evidence(self):
        for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
            with self.subTest(encoding=encoding):
                self.root = self.base / encoding
                raw = '第一章 归还\n他归还了钥匙。\n'.encode(encoding)
                self.source.write_bytes(raw)
                preview = self.preview(encoding=encoding)
                self.commit(preview)
                chapter = load_structure(self.root)['chapters'][0]
                self.assertEqual((self.root / chapter['filePath']).read_text(), '第一章 归还\n他归还了钥匙。\n')
                self.assertEqual((self.root / '.govel/import-source/sources/0.bin').read_bytes(), raw)
                self.assertEqual(self.source.read_bytes(), raw)

    def test_bad_encoding_and_empty_sources_do_not_replace_characters(self):
        for raw, code in ((b'\xffbad', 'IMPORT_ENCODING_INVALID'), (b'  \n', 'IMPORT_SOURCE_INVALID'), (b'a\x00b', 'IMPORT_SOURCE_INVALID')):
            self.source.write_bytes(raw)
            self.assert_code(code, self.preview)
            self.assertFalse(self.root.exists())

    def test_directory_natural_order_and_no_heading_fallback(self):
        source = self.base / '原稿目录'
        source.mkdir()
        for name in ('10.txt', '2.md', '1.txt'):
            (source / name).write_text('独立原稿 ' + name)
        result = preview_import(self.root, source=str(source))
        self.assertEqual([r['sourcePath'] for r in result['chapters']], ['1.txt', '2.md', '10.txt'])
        self.assertTrue(result['warnings'])
        self.assertTrue(all(not r.get('warnings') for r in result['chapters']))
        self.commit(result)

    def test_unreadable_subdirectory_cannot_be_silently_omitted(self):
        source = self.base / '原稿目录'
        blocked = source / '后半部'
        blocked.mkdir(parents=True)
        (source / '1.txt').write_bytes(self.raw)
        (blocked / '2.txt').write_bytes(self.raw)
        scandir = os.scandir
        def fail(path):
            if Path(path) == blocked:
                raise PermissionError(13, 'unreadable manuscript directory', str(path))
            return scandir(path)
        with patch('scripts.core.manuscript_import.os.scandir', side_effect=fail):
            self.assert_code('IMPORT_SOURCE_INVALID', preview_import, self.root, source=str(source))
        self.assertFalse(self.root.exists())

    def test_file_split_retains_chapter_headings_in_one_chapter(self):
        result = self.preview(split='file')
        self.assertEqual(result['chapterCount'], 1)
        self.commit(result)
        chapter = load_structure(self.root)['chapters'][0]
        self.assertEqual((self.root / chapter['filePath']).read_bytes(), self.raw)

    def test_explicit_approval_and_plan_binding_required(self):
        result = self.preview()
        self.assert_code('APPROVAL_REQUIRED', commit_import, self.root, planId=result['planId'], planHash=result['planHash'])
        self.assert_code('IMPORT_PLAN_STALE', commit_import, self.root, planId=result['planId'], planHash='0' * 64, approve=True)
        self.assertFalse(self.root.exists())

    def test_modified_source_and_replaced_object_require_new_preview(self):
        for replace in (False, True):
            preview = self.preview()
            if replace:
                before = self.source.stat()
                payload = self.source.read_bytes()
                # Allocate while the original exists so its inode cannot be recycled.
                replacement = self.source.with_name(self.source.name + '.replacement')
                replacement.write_bytes(payload)
                os.replace(replacement, self.source)
                after = self.source.stat()
                self.assertNotEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
                self.assertEqual(self.source.read_bytes(), payload)
            else:
                self.source.write_bytes(self.raw + b'changed')
            self.assert_code('IMPORT_SOURCE_CHANGED', self.commit, preview)
            self.assertFalse(self.root.exists())
            self.source.write_bytes(self.raw)

    def test_source_directory_new_file_invalidates_preview(self):
        source = self.base / '原稿目录'
        source.mkdir()
        (source / '1.txt').write_bytes(self.raw)
        preview = preview_import(self.root, source=str(source))
        (source / '2.txt').write_bytes(self.raw)
        self.assert_code('IMPORT_SOURCE_CHANGED', self.commit, preview)

    def test_snapshot_and_plan_tampering_refused(self):
        preview = self.preview()
        directory = plan_directory(self.root, preview['planId'])
        (directory / 'sources/0.bin').write_bytes(b'changed')
        self.assert_code('IMPORT_SNAPSHOT_DAMAGED', self.commit, preview)
        self.assertFalse(self.root.exists())
        preview = self.preview()
        directory = plan_directory(self.root, preview['planId'])
        plan = json.loads((directory / 'plan.json').read_text())
        plan['bookName'] = 'changed'
        (directory / 'plan.json').write_text(json.dumps(plan))
        self.assert_code('IMPORT_PLAN_STALE', self.commit, preview)

    def test_provider_or_manifest_drift_requires_new_preview(self):
        preview = self.preview()
        from scripts.core.data_provider import binding
        changed = {**binding(), 'providerId': 'changed'}
        with patch('scripts.core.data_provider.binding', return_value=changed):
            self.assert_code('IMPORT_PLAN_STALE', self.commit, preview)
        fake_engine = self.base / 'changed-engine'
        fake_engine.mkdir()
        (fake_engine / 'toolbox.manifest.json').write_text('{}')
        with patch('scripts.core.import_commit.TOOLBOX_ROOT', fake_engine):
            self.assert_code('IMPORT_PLAN_STALE', self.commit, preview)
        self.assertFalse(self.root.exists())

    def test_publication_conflict_preserves_external_directory(self):
        preview = self.preview()
        def occupy(stage):
            if stage == 'before-publication':
                self.root.mkdir()
                (self.root / '用户文件').write_text('保留')
        with patch('scripts.core.import_commit.checkpoint', side_effect=occupy):
            self.assert_code('IMPORT_TARGET_EXISTS', self.commit, preview)
        self.assertEqual((self.root / '用户文件').read_text(), '保留')
        self.assertFalse((self.root / '.govel').exists())

    def test_failure_before_publication_leaves_no_project_and_retry_succeeds(self):
        preview = self.preview()
        with patch('scripts.core.import_commit.checkpoint', side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):
                self.commit(preview)
        self.assertFalse(self.root.exists())
        self.assertFalse(list(plan_directory(self.root, preview['planId']).glob('build-*')))
        self.commit(preview)

    def test_late_source_change_during_staging_prevents_publication(self):
        preview = self.preview()
        def change(stage):
            if stage == 'before-publication':
                self.source.write_bytes(self.raw + b'new text')
        with patch('scripts.core.import_commit.checkpoint', side_effect=change):
            self.assert_code('IMPORT_SOURCE_CHANGED', self.commit, preview)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.source.read_bytes(), self.raw + b'new text')

    def test_abrupt_process_exit_before_and_after_rename_is_recoverable(self):
        for stage in ('staged', 'after-publication'):
            with self.subTest(stage=stage):
                self.root = self.base / stage
                preview = self.preview()
                program = '''
import json, os, sys
from pathlib import Path
from scripts.core import import_commit
root, stage, identity = sys.argv[1:]
def stop(current):
    if current == stage:
        os._exit(73)
import_commit.checkpoint = stop
import_commit.commit_import(Path(root), approve=True, **json.loads(identity))
'''
                identity = {'planId': preview['planId'], 'planHash': preview['planHash']}
                process = subprocess.run([sys.executable, '-c', program, str(self.root), stage, json.dumps(identity)],
                    cwd=ROOT, env=dict(os.environ, GOVEL_LOG_MODE='off', PYTHONDONTWRITEBYTECODE='1'),
                    text=True, capture_output=True, timeout=30)
                self.assertEqual(process.returncode, 73, process.stdout + process.stderr)
                self.assertEqual(self.root.exists(), stage == 'after-publication')
                result = self.commit(preview)
                self.assertEqual(result['alreadyCommitted'], stage == 'after-publication')
                structure = load_structure(self.root)
                self.assertEqual(b''.join((self.root / c['filePath']).read_bytes() for c in structure['chapters']), self.raw)

    def test_failure_after_publication_and_repeated_commit_return_original_receipt(self):
        preview = self.preview()
        def fail(stage):
            if stage == 'after-publication':
                raise RuntimeError('lost response')
        with patch('scripts.core.import_commit.checkpoint', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.commit(preview)
        original = (self.root / '.govel/structure.json').read_bytes()
        result = self.commit(preview)
        self.assertTrue(result['alreadyCommitted'])
        self.assertTrue(result['historicalReceipt'])
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), original)

    def test_symlinks_and_existing_targets_rejected(self):
        link = self.base / 'link.txt'
        link.symlink_to(self.source)
        self.assert_code('IMPORT_SOURCE_INVALID', preview_import, self.root, source=str(link))
        self.root.mkdir()
        self.assert_code('IMPORT_TARGET_EXISTS', self.preview)
        self.assert_code('IMPORT_TARGET_INVALID', preview_import, ROOT / 'accidental-private-book', source=str(self.source))

    def test_natural_language_discovery_points_to_public_mcp_entrypoint(self):
        from scripts.core.tool_catalog import list_capabilities
        from scripts.core.import_operations import import_operations
        from scripts.core.argument_validation import validate_arguments
        for spec in import_operations():
            for example in spec.examples:
                validate_arguments(spec.input_schema, example)
        result = list_capabilities(query='把这个外部小说做成govel项目')
        operation = next(row for row in result['operations'] if row['id'] == 'import.preview')
        self.assertEqual(operation['invocation']['tool'], 'preview_manuscript_import')
        self.assertFalse(operation['invocation']['cliOnly'])

    def test_simultaneous_commits_choose_one_complete_project(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        preview = self.preview()
        barrier = Barrier(2)
        def synchronize(stage):
            if stage == 'before-publication':
                barrier.wait(timeout=10)
        with patch('scripts.core.import_commit.checkpoint', side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as workers:
                jobs = [workers.submit(self.commit, preview) for _ in range(2)]
                results = [job.result(timeout=20) for job in jobs]
        self.assertEqual(sorted(row['alreadyCommitted'] for row in results), [False, True])
        self.assertEqual(results[0]['projectId'], results[1]['projectId'])
        structure = load_structure(self.root)
        self.assertEqual(b''.join((self.root / c['filePath']).read_bytes() for c in structure['chapters']), self.raw)

    def test_read_page_progresses_through_one_very_long_line(self):
        text = '他归还了钥匙。' * 6000
        self.source.write_text(text)
        preview = self.preview()
        chunks, offset = [], 0
        while True:
            page = read_import_text(self.root, planId=preview['planId'], planHash=preview['planHash'],
                                    sourcePath=self.source.name, offset=offset)
            self.assertEqual(page['startLine'], 1)
            chunks.append(page['text'])
            if page['nextOffset'] is None:
                break
            self.assertGreater(page['nextOffset'], offset)
            offset = page['nextOffset']
        self.assertEqual(''.join(chunks), text)

    def test_inspection_pages_cover_every_chapter_and_frozen_text(self):
        self.source.write_text(''.join(f'第{i}章 交班\n' + '他归还了钥匙。' * 100 + '\n' for i in range(60)))
        preview = self.preview()
        rows, offset = [], 0
        while True:
            page = inspect_import(self.root, planId=preview['planId'], planHash=preview['planHash'], offset=offset, limit=7)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), query_policy()['maxBytes']['list'])
            rows.extend(page['chapters'])
            if page['nextOffset'] is None:
                break
            self.assertGreater(page['nextOffset'], offset)
            offset = page['nextOffset']
        self.assertEqual([r['ordinal'] for r in rows], list(range(1, 61)))
        chunks, offset = [], 0
        while True:
            page = read_import_text(self.root, planId=preview['planId'], planHash=preview['planHash'], sourcePath=self.source.name, offset=offset)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), query_policy()['maxBytes']['detail'])
            chunks.append(page['text'])
            if page['nextOffset'] is None:
                break
            self.assertGreater(page['nextOffset'], offset)
            offset = page['nextOffset']
        self.assertEqual(''.join(chunks), self.source.read_text())

    def test_mcp_and_cli_work_outside_checkout_with_no_handwritten_candidate(self):
        environment = dict(os.environ, GOVEL_LOG_MODE='off', PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'import', 'preview',
            '--project-root', str(self.root), '--source', str(self.source)], cwd=self.base,
            env=environment, text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        preview = json.loads(process.stdout)
        arguments = preview['nextAction']['arguments']
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'commit_manuscript_import', 'arguments': arguments}}
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')], input=json.dumps(request) + '\n',
            cwd=self.base, env=environment, text=True, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent']['chapterCount'], 3)
        self.assertTrue((self.root / '.govel/structure.json').is_file())


if __name__ == '__main__':
    unittest.main()
