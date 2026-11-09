#!/usr/bin/env python3
"""Reproducible engineering fixtures for the toolkit's real static checkers."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SUITE = ROOT / "evals" / "checker-eval.json"
PROTOCOL_VERSION = 1
CHECKERS = {"cliche", "degeneration", "voice", "validator"}


class EvaluationError(ValueError):
    pass


def digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_suite(path: Path) -> dict:
    suite = json.loads(path.read_text(encoding="utf-8"))
    validate_suite(suite)
    return suite


def validate_suite(suite: dict) -> None:
    if not isinstance(suite, dict) or suite.get("schemaVersion") != 1:
        raise EvaluationError("Expected suite schemaVersion 1")
    if suite.get("kind") != "synthetic-engineering":
        raise EvaluationError("This runner only scores synthetic-engineering fixtures")
    characters = suite.get("characters", [])
    if not isinstance(characters, list):
        raise EvaluationError("Characters must be a list of native lexical profiles")
    character_ids = set()
    for character in characters:
        if not isinstance(character, dict) or not all(isinstance(character.get(key), str) and character[key] for key in ("characterId", "name")):
            raise EvaluationError("Character profiles need nonempty characterId and name")
        phrases = character.get("bannedPhrases")
        if not isinstance(phrases, list) or not all(isinstance(phrase, str) and phrase for phrase in phrases):
            raise EvaluationError("bannedPhrases must be a list of nonempty strings")
        if character["characterId"] in character_ids:
            raise EvaluationError("Duplicate characterId")
        character_ids.add(character["characterId"])
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("Suite needs nonempty cases")
    by_id = {}
    coverage: dict[str, set[bool]] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise EvaluationError("Every case needs a nonempty id")
        if case["id"] in by_id:
            raise EvaluationError(f"Duplicate case: {case['id']}")
        by_id[case["id"]] = case
        if not isinstance(case.get("text"), str) or not case["text"].strip():
            raise EvaluationError(f"Case {case['id']} needs nonempty text")
        assertions = case.get("assertions")
        if not isinstance(assertions, dict) or not assertions:
            raise EvaluationError(f"Case {case['id']} needs rule assertions")
        for rule, expected in assertions.items():
            if "/" not in rule or rule.split("/", 1)[0] not in CHECKERS or not rule.split("/", 1)[1]:
                raise EvaluationError(f"Unknown checker namespace: {rule}")
            if type(expected) is not bool:
                raise EvaluationError(f"Assertion must be boolean: {rule}")
            coverage.setdefault(rule, set()).add(expected)
    for rule, labels in coverage.items():
        if labels != {False, True}:
            raise EvaluationError(f"Rule needs positive and negative controls: {rule}")
    repair_ids = set()
    repairs = suite.get("repairs", [])
    if not isinstance(repairs, list):
        raise EvaluationError("Repairs must be a list")
    for repair in repairs:
        if not isinstance(repair, dict) or not isinstance(repair.get("id"), str) or not repair["id"] or repair["id"] in repair_ids:
            raise EvaluationError("Repair ids must be nonempty and unique")
        repair_ids.add(repair["id"])
        stages, rules = repair.get("stages", []), repair.get("rules", [])
        if not isinstance(stages, list) or not all(isinstance(stage, str) for stage in stages):
            raise EvaluationError("Repair stages must be a list of case ids")
        if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
            raise EvaluationError("Repair rules must be a list of rule ids")
        if len(stages) < 2 or not rules or len(set(rules)) != len(rules):
            raise EvaluationError(f"Repair needs stages and unique monitored rules: {repair['id']}")
        for stage in stages:
            if stage not in by_id or not set(rules) <= by_id[stage]["assertions"].keys():
                raise EvaluationError(f"Repair stage must label every monitored rule: {stage}")
        expected = repair.get("expected", {})
        if not isinstance(expected, dict) or set(expected) != {"introduced", "reintroduced", "textCycles", "remaining"}:
            raise EvaluationError(f"Repair needs explicit transition expectations: {repair['id']}")
        for field in ("introduced", "reintroduced"):
            rows = expected[field]
            if not isinstance(rows, list) or len(rows) != len(stages) - 1:
                raise EvaluationError(f"Repair {field} needs one list per transition")
            if any(not isinstance(row, list) or not set(row) <= set(rules) for row in rows):
                raise EvaluationError(f"Repair {field} references an unmonitored rule")
        if not isinstance(expected["remaining"], list) or not set(expected["remaining"]) <= set(rules):
            raise EvaluationError("Repair remaining references an unmonitored rule")
        if not isinstance(expected["textCycles"], list):
            raise EvaluationError("Repair textCycles must be a list")


def run_checkers(text: str, names: set[str], characters: list[dict]) -> set[str]:
    """Call production APIs; normalize emitted IDs, not prose or rule thresholds."""
    observed = set()
    if "cliche" in names:
        from scripts.ai_cliche_linter import build_report
        observed.update("cliche/" + item["ruleId"] for item in build_report(text)["findings"])
    if "degeneration" in names:
        from scripts.check_degeneration import scan
        report = scan(text)
        for item in report["blocking"] + report["advisory"]:
            # The upstream report has display names, not IDs, and embeds a score here.
            pattern = item["pattern"].split("(相似度=", 1)[0]
            observed.add("degeneration/" + pattern)
    if "voice" in names:
        from scripts.check_voice_lexicon import build_report, load_global_banned
        report = build_report(text, characters, load_global_banned())
        observed.update("voice/" + item["ruleId"] for item in report["findings"])
    if "validator" in names:
        from scripts.text.validator import RULE_ID_BY_NAME, build_checks
        _, checks, _ = build_checks("plot", strict_l5=False, strict_dialogue_floor=False)
        for name, check in checks:
            result = check(text)
            if type(result[0]) is not bool:
                raise EvaluationError(f"Invalid validator result: {name}")
            if not result[0]:
                observed.add("validator/" + RULE_ID_BY_NAME[name])
    return observed


def score_assertions(assertions: dict[str, bool], observed: set[str]) -> dict:
    return {
        "falsePositives": sorted(rule for rule, expected in assertions.items() if not expected and rule in observed),
        "falseNegatives": sorted(rule for rule, expected in assertions.items() if expected and rule not in observed),
    }


def repair_observations(stages: list[dict], rules: list[str]) -> dict:
    monitored = set(rules)
    hits = [set(stage["observed"]) & monitored for stage in stages]
    seen = set(hits[0])
    introduced, reintroduced, text_cycles = [], [], []
    for index in range(1, len(stages)):
        added = hits[index] - hits[index - 1]
        introduced.append(sorted(added))
        reintroduced.append(sorted(added & seen))
        seen.update(hits[index])
        earlier = None
        if stages[index]["textDigest"] != stages[index - 1]["textDigest"]:
            earlier = next((j for j in range(index - 1) if stages[j]["textDigest"] == stages[index]["textDigest"]), None)
        if earlier is not None:
            text_cycles.append({"stage": index, "repeatsStage": earlier})
    return {"introduced": introduced, "reintroduced": reintroduced,
            "textCycles": text_cycles, "remaining": sorted(hits[-1])}


def evaluate(suite: dict) -> dict:
    validate_suite(suite)
    per_rule: dict[str, dict] = {}
    cases = []
    for case in suite["cases"]:
        assertions = case["assertions"]
        names = {rule.split("/", 1)[0] for rule in assertions}
        observed = run_checkers(case["text"], names, suite.get("characters", []))
        failures = score_assertions(assertions, observed)
        cases.append({"id": case["id"], "textDigest": digest(case["text"]),
                      "observed": sorted(observed & assertions.keys()),
                      "unscored": sorted(observed - assertions.keys()),
                      "passed": not any(failures.values()), **failures})
        for rule, expected in assertions.items():
            counts = per_rule.setdefault(rule, {"TP": 0, "FP": 0, "TN": 0, "FN": 0})
            counts[("T" if expected == (rule in observed) else "F") + ("P" if rule in observed else "N")] += 1
    totals = {key: sum(counts[key] for counts in per_rule.values()) for key in ("TP", "FP", "TN", "FN")}
    for counts in [*per_rule.values(), totals]:
        counts["precision"] = counts["TP"] / (counts["TP"] + counts["FP"]) if counts["TP"] + counts["FP"] else None
        counts["recall"] = counts["TP"] / (counts["TP"] + counts["FN"]) if counts["TP"] + counts["FN"] else None
    by_id = {case["id"]: case for case in cases}
    repairs = []
    for repair in suite.get("repairs", []):
        actual = repair_observations([by_id[key] for key in repair["stages"]], repair["rules"])
        failures = [field for field in actual if actual[field] != repair["expected"][field]]
        repairs.append({"id": repair["id"], "observed": actual, "expected": repair["expected"],
                        "failedFields": failures, "passed": not failures})
    return {
        "schemaVersion": 1, "protocolVersion": PROTOCOL_VERSION,
        "kind": "synthetic-engineering", "suiteDigest": digest({"protocolVersion": PROTOCOL_VERSION, "suite": suite}),
        "limitations": ["Not a novel-quality score or human-approved calibration", "Only explicitly labeled case-rule pairs are scored",
                        "Repair stages are hand-authored, not model generations", "Cycles mean exact text return or monitored rule disappearance and return"],
        "summary": {"cases": len(cases), "rules": len(per_rule), "assertions": sum(totals[k] for k in ("TP", "FP", "TN", "FN")),
                    "failedCases": sum(not case["passed"] for case in cases), "repairs": len(repairs),
                    "failedRepairs": sum(not repair["passed"] for repair in repairs), **totals},
        "perRule": dict(sorted(per_rule.items())), "cases": cases, "repairs": repairs,
        "passed": all(case["passed"] for case in cases) and all(repair["passed"] for repair in repairs),
    }


def failure_keys(report: dict) -> set[str]:
    failures = set()
    for case in report["cases"]:
        for kind in ("falsePositives", "falseNegatives"):
            failures.update(f"case:{case['id']}:{kind}:{rule}" for rule in case[kind])
    for repair in report["repairs"]:
        failures.update(f"repair:{repair['id']}:{field}" for field in repair["failedFields"])
    return failures


def compare_baseline(report: dict, baseline: dict) -> dict:
    if not isinstance(baseline, dict):
        raise EvaluationError("Baseline must be a JSON report object")
    for key in ("schemaVersion", "protocolVersion", "kind", "suiteDigest"):
        if baseline.get(key) != report[key]:
            raise EvaluationError(f"Incomparable baseline: {key} differs; run the same suite/protocol")
    if set(baseline.get("perRule", {})) != set(report["perRule"]):
        raise EvaluationError("Baseline rule coverage differs")
    for field in ("cases", "repairs"):
        if [item["id"] for item in baseline[field]] != [item["id"] for item in report[field]]:
            raise EvaluationError(f"Baseline {field} coverage differs")
    regressions = sorted(failure_keys(report) - failure_keys(baseline))
    deltas = {}
    for rule, current in report["perRule"].items():
        previous = baseline["perRule"][rule]
        deltas[rule] = {key: current[key] - previous[key] for key in ("TP", "FP", "TN", "FN")}
        for key in ("FP", "FN"):
            if deltas[rule][key] > 0:
                regressions.append(f"rule:{rule}:{key}+{deltas[rule][key]}")
    return {"comparable": True, "regressed": bool(regressions), "regressions": regressions,
            "perRuleDelta": deltas}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--baseline", type=Path, help="Compare a previous JSON report from the identical suite")
    parser.add_argument("--output", type=Path, help="Write JSON report (baseline updates are explicit)")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the compact summary")
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_suite(args.suite))
        if args.baseline:
            report["baselineComparison"] = compare_baseline(report, json.loads(args.baseline.read_text(encoding="utf-8")))
        code = 0 if report["passed"] and not report.get("baselineComparison", {}).get("regressed", False) else 1
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            if args.output.resolve() in {args.suite.resolve(), args.baseline.resolve() if args.baseline else args.suite.resolve()}:
                raise EvaluationError("Output must not overwrite the suite or comparison baseline")
            args.output.write_text(serialized, encoding="utf-8")
        if args.json:
            print(serialized, end="")
        else:
            summary = report["summary"]
            print(f"Synthetic checker evaluation: {summary['cases']} cases, {summary['rules']} rules, "
                  f"FP={summary['FP']} FN={summary['FN']}, failed cases={summary['failedCases']}, "
                  f"failed repairs={summary['failedRepairs']}; {'PASS' if code == 0 else 'FAIL'}")
            for failure in sorted(failure_keys(report)):
                print(f"  {failure}")
            for regression in report.get("baselineComparison", {}).get("regressions", []):
                print(f"  regression: {regression}")
        return code
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "passed": False}, ensure_ascii=False))
        else:
            print(f"Evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
