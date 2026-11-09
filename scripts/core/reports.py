"""Diagnostic severity and report normalization."""

from __future__ import annotations

SEVERITY_SCORES = {
    "blocking": 4,
    "error": 3,
    "warning": 2,
    "advisory": 2,
    "info": 1,
}

RECOMMENDATION_SCORES = {
    "rewrite_with_generator": 5,
    "rewrite_required": 4,
    "surgical_edit": 3,
    "review_advisory": 2,
    "pass": 1,
}

# 非阻断推荐：advisory 是「值得人看一眼」的信号，不是门禁失败。最终化机械门只区分阻断与非
# 阻断，子检查器（check_degeneration 把 blocking / advisory 分开）与聚合器在这里对齐。
NON_BLOCKING_RECOMMENDATIONS = frozenset({"pass", "review_advisory"})


def severity_score(value: str) -> int:
    return SEVERITY_SCORES.get(value, 0)


def recommendation_score(value: str) -> int:
    return RECOMMENDATION_SCORES.get(value, 0)


def best_recommendation(recs) -> str:
    """从一组推荐里取分值最高者；空输入或全是未知值都视为 ``pass``。

    聚合器用它把各子检查器的 recommendation 合成一个总推荐。``review_advisory`` 必须在表内
    且高于 ``pass``——否则 advisory-only 的章节会被 ``pass`` 压掉，退化信号从推荐和门禁里一起
    消失。``rewrite_with_generator`` 也在表内，聚合器无需再把它隐式翻译成 ``rewrite_required``。
    """
    best = "pass"
    for value in recs:
        if recommendation_score(value) > recommendation_score(best):
            best = value
    return best


def gate_passed(best_rec: str) -> bool:
    """最终化机械门：只有阻断级推荐算不过；``review_advisory`` 是非阻断信号，照样放行。"""
    return best_rec in NON_BLOCKING_RECOMMENDATIONS


def finding(*, source: str, severity: str, message: str, evidence: str = "", rule_id: str = "") -> dict:
    return {
        "source": source,
        "severity": severity,
        "ruleId": rule_id,
        "message": message,
        "evidence": evidence,
    }
