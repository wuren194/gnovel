"""Reviewed field semantics and their existing values change as one bounded unit."""
from __future__ import annotations

from copy import deepcopy

from .chapter_refs import resolve_chapter_reference
from .project import canon_proposal_batch_max_items, source_model_policy
from .record_patch import apply_record_patch
from .source_content_proposals import _scope_valid, _validate_evidence
from .source_review import fingerprint
from .structure_store import StructureError, validate_structure


def migration_targets(source: dict, record_id: str) -> list[tuple[str, str, str]]:
    """Include unchanged attributes: their review belongs to this logical item too."""
    return [('fieldDefinitions', 'fieldId', record_id)] + [
        ('attributes', 'attributeId', row['attributeId']) for row in source['attributes']
        if row['fieldId'] == record_id]


def _attributes(source: dict, record_id: str) -> dict[str, dict]:
    targets = migration_targets(source, record_id)
    maximum = canon_proposal_batch_max_items()
    if len(targets) > maximum:
        raise StructureError('CANON_BATCH_LIMIT_EXCEEDED', 'field and all its attributes exceed the proposal target budget',
                             {'targetCount': len(targets), 'maxTargets': maximum})
    return {row['attributeId']: row for row in source['attributes'] if row['fieldId'] == record_id}


def _semantic_change(current: dict, updated: dict) -> bool:
    policy = source_model_policy()['mutationPolicy']['referencedFieldDefinitions']
    semantics, metadata = set(policy['semanticFields']), set(policy['metadataFields'])
    changed = {key for key in current.keys() | updated.keys()
               if (key in current) != (key in updated) or fingerprint(current.get(key)) != fingerprint(updated.get(key))}
    if updated.get('fieldId') != current['fieldId'] or changed - (semantics | metadata):
        raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration must preserve field identity and supported fields',
                             {'fieldId': current['fieldId'], 'fields': sorted(changed - (semantics | metadata))})
    if not changed & semantics:
        raise StructureError('FIELD_MIGRATION_SEMANTIC_CHANGE_REQUIRED', 'semantic migration must change field semantics')
    if (not isinstance(updated.get('label'), str) or not updated['label'].strip()
            or 'description' in updated and not isinstance(updated['description'], str)):
        raise StructureError('FIELD_DEFINITION_METADATA_INVALID', 'label must be nonempty text and description must be text')
    return fingerprint(current.get('valueType')) != fingerprint(updated.get('valueType'))


def _complete_entries(entries: object, attributes: dict, *, proposal: bool) -> dict[str, dict]:
    required = ({'attributeId', 'sourceChapterId', 'expectedOldRecordHash', 'newRecordHash', 'evidence'}
                if proposal else {'attributeId', 'values', 'evidence'})
    allowed = required if proposal else required | {'chapterId'}
    rows = {}
    if not isinstance(entries, list):
        raise StructureError('FIELD_MIGRATION_COVERAGE_INVALID', 'all existing attributes must be explicitly reviewed')
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) - allowed or required - set(entry)
                or not isinstance(entry.get('attributeId'), str) or entry['attributeId'] in rows):
            raise StructureError('FIELD_MIGRATION_COVERAGE_INVALID', 'attribute migration entries must have exact, unique identities')
        rows[entry['attributeId']] = entry
    if rows.keys() != attributes.keys():
        raise StructureError('FIELD_MIGRATION_COVERAGE_INVALID', 'migration must cover exactly every attribute using this field',
                             {'missingAttributeIds': sorted(attributes.keys() - rows.keys()),
                              'unexpectedAttributeIds': sorted(rows.keys() - attributes.keys())})
    return rows


def _chapter_scope(source: dict, attribute: dict, chapter: object, *, canonical: bool = False) -> str | None:
    if chapter is not None:
        if not isinstance(chapter, str):
            raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'attribute chapter scope must be a chapter identity')
        resolved = resolve_chapter_reference(source, chapter)
        if canonical and resolved != chapter:
            raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'proposal chapter scope must retain its resolved identity')
        chapter = resolved
    if not _scope_valid(source, attribute, chapter):
        raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'attribute is outside the selected chapter scope',
                             {'attributeId': attribute['attributeId'], 'chapterId': chapter})
    return chapter


def _migrate_values(attribute: dict, values: object, *, type_changed: bool) -> dict:
    replacements = {}
    if not isinstance(values, list):
        raise StructureError('FIELD_MIGRATION_VALUE_COVERAGE_INVALID', 'known values must be explicitly listed')
    for value in values:
        if (not isinstance(value, dict) or set(value) != {'valueId', 'value'}
                or not isinstance(value.get('valueId'), str) or value['valueId'] in replacements):
            raise StructureError('FIELD_MIGRATION_VALUE_COVERAGE_INVALID', 'each known value must retain a unique identity')
        replacements[value['valueId']] = value['value']
    known = {value['valueId'] for value in attribute['values'] if value.get('state', 'known') == 'known'}
    if replacements.keys() != known:
        raise StructureError('FIELD_MIGRATION_VALUE_COVERAGE_INVALID', 'list exactly the known values; preserve other states',
                             {'attributeId': attribute['attributeId'], 'missingValueIds': sorted(known - replacements.keys()),
                              'unexpectedValueIds': sorted(replacements.keys() - known)})
    updated = deepcopy(attribute)
    for value in updated['values']:
        if value['valueId'] not in replacements:
            continue
        replacement = replacements[value['valueId']]
        if not type_changed and fingerprint(value['value']) != fingerprint(replacement):
            raise StructureError('FIELD_MIGRATION_VALUE_CHANGE_FORBIDDEN', 'key or applicability changes must preserve exact values',
                                 {'attributeId': attribute['attributeId'], 'valueId': value['valueId']})
        value['value'] = deepcopy(replacement)
    return updated


