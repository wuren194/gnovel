"""App-authored atomic batches with tool-allocated stable identities."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

from .entity_registry import PREFIX_BY_TYPE
from .project import source_model_policy
from .locks import project_lock
from .source_model import BASE_COLLECTIONS, require_current_source
from .structure_commands import _base_plan, _write_plan_locked
from .structure_store import StructureError, canonical_json, load_structure, new_id, validate_structure
from .source_requests import request_identity, resume_request


def _collections() -> dict:
    collections = {name: key for name, (key, _) in BASE_COLLECTIONS.items()}
    collections['relations'] = 'relationId'
    collections['pairs'] = 'pairId'
    collections.update({name: spec['idField'] for name, spec in source_model_policy()['collections'].items()})
    return collections


def apply_changes(source: dict, changes: list[dict], *, pair_assessment: dict | None = None) -> tuple[dict, dict]:
    if not isinstance(changes, list) or (not changes and pair_assessment is None) or any(not isinstance(c, dict) for c in changes):
        raise StructureError('SOURCE_CHANGES_INVALID', 'changes must be a nonempty list of objects')
    proposed, pending, identities = deepcopy(source), deepcopy(changes), {}
    if pair_assessment is not None:
        proposed['pairAssessment'] = deepcopy(pair_assessment)
    collections = _collections()
    from .source_identity import source_identities
    allocated = source_identities(source)

    def allocate(row, key, kind):
        identity = row.get(key)
        if identity is None or (isinstance(identity, str) and identity.startswith('$')):
            created = new_id(kind, content_role=row.get('contentRole'))
            if created in allocated:
                raise StructureError('SOURCE_ID_COLLISION', 'generated identity is already in use', {'id': created})
            allocated.add(created)
            if identity is not None:
                if identity in identities:
                    raise StructureError('SOURCE_HANDLE_DUPLICATE', 'a temporary identity was declared twice')
                identities[identity] = created
            row[key] = created

    for change in pending:
        if change.get('action') == 'patch':
            from .record_patch import apply_record_patch
            if (set(change) != {'action', 'collection', 'id', 'expectedRecordHash', 'patch'}
                    or change['collection'] != 'fragments' or not isinstance(change['id'], str)):
                raise StructureError('SOURCE_CHANGES_INVALID', 'patch requires a bound existing planning fragment')
            old = next((row for row in source['fragments'] if row['fragmentId'] == change['id']), None)
            if old is None or old.get('status') == 'archived' or old.get('contentRole') == 'fact':
                raise StructureError('CONTENT_PROPOSAL_REQUIRED', 'use a reviewed fact proposal, not a generic planning patch')
            _, updated = apply_record_patch(old, change['patch'], change['expectedRecordHash'])
            if updated.get('fragmentId') != old['fragmentId'] or updated.get('ownerId') != old['ownerId']:
                raise StructureError('SOURCE_CHANGES_INVALID', 'planning patches preserve identity and owner')
            change.clear()
            change.update(action='put', collection='fragments', record=updated)
        if set(change) - {'action', 'collection', 'record', 'id'} or not isinstance(change.get('collection'), str) or change['collection'] not in collections:
            raise StructureError('SOURCE_CHANGES_INVALID', 'unknown change fields or collection')
        if not isinstance(change.get('action'), str) or change['action'] not in {'put', 'delete'}:
            raise StructureError('SOURCE_CHANGES_INVALID', 'action must be put or delete')
        collection = change['collection']
        if change['action'] == 'put':
            row = change.get('record')
            if not isinstance(row, dict) or 'id' in change:
                raise StructureError('SOURCE_CHANGES_INVALID', 'put requires a record without a separate id')
            key = collections[collection]
            kind = key.removesuffix('Id')
            if collection == 'entities':
                entity_type = row.get('entityType')
                if not isinstance(entity_type, str) or entity_type not in PREFIX_BY_TYPE:
                    raise StructureError('SOURCE_CHANGES_INVALID', 'new entity requires a known entityType')
                kind = entity_type
            if collection == 'fragments' and row.get('contentRole') not in source_model_policy()['contentRoles']:
                raise StructureError('SOURCE_ROLE_INVALID', 'fragment creation requires an explicit contentRole')
            allocate(row, key, kind)
            if collection == 'attributes' and isinstance(row.get('values'), list):
                for value in row['values']:
                    if isinstance(value, dict):
                        allocate(value, 'valueId', 'value')
        elif 'record' in change or not isinstance(change.get('id'), str):
            raise StructureError('SOURCE_CHANGES_INVALID', 'delete requires an id only')

    def resolve(value, key=''):
        if isinstance(value, dict):
            return {k: resolve(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, key) for v in value]
        if isinstance(value, str) and value.startswith('$') and (key.endswith('Id') or key in {
                'id', 'fromEntity', 'toEntity', 'contentBindings', 'dependsOn', 'volumeIds',
                'scopeIds', 'inputIds', 'eventIds', 'characterIds', 'basisIds', 'perspectiveIds'}):
            if value not in identities:
                raise StructureError('SOURCE_HANDLE_UNKNOWN', 'temporary identity was not declared', {'handle': value})
            return identities[value]
        return value

    seen = set()
    for change in resolve(pending):
        collection = change['collection']; key = collections[collection]
        rid = change['record'].get(key) if change['action'] == 'put' else change['id']
        if not isinstance(rid, str) or (collection, rid) in seen:
            raise StructureError('SOURCE_CHANGE_DUPLICATE', 'each identity may be changed once per batch')
        seen.add((collection, rid))
        rows = proposed.setdefault(collection, [])
        existing = next((i for i, row in enumerate(rows) if row[key] == rid), None)
        if change['action'] == 'delete':
            if existing is None:
                raise StructureError('SOURCE_RECORD_NOT_FOUND', 'cannot delete unknown identity', {'id': rid})
            if collection == 'entities':
                raise StructureError('CANON_PROPOSAL_REQUIRED', 'entity deletion requires a separate reviewed operation')
            if collection == 'relations':
                raise StructureError('RELATION_PROPOSAL_REQUIRED', 'existing relation deletion requires a reviewed proposal')
            if collection == 'fieldDefinitions' and any(row['fieldId'] == rid for row in source.get('attributes', [])):
                raise StructureError('FIELD_DEFINITION_MIGRATION_REQUIRED', 'referenced field definition cannot be deleted generically')
            if collection == 'attributes' or collection == 'fragments' and rows[existing].get('contentRole') == 'fact':
                raise StructureError('CONTENT_PROPOSAL_REQUIRED', 'existing attributes and facts require a reviewed proposal')
            rows.pop(existing)
        elif existing is None:
            rows.append(change['record'])
        else:
            if collection == 'entities' and rows[existing] != change['record']:
                raise StructureError('CANON_PROPOSAL_REQUIRED', 'existing entity facts require old-value-bound proposal')
            if collection == 'relations' and rows[existing] != change['record']:
                raise StructureError('RELATION_PROPOSAL_REQUIRED', 'existing relation requires old-record-bound proposal')
            if collection == 'fragments' and rows[existing].get('contentRole') != 'fact' and \
                    change['record'].get('contentRole') == 'fact':
                raise StructureError('FACT_PROMOTION_REVIEW_REQUIRED', 'existing planning fragment cannot become a fact by generic overwrite')
            if collection == 'fieldDefinitions' and rows[existing] != change['record'] and any(
                    row['fieldId'] == rid for row in source.get('attributes', [])):
                raise StructureError('FIELD_DEFINITION_MIGRATION_REQUIRED', 'referenced field definition cannot be updated generically')
            if (collection == 'attributes' or collection == 'fragments' and rows[existing].get('contentRole') == 'fact') and \
                    rows[existing] != change['record']:
                raise StructureError('CONTENT_PROPOSAL_REQUIRED', 'existing attributes and facts require old-value-bound proposal')
            rows[existing] = change['record']
    validate_structure(proposed)
    return proposed, identities


def preview_source_change(root: Path, *, changes: list[dict], requestId: str, baseRevision: int | None = None,
                          reason: str, actor: str = 'app', maintenanceReference: dict | None = None,
                          resolutionRef: str | None = None, pairAssessment: dict | None = None) -> dict:
    if not isinstance(requestId, str) or not requestId.strip() or not isinstance(reason, str) or not reason.strip():
        raise StructureError('SOURCE_REQUEST_INVALID', 'requestId and reason are required')
    with project_lock(root):
        return _preview_locked(root, changes=changes, request_id=requestId, base_revision=baseRevision, reason=reason,
                               actor=actor, maintenance_reference=maintenanceReference, resolution_ref=resolutionRef,
                               pair_assessment=pairAssessment)


def _preview_locked(root: Path, *, changes: list[dict], request_id: str, base_revision: int | None, reason: str,
                    actor: str, maintenance_reference: dict | None = None, resolution_ref: str | None = None,
                    chapter_facts: dict | None = None, pair_assessment: dict | None = None) -> dict:
    source = load_structure(root)
    require_current_source(source)
    if type(base_revision) is not int and not (base_revision is None and isinstance(resolution_ref, str)):
        raise StructureError('SOURCE_REQUEST_INVALID', 'baseRevision must be an integer')
    operation_id, request_hash = request_identity(source, request_id)
    payload = {'requestHash': request_hash, 'changes': changes, 'baseRevision': base_revision, 'reason': reason}
    if pair_assessment is not None:
        payload['pairAssessment'] = pair_assessment
    if maintenance_reference is not None:
        payload['maintenanceReference'] = maintenance_reference
    if resolution_ref is not None:
        payload['resolutionRef'] = resolution_ref
    if chapter_facts is not None:
        payload['chapterFacts'] = chapter_facts
    fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    resumed = resume_request(root, source, operation_id, fingerprint)
    if resumed is not None:
        return resumed
    if resolution_ref is not None:
        from .source_resolution_batch import bind_planning_changes
        changes, base_revision = bind_planning_changes(root, source, changes, base_revision, resolution_ref)
    if source['revision'] != base_revision:
        raise StructureError('REVISION_CONFLICT', 'source changed before the requested batch')
    proposed, identities = apply_changes(source, changes, pair_assessment=pair_assessment)
    plan = _base_plan(source, operation='source_change', actor=actor,
                      parameters={'reason': reason, 'identityMap': identities},
                      impact={}, proposed=proposed)
    if maintenance_reference is not None:
        plan['parameters']['maintenanceReference'] = maintenance_reference
    if chapter_facts is not None:
        plan['parameters']['chapterFacts'] = deepcopy(chapter_facts)
    plan['requestId'] = request_id
    plan['operationId'] = operation_id
    plan['requestFingerprint'] = fingerprint
    from .source_diff import source_diff
    plan['diff'] = source_diff(source, proposed, patches={row['id']: row['patch'] for row in changes if row['action'] == 'patch'})
    return _write_plan_locked(root.resolve(), plan)
