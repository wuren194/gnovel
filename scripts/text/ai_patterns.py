"""Registry-backed AI-pattern matching shared by prose diagnostics."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "shared" / "registries" / "text-patterns.json"
QUOTED_SPAN = re.compile(r"(?:“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]*\")")


def load_text_patterns() -> dict[str, Any]:
    try:
        from scripts.core.data_provider import registry
    except ImportError:
        from core.data_provider import registry
    try:
        data = registry('text-patterns', REGISTRY_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法加载机械文本规则 {REGISTRY_PATH}: {exc}") from exc
    if data.get("schemaVersion") != 5:  # 注册表自身格式版本，与项目 schema 无关
        raise ValueError(f"不支持的文本规则版本: {data.get('schemaVersion')}")
    rules = data.get("ai", {}).get("regex")
    if not isinstance(rules, list):
        raise ValueError("文本规则缺少 ai.regex")
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        rule_id = str(rule.get("ruleId") or f"AI-{index + 1:02d}")
        if rule_id in seen:
            raise ValueError(f"ai.regex[{index}] ruleId 重复: {rule_id!r}")
        seen.add(rule_id)
        try:
            re.compile(str(rule["pattern"]))
        except (KeyError, re.error) as exc:
            raise ValueError(f"ai.regex[{index}] 正则无效: {exc}") from exc
    return data


def _masked_narrative(text: str) -> str:
    lines = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            lines.append("".join("\n" if char == "\n" else " " for char in line))
            continue
        markup = in_fence or stripped.startswith(("#", ">", "|"))
        if markup:
            lines.append("".join("\n" if char == "\n" else " " for char in line))
            continue
        lines.append(QUOTED_SPAN.sub(lambda match: " " * len(match.group(0)), line))
    return "".join(lines)


def _paragraph_spans(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for match in re.finditer(r"\n\s*\n", text):
        yield start, match.start()
        start = match.end()
    yield start, len(text)


def scan_ai_patterns(text: str, *, mode: str = "mixed") -> list[dict[str, Any]]:
    """Return normalized matches after scope, mode, and density filtering."""
    registry = load_text_patterns()
    masked = _masked_narrative(text)
    results: list[dict[str, Any]] = []
    for rule in registry["ai"]["regex"]:
        if mode.lower() in {str(item).lower() for item in rule.get("exemptModes", [])}:
            continue
        source = masked if rule.get("scope", "narrative") == "narrative" else text
        compiled = re.compile(rule["pattern"])
        limit = int(rule.get("perParagraphLimit", 0))
        matches: list[re.Match[str]] = []
        if limit:
            for start, end in _paragraph_spans(source):
                paragraph_matches = list(compiled.finditer(source, start, end))
                matches.extend(paragraph_matches[limit:])
        else:
            matches = list(compiled.finditer(source))
        rule_id = str(rule.get("ruleId") or f"AI-{registry['ai']['regex'].index(rule) + 1:02d}")
        for match in matches:
            line_index = source.count("\n", 0, match.start())
            source_lines = text.splitlines()
            results.append({
                "line": line_index + 1,
                "ruleId": rule_id,
                "name": rule["name"],
                "severity": rule.get("severity", "blocking"),
                "matched": text[match.start():match.end()],
                "context": source_lines[line_index] if line_index < len(source_lines) else "",
                "rewriteHint": rule.get("rewriteHint", ""),
                "gate": bool(rule.get("gate", True)),
            })
    return sorted(results, key=lambda item: (item["line"], item["ruleId"], item["matched"]))
