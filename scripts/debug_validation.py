#!/usr/bin/env python3
"""结构化机械门违规明细（修稿驱动用）。

aggregate_diagnostics.py --json 只给"还有什么违规"的摘要与首例；
本脚本按行号/段落输出可操作的定位，直接驱动修稿：
行号 + 原文 + 缺哪通道 + 修的路径。复用 validate-text 的同一判定
逻辑（同一词表、同一阈值），保证 debug 与 gate 一致。

用法：
  python3 scripts/debug_validation.py <draft> --mode mixed --intensity-level 5
  python3 scripts/debug_validation.py <draft> --mode mixed --intensity-level 5 --json

输出默认人类可读；--json 给结构化明细（供 subagent/surgical 消费）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
try:
    from scripts.intensity_registry import default_level
except ImportError:
    from intensity_registry import default_level


def load_validate() -> Any:
    spec = importlib.util.spec_from_file_location("vt", SCRIPT_DIR / "validate-text.py")
    vt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vt)
    return vt


def smask(flag: bool) -> str:
    return "嗅" if flag else "·"


# ---------------------------------------------------------------------------
# S-10s 句长明细
# ---------------------------------------------------------------------------
def long_sentences(vt, text: str, mode: str) -> list[dict]:
    """S-10s 行号明细；判定来自 validate-text 的共享实现（机制 39）。

    此前本函数自带一份判定，在所有模式给黏腻句 80 字上限，而门禁只在
    mixed/peakScene 放宽——plot/buildUp 下 45–80 字的黏腻句门禁报、明细不报。
    """
    out: list[dict] = []
    for item in vt.sentence_length_violations(text, mode):
        if item["cap"] is not None:
            kind = "黏腻感官句" if item["sticky"] else "plain"
            fix = "复读长句的停顿与因果，不要求拆句" if item['severity'] == 'info' else "补合适的内部停顿"
        else:
            kind = "25字无标点"
            fix = "断句或补内部标点（叙事句超25字必须有标点）"
        out.append(
            {
                "rule": "S-10s",
                "line": item["line"],
                "len": item["len"],
                "cap": item["cap"],
                "kind": kind,
                "text": item["text"][:80],
                "problems": item["problems"],
                "severity": item['severity'],
                "fix": fix,
            }
        )
    return out


# ---------------------------------------------------------------------------
# S-09 感官通道明细（逐核心动作段 + 相邻合并后仍不足）
# ---------------------------------------------------------------------------
def sensory_misses(vt, text: str) -> list[dict]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    core_action = vt.scene_action_pattern()
    smell_kw = vt.load_text_patterns()['smellKeywords']
    touch_kw = ["热", "烫", "冷", "冰", "硬", "软", "湿", "黏", "滑", "疼", "酸", "胀", "麻", "酥", "触", "贴", "顶", "压", "掐", "勒"]
    sight_kw = ["看", "见", "光", "影", "暗", "黑", "红", "白", "青", "紫", "肿", "涨", "鼓", "隆", "轮廓", "形状"]
    ex_smell = re.compile(r"(?:不闻|不问|新闻|香(?:港|烟)|臭名|臭美)")
    ex_touch = re.compile(r"(?:硬(?:着头皮|是|要|朗)|软(?:件|弱|化|禁)|滑(?:稽|头)|疼(?:惜|爱)|冷(?:漠|峻))")
    ex_sight = re.compile(r"(?:看(?:来|似|守|法)|见(?:识|解|证|面)|光(?:明|荣|辉|泽)|红(?:人|军|利|旗|楼))")

    def ch(blob, ks, ex):
        return any(k in ex.sub("", blob) for k in ks)

    core_entries = []  # (para_idx, channels(s,t,v), text)
    for pi, p in enumerate(paragraphs):
        if vt._is_separator_para(p):
            continue
        narr = vt._narrative_core_blob(p)
        if len(narr) < vt.diagnostic_policy()['sceneDynamics']['minNarrativeLength'] or not core_action.search(narr):
            continue
        s = ch(p, smell_kw, ex_smell)
        t = ch(p, touch_kw, ex_touch)
        v = ch(p, sight_kw, ex_sight)
        core_entries.append((pi, [s, t, v], p))

    misses: list[dict] = []
    for ei, (pi, chan, p) in enumerate(core_entries):
        merged = list(chan)
        for ni in (ei - 1, ei + 1):
            if 0 <= ni < len(core_entries):
                opi, och, _ = core_entries[ni]
                if abs(opi - pi) == 1:
                    merged = [a or b for a, b in zip(merged, och)]
        if sum(int(x) for x in merged) < 2:
            first_line = 1 + text[: text.find(p)].count("\n") if p in text else 0
            misses.append(
                {
                    "rule": "S-09",
                    "line": first_line or pi + 1,
                    "para_idx": pi,
                    "channels": {"smell": bool(chan[0]), "touch": bool(chan[1]), "sight": bool(chan[2])},
                    "text": p[:80],
                    "fix": "核心动作段需≥2通道（嗅/触/视）；相邻段合并仍不足",
                }
            )
    return misses


# ---------------------------------------------------------------------------
# S-08 段落节奏明细（违反的单句段序列）
# ---------------------------------------------------------------------------
def paragraph_rhythm_issues(vt, text: str) -> list[dict]:
    """S-08 行号明细；判定来自 validate-text 的共享实现（机制 39）。

    此前本函数自带一份连续单句段计数，不为 functional（合法结果/承认单句）重置
    run，对门禁认为合规的段落报「连续单句段 3 个」。
    """
    analysis = vt.paragraph_rhythm_analysis(text)
    issues: list[dict] = [
        {"rule": "S-08", "detail": detail, "text": ""}
        for detail in analysis["longParagraphIssues"]
    ]
    if analysis["maxConsecutive"] >= 3:
        issues.append({"rule": "S-08", "detail": f"连续单句段 {analysis['maxConsecutive']} 个（上限2）", "text": ""})
    if analysis["windowViolation"]:
        issues.append(
            {
                "rule": "S-08",
                "detail": f"任意8段窗口内叙述单句段{analysis['windowViolation']['singles']}个（上限2）",
                "text": "…".join(entry["para"][:30] for entry in analysis["windowViolation"]["window"]),
            }
        )
    for entry in analysis["adjacentViolations"]:
        issues.append({"rule": "S-08", "detail": "相邻叙述单句段之间缺少多句段", "text": entry["para"][:30]})
    if analysis["ratio"] > 0.20:
        issues.append(
            {"rule": "S-08", "detail": f"叙述单句段比例 {analysis['ratio']:.1%}（上限20%）", "text": ""}
        )
    return issues


# ---------------------------------------------------------------------------
# S-11 连续"他"主语
# ---------------------------------------------------------------------------
def subject_sentences_issues(vt, text: str) -> list[dict]:
    """S-11 行号明细；判定来自 validate-text 的共享实现（机制 39）。

    此前本函数只统计以「他」开头的句子，具名主语（沈叙推开门）、省略主语延续
    （他把外套脱了。抬手挂上椅背。）与 500 字窗三条子项全缺，明细与门禁结论分叉。
    """
    out: list[dict] = []
    for item in vt.subject_sentence_violations(text):
        out.append(
            {
                "rule": "S-11",
                "kind": item["kind"],
                "blocking": item["blocking"],
                "detail": item["detail"],
                "text": item["sentence"][:40],
            }
        )
    return out


# ---------------------------------------------------------------------------
# S-06 冲突对白密度（只报未达标）
# ---------------------------------------------------------------------------
def dialogue_density(vt, text: str, intensity_level: int, *, strict: bool = False) -> dict | None:
    # debug 输出负责定位缺口，即使 gate 把地板降为 info 也不能隐藏证据。
    # `strict` 只标记调用方是否把这条证据视为阻断。
    ok, detail, _level = vt.check_dialogue_dynamics_density(text, intensity_level, strict=True)
    if ok:
        return None
    dialogue_count = len(re.findall(vt.load_text_patterns()["dialogue_dynamics"]["pattern"], text))
    total_chars = len(text)
    policy = vt.diagnostic_policy()['dialogueDynamics']
    window = policy['windowSize']
    windows = max(1, (total_chars + window - 1) // window)
    required = next((r['required'] for r in policy['shortTiers'] if total_chars < r['belowTextSize']),
                    windows * policy['perLevel'].get(str(intensity_level), policy['defaultPerWindow']))
    return {
        "rule": "S-06",
        "found": dialogue_count,
        "required": required,
        "detail": detail,
        "strict": strict,
    }


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("draft_file", type=Path)
    ap.add_argument("--mode", required=True, choices=["plot", "buildUp", "mixed", "peakScene"])
    ap.add_argument("--intensity-level", type=int, default=default_level())
    ap.add_argument(
        "--strict-floor",
        action="store_true",
        help="将 S-06 冲突对白密度作为阻断项；默认仅作为建议",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument('--scene-paragraphs', default='')
    args = ap.parse_args()

    vt = load_validate()
    text = args.draft_file.read_text(encoding="utf-8")
    scene_text = vt.select_paragraphs(text, args.scene_paragraphs)[0] if args.scene_paragraphs else text

    blocks: dict[str, list] = {
        "S-10": vt.ai_pattern_violations(text),
        "S-10s": long_sentences(vt, text, args.mode),
        "S-09": sensory_misses(vt, scene_text) if args.mode in {'mixed', 'peakScene'} and args.intensity_level >= vt.diagnostic_policy()['sceneDynamics']['minIntensityLevel'] else [],
        "S-08": paragraph_rhythm_issues(vt, text),
        "S-11": subject_sentences_issues(vt, text),
    }
    dd = dialogue_density(vt, scene_text, args.intensity_level, strict=args.strict_floor)
    if dd:
        blocks["S-06"] = [dd]

    total = sum(len(v) for v in blocks.values())
    if args.json:
        print(json.dumps({"mode": args.mode, "intensityLevel": args.intensity_level, "total": total, "blocks": blocks}, ensure_ascii=False, indent=2))
        return

    print(f"# debug_validation  {args.draft_file}")
    print(f"mode={args.mode} intensity={args.intensity_level}  违规实例 {total} 条\n")
    for rule, items in blocks.items():
        if not items:
            continue
        action = {"S-10": "核对原句后定点修复", "S-10s": "拆句", "S-09": "补感官通道", "S-08": "并/拆段落", "S-06": "核对对白约束", "S-11": "变换主语"}[rule]
        print(f"== {rule}  ({len(items)} 条)  → {action}")
        for it in items:
            ln = it.get("line", "")
            loc = f"L{ln}" if ln else "    "
            if rule == "S-06":
                print(f"  {loc}  [{it['detail']}]")
            elif rule in {"S-08", "S-11"}:
                print(f"  {loc}  {it.get('detail','')}  {it.get('text','')}")
            elif rule == "S-09":
                ch = it["channels"]
                print(f"  L{it['line']:<4} {smask(ch['smell'])}{smask(ch['touch'])}{smask(ch['sight'])}  {it['text']}")
            else:
                cap_text = f"{it['cap']}" if it.get("cap") is not None else "—"
                print(f"  L{it['line']:<4} {it['len']}/{cap_text}字[{it['kind']}]  {it['text']}   →{it['fix']}")
        print()


if __name__ == "__main__":
    main()
