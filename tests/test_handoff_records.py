"""Host handoff protocol tests（§35-§41 / §63）."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.artifacts import sha256_text
from scripts.core.handoff_records import (
    create_handoff,
    handoff_result_status,
    read_handoffs,
    register_handoff_return,
    stop_handoff,
)
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project


class HandoffRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        self.fixture = create_schema5_project(self.root, chapter_count=2)
        self.chapter_id = self.fixture["structure"]["chapters"][0]["chapterId"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(self, **overrides) -> dict:
        args = {"chapter_id": self.chapter_id, "task": "write_chapter", "station": "writer",
                "produces_candidate": True, "context_hash": "ctx-1", "pov": "沈砚",
                "effective_knowledge_mode": "curated", "knowledge_mode_source": "task_override"}
        args.update(overrides)
        return create_handoff(self.root, **args)["handoff"]

    def test_create_binds_required_identity_and_returns_record(self) -> None:
        handoff = self._create()
        self.assertTrue(handoff["handoffId"].startswith("HO-"))
        self.assertEqual(handoff["chapterId"], self.chapter_id)
        self.assertEqual(handoff["effectiveKnowledgeMode"], "curated")
        self.assertEqual(handoff["knowledgeModeSource"], "task_override")
        self.assertEqual(handoff["status"], "created")

    def test_single_track_blocks_concurrent_open_handoff(self) -> None:
        self._create()
        with self.assertRaises(StructureError) as ctx:
            self._create(task="another")
        self.assertEqual(ctx.exception.code, "HANDOFF_SINGLE_TRACK")

    def test_rerun_allowed_only_after_terminal_status(self) -> None:
        handoff = self._create()
        with self.assertRaises(StructureError) as ctx:
            self._create(task="rerun too early", rerun_of=handoff["handoffId"])
        self.assertEqual(ctx.exception.code, "HANDOFF_SINGLE_TRACK")
        stop_handoff(self.root, handoff["handoffId"], reason="作者取消")
        rerun = self._create(task="rerun", rerun_of=handoff["handoffId"])
        self.assertEqual(rerun["rerunOf"], handoff["handoffId"])

    def test_context_applicable_requires_proof_and_na_records_honestly(self) -> None:
        with self.assertRaises(StructureError) as ctx:
            self._create(context_hash=None)
        self.assertEqual(ctx.exception.code, "HANDOFF_CONTEXT_PROOF_REQUIRED")
        not_applicable = self._create(context_applicable=False, context_hash=None)
        self.assertFalse(not_applicable["contextApplicable"])
        self.assertIsNone(not_applicable["contextHash"])

    def test_return_is_idempotent_per_request_and_conflicts_on_different_output(self) -> None:
        handoff = self._create()
        first = register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                        return_request_id="RR1", body="宿主产出的正文。")
        replay = register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                         return_request_id="RR1", body="宿主产出的正文。")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(StructureError) as ctx:
            register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                    return_request_id="RR1", body="另一份输出")
        self.assertEqual(ctx.exception.code, "HANDOFF_RETURN_CONFLICT")

    def test_return_saves_candidate_without_activation(self) -> None:
        handoff = self._create(candidate_version=None)
        result = register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                         return_request_id="RR1", body="宿主产出的正文。")
        self.assertEqual(result["candidate"]["code"], "CANDIDATE_SAVED")
        self.assertFalse(result["candidate"]["active"])
        self.assertIsNone(result["candidate"]["activeVersion"])

    def test_late_return_after_dependency_change_is_marked_stale(self) -> None:
        dep = self.root / "工作台" / "handoff-dep.md"
        dep.parent.mkdir(parents=True, exist_ok=True)
        dep.write_text("依赖证据 v1", encoding="utf-8")
        handoff = self._create(dependency_manifest=[{"path": "工作台/handoff-dep.md", "sha256": sha256_text("依赖证据 v1")}])
        # 交接创建后依赖文件变了 → 返回必须标记 stale，且不能覆盖/借走任何新状态
        dep.write_text("依赖证据 v2（被其他客户端修改）", encoding="utf-8")
        result = register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                         return_request_id="RR1", body="宿主产出的正文。")
        self.assertTrue(result["stale"])
        self.assertIn("工作台/handoff-dep.md: changed", result["return"]["staleReasons"])
        status = handoff_result_status(self.root, handoff["handoffId"])
        self.assertEqual(status["status"], "returned")
        self.assertFalse(status["returnSummary"]["current"])

    def test_return_with_wrong_project_identity_is_rejected(self) -> None:
        handoff = self._create()
        # 直接篡改 record 的 projectId 模拟跨项目返回；正常路径由 create 绑定。
        from scripts.core.jsonio import read_json, write_json
        index_path = self.root / ".govel/handoffs/index.json"
        index = read_json(index_path)
        index["handoffs"][handoff["handoffId"]]["projectId"] = "OTHER-PROJECT"
        write_json(index_path, index)
        with self.assertRaises(StructureError) as ctx:
            register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                    return_request_id="RR1", body="x")
        self.assertEqual(ctx.exception.code, "HANDOFF_PROJECT_MISMATCH")

    def test_duplicate_return_id_across_handoffs_is_rejected(self) -> None:
        first = self._create(task="one")
        register_handoff_return(self.root, handoff_id=first["handoffId"],
                                return_request_id="SHARED", body="正文 A")
        second = self._create(task="two")  # first 已 returned，single-track 放行
        with self.assertRaises(StructureError) as ctx:
            register_handoff_return(self.root, handoff_id=second["handoffId"],
                                    return_request_id="SHARED", body="正文 B")
        self.assertEqual(ctx.exception.code, "HANDOFF_RETURN_CONFLICT")

    def test_unknown_handoff_and_missing_body_fail_closed(self) -> None:
        with self.assertRaises(StructureError) as ctx:
            register_handoff_return(self.root, handoff_id="HO-9999-ffffffff",
                                    return_request_id="RR1", body="x")
        self.assertEqual(ctx.exception.code, "HANDOFF_NOT_FOUND")
        handoff = self._create()
        with self.assertRaises(StructureError) as ctx:
            register_handoff_return(self.root, handoff_id=handoff["handoffId"],
                                    return_request_id="RR1", body=None)
        self.assertEqual(ctx.exception.code, "HANDOFF_RETURN_BODY_REQUIRED")

    def test_list_reports_open_handoff_for_reopen_recovery(self) -> None:
        handoff = self._create()
        listing = read_handoffs(self.root)
        self.assertEqual(listing["openHandoffId"], handoff["handoffId"])
        self.assertEqual(len(listing["handoffs"]), 1)


if __name__ == "__main__":
    unittest.main()
