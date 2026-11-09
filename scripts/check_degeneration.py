#!/usr/bin/env python3
"""Model degeneration detection for govel long-form prose.

Detects four classes of model degeneration that the model itself cannot notice:
- Verbatim repetition / near-duplicate sentences
- Truncated output (no terminal punctuation at file end)
- Placeholder refusals / AI self-reference
- Engineering term leakage (payload/outline vocabulary leaking into prose)

Blocking hits mean "go back to the generator and rewrite", not "de-slop this".
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

try:
    from core.exit_codes import exit_code_for_recommendation, FailClosedArgumentParser
except ImportError:  # 作为 scripts 包模块被导入（如 tests）时走包路径
    from scripts.core.exit_codes import exit_code_for_recommendation, FailClosedArgumentParser


class DegenMatch(NamedTuple):
    line_num: int
    pattern_name: str
    matched_text: str
    line_content: str
    severity: str  # "blocking" | "advisory"
    tier: int  # 1 = blocking engineering leak, 2 = advisory chapter ref


# ---- Repetition detection ----
# Verbatim repeat: same sentence (>= 20 chars) appears >= 3 times
# Adjacent line repeat: two consecutive non-dialogue lines are near-identical
REPETITION_MIN_LEN = 20
REPETITION_MIN_COUNT = 3
ADJACENT_SIMILARITY_THRESHOLD = 0.75  # Jaccard on character bigrams

# ---- Truncation detection ----
TERMINAL_PUNCTUATION = re.compile(
    r"[。！？\.!\?」』”’\"'…\uff09\)\]\}》】〉〕〗〙〛—―～~·\n]$"
)

# ---- Engineering term leakage ----
# tier1: pure pipeline vocabulary — blocking
ENGINEERING_TIER1_PATTERNS = [
    (r"细纲", "T1-细纲", "大纲工程术语泄漏(blocking)"),
    (r"情节点", "T1-情节点", "大纲工程术语泄漏(blocking)"),
    (r"六维(?:Payload|载荷)", "T1-六维Payload", "工程术语泄漏(blocking)"),
    (r"张力等级(?:\s*档|级别|等级)", "T1-张力等级档", "张力等级分级术语泄漏(blocking)"),
    (r"ContextPack", "T1-ContextPack", "工程术语泄漏(blocking)"),
    (r"ChapterOutline\s", "T1-ChapterOutline", "工程术语泄漏(blocking)"),
    (r"SceneFragment", "T1-SceneFragment", "工程术语泄漏(blocking)"),
]
# tier2: chapter-level discourse — advisory (may be legitimate in meta-narrative)
ENGINEERING_TIER2_PATTERNS = [
    (r"第[一二三四五六七八九十百千\d]+章", "T2-第X章", "章节号指称(advisory)"),
    (r"上一章", "T2-上一章", "章节引用(advisory)"),
    (r"伏笔\s", "T2-伏笔", "工程词泄漏(advisory)"),
    (r"读者\s", "T2-读者", "元叙事(advisory)"),
    (r"本章", "T2-本章", "章节引用(advisory)"),
]

# ---- AI self-reference / refusal / placeholder ----
AI_SELF_REFERENCE = re.compile(
    r"(?:作为(?:一个)?AI|我不能|我无法|很抱歉|我理解|根据\S*的(?:训练|知识|数据))"
)
PLACEHOLDER_PATTERNS = [r"\[待(?:补|填|写)\]", r"\{[A-Z_]+\}", r"\[此处.*?\]"]

# ---- Dialogue exemption ----
# 开引号（中文弯引号/直角引号/英文引号）——行首是引号即视为对白行，豁免退化检查
DIALOGUE_LINE = re.compile(r'^[「」『』“”‘’\'"]')
DIALOGUE_PATTERN = re.compile(r'[「『“‘\'"][^」』”’\'"]*[」』”’\'"]')


def _char_bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text)
    if len(text) < 2:
        return set()
    return {text[i:i+2] for i in range(len(text)-1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_dialogue_line(line: str) -> bool:
    stripped = line.strip()
    return bool(DIALOGUE_LINE.match(stripped))


def _is_dialogue_heavy_paragraph(paragraph: str) -> bool:
    """Return True if paragraph is mostly dialogue (e.g. a dialogue exchange)."""
    dialogue_chars = sum(len(m.group()) for m in DIALOGUE_PATTERN.finditer(paragraph))
    total_chars = max(len(re.sub(r"\s+", "", paragraph)), 1)
    return dialogue_chars / total_chars > 0.6


def _normalize_for_comparison(text: str) -> str:
    """Strip punctuation for repetition comparison."""
    return re.sub(r"[，。！？、；：\"'“”‘’「」『』…—\-\.\!\?,;:\s]+", "", text)


def detect_repetitions(text: str) -> list[DegenMatch]:
    """Detect verbatim and near-adjacent line repetition."""
    matches: list[DegenMatch] = []
    lines = text.splitlines()

    # Verbatim sentence-level repetition
    sentences: list[tuple[int, str]] = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```", "|", ">")):
            continue
        if _is_dialogue_line(stripped):
            continue  # dialogue repetition is legitimate
        norm = _normalize_for_comparison(stripped)
        if len(norm) >= REPETITION_MIN_LEN:
            sentences.append((i, norm))

    counter = Counter(s for _, s in sentences)
    for sentence, count in counter.items():
        if count >= REPETITION_MIN_COUNT:
            first_occurrence = next(ln for ln, s in sentences if s == sentence)
            matches.append(DegenMatch(
                first_occurrence,
                "逐字复读",
                sentence[:60] + ("…" if len(sentence) > 60 else ""),
                "",
                "blocking",
                1,
            ))

    # Adjacent non-dialogue line repetition
    for i in range(len(lines) - 1):
        a, b = lines[i].strip(), lines[i+1].strip()
        if not a or not b:
            continue
        if _is_dialogue_line(a) or _is_dialogue_line(b):
            continue
        a_norm, b_norm = _normalize_for_comparison(a), _normalize_for_comparison(b)
        if len(a_norm) < 15 or len(b_norm) < 15:
            continue
        sim = _jaccard(_char_bigrams(a_norm), _char_bigrams(b_norm))
        if sim >= ADJACENT_SIMILARITY_THRESHOLD:
            matches.append(DegenMatch(
                i + 1,
                "紧邻复读(相似度={:.2f})".format(sim),
                a[:40],
                a,
                "advisory",
                2,
            ))

    return matches


def detect_truncation(text: str) -> list[DegenMatch]:
    """Detect truncated output: file ends without terminal punctuation."""
    matches: list[DegenMatch] = []
    stripped = text.rstrip()
    if stripped and not TERMINAL_PUNCTUATION.search(stripped[-1]):
        last_line_num = len(text.splitlines())
        matches.append(DegenMatch(
            last_line_num,
            "末尾截断",
            stripped[-80:] if len(stripped) > 80 else stripped,
            "",
            "blocking",
            1,
        ))
    return matches


def detect_ai_self_reference(text: str) -> list[DegenMatch]:
    """Detect AI self-reference, refusal language, or placeholders."""
    matches: list[DegenMatch] = []
    for line_num, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```", "|", ">")):
            continue
        if _is_dialogue_line(stripped):
            continue  # "我不能" in character dialogue is legitimate

        # A narration prefix does not turn the character's quoted words into
        # assistant speech. Preserve offsets and still inspect text outside quotes.
        narrative = DIALOGUE_PATTERN.sub(lambda m: ' ' * len(m.group()), stripped)
        if AI_SELF_REFERENCE.search(narrative):
            matches.append(DegenMatch(
                line_num,
                "AI自指/拒绝语",
                AI_SELF_REFERENCE.search(narrative).group(),
                stripped,
                "blocking",
                1,
            ))

        for pattern in PLACEHOLDER_PATTERNS:
            for found in re.finditer(pattern, stripped):
                matches.append(DegenMatch(
                    line_num,
                    "占位符",
                    found.group(),
                    stripped,
                    "blocking",
                    1,
                ))

    return matches


def detect_engineering_terms(text: str) -> list[DegenMatch]:
    """Detect engineering vocabulary leaking into prose."""
    matches: list[DegenMatch] = []
    for line_num, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```", "|", ">")):
            continue
        if _is_dialogue_line(stripped):
            continue

        for pattern, rule_id, name in ENGINEERING_TIER1_PATTERNS:
            for found in re.finditer(pattern, stripped):
                matches.append(DegenMatch(
                    line_num, name, found.group(), stripped, "blocking", 1,
                ))

        for pattern, rule_id, name in ENGINEERING_TIER2_PATTERNS:
            for found in re.finditer(pattern, stripped):
                matches.append(DegenMatch(
                    line_num, name, found.group(), stripped, "advisory", 2,
                ))

    return matches


def scan(text: str, file_label: str = "") -> dict:
    """Run all degeneration checks. Return a report dict."""
    all_matches = (
        detect_repetitions(text)
        + detect_truncation(text)
        + detect_ai_self_reference(text)
        + detect_engineering_terms(text)
    )

    blocking = [m for m in all_matches if m.severity == "blocking"]
    advisory = [m for m in all_matches if m.severity == "advisory"]

    return {
        "file": file_label,
        "blocking_count": len(blocking),
        "advisory_count": len(advisory),
        "total_count": len(all_matches),
        "blocking": [
            {
                "line": m.line_num,
                "pattern": m.pattern_name,
                "matched": m.matched_text,
                "severity": m.severity,
            }
            for m in blocking
        ],
        "advisory": [
            {
                "line": m.line_num,
                "pattern": m.pattern_name,
                "matched": m.matched_text,
                "severity": m.severity,
            }
            for m in advisory
        ],
        "recommendation": (
            "rewrite_with_generator" if blocking else
            "review_advisory" if advisory else
            "pass"
        ),
    }


def main():
    parser = FailClosedArgumentParser(
        description="Model degeneration detection for govel long-form prose"
    )
    parser.add_argument("file", help="File to scan")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8")
    report = scan(text, file_label=str(Path(args.file).name))

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    else:
        if report["blocking"]:
            print(f"[BLOCKING] {report['blocking_count']} degeneration signal(s) — rewrite required:")
            for b in report["blocking"]:
                print(f"  L{b['line']} [{b['pattern']}] {b['matched']}")
        if report["advisory"]:
            print(f"[ADVISORY] {report['advisory_count']} warning(s):")
            for a in report["advisory"]:
                print(f"  L{a['line']} [{a['pattern']}] {a['matched']}")
        if report["recommendation"] == "pass":
            print("PASS — no degeneration signals detected")
        print(f"\nRecommendation: {report['recommendation']}")

    sys.exit(exit_code_for_recommendation(report["recommendation"]))


if __name__ == "__main__":
    main()
