"""Deterministic findings diff between two aggregated diagnostics reports.

返修闭环要求每轮重跑完整 aggregator；增量报告把「AI 重读全部 findings、自己对比
新旧」换成「工具按稳定身份配对、只呈现差异」。身份锚点按检查器分别选取（行号与
周边文本不进身份——返修后它们会漂移，按整段文本比较会把同一问题误报成
一修一增）：

- ai_cliche_linter: (ruleId, pattern, matched)
- validate_text:    (ruleId, 归一化 evidence)
- check_voice_lexicon: (ruleId, characterId, phrase)
- check_degeneration:  (pattern, matched)  [ruleId 字段复用了 pattern]

同锚点多处时按组报告 fixed/persisting 数量，不假装能逐处配对——复修第 1 处、
保留第 2 处同一词时，工具无法知道旧报告的哪一行对应新报告的哪一行。无法提取
锚点的条目显式标 unmatched，不强行归类。本模块只算差异，不判断「问题真的修好了」。
"""

from __future__ import annotations

import re
from typing import Any

NORMALIZE_WS = re.compile(r"\s+")


def _normalize(text: Any) -> str:
    return NORMALIZE_WS.sub(" ", str(text)).strip() if isinstance(text, str) else ""


# 各检查器的身份锚点：缺失字段的条目进 unmatched。
ANCHOR_FIELDS: dict[str, tuple[str, ...]] = {
    "ai_cliche_linter": ("ruleId", "raw.pattern", "raw.matched"),
    "validate_text": ("ruleId", "evidence"),
    "check_voice_lexicon": ("ruleId", "raw.characterId", "raw.phrase"),
    "check_degeneration": ("raw.pattern", "raw.matched"),
}


def _lookup(finding: dict[str, Any], path: str) -> Any:
    value: Any = finding
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def finding_anchor(finding: dict[str, Any]) -> tuple[str, ...] | None:
    """Deterministic identity: (source_tag, *identity_fields); None when unusable."""
    source = _lookup(finding, "source")
    fields = ANCHOR_FIELDS.get(source)
    if fields is None:
        # 未知来源：退回 (source, ruleId) 粒度；ruleId 也缺失时不可配对
        rule = _normalize(_lookup(finding, "ruleId"))
        return ("__unknown__", source or "", rule) if rule else None
    parts = [_normalize(_lookup(finding, path)) for path in fields]
    if any(not part for part in parts):
        return None
    return (str(source), *parts)


def _sight(finding: dict[str, Any]) -> dict[str, Any]:
    """Presentation view: what a human needs to locate the finding (not identity)."""
    line = _lookup(finding, "line")
    return {
        "line": line if isinstance(line, int) else None,
        "severity": _normalize(_lookup(finding, "severity")) or None,
        "message": _normalize(_lookup(finding, "message")) or None,
    }


def diff_findings(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """Group both reports by anchor and classify fixed / persisting / new / mixed.

    组级语义：count 下降为 mixed（persisting N + fixed M），归零为 fixed，新增为
    new，持平为 persisting。severity 变化单独标记，不与增减混淆。unmatched 单列，
    两侧都呈现，交给人判断。
    """
    def index(findings: list[dict[str, Any]]) -> tuple[dict[tuple[str, ...], list[dict]], list[dict]]:
        grouped: dict[tuple[str, ...], list[dict]] = {}
        unmatched: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict):
                unmatched.append({"reason": "not_an_object", "finding": finding})
                continue
            anchor = finding_anchor(finding)
            if anchor is None:
                unmatched.append({"reason": "anchor_unusable", "finding": {k: finding.get(k) for k in ("source", "ruleId", "line", "message")}})
                continue
            grouped.setdefault(anchor, []).append(finding)
        return grouped, unmatched

    old_groups, old_unmatched = index(before)
    new_groups, new_unmatched = index(after)
    groups: list[dict[str, Any]] = []
    for anchor in sorted(set(old_groups) | set(new_groups)):
        old_rows, new_rows = old_groups.get(anchor, []), new_groups.get(anchor, [])
        old_severities = sorted({str(_lookup(row, "severity")) for row in old_rows})
        new_severities = sorted({str(_lookup(row, "severity")) for row in new_rows})
        if not old_rows:
            status = "new"
        elif not new_rows:
            status = "fixed"
        elif len(new_rows) < len(old_rows):
            status = "mixed"
        elif old_severities != new_severities:
            status = "severity_changed"
        else:
            status = "persisting"
        groups.append({
            "anchor": {"source": anchor[0] if anchor[0] != "__unknown__" else "unknown",
                       "identity": list(anchor[1:])},            "status": status,
            "before": {"count": len(old_rows), "severities": old_severities,
                       "lines": [row.get("line") for row in old_rows]},
            "after": {"count": len(new_rows), "severities": new_severities,
                      "lines": [row.get("line") for row in new_rows]},
            "persistingCount": min(len(old_rows), len(new_rows)) if old_rows and new_rows else 0,
            "fixedCount": (len(old_rows) - len(new_rows)) if len(old_rows) > len(new_rows) else 0,
            "newCount": (len(new_rows) - len(old_rows)) if len(new_rows) > len(old_rows) else 0,
            "sample": _sight(new_rows[0] if new_rows else old_rows[0]),
        })
    summary = {
        "beforeTotal": len(before),
        "afterTotal": len(after),
        "fixedGroups": sum(g["status"] == "fixed" for g in groups),
        "persistingGroups": sum(g["status"] == "persisting" for g in groups),
        "mixedGroups": sum(g["status"] == "mixed" for g in groups),
        "newGroups": sum(g["status"] == "new" for g in groups),
        "severityChangedGroups": sum(g["status"] == "severity_changed" for g in groups),
        "unmatchedBefore": len(old_unmatched),
        "unmatchedAfter": len(new_unmatched),
    }
    return {
        "schemaVersion": 1,
        "summary": summary,
        "groups": groups,
        "unmatchedBefore": old_unmatched,
        "unmatchedAfter": new_unmatched,
        "coverage": {
            "grouping": "identical anchors are reported per group; within one group the tool cannot tell which line was repaired",
            "semanticFixVerified": False,
            "absenceMeaning": "no anchor-level difference found; rewrites that change every anchor field are surfaced as fixed+new pairs, not as persisting",
        },
    }


