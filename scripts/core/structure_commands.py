"""Preview-first, revision-bound story structure commands."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from copy import deepcopy
from pathlib import Path

from .clock import utc_now
from .entity_registry import replace_entity_fields
from .impact_analysis import (
    anchor_change_impact, archive_impact, entity_change_impact,
    insertion_impact, merge_entity_impact, move_impact,
)
from .jsonio import read_json, write_json, write_text
from .locks import project_lock
from .paths import ProjectPathError, safe_project_path
from .project import gc_keep_backups_default
from .structure_store import (
    StructureError,
    derive_display_codes,
    is_time_ordinal,
    load_structure,
    new_id,
    save_structure,
    structure_hash,
    validate_structure,
)
from .undo_journal import UNDO_OPERATION_NAME

PLAN_TTL_SECONDS = 1800


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not re.fullmatch(r"OP-[0-9A-F]{16}", operation_id):
        raise StructureError("OPERATION_ID_INVALID", "operationId must be OP- followed by 16 uppercase hexadecimal characters")


def new_operation_id() -> str:
    """The single generator for a formal operation id.

    Every entry in the operation ledger mints its id here — approved plans and the formal undo
    operations the undo producer commits — so the id space has one definition, not one per caller.
    """
    return f"OP-{uuid.uuid4().hex[:16].upper()}"


def _safe_path(root: Path, relative: str | Path) -> Path:
    try:
        return safe_project_path(root, relative)
    except ProjectPathError as exc:
        raise StructureError(exc.code, str(exc), exc.details) from exc


def _operation_paths(root: Path, operation_id: str) -> tuple[Path, Path, Path]:
    _validate_operation_id(operation_id)
    return (
        _safe_path(root, f".gnovel/plans/{operation_id}.json"),
        _safe_path(root, f".gnovel/operations/{operation_id}.json"),
        _safe_path(root, f".gnovel/backups/{operation_id}"),
    )


def _file_state(path: Path) -> dict:
    if path.exists() and not path.is_file():
        raise StructureError("TRANSACTION_TARGET_INVALID", "transaction target must be a regular file", {"path": str(path)})
    return {"exists": path.is_file(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}


def _validate_file_paths(root: Path, files: list[Path], backup: Path) -> None:
    for path in files:
        relative = str(path.relative_to(root))
        _file_state(_safe_path(root, relative))
        _file_state(_safe_path(root, _backup_path(backup, relative).relative_to(root)))


def _plan_dir(root: Path) -> Path:
    return _safe_path(root, ".gnovel/plans")


def _journal_dir(root: Path) -> Path:
    return _safe_path(root, ".gnovel/operations")


def _plan_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()






def _backup_path(backup: Path, relative: str) -> Path:
    return backup / "structure.json" if relative == ".gnovel/structure.json" else backup / relative








def _write_plan(root: Path, plan: dict) -> dict:
    with project_lock(root):
        return _write_plan_locked(root, plan)


def _write_plan_locked(root: Path, plan: dict) -> dict:
    """Stage a plan while the caller holds the project lock."""
    from .structure_transaction import prepare_writes
    plan_path, _, _ = _operation_paths(root, plan["operationId"])
    current = load_structure(root)
    if current["revision"] != plan["baseRevision"] or structure_hash(current) != plan["baseHash"]:
        raise StructureError("REVISION_CONFLICT", "project changed while preparing preview")
    prepare_writes(root, plan, current)
    plan["planHash"] = _plan_hash({key: value for key, value in plan.items() if key != "planHash"})
    write_json(plan_path, plan)
    from .source_reports import public_report
    return public_report(plan, root=root)


def _base_plan(structure: dict, *, operation: str, actor: str, parameters: dict, impact: dict, proposed: dict) -> dict:
    plan = {
        "schemaVersion": 1,
        "operationId": new_operation_id(),
        "operation": operation,
        "actor": actor,
        "createdAtEpoch": int(time.time()),
        "expiresAtEpoch": int(time.time()) + PLAN_TTL_SECONDS,
        "baseRevision": structure["revision"],
        "baseHash": structure_hash(structure),
        "parameters": parameters,
        "impact": impact,
        "proposedStructure": proposed,
        "requiresApproval": True,
    }
    return plan


def _new_chapter(structure: dict, *, title: str, placement: dict, before_chapter_id: str | None, primary_arc_id: str, secondary_arc_ids: list[str]) -> tuple[dict, str, dict]:
    from .source_placement import placement_error, placement_volume_id
    reason = placement_error(placement)
    if reason is not None:
        raise StructureError("PLACEMENT_INVALID", f"chapter placement is not a legal shape: {reason}")
    volume_id = placement_volume_id(placement)  # None for project scope
    volumes = {item["volumeId"] for item in structure["volumes"]}
    arcs = {item["arcId"] for item in structure["arcs"]}
    if volume_id is not None and volume_id not in volumes:
        raise StructureError("VOLUME_NOT_FOUND", f"unknown volume: {volume_id}")
    if primary_arc_id not in arcs:
        raise StructureError("ARC_NOT_FOUND", f"unknown arc: {primary_arc_id}")
    secondary = list(dict.fromkeys(secondary_arc_ids))
    missing = sorted(set(secondary) - arcs)
    if missing:
        raise StructureError("ARC_NOT_FOUND", "secondary arcs do not exist", {"arcIds": missing})
    if primary_arc_id in secondary:
        raise StructureError("ARC_ROLE_CONFLICT", "primary arc cannot also be secondary")
    selected = [item for item in structure["volumes"] if item["volumeId"] == volume_id]
    selected_arcs = [item for item in structure["arcs"] if item["arcId"] in {primary_arc_id, *secondary}]
    if any(item.get("status") == "archived" for item in selected + selected_arcs):
        raise StructureError("ARCHIVED_TARGET", "new chapters cannot use archived volumes or arcs")
    if volume_id is None:
        # Project-scope chapters bind global arcs only (arc.volumeIds == []).
        if any(item.get("volumeIds") for item in selected_arcs):
            raise StructureError("ARC_VOLUME_MISMATCH", "project chapters can only bind global arcs")
    elif any(item.get("volumeIds") and volume_id not in item["volumeIds"] for item in selected_arcs):
        raise StructureError("ARC_VOLUME_MISMATCH", "chapter volume is outside its arc scope")

    chapter_id = new_id("chapter")
    impact = insertion_impact(structure, before_chapter_id)
    position = impact["insertionIndex"] + 1
    proposed = deepcopy(structure)
    new_chapter = {
        "chapterId": chapter_id,
        "displayOrdinal": position,
        "displayCode": f"CH{position:03d}",
        "title": title.strip() or "未命名章节",
        "filePath": f"正文/{chapter_id}.md",
        "status": "planning_required",
        "storyTimeStartId": None,
        "storyTimeEndId": None,
    }
    new_chapter["placement"] = deepcopy(placement)
    # Chapter Materialization（契约 §4 行 1）：新建正式章必须密封 unbound binding，
    # generation 取本事务的提交 revision（prepare_writes 固定为 current+1）。
    from .prose_binding import materialization_binding
    new_chapter["proseBinding"] = materialization_binding(
        structure, new_chapter, generation=structure["revision"] + 1)
    active = sorted(
        (item for item in proposed["chapters"] if item.get("status") != "archived"),
        key=lambda item: item["displayOrdinal"],
    )
    archived = [item for item in proposed["chapters"] if item.get("status") == "archived"]
    active.insert(impact["insertionIndex"], new_chapter)
    proposed["chapters"] = active + archived
    derive_display_codes(proposed)
    proposed["chapterArcLinks"].append({"chapterId": chapter_id, "arcId": primary_arc_id, "role": "primary", "weight": 1.0, "phaseId": None, "notes": ""})
    for arc_id in secondary:
        proposed["chapterArcLinks"].append({"chapterId": chapter_id, "arcId": arc_id, "role": "secondary", "weight": 0.5, "phaseId": None, "notes": ""})
    validate_structure(proposed)
    return proposed, chapter_id, impact


def preview_insert_chapter(root: Path, *, primary_arc_id: str, volume_id: str | None = None, placement: dict | None = None, title: str = "", before_chapter_id: str | None = None, secondary_arc_ids: list[str] | None = None, actor: str = "human_ui") -> dict:
    from .source_placement import placement_from_volume
    root = root.resolve()
    structure = load_structure(root)
    from .source_model import require_current_source
    require_current_source(structure)
    if (placement is None) == (volume_id is None):
        raise StructureError("PLACEMENT_ARGUMENT_INVALID", "provide exactly one of volumeId (compat) or placement")
    resolved = placement if placement is not None else placement_from_volume(volume_id)
    proposed, chapter_id, impact = _new_chapter(structure, title=title, placement=resolved, before_chapter_id=before_chapter_id, primary_arc_id=primary_arc_id, secondary_arc_ids=secondary_arc_ids or [])
    chapter = next(item for item in proposed["chapters"] if item["chapterId"] == chapter_id)
    plan = _base_plan(structure, operation="insert_chapter", actor=actor, parameters={"chapterId": chapter_id, "title": title.strip() or "未命名章节", "placement": chapter.get("placement", chapter.get("volumeId")), "primaryArcId": primary_arc_id, "secondaryArcIds": list(dict.fromkeys(secondary_arc_ids or [])), "beforeChapterId": before_chapter_id}, impact=impact, proposed=proposed)
    return _write_plan(root, plan)


def apply_move_chapter(proposed: dict, chapter_id: str, before_chapter_id: str | None) -> dict:
    """Reorder ``proposed`` in place; shared by the single preview and structure batches."""
    impact = move_impact(proposed, chapter_id, before_chapter_id)
    ordered_ids = impact.pop("orderedChapterIds")
    by_id = {item["chapterId"]: item for item in proposed["chapters"]}
    proposed["chapters"] = [by_id[item_id] for item_id in ordered_ids] + [item for item in proposed["chapters"] if item.get("status") == "archived" and item["chapterId"] not in ordered_ids]
    derive_display_codes(proposed)
    return impact


def apply_archive_chapter(proposed: dict, chapter_id: str) -> dict:
    """Archive one chapter of ``proposed`` in place and renumber the rest."""
    chapter = next((item for item in proposed["chapters"] if item["chapterId"] == chapter_id), None)
    if chapter is None:
        raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
    if chapter.get("status") == "archived":
        raise StructureError("CHAPTER_ALREADY_ARCHIVED", f"chapter already archived: {chapter_id}")
    impact = archive_impact(proposed, chapter_id)
    chapter["status"] = "archived"
    derive_display_codes(proposed)
    return impact


def preview_move_chapter(root: Path, *, chapter_id: str, before_chapter_id: str | None = None, actor: str = "human_ui") -> dict:
    root = root.resolve()
    structure = load_structure(root)
    proposed = deepcopy(structure)
    impact = apply_move_chapter(proposed, chapter_id, before_chapter_id)
    validate_structure(proposed)
    plan = _base_plan(structure, operation="move_chapter", actor=actor, parameters={"chapterId": chapter_id, "beforeChapterId": before_chapter_id}, impact=impact, proposed=proposed)
    return _write_plan(root, plan)


def preview_archive_chapter(root: Path, *, chapter_id: str, reason: str = "", actor: str = "human_ui") -> dict:
    root = root.resolve()
    structure = load_structure(root)
    proposed = deepcopy(structure)
    impact = apply_archive_chapter(proposed, chapter_id)
    validate_structure(proposed)
    plan = _base_plan(structure, operation="archive_chapter", actor=actor, parameters={"chapterId": chapter_id, "reason": reason.strip()}, impact=impact, proposed=proposed)
    return _write_plan(root, plan)




def preview_set_chapter_timespan(root: Path, *, chapter_id: str, start_anchor_id: str | None, end_anchor_id: str | None = None, actor: str = "human_ui") -> dict:
    root = root.resolve()
    structure = load_structure(root)
    chapter = next((item for item in structure["chapters"] if item["chapterId"] == chapter_id), None)
    if chapter is None:
        raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
    anchors = {item["entityId"] for item in structure.get("entities", []) if item.get("entityType") == "time_anchor"}
    for anchor_id in (start_anchor_id, end_anchor_id):
        if anchor_id and anchor_id not in anchors:
            raise StructureError("TIME_ANCHOR_NOT_FOUND", f"unknown time anchor: {anchor_id}")
    impact = {
        "chapterId": chapter_id,
        "from": {"startAnchorId": chapter.get("storyTimeStartId"), "endAnchorId": chapter.get("storyTimeEndId")},
        "to": {"startAnchorId": start_anchor_id, "endAnchorId": end_anchor_id or start_anchor_id},
        "staleCandidates": [],
        "approvalInvalidations": [],
    }
    proposed = deepcopy(structure)
    target = next(item for item in proposed["chapters"] if item["chapterId"] == chapter_id)
    target["storyTimeStartId"] = start_anchor_id
    target["storyTimeEndId"] = end_anchor_id or start_anchor_id
    validate_structure(proposed)
    plan = _base_plan(structure, operation="set_chapter_timespan", actor=actor, parameters={"chapterId": chapter_id, "startAnchorId": start_anchor_id, "endAnchorId": end_anchor_id or start_anchor_id}, impact=impact, proposed=proposed)
    return _write_plan(root, plan)


def preview_create_time_anchor(root: Path, *, name: str, absolute_time: str, summary: str = "",
                               time_ordinal: float | None = None, actor: str = "human_ui") -> dict:
    root = root.resolve()
    structure = load_structure(root)
    if not name.strip():
        raise StructureError("ANCHOR_NAME_REQUIRED", "anchor name is required")
    if not absolute_time.strip():
        raise StructureError("ABSOLUTE_TIME_REQUIRED", "absoluteTime is required (sortable string, e.g. 0847-02-18 23:00)")
    if time_ordinal is not None and not is_time_ordinal(time_ordinal):
        raise StructureError("INVALID_TIME_ORDINAL", "timeOrdinal must be a number (e.g. D-45 → -45、D-Day → 0)")
    proposed = deepcopy(structure)
    anchor_id = new_id("TA")
    anchor = {
        "entityId": anchor_id,
        "entityType": "time_anchor",
        "name": name.strip(),
        "aliases": [],
        "status": "active",
        "summary": summary.strip(),
        "absoluteTime": absolute_time.strip(),
        "sourceRefs": [],
    }
    if time_ordinal is not None:
        anchor["timeOrdinal"] = time_ordinal
    proposed.setdefault("entities", []).append(anchor)
    validate_structure(proposed)
    impact = {"anchorId": anchor_id, "staleCandidates": [], "approvalInvalidations": []}
    parameters = {"anchorId": anchor_id, "name": name.strip(), "absoluteTime": absolute_time.strip()}
    if time_ordinal is not None:
        parameters["timeOrdinal"] = time_ordinal
    plan = _base_plan(structure, operation="create_time_anchor", actor=actor, parameters=parameters, impact=impact, proposed=proposed)
    return _write_plan(root, plan)




def list_pending_plans(root: Path) -> list[dict]:
    directory = _plan_dir(root.resolve())
    if not directory.is_dir():
        return []
    now = int(time.time())
    plans = []
    for path in sorted(directory.glob("OP-*.json")):
        plan = read_json(path)
        plans.append({
            "operationId": plan["operationId"],
            "operation": plan["operation"],
            "actor": plan["actor"],
            "parameters": plan.get("parameters", {}),
            "planHash": plan.get("planHash", ""),
            "baseRevision": plan.get("baseRevision"),
            "expiresAtEpoch": plan.get("expiresAtEpoch"),
            "expired": now > plan.get("expiresAtEpoch", 0),
        })
    return plans


def gc_project(root: Path, *, keep_backups: int | None = None) -> dict:
    root = root.resolve()
    # 默认值来自注册表（maintenance.structureGcKeepBackups），不在此写字面量。
    if keep_backups is None:
        keep_backups = gc_keep_backups_default()
    with project_lock(root):
        return _gc_project_locked(root, keep_backups=keep_backups)


def _gc_project_locked(root: Path, *, keep_backups: int) -> dict:
    root = root.resolve()
    now = int(time.time())
    removed_plans = []
    plan_dir = _plan_dir(root)
    if plan_dir.is_dir():
        for path in plan_dir.glob("OP-*.json"):
            try:
                plan = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if now > plan.get("expiresAtEpoch", 0):
                path.unlink()
                removed_plans.append(plan.get("operationId", path.stem))
    removed_backups = []
    backup_root = root / ".gnovel" / "backups"
    if backup_root.is_dir():
        # 只回收已不能撤销的备份：被更新操作覆盖后，旧备份对 undo 无用。
        #
        # 正式 undo operation 是这个规则的两个例外，缺一个都会让 gc 亲手毁掉刚建立的证据：
        # 它自己不可撤销（`reversible: false`），但它持久化的 before-image 与它证明的那次撤销
        # 一样是长期证据（撤销事实不会过期），所以永不回收；它逆转的那次操作也仍需要自己的备份
        # 供血缘校验，所以挑选「最新可撤销操作」时必须跳过 undo 条目——否则一次撤销会把被撤销
        # operation 的备份顶出保护区，此后该章的血缘再也无法重构。
        journals = list_operations(root)
        undo_operations = [row for row in journals if row.get("operation") == UNDO_OPERATION_NAME]
        undoable_journals = [row for row in journals if row.get("operation") != UNDO_OPERATION_NAME]
        protected = {row["operationId"] for row in undo_operations}
        if undoable_journals:
            protected.add(undoable_journals[0]["operationId"])
        candidates = sorted(
            (item for item in backup_root.iterdir() if item.is_dir() and item.name not in protected),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in candidates[max(keep_backups - len(protected), 0):]:
            shutil.rmtree(stale)
            removed_backups.append(stale.name)
    return {"ok": True, "code": "PROJECT_GC_DONE", "removedPlans": sorted(removed_plans), "removedBackups": sorted(removed_backups)}














def commit_plan(root: Path, *, operation_id: str, plan_hash: str, approved: bool, actor: str = "human_ui") -> dict:
    """Single commit entry for every structure plan.

    Refactor plans route through `refactor.commit_refactor`, which is the same
    transaction plus an optional project-folder relocation; every other plan is
    unchanged.
    """
    from .refactor import commit_refactor
    return commit_refactor(root, operation_id=operation_id, plan_hash=plan_hash, approved=approved, actor=actor)


def _validate_undo(root: Path, operation_id: str, journal: dict) -> tuple[Path, dict]:
    from .structure_transaction import validate_undo
    if journal.get('schemaVersion') != 4:
        raise StructureError('UNDO_UNVERIFIABLE', 'operation has no current reversible write set')
    return validate_undo(root, operation_id, journal)


def list_operations(root: Path) -> list[dict]:
    root = root.resolve()
    directory = _journal_dir(root)
    if not directory.is_dir():
        return []
    try:
        current = load_structure(root)
    except (StructureError, OSError, ValueError):
        current = None
    current_binding = current is not None and True
    rows = []
    for path in directory.glob("OP-*.json"):
        try:
            _operation_paths(root, path.stem)
            row = read_json(path)
        except (StructureError, OSError, ValueError):
            continue
        row["undoable"] = False
        row["bindingGeneration"] = None
        try:
            if current_binding:
                row["undoable"], row["bindingGeneration"] = _monotonic_undo_offer(
                    root, path.stem, row, path, current)
            else:
                _validate_undo(root, path.stem, row)
                row["undoable"] = row.get("afterHash") == structure_hash(current)
        except (StructureError, OSError, ValueError, KeyError, TypeError):
            pass
        rows.append(row)
    return sorted(rows, key=lambda item: item.get("committedAt", ""), reverse=True)


def _monotonic_undo_offer(root: Path, operation_id: str, journal: dict, journal_path: Path,
                          current: dict) -> tuple[bool, int | None]:
    """`(is undoable right now, the bindingGeneration the caller must echo back)`.

    Both come from the engine's own criteria, not a second copy of them: chain-top and
    reversibility from the same predicate, and the protected delta from the same derivation
    the commit path uses. A `None` token means this undo reverses no protected binding
    semantics, so there is no generation to echo（§23.2 冻结的「无语义变更则 generation 不推进」）;
    a number is a token the request has to carry, because the engine will not go look one up.
    A delta spanning more than one chapter is reported as not undoable — that shape is the
    single-token contract's refusal, not something the caller can be offered a token for.
    """
    if not _monotonic_undoable(journal, current):
        return False, None
    from .structure_transaction import (_chapter_row, _reverse_transitions, _undo_target_structure,
                                        _require_reverse_ownership, validate_undo)
    from .write_sets import digest
    _, before = _undo_target_structure(root, operation_id, journal)
    transitions = _reverse_transitions(current, before, operation_id=operation_id,
                                       journal_digest=digest(journal_path.read_bytes()))
    if len(transitions) > 1:
        return False, None
    _require_reverse_ownership(root, current, transitions)
    validate_undo(root, operation_id, journal, preserve_source_runtime=True)
    if not transitions:
        return True, None
    chapter = _chapter_row(current, transitions[0]["chapterId"])
    if chapter is None:
        return False, None
    return True, (chapter.get("proseBinding") or {}).get("generation")


def _monotonic_undoable(journal: dict, current: dict) -> bool:
    """Read-only report of whether the monotonic engine would accept this operation now.

    Same criteria the engine enforces under the lock: a reversible schema-4 operation
    whose committed after-state is still the current structure content（契约 §13）。
    """
    if not isinstance(journal, dict) or journal.get("schemaVersion") != 4:
        return False
    if journal.get("operation") == UNDO_OPERATION_NAME or journal.get("reversible") is False:
        return False
    return journal.get("afterHash") == structure_hash(current)


def undo_operation(root: Path, operation_id: str, *, actor: str = "human_ui",
                   binding_generation: int | None = None) -> dict:
    """Reverse one current transaction; binding changes require its observed generation."""
    root = root.resolve()
    _, journal_path, _ = _operation_paths(root, operation_id)
    if not journal_path.is_file():
        raise StructureError('OPERATION_NOT_FOUND', f'unknown operation: {operation_id}')
    journal = read_json(journal_path)
    current = load_structure(root)
    if journal.get('schemaVersion') != 4:
        raise StructureError('OWNERSHIP_MODEL_UNSUPPORTED_OPERATION', 'operation has no current reversible write set',
                             {'operationId': operation_id})
    from .structure_transaction import prevalidate_undo, undo
    prevalidate_undo(root, operation_id, journal, current, binding_generation)
    return undo(root, operation_id, actor=actor, binding_generation=binding_generation)
