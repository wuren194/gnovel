"""R1 Phase 2 — finalization & refactor transition cutover (synthetic fixtures only).

Everything here runs against throwaway current-binding projects built by
`tests.fixtures_ownership`: no real novel project is read, no real prose is used.
"""
from __future__ import annotations

from tests.fixtures_workflow import evidenced_slots

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.core import prose_binding, refactor, structure_transaction
from scripts.core.artifacts import file_sha256, sha256_json, sha256_text
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.finalization import (accept_external_workflow, finalize_chapter,
                                       preview_accept_external_modification)
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS
from scripts.core.source_model import is_current_binding
from scripts.core.structure_commands import _operation_paths, _plan_hash
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.workflow import WorkflowError
from tests import fixtures_ownership as fx
from tests.fixtures_workflow import register_and_approve

ROOT = Path(__file__).resolve().parents[1]

NEW_PROSE = "她把登记本翻到最后一页。\n"


def sha256_text_digest(text: str) -> str:
    return "sha256:" + sha256_text(text)


def _walk_to_review(root: Path, chapter_id: str, prose: str) -> None:
    """Register the five finalization artifacts and advance the chapter into `review`.

    Mirrors `tests.fixtures_workflow.review_ready_project`, but on top of an existing
    current-binding structure. The plan binding is taken from `outline_binding`, so the
    walk binds the authored source plan fragment.
    """
    from scripts.core.source_plan import outline_binding
    structure = read_json(root / ".govel" / "structure.json")
    chapter = next(row for row in structure["chapters"] if row["chapterId"] == chapter_id)
    workflow = read_json(root / ".govel" / "workflow.json")
    workflow["approvedChapterIds"] = list(dict.fromkeys([*workflow.get("approvedChapterIds", []), chapter_id]))
    write_json(root / ".govel" / "workflow.json", workflow)
    binding = outline_binding(root, structure, chapter)
    plan_field, plan_hash = binding["field"], binding["hash"]
    register_and_approve(root, chapter_id, "stage_a_review", {
        "decision": "adopt", "activeSeats": list(range(1, 11)), plan_field: plan_hash,
        "consensus": [], "disagreements": [], "openItems": [],
    })
    if chapter.get("status") != "planned":
        advance_chapter_status(root, chapter_id, "planned")
    register_and_approve(root, chapter_id, "candidate_prose", {"text": prose})
    advance_chapter_status(root, chapter_id, "draft")
    from scripts.core.mechanical_evidence import run_diagnostics
    result = run_diagnostics(root, chapter_id=chapter_id, mode='plot', intensity_level=1,
                             register=True, advance_lifecycle=True)
    workflow = read_json(root/'.govel/workflow.json')
    from scripts.core.workflow import approve_artifacts
    approve_artifacts(workflow, chapter_id, artifact_hashes={
        'diagnostics': workflow['artifacts'][chapter_id]['diagnostics']['artifactSha256']}, approval_mode='manual')
    write_json(root/'.govel/workflow.json', workflow)
    register_and_approve(root, chapter_id, "stage_b_review", {
        "verdict": "POLISH_READY",
        "slots": evidenced_slots(prose),
        "findings": [], "finalProseSha256": sha256_text(prose),
    })
    import scripts.state_cache as state_cache
    state, design = state_cache.build_state(root), state_cache.build_design(root)
    register_and_approve(root, chapter_id, "state_candidate", {
        "baseStateSha256": sha256_json(state), "designSha256": sha256_json(design),
        plan_field: plan_hash, "finalProseSha256": sha256_text(prose),
        "changes": [],
    })


