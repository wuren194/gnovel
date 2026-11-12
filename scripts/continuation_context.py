#!/usr/bin/env python3
"""续写上下文的兼容 facade（P5）。

正式装配逻辑已收敛到唯一实现 ``scripts/core/context_builder.py``；本模块只做两件事：

1. re-export 该 Builder 的公开与历史名字（``build_continuation_context`` 等），
   让既有导入方与测试无需改动即可继续工作；
2. 提供 ``python3 scripts/continuation_context.py`` 的命令行入口。

本文件**不再包含任何独立的上下文装配逻辑**——它不是第二套实现。其 CLI 是 Context read
入口，以 ``read_only=True`` 装配：只读投影，不发布派生缓存。缓存重建仍走显式维护命令
``novelctl project state rebuild``。

CLI:
  python3 scripts/continuation_context.py --project-root <项目> --chapter-id CHxxx
  python3 scripts/continuation_context.py --project-root <项目> --chapter-id CH005 --recent 3 --json
"""

from __future__ import annotations

import sys

try:  # 包内导入（scripts.continuation_context）
    from scripts.core.context_builder import (
        LAYERS,
        ContextBuilderError,
        ContinuationContextError,
        bound_station_context,
        build_context,
        build_continuation_context,
        build_station_context,
        serialize_context,
    )
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, EXIT_FINDING, EXIT_OK, FailClosedArgumentParser
except ImportError:  # 以 `python3 scripts/continuation_context.py` 直接运行时
    from core.context_builder import (
        LAYERS,
        ContextBuilderError,
        ContinuationContextError,
        bound_station_context,
        build_context,
        build_continuation_context,
        build_station_context,
        serialize_context,
    )
    from core.exit_codes import EXIT_FAIL_CLOSED, EXIT_FINDING, EXIT_OK, FailClosedArgumentParser

__all__ = [
    "build_continuation_context", "build_station_context", "build_context",
    "bound_station_context", "serialize_context", "ContextBuilderError",
    "ContinuationContextError", "LAYERS",
]


def _summarize(ctx: dict) -> str:
    lines = [
        f"续写上下文 · {ctx['projectId']} · 目标 {ctx['targetChapter']}（已提交 {ctx['committedChapters']} 章）",
        "",
    ]
    if not ctx.get("readiness", {}).get("ready", True):
        lines.append("NOT_READY: " + ", ".join(ctx["readiness"]["reasons"]))
    if "storySummary" in ctx:
        lines.append("剧情摘要（LLM）：")
        lines.append(f"- {ctx['storySummary']['storySummary']}")
        for c in ctx['storySummary'].get('plotLine', []):
            title = f" {c['title']}" if c.get('title') else ''
            lines.append(f"- {c['chapterId']}{title}：{c['summary']}")
        if ctx['storySummary'].get('characterSummaries'):
            lines.append("角色摘要：")
            for item in ctx["storySummary"]["characterSummaries"]:
                lines.append(f"- {item['characterId']}：{item['summary']}")
        lines.append("")
    lines.append("角色状态：")
    for item in ctx["characterStates"]:
        lines.append(
            f"- {item['characterId']} @ {item['locationId']}｜physical={item['physical']}"
            f"｜emotional={item['emotional']}"
        )
    overdue = [r for r in ctx["foreshadowing"] if r.get("classification") == "overdue"]
    due = [r for r in ctx["foreshadowing"] if r.get("classification") == "due"]
    lines.append(f"\n伏笔：overdue {len(overdue)} / due {len(due)}")
    for r in ctx["foreshadowing"][:6]:
        lines.append(f"- [{r.get('classification')}] {r.get('foreshadowId')}: {r.get('description')}")
    unknown = ctx.get("foreshadowingWarnings", [])
    if unknown:
        lines.append("警告（非规范伏笔状态，已按非活跃处理）：")
        for u in unknown:
            lines.append(f"- {u['hookId']}: {u['status']}")
    lines.append("\n剧情线：")
    for c in ctx["plotLine"][-8:]:
        lines.append(f"- {c['chapterId']} {c['title']}")
    lines.append("\n最近章节（按需读全文）：")
    for c in ctx["recentChapters"]:
        lines.append(f"- {c['chapterId']} {c['title']} → {c['relativePath']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = FailClosedArgumentParser(description="确定性装配一份工位续写上下文（兼容入口）")
    parser.add_argument("--project-root", required=True, help="v4 项目根目录")
    parser.add_argument("--chapter-id", required=True, help="目标章节号（如 CH005）")
    parser.add_argument("--recent", type=int, default=2, help="最近 N 章正文路径（默认 2）")
    parser.add_argument("--task-type", default="planner",
                        choices=["planner", "writer", "editor", "query"],
                        help="任务类型，决定记忆预算")
    parser.add_argument("--pov-character", default=None,
                        help="当前场景视角角色 ID，过滤注入信息边界")
    parser.add_argument("--chapter-goal", default="",
                        help="本章章纲目标，唯一驱动知识检索与记忆过滤")
    parser.add_argument("--view", default="full", choices=["full", "station"],
                        help="full 返回完整审计上下文；station 只返回预算化工位包")
    parser.add_argument("--json", action="store_true", help="输出 JSON（默认）")
    parser.add_argument("--summary", action="store_true", help="输出纯文本摘要")
    args = parser.parse_args(argv)

    try:
        ctx = build_station_context(
            args.project_root, args.chapter_id,
            recent=args.recent,
            task_type=args.task_type,
            pov_character=args.pov_character,
            chapter_goal=args.chapter_goal,
            view=args.view,
            read_only=True,
        )
    except ContextBuilderError as exc:
        print(f"continuation_context: {exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED

    if args.summary:
        print(_summarize(ctx) if args.view == "full" else serialize_context(ctx))
    else:
        print(serialize_context(ctx))
    return EXIT_OK if ctx["readiness"]["ready"] else EXIT_FINDING


if __name__ == "__main__":
    sys.exit(main())
