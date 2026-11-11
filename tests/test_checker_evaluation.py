from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import evaluate_checkers as evaluation

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "evals" / "checker-eval-baseline.json"


class CheckerEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = evaluation.load_suite(evaluation.DEFAULT_SUITE)
        cls.report = evaluation.evaluate(cls.suite)

    def test_real_checkers_match_all_labeled_controls(self) -> None:
        self.assertTrue(self.report["passed"], evaluation.failure_keys(self.report))
        self.assertEqual(self.report["summary"]["FP"], 0)
        self.assertEqual(self.report["summary"]["FN"], 0)
        self.assertEqual(self.report["summary"]["failedRepairs"], 0)
        self.assertEqual({rule.split("/")[0] for rule in self.report["perRule"]}, evaluation.CHECKERS)

    def test_each_rule_has_positive_and_negative_support(self) -> None:
        for rule, counts in self.report["perRule"].items():
            with self.subTest(rule=rule):
                self.assertGreater(counts["TP"] + counts["FN"], 0)
                self.assertGreater(counts["TN"] + counts["FP"], 0)
                self.assertEqual(counts["precision"], 1.0)
                self.assertEqual(counts["recall"], 1.0)

    def test_checked_in_baseline_is_comparable_and_has_no_regressions(self) -> None:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        comparison = evaluation.compare_baseline(self.report, baseline)
        self.assertFalse(comparison["regressed"], comparison)

    def test_reruns_are_identical(self) -> None:
        self.assertEqual(evaluation.evaluate(self.suite), self.report)

    def test_false_positive_and_negative_are_counted(self) -> None:
        suite = copy.deepcopy(self.suite)
        suite["cases"][0]["text"] = "他把钥匙放在桌上。"
        suite["cases"][1]["text"] = "他不是来取信，而是来还钥匙。"
        report = evaluation.evaluate(suite)
        counts = report["perRule"]["cliche/NOT-A-BUT-B"]
        self.assertEqual(counts["FP"], 1)
        self.assertEqual(counts["FN"], 1)
        self.assertEqual(counts["precision"], counts["TP"] / (counts["TP"] + 1))
        self.assertEqual(counts["recall"], counts["TP"] / (counts["TP"] + 1))
        self.assertFalse(report["passed"])
        self.assertIn("cliche/NOT-A-BUT-B", report["cases"][0]["falseNegatives"])
        self.assertIn("cliche/NOT-A-BUT-B", report["cases"][1]["falsePositives"])

    def test_missing_detections_are_not_a_perfect_precision_score(self) -> None:
        with patch.object(evaluation, "run_checkers", return_value=set()):
            report = evaluation.evaluate(self.suite)
        self.assertGreater(report["summary"]["FN"], 0)
        self.assertIsNone(report["summary"]["precision"])
        self.assertEqual(report["summary"]["recall"], 0)
        comparison = evaluation.compare_baseline(report, self.report)
        self.assertTrue(comparison["regressed"])
        self.assertTrue(any(":FN+" in item for item in comparison["regressions"]))

    def test_case_level_regression_cannot_hide_in_equal_totals(self) -> None:
        baseline = copy.deepcopy(self.report)
        current = copy.deepcopy(self.report)
        baseline["cases"][0]["falseNegatives"] = ["cliche/NOT-A-BUT-B"]
        current["cases"][1]["falsePositives"] = ["cliche/NOT-A-BUT-B"]
        comparison = evaluation.compare_baseline(current, baseline)
        self.assertTrue(comparison["regressed"])
        self.assertIn("case:contrast-dialogue-negative:falsePositives:cliche/NOT-A-BUT-B", comparison["regressions"])

    def test_suite_or_protocol_change_rejects_comparison(self) -> None:
        for key, value in (("suiteDigest", "different"), ("protocolVersion", 99), ("kind", "human-approved")):
            baseline = copy.deepcopy(self.report)
            baseline[key] = value
            with self.subTest(key=key), self.assertRaises(evaluation.EvaluationError):
                evaluation.compare_baseline(self.report, baseline)

    def test_fixture_edit_changes_digest(self) -> None:
        suite = copy.deepcopy(self.suite)
        suite["cases"][0]["text"] += "\n"
        self.assertNotEqual(evaluation.evaluate(suite)["suiteDigest"], self.report["suiteDigest"])

    def test_unscored_findings_do_not_become_false_positives(self) -> None:
        observed = evaluation.run_checkers("那个秘密一直在等。", {"cliche"}, [])
        self.assertIn("cliche/INANIMATE-AGENCY", observed)
        self.assertEqual(evaluation.score_assertions({"cliche/ABSTRACT-EMOTION": False}, observed),
                         {"falsePositives": [], "falseNegatives": []})

    def test_voice_adapter_consumes_actual_global_registry(self) -> None:
        with patch("scripts.check_voice_lexicon.load_global_banned", return_value=["周三"]):
            observed = evaluation.run_checkers("林舟说：“周三见。”", {"voice"}, self.suite["characters"])
        self.assertIn("voice/VOICE-GLOBAL-INTERJECTION", observed)

    def test_successful_and_deliberately_bad_repairs_are_distinguished(self) -> None:
        repairs = {item["id"]: item for item in self.report["repairs"]}
        self.assertEqual(repairs["successful-repair"]["observed"]["remaining"], [])
        introduced = repairs["introduced-truncation-then-fixed"]["observed"]
        self.assertEqual(introduced["introduced"], [["degeneration/末尾截断"], []])
        self.assertEqual(introduced["remaining"], [])
        self.assertEqual(repairs["introduced-placeholder-unfixed"]["observed"]["remaining"], ["degeneration/占位符"])

    def test_exact_text_cycle_and_rule_return_have_separate_evidence(self) -> None:
        repairs = {item["id"]: item["observed"] for item in self.report["repairs"]}
        exact = repairs["exact-text-cycle"]
        changed = repairs["rule-return-with-different-text"]
        self.assertEqual(exact["textCycles"], [{"stage": 2, "repeatsStage": 0}])
        self.assertEqual(changed["textCycles"], [])
        self.assertEqual(exact["reintroduced"], changed["reintroduced"])
        self.assertEqual(repairs["unchanged-is-not-a-cycle"]["reintroduced"], [[]])
        self.assertEqual(repairs["unchanged-is-not-a-cycle"]["textCycles"], [])

    def test_repeated_noop_stages_are_not_text_cycles(self) -> None:
        stage = next(item for item in self.report["cases"] if item["id"] == "repair-original")
        observed = evaluation.repair_observations([stage, stage, stage, stage], ["cliche/NOT-A-BUT-B"])
        self.assertEqual(observed["textCycles"], [])
        self.assertEqual(observed["reintroduced"], [[], [], []])

    def test_main_returns_one_for_same_suite_baseline_regression(self) -> None:
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(evaluation, "run_checkers", return_value=set()), contextlib.redirect_stdout(output):
            code = evaluation.main(["--baseline", str(BASELINE), "--json"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output.getvalue())["baselineComparison"]["regressed"])

    def test_incorrect_repair_expectation_fails_evaluation(self) -> None:
        suite = copy.deepcopy(self.suite)
        suite["repairs"][1]["expected"]["introduced"] = [[], []]
        report = evaluation.evaluate(suite)
        self.assertFalse(report["passed"])
        self.assertEqual(report["repairs"][1]["failedFields"], ["introduced"])

    def test_invalid_suites_fail_closed(self) -> None:
        for kind in ("duplicate", "unknown-checker", "non-boolean", "no-negative", "missing-stage", "unlabeled-repair-rule"):
            suite = copy.deepcopy(self.suite)
            if kind == "duplicate":
                suite["cases"].append(suite["cases"][0])
            elif kind == "unknown-checker":
                suite["cases"][0]["assertions"] = {"imaginary/rule": True}
            elif kind == "non-boolean":
                suite["cases"][0]["assertions"]["cliche/NOT-A-BUT-B"] = 1
            elif kind == "no-negative":
                for case in suite["cases"]:
                    if "cliche/NOT-A-BUT-B" in case["assertions"]:
                        case["assertions"]["cliche/NOT-A-BUT-B"] = True
            elif kind == "missing-stage":
                suite["repairs"][0]["stages"][0] = "missing"
            else:
                suite["repairs"][0]["rules"].append("cliche/HONEST-OPEN")
            with self.subTest(kind=kind), self.assertRaises(evaluation.EvaluationError):
                evaluation.validate_suite(suite)


