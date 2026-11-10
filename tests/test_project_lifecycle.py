"""Author-approved creation creates a complete project through either transport."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core import project_lifecycle as lifecycle
from scripts.core.structure_store import StructureError, load_structure


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'book'
        self.enterContext(patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.base / 'no-provider'),
                                                  'GOVEL_LOG_MODE': 'off'}, clear=True))

    def preview(self):
        return lifecycle.preview_create_project(self.root, bookName='山城旧事')

    def commit(self, preview, **extra):
        return lifecycle.commit_create_project(self.root, planRef=preview['planRef'], planHash=preview['planHash'],
                                                approve=True, **extra)

    def test_preview_and_approved_creation_include_template_and_manual_workflow(self):
        self.root = self.base / 'books' / 'new'
        preview = self.preview()
        self.assertFalse(self.root.parent.exists())
        result = self.commit(preview)
        source = load_structure(self.root)
        self.assertEqual(source['projectId'], preview['projectId'])
        self.assertEqual(source['chapters'], [])
        self.assertEqual(result['project']['approvalMode'], 'manual')
        self.assertTrue((self.root / 'AGENTS.md').is_file())
        self.assertTrue((self.root / '.govel/workflow.json').is_file())
        from scripts.core.project_doctor import project_doctor
        self.assertTrue(project_doctor(self.root)['healthy'])

    def test_creation_requires_original_plan_and_explicit_approval(self):
        preview = self.preview()
        for arguments, code in [({'planHash': preview['planHash'], 'approve': False}, 'APPROVAL_REQUIRED'),
                                ({'planHash': '0' * 64, 'approve': True}, 'PLAN_HASH_MISMATCH')]:
            with self.assertRaises(StructureError) as caught:
                lifecycle.commit_create_project(self.root, planRef=preview['planRef'], **arguments)
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(self.root.exists())

    def test_changed_environment_requires_new_preview(self):
        preview = self.preview()
        with patch.object(lifecycle, 'environment_binding', return_value={'changed': True}), self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PROJECT_CREATE_PLAN_STALE')
        self.assertFalse(self.root.exists())

    def test_target_collision_never_overwrites_user_files(self):
        preview = self.preview()
        self.root.mkdir()
        note = self.root / 'note.txt'
        note.write_bytes(b'keep\r\n')
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PROJECT_TARGET_EXISTS')
        self.assertEqual(note.read_bytes(), b'keep\r\n')

    def test_failure_before_publication_leaves_no_partial_project(self):
        preview = self.preview()
        with patch.object(lifecycle, 'checkpoint', side_effect=OSError('staging interruption')), self.assertRaises(OSError):
            self.commit(preview)
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.base.glob('.govel-create-*')), [])
        self.assertEqual(self.commit(preview)['projectId'], preview['projectId'])

    def test_lost_reply_can_replay_without_recreating_or_overwriting(self):
        preview = self.preview()
        def fail(stage):
            if stage == 'create-after-publication':
                raise OSError('reply lost')
        with patch.object(lifecycle, 'checkpoint', side_effect=fail), self.assertRaises(OSError):
            self.commit(preview)
        (self.root / 'AGENTS.md').write_text('作者后续的项目约束。\n')
        result = self.commit(preview)
        self.assertTrue(result['alreadyCommitted'])
        self.assertTrue(result['historicalReceipt'])
        self.assertEqual((self.root / 'AGENTS.md').read_text(), '作者后续的项目约束。\n')

    def test_incomplete_or_inconsistent_completion_receipt_cannot_claim_success(self):
        preview = self.preview()
        self.commit(preview)
        path = self.root / lifecycle.CREATE_RECEIPT
        original = json.loads(path.read_bytes())
        source = (self.root / '.govel/structure.json').read_bytes()
        mutations = [lambda value: value.update(kind='project_restore_receipt'),
                     lambda value: value.update(approved=False),
                     lambda value: value.pop('schemaVersion'),
                     lambda value: value.update(result={'ok': True}),
                     lambda value: value['result'].update(code='PROJECT_RESTORED'),
                     lambda value: value['result'].update(projectId='different-project'),
                     lambda value: value['result']['project'].update(bookName='different-book'),
                     lambda value: value['plan'].update(bookName='changed-preview')]
        for mutate in mutations:
            damaged = deepcopy(original)
            mutate(damaged)
            path.write_text(json.dumps(damaged))
            with self.subTest(receipt=damaged), self.assertRaises(StructureError) as caught:
                self.commit(preview)
            self.assertEqual(caught.exception.code, 'PROJECT_RECEIPT_INVALID')
            self.assertEqual((self.root / '.govel/structure.json').read_bytes(), source)
        path.write_text(json.dumps(original))
        with patch('scripts.core.transport_cache.get', side_effect=AssertionError('old cache is unavailable')):
            self.assertTrue(self.commit(preview)['historicalReceipt'])

    def test_removed_changed_or_added_staged_files_are_not_published(self):
        preview = self.preview()
        def change(stage, action):
            if stage != 'create-before-publication':
                return
            staging = next(self.base.glob('.govel-create-*'))
            if action == 'remove':
                (staging / '.govel/workflow.json').unlink()
            elif action == 'change':
                (staging / '.govel/structure.json').write_text('{}')
            else:
                (staging / 'unplanned.txt').write_text('unexpected')
        for action in ('remove', 'change', 'add'):
            with self.subTest(action=action), patch.object(lifecycle, 'checkpoint', side_effect=lambda stage: change(stage, action)), \
                 self.assertRaises(StructureError) as caught:
                self.commit(preview)
            self.assertEqual(caught.exception.code, 'PROJECT_CREATE_STAGING_CHANGED')
            self.assertFalse(self.root.exists())
            self.assertEqual(list(self.base.glob('.govel-create-*')), [])
        self.assertEqual(self.commit(preview)['projectId'], preview['projectId'])

    def test_legacy_initializer_preserves_its_idempotency_and_empty_directory_contract(self):
        self.root.mkdir()
        args = {'project_id': 'project-fixture', 'book_name': '山城旧事', 'idempotency_key': 'init-fixture',
                'approval_mode': 'manual'}
        marker, created, _ = lifecycle.initialize_project(self.root, **args)
        self.assertTrue(created)
        replay, created, _ = lifecycle.initialize_project(self.root, **args)
        self.assertFalse(created)
        self.assertEqual(marker, replay)

    def test_cli_preview_commits_through_real_stdio_mcp(self):
        repo = Path(__file__).resolve().parents[1]
        cli = subprocess.run([sys.executable, str(repo / 'scripts/novelctl.py'), 'project', 'create-preview',
                              '--project-root', str(self.root), '--book-name', '山城旧事'],
                             env=os.environ.copy(), cwd=repo, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        preview = json.loads(cli.stdout)
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05',
                'capabilities': {}, 'clientInfo': {'name': 'project-lifecycle-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'commit_project_creation',
                'arguments': preview['commitAction']['arguments']}},
        ]
        mcp = subprocess.run([sys.executable, str(repo / 'scripts/mcp_server.py')], env=os.environ.copy(),
                             cwd=repo, input=''.join(json.dumps(row) + '\n' for row in messages),
                             capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stdout + mcp.stderr)
        response = json.loads(mcp.stdout.splitlines()[-1])['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent']['projectId'], preview['projectId'])
        self.assertEqual(load_structure(self.root)['projectId'], preview['projectId'])
