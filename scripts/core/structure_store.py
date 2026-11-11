"""Strict story structure model for govel projects."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .clock import utc_now
from .jsonio import read_json, write_json
from .project import current_schema_version, source_model_policy, toolbox_manifest
from .source_ids import identity_pattern, new_id

# 唯一真源是 toolbox.manifest.json 的 projectVersionPolicy.supportedSchemaVersions；
# 禁止在本文件写字面量（AGENTS.md 真源纪律第 2 条）。
PROJECT_SCHEMA_VERSION = current_schema_version()
WORKFLOW_VERSION = str(toolbox_manifest()["workflowVersion"])
ENTITY_TYPES = set(source_model_policy()['entityPrefixes'])
ARC_ROLES = {"primary", "secondary", "intersection"}
LIFECYCLE_STATUSES = {"planned", "planning_required", "draft", "review", "stable", "stale", "archived"}
ID_PATTERNS = {kind: identity_pattern(kind) for kind in ('volume', 'arc', 'chapter', 'entity', 'relation')}


class StructureError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def is_time_ordinal(value: Any) -> bool:
    """`timeOrdinal` 是可选数值排序键（相对时间轴如 D-45 → -45、D-Day → 0）。bool 不算。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def structure_path(root: Path) -> Path:
    return root.expanduser().resolve() / ".govel" / "structure.json"


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def structure_hash(data: dict) -> str:
    copy = deepcopy(data)
    copy.pop("updatedAt", None)
    copy.pop("lastOperation", None)
    # revision 是乐观锁计数器而非结构内容：撤销会把内容恢复到早期状态并只递增
    # revision，连续撤销必须把「内容等价但 revision 不同」视为同一状态
    # （机制 36 审查的缺陷 10）；并发冲突由 expected_revision 单独把关。
    copy.pop("revision", None)
    return hashlib.sha256(canonical_json(copy).encode("utf-8")).hexdigest()


def empty_structure(project_id: str, book_name: str) -> dict:
    """Create the one current source format; chapters are materialized separately."""
    now = utc_now()
    volume_id = new_id("volume")
    arc_id = new_id("arc")
    result = {
        "schemaVersion": current_schema_version(),
        "projectId": project_id,
        "bookName": book_name,
        "revision": 0,
        "createdAt": now,
        "updatedAt": now,
        "volumes": [{
            "volumeId": volume_id,
            "ordinal": 1,
            "title": "第一卷",
            "status": "planned",
        }],
        "arcs": [{
            "arcId": arc_id,
            "title": "主线",
            "kind": "main",
            "status": "planned",
            "volumeIds": [volume_id],
            "parentArcId": None,
            "promise": "",
            "resolutionTargetChapterId": None,
        }],
        "chapters": [],
        "chapterArcLinks": [],
        "arcPhases": [],
        "entities": [],
        "pairs": [],
        "pairAssessment": {"status": "unassessed"},
        "relations": [],
        "references": [],
        "aliases": [],
        "migrations": [],
        "lastOperation": None,
    }
    from .source_model import content_defaults
    result.update(content_defaults())
    return result


