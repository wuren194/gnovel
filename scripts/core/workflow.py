"""Validated workflow state and chapter routing primitives."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .artifacts import file_sha256
from .paths import ProjectPathError, stored_project_path
from .review_artifacts import validate_artifact_binding

WORKFLOW_SCHEMA_VERSION = 2
ACTIONS = {
    "planner",
    "writer",
    "mechanical",
    "editor",
    "titler",
    "continuity",
    "commit",
    "router",
    "await_author",
}

STATUS_TO_ACTIONS = {
    "planning_required": {"planner", "router"},
    "planned": {"writer", "router"},
    "draft": {"mechanical", "editor", "router"},
    "review": {"editor", "titler", "router"},
    "stable": {"continuity", "planner", "router"},
    "stale": {"router", "planner"},
}
STATUS_TRANSITIONS = {
    "planning_required": {"planned"},
    "planned": {"draft"},
    "draft": {"review"},
    "review": {"draft", "stable"},
    "stable": {"stale"},
    "stale": {"review"},
}


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def default_workflow() -> dict[str, Any]:
    return {
        "schemaVersion": WORKFLOW_SCHEMA_VERSION,
        "approvedChapterIds": [],
        "chapterStates": {},
        "artifacts": {},
        "approvals": {},
        "repairRounds": {},
        "continuity": {},
        "nextAction": None,
        "updatedAt": None,
    }


def normalize_workflow(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schemaVersion") != WORKFLOW_SCHEMA_VERSION:
        raise WorkflowError("WORKFLOW_STATE_INVALID", "unsupported workflow schema", {"expected": WORKFLOW_SCHEMA_VERSION})
    result = default_workflow()
    result.update(data)
    for key in ("approvedChapterIds", "chapterStates", "artifacts", "approvals", "repairRounds", "continuity"):
        if not isinstance(result.get(key), type(default_workflow()[key])):
            raise WorkflowError("WORKFLOW_STATE_INVALID", f"workflow field must be {key}")
    next_action = result.get("nextAction")
    if next_action is not None:
        validate_next_action(next_action)
    return result


def validate_next_action(action: dict[str, Any]) -> None:
    if not isinstance(action, dict):
        raise WorkflowError("NEXT_ACTION_INVALID", "nextAction must be an object")
    chapter_id = str(action.get("chapterId", "")).strip()
    name = str(action.get("action", "")).strip()
    if not chapter_id:
        raise WorkflowError("NEXT_ACTION_INVALID", "nextAction.chapterId is required")
    if name not in ACTIONS:
        raise WorkflowError("NEXT_ACTION_INVALID", f"unsupported nextAction.action: {name}", {"supported": sorted(ACTIONS)})
    if not isinstance(action.get("needsApproval", False), bool):
        raise WorkflowError("NEXT_ACTION_INVALID", "nextAction.needsApproval must be boolean")


def transition_chapter_status(structure: dict[str, Any], chapter_id: str, target_status: str) -> dict[str, Any]:
    """Apply one explicitly allowed lifecycle transition to a chapter."""
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise WorkflowError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
    current_status = chapter.get("status")
    if target_status not in STATUS_TRANSITIONS.get(current_status, set()):
        raise WorkflowError(
            "INVALID_STATE_TRANSITION",
            f"cannot transition chapter from {current_status} to {target_status}",
            {"chapterId": chapter_id, "from": current_status, "to": target_status},
        )
    chapter["status"] = target_status
    return chapter


def current_serial_chapter(structure):
    """The first unfinished chapter owns the current placement, not volume status."""
    chapters = sorted((row for row in structure.get('chapters', []) if row.get('status') != 'archived'),
                      key=lambda row: row['displayOrdinal'])
    return next((row for row in chapters if row.get('status') != 'stable'), chapters[-1] if chapters else None)


def require_serial_scope(structure, workflow, chapter, *, root=None, planning=False,
                         current_volume_only=True, prewarm_next_chapter=False):
    from .source_placement import chapter_volume_id
    current = current_serial_chapter(structure)
    if current_volume_only and current is not None:
        volume = chapter_volume_id(current, structure['schemaVersion'])
        if chapter_volume_id(chapter, structure['schemaVersion']) != volume:
            raise WorkflowError('CHAPTER_OUTSIDE_CURRENT_VOLUME', 'chapter is outside the current unfinished placement',
                                {'chapterId': chapter['chapterId'], 'currentChapterId': current['chapterId'], 'currentVolumeId': volume})
    if planning:
        # Completed conditional candidates count only after their real batch
        # evidence has been validated. Future planning rows do not advance this frontier.
        ordered = sorted((row for row in structure.get('chapters', []) if row.get('status') != 'archived'),
                         key=lambda row: row['displayOrdinal'])
        for index, previous in enumerate(ordered):
            if previous['displayOrdinal'] >= chapter['displayOrdinal']:
                break
            if previous.get('status') == 'stable':
                continue
            if root is not None:
                from .candidate_batch import _current_recipe, candidate_handoff
                if _current_recipe(root, structure, workflow, previous['chapterId']) is not None:
                    try:
                        candidate_handoff(root, structure, workflow, previous['chapterId'])
                        continue
                    except (ValueError, OSError):
                        pass
            if prewarm_next_chapter and ordered[index + 1]['chapterId'] == chapter['chapterId']:
                # Only planning may prewarm the immediate successor. Its older
                # predecessors still need the same formal/conditional evidence.
                return previous
            raise WorkflowError('PREWARM_DISABLED', 'finish the preceding chapter before planning beyond it',
                                {'chapterId': chapter['chapterId'], 'predecessorChapterId': previous['chapterId']})
    return chapter


def validate_next_action_for_project(
    action: dict[str, Any],
    structure: dict[str, Any],
    workflow: dict[str, Any],
    *,
    root: Path | None = None,
    chapter_range_required: bool = True,
    current_volume_only: bool = True,
    prewarm_next_chapter: bool = False,
) -> dict[str, Any]:
    validate_next_action(action)
    chapter_id = action["chapterId"]
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise WorkflowError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
    approved = set(workflow.get("approvedChapterIds", []))
    if chapter_range_required and chapter_id not in approved and action["action"] != "continuity":
        raise WorkflowError("CHAPTER_OUTSIDE_APPROVED_RANGE", f"chapter is not approved: {chapter_id}", {"approvedChapterIds": sorted(approved)})
    status = chapter.get("status")
    allowed = STATUS_TO_ACTIONS.get(status, {"router"})
    if action["action"] not in allowed:
        raise WorkflowError(
            "INVALID_STATE_TRANSITION",
            f"action {action['action']} is not allowed while chapter status is {status}",
            {"chapterId": chapter_id, "status": status, "allowedActions": sorted(allowed)},
        )
    # Reject an impossible action before inspecting unrelated placement/order
    # inputs. In particular, planning_required cannot go straight to writer.
    predecessor_boundary = chapter
    if action['action'] != 'continuity':
        predecessor_boundary = require_serial_scope(structure, workflow, chapter, root=root, planning=action['action'] == 'planner',
                             current_volume_only=current_volume_only, prewarm_next_chapter=prewarm_next_chapter)
    if action["action"] in {"planner", "writer", "mechanical", "editor", "titler", "commit"}:
        from .continuity import require_predecessors_ready
        try:
            require_predecessors_ready(structure, workflow, predecessor_boundary, root=root, formal=action['action'] == 'commit')
        except ValueError as exc:
            raise WorkflowError(getattr(exc, 'code', 'PREDECESSOR_INVALID'), str(exc), getattr(exc, 'details', {})) from exc
    return {"chapter": chapter, "status": status, "allowedActions": sorted(allowed)}


def set_next_action(
    workflow: dict[str, Any],
    structure: dict[str, Any],
    *,
    root: Path | None = None,
    chapter_id: str,
    action: str,
    reason: str = "",
    needs_approval: bool = False,
    chapter_range_required: bool = True,
    current_volume_only: bool = True,
    prewarm_next_chapter: bool = False,
) -> dict[str, Any]:
    candidate = {"chapterId": chapter_id, "action": action, "reason": reason, "needsApproval": needs_approval}
    validate_next_action_for_project(candidate, structure, workflow, root=root, chapter_range_required=chapter_range_required, current_volume_only=current_volume_only, prewarm_next_chapter=prewarm_next_chapter)
    workflow["nextAction"] = candidate
    return workflow


def current_next_action(workflow: dict[str, Any], structure: dict[str, Any], *, root: Path | None = None, chapter_range_required: bool = True) -> dict[str, Any] | None:
    action = workflow.get("nextAction")
    if action is None:
        return None
    validate_next_action_for_project(action, structure, workflow, root=root, chapter_range_required=chapter_range_required, current_volume_only=False)
    return action


def registered_artifact(workflow: dict[str, Any], chapter_id: str, artifact_type: str) -> dict[str, Any] | None:
    entry = workflow.get("artifacts", {}).get(chapter_id, {}).get(artifact_type)
    return None if entry and entry.get("invalidatedBy") else entry


def require_approved_artifact(workflow: dict[str, Any], chapter_id: str, artifact_type: str) -> str:
    """Return the approved artifact hash, or fail closed when the gate is unmet.

    登记只说明工件存在；只有批准过的 hash 才能作为闸门证据。两者分开检查，
    错误码不同——「没交」与「交了但没批准」需要不同的处理动作。
    """
    entry = registered_artifact(workflow, chapter_id, artifact_type)
    if entry is None:
        raise WorkflowError(
            "ARTIFACT_NOT_REGISTERED",
            f"{artifact_type} is not registered for {chapter_id}",
            {"chapterId": chapter_id, "artifactType": artifact_type},
        )
    approval = workflow.get("approvals", {}).get(chapter_id, {})
    if not approval.get("approved"):
        raise WorkflowError(
            "ARTIFACTS_NOT_APPROVED",
            f"{artifact_type} is registered but not approved for {chapter_id}",
            {"chapterId": chapter_id, "artifactType": artifact_type},
        )
    approved_hash = approval.get("artifactHashes", {}).get(artifact_type)
    if approved_hash != entry.get("artifactSha256"):
        raise WorkflowError(
            "ARTIFACT_APPROVAL_HASH_MISMATCH",
            f"approved {artifact_type} hash is stale for {chapter_id}",
            {"chapterId": chapter_id, "artifactType": artifact_type},
        )
    return str(approved_hash)


def artifact_replacement(workflow: dict, chapter_id: str, artifact: dict) -> dict | None:
    """Receipt projection only: a new envelope never inherits the old approval."""
    kind = artifact['artifactType']
    previous = workflow.get('artifacts', {}).get(chapter_id, {}).get(kind)
    if not isinstance(previous, dict) or previous.get('artifactSha256') == artifact['artifactSha256']:
        return None
    approval = workflow.get('approvals', {}).get(chapter_id, {})
    return {'previousArtifactSha256': previous.get('artifactSha256'),
            'newArtifactSha256': artifact['artifactSha256'],
            'previouslyApproved': bool(approval.get('approved') and not previous.get('invalidatedBy')
                and approval.get('artifactHashes', {}).get(kind) == previous.get('artifactSha256')),
            'approvalInherited': False}


def record_artifact(
    workflow: dict[str, Any],
    chapter_id: str,
    artifact: dict[str, Any],
    *,
    structure: dict[str, Any],
    artifact_path: str | None = None,
    project_id: str | None = None,
    project_root: Path | None = None,
    artifact_bytes: bytes | None = None,
    _read_guards: list[dict] | None = None,
) -> dict[str, Any]:
    """Register one artifact against a real, non-archived chapter.

    structure 是必需参数：曾经它可选，调用方省略后章节存在性检查被整体跳过，
    workflow 里会积累指向不存在章节的 artifact 与 approval（幽灵工件），
    既无法入库也无法可靠清理。
    """
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise WorkflowError(
            "ARTIFACT_CHAPTER_UNKNOWN",
            f"cannot register an artifact for unknown chapter: {chapter_id}",
            {"chapterId": chapter_id},
        )
    if chapter.get("status") == "archived":
        raise WorkflowError(
            "ARTIFACT_CHAPTER_ARCHIVED",
            f"cannot register an artifact for an archived chapter: {chapter_id}",
            {"chapterId": chapter_id},
        )
    validate_artifact_binding(artifact, project_id=project_id, chapter_id=chapter_id)
    if artifact['artifactType'] == 'stage_a_review':
        from .review_plan import require_stage_a_completion
        require_stage_a_completion(structure, chapter_id, artifact['payload'])
        from .relationship_causality_review import validate_causality_completion
        validate_causality_completion(structure, chapter_id, artifact['payload'], 'stage_a_review')
    if artifact['artifactType'] == 'stage_b_review' and project_root is None:
        from .relationship_causality import chapter_causality
        from .behavior_review import pending_binding_fields
        if (chapter_causality(structure, chapter_id) or 'relationshipCausalityReviews' in artifact['payload']
                or pending_binding_fields(artifact['payload'])
                or artifact['payload'].get('cognitionReviews')
                or artifact['payload'].get('verdict') == 'POLISH_READY'):
            raise WorkflowError('CAUSALITY_REVIEW_MATERIAL_REQUIRED',
                                'relationship review registration requires the current project candidate')
        from .relationship_causality_review import validate_causality_completion
        validate_causality_completion(structure, chapter_id, artifact['payload'], 'stage_b_review')
    if artifact['artifactType'] == 'stage_a_review' and project_root is not None:
        from .outline_facts import require_mode_level_review
        require_mode_level_review(project_root, structure, chapter_id, artifact['payload'])
        if artifact['payload'].get('candidateRecipe') is not None:
            from .candidate_batch import validate_recipe, recipe_guards
            validate_recipe(project_root, structure, workflow, chapter_id, artifact['payload']['candidateRecipe'])
            if _read_guards is not None:
                _read_guards.extend(recipe_guards(project_root, structure, workflow, chapter_id, artifact['payload']['candidateRecipe']))
    if artifact['artifactType'] == 'candidate_prose' and project_root is not None:
        from .writing_notes import validate_candidate_binding, validate_notes
        validate_candidate_binding(project_root, structure, workflow, chapter_id, artifact['payload'])
        from .current_candidate import require_candidate_selection
        selected = require_candidate_selection(project_root, structure, chapter_id, artifact['payload'])
        if _read_guards is not None:
            _read_guards.extend(selected['guards'])
        from .mechanical_evidence import _registered
        previous = _registered(project_root, structure, workflow, chapter_id, 'candidate_prose', historical=True)
        if previous and previous['payload'].get('writingNotes') and previous['payload'] != artifact['payload']:
            if 'writingNotes' not in artifact['payload']:
                raise WorkflowError('WRITING_NOTES_REQUIRED', 'replacement must explicitly retain, revise or withdraw existing writing notes')
            if selected['selection'] is not None:
                frozen_previous = artifact['payload'].get('selectionHandoff', {}).get('previousCandidate')
                if not isinstance(frozen_previous, dict) or frozen_previous.get('artifactSha256') != previous['artifactSha256']:
                    raise WorkflowError('CANDIDATE_HANDOFF_STALE', 'writing notes changed after the selection handoff')
        validate_notes(artifact['payload'].get('writingNotes', {}), artifact['payload'].get('text', ''))
        if artifact['payload'].get('writingNotes', {}).get('newFacts'):
            raise WorkflowError('SOURCE_FACTS_REQUIRE_PROPOSAL', 'single-source new facts require source proposals')
    if artifact['artifactType'] == 'stage_b_review' and project_root is not None:
        previous = workflow.get('artifacts', {}).get(chapter_id, {}).get('stage_b_review', {})
        known_history = previous.get('repairHistory', []) if isinstance(previous, dict) else []
        if (any(row.get('decision') == 'resolved' for row in artifact['payload'].get('repairReview', {}).get('decisions', []))
                and artifact['artifactSha256'] != previous.get('artifactSha256')
                and any(row.get('artifactSha256') == artifact['artifactSha256'] for row in known_history)):
            from .mechanical_evidence import _registered
            from .repair_history import stage_b_obligations
            candidate = _registered(project_root, structure, workflow, chapter_id, 'candidate_prose')
            if candidate is not None:
                obligations = stage_b_obligations(project_root, structure, workflow, chapter_id, candidate)
                if obligations['blockingIssueIds'] or obligations['unavailableReports']:
                    raise WorkflowError('REPAIR_HISTORICAL_RESOLUTION',
                        'an old completed envelope cannot dispose of a later reopened finding',
                        {'chapterId': chapter_id, 'pendingIssueIds': obligations['blockingIssueIds']})
        from .review_artifacts import validate_registered_stage_b
        guards = validate_registered_stage_b(project_root, structure, workflow, chapter_id, artifact['payload'])
        if _read_guards is not None:
            _read_guards.extend(guards)
        from .repair_review import validate_current_repair_evidence
        validate_current_repair_evidence(project_root, structure, workflow, chapter_id, artifact['payload'])
    entry = {
        "artifactSha256": artifact["artifactSha256"],
        "sourceSha256": artifact.get("sourceSha256"),
        "artifactPath": artifact_path,
    }
    if artifact['artifactType'] == 'stage_a_review' and artifact['payload'].get('candidateRecipe') is not None:
        from .artifacts import sha256_json
        entry['candidateRecipeHash'] = sha256_json(artifact['payload']['candidateRecipe'])
    if artifact_path:
        path = Path(artifact_path)
        if project_root is not None:
            try:
                path = stored_project_path(project_root, path)
            except ProjectPathError as exc:
                raise WorkflowError(exc.code, str(exc), exc.details) from exc
            try:
                relative = path.relative_to(project_root.expanduser().resolve())
            except ValueError as exc:
                raise WorkflowError("PATH_OUTSIDE_PROJECT", "artifact path escapes project root") from exc
            if "工作台" not in relative.parts:
                raise WorkflowError("ARTIFACT_PATH_FORBIDDEN", "artifacts must be stored under 工作台")
            entry["artifactPath"] = str(path)
        if artifact_bytes is None:
            entry["artifactFileSha256"] = file_sha256(path)
        else:
            import json
            from .artifacts import sha256_bytes
            if json.loads(artifact_bytes) != artifact:
                raise WorkflowError("ARTIFACT_BYTES_MISMATCH", "staged bytes differ from the registered artifact")
            entry["artifactFileSha256"] = sha256_bytes(artifact_bytes)
    previous = workflow.get('artifacts', {}).get(chapter_id, {}).get(artifact['artifactType'])
    if isinstance(previous, dict):
        for prior in [previous, *previous.get('repairHistory', [])]:
            if isinstance(prior, dict) and prior.get('invalidatedBy') and (
                    prior.get('artifactSha256') == entry['artifactSha256'] or
                    prior.get('artifactFileSha256') and prior['artifactFileSha256'] == entry.get('artifactFileSha256')):
                # CLI --bind changes the envelope in memory before registration;
                # the unchanged input file is still the invalidated review.
                raise WorkflowError('ARTIFACT_SOURCE_CHANGED', 'invalidated artifact requires a new submission',
                                    {'chapterId': chapter_id, 'artifactType': artifact['artifactType']})
    from .repair_history import REPAIR_ARTIFACTS, retain_history
    if artifact['artifactType'] in REPAIR_ARTIFACTS:
        retain_history(entry, previous)
        if isinstance(previous, dict) and previous.get('invalidatedBy'):
            for reference in entry.get('repairHistory', []):
                if reference.get('artifactSha256') == previous.get('artifactSha256'):
                    reference['invalidatedBy'] = deepcopy(previous['invalidatedBy'])
    if artifact['artifactType'] == 'candidate_prose':
        previous = workflow.get('artifacts', {}).get(chapter_id, {}).get('candidate_prose')
        if previous and previous.get('artifactSha256') != artifact['artifactSha256']:
            from .evidence_invalidation import invalidate_prose_evidence
            invalidate_prose_evidence(workflow, chapter_id, {'reason': 'candidate_replacement',
                'candidateSha256': artifact['artifactSha256']})
    workflow.setdefault("artifacts", {}).setdefault(chapter_id, {})[artifact["artifactType"]] = entry
    return workflow


def approve_artifacts(
    workflow: dict[str, Any],
    chapter_id: str,
    *,
    artifact_hashes: dict[str, str],
    approval_mode: str,
    intensity_level: int | None = None,
    extreme_level: int = 10,
    artifact_paths: dict[str, str] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    if approval_mode not in {"manual", "auto"}:
        raise WorkflowError("APPROVAL_MODE_INVALID", f"unsupported approval mode: {approval_mode}")
    if approval_mode == "auto" and intensity_level is not None and intensity_level >= extreme_level:
        raise WorkflowError("EXTREME_LEVEL_MANUAL_REQUIRED", "extreme-level content requires manual approval")
    registered = workflow.get("artifacts", {}).get(chapter_id, {})
    missing = sorted(set(artifact_hashes) - set(registered))
    if missing:
        raise WorkflowError("ARTIFACT_NOT_REGISTERED", "approval references unregistered artifacts", {"missing": missing})
    invalidated = sorted(key for key in artifact_hashes if registered[key].get('invalidatedBy'))
    if invalidated:
        raise WorkflowError('ARTIFACT_SOURCE_CHANGED', 'approval references invalidated artifacts',
                            {'chapterId': chapter_id, 'artifactTypes': invalidated})
    mismatches = {
        key: {"expected": registered[key].get("artifactSha256"), "actual": value}
        for key, value in artifact_hashes.items()
        if registered[key].get("artifactSha256") != value
    }
    if mismatches:
        raise WorkflowError("ARTIFACT_APPROVAL_HASH_MISMATCH", "approval hash does not match registered artifact", {"mismatches": mismatches})
    path_mismatches = {}
    for key, relative in (artifact_paths or {}).items():
        path = Path(relative)
        if project_root is not None:
            try:
                path = stored_project_path(project_root, path)
            except ProjectPathError as exc:
                raise WorkflowError(exc.code, str(exc), exc.details) from exc
        actual = file_sha256(path)
        expected_file = registered[key].get("artifactFileSha256")
        if expected_file and actual != expected_file:
            path_mismatches[key] = {"expected": expected_file, "actual": actual}
    if path_mismatches:
        raise WorkflowError("ARTIFACT_SOURCE_CHANGED", "registered artifact source changed", {"mismatches": path_mismatches})
    if project_root is None and set(artifact_hashes) & {'stage_a_review', 'stage_b_review'}:
        raise WorkflowError('APPROVAL_MATERIAL_REQUIRED', 'review approval requires the current project and registered evidence')
    if project_root is not None and set(artifact_hashes) & {'candidate_prose', 'diagnostics', 'stage_b_review', 'state_candidate'}:
        from .mechanical_evidence import _registered
        from .structure_store import load_structure
        from .writing_notes import validate_candidate_binding
        current_source = load_structure(project_root)
        candidate = _registered(project_root, current_source, workflow, chapter_id, 'candidate_prose')
        if candidate is not None and 'candidate_prose' in artifact_hashes:
            validate_candidate_binding(project_root, current_source, workflow, chapter_id, candidate['payload'])
        elif candidate is None:
            from .current_candidate import require_candidate_selection
            require_candidate_selection(project_root, current_source, chapter_id, None)
    if 'stage_a_review' in artifact_hashes and project_root is not None:
        from .outline_facts import require_mode_level_review
        from .mechanical_evidence import _registered
        from .structure_store import load_structure
        structure = load_structure(project_root)
        review = _registered(project_root, structure, workflow, chapter_id, 'stage_a_review')
        if review is None:
            raise WorkflowError('ARTIFACT_SOURCE_CHANGED', 'stage A is invalidated or unavailable')
        from .review_plan import require_stage_a_completion
        require_stage_a_completion(structure, chapter_id, review['payload'])
        if review['payload'].get('candidateRecipe') is not None:
            from .candidate_batch import validate_recipe
            validate_recipe(project_root, structure, workflow, chapter_id, review['payload']['candidateRecipe'])
        require_mode_level_review(project_root, structure, chapter_id, review['payload'])
        from .outline_facts import inspect_outline
        constraints = inspect_outline(project_root, chapterId=chapter_id, structure=structure).get('projectConstraints', {})
        if constraints.get('conflicts') and review['payload'].get('projectExceptions') != constraints:
            raise WorkflowError('STAGE_A_PROJECT_EXCEPTION_UNREVIEWED', '项目区间例外未绑定当前阶段 A；先展示例外并重新登记工件')
    if 'stage_b_review' in artifact_hashes:
        from .mechanical_evidence import _registered
        from .structure_store import load_structure
        from .review_artifacts import validate_registered_stage_b
        structure = load_structure(project_root)
        review = _registered(project_root, structure, workflow, chapter_id, 'stage_b_review')
        if review is None:
            raise WorkflowError('ARTIFACT_SOURCE_CHANGED', 'stage B is invalidated or unavailable')
        validate_registered_stage_b(project_root, structure, workflow, chapter_id, review['payload'])
    existing = workflow.setdefault("approvals", {}).get(chapter_id, {})
    previous_hashes = existing.get("artifactHashes", {}) if isinstance(existing, dict) and existing.get('approved') else {}
    previous_hashes = {key: value for key, value in previous_hashes.items()
                       if (entry := registered_artifact(workflow, chapter_id, key))
                       and entry.get('artifactSha256') == value}
    merged_hashes = {**previous_hashes, **artifact_hashes}
    workflow.setdefault("approvals", {})[chapter_id] = {
        # These describe an earlier publication/title choice, not approval of
        # current artifacts. Preserve the metadata without reviving stale hashes
        # or invalidatedBy; the sealed continuity receipt proves finalization.
        **{key: deepcopy(existing[key]) for key in ('finalized', 'titleDecision') if key in existing},
        "mode": approval_mode,
        "artifactHashes": merged_hashes,
        "intensityLevel": intensity_level if intensity_level is not None else existing.get("intensityLevel"),
        "approved": True,
    }
    return workflow


def increment_repair_round(workflow: dict[str, Any], chapter_id: str, *, maximum: int) -> int:
    current = int(workflow.setdefault("repairRounds", {}).get(chapter_id, 0))
    if current >= maximum:
        raise WorkflowError("REPAIR_ROUND_LIMIT", f"automatic repair rounds exhausted for {chapter_id}", {"chapterId": chapter_id, "maximum": maximum})
    current += 1
    workflow["repairRounds"][chapter_id] = current
    return current


def record_automatic_repair(workflow: dict[str, Any], chapter_id: str, *, request_id: str) -> dict:
    """Stage one count with the candidate owner; its request receipt owns idempotency."""
    from .project import repair_policy
    previous = workflow.get('repairRounds', {}).get(chapter_id)
    if chapter_id in workflow.get('repairRounds', {}) and (type(previous) is not int or previous < 0):
        raise WorkflowError('REPAIR_ACCOUNTING_INVALID', 'repair round must be a nonnegative integer')
    if not isinstance(request_id, str) or not request_id.strip():
        raise WorkflowError('AUTOMATIC_REPAIR_REQUEST_REQUIRED', 'automatic repair requires a requestId')
    number = increment_repair_round(workflow, chapter_id, maximum=repair_policy()['maxAutomaticRounds'])
    return {'requestId': request_id, 'previousRound': previous, 'round': number}


def validate_automatic_repair(workflow: dict[str, Any], chapter_id: str, accounting: dict,
                              *, request_id: str) -> None:
    """A replay may explain exactly its own committed increment, never later changes."""
    if (not isinstance(accounting, dict) or set(accounting) != {'requestId', 'previousRound', 'round'}
            or accounting['requestId'] != request_id
            or accounting['previousRound'] is not None and (
                type(accounting['previousRound']) is not int or accounting['previousRound'] < 0)
            or type(accounting['round']) is not int
            or accounting['round'] != (accounting['previousRound'] or 0) + 1):
        raise WorkflowError('REPAIR_ACCOUNTING_INVALID', 'automatic repair receipt is missing or changed')
    if workflow.get('repairRounds', {}).get(chapter_id) != accounting['round']:
        raise WorkflowError('REPAIR_ACCOUNTING_STALE', 'repair rounds changed after this candidate was registered')
