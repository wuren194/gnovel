#!/usr/bin/env python3
"""Shared text preprocessing for structured-writing tools."""

from __future__ import annotations

import re

# 正文外协议行。默认剥离只用于兼容旧的“正文 + 审查块”产物；新产物应把
# Gate、盲审和 L2 结果保存为独立文件。这里必须与 SKILL.md 的交付协议同步，
# 否则元信息会被当成正文，污染字数、句长和密度检查。
META_LINE = re.compile(
    r"^\s*\[(?:"
    r"PARAM|GATE(?:-(?:MIN|FULL))?|FALL[-_]GATE|CAUSAL-GATE|"
    r"CAUSAL-PROOF|CAUSAL|POV|L2|INFO|A-(?:19|20|21|22|23)"
    r")\b",
    re.IGNORECASE,
)


def strip_meta(text: str) -> tuple[str, int]:
    kept: list[str] = []
    stripped = 0
    for line in text.splitlines():
        if META_LINE.match(line):
            stripped += 1
            continue
        kept.append(line)
    return re.sub(r"^\n+", "", "\n".join(kept)), stripped


# 汉字计数统一口径：CJK 扩展 A + 基本区 + 〇 + 扩展 B 及以后。
# 全库唯一真源（validate-text.py / continuation_context.py / story_summarizer.py 共用）。
HAN_PATTERN = r"[\u3400-\u9fff\u3007\U00020000-\U0002EBEF]"


def chinese_char_count(text: str) -> int:
    return len(re.findall(HAN_PATTERN, text))


def target_tolerance(target: int) -> int:
    """字数目标容差（±5%，下限 50）。wc-target 与 validate-text 共用同一公式。"""
    return max(50, int(target * 0.05))


def parse_frontmatter_and_prose(text: str) -> tuple[dict, str]:
    """
    Parse a Markdown file that may contain YAML Frontmatter fenced by '---'.
    Returns (frontmatter_dict, clean_markdown_prose).
    """
    if not text.startswith("---"):
        return {}, text.strip()

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()

    frontmatter_str = parts[1].strip()
    prose_str = parts[2].strip()

    metadata = {}
    # Simple line-by-line key: value parser to avoid hard dependency on PyYAML if not present
    for line in frontmatter_str.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            metadata[k.strip()] = v.strip().strip("\"'")

    return metadata, prose_str
