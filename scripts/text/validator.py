#!/usr/bin/env python3
"""
structured-writing 机械项快速回检脚本
按 shared/contracts/ 中可机械检测的项目，对正文做快速回检。

用法：
  python3 scripts/validate-text.py --mode peakScene < text.txt
  python3 scripts/validate-text.py --mode plot file.txt
  python3 scripts/validate-text.py --mode mixed --scene-paragraphs 3-8,12 --json body.txt
  python3 scripts/validate-text.py --mode peakScene --keep-meta mixed_with_gate.txt
  python3 scripts/validate-text.py --mode peakScene --no-strict-l5 --json body.txt

默认会剥离已登记的 Gate/盲审/L2 协议行，避免旧合并产物污染正文检。
新产物应保持 body-only：正文、Gate JSON 与审查结果分文件。

--mode:
  plot / buildUp  风格机械项 + 模式句长提示 + 声明档位的词面地板报告
  mixed / peakScene 全套；句长参考线来自 diagnosticPolicy；对话段不计入单句段配额
  各入口默认地板为 info；--strict-floor 显式启用

标点政策（2026-07-10g）：
  叙事：禁 —— 与 …
  对话行（引号开场/独占对话段内）：—— 与 … 放开，不计入省略号总量上限
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
import re
import sys
from pathlib import Path

try:
    from scripts import intensity_registry
    from scripts.text_utils import chinese_char_count, strip_meta, target_tolerance
    from scripts.text.parser import select_paragraphs as _select_paragraphs
    from scripts.text.ai_patterns import load_text_patterns as _load_text_patterns, scan_ai_patterns
    from scripts.core.project import diagnostic_policy
except ImportError:
    import intensity_registry
    from text_utils import chinese_char_count, strip_meta, target_tolerance
    from text.parser import select_paragraphs as _select_paragraphs
    from text.ai_patterns import load_text_patterns as _load_text_patterns, scan_ai_patterns
    from core.project import diagnostic_policy

ROOT = Path(__file__).resolve().parents[2]
TEXT_PATTERNS_PATH = ROOT / "shared" / "registries" / "text-patterns.json"


def load_text_patterns() -> dict:
    return _load_text_patterns()

RULE_ID_BY_NAME = {
    "破折号使用": "S-13",
    "省略号使用": "S-12",
    "句子长度": "S-10s",
    "句式节奏": "S-11",
    "段落节奏": "S-08",
    "AI句式套路": "S-10",
    "低信息副词补丁": "S-16",
    "形容词堆叠": "S-19",
    "感官通道": "S-09",
    "冲突对白密度": "S-06",
    "同义词轮换": "S-14",
    "身体反应模板": "S-15",
    "L5张力等级地板": "S-09b",
    "正文元信息隔离": "A-18",
    "武力Gate一致性": "F-07",
    "感官密度与多通道": "S-09d",
    "正文质感离线评分": "S-10q",
    "转折词密度": "S-17",
    "功能词密度": "S-18",
}

FIX_HINT_BY_ID = {
    "S-13": "放开功能性破折号——合理使用；仅收敛单段过密或3+连续堆叠",
    "S-12": "放开功能性省略号……合理使用；仅收敛单段叙事过密",
    "S-10s": "有停顿的长句只提示人工复读，不要求切碎；无标点长句补合适停顿；对话行豁免",
    "S-11": '核对连续句的主语、动作和节奏；修订不得省略必要因果。',
    "S-08": "恢复多句段间隔，拆开超长段；对话段、分隔符与保守识别的状态变化单句已豁免",
    "S-10": "按具体定位核对信息增量、视角和文风；语义候选允许保留，不按句式计数认定缺陷或要求全章重写",
    "S-16": "不要机械删副词。把副词+宽泛动词或程度词+抽象标签改为精确动词、可观察动作/感官/结果；每删一个修饰语都确认其信息功能已补回",
    "S-09": "场景补齐嗅/触/视；叙事核心动作段至少两类感官。纯对白/对白剥离后无叙事核心的不检",
    "S-06": '依据已启用的对白规则检查说话意图、称呼及上下文。',
    "S-14": "停止在短窗口内轮换多个同义词；稳定使用一个自然术语不惩罚",
    "S-15": "换身体反应写法，忌重复同一模板句",
    "S-09b": '依据本地规则声明的范围检查场景信号；未启用时不执行。',
    "A-18": "删除正文中的章节/写作工程词；标题行豁免，故事世界内真实讨论文本的例外交 L2 复核",
    "F-07": "按 FULL GATE 重写胜负：不得靠认输/放水/伤病/体力骤降/临时开挂，也不得把本场控制扩成长期支配",
    "S-09d": "提高触觉受力、状态/温度、微表情、听觉、嗅觉多通道感官词密度（目标≥10次/千字，覆盖≥2通道）",
    "S-10q": "词面指标仅供复核，不能评定文学质量；保留必要心理、因果和衔接，不为加分堆感官词",
    "S-17": "叙事段内转折词（然而/但是/不过/可是/却）每段≤1个、每500字≤2个；对话行豁免，用动作或语序自然转折替代",
    "S-18": "功能词密度：叙事每500字「这」家族≤3个、「是」系动词≤4个（复合词缀于是/可是/但是/还是/不是/而是/只是/要是/倒是/算是/老是/或是先剔除再计数）；对话行豁免，用动作/状态/语序改写判断句与指示词",
    "S-19": '核对形容词是否承载独立信息；保留有作用的细节，处理重复说明。',
}




DIALOGUE_START = re.compile(r'^[“"「\'『]')
COUNT_FRAG = re.compile(
    r"^[一二三四五六七八九十百零〇两\d]+(?:[，,][一二三四五六七八九十百零〇两\d]+)*[，,]?$"
)
# 场景分隔符：不参与段落节奏统计
SEPARATOR_PARA = re.compile(r"^(?:[-–—_*·。.]{3,}|={3,}|\*{3,}|~{3,})$")
# 对白引号跨度（用于剥离后只检叙事核心动作）
QUOTED_SPAN = re.compile(r"[“\"「『][^”\"」』]*[”\"」』]")
SPEECH_ATTRIBUTION_ONLY = re.compile(
    r"^(?:(?:他|她|[一-龥]{2,4})(?:低声|轻声|冷声|淡声|沉声)?)?"
    r"(?:说|问|答|道|喊|叫|骂|开口|反问|追问|回应)(?:了)?[。！？]?$"
)
LEGAL_SINGLE_STATE_CHANGE = re.compile(
    r"^(?:(?:他|她|[一-龥]{2,4})(?:终于|当场|忽然)?"
    r"(?:跪下|认输|承认|答应|停下|松手|放开|倒下|站住|退开|沉默|离开|回头)"
    r"(?:了|下来)?|"
    r"(?:他|她|[一-龥]{2,4})(?:的(?:手|手指|肩膀|眼睛|嘴唇|身体|动作|视线))?"
    r"(?:停在半空|停在门边|停住|僵在旁边|抬头|抬眼|闭上眼|睁开眼|动了动|颤了一下|碰到链端|抓住链端|落了下来))"
    r"[。！？]$"
)
PARAGRAPH_TRANSITION = re.compile(
    r"(?:随后|接着|片刻后|这时|与此同时|直到|转而|换成|"
    r"停下|松开|退开|站起|坐下|回头|话题|却|但|然而|[“「])"
)
CHAPTER_TITLE = re.compile(
    r"^第(?:\d+|[零〇一二三四五六七八九十百千万两]+|[XxＸ]+)章(?:\s+.*)?$"
)
PROJECT_META = re.compile(
    r"第(?:\d+|[零〇一二三四五六七八九十百千万两]+|[XxＸ]+)章|"
    r"上一章|前文|后文|伏笔|细纲|读者|加载链|"
    r"(?i:\b(?:gate|param|tag|module|l2|l5|pov|info|causal-proof|causal|a-(?:19|20|21|22|23))\b)"
)
MARTIAL_CONTEXT = re.compile(
    r"(?:交手|对决|比试|获胜|胜利|打赢|击败|落败|败给|认输|投降|放水|让权|"
    r"出拳|拳|踢|摔|拆手|按住|制服|锁住|压回|胜负)"
)
MARTIAL_TEXT_CONFLICTS = {
    "认输/放水/主动让权": re.compile(r"(?:认输|投降|举手认输|放弃反抗|停止反抗|主动让权|故意放水|收力让|任由.{0,12}(?:获胜|打赢))"),
    "伤病制造胜负": re.compile(r"(?:旧伤(?:复发|发作|牵动|未愈|限制|影响)|伤势(?:发作|加重|限制)|带伤上场|药物发作|神志不清|失去意识)"),
    "体力骤降": re.compile(r"(?:体力(?:突然|骤然)?(?:不支|耗尽|下降|崩溃)|突然脱力|骤然乏力|莫名乏力)"),
    "临时开挂": re.compile(r"(?:临时绝招|秘传绝招|临时觉醒|突然觉醒|本场才使出|从未展示的招式|一次性爆发)"),
}
TEXT_NEGATION_BEFORE = re.compile(
    r"(?:未|没有|并未|绝非|并非|不是|不曾|从未|排除|绝无|无|并不)"
    r"(?:(?:主动|故意|明确|真的|曾经|选择|当场|继续|再次|任何)){0,2}$"
)
TEXT_LONG_TERM_DOMINANCE = re.compile(
    r"(?:(?:永久|永远|无限期|长期).{0,12}(?:听从|服从|听命|支配|归属|权限|命令|关系)|"
    r"(?:从此|以后|每次).{0,14}(?:听从|服从|听命|受.{0,4}支配|归属|所有命令|权限.{0,4}生效)|"
    r"彻底归属|长期听命|每次都必须听从|持续关系支配)"
)


def _is_dialogue_para(para: str) -> bool:
    s = para.strip()
    if not s:
        return False
    narrative = re.sub(r"\s+", "", QUOTED_SPAN.sub("", s))
    return not narrative or bool(SPEECH_ATTRIBUTION_ONLY.fullmatch(narrative))


def _is_separator_para(para: str) -> bool:
    s = para.strip()
    if not s:
        return False
    if SEPARATOR_PARA.match(s):
        return True
    # 单行 markdown 规则线 / 全角破折号串
    if re.fullmatch(r"[-–—_*·。.\s]{3,}", s):
        return True
    return False


def _strip_quoted_spans(text: str) -> str:
    """去掉成对引号对白，留下叙事骨架。"""
    return QUOTED_SPAN.sub("", text)


def _narrative_core_blob(para: str) -> str:
    """段落中的叙事部分（剥离对白后）。用于 S-09 核心动作判定。"""
    return re.sub(r"\s+", "", _strip_quoted_spans(para))


def _is_legal_single_state_change(para: str) -> bool:
    """Conservative syntax-only exemption for a standalone result/acknowledgement beat."""
    narrative = re.sub(r"\s+", "", _strip_quoted_spans(para))
    return bool(LEGAL_SINGLE_STATE_CHANGE.fullmatch(narrative))


def _is_dialogue_sentence(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if QUOTED_SPAN.search(s):
        narrative = re.sub(r"\s+", "", _strip_quoted_spans(s))
        return not narrative or bool(SPEECH_ATTRIBUTION_ONLY.fullmatch(narrative))
    if DIALOGUE_START.match(s) or s.startswith(("「", "『")):
        return True
    if COUNT_FRAG.match(s):
        return True
    if re.fullmatch(r"[啊嗯唔呃操滚深慢射求你放]+[，,]?", s):
        return True
    return False


def _sentence_units(text: str) -> list[str]:
    """按句末标点/换行切出最小句单元。

    这是节奏门的保守分词：它不尝试解析复杂引号语法，只把明确
    对话单元交给 `_is_dialogue_sentence` 豁免。
    """
    return [s.strip() for s in re.split(r"[。！？\n]+", text) if s.strip()]


def _subject_key(sentence: str) -> str:
    """仅识别句首显式主语，不猜省略主语。"""
    s = sentence.strip()
    if not s or _is_dialogue_sentence(s):
        return ""
    s = s.lstrip('”」』"').strip()
    if not s:
        return ""
    pronoun = re.match(
        r"^(?:这时|此刻|随后|接着)?(他|她|它|角色甲|角色乙|攻|受)"
        r"(?=[的把将向对从在又却仍没不先再已终忽猛缓轻一-龥])",
        s,
    )
    if pronoun:
        return pronoun.group(1)
    # 非贪婪捕获：贪婪会把动词吞进主语键（「沈叙推开门」读成「沈叙推」——
    # 因为「开」也在动词表里），同名连续主语因此永远凑不成 run（机制 39 修复）。
    named = re.match(
        r"^([一-龥]{2,4}?)(?<![很太极更最挺蛮略稍不太])(?=(?:把|将|向|对|从|在|又|却|仍|没|不|先|再|已经|"
        r"终于|忽然|猛地|缓缓|抬|低|伸|收|转|走|坐|站|看|听|说|问|答|道|"
        r"笑|停|松|抓|按|摑|推|拉|退|跪|靠|贴|顶|撞|咬|舔|操|插|抽|射|"
        r"点|摇|放|拿|关|开))",
        s,
    )
    if not named:
        return ""
    candidate = named.group(1)
    # 句首候选本身是已知动词短语（转身走/低头看/把手放）时，它属于省略主语延续
    # 而不是人名——否则 _subject_key 与 _omitted_subject_sentence 会互相矛盾。
    head = _OMITTED_SUBJECT_HEAD.match(s)
    if head and head.end() <= len(candidate):
        return ""
    return candidate


# 省略主语延续：以动作动词或时间/语气副词开头的叙述句（“放下手机”“又往前走”），
# 在连续主语 run 中视为承接前一显式主语；它不能作为 run 的起点，但能延续 run。
_OMITTED_SUBJECT_HEAD = re.compile(
    r"^(?:又|却|仍|没|不|先|再|已经|终于|忽然|猛地|缓缓|还是|仍然|"
    r"抬|低|伸|收|转|走|坐|站|看|听|说|问|答|道|笑|停|松|抓|按|摑|"
    r"推|拉|退|跪|靠|贴|顶|撞|咬|舔|操|插|抽|射|点|摇|放|拿|关|开|"
    r"迈|踏|跨|蹲|爬|钻|躲|闪|扑|抱|揽|扶|背|扛|拖|拎|提|端|举|挥|"
    r"敲|拍|打|踢|踩|蹬|踹|跳|蹦|躺|趴|倚|捡|摸|握|捏|掐|勒|环|"
    r"把手|用[一手单双]?[手指脚]|把|将)(?=[一-龥\"])"
)


def _omitted_subject_sentence(sentence: str) -> bool:
    """保守识别可承接显式主语的动作句，不把否定存在句补成人物动作。"""
    s = sentence.strip()
    if not s or _is_dialogue_sentence(s):
        return False
    if s.startswith(("“", "「", "『")):
        return False
    if _subject_key(s):
        return False
    # “没有新通知”陈述存在性，不继承上一句的人物主语；“没有通知同事”
    # 等否定动作仍由原动作前缀处理。明确人物主语已在上面分支保留。
    if re.match(diagnostic_policy()['subjectSentence']['impersonalExistencePattern'], s):
        return False
    return bool(_OMITTED_SUBJECT_HEAD.match(s))


def _without_title_line(text: str) -> str:
    """只豁免可机械识别的标题，不把普通首句当标题。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or CHAPTER_TITLE.fullmatch(stripped):
            del lines[index]
        break
    return "\n".join(lines)