def binding_project(root: Path, *, chapter_id: str = fx.CHAPTER_B, state: str = "unbound",
                    prose: str | None = None, revision: int = 7) -> dict:
    """A ONE-chapter current-binding project.

    Finalization preflight requires every earlier chapter to be committed and its
    continuity complete (`continuity.require_predecessors_ready`), so a review-ready
    fixture must be chapter 1 and have no predecessors — exactly what
    `tests.fixtures_workflow.review_ready_project` does for the schema-5 builder.
    """
    from scripts.core.structure_store import (PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION,
                                              empty_structure, validate_structure)
    from scripts.core.prose_binding import seal_binding
    root.mkdir(parents=True, exist_ok=True)
    for relative in (".govel", "正文", "规划", "设定", "工作台"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    structure = empty_structure(fx.PROJECT_ID, "绑定测试")
    structure["volumes"] = [{"volumeId": fx.VOLUME_A, "ordinal": 1, "title": "第一卷", "status": "planned"}]
    structure["arcs"] = [{"arcId": fx.ARC_MAIN, "title": "主线", "kind": "main", "status": "planned",
                          "volumeIds": [fx.VOLUME_A], "parentArcId": None, "promise": "",
                          "resolutionTargetChapterId": None}]
    row = fx._chapter(chapter_id, 1, "白塔", "planning_required")
    structure["chapters"] = [row]
    structure["chapterArcLinks"] = [{"chapterId": chapter_id, "arcId": fx.ARC_MAIN, "role": "primary",
                                     "weight": 1.0, "phaseId": None, "notes": ""}]
    structure['fieldDefinitions'] = [{'fieldId': 'FD-BINDING-PLAN', 'key': 'plot.plan', 'label': '章节计划',
                                     'valueType': 'topic', 'appliesTo': ['chapter']}]
    structure['attributes'] = [{'attributeId': 'AT-BINDING-PLAN', 'ownerId': chapter_id,
                               'fieldId': 'FD-BINDING-PLAN', 'values': []}]
    structure['fragments'] = [{'fragmentId': 'SF-BINDING-PLAN', 'ownerId': chapter_id, 'order': 1,
                              'contentRole': 'plan', 'contentBindings': ['AT-BINDING-PLAN'],
                              'text': '主角核对钥匙来源。', 'chapterContract': {'targetRange': [1, 1000],
                              'mode': 'plot', 'intensityLevel': 1, 'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}}]
    structure["projectId"] = fx.PROJECT_ID
    structure["revision"] = revision
    digest = "sha256:" + fx.sha256_hex(prose.encode("utf-8")) if prose else None
    row["proseBinding"] = seal_binding(fx.PROJECT_ID, chapter_id, row["filePath"], state=state,
                                       generation=revision, expected_content_digest=digest)
    if prose is not None:
        (root / "正文" / f"{chapter_id}.md").write_text(prose, encoding="utf-8")
    write_json(root / ".govel" / "project.json", {
        "schemaVersion": PROJECT_SCHEMA_VERSION, "workflowVersion": WORKFLOW_VERSION,
        "projectId": fx.PROJECT_ID, "bookName": "绑定测试", "projectRevision": revision,
        "createdAt": "2026-09-28T00:00:00Z", "approvalMode": "manual"})
    write_json(root / ".govel" / "workflow.json", {
        "schemaVersion": 2, "approvedChapterIds": [], "chapterStates": {}, "artifacts": {},
        "approvals": {}, "repairRounds": {}, "nextAction": None,
        "updatedAt": "2026-09-28T00:00:00Z"})
    validate_structure(structure)
    write_json(root / ".govel" / "structure.json", structure)
    return structure


def review_ready_binding_project(root: Path, *,              chapter_id: str = fx.CHAPTER_B, state: str = "unbound",
                                 prose: str = NEW_PROSE) -> dict:
    binding_project(root, chapter_id=chapter_id, state=state,
                    prose=prose if state == "managed" else None)
    _walk_to_review(root, chapter_id, prose)
    return load_structure(root)


def review_ready_schema5_project(root: Path, *, state: str = "unbound",
                                 prose: str = NEW_PROSE) -> dict:
    """Current-format review fixture with the same ownership transitions."""
    return review_ready_binding_project(root, state=state, prose=prose)


def chapter_of(root: Path, chapter_id: str) -> dict:
    structure = load_structure(root)
    return next(row for row in structure["chapters"] if row["chapterId"] == chapter_id)


def error_code(callable_) -> str:
    try:
        callable_()
    except StructureError as exc:
        return exc.code
    return "NO-ERROR"


def workflow_error_code(callable_) -> str:
    try:
        callable_()
    except (StructureError, WorkflowError) as exc:
        return exc.code
    return "NO-ERROR"


class TempRootCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def chapter(self, chapter_id: str) -> dict:
        return chapter_of(self.root, chapter_id)

    def observation(self, chapter_id: str) -> dict:
        return prose_binding.observe_chapter(self.root, load_structure(self.root), self.chapter(chapter_id))


class SyntheticProjectCase(TempRootCase):

    def setUp(self) -> None:
        super().setUp()
        self.structure = fx.ownership_project(self.root)

    def chapter(self, chapter_id: str) -> dict:
        return chapter_of(self.root, chapter_id)

    def observation(self, chapter_id: str) -> dict:
        return prose_binding.observe_chapter(self.root, load_structure(self.root), self.chapter(chapter_id))


# ---------------------------------------------------------------------------
# Test Group A — observation（契约 §12–§20）
# ---------------------------------------------------------------------------

class ObservationTests(SyntheticProjectCase):
    def test_a1_unbound_empty_slot_is_unbound(self) -> None:
        self.assertEqual(self.observation(fx.CHAPTER_B)["state"], prose_binding.UNBOUND)

    def test_a2_unbound_with_existing_target_is_unknown_target(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_B}.md").write_text("外来字节", encoding="utf-8")
        receipt = self.observation(fx.CHAPTER_B)
        self.assertEqual(receipt["state"], prose_binding.UNKNOWN_TARGET)
        self.assertNotIn("adopt", receipt["allowedActions"])
        self.assertTrue(receipt["requiresExplicitApproval"])

    def test_a2b_identical_bytes_are_still_unknown_target(self) -> None:
        # 契约 §13/§28：候选字节与盘上完全一致也不构成归属。
        (self.root / f"正文/{fx.CHAPTER_B}.md").write_text(NEW_PROSE, encoding="utf-8")
        self.assertEqual(self.observation(fx.CHAPTER_B)["state"], prose_binding.UNKNOWN_TARGET)

    def test_a3_managed_with_matching_digest_is_owned_clean(self) -> None:
        receipt = self.observation(fx.CHAPTER_A)
        self.assertEqual(receipt["state"], prose_binding.OWNED_CLEAN)
        self.assertEqual(receipt["observedContentDigest"], fx.managed_digest())

    def test_a4_managed_with_different_bytes_is_externally_modified(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_A}.md").write_text("被外部改过", encoding="utf-8")
        receipt = self.observation(fx.CHAPTER_A)
        self.assertEqual(receipt["state"], prose_binding.OWNED_MODIFIED_EXTERNALLY)
        self.assertEqual(receipt["expectedContentDigest"], fx.managed_digest())
        self.assertNotEqual(receipt["observedContentDigest"], fx.managed_digest())

    def test_a5_managed_missing_file_is_locator_drift(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_A}.md").unlink()
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.LOCATOR_DRIFT)

    def test_a6_unresolved_is_needs_confirmation(self) -> None:
        receipt = self.observation(fx.CHAPTER_C)
        self.assertEqual(receipt["state"], prose_binding.NEEDS_CONFIRMATION)
        self.assertEqual(receipt["allowedActions"], ["ask_author_decision"])

    def test_a6b_unresolved_with_matching_disk_bytes_still_needs_confirmation(self) -> None:
        # 即使盘上存在字节且某个哈希吻合，unresolved 也绝不自动变成 ownership-positive。
        (self.root / f"正文/{fx.CHAPTER_C}.md").write_text(fx.MANAGED_PROSE, encoding="utf-8")
        self.assertEqual(self.observation(fx.CHAPTER_C)["state"], prose_binding.NEEDS_CONFIRMATION)

    def test_a7_corrupt_integrity_is_binding_invalid(self) -> None:
        path = self.root / ".govel" / "structure.json"
        on_disk = read_json(path)
        on_disk["chapters"][1]["proseBinding"]["integrityDigest"] = "sha256:" + "0" * 64
        write_json(path, on_disk)
        self.assertEqual(self.observation(fx.CHAPTER_B)["state"], prose_binding.BINDING_INVALID)

    def test_a8_alias_conflict_is_conflict(self) -> None:
        # 规范定位符重复在结构校验层就是 STRUCTURE_INVALID（项目致命，根本装载不进来）；
        # 观测层的 CONFLICT 覆盖**物理对象别名**：两个不同定位符解析到同一个 inode。
        os.link(self.root / f"正文/{fx.CHAPTER_A}.md", self.root / f"正文/{fx.CHAPTER_B}.md")
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.CONFLICT)
        self.assertEqual(self.observation(fx.CHAPTER_B)["state"], prose_binding.CONFLICT)

    def test_a9_pending_transaction_outranks_everything(self) -> None:
        transactions = self.root / ".govel" / "transactions"
        transactions.mkdir(parents=True, exist_ok=True)
        write_json(transactions / "TX-DEADBEEF.json",
                   {"transactionId": "TX-DEADBEEF", "phase": "prepared", "files": []})
        receipt = self.observation(fx.CHAPTER_A)
        self.assertEqual(receipt["state"], prose_binding.TRANSACTION_PENDING)
        self.assertEqual(receipt["nextSafeAction"], "workflow_recover")

    def test_a10_observation_is_never_persisted(self) -> None:
        before = (self.root / ".govel" / "structure.json").read_bytes()
        self.observation(fx.CHAPTER_A)
        self.assertEqual((self.root / ".govel" / "structure.json").read_bytes(), before)


# ---------------------------------------------------------------------------
# Test Group B — first finalization（契约 §25–§29）
# ---------------------------------------------------------------------------

class FirstFinalizationTests(TempRootCase):
    def setUp(self) -> None:
        super().setUp()
        # 单章工程：定稿前置要求更早的章已入库且连续性完成，所以可定稿的章必须是第 1 章。
        binding_project(self.root)
        self.structure = review_ready_binding_project(self.root)
        self.prose_path = self.root / f"正文/{fx.CHAPTER_B}.md"

    def test_b1_unbound_empty_slot_finalizes_to_managed(self) -> None:
        self.assertFalse(self.prose_path.exists())
        result = finalize_chapter(self.root, fx.CHAPTER_B, skip_title=True,
                                  binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])
        self.assertEqual(result["code"], "CHAPTER_FINALIZED")
        chapter = self.chapter(fx.CHAPTER_B)
        self.assertEqual(chapter["proseBinding"]["state"], "managed")
        self.assertEqual(chapter["proseBinding"]["expectedContentDigest"], sha256_text_digest(NEW_PROSE))
        self.assertEqual(self.prose_path.read_text(encoding="utf-8"), NEW_PROSE)

    def test_b2_prose_and_binding_ride_one_committed_write_set(self) -> None:
        seen: list[list[str]] = []
        from scripts.core import write_sets
        real = write_sets.apply_write_set

        def watching(root, outputs, **kwargs):
            seen.append(sorted(outputs))
            return real(root, outputs, **kwargs)

        with mock.patch("scripts.core.write_sets.apply_write_set", watching):
            finalize_chapter(self.root, fx.CHAPTER_B, skip_title=True,
                             binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])
        self.assertEqual(len(seen), 1)
        self.assertIn(f"正文/{fx.CHAPTER_B}.md", seen[0])
        self.assertIn(".govel/structure.json", seen[0])

    def test_b3_target_appearing_after_locked_preflight_is_refused(self) -> None:
        sentinel = "外部抢占字节".encode("utf-8")
        from scripts.core import derived_views
        real = derived_views.derived_outputs
        structure_before = (self.root / ".govel" / "structure.json").read_bytes()

        def racing(*args, **kwargs):
            self.prose_path.write_bytes(sentinel)
            return real(*args, **kwargs)

        with mock.patch("scripts.core.derived_views.derived_outputs", racing):
            self.assertIn(
                workflow_error_code(lambda: finalize_chapter(
                    self.root, fx.CHAPTER_B, skip_title=True,
                    binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])),
                {"PROSE_TARGET_NOT_OWNED", "WRITE_SET_CONFLICT", "WRITE_SET_MISMATCH"})
        # 抢占进来的字节一个字节都没被覆盖；结构、事务目录与状态都没有被半途改动。
        self.assertEqual(self.prose_path.read_bytes(), sentinel)
        self.assertEqual((self.root / ".govel" / "structure.json").read_bytes(), structure_before)
        self.assertEqual(self.chapter(fx.CHAPTER_B)["proseBinding"]["state"], "unbound")
        self.assertFalse(list((self.root / ".govel" / "transactions").glob("*.json")))

    def test_b4_target_appearing_with_identical_bytes_is_still_refused(self) -> None:
        from scripts.core import derived_views
        real = derived_views.derived_outputs

        def racing(*args, **kwargs):
            self.prose_path.write_text(NEW_PROSE, encoding="utf-8")
            return real(*args, **kwargs)

        with mock.patch("scripts.core.derived_views.derived_outputs", racing):
            self.assertIn(
                workflow_error_code(lambda: finalize_chapter(
                    self.root, fx.CHAPTER_B, skip_title=True,
                    binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])),
                {"PROSE_TARGET_NOT_OWNED", "WRITE_SET_CONFLICT"})
        self.assertEqual(self.chapter(fx.CHAPTER_B)["proseBinding"]["state"], "unbound")

    def test_b5_generation_equals_the_committed_structure_revision(self) -> None:
        before = load_structure(self.root)
        finalize_chapter(self.root, fx.CHAPTER_B, skip_title=True,
                         binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])
        after = load_structure(self.root)
        chapter = next(row for row in after["chapters"] if row["chapterId"] == fx.CHAPTER_B)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(chapter["proseBinding"]["generation"], after["revision"])

    def test_b6_expected_digest_is_sha256_of_committed_prose(self) -> None:
        finalize_chapter(self.root, fx.CHAPTER_B, skip_title=True,
                         binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])
        committed = self.prose_path.read_bytes()
        expected = "sha256:" + hashlib.sha256(committed).hexdigest()
        self.assertEqual(self.chapter(fx.CHAPTER_B)["proseBinding"]["expectedContentDigest"], expected)

    def test_b7_missing_generation_token_is_refused(self) -> None:
        self.assertEqual(
            workflow_error_code(lambda: finalize_chapter(self.root, fx.CHAPTER_B, skip_title=True)),
            "OWNERSHIP_GENERATION_CONFLICT")
        self.assertFalse(self.prose_path.exists())

    def test_b8_unknown_target_is_never_adopted_by_finalization(self) -> None:
        self.prose_path.write_text("不是本章的字节", encoding="utf-8")
        preflight = preflight_finalization(self.root, fx.CHAPTER_B, skip_title=True,
                                           binding_generation=self.chapter(fx.CHAPTER_B)["proseBinding"]["generation"])
        self.assertIn("PROSE_TARGET_NOT_OWNED", [b["code"] for b in preflight.blockers])


