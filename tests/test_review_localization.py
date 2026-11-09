"""Review localization and work-note tests（§42-§45 / §64）."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.review_localization import (
    localize_findings,
    load_issue_notes,
    note_issue,
    resolve_localization_body,
    review_report_view,
)
from scripts.core.structure_store import StructureError
from tests.fixtures_workflow import register_and_approve, review_ready_project
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS

BODY = "他把钥匙放回桌上。\n\n窗外的雨停了。\n\n他吹灭了灯，屋里只剩呼吸声。"
REVIEWED_BODY = "他把钥匙放回桌上，合上了登记本。"


class LocalizeFindingsTests(unittest.TestCase):
    def test_unique_quote_gets_exact_offsets(self) -> None:
        rows = localize_findings([{"severity": "S2", "ruleId": "R-1", "summary": "s", "location": "窗外的雨停了。"}], BODY)
        self.assertEqual(rows[0]["anchor"]["state"], "unique")
        self.assertEqual(rows[0]["anchor"]["offsets"], [[BODY.index("窗外的雨停了。"), BODY.index("窗外的雨停了。") + 7]])

    def test_repeated_quote_is_ambiguous_not_guessed(self) -> None:
        rows = localize_findings([{"severity": "S3", "ruleId": "R-2", "summary": "s", "location": "他"}], BODY)
        self.assertEqual(rows[0]["anchor"]["state"], "ambiguous")
        self.assertGreater(rows[0]["anchor"]["occurrences"], 1)

    def test_missing_quote_and_empty_quote_stay_missing(self) -> None:
        rows = localize_findings([
            {"severity": "S3", "ruleId": "R-3", "summary": "s", "location": "这句不在正文里"},
            {"severity": "S3", "ruleId": "R-4", "summary": "s"},
        ], BODY)
        self.assertEqual(rows[0]["anchor"]["state"], "missing")
        self.assertEqual(rows[1]["anchor"]["state"], "missing")


class ReviewReportViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        fixture = review_ready_project(self.root)
        self.chapter_id = fixture["structure"]["chapters"][0]["chapterId"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _register_stage_b(self, *, with_body_hash: bool = True) -> dict:
        payload = {
            "verdict": "REPAIR_REQUIRED", "repairScope": "local",
            "slots": {slot: {"conclusion": "通过"} for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
            "findings": [
                {"severity": "S2", "ruleId": "R-CAUSAL", "summary": "钥匙来源缺一句交代",
                 "location": "他把钥匙放回桌上", "repairSuggestion": "补一句来源"},
                {"severity": "S3", "ruleId": "R-RHYTHM", "summary": "节奏建议", "location": "上"},
            ],
        }
        if with_body_hash:
            from scripts.core.artifacts import sha256_text
            payload["finalProseSha256"] = sha256_text(REVIEWED_BODY)
        return register_and_approve(self.root, self.chapter_id, "stage_b_review", payload)

    def test_view_binds_report_and_localizes_against_given_body(self) -> None:
        artifact = self._register_stage_b()
        view = review_report_view(self.root, self.chapter_id, body=REVIEWED_BODY, body_kind="candidate_prose")
        self.assertEqual(view["report"]["artifactSha256"], artifact["artifactSha256"])
        self.assertTrue(view["versionBinding"]["known"])
        self.assertTrue(view["versionBinding"]["matchesBody"])
        self.assertTrue(view["versionBinding"]["current"])
        self.assertEqual(view["localizedAgainst"]["kind"], "candidate_prose")
        states = [row["anchor"]["state"] for row in view["localization"]]
        self.assertEqual(states, ["unique", "ambiguous"])

    def test_given_body_mismatch_never_gets_current_offsets(self) -> None:
        self._register_stage_b()
        view = review_report_view(self.root, self.chapter_id, body=BODY, body_kind="active_candidate")
        self.assertTrue(view["versionBinding"]["known"])
        self.assertFalse(view["versionBinding"]["matchesBody"])
        self.assertFalse(view["versionBinding"]["current"])
        self.assertFalse(view["canAnnotate"])
        self.assertTrue(all(not row["anchor"]["offsets"] for row in view["localization"]))

    def test_review_without_body_hash_is_not_registerable(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._register_stage_b(with_body_hash=False)
        self.assertEqual(caught.exception.code, 'REVIEW_FIELD_REQUIRED')

    def test_absent_report_is_honest_not_fabricated(self) -> None:
        # review_ready_project 只给第一章登记过工件；第二章应当如实报「无报告」。
        from scripts.core.jsonio import read_json
        structure = read_json(self.root / ".govel/structure.json")
        second_id = structure["chapters"][1]["chapterId"]
        view = review_report_view(self.root, second_id, body=None, body_kind="none")
        self.assertEqual(view["code"], "REVIEW_REPORT_ABSENT")
        self.assertIsNone(view["report"])

    def test_work_notes_are_working_state_not_approval(self) -> None:
        artifact = self._register_stage_b()
        result = note_issue(self.root, self.chapter_id, report_sha256=artifact["artifactSha256"],
                            finding_index=0, state="processed", note="已并入缓冲修改")
        self.assertEqual(result["state"], "processed")
        notes = load_issue_notes(self.root, self.chapter_id)["notes"]
        key = f"{artifact['artifactSha256']}:0"
        self.assertEqual(notes[key]["state"], "processed")
        with self.assertRaises(StructureError):
            note_issue(self.root, self.chapter_id, report_sha256=artifact["artifactSha256"],
                       finding_index=0, state="passed")
        # 换一份（不同 hash 的）报告后旧笔记键失配：报告版本隔离
        with self.assertRaises(StructureError) as ctx:
            note_issue(self.root, self.chapter_id, report_sha256="0" * 64,
                       finding_index=0, state="processed")
        self.assertEqual(ctx.exception.code, "REVIEW_REPORT_STALE")

    def test_resolve_localization_body_prefers_active_candidate(self) -> None:
        from scripts.core.candidate_versions import activate_candidate_version, save_candidate_version
        save_candidate_version(self.root, self.chapter_id, body="工作台激活版正文。",
                               request_id="R1", base_candidate_version=None)
        activate_candidate_version(self.root, self.chapter_id, candidate_version=1, expected_active_token=0)
        body, kind = resolve_localization_body(self.root, self.chapter_id)
        self.assertEqual(kind, "active_candidate")
        self.assertEqual(body, "工作台激活版正文。")


if __name__ == "__main__":
    unittest.main()
