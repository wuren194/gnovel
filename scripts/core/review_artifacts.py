"""Validation for Stage A/B review and repair artifacts."""

from __future__ import annotations

from typing import Any

from .artifacts import ArtifactError, validate_envelope

STAGE_B_SLOTS = (
    "writer",
    "reader",
    "scene_design",
    "logic",
    "positioning",
    "voice",
    "relationship",
    "serial_editor",
    "continuity",
    "quality",
)
AUXILIARY_GATES = ("cognition", "organization")
VERDICTS = {"REPAIR_REQUIRED", "POLISH_READY", "INCOMPLETE"}
STAGE_A_DECISIONS = {"adopt", "modify", "reject", "pending"}
REPAIR_SCOPES = {"local", "expansion", "structural"}


class ReviewArtifactError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _require_text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReviewArtifactError("REVIEW_FIELD_REQUIRED", f"{field} must be a non-empty string")


def require_completed_text(value: Any, field: str) -> None:
    from .project import review_policy
    _require_text(value, field)
    if value.strip().casefold() in {word.casefold() for word in review_policy()["unfinishedConclusions"]}:
        raise ReviewArtifactError("REVIEW_PLACEHOLDER", f"{field} still contains a placeholder")


def validate_stage_b_completion(payload: dict, text: str) -> None:
    """Verify completion evidence, never the truth of a literary judgment.

    Historical envelopes remain readable. New registrations and finalization both
    call this gate; old reports without evidence must be reviewed again to publish.
    Evidence quotes use exact Unicode character bounds, not mutable line numbers.
    """
    if payload.get('verdict') != 'POLISH_READY':
        return
    from .artifacts import sha256_text
    from .evidence_quotes import quote_span
    if payload.get('finalProseSha256') != sha256_text(text):
        raise ReviewArtifactError('STAGE_B_PROSE_MISMATCH', 'completed review must bind the current candidate prose')
    for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES):
        item = payload['slots'][slot]
        require_completed_text(item.get('conclusion'), f'slots.{slot}.conclusion')
        if item.get('blocking'):
            raise ReviewArtifactError('REVIEW_COMPLETION_CONFLICT', f'completed review still has a blocking slot: {slot}')
        evidence, reason = item.get('evidence', []), item.get('notApplicableReason')
        if not isinstance(evidence, list):
            raise ReviewArtifactError('REVIEW_EVIDENCE_INVALID', f'slots.{slot}.evidence must be an array')
        if reason is not None:
            require_completed_text(reason, f'slots.{slot}.notApplicableReason')
            if evidence:
                raise ReviewArtifactError('REVIEW_EVIDENCE_INVALID', f'{slot} cannot be both evidenced and not applicable')
        elif not evidence:
            raise ReviewArtifactError('REVIEW_EVIDENCE_REQUIRED', f'{slot} requires current prose quotations or an explicit not-applicable reason', {'slot': slot})
        for index, quote in enumerate(evidence):
            try:
                quote_span(text, quote)
            except ValueError as exc:
                exc.details = {**getattr(exc, 'details', {}), 'artifactType': 'stage_b_review',
                    'slot': slot, 'field': f'slots.{slot}.evidence[{index}]', 'quoteIndex': index,
                    'readAction': {'operation': 'workflow.review-form', 'arguments': {'artifactType': 'stage_b_review'}}}
                raise
    from .fact_inventory import validate_completion
    validate_completion(payload, text)
    if any(row.get('severity') in {'S1', 'S2', 'blocking'} for row in payload.get('findings', [])):
        raise ReviewArtifactError('REVIEW_COMPLETION_CONFLICT', 'completed review still has blocking findings')


def validate_registered_stage_b(root, structure, workflow, chapter_id, payload) -> list[dict]:
    from .mechanical_evidence import _registered
    candidate = _registered(root, structure, workflow, chapter_id, 'candidate_prose')
    body = candidate.get('payload') if candidate is not None else None
    if not isinstance(body, dict) or not isinstance(body.get('text'), str) or not body['text'].strip():
        raise ReviewArtifactError('REVIEW_CANDIDATE_REQUIRED', 'completed review requires a valid registered current candidate')
    from .fact_inventory import validate_retention
    validate_retention(root, structure, workflow, chapter_id, payload, body['text'])
    from .behavior_review import validate_retention as validate_behavior_retention
    validate_behavior_retention(root, structure, workflow, chapter_id, payload, body['text'])
    from .cognition_review import validate_review as validate_cognition_review
    cognition_guards = validate_cognition_review(root, structure, workflow, chapter_id, payload, body['text'])
    validate_stage_b_completion(payload, body['text'])
    from .relationship_causality_review import validate_causality_completion
    validate_causality_completion(structure, chapter_id, payload, 'stage_b_review', body['text'])
    from .writing_notes import validate_writing_decisions
    validate_writing_decisions(payload, candidate)
    from .repair_history import require_obligations_closed
    require_obligations_closed(root, structure, workflow, chapter_id, payload, candidate)
    return cognition_guards


