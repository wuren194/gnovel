"""Constrained semantic edits over a frozen review form, never review credit."""
from __future__ import annotations

from copy import deepcopy

from .argument_validation import validate_arguments
from .artifact_templates import SCHEMA_PATH
from .static_cache import cached_json
from .structure_store import StructureError, load_structure

# These are write permissions, not a second artifact schema. Values and enum
# constraints come from the artifact schema and the existing review validators.
SCALARS = {
    'stage_a_review': ('decision', 'modeLevelReason', 'consensus', 'disagreements', 'openItems'),
    'stage_b_review': ('verdict', 'repairScope'),
}
GROUPS = ('behaviorCoverage', 'factAssessment')
ROWS = {
    'relationshipCausalityReviews': ('id', ('conclusion', 'evidence')),
    'behaviorReviews': ('id', ('disposition', 'conclusion', 'evidence')),
    'writingDecisions': ('id', ('decision', 'reason')),
    'cognitionReviews': ('id', ('characterId', 'information', 'disposition', 'reason', 'sources', 'findingId')),
    'findings': ('findingId', ('severity', 'summary', 'location', 'repairSuggestion', 'line',
                             'start', 'end', 'factChange')),
}
FINDING_FIELDS = {
    'findingId': {'type': 'string', 'minLength': 1},
    'severity': {'type': 'string', 'minLength': 1},
    'ruleId': {'type': 'string', 'minLength': 1},
    'summary': {'type': 'string', 'minLength': 1},
    'location': {'type': 'string'}, 'repairSuggestion': {'type': 'string'},
    'line': {'type': 'integer', 'minimum': 1},
    'start': {'type': 'integer', 'minimum': 0}, 'end': {'type': 'integer', 'minimum': 1},
}


def _forbidden(path, message='only the offered semantic fields may be edited'):
    raise StructureError('REVIEW_FORM_EDIT_FORBIDDEN', message, {'field': path})


def _object(value, path):
    if not isinstance(value, dict):
        raise StructureError('REVIEW_FORM_EDITS_INVALID', 'semantic edits must be objects', {'field': path})


def _checked_values(value, properties, path):
    _object(value, path)
    for key, item in value.items():
        if key not in properties:
            _forbidden(path + '.' + key)
        validate_arguments(properties[key], item, path + '.' + key)


def semantic_fields(payload, artifact_type):
    """One fillable tree; no hashes or separate per-leaf path/schema inventory."""
    allowed = {*SCALARS[artifact_type], *GROUPS, *ROWS, 'repairDecisions',
               'seatReviews' if artifact_type == 'stage_a_review' else 'slots'}
    return {key: deepcopy(value) for key, value in payload.items() if key in allowed}


def model_guide(guide, payload, artifact_type):
    # The full guide stays in the original snapshot. Keep semantic instructions,
    # but express each nested enum once instead of returning its entire schema.
    omit = {'fieldSchemas', 'toolOwnedFields', 'formAction', 'seatReviews',
            'seatReviewSource', 'seatStructure', 'authorship'}
    result = {key: deepcopy(value) for key, value in guide.items() if key not in omit}
    result['enums'] = deepcopy(result.get('enums', {}))
    schemas = cached_json(SCHEMA_PATH)['$defs'][artifact_type]['properties']
    result['enums']['behaviorCoverage'] = schemas['behaviorCoverage']['properties']['relationship']['enum']
    if payload.get('behaviorReviews') or artifact_type == 'stage_b_review':
        result['enums']['behaviorReviews.disposition'] = schemas['behaviorReviews']['items']['properties']['disposition']['enum']
    if artifact_type == 'stage_b_review':
        result['enums']['factAssessment.status'] = schemas['factAssessment']['properties']['status']['enum']
        if payload.get('cognitionReviews'):
            result['enums']['cognitionReviews.disposition'] = schemas['cognitionReviews']['items']['properties']['disposition']['enum']
        if payload.get('writingDecisions'):
            result['enums']['writingDecisions.decision'] = schemas['writingDecisions']['items']['properties']['decision']['enum']
    result['edits'] = ('edits 只提交实际填写的字段，形状同 fields；对象按键合并，已有条目按 id/findingId 合并，'
                       '漏项和空数组不会删除底表。席位、条目身份及来源保持冻结，未填仍未填。')
    if artifact_type == 'stage_b_review':
        result['append'] = ('新发现显式放 edits.append.findings、behaviorReviews或cognitionReviews；'
                            '新增行为须 origin=prose，新增认知使用须origin=semantic。已有事项使用原 ID 更新，不能重建或删除。')
    return result


def _row_properties(schemas, field):
    properties = deepcopy(schemas[field]['items']['properties'])
    if field == 'findings':
        properties.update(FINDING_FIELDS)
    return properties


def _update_rows(payload, field, updates, schemas):
    identity, writable = ROWS[field]
    if not isinstance(updates, list):
        raise StructureError('REVIEW_FORM_EDITS_INVALID', 'row edits must be arrays', {'field': field})
    existing = {row[identity]: row for row in payload.get(field, [])}
    properties = _row_properties(schemas, field)
    seen = set()
    for update in updates:
        _object(update, field)
        selected = update.get(identity)
        if not isinstance(selected, str) or selected not in existing or selected in seen:
            _forbidden(field, 'select each existing item once using its original identity')
        seen.add(selected)
        row = existing[selected]
        for key, value in update.items():
            path = field + '.' + selected + '.' + key
            if key not in writable:
                # The offered template can be submitted directly, but its
                # identity/source annotations must remain byte-for-byte equal.
                if key not in row or value != row[key] or type(value) is not type(row[key]):
                    _forbidden(path, 'item identity and source annotations are frozen')
            else:
                validate_arguments(properties[key], value, path)
                row[key] = deepcopy(value)


