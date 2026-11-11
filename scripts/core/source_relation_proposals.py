"""Source-bound, old-record reviewed changes to registered Canon relationships."""
from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from .record_patch import MISSING, expand_patch
from pathlib import Path

from .project import query_policy
from .source_model import SourceIndex, snapshot_hash
from .source_review import fingerprint, source_snapshot
from .structure_commands import _base_plan, _write_plan
from .structure_store import StructureError, canonical_json, validate_structure

MUTABLE_FIELDS = {'kind', 'validDuring', 'perspectiveId', 'basisIds'}


def _ref(source: dict, relation: dict, chapter_id: str | None) -> str:
    payload = {'sourceHash': snapshot_hash(source), 'relationId': relation['relationId'],
               'fromEntity': relation['fromEntity'], 'toEntity': relation['toEntity'],
               'kind': relation['kind'], 'chapterId': chapter_id}
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip('=')


def _in_scope(source: dict, relation: dict, chapter_id: str | None) -> bool:
    if chapter_id is None:
        return True
    chapters = {row['chapterId'] for row in source['chapters'] if row.get('status') != 'archived'}
    if chapter_id not in chapters:
        raise StructureError('RESOLVER_SCOPE_UNKNOWN', 'chapter scope does not exist')
    return any(row['sourceId'] == chapter_id and row['targetId'] in
               {relation['fromEntity'], relation['toEntity']} for row in source.get('references', []))


def resolve_relation_target(root: Path, *, relationId: str | None = None,
                            fromEntity: str | None = None, toEntity: str | None = None,
                            kind: str | None = None, chapterId: str | None = None) -> dict:
    source = source_snapshot(root.resolve())
    if relationId is None and (not all(isinstance(v, str) and v for v in (fromEntity, toEntity, kind))):
        raise StructureError('RELATION_TARGET_INVALID', 'exact relationId or both endpoints and kind required')
    if relationId is not None and (not isinstance(relationId, str) or not relationId or
                                   any(v is not None for v in (fromEntity, toEntity, kind))):
        raise StructureError('RELATION_TARGET_INVALID', 'relationId cannot be mixed with endpoint selector')
    rows = [row for row in source.get('relations', [])
            if (relationId is None or row['relationId'] == relationId) and
            (fromEntity is None or row['fromEntity'] == fromEntity) and
            (toEntity is None or row['toEntity'] == toEntity) and
            (kind is None or row['kind'] == kind) and _in_scope(source, row, chapterId)]
    if chapterId is not None and not rows:
        _in_scope(source, {'fromEntity': '', 'toEntity': ''}, chapterId)
    result = {'ok': True, 'code': 'RELATION_TARGET_RESOLUTION', 'sourceHash': snapshot_hash(source),
              'status': 'resolved' if len(rows) == 1 else 'ambiguous' if rows else 'not_found',
              'matches': [{'relationId': row['relationId'], 'fromEntity': row['fromEntity'],
                           'toEntity': row['toEntity'], 'kind': row['kind']} for row in rows]}
    if len(rows) == 1:
        row = rows[0]
        result.update({'record': deepcopy(row), 'recordHash': fingerprint(row),
                       'sessionRef': _ref(source, row, chapterId)})
    if len(canonical_json(result).encode()) > query_policy()['maxBytes']['detail']:
        raise StructureError('QUERY_BUDGET_EXCEEDED', 'resolved relation exceeds detail response budget')
    return result


def _resolved(source: dict, session_ref: str) -> tuple[dict, dict]:
    if not isinstance(session_ref, str) or len(session_ref) > 2048:
        raise StructureError('SESSION_REF_INVALID', 'invalid session reference')
    try:
        payload = json.loads(base64.urlsafe_b64decode(session_ref + '=' * (-len(session_ref) % 4)))
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise StructureError('SESSION_REF_INVALID', 'unreadable session reference') from exc
    keys = {'sourceHash', 'relationId', 'fromEntity', 'toEntity', 'kind', 'chapterId'}
    if not isinstance(payload, dict) or set(payload) != keys or any(
            not isinstance(payload.get(key), str) for key in keys - {'chapterId', 'sourceHash'}) or \
            (payload.get('chapterId') is not None and not isinstance(payload['chapterId'], str)):
        raise StructureError('SESSION_REF_INVALID', 'session reference has invalid fields')
    if payload['sourceHash'] != snapshot_hash(source):
        raise StructureError('SESSION_REF_STALE', 'source changed after relation resolution')
    row = next((row for row in source.get('relations', []) if row['relationId'] == payload['relationId']), None)
    if row is None or any(row[key] != payload[key] for key in ('fromEntity', 'toEntity', 'kind')) or \
            not _in_scope(source, row, payload['chapterId']) or _ref(source, row, payload['chapterId']) != session_ref:
        raise StructureError('SESSION_REF_INVALID', 'session reference does not identify the current relation')
    return row, payload


