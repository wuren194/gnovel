"""Resolve display chapter codes (CH001) to stable chapter IDs at the call boundary.

章节显示编号是可变的阅读位置，chapterId 才是身份。调用者常只知道「第 1 章 / CH001」，
因此在 CLI 与 MCP 入口把章节参数里的显示编号换成稳定 ID，下游处理器只见稳定 ID。

- 先按稳定 ID 精确匹配；旧项目的 chapterId 本身可能就是 CH001，此时原样保留。
- 显示编号只在活动章节中唯一解析；归档章节保留旧 displayCode，不参与匹配。
- 找不到或不唯一时结构化拒绝，不猜最近的章节。
- 解析发生在当前结构上；预览一旦生成，计划里只记录稳定 ID。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .structure_store import StructureError, structure_path

DISPLAY_CODE_RE = re.compile(r"CH(\d{1,6})", re.IGNORECASE)
_SINGLE_FIELDS = {"chapterId", "beforeChapterId", "afterChapterId", "readerChapterId",
                  "chapter_id", "before_chapter_id", "after_chapter_id", "reader_chapter_id"}
_LIST_FIELDS = {"chapterIds", "chapter_ids"}


def _chapter_list(key, value):
    # argparse append keeps the singular option's destination (chapter_id).
    return key in (_SINGLE_FIELDS | _LIST_FIELDS) and isinstance(value, list)


def is_display_code(value: object) -> bool:
    return isinstance(value, str) and DISPLAY_CODE_RE.fullmatch(value.strip()) is not None


def _structure(root: Path) -> dict | None:
    path = structure_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None  # 让原处理器报告结构损坏，这里不抢先改写错误
    return data if isinstance(data, dict) and isinstance(data.get("chapters"), list) else None


def resolve_chapter_reference(structure: dict, value: str, *, field: str = "chapterId") -> str:
    """Return the stable chapterId for ``value``; stable IDs pass through unchanged."""
    chapters = [row for row in structure.get("chapters", []) if isinstance(row, dict)]
    if any(row.get("chapterId") == value for row in chapters):
        return value
    match = DISPLAY_CODE_RE.fullmatch(value.strip())
    if match is None:
        return value
    ordinal = int(match.group(1))
    active = [row for row in chapters if row.get("status") != "archived" and row.get("displayOrdinal") == ordinal]
    if len(active) == 1:
        return active[0]["chapterId"]
    details = {"field": field, "reference": value, "activeChapterCount":
               sum(1 for row in chapters if row.get("status") != "archived")}
    if active:
        details["candidates"] = sorted(row["chapterId"] for row in active)
        raise StructureError("CHAPTER_REFERENCE_AMBIGUOUS", f"display code matches several active chapters: {value}", details)
    archived = sorted(row["chapterId"] for row in chapters
                      if row.get("status") == "archived" and str(row.get("displayCode", "")).upper() == f"CH{ordinal:03d}")
    if archived:
        details["archivedChapterIds"] = archived
    raise StructureError("CHAPTER_REFERENCE_NOT_FOUND", f"no active chapter has display code {value}", details)


def resolve_chapter_arguments(root: Path, arguments: dict) -> list[dict]:
    """Rewrite display codes in top-level chapter fields of ``arguments`` in place.

    Returns one record per rewritten value; an empty list means nothing changed and
    the structure was not read unless a display code was present.
    """
    pending = [key for key, value in arguments.items()
               if (key in _SINGLE_FIELDS and is_display_code(value))
               or (_chapter_list(key, value) and any(is_display_code(v) for v in value))]
    if not pending:
        return []
    structure = _structure(root)
    if structure is None:
        return []
    resolved = []
    for key in pending:
        is_list = _chapter_list(key, arguments[key])
        values = arguments[key] if is_list else [arguments[key]]
        rewritten = []
        for value in values:
            target = resolve_chapter_reference(structure, value, field=key) if is_display_code(value) else value
            if target != value:
                resolved.append({"field": key, "reference": value, "chapterId": target})
            rewritten.append(target)
        arguments[key] = rewritten if is_list else rewritten[0]
    return resolved


def annotate(result, resolved: list[dict]):
    if resolved and isinstance(result, dict) and "chapterReferences" not in result:
        result["chapterReferences"] = resolved
    return result
