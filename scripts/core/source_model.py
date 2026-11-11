"""Identity and indexes for the single-file setting/outline source.

Indexes are projections, never persisted authored facts. Importing this module
does not initialize or upgrade a project.
"""
from __future__ import annotations

import math
import re
import hashlib
from dataclasses import dataclass, field
from typing import Any

from .project import current_schema_version, source_model_policy, unsupported_error_code

BASE_COLLECTIONS = {
    'volumes': ('volumeId', 'volume'), 'arcs': ('arcId', 'arc'),
    'chapters': ('chapterId', 'chapter'), 'entities': ('entityId', 'entity'),
}


def owner_types() -> set[str]:
    """Object kinds accepted by source fields, selections and query filters."""
    from .structure_store import ENTITY_TYPES
    return ENTITY_TYPES | {kind for _, kind in BASE_COLLECTIONS.values() if kind != 'entity'} | {'project', 'unit', 'pair'}


def snapshot_hash(source: dict) -> str:
    """Read/evidence identity includes revision, unlike undo's content hash."""
    from .structure_store import canonical_json
    return hashlib.sha256(canonical_json(source).encode()).hexdigest()


def author_content(record: dict | None, owner_type: str | None) -> dict | None:
    """Project authored content without chapter publication/lifecycle metadata.

    This projection is shared by decision scope proofs, plans and impact. It is
    never a replacement for the complete source snapshot or a write CAS.
    Placement, order, arc membership and all actual authored fields stay inputs.
    """
    if owner_type == 'chapter' and isinstance(record, dict):
        return {key: value for key, value in record.items()
                if key not in {'status', 'filePath', 'proseBinding'}}
    return record


def is_current_binding(structure: dict) -> bool:
    """Only the current project format carries authoritative prose bindings."""
    return (isinstance(structure, dict) and type(structure.get('schemaVersion')) is int
            and structure['schemaVersion'] == current_schema_version())


def require_current_source(structure: dict) -> None:
    """Other formats are external manuscripts, never a runtime compatibility mode."""
    if not is_current_binding(structure):
        from .structure_store import StructureError
        raise StructureError(unsupported_error_code(),
            '仅支持当前项目格式；请将旧项目当作外部小说，人工整理后迁入新项目。',
            {'supportedSchemaVersion': current_schema_version(),
             'receivedSchemaVersion': structure.get('schemaVersion') if isinstance(structure, dict) else None})


def content_defaults() -> dict:
    return {name: [] for name in source_model_policy()['collections']}


def content_id_patterns() -> dict[str, re.Pattern]:
    from .source_ids import identity_pattern
    policy = source_model_policy()
    kinds = {row['idField']: row['idField'].removesuffix('Id') for row in policy['collections'].values()}
    return {key: identity_pattern(kind) for key, kind in {**kinds, 'valueId': 'value'}.items()}


