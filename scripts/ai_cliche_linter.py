#!/usr/bin/env python3
"""Static prose diagnostics for gnovel.

The linter reports form-level evidence only.  It does not claim to prove POV,
character voice, causality, consent, or physical continuity.

Calibration baseline (v13): blocking rules MUST achieve 0 false positives on the
author's published chapters before being promoted to Editor hard-gate. Each rule
annotated with calibration status:
  - CALIBRATED: tested on {"<work>"} N chapters, 0 hits
  - UNCALIBRATED: pending human-verified corpus run
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

try:
    from scripts import intensity_registry
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
    from scripts.text_utils import chinese_char_count
    from scripts.text.ai_patterns import scan_ai_patterns
except ImportError:  # Direct script invocation without the repository on sys.path.
    import intensity_registry
    from core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
    from text_utils import chinese_char_count
    from text.ai_patterns import scan_ai_patterns


class ClicheMatch(NamedTuple):
    line_num: int
    pattern_name: str
    matched_text: str
    line_content: str
    severity: str = "warning"
    rule_id: str = "STYLE"
    rewrite_hint: str = ""  # points to libraries/de-ai-humanize/sections/ section slug


# AI 句式规则唯一真源：shared/registries/text-patterns.json.ai.regex。
# 本模块只保留 linter 报告与兼容 CLI，不平行声明正则或严重度。

BODY_WORDS = (
    "耳", "脸", "喉", "肩", "背", "手", "指", "掌", "腿", "膝", "腰", "腹", "胸",
    "皮肤", "肌肉", "骨", "呼吸", "喘", "汗", "疼", "酸", "胀", "麻", "酥", "烫", "紧", "颤",
)

# 破折号、转折词与功能词的密度阈值唯一真源：validate-text.py S-13/S-17/S-18，
# 词表见 shared/registries/text-patterns.json。本 linter 不再平行判定，
# 避免与机械层给出矛盾结论。


def _is_markup_or_instruction(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith(("#", "```", "|", ">"))
        or (stripped.startswith(("【", "[")) and "规则" in stripped)
    )


def _add_regex_matches(text: str) -> list[ClicheMatch]:
    matches: list[ClicheMatch] = []
    for item in scan_ai_patterns(text):
        matches.append(ClicheMatch(
            item["line"], item["name"], item["matched"], item["context"],
            item["severity"], item["ruleId"], item["rewriteHint"],
        ))
    for line_num, line in enumerate(text.splitlines(), 1):
        if _is_markup_or_instruction(line):
            continue
        if re.search(r"(?:感到|觉得|涌起|升起).{0,10}(?:羞耻|屈辱|恐惧|绝望|痛苦)", line):
            if not any(word in line for word in BODY_WORDS):
                matches.append(ClicheMatch(
                    line_num,
                    "心理未绑定身体反应",
                    "abstract-emotion-without-body",
                    line.strip(),
                    "blocking",
                    "BODY-FIRST",
                    "abstract-emotion",
                ))
    return matches


def _sentence_rhythm(text: str) -> list[ClicheMatch]:
    matches: list[ClicheMatch] = []
    for line_num, line in enumerate(text.splitlines(), 1):
        if _is_markup_or_instruction(line):
            continue
        sentences = [part.strip() for part in re.split(r"[。！？；!?]", line) if len(part.strip()) > 3]
        if len(sentences) < 6:
            continue
        lengths = [len(sentence) for sentence in sentences]
        mean = sum(lengths) / len(lengths)
        variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
        if mean > 10 and variance < 5:
            matches.append(ClicheMatch(
                line_num,
                "句长方差过低",
                f"variance={variance:.1f}, mean={mean:.1f}",
                line.strip(),
                "warning",
                "RHYTHM-LOW-VARIANCE",
            ))
    return matches


def scan_text_for_cliches(text: str) -> list[ClicheMatch]:
    """Return deterministic form-level findings for compatibility callers."""
    matches = _add_regex_matches(text)
    matches.extend(_sentence_rhythm(text))
    return sorted(matches, key=lambda item: (item.line_num, item.rule_id, item.matched_text))


def check_intensity_level_consistency(text: str, declared_level: int, mode: str = "mixed") -> dict:
    """Check upper-bound violations; intensityLevel is a ceiling, not a quota.

    The word table lives in shared/registries/intensity.json.  The floor side
    ("did the prose reach the declared level?") is validate-text.py S-09b and is
    deliberately NOT duplicated here.
    """
    minimum, maximum = intensity_registry.level_bounds()
    if not minimum <= declared_level <= maximum:
        return {
            "status": "INVALID",
            "declared_level": declared_level,
            "message": f"intensityLevel 必须在 {minimum}–{maximum} 之间",
            "violations": [],
        }
    forbidden = set(intensity_registry.ceiling_violations(declared_level))
    if declared_level < intensity_registry.extreme_level():
        # Level 10 专属内容在任何较低声明下都是越界，与分级词表叠加。
        forbidden |= set(intensity_registry.extreme_markers())
    ambiguous = set(intensity_registry.ambiguous_ceiling_words())
    candidates = sorted(word for word in forbidden & ambiguous if word in text)
    violations = sorted(word for word in forbidden - ambiguous if word in text)
    return {
        "status": "INCONSISTENT" if violations else 'REVIEW_REQUIRED' if candidates else "PASS",
        "declared_level": declared_level,
        "mode": mode,
        "message": "检测到超出声明上限的词汇" if violations else "未检测到张力等级上限违规",
        "violations": violations,
        "contextReview": candidates,
    }


def build_report(text: str, *, intensity_level: int | None = None, mode: str = "mixed") -> dict:
    matches = scan_text_for_cliches(text)
    counts = {
        "blocking": sum(1 for item in matches if item.severity == "blocking"),
        "error": sum(1 for item in matches if item.severity == "error"),
        "advisory": sum(1 for item in matches if item.severity == "advisory"),
    }
    han_count = chinese_char_count(text)
    blocking_density = (counts["blocking"]) / max(han_count / 1000, 1)
    dirty = check_intensity_level_consistency(text, intensity_level, mode) if intensity_level is not None else None
    if counts["blocking"] >= 3:
        # 三条及以上 blocking = 场景是整段 AI 生成，需回生成器重写
        recommendation = "rewrite_required"
    elif counts["blocking"] >= 1 or counts["error"] >= 6 or (dirty and dirty["status"] in {'INCONSISTENT', 'INVALID'}):
        # 单条 blocking 可外科修复，不是全章重写
        recommendation = "surgical_edit"
    elif dirty and dirty.get('contextReview'):
        recommendation = 'review_advisory'
    else:
        recommendation = "pass"
    return {
        "schemaVersion": 1,
        "tool": "ai-cliche-linter",
        "limitations": ["form-only", "no-pov-proof", "no-ooc-proof", "no-causality-proof", "no-consent-proof"],
        "calibrationStatus": "UNCALIBRATED",  # pending human-authored corpus validation
        "metrics": {
            "hanCount": han_count,
            "findingCount": len(matches),
            "counts": counts,
            "blockingDensityPerKHan": round(blocking_density, 2),
        },
        "recommendation": recommendation,
        "findings": [
            {
                "line": item.line_num,
                "ruleId": item.rule_id,
                "severity": item.severity,
                "pattern": item.pattern_name,
                "matched": item.matched_text,
                "context": item.line_content,
                "rewriteHint": item.rewrite_hint,
            }
            for item in matches
        ],
        "intensityLevelCheck": dirty,
    }


def main() -> int:
    parser = FailClosedArgumentParser(description="Static gnovel prose diagnostics")
    parser.add_argument("file_path", type=Path, help="Markdown or text file to scan")
    parser.add_argument("--json", action="store_true", help="Output the stable JSON report")
    parser.add_argument("--output", type=Path, help="Write the report to a file")
    minimum, maximum = intensity_registry.level_bounds()
    parser.add_argument(
        "--intensity-level", "--check-intensity-level", dest="intensity_level", type=int,
        help=f"Declared ceiling ({minimum}-{maximum}); table: shared/registries/intensity.json",
    )
    parser.add_argument("--mode", default="mixed", choices=["plot", "buildUp", "mixed", "peakScene"])
    args = parser.parse_args()
    try:
        text = args.file_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"无法读取 {args.file_path}: {exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    report = build_report(text, intensity_level=args.intensity_level, mode=args.mode)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    if args.json or not args.output:
        print(serialized)
    return exit_code_for_recommendation(report["recommendation"])


if __name__ == "__main__":
    raise SystemExit(main())
