from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts import state_cache
from tests.fixtures_current import initialize_project, create_schema5_project
from tests.fixtures_transaction import interrupted_write_set
from scripts.core import finalization, locks
from scripts.core import structure_commands as commands
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_store import StructureError, empty_structure, load_structure, save_structure
from scripts.core.workflow import WorkflowError
from scripts.dashboard_server import DashboardHandler
from scripts.mcp_server import call_tool


def snapshot(root: Path) -> dict:
    result = {}
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", str(path.readlink()))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory",)
    return result


class TransactionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "book"
        fixture = create_schema5_project(self.root, book_name='safety', chapter_count=1)
        self.cid = fixture['chapterIds'][0]
        self.tokens = {}

    def preview(self) -> dict:
        return commands.preview_create_time_anchor(self.root, name="anchor", absolute_time="2000-01-01")

    def insert(self) -> dict:
        structure = load_structure(self.root)
        return commands.preview_insert_chapter(
            self.root, title="chapter", volume_id=structure["volumes"][0]["volumeId"],
            primary_arc_id=structure["arcs"][0]["arcId"],
        )

    def commit(self, plan: dict) -> dict:
        result = commands.commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        chapter_id = (result.get('chapter') or {}).get('chapterId') or result.get('identities', {}).get('chapterId')
        chapter = next((c for c in load_structure(self.root)['chapters'] if c['chapterId'] == chapter_id), None)
        if chapter:
            self.tokens[result['operationId']] = chapter['proseBinding']['generation']
        return read_json(self.root/f".govel/operations/{result['operationId']}.json") | {
            'code': result['code'], 'ok': True, **({'chapter': chapter} if chapter else {})}

    def move(self):
        from scripts.core.refactor import preview_relocate_chapter_paths
        return self.commit(preview_relocate_chapter_paths(self.root, chapter_id=self.cid, file_path='正文/moved.md'))

    def undo(self, operation_id):
        return commands.undo_operation(self.root, operation_id, binding_generation=self.tokens.get(operation_id))

    def assert_rejected_unchanged(self, action, code: str, *, before_lock: bool = False) -> None:
        before = snapshot(self.base)
        with patch.object(locks, "_acquire", wraps=locks._acquire) as acquire:
            with self.assertRaises((StructureError, WorkflowError)) as caught:
                action()
            self.assertEqual(caught.exception.code, code)
            if before_lock:
                acquire.assert_not_called()
        self.assertEqual(snapshot(self.base), before)

    def test_undo_preserves_manually_edited_prose(self) -> None:
        result = self.move(); prose = self.root/result['chapter']['filePath']
        prose.write_text('USER AUTHORED DATA\n')
        self.assertFalse(next(r for r in commands.list_operations(self.root) if r['operationId'] == result['operationId'])['undoable'])
        self.assert_rejected_unchanged(lambda: self.undo(result['operationId']), 'PROSE_EXTERNAL_MODIFICATION')

    def test_undo_rejects_newer_authored_source_and_prose(self) -> None:
        result = self.move()
        self.assertEqual(set(result['afterFiles']), set(result['backupFiles']))
        source_path = self.root/'.govel/structure.json'; source_bytes = source_path.read_bytes()
        source = read_json(source_path); source['bookName'] = 'new authored name'; write_json(source_path, source)
        self.assert_rejected_unchanged(lambda: self.undo(result['operationId']), 'UNDO_NOT_CHAIN_TOP')
        source_path.write_bytes(source_bytes)
        body = self.root/result['chapter']['filePath']; body_bytes = body.read_bytes()
        body.write_bytes(body_bytes+b'new author work')
        self.assert_rejected_unchanged(lambda: self.undo(result['operationId']), 'PROSE_EXTERNAL_MODIFICATION')
        body.write_bytes(body_bytes)
        self.assertTrue(next(r for r in commands.list_operations(self.root) if r['operationId'] == result['operationId'])['undoable'])

    def test_undo_checks_deleted_and_previously_absent_targets(self) -> None:
        result = self.move(); target = self.root/result['chapter']['filePath']; original = target.read_bytes()
        target.unlink()
        self.assert_rejected_unchanged(lambda: self.undo(result['operationId']), 'PROSE_LOCATOR_DRIFT')
        target.write_bytes(original)
        old_path = next(row['sourcePath'] for row in result['pathMoves'] if row['field'] == 'filePath')
        (self.root/old_path).write_text('FOREIGN REPLACEMENT')
        self.assert_rejected_unchanged(lambda: self.undo(result['operationId']), 'PROSE_TARGET_NOT_OWNED')

    def test_unchanged_undo_reverses_only_its_own_prose_move(self) -> None:
        result = self.move(); target = self.root/result['chapter']['filePath']
        other = self.root/'工作台/unrelated.md'; other.write_text('retain')
        undone = self.undo(result['operationId'])
        self.assertEqual(undone['code'], 'STRUCTURE_UNDONE')
        self.assertFalse(target.exists()); self.assertEqual(other.read_text(), 'retain')
        self.assertEqual([c['chapterId'] for c in load_structure(self.root)['chapters']], [self.cid])

    def test_two_consecutive_undos_are_supported(self) -> None:
        """连续撤销两步（缺陷 10）：undo 只递增 revision，不得让更早的操作报冲突。"""
        first = self.commit(self.preview())
        from scripts.core.refactor import preview_rename_volume
        volume = load_structure(self.root)['volumes'][0]
        second = self.commit(preview_rename_volume(self.root, volume_id=volume['volumeId'], new_title='新卷名'))
        self.assertEqual(self.undo(second["operationId"])["code"], "STRUCTURE_UNDONE")
        undoable = {row["operationId"]: row["undoable"] for row in commands.list_operations(self.root)}
        self.assertTrue(undoable[first["operationId"]], "撤销一步后，更早的操作仍应可撤销")
        self.assertEqual(self.undo(first["operationId"])["code"], "STRUCTURE_UNDONE")
        structure = load_structure(self.root)
        self.assertFalse(any(e['entityType'] == 'time_anchor' for e in structure['entities']))
        self.assertEqual([c['chapterId'] for c in structure['chapters']], [self.cid])

    def test_undo_after_newer_content_edits_still_conflicts(self) -> None:
        """连续撤销的放宽只认「内容等价、revision 不同」：内容真的变了仍必须拦。"""
        first = self.commit(self.preview())
        from scripts.core.refactor import preview_rename_volume
        volume = load_structure(self.root)['volumes'][0]
        second = self.commit(preview_rename_volume(self.root, volume_id=volume['volumeId'], new_title='新卷名'))
        self.undo(second["operationId"])
        structure = load_structure(self.root)
        structure["bookName"] = "手工改名"
        save_structure(self.root, structure)
        self.assert_rejected_unchanged(
            lambda: self.undo(first["operationId"]), "UNDO_NOT_CHAIN_TOP",
        )

    def test_legacy_journal_is_not_automatically_undoable(self) -> None:
        result = self.move()
        journal_path = self.root / f".govel/operations/{result['operationId']}.json"
        for version in (1, 2):
            with self.subTest(version=version):
                journal = read_json(journal_path)
                journal["schemaVersion"] = version
                journal.pop("afterFiles", None)
                journal.pop("beforeFiles", None)
                write_json(journal_path, journal)
                self.assertFalse(commands.list_operations(self.root)[0]["undoable"])
                self.assert_rejected_unchanged(lambda: self.undo(result["operationId"]), "OWNERSHIP_MODEL_UNSUPPORTED_OPERATION")

    def test_undo_missing_or_damaged_backup_never_partially_restores(self) -> None:
        result = self.move()
        source = self.root / result["backupPath"] / "structure.json"
        original = source.read_bytes()
        source.write_bytes(b"damaged")
        self.assert_rejected_unchanged(lambda: self.undo(result["operationId"]), "BACKUP_HASH_MISMATCH")
        source.unlink()
        self.assert_rejected_unchanged(lambda: self.undo(result["operationId"]), "BACKUP_MISSING")
        source.write_bytes(original)
        self.assertTrue(commands.list_operations(self.root)[0]["undoable"])

    def test_pending_finalization_blocks_all_structure_writers(self) -> None:
        result = self.move()
        plan = self.preview()
        tx = interrupted_write_set(self.root, {'.govel/structure.json': (self.root/'.govel/structure.json').read_bytes()})
        actions = (
            lambda: self.commit(plan),
            lambda: self.undo(result["operationId"]),
            self.preview,
            lambda: commands.gc_project(self.root, keep_backups=0),
            lambda: advance_chapter_status(self.root, result["chapter"]["chapterId"], "stale"),
            lambda: finalization.finalize_chapter(self.root, result["chapter"]["chapterId"], skip_title=True, binding_generation=self.tokens[result["operationId"]]),
            lambda: finalization.reconcile_workflow(self.root, persist=True),
        )
        for action in actions:
            self.assert_rejected_unchanged(action, "TRANSACTION_INCOMPLETE")
        self.assertEqual(finalization.recover_transactions(self.root, apply=True)["code"], "TRANSACTION_RECOVERED")
        self.assertTrue((self.root/f".govel/transactions/{tx['transactionId']}/completed.json").is_file())
        # 恢复未改当前源字节；同一冻结计划仍可提交。
        self.assertEqual(self.commit(plan)['code'], 'STRUCTURE_COMMITTED')

    def test_pending_check_happens_after_acquiring_shared_lock(self) -> None:
        from scripts.core import locks
        plan = self.preview()
        acquire = locks._acquire
        def acquire_and_interrupt(path):
            fd = acquire(path)
            interrupted_write_set(self.root, {'.govel/structure.json': (self.root/'.govel/structure.json').read_bytes()}, lock_held=True)
            return fd
        original = (self.root / ".govel/structure.json").read_bytes()
        with patch.object(locks, "_acquire", side_effect=acquire_and_interrupt):
            with self.assertRaises(StructureError) as caught:
                self.commit(plan)
        self.assertEqual(caught.exception.code, "TRANSACTION_INCOMPLETE")
        self.assertEqual((self.root / ".govel/structure.json").read_bytes(), original)
        self.assertFalse(list((self.root / ".govel/backups").iterdir()))

    def test_recovery_preflights_all_backups_before_restoring_any_target(self) -> None:
        first, second, created = (self.root / "工作台" / name for name in ("first", "second", "created"))
        first.write_bytes(b"first-before")
        second.write_bytes(b"second-before")
        tx = interrupted_write_set(self.root, {
            first.relative_to(self.root).as_posix(): b'first-after',
            second.relative_to(self.root).as_posix(): b'second-after',
            created.relative_to(self.root).as_posix(): b'created-after'},
            phase='write', relative=second.relative_to(self.root).as_posix())
        source = self.root / f".govel/transactions/{tx['transactionId']}/before" / second.relative_to(self.root)
        original = source.read_bytes()
        for mode in ("missing", "damaged"):
            with self.subTest(mode=mode):
                if mode == "missing":
                    source.unlink()
                else:
                    source.write_bytes(b"corrupt")
                self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True), "TRANSACTION_RECOVERY_BLOCKED")
                self.assertEqual(len(finalization.pending_transactions(self.root)), 1)
        source.write_bytes(original)
        finalization.recover_transactions(self.root, apply=True)
        self.assertEqual(first.read_bytes(), b"first-before")
        self.assertEqual(second.read_bytes(), b"second-before")
        self.assertFalse(created.exists())

    def test_recovery_keeps_unreadable_and_identity_mismatched_manifests(self) -> None:
        path = self.root / ".govel/transactions/TX-broken.json"
        path.parent.mkdir(exist_ok=True)
        for content in ("not json", "[]", '{"transactionId": "TX-other", "files": []}'):
            path.write_text(content, encoding="utf-8")
            self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True), "TRANSACTION_RECOVERY_BLOCKED")

    def test_recovery_preflights_all_transactions(self) -> None:
        target = self.root / "工作台/first"
        target.write_bytes(b"original")
        interrupted_write_set(self.root, {target.relative_to(self.root).as_posix(): b'changed'},
                              phase='write', relative=target.relative_to(self.root).as_posix())
        write_json(self.root / ".govel/transactions/TX-Z.json", {"transactionId": "TX-Z", "files": []})
        self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True), "TRANSACTION_RECOVERY_BLOCKED")

    def test_recovery_refuses_external_edits_after_interruption(self) -> None:
        target = self.root/'工作台/edited'
        target.write_bytes(b'before')
        interrupted_write_set(self.root, {'工作台/edited': b'publisher-after'}, phase='write', relative='工作台/edited')
        target.write_bytes(b'author-edited-after-interruption')
        self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True),
                                       'TRANSACTION_RECOVERY_BLOCKED')
        self.assertEqual(target.read_bytes(), b'author-edited-after-interruption')

    def test_recovery_refuses_unsafe_paths_and_symlinked_before_images(self) -> None:
        target = self.root/'工作台/guarded'
        target.write_bytes(b'before')
        tx = interrupted_write_set(self.root, {'工作台/guarded': b'after'}, phase='write', relative='工作台/guarded')
        path = self.root/f".govel/transactions/{tx['transactionId']}.json"
        original = path.read_bytes()
        bad = read_json(path); bad['files'][0]['path'] = '../outside'
        write_json(path, bad)
        self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True),
                                       'PATH_OUTSIDE_PROJECT')
        path.write_bytes(original)
        backup = self.root/f".govel/transactions/{tx['transactionId']}/before/工作台/guarded"
        outside = self.base/'outside-backup'
        backup.rename(outside)
        backup.symlink_to(outside)
        self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True),
                                       'SYMLINK_PATH_FORBIDDEN')
        self.assertEqual(outside.read_bytes(), b'before')

    def test_recovery_rejects_other_protocol_versions_without_conversion(self) -> None:
        target = self.root/'工作台/protocol'
        target.write_bytes(b'before')
        tx = interrupted_write_set(self.root, {'工作台/protocol': b'after'}, phase='write', relative='工作台/protocol')
        path = self.root/f".govel/transactions/{tx['transactionId']}.json"
        tx['schemaVersion'] = 1
        write_json(path, tx)
        self.assert_rejected_unchanged(lambda: finalization.recover_transactions(self.root, apply=True),
                                       'TRANSACTION_RECOVERY_BLOCKED')

    def test_invalid_operation_ids_rejected_before_any_write(self) -> None:
        plan = self.preview()
        for operation_id in ("../../../outside/OP-COPIED", "/tmp/OP-COPIED", "OP-", "OP-123", "OP-" + "A" * 16 + "/../x", "OP-" + "a" * 16, "OP-" + "A" * 16 + "\n"):
            with self.subTest(operation_id=operation_id):
                self.assert_rejected_unchanged(lambda: commands.commit_plan(self.root, operation_id=operation_id, plan_hash=plan["planHash"], approved=True), "OPERATION_ID_INVALID", before_lock=True)
                self.assert_rejected_unchanged(lambda: self.undo(operation_id), "OPERATION_ID_INVALID", before_lock=True)

    def test_mcp_traversal_cannot_write_or_remove_external_plan(self) -> None:
        plan = self.preview()
        outside = self.base / "outside"
        outside.mkdir()
        shutil.copy2(self.root / f".govel/plans/{plan['operationId']}.json", outside / "OP-COPIED.json")
        self.assert_rejected_unchanged(lambda: call_tool("commit_structure_plan", {
            "projectRoot": str(self.root), "operationId": "../../../outside/OP-COPIED",
            "planHash": plan["planHash"], "approve": True,
        }), "OPERATION_ID_INVALID", before_lock=True)

    def test_plan_and_journal_ids_must_match_requested_id(self) -> None:
        plan = self.preview()
        copied_id = "OP-" + "F" * 16
        copied = self.root / f".govel/plans/{copied_id}.json"
        shutil.copy2(self.root / f".govel/plans/{plan['operationId']}.json", copied)
        self.assert_rejected_unchanged(lambda: commands.commit_plan(self.root, operation_id=copied_id, plan_hash=plan["planHash"], approved=True), "OPERATION_ID_MISMATCH", before_lock=True)
        self.commit(plan)
        shutil.copy2(self.root / f".govel/operations/{plan['operationId']}.json", self.root / f".govel/operations/{copied_id}.json")
        self.assert_rejected_unchanged(lambda: self.undo(copied_id), "OPERATION_ID_MISMATCH", before_lock=True)

    def test_commit_rejects_symlinks_in_all_control_and_target_paths(self) -> None:
        plan = self.insert()
        relatives = [
            f".govel/plans/{plan['operationId']}.json", '.govel/plans',
            '.govel/operations', f".govel/operations/{plan['operationId']}.json",
            '.govel/backups', f".govel/backups/{plan['operationId']}",
            '.govel/structure.json', '.govel/workflow.json', '.govel/source-review.json',
        ]
        for relative in relatives:
            for external in (False, True):
                with self.subTest(relative=relative, external=external):
                    path = self.root / relative
                    parent_existed = path.parent.exists()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    moved = (self.base if external else self.root) / "link-target"
                    existed = path.exists()
                    if existed:
                        path.rename(moved)
                    else:
                        moved.write_bytes(b"sentinel")
                    path.symlink_to(moved)
                    self.assert_rejected_unchanged(lambda: self.commit(plan), "SYMLINK_PATH_FORBIDDEN", before_lock=True)
                    path.unlink()
                    if existed:
                        moved.rename(path)
                    else:
                        moved.unlink()
                    if not parent_existed:
                        path.parent.rmdir()

    def test_undo_rejects_unsafe_journal_paths_and_symlinks(self) -> None:
        result = self.move()
        operation_id = result["operationId"]
        journal_path = self.root / f".govel/operations/{operation_id}.json"
        original = read_json(journal_path)
        journal = read_json(journal_path)
        journal["backupPath"] = "../../../outside"
        write_json(journal_path, journal)
        self.assert_rejected_unchanged(lambda: self.undo(operation_id), "BACKUP_PATH_INVALID")
        for relative in ("../../../outside", str(self.base / "outside")):
            journal = dict(original)
            journal["backupFiles"] = [*original["backupFiles"], relative]
            journal["afterFiles"] = {**original["afterFiles"], relative: {"exists": False, "sha256": None}}
            journal["beforeFiles"] = {**original["beforeFiles"], relative: {"exists": False, "sha256": None}}
            write_json(journal_path, journal)
            self.assert_rejected_unchanged(lambda: self.undo(operation_id), "ABSOLUTE_PATH_FORBIDDEN" if relative.startswith("/") else "PATH_OUTSIDE_PROJECT")
        write_json(journal_path, original)
        relatives = [str(journal_path.relative_to(self.root)), '.govel/operations', result['backupPath'],
                     result['backupPath']+'/structure.json', result['chapter']['filePath']]
        for relative in relatives:
            with self.subTest(relative=relative):
                path = self.root / relative
                moved = self.base / "link-target"
                path.rename(moved)
                path.symlink_to(moved)
                code = 'PROSE_LOCATOR_CONFLICT' if relative == result['chapter']['filePath'] else 'SYMLINK_PATH_FORBIDDEN'
                self.assert_rejected_unchanged(lambda: self.undo(operation_id), code)
                path.unlink()
                moved.rename(path)


class DashboardPreviewSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        initialize_project(self.root, book_name="preview")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        self.server.project_root = str(self.root)
        self.server.mutation_token = "private-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, headers, body=None, method="POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(method, path, body=json.dumps(body or {}) if method == "POST" else None, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        conn.close()
        return result

    def test_every_preview_rejects_unauthenticated_or_cross_origin_posts_without_writes(self) -> None:
        paths = (
            "/api/chapter/insert/preview", "/api/chapter/move/preview", "/api/chapter/archive/preview",
            "/api/chapter/timespan/preview",
            "/api/anchor/create/preview",
        )
        headers = (
            {"Content-Type": "text/plain", "Origin": "https://evil.example"},
            {"Content-Type": "application/json"},
            {"X-govel-Token": "private-token", "Origin": "https://evil.example"},
            {"Cookie": "govelMutation=private-token", "Origin": "http://localhost:1"},
        )
        before = snapshot(self.root)
        for path in paths:
            for request_headers in headers:
                with self.subTest(path=path, headers=request_headers):
                    status, _, data = self.request(path, request_headers)
                    # 机制版本 104 起：全部 /api 路由需要本地会话；跨站一律 403，
                    # 无凭据一律 401。旧契约（预览免会话 + MUTATION_TOKEN_REQUIRED）
                    # 已被 §10 策略取代；意图不变——未授权预览绝不产生写入。
                    if "Origin" in request_headers:
                        self.assertEqual(status, 403)
                        self.assertEqual(data["code"], "ORIGIN_REJECTED")
                    else:
                        self.assertEqual(status, 401)
                        self.assertEqual(data["code"], "SESSION_REQUIRED")
                    self.assertEqual(snapshot(self.root), before)

    def test_same_origin_config_cookie_allows_preview_and_commit(self) -> None:
        status, headers, _ = self.request("/api/config", {"Origin": f"http://127.0.0.1:{self.server.server_port}"}, method="GET")
        self.assertEqual(status, 200)
        auth = {"Cookie": headers["Set-Cookie"].split(";", 1)[0], "Origin": f"http://127.0.0.1:{self.server.server_port}", "Content-Type": "application/json"}
        status, _, plan = self.request("/api/anchor/create/preview", auth, {"name": "anchor", "absoluteTime": "2000-01-01"})
        self.assertEqual(status, 200)
        self.assertEqual(len(commands.list_pending_plans(self.root)), 1)
        status, _, result = self.request("/api/plan/commit", auth, {"operationId": plan["operationId"], "planHash": plan["planHash"], "approve": True})
        self.assertEqual(status, 200)
        self.assertEqual(result["code"], "STRUCTURE_COMMITTED")

    def test_empty_server_token_does_not_authorize_preview(self) -> None:
        self.server.mutation_token = ""
        before = snapshot(self.root)
        status, _, _ = self.request("/api/anchor/create/preview", {}, {"name": "anchor", "absoluteTime": "2000-01-01"})
        self.assertIn(status, {401, 403})
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
