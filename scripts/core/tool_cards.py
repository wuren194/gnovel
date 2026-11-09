"""Generated station condition cards: the catalogue is the only source.

六个工位文档绝不手写工具表（那会变成六份需要人工同步的副本）；本模块按受众
从能力目录生成每工位一份条件卡片，`--check` 锁定无漂移。
"""
from __future__ import annotations

from pathlib import Path

from .jsonio import write_text
from .project import TOOLBOX_ROOT
from .tool_catalog import list_capabilities

STATIONS = {
    "composer": "gnovel-composer",
    "router": "gnovel-router",
    "planner": "gnovel-planner",
    "writer": "gnovel-writer",
    "editor": "gnovel-editor",
    "titler": "gnovel-titler",
}
RELATIVE = "{station}/references/tool-capabilities.md"


def render_card(audience: str) -> str:
    catalog = list_capabilities(audience=audience)
    lines = [
        f"# {audience} 工位能力条件段（生成物）",
        "",
        "> GENERATED: 由 `novelctl project tools station-cards --apply` 从能力目录生成。",
        "> 手改会被 `--check` 报成漂移；条件、参数与效果的真源是 shared/registries/tools.json",
        "> 与 scripts/core/operations.py，本文件只是它的人读投影。",
        "",
        "## 本工位相关能力",
        "",
        "| 能力 ID | 何时适用（事实 → 观察值） | 为什么 | 证据/义务 | 绑定 |",
        "|---|---|---|---|---|",
    ]
    for cap in catalog["capabilities"]:
        when = cap["when"]
        binding = cap["binding"]
        bound = binding["ref"] if binding["kind"] == "operation" else binding["ref"]
        obligation = "、".join(cap["requiredFor"]) + (f"（证据：{cap['evidence']}）" if cap["evidence"] else "") or "—"
        lines.append(f"| {cap['id']} | {when['fact']} {when['op']} {str(when['value']).lower()} | {when['reason']} | {obligation} | {binding['kind']}:{bound} |")
    lines += [
        "",
        "## 说明",
        "",
        "- 条件为 true/false/unknown 三值；unknown 不放行必要证据，也不当作不适用。",
        "- necessary 的能力缺失时，`project prepare` 会阻塞并给出缺什么；语义审查由本工位执行，系统只验证结论工件。",
        "- 不在本表的能力属于其它工位或维护通道；不要跨工位代跑。",
        "",
    ]
    return "\n".join(lines)


def station_cards() -> dict[str, str]:
    return {audience: render_card(audience) for audience in STATIONS}


def check_station_cards() -> dict:
    drift, missing = [], []
    for audience, text in station_cards().items():
        path = TOOLBOX_ROOT / RELATIVE.format(station=STATIONS[audience])
        if not path.is_file():
            missing.append(str(path.relative_to(TOOLBOX_ROOT)))
        elif path.read_text(encoding="utf-8") != text:
            drift.append(str(path.relative_to(TOOLBOX_ROOT)))
    return {"ok": not drift and not missing, "code": "STATION_CARDS_CHECKED",
            "drift": drift, "missing": missing}


def apply_station_cards() -> dict:
    written = []
    for audience, text in station_cards().items():
        path = TOOLBOX_ROOT / RELATIVE.format(station=STATIONS[audience])
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, text)
        written.append(str(path.relative_to(TOOLBOX_ROOT)))
    return {"ok": True, "code": "STATION_CARDS_APPLIED", "written": written}