# ---------------------------------------------------------------------------
# Test Group C — re-finalization（契约 §31–§33）
# ---------------------------------------------------------------------------

class ReFinalizationTests(TempRootCase):
    def setUp(self) -> None:
        super().setUp()
        # 受管章 + 盘上字节：再次定稿写回的候选正文与盘上完全一致（H2 == H1）。
        self.structure = review_ready_binding_project(
            self.root, chapter_id=fx.CHAPTER_A, state="managed", prose=NEW_PROSE)
        self.prose_path = self.root / f"正文/{fx.CHAPTER_A}.md"

    def test_c1_owned_clean_refinalizes_to_the_new_bytes(self) -> None:
        before = self.chapter(fx.CHAPTER_A)
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)
        finalize_chapter(self.root, fx.CHAPTER_A, skip_title=True,
                         binding_generation=before["proseBinding"]["generation"])
        chapter = self.chapter(fx.CHAPTER_A)
        self.assertEqual(chapter["proseBinding"]["state"], "managed")
        self.assertEqual(chapter["proseBinding"]["expectedContentDigest"], sha256_text_digest(NEW_PROSE))
        self.assertGreater(chapter["proseBinding"]["generation"], before["proseBinding"]["generation"])
        self.assertEqual(self.prose_path.read_text(encoding="utf-8"), NEW_PROSE)

    def test_c1b_generation_advances_even_when_bytes_are_unchanged(self) -> None:
        # candidate 与盘上字节完全相同（H2 == H1）：正式 re-finalization 仍是一次
        # binding-changing 提交，generation 必须前进（契约 §33）。
        before = self.chapter(fx.CHAPTER_A)
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)
        finalize_chapter(self.root, fx.CHAPTER_A, skip_title=True,
                         binding_generation=before["proseBinding"]["generation"])
        chapter = self.chapter(fx.CHAPTER_A)
        self.assertEqual(chapter["proseBinding"]["expectedContentDigest"], sha256_text_digest(NEW_PROSE))
        self.assertEqual(chapter["proseBinding"]["generation"], load_structure(self.root)["revision"])
        self.assertGreater(chapter["proseBinding"]["generation"], before["proseBinding"]["generation"])

    def test_c2_disk_change_after_preview_is_refused_at_commit(self) -> None:
        generation = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        from scripts.core import derived_views
        real = derived_views.derived_outputs

        def racing(*args, **kwargs):
            self.prose_path.write_text("预览之后被改", encoding="utf-8")
            return real(*args, **kwargs)

        with mock.patch("scripts.core.derived_views.derived_outputs", racing):
            self.assertIn(
                workflow_error_code(lambda: finalize_chapter(
                    self.root, fx.CHAPTER_A, skip_title=True, binding_generation=generation)),
                {"PROSE_EXTERNAL_MODIFICATION", "WRITE_SET_CONFLICT"})

    def test_c3_stale_binding_generation_is_refused(self) -> None:
        generation = self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"]
        self.assertEqual(
            workflow_error_code(lambda: finalize_chapter(
                self.root, fx.CHAPTER_A, skip_title=True, binding_generation=generation + 5)),
            "OWNERSHIP_GENERATION_CONFLICT")

    def test_c4_externally_modified_is_not_refinalizable(self) -> None:
        self.prose_path.write_text("外部改动", encoding="utf-8")
        self.assertEqual(
            workflow_error_code(lambda: finalize_chapter(
                self.root, fx.CHAPTER_A, skip_title=True,
                binding_generation=self.chapter(fx.CHAPTER_A)["proseBinding"]["generation"])),
            "PROSE_EXTERNAL_MODIFICATION")


# ---------------------------------------------------------------------------
# Test Group D — external modification（契约 §34–§38）
# ---------------------------------------------------------------------------

