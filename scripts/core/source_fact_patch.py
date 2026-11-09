"""Atomic literal patches over existing facts, using the ordinary proposal boundary."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .source_content_proposals import _record, _ref, build_content_change
from .source_model import snapshot_hash
from .source_review import source_snapshot, fingerprint
from .source_requests import request_identity, resume_request
from .locks import project_lock
from .structure_commands import _base_plan, _write_plan_locked
from .structure_store import StructureError, validate_structure


def preview_fact_patch(root: Path, *, sourceHash: str, patches: list[dict],
                       approvalActor: str, approvalEvidence: str, requestId: str | None = None,
                       maintenanceReference: dict | None = None) -> dict:
    root = root.expanduser().resolve()
    with project_lock(root):
        return _preview_locked(root, sourceHash=sourceHash, patches=patches, approvalActor=approvalActor,
                               approvalEvidence=approvalEvidence, requestId=requestId,
                               maintenanceReference=maintenanceReference)


def _preview_locked(root: Path, *, sourceHash: str, patches: list[dict], approvalActor: str,
                    approvalEvidence: str, requestId: str | None, maintenanceReference: dict | None) -> dict:
    source = source_snapshot(root)
    operation_id = request_fingerprint = None
    if requestId is not None:
        operation_id, request_hash = request_identity(source, requestId)
        request_fingerprint = fingerprint({'requestHash': request_hash, 'operation': 'canon_fact_patch',
            'sourceHash': sourceHash, 'patches': patches, 'approvalActor': approvalActor,
            'approvalEvidence': approvalEvidence, 'maintenanceReference': maintenanceReference})
        resumed = resume_request(root, source, operation_id, request_fingerprint)
        if resumed is not None:
            return resumed
    if sourceHash != snapshot_hash(source):
        raise StructureError('SESSION_REF_STALE', 'source changed before patch preview')
    if not isinstance(patches, list) or not patches:
        raise StructureError('SOURCE_PATCH_INVALID', 'nonempty patches required')
    proposed, proposals, seen = deepcopy(source), [], set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {'recordId', 'replacements', 'reason'}:
            raise StructureError('SOURCE_PATCH_INVALID', 'each patch requires recordId, replacements and reason')
        rid = patch['recordId']
        if not isinstance(rid, str) or rid in seen:
            raise StructureError('SOURCE_PATCH_INVALID', 'each fact may appear only once')
        seen.add(rid)
        old = _record(source, 'fact', rid)
        if old is None:
            raise StructureError('SOURCE_RECORD_NOT_FOUND', 'active fact not found', {'recordId': rid})
        replacements = patch['replacements']
        if not isinstance(replacements, list) or not replacements:
            raise StructureError('SOURCE_PATCH_INVALID', 'nonempty replacements required')
        spans, evidence = [], []
        for replacement in replacements:
            if not isinstance(replacement, dict) or set(replacement) != {'old', 'new'} or \
                    not isinstance(replacement['old'], str) or not replacement['old'] or \
                    not isinstance(replacement['new'], str) or replacement['old'] == replacement['new']:
                raise StructureError('SOURCE_PATCH_INVALID', 'replacement requires distinct old/new strings')
            text, needle = old['text'], replacement['old']
            start = text.find(needle)
            if start < 0 or text.find(needle, start + 1) >= 0:
                raise StructureError('SOURCE_PATCH_MATCH_INVALID', 'old text must occur exactly once in original fact',
                                     {'recordId': rid, 'old': needle, 'match': 'missing' if start < 0 else 'ambiguous'})
            spans.append((start, start + len(needle), replacement['new']))
            evidence.append({'fragmentId': rid, 'quote': needle})
        spans.sort()
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
            raise StructureError('SOURCE_PATCH_OVERLAP', 'replacement spans overlap', {'recordId': rid})
        new = deepcopy(old)
        for start, end, replacement in reversed(spans):
            new['text'] = new['text'][:start] + replacement + new['text'][end:]
        _, proposal = build_content_change(
            source, sessionRef=_ref(source, 'fact', rid, old['ownerId'], None),
            expectedOldRecord=old, newRecord=new, reason=patch['reason'], evidence=evidence,
            approvalActor=approvalActor, approvalEvidence=approvalEvidence)
        proposals.append(proposal)
        proposed['fragments'] = [new if row['fragmentId'] == rid else row for row in proposed['fragments']]
    validate_structure(proposed)
    parameters = {'contentProposals': proposals}
    if maintenanceReference is not None:
        parameters['maintenanceReference'] = maintenanceReference
    plan = _base_plan(source, operation='canon_fact_patch', actor='app',
                      parameters=parameters, impact={}, proposed=proposed)
    from .source_diff import source_diff
    plan['diff'] = source_diff(source, proposed, patches={row['recordId']: [
        {'op': 'text', 'path': ['text'], 'original': item['old'], 'replacement': item['new']}
        for item in row['replacements']] for row in patches})
    if operation_id is not None:
        plan.update({'operationId': operation_id, 'requestId': requestId, 'requestFingerprint': request_fingerprint})
    return _write_plan_locked(root, plan)


def require_fact_patch(current: dict, proposed: dict, plan: dict) -> None:
    """Replay every single-record guard, then reject any unaccounted batch change."""
    from .canon_guard import require_canon_proposal
    proposals = plan.get('parameters', {}).get('contentProposals')
    if not isinstance(proposals, list) or not proposals:
        raise StructureError('CONTENT_PROPOSAL_SCOPE_INVALID', 'batch proposals required')
    expected, seen = deepcopy(current), set()
    after = {row['fragmentId']: row for row in proposed['fragments']}
    for proposal in proposals:
        if not isinstance(proposal, dict) or proposal.get('kind') != 'fact':
            raise StructureError('CONTENT_PROPOSAL_SCOPE_INVALID', 'fact proposals only')
        rid = proposal.get('recordId')
        if not isinstance(rid, str) or rid in seen or rid not in after:
            raise StructureError('CONTENT_PROPOSAL_SCOPE_INVALID', 'duplicate, deleted or invalid target')
        seen.add(rid)
        old = _record(current, 'fact', rid)
        new = after[rid]
        if old is None or {k: v for k, v in new.items() if k != 'text'} != {k: v for k, v in old.items() if k != 'text'}:
            raise StructureError('CONTENT_PROPOSAL_SCOPE_INVALID', 'fact patch may only change text')
        single = deepcopy(current)
        single['fragments'] = [new if row['fragmentId'] == rid else row for row in single['fragments']]
        require_canon_proposal(current, single, {'operation': 'canon_content_proposal',
                                               'parameters': {'contentProposal': proposal}})
        expected['fragments'] = [new if row['fragmentId'] == rid else row for row in expected['fragments']]
    ignored = {'revision', 'updatedAt', 'lastOperation'}
    if any(expected.get(key) != proposed.get(key) for key in expected.keys() | proposed.keys() if key not in ignored):
        raise StructureError('CONTENT_PROPOSAL_SCOPE_INVALID', 'batch changed unrelated source state')
