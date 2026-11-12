"""Scoped resolution and old-value-bound proposals for authored entity facts."""
from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from .record_patch import MISSING, apply_record_patch
from .source_review import fingerprint
from pathlib import Path

from .consistency import require_no_pending
from .entity_registry import entity_candidates, entity_matches, require_resolution_budget
from .source_model import SourceIndex, require_current_source, snapshot_hash
from .structure_commands import _base_plan, _write_plan
from .structure_store import ENTITY_TYPES, StructureError, canonical_json, load_structure, validate_structure


def _ref(source: dict, entity_id: str, *, entity_type: str | None = None,
         chapter_id: str | None = None) -> str:
    raw = canonical_json({'sourceHash': snapshot_hash(source), 'entityId': entity_id,
                          'entityType': entity_type, 'chapterId': chapter_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def resolve_canon_target(root: Path, *, selector: str, entityType: str | None = None,
                         chapterId: str | None = None, includeArchived: bool = False) -> dict:
    root = root.resolve()
    require_no_pending(root)
    source = load_structure(root)
    require_no_pending(root)
    if not isinstance(selector, str) or not selector.strip():
        raise StructureError('RESOLVER_SELECTOR_INVALID', 'nonempty selector required')
    if entityType is not None and entityType not in ENTITY_TYPES:
        raise StructureError('RESOLVER_TYPE_UNKNOWN', 'entity type is unknown')
    if chapterId is not None and chapterId not in {row['chapterId'] for row in source['chapters']}:
        raise StructureError('RESOLVER_SCOPE_UNKNOWN', 'chapter scope does not exist')
    rows = entity_matches(source, selector, entity_type=entityType,
                          chapter_id=chapterId, include_archived=includeArchived)
    result = {'ok': True, 'code': 'CANON_TARGET_RESOLUTION', 'revision': source['revision'],
              'sourceHash': snapshot_hash(source), 'status': 'resolved' if len(rows) == 1 else
              'ambiguous' if rows else 'not_found', 'matches': entity_candidates(source, rows)}
    if len(rows) == 1 and rows[0].get('status') != 'archived':
        result['recordHash'] = fingerprint(rows[0])
        result['sessionRef'] = _ref(source, rows[0]['entityId'], entity_type=entityType, chapter_id=chapterId)
    require_resolution_budget(result)
    return result


def _target_from_ref(source: dict, session_ref: str) -> tuple[dict, dict]:
    if not isinstance(session_ref, str) or len(session_ref) > 2048:
        raise StructureError('SESSION_REF_INVALID', 'invalid session reference')
    try:
        raw = base64.urlsafe_b64decode(session_ref + '=' * (-len(session_ref) % 4))
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise StructureError('SESSION_REF_INVALID', 'unreadable session reference') from exc
    if not isinstance(payload, dict) or set(payload) != {'sourceHash', 'entityId', 'entityType', 'chapterId'}:
        raise StructureError('SESSION_REF_INVALID', 'session reference has unknown fields')
    if not isinstance(payload.get('entityId'), str) or \
            (payload.get('entityType') is not None and not isinstance(payload['entityType'], str)) or \
            (payload.get('chapterId') is not None and not isinstance(payload['chapterId'], str)):
        raise StructureError('SESSION_REF_INVALID', 'session reference has invalid fields')
    if payload['sourceHash'] != snapshot_hash(source):
        raise StructureError('SESSION_REF_STALE', 'source changed after resolution')
    entity = next((row for row in source['entities'] if row['entityId'] == payload['entityId']), None)
    if entity is None or entity.get('status') == 'archived' or \
            (payload['entityType'] is not None and entity['entityType'] != payload['entityType']) or \
            (payload['chapterId'] is not None and not any(row['sourceId'] == payload['chapterId']
                                                        and row['targetId'] == entity['entityId']
                                                        for row in source.get('references', []))) or \
            _ref(source, entity['entityId'], entity_type=payload['entityType'],
                 chapter_id=payload['chapterId']) != session_ref:
        raise StructureError('SESSION_REF_INVALID', 'session reference does not identify an active entity')
    return entity, payload


MUTABLE_FIELDS = {'name', 'aliases', 'status', 'summary', 'absoluteTime', 'timeOrdinal',
                  'timeStartId', 'timeEndId', 'ruleSpec', 'voiceProfile', 'designations'}


def _value(entity: dict, path: str):
    if path in MUTABLE_FIELDS:
        return path in entity, entity.get(path)
    if path.startswith('fields.') and len(path) > len('fields.') and '.' not in path[len('fields.'):]:
        fields = entity.get('fields') or {}
        key = path[len('fields.'):]
        return key in fields, fields.get(key)
    raise StructureError('CANON_FIELD_INVALID', 'unsupported entity field path')


def build_canon_change(source: dict, *, sessionRef: str, fieldPath: str | None = None, expectedOldExists=MISSING, expectedOldValue=MISSING,
                         newValue=MISSING, patch: list | None = None, expectedRecordHash: str | None = None, reason: str, evidence: list[dict], approvalActor: str,
                         approvalEvidence: str) -> tuple[dict, dict]:
    require_current_source(source)
    entity, reference = _target_from_ref(source, sessionRef)
    if patch is not None:
        if any(value is not MISSING for value in (expectedOldExists, expectedOldValue, newValue)) or fieldPath is not None:
            raise StructureError('RECORD_PATCH_INVALID', 'choose field values or patch')
        # Canon's single-field proposal preserves its established evidence/approval scope.
        _, changed = apply_record_patch(entity, patch, expectedRecordHash)
        if len(patch) != 1:
            raise StructureError('RECORD_PATCH_INVALID', 'canon proposal accepts one field per proposal')
        if any(not isinstance(k, str) for k in patch[0]['path']):
            raise StructureError('CANON_FIELD_INVALID', 'canon proposal requires an object field')
        fieldPath = '.'.join(patch[0]['path'])
        expectedOldExists, expectedOldValue = _value(entity, fieldPath)
        _, newValue = _value(changed, fieldPath)
    elif expectedRecordHash is not None:
        raise StructureError('RECORD_PATCH_INVALID', 'expectedRecordHash requires patch')
    if not isinstance(fieldPath, str):
        raise StructureError('CANON_FIELD_INVALID', 'field path must be a string')
    actual_exists, actual = _value(entity, fieldPath)
    if type(expectedOldExists) is not bool or actual_exists != expectedOldExists or \
            type(actual) is not type(expectedOldValue) or actual != expectedOldValue:
        raise StructureError('EXPECTED_OLD_VALUE_CONFLICT', 'authoritative value changed before proposal',
                             {'entityId': entity['entityId'], 'fieldPath': fieldPath,
                              'actualExists': actual_exists, 'actual': actual})
    if fieldPath == 'aliases':
        valid_value = isinstance(newValue, list) and all(isinstance(item, str) and item.strip() for item in newValue)
    elif fieldPath == 'status':
        valid_value = isinstance(newValue, str) and newValue in {'active', 'archived'}
    elif fieldPath.startswith('fields.') or fieldPath in {'summary', 'name', 'absoluteTime', 'timeStartId', 'timeEndId'}:
        valid_value = newValue is None or isinstance(newValue, str)
        if fieldPath == 'name':
            valid_value = isinstance(newValue, str) and bool(newValue.strip())
    else:
        valid_value = newValue is None or isinstance(newValue, (dict, list, str, int, float)) and type(newValue) is not bool
    if not valid_value:
        raise StructureError('CANON_VALUE_INVALID', 'new value has the wrong type for the selected field')
    if not isinstance(reason, str) or not reason.strip() or not isinstance(approvalActor, str) or not approvalActor.strip() or \
            not isinstance(approvalEvidence, str) or not approvalEvidence.strip():
        raise StructureError('CANON_PROPOSAL_INCOMPLETE', 'reason and declared author approval are required')
    if not isinstance(evidence, list) or not evidence:
        raise StructureError('CANON_EVIDENCE_REQUIRED', 'at least one evidence item is required')
    fragments = SourceIndex.build(source).fragments
    relevant_evidence = False
    for item in evidence:
        if (not isinstance(item, dict) or set(item) != {'fragmentId', 'quote'} or
                not isinstance(item['fragmentId'], str) or not isinstance(item['quote'], str) or not item['quote'].strip() or
                item['fragmentId'] not in fragments or fragments[item['fragmentId']].get('status') == 'archived' or
                fragments[item['fragmentId']].get('contentRole') != 'fact' or
                item['quote'] not in fragments[item['fragmentId']]['text']):
            raise StructureError('CANON_EVIDENCE_INVALID', 'evidence quote must match a current source fragment')
        if fragments[item['fragmentId']]['ownerId'] in {entity['entityId'], reference['chapterId']}:
            relevant_evidence = True
    if not relevant_evidence:
        raise StructureError('CANON_EVIDENCE_SCOPE_INVALID',
                             'at least one current quote must belong to the target or resolved chapter')
    if actual == newValue:
        raise StructureError('CANON_CHANGE_EMPTY', 'new value equals expected old value')
    proposed = deepcopy(source)
    target = next(row for row in proposed['entities'] if row['entityId'] == entity['entityId'])
    if fieldPath.startswith('fields.'):
        field = fieldPath[len('fields.'):]
        if newValue is None:
            target.get('fields', {}).pop(field, None)
            if not target.get('fields'):
                target.pop('fields', None)
        else:
            target.setdefault('fields', {})[field] = newValue
    elif newValue is None:
        target.pop(fieldPath, None)
    else:
        target[fieldPath] = newValue
    validate_structure(proposed)
    proposal = {'entityId': entity['entityId'], 'entityType': entity['entityType'],
                'sourceChapterId': reference['chapterId'], 'fieldPath': fieldPath,
                'expectedOldExists': expectedOldExists, 'expectedOldValue': expectedOldValue,
                'newValue': newValue, 'reason': reason,
                'evidence': evidence, 'approval': {'actorId': approvalActor, 'evidence': approvalEvidence,
                                                   'verifiedHuman': False}}
    proposal['proposalHash'] = hashlib.sha256(canonical_json(proposal).encode()).hexdigest()
    return proposed, proposal


def preview_canon_change(root: Path, *, maintenanceReference: dict | None = None, **arguments) -> dict:
    root = root.resolve()
    require_no_pending(root)
    source = load_structure(root)
    proposed, proposal = build_canon_change(source, **arguments)
    plan = _base_plan(source, operation='canon_change_proposal', actor='app',
                      parameters={'canonProposal': proposal}, impact={}, proposed=proposed)
    if maintenanceReference is not None:
        plan['parameters']['maintenanceReference'] = maintenanceReference
    return _write_plan(root, plan)
