"""Exact, reviewed metadata corrections for existing field definitions."""
from __future__ import annotations

from copy import deepcopy

from .project import source_model_policy
from .record_patch import apply_record_patch
from .source_review import fingerprint
from .structure_store import StructureError, validate_structure


def _metadata_change(current: dict, updated: dict) -> None:
    allowed = set(source_model_policy()['mutationPolicy']['referencedFieldDefinitions']['metadataFields'])
    changed = {key for key in current.keys() | updated.keys() if current.get(key) != updated.get(key)
               or (key in current) != (key in updated)}
    if not changed:
        raise StructureError('FIELD_DEFINITION_CHANGE_EMPTY', 'field definition metadata is unchanged')
    if updated.get('fieldId') != current['fieldId'] or changed - allowed:
        raise StructureError('FIELD_DEFINITION_MIGRATION_REQUIRED',
                             'only label and description may use the reviewed metadata correction route',
                             {'fieldId': current['fieldId'], 'fields': sorted(changed - allowed)})
    if (not isinstance(updated.get('label'), str) or not updated['label'].strip()
            or 'description' in updated and not isinstance(updated['description'], str)):
        raise StructureError('FIELD_DEFINITION_METADATA_INVALID', 'label must be nonempty text and description must be text')


def build_field_definition_change(source: dict, *, recordId: str, patch: list, reason: str,
                                  evidence: list, approvalActor: str, approvalEvidence: str) -> tuple[dict, dict]:
    current = next((row for row in source['fieldDefinitions'] if row['fieldId'] == recordId), None)
    if current is None:
        raise StructureError('SOURCE_RECORD_NOT_FOUND', 'field definition does not exist', {'recordId': recordId})
    if evidence != []:
        raise StructureError('FIELD_DEFINITION_EVIDENCE_INVALID',
                             'metadata corrections use the bound field snapshot; evidence must be empty')
    if any(not isinstance(value, str) or not value.strip() for value in (reason, approvalActor, approvalEvidence)):
        raise StructureError('FIELD_DEFINITION_PROPOSAL_INCOMPLETE', 'reason and declared approval are required')
    _, updated = apply_record_patch(current, patch, fingerprint(current))
    _metadata_change(current, updated)
    proposed = deepcopy(source)
    proposed['fieldDefinitions'] = [deepcopy(updated) if row['fieldId'] == recordId else row
                                    for row in proposed['fieldDefinitions']]
    validate_structure(proposed)
    proposal = {'recordId': recordId, 'expectedOldRecordHash': fingerprint(current),
                'newRecordHash': fingerprint(updated), 'reason': reason,
                'approval': {'actorId': approvalActor, 'evidence': approvalEvidence, 'verifiedHuman': False}}
    return proposed, proposal


def require_field_definition_proposal(current: dict, proposed: dict, plan: dict) -> None:
    proposal = plan.get('parameters', {}).get('fieldDefinitionProposal')
    if (not isinstance(proposal, dict)
            or set(proposal) != {'recordId', 'expectedOldRecordHash', 'newRecordHash', 'reason', 'approval'}):
        raise StructureError('FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID', 'field definition proposal must contain exactly its bound metadata review')
    rid = proposal.get('recordId')
    old = next((row for row in current['fieldDefinitions'] if row['fieldId'] == rid), None)
    updated = next((row for row in proposed['fieldDefinitions'] if row['fieldId'] == rid), None)
    if (old is None or updated is None or proposal.get('expectedOldRecordHash') != fingerprint(old)
            or proposal.get('newRecordHash') != fingerprint(updated)):
        raise StructureError('FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID', 'proposal must retain its exact old and new field definition')
    _metadata_change(old, updated)
    approval = proposal.get('approval')
    if (not isinstance(approval, dict) or set(approval) != {'actorId', 'evidence', 'verifiedHuman'}
            or approval.get('verifiedHuman') is not False
            or any(not isinstance(value, str) or not value.strip() for value in
                   (proposal.get('reason'), approval.get('actorId'), approval.get('evidence')))):
        raise StructureError('FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID', 'proposal lacks reason and declared approval')
    expected = deepcopy(current)
    expected['fieldDefinitions'] = [deepcopy(updated) if row['fieldId'] == rid else row
                                   for row in expected['fieldDefinitions']]
    ignored = {'revision', 'updatedAt', 'lastOperation'}
    if any(expected.get(key) != proposed.get(key) for key in expected.keys() | proposed.keys() if key not in ignored):
        raise StructureError('FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID', 'proposal changed another record, collection or row order')