def _append_rows(payload, additions, schemas):
    _object(additions, 'append')
    if set(additions) - {'findings', 'behaviorReviews', 'cognitionReviews'}:
        _forbidden('append')
    for field, rows in additions.items():
        if not isinstance(rows, list):
            raise StructureError('REVIEW_FORM_EDITS_INVALID', 'append fields must be arrays', {'field': field})
        identity = ROWS[field][0]
        existing = {row.get(identity) for row in payload.get(field, [])}
        properties = _row_properties(schemas, field)
        required = (['severity', 'ruleId', 'summary'] if field == 'findings'
                    else schemas[field]['items']['required'])
        for row in rows:
            _checked_values(row, properties, 'append.' + field)
            validate_arguments({'type': 'object', 'required': required}, row, 'append.' + field)
            selected = row.get(identity)
            if selected is not None and selected in existing:
                _forbidden('append.' + field, 'appended identities must be new; use row edits for existing items')
            if field == 'behaviorReviews' and row.get('origin') != 'prose':
                _forbidden('append.behaviorReviews.origin', 'only explicit prose discoveries may be appended')
            if field == 'cognitionReviews' and row.get('origin') != 'semantic':
                _forbidden('append.cognitionReviews.origin', 'only explicit semantic discoveries may be appended')
            if selected is not None:
                existing.add(selected)
            payload.setdefault(field, []).append(deepcopy(row))


def apply_semantic_edits(payload, artifact_type, edits):
    """Patch selected semantic nodes; absent nodes never mean deletion or pass."""
    _object(edits, 'edits')
    result = deepcopy(payload)
    definitions = cached_json(SCHEMA_PATH)['$defs']
    schemas = definitions[artifact_type]['properties']
    seats = 'seatReviews' if artifact_type == 'stage_a_review' else 'slots'
    allowed = {*SCALARS[artifact_type], 'behaviorCoverage', seats,
               'relationshipCausalityReviews', 'behaviorReviews'}
    if artifact_type == 'stage_b_review':
        allowed.update({'findings', 'writingDecisions', 'factAssessment', 'cognitionReviews', 'append'})
        if 'repairRef' in payload:
            allowed.add('repairDecisions')
    # JSON object order never changes the meaning of a patch. New items cannot
    # become targets of the existing-item updates in the same submission.
    for field in sorted(edits, key=lambda key: (key == 'append', key)):
        value = edits[field]
        if field not in allowed:
            _forbidden(field)
        if field in SCALARS[artifact_type]:
            validate_arguments(schemas[field], value, field)
            result[field] = deepcopy(value)
        elif field in GROUPS:
            _checked_values(value, schemas[field]['properties'], field)
            result.setdefault(field, {}).update(deepcopy(value))
        elif field == seats:
            _object(value, field)
            slot = definitions['stage_b_review']['properties']['slots']['additionalProperties']['properties']
            writable = {key: slot[key] for key in ('conclusion', 'evidence')}
            # Empty conclusions remain legal input to the form. The existing
            # pack/completion gates reject them instead of manufacturing text.
            writable['conclusion'] = {'type': 'string'}
            if artifact_type == 'stage_b_review':
                writable.update(notApplicableReason=slot['notApplicableReason'],
                                blocking={'type': 'boolean'}, location={'type': 'string'})
            for identity, update in value.items():
                if identity not in result[field]:
                    _forbidden(field + '.' + str(identity), 'only the actual frozen seats may be filled')
                _checked_values(update, writable, field + '.' + identity)
                result[field][identity].update(deepcopy(update))
        elif field in ROWS:
            _update_rows(result, field, value, schemas)
        elif field == 'append':
            _append_rows(result, value, schemas)
        elif field == 'repairDecisions':
            from .operation_schemas import QUOTE
            from .repair_review import DECISIONS
            properties = {'issueId': {'type': 'string', 'minLength': 1},
                          'decision': {'type': 'string', 'enum': sorted(DECISIONS)},
                          'reason': {'type': 'string', 'minLength': 1},
                          'quotes': {'type': 'array', 'items': QUOTE}}
            validate_arguments({'type': 'array', 'items': {'type': 'object', 'properties': properties,
                                'required': list(properties), 'additionalProperties': False}}, value, field)
            result[field] = deepcopy(value)
    return result


def resolve_form(root, *, chapter_id, artifact_type, reference, edits):
    from .prepare_dependencies import validate_dependencies
    from .transport_cache import get
    saved = get(root, reference, kind='review-form')
    report = saved['report']
    if (saved['projectId'] != load_structure(root)['projectId']
            or report['chapterId'] != chapter_id or report['artifactType'] != artifact_type):
        raise StructureError('REVIEW_FORM_SCOPE_INVALID', 'use the form prepared for this project, chapter and review stage')
    validate_dependencies(root, saved['dependencies'], code='REVIEW_FORM_STALE')
    payload = apply_semantic_edits(report['payload'], artifact_type, edits)
    validate_dependencies(root, saved['dependencies'], code='REVIEW_FORM_STALE')
    return payload, deepcopy(saved['dependencies'])
