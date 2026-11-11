"""Transport-neutral project queries and commands."""

from __future__ import annotations

import re
from pathlib import Path

from .entity_registry import references_to, resolve_entity
from .paths import ProjectPathError, safe_project_path
from .project import chapter_length_bounds
from .structure_store import StructureError, is_time_ordinal, load_structure, ordered_chapters, structure_hash

try:
    from ..text_utils import chinese_char_count
except ImportError:  # 以 core.commands 顶层包加载时（scripts/ 在 sys.path）
    from text_utils import chinese_char_count


def _han_count(root: Path, relative: str) -> int | None:
    try:
        path = safe_project_path(root, relative)
    except ProjectPathError:
        return None
    if not path.is_file():
        return None
    try:
        return chinese_char_count(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _with_word_counts(root: Path, chapters: list[dict]) -> list[dict]:
    low, high = chapter_length_bounds()
    rows = []
    for chapter in chapters:
        count = _han_count(root, chapter.get("filePath", ""))
        length_status = None
        if count is not None and chapter.get("status") in {"draft", "review", "stable"}:
            if count < low:
                length_status = "short"
            elif count > high:
                length_status = "long"
            else:
                length_status = "ok"
        rows.append({**chapter, "hanCharCount": count, "lengthStatus": length_status})
    return rows


def project_overview(root: Path) -> dict:
    structure = load_structure(root)
    chapters = _with_word_counts(root, ordered_chapters(structure))
    stable = [item for item in chapters if item.get("status") == "stable"]
    low, high = chapter_length_bounds()
    return {
        "projectId": structure["projectId"],
        "bookName": structure["bookName"],
        "revision": structure["revision"],
        "structureHash": structure_hash(structure),
        "chapterLength": {"minHanCharacters": low, "maxHanCharacters": high},
        "counts": {
            "volumes": len(structure["volumes"]),
            "arcs": len(structure["arcs"]),
            "chapters": len(chapters),
            "entities": len(structure["entities"]),
            "relations": len(structure["relations"]),
        },
        "latestStableChapter": stable[-1] if stable else None,
        "chapters": chapters,
        "arcs": structure["arcs"],
        "chapterArcLinks": structure["chapterArcLinks"],
    }


def story_timeline(root: Path, *, structure: dict | None = None) -> dict:
    """故事时间线（多轨道）。

    - 时间锚点 = time_anchor 实体，`absoluteTime` 存绝对时间（可排序字符串，如
      "0847-02-18 23:00" 或 "第03年·冬"，按字典序排序），`name` 是给读者看的称呼。
    - 当前约束能求出精确数值时按数值排序；其余仍按显示标签排序，显示次序不证明先后。
    - 主轨道 = 章节（storyTimeStartId/EndId 挂到锚点上，可跨段）。
    - 其他轨道 = 任意实体（人物、伏笔、事件……）通过 timeStartId/timeEndId 挂载，
      起止相同即为一个点，不同即为一段。
    """
    structure = load_structure(root) if structure is None else structure
    from .source_time import story_time_states, time_span_states
    time_states = story_time_states(structure)
    time_spans = time_span_states(structure, time_states=time_states)
    mounted_ids = {span[key] for span in time_spans.values() for key in ('startId', 'endId')
                   if span[key] is not None}
    raw_anchors = [item for item in structure.get("entities", [])
                   if item.get("entityType") == "time_anchor"
                   or item.get("entityType") == "event" and item.get("entityId") in mounted_ids]
    order_hint = {item["entityId"]: i for i, item in enumerate(raw_anchors)}
    archived_anchor_ids = {item["entityId"] for item in raw_anchors if item.get("status") == "archived"}

    def anchor_key(anchor: dict) -> tuple:
        hint = order_hint[anchor["entityId"]]
        state = time_states.get(anchor['entityId'], {})
        ordinal = state.get('lower') if state.get('status') == 'exact' else None
        if ordinal is not None:
            return (0, float(ordinal), False, "", hint)
        absolute = str(anchor.get("absoluteTime") or "")
        # 缺 absoluteTime 的锚点排在字典序组末尾：空串的字典序最小，不另设一位会让它
        # 跑到所有锚点最前，把挂在上面的章节拉到时间线开头。
        return (1, 0.0, not absolute, absolute, hint)

    anchors = sorted(raw_anchors, key=anchor_key)
    anchor_order = {item["entityId"]: index for index, item in enumerate(anchors)}
    problems = []
    for anchor in anchors:
        if (not str(anchor.get("absoluteTime") or "").strip()
                and time_states.get(anchor['entityId'], {}).get('status') not in {'exact', 'range'}):
            problems.append({"code": "ANCHOR_MISSING_ABSOLUTE_TIME", "anchorId": anchor["entityId"]})

    def span(item: dict, start_key: str, end_key: str, id_key: str) -> tuple[int | None, int | None]:
        start_id, end_id = item.get(start_key), item.get(end_key)
        for anchor_id in (start_id, end_id):
            if anchor_id and anchor_id not in anchor_order:
                problems.append({"code": "UNKNOWN_TIME_ANCHOR", id_key: item.get(id_key), "anchorId": anchor_id})
            elif anchor_id in archived_anchor_ids:
                problems.append({"code": "ARCHIVED_ANCHOR_REFERENCED", id_key: item.get(id_key), "anchorId": anchor_id,
                                 "message": "该锚点已归档但仍被挂载；请迁移到活跃锚点或解除挂载"})
        start_index = anchor_order.get(start_id) if start_id else None
        end_index = anchor_order.get(end_id) if end_id else start_index
        return start_index, end_index

    chapters = ordered_chapters(structure)
    volume_names = {str(v.get("volumeId")): v.get("title", "") for v in structure.get("volumes", [])}
    from .source_placement import chapter_volume_id
    schema_version = structure.get("schemaVersion")
    arc_names = {str(a.get("arcId")): a.get("title", "") for a in structure.get("arcs", [])}
    arc_ids_by_chapter: dict[str, list[str]] = {}
    for link in structure.get("chapterArcLinks", []):
        arc_ids_by_chapter.setdefault(str(link.get("chapterId")), []).append(str(link.get("arcId", "")))
    chapter_rows = []
    previous_start = None
    previous_time = None
    for chapter in chapters:
        start_index, end_index = span(chapter, "storyTimeStartId", "storyTimeEndId", "chapterId")
        current_time = time_states.get(chapter.get('storyTimeStartId'), {})
        if (previous_time and current_time.get('status') == previous_time.get('status') == 'exact'
                and current_time['lower'] < previous_time['lower']):
            problems.append({"code": "TIME_ORDER_INVERSION", "chapterId": chapter["chapterId"],
                             "message": "本章故事时间早于前一章（若为倒叙请忽略）"})
        elif start_index is not None and previous_start is not None and start_index < previous_start:
            problems.append({"code": "TIME_DISPLAY_ORDER_INVERSION", "chapterId": chapter["chapterId"],
                             "message": "本章显示位置早于前章；非精确时间排序不证明故事时间冲突",
                             "certainty": "display_only"})
        if start_index is not None:
            previous_start = start_index
            previous_time = current_time
        chapter_rows.append({
            "chapterId": chapter["chapterId"],
            "displayCode": chapter.get("displayCode", ""),
            "displayOrdinal": chapter.get("displayOrdinal"),
            "title": chapter.get("title", ""),
            "volumeName": (lambda vid: volume_names.get(str(vid), "") if vid is not None else None)(chapter_volume_id(chapter, schema_version)),
            "arcNames": [arc_names[arc_id] for arc_id in arc_ids_by_chapter.get(str(chapter.get("chapterId", "")), []) if arc_id in arc_names],
            "status": chapter.get("status", ""),
            "startAnchorId": chapter.get("storyTimeStartId"),
            "endAnchorId": chapter.get("storyTimeEndId"),
            "startIndex": start_index,
            "endIndex": end_index,
            "timeSpan": time_spans.get(chapter['chapterId']),
        })

    tracks: dict[str, list[dict]] = {}
    for entity in structure.get("entities", []):
        if entity.get("entityType") == "time_anchor":
            continue
        if not entity.get("timeStartId") and not entity.get("timeEndId"):
            continue
        start_index, end_index = span(entity, "timeStartId", "timeEndId", "entityId")
        if start_index is None and end_index is None:
            continue
        tracks.setdefault(entity["entityType"], []).append({
            "entityId": entity["entityId"],
            "name": entity.get("name", ""),
            "entityType": entity.get("entityType", ""),
            "status": entity.get("status", ""),
            "summary": entity.get("summary", ""),
            "startAnchorId": entity.get('timeStartId'),
            "endAnchorId": entity.get('timeEndId'),
            "timeSpan": time_spans.get(entity['entityId']),
            "startIndex": start_index if start_index is not None else end_index,
            "endIndex": end_index if end_index is not None else start_index,
            "isPoint": (start_index if start_index is not None else end_index) == (end_index if end_index is not None else start_index),
        })
    for rows in tracks.values():
        rows.sort(key=lambda item: (item["startIndex"], item["endIndex"], item["entityId"]))

    return {
        "bookName": structure.get("bookName", ""),
        "anchors": [{"entityId": a["entityId"], "name": a["name"], "absoluteTime": str(a.get("absoluteTime") or ""),
                     "timeOrdinal": a.get("timeOrdinal"), "storyTime": time_states.get(a['entityId']),
                     "summary": a.get("summary", ""), "index": i}
                    for i, a in enumerate(anchors)],
        "chapters": chapter_rows,
        "tracks": tracks,
        "problems": problems,
        "unmappedChapterCount": sum(1 for row in chapter_rows if row["startIndex"] is None and row["status"] != "archived"),
    }


def _chapter_runs(ordinals: list) -> list[str]:
    """把连续章号压成区间标签：1,2,3 → 第01–03章；7 → 第07章。"""
    runs: list[str] = []
    start: int | None = None
    previous: int | None = None
    for value in sorted({int(v) for v in ordinals if isinstance(v, int)}) + [None]:
        if start is None:
            start = previous = value
            continue
        if value is not None and value == previous + 1:
            previous = value
            continue
        runs.append(f"第{start:02d}章" if start == previous else f"第{start:02d}–{previous:02d}章")
        start = previous = value
    return runs


_MERMAID_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MERMAID_WHITESPACE = re.compile(r"\s+")


def _single_line(value) -> str:
    """把任意文本压成一行。

    换行/制表/控制字符是 mermaid 语法的硬断点——一个含换行的实体名会让整张图渲染失败，
    而不是显示得难看一点。名字只经 `str().strip()` 落库，所以这里必须兜住。
    """
    return _MERMAID_WHITESPACE.sub(" ", _MERMAID_CONTROL.sub(" ", str(value))).strip()


def _mermaid_text(value: str) -> str:
    """mermaid 单行文本转义：半角冒号是 timeline 的字段分隔符，# 有注释语义。"""
    return _single_line(value).replace(":", "：").replace("#", "＃")


def _mermaid_edge_label(value: str) -> str:
    """mermaid 边标签转义：`|` 就是 `-->|label|` 的分隔符，必须换成全角。"""
    return _single_line(value).replace("|", "｜")


def timeline_mermaid(timeline: dict) -> str:
    """把 story_timeline 的输出渲染成 mermaid timeline 图。

    相对时间轴（倒计时）没有日期，gantt 型图表用不了；timeline 图只需时段标签，
    每个锚点是一个时段，挂在该锚点上的章节压缩成区间作为事件。
    """
    title = _mermaid_text(timeline.get("bookName") or "") or "故事时间线"
    lines = ["timeline", f"    title {title} · 故事时间线"]
    chapters_by_anchor: dict[str, list] = {}
    for row in timeline.get("chapters", []):
        anchor_id = row.get("startAnchorId") or row.get("endAnchorId")
        if not anchor_id:
            continue
        chapters_by_anchor.setdefault(anchor_id, []).append(row.get("displayOrdinal"))
    for anchor in timeline.get("anchors", []):
        label = _mermaid_text(anchor.get("name", ""))
        ordinal = anchor.get("timeOrdinal")
        if is_time_ordinal(ordinal):
            number = int(ordinal) if float(ordinal).is_integer() else ordinal
            label = f"{number} · {label}"
        events = _chapter_runs(chapters_by_anchor.get(anchor["entityId"], []))
        lines.append(f"    {label}" + (f" : {' : '.join(events)}" if events else ""))
    return "\n".join(lines) + "\n"


ENTITY_TYPE_LABELS = {
    "character": "人物", "location": "地点", "faction": "势力", "prop": "道具", "rule": "规则",
    "event": "事件", "hook": "伏笔", "secret": "秘密", "soft_limit": "软限制",
}
_TYPE_ORDER = ["character", "location", "faction", "prop", "rule", "event", "hook", "secret", "soft_limit"]


def _node_id(entity_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", str(entity_id))


def graph_mermaid(graph: dict) -> str:
    """把 structure_graph 的输出渲染成按实体类型分组的 mermaid flowchart。"""
    groups: dict[str, list[dict]] = {}
    for node in graph.get("nodes", []):
        groups.setdefault(str(node.get("entityType", "")), []).append(node)
    ordered = [kind for kind in _TYPE_ORDER if kind in groups]
    ordered += sorted(kind for kind in groups if kind not in _TYPE_ORDER)
    lines = ["flowchart LR"]
    for entity_type in ordered:
        label = _single_line(ENTITY_TYPE_LABELS.get(entity_type, entity_type or "其他")).replace('"', "'")
        lines.append(f'    subgraph "{label}"')
        for node in groups[entity_type]:
            name = _single_line(node.get("name", "")).replace('"', "'")
            mentions = node.get("mentions") or 0
            text = f"{name} ·{mentions}" if mentions else name
            lines.append(f'        {_node_id(node["entityId"])}["{text}"]')
        lines.append("    end")
    for edge in graph.get("edges", []):
        kind = _mermaid_edge_label(edge.get("kind", ""))
        arrow = f"-->|{kind}|" if kind and kind != "unspecified" else "-->"
        lines.append(f'    {_node_id(edge["from"])} {arrow} {_node_id(edge["to"])}')
    return "\n".join(lines) + "\n"


def entity_graph(root: Path, *, structure: dict | None = None) -> dict:
    """关系图谱数据：非锚点、未归档实体为节点，relations 为边。"""
    structure = load_structure(root) if structure is None else structure
    entities = [e for e in structure.get("entities", []) if e.get("entityType") != "time_anchor" and e.get("status") != "archived"]
    ids = {e["entityId"] for e in entities}
    mention_counts: dict[str, int] = {}
    for ref in structure.get("references", []):
        target = ref.get("targetId")
        if target in ids:
            mention_counts[target] = mention_counts.get(target, 0) + 1
    edges = [
        {"relationId": r.get("relationId", ""), "from": r["fromEntity"], "to": r["toEntity"], "kind": r.get("kind", "")}
        for r in structure.get("relations", [])
        if r.get("fromEntity") in ids and r.get("toEntity") in ids
    ]
    nodes = [
        {"entityId": e["entityId"], "name": e.get("name", ""), "entityType": e.get("entityType", ""),
         "summary": e.get("summary", ""), "mentions": mention_counts.get(e["entityId"], 0)}
        for e in entities
    ]
    return {"nodes": nodes, "edges": edges}


def chapter_prose(root: Path, chapter_id: str) -> dict:
    """只读读取章节正文，路径必须落在项目根内。"""
    structure = load_structure(root)
    chapter = next((item for item in structure["chapters"] if item["chapterId"] == chapter_id), None)
    if chapter is None:
        raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
    relative = str(chapter.get("filePath", "")).strip()
    try:
        path = safe_project_path(root, relative)
    except ProjectPathError as exc:
        raise StructureError(exc.code, str(exc), exc.details) from exc
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {
        "chapterId": chapter_id,
        "displayCode": chapter.get("displayCode", ""),
        "title": chapter.get("title", ""),
        "status": chapter.get("status", ""),
        "filePath": relative,
        "exists": path.is_file(),
        "hanCharCount": chinese_char_count(text) if text else 0,
        "text": text,
    }


def list_foreshadowing(root: Path) -> dict:
    """伏笔视图：hook/secret 实体 + 埋设与回收章节。"""
    structure = load_structure(root)
    chapter_codes = {item["chapterId"]: item.get("displayCode", "") for item in structure["chapters"]}
    rows = []
    for entity in structure.get("entities", []):
        if entity.get("entityType") not in {"hook", "secret"}:
            continue
        mentions = sorted(
            {ref["sourceId"] for ref in references_to(structure, entity["entityId"]) if ref.get("sourceId") in chapter_codes},
            key=lambda cid: chapter_codes.get(cid, ""),
        )
        rows.append({
            **entity,
            "mentionChapters": [{"chapterId": cid, "displayCode": chapter_codes.get(cid, "")} for cid in mentions],
            "unresolved": entity.get("status") == "active",
        })
    rows.sort(key=lambda item: (not item["unresolved"], item.get("entityType", ""), item["entityId"]))
    return {"foreshadowing": rows, "unresolvedCount": sum(1 for row in rows if row["unresolved"])}


def structure_graph(root: Path) -> dict:
    structure = load_structure(root)
    return {
        "projectId": structure["projectId"],
        "revision": structure["revision"],
        "volumes": structure["volumes"],
        "arcs": structure["arcs"],
        "chapters": ordered_chapters(structure, include_archived=True),
        "chapterArcLinks": structure["chapterArcLinks"],
        "arcPhases": structure.get("arcPhases", []),
        # Units and their member lists are authorial planning containers, surfaced here
        # so the structural graph shows cross-volume and project-scope grouping.
        "units": structure.get("units", []),
    }


def list_entities(root: Path, *, entity_type: str | None = None, query: str = "") -> dict:
    structure = load_structure(root)
    query_key = query.strip().casefold()
    rows = []
    for entity in structure.get("entities", []):
        if entity_type and entity.get("entityType") != entity_type:
            continue
        labels = [entity.get("entityId", ""), entity.get("name", ""), *entity.get("aliases", [])]
        if query_key and not any(query_key in str(label).casefold() for label in labels):
            continue
        rows.append({**entity, "referenceCount": len(references_to(structure, entity["entityId"]))})
    rows.sort(key=lambda item: (item.get("entityType", ""), item.get("name", ""), item["entityId"]))
    return {"entities": rows, "count": len(rows)}


def get_entity(root: Path, selector: str) -> dict:
    from .consistency import require_no_pending
    from .source_fields import projections

    require_no_pending(root)
    structure = load_structure(root)
    entity = resolve_entity(structure, selector)
    projected = projections(structure)[entity["entityId"]]
    # This is the author-level entity query, as before; it has no POV/asOf input.
    # Keep source facts beside the metadata, never on the writable entity record.
    source_projection = {
        "valueSource": ".govel/structure.json",
        "knowledgeMode": "author",
        "timeScope": "all",
        "excludedReason": "archived" if entity.get("status") == "archived" else None,
        "fields": projected["fields"],
        "fieldValues": projected["fieldValues"],
        "fragments": projected["fragments"],
        "fieldDefinitions": [field for field in structure["fieldDefinitions"]
                             if entity["entityType"] in field["appliesTo"]],
    }
    require_no_pending(root)
    return {"entity": entity, "references": references_to(structure, entity["entityId"]),
            "sourceProjection": source_projection}


EXPORT_STATUSES = ("stable", "review", "draft", "planning_required")
EXPORT_DIR = "工作台"


def _project_relative(base: Path, raw: Path) -> Path | None:
    """把 `--output` 归一成项目内相对路径；确认在项目外则返回 None。

    绝对路径要比两次：先按字面比，再按 `resolve()` 后的真身比。macOS 的 `/var` 与
    `/private/var`（`/tmp` 与 `/private/tmp` 同理）是同一个目录的两种写法，只比字面
    会把项目内的绝对路径误判成项目外，从而绕过 `工作台/` 限制。
    """
    if not raw.is_absolute():
        return raw
    try:
        return raw.relative_to(base)
    except ValueError:
        pass
    try:
        return raw.resolve(strict=False).relative_to(base)
    except ValueError:
        return None


def resolve_export_target(root: Path, output: Path | None, *, book_name: str, fmt: str) -> Path:
    """解析成品稿落点，把导出限制在作者可见的导出区。

    导出会写文件，因此不能像其他只读命令那样接受任意目标：

    - 不给 `--output`：落 `工作台/<书名>-成品.<格式>`；
    - 相对 `--output`：按 `--project-root` 解析（而不是进程 CWD），且项目内目标必须位于
      `工作台/` 下——`设定/`、`规划/`、正文与 `.govel/` 都不是导出目标；
    - 项目外交付物：用绝对路径显式指定（例如 `~/Desktop/xxx.md`）；
    - 目标本身是符号链接一律拒绝（`resolve()` 会吞掉链接，所以要在解析前检查）。
    """
    base = root.expanduser().resolve()
    raw = Path(output).expanduser() if output else Path(EXPORT_DIR) / f"{book_name}-成品.{fmt}"
    inside = _project_relative(base, raw)
    if inside is None:
        if raw.is_symlink():
            raise ProjectPathError(
                "SYMLINK_PATH_FORBIDDEN", "export output symlink is not allowed", {"path": str(raw)}
            )
        target = raw.resolve(strict=False)
        if target.exists() and not target.is_file():
            raise ProjectPathError(
                "EXPORT_TARGET_NOT_FILE", "export target is not a regular file", {"path": str(target)}
            )
        return target
    try:
        target = safe_project_path(base, inside)
    except ProjectPathError as exc:
        if exc.code == "PATH_OUTSIDE_PROJECT":
            raise ProjectPathError(
                "PATH_OUTSIDE_PROJECT",
                "相对路径按 --project-root 解析；项目外导出请用绝对路径",
                {"path": str(raw)},
            ) from exc
        raise
    if target.relative_to(base).parts[0] != EXPORT_DIR:
        raise ProjectPathError(
            "EXPORT_OUTPUT_FORBIDDEN",
            f"项目内导出目标必须位于 {EXPORT_DIR}/ 下",
            {"path": str(raw), "directory": EXPORT_DIR},
        )
    return target


def export_manuscript(
    root: Path,
    *,
    statuses: tuple[str, ...] = ("stable",),
    fmt: str = "txt",
    output: Path | None = None,
    overwrite: bool = False,
    expected_file_hash: str | None = None,
) -> dict:
    """核验当前正文归属，按阅读顺序导出；保留已有交付物与作者修改。"""
    from .manuscript_export import export_manuscript as export
    return export(root, statuses=statuses, fmt=fmt, output=output,
                  overwrite=overwrite, expected_file_hash=expected_file_hash)
