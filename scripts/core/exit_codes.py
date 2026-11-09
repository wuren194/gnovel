"""诊断与工具脚本的退出码契约——`shared/contracts/RUNTIME-CONFIG.md` 的程序侧真源。

统一之前，「配置 / 输入不可解析」在各工具里各用 1 或 2，与诊断语义里 2=阻断发现相撞：直接
调用子检查器的工位会把一次读取失败当成「需整章重写」。现在各处入口共用这套码：

    0 = 正常 / 无发现
    1 = 有发现（非阻断：advisory、需局部 surgical_edit）
    2 = 有阻断发现（需整章重写：rewrite_required / rewrite_with_generator）
    3 = 配置 / 输入不可解析（fail closed）

失败一律走 :data:`EXIT_FAIL_CLOSED`；发现类退出码由 :func:`exit_code_for_recommendation` 从
recommendation 统一推导，子检查器与聚合器不再各自硬编码 0/1/2 的映射（RUNTIME-CONFIG 第 22 条：
不得在多个模块重复硬编码同一阈值）。
"""

from __future__ import annotations

import argparse
import sys

from .reports import recommendation_score

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_BLOCKING = 2
EXIT_FAIL_CLOSED = 3


def exit_code_for_recommendation(recommendation: str) -> int:
    """recommendation → 退出码：``pass``→0，rewrite 级→2，其余（advisory / surgical_edit）→1。"""
    if recommendation == "pass":
        return EXIT_OK
    if recommendation_score(recommendation) >= recommendation_score("rewrite_required"):
        return EXIT_BLOCKING
    return EXIT_FINDING


class FailClosedArgumentParser(argparse.ArgumentParser):
    """用法错误（缺参数 / 非法选项）也走 fail-closed（3），不与诊断语义 2=阻断发现相撞。

    argparse 默认 `error()` 退 2；直接调用子检查器的工位会把一次漏传参数当成「需整章重写」。
    只覆盖 `error()`——`--help` 仍走 `exit(0)`，正常解析路径不受影响。
    """

    def error(self, message: str):  # noqa: D401 - argparse 的 error 约定即打印后退出
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAIL_CLOSED, f"{self.prog}: error: {message}\n")