class ExternalModificationTests(TempRootCase):
    def setUp(self) -> None:
        super().setUp()
        self.structure = binding_project(self.root, chapter_id=fx.CHAPTER_A, state="managed",
                                         prose=fx.MANAGED_PROSE)
        self.prose_path = self.root / f"正文/{fx.CHAPTER_A}.md"
        self.prose_path.write_text("外部写入的正文", encoding="utf-8")
        self.external_digest = "sha256:" + hashlib.sha256(self.prose_path.read_bytes()).hexdigest()

    def test_d1_normal_finalization_is_blocked(self) -> None:
        chapter = self.chapter(fx.CHAPTER_A)
        preflight = preflight_finalization(
            self.root, fx.CHAPTER_A, skip_title=True,
            binding_generation=chapter["proseBinding"]["generation"])
        self.assertIn("PROSE_EXTERNAL_MODIFICATION", [b["code"] for b in preflight.blockers])
        self.assertEqual(self.prose_path.read_text(encoding="utf-8"), "外部写入的正文")

    def test_d2_normal_locator_move_is_blocked(self) -> None:
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path="正文/甲.md")),
            "PROSE_EXTERNAL_MODIFICATION")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], f"正文/{fx.CHAPTER_A}.md")

    def test_d3_preview_reports_the_whole_token(self) -> None:
        chapter = self.chapter(fx.CHAPTER_A)
        plan = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        parameters = plan["parameters"]
        self.assertEqual(parameters["chapterId"], fx.CHAPTER_A)
        self.assertEqual(parameters["expectedContentDigest"], chapter["proseBinding"]["expectedContentDigest"])
        self.assertEqual(parameters["observedContentDigest"], self.external_digest)
        self.assertEqual(parameters["bindingGeneration"], chapter["proseBinding"]["generation"])
        self.assertEqual(parameters["filePath"], chapter["filePath"])
        self.assertTrue(plan["requiresApproval"])

    def test_d4_accept_semantics_are_frozen_in_the_preview_plan(self) -> None:
        # §36/§37/§10.2 的验收语义全部在预览计划里成立（提交本身 fail closed，见 N 组）：
        # 期望哈希 = 当前盘上 H2、generation 前进到新提交版本、integrityDigest 重新封签、
        # 正文字节一个都不重写、章节回退 stale。
        before = self.chapter(fx.CHAPTER_A)
        before_revision = load_structure(self.root)["revision"]
        before_bytes = self.prose_path.read_bytes()
        plan = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        staged = read_json(_operation_paths(self.root, plan["operationId"])[0])
        proposed = staged["proposedStructure"]
        chapter = next(row for row in proposed["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        binding = chapter["proseBinding"]
        self.assertEqual(proposed["revision"], before_revision + 1)
        self.assertEqual(binding["state"], "managed")
        self.assertEqual(binding["expectedContentDigest"], self.external_digest)
        self.assertEqual(binding["generation"], before_revision + 1)
        self.assertEqual(binding["generation"], proposed["revision"])
        self.assertEqual(binding["integrityDigest"], prose_binding.seal_binding(
            fx.PROJECT_ID, fx.CHAPTER_A, chapter["filePath"], state="managed",
            generation=before_revision + 1,
            expected_content_digest=self.external_digest)["integrityDigest"])
        self.assertEqual(chapter["status"], "stale")
        self.assertEqual(staged["parameters"]["expectedContentDigest"],
                         before["proseBinding"]["expectedContentDigest"])
        self.assertEqual(staged["parameters"]["observedContentDigest"], self.external_digest)
        self.assertEqual(self.prose_path.read_bytes(), before_bytes)

    def test_d5_accept_does_not_finalize_the_chapter(self) -> None:
        plan = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        staged = self._staged_workflow(plan["operationId"])
        self.assertNotIn(fx.CHAPTER_A, staged.get("continuity", {}))
        self.assertNotEqual(staged.get("nextAction", {}).get("action"), "continuity")
        self.assertEqual(staged["nextAction"]["action"], "planner")

    def test_d6_prose_dependent_approvals_are_invalidated_by_the_transaction(self) -> None:
        workflow = read_json(self.root / ".govel" / "workflow.json")
        workflow.setdefault("approvals", {})[fx.CHAPTER_A] = {
            "approved": True, "artifactHashes": {"stage_b_review": "x"}, "finalized": True,
        }
        workflow.setdefault("artifacts", {})[fx.CHAPTER_A] = {
            "stage_b_review": {"artifactSha256": "x"}, "stage_a_review": {"artifactSha256": "y"}}
        workflow.setdefault("continuity", {})[fx.CHAPTER_A] = {"batchHash": "kept"}
        write_json(self.root / ".govel" / "workflow.json", workflow)
        plan = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        after = self._staged_workflow(plan["operationId"])
        approval = after["approvals"][fx.CHAPTER_A]
        self.assertFalse(approval["approved"])
        self.assertNotIn("artifactHashes", approval)
        self.assertEqual(approval["invalidatedBy"], "prose_external_modification")
        self.assertEqual(after["artifacts"][fx.CHAPTER_A]["stage_b_review"]["invalidatedBy"],
                         "prose_external_modification")
        # stage_a_review 只依赖大纲推演：正文外部改动不推定它失效（§10.3）。
        self.assertNotIn("invalidatedBy", after["artifacts"][fx.CHAPTER_A]["stage_a_review"])
        # 历史档案原样保留：不写 continuity 成功记录、不宣布审校通过。
        self.assertEqual(after["continuity"][fx.CHAPTER_A], {"batchHash": "kept"})

    def test_d7_accept_requires_a_fresh_preview_and_locked_reverification(self) -> None:
        plan = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        # 预览与提交之间磁盘再次变化：锁内重验必须拒绝，绝不按旧计划落盘。这道 current-state
        # CAS 与 authority 门槛正交且排在它之前——即使将来有了特权通道，旧计划照样被拒。
        self.prose_path.write_text("又一次外部写入", encoding="utf-8")
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"],
                approved=True, actor="author")),
            "PROSE_CONTENT_CHANGED")


    def test_d9_workflow_invalidation_never_invents_a_conclusion(self) -> None:
        workflow = {"approvals": {fx.CHAPTER_A: {"approved": True, "finalized": True}},
                    "artifacts": {fx.CHAPTER_A: {"stage_b_review": {"artifactSha256": "x"}}},
                    "continuity": {fx.CHAPTER_A: {"batchHash": "kept"}},
                    "chapterStates": {fx.CHAPTER_A: {"status": "stable"}}}
        after = accept_external_workflow(workflow, fx.CHAPTER_A)
        # 历史档案原样保留；不写 continuity 成功记录、不宣布审校通过、不自动定序。
        self.assertEqual(after["continuity"][fx.CHAPTER_A], {"batchHash": "kept"})
        self.assertNotIn("canonAccepted", after)
        self.assertEqual(after["chapterStates"][fx.CHAPTER_A]["status"], "stale")

    def _staged_workflow(self, operation_id: str) -> dict:
        """The workflow half the transaction would stage — read from the real staged plan."""
        staged = self.root / f".govel/plans/{operation_id}.files/.govel/workflow.json"
        self.assertTrue(staged.is_file(), "accept-external plan must stage its workflow invalidation")
        return read_json(staged)


# ---------------------------------------------------------------------------
# Test Group N — Accept External Modification capability separation（契约 §10.1 返修）
# ---------------------------------------------------------------------------

