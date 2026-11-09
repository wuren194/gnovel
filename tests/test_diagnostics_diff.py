"""Findings diff identity stability and the live chapter status board."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.diagnostics_diff import diff_findings, diff_reports
from scripts.core.artifacts import envelope, sha256_text
from scripts.core.jsonio import read_json, write_json
from scripts.core.review_artifacts import AUXILIARY_GATES, STAGE_B_SLOTS
from tests.fixtures_workflow import review_ready_project

ROOT = Path(__file__).resolve().parents[1]


def cli(*argv: str) -> dict:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), *argv],
                          capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"CLI produced no JSON: {proc.stderr}\nstdout: {proc.stdout[:400]}")
    payload["__returncode"] = proc.returncode
    return payload


def cliche(rule: str, matched: str, line: int, severity: str = "warning") -> dict:
    return {"source": "ai_cliche_linter", "severity": severity, "ruleId": rule,
            "message": f"[{rule}] {matched}", "line": line,
            "raw": {"pattern": rule, "matched": matched, "line": line}}


def voice(character: str, phrase: str, line: int) -> dict:
    return {"source": "check_voice_lexicon", "severity": "error", "ruleId": "banned",
            "message": f"{character} used '{phrase}'", "line": line,
            "raw": {"characterId": character, "phrase": phrase, "line": line}}


def validate(rule: str, evidence: str, line: int) -> dict:
    return {"source": "validate_text", "severity": "warning", "ruleId": rule,
            "message": rule, "line": line, "evidence": evidence, "raw": {"rule_id": rule}}


class FindingsDiffTests(unittest.TestCase):
    def test_moved_finding_is_persisting_not_fixed_plus_new(self):
        """同一问题行号漂移：按锚点配对为 persisting，不误报一修一增。"""
        before = [cliche("S-01", "压抑", 12)]
        after = [cliche("S-01", "压抑", 47)]
        result = diff_findings(before, after)
        self.assertEqual(result["summary"]["persistingGroups"], 1)
        self.assertEqual(result["summary"]["fixedGroups"], 0)
        self.assertEqual(result["summary"]["newGroups"], 0)
        group = result["groups"][0]
        self.assertEqual(group["before"]["lines"], [12])
        self.assertEqual(group["after"]["lines"], [47])

    def test_partial_repair_is_grouped_not_fabricated(self):
        """同词三处修了一处：mixed 组（persisting 2 + fixed 1），不假装逐处配对。"""
        before = [cliche("S-01", "压抑", 12), cliche("S-01", "压抑", 30), cliche("S-01", "压抑", 88)]
        after = [cliche("S-01", "压抑", 33), cliche("S-01", "压抑", 90)]
        result = diff_findings(before, after)
        self.assertEqual(result["summary"]["mixedGroups"], 1)
        group = result["groups"][0]
        self.assertEqual(group["persistingCount"], 2)
        self.assertEqual(group["fixedCount"], 1)

    def test_fixed_and_new_and_severity_change(self):
        before = [cliche("S-01", "压抑", 12), cliche("S-02", "不禁", 40, "warning")]
        after = [cliche("S-02", "不禁", 41, "error"), cliche("S-03", "一丝", 9)]
        result = diff_findings(before, after)
        by_anchor = {tuple(g["anchor"]["identity"]): g for g in result["groups"]}
        self.assertEqual(by_anchor[("S-01", "S-01", "压抑")]["status"], "fixed")
        self.assertEqual(by_anchor[("S-02", "S-02", "不禁")]["status"], "severity_changed")
        self.assertEqual(by_anchor[("S-03", "S-03", "一丝")]["status"], "new")

    def test_evidence_rewrite_is_honest_pair(self):
        """validate_text 的 evidence 整句被改写：旧 fixed、新 new，附 absence 说明。"""
        before = [validate("conj-chain", "他转过身，然后走了，然后停了", 5)]
        after = [validate("conj-chain", "他转身就走，随即停住", 6)]
        result = diff_findings(before, after)
        self.assertEqual(result["summary"]["fixedGroups"], 1)
        self.assertEqual(result["summary"]["newGroups"], 1)
        self.assertIn("fixed+new pairs", result["coverage"]["absenceMeaning"])

    def test_unusable_fields_go_to_unmatched(self):
        result = diff_findings([{"source": "validate_text", "severity": "warning"}],
                               [{"severity": "warning"}])
        self.assertEqual(result["summary"]["unmatchedBefore"], 1)
        self.assertEqual(result["summary"]["unmatchedAfter"], 1)


class DiagnosticsDiffCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=1)
        self.chapter = self.fixture["chapterIds"][0]

    def _register_findings_diagnostics(self, findings: list[dict]) -> Path:
        """fixture 的 diagnostics 没有 findings；用生产入口重注册一份带 findings 的。"""
        structure = read_json(self.root / ".gnovel/structure.json")
        workflow = read_json(self.root / ".gnovel/workflow.json")
        prose = read_json(Path(workflow["artifacts"][self.chapter]["candidate_prose"]["artifactPath"]))
        artifact = envelope("diagnostics", {
            "passed": True, "findings": findings,
        }, project_id=structure["projectId"], chapter_id=self.chapter)
        path = self.root / "工作台" / f"{self.chapter}-diag-v2.json"
        write_json(path, artifact)
        result = cli("project", "workflow", "artifact-register", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--artifact", str(path),
                     "--bind", "draftSha256")
        self.assertTrue(result["ok"], result)
        approve = cli("project", "workflow", "artifact-approve", "--project-root", str(self.root),
                      "--chapter-id", self.chapter, "--artifact", "@" + str(path))
        self.assertTrue(approve["ok"], approve)
        self.assertEqual(read_json(path)["payload"]["draftSha256"], sha256_text(prose["payload"]["text"]))
        return path

    def test_default_baseline_is_registered_artifact(self):
        """against 缺省：用该章已注册 diagnostics 作为上一轮基线。"""
        registered = self._register_findings_diagnostics([
            {"source": "ai_cliche_linter", "severity": "warning", "ruleId": "S-OLD",
             "message": "old", "line": 3, "raw": {"pattern": "S-OLD", "matched": "旧词", "line": 3}}])
        baseline = read_json(registered)
        current_path = self.root / "工作台" / "round2.json"
        current = dict(baseline)
        current["payload"] = dict(baseline["payload"])
        current["payload"]["findings"] = baseline["payload"]["findings"] + [
            {"source": "ai_cliche_linter", "severity": "warning", "ruleId": "S-NEW",
             "message": "new", "line": 7, "raw": {"pattern": "S-NEW", "matched": "新词", "line": 7}}]
        write_json(current_path, current)
        result = cli("project", "workflow", "diagnostics-diff", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--current", str(current_path))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["newGroups"], 1)
        self.assertEqual(result["summary"]["persistingGroups"], 1)

    def test_missing_baseline_requires_against(self):
        spare = self.root / "工作台" / "spare.json"
        write_json(spare, {"findings": []})
        result = cli("project", "workflow", "diagnostics-diff", "--project-root", str(self.root),
                     "--chapter-id", "CH-999", "--current", str(spare))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "DIAGNOSTICS_BASELINE_MISSING")

    def test_explicit_against_and_draft_unchanged_note(self):
        report = self._register_findings_diagnostics([])
        result = cli("project", "workflow", "diagnostics-diff", "--project-root", str(self.root),
                     "--chapter-id", self.chapter, "--current", str(report),
                     "--against", str(report))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["summary"].get("draftUnchanged"))

    def test_missing_baseline_requires_against(self):
        spare = self.root / "工作台" / "spare.json"
        write_json(spare, {"findings": []})
        result = cli("project", "workflow", "diagnostics-diff", "--project-root", str(self.root),
                     "--chapter-id", "CH-999", "--current", str(spare))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "DIAGNOSTICS_BASELINE_MISSING")


class ChapterStatusBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = review_ready_project(self.root, chapter_count=1)
        self.chapter = self.fixture["chapterIds"][0]

    def test_review_chapter_shows_full_board_and_finalize_branch(self):
        result = cli("project", "workflow", "chapter-status", "--project-root", str(self.root),
                     "--chapter-id", self.chapter)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], "review")
        kinds = {row["artifactType"]: row for row in result["artifacts"]}
        for kind in ("stage_a_review", "candidate_prose", "diagnostics", "stage_b_review", "state_candidate"):
            self.assertTrue(kinds[kind]["registered"], kind)
            self.assertTrue(kinds[kind]["approved"], kind)
            if kind == 'diagnostics':
                self.assertTrue(kinds[kind]['fileFresh'])
                self.assertTrue(kinds[kind]['fresh'])
                self.assertNotIn('legacy', result['diagnosticsEvidence'])
            else:
                self.assertTrue(kinds[kind]["fresh"], kind)
        self.assertFalse(kinds["title_candidates"]["registered"])
        # 缺标题：下一步应指向标题登记；阻塞分支与作者批准分支共存时每条都带依据
        self.assertTrue(any("title" in row["command"] or "标题" in row["command"]
                            for row in result["nextActions"]))
        self.assertTrue(all(row.get("basis") for row in result["nextActions"]))
        self.assertTrue(any(row["needsApproval"] for row in result["nextActions"]))
        self.assertTrue(result["coverage"]["statusRecomputed"])

    def test_next_action_derived_from_state_not_template(self):
        """改变实际状态，下一步随之改变：diagnostics 未批准时指向批准而非 advance。"""
        workflow_path = self.root / ".gnovel/workflow.json"
        workflow = read_json(workflow_path)
        workflow["approvals"][self.chapter]["approved"] = False
        write_json(workflow_path, workflow)
        # 章回退到 draft 场景不可行（状态机），改用 review 章的 stage_b verdict 驱动
        entry = workflow["artifacts"][self.chapter]["stage_b_review"]
        data = read_json(Path(entry["artifactPath"]))
        data["payload"]["verdict"] = "REPAIR_REQUIRED"
        data["payload"]["repairScope"] = "local"
        from scripts.core.artifacts import sha256_json
        data["artifactSha256"] = sha256_json({k: v for k, v in data.items() if k != "artifactSha256"})
        write_json(Path(entry["artifactPath"]), data)
        result = cli("project", "workflow", "chapter-status", "--project-root", str(self.root),
                     "--chapter-id", self.chapter)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("repairScope=local" in row["command"] for row in result["nextActions"]),
                        result["nextActions"])

    def test_board_is_read_only(self):
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        cli("project", "workflow", "chapter-status", "--project-root", str(self.root),
            "--chapter-id", self.chapter)
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_unknown_chapter_rejected(self):
        result = cli("project", "workflow", "chapter-status", "--project-root", str(self.root),
                     "--chapter-id", "CH-404")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CHAPTER_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
