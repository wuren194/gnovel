"""One reviewed structure plan for an ordered list of chapter and entity edits.

批量重排、归档或登记实体时，逐条 preview → commit 会产生几十个计划、几十次提交和
几十条撤销记录。这里把有序步骤依次施加到同一份结构快照上，生成**一个**计划：
一个 operationId、一个 planHash、一次提交、一次撤销。

- 每一步复用单步预览的同一个变换函数，不另写一份移动／归档／登记逻辑。
- 步骤里的章节显示编号（CH001）按预览开始时的结构解析，与调用者看到的编号一致；
  计划只记录稳定 ID。
- 提交、批准、Canon 边界、写集事务与撤销沿用 `structure commit` / `structure undo`；
  批量不授予任何单步没有的权限：single-source 下修改既有实体仍须走受审提案。
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .chapter_refs import resolve_chapter_reference
from .structure_store import StructureError, load_structure, validate_structure

STEP_OPS = ("move-chapter", "archive-chapter", "create-entity", "create-volume", "create-arc", "relink-range")


def _chapter(base: dict, step: dict, key: str, index: int) -> str | None:
    value = step.get(key)
    if value is None:
        return None
    return resolve_chapter_reference(base, value, field=f"steps[{index}].{key}")


def _ref(refs: dict[str, str], value: str, field: str) -> str:
    if value in refs:
        return refs[value]
    if value.startswith("$"):
        raise StructureError("STRUCTURE_BATCH_REFERENCE_NOT_FOUND", "local reference must name an earlier create step",
                             {"ref": value, "fieldPath": field})
    return value


def _bind_ref(refs: dict[str, str], value: str, identity: str) -> None:
    if not isinstance(value, str) or not value.startswith("$") or value in refs:
        raise StructureError("STRUCTURE_BATCH_REFERENCE_INVALID", "create refs must be unique $local names",
                             {"ref": value, "existingRefs": sorted(refs)})
    refs[value] = identity


def _range_chapters(base: dict, start: int, end: int) -> list[str]:
    if start > end:
        raise StructureError("STRUCTURE_BATCH_RANGE_INVALID", "fromOrdinal must not exceed toOrdinal",
                             {"fromOrdinal": start, "toOrdinal": end})
    active = sorted((chapter for chapter in base["chapters"] if chapter.get("status") != "archived"),
                    key=lambda chapter: chapter["displayOrdinal"])
    if end > len(active):
        raise StructureError("STRUCTURE_BATCH_RANGE_INVALID", "toOrdinal exceeds the active chapter count",
                             {"fromOrdinal": start, "toOrdinal": end, "chapterCount": len(active)})
    selected = [chapter["chapterId"] for chapter in active if start <= chapter["displayOrdinal"] <= end]
    if not selected:
        raise StructureError("STRUCTURE_BATCH_RANGE_EMPTY", "ordinal range contains no active chapters",
                             {"fromOrdinal": start, "toOrdinal": end, "chapterCount": len(active)})
    return selected


def _apply(base: dict, proposed: dict, step: dict, index: int, refs: dict[str, str]) -> dict:
    from .catalog_commands import apply_create_arc, apply_create_entity, apply_create_volume, apply_relink_chapter
    from .structure_commands import apply_archive_chapter, apply_move_chapter
    op = step["op"]
    if op == "move-chapter":
        chapter_id = _chapter(base, step, "chapterId", index)
        before = _chapter(base, step, "beforeChapterId", index)
        impact = apply_move_chapter(proposed, chapter_id, before)
        return {"op": op, "chapterId": chapter_id, "beforeChapterId": before, "impact": impact}
    if op == "archive-chapter":
        chapter_id = _chapter(base, step, "chapterId", index)
        impact = apply_archive_chapter(proposed, chapter_id)
        return {"op": op, "chapterId": chapter_id, "reason": step.get("reason", "").strip(), "impact": impact}
    if op == "create-entity":
        entity_id = apply_create_entity(proposed, entity_type=step["entityType"], name=step["name"],
                                        aliases=step.get("aliases"), summary=step.get("summary", ""))
        return {"op": op, "entityId": entity_id, "entityType": step["entityType"], "name": step["name"].strip(),
                "impact": {"staleCandidates": [], "approvalInvalidations": []}}
    if op == "create-volume":
        volume_id = apply_create_volume(proposed, title=step["title"])
        _bind_ref(refs, step["ref"], volume_id)
        return {"op": op, "ref": step["ref"], "volumeId": volume_id, "title": step["title"].strip(),
                "impact": {"staleCandidates": [], "approvalInvalidations": []}}
    if op == "create-arc":
        volume_ids = [_ref(refs, value, f"steps[{index}].volumeRefs") for value in step["volumeRefs"]]
        parent = step.get("parentArcRef")
        parent_id = _ref(refs, parent, f"steps[{index}].parentArcRef") if parent else None
        arc_id = apply_create_arc(proposed, title=step["title"], volume_ids=volume_ids,
                                  kind=step.get("kind", "main"), parent_arc_id=parent_id,
                                  promise=step.get("promise", ""))
        _bind_ref(refs, step["ref"], arc_id)
        return {"op": op, "ref": step["ref"], "arcId": arc_id, "title": step["title"].strip(),
                "volumeIds": volume_ids, "impact": {"staleCandidates": [], "approvalInvalidations": []}}
    if op == "relink-range":
        volume_id = _ref(refs, step["volumeRef"], f"steps[{index}].volumeRef")
        primary_arc_id = _ref(refs, step["primaryArcRef"], f"steps[{index}].primaryArcRef")
        secondary = [_ref(refs, value, f"steps[{index}].secondaryArcRefs")
                     for value in step.get("secondaryArcRefs", [])]
        chapter_ids = _range_chapters(base, step["fromOrdinal"], step["toOrdinal"])
        impacts = [apply_relink_chapter(proposed, chapter_id=chapter_id, primary_arc_id=primary_arc_id,
                                        volume_id=volume_id, secondary_arc_ids=secondary)
                   for chapter_id in chapter_ids]
        return {"op": op, "volumeId": volume_id, "primaryArcId": primary_arc_id,
                "secondaryArcIds": secondary, "fromOrdinal": step["fromOrdinal"],
                "toOrdinal": step["toOrdinal"], "chapterIds": chapter_ids,
                "impact": {"staleCandidates": [cid for row in impacts for cid in row["staleCandidates"]],
                            "approvalInvalidations": [cid for row in impacts for cid in row["approvalInvalidations"]]}}
    raise StructureError("STRUCTURE_BATCH_OPERATION_INVALID", "unsupported structure batch step", {"op": op})


def _renumbered(base: dict, proposed: dict) -> list[dict]:
    before = {row["chapterId"]: row.get("displayCode", "") if row.get("status") != "archived" else ""
              for row in base["chapters"]}
    rows = []
    for row in proposed["chapters"]:
        after = row.get("displayCode", "") if row.get("status") != "archived" else ""
        if before.get(row["chapterId"]) != after:
            rows.append({"chapterId": row["chapterId"], "from": before.get(row["chapterId"]), "to": after})
    return rows


def preview_structure_batch(root: Path, *, steps: list[dict], actor: str = "human_ui") -> dict:
    from .project import structure_batch_max_steps
    from .source_model import require_current_source
    from .structure_commands import _base_plan, _write_plan
    root = root.resolve()
    structure = load_structure(root)
    require_current_source(structure)
    limit = structure_batch_max_steps()
    if not isinstance(steps, list) or not steps or len(steps) > limit:
        raise StructureError("STRUCTURE_BATCH_INVALID", f"steps must hold 1-{limit} items",
                             {"maxSteps": limit, "received": len(steps) if isinstance(steps, list) else None})
    proposed = deepcopy(structure)
    applied = []
    refs = {}
    for index, step in enumerate(steps):
        try:
            applied.append(_apply(structure, proposed, step, index, refs))
        except (StructureError, ValueError) as exc:
            code = getattr(exc, "code", "STEP_INVALID")
            raise StructureError("STRUCTURE_BATCH_STEP_FAILED", f"steps[{index}] ({step.get('op')}) failed: {exc}",
                                 {"stepIndex": index, "op": step.get("op"), "stepCode": code,
                                  "stepDetails": getattr(exc, "details", {})}) from exc
    validate_structure(proposed)
    renumbered = _renumbered(structure, proposed)
    if proposed == structure:
        raise StructureError("STRUCTURE_BATCH_NOOP", "steps leave the structure unchanged")
    stale = sorted({cid for row in applied for cid in row["impact"].get("staleCandidates", [])}
                   | {row["chapterId"] for row in renumbered})
    invalidations = sorted({cid for row in applied for cid in row["impact"].get("approvalInvalidations", [])}
                           | {row["chapterId"] for row in renumbered})
    summary = {
        "stepCount": len(applied),
        "stepCounts": {op: sum(1 for row in applied if row["op"] == op) for op in STEP_OPS
                       if any(row["op"] == op for row in applied)},
        "renumberedCount": len(renumbered),
        "archivedChapterIds": [row["chapterId"] for row in applied if row["op"] == "archive-chapter"],
        "createdEntities": [{"entityId": row["entityId"], "entityType": row["entityType"], "name": row["name"]}
                            for row in applied if row["op"] == "create-entity"],
        "createdVolumes": [{"volumeId": row["volumeId"], "title": row["title"]}
                           for row in applied if row["op"] == "create-volume"],
        "createdArcs": [{"arcId": row["arcId"], "title": row["title"]}
                        for row in applied if row["op"] == "create-arc"],
        "relinkedChapterCount": sum(len(row.get("chapterIds", [])) for row in applied if row["op"] == "relink-range"),
    }
    impact = {"renumberedChapters": renumbered, "staleCandidates": stale, "approvalInvalidations": invalidations,
              "manualReview": "脚本只可重建带 `> GENERATED:` 标记的文件；不带标记的文件永不写入，须人工整理。"}
    parameters = {"steps": [{key: value for key, value in row.items() if key != "impact"} for row in applied]}
    plan = _base_plan(structure, operation="structure_batch", actor=actor, parameters=parameters,
                      impact=impact, proposed=proposed)
    plan["batch"] = summary
    result = _write_plan(root, plan)
    result.setdefault("batch", summary)
    return result