class AcceptExternalCapabilitySeparationTests(TempRootCase):
    """同一 unprivileged caller 不得 preview + approve + commit（契约 §10.1）。

    仓库没有任何不可由普通调用者伪造的特权批准通道，因此 `accept_external_modification`
    的提交在锁内、任何写入之前以 `APPROVAL_CAPABILITY_REQUIRED` fail closed；
    `--approve` / `approved=True` / actor 字符串 / planHash 回传都不能自造 authority。
    """

    def setUp(self) -> None:
        super().setUp()
        binding_project(self.root, chapter_id=fx.CHAPTER_A, state="managed", prose=fx.MANAGED_PROSE)
        self.prose_path = self.root / f"正文/{fx.CHAPTER_A}.md"
        self.prose_path.write_text("外部写入的正文", encoding="utf-8")
        self.external_digest = "sha256:" + hashlib.sha256(self.prose_path.read_bytes()).hexdigest()
        self.plan = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        self.plan_path, _, _ = _operation_paths(self.root, self.plan["operationId"])

    # --- sentinels：拒绝必须发生在任何写入之前 ---------------------------------

    def _side_effects(self) -> dict:
        return {
            "structure": (self.root / ".govel/structure.json").read_bytes(),
            "workflow": (self.root / ".govel/workflow.json").read_bytes(),
            "prose": self.prose_path.read_bytes(),
            "journal": (self.root / f".govel/operations/{self.plan['operationId']}.json").exists(),
            "backup": (self.root / f".govel/backups/{self.plan['operationId']}").exists(),
            "plan": self.plan_path.is_file(),
        }

    def _assert_refused_without_side_effects(self, callable_, expected_code: str) -> None:
        before = self._side_effects()
        self.assertEqual(error_code(callable_), expected_code)
        self.assertEqual(self._side_effects(), before)

    # --- A / E：同一 caller 自批准与 actor 字符串伪装 ---------------------------

    def test_n1_self_approved_commit_is_refused_for_every_actor_string(self) -> None:
        for actor in ("author", "human_ui", "human", "cli"):
            with self.subTest(actor=actor):
                self._assert_refused_without_side_effects(
                    lambda actor=actor: refactor.commit_refactor(
                        self.root, operation_id=self.plan["operationId"],
                        plan_hash=self.plan["planHash"], approved=True, actor=actor),
                    "APPROVAL_CAPABILITY_REQUIRED")

    # --- C：direct internal API（generic transaction）不能绕过 -----------------

    def test_n2_generic_transaction_commit_is_refused_too(self) -> None:
        self._assert_refused_without_side_effects(
            lambda: structure_transaction.commit(
                self.root, operation_id=self.plan["operationId"],
                plan_hash=self.plan["planHash"], approved=True, actor="human_ui"),
            "APPROVAL_CAPABILITY_REQUIRED")

    # --- D：planHash 不是 capability -------------------------------------------

    def test_n3_plan_hash_echo_is_not_a_capability(self) -> None:
        # 持有**正确**的 planHash（即「就是被预览的那一份计划」）仍不构成 authority……
        self._assert_refused_without_side_effects(
            lambda: refactor.commit_refactor(
                self.root, operation_id=self.plan["operationId"],
                plan_hash=self.plan["planHash"], approved=True, actor="human_ui"),
            "APPROVAL_CAPABILITY_REQUIRED")
        # ……而错误的 planHash 在更早的完整性检查处就被拒（证明拒绝不是「任何错误都行」）。
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=self.plan["operationId"],
                plan_hash="0" * 64, approved=True, actor="human_ui")),
            "PLAN_HASH_MISMATCH")

    # --- 伪造计划：改 operation 字符串 / 拆掉 transitions -----------------------

    def _forge_plan(self, mutate) -> dict:
        raw = read_json(self.plan_path)
        mutate(raw)
        raw["planHash"] = _plan_hash({key: value for key, value in raw.items() if key != "planHash"})
        write_json(self.plan_path, raw)
        return raw

    def test_n4_rewriting_the_operation_string_does_not_manufacture_authority(self) -> None:
        # 计划文件不是签名的：调用方可以重写 operation 并重算 planHash。authority 门槛按
        # **声明的 transition kind** 判定，伪造 operation 名同样 fail closed。
        def rename_operation(plan: dict) -> None:
            plan["operation"] = "refactor_relocate_chapter_paths"

        raw = self._forge_plan(rename_operation)
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=raw["operationId"], plan_hash=raw["planHash"],
                approved=True, actor="human_ui")),
            "APPROVAL_CAPABILITY_REQUIRED")

    def test_n5_stripping_transitions_hits_the_writer_boundary(self) -> None:
        # 连 transition 一起拆掉、只留手搓的 proposedStructure：写入者边界独立重推
        # （authorized == changed 精确相等）先一步拒绝——authority 门槛不是唯一防线。
        def strip_transitions(plan: dict) -> None:
            plan["operation"] = "refactor_rename_chapter"
            plan.pop("bindingTransitions", None)

        raw = self._forge_plan(strip_transitions)
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=raw["operationId"], plan_hash=raw["planHash"],
                approved=True, actor="human_ui")),
            "OWNERSHIP_UNAUTHORIZED_MUTATION")

    # --- §8：保留的 TOCTOU / CAS 证据必须继续生效 ------------------------------

    def test_n6_locked_reverification_still_rejects_a_moved_disk(self) -> None:
        self.prose_path.write_text("又一次外部写入", encoding="utf-8")
        self._assert_refused_without_side_effects(
            lambda: refactor.commit_refactor(
                self.root, operation_id=self.plan["operationId"],
                plan_hash=self.plan["planHash"], approved=True, actor="author"),
            "PROSE_CONTENT_CHANGED")

    def test_n7_stale_generation_and_tampered_digests_still_reject(self) -> None:
        structure = load_structure(self.root)
        chapter = next(row for row in structure["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        base = read_json(self.plan_path)

        def plan_with(**overrides) -> dict:
            transition = {**base["bindingTransitions"][0], **overrides}
            return {**base, "bindingTransitions": [transition]}

        self.assertEqual(
            error_code(lambda: prose_binding.require_plan_transitions_current(
                self.root, structure, plan_with(bindingGeneration=chapter["proseBinding"]["generation"] + 5))),
            "OWNERSHIP_GENERATION_CONFLICT")
        self.assertEqual(
            error_code(lambda: prose_binding.require_plan_transitions_current(
                self.root, structure, plan_with(expectedContentDigest="sha256:" + "1" * 64))),
            "PROSE_CONTENT_CHANGED")
        self.assertEqual(
            error_code(lambda: prose_binding.require_plan_transitions_current(
                self.root, structure, plan_with(observedContentDigest="sha256:" + "2" * 64))),
            "PROSE_CONTENT_CHANGED")

    def test_n8_a_refused_commit_does_not_consume_or_stale_the_preview(self) -> None:
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=self.plan["operationId"],
                plan_hash=self.plan["planHash"], approved=True, actor="author")),
            "APPROVAL_CAPABILITY_REQUIRED")
        # 拒绝不消耗计划：计划文件仍在，且预览/观测照常可用，盘上状态未被触碰。
        self.assertTrue(self.plan_path.is_file())
        fresh = preview_accept_external_modification(self.root, chapter_id=fx.CHAPTER_A, actor="author")
        self.assertEqual(fresh["parameters"]["observedContentDigest"], self.external_digest)
        self.assertEqual(self._side_effects()["prose"],
                         "外部写入的正文".encode("utf-8"))

    # --- B：CLI --approve 不能自己制造 authority -------------------------------

    def _run_cli(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "workflow",
             "accept-external", "--project-root", str(self.root), *args],
            capture_output=True, text=True, timeout=60)
        return proc.returncode, json.loads(proc.stdout)

    def test_n9_cli_approve_flag_cannot_manufacture_authority(self) -> None:
        before = self._side_effects()
        code, payload = self._run_cli("--chapter-id", fx.CHAPTER_A,
                                      "--operation-id", self.plan["operationId"],
                                      "--plan-hash", self.plan["planHash"], "--approve")
        self.assertEqual(code, 1)
        self.assertEqual(payload["code"], "APPROVAL_CAPABILITY_REQUIRED")
        self.assertIn("authority", payload["error"])
        self.assertEqual(self._side_effects(), before)

    def test_n10_cli_preview_still_works_without_the_commit_flags(self) -> None:
        code, payload = self._run_cli("--chapter-id", fx.CHAPTER_A)
        self.assertEqual(code, 0)
        self.assertEqual(payload["operation"], "accept_external_modification")
        self.assertEqual(payload["parameters"]["observedContentDigest"], self.external_digest)

    # --- 阴性对照：只收紧 accept_external，别的 refactor 不受影响 ---------------

    def test_n11_ordinary_refactors_still_commit(self) -> None:
        control = Path(self.temp.name) / "control"
        fx.ownership_project(control)
        move = refactor.preview_relocate_chapter_paths(control, chapter_id=fx.CHAPTER_B,
                                                       file_path="正文/空槽-B.md")
        result = refactor.commit_refactor(control, operation_id=move["operationId"],
                                          plan_hash=move["planHash"], approved=True, actor="test")
        self.assertEqual(result["code"], "STRUCTURE_COMMITTED")
        rename = refactor.preview_rename_chapter(control, chapter_id=fx.CHAPTER_C, new_title="悬案·改")
        result = refactor.commit_refactor(control, operation_id=rename["operationId"],
                                          plan_hash=rename["planHash"], approved=True, actor="test")
        self.assertEqual(result["code"], "STRUCTURE_COMMITTED")
        self.assertEqual(chapter_of(control, fx.CHAPTER_C)["title"], "悬案·改")