def validate_stage_b(data: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_envelope(data, artifact_type="stage_b_review")
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc
    payload = data["payload"]
    if not isinstance(payload, dict):
        raise ReviewArtifactError("REVIEW_PAYLOAD_INVALID", "stage B payload must be an object")
    _require_text(payload.get('finalProseSha256'), 'payload.finalProseSha256')
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise ReviewArtifactError("REVIEW_VERDICT_INVALID", f"unsupported verdict: {verdict}", {"supported": sorted(VERDICTS)})
    slots = payload.get("slots")
    if not isinstance(slots, dict):
        raise ReviewArtifactError("REVIEW_SLOTS_REQUIRED", "stage B payload.slots must be an object")
    missing = [slot for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES) if slot not in slots]
    if missing:
        raise ReviewArtifactError("REVIEW_SLOTS_MISSING", "stage B review slots are incomplete", {"missing": missing})
    for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES):
        item = slots[slot]
        if not isinstance(item, dict):
            raise ReviewArtifactError("REVIEW_SLOT_INVALID", f"slot {slot} must be an object")
        _require_text(item.get("conclusion"), f"slots.{slot}.conclusion")
        if item.get("blocking") and not item.get("location"):
            raise ReviewArtifactError("REVIEW_BLOCKING_EVIDENCE_REQUIRED", f"blocking slot {slot} needs location")
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise ReviewArtifactError("REVIEW_FINDINGS_INVALID", "findings must be an array")
    identities = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ReviewArtifactError("REVIEW_FINDING_INVALID", f"finding {index} must be an object")
        for field in ("severity", "ruleId", "summary"):
            _require_text(finding.get(field), f"findings[{index}].{field}")
        if 'findingId' in finding:
            _require_text(finding['findingId'], f'findings[{index}].findingId')
            if finding['findingId'] in identities:
                raise ReviewArtifactError('REVIEW_FINDING_ID_DUPLICATE', 'findingId must be unique within a report')
            identities.add(finding['findingId'])
        if finding.get("severity") in {"S1", "S2", "blocking"}:
            retained_unlocated = (finding.get('repairState') == 'needs_location' and verdict != 'POLISH_READY'
                                  and isinstance(payload.get('repairReview'), dict)
                                  and finding.get('findingId') in payload.get('repairReview', {}).get('pendingIssueIds', []))
            if (not finding.get("location") and not retained_unlocated) or not finding.get("repairSuggestion"):
                raise ReviewArtifactError("REVIEW_BLOCKING_EVIDENCE_REQUIRED", f"blocking finding {index} needs location and repairSuggestion")
    scope = payload.get("repairScope")
    if scope is not None and scope not in REPAIR_SCOPES:
        raise ReviewArtifactError("REPAIR_SCOPE_INVALID", f"unsupported repairScope: {scope}")
    if verdict == "REPAIR_REQUIRED" and not scope:
        raise ReviewArtifactError("REPAIR_SCOPE_REQUIRED", "REPAIR_REQUIRED report must declare repairScope")
    if verdict == "POLISH_READY" and scope:
        raise ReviewArtifactError("REPAIR_SCOPE_CONFLICT", "POLISH_READY report cannot declare repairScope")
    from .fact_inventory import validate_shape
    validate_shape(payload)
    from .repair_review import validate_repair_review
    validate_repair_review(payload)
    from .relationship_causality_review import validate_review_shape
    from .behavior_review import validate_shape
    validate_review_shape(payload)
    validate_shape(payload)
    from .cognition_review import validate_shape as validate_cognition_shape
    validate_cognition_shape(payload)
    return data


def validate_plan_binding(payload: dict) -> None:
    if 'outlineSha256' in payload:
        raise ReviewArtifactError('REVIEW_PLAN_BINDING_INVALID', 'only sourcePlanHash can bind the chapter plan')
    _require_text(payload.get('sourcePlanHash'), 'payload.sourcePlanHash')


