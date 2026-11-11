#!/usr/bin/env python3
"""Static prose diagnostics for govel.

The linter reports form-level evidence only.  It does not claim to prove POV,
character voice, causality, consent, or physical continuity.

Style candidates require contextual review. Synthetic regression controls do
not calibrate literary quality, establish authorship, or authorize a rewrite.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from scripts import intensity_registry
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
    from scripts.text_utils import chinese_char_count
    from scripts.text.ai_patterns import load_text_patterns, paragraph_spans, scan_ai_patterns
    from scripts.text.prose_spans import mask_prose
except ImportError:  # Direct script invocation without the repository on sys.path.
    import intensity_registry
    from core.exit_codes import EXIT_FAIL_CLOSED, exit_code_for_recommendation, FailClosedArgumentParser
    from text_utils import chinese_char_count
    from text.ai_patterns import load_text_patterns, paragraph_spans, scan_ai_patterns
    from text.prose_spans import mask_prose


# 词面规则、范围和阈值来自 text-patterns 注册表；本模块只组装报告。
# 标点和功能词密度仍由 validate-text 的既有检查负责。


def _sentence_rhythm(text: str) -> list[dict]:
    policy = load_text_patterns()["diagnosticPolicy"]["sentenceRhythm"]
    source = mask_prose(text)
    matches = []
    for start, end in paragraph_spans(source):
        paragraph = source[start:end]
        sentences = [part.strip() for part in re.split(r"[。！？；!?]", paragraph)
                     if len(part.strip()) >= policy["minSentenceChars"]]
        if len(sentences) < policy["minSentences"]:
            continue
        lengths = [len(sentence) for sentence in sentences]
        mean = sum(lengths) / len(lengths)
        variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
        if mean > policy["minMeanChars"] and variance < policy["maxVariance"]:
            start += len(paragraph) - len(paragraph.lstrip())
            end -= len(paragraph) - len(paragraph.rstrip())
            matches.append({
                "line": text.count("\n", 0, start) + 1,
                "column": start - text.rfind("\n", 0, start), "start": start, "end": end,
                "name": "连续句长接近", "ruleId": "RHYTHM-LOW-VARIANCE",
                "matched": text[start:end], "context": text[start:end],
                "severity": policy["severity"], "review": "semantic", "gate": False,
                "metrics": {"variance": round(variance, 2), "meanChars": round(mean, 2), "sentences": len(sentences)},
                "rewriteHint": "检查连续句子是否机械同长；有意节奏可保留，需要调整时从现有句子拆合，不补无关内容。",
            })
    return matches


def scan_text_for_cliches(text: str, *, mode: str = "mixed") -> list[dict]:
    """Return deterministic candidates, including the evidence for review."""
    matches = scan_ai_patterns(text, mode=mode)
    matches.extend(_sentence_rhythm(text))
    return sorted(matches, key=lambda item: (item["start"], item["ruleId"], item["end"]))


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
    matches = scan_text_for_cliches(text, mode=mode)
    counts = {
        severity: sum(1 for item in matches if item["severity"] == severity)
        for severity in ("blocking", "error", "advisory", "info")
    }
    han_count = chinese_char_count(text)
    narrative_han_count = chinese_char_count(mask_prose(text))
    blocking_density = counts["blocking"] * 1000 / max(narrative_han_count, 1)
    dirty = check_intensity_level_consistency(text, intensity_level, mode) if intensity_level is not None else None
    if any(item["gate"] for item in matches) or (dirty and dirty["status"] in {'INCONSISTENT', 'INVALID'}):
        recommendation = "surgical_edit"
    elif matches or (dirty and dirty.get('contextReview')):
        recommendation = 'review_advisory'
    else:
        recommendation = "pass"
    return {
        "schemaVersion": 1,
        "tool": "ai-cliche-linter",
        "limitations": ["form-only", "no-ai-origin-proof", "no-pov-proof", "no-ooc-proof", "no-causality-proof", "no-consent-proof"],
        "calibrationStatus": "UNCALIBRATED",  # pending human-authored corpus validation
        "metrics": {
            "hanCount": han_count,
            "narrativeHanCount": narrative_han_count,
            "findingCount": len(matches),
            "counts": counts,
            "blockingDensityPerKHan": round(blocking_density, 2),
        },
        "recommendation": recommendation,
        "findings": [
            {**{key: value for key, value in item.items() if key != "name"}, "pattern": item["name"]}
            for item in matches
        ],
        "intensityLevelCheck": dirty,
    }


def main() -> int:
    parser = FailClosedArgumentParser(description="Static govel prose diagnostics")
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