# ---------------------------------------------------------------------------
# Test Group E — managed locator move（契约 §41–§44 / §50 / §51）
# ---------------------------------------------------------------------------

class ManagedMoveTests(SyntheticProjectCase):
    def _move(self, target: str):
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path=target)
        return refactor.commit_refactor(self.root, operation_id=plan["operationId"],
                                        plan_hash=plan["planHash"], approved=True, actor="test")

    def test_e1_managed_move_carries_bytes_and_advances_generation(self) -> None:
        source = self.root / f"正文/{fx.CHAPTER_A}.md"
        before_bytes = source.read_bytes()
        before = self.chapter(fx.CHAPTER_A)
        target = "正文/搬家-0001.md"
        self._move(target)
        after = self.chapter(fx.CHAPTER_A)
        self.assertFalse(source.exists())
        self.assertEqual((self.root / target).read_bytes(), before_bytes)
        self.assertEqual(after["filePath"], target)
        # 纯移动不改变正文字节：expect digest 不变，generation/整包摘要随提交前进。
        self.assertEqual(after["proseBinding"]["expectedContentDigest"],
                         before["proseBinding"]["expectedContentDigest"])
        self.assertGreater(after["proseBinding"]["generation"], before["proseBinding"]["generation"])
        self.assertEqual(after["proseBinding"]["generation"], load_structure(self.root)["revision"])

    def test_e2_existing_destination_is_refused(self) -> None:
        (self.root / "正文" / "占用.md").write_text("别人的字节", encoding="utf-8")
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path="正文/占用.md")),
            "PROSE_TARGET_NOT_OWNED")
        self.assertEqual((self.root / "正文" / "占用.md").read_text(encoding="utf-8"), "别人的字节")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], f"正文/{fx.CHAPTER_A}.md")

    def test_e3_source_digest_mismatch_is_refused(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_A}.md").write_text("改了", encoding="utf-8")
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path="正文/搬家.md")),
            "PROSE_EXTERNAL_MODIFICATION")

    def test_e4_missing_source_is_locator_drift(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_A}.md").unlink()
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path="正文/搬家.md")),
            "PROSE_LOCATOR_DRIFT")

    def test_e5_stale_generation_token_in_the_plan_is_refused(self) -> None:
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/搬家.md")
        from scripts.core.structure_commands import _operation_paths, _plan_hash
        plan_path, _, _ = _operation_paths(self.root, plan["operationId"])
        stored = read_json(plan_path)
        stored["bindingTransitions"][0]["bindingGeneration"] = 999
        stored["planHash"] = _plan_hash({k: v for k, v in stored.items() if k != "planHash"})
        write_json(plan_path, stored)
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=plan["operationId"], plan_hash=stored["planHash"],
                approved=True, actor="test")),
            "OWNERSHIP_GENERATION_CONFLICT")

    def test_e6_destination_reserved_by_an_unresolved_chapter_is_refused(self) -> None:
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path=f"正文/{fx.CHAPTER_C}.md")),
            "PROSE_LOCATOR_CONFLICT")

    def test_e7_pure_locator_move_creates_no_creative_source_impact(self) -> None:
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/搬家.md")
        self.assertEqual(plan["impact"]["staleCandidates"], [])
        self.assertEqual(plan["impact"]["approvalInvalidations"], [])

    def test_e7b_preview_reports_the_whole_ownership_token(self) -> None:
        # 契约 §62：预览必须给出章、旧/新定位符、代次、期望摘要、观测摘要与观测状态。
        chapter = self.chapter(fx.CHAPTER_A)
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/搬家.md")
        ownership = plan["impact"]["ownership"]
        self.assertEqual(ownership["chapterId"], fx.CHAPTER_A)
        self.assertEqual(ownership["state"], "OWNED_CLEAN")
        self.assertEqual(ownership["currentLocator"], f"正文/{fx.CHAPTER_A}.md")
        self.assertEqual(ownership["bindingGeneration"], chapter["proseBinding"]["generation"])
        self.assertEqual(ownership["expectedContentDigest"], fx.managed_digest())
        self.assertEqual(ownership["observedContentDigest"], fx.managed_digest())
        self.assertEqual(ownership["blockers"], [])
        self.assertEqual(plan["bindingTransitions"][0]["newFilePath"], "正文/搬家.md")
        self.assertEqual(plan["pathMoves"][0]["sourcePath"], f"正文/{fx.CHAPTER_A}.md")

    def test_e8_unsafe_destination_locator_is_refused(self) -> None:
        # 逃逸目录、绝对路径这类定位符自身就不安全：必须在归属/占用判定之前被拒。
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path="../逃逸.md")),
            "PROSE_LOCATOR_UNSAFE")
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path="/etc/passwd")),
            "PROSE_LOCATOR_UNSAFE")


# ---------------------------------------------------------------------------
# Test Group F — unbound reservation move（契约 §45–§48）
# ---------------------------------------------------------------------------