def source_schema() -> dict:
    from .author_decisions import SCOPE_SNAPSHOT_BASIS
    from .source_validation import RECORD_FIELDS, REQUIRED_FIELDS
    from .source_cognition import COGNITION_FIELDS
    from .source_relation_proposals import MUTABLE_FIELDS as RELATION_MUTABLE_FIELDS
    from .chapter_contract import (FIELDS as CONTRACT_FIELDS, REQUIRED_FIELDS as CONTRACT_REQUIRED_FIELDS,
                                   contract_schema)
    from .relationship_causality import declaration_schema
    from .pair_classification import pair_schema
    from .behavior_review import schema as behavior_schema
    from .source_rules import RULE_REQUIRED
    policy = source_model_policy()
    patterns = content_id_patterns()
    return {'ok': True, 'code': 'SOURCE_SCHEMA', 'schemaVersion': current_schema_version(),
            'proseBinding': policy['proseBinding'],
            'placementScopes': list(policy['placementScopes']),
            'unitMemberTypes': list(policy['unitMemberTypes']),
            'collections': {name: {**spec, 'idPattern': patterns[spec['idField']].pattern,
                                    'requiredFields': sorted(REQUIRED_FIELDS[name]),
                                    'optionalFields': sorted(RECORD_FIELDS[name] - REQUIRED_FIELDS[name])}
                            for name, spec in policy['collections'].items()},
            'mutableCollections': {**{name: {'idField': key} for name, (key, _) in BASE_COLLECTIONS.items()},
                                   'relations': {'idField': 'relationId'},
                                   'pairs': {'idField': 'pairId'},
                                   **{name: {'idField': spec['idField']} for name, spec in policy['collections'].items()}},
            'pairClassification': pair_schema(),
             'mutationPolicy': policy['mutationPolicy'],
            'chapterContract': {'owner': 'active chapter plan fragment',
                'inputSchema': contract_schema(),
                'required': sorted(CONTRACT_REQUIRED_FIELDS),
                'optional': sorted(CONTRACT_FIELDS - CONTRACT_REQUIRED_FIELDS),
                'targetRange': '[positive minimum Han characters, maximum >= minimum]',
                'beats': [{'id': 'B01', 'budget': 'positive integer'}],
                'requirements': 'array of semantic requirements, not automatic pass claims',
                'quotedTargets': 'only explicitly required literal quotations',
                'relationshipCausality': declaration_schema(),
                **behavior_schema(),
                'authority': 'fragment.chapterContract; included in sourcePlanHash; source preview/commit required'},
            'valueIdPattern': patterns['valueId'].pattern,
            'valueTypes': policy['valueTypes'], 'valueStates': policy['valueStates'],
            'contentRoles': policy['contentRoles'], 'recordStatuses': policy['recordStatuses'],
            'fragmentPrefixes': policy['fragmentPrefixes'],
            'entityPrefixes': policy['entityPrefixes'],
            'receiptPrefixes': policy['receiptPrefixes'],
            'generatedSuffixLength': policy['generatedSuffixLength'],
            'profileTopics': policy['profileTopics'],
            'recallTopics': policy['recallTopics'],
            'cognition': {'fields': sorted(COGNITION_FIELDS),
                          'attitudes': policy['cognitionAttitudes'],
                          'target': 'concrete valueId or proposition fragmentId; never fieldId or attributeId',
                          'time': 'acquiredAtId and fragment.validDuring are story time; null is undetermined'},
            'checks': {'statuses': policy['checkStatuses'], 'appendOnly': True,
                       'bindings': 'tool-owned targetHash and inputHashes; changed evidence remains historical'},
            'designations': {'owner': 'entity', 'fields': ['label', 'perspectiveIds', 'validDuring'],
                             'existing': 'canon_proposal', 'visibility': 'explicit audience and determined story time'},
            'relations': {'fields': ['relationId', 'fromEntity', 'toEntity', 'kind', 'origin',
                                     'perspectiveId', 'validDuring', 'basisIds'],
                          'mutableFields': sorted(RELATION_MUTABLE_FIELDS),
                          'perspectiveId': 'character or faction; omitted means author relation'},
            'recall': {'topics': policy['recallTopics'], 'weightMeaning': 'reminder frequency',
                       'withoutReplacement': True, 'sceneConditioned': False,
                       'taskRetries': 'reuse sampleId; refuse changed inputs'},
            'presentationKinds': policy['presentationKinds'],
            'ruleSpec': {'ownerType': 'rule', 'requiredFields': sorted(RULE_REQUIRED),
                         'optionalFields': ['evaluation', 'exceptionsAllowed'], 'actions': policy['ruleActions'],
                         'methods': policy['ruleMethods'], 'enforcement': policy['ruleEnforcements'],
                         'evaluation': {'kind': 'count_at_most',
                                        'requiredFields': ['kind', 'eventIds', 'limitValueId', 'complete']}},
            'timeSpec': {'kinds': policy['timeSpecKinds'],
                         'relative': ['kind', 'anchorId', 'offset'],
                         'range': ['kind', 'afterId?', 'beforeId?'],
                         'coarse': ['kind', 'label'], 'unknown': ['kind']},
            'knowledgeModes': policy['knowledgeModes'],
            'validDuring': {'fromId': 'event or time_anchor; inclusive, nullable',
                            'toId': 'event or time_anchor; exclusive, nullable'},
            'filterRule': 'keep fragment if any selected content binding survives exclusions; metadata is not a binding',
            'planningCandidates': policy['planningCandidates'],
            'authorDecisions': {**policy['authorDecisions'],
                'scopeProof': {'scopeSnapshotBasis': SCOPE_SNAPSHOT_BASIS, 'generatedBy': 'approve',
                               'missingOrUnknownBasis': 'historical_unproven',
                               'scopeInvalidatedBy': 'tool-derived committed source operation; monotonic, never client-editable',
                               'scopeDependencies': 'registered input closure; chapter uses base plan without decisions',
                               'refresh': 'create with supersedes, then explicitly approve'}},
            'workVoiceProfile': {'owner': 'project', 'role': 'approved_plan',
                                 'axes': ['narrativeDistance', 'sentenceVariation', 'dialogueExposition',
                                          'emotionLabeling', 'somaticExpression', 'sceneTransition']}}


