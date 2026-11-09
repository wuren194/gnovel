#!/usr/bin/env python3
"""Sepia 中文启发式指标桥（不是语义审校）。

本脚本是 Python 侧的正则与统计指标器（句长分布、套话模板、连词/冗余/通胀词），
只提供可定位的表面证据。语义审校由 Editor 结合当前计划和正文执行。本脚本
不装载第三方 Skill；需要外部方法时由作者显式通过 Provider 接入。

支持的操作（与 argparse 一致）：
  - review / diagnose: 人读文本诊断（仅当有定位证据时给出修复方向）
  - json: aggregate_diagnostics / Editor 消费的机器可读指标
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

try:
    from core.exit_codes import EXIT_FAIL_CLOSED, FailClosedArgumentParser
except ImportError:  # 作为 scripts 包模块被导入（如 tests）时走包路径
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, FailClosedArgumentParser

ROOT = Path(__file__).resolve().parents[1]

# Connective and slop words from zh.md and govel text-patterns
ZH_CONNECTIVES = ("和", "以及", "并且", "同时", "此外", "因此", "然而", "不仅如此", "与此同时")
ZH_DISYLLABIC_PADDING = ("进行", "加以", "予以", "做出")
ZH_AI_TEMPLATES = (
    (r"不是[^，。！？]+而是", "NOT-A-BUT-B: 试图用双重否定界定意义，改用直陈单一动作"),
    (r"[^，。！？]+意味着", "EXPLAIN-MEANING: 旁白解释腔，剥夺读者推导空间"),
    (r"(意识|绝望|喜悦|屈辱|痛苦|本能)是从[^，。！？]+的", "CLEFT-EMPHASIS: 分裂句焦点强调与抽象主语，核对强调是否提供必要信息，保留有效因果"),
    (r"正是[^，。！？]+才", "CLEFT-EMPHASIS: 焦点分裂强调句，核对强调与上下文的作用"),
    (r"身体的妥协[，、]远比语言来得诚实", "CLICHE-GOLDEN-RULE: 总结式升华金句"),
    (r"仿佛在这一瞬间彻底凝固", "CLICHE-FROZEN: 库存情绪与意象冻结"),
    (r"(显然|事实上|实际上|毫无疑问|说白了|换句话说)", "SLOP-EXPLAINER: 说明书式解释词汇"),
)
INFLATION_ADJECTIVES = ("极其", "极度", "惨绝人寰", "不可思议", "狂暴", "恐怖至极")


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Keep closing quotation marks with the sentence; ignore punctuation-only tails."""
    spans = []
    for match in re.finditer(r'[^。！？\n]+[。！？]*[”’」』"]*', text):
        value = match.group().strip()
        if not re.search(r'[\w\u4e00-\u9fff]', value) or value.startswith(('#', '>', '|', '```')):
            continue
        start = match.start() + len(match.group()) - len(match.group().lstrip())
        spans.append((start, start + len(value)))
    return spans


def split_sentences(text: str) -> list[str]:
    return [text[start:end] for start, end in sentence_spans(text)]


def analyze_zh_calibration(text: str) -> dict[str, Any]:
    """Surface calibration: sentence rhythm runs stay inside one prose paragraph."""
    spans = sentence_spans(text)
    sentences = [text[start:end] for start, end in spans]
    if not sentences:
        return {"sentence_count": 0, "length_sd": 0.0, "mean_length": 0.0, "flat_runs": [], "flat_runs_count": 0, "template_hits": [], "connective_hits": {}, "padding_hits": {}, "inflation_hits": {}}

    lengths = [len(s) for s in sentences]
    n = len(lengths)
    mean_len = sum(lengths) / n
    variance = sum((l - mean_len) ** 2 for l in lengths) / n if n > 1 else 0.0
    sd = math.sqrt(variance)

    # Detect flat runs (runs of 3+ sentences of near-equal length, difference <= 3 chars)
    flat_runs = []
    current_run = [0]
    for i in range(1, n):
        previous, current = sentences[i - 1], sentences[i]
        paragraph_boundary = '\n' in text[spans[i - 1][1]:spans[i][0]]
        dialogue = any(mark in previous + current for mark in '“”「」『』"')
        if not paragraph_boundary and not dialogue and abs(lengths[i] - lengths[i - 1]) <= 3:
            current_run.append(i)
        else:
            if len(current_run) >= 3:
                flat_runs.append((current_run[0], current_run[-1], [lengths[idx] for idx in current_run]))
            current_run = [i]
    if len(current_run) >= 3:
        flat_runs.append((current_run[0], current_run[-1], [lengths[idx] for idx in current_run]))

    # Connective count
    connective_hits = {}
    for c in ZH_CONNECTIVES:
        cnt = text.count(c)
        if cnt > 0:
            connective_hits[c] = cnt

    # Disyllabic padding
    padding_hits = {}
    for p in ZH_DISYLLABIC_PADDING:
        cnt = text.count(p)
        if cnt > 0:
            padding_hits[p] = cnt

    # Template hits
    template_hits = []
    for pattern, desc in ZH_AI_TEMPLATES:
        for m in re.finditer(pattern, text):
            template_hits.append({"pattern": desc, "match": m.group(0), "start": m.start(), "end": m.end(), "line": text.count("\n", 0, m.start()) + 1})

    # Inflation adjectives
    inflation_hits = {}
    for adj in INFLATION_ADJECTIVES:
        cnt = text.count(adj)
        if cnt > 0:
            inflation_hits[adj] = cnt

    return {
        "sentence_count": n,
        "mean_length": round(mean_len, 2),
        "length_sd": round(sd, 2),
        "flat_runs_count": len(flat_runs),
        "flat_runs": [{"start": spans[a][0], "end": spans[b][1],
                       "line": text.count("\n", 0, spans[a][0]) + 1,
                       "lengths": values} for a, b, values in flat_runs],
        "connective_hits": connective_hits,
        "padding_hits": padding_hits,
        "template_hits": template_hits,
        "inflation_hits": inflation_hits,
    }