def _load_report(path: Any, label: str) -> dict[str, Any]:
    from .jsonio import read_json
    data = read_json(path)
    if not isinstance(data, dict):
        from .workflow import WorkflowError
        raise WorkflowError("DIAGNOSTICS_REPORT_INVALID",
                            f"{label} must be an aggregated diagnostics report with a findings array",
                            {"path": str(path)})
    # 两种合法形态：aggregate_diagnostics 的原始输出（findings/draftSha256 在顶层），
    # 或注册后的工件信封（同一组字段在 payload 里）。
    if not isinstance(data.get("findings"), list) and isinstance(data.get("payload"), dict):
        inner = data["payload"]
        if isinstance(inner.get("findings"), list):
            return {"findings": inner["findings"],
                    "draftSha256": data.get("draftSha256") or inner.get("draftSha256"),
                    "inputHash": data.get("inputHash") or inner.get("inputHash")}
    if not isinstance(data.get("findings"), list):
        from .workflow import WorkflowError
        raise WorkflowError("DIAGNOSTICS_REPORT_INVALID",
                            f"{label} must be an aggregated diagnostics report with a findings array",
                            {"path": str(path)})
    return data


def model_diff(result: dict[str, Any]) -> dict[str, Any]:
    """Compact unchanged groups without dropping their unresolved locations."""
    from copy import deepcopy
    compact = deepcopy(result)
    unchanged: dict[str, list[dict]] = {}
    changed = []
    for group in compact['groups']:
        if (group['status'] == 'persisting' and group['before']['count'] == group['after']['count']
                and group['before']['severities'] == group['after']['severities']):
            unchanged.setdefault(group['anchor']['source'], []).append({
                'identity': group['anchor']['identity'], 'count': group['after']['count'],
                'lines': group['after']['lines'], 'severities': group['after']['severities']})
        else:
            changed.append(group)
    compact['groups'] = changed
    compact['persisting'] = [{'source': source, 'items': items} for source, items in unchanged.items()]
    compact['view'] = 'model'
    compact['coverage']['persistingMeaning'] = 'unchanged groups remain unresolved; current locations are retained in persisting'
    return compact


def diff_reports(before_path, after_path, *, view: str = 'full') -> dict[str, Any]:
    """Diff two report files and annotate with their draft bindings when present."""
    if view not in ('full', 'model'):
        raise ValueError('view must be full or model')
    before = _load_report(before_path, "before report")
    after = _load_report(after_path, "after report")
    result = diff_findings(before["findings"], after["findings"])
    result["before"] = {"draftSha256": before.get("draftSha256"), "inputHash": before.get("inputHash")}
    result["after"] = {"draftSha256": after.get("draftSha256"), "inputHash": after.get("inputHash")}
    if before.get("draftSha256") and after.get("draftSha256") and before["draftSha256"] == after["draftSha256"]:
        result["summary"]["draftUnchanged"] = True
    return model_diff(result) if view == 'model' else result
