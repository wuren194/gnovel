"""Mixed Canon updates reuse the original per-record proposal and transaction gates."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .locks import project_lock
from .project import canon_proposal_batch_max_items
from .record_patch import MISSING, expand_patch
from .source_content_proposals import PROTECTED
from .source_model import require_current_source, snapshot_hash
from .source_requests import request_identity, resume_request
from .source_review import fingerprint, source_snapshot
from .structure_commands import _base_plan, _write_plan_locked
from .structure_store import StructureError, validate_structure

TARGETS = {
    'entity': ('entities', 'entityId', 'canon_change_proposal', 'canonProposal'),
    **{kind: (*PROTECTED[kind], 'canon_content_proposal', 'contentProposal') for kind in ('attribute', 'fact')},
    'planning': (*PROTECTED['planning'], 'source_change', 'planningProposal'),
    'relation': ('relations', 'relationId', 'canon_relation_proposal', 'relationProposal'),
    'field_definition': ('fieldDefinitions', 'fieldId', 'canon_field_definition_proposal', 'fieldDefinitionProposal'),
    'field_migration': ('fieldDefinitions', 'fieldId', 'canon_field_migration_proposal', 'fieldMigrationProposal'),
}


def _replace(source, collection, key, record):
    source[collection] = [deepcopy(record) if row[key] == record[key] else row for row in source[collection]]


def _targets(source, kind, record_id):
    if kind == 'field_migration':
        from .source_field_migrations import migration_targets
        return migration_targets(source, record_id)
    collection, key, _, _ = TARGETS[kind]
    return [(collection, key, record_id)]


def _reserve_targets(seen, targets, duplicate_code):
    identities = {(collection, rid) for collection, _, rid in targets}
    if seen & identities:
        raise StructureError(duplicate_code, 'each record may belong to only one proposal per batch',
                             {'recordIds': sorted(rid for _, rid in seen & identities)})
    total = len(seen | identities)
    maximum = canon_proposal_batch_max_items()
    if total > maximum:
        raise StructureError('CANON_BATCH_LIMIT_EXCEEDED', 'expanded proposal targets exceed the batch budget',
                             {'targetCount': total, 'maxTargets': maximum})
    seen.update(identities)


def _merge_records(target, source, targets):
    for collection, key, rid in targets:
        rows = source.get(collection)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or key not in row for row in rows):
            raise StructureError('CANON_BATCH_SCOPE_INVALID', 'batch must retain well-formed source records', {'collection': collection})
        record = next((row for row in rows if row[key] == rid), None)
        if record is None:
            raise StructureError('CANON_BATCH_SCOPE_INVALID', 'batch must preserve its existing records', {'recordId': rid})
        _replace(target, collection, key, record)


def _build_item(source, item, approval_actor, approval_evidence):
    from .canon_proposals import _ref as entity_ref, build_canon_change
    from .source_content_proposals import _ref as content_ref, _record, _scope_valid, build_content_change
    from .source_relation_proposals import _ref as relation_ref, build_relation_change
    from .chapter_refs import resolve_chapter_reference
    kind, rid = item['kind'], item['recordId']
    collection, key, operation, _ = TARGETS[kind]
    old = (_record(source, kind, rid) if kind in {'attribute', 'fact', 'planning'} else
           next((row for row in source[collection] if row[key] == rid), None))
    if old is None:
        raise StructureError('SOURCE_RECORD_NOT_FOUND', 'batch target is not a current record of the selected kind',
                             {'kind': kind, 'recordId': rid})
    chapter = item.get('chapterId')
    if chapter is not None:
        chapter = resolve_chapter_reference(source, chapter)
    shared = {'reason': item['reason'], 'evidence': item['evidence'],
              'approvalActor': approval_actor, 'approvalEvidence': approval_evidence}
    patch = {'patch': item['patch'], 'expectedRecordHash': fingerprint(old)}
    if kind == 'planning':
        from .source_changes import apply_changes
        if not _scope_valid(source, old, chapter):
            raise StructureError('SOURCE_RECORD_NOT_FOUND', 'planning target is outside the selected chapter scope')
        change = {'action': 'patch', 'collection': collection, 'id': rid, **patch}
        proposed, _ = apply_changes(source, [change])
        if proposed == source:
            raise StructureError('CONTENT_CHANGE_EMPTY', 'planning patch does not change the record')
        if item['evidence']:
            raise StructureError('CONTENT_PROPOSAL_INVALID', 'planning patches use the source snapshot; evidence must be empty')
        if not approval_actor.strip() or not approval_evidence.strip():
            raise StructureError('CONTENT_PROPOSAL_INCOMPLETE', 'declared approval is required')
        proposal = {'kind': kind, 'recordId': rid, 'sourceChapterId': chapter, 'change': change, 'reason': item['reason'],
                    'approval': {'actorId': approval_actor, 'evidence': approval_evidence, 'verifiedHuman': False}}
    elif kind == 'entity':
        proposed, proposal = build_canon_change(source, sessionRef=entity_ref(source, rid, chapter_id=chapter),
                                                **patch, **shared)
    elif kind == 'relation':
        proposed, proposal = build_relation_change(source, sessionRef=relation_ref(source, old, chapter),
                                                   evidenceMode=item['evidenceMode'], **patch, **shared)
    elif kind == 'field_definition':
        from .source_field_proposals import build_field_definition_change
        if chapter is not None:
            raise StructureError('FIELD_DEFINITION_SCOPE_INVALID', 'field definitions have project scope, not chapter scope')
        proposed, proposal = build_field_definition_change(source, recordId=rid, patch=item['patch'], **shared)
    elif kind == 'field_migration':
        from .source_field_migrations import build_field_migration
        if chapter is not None:
            raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'field migrations have project scope')
        proposed, proposal = build_field_migration(source, recordId=rid, patch=item['patch'],
                                                   attributeMigrations=item['attributeMigrations'], **shared)
    else:
        previous, updated = expand_patch(old, MISSING, MISSING, item['patch'], fingerprint(old))
        proposed, proposal = build_content_change(source, sessionRef=content_ref(source, kind, rid, old['ownerId'], chapter),
                                                  expectedOldRecord=previous, newRecord=updated, **shared)
    return proposed, {'operation': operation, 'proposal': proposal}


def preview_proposal_batch(root: Path, *, sourceHash: str, items: list, requestId: str,
                           approvalActor: str, approvalEvidence: str, maintenanceReference: dict | None = None) -> dict:
    from .argument_validation import validate_arguments
    from .operation_schemas import canon_proposal_item
    validate_arguments({'type': 'array', 'minItems': 1, 'maxItems': canon_proposal_batch_max_items(),
                        'items': canon_proposal_item()}, items, 'items')
    root = root.expanduser().resolve()
    with project_lock(root):
        source = source_snapshot(root)
        require_current_source(source)
        operation_id, request_hash = request_identity(source, requestId)
        request_fingerprint = fingerprint({'requestHash': request_hash, 'operation': 'canon_proposal_batch',
                                           'sourceHash': sourceHash, 'items': items, 'approvalActor': approvalActor,
                                           'approvalEvidence': approvalEvidence, 'maintenanceReference': maintenanceReference})
        resumed = resume_request(root, source, operation_id, request_fingerprint)
        if resumed is not None:
            return resumed
        if sourceHash != snapshot_hash(source):
            raise StructureError('SESSION_REF_STALE', 'source changed before batch preview')
        proposed, proposals, seen = deepcopy(source), [], set()
        for index, item in enumerate(items):
            try:
                targets = _targets(source, item['kind'], item['recordId'])
                _reserve_targets(seen, targets, 'CANON_BATCH_DUPLICATE')
                single, proposal = _build_item(source, item, approvalActor, approvalEvidence)
                _merge_records(proposed, single, targets)
                proposals.append(proposal)
            except StructureError as exc:
                raise StructureError(exc.code, str(exc), {**exc.details, 'itemIndex': index}) from exc
        validate_structure(proposed)
        parameters = {'proposals': proposals}
        if maintenanceReference is not None:
            parameters['maintenanceReference'] = maintenanceReference
        plan = _base_plan(source, operation='canon_proposal_batch', actor='app', parameters=parameters,
                          impact={}, proposed=proposed)
        plan.update(operationId=operation_id, requestId=requestId, requestFingerprint=request_fingerprint)
        from .source_diff import source_diff
        plan['diff'] = source_diff(source, proposed, patches={item['recordId']: item['patch'] for item in items})
        return _write_plan_locked(root, plan)


def require_proposal_batch(current, proposed, plan):
    """Replay each original gate and prove the final state contains exactly those updates."""
    from .canon_guard import require_canon_proposal
    proposals = plan.get('parameters', {}).get('proposals')
    if (not isinstance(proposals, list) or not proposals
            or len(proposals) > canon_proposal_batch_max_items()):
        raise StructureError('CANON_BATCH_SCOPE_INVALID', 'a bounded nonempty proposal list is required')
    expected, seen = deepcopy(current), set()
    for index, row in enumerate(proposals):
        if (not isinstance(row, dict) or set(row) != {'operation', 'proposal'}
                or not isinstance(row['operation'], str) or not isinstance(row['proposal'], dict)):
            raise StructureError('CANON_BATCH_SCOPE_INVALID', 'invalid per-record proposal', {'itemIndex': index})
        proposal, operation = row['proposal'], row['operation']
        kind = ({'canon_change_proposal': 'entity', 'canon_relation_proposal': 'relation',
                 'canon_field_definition_proposal': 'field_definition',
                 'canon_field_migration_proposal': 'field_migration',
                 'source_change': 'planning'}.get(operation)
                if operation != 'canon_content_proposal' else proposal.get('kind'))
        if not isinstance(kind, str) or kind not in TARGETS:
            raise StructureError('CANON_BATCH_SCOPE_INVALID', 'unsupported proposal operation or kind', {'itemIndex': index})
        collection, key, expected_operation, parameter = TARGETS[kind]
        rid = proposal.get(key if kind in {'entity', 'relation'} else 'recordId')
        if operation != expected_operation or not isinstance(rid, str):
            raise StructureError('CANON_BATCH_SCOPE_INVALID', 'duplicate or mismatched proposal target', {'itemIndex': index})
        old = next((item for item in current[collection] if item[key] == rid), None)
        new = next((item for item in proposed[collection] if item[key] == rid), None)
        if old is None or new is None or fingerprint(old) == fingerprint(new):
            raise StructureError('CANON_BATCH_SCOPE_INVALID', 'batch proposals update existing records, without creation or deletion',
                                 {'itemIndex': index, 'recordId': rid})
        try:
            targets = _targets(current, kind, rid)
            _reserve_targets(seen, targets, 'CANON_BATCH_SCOPE_INVALID')
            single = deepcopy(current)
            _merge_records(single, proposed, targets)
            if kind == 'planning':
                from .source_changes import apply_changes
                from .source_content_proposals import _scope_valid
                change = proposal.get('change')
                if (not isinstance(change, dict) or change.get('action') != 'patch'
                        or change.get('collection') != collection or change.get('id') != rid
                        or not proposal.get('reason') or not proposal.get('approval', {}).get('actorId')
                        or not proposal.get('approval', {}).get('evidence')
                        or proposal.get('approval', {}).get('verifiedHuman') is not False):
                    raise StructureError('CANON_BATCH_SCOPE_INVALID', 'planning proposal must retain its bound patch and approval')
                replayed, _ = apply_changes(current, [change])
                if replayed != single or not _scope_valid(current, old, proposal.get('sourceChapterId')):
                    raise StructureError('CANON_BATCH_SCOPE_INVALID', 'planning patch differs from proposed record')
            require_canon_proposal(current, single, {'operation': operation, 'parameters': {parameter: proposal}})
            validate_structure(single)
        except StructureError as exc:
            raise StructureError(exc.code, str(exc), {**exc.details, 'itemIndex': index}) from exc
        _merge_records(expected, single, targets)
    ignored = {'revision', 'updatedAt', 'lastOperation'}
    if fingerprint({key: value for key, value in expected.items() if key not in ignored}) != fingerprint(
            {key: value for key, value in proposed.items() if key not in ignored}):
        raise StructureError('CANON_BATCH_SCOPE_INVALID', 'batch changed unrelated source state or record order')
    validate_structure(proposed)
