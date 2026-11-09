"""R1 Phase 3 — Transaction & Monotonic Undo Engine (synthetic fixtures only).

Everything here runs against throwaway current-binding projects built by
`tests.fixtures_ownership`: no real novel project is read, no real prose is used.

Formal Undo is exercised exactly as the frozen contract defines it: a NEW forward commit
that reverses one committed operation — new revision, new generation, freshly sealed
integrityDigest — never a snapshot restore, never an old generation coming back（契约 §4/§9）。
"""
from __future__ import annotations

import hashlib
import http.client
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts import state_cache
from scripts.core import locks, prose_binding, refactor, structure_commands, structure_transaction
from scripts.core.finalization import finalize_chapter, pending_transactions, recover_transactions
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_model import is_current_binding
from scripts.core.structure_commands import _operation_paths, _plan_hash, gc_project, list_operations
from scripts.core.structure_store import (
    StructureError, empty_structure, load_structure, save_structure)
from scripts.core.workflow import WorkflowError
from scripts.dashboard_server import DashboardHandler
from scripts.mcp_server import TOOLS, call_tool
from tests import fixtures_ownership as fx
from tests.test_transaction_safety import snapshot

ROOT = Path(__file__).resolve().parents[1]

MANAGED_PATH = f"正文/{fx.CHAPTER_A}.md"
# Sentinel for the test caller's default request token: "echo whatever this chapter's binding
# carries now". Explicit None means the request omitted the token, which the engine refuses.
_CURRENT_TOKEN = object()
MOVED_PATH = "正文/搬家-0001.md"


def error_code(callable_) -> str:
    try:
        callable_()
    except (StructureError, WorkflowError) as exc:
        return exc.code
    return "NO-ERROR"


class MonotonicUndoCase(unittest.TestCase):

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = fx.ownership_project(self.root)

    def chapter(self, chapter_id: str) -> dict:
        structure = load_structure(self.root)
        return next(row for row in structure["chapters"] if row["chapterId"] == chapter_id)

    def move(self, chapter_id: str, target: str) -> str:
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=chapter_id, file_path=target)
        refactor.commit_refactor(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"],
                                 approved=True, actor="test")
        return plan["operationId"]

    def undo(self, operation_id: str, *, binding_generation=_CURRENT_TOKEN,
             chapter_id: str = fx.CHAPTER_A, **kwargs) -> dict:
        """Call undo the way a well-behaved caller must: echo the generation this chapter's
        binding carries right now.

        Tests probing the token boundary pass `binding_generation=None` (a request that omits
        the token) or an explicit stale number. Nothing here lets the engine pick a token:
        that substitution is exactly what the contract refuses.
        """
        if binding_generation is _CURRENT_TOKEN:
            binding_generation = self.chapter(chapter_id)["proseBinding"]["generation"]
        return structure_transaction.undo(self.root, operation_id, actor="test",
                                          binding_generation=binding_generation, **kwargs)

    def observation(self, chapter_id: str):
        structure = load_structure(self.root)
        chapter = next(row for row in structure["chapters"] if row["chapterId"] == chapter_id)
        return prose_binding.observe_chapter(self.root, structure, chapter)


# ---------------------------------------------------------------------------
# Group A/B — classic ABA, managed locator move
# ---------------------------------------------------------------------------

