"""Entity registry helpers for strict govel structures."""

from __future__ import annotations

import re
from typing import Iterable

from .structure_store import StructureError, canonical_json, is_time_ordinal
from .project import query_policy, source_model_policy
from .source_ids import identity_prefixes

PREFIX_BY_TYPE = source_model_policy()['entityPrefixes']

CARD_ORIGIN = "card"
UNSPECIFIED_KIND = "unspecified"
LINK_FIELD = "关联ID"
_TOKEN_SPLIT = re.compile(r"[、,，;；\s]+")


def match_labels(selector: str, labels_by_id: dict[str, Iterable[str]]) -> list[str]:
    """Match already scoped identities first, then normalized display labels."""
    if selector in labels_by_id:
        return [selector]
    prefixes = '|'.join(re.escape(prefix) for prefix in set(identity_prefixes().values()))
    if re.fullmatch(f'(?:{prefixes})-{source_model_policy()["idSuffixPattern"]}',
                    selector.strip(), re.IGNORECASE):
        return []
    key = selector.strip().casefold()
    return sorted(rid for rid, labels in labels_by_id.items()
                  if key and any(key == label.strip().casefold() for label in labels))


def entity_matches(structure: dict, selector: str, *, entity_type: str | None = None,
                   chapter_id: str | None = None, include_archived: bool = True) -> list[dict]:
    scoped_ids = ({row['targetId'] for row in structure.get('references', []) if row['sourceId'] == chapter_id}
                  if chapter_id is not None else None)
    entities = {row['entityId']: row for row in structure.get('entities', [])
                if (entity_type is None or row['entityType'] == entity_type)
                and (scoped_ids is None or row['entityId'] in scoped_ids)
                and (include_archived or row.get('status') != 'archived')}
    labels = {rid: [row['name'], *row.get('aliases', [])] for rid, row in entities.items()}
    return [entities[rid] for rid in match_labels(selector, labels)]


def entity_candidates(structure: dict, entities: list[dict]) -> list[dict]:
    """Author-visible identity hints, bounded by the existing query policy."""
    policy = query_policy()
    rows = []
    for entity in entities:
        chapters = sorted({ref['sourceId'] for ref in references_to(structure, entity['entityId'])})
        summary = entity.get('summary') or ''
        row = {key: entity.get(key) for key in ('entityId', 'entityType', 'name', 'status')}
        row.update(summary=summary[:policy['searchContextCharacters']],
                   chapterIds=chapters[:policy['defaultLimit']], chapterCount=len(chapters))
        if len(row['summary']) < len(summary):
            row['summaryTruncated'] = True
        rows.append(row)
    return rows


def require_resolution_budget(result: dict) -> None:
    if len(canonical_json(result).encode()) > query_policy()['maxBytes']['detail']:
        raise StructureError('QUERY_BUDGET_EXCEEDED', 'narrow the scope or select a complete stable ID')


def resolve_entity(structure: dict, selector: str, *, entity_type: str | None = None) -> dict:
    matches = entity_matches(structure, selector, entity_type=entity_type)
    if not matches:
        raise StructureError("ENTITY_NOT_FOUND", f"entity not found: {selector}")
    if len(matches) > 1:
        details = {
            "entityIds": [item["entityId"] for item in matches],
            "candidates": entity_candidates(structure, matches),
        }
        require_resolution_budget(details)
        raise StructureError("ENTITY_AMBIGUOUS", f"entity selector is ambiguous: {selector}", details)
    return matches[0]


def references_to(structure: dict, entity_id: str) -> list[dict]:
    return [ref for ref in structure.get("references", []) if ref.get("targetId") == entity_id]


def register_entity(structure: dict, entity: dict) -> None:
    required = {"entityId", "entityType", "name"}
    missing = sorted(required - set(entity))
    if missing:
        raise StructureError("ENTITY_INVALID", "entity is missing required fields", {"missing": missing})
    item = {
        "entityId": str(entity["entityId"]),
        "entityType": str(entity["entityType"]),
        "name": str(entity["name"]).strip(),
        "aliases": sorted({str(value).strip() for value in entity.get("aliases", []) if str(value).strip()}),
        "status": entity.get("status", "active"),
        "summary": str(entity.get("summary", "")),
        "sourceRefs": list(entity.get("sourceRefs", [])),
    }
    for optional in ("absoluteTime", "timeStartId", "timeEndId", "timeOrdinal"):
        if entity.get(optional) is not None:
            item[optional] = entity[optional]
    structure.setdefault("entities", []).append(item)


def replace_entity_fields(structure: dict, entity_id: str, changes: dict, allowed: Iterable[str] = ("name", "aliases", "status", "summary", "absoluteTime", "timeOrdinal")) -> dict:
    entity = resolve_entity(structure, entity_id)
    if not isinstance(changes, dict) or not changes:
        raise StructureError("ENTITY_INVALID", "changes must be a nonempty object")
    allowed_set = set(allowed)
    rejected = sorted(set(changes) - allowed_set)
    if rejected:
        raise StructureError("ENTITY_FIELD_FORBIDDEN", "entity fields are not editable", {"fields": rejected})
    for key, value in changes.items():
        if key == "aliases":
            if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
                raise StructureError("ENTITY_INVALID", "aliases must be a list of nonempty strings")
            value = list(dict.fromkeys(v.strip() for v in value))
        elif key == "timeOrdinal":
            if value is None:
                entity.pop("timeOrdinal", None)
                continue
            if not is_time_ordinal(value):
                # 与 anchor-preview 的入口保持同一错误码：同类非法输入不得按入口分叉
                # （机制 36 审查的缺陷 9），调用方才能用统一分支处理。
                raise StructureError("INVALID_TIME_ORDINAL", "timeOrdinal must be a number, or null to clear it")
        elif not isinstance(value, str) or (key != "summary" and not value.strip()):
            raise StructureError("ENTITY_INVALID", f"{key} must be a string and cannot be blank except summary")
        entity[key] = value
    return entity