def _replace_records(source: dict, field: dict, attributes: dict[str, dict]) -> dict:
    result = deepcopy(source)
    result['fieldDefinitions'] = [deepcopy(field) if row['fieldId'] == field['fieldId'] else row
                                  for row in result['fieldDefinitions']]
    result['attributes'] = [deepcopy(attributes.get(row['attributeId'], row)) for row in result['attributes']]
    return result


def build_field_migration(source: dict, *, recordId: str, patch: list, attributeMigrations: list,
                          reason: str, evidence: list, approvalActor: str, approvalEvidence: str) -> tuple[dict, dict]:
    current = next((row for row in source['fieldDefinitions'] if row['fieldId'] == recordId), None)
    if current is None:
        raise StructureError('SOURCE_RECORD_NOT_FOUND', 'field definition does not exist', {'recordId': recordId})
    if evidence != []:
        raise StructureError('FIELD_MIGRATION_EVIDENCE_INVALID', 'field semantics use the snapshot; put fact evidence on each attribute')
    if any(not isinstance(value, str) or not value.strip() for value in (reason, approvalActor, approvalEvidence)):
        raise StructureError('FIELD_MIGRATION_PROPOSAL_INCOMPLETE', 'reason and declared approval are required')
    _, field = apply_record_patch(current, patch, fingerprint(current))
    type_changed = _semantic_change(current, field)
    attributes = _attributes(source, recordId)
    entries = _complete_entries(attributeMigrations, attributes, proposal=False)
    updated, reviews = {}, []
    for aid, old in attributes.items():
        entry = entries[aid]
        chapter = _chapter_scope(source, old, entry.get('chapterId'))
        _validate_evidence(source, 'attribute', old, chapter, entry['evidence'])
        new = _migrate_values(old, entry['values'], type_changed=type_changed)
        updated[aid] = new
        reviews.append({'attributeId': aid, 'sourceChapterId': chapter, 'expectedOldRecordHash': fingerprint(old),
                        'newRecordHash': fingerprint(new), 'evidence': deepcopy(entry['evidence'])})
    proposed = _replace_records(source, field, updated)
    validate_structure(proposed)
    proposal = {'recordId': recordId, 'expectedOldRecordHash': fingerprint(current), 'newRecordHash': fingerprint(field),
                'attributeMigrations': reviews, 'reason': reason,
                'approval': {'actorId': approvalActor, 'evidence': approvalEvidence, 'verifiedHuman': False}}
    return proposed, proposal


def require_field_migration_proposal(current: dict, proposed: dict, plan: dict) -> None:
    proposal = plan.get('parameters', {}).get('fieldMigrationProposal')
    if (not isinstance(proposal, dict) or set(proposal) != {
            'recordId', 'expectedOldRecordHash', 'newRecordHash', 'attributeMigrations', 'reason', 'approval'}):
        raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration proposal must retain its exact reviewed scope')
    validate_structure(proposed)
    rid = proposal.get('recordId')
    old = next((row for row in current['fieldDefinitions'] if row['fieldId'] == rid), None)
    field = next((row for row in proposed['fieldDefinitions'] if row['fieldId'] == rid), None)
    if (old is None or field is None or proposal['expectedOldRecordHash'] != fingerprint(old)
            or proposal['newRecordHash'] != fingerprint(field)):
        raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration must retain the exact old and new field records')
    type_changed = _semantic_change(old, field)
    approval = proposal['approval']
    if (not isinstance(approval, dict) or set(approval) != {'actorId', 'evidence', 'verifiedHuman'}
            or approval['verifiedHuman'] is not False
            or any(not isinstance(value, str) or not value.strip() for value in
                   (proposal['reason'], approval['actorId'], approval['evidence']))):
        raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration requires reason and declared approval')
    attributes = _attributes(current, rid)
    entries = _complete_entries(proposal['attributeMigrations'], attributes, proposal=True)
    after = {row['attributeId']: row for row in proposed['attributes']}
    updated = {}
    for aid, attribute in attributes.items():
        entry, new = entries[aid], after.get(aid)
        if (new is None or entry['expectedOldRecordHash'] != fingerprint(attribute)
                or entry['newRecordHash'] != fingerprint(new)):
            raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'attribute review must retain its exact old and new records',
                                 {'attributeId': aid})
        chapter = _chapter_scope(current, attribute, entry['sourceChapterId'], canonical=True)
        _validate_evidence(current, 'attribute', attribute, chapter, entry['evidence'])
        new_values = new.get('values')
        if not isinstance(new_values, list) or any(not isinstance(value, dict) for value in new_values):
            raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration must retain existing value records')
        # Rebuild only the permitted values on the old records. This catches
        # changed identities, state, time, order, ownership and unknown values.
        values = [{'valueId': value.get('valueId'), 'value': value.get('value')} for value in new_values
                  if value.get('state', 'known') == 'known']
        rebuilt = _migrate_values(attribute, values, type_changed=type_changed)
        if fingerprint(rebuilt) != fingerprint(new):
            raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration changed attribute identity, metadata or value structure',
                                 {'attributeId': aid})
        updated[aid] = new
    expected = _replace_records(current, field, updated)
    ignored = {'revision', 'updatedAt', 'lastOperation'}
    if fingerprint({key: value for key, value in expected.items() if key not in ignored}) != fingerprint(
            {key: value for key, value in proposed.items() if key not in ignored}):
        raise StructureError('FIELD_MIGRATION_SCOPE_INVALID', 'migration changed another record, collection or row order')
