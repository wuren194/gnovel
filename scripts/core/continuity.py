"""Human-confirmed continuity receipts, separate from committed chapter status."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .artifacts import file_sha256, sha256_json
from .clock import utc_now
from .locks import project_lock_reentrant
from .paths import safe_project_path
from .structure_store import load_structure
from .summary_status import summary_freshness
from .write_sets import apply_write_set, json_bytes


class ContinuityError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _file(root: Path, relative: str) -> dict:
    path = safe_project_path(root, relative)
    if not path.is_file():
        raise ContinuityError("CONTINUITY_SOURCE_MISSING", "continuity evidence file is missing", {"path": relative})
    return {"path": str(path.relative_to(root)), "sha256": file_sha256(path)}


def validated_continuity_records(workflow: dict) -> dict:
    """The single formal continuity-record contract: validate every record or raise.

    This is the only place a continuity record's shape is judged — status, chapterId/key
    agreement, batchHash binding, coversChapterIds/items shape, and (for complete records)
    the confirmation hash. Historical evidence consumers reuse this validation; a dict
    carrying a path and a sha does not suffice. This keeps continuity and finalization
    evidence on one record contract. Current prose ownership is independently established
    by prose_binding; continuity never restores missing or invalid ownership.
    """
    records = workflow.get("continuity", {})
    if not isinstance(records, dict):
        raise ContinuityError("CONTINUITY_STATE_INVALID", "continuity records must be an object")
    for chapter_id, record in records.items():
        if not isinstance(record, dict) or record.get("status") not in {"pending", "complete"}:
            raise ContinuityError("CONTINUITY_STATE_INVALID", "invalid continuity record", {"chapterId": chapter_id})
        if not isinstance(record.get("batchHash"), str) or not isinstance(record.get("sourceFiles"), list):
            raise ContinuityError("CONTINUITY_STATE_INVALID", "continuity record lacks its source binding", {"chapterId": chapter_id})
        if record.get('sourceMode') is not True or record.get('legacyBaseline') is not False or record.get('coversChapterIds') != []:
            raise ContinuityError('CONTINUITY_STATE_INVALID', 'continuity requires a current single-chapter source receipt', {'chapterId': chapter_id})
        try:
            valid = record["chapterId"] == chapter_id and record["batchHash"] == _batch_hash(record)
            valid = valid and isinstance(record["coversChapterIds"], list) and isinstance(record["items"], list)
            if record["status"] == "complete":
                valid = valid and isinstance(record.get("confirmation"), dict)
                valid = valid and record.get("confirmationHash") == sha256_json(record.get("confirmation"))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ContinuityError("CONTINUITY_STATE_INVALID", "continuity record binding is invalid", {"chapterId": chapter_id})
    return records


def validated_continuity_record(workflow: dict, chapter_id: str) -> dict | None:
    """Return the formally valid continuity record for chapter_id, or None when absent.

    A malformed record anywhere in the workflow raises CONTINUITY_STATE_INVALID (via
    validated_continuity_records): invalid evidence is never silently downgraded into a valid
    absence. A well-formed workflow with no record for this chapter returns None — no
    continuity receipt exists, which the caller distinguishes from invalid evidence.
    """
    return validated_continuity_records(workflow).get(chapter_id)


def _records(workflow: dict) -> dict:
    return validated_continuity_records(workflow)


def finalization_evidence(root: Path, structure: dict, workflow: dict, chapter: dict) -> dict:
    """Verify the existing finalization seal for the current clean prose generation.

    Finalization writes factReview and the chapter state in the same transaction.
    Later approvals may replace B, or source edits may invalidate it for a new
    revision; neither changes that historical publication. This does not restore
    ownership, approve current artifacts, or rerun historical semantic judgments.
    """
    from .prose_binding import OWNED_CLEAN, require_observation
    from .repair_history import read_versions

    chapter_id = chapter['chapterId']
    observation = require_observation(root, structure, chapter, (OWNED_CLEAN,),
                                      action='verify historical finalization')
    record = validated_continuity_record(workflow, chapter_id)

    def missing(reason: str, **details):
        raise ContinuityError('STABLE_CHAPTER_APPROVAL_MISSING',
            'stable chapter lacks verified finalization evidence for its current prose generation',
            {'chapterId': chapter_id, 'reason': reason, **details})

    if record is None:
        missing('finalization_receipt_missing')
    review = record.get('factReview')
    prose_sha = observation['observedContentDigest'].removeprefix('sha256:')
    generation = observation['bindingGeneration']
    if (not isinstance(review, dict) or type(review.get('bindingGeneration')) is not int
            or review != {'artifactSha256': review.get('artifactSha256'),
                          'proseSha256': prose_sha, 'bindingGeneration': generation}):
        missing('finalization_prose_binding_mismatch')
    state = workflow.get('chapterStates', {}).get(chapter_id, {})
    if (not isinstance(state, dict) or state.get('status') != 'stable'
            or not record.get('stateArtifactSha256')
            or state.get('sourceArtifactSha256') != record['stateArtifactSha256']):
        missing('finalization_state_binding_mismatch')
    if {'path': chapter['filePath'], 'sha256': prose_sha} not in record['sourceFiles']:
        missing('finalization_source_binding_mismatch')
    versions, unavailable = read_versions(root, structure, workflow, chapter_id, 'stage_b_review')
    historical = next((row['report'] for row in versions
                       if row['report']['artifactSha256'] == review['artifactSha256']), None)
    if (historical is None or historical['payload'].get('verdict') != 'POLISH_READY'
            or historical['payload'].get('finalProseSha256') != prose_sha):
        missing('finalization_review_unavailable', artifactSha256=review['artifactSha256'],
                unavailableReports=[row for row in unavailable
                                    if row['artifactSha256'] == review['artifactSha256']])
    return {'batchHash': record['batchHash'], 'stateArtifactSha256': record['stateArtifactSha256'],
            'factReview': dict(review)}


def _batch_hash(record: dict) -> str:
    payload = {key: record[key] for key in (
        "chapterId", "legacyBaseline", "coversChapterIds", "sourceFiles", "stateArtifactSha256", "items"
    )}
    if "sourceMode" in record:
        payload["sourceMode"] = record["sourceMode"]
    if 'factReview' in record:
        payload['factReview'] = record['factReview']
    if 'candidateConversion' in record:
        payload['candidateConversion'] = record['candidateConversion']
    return sha256_json(payload)


def _ordered(structure: dict) -> list[dict]:
    return sorted((row for row in structure.get("chapters", []) if isinstance(row, dict)),
                  key=lambda row: (row.get("displayOrdinal", 0), str(row.get("chapterId", ""))))


def pending_continuity(structure: dict, workflow: dict, *, before_ordinal: int | None = None) -> list[dict]:
    records = _records(workflow)
    pending = []
    for chapter in _ordered(structure):
        if chapter.get("status") != "stable" or (
                before_ordinal is not None and chapter.get("displayOrdinal", 0) >= before_ordinal):
            continue
        chapter_id = chapter.get("chapterId")
        if not chapter_id:
            continue
        record = records.get(chapter_id)
        if record is not None:
            if record["status"] != "complete":
                pending.append({"chapterId": chapter_id, "displayOrdinal": chapter.get("displayOrdinal"), "status": "pending"})
        else:
            pending.append({'chapterId': chapter_id, 'displayOrdinal': chapter.get('displayOrdinal'),
                            'status': 'receipt_missing'})
    return sorted(pending, key=lambda row: row["displayOrdinal"] or 0)


def require_predecessors_ready(structure: dict, workflow: dict, chapter: dict, *, root=None, candidate_recipe=None, formal=False) -> None:
    if root is not None and not formal:
        from .candidate_batch import require_candidate_predecessors
        if require_candidate_predecessors(root, structure, workflow, chapter, recipe=candidate_recipe):
            return
    ordinal = chapter.get("displayOrdinal") or 0
    previous = [row for row in _ordered(structure) if (row.get("displayOrdinal") or 0) < ordinal]
    unstable = [row["chapterId"] for row in previous if row.get("status") != "stable"]
    if unstable:
        raise ContinuityError("PREDECESSOR_UNSTABLE", "earlier chapters are not committed", {"chapterIds": unstable})
    pending = pending_continuity(structure, workflow, before_ordinal=ordinal)
    if pending:
        raise ContinuityError("CONTINUITY_REQUIRED", "complete earlier continuity before starting this chapter", {"pending": pending})
    if root is not None:
        from .fact_inventory import require_predecessor_facts
        require_predecessor_facts(root, structure, workflow, before_ordinal=ordinal)


def new_continuity_record(root: Path, chapter: dict, sync: dict, state_artifact_sha: str,
                          *, source_hashes: dict[str, str] | None = None, fact_review=None, candidate_conversion=None) -> dict:
    items = [{"itemId": f"change-{index + 1}", "document": row["document"], "section": row["section"],
              "identity": row["identity"], "collection": row["collection"], "suggestedChange": deepcopy(row)} for index, row in enumerate(sync["rows"])]
    record = {
        "chapterId": chapter["chapterId"], "status": "pending", "legacyBaseline": False,
        "coversChapterIds": [], "stateArtifactSha256": state_artifact_sha,
        "sourceFiles": [{"path": relative, "sha256": source_hashes[relative]} if source_hashes and relative in source_hashes
                        else _file(root, relative) for relative in
                        (chapter["filePath"], ".gnovel/structure.json")],
        "items": items,
        "sourceMode": True,
    }
    if fact_review is not None:
        record['factReview'] = deepcopy(fact_review)
    if candidate_conversion is not None:
        record['candidateConversion'] = deepcopy(candidate_conversion)
    record["batchHash"] = _batch_hash(record)
    return record


def continuity_status(root: Path, structure: dict, workflow: dict, chapter: dict) -> dict:
    record = _records(workflow).get(chapter["chapterId"])
    if chapter.get("status") != "stable":
        return {"status": "not_committed", "readyForContinuation": False}
    if record is None:
        return {'status': 'receipt_missing', 'readyForContinuation': False}
    from .fact_inventory import inventory_status
    facts = inventory_status(root, structure, workflow, chapter['chapterId'])
    return {"status": record["status"], 'continuityComplete': record['status'] == 'complete',
            'factInventory': facts, "readyForContinuation": record["status"] == "complete" and facts['complete'],
            "batchHash": record["batchHash"], "completedAt": record.get("completedAt")}


def continuity_preview(root: Path, chapter_id: str) -> dict:
    from .finalization import _load_workflow, pending_transactions

    root = root.expanduser().resolve()
    structure, workflow = load_structure(root), _load_workflow(root)
    chapter = next((row for row in structure["chapters"] if row["chapterId"] == chapter_id), None)
    if chapter is None or chapter.get("status") != "stable":
        raise ContinuityError("CONTINUITY_CHAPTER_NOT_COMMITTED", "continuity requires a committed chapter")
    record = deepcopy(_records(workflow).get(chapter_id))
    if record is None:
        raise ContinuityError('CONTINUITY_RECEIPT_MISSING', 'no finalization continuity receipt exists for this chapter',
                              {'chapterId': chapter_id})
    blockers = []
    if record["batchHash"] != _batch_hash(record):
        blockers.append({"code": "CONTINUITY_BATCH_INVALID", "message": "continuity batch binding is invalid"})
    if record["status"] != "complete":
        for source in record["sourceFiles"]:
            try:
                if _file(root, source["path"]) != source:
                    blockers.append({"code": "CONTINUITY_SOURCE_CHANGED", "path": source["path"]})
            except (ValueError, OSError) as exc:
                blockers.append({"code": getattr(exc, "code", "CONTINUITY_SOURCE_MISSING"), "message": str(exc)})
        registered = workflow.get("artifacts", {}).get(chapter_id, {}).get("state_candidate", {})
        if registered.get("artifactSha256") != record["stateArtifactSha256"]:
            blockers.append({"code": "CONTINUITY_STATE_CHANGED", "message": "registered state candidate changed"})
    if pending_transactions(root):
        blockers.append({"code": "TRANSACTION_INCOMPLETE", "message": "recover the interrupted transaction first"})
    template = {
        "decisions": [{"itemId": item["itemId"], "resolution": "pending", "reason": "",
                       "evidence": []} for item in record["items"]],
        "summary": {"resolution": "pending", "reason": ""},
    }
    freshness = summary_freshness(root, structure, target_chapter_id=chapter_id)
    from .operation_schemas import CONTINUITY_CONFIRMATION, CONTINUITY_DECISION
    from .fact_inventory import inventory_status
    facts = inventory_status(root, structure, workflow, chapter_id)
    return {"ok": not blockers, "code": "CONTINUITY_PREVIEW", "chapterId": chapter_id,
            "status": record['status'],
            "readyForContinuation": record["status"] == "complete" and not blockers and facts["complete"],
            "continuityComplete": record["status"] == "complete" and not blockers, "factInventory": facts,
            "batchHash": record["batchHash"], "batch": record, "blockers": blockers,
            "summaryFreshness": freshness,
            "confirmationGuide": {
                "decisions.resolution": list(CONTINUITY_DECISION['properties']['resolution']['enum']),
                "summary.resolution": list(CONTINUITY_CONFIRMATION['properties']['summary']['properties']['resolution']['enum']),
                "summary": "refreshed 使用摘要提交返回的 sha256（与 summaryVersion 相同）；not_required 须说明理由。",
                "facts": "新增事实通过源提案和正文证据登记；空批次不证明小说没有新增事实。",
            },
            "confirmationTemplate": template}


def _reason(value: Any) -> bool:
    from .project import review_policy
    return isinstance(value, str) and bool(value.strip()) and value.strip().casefold() not in {
        word.casefold() for word in review_policy()['unfinishedConclusions']}


def _validate_confirmation(root: Path, preview: dict, confirmation: dict) -> None:
    if not isinstance(confirmation, dict) or not isinstance(confirmation.get("decisions"), list):
        raise ContinuityError("CONTINUITY_CONFIRMATION_INVALID", "decisions must be an array")
    decisions = confirmation["decisions"]
    if any(not isinstance(row, dict) or not isinstance(row.get("itemId"), str) for row in decisions):
        raise ContinuityError("CONTINUITY_CONFIRMATION_INVALID", "every decision needs an itemId")
    expected = {row["itemId"] for row in preview["batch"]["items"]}
    if len(decisions) != len(expected) or {row["itemId"] for row in decisions} != expected:
        raise ContinuityError("CONTINUITY_DECISIONS_INCOMPLETE", "confirm each batch item exactly once")
    for row in decisions:
        if row.get("resolution") not in {"applied", "no_change"} or not _reason(row.get("reason")):
            raise ContinuityError("CONTINUITY_RESOLUTION_REQUIRED", "each change requires a resolution and reason")
        evidence = row.get("evidence", [])
        if not isinstance(evidence, list) or (row["resolution"] == "applied" and not evidence):
            raise ContinuityError("CONTINUITY_EVIDENCE_REQUIRED", "applied changes require current document hashes")
        if evidence:
            raise ContinuityError('CONTINUITY_EVIDENCE_INVALID', 'source facts require source proposals and prose evidence, not Markdown decisions')
    summary = confirmation.get("summary")
    if not isinstance(summary, dict) or not _reason(summary.get("reason")):
        raise ContinuityError("CONTINUITY_SUMMARY_REQUIRED", "record how the story summary was handled")
    if summary.get("resolution") == "refreshed":
        if preview["summaryFreshness"]["stale"]:
            raise ContinuityError("CONTINUITY_SUMMARY_STALE", "the story summary still needs refreshing")
        if summary.get("sha256") != _file(root, ".gnovel/aux/story-summary.json")["sha256"]:
            raise ContinuityError("CONTINUITY_SUMMARY_CHANGED", "confirmed story summary has changed")
    elif summary.get("resolution") != "not_required":
        raise ContinuityError("CONTINUITY_SUMMARY_REQUIRED", "summary resolution must be refreshed or not_required")


def complete_continuity(root: Path, chapter_id: str, *, batch_hash: str, confirmation: dict, approved: bool,
                        _closeout_request=None) -> dict:
    from .finalization import _load_workflow, derive_next_action

    if not approved:
        raise ContinuityError("CONTINUITY_APPROVAL_REQUIRED", "explicit author approval is required, including in auto mode")
    root = root.expanduser().resolve()
    with project_lock_reentrant(root):
        preview = continuity_preview(root, chapter_id)
        if preview.get("batchHash") != batch_hash:
            raise ContinuityError("CONTINUITY_BATCH_CHANGED", "confirmation does not match the current continuity batch")
        if preview.get("blockers"):
            raise ContinuityError("CONTINUITY_BLOCKED", "resolve continuity blockers first", {"blockers": preview["blockers"]})
        record = preview["batch"]
        if record["status"] == "complete":
            if record["confirmation"] != confirmation:
                raise ContinuityError("CONTINUITY_ALREADY_COMPLETE", "this batch already has a different confirmation")
            return {"ok": True, "code": "CONTINUITY_IDEMPOTENT", "chapterId": chapter_id, "readyForContinuation": preview["readyForContinuation"], "continuityComplete": True,
                    "summaryDisposition": deepcopy(confirmation['summary']), "summaryFreshness": preview['summaryFreshness']}
        _validate_confirmation(root, preview, confirmation)
        structure, workflow = load_structure(root), _load_workflow(root)
        workflow.setdefault("continuity", {})[chapter_id] = {
            **record, "status": "complete", "completedAt": utc_now(),
            "confirmation": deepcopy(confirmation), "confirmationHash": sha256_json(confirmation),
            **({'conversionReceipt': record['candidateConversion']['witnessHash']} if record.get('candidateConversion') else {}),
        }
        workflow["nextAction"] = derive_next_action(structure, workflow, root=root)
        workflow["updatedAt"] = utc_now()
        outputs = {".gnovel/workflow.json": json_bytes(workflow)}
        guards = []
        expected = None
        if _closeout_request is not None:
            from .chapter_closeout import receipt_outputs
            receipt, guards, expected = receipt_outputs(root, _closeout_request, 'continuity', outputs,
                {'chapterId': chapter_id, 'batchHash': batch_hash,
                 **({'candidateConversionHash': record['candidateConversion']['witnessHash']} if record.get('candidateConversion') else {}),
                 'confirmationHash': workflow['continuity'][chapter_id]['confirmationHash']})
            outputs.update(receipt)
        apply_write_set(root, outputs, operation="complete_continuity", guards=guards, expected=expected)
    current = continuity_status(root, structure, workflow, next(c for c in structure["chapters"] if c["chapterId"] == chapter_id))
    return {"ok": True, "code": "CONTINUITY_COMPLETED", "chapterId": chapter_id,
            "readyForContinuation": current["readyForContinuation"], "continuityComplete": True, "batchHash": batch_hash, "nextAction": workflow["nextAction"],
            "summaryDisposition": deepcopy(confirmation["summary"]),
            "summaryFreshness": preview["summaryFreshness"]}