def validate_stage_a(data: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_envelope(data, artifact_type="stage_a_review")
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ReviewArtifactError("REVIEW_PAYLOAD_INVALID", "stage A payload must be an object")
    decision = payload.get("decision")
    if decision not in STAGE_A_DECISIONS:
        raise ReviewArtifactError("REVIEW_DECISION_INVALID", f"unsupported stage A decision: {decision}")
    if not isinstance(payload.get("activeSeats"), list) or not payload["activeSeats"]:
        raise ReviewArtifactError("REVIEW_SEATS_REQUIRED", "stage A activeSeats must be a non-empty array")
    # 绑定推演文件：没有它，阶段 A 批准无法证明批的是当前这份推演。
    validate_plan_binding(payload)
    for field in ("consensus", "disagreements", "openItems"):
        if not isinstance(payload.get(field, []), list):
            raise ReviewArtifactError("REVIEW_FIELD_INVALID", f"stage A {field} must be an array")
    from .relationship_causality_review import validate_review_shape
    from .behavior_review import validate_shape
    validate_review_shape(payload)
    validate_shape(payload)
    return data


def validate_diagnostics(data: dict[str, Any]) -> dict[str, Any]:
    """机械门报告必须自带结论与正文绑定。

    aggregate_diagnostics.py 已经输出 draftSha256，此前没有任何消费者，
    导致「对正文 A 跑机械门、拿正文 B 入库」在运行时无法被拦下。
    """
    try:
        validate_envelope(data, artifact_type="diagnostics")
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ReviewArtifactError("DIAGNOSTICS_PAYLOAD_INVALID", "diagnostics payload must be an object")
    _require_text(payload.get("draftSha256"), "payload.draftSha256")
    if not isinstance(payload.get("passed"), bool):
        raise ReviewArtifactError("DIAGNOSTICS_VERDICT_REQUIRED", "diagnostics payload.passed must be a boolean")
    return data


def validate_title_candidates(data: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_envelope(data, artifact_type="title_candidates")
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc
    payload = data.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list) or not payload["candidates"]:
        raise ReviewArtifactError("TITLE_CANDIDATES_REQUIRED", "title candidate payload must contain candidates")
    for index, candidate in enumerate(payload["candidates"]):
        if not isinstance(candidate, dict):
            raise ReviewArtifactError("TITLE_CANDIDATE_INVALID", f"candidate {index} must be an object")
        _require_text(candidate.get("title"), f"candidates[{index}].title")
        # candidateId 让「作者选了哪一个」可被记录和复核，而不是入库时另传一个字符串。
        _require_text(candidate.get("candidateId"), f"candidates[{index}].candidateId")
    identifiers = [str(item["candidateId"]) for item in payload["candidates"]]
    if len(set(identifiers)) != len(identifiers):
        raise ReviewArtifactError("TITLE_CANDIDATE_ID_DUPLICATE", "candidateId must be unique within one artifact")
    return data


def validate_repair_plan(data: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_envelope(data, artifact_type="repair_plan")
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc
    payload = data.get("payload")
    if not isinstance(payload, dict) or payload.get("repairScope") not in REPAIR_SCOPES:
        raise ReviewArtifactError("REPAIR_SCOPE_REQUIRED", "repair plan must declare a valid repairScope")
    if not isinstance(payload.get("findings"), list):
        raise ReviewArtifactError("REPAIR_FINDINGS_INVALID", "repair plan findings must be an array")
    return data
def validate_review_artifact(data: dict[str, Any]) -> dict[str, Any]:
    artifact_type = data.get("artifactType") if isinstance(data, dict) else None
    if artifact_type == "stage_a_review":
        return validate_stage_a(data)
    if artifact_type == "stage_b_review":
        return validate_stage_b(data)
    if artifact_type == "diagnostics":
        return validate_diagnostics(data)
    if artifact_type == "title_candidates":
        return validate_title_candidates(data)
    if artifact_type == "repair_plan":
        return validate_repair_plan(data)
    if artifact_type == "state_candidate":
        return validate_state_candidate(data)
    if artifact_type == 'candidate_prose':
        payload = data.get('payload')
        if not isinstance(payload, dict) or not isinstance(payload.get('text'), str) or not payload['text'].strip():
            raise ReviewArtifactError('PROSE_PAYLOAD_INVALID', 'candidate_prose payload.text must be nonempty text')
        from .writing_notes import validate_beat_spans, validate_notes
        validate_beat_spans(payload)
        validate_notes(payload.get('writingNotes', {}), payload['text'])
    try:
        return validate_envelope(data)
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc


def validate_artifact_binding(
    data: dict[str, Any],
    *,
    project_id: str | None = None,
    chapter_id: str | None = None,
) -> dict[str, Any]:
    """Validate optional project/chapter identity without breaking generic use."""
    validate_review_artifact(data)
    if project_id is not None and data.get("projectId") != project_id:
        raise ReviewArtifactError(
            "ARTIFACT_PROJECT_MISMATCH",
            "artifact projectId does not match project",
            {"expected": project_id, "actual": data.get("projectId")},
        )
    if chapter_id is not None and data.get("chapterId") != chapter_id:
        raise ReviewArtifactError(
            "ARTIFACT_CHAPTER_MISMATCH",
            "artifact chapterId does not match chapter",
            {"expected": chapter_id, "actual": data.get("chapterId")},
        )
    return data


def validate_state_candidate(data: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_envelope(data, artifact_type="state_candidate")
    except ArtifactError as exc:
        raise ReviewArtifactError(exc.code, str(exc), exc.details) from exc
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ReviewArtifactError("STATE_CANDIDATE_INVALID", "state candidate payload must be an object")
    validate_plan_binding(payload)
    for field in ("baseStateSha256", "designSha256", "finalProseSha256"):
        _require_text(payload.get(field), f"payload.{field}")
    if payload.get('changes') != [] or payload.get('proposedFacts', []) != []:
        raise ReviewArtifactError('SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION',
                                  'state candidate binds the current source; facts are submitted through source proposals')
    return data
