"""Registry-backed style candidates shared by prose diagnostics."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import json
import math
from pathlib import Path
import re
from typing import Any, Iterator

from .prose_spans import mask_prose

try:
    from scripts.text_utils import HAN_PATTERN, chinese_char_count
except ImportError:
    from text_utils import HAN_PATTERN, chinese_char_count

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "shared" / "registries" / "text-patterns.json"


def pattern_rules(registry: dict) -> list[dict]:
    triple = registry["ai"]["triple"]
    return [*registry["ai"]["regex"], {**triple, "excludePattern": triple["action_exempt_pattern"]}]


def finding_policy(rule: dict) -> dict:
    """A semantic candidate cannot acquire a mechanical veto from its count."""
    review = rule.get("review", "semantic")
    severity = rule.get("severity", "advisory")
    if review == "semantic" and severity in {"blocking", "error"}:
        severity = "advisory"
    return {"review": review, "severity": severity,
            "gate": review == "mechanical" and severity in {"blocking", "error"} and bool(rule.get("gate", False))}


def load_text_patterns() -> dict[str, Any]:
    try:
        from scripts.core.data_provider import registry
    except ImportError:
        from core.data_provider import registry
    try:
        data = registry("text-patterns", REGISTRY_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法加载机械文本规则 {REGISTRY_PATH}: {exc}") from exc
    if data.get("schemaVersion") != 5:
        raise ValueError(f"不支持的文本规则版本: {data.get('schemaVersion')}")
    if not isinstance(data.get("ai", {}).get("regex"), list):
        raise ValueError("文本规则缺少 ai.regex")
    if not isinstance(data["ai"].get("triple"), dict):
        raise ValueError("文本规则缺少 ai.triple")
    rules = pattern_rules(data)
    clusters = data["ai"].get("clusters", [])
    if not isinstance(clusters, list) or any(not isinstance(rule, dict) for rule in [*rules, *clusters]):
        raise ValueError("文本规则和聚集规则必须为对象列表")
    seen: set[str] = set()
    for rule in [*rules, *clusters]:
        rule_id = rule.get("ruleId")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen:
            raise ValueError(f"文本规则 ruleId 缺失或重复: {rule_id!r}")
        seen.add(rule_id)
        if rule.get("scope", "narrative") not in {"narrative", "all"}:
            raise ValueError(f"{rule_id}: scope 必须为 narrative 或 all")
        if rule.get("review", "semantic") not in {"mechanical", "semantic"}:
            raise ValueError(f"{rule_id}: review 无效")
        if rule.get("severity", "advisory") not in {"blocking", "error", "advisory", "info"}:
            raise ValueError(f"{rule_id}: severity 无效")
        if "gate" in rule and type(rule["gate"]) is not bool:
            raise ValueError(f"{rule_id}: gate 必须为布尔值")
        modes = rule.get("exemptModes", [])
        if (not isinstance(modes, list) or any(not isinstance(mode, str)
                or mode.lower() not in {"plot", "buildup", "mixed", "peakscene"} for mode in modes)):
            raise ValueError(f"{rule_id}: exemptModes 无效")
        for field in ("perParagraphLimit", "minMatches", "endingWindowHan", "windowHan"):
            if field in rule and (type(rule[field]) is not int or rule[field] < (0 if field == "perParagraphLimit" else 1)):
                raise ValueError(f"{rule_id}: {field} 无效")
        density = rule.get("minPerKHan", 0)
        if type(density) not in (int, float) or not math.isfinite(density) or density < 0:
            raise ValueError(f"{rule_id}: minPerKHan 无效")
        for field in ("pattern", "excludePattern"):
            if field not in rule:
                continue
            try:
                re.compile(rule[field])
            except (TypeError, re.error) as exc:
                raise ValueError(f"{rule_id}: {field} 无效: {exc}") from exc
    by_id = {rule["ruleId"]: rule for rule in rules}
    if any(not isinstance(rule.get("pattern"), str) for rule in rules):
        raise ValueError("词面规则必须声明 pattern")
    for cluster in clusters:
        members = cluster.get("members")
        if (not isinstance(members, list) or not members or any(not isinstance(member, str) or member not in by_id for member in members)
                or len(set(members)) != len(members)
                or any(by_id[member].get("scope", "narrative") != cluster.get("scope", "narrative") for member in members)
                or not {"minMatches", "minPerKHan", "windowHan"} <= cluster.keys()):
            raise ValueError(f"{cluster['ruleId']}: 聚集规则的成员、范围或阈值无效")
    policy = data.get("diagnosticPolicy", {})
    for name, fields in (("sentenceRhythm", ("minSentences", "minSentenceChars", "minMeanChars", "maxVariance")),
                         ("degeneration", ("verbatimMinChars", "verbatimMinCount", "shortRepeatMinChars",
                                           "shortRepeatMinCount", "adjacentMinChars"))):
        values = policy.get(name, {})
        if any(type(values.get(field)) is not int or values[field] < 1 for field in fields):
            raise ValueError(f"diagnosticPolicy.{name} 必须声明正整数阈值")
    repeat = policy["degeneration"]
    similarity = repeat.get("adjacentSimilarity")
    if (repeat["shortRepeatMinChars"] >= repeat["verbatimMinChars"]
            or type(similarity) not in (int, float) or not 0 < similarity <= 1):
        raise ValueError("diagnosticPolicy.degeneration 的长度或相似度范围无效")
    if policy["sentenceRhythm"].get("severity") != "advisory":
        raise ValueError("diagnosticPolicy.sentenceRhythm 只报告 advisory")
    return data


def paragraph_spans(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for match in re.finditer(r"\n\s*\n", text):
        yield start, match.start()
        start = match.end()
    yield start, len(text)


def _finding(text: str, rule: dict, start: int, end: int) -> dict:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    return {"line": text.count("\n", 0, start) + 1, "column": start - line_start + 1,
            "start": start, "end": end, "ruleId": rule["ruleId"], "name": rule["name"],
            **finding_policy(rule), "matched": text[start:end],
            "context": text[line_start:line_end if line_end != -1 else len(text)].rstrip("\r"),
            "rewriteHint": rule.get("rewriteHint", ""), "scope": rule.get("scope", "narrative")}


def _cluster_findings(text: str, source: str, rule: dict, raw: list[dict]) -> list[dict]:
    pool = sorted((hit for hit in raw if hit["ruleId"] in rule["members"]),
                  key=lambda hit: (hit["start"], -hit["end"]))
    # Count a source span once even if two member patterns cover it.
    unique = []
    for hit in pool:
        if not unique or hit["start"] >= unique[-1]["end"]:
            unique.append(hit)
    hans = [match.start() for match in re.finditer(HAN_PATTERN, source)]
    positions = [hit["start"] for hit in unique]
    results, cursor = [], 0
    while cursor < len(unique):
        first = unique[cursor]
        han_start = bisect_left(hans, first["start"])
        han_end = min(len(hans), han_start + rule["windowHan"])
        window_end = hans[han_end] if han_end < len(hans) else len(source)
        stop = bisect_left(positions, window_end)
        selected = [hit for hit in unique[cursor:stop] if hit["end"] <= window_end]
        han_count = han_end - han_start
        density = len(selected) * 1000 / max(han_count, 1)
        if len(selected) >= rule["minMatches"] and density >= rule["minPerKHan"]:
            row = _finding(text, rule, first["start"], selected[-1]["end"])
            row["occurrences"] = [{k: hit[k] for k in ("ruleId", "line", "column", "start", "end", "matched")}
                                  for hit in selected]
            row["density"] = {"count": len(selected), "hanCount": han_count, "perKHan": round(density, 2),
                              **{k: rule[k] for k in ("minMatches", "minPerKHan", "windowHan")}}
            results.append(row)
            cursor = stop
        else:
            cursor += 1
    return results


def scan_ai_patterns(text: str, *, mode: str = "mixed") -> list[dict[str, Any]]:
    """Return source evidence after scope, mode, density and ending filters."""
    registry = load_text_patterns()
    sources = {"narrative": mask_prose(text), "all": mask_prose(text, include_dialogue=True)}
    results, raw = [], []
    for rule in pattern_rules(registry):
        if mode.lower() in {str(item).lower() for item in rule.get("exemptModes", [])}:
            continue
        source = sources[rule.get("scope", "narrative")]
        start = 0
        if "endingWindowHan" in rule:
            hans = [match.start() for match in re.finditer(HAN_PATTERN, source)]
            if len(hans) > rule["endingWindowHan"]:
                start = hans[-rule["endingWindowHan"]]
        matches = [match for match in re.finditer(rule["pattern"], source) if match.start() >= start
                   and match.end() > match.start()
                   and not (rule.get("excludePattern") and re.search(rule["excludePattern"], match[0]))]
        hits = [_finding(text, rule, match.start(), match.end()) for match in matches]
        raw.extend(hits)
        density = len(hits) * 1000 / max(chinese_char_count(source[start:]), 1)
        if len(hits) < rule.get("minMatches", 1) or density < rule.get("minPerKHan", 0):
            continue
        limit = rule.get("perParagraphLimit", 0)
        if limit:
            starts = [span[0] for span in paragraph_spans(source)]
            counts: dict[int, int] = {}
            kept = []
            for hit in hits:
                paragraph = bisect_right(starts, hit["start"]) - 1
                counts[paragraph] = counts.get(paragraph, 0) + 1
                if counts[paragraph] > limit:
                    kept.append(hit)
            hits = kept
        results.extend(hits)
    for rule in registry["ai"].get("clusters", []):
        if mode.lower() in {str(item).lower() for item in rule.get("exemptModes", [])}:
            continue
        clusters = _cluster_findings(text, sources[rule.get("scope", "narrative")], rule, raw)
        # One cluster carries its own individual occurrences; avoid duplicate nudges.
        results = [hit for hit in results if not any(hit["ruleId"] in rule["members"]
                   and cluster["start"] <= hit["start"] and hit["end"] <= cluster["end"] for cluster in clusters)]
        results.extend(clusters)
    return sorted(results, key=lambda item: (item["start"], item["ruleId"], item["end"]))