def finite_number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


@dataclass
class SourceIndex:
    objects: dict[str, dict]
    owner_types: dict[str, str]
    fields: dict[str, dict]
    attributes: dict[str, dict]
    fragments: dict[str, dict]
    values: dict[str, dict]
    value_attributes: dict[str, str]
    records: dict[str, dict]
    time_states: dict[str, dict] | None = None
    schema_version: int | None = None
    planning_candidates: dict[str, dict] | None = None
    unit_memberships: dict[str, list[dict]] | None = None
    member_units: dict[str, list[str]] | None = None
    time_comparisons: dict[tuple[str, str], dict] = field(default_factory=dict)

    @classmethod
    def build(cls, structure: dict) -> 'SourceIndex':
        """Build only from an already validated source snapshot."""
        objects, owner_types = {}, {}
        for collection, (key, kind) in BASE_COLLECTIONS.items():
            for row in structure.get(collection, []):
                objects[row[key]] = row
                owner_types[row[key]] = row.get('entityType', kind)
        for row in structure.get('units', []):
            objects[row['unitId']] = row
            owner_types[row['unitId']] = 'unit'
        for row in structure.get('pairs', []):
            objects[row['pairId']] = row
            owner_types[row['pairId']] = 'pair'
        if structure.get('projectId'):
            from .pair_classification import assessment
            pid = structure['projectId']
            objects[pid] = {'projectId': pid, 'title': structure.get('bookName', ''),
                            'pairAssessment': assessment(structure)}
            owner_types[pid] = 'project'
        fields = {r['fieldId']: r for r in structure.get('fieldDefinitions', [])}
        attributes = {r['attributeId']: r for r in structure.get('attributes', [])}
        fragments = {r['fragmentId']: r for r in structure.get('fragments', [])}
        values, value_attributes = {}, {}
        for aid, attribute in attributes.items():
            for value in attribute['values']:
                values[value['valueId']] = value
                value_attributes[value['valueId']] = aid
        records = {**objects, **fields, **attributes, **fragments, **values}
        records.update({r['presentationId']: r for r in structure.get('presentations', [])})
        records.update({r['relationId']: r for r in structure.get('relations', [])})
        for name in ('checks', 'recallPools'):
            key = source_model_policy()['collections'][name]['idField']
            records.update({r[key]: r for r in structure.get(name, [])})
        schema_version = structure.get('schemaVersion') if type(structure.get('schemaVersion')) is int else None
        candidates = {r['candidateId']: r for r in structure.get('planningCandidates', [])
                      if isinstance(r, dict) and isinstance(r.get('candidateId'), str)}
        memberships, member_units = cls._unit_memberships(structure, schema_version)
        return cls(objects, owner_types, fields, attributes, fragments, values, value_attributes, records,
                   None, schema_version, candidates, memberships, member_units)

    @staticmethod
    def _unit_memberships(structure: dict, schema_version: int | None) -> tuple[dict[str, list[dict]], dict[str, list[str]]]:
        memberships: dict[str, list[dict]] = {}
        member_units: dict[str, list[str]] = {}
        for unit in structure.get('units', []):
            members = unit.get('members', []) or []
            memberships[unit['unitId']] = members
            for member in members:
                member_units.setdefault(member.get('memberId'), []).append(unit['unitId'])
        return memberships, member_units

    def units_for(self, member_id: str) -> list[str]:
        """Unit IDs whose member list contains ``member_id`` (any status)."""
        return list((self.member_units or {}).get(member_id, []))

    def owner(self, record_id: str) -> str | None:
        if record_id in self.value_attributes:
            return self.attributes[self.value_attributes[record_id]]['ownerId']
        if record_id in self.objects:
            return record_id
        return self.records.get(record_id, {}).get('ownerId')

    def time_bound(self, anchor_id: str | None, *, lower: bool) -> float | int | None:
        if anchor_id is None:
            return -math.inf if lower else math.inf
        if self.time_states is None:
            from .source_time import story_time_states
            self.time_states = story_time_states({}, index=self)
        state = self.time_states.get(anchor_id, {})
        return state.get('lower') if state.get('status') == 'exact' else None

    def interval(self, record: dict) -> tuple[float | int | None, float | int | None]:
        period = record.get('validDuring') or {}
        return (self.time_bound(period.get('fromId'), lower=True),
                self.time_bound(period.get('toId'), lower=False))