def _validate_evidence(source: dict, row: dict, chapter_id: str | None,
                       evidence_mode: str, evidence: list[dict]) -> None:
    if evidence_mode == 'author_declaration':
        if evidence != []:
            raise StructureError('RELATION_EVIDENCE_INVALID', 'author declaration must not carry invented source quotes')
        return
    if evidence_mode != 'source' or not isinstance(evidence, list) or not evidence:
        raise StructureError('RELATION_EVIDENCE_REQUIRED', 'source quote or explicit author declaration required')
    fragments = SourceIndex.build(source).fragments
    field_types = {field['fieldId']: field['valueType'] for field in source.get('fieldDefinitions', [])}
    relevant = False
    endpoints = {row['fromEntity'], row['toEntity']}
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'fragmentId', 'quote'} or \
                not isinstance(item['fragmentId'], str) or not isinstance(item['quote'], str):
            raise StructureError('RELATION_EVIDENCE_INVALID', 'current fact quote required')
        fragment = fragments.get(item['fragmentId'])
        if fragment is None or fragment.get('status') == 'archived' or fragment.get('contentRole') != 'fact' or \
                not item['quote'].strip() or item['quote'] not in fragment['text']:
            raise StructureError('RELATION_EVIDENCE_INVALID', 'quote does not match an active fact fragment')
        linked = set(fragment.get('dependsOn', [])) | set(fragment.get('contentBindings', []))
        for attribute in source.get('attributes', []):
            if attribute['attributeId'] in linked:
                linked.add(attribute['ownerId'])
                if field_types.get(attribute['fieldId']) == 'reference':
                    linked.update(value.get('value') for value in attribute['values']
                                  if isinstance(value.get('value'), str))
        if fragment['ownerId'] in endpoints | {chapter_id} and endpoints <= (linked | {fragment['ownerId']}):
            relevant = True
    if not relevant:
        raise StructureError('RELATION_EVIDENCE_SCOPE_INVALID', 'fact quote has no registered endpoint or chapter relationship')


def build_relation_change(source: dict, *, sessionRef: str, expectedOldRecord=MISSING,
                            newRecord=MISSING, patch: list | None = None, expectedRecordHash: str | None = None, reason: str, evidence: list[dict],
                            evidenceMode: str, approvalActor: str, approvalEvidence: str) -> tuple[dict, dict]:
    current, reference = _resolved(source, sessionRef)
    expectedOldRecord, newRecord = expand_patch(current, expectedOldRecord, newRecord, patch, expectedRecordHash)
    if not isinstance(expectedOldRecord, dict) or expectedOldRecord != current:
        raise StructureError('EXPECTED_OLD_VALUE_CONFLICT', 'relation changed before proposal',
                             {'relationId': current['relationId'], 'currentHash': fingerprint(current)})
    if newRecord is not None and (not isinstance(newRecord, dict) or
                                  {key: value for key, value in newRecord.items() if key not in MUTABLE_FIELDS} !=
                                  {key: value for key, value in current.items() if key not in MUTABLE_FIELDS}):
        raise StructureError('RELATION_PROPOSAL_INVALID', 'preserve relation identity, direction and provenance')
    if newRecord == current:
        raise StructureError('RELATION_CHANGE_EMPTY', 'new relation equals current relation')
    if not isinstance(reason, str) or not reason.strip() or not isinstance(approvalActor, str) or \
            not approvalActor.strip() or not isinstance(approvalEvidence, str) or not approvalEvidence.strip():
        raise StructureError('RELATION_PROPOSAL_INCOMPLETE', 'reason and declared author approval required')
    _validate_evidence(source, current, reference['chapterId'], evidenceMode, evidence)
    proposed = deepcopy(source)
    rows = proposed['relations']
    index = next(i for i, row in enumerate(rows) if row['relationId'] == current['relationId'])
    if newRecord is None:
        rows.pop(index)
    else:
        rows[index] = deepcopy(newRecord)
    validate_structure(proposed)
    proposal = {'relationId': current['relationId'], 'sessionRef': sessionRef,
                'sourceChapterId': reference['chapterId'],
                'expectedOldRecord': deepcopy(current), 'newRecord': deepcopy(newRecord),
                'evidenceMode': evidenceMode, 'evidence': deepcopy(evidence), 'reason': reason,
                'approval': {'actorId': approvalActor, 'evidence': approvalEvidence, 'verifiedHuman': False}}
    proposal['proposalHash'] = hashlib.sha256(canonical_json(proposal).encode()).hexdigest()
    return proposed, proposal


def preview_relation_change(root: Path, *, maintenanceReference: dict | None = None, **arguments) -> dict:
    root = root.resolve()
    source = source_snapshot(root)
    proposed, proposal = build_relation_change(source, **arguments)
    plan = _base_plan(source, operation='canon_relation_proposal', actor='app',
                      parameters={'relationProposal': proposal}, impact={}, proposed=proposed)
    if maintenanceReference is not None:
        plan['parameters']['maintenanceReference'] = maintenanceReference
    return _write_plan(root, plan)
