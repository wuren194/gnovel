from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import state_cache
from scripts.core.artifacts import envelope, file_sha256
from scripts.core.source_plan import chapter_plan
from tests.fixtures_workflow import register_and_approve
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.jsonio import read_json, write_json
from scripts.core.workflow import WorkflowError, approve_artifacts, default_workflow, record_artifact
from tests.fixtures_current import create_schema5_project


class ChapterStateTests(unittest.TestCase):
    def _planning_project(self, root: Path) -> tuple[dict, dict]:
        fixture = create_schema5_project(root, chapter_count=1)
        structure = read_json(root / ".gnovel/structure.json")
        chapter = structure["chapters"][0]
        chapter["status"] = "planning_required"
        write_json(root / ".gnovel/structure.json", structure)
        state_cache.rebuild(root)
        return structure, chapter

    def test_stage_a_gate_advances_planning_required_to_planned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure, chapter = self._planning_project(root)
            artifact = envelope(
                "stage_a_review",
                {
                    "decision": "adopt",
                    "activeSeats": list(range(1, 11)),
                    "behaviorCoverage": {"relationship": "not_applicable", "living": "not_applicable"},
                    "seatReviews": {str(seat): {"conclusion": "合成钥匙核对计划没有承重关系选择或持续生活安排。",
                        "evidence": [{"text": structure['fragments'][0]['text']}] } for seat in range(1, 11)},
                    "sourcePlanHash": chapter_plan(structure, chapter["chapterId"])["sourcePlanHash"],
                    "consensus": [], "disagreements": [], "openItems": [],
                },
                project_id=structure["projectId"], chapter_id=chapter["chapterId"],
            )
            path = root / "工作台/stage-a.json"
            write_json(path, artifact)
            workflow = default_workflow()
            workflow["approvedChapterIds"] = [chapter["chapterId"]]
            record_artifact(workflow, chapter["chapterId"], artifact, structure=structure, artifact_path=str(path), project_id=structure["projectId"], project_root=root)
            approve_artifacts(workflow, chapter["chapterId"], artifact_hashes={"stage_a_review": artifact["artifactSha256"]}, approval_mode="manual", project_root=root)
            write_json(root / ".gnovel/workflow.json", workflow)

            result = advance_chapter_status(root, chapter["chapterId"], "planned")
            self.assertEqual(result["status"], "planned")
            self.assertEqual(read_json(root / ".gnovel/structure.json")["chapters"][0]["status"], "planned")
            self.assertEqual(read_json(root / ".gnovel/workflow.json")["chapterStates"][chapter["chapterId"]]["status"], "planned")

    def test_stage_a_missing_or_stale_outline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure, chapter = self._planning_project(root)
            write_json(root / ".gnovel/workflow.json", default_workflow())
            with self.assertRaises(WorkflowError) as caught:
                advance_chapter_status(root, chapter["chapterId"], "planned")
            self.assertEqual(caught.exception.code, "ARTIFACT_NOT_REGISTERED")

    def test_candidate_and_diagnostics_gate_draft_and_review(self) -> None:
        from scripts.core.artifact_pack import pack_artifact
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); structure, chapter = self._planning_project(root); cid = chapter['chapterId']
            workflow = default_workflow(); workflow['approvedChapterIds'] = [cid]
            write_json(root/'.gnovel/workflow.json', workflow)
            register_and_approve(root, cid, 'stage_a_review', {'decision': 'adopt', 'activeSeats': list(range(1, 11)),
                'sourcePlanHash': chapter_plan(structure, cid)['sourcePlanHash'], 'consensus': [], 'disagreements': [], 'openItems': []})
            advance_chapter_status(root, cid, 'planned')
            pack_artifact(root, chapterId=cid, artifactType='candidate_prose', text='候选正文')
            self.assertEqual(advance_chapter_status(root, cid, 'draft')['status'], 'draft')
            with self.assertRaises(WorkflowError) as caught: advance_chapter_status(root, cid, 'review')
            self.assertEqual(caught.exception.code, 'DIAGNOSTICS_NOT_REGISTERED')


if __name__ == "__main__":
    unittest.main()