def _affirmative_text_match(text: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 6):match.start()]
        if TEXT_NEGATION_BEFORE.search(before):
            continue
        return match
    return None


def load_full_gate(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--gate 不是可读的 FULL GATE JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("pairRelations"), list):
        raise SystemExit("--gate 必须指向含 pairRelations 的 FULL GATE JSON")
    return value


def check_martial_gate_consistency(text: str, gate: dict) -> tuple[bool, str]:
    martial_pairs = [
        pair
        for pair in gate.get("pairRelations") or []
        if pair.get("dominanceSourceType") in {"武力", "复合"}
        and isinstance(pair.get("martialControl"), dict)
    ]
    if not martial_pairs:
        return (True, "FULL GATE 无武力/复合配对")

    units = _sentence_units(text)
    problems: list[str] = []

    for pair in martial_pairs:
        pair_names = {
            str(pair.get(field, ""))
            for field in ("topId", "bottomId")
            if str(pair.get(field, ""))
        }
        named_indexes = {
            index
            for index, unit in enumerate(units)
            if any(name in unit for name in pair_names)
        }
        context_indexes = {
            index
            for index in named_indexes
            if MARTIAL_CONTEXT.search(units[index])
            or TEXT_LONG_TERM_DOMINANCE.search(units[index])
            or _affirmative_text_match(units[index], MARTIAL_TEXT_CONFLICTS["临时开挂"])
        }
        relevant_indexes = set(context_indexes)
        for index in context_indexes:
            relevant_indexes.update(
                neighbor
                for neighbor in (index - 1, index + 1)
                if neighbor in named_indexes
            )
        if not relevant_indexes:
            continue
        pair_corpus = "\n".join(units[index] for index in sorted(relevant_indexes))
        pair_label = f"{pair.get('topId')}→{pair.get('bottomId')}"
        for label, pattern in MARTIAL_TEXT_CONFLICTS.items():
            actor = str(pair.get(
                "topId" if label == "临时开挂" else "bottomId",
                "",
            ))
            for index in sorted(relevant_indexes):
                unit = units[index]
                match = _affirmative_text_match(unit, pattern)
                if not match:
                    continue
                local_context = unit[
                    max(0, match.start() - 18):min(len(unit), match.end() + 18)
                ]
                if actor and actor in local_context:
                    problems.append(f"{pair_label} {label}: {match.group(0)}")
                    break

        long_term = _affirmative_text_match(pair_corpus, TEXT_LONG_TERM_DOMINANCE)
        if long_term and pair.get("dominanceScope") != "持续关系支配":
            problems.append(
                f"本场/限时控制被扩成长期支配: {long_term.group(0)}"
                f"（{pair_label}={pair.get('dominanceScope')}）"
            )
    if not problems:
        return (True, "未发现当前武力配对的胜负/支配冲突")
    return (not problems, "；".join(problems))