class UnboundMoveTests(SyntheticProjectCase):
    def test_f1_moves_the_empty_reservation_and_keeps_it_unbound(self) -> None:
        before = self.chapter(fx.CHAPTER_B)
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_B,
                                                       file_path="正文/空槽新址.md")
        refactor.commit_refactor(self.root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        after = self.chapter(fx.CHAPTER_B)
        self.assertEqual(after["filePath"], "正文/空槽新址.md")
        self.assertEqual(after["proseBinding"]["state"], "unbound")
        self.assertIsNone(after["proseBinding"]["expectedContentDigest"])
        self.assertGreater(after["proseBinding"]["generation"], before["proseBinding"]["generation"])
        self.assertEqual(after["proseBinding"]["generation"], load_structure(self.root)["revision"])

    def test_f2_occupied_old_slot_is_refused(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_B}.md").write_text("莫名出现", encoding="utf-8")
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_B, file_path="正文/空槽新址.md")),
            "PROSE_TARGET_NOT_OWNED")

    def test_f3_occupied_new_slot_is_refused(self) -> None:
        (self.root / "正文" / "占位.md").write_text("别人的", encoding="utf-8")
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_B, file_path="正文/占位.md")),
            "PROSE_TARGET_NOT_OWNED")

    def test_f4_unbound_move_never_promotes_to_managed(self) -> None:
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_B,
                                                       file_path="正文/空槽新址.md")
        refactor.commit_refactor(self.root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        self.assertEqual(self.chapter(fx.CHAPTER_B)["proseBinding"]["state"], "unbound")


# ---------------------------------------------------------------------------
# Test Group G — unresolved（契约 §17 / §49 / §20.8）
# ---------------------------------------------------------------------------

class UnresolvedTests(SyntheticProjectCase):
    def test_g1_ordinary_finalization_is_refused(self) -> None:
        preflight = preflight_finalization(self.root, fx.CHAPTER_C, skip_title=True)
        self.assertIn("OWNERSHIP_CONFIRMATION_REQUIRED", [b["code"] for b in preflight.blockers])

    def test_g2_ordinary_move_is_refused(self) -> None:
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_C, file_path="正文/悬案新址.md")),
            "OWNERSHIP_CONFIRMATION_REQUIRED")

    def test_g3_matching_disk_digest_does_not_help(self) -> None:
        (self.root / f"正文/{fx.CHAPTER_C}.md").write_text(fx.MANAGED_PROSE, encoding="utf-8")
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_C, file_path="正文/悬案新址.md")),
            "OWNERSHIP_CONFIRMATION_REQUIRED")

    def test_g4_missing_target_does_not_help(self) -> None:
        self.assertFalse((self.root / f"正文/{fx.CHAPTER_C}.md").exists())
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_C, file_path="正文/悬案新址.md")),
            "OWNERSHIP_CONFIRMATION_REQUIRED")


# ---------------------------------------------------------------------------
# Test Group H — current undo routing（契约 §56–§59 / §70）
# ---------------------------------------------------------------------------

class CurrentUndoRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = None







    def test_h5_undo_routes_to_the_monotonic_engine_on_current_binding(self) -> None:
        # Phase 3 起：当前格式 的 undo 不再按「模型不支持」fail closed，而是走单调
        # 逆向事务；未知 operation 仍被确定性拒绝，绝不落入 历史整包恢复。
        # 逆向事务的完整语义见 tests/test_phase3_monotonic_undo.py。
        from scripts.core.structure_commands import undo_operation
        root = Path(self.temp.name) / "binding-undo"
        fx.ownership_project(root)
        self.assertEqual(
            error_code(lambda: undo_operation(root, "OP-0000000000000000")),
            "OPERATION_NOT_FOUND")


# ---------------------------------------------------------------------------
# Test Group I — history independence（契约 §14 / §71 / §72）
# ---------------------------------------------------------------------------

class HistoryIndependenceTests(SyntheticProjectCase):
    def test_i1_damaged_history_does_not_change_current_ownership(self) -> None:
        for relative in (".govel/operations", ".govel/backups", ".govel/undo-receipts",
                         ".govel/undo-operations"):
            directory = self.root / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "damaged.json").write_text("{ not json", encoding="utf-8")
        workflow = read_json(self.root / ".govel" / "workflow.json")
        workflow["continuity"] = {fx.CHAPTER_A: {"damaged": True}}
        write_json(self.root / ".govel" / "workflow.json", workflow)
        self.assertEqual(self.observation(fx.CHAPTER_A)["state"], prose_binding.OWNED_CLEAN)

    def test_i2_damaged_history_does_not_block_a_locator_move(self) -> None:
        (self.root / ".govel" / "operations").mkdir(parents=True, exist_ok=True)
        (self.root / ".govel" / "operations" / "junk.json").write_text("{", encoding="utf-8")
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/搬家.md")
        refactor.commit_refactor(self.root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], "正文/搬家.md")


# ---------------------------------------------------------------------------
# Test Group J — inode replacement（契约 §52）
# ---------------------------------------------------------------------------

class InodeReplacementTests(SyntheticProjectCase):
    def test_j1_same_bytes_different_inode_is_owned_clean(self) -> None:
        path = self.root / f"正文/{fx.CHAPTER_A}.md"
        replacement = self.root / "正文" / "replacement.tmp"
        replacement.write_bytes(path.read_bytes())
        path.unlink()
        os.replace(replacement, path)
        observation = self.observation(fx.CHAPTER_A)
        self.assertEqual(observation["state"], prose_binding.OWNED_CLEAN)
        self.assertNotIn("inode", observation)


# ---------------------------------------------------------------------------
# Test Group K — case-only rename（契约 §13 / §54）
# ---------------------------------------------------------------------------

