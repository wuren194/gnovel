"""Full backups preserve project evidence and restore without overwriting a target."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.core import project_archive as archive_ops
from scripts.core import project_archive_io as archive_io
from scripts.core.jsonio import write_json
from scripts.core.paths import recorded_project_roots, stored_project_path
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.workflow import default_workflow
from tests.fixtures_refactor import refactor_project


class ProjectArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'source'
        self.destination = self.base / 'restored'
        self.archive = self.base / 'book.zip'
        self.fixture = refactor_project(self.root)
        workflow = default_workflow()
        workflow['approvedChapterIds'] = [self.fixture['chapterOne']]
        write_json(self.root / '.govel/workflow.json', workflow)
        (self.root / '工作台/raw.bin').write_bytes(b'\x00\xff\r\nkept')
        (self.root / '空目录').mkdir()
        self.enterContext(patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.base / 'no-provider'),
                                                  'GOVEL_LOG_MODE': 'off'}, clear=True))

    def backup(self):
        return archive_ops.backup_project(self.root, output=str(self.archive))

    def preview(self):
        return archive_ops.preview_restore_project(self.destination, archive=str(self.archive))

    def commit(self, preview):
        return archive_ops.commit_restore_project(self.destination, planRef=preview['planRef'],
            planHash=preview['planHash'], approve=True)

    def rewrite_archive(self, transform):
        with zipfile.ZipFile(self.archive) as source:
            rows = [(info, source.read(info)) for info in source.infolist()]
        rows = transform(rows)
        with zipfile.ZipFile(self.archive, 'w') as target:
            for name, raw in rows:
                target.writestr(name, raw)

    def test_round_trip_preserves_original_bytes_modes_identity_and_empty_directories(self):
        prose = self.root / '正文' / (self.fixture['chapterOne'] + '.md')
        os.chmod(prose, 0o440)
        backup = self.backup()
        original = {path.relative_to(self.root).as_posix(): path.read_bytes()
                    for path in self.root.rglob('*') if path.is_file()}
        verified = archive_ops.verify_project_backup(archive=str(self.archive), expectedArchiveHash=backup['archiveHash'])
        self.assertEqual(verified['fileCount'], len(original))
        preview = self.preview()
        self.assertFalse(self.destination.exists())
        result = self.commit(preview)
        self.assertEqual(load_structure(self.destination)['projectId'], self.fixture['structure']['projectId'])
        self.assertTrue(result['preservesProjectIdentity'])
        self.assertFalse(result['hostProcessesRestored'])
        for relative, raw in original.items():
            self.assertEqual((self.destination / relative).read_bytes(), raw, relative)
        self.assertTrue((self.destination / '空目录').is_dir())
        self.assertEqual(stat.S_IMODE((self.destination / prose.relative_to(self.root)).stat().st_mode), 0o440)
        self.assertIn(str(self.root), recorded_project_roots(self.destination))
        self.assertEqual(stored_project_path(self.destination, str(prose)), self.destination / prose.relative_to(self.root))

    def test_restore_preserves_previous_locator_and_appends_history(self):
        from scripts.core.project_relocate import compute_locator_receipt
        former = self.base / 'former'
        locator = compute_locator_receipt(None, project_id=self.fixture['structure']['projectId'],
            root_at_operation=str(former), current_locator=str(self.root), actor='author', operation_id='MOVE-FIXTURE',
            relocated_at='2026-11-10T00:00:00Z')
        write_json(self.root / '.govel/project-locator.json', locator)
        previous = (self.root / '.govel/project-locator.json').read_bytes()
        self.backup()
        result = self.commit(self.preview())
        preserved = [relative for relative in result['preservedMetadata'] if relative.endswith('project-locator.json')]
        self.assertEqual(len(preserved), 1)
        self.assertEqual((self.destination / preserved[0]).read_bytes(), previous)
        self.assertEqual(recorded_project_roots(self.destination), {str(former), str(self.root), str(self.destination)})

    def test_restoring_a_manually_moved_project_records_the_observed_snapshot_location(self):
        from scripts.core.project_relocate import compute_locator_receipt
        former, previous = self.base / 'former', self.base / 'previous'
        locator = compute_locator_receipt(None, project_id=self.fixture['structure']['projectId'],
            root_at_operation=str(former), current_locator=str(previous), actor='author', operation_id='MOVE-FIXTURE',
            relocated_at='2026-11-10T00:00:00Z')
        write_json(self.root / '.govel/project-locator.json', locator)
        self.backup()
        preview = self.preview()
        self.assertEqual(preview['locatorHistoryAction'], 'record_snapshot_location')
        self.commit(preview)
        self.assertEqual(recorded_project_roots(self.destination),
                         {str(former), str(previous), str(self.root), str(self.destination)})

    def test_restore_stops_old_host_handoff_and_preserves_its_original_record(self):
        index = {'schemaVersion': 1, 'projectId': self.fixture['structure']['projectId'], 'sequence': 1,
                 'handoffs': {'HF-FIXTURE': {'handoffId': 'HF-FIXTURE', 'status': 'created'}}, 'returns': {}}
        write_json(self.root / '.govel/handoffs/index.json', index)
        raw = (self.root / '.govel/handoffs/index.json').read_bytes()
        self.backup()
        preview = self.preview()
        self.assertEqual(preview['handoffsToStop'], ['HF-FIXTURE'])
        result = self.commit(preview)
        restored = json.loads((self.destination / '.govel/handoffs/index.json').read_bytes())
        self.assertEqual(restored['handoffs']['HF-FIXTURE']['status'], 'stopped')
        preserved = [relative for relative in result['preservedMetadata'] if relative.endswith('handoffs/index.json')]
        self.assertEqual((self.destination / preserved[0]).read_bytes(), raw)
        self.assertEqual((self.root / '.govel/handoffs/index.json').read_bytes(), raw)

    def test_edited_prose_is_preserved_without_forging_clean_ownership(self):
        prose = self.root / '正文' / (self.fixture['chapterOne'] + '.md')
        prose.write_text('作者在外部修改的原文。\r\n', newline='')
        self.backup()
        self.commit(self.preview())
        self.assertEqual((self.destination / prose.relative_to(self.root)).read_bytes(), prose.read_bytes())
        self.assertEqual((self.destination / '.govel/structure.json').read_bytes(), (self.root / '.govel/structure.json').read_bytes())

    def test_backup_refuses_pending_transaction_and_internal_output(self):
        with self.assertRaises(StructureError) as caught:
            archive_ops.backup_project(self.root, output=str(self.root / 'backup.zip'))
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_PATH_INVALID')
        write_json(self.root / '.govel/transactions/TX-FIXTURE.json', {})
        with self.assertRaises(StructureError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'TRANSACTION_INCOMPLETE')
        self.assertFalse(self.archive.exists())

    def test_backup_refuses_symlink_without_following_it(self):
        external = self.base / 'external.txt'
        external.write_text('external')
        (self.root / 'link.txt').symlink_to(external)
        with self.assertRaises(StructureError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_PATH_INVALID')
        self.assertFalse(self.archive.exists())
        self.assertEqual(external.read_text(), 'external')

    def test_unreadable_directory_cannot_be_reported_as_a_complete_snapshot(self):
        directory = self.root / 'unreadable'
        directory.mkdir()
        (directory / 'manuscript.txt').write_text('不能漏掉的原稿。')
        directory.chmod(0)
        try:
            if os.geteuid() == 0:
                # Root bypasses mode bits; exercise the same os.walk error path.
                real_walk = archive_io.os.walk
                def unreadable_walk(root, **kwargs):
                    kwargs['onerror'](PermissionError(13, 'Permission denied', str(directory)))
                    yield from real_walk(root, **kwargs)
                context = patch.object(archive_io.os, 'walk', side_effect=unreadable_walk)
            else:
                from contextlib import nullcontext
                context = nullcontext()
            with context, self.assertRaises(StructureError) as caught:
                self.backup()
            self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_UNREADABLE')
            self.assertFalse(self.archive.exists())
        finally:
            directory.chmod(0o700)

    def test_backup_does_not_publish_a_package_missing_required_workflow(self):
        (self.root / '.govel/workflow.json').unlink()
        with self.assertRaises(StructureError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_INVALID')
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.base.glob('.govel-backup-*')), [])

    def test_source_change_before_publication_discards_partial_backup(self):
        def mutate(stage):
            if stage == 'backup-before-publication':
                (self.root / '工作台/raw.bin').write_bytes(b'new version')
        with patch.object(archive_ops, 'checkpoint', side_effect=mutate), self.assertRaises(StructureError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_INPUT_CHANGED')
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.base.glob('.govel-backup-*')), [])

    def test_publication_never_overwrites_a_newly_occupied_backup_path(self):
        def occupy(stage):
            if stage == 'backup-before-publication':
                self.archive.write_bytes(b'another file')
        with patch.object(archive_ops, 'checkpoint', side_effect=occupy), self.assertRaises(StructureError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'PROJECT_BACKUP_EXISTS')
        self.assertEqual(self.archive.read_bytes(), b'another file')

    def test_tampered_member_and_traversal_are_rejected(self):
        self.backup()
        def tamper(rows):
            return [(name, b'changed' if name.filename.endswith('raw.bin') else raw) for name, raw in rows]
        self.rewrite_archive(tamper)
        with self.assertRaises(StructureError):
            archive_ops.verify_project_backup(archive=str(self.archive))
        self.assertFalse(self.destination.exists())
        self.archive.unlink()
        self.backup()
        self.rewrite_archive(lambda rows: [*rows, ('project/../../outside.txt', b'escape')])
        with self.assertRaises(StructureError):
            self.preview()
        self.assertFalse((self.base / 'outside.txt').exists())

    def test_archive_change_after_preview_and_missing_approval_do_not_create_target(self):
        self.backup()
        preview = self.preview()
        with self.assertRaises(StructureError) as caught:
            archive_ops.commit_restore_project(self.destination, planRef=preview['planRef'], planHash=preview['planHash'])
        self.assertEqual(caught.exception.code, 'APPROVAL_REQUIRED')
        with self.archive.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_HASH_MISMATCH')
        self.assertFalse(self.destination.exists())

    def test_restore_failure_and_lost_reply_have_recoverable_results(self):
        self.backup()
        preview = self.preview()
        def fail_before(stage):
            if stage == 'restore-before-publication':
                raise OSError('interrupted')
        with patch.object(archive_ops, 'checkpoint', side_effect=fail_before), self.assertRaises(OSError):
            self.commit(preview)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.base.glob('.govel-restore-*')), [])
        def fail_after(stage):
            if stage == 'restore-after-publication':
                raise OSError('reply lost')
        with patch.object(archive_ops, 'checkpoint', side_effect=fail_after), self.assertRaises(OSError):
            self.commit(preview)
        result = self.commit(preview)
        self.assertTrue(result['alreadyCommitted'])
        self.assertTrue(result['historicalReceipt'])

    def test_restore_replay_rejects_damaged_receipts_but_allows_later_author_edits(self):
        self.backup()
        preview = self.preview()
        self.commit(preview)
        receipt_path = self.destination / archive_ops.RESTORE_RECEIPT
        original = json.loads(receipt_path.read_bytes())
        prose = self.destination / '正文' / (self.fixture['chapterOne'] + '.md')
        prose.write_text('恢复后作者继续修改了正文。')
        mutations = [lambda value: value.update(kind='project_create_receipt'),
                     lambda value: value.update(approved=False),
                     lambda value: value.pop('plan'),
                     lambda value: value['result'].update(code='PROJECT_CREATED'),
                     lambda value: value['result'].update(archiveHash='0' * 64),
                     lambda value: value['result'].update(projectId='other-project'),
                     lambda value: value['result'].update(preservesProjectIdentity=False),
                     lambda value: value['result'].update(changedMetadata=[])]
        for mutate in mutations:
            damaged = deepcopy(original)
            mutate(damaged)
            receipt_path.write_text(json.dumps(damaged))
            with self.subTest(receipt=damaged), self.assertRaises(StructureError) as caught:
                self.commit(preview)
            self.assertEqual(caught.exception.code, 'PROJECT_RECEIPT_INVALID')
            self.assertEqual(prose.read_text(), '恢复后作者继续修改了正文。')
        receipt_path.write_text(json.dumps(original))
        with patch('scripts.core.transport_cache.get', side_effect=AssertionError('old cache is unavailable')):
            replay = self.commit(preview)
        self.assertTrue(replay['historicalReceipt'])
        self.assertFalse(replay['currentProjectRevalidated'])

    def test_existing_restore_target_is_untouched(self):
        self.backup()
        preview = self.preview()
        self.destination.mkdir()
        (self.destination / 'note.txt').write_bytes(b'keep')
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PROJECT_TARGET_EXISTS')
        self.assertEqual((self.destination / 'note.txt').read_bytes(), b'keep')

    def test_changed_staged_body_is_not_published(self):
        self.backup()
        preview = self.preview()
        def mutate(stage):
            if stage == 'restore-extracted':
                staging = next(self.base.glob('.govel-restore-*'))
                (staging / '正文' / (self.fixture['chapterOne'] + '.md')).write_text('未经批准的变化。')
        with patch.object(archive_ops, 'checkpoint', side_effect=mutate), self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PROJECT_RESTORE_STAGING_CHANGED')
        self.assertFalse(self.destination.exists())

    def test_archive_replacement_during_restore_is_rejected_before_publication(self):
        self.backup()
        preview = self.preview()
        def replace(stage):
            if stage == 'restore-extracted':
                original = self.archive.read_bytes()
                self.archive.unlink()
                self.archive.write_bytes(original)
        with patch.object(archive_ops, 'checkpoint', side_effect=replace), self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_INPUT_CHANGED')
        self.assertFalse(self.destination.exists())

    def test_cli_backup_mcp_verify_preview_and_cli_restore_share_one_contract(self):
        repo = Path(__file__).resolve().parents[1]
        backup = subprocess.run([sys.executable, str(repo / 'scripts/novelctl.py'), 'project', 'backup',
            '--project-root', str(self.root), '--output', str(self.archive)],
            env=os.environ.copy(), cwd=repo, capture_output=True, text=True)
        self.assertEqual(backup.returncode, 0, backup.stdout + backup.stderr)
        receipt = json.loads(backup.stdout)
        arguments = {'archive': str(self.archive), 'expectedArchiveHash': receipt['archiveHash']}
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05',
                'capabilities': {}, 'clientInfo': {'name': 'project-archive-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'verify_project_backup',
                'arguments': arguments}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'preview_project_restore',
                'arguments': {**arguments, 'projectRoot': str(self.destination)}}},
        ]
        mcp = subprocess.run([sys.executable, str(repo / 'scripts/mcp_server.py')], env=os.environ.copy(), cwd=repo,
            input=''.join(json.dumps(row) + '\n' for row in requests), capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stdout + mcp.stderr)
        responses = [json.loads(line) for line in mcp.stdout.splitlines()]
        self.assertFalse(responses[-2]['result']['isError'], responses[-2])
        self.assertFalse(responses[-1]['result']['isError'], responses[-1])
        preview = responses[-1]['result']['structuredContent']
        restored = subprocess.run([sys.executable, str(repo / 'scripts/novelctl.py'), 'project', 'restore-commit',
            '--project-root', str(self.destination), '--plan-ref', preview['planRef'], '--plan-hash', preview['planHash'],
            '--approve'], env=os.environ.copy(), cwd=repo, capture_output=True, text=True)
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(json.loads(restored.stdout)['projectId'], receipt['projectId'])

    def test_configured_size_limit_is_enforced_before_backup_creation(self):
        policy = deepcopy(archive_io.workflow_policy())
        policy['maintenance']['projectArchiveMaxBytes'] = 1
        with patch.object(archive_io, 'workflow_policy', return_value=policy), self.assertRaises(StructureError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, 'PROJECT_ARCHIVE_LIMIT')
        self.assertFalse(self.archive.exists())
