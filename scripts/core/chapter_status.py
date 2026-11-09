"""Read-only per-chapter pipeline status board, recomputed live.

统一入口：一次调用呈现章节所处流水线阶段、各工件的登记/批准/新鲜/绑定一致状态、
当前阻塞原因与下一步。三条硬规矩：

1. 状态实时重算（preflight_finalization 快照 + 结构/工作流现读），不消费任何缓存
   的就绪结论；
2. 下一步由实际证据推导（注册了什么、批准没有、闸门缺什么），同阶段存在多个
   合法分支时全部列出并标注依据，不假装只有一条路；
3. 命令只是入口提示，不是授权：needsApproval、作者批准、extreme 人工门照旧。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import stored_project_path
from .finalization import _load_workflow, pending_transactions
from .finalization_preflight import preflight_finalization
from .source_placement import chapter_placement, chapter_volume_id
from .structure_store import StructureError, load_structure
from .workflow import registered_artifact

BOARD_ARTIFACTS = ("title_candidates", "stage_a_review", "candidate_prose", "diagnostics", "stage_b_review", "state_candidate")
# 入库预检只对这两个阶段给出本章待办；其他阶段的预检阻塞是「还没到入库」的噪声。
FINALIZATION_STAGES = frozenset({"review", "stale"})
FINALIZE_BLOCKERS_EXEMPT = {
    # 非 review 章跑入库预检必然报这些项；它们不是本章阶段的待办
    "INVALID_STATE_TRANSITION",
    "TITLE_DECISION_REQUIRED",
    "FINAL_ARTIFACTS_MISSING",
    "ARTIFACTS_NOT_APPROVED",
    "ARTIFACT_APPROVAL_HASH_MISMATCH",
    "CHAPTER_OUTSIDE_APPROVED_RANGE",
    "STAGE_A_NOT_ADOPTED",
    "STAGE_A_OUTLINE_MISMATCH",
    "STAGE_B_NOT_READY",
    "DIAGNOSTICS_NOT_PASSED",
    "PROSE_PAYLOAD_INVALID",
}


def _artifact_rows(workflow: dict[str, Any], chapter_id: str, preflight_rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    registrations = workflow.get("artifacts", {}).get(chapter_id, {})
    approval = workflow.get("approvals", {}).get(chapter_id, {})
    hashes = approval.get("artifactHashes", {}) if isinstance(approval, dict) else {}
    preflight_by_kind = {row["artifactType"]: row for row in (preflight_rows or []) if isinstance(row, dict)}
    kinds = sorted(set(registrations) | set(BOARD_ARTIFACTS) | set(preflight_by_kind))
    rows = []
    for kind in kinds:
        entry = registrations.get(kind)
        row = {
            "artifactType": kind,
            "registered": isinstance(entry, dict),
            "approved": bool(isinstance(entry, dict) and not entry.get('invalidatedBy') and approval.get("approved")
                             and hashes.get(kind) == entry.get("artifactSha256")),
            "fresh": preflight_by_kind[kind]["fresh"] if kind in preflight_by_kind else None,
            "envelopeValid": preflight_by_kind[kind].get("envelopeValid") if kind in preflight_by_kind else None,
            "invalidated": bool(isinstance(entry, dict) and entry.get("invalidatedBy")),
            "artifactSha256": entry.get("artifactSha256") if isinstance(entry, dict) else None,
            "bindings": entry.get("bindings", {}) if isinstance(entry, dict) else {},
        }
        rows.append(row)
    return rows


def _approval_scope(workflow: dict[str, Any], chapter_id: str) -> bool:
    return chapter_id in workflow.get("approvedChapterIds", [])


def _next_actions(chapter: dict[str, Any], workflow: dict[str, Any], artifacts: list[dict[str, Any]],
                  stage_b_payload: dict[str, Any] | None, finalize_blockers: list[dict[str, Any]], *, root: Path,
                  receipt: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Derive candidate next actions from actual evidence; list all legitimate branches."""
    chapter_id = chapter["chapterId"]
    status = chapter.get("status")
    by_kind = {row["artifactType"]: row for row in artifacts}
    actions: list[dict[str, Any]] = []

    def action(command: str, why: str, *, needs_approval: bool = False, operation=None, arguments=None, semantic_inputs=()) -> None:
        from .chapter_actions import describe_action
        if operation == 'project.workflow.artifact-approve' and not (arguments or {}).get('artifact'):
            semantic_inputs = (*semantic_inputs, 'current_reviewed_artifacts')
        actions.append({"command": command, "basis": why, **describe_action(root, operation, arguments or {},
                       needs_approval=needs_approval, semantic_inputs=semantic_inputs)})

    def approve(kinds):
        from .paths import safe_project_path
        entries = workflow.get('artifacts', {}).get(chapter_id, {})
        paths = []
        for kind in kinds:
            entry = entries.get(kind, {})
            if not entry.get('artifactPath') or entry.get('invalidatedBy'):
                continue
            try:
                path = stored_project_path(root, entry['artifactPath'])
                if path.is_file():
                    paths.append('@' + str(path))
            except (OSError, ValueError):
                continue
        return {'chapterId': chapter_id, 'artifact': paths}

    if status == "planning_required":
        if not by_kind.get("stage_a_review", {}).get("registered") or by_kind.get('stage_a_review', {}).get('invalidated'):
            action("project prepare --task design --chapter-id " + chapter_id, "阶段 A 推演尚未登记",
                   operation='task.prepare', arguments={'chapterId': chapter_id, 'task': 'design', 'view': 'model'})
        elif not by_kind.get("stage_a_review", {}).get("approved"):
            action("展示阶段 A 裁决并获作者同意后 workflow artifact-approve --artifact @<stage_a文件>", "stage_a_review 已登记未批准", needs_approval=True,
                   operation='project.workflow.artifact-approve', arguments=approve(['stage_a_review']))
        else:
            action("workflow advance --chapter-id " + chapter_id + " --to planned", "stage_a_review 已批准；advance 时仍会复核推演绑定",
                   operation='project.workflow.advance', arguments={'chapterId': chapter_id, 'to': 'planned'})
        return actions
    if status == "planned":
        if not by_kind.get("candidate_prose", {}).get("registered") or by_kind.get('candidate_prose', {}).get('invalidated'):
            action("project prepare --task write --chapter-id " + chapter_id, "正文候选尚未登记",
                   operation='task.prepare', arguments={'chapterId': chapter_id, 'task': 'write', 'view': 'model'})
        else:
            action("workflow advance --chapter-id " + chapter_id + " --to draft", "正文候选已登记；机械门在 draft 之后",
                   operation='project.workflow.advance', arguments={'chapterId': chapter_id, 'to': 'draft'})
        return actions
    if status == "draft":
        diag = by_kind.get("diagnostics", {})
        if not diag.get("registered") or diag.get('invalidated') or diag.get('fresh') is False:
            from .outline_facts import inspect_outline
            facts = inspect_outline(root, chapterId=chapter_id)
            args = {'chapterId': chapter_id, 'register': True,
                    **{key: facts[key] for key in ('mode',) if facts.get(key) is not None}}
            action("运行并登记当前正文的完整机械检查", "机械报告缺失或已失效", operation='diagnostics.aggregate', arguments=args,
                   semantic_inputs=tuple(key for key in ('mode', 'intensityLevel') if facts.get(key) is None))
        else:
            action("绑定 review 状态的机械证据并进入阶段 B", "诊断可随阶段 B 一并确认；状态推进与证据登记共同完成",
                   operation='diagnostics.aggregate', arguments={'chapterId': chapter_id, 'register': True, 'advanceLifecycle': True})
        action("准备阶段 B 逐席审校", "阶段 B 必须复核落地项、POV、偏离和新增事实；机械零命中不代替审校",
               operation='task.prepare', arguments={'chapterId': chapter_id, 'task': 'editor', 'view': 'model'})
        return actions
    if status in {"review", "stale"}:
        if not by_kind.get('stage_b_review', {}).get('registered') or by_kind.get('stage_b_review', {}).get('invalidated'):
            action("准备阶段 B 逐席审校", "当前候选尚无阶段 B 结论", operation='task.prepare',
                   arguments={'chapterId': chapter_id, 'task': 'editor', 'view': 'model'})
        if stage_b_payload is not None and stage_b_payload.get("verdict") == "REPAIR_REQUIRED":
            scope = stage_b_payload.get("repairScope")
            action(f"按 repairScope={scope} 返修后重跑完整 aggregator", "阶段 B 结论 REPAIR_REQUIRED",
                   operation='workflow.repair-context', arguments={'chapterId': chapter_id})
            return actions
        if not by_kind.get("title_candidates", {}).get("registered") and not by_kind.get("title_candidates", {}).get("approved"):
            action("workflow chapter-receipt 预检后由作者选定标题，登记 title_candidates", "标题候选尚未登记（或显式 --skip-title）", needs_approval=True,
                   operation='task.prepare', arguments={'chapterId': chapter_id, 'task': 'titler', 'view': 'model'})
        remaining = [b for b in finalize_blockers]
        if remaining:
            for blocker in remaining:
                from .chapter_actions import blocker_route
                operation, arguments, missing = blocker_route(blocker, chapter_id)
                if blocker['code'] in {'ARTIFACTS_NOT_APPROVED', 'ARTIFACT_APPROVAL_HASH_MISMATCH'}:
                    operation, arguments = 'project.workflow.artifact-approve', approve(
                        [row['artifactType'] for row in artifacts if row['registered'] and not row['approved']
                         and row['fresh'] and not row['invalidated']])
                    missing = () if arguments['artifact'] else ('current_reviewed_artifacts',)
                action("处理当前阻塞", f"{blocker['code']}: {blocker['message']}",
                       operation=operation, arguments=arguments, semantic_inputs=missing)
            return actions
        action("作者选定标题方案后入库", "六类工件就绪、预检无阻塞；等待作者批准与入库", needs_approval=True,
               operation='project.workflow.finalize-chapter', arguments={'chapterId': chapter_id,
                   **({'bindingGeneration': chapter['proseBinding']['generation']} if chapter.get('proseBinding') else {})},
               semantic_inputs=('titleCandidateId_or_skipTitle',))
        return actions
    if status == "stable":
        from .prose_binding import OWNED_CLEAN
        if receipt['continuity']['status'] != 'complete':
            action("workflow continuity-preview 确认设定回写候选，作者确认后 continuity-complete", "章节已入库；连续性收尾待确认", needs_approval=True,
                   operation='project.workflow.continuity-preview', arguments={'chapterId': chapter_id})
        if not receipt['factInventory']['complete']:
            action("读取本章事实盘点与回流待办", "事实盘点或源引证尚未完成；连续性确认不代替事实交接",
                   operation='project.workflow.chapter-receipt', arguments={'chapterId': chapter_id, 'view': 'model'})
        if (receipt['ownership'] or {}).get('state') != OWNED_CLEAN:
            action("读取本章正文归属状态", "已入库正文的当前归属或字节需要处理",
                   operation='project.workflow.chapter-receipt', arguments={'chapterId': chapter_id, 'view': 'model'})
        return actions
    return actions