def _duplicates(items: list[dict], field: str) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        value = str(item.get(field, ""))
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def validate_structure(data: dict) -> None:
    if not isinstance(data, dict):
        raise StructureError('STRUCTURE_INVALID', 'structure must be an object')
    errors: list[dict] = []
    from .source_model import require_current_source
    require_current_source(data)
    collections = {
        "volumes": ("volumeId", "volume"),
        "arcs": ("arcId", "arc"),
        "chapters": ("chapterId", "chapter"),
        "entities": ("entityId", "entity"),
        "relations": ("relationId", "relation"),
    }
    for name in (*collections, 'pairs', 'references', 'chapterArcLinks', 'arcPhases', 'aliases', 'migrations'):
        items = data.get(name, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            errors.append({'code': 'COLLECTION_TYPE', 'path': name})
    if errors:
        raise StructureError('STRUCTURE_INVALID', 'structure validation failed', {'errors': errors})
    # Shape validation precedes ownership inspection so malformed collections are
    # reported as diagnostics. Chapter-local binding damage remains isolated.
    from .prose_binding import ownership_fatal_errors
    errors.extend(ownership_fatal_errors(data))
    all_ids: dict[str, str] = {}
    for name, (field, kind) in collections.items():
        items = data.get(name, [])
        if not isinstance(items, list):
            errors.append({"code": "COLLECTION_TYPE", "path": name})
            continue
        for duplicate in _duplicates(items, field):
            errors.append({"code": "DUPLICATE_ID", "path": name, "id": duplicate})
        pattern = ID_PATTERNS[kind]
        for item in items:
            item_id = item.get(field)
            if not isinstance(item_id, str) or not pattern.fullmatch(item_id):
                detail = {"code": "INVALID_ID", "path": name, "id": item_id, "expectedPattern": pattern.pattern}
                if name == 'entities':
                    from .entity_registry import PREFIX_BY_TYPE
                    entity_type = item.get('entityType')
                    detail.update(entityType=entity_type, generatedPrefix=PREFIX_BY_TYPE.get(entity_type) if isinstance(entity_type, str) else None,
                                  guidance='使用实体创建预览自动分配 ID；源批量新增可省略 ID 或使用 $临时引用。')
                errors.append(detail)
                continue
            if item_id in all_ids:
                errors.append({"code": "GLOBAL_ID_COLLISION", "id": item_id, "paths": [all_ids[item_id], name]})
            all_ids[item_id] = name

    if errors:
        raise StructureError('STRUCTURE_INVALID', 'structure validation failed', {'errors': errors})

    volumes = {v.get("volumeId") for v in data.get("volumes", [])}
    arcs = {a.get("arcId") for a in data.get("arcs", [])}
    chapters = {c.get("chapterId") for c in data.get("chapters", [])}
    entities = {e.get("entityId") for e in data.get("entities", [])}
    if not isinstance(data.get("pairs", []), list):
        errors.append({"code": "COLLECTION_TYPE", "path": "pairs"})
    from .pair_classification import validate_pairs
    errors.extend(validate_pairs(data))
    if errors:
        raise StructureError('STRUCTURE_INVALID', 'structure validation failed', {'errors': errors})

    ordinals: dict[int, str] = {}
    for chapter in data.get("chapters", []):
        chapter_id = chapter.get("chapterId")
        ordinal = chapter.get("displayOrdinal")
        archived = chapter.get("status") == "archived"
        if not isinstance(ordinal, int) or ordinal < 1:
            if not archived:
                errors.append({"code": "INVALID_ORDINAL", "chapterId": chapter_id})
        elif archived:
            pass  # 归档章不占显示编号，不参与唯一性检查
        elif ordinal in ordinals:
            errors.append({"code": "DUPLICATE_ORDINAL", "ordinal": ordinal, "chapters": [ordinals[ordinal], chapter_id]})
        else:
            ordinals[ordinal] = chapter_id
        # schema 3: placement is the only structural anchor. Legacy volumeId/unitId
        # must never coexist with it — no second source of truth for where a chapter lives.
        from .source_placement import placement_error, placement_volume_id
        if "volumeId" in chapter:
            errors.append({"code": "CHAPTER_LEGACY_VOLUME_FORBIDDEN", "chapterId": chapter_id})
        if "unitId" in chapter:
            errors.append({"code": "CHAPTER_LEGACY_UNIT_FORBIDDEN", "chapterId": chapter_id})
        if 'outlinePath' in chapter:
            errors.append({'code': 'CHAPTER_OUTLINE_LOCATOR_FORBIDDEN', 'chapterId': chapter_id,
                           'message': 'chapter plans are source fragments, not maintained Markdown files'})
        reason = placement_error(chapter.get("placement"))
        if reason is not None:
            errors.append({"code": "CHAPTER_PLACEMENT_INVALID", "chapterId": chapter_id, "reason": reason})
        else:
            volume_id = placement_volume_id(chapter.get("placement"))
            if volume_id is not None and volume_id not in volumes:
                errors.append({"code": "DANGLING_VOLUME", "chapterId": chapter_id, "volumeId": volume_id})
        if chapter.get("status") not in LIFECYCLE_STATUSES:
            errors.append({"code": "INVALID_STATUS", "chapterId": chapter_id})
        if not str(chapter.get("filePath", "")).strip():
            errors.append({"code": "MISSING_FILE_PATH", "chapterId": chapter_id})

    links_seen: set[tuple[str, str]] = set()
    primary_count = {chapter_id: 0 for chapter_id in chapters}
    for link in data.get("chapterArcLinks", []):
        chapter_id, arc_id = link.get("chapterId"), link.get("arcId")
        key = (chapter_id, arc_id)
        if key in links_seen:
            errors.append({"code": "DUPLICATE_ARC_LINK", "chapterId": chapter_id, "arcId": arc_id})
        links_seen.add(key)
        if chapter_id not in chapters:
            errors.append({"code": "DANGLING_CHAPTER_LINK", "chapterId": chapter_id})
        if arc_id not in arcs:
            errors.append({"code": "DANGLING_ARC_LINK", "arcId": arc_id})
        if link.get("role") not in ARC_ROLES:
            errors.append({"code": "INVALID_ARC_ROLE", "chapterId": chapter_id, "arcId": arc_id})
        if link.get("role") == "primary" and chapter_id in primary_count:
            primary_count[chapter_id] += 1
    for chapter_id, count in primary_count.items():
        if count != 1:
            errors.append({"code": "PRIMARY_ARC_REQUIRED", "chapterId": chapter_id, "count": count})

    names: dict[str, str] = {}
    for entity in data.get("entities", []):
        entity_id = entity.get("entityId")
        if entity.get("entityType") not in ENTITY_TYPES:
            errors.append({"code": "INVALID_ENTITY_TYPE", "entityId": entity_id})
        elif not identity_pattern(entity['entityType']).fullmatch(entity_id):
            errors.append({'code': 'ENTITY_ID_TYPE_MISMATCH', 'entityId': entity_id,
                           'entityType': entity['entityType']})
        ordinal = entity.get("timeOrdinal")
        if ordinal is not None and not is_time_ordinal(ordinal):
            errors.append({"code": "INVALID_TIME_ORDINAL", "entityId": entity_id, "value": ordinal})
        for label in [entity.get("name", ""), *entity.get("aliases", [])]:
            normalized = str(label).strip().casefold()
            if not normalized:
                continue
            if normalized in names and names[normalized] != entity_id:
                errors.append({"code": "ENTITY_NAME_COLLISION", "name": label, "entities": [names[normalized], entity_id]})
            names[normalized] = entity_id

    from .planning_candidates import validate_candidates
    errors.extend(validate_candidates(data))
    from .author_decisions import validate_decisions
    errors.extend(validate_decisions(data))
    from .work_voice_profile import validate_voice_profile
    errors.extend(validate_voice_profile(data))
    from .source_validation import validate_source_content
    errors.extend(validate_source_content(data))

    for relation in data.get("relations", []):
        for field in ("fromEntity", "toEntity"):
            if relation.get(field) not in entities:
                errors.append({"code": "DANGLING_RELATION", "relationId": relation.get("relationId"), field: relation.get(field)})
    for reference in data.get("references", []):
        if reference.get("sourceId") not in all_ids or reference.get("targetId") not in all_ids:
            errors.append({"code": "DANGLING_REFERENCE", "reference": reference})
            continue
        # References express chapter → entity scope; consumers trust this direction.
        if reference.get("sourceId") not in chapters:
            errors.append({"code": "REFERENCE_SOURCE_NOT_CHAPTER", "reference": reference})
        if reference.get("targetId") not in entities:
            errors.append({"code": "REFERENCE_TARGET_NOT_ENTITY", "reference": reference})

    if errors:
        raise StructureError("STRUCTURE_INVALID", "structure validation failed", {"errors": errors})


def load_structure(root: Path) -> dict:
    from .data_provider import require_project_provider
    require_project_provider(root)
    path = structure_path(root)
    if not path.is_file():
        raise StructureError("STRUCTURE_NOT_INITIALIZED", "missing .govel/structure.json")
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise StructureError("STRUCTURE_INVALID", f"cannot read structure: {exc}") from exc
    validate_structure(data)
    return data


def save_structure(root: Path, data: dict, *, expected_revision: int | None = None,
                   _binding_authority: Any = None, _initializer: Any = None) -> dict:
    path = structure_path(root)
    from .source_model import require_current_source
    require_current_source(data)
    current = load_structure(root) if path.is_file() else None
    if current is not None and expected_revision is not None:
        if current.get("revision") != expected_revision:
            raise StructureError("REVISION_CONFLICT", "structure changed after preview", {
                "expectedRevision": expected_revision,
                "actualRevision": current.get("revision"),
            })
    if current is None:
        # Creation requires initializer authority. A valid digest alone cannot
        # authorize a new project or manufacture managed prose ownership.
        from .prose_binding import is_initializer_authority
        if not is_initializer_authority(_initializer):
            raise StructureError('OWNERSHIP_UNAUTHORIZED_MUTATION',
                                 'generic save_structure cannot create an ownership-aware project; '
                                 'initial current-binding structures come from a sanctioned initializer '
                                 '(project init quick-init, importer.create_project_from_candidate)',
                                 {'schemaVersion': data.get('schemaVersion')})
    if current is not None:
        # Option B（契约 §5.3）：generic save_structure 不是 ownership transition API。
        # 任一侧是 ownership-aware schema 时，受保护字段（filePath/proseBinding）相对
        # 磁盘现状的任何变更都必须由本模块内部 capability（prose_binding 的 formal
        # transition）授权，否则拒绝 OWNERSHIP_UNAUTHORIZED_MUTATION。
        from .prose_binding import enforce_writer_boundary
        enforce_writer_boundary(current, data, authority=_binding_authority, root=root)
        from .canon_guard import require_canon_proposal
        require_canon_proposal(current, data, {'operation': 'direct_structure_write', 'parameters': {}})
        from .author_decisions import require_direct_decision_write
        require_direct_decision_write(current, data)
    validate_structure(data)
    data = deepcopy(data)
    data["updatedAt"] = utc_now()
    write_json(path, data)
    return data


def ordered_chapters(data: dict, *, include_archived: bool = False) -> list[dict]:
    chapters = data.get("chapters", [])
    if not include_archived:
        chapters = [chapter for chapter in chapters if chapter.get("status") != "archived"]
    return sorted(chapters, key=lambda chapter: (chapter["displayOrdinal"], chapter["chapterId"]))


def derive_display_codes(data: dict) -> None:
    active = [chapter for chapter in data.get("chapters", []) if chapter.get("status") != "archived"]
    archived = [chapter for chapter in data.get("chapters", []) if chapter.get("status") == "archived"]
    for ordinal, chapter in enumerate(active, 1):
        chapter["displayOrdinal"] = ordinal
        chapter["displayCode"] = f"CH{ordinal:03d}"
    for chapter in archived:
        chapter.setdefault("displayCode", "")
