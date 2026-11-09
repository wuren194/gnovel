"""Exact, old-record-bound proposals for existing source attributes and facts."""
from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .source_model import SourceIndex, snapshot_hash
from .project import query_policy
from .source_review import fingerprint, source_snapshot
from .structure_commands import _base_plan, _write_plan
from .structure_store import StructureError, canonical_json, validate_structure

from .record_patch import MISSING, expand_patch

PROTECTED = {'attribute': ('attributes', 'attributeId'), 'fact': ('fragments', 'fragmentId'), 'planning': ('fragments', 'fragmentId')}


def _record(source: dict, kind: str, record_id: str) -> dict | None:
    collection, key = PROTECTED[kind]
    return next((row for row in source[collection] if row[key] == record_id
                 and (kind == 'attribute' or (row.get('contentRole') == 'fact' if kind == 'fact' else row.get('contentRole') != 'fact')
                      and row.get('status') != 'archived')), None)


def _scope_valid(source: dict, record: dict, chapter_id: str | None) -> bool:
    if chapter_id is None:
        return True
    if chapter_id not in {row['chapterId'] for row in source['chapters'] if row.get('status') != 'archived'}:
        raise StructureError('RESOLVER_SCOPE_UNKNOWN', 'chapter scope does not exist')
    owner = record['ownerId']
    return owner == chapter_id or any(row['sourceId'] == chapter_id and row['targetId'] == owner
                                      for row in source.get('references', []))


def _ref(source: dict, kind: str, record_id: str, owner_id: str, chapter_id: str | None, *, source_hash=None) -> str:
    payload = {'sourceHash': snapshot_hash(source) if source_hash is None else source_hash, 'kind': kind, 'recordId': record_id,
               'ownerId': owner_id, 'chapterId': chapter_id}
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip('=')


def resolve_content_target(root: Path, *, recordId: str, kind: str,
                           chapterId: str | None = None, view: str = 'full') -> dict:
    source = source_snapshot(root.resolve())
    if not isinstance(kind, str) or kind not in PROTECTED or not isinstance(recordId, str) or not recordId or view not in {'full', 'model'}:
        raise StructureError('CONTENT_TARGET_INVALID', 'exact record ID and supported kind required')
    return resolve_record(root, source, recordId, kind, chapterId, view, snapshot_hash(source))


def resolve_record(root, source, recordId, kind, chapterId, view, source_hash):
    """Single and batch resolution share scope, status and binding construction."""
    record = _record(source, kind, recordId)
    if record is not None and not _scope_valid(source, record, chapterId):
        record = None
    result = {'ok': True, 'code': 'CONTENT_TARGET_RESOLUTION', 'status': 'resolved' if record else 'not_found',
              'sourceHash': source_hash, 'kind': kind, 'recordId': recordId,
              'chapterId': chapterId}
    if record is not None:
        result.update({'ownerId': record['ownerId'], 'recordHash': fingerprint(record),
                       'sessionRef': _ref(source, kind, recordId, record['ownerId'], chapterId, source_hash=source_hash)})
        if view == 'full':
            result['record'] = deepcopy(record)
        else:
            from .transport_cache import put
            reference = put(root, 'source-record', {**result, 'record': record})
            result.update(recordRef=reference, recordIncluded=False,
                          readAction={'operation': 'task.snapshot', 'arguments': {'reference': reference}})
    if len(canonical_json(result).encode()) > query_policy()['maxBytes']['detail']:
        raise StructureError('QUERY_BUDGET_EXCEEDED', 'resolved source record exceeds detail response budget',
                             {'operation': 'source.content-resolve', 'arguments': {
                                 'recordId': recordId, 'kind': kind, 'chapterId': chapterId, 'view': 'model'}})
    return result


