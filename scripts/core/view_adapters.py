"""Schema-aware chapter view adapters and the bounded workspace summary.

旧前端直接读 `chapter.volumeId`（schema 1/2/4 legacy 布局字段），schema 3/5 的
placement 章节因此被误读成「未分卷」（FRONTEND_CURRENT_REALITY §placement）。
本模块是 UI 消费章节结构时的唯一投影层：

- `chapter_view()` 统一返回 placement / derivedVolumeId / unit 成员 / 弧线链接 /
  ownership 摘要，UI 不再自行猜 schema 字段。
- `project_summary()` 是有界、可缓存的首页/轮询载荷：**不读任何正文字节**，字数
  来自 stat 键控缓存（路径 + mtime_ns + size 未变即复用），重复轮询只做 stat。
  `/api/overview`（全书扫正文）保留给需要逐章 freshness 的旧调用方。
- `plan_impact_view()` 把「无影响证据」如实表达为 unknown，禁止 `{} → 零影响`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jsonio import read_json, write_json
from .paths import safe_project_path
from .source_placement import chapter_placement, chapter_volume_id
from .static_cache import signature
from .structure_store import StructureError, load_structure, ordered_chapters, structure_hash

SUMMARY_COUNTS_SCHEMA_VERSION = 1
COUNTS_CACHE_PATH = ".gnovel/workbench/summary-counts.json"


def _units_index(structure: dict[str, Any]) -> dict[str, list[str]]:
    membership: dict[str, list[str]] = {}
    for unit in structure.get("units", []):
        unit_id = unit.get("unitId")
        if not isinstance(unit_id, str):
            continue
        for member in unit.get("members", []):
            if member.get("memberType") == "chapter" and isinstance(member.get("memberId"), str):
                membership.setdefault(member["memberId"], []).append(unit_id)
    return membership


def chapter_view(structure: dict[str, Any], chapter: dict[str, Any], *, units: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """One schema-agnostic chapter row for UI consumption. Never exposes volumeId."""
    if units is None:
        units = _units_index(structure)
    schema_version = structure.get("schemaVersion")
    placement = chapter_placement(chapter, schema_version)
    if placement is not None and placement.get("scope") not in structure.get("placementScopes", ("volume", "project")):
        # 未知 placement 形状如实带出，由 UI 显示为异常，而不是静默归到某一栏。
        raise StructureError("PLACEMENT_INVALID", "chapter carries an unknown placement shape",
                             {"chapterId": chapter.get("chapterId"), "placement": placement})
    arc_links = [link for link in structure.get("chapterArcLinks", []) if link.get("chapterId") == chapter.get("chapterId")]
    chapter_id = chapter.get("chapterId")
    entity_ids = sorted({ref.get("targetId") for ref in structure.get("references", [])
                         if ref.get("sourceId") == chapter_id and ref.get("targetId")})
    binding = chapter.get("proseBinding")
    return {
        "chapterId": chapter_id,
        "title": chapter.get("title", ""),
        "displayCode": chapter.get("displayCode", ""),
        "displayOrdinal": chapter.get("displayOrdinal"),
        "status": chapter.get("status", ""),
        "placement": placement,
        "derivedVolumeId": chapter_volume_id(chapter, schema_version),
        "unitMembership": sorted((units or {}).get(chapter_id, [])),
        "arcLinks": [{"arcId": link.get("arcId"), "role": link.get("role")} for link in arc_links],
        "entityIds": entity_ids,
        "filePath": chapter.get("filePath"),
        "ownershipSummary": {
            "proseBindingState": binding.get("state") if isinstance(binding, dict) else None,
        },
    }


def project_view(root: Path) -> dict[str, Any]:
    """Full structure projection (volumes/arcs/units/chapter view rows), no prose bytes."""
    structure = load_structure(root)
    units = _units_index(structure)
    return {
        "projectId": structure.get("projectId"),
        "bookName": structure.get("bookName", ""),
        "revision": structure.get("revision"),
        "structureHash": structure_hash(structure),
        "schemaVersion": structure.get("schemaVersion"),
        "volumes": structure.get("volumes", []),
        "arcs": structure.get("arcs", []),
        "units": [
            {"unitId": unit.get("unitId"), "title": unit.get("title", ""), "placement": unit.get("placement"),
             "members": unit.get("members", [])}
            for unit in structure.get("units", [])
        ],
        "chapterArcLinks": structure.get("chapterArcLinks", []),
        "chapters": [chapter_view(structure, chapter, units=units) for chapter in ordered_chapters(structure, include_archived=True)],
    }


def _load_counts_cache(root: Path) -> dict[str, Any]:
    path = safe_project_path(root, COUNTS_CACHE_PATH)
    if not path.is_file():
        return {"schemaVersion": SUMMARY_COUNTS_SCHEMA_VERSION, "files": {}}
    try:
        cache = read_json(path)
    except (OSError, ValueError):
        return {"schemaVersion": SUMMARY_COUNTS_SCHEMA_VERSION, "files": {}}
    if not isinstance(cache, dict) or cache.get("schemaVersion") != SUMMARY_COUNTS_SCHEMA_VERSION or not isinstance(cache.get("files"), dict):
        return {"schemaVersion": SUMMARY_COUNTS_SCHEMA_VERSION, "files": {}}
    return cache


def _han_counts_bounded(root: Path, chapters: list[dict[str, Any]]) -> tuple[dict[str, int | None], int]:
    """Cache only counts observed from an unchanged file object and byte version."""
    from .commands import chinese_char_count
    from .write_sets import guard_observation

    cache = _load_counts_cache(root)
    files: dict[str, Any] = cache.get("files", {})
    counts: dict[str, int | None] = {}
    reads = 0
    dirty = False
    for chapter in chapters:
        relative = str(chapter.get("filePath", "") or "")
        if not relative:
            counts[chapter["chapterId"]] = None
            continue
        try:
            path = safe_project_path(root, relative)
            observed_signature = signature(path)
        except (OSError, ValueError):  # 路径守卫拒绝/文件缺失都按无字数处理
            counts[chapter["chapterId"]] = None
            continue
        key = list(observed_signature) if observed_signature is not None else None
        row = files.get(relative)
        if key is not None and isinstance(row, dict) and row.get("fileIdentity") == key and type(row.get("hanCharCount")) is int:
            counts[chapter["chapterId"]] = row["hanCharCount"]
            continue
        count = None
        try:
            state, raw = guard_observation(path)
            reads += 1
            if (raw is not None and observed_signature is not None
                    and (state['dev'], state['ino']) == tuple(observed_signature[:2])
                    and len(raw) == observed_signature[2] and signature(path) == observed_signature):
                count = chinese_char_count(raw.decode('utf-8'))
        except (OSError, ValueError):
            pass
        counts[chapter["chapterId"]] = count
        if count is not None:
            files[relative] = {"fileIdentity": key, "hanCharCount": count}
            dirty = True
        elif relative in files:
            del files[relative]
            dirty = True
    if dirty:
        try:
            write_json(safe_project_path(root, COUNTS_CACHE_PATH), {**cache, "files": files})
        except OSError:
            pass  # 缓存写失败不影响正确性：下次调用重算
    return counts, reads


def project_summary(root: Path) -> dict[str, Any]:
    """Bounded, cacheable homepage payload. Polling this never scans book prose."""
    root = root.expanduser().resolve()
    view = project_view(root)
    structure = load_structure(root)
    counts, reads = _han_counts_bounded(root, ordered_chapters(structure, include_archived=True))
    rows = []
    total_han = 0
    for chapter_view_row in view["chapters"]:
        count = counts.get(chapter_view_row.get("chapterId"))
        if count is not None:
            total_han += count
        rows.append({**chapter_view_row, "hanCharCount": count})
    stable = [row for row in rows if row.get("status") == "stable"]
    return {
        "ok": True,
        "code": "PROJECT_SUMMARY",
        "projectId": view["projectId"],
        "bookName": view["bookName"],
        "revision": view["revision"],
        "structureHash": view["structureHash"],
        "schemaVersion": view["schemaVersion"],
        "counts": {
            "volumes": len(view["volumes"]),
            "arcs": len(view["arcs"]),
            "chapters": len(rows),
            "entities": len(structure.get("entities", [])),
            "relations": len(structure.get("relations", [])),
            "hanCharacters": total_han,
        },
        "latestStableChapter": stable[-1] if stable else None,
        "chapters": rows,
        "volumes": view["volumes"],
        "arcs": view["arcs"],
        "units": view["units"],
        "chapterArcLinks": view["chapterArcLinks"],
        "countsCache": {"filesReadThisCall": reads, "bounded": True},
    }


def plan_impact_view(plan: dict[str, Any]) -> dict[str, Any]:
    """Honest impact projection: empty/missing impact is `unknown`, never zero (§6)."""
    impact = plan.get("impact")
    known = isinstance(impact, dict) and len(impact) > 0
    if not known:
        return {"known": False,
                "note": "影响信息尚未完整取得；这份计划类型不携带结构影响证据",
                "renumberedChapters": None, "referenceCount": None, "staleCandidateCount": None}
    return {
        "known": True,
        "renumberedChapters": impact.get("renumberedChapters", []),
        "referenceCount": impact.get("referenceCount"),
        "staleCandidateCount": len(impact.get("staleCandidates", [])) if isinstance(impact.get("staleCandidates"), list) else None,
        "redirectedReferenceCount": impact.get("redirectedReferenceCount"),
        "redirectedRelationCount": impact.get("redirectedRelationCount"),
        "raw": impact,
    }