def check_project_meta(text: str) -> tuple[bool, str]:
    """A-18：标题外正文不得泄漏章节/写作工程词。"""
    body = _without_title_line(text)
    hits = [m.group(0) for m in PROJECT_META.finditer(body)]
    if not hits:
        return (True, "")
    unique = list(dict.fromkeys(hits))
    return (False, f"正文工程词 {len(hits)} 处：{'/'.join(unique[:6])}")


def check_dash(text: str) -> tuple[bool, str]:
    """放开功能性破折号 ——；仅阻断无意义连续堆叠（如 3+ 连破折号）或同段落过密（>2 处叙事破折号）。"""
    abusive_lines = 0
    samples: list[str] = []
    for line in text.splitlines():
        if "——" not in line:
            continue
        if re.search(r"——{2,}", line):
            abusive_lines += 1
            if len(samples) < 2:
                samples.append(f"连续破折号堆叠: {line.strip()[:60]}")
            continue
        outside = _strip_quoted_spans(line).count("——")
        if outside > 2:
            abusive_lines += 1
            if len(samples) < 2:
                samples.append(f"单段叙事破折号过密({outside}处): {line.strip()[:60]}")
    if abusive_lines:
        det = f"破折号使用不合规 {abusive_lines} 处"
        if samples:
            det += f"；例：{samples[0]}"
        return (False, det)
    return (True, "")


def check_ellipsis(text: str) -> tuple[bool, str]:
    """放开功能性省略号；仅阻断单段叙事过密（>2 处叙事省略号）。"""
    abusive_lines = 0
    samples: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        outside = len(re.findall(r"\.\.\.|…", _strip_quoted_spans(stripped)))
        if outside > 2:
            abusive_lines += 1
            if len(samples) < 2:
                samples.append(f"单段叙事省略号过密({outside}处): {stripped[:60]}")
    if abusive_lines:
        detail = f"叙事省略号过密 {abusive_lines} 行；需适度收敛"
        if samples:
            detail += f"；例：{samples[0]}"
        return (False, detail)
    return (True, "")


def _is_sticky_sensory(sentence: str) -> bool:
    policy = diagnostic_policy()['sceneDynamics']
    if not policy['enabled']:
        return False
    if sentence.count("，") + sentence.count(",") < policy['stickyMinCommas']:
        return False
    return bool(re.search(policy['stickyPattern'], sentence))


def sentence_length_violations(text: str, mode: str) -> list[dict]:
    """S-10s 逐句违规明细（唯一判定实现）。

    `check_sentence_length`（门禁布尔与摘要）与 `debug_validation.long_sentences`
    （行号级修稿明细）共用本函数，两边不可能再对同一句给出不同结论
    （机制 39 修复：此前 debug 在所有模式都给黏腻句 80 字上限，而门禁只在
    mixed/peakScene 放宽——plot/buildUp 下 45–80 字的黏腻句门禁报、debug 不报）。

    mode 在此统一归一化，调用方传原始 CLI 取值即可。
    """
    mode = (mode or "").lower().strip()
    policy = diagnostic_policy()['sentenceLength']
    hard = policy['byMode'][mode]
    sticky_cap = policy['stickyCap']
    violations: list[dict] = []
    for line_no, line in enumerate(text.split("\n"), 1):
        for sent in re.split(r"[。！？\n]", line):
            s = sent.strip()
            if not s or _is_dialogue_sentence(s):
                continue
            problems: list[str] = []
            unpunctuated = len(s) > policy['unpunctuatedCap'] and not any(c in s for c in "，,、：:；;．——…")
            if unpunctuated:
                problems.append(f"{policy['unpunctuatedCap']}字无标点（{len(s)}字）")
            sticky = mode in {"mixed", "peakscene"} and _is_sticky_sensory(s)
            cap = sticky_cap if sticky else hard
            if len(s) > cap:
                problems.append(f"{'黏腻感官句' if sticky else '单句'}超{cap}字（{len(s)}字）")
            if problems:
                violations.append({
                    "line": line_no,
                    "text": s,
                    "len": len(s),
                    "cap": cap if len(s) > cap else None,
                    "sticky": sticky,
                    "severity": 'fail' if unpunctuated else policy['lengthSeverity'],
                    "problems": problems,
                })
    return violations


def check_sentence_length(text: str, mode: str) -> tuple:
    violations = sentence_length_violations(text, mode)
    if not violations:
        return (True, "")
    failures = [row for row in violations if row['severity'] == 'fail']
    representative = (failures or violations)[0]
    det = f"句子长度检查 {len(violations)} 处（fail {len(failures)}，info {len(violations) - len(failures)}）；例：{representative['text'][:50]}"
    if not failures:
        return (True, det + '（长句复读提示，不要求拆句）', 'info')
    return (False, det)


def subject_sentence_violations(text: str) -> list[dict]:
    """S-11 逐条违规（唯一判定实现）。

    `check_subject_sentences`（门禁）与 `debug_validation.subject_sentences_issues`
    （行号级修稿明细）共用本函数（机制 39 修复：此前 debug 只统计以「他」开头的句子，
    具名主语、省略主语延续与 500 字窗三条子项全缺，明细与门禁结论分叉）。

    三个子项：
    - `window`（blocking）：500 字窗内双逗号叙事句不足 2；
    - `subject_run`（blocking）：连续 3 句同显式主语，或显式主语 + 省略主语延续；
    - `short_run`（report_only）：连续 5 句少于 10 字，只报告不阻断。
    """
    sentences = _sentence_units(text)
    window_violations: list[dict] = []
    reports: list[dict] = []
    subject_violations: list[dict] = []

    # 只检完整 500 字窗；不足 500 字不用比例类门。对话句不能充当
    # “双逗号叙事句”。
    window_source = QUOTED_SPAN.sub(lambda match: "□" * len(match.group(0)), text)
    compact = re.sub(r"\s+", "", window_source)
    full_windows = len(compact) // 500
    for win_index in range(full_windows):
        window = compact[win_index * 500 : (win_index + 1) * 500]
        rich = sum(
            1
            for sentence in _sentence_units(window)
            if not _is_dialogue_sentence(sentence)
            and sentence.count("，") + sentence.count(",") >= 2
        )
        if rich < 2:
            window_violations.append({
                "kind": "window",
                "blocking": True,
                "detail": f"第{win_index + 1}个500字窗只有{rich}句含至少2个逗号（至少2句）",
                "sentence": "",
            })

    # 连续 5 句 <10 字。
    short = 0
    for i, s in enumerate(sentences):
        if _is_dialogue_sentence(s):
            short = 0
            continue
        if len(s) < 10:
            short += 1
            if short >= 5:
                window = sentences[i - 4 : i + 1]
                if any(_is_dialogue_sentence(x) or len(x) >= 10 for x in window):
                    short = sum(
                        1
                        for x in window
                        if len(x) < 10 and not _is_dialogue_sentence(x)
                    )
                    if short < 5:
                        continue
                reports.append({
                    "kind": "short_run",
                    "blocking": False,
                    "detail": f"连续5句少于10字（位置约在字{sum(len(x) for x in sentences[: i + 1])}处）",
                    "sentence": "",
                })
                break
        else:
            short = 0

    # 连续 3 句以同一显式主语开头，或显式主语 + 连续省略主语延续达 3 句。
    # 删除/省略主语不算修复：省略主语延续 run 而不同时清门，否则“他V。V。V。”
    # 会让 Writer 靠删主语逃过 S-11。对话句仍重置 run（不参与连续主语统计）。
    last_subject = ""
    subject_run = 0
    run_has_omission = False
    for sentence in sentences:
        subject = _subject_key(sentence)
        omitted = _omitted_subject_sentence(sentence)
        if not subject and not omitted:
            last_subject = ""
            subject_run = 0
            run_has_omission = False
            continue
        if omitted:
            # 省略主语只能延续已建立的 run，不能作为 run 起点
            if not last_subject:
                continue
            subject_run += 1
            run_has_omission = True
        elif subject == last_subject:
            subject_run += 1
        else:
            last_subject = subject
            subject_run = 1
            run_has_omission = False
        if subject_run >= 3:
            subject_description = (f"连续3句承接「{last_subject}」为主语（含省略主语延续）"
                                   if run_has_omission else f"连续3句以「{last_subject}」为显式主语开头")
            subject_violations.append({
                "kind": "subject_run",
                "blocking": True,
                "detail": f"{subject_description}；例：{sentence[:50]}",
                "sentence": sentence,
            })
            break

    return window_violations + subject_violations + reports