class AbaAndManagedMoveTests(MonotonicUndoCase):
    def test_a1_aba_old_generation_token_stays_dead(self) -> None:
        old_generation = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        moved = self.chapter(fx.CHAPTER_A)
        self.assertEqual(moved["filePath"], MOVED_PATH)
        result = self.undo(target, binding_generation=moved["proseBinding"]["generation"])
        chapter = self.chapter(fx.CHAPTER_A)
        revision = load_structure(self.root)["revision"]
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(chapter["filePath"], MANAGED_PATH)
        # 撤销是新的正向事务：generation 是**新提交 revision**，既不是历史 g10，也不是被撤销
        # operation 的 g11；integrityDigest 是针对 (A, new_generation, H) 的新封签。
        self.assertEqual(chapter["proseBinding"]["generation"], revision)
        self.assertNotEqual(chapter["proseBinding"]["generation"], old_generation)
        self.assertEqual(chapter["proseBinding"]["expectedContentDigest"], fx.managed_digest())
        self.assertEqual(
            chapter["proseBinding"]["integrityDigest"],
            prose_binding.binding_integrity_digest(fx.PROJECT_ID, fx.CHAPTER_A, MANAGED_PATH,
                                                   "managed", revision, fx.managed_digest()))
        # 旧令牌（locator 与正文摘要都回到原值）依然因代次不符被硬性拒绝。
        self.assertEqual(
            error_code(lambda: finalize_chapter(self.root, fx.CHAPTER_A, binding_generation=old_generation)),
            "OWNERSHIP_GENERATION_CONFLICT")

    def test_a2_pre_move_plan_token_is_stale_after_undo(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        first = self.undo(target)
        # 撤销之后为「再搬一次」做的新预览必须回显**当前** generation；篡改成旧代次的计划
        # 在提交锁内被拒——ABA 复活在正向通道上同样不成立。
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/再搬家.md")
        plan_path, _, _ = _operation_paths(self.root, plan["operationId"])
        stored = read_json(plan_path)
        stored["bindingTransitions"][0]["bindingGeneration"] = self.structure["revision"]
        stored["planHash"] = _plan_hash({key: value for key, value in stored.items() if key != "planHash"})
        write_json(plan_path, stored)
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=plan["operationId"], plan_hash=stored["planHash"],
                approved=True, actor="test")),
            "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)
        self.assertEqual(first["code"], "STRUCTURE_UNDONE")

    def test_b1_managed_move_undo_restores_locator_bytes_and_advances(self) -> None:
        import scripts.state_cache as state_cache
        before = self.chapter(fx.CHAPTER_A)
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        self.assertEqual((self.root / MOVED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)
        self.undo(target)
        after = self.chapter(fx.CHAPTER_A)
        self.assertEqual(after["filePath"], MANAGED_PATH)
        self.assertEqual((self.root / MANAGED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)
        self.assertFalse((self.root / MOVED_PATH).exists())
        # 派生投影读取当前真源，不依赖历史缓存被恢复或重建。
        current = load_structure(self.root)
        for projection in (state_cache.build_state(self.root), state_cache.build_design(self.root)):
            self.assertEqual(projection['structureRevision'], current['revision'])
            self.assertEqual(projection['structureHash'], state_cache._content_hash(current))
        # 纯定位符反向：正文字节不变，expected digest 原样，generation 前进，封签重算。
        self.assertEqual(after["proseBinding"]["expectedContentDigest"],
                         before["proseBinding"]["expectedContentDigest"])
        self.assertGreater(after["proseBinding"]["generation"], before["proseBinding"]["generation"])
        self.assertEqual(after["proseBinding"]["generation"], load_structure(self.root)["revision"])
        self.assertNotEqual(after["proseBinding"]["integrityDigest"],
                            before["proseBinding"]["integrityDigest"])

    def test_b2_undo_evidence_is_one_monotonic_reverse_of_the_target_journal(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        _, journal_path, _ = _operation_paths(self.root, target)
        target_journal_digest = structure_transaction.digest(journal_path.read_bytes())
        result = self.undo(target)
        undo_journal = read_json(self.root / result["undoJournalPath"])
        # 正式 undo operation：自己的新 OP id、reversible False（无 undo-of-undo）、
        # beforeHash == 撤销前结构、afterHash == 恢复后结构、revision 严格 +1。
        self.assertEqual(undo_journal["operation"], "undo")
        self.assertEqual(undo_journal["operationId"], result["undoOperationId"])
        self.assertNotEqual(undo_journal["operationId"], target)
        self.assertIs(undo_journal["reversible"], False)
        self.assertEqual(undo_journal["reversedOperationId"], target)
        self.assertEqual(undo_journal["reversedJournalDigest"], target_journal_digest)
        self.assertEqual(undo_journal["afterRevision"], undo_journal["beforeRevision"] + 1)
        # before/after 语义哈希与两次真实结构状态精确对上（structure_hash 忽略 revision/时间戳）。
        self.assertEqual(undo_journal["afterHash"], structure_transaction.structure_hash(load_structure(self.root)))
        # beforeHash 不是自声明：undo 自己落盘的 durable before-image 能把它重算出来。
        pre_undo = read_json(self.root / ".govel" / "backups" / result["undoOperationId"] / "structure.json")
        self.assertEqual(structure_transaction.structure_hash(pre_undo), undo_journal["beforeHash"])
        # receipt 与辅助 authority 是索引：它们指向同一次正式 undo operation。
        receipt = read_json(self.root / result["undoReceiptPath"])
        authority = read_json(self.root / result["undoAuthorityPath"])
        self.assertEqual(receipt["undoOperationId"], result["undoOperationId"])
        self.assertEqual(receipt["operationId"], target)
        self.assertEqual(authority["undoOperationId"], result["undoOperationId"])
        self.assertEqual(authority["reversedOperationId"], target)
        self.assertEqual(authority["restoredRevision"], undo_journal["afterRevision"])
        # 撤销 op 自己是 ledger 里的不可撤销条目，目标 operation 不再是链顶。
        rows = {row["operationId"]: row for row in list_operations(self.root)}
        self.assertIn(result["undoOperationId"], rows)
        self.assertIs(rows[result["undoOperationId"]]["undoable"], False)
        self.assertIs(rows[target]["undoable"], False)

    def test_b3_undo_crosses_the_same_writer_boundary_and_central_sealer(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        calls: list[dict] = []
        original = prose_binding.enforce_writer_boundary

        def probe(current, proposed, **kwargs):
            calls.append({"authority": kwargs.get("authority"), "proposed": proposed})
            return original(current, proposed, **kwargs)

        with mock.patch.object(prose_binding, "enforce_writer_boundary", side_effect=probe):
            self.undo(target)
        self.assertEqual(len(calls), 1)
        authority = calls[0]["authority"]
        records = authority.transitions
        # 边界看到的是**恰好**被逆转的那一章，且 kind 是正式 reverse transition。
        self.assertEqual(sorted(records), [fx.CHAPTER_A])
        self.assertEqual(records[fx.CHAPTER_A]["kind"], "formal_undo")
        self.assertEqual(records[fx.CHAPTER_A]["reversedOperationId"], target)
        proposed_chapter = next(row for row in calls[0]["proposed"]["chapters"]
                                if row["chapterId"] == fx.CHAPTER_A)
        # 提案里的受保护字段就是中央 sealer 的输出：没有第二套手工封签。
        revision = load_structure(self.root)["revision"]
        self.assertEqual(proposed_chapter["proseBinding"], prose_binding.seal_binding(
            fx.PROJECT_ID, fx.CHAPTER_A, MANAGED_PATH, state="managed", generation=revision,
            expected_content_digest=fx.managed_digest()))

    def test_b4_unbound_move_undo_keeps_the_empty_reservation_unbound(self) -> None:
        target = self.move(fx.CHAPTER_B, "正文/空槽新址.md")
        self.undo(target, chapter_id=fx.CHAPTER_B)
        chapter = self.chapter(fx.CHAPTER_B)
        self.assertEqual(chapter["filePath"], f"正文/{fx.CHAPTER_B}.md")
        self.assertEqual(chapter["proseBinding"]["state"], "unbound")
        self.assertIsNone(chapter["proseBinding"]["expectedContentDigest"])
        self.assertEqual(chapter["proseBinding"]["generation"], load_structure(self.root)["revision"])
        self.assertFalse((self.root / "正文/空槽新址.md").exists())
        self.assertFalse((self.root / f"正文/{fx.CHAPTER_B}.md").exists())


# ---------------------------------------------------------------------------
# Group C/D — unbound reverse, operation without binding delta
# ---------------------------------------------------------------------------

class UnboundAndNoDeltaTests(MonotonicUndoCase):
    def test_c1_unbound_move_undo_restores_state_and_digest_semantics(self) -> None:
        before = self.chapter(fx.CHAPTER_B)
        target = self.move(fx.CHAPTER_B, "正文/空槽新址.md")
        mid = self.chapter(fx.CHAPTER_B)
        self.assertEqual(mid["proseBinding"]["state"], "unbound")
        result = self.undo(target, chapter_id=fx.CHAPTER_B)
        after = self.chapter(fx.CHAPTER_B)
        self.assertEqual(result["revision"], load_structure(self.root)["revision"])
        self.assertEqual(after["filePath"], before["filePath"])
        self.assertEqual(after["proseBinding"]["state"], "unbound")
        self.assertIsNone(after["proseBinding"]["expectedContentDigest"])
        self.assertGreater(after["proseBinding"]["generation"], mid["proseBinding"]["generation"])
        self.assertEqual(after["proseBinding"]["generation"], result["revision"])
        for relative in ("正文/空槽新址.md", f"正文/{fx.CHAPTER_B}.md"):
            self.assertFalse((self.root / relative).exists())

    def test_c2_unbound_reverse_refuses_an_occupied_destination(self) -> None:
        target = self.move(fx.CHAPTER_B, "正文/空槽新址.md")
        (self.root / f"正文/{fx.CHAPTER_B}.md").write_text("外来字节", encoding="utf-8")
        self.assertEqual(error_code(lambda: self.undo(target, chapter_id=fx.CHAPTER_B)),
                         "PROSE_TARGET_NOT_OWNED")
        self.assertEqual((self.root / f"正文/{fx.CHAPTER_B}.md").read_text(encoding="utf-8"), "外来字节")
        self.assertEqual(self.chapter(fx.CHAPTER_B)["filePath"], "正文/空槽新址.md")

    def test_d1_operation_without_binding_delta_does_not_bump_generation(self) -> None:
        before = load_structure(self.root)
        plan = structure_commands.preview_move_chapter(self.root, chapter_id=fx.CHAPTER_B,
                                                       before_chapter_id=fx.CHAPTER_A)
        structure_commands.commit_plan(self.root, operation_id=plan["operationId"],
                                       plan_hash=plan["planHash"], approved=True, actor="test")
        committed = load_structure(self.root)
        self.assertNotEqual([row["chapterId"] for row in committed["chapters"]],
                            [row["chapterId"] for row in before["chapters"]])
        result = self.undo(plan["operationId"])
        after = load_structure(self.root)
        chapter = next(row for row in after["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        committed_chapter = next(row for row in committed["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        # 结构 revision 前进；本章 binding payload 一字节不变（generation 不推进）。
        self.assertEqual(after["revision"], result["revision"])
        self.assertGreater(after["revision"], committed["revision"])
        self.assertEqual(chapter["proseBinding"], committed_chapter["proseBinding"])
        self.assertEqual(chapter["proseBinding"]["generation"], self.structure["revision"])
        # 非受保护语义按被撤销 operation 的 before 态逆推：章序恢复。
        self.assertEqual([row["chapterId"] for row in after["chapters"]],
                         [row["chapterId"] for row in before["chapters"]])
        self.assertEqual((self.root / MANAGED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)


# ---------------------------------------------------------------------------
# Group E/F/G — occupied reverse target, external modification, stale generation
# ---------------------------------------------------------------------------

class ReverseSafetyTests(MonotonicUndoCase):
    def test_e1_foreign_bytes_at_reverse_target_are_never_taken_over(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        foreign = self.root / MANAGED_PATH
        foreign.write_text("别人的字节", encoding="utf-8")
        before_snapshot = foreign.read_bytes()
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_TARGET_NOT_OWNED")
        self.assertEqual(foreign.read_bytes(), before_snapshot)
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_e2_identical_bytes_at_the_target_are_still_refused(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        (self.root / MANAGED_PATH).write_text(fx.MANAGED_PROSE, encoding="utf-8")
        # 同字节不构成归属：绝不按哈希认领，只报告目标被占用。
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_TARGET_NOT_OWNED")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_e3_broken_symlink_still_occupies_the_reverse_target(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        (self.root / MANAGED_PATH).symlink_to(self.root / "不存在的目标")
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_TARGET_NOT_OWNED")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_f1_externally_modified_managed_prose_blocks_the_undo(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        moved = self.root / MOVED_PATH
        moved.write_text("被外部改成另一批字节", encoding="utf-8")
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_EXTERNAL_MODIFICATION")
        # 绝不拿 operation 的 before-image 去覆盖未知当前字节。
        self.assertEqual(moved.read_text(encoding="utf-8"), "被外部改成另一批字节")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_f2_missing_managed_prose_is_locator_drift(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        (self.root / MOVED_PATH).unlink()
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_LOCATOR_DRIFT")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_g1_stale_binding_generation_token_is_refused(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        self.assertEqual(
            error_code(lambda: self.undo(target, binding_generation=self.structure["revision"])),
            "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        # 只有回显**当前**代次才继续走当前状态重验；省略令牌不是「让引擎自己看当前代次」。
        result = self.undo(target, binding_generation=load_structure(self.root)["revision"])
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")

    def test_g2_reverse_declaration_that_does_not_match_the_current_binding_is_refused(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        self.undo(target)
        structure = load_structure(self.root)
        chapter = self.chapter(fx.CHAPTER_A)
        declaration = {"kind": "formal_undo", "chapterId": fx.CHAPTER_A,
                       "reversedOperationId": target, "reversedJournalDigest": "0" * 8,
                       "currentFilePath": MANAGED_PATH, "currentBindingGeneration": 999,
                       "currentExpectedContentDigest": fx.managed_digest(),
                       "restoreFilePath": MOVED_PATH, "restoreState": "managed",
                       "restoreExpectedContentDigest": fx.managed_digest()}
        self.assertEqual(
            error_code(lambda: prose_binding.binding_transition_result(
                structure, chapter, declaration, generation=structure["revision"] + 1)),
            "OWNERSHIP_GENERATION_CONFLICT")
        declaration["currentBindingGeneration"] = chapter["proseBinding"]["generation"]
        declaration["currentFilePath"] = MOVED_PATH
        self.assertEqual(
            error_code(lambda: prose_binding.binding_transition_result(
                structure, chapter, declaration, generation=structure["revision"] + 1)),
            "UNDO_REVERSE_STATE_MISMATCH")


# ---------------------------------------------------------------------------
# Group H/I — chain top, evidence integrity
# ---------------------------------------------------------------------------

class ChainTopAndEvidenceTests(MonotonicUndoCase):
    def test_h1_a_later_operation_pushes_the_target_off_the_chain_top(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        plan = structure_commands.preview_move_chapter(self.root, chapter_id=fx.CHAPTER_C,
                                                       before_chapter_id=fx.CHAPTER_B)
        structure_commands.commit_plan(self.root, operation_id=plan["operationId"],
                                       plan_hash=plan["planHash"], approved=True, actor="test")
        structure_before = load_structure(self.root)
        self.assertEqual(error_code(lambda: self.undo(target)), "UNDO_NOT_CHAIN_TOP")
        # production 入口在锁外先给出同一条确定性拒绝（拒绝不建锁、不写盘）。
        self.assertEqual(error_code(lambda: structure_commands.undo_operation(self.root, target, actor="test")),
                         "UNDO_NOT_CHAIN_TOP")
        # 失败不产生任何写入：结构、字节、章位置都保持第二次提交之后的状态。
        self.assertEqual(load_structure(self.root), structure_before)
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_h2_undoable_reporting_follows_the_same_chain_top_rule(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        rows = {row["operationId"]: row for row in list_operations(self.root)}
        self.assertIs(rows[target]["undoable"], True)
        plan = structure_commands.preview_move_chapter(self.root, chapter_id=fx.CHAPTER_C,
                                                       before_chapter_id=fx.CHAPTER_B)
        structure_commands.commit_plan(self.root, operation_id=plan["operationId"],
                                       plan_hash=plan["planHash"], approved=True, actor="test")
        rows = {row["operationId"]: row for row in list_operations(self.root)}
        self.assertIs(rows[target]["undoable"], False)
        self.assertIs(rows[plan["operationId"]]["undoable"], True)

    def test_i1_missing_before_image_blocks_the_undo_and_not_the_ownership(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        shutil.rmtree(self.root / ".govel" / "backups" / target)
        self.assertEqual(error_code(lambda: self.undo(target)), "BACKUP_MISSING")
        self.assertEqual(error_code(lambda: structure_commands.undo_operation(self.root, target, actor="test")),
                         "BACKUP_MISSING")
        # 历史证据缺失只阻止这次 Undo：当前健康归属照旧，正常定位符移动照常提交。
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)
        self.assertEqual(self.move(fx.CHAPTER_A, "正文/继续搬家.md")[:3], "OP-")

    def test_i2_damaged_before_image_blocks_the_undo(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        backup_structure = self.root / ".govel" / "backups" / target / "structure.json"
        backup_structure.write_text("{ not the committed bytes", encoding="utf-8")
        self.assertEqual(error_code(lambda: self.undo(target)), "BACKUP_HASH_MISMATCH")
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)

    def test_i3_damaged_prose_before_image_blocks_the_undo(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        (self.root / ".govel" / "backups" / target / MANAGED_PATH).write_text("损坏", encoding="utf-8")
        self.assertEqual(error_code(lambda: self.undo(target)), "BACKUP_HASH_MISMATCH")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)


# ---------------------------------------------------------------------------
# Group J/K — forged evidence, resurrection of the old token at the same locator
# ---------------------------------------------------------------------------

class EvidenceForgeryTests(MonotonicUndoCase):
    def test_j1_hand_written_receipts_and_authorities_prove_nothing(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        bogus_operation = "OP-" + "A" * 16
        receipts = self.root / ".govel" / "undo-receipts"
        authorities = self.root / ".govel" / "undo-operations"
        receipts.mkdir(parents=True, exist_ok=True)
        authorities.mkdir(parents=True, exist_ok=True)
        write_json(receipts / f"{target}-999.json", {
            "operationId": target, "undoOperationId": bogus_operation, "undoRevision": 999,
            "actor": "human", "undoneAt": "2026-09-29T00:00:00Z", "impact": {}})
        write_json(authorities / f"{target}-999.json", {
            "operation": "undo", "undoOperationId": bogus_operation,
            "reversedOperationId": target, "restoredRevision": 999})
        # 自声明 JSON 不构成 authority：当前归属不被改写，正式 Undo 仍按真实状态执行。
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        result = self.undo(target)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)

    def test_j2_a_journal_like_json_cannot_mint_an_undoable_operation(self) -> None:
        forged = "OP-" + "B" * 16
        (self.root / ".govel" / "operations").mkdir(parents=True, exist_ok=True)
        write_json(self.root / ".govel" / "operations" / f"{forged}.json", {
            "schemaVersion": 4, "operationId": forged, "operation": "undo",
            "reversible": True, "actor": "human", "beforeHash": "0" * 64, "afterHash": "0" * 64,
            "beforeRevision": 7, "afterRevision": 8, "impact": {},
            "backupPath": f".govel/backups/{forged}", "backupFiles": [], "beforeFiles": {},
            "afterFiles": {}, "committedAt": "2026-09-29T00:00:00Z"})
        # 伪造的 undo 条目即使自称 reversible 也绝不可撤销；没有可核对的 before-image 的
        # journal-like JSON 同样不构成证据。
        self.assertEqual(error_code(lambda: self.undo(forged)), "OPERATION_NOT_REVERSIBLE")
        fake_target = "OP-" + "C" * 16
        write_json(self.root / ".govel" / "operations" / f"{fake_target}.json", {
            "schemaVersion": 4, "operationId": fake_target, "operation": "refactor_relocate_chapter_paths",
            "reversible": True, "actor": "human",
            "beforeHash": "0" * 64, "afterHash": structure_transaction.structure_hash(load_structure(self.root)),
            "beforeRevision": 7, "afterRevision": 8, "impact": {},
            "backupPath": f".govel/backups/{fake_target}", "backupFiles": [],
            "beforeFiles": {}, "afterFiles": {}, "committedAt": "2026-09-29T00:00:00Z"})
        self.assertEqual(error_code(lambda: self.undo(fake_target)), "UNDO_UNVERIFIABLE")

    def test_j3_a_plan_cannot_declare_a_formal_undo_transition(self) -> None:
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/伪造.md")
        plan_path, _, _ = _operation_paths(self.root, plan["operationId"])
        stored = read_json(plan_path)
        stored["bindingTransitions"][0]["kind"] = "formal_undo"
        stored["planHash"] = _plan_hash({key: value for key, value in stored.items() if key != "planHash"})
        write_json(plan_path, stored)
        # 计划的 binding transition 通道没有 formal_undo 的观测前置——声明它只会被拒，
        # 不会被当成授权（declared kind != authority）。
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=plan["operationId"], plan_hash=stored["planHash"],
                approved=True, actor="test")),
            "OWNERSHIP_MODEL_UNSUPPORTED_OPERATION")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)

    def test_j4_the_undo_itself_is_not_undoable(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        result = self.undo(target)
        self.assertEqual(error_code(lambda: self.undo(result["undoOperationId"])),
                         "OPERATION_NOT_REVERSIBLE")
        # 同一目标再撤一次也已经不在链顶：绝不允许把撤销结果再覆盖回去。
        self.assertEqual(error_code(lambda: self.undo(target)), "UNDO_NOT_CHAIN_TOP")


class LocatorResurrectionTests(MonotonicUndoCase):
    def test_k1_locator_and_digest_return_but_the_old_seal_never_does(self) -> None:
        before = self.chapter(fx.CHAPTER_A)
        old_generation = before["proseBinding"]["generation"]
        old_integrity = before["proseBinding"]["integrityDigest"]
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        mid_generation = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        self.undo(target)
        after = self.chapter(fx.CHAPTER_A)
        self.assertEqual(after["filePath"], before["filePath"])
        self.assertEqual(after["proseBinding"]["expectedContentDigest"],
                         before["proseBinding"]["expectedContentDigest"])
        self.assertNotIn(after["proseBinding"]["generation"], (old_generation, mid_generation))
        self.assertEqual(after["proseBinding"]["generation"], load_structure(self.root)["revision"])
        self.assertNotEqual(after["proseBinding"]["integrityDigest"], old_integrity)
        self.assertEqual(after["proseBinding"]["integrityDigest"],
                         prose_binding.binding_integrity_digest(
                             fx.PROJECT_ID, fx.CHAPTER_A, MANAGED_PATH, "managed",
                             after["proseBinding"]["generation"], fx.managed_digest()))


# ---------------------------------------------------------------------------
# Group L/M/N — sibling isolation, reservation conflict, hardlink alias
# ---------------------------------------------------------------------------

class SiblingAndAliasTests(MonotonicUndoCase):
    def test_l1_a_damaged_sibling_neither_blocks_nor_is_touched_by_the_undo(self) -> None:
        # 无关兄弟章在被撤销 operation 之前就已损坏：它的语义字段没变，因此不参与这次
        # 逆向、不被修复，也绝不因此把整个 undo 锁死（契约 §18）。
        structure = load_structure(self.root)
        sibling = next(row for row in structure["chapters"] if row["chapterId"] == fx.CHAPTER_C)
        sibling["proseBinding"]["integrityDigest"] = "sha256:" + "0" * 64
        write_json(self.root / ".govel" / "structure.json", structure)
        self.assertEqual(self.observation(fx.CHAPTER_C)["state"], prose_binding.BINDING_INVALID)
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        sibling_before = json.dumps(self.chapter(fx.CHAPTER_C), sort_keys=True)
        result = self.undo(target)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)
        self.assertEqual(json.dumps(self.chapter(fx.CHAPTER_C), sort_keys=True), sibling_before)
        # sibling 的安全定位符保留仍在：任何章都不能接管它的槽位。
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_B, file_path=f"正文/{fx.CHAPTER_C}.md")),
            "PROSE_LOCATOR_CONFLICT")

    def test_l2_a_drifted_sibling_on_disk_stays_isolated(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        (self.root / f"正文/{fx.CHAPTER_C}.md").write_text("悬案的来历不明字节", encoding="utf-8")
        sibling_payload = json.dumps(self.chapter(fx.CHAPTER_C), sort_keys=True)
        result = self.undo(target)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)
        self.assertEqual(json.dumps(self.chapter(fx.CHAPTER_C), sort_keys=True), sibling_payload)
        self.assertEqual((self.root / f"正文/{fx.CHAPTER_C}.md").read_text(encoding="utf-8"),
                         "悬案的来历不明字节")

    def test_l3_a_damaged_target_chapter_is_never_repaired_by_the_undo(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        structure = load_structure(self.root)
        chapter = next(row for row in structure["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        chapter["proseBinding"]["integrityDigest"] = "sha256:" + "0" * 64
        write_json(self.root / ".govel" / "structure.json", structure)
        self.assertEqual(error_code(lambda: self.undo(target)), "UNDO_NOT_CHAIN_TOP")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.BINDING_INVALID)

    def test_m1_a_sibling_in_the_old_locator_blocks_the_reverse(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_B,
                                                       file_path=MANAGED_PATH)
        refactor.commit_refactor(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"],
                                 approved=True, actor="test")
        sibling_payload = json.dumps(self.chapter(fx.CHAPTER_B), sort_keys=True)
        # 曾经属于本章的定位符被兄弟章合法占用：撤销必须拒绝，绝不覆盖、绝不自动认领。
        self.assertEqual(error_code(lambda: self.undo(target)), "UNDO_NOT_CHAIN_TOP")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        self.assertEqual(json.dumps(self.chapter(fx.CHAPTER_B), sort_keys=True), sibling_payload)
        self.assertFalse((self.root / MANAGED_PATH).exists())

    def test_m2_the_reverse_destination_reservation_is_checked_directly(self) -> None:
        # 聚焦测试反向安全检查本身：即便当前结构仍与目标的 after 态一致、本章盘上字节也
        # 健康，只要恢复目标被另一个章保留（含 unresolved/archived），逆向必须 fail closed。
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        structure = load_structure(self.root)
        moved = next(row for row in structure["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        crafted = json.loads(json.dumps(structure))
        for index, row in enumerate(crafted["chapters"]):
            if row["chapterId"] == fx.CHAPTER_C:
                crafted["chapters"][index]["filePath"] = MANAGED_PATH
                crafted["chapters"][index]["proseBinding"] = prose_binding.seal_binding(
                    fx.PROJECT_ID, fx.CHAPTER_C, MANAGED_PATH, state="unresolved",
                    generation=moved["proseBinding"]["generation"], expected_content_digest=None)
        transition = {"kind": "formal_undo", "chapterId": fx.CHAPTER_A,
                      "reversedOperationId": target, "reversedJournalDigest": "0" * 64,
                      "currentFilePath": MOVED_PATH,
                      "currentBindingGeneration": moved["proseBinding"]["generation"],
                      "currentExpectedContentDigest": fx.managed_digest(),
                      "restoreFilePath": MANAGED_PATH, "restoreState": "managed",
                      "restoreExpectedContentDigest": fx.managed_digest()}
        self.assertEqual(
            error_code(lambda: structure_transaction._require_reverse_ownership(
                self.root, crafted, [transition])),
            "PROSE_LOCATOR_CONFLICT")

    def test_n1_hardlinked_reverse_destination_is_refused(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        os.link(self.root / MOVED_PATH, self.root / MANAGED_PATH)
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_TARGET_NOT_OWNED")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_n2_alias_between_the_current_locator_and_a_sibling_is_a_conflict(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        os.link(self.root / MOVED_PATH, self.root / f"正文/{fx.CHAPTER_C}.md")
        self.assertEqual(error_code(lambda: self.undo(target)), "PROSE_LOCATOR_CONFLICT")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)


# ---------------------------------------------------------------------------
# Group O/P — history independence after the undo, crash recovery
# ---------------------------------------------------------------------------

class HistoryIndependenceTests(MonotonicUndoCase):
    def test_o1_current_ownership_survives_history_gc_after_a_successful_undo(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        self.undo(target)
        gc_project(self.root)
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)
        # 更彻底的历史损失：正式 undo 之后的当前归属不依赖任何历史 replay。
        for relative in (".govel/operations", ".govel/backups",
                         ".govel/undo-receipts", ".govel/undo-operations"):
            shutil.rmtree(self.root / relative, ignore_errors=True)
        workflow = read_json(self.root / ".govel" / "workflow.json")
        workflow["continuity"] = {}
        write_json(self.root / ".govel" / "workflow.json", workflow)
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)
        # 未来正常 ownership permission 只看当前 binding：正向移动照常提交并推进代次。
        previous_generation = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        self.move(fx.CHAPTER_A, "正文/历史之后.md")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], "正文/历史之后.md")
        self.assertGreater(self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"], previous_generation)


class CrashRecoveryTests(MonotonicUndoCase):
    def _crash_script(self, kill_phase: str, kill_path_guard: str | None = None) -> str:
        guard = f" and relative == {kill_path_guard!r}" if kill_path_guard else ""
        return f"""import os,sys
from pathlib import Path
from scripts.core import write_sets
from scripts.core.structure_transaction import undo
write_sets.checkpoint=lambda phase, relative=None: os._exit(77) if phase=={kill_phase!r}{guard} else None
undo(Path(sys.argv[1]), sys.argv[2], actor='test', binding_generation=int(sys.argv[3]))
"""

    def _run_crash(self, script: str, operation_id: str, chapter_id: str = fx.CHAPTER_A) -> None:
        # 崩溃注入要打进真正的提交路径，所以请求必须带着合法令牌通过代次门：无令牌的请求在
        # 任何写入之前就被拒绝，那样的「恢复」根本没有写集中断可验。
        generation = self.chapter(chapter_id)["proseBinding"]["generation"]
        result = subprocess.run([sys.executable, "-c", script, str(self.root), operation_id,
                                 str(generation)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)

    def test_p1_prepared_crash_recovers_to_complete_old(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        self._run_crash(self._crash_script("prepared"), target)
        # 中断的写集留下 durable manifest：在恢复之前引擎拒绝继续推进。
        self.assertTrue(pending_transactions(self.root))
        self.assertEqual(error_code(lambda: self.undo(target)), "TRANSACTION_INCOMPLETE")
        recover_transactions(self.root, apply=True)
        self.assertFalse(pending_transactions(self.root))
        chapter = self.chapter(fx.CHAPTER_A)
        self.assertEqual(chapter["filePath"], MOVED_PATH)
        self.assertEqual((self.root / MOVED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)
        self.assertFalse((self.root / MANAGED_PATH).exists())
        self.assertEqual(chapter["proseBinding"]["generation"], self.structure["revision"] + 1)
        # 恢复之后同一次撤销可以重新执行，且结果与新提交等价。
        result = self.undo(target)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)

    def test_p2_committed_cleanup_interruption_recovers_to_complete_new(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        before = self.chapter(fx.CHAPTER_A)
        self._run_crash(self._crash_script("committed"), target)
        recover_transactions(self.root, apply=True)
        chapter = self.chapter(fx.CHAPTER_A)
        # phase == committed：已提交的新权威必须保持——绝不回滚成旧 revision。
        self.assertEqual(chapter["filePath"], MANAGED_PATH)
        self.assertEqual((self.root / MANAGED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)
        self.assertFalse((self.root / MOVED_PATH).exists())
        self.assertGreater(chapter["proseBinding"]["generation"], before["proseBinding"]["generation"])
        self.assertEqual(chapter["proseBinding"]["generation"], load_structure(self.root)["revision"])

    def test_p3_mid_write_crash_never_leaves_a_half_applied_reverse(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        # 在正文回搬落盘后中断：结构与字节绝不能出现一半新一半旧。
        self._run_crash(self._crash_script("write", kill_path_guard=MANAGED_PATH), target)
        recover_transactions(self.root, apply=True)
        chapter = self.chapter(fx.CHAPTER_A)
        self.assertEqual(chapter["filePath"], MOVED_PATH)
        self.assertEqual((self.root / MOVED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)
        self.assertFalse((self.root / MANAGED_PATH).exists())
        # 恢复走的是 COMPLETE OLD：绝不存在 prepared → forward replay。
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)


# ---------------------------------------------------------------------------
# Group Q — schema parity, production entry points, boundary discipline
# ---------------------------------------------------------------------------

class InPlaceReverseTests(MonotonicUndoCase):
    """§16：撤销真正创建/覆盖 prose bytes 的 operation（locator 不变）。

    当前生产者里没有一条把「定稿」写进共享 ledger（finalization 走自己的写集，不落
    operation journal），因此这里构造该形状的**真实可逆写集证据**（before-image 与
    journal 齐备、当前盘上状态精确等于 after 态），逐条验证引擎按 §16 推导逆操作：
    只有当前字节精确吻合 after 态才删除/恢复，绝不删除未知新字节。
    """

    def _finalization_evidence(self, chapter_id: str, *, after_text: str, before_text: str | None) -> str:
        from scripts.core.write_sets import file_state

        structure_path = self.root / ".govel" / "structure.json"
        structure_before = read_json(structure_path)
        chapter = next(row for row in structure_before["chapters"] if row["chapterId"] == chapter_id)
        locator = chapter["filePath"]
        previous_digest = (f"sha256:{hashlib.sha256(before_text.encode('utf-8')).hexdigest()}"
                           if before_text is not None else None)
        chapter["proseBinding"] = prose_binding.seal_binding(
            fx.PROJECT_ID, chapter_id, locator,
            state="managed" if before_text is not None else "unbound",
            generation=structure_before["revision"], expected_content_digest=previous_digest)
        write_json(structure_path, structure_before)
        before_state = file_state(structure_path)
        before_bytes = structure_path.read_bytes()
        prose_path = self.root / locator
        revision = structure_before["revision"] + 1
        after = json.loads(before_bytes)
        after["revision"] = revision
        target = next(row for row in after["chapters"] if row["chapterId"] == chapter_id)
        prose_bytes = after_text.encode("utf-8")
        target["proseBinding"] = prose_binding.seal_binding(
            fx.PROJECT_ID, chapter_id, locator, state="managed", generation=revision,
            expected_content_digest=f"sha256:{hashlib.sha256(prose_bytes).hexdigest()}")
        write_json(structure_path, after)
        prose_path.write_bytes(prose_bytes)
        operation_id = "OP-" + "F" * 16
        backup = self.root / ".govel" / "backups" / operation_id
        (backup / "structure.json").parent.mkdir(parents=True, exist_ok=True)
        (backup / "structure.json").write_bytes(before_bytes)
        if before_text is not None:
            backup_prose = backup / locator
            backup_prose.parent.mkdir(parents=True, exist_ok=True)
            backup_prose.write_bytes(before_text.encode("utf-8"))
        write_json(self.root / ".govel" / "operations" / f"{operation_id}.json", {
            "schemaVersion": 4, "operationId": operation_id, "operation": "finalize_chapter",
            "actor": "workflow", "beforeHash": structure_transaction.structure_hash(structure_before),
            "afterHash": structure_transaction.structure_hash(after),
            "beforeRevision": structure_before["revision"], "afterRevision": revision, "impact": {},
            "backupPath": f".govel/backups/{operation_id}",
            "backupFiles": [".govel/structure.json", locator],
            "beforeFiles": {
                ".govel/structure.json": before_state,
                locator: ({"exists": True, "sha256": hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
                           "mode": None} if before_text is not None else {"exists": False, "sha256": None, "mode": None}),
            },
            "afterFiles": {
                ".govel/structure.json": file_state(structure_path),
                locator: file_state(prose_path),
            },
            "existingFiles": [".govel/structure.json"] + ([locator] if before_text is not None else []),
            "createdFiles": [] if before_text is not None else [locator],
            "writtenIndexes": [], "refusedIndexes": [],
            "committedAt": "2026-09-29T00:00:00Z", "reversible": True,
        })
        return operation_id

    def test_s1_created_prose_is_deleted_and_binding_returns_to_unbound(self) -> None:
        operation_id = self._finalization_evidence(fx.CHAPTER_B, after_text="新定稿正文\n", before_text=None)
        result = self.undo(operation_id, chapter_id=fx.CHAPTER_B)
        chapter = self.chapter(fx.CHAPTER_B)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(chapter["filePath"], f"正文/{fx.CHAPTER_B}.md")
        self.assertEqual(chapter["proseBinding"]["state"], "unbound")
        self.assertIsNone(chapter["proseBinding"]["expectedContentDigest"])
        self.assertEqual(chapter["proseBinding"]["generation"], result["revision"])
        self.assertFalse((self.root / f"正文/{fx.CHAPTER_B}.md").exists())

    def test_s2_created_prose_modified_externally_is_never_deleted(self) -> None:
        operation_id = self._finalization_evidence(fx.CHAPTER_B, after_text="新定稿正文\n", before_text=None)
        (self.root / f"正文/{fx.CHAPTER_B}.md").write_text("未知新字节", encoding="utf-8")
        self.assertEqual(error_code(lambda: self.undo(operation_id, chapter_id=fx.CHAPTER_B)),
                         "PROSE_EXTERNAL_MODIFICATION")
        self.assertEqual((self.root / f"正文/{fx.CHAPTER_B}.md").read_text(encoding="utf-8"), "未知新字节")
        self.assertEqual(self.chapter(fx.CHAPTER_B)["proseBinding"]["state"], "managed")

    def test_s3_overwritten_prose_is_restored_from_the_validated_before_image(self) -> None:
        operation_id = self._finalization_evidence(fx.CHAPTER_B, after_text="第二版正文\n", before_text="第一版正文\n")
        result = self.undo(operation_id, chapter_id=fx.CHAPTER_B)
        chapter = self.chapter(fx.CHAPTER_B)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual((self.root / f"正文/{fx.CHAPTER_B}.md").read_text(encoding="utf-8"), "第一版正文\n")
        self.assertEqual(chapter["proseBinding"]["state"], "managed")
        self.assertEqual(chapter["proseBinding"]["expectedContentDigest"],
                         "sha256:" + hashlib.sha256("第一版正文\n".encode("utf-8")).hexdigest())
        # 恢复的是语义字段与历史字节，**不是**历史 generation / integrityDigest。
        self.assertEqual(chapter["proseBinding"]["generation"], result["revision"])


class CurrentFormatUndoTests(unittest.TestCase):
    def test_q1_current_format_uses_monotonic_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fx.ownership_project(root)
            case = MonotonicUndoCase()
            case.root = root
            case.structure = load_structure(root)
            target = case.move(fx.CHAPTER_A, MOVED_PATH)
            old_generation = case.structure["chapters"][0]["proseBinding"]["generation"]
            result = case.undo(target)
            chapter = case.chapter(fx.CHAPTER_A)
            self.assertEqual(result["code"], "STRUCTURE_UNDONE")
            self.assertEqual(chapter["filePath"], MANAGED_PATH)
            self.assertEqual(chapter["proseBinding"]["generation"], result["revision"])
            self.assertNotEqual(chapter["proseBinding"]["generation"], old_generation)
            self.assertEqual((root / MANAGED_PATH).read_text(encoding="utf-8"), fx.MANAGED_PROSE)
            self.assertEqual(
                error_code(lambda: finalize_chapter(root, fx.CHAPTER_A,
                                                    binding_generation=old_generation)),
                "OWNERSHIP_GENERATION_CONFLICT")

    def test_q2_schema5_full_history_loss_keeps_the_current_binding_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fx.ownership_project(root)
            case = MonotonicUndoCase()
            case.root = root
            target = case.move(fx.CHAPTER_A, MOVED_PATH)
            case.undo(target)
            shutil.rmtree(root / ".govel" / "operations", ignore_errors=True)
            shutil.rmtree(root / ".govel" / "backups", ignore_errors=True)
            self.assertEqual(case.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)


class ProductionEntryPointTests(MonotonicUndoCase):
    def test_r1_cli_undo_routes_through_the_monotonic_engine(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        generation = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "structure", "undo",
             "--operation-id", target, "--binding-generation", str(generation),
             "--project-root", str(self.root)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)
        # 陈旧代次令牌在 CLI 通道同样被拒（CLI 不是旁路）。
        second = self.move(fx.CHAPTER_A, MOVED_PATH)
        stale = subprocess.run(
            [sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "structure", "undo",
             "--operation-id", second, "--binding-generation", str(generation),
             "--project-root", str(self.root)],
            capture_output=True, text=True)
        self.assertEqual(stale.returncode, 1, stale.stdout + stale.stderr)
        self.assertEqual(json.loads(stale.stdout)["code"], "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_r2_undo_operation_only_delegates_to_the_locked_engine(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        token = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        with mock.patch.object(structure_transaction, "undo",
                               side_effect=structure_transaction.undo) as spy:
            result = structure_commands.undo_operation(self.root, target, actor="test",
                                                       binding_generation=token)
            # 调用者的令牌必须原样进到锁内引擎：入口层既不许丢它，也不许换个值代它。
            self.assertEqual(spy.call_args.kwargs["binding_generation"], token)
            self.assertEqual(spy.call_args.args[1], target)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)


# ---------------------------------------------------------------------------
# Group T — Formal Undo 的请求令牌边界（request generation CAS）
# ---------------------------------------------------------------------------

class _QuietDashboardHandler(DashboardHandler):
    def log_message(self, *_args) -> None:
        pass


class RequestTokenBoundaryTests(MonotonicUndoCase):
    """撤销请求必须自己带回它看到的代次：省略令牌不等于授权引擎去查当前值。

    四个入口（direct API / CLI / MCP / dashboard）全部失败闭合，拒绝不留副作用；
    零受保护 delta 的撤销不被强要无意义令牌；多章 delta 不可由一个整数覆盖。
    """

    def cli_undo(self, operation_id: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "structure", "undo",
             "--operation-id", operation_id, *extra, "--project-root", str(self.root)],
            capture_output=True, text=True)

    def dashboard_undo(self, body: dict) -> tuple[int, dict]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietDashboardHandler)
        server.project_root = str(self.root)
        server.mutation_token = "token-boundary"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request("POST", "/api/operation/undo", body=json.dumps(body),
                     headers={"Content-Type": "application/json", "X-govel-Token": "token-boundary"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        return response.status, payload

    def test_t1_direct_engine_refuses_a_request_that_omits_the_token(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        before = snapshot(self.root)
        # 引擎此刻手里就握着当前结构，「顺手读一个代次」在技术上零成本——正因如此拒绝必须是
        # 硬闭合：不带的请求永远不等于「请服务器按现状授权」。
        self.assertEqual(error_code(lambda: self.undo(target, binding_generation=None)),
                         "OWNERSHIP_GENERATION_REQUIRED")
        self.assertEqual(error_code(lambda: structure_transaction.undo(self.root, target, actor="test")),
                         "OWNERSHIP_GENERATION_REQUIRED")
        self.assertEqual(snapshot(self.root), before)

    def test_t2_production_entry_refuses_before_the_project_lock(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        before = snapshot(self.root)
        with mock.patch.object(locks, "_acquire", wraps=locks._acquire) as acquire:
            with self.assertRaises(StructureError) as caught:
                structure_commands.undo_operation(self.root, target, actor="test")
            self.assertEqual(caught.exception.code, "OWNERSHIP_GENERATION_REQUIRED")
            acquire.assert_not_called()
        self.assertEqual(snapshot(self.root), before)

    def test_t3_missing_and_stale_are_distinguishable_and_the_current_token_still_wins(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        current = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        self.assertEqual(error_code(lambda: self.undo(target, binding_generation=None)),
                         "OWNERSHIP_GENERATION_REQUIRED")
        for stale in (current - 1, current + 1, 1):
            with self.subTest(binding_generation=stale):
                self.assertEqual(error_code(lambda: self.undo(target, binding_generation=stale)),
                                 "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(self.undo(target, binding_generation=current)["code"], "STRUCTURE_UNDONE")

    def test_t4_refusal_names_the_token_source_for_a_low_capability_caller(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        with self.assertRaises(StructureError) as caught:
            self.undo(target, binding_generation=None)
        message = str(caught.exception)
        # 拒绝必须可执行：说清读哪个字段、往哪个参数回显，而不是只报「缺参数」。
        self.assertIn("bindingGeneration", message)
        self.assertIn("chapter-receipt", message)
        self.assertEqual(caught.exception.details["chapterId"], fx.CHAPTER_A)

    def test_t5_undo_without_a_binding_delta_demands_no_token(self) -> None:
        plan = structure_commands.preview_move_chapter(self.root, chapter_id=fx.CHAPTER_B,
                                                       before_chapter_id=fx.CHAPTER_A)
        structure_commands.commit_plan(self.root, operation_id=plan["operationId"],
                                       plan_hash=plan["planHash"], approved=True, actor="test")
        before_bindings = {row["chapterId"]: row["proseBinding"]
                           for row in load_structure(self.root)["chapters"]}
        # delta 由引擎比对 committed before-image 与当前结构得出，不是调用者声称的：
        # 这条撤销没有 binding 语义变化，因此没有任何代次可回显，也就不得索要令牌。
        result = self.undo(plan["operationId"], binding_generation=None)
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        after_bindings = {row["chapterId"]: row["proseBinding"]
                          for row in load_structure(self.root)["chapters"]}
        self.assertEqual(after_bindings, before_bindings)

    def test_t6_operation_metadata_reports_the_token_the_engine_demands(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        rows = {row["operationId"]: row for row in list_operations(self.root)}
        row = rows[target]
        self.assertTrue(row["undoable"])
        self.assertEqual(row["bindingGeneration"],
                         self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"])
        # 元数据只是给调用者回显的读数，不是 authority：改一位立刻过期。
        self.assertEqual(error_code(lambda: self.undo(target, binding_generation=row["bindingGeneration"] + 1)),
                         "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(self.undo(target, binding_generation=row["bindingGeneration"])["code"],
                         "STRUCTURE_UNDONE")

    def test_t7_one_token_cannot_authorize_two_binding_targets(self) -> None:
        first = self.move(fx.CHAPTER_A, MOVED_PATH)
        self.move(fx.CHAPTER_B, "正文/空槽新址.md")
        # 真实生产者不会一次改两章受保护语义，这个形状只能伪造（把第一条 operation 的
        # afterHash 钉到「两章都搬过」的当前结构上）。伪造体必须被拒，而不是让同一个整数
        # 去逐章比对——那只会对其中一章成立。
        _, journal_path, _ = _operation_paths(self.root, first)
        journal = read_json(journal_path)
        journal["afterHash"] = structure_transaction.structure_hash(load_structure(self.root))
        write_json(journal_path, journal)
        token_a = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        before = snapshot(self.root)
        for token in (token_a, None, token_a - 1):
            with self.subTest(binding_generation=token):
                self.assertEqual(error_code(lambda: self.undo(first, binding_generation=token)),
                                 "UNDO_UNSUPPORTED_OPERATION")
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)

    def test_t8_no_production_path_derives_the_token_for_the_caller(self) -> None:
        # 契约禁止的伪修复形态：`binding_generation = <读来的当前值>`。源码形状 + 行为双重锁定。
        for function in (structure_transaction.undo, structure_transaction.prevalidate_undo,
                         structure_transaction._undo_evidence_prefix,
                         structure_transaction._require_request_generation_token,
                         structure_transaction._monotonic_undo_locked,
                         structure_commands.undo_operation):
            with self.subTest(function=function.__name__):
                source = inspect.getsource(function)
                self.assertEqual(
                    [line.strip() for line in source.splitlines()
                     if re.search(r"^\s*binding_generation\s*=(?!=)", line)], [])

    def test_t9_cli_channel_requires_the_token(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        missing = self.cli_undo(target)
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertEqual(json.loads(missing.stdout)["code"], "OWNERSHIP_GENERATION_REQUIRED")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        token = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        current = self.cli_undo(target, "--binding-generation", str(token))
        self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
        self.assertEqual(json.loads(current.stdout)["code"], "STRUCTURE_UNDONE")

    def test_t10_mcp_schema_and_channel_carry_the_token(self) -> None:
        tool = next(item for item in TOOLS if item["name"] == "undo_structure_operation")
        properties = tool["inputSchema"]["properties"]
        # 结构中性撤销可省略令牌；正文 binding 变化仍由引擎强制核验。
        self.assertEqual(properties["bindingGeneration"]["type"], "integer")
        self.assertNotIn("bindingGeneration", tool["inputSchema"]["required"])
        self.assertIn("list_structure_operations", tool["description"])
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        token = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        self.assertEqual(
            error_code(lambda: call_tool("undo_structure_operation",
                                         {"projectRoot": str(self.root), "operationId": target})),
            "OWNERSHIP_GENERATION_REQUIRED")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        result = call_tool("undo_structure_operation",
                           {"projectRoot": str(self.root), "operationId": target,
                            "bindingGeneration": token})
        self.assertEqual(result["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)

    def test_t11_dashboard_channel_requires_and_echoes_the_token(self) -> None:
        target = self.move(fx.CHAPTER_A, MOVED_PATH)
        token = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        status, payload = self.dashboard_undo({"operationId": target})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "OWNERSHIP_GENERATION_REQUIRED")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        status, payload = self.dashboard_undo({"operationId": target, "bindingGeneration": token - 3})
        # 机制版本 104：冲突类错误统一 409（旧服务器一律 400）。
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MOVED_PATH)
        # 前端回显的就是它在 /api/operations 里看到的那个读数，服务端不代为补齐。
        status, payload = self.dashboard_undo({"operationId": target, "bindingGeneration": token})
        self.assertEqual(status, 200)
        self.assertEqual(payload["code"], "STRUCTURE_UNDONE")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], MANAGED_PATH)




if __name__ == "__main__":
    unittest.main()
