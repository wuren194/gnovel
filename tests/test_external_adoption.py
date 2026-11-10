"""Native-confirmed external adoption; all native dialogs are replaced in tests."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import mcp_server
from scripts.core import external_approval
from scripts.core.external_edit_operations import commit_external_edit, read_external_edit
from scripts.core.finalization import finalize_chapter, preview_accept_external_modification
from scripts.core.jsonio import read_json, write_bytes, write_json
from scripts.core.operations import operations
from scripts.core.prose_binding import observe_chapter
from scripts.core.structure_commands import _operation_paths, _plan_hash, commit_plan
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.write_sets import digest, inventory, json_bytes
from tests.cli_harness import cli_json
from tests.fixtures_workflow import review_ready_project


ROOT = Path(__file__).resolve().parents[1]


class ExternalAdoptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / '外改测试'
        native = patch('scripts.core.external_approval._confirm_on_device', return_value=True)
        self.confirm = native.start()
        self.addCleanup(native.stop)
        environment = patch.dict(os.environ, {'GOVEL_LOG_MODE': 'off'})
        environment.start()
        self.addCleanup(environment.stop)
        fixture = review_ready_project(self.root)
        chapter = fixture['structure']['chapters'][0]
        self.cid = chapter['chapterId']
        finalize_chapter(self.root, self.cid, skip_title=True,
                         binding_generation=chapter['proseBinding']['generation'])
        self.initial = load_structure(self.root)
        self.chapter = next(c for c in self.initial['chapters'] if c['chapterId'] == self.cid)
        self.path = self.root / self.chapter['filePath']
        self.h1 = self.path.read_bytes()
        self.h2 = self.h1 + '\n他检查了门锁。\n'.encode()
        self.path.write_bytes(self.h2)
        self.workflow = read_json(self.root / '.govel/workflow.json')

    def preview(self, request_id='external-adopt-1'):
        return preview_accept_external_modification(self.root, chapter_id=self.cid, request_id=request_id)

    def stored(self, report):
        return read_json(_operation_paths(self.root, report['operationId'])[0])

    def commit(self, report, **kwargs):
        return commit_external_edit(self.root, operationId=report['operationId'], planHash=report['planHash'],
                                    approve=True, confirmOnDevice=True, **kwargs)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(StructureError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and p != self.root / '.govel/structure.lock'}

    def assert_metadata_unchanged(self):
        self.assertEqual(load_structure(self.root), self.initial)
        self.assertEqual(read_json(self.root / '.govel/workflow.json'), self.workflow)

    def issue(self, plan):
        token = external_approval.issue_local_approval(self.root, plan)
        self.addCleanup(external_approval.discard_local_approval, token)
        return token

    def rewrite_plan(self, plan, outputs=None):
        """Attack fixture: recompute ordinary hashes; they still are not authority."""
        if outputs:
            by_path = {r['path']: r for r in plan['writeSet']}
            for relative, data in outputs.items():
                staged = self.root / f".govel/plans/{plan['operationId']}.files" / relative
                write_bytes(staged, data)
                if relative in by_path:
                    by_path[relative]['after']['sha256'] = digest(data)
                else:
                    plan['writeSet'].extend(inventory(self.root, {relative: data}))
        plan['planHash'] = _plan_hash({k: v for k, v in plan.items() if k != 'planHash'})
        write_json(_operation_paths(self.root, plan['operationId'])[0], plan)
        return {'operationId': plan['operationId'], 'planHash': plan['planHash']}

    def test_confirmed_adoption_updates_only_binding_and_invalidates_old_prose_reviews(self):
        preview = self.preview()
        inode = self.path.stat().st_ino
        result = self.commit(preview)
        current = load_structure(self.root)
        chapter = next(c for c in current['chapters'] if c['chapterId'] == self.cid)
        self.assertEqual(current['revision'], self.initial['revision'] + 1)
        self.assertEqual(chapter['status'], 'stale')
        self.assertEqual(chapter['proseBinding']['generation'], current['revision'])
        self.assertEqual(chapter['proseBinding']['expectedContentDigest'], 'sha256:' + digest(self.h2))
        self.assertEqual(observe_chapter(self.root, current, chapter)['state'], 'OWNED_CLEAN')
        self.assertEqual(self.path.read_bytes(), self.h2)
        self.assertEqual(self.path.stat().st_ino, inode)
        for collection in ('entities', 'attributes', 'fragments', 'relations', 'references', 'chapterArcLinks'):
            self.assertEqual(current[collection], self.initial[collection])
        workflow = read_json(self.root / '.govel/workflow.json')
        self.assertEqual(workflow.get('continuity'), self.workflow.get('continuity'))
        approval = workflow['approvals'][self.cid]
        self.assertFalse(approval['approved'])
        self.assertTrue(approval['finalized'])
        self.assertEqual(approval['titleDecision'], self.workflow['approvals'][self.cid]['titleDecision'])
        self.assertNotIn('artifactHashes', approval)
        self.assertEqual(approval['invalidatedBy'], 'prose_external_modification')
        for kind, item in workflow['artifacts'][self.cid].items():
            if kind == 'stage_a_review':
                self.assertEqual(item, self.workflow['artifacts'][self.cid][kind])
            else:
                self.assertEqual(item['invalidatedBy'], 'prose_external_modification')
        self.assertEqual(workflow['chapterStates'][self.cid]['status'], 'stale')
        self.confirm.assert_called_once()
        message = self.confirm.call_args.args[0]
        for expected in (self.cid, str(self.root), preview['planHash'], 'sha256:' + digest(self.h2)):
            self.assertIn(expected, message)
        journal = read_json(_operation_paths(self.root, preview['operationId'])[1])
        self.assertEqual(journal['authorConfirmation']['channel'], 'macos-native-confirmation')
        self.assertTrue(journal['authorConfirmation']['oneUse'])
        historical = read_external_edit(self.root, operationId=preview['operationId'],
                                        planHash=preview['planHash'], part='H2')
        self.assertTrue(historical['historical'])
        self.assertEqual(historical['text'].encode('utf-8'), self.h2)
        self.assertNotIn('nonce', journal['authorConfirmation'])
        self.assertNotIn(self.chapter['filePath'], journal['changedFiles'])
        self.assertTrue(result['ok'])

    def test_cancelled_confirmation_leaves_project_and_plan_bytes_unchanged(self):
        preview = self.preview()
        before = self.snapshot()
        self.confirm.return_value = False
        self.assert_code('APPROVAL_CANCELLED', self.commit, preview)
        self.assertEqual(self.snapshot(), before)
        self.confirm.assert_called_once()

    def test_plain_approval_flags_cannot_mint_or_bypass_local_confirmation(self):
        preview = self.preview()
        before = self.snapshot()
        for approved, confirmation in ((False, False), (True, False), (False, True)):
            self.assert_code('APPROVAL_CAPABILITY_REQUIRED', commit_external_edit, self.root,
                operationId=preview['operationId'], planHash=preview['planHash'],
                approve=approved, confirmOnDevice=confirmation)
        self.assert_code('APPROVAL_CAPABILITY_REQUIRED', commit_plan, self.root,
            operation_id=preview['operationId'], plan_hash=preview['planHash'], approved=True)
        self.confirm.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_h3_written_while_author_confirms_is_preserved_and_never_adopted(self):
        preview = self.preview()
        h3 = self.h2 + '作者继续修改。'.encode()
        def confirm(message):
            self.path.write_bytes(h3)
            return True
        self.confirm.side_effect = confirm
        self.assert_code('PROSE_CONTENT_CHANGED', self.commit, preview)
        self.assert_metadata_unchanged()
        self.assertEqual(self.path.read_bytes(), h3)
        self.assertEqual((self.root / self.stored(preview)['externalReviewMaterials']['H2']['path']).read_bytes(), self.h2)

    def test_replaced_same_content_inode_while_confirming_is_rejected(self):
        preview = self.preview()
        old_inode = self.path.stat().st_ino
        def confirm(message):
            replacement = self.base / 'replacement.md'
            replacement.write_bytes(self.h2)
            os.replace(replacement, self.path)
            return True
        self.confirm.side_effect = confirm
        self.assert_code('TRANSACTION_GUARD_DRIFT', self.commit, preview)
        self.assert_metadata_unchanged()
        self.assertEqual(self.path.read_bytes(), self.h2)
        self.assertNotEqual(self.path.stat().st_ino, old_inode)

    def test_h3_written_during_transaction_rolls_back_metadata_without_rewriting_prose(self):
        preview = self.preview()
        h3 = self.h2 + '另一处外改。'.encode()
        def interfere(phase, relative=None):
            if phase == 'write' and relative == '.govel/structure.json':
                self.path.write_bytes(h3)
        with patch('scripts.core.write_sets.checkpoint', side_effect=interfere):
            self.assert_code('TRANSACTION_GUARD_DRIFT', self.commit, preview)
        self.assert_metadata_unchanged()
        self.assertEqual(self.path.read_bytes(), h3)

    def test_capability_expires_and_is_consumed_once(self):
        plan = self.stored(self.preview())
        expired = self.issue(plan)
        with patch('scripts.core.external_approval.time.monotonic', return_value=expired.expires + 1):
            self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                             plan, expired, root=self.root)
        self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                         plan, expired, root=self.root)
        valid = self.issue(plan)
        witness = external_approval.consume_local_approval(plan, valid, root=self.root)
        self.assertTrue(witness['oneUse'])
        self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                         plan, valid, root=self.root)
        self.assert_metadata_unchanged()

    def test_copied_token_or_ordinary_data_cannot_replace_issued_capability(self):
        plan = self.stored(self.preview())
        token = self.issue(plan)
        self.assert_code('APPROVAL_CAPABILITY_REQUIRED', external_approval.consume_local_approval,
                         plan, {'nonce': token.nonce, 'binding': token.binding}, root=self.root)
        self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                         plan, replace(token), root=self.root)
        self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                         plan, token, root=self.root)
        self.assert_metadata_unchanged()

    def test_capability_is_bound_to_root_plan_and_observed_object(self):
        plan = self.stored(self.preview())
        token = self.issue(plan)
        self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                         plan, token, root=self.base / 'other-book')
        for mutate in ('planHash', 'observedObject'):
            token = self.issue(plan)
            forged = deepcopy(plan)
            if mutate == 'planHash':
                forged['planHash'] = '0' * 64
            else:
                forged['externalReviewMaterials']['observedObject']['ino'] += 1
            self.assert_code('APPROVAL_CAPABILITY_INVALID', external_approval.consume_local_approval,
                             forged, token, root=self.root)
        self.assert_metadata_unchanged()

    def test_forged_structure_changes_are_rejected_before_native_confirmation(self):
        preview = self.preview()
        plan = self.stored(preview)
        plan['proposedStructure']['bookName'] = '偷偷改了书名'
        forged = self.rewrite_plan(plan, {'.govel/structure.json': json_bytes(plan['proposedStructure'])})
        self.assert_code('EXTERNAL_ADOPTION_SCOPE_INVALID', self.commit, forged)
        self.confirm.assert_not_called()
        self.assert_metadata_unchanged()

    def test_extra_prose_write_is_rejected_before_native_confirmation(self):
        plan = self.stored(self.preview())
        extra = '正文/夹带写入.md'
        forged = self.rewrite_plan(plan, {extra: '不属于外改采用的正文。'.encode()})
        self.assert_code('EXTERNAL_ADOPTION_SCOPE_INVALID', self.commit, forged)
        self.confirm.assert_not_called()
        self.assertFalse((self.root / extra).exists())
        self.assert_metadata_unchanged()

    def test_removed_live_guard_is_not_authorized_by_rehashing_the_plan(self):
        plan = self.stored(self.preview())
        plan['externalReviewGuards'] = [row for row in plan['externalReviewGuards'] if row['path'] != self.chapter['filePath']]
        forged = self.rewrite_plan(plan)
        with self.assertRaises(StructureError) as caught:
            self.commit(forged)
        self.assertIn(caught.exception.code, {'SOURCE_REQUEST_STALE', 'EXTERNAL_REVIEW_MATERIAL_INVALID'})
        self.confirm.assert_not_called()
        self.assert_metadata_unchanged()

    def test_frozen_material_tampering_and_expired_plan_do_not_prompt(self):
        preview = self.preview()
        plan = self.stored(preview)
        with patch('scripts.core.source_requests.time.time', return_value=plan['expiresAtEpoch'] + 1):
            self.assert_code('SOURCE_REQUEST_STALE', self.commit, preview)
        frozen = self.root / plan['externalReviewMaterials']['H2']['path']
        frozen.write_text('被更换的冻结正文。')
        self.assert_code('SOURCE_REQUEST_STALE', self.commit, preview)
        self.confirm.assert_not_called()
        self.assert_metadata_unchanged()

    def test_successful_adoption_replays_without_reconfirmation_or_new_revision(self):
        preview = self.preview()
        self.commit(preview)
        before = self.snapshot()
        self.confirm.reset_mock()
        result = self.commit(preview)
        self.assertEqual(result['code'], 'EXTERNAL_ALREADY_ADOPTED')
        self.assertTrue(result['alreadyCommitted'])
        self.assertTrue(result['historical'])
        self.confirm.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_fabricated_commit_journal_cannot_claim_historical_adoption(self):
        preview = self.preview()
        _, journal, _ = _operation_paths(self.root, preview['operationId'])
        write_json(journal, {'schemaVersion': 4, 'operationId': preview['operationId'],
            'planHash': preview['planHash'], 'operation': 'accept_external_modification',
            'authorConfirmation': {'channel': 'macos-native-confirmation'}})
        self.assert_code('OPERATION_RECEIPT_INVALID', self.commit, preview)
        self.confirm.assert_not_called()
        self.assert_metadata_unchanged()

    def test_cli_and_mcp_commit_share_the_same_confirmation_and_receipt(self):
        preview = self.preview()
        code, first = cli_json(['project', 'workflow', 'external-commit', '--project-root', str(self.root),
            '--operation-id', preview['operationId'], '--plan-hash', preview['planHash'], '--approve', '--confirm-on-device'])
        self.assertEqual(code, 0, first)
        self.confirm.assert_called_once()
        response = mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'accept_external_manuscript_edit', 'arguments': {'projectRoot': str(self.root),
                'operationId': preview['operationId'], 'planHash': preview['planHash'], 'approve': True,
                'confirmOnDevice': True}}})['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent']['code'], 'EXTERNAL_ALREADY_ADOPTED')
        self.confirm.assert_called_once()
        spec = next(s for s in operations() if s.id == 'workflow.external-commit')
        tool = next(t for t in mcp_server.TOOLS if t['name'] == spec.name)
        self.assertEqual(tool['inputSchema'], spec.input_schema)
        from scripts.novelctl import build_parser
        parsed = build_parser().parse_args(['project', *spec.cli, '--project-root', str(self.root),
            '--operation-id', preview['operationId'], '--plan-hash', preview['planHash'], '--approve', '--confirm-on-device'])
        self.assertEqual(parsed.operation_binding, spec.id)
        self.assertTrue(parsed.approve)
        self.assertTrue(parsed.confirmOnDevice)

    def test_real_process_preview_and_read_never_need_native_confirmation(self):
        environment = dict(os.environ, GOVEL_LOG_MODE='off', PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'workflow', 'external-preview',
            '--project-root', str(self.root), '--chapter-id', self.cid, '--request-id', 'external-process-1'],
            cwd=self.base, env=environment, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        preview = json.loads(process.stdout)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'read_external_manuscript_edit', 'arguments': {'projectRoot': str(self.root),
                'operationId': preview['operationId'], 'planHash': preview['planHash'], 'part': 'H2'}}}
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/mcp_server.py')], input=json.dumps(request) + '\n',
            cwd=self.base, env=environment, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent']['text'].encode(), self.h2)
        self.confirm.assert_not_called()
        self.assert_metadata_unchanged()


if __name__ == '__main__':
    unittest.main()
