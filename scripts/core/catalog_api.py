"""Shared CLI/MCP/HTTP descriptions for catalog preview operations."""

from __future__ import annotations

import re
from pathlib import Path

from . import catalog_commands
from .structure_store import StructureError

STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
CHANGES = {"type": "object"}
BOOLEAN = {"type": "boolean"}


def _spec(name: str, cli: str, route: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "cli": cli, "route": route, "description": description,
            "inputSchema": {"type": "object", "properties": {"projectRoot": STRING, **properties},
                            "required": ["projectRoot", *required], "additionalProperties": False},
            "annotations": {"readOnlyHint": False, "destructiveHint": False}}


CATALOG_OPERATIONS = [
    _spec("preview_create_entity", "entity-create-preview", "/api/entity/create/preview", "预览新建实体；允许同名与共享别名，分别分配稳定 ID，批准提交后登记。",
          {"entityType": {"type": "string", "enum": sorted(set(catalog_commands.PREFIX_BY_TYPE) - {"time_anchor"})}, "name": STRING, "aliases": STRINGS, "summary": STRING}, ["entityType", "name"]),
    _spec("preview_archive_entity", "entity-archive-preview", "/api/entity/archive/preview", "预览归档实体；复用正式重构事务，保留稳定 ID、事实内容与历史引用。", {"selector": STRING}, ["selector"]),
    _spec("preview_create_volume", "volume-create-preview", "/api/volume/create/preview", "预览追加卷。", {"title": STRING}, ["title"]),
    _spec("preview_update_volume", "volume-update-preview", "/api/volume/update/preview", "预览修改卷标题或状态。", {"volumeId": STRING, "changes": CHANGES}, ["volumeId", "changes"]),
    _spec("preview_archive_volume", "volume-archive-preview", "/api/volume/archive/preview", "预览归档无活动章节或弧线使用的卷。", {"volumeId": STRING}, ["volumeId"]),
    _spec("preview_create_arc", "arc-create-preview", "/api/arc/create/preview", "预览新建弧线及卷范围、父弧线和承诺。", {"title": STRING, "volumeIds": STRINGS, "kind": STRING, "parentArcId": {"type": ["string", "null"]}, "promise": STRING}, ["title", "volumeIds"]),
    _spec("preview_update_arc", "arc-update-preview", "/api/arc/update/preview", "预览修改弧线；拒绝悬空引用、父弧线环和章节卷范围冲突。", {"arcId": STRING, "changes": CHANGES}, ["arcId", "changes"]),
    _spec("preview_archive_arc", "arc-archive-preview", "/api/arc/archive/preview", "预览归档无活动章节或子弧线使用的弧线。", {"arcId": STRING}, ["arcId"]),
    _spec("preview_relink_chapter", "relink-preview", "/api/chapter/relink/preview", "预览一次替换章节所属卷、主弧线和副弧线集合；不移动或改写正文。", {"chapterId": STRING, "volumeId": STRING, "primaryArcId": STRING, "secondaryArcIds": STRINGS}, ["chapterId", "volumeId", "primaryArcId"]),
    _spec("preview_release_quarantined_chapter", "chapter-quarantine-release-preview", "/api/chapter/quarantine/release/preview", "作者复核隔离材料后预览解除 quarantine；保留章 ID、正文、位置并回到 planning_required，旧历史采用不恢复。", {"chapterId": STRING, "reason": STRING}, ["chapterId", "reason"]),
    # 作者重构（identity-safe）：稳定 ID 不变，只改显示元数据或定位符。
    _spec("preview_rename_book", "book-rename-preview", "/api/book/rename/preview", "预览改书名；project.json 与 structure.json 的 bookName 在同一事务内原子对齐。可选连同项目文件夹一起改名。", {"newName": STRING, "projectFolderTarget": STRING}, ["newName"]),
    _spec("preview_rename_unit", "unit-rename-preview", "/api/unit/rename/preview", "预览改 Unit 标题；unitId 与成员不变。", {"unitId": STRING, "newTitle": STRING}, ["unitId", "newTitle"]),
    _spec("preview_rename_chapter", "chapter-rename-preview", "/api/chapter/rename/preview", "预览改章节标题；可选按 fileNameStrategy 同步正文与推演文件名。", {"chapterId": STRING, "newTitle": STRING, "fileNameStrategy": {"type": "string", "enum": ["display_only", "sync_safe_paths"]}}, ["chapterId", "newTitle"]),
    _spec("preview_relocate_chapter_paths", "chapter-path-preview", "/api/chapter/path/preview", "预览只改正式正文定位符；字节与当前归属在同一事务内移动。", {"chapterId": STRING, "filePath": STRING}, ["chapterId", "filePath"]),
    _spec("preview_rename_entity", "entity-rename-preview", "/api/entity/rename/preview", "按完整 ID 或唯一名称／别名预览改名，可与他人同名；keepOldNameAsAlias 必须显式给出。", {"selector": STRING, "newName": STRING, "keepOldNameAsAlias": BOOLEAN}, ["selector", "newName", "keepOldNameAsAlias"]),
    _spec("preview_alias_entity", "entity-alias-preview", "/api/entity/alias/preview", "按完整 ID 或唯一名称／别名预览称呼修改；允许共用称呼，歧义拒绝生成计划。", {"selector": STRING, "aliases": STRINGS}, ["selector", "aliases"]),
    _spec("preview_restore_chapter", "chapter-restore-preview", "/api/chapter/restore/preview", "预览把归档章节恢复回同一稳定 ID；不新建章节。", {"chapterId": STRING, "status": {"type": "string", "enum": ["planned", "planning_required", "draft", "review"]}}, ["chapterId"]),
    _spec("preview_restore_entity", "entity-restore-preview", "/api/entity/restore/preview", "预览把归档实体恢复回 active；entityId 与历史引用不变。", {"selector": STRING}, ["selector"]),
    _spec("preview_reparent_unit", "unit-reparent-preview", "/api/unit/reparent/preview", "预览改 Unit 归属（卷范围 ↔ 项目范围）；unitId 与成员不变。", {"unitId": STRING, "placement": {"type": "object"}}, ["unitId", "placement"]),
    _spec("preview_set_visibility", "visibility-preview", "/api/visibility/preview", "预览卷、弧线、章节或配对规划的 POV 可见范围；实体事实需受审源提案。", {"collection": {"type": "string", "enum": sorted(catalog_commands.VISIBILITY_IDENTITIES)}, "recordId": STRING, "visibility": {"type": "string", "enum": ["public", "private"]}, "visibleTo": STRINGS}, ["collection", "recordId"]),
]


def snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def call_catalog_operation(name: str, root: Path, arguments: dict, *, actor: str) -> dict:
    spec = next((s for s in CATALOG_OPERATIONS if s["name"] == name), None)
    if spec is None:
        raise StructureError("TOOL_NOT_FOUND", f"unknown catalog operation: {name}")
    schema = spec["inputSchema"]
    supplied = {k: v for k, v in arguments.items() if k != "projectRoot"}
    missing = set(schema["required"]) - {"projectRoot"} - set(supplied)
    extra = set(supplied) - set(schema["properties"])
    if missing or extra:
        raise StructureError("ARGUMENTS_INVALID", "missing or unknown arguments", {"missing": sorted(missing), "unknown": sorted(extra)})
    for key, value in supplied.items():
        field = schema["properties"][key]
        types = field["type"] if isinstance(field["type"], list) else [field["type"]]
        valid = (("string" in types and isinstance(value, str)) or ("null" in types and value is None)
                 or ("boolean" in types and isinstance(value, bool))
                 or ("array" in types and isinstance(value, list) and all(isinstance(x, str) for x in value))
                 or ("object" in types and isinstance(value, dict)))
        if not valid or ("enum" in field and value not in field["enum"]):
            raise StructureError("ARGUMENTS_INVALID", f"invalid argument: {key}")
    return getattr(catalog_commands, name)(root, **{snake_case(k): v for k, v in supplied.items()}, actor=actor)
