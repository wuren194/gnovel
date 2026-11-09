"""Read-only chapter and project receipts backed by finalization preflight."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .source_placement import chapter_placement, chapter_volume_id
from .continuity import continuity_status
from .finalization import _load_workflow, pending_transactions
from .finalization_preflight import preflight_finalization
from .structure_store import StructureError, load_structure, ordered_chapters
from .summary_status import summary_freshness
from .tool_plan import suggested_actions
from .workflow import default_workflow


def bound_receipt_response(root: Path, result: dict) -> dict:
    """Include transport status and resolved chapter references in the same budget."""
    if result.get('code') == 'CHAPTER_RECEIPT' and result.get('view') == 'model':
        from .artifact_pack import bounded_authoring_report
        return bounded_authoring_report(root, result)
    return result


def _chapter_receipt(root: Path, structure: dict[str, Any], workflow: dict[str, Any], chapter: dict[str, Any],
                     *, title_candidate_id: str | None = None, skip_title: bool = False,
                     view: str = 'full') -> dict[str, Any]:
    chapter_id = chapter["chapterId"]
    preflight = preflight_finalization(root, chapter_id, title_candidate_id=title_candidate_id, skip_title=skip_title)
    freshness = summary_freshness(root, structure, target_chapter_id=chapter_id)
    stored_next_action = workflow.get("nextAction")
    next_action = stored_next_action if isinstance(stored_next_action, dict) and stored_next_action.get("chapterId") == chapter_id else None
    continuity = continuity_status(root, structure, workflow, chapter)
    # 已入库章节不会再次入库：对它重跑入库预检必然报「只有 review 才能 finalize」、
    # 「未选择标题」和生成后的基线漂移，这些不是待办。原始清单仍完整保留在
    # finalization 下，收尾进度由 continuity 表达。
    committed = chapter.get("status") in {"stable", "archived"}
    blockers = [] if committed else preflight.blockers
    guidance = {**preflight.context.get('guidance', {}), 'blockerCount': len(blockers)}
    if chapter.get("status") == "stable" and continuity["status"] != "complete":
        next_action = {"chapterId": chapter_id, "action": "continuity", "reason": "continuity confirmation is pending", "needsApproval": True}
    fact_inventory = None
    if chapter.get('status') == 'stable':
        from .fact_inventory import inventory_status
        fact_inventory = inventory_status(root, structure, workflow, chapter_id)
        if continuity['status'] == 'complete' and fact_inventory['complete'] and next_action and next_action.get('action') == 'continuity':
            next_action = None
        if not fact_inventory['complete']:
            next_action = {'chapterId': chapter_id, 'action': 'continuity', 'reason': 'fact inventory or source evidence is pending', 'needsApproval': False}
    approval = workflow.get("approvals", {}).get(chapter_id, {})
    artifacts = preflight.artifacts
    if view == 'model':
        artifacts = []
        for row in preflight.artifacts:
            payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
            compact = {key: value for key, value in row.items() if key != 'payload'}
            compact['summary'] = {key: payload[key] for key in ('verdict', 'decision', 'passed', 'finalProseSha256', 'draftSha256', 'factAssessment') if key in payload}
            for key in ('findings', 'changes', 'candidates'):
                if isinstance(payload.get(key), list):
                    compact['summary'][key + 'Count'] = len(payload[key])
            if isinstance(payload.get('text'), str):
                compact['summary']['characters'] = len(payload['text'])
            artifacts.append(compact)
    finalization_action = None
    if not committed and preflight.ready:
        args = {'chapterId': chapter_id}
        generation = (preflight.ownership or {}).get('bindingGeneration')
        if generation is not None:
            args['bindingGeneration'] = generation
        if skip_title:
            args['skipTitle'] = True
        else:
            args['titleCandidateId'] = title_candidate_id
        finalization_action = {'operation': 'project.workflow.finalize-chapter', 'arguments': args,
                               'authorization': 'existing author approval required; receipt grants none'}
    finalization = ({'applicable': False,
                     'note': '已入库章节不再重新入库；收尾进度见 continuity。',
                     'blockers': preflight.blockers}
                    if committed else {'applicable': True, 'note': '', 'blockers': []})
    if committed and view == 'model':
        # These are non-applicable re-finalization checks, not current work.
        # Preserve their exact same-call evidence without letting historical
        # recovery instructions crowd out current fact handoff quotations.
        from .transport_cache import put
        try:
            reference = put(root, 'authoring-report', {'chapterId': chapter_id, 'finalization': finalization})
        except (OSError, ValueError):
            # Optional historical delivery must not replace current evidence
            # with a new cache error. The existing report budget still applies.
            pass
        else:
            finalization = {'applicable': False, 'note': finalization['note'],
                            'historicalBlockerCount': len(preflight.blockers),
                            'detailsAction': {'operation': 'task.snapshot', 'arguments': {'reference': reference}}}
    return {
        "finalizeAction": finalization_action,
        "chapter": {**{key: chapter.get(key) for key in ("chapterId", "displayCode", "title", "status", "filePath")},
                    "volumeId": chapter_volume_id(chapter, structure["schemaVersion"]),
                    "placement": chapter_placement(chapter, structure["schemaVersion"])},
        "approvedRange": chapter_id in workflow.get("approvedChapterIds", []),
        "approvalScope": workflow.get("approvalScope"),
        "nextAction": next_action,
        "artifacts": artifacts,
        "titleDecision": approval.get("titleDecision") if isinstance(approval, dict) else None,
        "finalizationOptions": {"titleCandidateId": title_candidate_id, "skipTitle": skip_title},
        "finalization": finalization,
        "summaryFreshness": freshness,
        "continuity": continuity,
        "factInventory": fact_inventory,
        "readyForContinuation": continuity["readyForContinuation"] and bool(fact_inventory and fact_inventory["complete"]),
        "transactions": preflight.transactions,
        # 低能力 AI 契约（§16/§21）：ownership 由 core 推导并以机器可读 receipt 暴露，
        # 调用方只负责回显令牌，不自己拼归属逻辑。schema 1/3 上为 null。
        "ownership": preflight.ownership,
        "bindingGeneration": (preflight.ownership or {}).get("bindingGeneration"),
        "blockers": blockers,
        "guidance": guidance,
        "readyToFinalize": False if committed else preflight.ready,
        "suggestedActions": suggested_actions(root, "editor" if chapter.get("status") in {"draft", "review"} else "planner",
                                             chapter_id=chapter_id),
    }


def chapter_receipt(root: Path, chapter_id: str | None = None, *, title_candidate_id: str | None = None,
                    skip_title: bool = False, view: str = 'full', _bounded: bool = True) -> dict[str, Any]:
    """Return a readiness snapshot for exactly the supplied finalization options.

    No title is chosen implicitly. Registered candidates and historical title
    decisions do not substitute for --title-candidate-id or --skip-title.
    """
    if view not in {'full', 'model'}:
        raise StructureError('RECEIPT_VIEW_INVALID', 'view must be full or model')
    root = root.expanduser().resolve()
    from .artifact_pack import bounded_authoring_report
    structure = load_structure(root)
    try:
        workflow = _load_workflow(root)
    except (OSError, ValueError, TypeError):
        # Preflight reports the load failure; presentation still has chapter data.
        workflow = default_workflow()
    if chapter_id is not None:
        chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
        if chapter is None:
            raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
        result = {"schemaVersion": 1, "projectId": structure["projectId"], "view": view,
                "detailsOperation": {"operation": "project.workflow.chapter-receipt", "arguments": {
                    "chapterId": chapter_id, "view": "full", "skipTitle": skip_title,
                    **({"titleCandidateId": title_candidate_id} if title_candidate_id else {})}}, **_chapter_receipt(
            root, structure, workflow, chapter, title_candidate_id=title_candidate_id, skip_title=skip_title, view=view
        )}
        return bounded_authoring_report(root, result) if view == 'model' and _bounded else result

    receipts = [_chapter_receipt(root, structure, workflow, chapter, title_candidate_id=title_candidate_id, skip_title=skip_title)
                for chapter in ordered_chapters(structure)]
    transactions = pending_transactions(root)
    result = {
        "schemaVersion": 1,
        "view": view,
        "projectId": structure["projectId"],
        "bookName": structure.get("bookName", ""),
        "nextAction": workflow.get("nextAction"),
        "approvalScope": workflow.get("approvalScope"),
        "finalizationOptions": {"titleCandidateId": title_candidate_id, "skipTitle": skip_title},
        "summaryFreshness": summary_freshness(root, structure),
        "transactions": transactions,
        "chapters": [
            {
                "chapter": item["chapter"],
                "approvedRange": item["approvedRange"],
                "readyToFinalize": item["readyToFinalize"],
                "continuity": item["continuity"],
                "readyForContinuation": item["readyForContinuation"],
                "blockerCount": len(item["blockers"]),
                "blockers": item["blockers"],
            }
            for item in receipts
        ],
        "healthy": not transactions and not any(item["blockers"] for item in receipts if item["chapter"].get("status") in {"draft", "review"}),
    }
    return bounded_authoring_report(root, result) if view == 'model' and _bounded else result