def run_review(file_path: Path) -> str:
    """Produce human-readable diagnosis adhering to Sepia format."""
    text = file_path.read_text(encoding="utf-8")
    zh_metrics = analyze_zh_calibration(text)

    report_lines = [
        f"SEPIA DIAGNOSIS — {file_path.name}",
        "Scope: surface pattern and sentence-rhythm observations",
        "Model: author=unknown executor=mechanical scanner",
        "Narrative layer: not evaluated by this scanner",
        "Prose layer: surface observations only",
        "",
        "--- [表面句式与结尾表达观察] ---",
    ]

    # Check ending reflection / summarization
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip() and not p.startswith(("#", ">", "|"))]
    if paragraphs:
        last_p = paragraphs[-1]
        if any(w in last_p for w in ("意味着", "身体的妥协", "证明了", "在这一刻", "从此", "彻底凝固")):
            report_lines.append(f"Group A (Thematic commentary): 命中章末总结/反思拖拽 —— 引用: \"{last_p[:40]}...\"")
        else:
            report_lines.append("Group A (Thematic commentary): 未见明显章末反思拖拽 (通过)")

    if zh_metrics["template_hits"]:
        report_lines.append("Group A & B (AI Templates & Explanatory Prose):")
        for hit in zh_metrics["template_hits"][:5]:
            report_lines.append(f"  - 命中: {hit['pattern']} [匹配: \"{hit['match']}\"]")
    else:
        report_lines.append("Group A & B (AI Templates): 未检出高危AI模式 (通过)")

    report_lines.extend([
        "",
        "--- [中文表面表达与句长观察] ---",
        f"句子总数: {zh_metrics['sentence_count']} | 平均字数: {zh_metrics['mean_length']} | 句长标准差 (SD): {zh_metrics['length_sd']}（仅描述本段，不作 AI 判定）",
    ])

    if zh_metrics["flat_runs_count"] > 0:
        report_lines.append(f"  ! 候选: 检出 {zh_metrics['flat_runs_count']} 处连续3句以上近似等长句流；需结合段落语境判断，不能据此判定 AI 写作或强制改写。")

    if zh_metrics["inflation_hits"]:
        report_lines.append(f"通胀形容词堆叠: {zh_metrics['inflation_hits']} (结合上下文核对重复修饰，保留必要信息)")
    if zh_metrics["padding_hits"]:
        report_lines.append(f"双音节动词冗余: {zh_metrics['padding_hits']} (如“进行XX”建议直陈动词)")

    # 修复方向只在有定位证据时给出；不得无条件建议「增加物理打断」或按固定比例改写——
    # 修订依据当前作品与审校证据，不把外部方法的统计比例当作配额。
    has_evidence = bool(zh_metrics["template_hits"] or zh_metrics["padding_hits"]
                        or zh_metrics["inflation_hits"])
    if has_evidence:
        report_lines.extend([
            "",
            "Plan (仅针对上文已定位的表面证据；架构与语义修复由 Editor 依据当前材料决定):",
            "  1. 删除或改写命中的套话/解释性句式，保留因果与人物动机；",
            "  2. 若段落确实因等长句而单调，再调整句式；不新增事件或凭空插入打断；",
            "  3. 削减通胀形容词与冗余双音节动词，保留其实际信息作用。",
        ])
    else:
        report_lines.extend(["", "Plan: 未发现足以支持机械改写的表面证据；等长句仅作语境提示（语义审校由 Editor 结合当前材料完成）。"])

    return "\n".join(report_lines)


def main() -> int:
    parser = FailClosedArgumentParser(description="Sepia De-AI Bridge for govel")
    parser.add_argument("operation", choices=["review", "diagnose", "json"], help="Operation to run")
    parser.add_argument("file", type=Path, help="Target markdown file")
    args = parser.parse_args()

    if not args.file.is_file():
        sys.stderr.write(f"Error: File not found: {args.file}\n")
        return EXIT_FAIL_CLOSED

    if args.operation in ("review", "diagnose"):
        print(run_review(args.file))
    elif args.operation == "json":
        text = args.file.read_text(encoding="utf-8")
        metrics = analyze_zh_calibration(text)
        print(json.dumps(metrics, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