class EvaluationCLITests(unittest.TestCase):
    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/evaluate_checkers.py"), *args],
                              cwd=cwd or ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                              capture_output=True, text=True, timeout=30)

    def test_json_baseline_and_output_work_from_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            proc = self.run_cli("--baseline", str(BASELINE), "--json", "--output", str(output), cwd=Path(directory))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
            self.assertFalse(report["baselineComparison"]["regressed"])

    def test_cli_returns_one_for_real_checker_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            suite = evaluation.load_suite(evaluation.DEFAULT_SUITE)
            suite["cases"][0]["text"] = "他把钥匙放在桌上。"
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
            proc = self.run_cli("--suite", str(path), "--json")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["summary"]["FN"], 1)

    def test_incomparable_baseline_and_invalid_json_return_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{}", encoding="utf-8")
            proc = self.run_cli("--baseline", str(path), "--json")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("Incomparable", json.loads(proc.stdout)["error"])
            path.write_text("{", encoding="utf-8")
            proc = self.run_cli("--suite", str(path), "--json")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error", json.loads(proc.stdout))

    def test_output_cannot_overwrite_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            original = evaluation.DEFAULT_SUITE.read_text(encoding="utf-8")
            path.write_text(original, encoding="utf-8")
            proc = self.run_cli("--suite", str(path), "--output", str(path), "--json")
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