def check_subject_sentences(text: str) -> tuple[bool, str]:
    """S-11 三子项：500字窗、连续短句、连续同主语。

    连续短句子项只报告不阻断：密集短句可以是文风与张力等级手段；
    500字窗与连续同主语照常判定。
    """
    violations = subject_sentence_violations(text)
    issues = [item["detail"] for item in violations if item["blocking"]]
    reports = [item["detail"] for item in violations if not item["blocking"]]
    detail = "；".join(issues + [f"[报告] {r}" for r in reports])
    return (len(issues) == 0, detail)


def paragraph_rhythm_analysis(text: str) -> dict:
    """S-08 共享判定（唯一实现）。

    `check_paragraph_rhythm`（门禁报告）与 `debug_validation.paragraph_rhythm_issues`
    （行号级明细）共用本函数（机制 39 修复：此前 debug 的连续单句段计数不为
    `functional`（合法结果/承认单句）重置 run，对门禁认为合规的段落报「连续单句段 3 个」）。

    返回结构化事实：段落条目（kind: dialogue/single/functional/multi）、连续单句段
    上限、长段问题、相邻单句段违规、8 段窗口违规与叙述单句段比例。
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    entries: list[dict] = []
    single_sentence_paras = 0
    functional_single = 0
    max_consecutive = 0
    current_run = 0
    long_paragraph_issues: list[str] = []

    for para_index, para in enumerate(paragraphs):
        # 分隔符不计入窗口。Markdown/章回标题也不是正文段。
        if _is_separator_para(para):
            continue
        if para_index == 0 and (para.startswith("#") or CHAPTER_TITLE.fullmatch(para)):
            continue
        # 对话段与「对话+动作节拍」段（STYLE-AND-VOICE 鼓励的承接形式）都不是
        # 叙述单句段：引号外跟着动作/体感描写的段落在节奏上属于对白交换，不打击。
        if _is_dialogue_para(para) or QUOTED_SPAN.search(para):
            current_run = 0
            entries.append({"kind": "dialogue", "para": para, "sent_count": 0})
            continue
        narrative = _strip_quoted_spans(para).strip()
        sent_count = len(_sentence_units(narrative))
        if sent_count <= 1 and narrative:
            if _is_legal_single_state_change(para):
                functional_single += 1
                current_run = 0
                entries.append({"kind": "functional", "para": para, "sent_count": sent_count})
                continue
            single_sentence_paras += 1
            current_run += 1
            max_consecutive = max(max_consecutive, current_run)
            entries.append({"kind": "single", "para": para, "sent_count": sent_count})
        else:
            current_run = 0
            entries.append({"kind": "multi", "para": para, "sent_count": sent_count})

        if sent_count >= 12:
            long_paragraph_issues.append(f"单段连续{sent_count}句未换段（上限11句）")
        elif sent_count > 10 and not PARAGRAPH_TRANSITION.search(narrative):
            long_paragraph_issues.append(f"单段{sent_count}句且无可见动作/话题/节奏转换")

    # 单句段之间必须真正出现多句叙述段。独占对话和合法状态
    # 单句不算违规单句，但也不得冒充多句段来清空间隔要求。
    adjacent_violations: list[dict] = []
    previous_single = False
    for entry in entries:
        if entry["kind"] == "single":
            if previous_single:
                adjacent_violations.append(entry)
            previous_single = True
        elif entry["kind"] == "multi":
            previous_single = False

    # 任意 8 个可见段落中，叙述单句段不得超过 2 个。对话段保留
    # 窗口位置，但不算作单句段。
    window_violation = None
    for start in range(0, max(0, len(entries) - 7)):
        window = entries[start : start + 8]
        singles = sum(1 for entry in window if entry["kind"] == "single")
        if singles > 2:
            window_violation = {"singles": singles, "window": window}
            break

    narrative_entries = [entry for entry in entries if entry["kind"] != "dialogue"]
    ratio = single_sentence_paras / len(narrative_entries) if narrative_entries else 0.0
    return {
        "entries": entries,
        "singleCount": single_sentence_paras,
        "functionalCount": functional_single,
        "maxConsecutive": max_consecutive,
        "longParagraphIssues": long_paragraph_issues,
        "adjacentViolations": adjacent_violations,
        "windowViolation": window_violation,
        "ratio": ratio,
    }


def check_paragraph_rhythm(text: str) -> tuple[bool, str, str]:
    analysis = paragraph_rhythm_analysis(text)
    issues: list[str] = list(analysis["longParagraphIssues"])
    if analysis["maxConsecutive"] >= 3:
        issues.append(f"连续单句段 {analysis['maxConsecutive']} 个（上限2，对话段/分隔符已豁免）")
    if analysis["windowViolation"]:
        issues.append(f"任意8段窗口内叙述单句段{analysis['windowViolation']['singles']}个（上限2）")
    if analysis["adjacentViolations"]:
        issues.append("相邻叙述单句段之间缺少多句段")
    ratio_limit = diagnostic_policy()['paragraphRatio']['max']
    if analysis["ratio"] > ratio_limit:
        issues.append(f"叙述单句段比例 {analysis['ratio']:.1%}（参考线{ratio_limit:.0%}，只报告不阻断，对话段/分隔符已豁免）")

    detail = "；".join(issues)
    if not issues and analysis["functionalCount"]:
        detail = f"合法结果/承认单句段 {analysis['functionalCount']} 个"
    if issues:
        # S-08 全类报告化：密集短句/单句段节奏可以是文风与张力等级手段，
        # 只报告不阻断；长段未换段等一并保留在报告文本中可见。
        return (True, detail, "info")
    return (True, detail, "pass")


def scene_action_pattern():
    return re.compile(diagnostic_policy()['sceneDynamics']['coreActionPattern'])


def check_sensory_channels(text: str) -> tuple:
    policy = diagnostic_policy()['sceneDynamics']
    if not policy['enabled']:
        return True, '未启用场景感官规则', 'not_applicable'
    exclude_smell = re.compile(r"(?:不闻|不问|新闻|香(?:港|烟)|臭名|臭美)")
    exclude_touch = re.compile(
        r"(?:硬(?:着头皮|是|要|朗)|软(?:件|弱|化|禁)|滑(?:稽|头)|疼(?:惜|爱)|冷(?:漠|峻))"
    )
    exclude_sight = re.compile(
        r"(?:看(?:来|似|守|法)|见(?:识|解|证|面)|光(?:明|荣|辉|泽)|红(?:人|军|利|旗|楼))"
    )

    smell_kw = load_text_patterns()['smellKeywords']
    touch_kw = [
        "热", "烫", "冷", "冰", "硬", "软", "湿", "黏", "滑", "疼",
        "酸", "胀", "麻", "酥", "触", "贴", "顶", "压", "掐", "勒",
    ]
    sight_kw = [
        "看", "见", "光", "影", "暗", "黑", "红", "白", "青", "紫",
        "肿", "涨", "鼓", "隆", "轮廓", "形状",
    ]

    def _channel_has(blob: str, keywords: list[str], exclude: re.Pattern) -> bool:
        cleaned = exclude.sub("", blob)
        return any(k in cleaned for k in keywords)

    has_smell = _channel_has(text, smell_kw, exclude_smell)
    has_touch = _channel_has(text, touch_kw, exclude_touch)
    has_sight = _channel_has(text, sight_kw, exclude_sight)

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    total = len(paragraphs)
    if total == 0:
        return (False, "输入无有效段落")

    full_coverage = 0
    miss_counts = {"嗅觉": 0, "触觉": 0, "视觉": 0}
    core_action = scene_action_pattern()
    core_total = 0
    core_under = []
    core_entries = []
    core_exempt = 0

    for para_index, para in enumerate(paragraphs):
        if _is_separator_para(para):
            continue
        s = _channel_has(para, smell_kw, exclude_smell)
        t = _channel_has(para, touch_kw, exclude_touch)
        v = _channel_has(para, sight_kw, exclude_sight)
        if s and t and v:
            full_coverage += 1
        if not s:
            miss_counts["嗅觉"] += 1
        if not t:
            miss_counts["触觉"] += 1
        if not v:
            miss_counts["视觉"] += 1
        # 只对「叙事核心动作」要求两通道：剥离对白后仍有 ≥25 字叙事且含动作词
        narr = _narrative_core_blob(para)
        if len(narr) < policy['minNarrativeLength'] or not core_action.search(narr):
            if core_action.search(para) and len(para) >= policy['minNarrativeLength']:
                core_exempt += 1
            continue
        core_total += 1
        core_entries.append((para_index, len(para), (s, t, v)))

    for entry_index, (para_index, chars, channels) in enumerate(core_entries):
        merged = list(channels)
        for neighbor_index in (entry_index - 1, entry_index + 1):
            if 0 <= neighbor_index < len(core_entries):
                other_para_index, _, other_channels = core_entries[neighbor_index]
                if abs(other_para_index - para_index) == 1:
                    merged = [a or b for a, b in zip(merged, other_channels)]
        count = sum(int(x) for x in merged)
        if count < 2:
            core_under.append((chars, count))

    rate_str = f"逐段三通道齐全率 {full_coverage}/{total} ({full_coverage / total:.0%})"
    miss_str = "，".join(f"{ch}缺{cn}段" for ch, cn in miss_counts.items() if cn > 0)
    exempt_str = f"；对白核心豁免{core_exempt}段" if core_exempt else ""

    if not core_entries:
        return True, '未识别到明确的性动作词组，核心动作段 0；词面识别不证明是否存在关键场景', 'info'
    if not has_smell or not has_touch or not has_sight:
        missing = []
        if not has_smell:
            missing.append("嗅觉")
        if not has_touch:
            missing.append("触觉")
        if not has_sight:
            missing.append("视觉")
        return (False, f"全文缺失感官通道：{'、'.join(missing)} | {rate_str}")

    if core_under:
        sample = "，".join(f"{chars}字/{channels}通道" for chars, channels in core_under[:3])
        return (
            False,
            f"叙事核心动作单元感官不足 {len(core_under)}/{core_total}（单段或相邻双段至少2通道）：{sample}{exempt_str}",
        )

    return (
        True,
        f"叙事核心动作段两通道 {core_total}/{core_total} | {rate_str}"
        + (f" | {miss_str}" if miss_str else "")
        + exempt_str,
    )


def ai_pattern_violations(text: str, *, mode: str = "plot") -> list[dict]:
    rows = []
    for item in scan_ai_patterns(text, mode=mode):
        rows.append({**item, 'rule': 'S-10', 'pattern': item['name'],
                     'text': item['matched'], 'blocking': item['gate']})
    return rows


def check_ai_patterns(text: str, *, mode: str = "plot") -> tuple:
    rows = ai_pattern_violations(text, mode=mode)
    if not rows:
        return True, ''
    blocking = [row for row in rows if row['blocking']]
    reported = blocking or rows
    counts = Counter(row['pattern'] for row in reported)
    detail = '；'.join(f'{name}（{count}处）' for name, count in counts.items()) + '；例：' + reported[0]['text']
    if blocking:
        return False, '形式约束命中：' + detail
    return True, '风格候选待语义复核：' + detail, 'info'


def check_adjective_stack(text: str) -> tuple[bool, str]:
    """S-19：叙事层形容词堆叠（连续"的"字定语链 / 顿号形容词链）。

    只查叙事，剥离对话行（对白里的堆叠可能是声纹）。替代方向：
    味道（嗅觉/味觉来源）与微动作替代形容词，见 STYLE-AND-VOICE
    「味道与微动作替代形容词」。形式证据，语义由 Editor 复核。
    """
    config = load_text_patterns().get("adjective_stack")
    if not config:
        return (True, "")
    narrative = _without_title_line(text)
    narrative = "\n".join(_strip_quoted_spans(line) for line in narrative.splitlines())
    found: list[str] = []
    samples: list[str] = []
    for key in ("defChain", "listChain"):
        item = config[key]
        matches = list(re.finditer(item["pattern"], narrative))
        if matches:
            found.append(f"{item['name']}（{len(matches)}处）")
            if len(samples) < 3:
                samples.extend(m.group(0)[:40] for m in matches[:3])
    if not found:
        return (True, "")
    detail = (
        "形容词堆叠 S-19："
        + "；".join(found)
        + "；修复方向：增补而非删除——在形容词链中加味道（气味/味觉来源）或微动作（指节收紧/喉结滚/汗滴落）让形容词有身体落点，形容词本身可保留"
    )
    if samples:
        detail += "；例：" + " / ".join(samples)
    return (False, detail)


def check_adverb_patches(text: str) -> tuple[bool, str]:
    """S-16：只拦低信息副词补丁，不做中文副词的一刀切词性判断。"""
    config = load_text_patterns()["adverb_patch"]
    narrative = _without_title_line(text)
    narrative = "\n".join(_strip_quoted_spans(line) for line in narrative.splitlines())
    found: list[str] = []
    samples: list[str] = []
    seen_spans: set[tuple[int, int]] = set()
    hit_count = 0

    for item in config["regex"]:
        matches = []
        for match in re.finditer(item["pattern"], narrative):
            span = match.span()
            if any(span[0] < old_end and span[1] > old_start for old_start, old_end in seen_spans):
                continue
            seen_spans.add(span)
            matches.append(match)
            hit_count += 1
            if len(samples) < 3:
                samples.append(match.group(0))
        if matches:
            found.append(f"{item['name']}（{len(matches)}处）")

    if not found:
        return (True, "")
    detail = f"低信息副词补丁 {hit_count} 处：" + "；".join(found)
    if samples:
        detail += "；例：" + " / ".join(samples)
    return (False, detail)


def check_transition_density(text: str) -> tuple[bool, str]:
    """S-17：叙事转折词密度。每段 ≤1 个、全文每 500 字 ≤2 个。对话行豁免。"""
    config = load_text_patterns().get("transition")
    if not config:
        return (True, "")
    narrative = _without_title_line(text)
    paragraphs = [p.strip() for p in narrative.split("\n\n") if p.strip()]
    per_para_limit = config.get("per_paragraph_limit", 1)
    per_500_limit = config.get("per_500_chars_limit", 2)

    patterns = [re.compile(item["pattern"]) for item in config["regex"]]
    para_violations: list[str] = []
    total_hits = 0
    total_narrative_chars = 0
    samples: list[str] = []

    for para_index, para in enumerate(paragraphs):
        if _is_separator_para(para):
            continue
        if para_index == 0 and (para.startswith("#") or CHAPTER_TITLE.fullmatch(para)):
            continue
        # 剥离对话，只检查叙事
        narrative_text = _strip_quoted_spans(para).strip()
        if not narrative_text:
            continue
        total_narrative_chars += len(narrative_text)
        para_hits = 0
        for pat in patterns:
            matches = list(pat.finditer(narrative_text))
            para_hits += len(matches)
            total_hits += len(matches)
            for m in matches:
                if len(samples) < 5:
                    # 取匹配词及前后各5字作为上下文
                    start = max(0, m.start() - 5)
                    end = min(len(narrative_text), m.end() + 5)
                    samples.append(narrative_text[start:end])
        if para_hits > per_para_limit:
            para_violations.append(
                f"段{para_index + 1}含{para_hits}个转折词（限{per_para_limit}）"
            )

    # 全文密度门
    num_windows = max(1, (total_narrative_chars + 499) // 500)
    allowed_total = num_windows * per_500_limit
    issues: list[str] = []
    if para_violations:
        issues.append("段内超限：" + "；".join(para_violations[:3]))
    if total_hits > allowed_total:
        issues.append(
            f"全文密度超限：{total_hits}处转折词"
            f"（{total_narrative_chars}字/{num_windows}窗口，限{allowed_total}）"
        )
    if not issues:
        return (True, "")
    detail = "转折词密度 S-17：" + "。".join(issues)
    if samples:
        detail += "；例：" + " / ".join(samples[:3])
    return (False, detail)


def check_word_density(text: str) -> tuple[bool, str]:
    """S-18：功能词密度。叙事每 500 字「这」≤3、「是」系动词≤4。对话行豁免。

    阈值标定（2026-08，真人网文抽样 2285 部中位）：这≈7.4/千字、是≈8.7/千字
    （剔除词缀后）。AI 生成叙事判断句与指示词泛滥，压到低于人类中位。
    """
    config = load_text_patterns().get("word_density")
    if not config:
        return (True, "")
    narrative = _without_title_line(text)
    paragraphs = [p.strip() for p in narrative.split("\n\n") if p.strip()]
    issues: list[str] = []
    hint = ""

    for family in config["families"]:
        limit = family["per_500_chars_limit"]
        strip = family.get("strip_affixes") or []
        strip_re = re.compile("|".join(strip)) if strip else None
        pat = re.compile(family["pattern"])
        chars = 0
        hits = 0
        for para in paragraphs:
            if _is_separator_para(para):
                continue
            narr = _strip_quoted_spans(para).strip()
            if not narr:
                continue
            if strip_re:
                narr = strip_re.sub("", narr)
            chars += len(narr)
            hits += len(pat.findall(narr))
        windows = max(1, (chars + 499) // 500)
        allowed = windows * limit
        if hits > allowed:
            issues.append(
                f"{family['name']}{hits}个（{chars}字/{windows}窗口，限{allowed}，约{hits / max(chars / 1000, 0.001):.1f}/千字）"
            )
            hint = family.get("rewrite_hint", "")
    if not issues:
        return (True, "")
    detail = "功能词密度 S-18：" + "；".join(issues)
    if hint:
        detail += f"；{hint}"
    return (False, detail)


def check_dialogue_dynamics_density(
    text: str,
    intensity_level: int = 5,
    *,
    strict: bool = True,
) -> tuple[bool, str, str]:
    # intensityLevel 1–2：不套 L3+ 密度地板（C-CORE S-06）
    policy = diagnostic_policy()['dialogueDynamics']
    if not policy['enabled']:
        return True, '未启用对白密度规则', 'not_applicable'
    if intensity_level < policy['minLevel']:
        return (
            True,
            f"冲突对白密度：intensityLevel={intensity_level}，跳过密度地板",
            "info",
        )
    total_chars = len(text)
    dialogue_count = len(re.findall(load_text_patterns()["dialogue_dynamics"]["pattern"], text))
    window = policy['windowSize']
    num_windows = max(1, (total_chars + window - 1) // window)
    avg_density = dialogue_count / num_windows
    required = next((row['required'] for row in policy['shortTiers'] if total_chars < row['belowTextSize']),
                    num_windows * policy['perLevel'].get(str(intensity_level), policy['defaultPerWindow']))
    if dialogue_count < required:
        if not strict:
            return (
                True,
                f"冲突对白密度为建议项：平均每{window}字{avg_density:.1f}处（全文{dialogue_count}处，建议至少{required}处）",
                "info",
            )
        return (
            False,
            f"冲突对白密度不足：平均每{window}字{avg_density:.1f}处（全文{dialogue_count}处，按篇幅至少{required}处）",
            "fail",
        )
    return (
        True,
        f"冲突对白密度：平均每{window}字{avg_density:.1f}处（全文{dialogue_count}处）",
        "pass",
    )


def check_synonym_churn(text: str) -> tuple[bool, str]:
    synonym_groups = load_text_patterns()['synonymGroups']
    # 稳定使用同一个自然词不惩罚。只拦截短窗口内同一概念的
    # 明显换词：≥3个词形且总出现≥4次。
    problems = []
    window_size = 300
    starts = list(range(0, max(1, len(text)), 150))
    for start in starts:
        window = text[start : start + window_size]
        if not window:
            continue
        for group_name, synonyms in synonym_groups.items():
            # 长词优先计数，避免同一概念重复计数
            counts = {}
            scratch = window
            for syn in sorted(synonyms, key=len, reverse=True):
                count = scratch.count(syn)
                if count > 0:
                    counts[syn] = count
                    scratch = scratch.replace(syn, "□" * len(syn))
            total = sum(counts.values())
            if len(counts) >= 3 and total >= 4:
                char_pos = min(start + window_size // 2, len(text))
                detail = "/".join(f"{word}×{count}" for word, count in counts.items())
                problems.append(
                    f"{group_name}组300字窗轮换{len(counts)}个词形（{detail}，位置约在字{char_pos}处）"
                )
                break
        if problems:
            break
    return (len(problems) == 0, "；".join(problems) if problems else "")


def check_body_reaction_repetition(text: str) -> tuple[bool, str]:
    reaction_patterns = [
        "从喉咙里",
        "从嘴里",
        "挤出来",
        "漏出来",
        "颤抖着",
        "不由自主",
        "忍不住",
    ]
    problems = []
    for pattern in reaction_patterns:
        count = text.count(pattern)
        if count > 3:
            problems.append(f"「{pattern}」出现{count}次（上限3次）")
    return (len(problems) == 0, "；".join(problems) if problems else "")


def _count_contrast_windows(
    text: str,
    window_size: int,
    elevation: tuple[str, ...],
    degradation: tuple[str, ...],
) -> int:
    """S-09c: count windows where an elevation signal and a degradation signal
    co-occur.  Elevation is the loose pool (identity/ability/esteem/body terms),
    degradation the second signal pool; one without the other does not count.
    Adjacent hit windows merge into one contrast (same shot), so the count is a
    floor, not an exact tally."""
    if not elevation or not degradation:
        return 0
    elev_re = re.compile("|".join(map(re.escape, elevation)))
    degr_re = re.compile("|".join(map(re.escape, degradation)))
    count = 0
    last_end = -1
    step = max(1, window_size // 2)
    for start in range(0, len(text), step):
        window = text[start : start + window_size]
        if not (elev_re.search(window) and degr_re.search(window)):
            continue
        if start < last_end:
            continue
        count += 1
        last_end = start + window_size
    return count


def check_l5_floor(text: str, intensity_level: int = 5) -> tuple[bool, str, str]:
    # 词池与各档阈值来自 shared/registries/intensity.json（degradationFloor），
    # 与 ai_cliche_linter 的上限表同源，避免两侧词表漂移。
    if not intensity_registry.degradation_floor()['enabled']:
        return True, '未启用场景词面规则', 'not_applicable'
    if intensity_level < intensity_registry.degradation_floor()['minLevel']:
        return True, '声明低于词面地板适用档位，不检查', 'info'
    policy = diagnostic_policy()['sceneDynamics']
    l5_words = list(intensity_registry.degradation_floor()["words"])
    hit = [w for w in l5_words if w in text]
    smell_indep = len(
        re.findall(
            load_text_patterns()['sceneSmellPattern'],
            text,
        )
    )
    sensory_stay = 0
    sticky = 0
    for sent in re.split(r"[。！？\n]", text):
        s = sent.strip()
        if len(s) >= policy['sensoryStayMinLength'] and re.search(policy['sensoryStayPattern'], s):
            sensory_stay += 1
        if _is_sticky_sensory(s) and len(s) >= policy['stickyMinLength']:
            sticky += 1
    # 二次污染链：状态 + 抹刮糊蹭涂干塞堵（关键场景过程；禁壳/碾假微距凑数）
    pollution = len(
        re.findall(
            load_text_patterns()['sceneSequencePattern'],
            text,
        )
    )
    # 声响锚：引号对话/声音行（粗计）
    voice_lines = len(re.findall(r"[「\"“][^」\"”]{1,40}[」\"”]", text))
    text_size = len(re.sub(r"\s+", "", text))
    required = intensity_registry.floor_thresholds(text_size, intensity_level)
    need_words = required["words"]
    need_smell = required["smell"]
    need_stay = required["sensoryStay"]
    need_sticky = required["sticky"]
    need_pollution = required["pollutionChain"]
    need_voice = required["voiceLines"]
    contrast_cfg = intensity_registry.contrast_config()
    contrast = _count_contrast_windows(
        text,
        contrast_cfg["windowSize"],
        contrast_cfg["elevationSignals"],
        contrast_cfg["degradationSignals"],
    )
    need_contrast = required["contrast"] if intensity_level >= 5 else 0
    detail = (
        f"intensity={intensity_level}；正文规模≈{text_size}字；降格词{len(hit)}/{need_words}{hit[:5]}；"
        f"独立嗅觉≈{smell_indep}/{need_smell}；感官停留≈{sensory_stay}/{need_stay}；"
        f"黏腻句≈{sticky}/{need_sticky}；生格/降格对比≈{contrast}/{need_contrast}"
    )
    if intensity_level >= 6:
        detail += f"；二次污染链≈{pollution}/{need_pollution}；声响行≈{voice_lines}/{need_voice}"
    ok = (
        len(hit) >= need_words
        and smell_indep >= need_smell
        and sensory_stay >= need_stay
        and sticky >= need_sticky
        and pollution >= need_pollution
        and voice_lines >= need_voice
        and contrast >= need_contrast
    )
    return (ok, detail, "info")


# Historical lexical score, reported as an uncalibrated indicator by default.
PROSE_CLICHE_PATTERNS = {
    "抽象心理直说": (
        re.compile(
            r"(?:他|攻|受|[一-龥]{2,4})(?:感到|意识到|明白了|内心|心中)(?:无比|一阵|一种|深深|十分)?"
        ),
        5,
    ),
    "模板过渡词": (
        re.compile(r"(?:与此同时|就在这时|恰在此时|正当此刻|不知何时|紧接着)"),
        4,
    ),
    "上帝/解释腔": (
        re.compile(r"(?:这意味着|之所以[^。\n]{0,30}是因为|他不知道的是|殊不知)"),
        4,
    ),
    "库存神态与套话": (
        re.compile(
            r"(?:嘴角(?:勾起|扬起|扯出)[^。！？\n]{0,8}(?:弧度|笑意)?|唇角(?:勾起|扬起)|喉结(?:滚动|滚了?一下)|修长的手指|每一帧|像慢镜头|"
            r"倒抽(?:了)?一口?凉气|倒吸(?:了)?一口?凉气|(?:后背|背后)?(?:瞬间|一下|唰地)?(?:浸出|渗出|冒出|洇出)(?:一层)?冷汗|冷汗(?:涔涔|直冒))"
        ),
        3,
    ),
    "升华/伪深刻结尾": (
        re.compile(
            r"(?:这一刻[，,][^。！？\n]{0,30}(?:终于|才)|这不是[^。！？\n]{0,20}而是|看似[^。！？\n]{0,20}实则)"
        ),
        6,
    ),
    "低可见度/空洞修饰词": (
        re.compile(
            r"(?:某种程度上|难以言喻|无法形容|毫无疑问|不可否认|总而言之|简单来说|说白了)"
        ),
        3,
    ),
}


def analyze_sensory_density(text: str) -> dict:
    cn_chars = chinese_char_count(text)
    total_len = cn_chars if cn_chars > 0 else max(1, len(text))
    channel_counts: dict[str, int] = {}
    total_hits = 0
    covered_channels: list[str] = []

    for name, pattern in diagnostic_policy()['sensoryDensity']['channels'].items():
        matches = re.findall(pattern, text)
        count = len(matches)
        channel_counts[name] = count
        total_hits += count
        if count > 0:
            covered_channels.append(name)

    density_per_1k = round((total_hits / total_len) * 1000, 1)
    covered_count = len(covered_channels)

    return {
        "total_hits": total_hits,
        "sensory_density_per_1k": density_per_1k,
        "covered_count": covered_count,
        "covered_channels": covered_channels,
        "channel_counts": channel_counts,
    }


def analyze_prose_quality(text: str, sensory_info: dict) -> dict:
    base_score = 100
    deductions = 0
    details: list[dict] = []

    for label, (pattern, penalty_per_hit) in PROSE_CLICHE_PATTERNS.items():
        hits = len(pattern.findall(text))
        if hits > 0:
            loss = hits * penalty_per_hit
            deductions += loss
            details.append(
                {
                    "category": label,
                    "hits": hits,
                    "penalty_per_hit": penalty_per_hit,
                    "total_penalty": loss,
                }
            )

    bonuses = 0
    bonus_details: list[str] = []
    cov_bonus = sensory_info["covered_count"] * 2
    if cov_bonus > 0:
        bonuses += cov_bonus
        bonus_details.append(
            f"感官多通道覆盖[{sensory_info['covered_count']}/5] +{cov_bonus}分"
        )

    if sensory_info["sensory_density_per_1k"] >= 15.0:
        bonuses += 5
        bonus_details.append(
            f"高感官词密度({sensory_info['sensory_density_per_1k']}/1k字) +5分"
        )

    final_score = max(0, min(100, base_score - deductions + bonuses))

    if final_score >= 90:
        grade = "S"
    elif final_score >= 80:
        grade = "A"
    elif final_score >= 70:
        grade = "B"
    elif final_score >= 60:
        grade = "C"
    else:
        grade = "FAIL"

    return {
        "score": final_score,
        "grade": grade,
        "deductions": deductions,
        "bonuses": bonuses,
        "details": details,
        "bonus_details": bonus_details,
    }


def check_sensory_density(text: str) -> tuple:
    info = analyze_sensory_density(text)
    dens = info["sensory_density_per_1k"]
    cov = info["covered_count"]
    policy = diagnostic_policy()['sensoryDensity']
    ok = (dens >= policy['minPerKHan'] and cov >= policy['minChannels']) or (len(text.strip()) < policy['minTextLength'])
    detail = f"{dens}次/1000字 · 覆盖通道[{cov}/5]: {', '.join(info['covered_channels'])}"
    if not ok and policy['severity'] == 'info':
        return True, detail + '（词表覆盖提示，不证明感官描写不足，不要求凑词）', 'info'
    return ok, detail


def check_prose_quality(text: str) -> tuple:
    s_info = analyze_sensory_density(text)
    q_info = analyze_prose_quality(text, s_info)
    policy = diagnostic_policy()['proseQuality']
    ok = q_info["score"] >= policy['minScore']
    detail = f"启发式得分 {q_info['score']}/100 (等级:{q_info['grade']}) · 扣分:-{q_info['deductions']} · 加分:+{q_info['bonuses']}；不是文学质量评分"
    if policy['severity'] == 'info':
        return True, detail, 'info'
    return ok, detail


def _norm(result, name):
    if len(result) == 3:
        ok, detail, level = result
    else:
        ok, detail = result
        level = "pass" if ok else "fail"
    rid = RULE_ID_BY_NAME.get(name, "")
    item = {
        "name": name,
        "rule_id": rid,
        "pass": ok,
        "level": level,
        "detail": detail,
        "evidence": _extract_evidence(detail),
    }
    if level == 'not_applicable':
        item['evaluated'] = False
        item['status'] = 'not_applicable'
    if not ok and rid:
        item["fix_hint"] = FIX_HINT_BY_ID.get(rid, "见 shared/contracts/ 对应规则")
    return item


def _extract_evidence(detail: str) -> str:
    """从 check detail 提取可定位证据摘录（“例：”后文本），供外科返修直接定位。"""
    if not detail:
        return ""
    match = re.search(r"例[:：]\s*([^；。]{0,60})", detail)
    if match:
        return match.group(1).strip()
    return ""


def build_checks(
    mode: str,
    strict_l5: bool,
    intensity_level: int = 5,
    strict_dialogue_floor: bool = False,
):
    mode = mode.lower().strip()
    if mode not in {"plot", "buildup", "mixed", "peakscene"}:
        raise SystemExit(f"未知 mode: {mode}（plot|buildUp|mixed|peakScene）")

    def sentence_len(t: str):
        return check_sentence_length(t, mode)

    def dialogue_density(t: str):
        return check_dialogue_dynamics_density(t, intensity_level, strict=strict_dialogue_floor)

    def ai_patterns(t: str):
        return check_ai_patterns(t, mode=mode)

    checks = [
        ("破折号使用", check_dash),
        ("省略号使用", check_ellipsis),
        ("句子长度", sentence_len),
        ("句式节奏", check_subject_sentences),
        ("段落节奏", check_paragraph_rhythm),
        ("AI句式套路", ai_patterns),
        ("转折词密度", check_transition_density),
        ("功能词密度", check_word_density),
        ("低信息副词补丁", check_adverb_patches),
        ("形容词堆叠", check_adjective_stack),
        ("正文元信息隔离", check_project_meta),
        ("感官密度与多通道", check_sensory_density),
        ("正文质感离线评分", check_prose_quality),
    ]

    def l5_floor(t: str):
        return check_l5_floor(t, intensity_level)

    def scene_check(check):
        def evaluate(text):
            if not diagnostic_policy()['sceneDynamics']['enabled']:
                return True, '未启用场景规则', 'not_applicable'
            return check(text)
        return evaluate

    if mode in {"mixed", "peakscene"} and intensity_level >= diagnostic_policy()['sceneDynamics']['minIntensityLevel']:
        checks.extend(
            [
                ("感官通道", check_sensory_channels),
                ("同义词轮换", scene_check(check_synonym_churn)),
                ("身体反应模板", scene_check(check_body_reaction_repetition)),
            ]
        )
    # Level declarations must stay visible even in a build-up/plot chapter.
    if intensity_level >= diagnostic_policy()['dialogueDynamics']['minLevel']:
        checks.append(("冲突对白密度", dialogue_density))
    if intensity_level >= intensity_registry.degradation_floor()['minLevel']:
        checks.append(("L5张力等级地板", l5_floor))
    return mode, checks, strict_l5


ADULT_CHECK_NAMES = {
    "感官通道",
    "冲突对白密度",
    "同义词轮换",
    "身体反应模板",
    "L5张力等级地板",
}


def select_paragraphs(text: str, spec: str) -> tuple[str, list[int], int]:
    """Compatibility wrapper around the shared paragraph selector."""
    return _select_paragraphs(text, spec)


def main():
    parser = argparse.ArgumentParser(description="structured-writing 机械项快速回检")
    parser.add_argument("file", nargs="?", help="输入文本文件，省略则从 stdin 读取")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument(
        "--mode",
        default="peakScene",
        help="plot|buildUp|mixed|peakScene（默认 peakScene）",
    )
    parser.add_argument(
        "--keep-meta",
        action="store_true",
        help="保留 Gate/盲审/L2 等元信息行（默认剥离；保留后 A-18 可阻断）",
    )
    parser.add_argument(
        "--strict-floor", action="store_true", help="显式把 S-06 与 S-09b 词面地板作为阻断项；各入口默认关闭",
    )
    parser.add_argument(
        "--strict-l5",
        action="store_true",
        help="显式强制地板 FAIL（兼容旧调用；新调用用 --strict-floor）",
    )
    parser.add_argument(
        "--no-strict-l5",
        action="store_true",
        help="兼容参数；地板默认仅 INFO",
    )
    parser.add_argument(
        "--no-dialogue-floor",
        action="store_true",
        help="将 S-06 冲突对白密度降为建议项；正文重写流程默认使用，避免词数配额污染整章",
    )
    intensity_min, intensity_max = intensity_registry.level_bounds()
    parser.add_argument(
        "--intensity-level",
        type=int,
        default=intensity_registry.default_level(),
        help=(
            f"张力等级 {intensity_min}-{intensity_max}（默认取 shared/registries/intensity.json 的 defaultLevel；"
            "低档不加载地板；达到适用档位后仅显式 strict 才阻断）"
        ),
    )
    parser.add_argument(
        "--target",
        type=int,
        default=0,
        help="目标汉字数（写入 JSON word_count；不达标不阻断，仅报告）",
    )
    parser.add_argument(
        "--scene-paragraphs",
        default="",
        metavar="RANGES",
        help="显式适用段，按1-based非空段号，如3-8,12；通用检查仍跑全文",
    )
    parser.add_argument(
        "--gate",
        default="",
        metavar="FULL_JSON",
        help="可选 FULL GATE；武力/复合场景用它机械复核 F-07 与支配范围",
    )
    args = parser.parse_args()
    if args.intensity_level < intensity_min or args.intensity_level > intensity_max:
        raise SystemExit(f"--intensity-level must be {intensity_min}-{intensity_max}")

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        if args.json:
            json.dump({"error": "输入为空"}, sys.stdout, ensure_ascii=False)
        else:
            print("FAIL：输入为空")
        sys.exit(1)

    stripped_n = 0
    text = raw
    if not args.keep_meta:
        text, stripped_n = strip_meta(raw)
        if not text.strip():
            if args.json:
                json.dump({"error": "剥离元信息后正文为空"}, sys.stdout, ensure_ascii=False)
            else:
                print("FAIL：剥离已登记元信息后正文为空")
            sys.exit(1)

    mode_raw = args.mode.lower().strip()
    validation_warnings: list[dict[str, str]] = []
    scene_text = text
    selected_scene_paragraphs: list[int] = []
    paragraph_count = len([p for p in text.split("\n\n") if p.strip()])
    mixed_scope = "not_applicable"
    if mode_raw == "mixed":
        if args.scene_paragraphs:
            try:
                scene_text, selected_scene_paragraphs, paragraph_count = select_paragraphs(
                    text, args.scene_paragraphs
                )
            except ValueError as exc:
                parser.error(str(exc))
            mixed_scope = "explicit_paragraphs"
        else:
            # 旧命令兼容：仍用全文跑关键场景门，但必须在机器报告中
            # 明示标记，新调用不应继续依赖这个过渡语义。
            mixed_scope = "whole_text_compat"
            validation_warnings.append(
                {
                    "code": "MIXED_SCOPE_COMPAT",
                    "message": "mixed 未传 --scene-paragraphs，兼容性对全文执行关键场景门；新调用应显式指定关键场景段",
                }
            )
    elif args.scene_paragraphs:
        try:
            scene_text, selected_scene_paragraphs, paragraph_count = select_paragraphs(text, args.scene_paragraphs)
        except ValueError as exc:
            parser.error(str(exc))

    if (args.strict_floor or args.strict_l5) and args.mode != 'peakScene' and not args.scene_paragraphs:
        parser.error('非全章关键场景的严格地板检查须显式传 --scene-paragraphs，避免把剧情段计入密度')

    if getattr(args, "no_strict_l5", False):
        strict_l5 = False
    elif args.strict_floor or getattr(args, "strict_l5", False):
        strict_l5 = True
    else:
        strict_l5 = False
    if args.intensity_level < 5:
        strict_l5 = False

    mode, check_specs, strict_l5 = build_checks(
        args.mode,
        strict_l5,
        args.intensity_level,
        strict_dialogue_floor=not args.no_dialogue_floor and (args.strict_floor or args.strict_l5),
    )
    checks = []
    for name, fn in check_specs:
        check_text = scene_text if args.scene_paragraphs and name in ADULT_CHECK_NAMES else text
        result = fn(check_text)
        item = _norm(result, name)
        if name == 'AI句式套路':
            locations = ai_pattern_violations(text, mode=mode)
            if locations:
                item['locations'] = locations
                item['line'] = locations[0]['line']
                item['evidence'] = locations[0]['text']
        if name == "L5张力等级地板" and strict_l5 and not item["pass"]:
            item["level"] = "fail"
            item["pass"] = False
            if "strict-l5" not in (item["detail"] or ""):
                item["detail"] = (item["detail"] or "") + "（strict-l5：未达L5地板）"
        elif name == "L5张力等级地板" and item['level'] != 'not_applicable':
            item["level"] = "info"
            item["pass"] = True
            if result[0] is False and "info不阻断" not in (item["detail"] or ""):
                item["detail"] = (item["detail"] or "") + "（未达词面地板，info不阻断；不能据此反推实际档位）"
        checks.append(item)
    if args.gate:
        gate = load_full_gate(args.gate)
        checks.append(_norm(check_martial_gate_consistency(text, gate), "武力Gate一致性"))

    all_pass = all(c["pass"] or c["level"] == "info" for c in checks)
    blocks = [
        {
            "id": c.get("rule_id") or c["name"],
            "name": c["name"],
            "detail": c.get("detail") or "",
            "fix_hint": c.get("fix_hint") or FIX_HINT_BY_ID.get(c.get("rule_id") or "", ""),
        }
        for c in checks
        if not c["pass"] and c.get("level") != "info"
    ]
    infos = [
        {
            "id": c.get("rule_id") or c["name"],
            "name": c["name"],
            "detail": c.get("detail") or "",
        }
        for c in checks
        if c.get("level") == "info"
    ]

    cn_chars = chinese_char_count(text)
    word_count = {
        "chinese_chars": cn_chars,
        "total_chars": len(text),
        "raw_chars": len(raw),
    }
    if args.target and args.target > 0:
        delta = cn_chars - args.target
        word_count["target"] = args.target
        word_count["delta"] = delta
        word_count["pct"] = round(cn_chars / args.target * 100, 1)
        word_count["on_target"] = abs(delta) <= target_tolerance(args.target)

    sensory_metrics = analyze_sensory_density(text)
    prose_quality_info = analyze_prose_quality(text, sensory_metrics)

    if args.json:
        json.dump(
            {
                "mode": mode,
                "total_chars": len(text),
                "raw_chars": len(raw),
                "chinese_chars": cn_chars,
                "word_count": word_count,
                "meta_lines_stripped": stripped_n,
                "warnings": validation_warnings,
                "mixed_scope": mixed_scope,
                "scene_paragraphs": selected_scene_paragraphs,
                "paragraph_count": paragraph_count,
                "strict_l5": strict_l5,
                "ok": all_pass,
                "all_pass": all_pass,
                "blocks": blocks,
                "infos": infos,
                "checks": checks,
                "sensory_metrics": sensory_metrics,
                "prose_quality": prose_quality_info,
                "l2": "Editor 语义审校（十身份阶段 B）",
                "l2_deliver": "[L2] pass|fail ids=…",
            },
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        print()
    else:
        print("=" * 60)
        print(
            f"机械项快速回检报告（{len(text)}字 / 汉字{cn_chars} · mode={mode} · strict_l5={strict_l5}）"
        )
        if args.target and args.target > 0:
            flag = "达标" if word_count.get("on_target") else "未达标"
            print(
                f"字数：汉字 {cn_chars} / 目标 {args.target}（Δ{word_count['delta']:+d} · {word_count['pct']}% · {flag}）"
            )
        if stripped_n:
            print(f"已剥离元信息行 {stripped_n} 行（--keep-meta 可保留）")
        for warning in validation_warnings:
            print(f"⚠️ {warning['code']}：{warning['message']}")
        if mode in {"plot", "buildup"}:
            print("覆盖：破折号(放开功能性/阻断滥用)、省略号(放开功能性/阻断滥用)、句长45、短句、段落、AI句式、感官词密度、AI味离线评分。")
        else:
            print(
                "覆盖：破折号(放开功能性/阻断滥用)、省略号(放开功能性/阻断滥用)、句长60/黏腻80、"
                f"短句(对话豁免)、段落(对话豁免/分隔符豁免)、叙事感官、AI句式、冲突对白、同义词、模板、L5地板({'strict FAIL' if strict_l5 else 'info'})、感官词密度、AI味离线评分。"
            )
        print("-" * 60)
        print("【离线正文质感与 AI 味量化评估】")
        print(
            f"  正文质感得分：{prose_quality_info['score']} / 100 分（等级：{prose_quality_info['grade']}）"
        )
        print(
            f"  感官词密度：{sensory_metrics['sensory_density_per_1k']} 次 / 1000字（覆盖通道 [{sensory_metrics['covered_count']}/5]：{', '.join(sensory_metrics['covered_channels']) or '无'}）"
        )
        if prose_quality_info["details"]:
            print("  扣分项明细：")
            for d in prose_quality_info["details"]:
                print(f"    • {d['category']}：{d['hits']} 次 (-{d['total_penalty']}分)")
        else:
            print("  扣分项明细：无扣分（未检出模板/解释腔/抽象心理直说）")
        if prose_quality_info["bonus_details"]:
            print("  加分项明细：")
            for b in prose_quality_info["bonus_details"]:
                print(f"    • {b}")
        print("=" * 60)
        print("不覆盖(L2)：Editor 语义审校（十身份阶段 B）→ 交付行 [L2] pass|fail ids=…")
        print("规则ID见 shared/registries/ids.yaml；契约短卡见 shared/contracts/")
        print("=" * 60)
        for c in checks:
            if c["level"] == "info":
                status = "⚠️ INFO"
            elif c["pass"]:
                status = "✅ PASS"
            else:
                status = "❌ FAIL"
            rid = f"[{c['rule_id']}] " if c.get("rule_id") else ""
            print(f"  {status}  {rid}{c['name']}", end="")
            if c["detail"]:
                print(f" — {c['detail']}")
            else:
                print()
            if not c["pass"] and c.get("fix_hint"):
                print(f"           fix: {c['fix_hint']}")
        print("=" * 60)
        print(
            "结论：",
            "机械项通过 → 再跑 Editor 语义审校（十身份阶段 B），输出 [L2] 短行"
            if all_pass
            else "机械项未通过 → 按 blocks/fix_hint 返修后重跑",
        )

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