class CaseOnlyRenameTests(SyntheticProjectCase):
    def test_k1_case_only_rename_stays_deferred(self) -> None:
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_A, file_path=f"正文/{fx.CHAPTER_A}.MD")),
            "REFACTOR_CASE_ONLY_RENAME_DEFERRED")

    def test_k2_case_only_rename_on_an_unbound_slot_stays_deferred(self) -> None:
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_B, file_path=f"正文/{fx.CHAPTER_B}.MD")),
            "REFACTOR_CASE_ONLY_RENAME_DEFERRED")

    def test_k2b_decomposed_equivalent_locator_stays_deferred(self) -> None:
        # NFC 归一后等价的两种拼写（e + U+0301 vs U+00E9）也是 normalization-only rename。
        path = self.root / ".govel" / "structure.json"
        on_disk = read_json(path)
        chapter = on_disk["chapters"][1]
        chapter["filePath"] = "正文/\u00e9.md"
        chapter["proseBinding"] = prose_binding.seal_binding(
            fx.PROJECT_ID, chapter["chapterId"], chapter["filePath"], state="unbound",
            generation=on_disk["revision"], expected_content_digest=None)
        write_json(path, on_disk)
        self.assertEqual(
            error_code(lambda: refactor.preview_relocate_chapter_paths(
                self.root, chapter_id=fx.CHAPTER_B, file_path="正文/e\u0301.md")),
            "REFACTOR_CASE_ONLY_RENAME_DEFERRED")

    def test_k3_sync_safe_paths_goes_through_a_formal_binding_transition(self) -> None:
        """sync_safe_paths 在 当前格式 上不再 fail closed：filePath 以正式 binding transition 改。

        它仍受同一条 DEFERRED 裁决约束：派生的正文定位符与当前定位符只差大小写/归一化时，
        改名被显式阻断，而不是原地改目录项（契约 §13/§54/§60）。
        """
        # 正控：受管章 + 盘上正文，同步改名把 title 与正文/推演定位符一起落地。
        root = Path(self.temp.name) / "sync"
        review_ready_binding_project(root, chapter_id=fx.CHAPTER_B, state="managed", prose=NEW_PROSE)
        before = chapter_of(root, fx.CHAPTER_B)
        plan = refactor.preview_rename_chapter(root, chapter_id=fx.CHAPTER_B, new_title="空槽·新名",
                                               file_name_strategy="sync_safe_paths")
        self.assertEqual(plan["bindingTransitions"][0]["kind"], "move_managed")
        refactor.commit_refactor(root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        after = chapter_of(root, fx.CHAPTER_B)
        self.assertEqual(after["title"], "空槽·新名")
        # `safe_component` 只替换非法文件名字符，全角标点与 CJK 原样保留。
        self.assertEqual(after["filePath"], "正文/001-空槽·新名.md")
        self.assertTrue((root / "正文/001-空槽·新名.md").is_file())
        self.assertFalse((root / before["filePath"]).exists())
        self.assertEqual(after["proseBinding"]["expectedContentDigest"],
                         before["proseBinding"]["expectedContentDigest"])
        self.assertGreater(after["proseBinding"]["generation"], before["proseBinding"]["generation"])


# ---------------------------------------------------------------------------
# Test Group L — writer boundary（契约 §5.3 / §64）
# ---------------------------------------------------------------------------

class WriterBoundaryTests(SyntheticProjectCase):
    def _raw_save(self, mutate):
        from scripts.core.structure_store import save_structure
        structure = load_structure(self.root)
        mutate(structure)
        return error_code(lambda: save_structure(self.root, structure))

    def test_l1_generic_save_cannot_move_a_locator(self) -> None:
        def mutate(structure):
            structure["chapters"][0]["filePath"] = "正文/手改.md"
        self.assertEqual(self._raw_save(mutate), "OWNERSHIP_UNAUTHORIZED_MUTATION")

    def test_l2_generic_save_cannot_change_the_binding_state(self) -> None:
        def mutate(structure):
            structure["chapters"][0]["proseBinding"]["state"] = "unbound"
            structure["chapters"][0]["proseBinding"]["expectedContentDigest"] = None
        self.assertEqual(self._raw_save(mutate), "OWNERSHIP_UNAUTHORIZED_MUTATION")

    def test_l3_generic_save_cannot_reseal_the_integrity_digest(self) -> None:
        def mutate(structure):
            structure["chapters"][0]["proseBinding"]["integrityDigest"] = "sha256:" + "a" * 64
        self.assertEqual(self._raw_save(mutate), "OWNERSHIP_UNAUTHORIZED_MUTATION")

    def test_l4_generic_save_cannot_forge_a_generation_bump(self) -> None:
        def mutate(structure):
            structure["chapters"][0]["proseBinding"] = prose_binding.seal_binding(
                fx.PROJECT_ID, fx.CHAPTER_A, structure["chapters"][0]["filePath"],
                state="managed", generation=999,
                expected_content_digest=fx.managed_digest())
        self.assertEqual(self._raw_save(mutate), "OWNERSHIP_UNAUTHORIZED_MUTATION")

    def test_l5_formal_transition_authority_is_the_only_way_through(self) -> None:
        structure = load_structure(self.root)
        transition = {"kind": "move_managed", "chapterId": fx.CHAPTER_A,
                      "newFilePath": "正文/正式搬家.md"}
        result = prose_binding.commit_binding_transitions(
            self.root, [transition], actor="test",
            proposed=_with_revision(structure, "正文/正式搬家.md"))
        self.assertEqual(result["revision"], structure["revision"] + 1)
        self.assertEqual(self.chapter(fx.CHAPTER_A)["filePath"], "正文/正式搬家.md")


def _with_revision(structure: dict, new_path: str) -> dict:
    from copy import deepcopy
    proposed = deepcopy(structure)
    proposed["revision"] = structure["revision"] + 1
    return proposed


# ---------------------------------------------------------------------------
# Test Group M — ABA / stale token（契约 §7.3 / §90）
# ---------------------------------------------------------------------------

class StaleTokenTests(SyntheticProjectCase):
    def test_m1_token_from_before_a_move_is_stale_after_it(self) -> None:
        structure = load_structure(self.root)
        chapter = next(row for row in structure["chapters"] if row["chapterId"] == fx.CHAPTER_A)
        token = chapter["proseBinding"]["generation"]
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/搬家.md")
        refactor.commit_refactor(self.root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        after = chapter_of(self.root, fx.CHAPTER_A)
        self.assertNotEqual(token, after["proseBinding"]["generation"])
        self.assertFalse(prose_binding.request_generation_current(after, token))

    def test_m2_stale_token_cannot_authorize_a_finalization(self) -> None:
        # A 移动 g10 → B g11 之后，针对 A/g10 签发的定稿令牌必须因代次不符被拒。
        root = Path(self.temp.name) / "aba"
        review_ready_binding_project(root, chapter_id=fx.CHAPTER_A, state="managed", prose=NEW_PROSE)
        generation = chapter_of(root, fx.CHAPTER_A)["proseBinding"]["generation"]
        plan = refactor.preview_relocate_chapter_paths(root, chapter_id=fx.CHAPTER_A,
                                                       file_path="正文/搬家.md")
        refactor.commit_refactor(root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        self.assertNotEqual(chapter_of(root, fx.CHAPTER_A)["proseBinding"]["generation"], generation)
        self.assertEqual(
            workflow_error_code(lambda: finalize_chapter(
                root, fx.CHAPTER_A, skip_title=True, binding_generation=generation)),
            "OWNERSHIP_GENERATION_CONFLICT")


class Schema5ParityTests(TempRootCase):
    """同一个 current-binding 运行时在 single-source 布局（schema 5）上同样成立。"""

    def _project(self, *, state: str = "unbound", prose: str = NEW_PROSE) -> str:
        structure = review_ready_schema5_project(self.root, state=state, prose=prose)
        return structure["chapters"][0]["chapterId"]

    def test_s5_observation_and_move_work_on_schema5(self) -> None:
        chapter_id = self._project(state="managed", prose=NEW_PROSE)
        self.assertEqual(self.observation(chapter_id)["state"], prose_binding.OWNED_CLEAN)
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=chapter_id,
                                                       file_path="正文/搬家.md")
        refactor.commit_refactor(self.root, operation_id=plan["operationId"],
                                 plan_hash=plan["planHash"], approved=True, actor="test")
        self.assertEqual(self.chapter(chapter_id)["filePath"], "正文/搬家.md")
        self.assertTrue(is_current_binding(load_structure(self.root)))

    def test_s5_first_finalization_works_on_schema5(self) -> None:
        chapter_id = self._project()
        self.assertEqual(self.chapter(chapter_id)["proseBinding"]["state"], "unbound")
        finalize_chapter(self.root, chapter_id, skip_title=True,
                         binding_generation=self.chapter(chapter_id)["proseBinding"]["generation"])
        self.assertEqual(self.chapter(chapter_id)["proseBinding"]["state"], "managed")
        self.assertTrue((self.root / self.chapter(chapter_id)["filePath"]).is_file())

    def test_s5_accept_external_preview_works_and_commit_fails_closed_on_schema5(self) -> None:
        chapter_id = self._project(state="managed", prose=NEW_PROSE)
        body = self.root / self.chapter(chapter_id)["filePath"]
        body.write_text("外部改过\n", encoding="utf-8")
        external = "sha256:" + hashlib.sha256(body.read_bytes()).hexdigest()
        plan = preview_accept_external_modification(self.root, chapter_id=chapter_id, actor="author")
        staged = read_json(_operation_paths(self.root, plan["operationId"])[0])
        proposed = next(row for row in staged["proposedStructure"]["chapters"]
                        if row["chapterId"] == chapter_id)
        self.assertEqual(proposed["proseBinding"]["expectedContentDigest"], external)
        self.assertEqual(proposed["status"], "stale")
        # authority 门槛在 schema 5 上同样成立：同一 caller 不能自批准。
        self.assertEqual(
            error_code(lambda: refactor.commit_refactor(
                self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"],
                approved=True, actor="author")),
            "APPROVAL_CAPABILITY_REQUIRED")
        self.assertEqual(body.read_text(encoding="utf-8"), "外部改过\n")


class TransportBoundaryTests(unittest.TestCase):
    """高风险所有权流转的能力分离在传输层也成立（契约 §10.1）。"""

    def test_accept_external_is_classified_but_not_mcp_exposed(self) -> None:
        from scripts.core.operations import MCP_ALLOWLIST, WRITE_COMMANDS, operations
        self.assertEqual(WRITE_COMMANDS[("workflow", "accept-external")], "protected-write")
        # MCP 是低能力/无特权通道：只预览不够，这个 leaf 同时能提交，因此整体不暴露。
        self.assertNotIn(("workflow", "accept-external"), MCP_ALLOWLIST)
        classified = {spec.id for spec in operations()}
        self.assertTrue(classified)  # 分类器整体仍然可走通（新 leaf 不会让它抛错）


if __name__ == "__main__":
    unittest.main()