def _resolved(source: dict, session_ref: str) -> tuple[str, dict, dict]:
    if not isinstance(session_ref, str) or len(session_ref) > 2048:
        raise StructureError('SESSION_REF_INVALID', 'invalid session reference')
    try:
        payload = json.loads(base64.urlsafe_b64decode(session_ref + '=' * (-len(session_ref) % 4)))
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise StructureError('SESSION_REF_INVALID', 'unreadable session reference') from exc
    if not isinstance(payload, dict) or set(payload) != {'sourceHash', 'kind', 'recordId', 'ownerId', 'chapterId'} or \
            payload.get('kind') not in PROTECTED or not isinstance(payload.get('recordId'), str) or \
            not isinstance(payload.get('ownerId'), str) or \
            (payload.get('chapterId') is not None and not isinstance(payload['chapterId'], str)):
        raise StructureError('SESSION_REF_INVALID', 'session reference has invalid fields')
    if payload['sourceHash'] != snapshot_hash(source):
        raise StructureError('SESSION_REF_STALE', 'source changed or session reference is invalid')
    kind = payload['kind']
    record = _record(source, kind, payload['recordId'])
    if record is None or record['ownerId'] != payload['ownerId'] or not _scope_valid(source, record, payload['chapterId']) or \
            _ref(source, kind, payload['recordId'], payload['ownerId'], payload['chapterId']) != session_ref:
        raise StructureError('SESSION_REF_INVALID', 'session reference does not identify an active record')
    return kind, record, payload


def _validate_evidence(source: dict, kind: str, record: dict, chapter_id: str | None,
                       evidence: list[dict]) -> None:
    if not isinstance(evidence, list) or not evidence:
        raise StructureError('CONTENT_EVIDENCE_REQUIRED', 'source evidence is required')
    fragments = SourceIndex.build(source).fragments
    relevant = False
    record_id = record['attributeId'] if kind == 'attribute' else record['fragmentId']
    bound_facts = [fragment for fragment in fragments.values()
                   if fragment.get('contentRole') == 'fact' and fragment.get('status') != 'archived'
                   and record_id in fragment.get('contentBindings', [])] if kind == 'attribute' else []
    for evidence_index, item in enumerate(evidence):
        if (kind == 'attribute' and not bound_facts and isinstance(item, dict)
                and set(item) == {'recordHash'} and item['recordHash'] == fingerprint(record)):
            relevant = True
            continue
        if not isinstance(item, dict) or not isinstance(item.get('fragmentId'), str) or \
                not isinstance(item.get('quote'), str) or set(item) != {'fragmentId', 'quote'}:
            raise StructureError('CONTENT_EVIDENCE_INVALID', 'current fact fragment quote required',
                                 {'evidenceIndex': evidence_index, 'recordId': record_id, 'reason': 'invalid_evidence_shape'})
        fragment = fragments.get(item['fragmentId'])
        failure = ('fragment_not_found' if fragment is None else
                   'fragment_archived' if fragment.get('status') == 'archived' else
                   'non_fact_fragment' if fragment.get('contentRole') != 'fact' else
                   'empty_quote' if not item['quote'].strip() else
                   'quote_not_found' if item['quote'] not in fragment['text'] else None)
        if failure:
            raise StructureError('CONTENT_EVIDENCE_INVALID', f'fact evidence rejected: {failure}',
                                 {'evidenceIndex': evidence_index, 'recordId': record_id,
                                  'fragmentId': item['fragmentId'], 'quote': item['quote'], 'reason': failure,
                                  'contentRole': fragment.get('contentRole') if fragment else None,
                                  'status': fragment.get('status', 'active') if fragment else None,
                                  'requiredContentRole': 'fact'})
        if (kind == 'fact' and item['fragmentId'] == record_id) or \
                (kind == 'attribute' and record_id in fragment['contentBindings'] and
                 fragment['ownerId'] in {record['ownerId'], chapter_id}):
            relevant = True
    if not relevant:
        raise StructureError('CONTENT_EVIDENCE_SCOPE_INVALID',
                             'a bound fact quote or unbound attribute snapshot must identify the target')


