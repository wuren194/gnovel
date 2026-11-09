"""Candidate version protocol adversarial tests（§24-§28 / §61 / §62）."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.candidate_versions import (
    activate_candidate_version,
    candidate_recovery_status,
    list_candidate_versions,
    read_candidate_version,
    save_candidate_version,
)
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_store import StructureError
from scripts.core.workflow import WorkflowError, require_approved_artifact
from tests.fixtures_workflow import review_ready_project

PROSE_V1 = "他把钥匙放回桌上，合上了登记本。"


def save(root: Path, chapter_id: str, body: str, request_id: str, base: int | None, **kw) -> dict:
    return save_candidate_version(root, chapter_id, body=body, request_id=request_id,
                                  base_candidate_version=base, **kw)


class CandidateVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        fixture = review_ready_project(self.root)
        self.chapter_id = fixture["structure"]["chapters"][0]["chapterId"]
        self.prose = PROSE_V1

    def tearDown(self) -> None:
        self.temp.cleanup()

    # ---- 保存协议（§24） ---------------------------------------------------------

    def test_save_returns_receipt_with_identity_and_hash(self) -> None:
        receipt = save(self.root, self.chapter_id, self.prose, "R1", None)
        self.assertEqual(receipt["code"], "CANDIDATE_SAVED")
        self.assertEqual(receipt["version"], 1)
        self.assertFalse(receipt["active"])
        self.assertIsNone(receipt["activeVersion"])

    def test_same_request_id_same_content_replays_original_receipt(self) -> None:
        first = save(self.root, self.chapter_id, self.prose, "R1", None)
        replay = save(self.root, self.chapter_id, self.prose, "R1", None)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["version"], first["version"])
        self.assertEqual(len(list_candidate_versions(self.root, self.chapter_id)["versions"]), 1)

    def test_same_request_id_different_content_is_rejected(self) -> None:
        save(self.root, self.chapter_id, self.prose, "R1", None)
        with self.assertRaises(StructureError) as ctx:
            save(self.root, self.chapter_id, "不同的内容", "R1", None)
        self.assertEqual(ctx.exception.code, "CANDIDATE_REQUEST_CONFLICT")

    def test_stale_base_is_rejected_for_browser_saves(self) -> None:
        save(self.root, self.chapter_id, self.prose, "R1", None)
        with self.assertRaises(StructureError) as ctx:
            save(self.root, self.chapter_id, "第二版", "R2", 0)
        self.assertEqual(ctx.exception.code, "CANDIDATE_BASE_STALE")

    def test_versions_are_immutable_and_hash_verified(self) -> None:
        save(self.root, self.chapter_id, self.prose, "R1", None)
        save(self.root, self.chapter_id, "第二版正文", "R2", 1)
        row = read_candidate_version(self.root, self.chapter_id, 1)
        self.assertEqual(row["body"], self.prose)
        # 直接篡改版本文件必须被 hash 校验拦下（不可变性可验证，不只是约定）。
        index_dir = self.root / ".govel/workbench/candidates" / self.chapter_id
        body_file = next(path for path in index_dir.glob("v0001-*.md"))
        body_file.write_text("被外部篡改的字节", encoding="utf-8")
        with self.assertRaises(StructureError) as ctx:
            read_candidate_version(self.root, self.chapter_id, 1)
        self.assertEqual(ctx.exception.code, "CANDIDATE_VERSION_CORRUPT")

    # ---- Case 1：保存 v2 不激活，v1 的批准纹丝不动（§61 Case 1） ------------------

    def test_saved_not_active_keeps_v1_review_and_formal_prose(self) -> None:
        prose_path = self.root / ".govel/workflow.json"
        before = prose_path.read_bytes()
        save(self.root, self.chapter_id, self.prose, "R1", None)
        v2 = save(self.root, self.chapter_id, self.prose + "\n他吹灭了灯。", "R2", 1)
        # v2 可读
        self.assertEqual(read_candidate_version(self.root, self.chapter_id, v2["version"])["body"], self.prose + "\n他吹灭了灯。")
        # v1 的审校仍然有效：批准闸门对 v1 工件链照常通过
        workflow = read_json(self.root / ".govel/workflow.json")
        approved = require_approved_artifact(workflow, self.chapter_id, "stage_b_review")
        self.assertTrue(approved)
        # 正式正文字节不变（首章此时没有正式文件，workflow.json 也未被触碰）
        self.assertEqual(prose_path.read_bytes(), before)

    # ---- Case 2：激活原子生效 + 旧证据失效（§61 Case 2） -------------------------

    def _activate_v2(self) -> dict:
        save(self.root, self.chapter_id, self.prose, "R1", None)
        v2 = save(self.root, self.chapter_id, self.prose + "\n他吹灭了灯。", "R2", 1)
        return activate_candidate_version(self.root, self.chapter_id,
                                          candidate_version=v2["version"], expected_active_token=0)

    def test_activation_requires_current_active_token(self) -> None:
        self._activate_v2()
        with self.assertRaises(StructureError) as ctx:
            activate_candidate_version(self.root, self.chapter_id, candidate_version=1, expected_active_token=0)
        self.assertEqual(ctx.exception.code, "CANDIDATE_ACTIVE_TOKEN_CONFLICT")
        # 失败方基于旧 token 的重试永远进不来，但刷新基线后可以另存新版本（独立路径）。
        save(self.root, self.chapter_id, "第三版", "R3", 2)

    def test_activation_atomically_invalidates_old_evidence(self) -> None:
        activation = self._activate_v2()
        self.assertTrue(activation["changed"])
        self.assertEqual(sorted(activation["invalidatedArtifacts"]), ["diagnostics", "stage_b_review", "state_candidate"])
        workflow = read_json(self.root / ".govel/workflow.json")
        entries = workflow["artifacts"][self.chapter_id]
        for kind in ("diagnostics", "stage_b_review", "state_candidate"):
            self.assertTrue(entries[kind].get("invalidatedBy"), f"{kind} must carry invalidatedBy")
            self.assertEqual(entries[kind]["invalidatedBy"]["reason"], "candidate_activation")
        approval = workflow["approvals"][self.chapter_id]
        self.assertIn("invalidatedByCandidateActivation", approval)
        # 失效后闸门 fail closed：旧批准无法授权新正文
        with self.assertRaises(WorkflowError):
            require_approved_artifact(workflow, self.chapter_id, "stage_b_review")

    def test_activation_blocks_finalization_of_new_body(self) -> None:
        self._activate_v2()
        preflight = preflight_finalization(self.root, self.chapter_id)
        self.assertTrue(preflight.blockers, "activation must leave finalization blocked until the pipeline re-runs")
        # 缺正文 hash 的 stage B 不可登记。
        from tests.fixtures_workflow import register_and_approve
        from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS
        with self.assertRaises(ValueError) as caught:
            register_and_approve(self.root, self.chapter_id, "stage_b_review", {
                "verdict": "POLISH_READY",
                "slots": {slot: {"conclusion": "通过"} for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
                "findings": [],
            })
        self.assertEqual(caught.exception.code, 'REVIEW_FIELD_REQUIRED')
        preflight = preflight_finalization(self.root, self.chapter_id)
        self.assertTrue(preflight.blockers)

    def test_cli_style_call_shares_the_same_core_path(self) -> None:
        """CLI/MCP 走同一 core 函数：激活后证据失效语义一致（§59）。"""
        from scripts.core.operations import call_operation
        self._activate_v2()
        # 用 CLI 参数形状再保存并激活一次，断言与直调结果同构。
        receipt = call_operation("candidate.save-version", self.root, {
            "chapterId": self.chapter_id, "body": "第三版", "requestId": "CLI-1", "baseCandidateVersion": 2})
        self.assertEqual(receipt["version"], 3)
        activation = call_operation("candidate.activate-version", self.root, {
            "chapterId": self.chapter_id, "candidateVersion": 3,
            "expectedActiveToken": list_candidate_versions(self.root, self.chapter_id)["activeToken"]})
        self.assertEqual(activation["activeVersion"], 3)

    # ---- Case 3：保存/中断恢复（§28 / §61 Case 3 / §62 D） ------------------------

    def test_saved_not_active_and_pending_transaction_surface_in_recovery(self) -> None:
        save(self.root, self.chapter_id, self.prose, "R1", None)
        status = candidate_recovery_status(self.root, self.chapter_id)
        self.assertTrue(status["savedNotActive"])
        self.assertTrue(status["consistent"])
        # 模拟激活写集中断留下的未完成事务
        tx_dir = self.root / ".govel/transactions"
        tx_dir.mkdir(parents=True, exist_ok=True)
        write_json(tx_dir / "TX-TESTPENDING.json", {
            "schemaVersion": 2, "transactionId": "TX-TESTPENDING", "operation": "candidate_activate",
            "phase": "prepared", "files": [], "guards": [], "treeGuards": []})
        status = candidate_recovery_status(self.root, self.chapter_id)
        self.assertTrue(status["blockedByRecovery"])
        self.assertEqual(status["pendingTransactions"][0]["transactionId"], "TX-TESTPENDING")
        # 未完成事务恢复完成前，finalization 被 project_lock 挡住（TRANSACTION_INCOMPLETE）。
        from scripts.core.locks import project_lock
        with self.assertRaises(StructureError) as ctx:
            with project_lock(self.root):
                pass
        self.assertEqual(ctx.exception.code, "TRANSACTION_INCOMPLETE")

    # ---- Case 4：两个客户端同基线竞争（§61 Case 4） ------------------------------

    def test_two_clients_same_base_cannot_silently_override_each_other(self) -> None:
        save(self.root, self.chapter_id, self.prose, "R1", None)  # v1
        # 客户端 A 基于 v1 保存 v2 并激活
        a = save(self.root, self.chapter_id, "A 的第二版", "A2", 1)
        activate_candidate_version(self.root, self.chapter_id, candidate_version=a["version"], expected_active_token=0)
        # 客户端 B 也基于 v1：保存被拒（基线过期），拿旧 token 激活也被拒
        with self.assertRaises(StructureError) as ctx:
            save(self.root, self.chapter_id, "B 的第二版", "B2", 1)
        self.assertEqual(ctx.exception.code, "CANDIDATE_BASE_STALE")
        with self.assertRaises(StructureError):
            activate_candidate_version(self.root, self.chapter_id, candidate_version=1, expected_active_token=0)
        # B 刷新基线后另存新版本：只追加，不覆盖 A，也不恢复任何历史批准
        b = save(self.root, self.chapter_id, "B 的第二版", "B2", 2)
        self.assertEqual(b["version"], 3)
        self.assertFalse(b["active"])
        listing = list_candidate_versions(self.root, self.chapter_id)
        self.assertEqual(listing["activeVersion"], a["version"])
        self.assertEqual([row["version"] for row in listing["versions"]], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
