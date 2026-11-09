from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts import state_cache
from scripts.core.artifacts import envelope, file_sha256, sha256_json, sha256_text
from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.locks import project_lock
from scripts.core.workflow import WorkflowError, approve_artifacts, default_workflow, record_artifact
from tests.fixtures_current import create_schema5_project
from tests.fixtures_workflow import review_ready_project
from scripts.core.artifact_pack import pack_artifact


def ready_project(root: Path) -> tuple[str, dict]:
    """Real current-format pipeline, with stable file names for tamper probes."""
    fixture = review_ready_project(root, chapter_count=1)
    cid = fixture['chapterIds'][0]
    packed = pack_artifact(root, chapterId=cid, artifactType='title_candidates',
                          payload={'candidates': [{'candidateId': 'title-1', 'title': '入库', 'reason': '登记结果'}]})
    workflow = read_json(root/'.govel/workflow.json')
    approve_artifacts(workflow, cid, artifact_hashes={'title_candidates': packed['artifactSha256']}, approval_mode='manual')
    artifacts = {}
    for kind, entry in workflow['artifacts'][cid].items():
        artifact = read_json(Path(entry['artifactPath']))
        path = root/'工作台'/f'{kind}.json'
        write_json(path, artifact)
        entry.update(artifactPath=str(path), artifactFileSha256=file_sha256(path))
        artifacts[kind] = artifact
    write_json(root/'.govel/workflow.json', workflow)
    return cid, artifacts


def snapshot(root: Path) -> dict:
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            if path.is_file() and not path.is_symlink() else ("symlink", str(path.readlink()))
            if path.is_symlink() else ("directory", path.stat().st_mtime_ns)
            for path in root.rglob("*")}


class FinalizationPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chapter_id, self.artifacts = ready_project(self.root)
        self.generation = chapter_receipt(self.root, self.chapter_id)['bindingGeneration']

    def workflow_change(self, change) -> None:
        path = self.root / ".govel/workflow.json"
        workflow = read_json(path)
        change(workflow)
        write_json(path, workflow)

    def replace_payload(self, kind: str, **changes) -> None:
        old = self.artifacts[kind]
        payload = {**old["payload"], **changes}
        artifact = envelope(kind, payload, project_id=old["projectId"], chapter_id=self.chapter_id)
        self.artifacts[kind] = artifact
        path = self.root / "工作台" / f"{kind}.json"
        write_json(path, artifact)
        def update(workflow):
            workflow["artifacts"][self.chapter_id][kind].update(artifactSha256=artifact["artifactSha256"], artifactFileSha256=file_sha256(path))
            workflow["approvals"][self.chapter_id]["artifactHashes"][kind] = artifact["artifactSha256"]
        self.workflow_change(update)

    def assert_blocked(self, *codes: str, **options) -> dict:
        if not options:
            options = {"title_candidate_id": "title-1"}
        before = snapshot(self.root)
        receipt = chapter_receipt(self.root, self.chapter_id, **options)
        preflight = preflight_finalization(self.root, self.chapter_id, binding_generation=self.generation, **options)
        self.assertEqual(receipt["blockers"], preflight.blockers)
        self.assertFalse(receipt["readyToFinalize"])
        self.assertTrue(set(codes).issubset({item["code"] for item in receipt["blockers"]}), receipt["blockers"])
        with self.assertRaises(WorkflowError) as caught:
            finalize_chapter(self.root, self.chapter_id, binding_generation=self.generation, **options)
        self.assertEqual(caught.exception.code, receipt["blockers"][0]["code"])
        self.assertEqual(snapshot(self.root), before)
        return receipt

    def test_ready_receipt_matches_real_commit(self) -> None:
        before = snapshot(self.root)
        with patch.object(state_cache, "load", side_effect=AssertionError("must not load cache")), patch.object(state_cache, "rebuild", side_effect=AssertionError("must not rebuild cache")):
            receipt = chapter_receipt(self.root, self.chapter_id, title_candidate_id="title-1")
        self.assertTrue(receipt["readyToFinalize"], receipt["blockers"])
        self.assertEqual(receipt["blockers"], [])
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(finalize_chapter(self.root, self.chapter_id, binding_generation=self.generation, title_candidate_id="title-1")["code"], "CHAPTER_FINALIZED")

    def test_outside_approved_range_regression(self) -> None:
        self.workflow_change(lambda workflow: workflow.update(approvedChapterIds=[]))
        receipt = self.assert_blocked("CHAPTER_OUTSIDE_APPROVED_RANGE")
        self.assertFalse(receipt["approvedRange"])
        project = chapter_receipt(self.root, title_candidate_id="title-1")
        self.assertEqual(project["chapters"][0]["blockers"], receipt["blockers"])

    def test_collects_independent_blockers(self) -> None:
        self.workflow_change(lambda workflow: workflow.update(approvedChapterIds=[]))
        self.replace_payload("stage_a_review", decision="reject", sourcePlanHash="wrong")
        self.replace_payload("stage_b_review", verdict="INCOMPLETE", finalProseSha256="wrong")
        self.replace_payload("diagnostics", passed=False, draftSha256="wrong")
        self.replace_payload("state_candidate", baseStateSha256="wrong", designSha256="wrong", finalProseSha256="wrong", sourcePlanHash="wrong")
        self.assert_blocked("CHAPTER_OUTSIDE_APPROVED_RANGE", "STAGE_A_NOT_ADOPTED", "STAGE_A_OUTLINE_MISMATCH",
                            "STAGE_B_NOT_READY", "STAGE_B_PROSE_MISMATCH", "DIAGNOSTICS_NOT_PASSED",
                            "DIAGNOSTICS_PROSE_MISMATCH", "STATE_BASE_MISMATCH", "DESIGN_BASE_MISMATCH",
                            "PROSE_HASH_MISMATCH", "OUTLINE_BASE_MISMATCH")

    def test_status_is_a_blocker(self) -> None:
        path = self.root / ".govel/structure.json"
        structure = read_json(path)
        structure["chapters"][0]["status"] = "draft"
        write_json(path, structure)
        self.assert_blocked("INVALID_STATE_TRANSITION")

    def test_unregistered_and_unapproved(self) -> None:
        def change(workflow):
            workflow["artifacts"][self.chapter_id].pop("stage_a_review")
            workflow["approvals"][self.chapter_id]["approved"] = False
        self.workflow_change(change)
        self.assert_blocked("FINAL_ARTIFACTS_MISSING", "ARTIFACTS_NOT_APPROVED")

    def test_approval_hash_is_stale(self) -> None:
        self.workflow_change(lambda workflow: workflow["approvals"][self.chapter_id]["artifactHashes"].update(candidate_prose="wrong"))
        self.assert_blocked("ARTIFACT_APPROVAL_HASH_MISMATCH")

    def test_changed_and_missing_sources_are_collected(self) -> None:
        (self.root / "工作台/diagnostics.json").write_text("broken", encoding="utf-8")
        (self.root / "工作台/stage_b_review.json").unlink()
        receipt = self.assert_blocked("ARTIFACT_SOURCE_CHANGED", "ARTIFACT_INVALID")
        kinds = {item["details"].get("artifactType") for item in receipt["blockers"]}
        self.assertTrue({"diagnostics", "stage_b_review"}.issubset(kinds))

    def test_project_identity_is_bound(self) -> None:
        artifact = self.artifacts["candidate_prose"]
        artifact = envelope("candidate_prose", artifact["payload"], project_id="other-project", chapter_id=self.chapter_id)
        write_json(self.root / "工作台/candidate_prose.json", artifact)
        self.assert_blocked("ARTIFACT_PROJECT_MISMATCH")

    def test_empty_prose_is_blocked(self) -> None:
        self.replace_payload("candidate_prose", text="  \n")
        self.assert_blocked("PROSE_PAYLOAD_INVALID")

    def test_missing_source_plan(self) -> None:
        path = self.root/'.govel/structure.json'
        structure = read_json(path)
        structure['fragments'] = [f for f in structure['fragments'] if f['ownerId'] != self.chapter_id]
        write_json(path, structure)
        self.assert_blocked('SOURCE_CHAPTER_PLAN_MISSING')

    def test_stale_source_plan(self) -> None:
        path = self.root/'.govel/structure.json'
        structure = read_json(path)
        next(f for f in structure['fragments'] if f['ownerId'] == self.chapter_id)['text'] = '不同的章节计划。'
        write_json(path, structure)
        self.assert_blocked('OUTLINE_BASE_MISMATCH', 'STAGE_A_OUTLINE_MISMATCH')

    def test_missing_cache_is_not_rebuilt(self) -> None:
        paths = [self.root/f'.govel/cache/story-{name}.json' for name in ('state', 'design')]
        for path in paths:
            if 'missing' == 'missing':
                path.unlink(missing_ok=True)
            else:
                path.write_text('bad cache')
        before = snapshot(self.root)
        with patch.object(state_cache, 'load', side_effect=AssertionError('no cache load')), patch.object(state_cache, 'rebuild', side_effect=AssertionError('no cache rebuild')):
            receipt = chapter_receipt(self.root, self.chapter_id, title_candidate_id='title-1')
        self.assertTrue(receipt['readyToFinalize'], receipt['blockers'])
        self.assertEqual(snapshot(self.root), before)

    def test_modified_cache(self) -> None:
        paths = [self.root/f'.govel/cache/story-{name}.json' for name in ('state', 'design')]
        for path in paths:
            if 'modified' == 'missing':
                path.unlink(missing_ok=True)
            else:
                path.write_text('bad cache')
        before = snapshot(self.root)
        with patch.object(state_cache, 'load', side_effect=AssertionError('no cache load')), patch.object(state_cache, 'rebuild', side_effect=AssertionError('no cache rebuild')):
            receipt = chapter_receipt(self.root, self.chapter_id, title_candidate_id='title-1')
        self.assertTrue(receipt['readyToFinalize'], receipt['blockers'])
        self.assertEqual(snapshot(self.root), before)

    def test_structure_change_cannot_hide_behind_stale_cache(self) -> None:
        path = self.root / ".govel/structure.json"
        structure = read_json(path)
        structure['bookName'] = '新书名'
        write_json(path, structure)
        self.assert_blocked("STATE_BASE_MISMATCH", "DESIGN_BASE_MISMATCH")

    def test_approved_titles_are_not_an_implicit_choice(self) -> None:
        self.assert_blocked("TITLE_DECISION_REQUIRED", skip_title=False)

    def test_historical_title_decision_is_not_reused(self) -> None:
        self.workflow_change(lambda workflow: workflow["approvals"][self.chapter_id].update(titleDecision={"finalized": False, "reason": "author skipped naming"}))
        self.assert_blocked("TITLE_DECISION_REQUIRED", skip_title=False)

    def test_unknown_title_choice(self) -> None:
        self.assert_blocked("TITLE_CANDIDATE_UNKNOWN", title_candidate_id="unknown")

    def test_skip_title_needs_no_title_artifact(self) -> None:
        self.workflow_change(lambda workflow: workflow["artifacts"][self.chapter_id].pop("title_candidates"))
        receipt = chapter_receipt(self.root, self.chapter_id, skip_title=True)
        self.assertTrue(receipt["readyToFinalize"], receipt["blockers"])
        self.assertFalse(finalize_chapter(self.root, self.chapter_id, binding_generation=self.generation, skip_title=True)["titleFinalized"])

    def test_other_chapter_pending_transaction_blocks_project(self) -> None:
        path = self.root / ".govel/transactions/TX-OTHER.json"
        write_json(path, {"transactionId": "TX-OTHER", "chapterId": "CH-OTHER", "phase": "prose"})
        receipt = self.assert_blocked("TRANSACTION_INCOMPLETE")
        self.assertEqual(receipt["transactions"][0]["chapterId"], "CH-OTHER")

    def test_unreadable_transaction_blocks_project(self) -> None:
        path = self.root / ".govel/transactions/TX-BROKEN.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bad json", encoding="utf-8")
        self.assert_blocked("TRANSACTION_INCOMPLETE")

    def test_live_lock_is_read_only_blocker(self) -> None:
        from scripts.core.locks import project_lock
        with project_lock(self.root):
            self.assert_blocked("PROJECT_LOCKED")

    def test_state_change_target_is_checked_before_writing(self) -> None:
        self.replace_payload("state_candidate", changes=[{"op": "transition", "collection": "decisions", "value": {"decisionId": "missing"}}])
        self.assert_blocked("SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION")

    def test_proposed_structure_is_validated_before_transaction(self) -> None:
        self.replace_payload("state_candidate", changes=[{"op": "append", "collection": "relations", "value": {"relationId": "REL-ABCDEF12", "fromEntity": "absent", "toEntity": "absent"}}])
        self.assert_blocked("SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION")

    def test_transaction_targets_are_checked(self) -> None:
        source = read_json(self.root/'.govel/structure.json')
        target = self.root/source['chapters'][0]['filePath']
        target.mkdir()
        self.assert_blocked('TRANSACTION_TARGET_INVALID')

    def test_revalidates_after_acquiring_commit_lock(self) -> None:
        @contextmanager
        def mutate_before_lock(root):
            self.workflow_change(lambda workflow: workflow.update(approvedChapterIds=[]))
            with project_lock(root):
                yield
        with patch("scripts.core.finalization.project_lock", mutate_before_lock):
            with self.assertRaises(WorkflowError) as caught:
                finalize_chapter(self.root, self.chapter_id, binding_generation=self.generation, title_candidate_id="title-1")
        self.assertEqual(caught.exception.code, "CHAPTER_OUTSIDE_APPROVED_RANGE")
        self.assertEqual(read_json(self.root / ".govel/structure.json")["chapters"][0]["status"], "review")
        self.assertFalse(list((self.root / ".govel/transactions").glob("*.json")))

    def test_invalid_workflow_is_a_receipt_blocker(self) -> None:
        (self.root / ".govel/workflow.json").write_text("bad json", encoding="utf-8")
        self.assert_blocked("WORKFLOW_STATE_INVALID")

    def test_invalid_cache_is_not_repaired(self) -> None:
        paths = [self.root/f'.govel/cache/story-{name}.json' for name in ('state', 'design')]
        for path in paths:
            if 'invalid' == 'missing':
                path.unlink(missing_ok=True)
            else:
                path.write_text('bad cache')
        before = snapshot(self.root)
        with patch.object(state_cache, 'load', side_effect=AssertionError('no cache load')), patch.object(state_cache, 'rebuild', side_effect=AssertionError('no cache rebuild')):
            receipt = chapter_receipt(self.root, self.chapter_id, title_candidate_id='title-1')
        self.assertTrue(receipt['readyToFinalize'], receipt['blockers'])
        self.assertEqual(snapshot(self.root), before)

    def test_registered_envelope_hash_is_bound(self) -> None:
        self.workflow_change(lambda workflow: workflow["artifacts"][self.chapter_id]["diagnostics"].update(artifactSha256="wrong"))
        self.assert_blocked("ARTIFACT_APPROVAL_HASH_MISMATCH")

    def test_artifact_envelope_tampering_is_blocked(self) -> None:
        path = self.root / "工作台/diagnostics.json"
        artifact = read_json(path)
        artifact["payload"]["passed"] = False
        write_json(path, artifact)
        self.assert_blocked("ARTIFACT_SOURCE_CHANGED", "ARTIFACT_HASH_MISMATCH")

    def test_missing_artifact_source_has_compatible_code(self) -> None:
        (self.root / "工作台/stage_b_review.json").unlink()
        self.assert_blocked("ARTIFACT_SOURCE_MISSING")

    def test_matching_stage_b_prose_hash(self) -> None:
        self.replace_payload("stage_b_review", finalProseSha256=sha256_text(self.artifacts["candidate_prose"]["payload"]["text"]))
        self.assertTrue(chapter_receipt(self.root, self.chapter_id, title_candidate_id="title-1")["readyToFinalize"])
        self.assertEqual(finalize_chapter(self.root, self.chapter_id, binding_generation=self.generation, title_candidate_id="title-1")["code"], "CHAPTER_FINALIZED")


if __name__ == "__main__":
    unittest.main()
