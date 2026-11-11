"""Chapter lifecycle advancement with contract gates.

动机：`transition_chapter_status()` 此前只被 `finalize_chapter()` 调用一次，
没有任何命令能把章节从 planning_required 推到 planned/draft/review——状态机
是半开的，实际项目只能靠手改 structure.json 前进，闸门自然全部失效。

本模块提供唯一的状态推进入口，并把契约闸门绑在具体转换上：
CHAPTER-PIPELINE「前置条件」要求阶段 A 先获批准，WORKSTATION-LIFECYCLE
要求阶段 A 产物封装成 stage_a_review 工件登记。review → stable 不在此处，
它属于 finalize_chapter() 的事务范围。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import stored_project_path
from .clock import utc_now
from .jsonio import read_json
from .locks import project_lock
from .paths import ProjectPathError
from .structure_store import load_structure, StructureError
from .workflow import (
    WorkflowError,
    default_workflow,
    normalize_workflow,
    registered_artifact,
    require_approved_artifact,
    transition_chapter_status,
)

# review → stable 缺席是有意的：正式入库要同时写正文、设定回写候选与缓存，
# 只能走 finalize_chapter() 的事务路径。
ADVANCEABLE_STATUSES = ("planned", "draft", "review", "stale")


def _workflow_path(root: Path) -> Path:
    return root / ".govel" / "workflow.json"


def _load_workflow(root: Path) -> dict[str, Any]:
    path = _workflow_path(root)
    if not path.is_file():
        return default_workflow()
    return normalize_workflow(read_json(path))


def _gate_planned(root: Path, chapter: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    """阶段 A 闸门：推演必须存在，且已批准的 stage_a_review 绑定当前推演内容。"""
    chapter_id = chapter["chapterId"]
    from .source_plan import outline_binding
    try:
        binding = outline_binding(root, load_structure(root), chapter)
    except StructureError as exc:
        raise WorkflowError(exc.code, str(exc), exc.details) from exc
    require_approved_artifact(workflow, chapter_id, "stage_a_review")
    entry = registered_artifact(workflow, chapter_id, "stage_a_review") or {}
    artifact_path = entry.get("artifactPath")
    if not artifact_path:
        raise WorkflowError("ARTIFACT_SOURCE_MISSING", "stage_a_review has no source path", {"chapterId": chapter_id})
    from .review_artifacts import validate_artifact_binding, ReviewArtifactError
    try:
        path = stored_project_path(root, artifact_path)
        data = read_json(path)
        validate_artifact_binding(data, project_id=load_structure(root)['projectId'], chapter_id=chapter_id)
        if data['artifactSha256'] != entry.get('artifactSha256'):
            raise WorkflowError('ARTIFACT_APPROVAL_HASH_MISMATCH', 'approved stage A artifact was replaced')
    except (ProjectPathError, ReviewArtifactError) as exc:
        raise WorkflowError(exc.code, str(exc), exc.details) from exc
    payload = data['payload']
    from .outline_facts import inspect_outline
    constraints = inspect_outline(root, chapterId=chapter_id).get('projectConstraints', {})
    if constraints.get('conflicts') and payload.get('projectExceptions') != constraints:
        raise WorkflowError('STAGE_A_PROJECT_EXCEPTION_UNREVIEWED',
                            '项目区间例外尚未进入当前阶段 A 工件；须登记原文依据并由作者明确批准')
    declared = str(payload.get(binding['field'], ""))
    actual = binding['hash']
    if declared != actual:
        raise WorkflowError(
            "STAGE_A_OUTLINE_MISMATCH",
            "approved stage A review is not bound to the current outline",
            {"chapterId": chapter_id, "expected": declared, "actual": actual},
        )
    if payload.get("decision") not in {"adopt", "modify"}:
        raise WorkflowError(
            "STAGE_A_NOT_ADOPTED",
            "stage A decision must be adopt or modify before writing",
            {"chapterId": chapter_id, "decision": payload.get("decision")},
        )
    from .relationship_causality_review import validate_causality_completion
    validate_causality_completion(load_structure(root), chapter_id, payload, 'stage_a_review')
    from .review_plan import require_stage_a_completion
    require_stage_a_completion(load_structure(root), chapter_id, payload)
    return {binding['field']: actual}


def _gate_draft(root: Path, chapter: dict[str, Any], workflow: dict[str, Any], *, candidate=None) -> dict[str, Any]:
    """Writer 候选必须登记在工作台；正式正文只能由 finalize 写入受保护路径。"""
    chapter_id = chapter["chapterId"]
    _gate_planned(root, chapter, workflow)
    entry = registered_artifact(workflow, chapter_id, "candidate_prose")
    if entry is None:
        raise WorkflowError(
            "CANDIDATE_PROSE_NOT_REGISTERED",
            "candidate prose must be registered before draft status",
            {"chapterId": chapter_id},
        )
    if candidate is None:
        from .mechanical_evidence import candidate_prose
        binding = candidate_prose(root, chapter_id).get('planBinding', {})
    else:
        from .writing_notes import validate_candidate_binding
        binding = validate_candidate_binding(root, load_structure(root), workflow, chapter_id, candidate)
    if binding.get('status') == 'bound':
        _gate_planned(root, chapter, workflow)
    return {"candidateProseSha256": entry.get("artifactSha256")}


def _gate_review(root: Path, chapter: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    """机械门证据必须已登记且绑定当前输入；是否通过由 finalize 阶段按 hash 链复核。"""
    chapter_id = chapter["chapterId"]
    _gate_planned(root, chapter, workflow)
    if registered_artifact(workflow, chapter_id, "diagnostics") is None:
        raise WorkflowError(
            "DIAGNOSTICS_NOT_REGISTERED",
            "mechanical diagnostics must be registered before review",
            {"chapterId": chapter_id},
        )
    from .mechanical_evidence import evidence_current
    status = evidence_current(root, chapter_id)
    if status.get("registered") and not status.get("fresh"):
        raise WorkflowError(
            "DIAGNOSTICS_STALE",
            "registered mechanical evidence no longer matches the current inputs; rerun diagnostics",
            {"chapterId": chapter_id, "reason": status.get("reason")},
        )
    return {"diagnosticsCurrent": status.get("current")}


def _gate_stale(root: Path, chapter: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    return {}


GATES = {
    "planned": _gate_planned,
    "draft": _gate_draft,
    "review": _gate_review,
    "stale": _gate_stale,
}






def _advance_chapter_status(root: Path, chapter_id: str, target_status: str, *, actor: str = "cli") -> dict[str, Any]:
    """Move one chapter to the next lifecycle status after its gate passes."""
    root = root.expanduser().resolve()
    if target_status not in ADVANCEABLE_STATUSES:
        raise WorkflowError(
            "STATUS_NOT_ADVANCEABLE",
            f"unsupported advance target: {target_status}",
            {"supported": list(ADVANCEABLE_STATUSES), "note": "review → stable 只能通过 workflow finalize-chapter"},
        )
    with project_lock(root):
        structure = load_structure(root)
        chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
        if chapter is None:
            raise WorkflowError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
        workflow = _load_workflow(root)
        previous_status = chapter.get("status")
        from .derived_views import derived_outputs
        from .write_sets import apply_write_set, json_bytes
        proposed, evidence = prepare_transition(root, structure, workflow, chapter_id, target_status, actor=actor)
        outputs, _ = derived_outputs(root, proposed, current=structure,
                                     extras={".govel/workflow.json": json_bytes({**workflow, "updatedAt": utc_now()})})
        apply_write_set(root, outputs, operation="advance_chapter_status")
    return {
        "ok": True,
        "code": "CHAPTER_STATUS_ADVANCED",
        "chapterId": chapter_id,
        "from": previous_status,
        "status": target_status,
        "evidence": evidence,
    }


def advance_chapter_status(root: Path, chapter_id: str, target_status: str, *, actor: str = 'cli') -> dict[str, Any]:
    try:
        return _advance_chapter_status(root, chapter_id, target_status, actor=actor)
    except (ValueError, OSError) as exc:
        code = getattr(exc, 'code', 'CHAPTER_ADVANCE_FAILED')
        details = dict(getattr(exc, 'details', {}) or {})
        actions = [{'operation': 'project.workflow.chapter-receipt', 'arguments': {'chapterId': chapter_id, 'view': 'model'}}]
        if code == 'TRANSACTION_INCOMPLETE':
            actions = [{'operation': 'project.workflow.recover', 'arguments': {'apply': False}}]
        elif code in {'STRUCTURE_INVALID', 'PROJECT_NOT_INITIALIZED'}:
            actions = [{'operation': 'project.doctor', 'arguments': {}}]
        elif code in {'PREDECESSOR_UNSTABLE', 'CONTINUITY_REQUIRED', 'CHAPTER_FACTS_REQUIRED'}:
            actions.append({'operation': 'project.workflow.reconcile', 'arguments': {'persist': False}})
        elif code.startswith(('STAGE_A_', 'REVIEW_SEATS_', 'SOURCE_CHAPTER_PLAN_', 'BEHAVIOR_')):
            actions.extend([{'operation': 'source.chapter-plan', 'arguments': {'chapterId': chapter_id, 'view': 'detail'}},
                            {'operation': 'workflow.review-form', 'arguments': {'chapterId': chapter_id, 'artifactType': 'stage_a_review'}}])
        details.setdefault('recovery', {'automatic': False, 'readActions': actions,
            'instruction': '先读取原材料和具体缺项；损坏项目保留原件核验，未完成审查按原流程补齐。恢复不改变原期望版本或产生批准。'})
        exc.details = details
        raise


def prepare_transition(root, structure, workflow, chapter_id, target_status, *, actor, pending_diagnostics=False, pending_candidate=None):
    """Shared gated transition proposal; caller must commit registration in the same write set."""
    chapter = next(row for row in structure["chapters"] if row["chapterId"] == chapter_id)
    from .source_model import require_current_source
    require_current_source(structure)
    if target_status in {'planned', 'draft', 'review'}:
        from .source_rules import require_rules
        from .structure_store import StructureError
        try:
            require_rules(structure, chapter_id, 'prepare' if target_status == 'planned' else target_status,
                          root=root)
        except StructureError as exc:
            raise WorkflowError(exc.code, str(exc), exc.details) from exc
    if target_status != "stale":
        from .project import serial_planning_policy
        from .workflow import require_serial_scope
        policy = serial_planning_policy()
        predecessor_boundary = require_serial_scope(structure, workflow, chapter, root=root, planning=target_status == 'planned',
                             current_volume_only=policy['currentVolumeOnly'],
                             prewarm_next_chapter=policy['prewarmNextChapter'])
        from .continuity import require_predecessors_ready
        require_predecessors_ready(structure, workflow, predecessor_boundary, root=root)
    if pending_diagnostics and target_status == 'review':
        if chapter.get('status') != 'draft':
            raise WorkflowError('INVALID_STATE_TRANSITION', 'review requires draft')
        _gate_planned(root, chapter, workflow)
        evidence = {'pendingMechanicalRegistration': True}
    elif target_status == 'draft' and pending_candidate is not None:
        evidence = _gate_draft(root, chapter, workflow, candidate=pending_candidate)
    else:
        evidence = GATES[target_status](root, chapter, workflow)

    proposed = deepcopy(structure)
    transition_chapter_status(proposed, chapter_id, target_status)
    proposed["revision"] = structure["revision"] + 1
    proposed["updatedAt"] = utc_now()
    proposed["lastOperation"] = {
        "operationId": f"ADVANCE-{chapter_id}-{target_status.upper()}",
        "operation": "advance_chapter_status", "actor": actor, "committedAt": utc_now(),
    }
    from .canon_guard import require_canon_proposal
    require_canon_proposal(structure, proposed,
                           {'operation': 'advance_chapter_status', 'parameters': {}})
    workflow.setdefault("chapterStates", {})[chapter_id] = {
        "status": target_status, "updatedAt": utc_now(), "actor": actor,
    }
    return proposed, evidence