def finalization_blockers(root: Path, structure: dict, chapter: dict) -> list[dict[str, Any]]:
    """本章阶段内的入库阻塞，与状态板 blockers 同一判断。

    FINALIZATION_STAGES 之外的章按定义没有入库阻塞，因此不跑预检；只要阻塞的调用方
    （逐章聚合）也就不必为每章重建工件行和下一步。structure 是调用方已校验的快照。
    """
    if chapter.get("status") not in FINALIZATION_STAGES:
        return []
    return preflight_finalization(root, chapter["chapterId"], structure=structure).blockers


def chapter_status(root: Path, chapter_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        structure = load_structure(root)
    except (StructureError, OSError, ValueError, TypeError) as exc:
        raise StructureError(getattr(exc, "code", "STRUCTURE_INVALID"), str(exc)) from exc
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
    try:
        workflow = _load_workflow(root)
    except (OSError, ValueError, TypeError):
        from .workflow import default_workflow
        workflow = default_workflow()
    status = chapter.get("status")
    receipt = None
    if status == 'stable':
        from .chapter_receipt import _chapter_receipt
        receipt = _chapter_receipt(root, structure, workflow, chapter, view='model')
    # 入库预检只对 review/stale 章给出有意义结论；其他阶段仍跑一次以拿工件级
    # fresh/envelopeValid，但过滤掉阶段外噪声，不把「还没到入库」当阻塞展示。
    # 预检复用本次已加载的同一快照，不再自行重读结构与状态投影。
    preflight = preflight_finalization(root, chapter_id, structure=structure)
    stage_b_payload = None
    stage_b_entry = registered_artifact(workflow, chapter_id, "stage_b_review")
    if stage_b_entry and stage_b_entry.get("artifactPath"):
        from .jsonio import read_json
        try:
            stage_b_payload = read_json(stored_project_path(root, stage_b_entry["artifactPath"])).get("payload")
        except (OSError, ValueError):
            stage_b_payload = None
    stage_blockers = (preflight.blockers if status in FINALIZATION_STAGES else
                      [] if status in {'stable', 'archived'} else [row for row in preflight.blockers
                      if row['code'] in {'CANDIDATE_PLAN_STALE', 'CANDIDATE_STAGE_A_STALE', 'DIAGNOSTICS_STALE'}])
    evidence_notices = [row for row in preflight.blockers if row['code'] in {
        'CANDIDATE_PLAN_STALE', 'CANDIDATE_STAGE_A_STALE', 'DIAGNOSTICS_STALE'}] if status in {'stable', 'archived'} else []
    artifacts = _artifact_rows(workflow, chapter_id, preflight.artifacts)
    from .mechanical_evidence import evidence_current
    diagnostics_status = evidence_current(root, chapter_id, structure=structure)
    for row in artifacts:
        if row['artifactType'] == 'diagnostics':
            row.update(fileFresh=row['fresh'], fresh=diagnostics_status['fresh'],
                       passed=diagnostics_status['passed'], freshnessReason=diagnostics_status.get('reason'))
    if status not in {'stable', 'archived'}:
        from .continuity import require_predecessors_ready
        try:
            require_predecessors_ready(structure, workflow, chapter, root=root)
        except ValueError as exc:
            stage_blockers.append({'code': getattr(exc, 'code', 'PREDECESSOR_INVALID'), 'message': str(exc),
                                   'details': getattr(exc, 'details', {})})
        if chapter_id not in workflow.get('approvedChapterIds', []):
            stage_blockers.append({'code': 'CHAPTER_OUTSIDE_APPROVED_RANGE', 'message': '本章不在作者批准范围内',
                                   'details': {'chapterId': chapter_id}})
    repair_rounds = workflow.get("repairRounds", {}).get(chapter_id)
    from .outline_facts import inspect_outline
    outline = inspect_outline(root, chapterId=chapter_id, structure=structure)
    next_action = workflow.get('nextAction')
    if not isinstance(next_action, dict) or next_action.get('chapterId') != chapter_id:
        next_action = None
    if receipt:
        next_action = receipt['nextAction']
    return {
        "schemaVersion": 1,
        "projectId": structure["projectId"],
        "chapter": {**{key: chapter.get(key) for key in ("chapterId", "displayCode", "title", "status", "filePath")},
                    "volumeId": chapter_volume_id(chapter, structure["schemaVersion"]),
                    "placement": chapter_placement(chapter, structure["schemaVersion"])},
        "stage": status,
        "approvedRange": _approval_scope(workflow, chapter_id),
        "nextAction": next_action,
        **({key: receipt[key] for key in ('continuity', 'factInventory', 'ownership', 'readyForContinuation')} if receipt else {}),
        "artifacts": artifacts,
        "artifactsSummary": {
            "registered": sum(row["registered"] for row in artifacts),
            "approved": sum(row["approved"] for row in artifacts),
            "invalidated": sum(row["invalidated"] for row in artifacts),
        },
        "repairRounds": repair_rounds,
        "blockers": stage_blockers,
        "evidenceNotices": evidence_notices,
        "evidencePurpose": "historical publication evidence; rerun before a new revision" if status in {"stable", "archived"} else "current publication gate",
        "candidatePlanBinding": preflight.context.get('candidatePlanBinding'),
        "diagnosticsEvidence": diagnostics_status,
        "outlineConstraints": outline.get('projectConstraints', {}),
        "writingNotes": next((row.get('payload', {}).get('writingNotes', {}) for row in preflight.artifacts
                               if row['artifactType'] == 'candidate_prose' and isinstance(row.get('payload'), dict)), {}),
        "transactions": pending_transactions(root),
        "nextActions": _next_actions(chapter, workflow, artifacts, stage_b_payload, stage_blockers, root=root, receipt=receipt),
        "coverage": {
            "statusRecomputed": True,
            "machineProvesSemanticCorrectness": False,
            "absenceMeaning": "no blockers means mechanical gates pass; semantic review and author approval are separate",
        },
    }
