#!/usr/bin/env python3
"""Model degeneration detection for govel long-form prose.

Reports four classes of possible generation defects:
- Verbatim repetition / near-duplicate sentences
- Truncated output (no terminal punctuation at file end)
- Placeholder refusals / AI self-reference
- Engineering term leakage (payload/outline vocabulary leaking into prose)

Short repetitions are contextual review candidates. A finding does not prove
that a model authored the prose or prescribe rewriting the entire chapter.
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
    from text.ai_patterns import load_text_patterns
    from text.prose_spans import mask_prose
except ImportError:  # 作为 scripts 包模块被导入（如 tests）时走包路径
    from scripts.core.exit_codes import exit_code_for_recommendation, FailClosedArgumentParser
    from scripts.text.ai_patterns import load_text_patterns
    from scripts.text.prose_spans import mask_prose


class DegenMatch(NamedTuple):
    line_num: int
    pattern_name: str
    matched_text: str
    line_content: str
    severity: str  # "blocking" | "advisory"


# Repetition thresholds: text-patterns.diagnosticPolicy.degeneration.

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
    r"(?:作为(?:一个|一名)?\s*(?:AI(?:语言模型|助手|模型)?|人工智能(?:助手)?|(?:大型?|大)?语言模型)"
    r"(?=[，,。；;：:！!？?\s]|我|无法|不能|$)"
    r"|我(?:无法|不能)(?:再|继续)?(?:生成|创作|撰写|续写)(?:这(?:段|类|些)|该|相关|所请求的)?(?:内容|文本|正文|故事)"
    r"|让我们(?:来)?看(?:一下)?(?:接下来|下面)的(?:情节|故事|章节)(?:发展)?)"
)
PLACEHOLDER_PATTERNS = [r"\[待(?:补|填|写)\]", r"\{[A-Z_]+\}", r"\[此处.*?\]"]



def _char_bigrams(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text)
    if len(text) < 2:
        return set()
    return {text[i:i+2] for i in range(len(text)-1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)




def _normalize_for_comparison(text: str) -> str:
    """Strip punctuation for repetition comparison."""
    return re.sub(r"[，。！？、；：\"'“”‘’「」『』…—\-\.\!\?,;:\s]+", "", text)


def detect_repetitions(text: str) -> list[DegenMatch]:
    """Inspect actual sentences and adjacent lines, not just whole-line copies."""
    matches: list[DegenMatch] = []
    policy = load_text_patterns()['diagnosticPolicy']['degeneration']
    narrative = mask_prose(text)
    lines = narrative.splitlines()
    originals = text.splitlines()
    sentences: list[tuple[int, str]] = []
    for found in re.finditer(r'[^。！？!?\n]+(?:[。！？!?]+|$)', narrative, re.MULTILINE):
        norm = _normalize_for_comparison(found[0])
        if norm:
            start = found.start() + len(found[0]) - len(found[0].lstrip())
            sentences.append((text.count('\n', 0, start) + 1, norm))

    counter = Counter(s for _, s in sentences if len(s) >= policy['verbatimMinChars'])
    for sentence, count in counter.items():
        if count >= policy['verbatimMinCount']:
            first_line = next(line for line, value in sentences if value == sentence)
            matches.append(DegenMatch(first_line, '逐字复读', sentence[:60] + ('…' if len(sentence) > 60 else ''),
                                      originals[first_line - 1], 'blocking'))

    # A short refrain may be intentional; it does not veto the prose.
    cursor = 0
    while cursor < len(sentences):
        line_num, sentence = sentences[cursor]
        stop = cursor + 1
        while stop < len(sentences) and sentences[stop][1] == sentence:
            stop += 1
        if (policy['shortRepeatMinChars'] <= len(sentence) < policy['verbatimMinChars']
                and stop - cursor >= policy['shortRepeatMinCount']):
            matches.append(DegenMatch(line_num, '短句连续复读', sentence,
                                      originals[line_num - 1], 'advisory'))
        cursor = stop

    for i in range(len(lines) - 1):
        a, b = lines[i].strip(), lines[i + 1].strip()
        if not a or not b:
            continue
        a_norm, b_norm = _normalize_for_comparison(a), _normalize_for_comparison(b)
        if len(a_norm) < policy['adjacentMinChars'] or len(b_norm) < policy['adjacentMinChars']:
            continue
        sim = _jaccard(_char_bigrams(a_norm), _char_bigrams(b_norm))
        if sim >= policy['adjacentSimilarity']:
            matches.append(DegenMatch(i + 1, '紧邻复读(相似度={:.2f})'.format(sim),
                                      a[:40], originals[i], 'advisory'))
    return matches

def detect_truncation(text: str) -> list[DegenMatch]:
    """Check the last prose character, excluding trailing document metadata."""
    stripped = mask_prose(text, include_dialogue=True).rstrip()
    if stripped and not TERMINAL_PUNCTUATION.search(stripped[-1]):
        return [DegenMatch(text.count('\n', 0, len(stripped)) + 1, '末尾截断',
                           stripped[-80:], '', 'blocking')]
    return []

def detect_ai_self_reference(text: str) -> list[DegenMatch]:
    """Require an explicit assistant/generation context, not ordinary first person."""
    matches: list[DegenMatch] = []
    originals = text.splitlines()
    for line_num, line in enumerate(mask_prose(text).splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        found = AI_SELF_REFERENCE.search(stripped)
        if found:
            matches.append(DegenMatch(line_num, 'AI自指/拒绝语', found.group(),
                                      originals[line_num - 1], 'blocking'))
        for pattern in PLACEHOLDER_PATTERNS:
            for found in re.finditer(pattern, stripped):
                matches.append(DegenMatch(line_num, '占位符', found.group(),
                                          originals[line_num - 1], 'blocking'))
    return matches

def detect_engineering_terms(text: str) -> list[DegenMatch]:
    """Report pipeline vocabulary outside quoted speech and document metadata."""
    matches: list[DegenMatch] = []
    originals = text.splitlines()
    for line_num, line in enumerate(mask_prose(text).splitlines(), 1):
        for patterns, severity in ((ENGINEERING_TIER1_PATTERNS, 'blocking'),
                                   (ENGINEERING_TIER2_PATTERNS, 'advisory')):
            for pattern, rule_id, name in patterns:
                for found in re.finditer(pattern, line):
                    matches.append(DegenMatch(line_num, name, found.group(),
                                              originals[line_num - 1], severity))
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
                "review": "mechanical" if m.severity == "blocking" else "semantic",
            }
            for m in blocking
        ],
        "advisory": [
            {
                "line": m.line_num,
                "pattern": m.pattern_name,
                "matched": m.matched_text,
                "severity": m.severity,
                "review": "mechanical" if m.severity == "blocking" else "semantic",
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
