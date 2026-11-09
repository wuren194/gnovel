"""Declared reasons for character choices, with registered source dependencies.

This module checks shape and references only. Described appearance never implies
actual ability, and a complete declaration is not a judgment that a choice is
credible. Review conclusions belong to the existing review artifacts.
"""
from __future__ import annotations

from copy import deepcopy


TEXT_FIELDS = ('id', 'relationId', 'capabilityAssessment', 'resistanceCost',
               'complianceCost', 'choiceReason', 'scope')
BASIS_FIELDS = ('capabilityBasisIds', 'leverageBasisIds')
DECLARATION_FIELDS = frozenset((*TEXT_FIELDS, *BASIS_FIELDS, 'continuation'))
CONTINUATION_FIELDS = frozenset(('status', 'successorBasisIds', 'reason'))
CONTINUATION_STATUSES = ('retained', 'replaced', 'released')


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _references(value, *, nonempty: bool) -> bool:
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(_text(item) for item in value)
            and len(value) == len(set(value)))


def validate_declarations(value) -> list[str]:
    """Validate explicit declaration shape without evaluating its reasoning."""
    if not isinstance(value, list):
        return ['RELATIONSHIP_CAUSALITY_INVALID']
    errors, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) != DECLARATION_FIELDS:
            errors.append('RELATIONSHIP_CAUSALITY_FIELDS_INVALID')
            continue
        for field in TEXT_FIELDS:
            if not _text(row[field]):
                errors.append('RELATIONSHIP_CAUSALITY_TEXT_INVALID:' + field)
        if _text(row['id']):
            if row['id'] in seen:
                errors.append('RELATIONSHIP_CAUSALITY_ID_DUPLICATE')
            seen.add(row['id'])
        for field in BASIS_FIELDS:
            if not _references(row[field], nonempty=True):
                errors.append('RELATIONSHIP_CAUSALITY_REFERENCES_INVALID:' + field)
        continuation = row['continuation']
        if not isinstance(continuation, dict) or set(continuation) != CONTINUATION_FIELDS:
            errors.append('RELATIONSHIP_CAUSALITY_CONTINUATION_INVALID')
            continue
        status = continuation['status']
        if status not in CONTINUATION_STATUSES:
            errors.append('RELATIONSHIP_CAUSALITY_STATUS_INVALID')
        if not _text(continuation['reason']):
            errors.append('RELATIONSHIP_CAUSALITY_TEXT_INVALID:continuation.reason')
        successors = continuation['successorBasisIds']
        if (not _references(successors, nonempty=status == 'replaced')
                or status in ('retained', 'released') and successors):
            errors.append('RELATIONSHIP_CAUSALITY_SUCCESSORS_INVALID')
    return list(dict.fromkeys(errors))


def required_dependency_ids(row: dict) -> set[str]:
    """Return the dependencies of one shape-validated declaration."""
    return {row['relationId'], *row['capabilityBasisIds'], *row['leverageBasisIds'],
            *row['continuation']['successorBasisIds']}


def validate_source_declarations(fragment: dict, index) -> list[dict]:
    """Require existing character relations, evidence records and explicit edges."""
    contract = fragment.get('chapterContract')
    if not isinstance(contract, dict):
        return []
    from .behavior_review import validate_source_arrangements
    living_errors = validate_source_arrangements(fragment, index)
    if 'relationshipCausality' not in contract:
        return living_errors
    path = 'fragments.' + str(fragment.get('fragmentId')) + '.chapterContract.relationshipCausality'
    declarations = contract['relationshipCausality']
    errors = [{'code': code, 'path': path} for code in validate_declarations(declarations)]
    if errors:
        return errors
    dependencies = fragment.get('dependsOn', [])
    registered = set(dependencies) if _references(dependencies, nonempty=False) else set()
    for position, row in enumerate(declarations):
        row_path = path + '.' + str(position)
        relation = index.records.get(row['relationId'])
        if (not isinstance(relation, dict) or relation.get('relationId') != row['relationId']
                or any(not _text(relation.get(endpoint))
                       or index.owner_types.get(relation[endpoint]) != 'character'
                       for endpoint in ('fromEntity', 'toEntity'))):
            errors.append({'code': 'RELATIONSHIP_CAUSALITY_RELATION_INVALID',
                           'path': row_path + '.relationId'})
        for field in BASIS_FIELDS:
            for basis_id in row[field]:
                if basis_id not in index.records:
                    errors.append({'code': 'RELATIONSHIP_CAUSALITY_BASIS_MISSING',
                                   'path': row_path + '.' + field, 'recordId': basis_id})
        for basis_id in row['continuation']['successorBasisIds']:
            if basis_id not in index.records:
                errors.append({'code': 'RELATIONSHIP_CAUSALITY_BASIS_MISSING',
                               'path': row_path + '.continuation.successorBasisIds',
                               'recordId': basis_id})
        for dependency in sorted(required_dependency_ids(row) - registered):
            errors.append({'code': 'RELATIONSHIP_CAUSALITY_DEPENDENCY_MISSING',
                           'path': row_path, 'recordId': dependency})
    return [*errors, *living_errors]


def chapter_causality(source: dict, chapter_id: str) -> list[dict]:
    """Read the current chapter's unique active contract, without inheritance."""
    from .structure_store import StructureError
    contracts = [fragment['chapterContract'] for fragment in source.get('fragments', [])
                 if fragment.get('ownerId') == chapter_id and fragment.get('contentRole') == 'plan'
                 and fragment.get('status') != 'archived' and 'chapterContract' in fragment]
    if len(contracts) > 1:
        raise StructureError('CHAPTER_CONTRACT_AMBIGUOUS',
                             'relationship review requires one active chapter contract')
    if not contracts:
        return []
    contract = contracts[0]
    if not isinstance(contract, dict):
        raise StructureError('CHAPTER_CONTRACT_FIELDS_INVALID', 'chapter contract must be an object')
    declarations = contract.get('relationshipCausality', [])
    errors = validate_declarations(declarations)
    if errors:
        raise StructureError('RELATIONSHIP_CAUSALITY_INVALID',
                             'relationship causality declarations have invalid fields', {'issues': errors})
    return deepcopy(declarations)


def blank_reviews(source: dict, chapter_id: str) -> list[dict]:
    """Create empty review rows; no declaration earns a conclusion by itself."""
    return [{'id': row['id'], 'conclusion': '', 'evidence': []}
            for row in chapter_causality(source, chapter_id)]


def declaration_schema() -> dict:
    """Describe the same fields and statuses consumed by the runtime validators."""
    return {'type': 'array', 'requiredFields': sorted(DECLARATION_FIELDS),
            'textFields': list(TEXT_FIELDS), 'basisFields': list(BASIS_FIELDS),
            'id': 'nonempty and unique within the chapter contract',
            'relationId': 'registered relation between character entities',
            'basisReferences': 'nonempty unique existing source record IDs',
            'continuation': {'requiredFields': sorted(CONTINUATION_FIELDS),
                             'statuses': list(CONTINUATION_STATUSES),
                             'successorBasisIds': 'nonempty for replaced; empty for retained or released',
                             'reason': 'nonempty explanation'},
            'dependencies': 'relationId and every basis ID must occur in fragment.dependsOn',
            'coverage': 'shape and references only; character-choice reasoning requires semantic review'}
