"""Entity registry helpers for strict govel structures."""

from __future__ import annotations

import re
from typing import Iterable

from .structure_store import StructureError, is_time_ordinal

from .project import source_model_policy

PREFIX_BY_TYPE = source_model_policy()['entityPrefixes']

CARD_ORIGIN = "card"
UNSPECIFIED_KIND = "unspecified"
LINK_FIELD = "关联ID"
_TOKEN_SPLIT = re.compile(r"[、,，;；\s]+")


def resolve_entity(structure: dict, selector: str) -> dict:
    selector_key = selector.strip().casefold()
    matches = []
    for entity in structure.get("entities", []):
        labels = [entity.get("entityId", ""), entity.get("name", ""), *entity.get("aliases", [])]
        if selector_key in {str(label).strip().casefold() for label in labels}:
            matches.append(entity)
    if not matches:
        raise StructureError("ENTITY_NOT_FOUND", f"entity not found: {selector}")
    if len(matches) > 1:
        raise StructureError("ENTITY_AMBIGUOUS", f"entity selector is ambiguous: {selector}", {
            "entityIds": [item["entityId"] for item in matches],
        })
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
