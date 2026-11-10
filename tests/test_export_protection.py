"""Manuscript delivery never upgrades foreign prose or discards author output edits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.core.commands import export_manuscript
from scripts.core import manuscript_export
from scripts.core.operations import operations
from scripts.core.prose_binding import seal_binding
from scripts.core.source_selection import select_project_content
from scripts.core.structure_store import StructureError
from scripts.mcp_server import handle
from tests.test_export import CHAPTERS, make_project, run_cli


class ExportProtectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / 'book'
        make_project(self.root)
        self.target = self.root / '工作台' / '导出测试-成品.txt'
        self.prose = self.root / '正文' / f'{CHAPTERS[0][0]}.md'
        self.source = self.root / '.govel' / 'structure.json'

    def export(self, **kwargs):
        return export_manuscript(self.root, **kwargs)

    def cli(self, *arguments):
        return run_cli(['project', 'export', '--project-root', str(self.root), *arguments])

    def mcp(self, **arguments):
        return handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'export_discovered_operation', 'arguments': {
                'projectRoot': str(self.root), 'operation': 'project.export', 'arguments': arguments}}})['result']

    def test_external_modification_rejected_without_output_on_both_transports(self):
        modified = '作者在外部编辑器写的新句。'
        self.prose.write_text(modified)
        source = self.source.read_bytes()
        code, result = self.cli()
        self.assertNotEqual(code, 0)
        self.assertEqual(result['code'], 'PROSE_EXTERNAL_MODIFICATION')
        bridged = self.mcp()
        self.assertTrue(bridged['isError'], bridged)
        self.assertEqual(bridged['structuredContent']['code'], 'PROSE_EXTERNAL_MODIFICATION')
        self.assertEqual(self.prose.read_text(), modified)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertFalse(self.target.exists())

    def test_unowned_existing_prose_is_not_exported(self):
        source = json.loads(self.source.read_text())
        chapter = source['chapters'][0]
        chapter['proseBinding'] = seal_binding(source['projectId'], chapter['chapterId'], chapter['filePath'],
                                               state='unbound', generation=source['revision'], expected_content_digest=None)
        self.source.write_text(json.dumps(source, ensure_ascii=False, indent=2))
        with self.assertRaises(StructureError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')
        self.assertFalse(self.target.exists())

    def test_missing_selected_chapter_marks_file_and_receipt_partial(self):
        self.prose.unlink()
        result = self.export(fmt='md')
        self.assertEqual(result['code'], 'MANUSCRIPT_PARTIALLY_EXPORTED')
        self.assertFalse(result['complete'])
        self.assertEqual(result['missingChapters'][0]['chapterId'], CHAPTERS[0][0])
        self.assertIn('【部分导出】', Path(result['output']).read_text())
        self.assertIn('CH001', Path(result['output']).read_text())
        self.assertEqual(result['chapterCount'], 1)

    def test_status_exclusion_is_complete_for_requested_scope(self):
        result = self.export()
        self.assertTrue(result['complete'])
        self.assertEqual(result['missingChapters'], [])
        self.assertNotIn('【部分导出】', self.target.read_text())
        self.assertTrue(all(row['observedState'] == 'OWNED_CLEAN' for row in result['chapters']))
        self.assertEqual(result['chapters'][0]['contentHash'], hashlib.sha256(self.prose.read_bytes()).hexdigest())
        self.assertEqual(result['snapshot']['sourceHash'], select_project_content(self.root)['snapshot']['sourceHash'])

    def test_requested_nonstable_prose_remains_identified_as_nonstable(self):
        result = self.export(statuses=('stable', 'draft'))
        self.assertTrue(result['complete'])
        self.assertIn('【含非稳定章节】', self.target.read_text())
        self.assertEqual(result['chapters'][-1]['status'], 'draft')

    def test_mcp_status_selection_applies_choices_to_each_item(self):
        definition = next(item for item in operations() if item.id == 'project.export')
        field = definition.input_schema['properties']['status']
        self.assertEqual(field['items']['enum'], ['stable', 'review', 'draft', 'planning_required'])
        self.assertNotIn('enum', field)
        result = self.mcp(status=['stable', 'draft'])
        self.assertFalse(result['isError'], result)
        self.assertEqual(result['structuredContent']['chapterCount'], 3)
        self.assertIn('【含非稳定章节】', self.target.read_text())
        previous = self.target.read_bytes()
        for status in (['stable', 'archived'], ['stable', 7], 'stable'):
            with self.subTest(status=status):
                failure = self.mcp(status=status)
                self.assertTrue(failure['isError'], failure)
                self.assertEqual(failure['structuredContent']['code'], 'ARGUMENTS_INVALID')
                self.assertEqual(self.target.read_bytes(), previous)

    def test_identical_export_does_not_rewrite_target(self):
        first = self.export()
        stat_before = self.target.stat()
        second = self.export()
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual(self.target.stat().st_ino, stat_before.st_ino)
        self.assertEqual(self.target.stat().st_mtime_ns, stat_before.st_mtime_ns)
        self.assertNotIn('previousOutput', second)

    def test_author_annotations_require_explicit_bound_overwrite_and_are_preserved(self):
        self.export()
        self.target.write_text(self.target.read_text() + '\n作者独有批注。\n')
        previous = self.target.read_bytes()
        inspected = hashlib.sha256(previous).hexdigest()
        for flags in ((), ('--overwrite',), ('--expected-file-hash', inspected)):
            code, result = self.cli(*flags)
            self.assertNotEqual(code, 0, result)
            self.assertEqual(result['code'], 'EXPORT_TARGET_EXISTS')
            self.assertEqual(result['details']['sha256'], inspected)
            self.assertEqual(self.target.read_bytes(), previous)
        code, result = self.cli('--overwrite', '--expected-file-hash', inspected)
        self.assertEqual(code, 0, result)
        self.assertEqual(Path(result['previousOutput']).read_bytes(), previous)
        self.assertNotIn('作者独有批注。', self.target.read_text())

    def test_discovered_operation_exposes_and_consumes_overwrite_hash(self):
        definition = next(item for item in operations() if item.id == 'project.export')
        self.assertIn('overwrite', definition.input_schema['properties'])
        self.assertIn('expectedFileHash', definition.input_schema['properties'])
        self.target.parent.mkdir(exist_ok=True)
        old = b'author notes from another editor\n'
        self.target.write_bytes(old)
        failed = self.mcp()
        self.assertTrue(failed['isError'], failed)
        self.assertEqual(failed['structuredContent']['code'], 'EXPORT_TARGET_EXISTS')
        stale = self.mcp(overwrite=True, expectedFileHash='0' * 64)
        self.assertEqual(stale['structuredContent']['code'], 'EXPORT_TARGET_CONFLICT')
        result = self.mcp(overwrite=True, expectedFileHash=hashlib.sha256(old).hexdigest())
        self.assertFalse(result['isError'], result)
        self.assertEqual(Path(result['structuredContent']['previousOutput']).read_bytes(), old)

    def test_stale_target_hash_is_rejected_even_for_identical_payload(self):
        self.export()
        before = self.target.read_bytes()
        with self.assertRaises(StructureError) as caught:
            self.export(overwrite=True, expected_file_hash='0' * 64)
        self.assertEqual(caught.exception.code, 'EXPORT_TARGET_CONFLICT')
        self.assertEqual(self.target.read_bytes(), before)

    def test_disappeared_inspected_target_is_not_silently_recreated(self):
        with self.assertRaises(StructureError) as caught:
            self.export(overwrite=True, expected_file_hash='0' * 64)
        self.assertEqual(caught.exception.code, 'EXPORT_TARGET_CONFLICT')
        self.assertFalse(self.target.exists())

    def test_output_path_boundary_and_symlink_target_are_preserved(self):
        original = self.prose.read_bytes()
        for output in (self.prose, self.source):
            with self.assertRaises(StructureError) as caught:
                self.export(output=output)
            self.assertEqual(caught.exception.code, 'EXPORT_OUTPUT_FORBIDDEN')
        self.target.parent.mkdir(exist_ok=True)
        self.target.symlink_to(self.prose)
        with self.assertRaises(StructureError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, 'SYMLINK_PATH_FORBIDDEN')
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.prose.read_bytes(), original)

    def test_prose_symlink_is_rejected(self):
        other = self.root.parent / 'outside.md'
        self.prose.rename(other)
        self.prose.symlink_to(other)
        with self.assertRaises(StructureError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, 'SYMLINK_PATH_FORBIDDEN')
        self.assertFalse(self.target.exists())

    def test_pending_transaction_blocks_export(self):
        transactions = self.root / '.govel' / 'transactions'
        transactions.mkdir(exist_ok=True)
        (transactions / 'TX-pending.json').write_text('{}')
        with self.assertRaises(StructureError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, 'TRANSACTION_INCOMPLETE')
        code, result = self.cli()
        self.assertNotEqual(code, 0, result)
        self.assertEqual(result['code'], 'TRANSACTION_INCOMPLETE')
        bridged = self.mcp()
        self.assertTrue(bridged['isError'], bridged)
        self.assertEqual(bridged['structuredContent']['code'], 'TRANSACTION_INCOMPLETE')
        self.assertFalse(self.target.exists())

    def test_changed_prose_between_ownership_check_and_actual_read_is_not_exported(self):
        original = manuscript_export.guard_observation

        def changed(path):
            if path == self.prose:
                path.write_text('读入前发生外部改稿。')
            return original(path)

        with patch.object(manuscript_export, 'guard_observation', side_effect=changed):
            with self.assertRaises(StructureError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, 'PROSE_EXTERNAL_MODIFICATION')
        self.assertEqual(self.prose.read_text(), '读入前发生外部改稿。')
        self.assertFalse(self.target.exists())

    def test_input_changes_before_publication_reject_output_without_reverting_editor(self):
        for target in ('prose', 'source'):
            with self.subTest(target=target):
                path = self.prose if target == 'prose' else self.source
                old = path.read_bytes()

                def changed(stage):
                    if stage == 'before_publish':
                        path.write_bytes(old + b'\n')

                with patch.object(manuscript_export, 'checkpoint', side_effect=changed):
                    with self.assertRaises(StructureError) as caught:
                        self.export()
                self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
                self.assertEqual(path.read_bytes(), old + b'\n')
                self.assertFalse(self.target.exists())
                path.write_bytes(old)

    def test_new_output_race_preserves_other_writer(self):
        def changed(stage):
            if stage == 'before_publish':
                self.target.write_text('并发写入的独有批注。')

        with patch.object(manuscript_export, 'checkpoint', side_effect=changed):
            with self.assertRaises(StructureError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, 'EXPORT_TARGET_CONFLICT')
        self.assertEqual(self.target.read_text(), '并发写入的独有批注。')

    def test_overwrite_race_before_preservation_restores_actual_changed_bytes(self):
        self.target.parent.mkdir(exist_ok=True)
        self.target.write_bytes(b'inspected output')

        def changed(stage):
            if stage == 'before_preserve':
                self.target.write_bytes(b'concurrent author change')

        with patch.object(manuscript_export, 'checkpoint', side_effect=changed):
            with self.assertRaises(StructureError) as caught:
                self.export(overwrite=True, expected_file_hash=hashlib.sha256(b'inspected output').hexdigest())
        self.assertEqual(caught.exception.code, 'EXPORT_TARGET_CONFLICT')
        self.assertEqual(self.target.read_bytes(), b'concurrent author change')

    def test_overwrite_race_after_preservation_keeps_both_outputs(self):
        self.target.parent.mkdir(exist_ok=True)
        old = b'inspected output'
        self.target.write_bytes(old)

        def changed(stage):
            if stage == 'preserved':
                self.target.write_bytes(b'concurrent new output')

        with patch.object(manuscript_export, 'checkpoint', side_effect=changed):
            with self.assertRaises(StructureError) as caught:
                self.export(overwrite=True, expected_file_hash=hashlib.sha256(old).hexdigest())
        self.assertEqual(caught.exception.code, 'EXPORT_TARGET_CONFLICT')
        self.assertEqual(self.target.read_bytes(), b'concurrent new output')
        self.assertEqual(Path(caught.exception.details['previousOutput']).read_bytes(), old)

    def test_source_drift_after_preserving_output_restores_output(self):
        self.target.parent.mkdir(exist_ok=True)
        old = b'inspected output'
        self.target.write_bytes(old)

        def changed(stage):
            if stage == 'preserved':
                self.prose.write_text('作者稍后改稿。')

        with patch.object(manuscript_export, 'checkpoint', side_effect=changed):
            with self.assertRaises(StructureError) as caught:
                self.export(overwrite=True, expected_file_hash=hashlib.sha256(old).hexdigest())
        self.assertEqual(caught.exception.code, 'TRANSACTION_GUARD_DRIFT')
        self.assertEqual(self.target.read_bytes(), old)
        self.assertEqual(self.prose.read_text(), '作者稍后改稿。')

    def test_successful_overwrite_preserves_later_writes_to_open_old_inode(self):
        self.target.parent.mkdir(exist_ok=True)
        old = b'inspected output'
        self.target.write_bytes(old)
        with self.target.open('ab') as editor:
            result = self.export(overwrite=True, expected_file_hash=hashlib.sha256(old).hexdigest())
            editor.write(b'\neditor wrote later')
        self.assertEqual(Path(result['previousOutput']).read_bytes(), old + b'\neditor wrote later')
        self.assertNotIn(b'editor wrote later', self.target.read_bytes())

    def test_directory_symlink_swap_cannot_redirect_publication(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        moved = self.root / 'saved-workbench'

        def changed(stage):
            if stage == 'prepared':
                self.target.parent.rename(moved)
                self.target.parent.symlink_to(outside, target_is_directory=True)

        with patch.object(manuscript_export, 'checkpoint', side_effect=changed):
            with self.assertRaises(StructureError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, 'EXPORT_TARGET_CONFLICT')
        self.assertFalse((outside / self.target.name).exists())
        self.assertEqual(list(moved.glob('.*.export-*')), [])

    def test_random_temporary_or_preservation_collision_never_deletes_existing_file(self):
        self.target.parent.mkdir(exist_ok=True)
        old = b'existing author output'
        self.target.write_bytes(old)
        names = [f'.{self.target.name}.export-collision',
                 f'{self.target.stem}.previous-collision{self.target.suffix}']
        for name in names:
            with self.subTest(name=name):
                collision = self.target.with_name(name)
                collision.write_bytes(b'preexisting unrelated author bytes')
                with patch.object(manuscript_export.uuid, 'uuid4', return_value=SimpleNamespace(hex='collision')):
                    with self.assertRaises(StructureError) as caught:
                        self.export(overwrite=True, expected_file_hash=hashlib.sha256(old).hexdigest())
                self.assertEqual(caught.exception.code, 'EXPORT_WRITE_FAILED')
                self.assertEqual(collision.read_bytes(), b'preexisting unrelated author bytes')
                self.assertEqual(self.target.read_bytes(), old)
                collision.unlink()


if __name__ == '__main__':
    unittest.main()
