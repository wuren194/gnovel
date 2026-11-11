"""Preview-first maintenance of entities, volumes, arcs and chapter membership."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .entity_registry import PREFIX_BY_TYPE, register_entity
from .structure_commands import _base_plan, _write_plan
from .structure_store import LIFECYCLE_STATUSES, StructureError, load_structure, new_id, validate_structure


def _text(value, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise StructureError("FIELD_INVALID", f"{field} must be a {'possibly empty ' if empty else 'nonempty '}string")
    return value.strip()


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise StructureError("FIELD_INVALID", f"{field} must be a list of nonempty strings")
    return list(dict.fromkeys(v.strip() for v in value))


def _find(structure: dict, kind: str, item_id: str, *, active: bool = False) -> dict:
    item = next((x for x in structure[kind + "s"] if x[kind + "Id"] == item_id), None)
    if item is None:
        raise StructureError(f"{kind.upper()}_NOT_FOUND", f"unknown {kind}: {item_id}")
    if active and item.get("status") == "archived":
        raise StructureError("ARCHIVED_TARGET", f"{kind} is archived: {item_id}")
    return item


def _affected(structure: dict, kind: str, item_id: str) -> list[str]:
    if kind == "volume":
        from .source_placement import chapter_volume_id
        schema_version = structure.get("schemaVersion")
        ids = {c["chapterId"] for c in structure["chapters"] if chapter_volume_id(c, schema_version) == item_id}
    elif kind == "arc":
        ids = {x["chapterId"] for x in structure["chapterArcLinks"] if x["arcId"] == item_id}
    elif kind == "entity":
        chapter_ids = {c["chapterId"] for c in structure["chapters"]}
        ids = {ref["sourceId"] for ref in structure.get("references", []) if ref.get("targetId") == item_id and ref.get("sourceId") in chapter_ids}
    else:
        ids = {item_id}
    return sorted(ids)


def _plan(root: Path, structure: dict, proposed: dict, operation: str, parameters: dict, kind: str, item_id: str, actor: str,
          *, impact: dict | None = None) -> dict:
    validate_structure(proposed)
    if impact is None:
        affected = _affected(structure, kind, item_id)
        impact = {kind + "Id": item_id, "staleCandidates": affected, "approvalInvalidations": affected,
                  "manualReview": "脚本只可重建带 `> GENERATED:` 标记的文件；不带标记的文件永不写入，须人工整理。"}
    return _write_plan(root.resolve(), _base_plan(structure, operation=operation, actor=actor, parameters=parameters, impact=impact, proposed=proposed))


def apply_create_entity(proposed: dict, *, entity_type: str, name: str, aliases: list[str] | None = None, summary: str = "") -> str:
    """Register a new entity in ``proposed``; shared by the single preview and structure batches."""
    if entity_type not in PREFIX_BY_TYPE or entity_type == "time_anchor":
        raise StructureError("ENTITY_TYPE_INVALID", "use a supported entity type; time anchors use preview_create_time_anchor")
    entity_id = new_id(entity_type)
    register_entity(proposed, {"entityId": entity_id, "entityType": entity_type, "name": _text(name, "name"),
                              "aliases": _strings(aliases if aliases is not None else [], "aliases"), "summary": _text(summary, "summary", empty=True)})
    return entity_id


def preview_create_entity(root: Path, *, entity_type: str, name: str, aliases: list[str] | None = None, summary: str = "", actor: str = "human_ui") -> dict:
    structure = load_structure(root)
    proposed = deepcopy(structure)
    entity_id = apply_create_entity(proposed, entity_type=entity_type, name=name, aliases=aliases, summary=summary)
    return _plan(root, structure, proposed, "create_entity", {"entityId": entity_id, "entityType": entity_type, "name": name}, "entity", entity_id, actor)


def preview_archive_entity(root: Path, *, selector: str, actor: str = "human_ui") -> dict:
    from .refactor import preview_archive_entity as preview
    return preview(root, selector=selector, actor=actor)


def _validate_catalog(structure: dict) -> None:
    for kind in ("volume", "arc"):
        for item in structure[kind + "s"]:
            _text(item["title"], "title")
            if item.get("status") not in LIFECYCLE_STATUSES:
                raise StructureError("STATUS_INVALID", f"invalid {kind} status")
    arcs = {a["arcId"]: a for a in structure["arcs"]}
    for arc in arcs.values():
        for volume_id in _strings(arc.get("volumeIds", []), "volumeIds"):
            _find(structure, "volume", volume_id, active=arc.get("status") != "archived")
        target = arc.get("resolutionTargetChapterId")
        if target is not None:
            _find(structure, "chapter", target, active=arc.get("status") != "archived")
        seen = {arc["arcId"]}
        parent_id = arc.get("parentArcId")
        while parent_id is not None:
            if parent_id in seen:
                raise StructureError("ARC_PARENT_CYCLE", "arc parent links must be acyclic")
            seen.add(parent_id)
            parent = _find(structure, "arc", parent_id, active=arc.get("status") != "archived")
            parent_id = parent.get("parentArcId")
    for chapter in structure["chapters"]:
        if chapter.get("status") != "archived":
            from .source_placement import chapter_volume_id
            volume_id = chapter_volume_id(chapter, structure.get("schemaVersion"))
            if volume_id is not None:
                _find(structure, "volume", volume_id, active=True)
            for link in structure["chapterArcLinks"]:
                if link["chapterId"] == chapter["chapterId"]:
                    arc = _find(structure, "arc", link["arcId"], active=True)
                    arc_volumes = arc.get("volumeIds")
                    if arc_volumes and (volume_id is None or volume_id not in arc_volumes):
                        raise StructureError("ARC_VOLUME_MISMATCH", "chapter volume is outside its arc scope", {"chapterId": chapter["chapterId"], "arcId": arc["arcId"]})


def preview_create_volume(root: Path, *, title: str, actor: str = "human_ui") -> dict:
    structure = load_structure(root)
    proposed = deepcopy(structure)
    item_id = new_id("volume")
    ordinal = max((v.get("ordinal", 0) for v in structure["volumes"]), default=0) + 1
    proposed["volumes"].append({"volumeId": item_id, "ordinal": ordinal, "title": _text(title, "title"), "status": "planned"})
    _validate_catalog(proposed)
    return _plan(root, structure, proposed, "create_volume", {"volumeId": item_id, "title": title}, "volume", item_id, actor)


def preview_create_arc(root: Path, *, title: str, volume_ids: list[str], kind: str = "main", parent_arc_id: str | None = None, promise: str = "", actor: str = "human_ui") -> dict:
    structure = load_structure(root)
    proposed = deepcopy(structure)
    item_id = new_id("arc")
    proposed["arcs"].append({"arcId": item_id, "title": _text(title, "title"), "kind": _text(kind, "kind"),
                             "status": "planned", "volumeIds": _strings(volume_ids, "volumeIds"), "parentArcId": parent_arc_id,
                             "promise": _text(promise, "promise", empty=True), "resolutionTargetChapterId": None})
    _validate_catalog(proposed)
    return _plan(root, structure, proposed, "create_arc", {"arcId": item_id, "title": title, "volumeIds": volume_ids}, "arc", item_id, actor)


def _update_catalog(root: Path, kind: str, item_id: str, changes: dict, actor: str, *, archive: bool = False) -> dict:
    allowed = {"title", "status"} if kind == "volume" else {"title", "status", "kind", "volumeIds", "parentArcId", "promise", "resolutionTargetChapterId"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise StructureError("FIELD_FORBIDDEN", f"editable {kind} fields: {', '.join(sorted(allowed))}")
    structure = load_structure(root)
    proposed = deepcopy(structure)
    item = _find(proposed, kind, item_id)
    for key, value in changes.items():
        if key == "volumeIds":
            value = _strings(value, key)
        elif key in {"parentArcId", "resolutionTargetChapterId"}:
            value = _text(value, key) if value is not None else None
        else:
            value = _text(value, key, empty=key == "promise")
        item[key] = value
    _validate_catalog(proposed)
    return _plan(root, structure, proposed, ("archive_" if archive else "update_") + kind, {kind + "Id": item_id, "changes": changes}, kind, item_id, actor)


def preview_update_volume(root: Path, *, volume_id: str, changes: dict, actor: str = "human_ui") -> dict:
    return _update_catalog(root, "volume", volume_id, changes, actor)


def preview_archive_volume(root: Path, *, volume_id: str, actor: str = "human_ui") -> dict:
    return _update_catalog(root, "volume", volume_id, {"status": "archived"}, actor, archive=True)


def preview_update_arc(root: Path, *, arc_id: str, changes: dict, actor: str = "human_ui") -> dict:
    return _update_catalog(root, "arc", arc_id, changes, actor)


def preview_archive_arc(root: Path, *, arc_id: str, actor: str = "human_ui") -> dict:
    return _update_catalog(root, "arc", arc_id, {"status": "archived"}, actor, archive=True)


def preview_relink_chapter(root: Path, *, chapter_id: str, primary_arc_id: str, volume_id: str | None = None, placement: dict | None = None, secondary_arc_ids: list[str] | None = None, actor: str = "human_ui") -> dict:
    from .source_model import require_current_source
    from .source_placement import placement_error, placement_from_volume
    structure = load_structure(root)
    require_current_source(structure)
    if (placement is None) == (volume_id is None):
        raise StructureError("PLACEMENT_ARGUMENT_INVALID", "provide exactly one of volumeId (compat) or placement")
    resolved = placement if placement is not None else placement_from_volume(volume_id)
    reason = placement_error(resolved)
    if reason is not None:
        raise StructureError("PLACEMENT_INVALID", f"chapter placement is not a legal shape: {reason}")
    proposed = deepcopy(structure)
    chapter = _find(proposed, "chapter", chapter_id, active=True)
    secondary = _strings(secondary_arc_ids if secondary_arc_ids is not None else [], "secondaryArcIds")
    if primary_arc_id in secondary:
        raise StructureError("ARC_ROLE_CONFLICT", "primary arc cannot also be secondary")
    chapter.pop("volumeId", None)
    chapter.pop("unitId", None)
    chapter["placement"] = deepcopy(resolved)
    proposed["chapterArcLinks"] = [x for x in proposed["chapterArcLinks"] if x["chapterId"] != chapter_id]
    for arc_id in [primary_arc_id, *secondary]:
        primary = arc_id == primary_arc_id
        proposed["chapterArcLinks"].append({"chapterId": chapter_id, "arcId": arc_id, "role": "primary" if primary else "secondary", "weight": 1.0 if primary else 0.5, "phaseId": None, "notes": ""})
    _validate_catalog(proposed)
    parameters = {"chapterId": chapter_id, "placement": chapter.get("placement", chapter.get("volumeId")),
                  "primaryArcId": primary_arc_id, "secondaryArcIds": secondary}
    return _plan(root, structure, proposed, "relink_chapter", parameters, "chapter", chapter_id, actor)


VISIBILITY_IDENTITIES = {
    "pairs": ("pairId",), "volumes": ("volumeId",),
    "arcs": ("arcId",), "chapters": ("chapterId",),
}


def preview_set_visibility(root: Path, *, collection: str, record_id: str, visibility: str | None = None, visible_to: list[str] | None = None, actor: str = "human_ui") -> dict:
    if collection not in VISIBILITY_IDENTITIES:
        raise StructureError("VISIBILITY_COLLECTION_INVALID", "collection has no supported stable identity")
    if (visibility is None) == (visible_to is None):
        raise StructureError("VISIBILITY_SCOPE_REQUIRED", "provide exactly one of visibility or visibleTo")
    if visibility is not None and visibility not in {"public", "private"}:
        raise StructureError("VISIBILITY_SCOPE_INVALID", "visibility must be public or private")
    structure = load_structure(root)
    proposed = deepcopy(structure)
    matches = [row for row in proposed.get(collection, []) if record_id in {row.get(key) for key in VISIBILITY_IDENTITIES[collection]}]
    if len(matches) != 1:
        raise StructureError("VISIBILITY_TARGET_INVALID", "stable identity must match exactly one record")
    if visible_to is not None:
        visible_to = _strings(visible_to, "visibleTo")
        characters = {e["entityId"] for e in structure["entities"] if e["entityType"] == "character"}
        if set(visible_to) - characters:
            raise StructureError("CHARACTER_NOT_FOUND", "visibleTo requires existing character IDs")
    target = matches[0]
    for key in ("visibleTo", "visibility", "povCharacter"):
        target.pop(key, None)
    if visibility == "public":
        target["visibility"] = "public"
    else:
        target["visibleTo"] = visible_to or []
    validate_structure(proposed)
    affected = sorted(c["chapterId"] for c in structure["chapters"] if c.get("status") != "archived")
    impact = {"staleCandidates": affected, "approvalInvalidations": affected,
              "manualReview": "可见性是作者对整条记录的声明，不是语义脱敏；请复核所有受影响 POV 上下文。"}
    parameters = {"collection": collection, "recordId": record_id, "visibility": visibility, "visibleTo": visible_to}
    return _write_plan(root.resolve(), _base_plan(structure, operation="set_visibility", actor=actor, parameters=parameters, impact=impact, proposed=proposed))


# ---------------------------------------------------------------------------
# Authoring refactor surface
#
# These are thin wrappers: the contract, plan shape and path safety all live in
# `refactor.py`. They exist here so the refactor operations flow through the same
# CLI / MCP / HTTP catalog plumbing as every other structure preview.
# ---------------------------------------------------------------------------

def preview_rename_book(root: Path, *, new_name: str, project_folder_target: str | None = None,
                        actor: str = "human_ui") -> dict:
    from .refactor import preview_rename_book as _preview
    return _preview(root, new_name=new_name, project_folder_target=project_folder_target, actor=actor)


def preview_rename_unit(root: Path, *, unit_id: str, new_title: str, actor: str = "human_ui") -> dict:
    from .refactor import preview_rename_unit as _preview
    return _preview(root, unit_id=unit_id, new_title=new_title, actor=actor)


def preview_rename_chapter(root: Path, *, chapter_id: str, new_title: str,
                           file_name_strategy: str = "display_only", actor: str = "human_ui") -> dict:
    from .refactor import preview_rename_chapter as _preview
    return _preview(root, chapter_id=chapter_id, new_title=new_title,
                    file_name_strategy=file_name_strategy, actor=actor)


def preview_relocate_chapter_paths(root: Path, *, chapter_id: str, file_path: str | None = None,
                                   actor: str = "human_ui") -> dict:
    from .refactor import preview_relocate_chapter_paths as _preview
    return _preview(root, chapter_id=chapter_id, file_path=file_path, actor=actor)


def preview_rename_entity(root: Path, *, selector: str, new_name: str, keep_old_name_as_alias: bool,
                          actor: str = "human_ui") -> dict:
    from .refactor import preview_rename_entity as _preview
    return _preview(root, selector=selector, new_name=new_name,
                    keep_old_name_as_alias=keep_old_name_as_alias, actor=actor)


def preview_alias_entity(root: Path, *, selector: str, aliases: list[str], actor: str = "human_ui") -> dict:
    from .refactor import preview_alias_entity as _preview
    return _preview(root, selector=selector, aliases=aliases, actor=actor)


def preview_restore_chapter(root: Path, *, chapter_id: str, status: str = "planned",
                            actor: str = "human_ui") -> dict:
    from .refactor import preview_restore_chapter as _preview
    return _preview(root, chapter_id=chapter_id, status=status, actor=actor)


def preview_restore_entity(root: Path, *, selector: str, actor: str = "human_ui") -> dict:
    from .refactor import preview_restore_entity as _preview
    return _preview(root, selector=selector, actor=actor)


def preview_reparent_unit(root: Path, *, unit_id: str, placement: dict, actor: str = "human_ui") -> dict:
    from .refactor import preview_reparent_unit as _preview
    return _preview(root, unit_id=unit_id, placement=placement, actor=actor)