def build_content_change(source: dict, *, sessionRef: str, expectedOldRecord: dict,
                           newRecord: dict | None, reason: str, evidence: list[dict],
                           approvalActor: str, approvalEvidence: str) -> tuple[dict, dict]:
    kind, current, reference = _resolved(source, sessionRef)
    if kind == 'planning':
        raise StructureError('CONTENT_PROPOSAL_INVALID', 'planning changes use the source change preview route')
    if not isinstance(expectedOldRecord, dict) or current != expectedOldRecord:
        raise StructureError('EXPECTED_OLD_VALUE_CONFLICT', 'authoritative record changed before proposal',
                             {'recordId': reference['recordId'], 'currentHash': fingerprint(current)})
    if newRecord is not None and (not isinstance(newRecord, dict) or
                                  newRecord.get(PROTECTED[kind][1]) != reference['recordId'] or
                                  newRecord.get('ownerId') != reference['ownerId'] or
                                  kind == 'fact' and newRecord.get('contentRole') != 'fact'):
        raise StructureError('CONTENT_PROPOSAL_INVALID', 'new record must preserve identity, owner and fact role')
    if newRecord == current:
        raise StructureError('CONTENT_CHANGE_EMPTY', 'new record equals current record')
    if not isinstance(reason, str) or not reason.strip() or not isinstance(approvalActor, str) or \
            not approvalActor.strip() or not isinstance(approvalEvidence, str) or not approvalEvidence.strip():
        raise StructureError('CONTENT_PROPOSAL_INCOMPLETE', 'reason and declared approval required')
    _validate_evidence(source, kind, current, reference['chapterId'], evidence)
    proposed = deepcopy(source)
    collection, key = PROTECTED[kind]
    rows = proposed[collection]
    index = next(i for i, row in enumerate(rows) if row[key] == reference['recordId'])
    if newRecord is None:
        rows.pop(index)
    else:
        rows[index] = deepcopy(newRecord)
    validate_structure(proposed)
    proposal = {'kind': kind, 'recordId': reference['recordId'], 'ownerId': reference['ownerId'],
                'sourceChapterId': reference['chapterId'], 'expectedOldRecordHash': fingerprint(current),
                'newRecordHash': fingerprint(newRecord), 'delete': newRecord is None, 'reason': reason,
                'evidence': evidence, 'approval': {'actorId': approvalActor,
                                                   'evidence': approvalEvidence, 'verifiedHuman': False}}
    proposal['proposalHash'] = hashlib.sha256(canonical_json(proposal).encode()).hexdigest()
    return proposed, proposal


def preview_content_change(root: Path, *, sessionRef: str, expectedOldRecord=MISSING,
                           newRecord=MISSING, patch: list | None = None, expectedRecordHash: str | None = None, reason: str, evidence: list[dict],
                           approvalActor: str, approvalEvidence: str, maintenanceReference: dict | None = None) -> dict:
    root = root.resolve()
    source = source_snapshot(root)
    kind, current, reference = _resolved(source, sessionRef)
    expectedOldRecord, newRecord = expand_patch(current, expectedOldRecord, newRecord, patch, expectedRecordHash)
    if kind == 'planning':
        if expectedOldRecord != current:
            raise StructureError('EXPECTED_OLD_VALUE_CONFLICT', 'planning fragment changed')
        if newRecord is not None and (newRecord.get('ownerId') != current['ownerId'] or newRecord.get('fragmentId') != current['fragmentId']):
            raise StructureError('CONTENT_PROPOSAL_INVALID', 'patch must preserve identity and owner')
        if not reason.strip() or not approvalActor.strip() or not approvalEvidence.strip():
            raise StructureError('CONTENT_PROPOSAL_INCOMPLETE', 'reason and declared approval required')
        from .source_changes import apply_changes
        change = {'collection': 'fragments', 'action': 'delete', 'id': current['fragmentId']} if newRecord is None else {
            'collection': 'fragments', 'action': 'put', 'record': newRecord}
        proposed, identities = apply_changes(source, [change])
        plan = _base_plan(source, operation='source_change', actor='app', parameters={
            'reason': reason, 'identityMap': identities, 'expectedRecordHash': fingerprint(current),
            'approval': {'actorId': approvalActor, 'evidence': approvalEvidence, 'verifiedHuman': False}}, impact={}, proposed=proposed)
        from .source_diff import source_diff
        plan['diff'] = source_diff(source, proposed, patches={reference['recordId']: patch or []})
        if maintenanceReference is not None:
            plan['parameters']['maintenanceReference'] = maintenanceReference
        return _write_plan(root, plan)
    proposed, proposal = build_content_change(
        source, sessionRef=sessionRef, expectedOldRecord=expectedOldRecord, newRecord=newRecord,
        reason=reason, evidence=evidence, approvalActor=approvalActor, approvalEvidence=approvalEvidence)
    plan = _base_plan(source, operation='canon_content_proposal', actor='app',
                      parameters={'contentProposal': proposal}, impact={}, proposed=proposed)
    from .source_diff import source_diff
    plan['diff'] = source_diff(source, proposed, patches={reference['recordId']: patch or []})
    if maintenanceReference is not None:
        plan['parameters']['maintenanceReference'] = maintenanceReference
    return _write_plan(root, plan)
