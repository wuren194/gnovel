"""Typed content bindings and temporal value invariants at the write boundary."""
from __future__ import annotations

from itertools import combinations

from .project import source_model_policy
from .source_model import BASE_COLLECTIONS, SourceIndex, content_id_patterns, finite_number, owner_types

RECORD_FIELDS = {
    'fieldDefinitions': {'fieldId', 'key', 'label', 'valueType', 'appliesTo', 'description'},
    'attributes': {'attributeId', 'ownerId', 'fieldId', 'values'},
    'fragments': {'fragmentId', 'ownerId', 'text', 'order', 'contentBindings', 'dependsOn', 'validDuring',
                  'contentRole', 'status', 'perspectiveId', 'sourceRefs', 'chapterContract'},
    'units': {'unitId', 'placement', 'title', 'ordinal', 'status', 'members'},
    'presentations': {'presentationId', 'eventId', 'chapterId', 'kind', 'order'},
}
REQUIRED_FIELDS = {
    'fieldDefinitions': {'fieldId', 'key', 'label', 'valueType', 'appliesTo'},
    'attributes': {'attributeId', 'ownerId', 'fieldId', 'values'},
    'fragments': {'fragmentId', 'ownerId', 'text', 'order', 'contentBindings', 'contentRole'},
    'units': {'unitId', 'placement', 'title', 'ordinal', 'members'},
    'presentations': {'presentationId', 'eventId', 'chapterId', 'kind', 'order'},
}
UNIT_MEMBER_FIELDS = {'memberType', 'memberId', 'order'}


def _strings(value, *, nonempty=False) -> bool:
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(x, str) and x.strip() for x in value)
            and len(value) == len(set(value)))


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _owner_kind(index, value):
    return index.owner_types.get(value) if isinstance(value, str) else None


def _validate_units(structure: dict, index, policy: dict) -> list[dict]:
    """schema-3 units are typed structural containers with placement and members.

    A volume unit's members must all share that volume; a project unit may span
    volumes, hold project-level chapters and hold not-yet-placed candidates. An
    active chapter or candidate belongs to at most one active unit.
    """
    from .source_placement import placement_error, placement_volume_id, VOLUME_SCOPE, PROJECT_SCOPE
    errors: list[dict] = []

    def err(code, path, **detail):
        errors.append({'code': code, 'path': path, **detail})

    candidates = index.planning_candidates or {}
    ordinals: set = set()
    active_membership: dict[str, int] = {}
    for unit in structure['units']:
        path = 'units.' + unit['unitId']
        reason = placement_error(unit.get('placement'))
        if reason is not None:
            err('SOURCE_UNIT_PLACEMENT_INVALID', path, reason=reason)
            continue
        volume_id = placement_volume_id(unit.get('placement'))
        if volume_id is not None and _owner_kind(index, volume_id) != 'volume':
            err('SOURCE_UNIT_PLACEMENT_INVALID', path, reason='DANGLING_VOLUME')
            continue
        if not _text(unit.get('title')):
            err('SOURCE_UNIT_INVALID', path)
            continue
        status = unit.get('status', 'active')
        if status not in policy['recordStatuses']:
            err('SOURCE_STATUS_INVALID', path)
        if type(unit.get('ordinal')) is not int or unit['ordinal'] < 1:
            err('SOURCE_ORDER_INVALID', path)
        else:
            scope_key = (VOLUME_SCOPE, volume_id) if volume_id is not None else (PROJECT_SCOPE,)
            if (scope_key, unit['ordinal']) in ordinals:
                err('SOURCE_UNIT_ORDER_DUPLICATE', path)
            else:
                ordinals.add((scope_key, unit['ordinal']))
        members = unit.get('members')
        if not isinstance(members, list):
            err('SOURCE_UNIT_MEMBERS_INVALID', path)
            continue
        seen_orders: set = set()
        seen_targets: set = set()
        for member in members:
            if not isinstance(member, dict) or set(member) != UNIT_MEMBER_FIELDS:
                err('SOURCE_UNIT_MEMBER_INVALID', path)
                continue
            mtype, mid, order = member['memberType'], member['memberId'], member['order']
            if mtype not in policy['unitMemberTypes'] or not isinstance(mid, str):
                err('SOURCE_UNIT_MEMBER_INVALID', path, memberId=mid)
                continue
            if type(order) is not int or order < 1:
                err('SOURCE_UNIT_MEMBER_ORDER_INVALID', path, memberId=mid)
            elif order in seen_orders:
                err('SOURCE_UNIT_MEMBER_ORDER_DUPLICATE', path, order=order)
            else:
                seen_orders.add(order)
            if (mtype, mid) in seen_targets:
                err('SOURCE_UNIT_MEMBER_DUPLICATE', path, memberId=mid)
                continue
            seen_targets.add((mtype, mid))
            if mtype == 'chapter':
                if _owner_kind(index, mid) != 'chapter':
                    err('SOURCE_UNIT_MEMBER_TARGET_MISSING', path, memberId=mid)
                    continue
                member_volume = placement_volume_id(index.objects[mid].get('placement'))
            else:
                candidate = candidates.get(mid)
                if candidate is None:
                    err('SOURCE_UNIT_MEMBER_TARGET_MISSING', path, memberId=mid)
                    continue
                if candidate.get('status') == 'materialized':
                    err('SOURCE_UNIT_MEMBER_MATERIALIZED', path, memberId=mid)
                    continue
                member_volume = placement_volume_id(candidate.get('placement'))
            if volume_id is not None and member_volume != volume_id:
                err('SOURCE_UNIT_MEMBER_VOLUME_MISMATCH', path, memberId=mid)
            if status != 'archived':
                active_membership[mid] = active_membership.get(mid, 0) + 1
    for mid, count in sorted(active_membership.items()):
        if count > 1:
            err('SOURCE_MEMBER_MULTIPLE_ACTIVE_UNITS', 'units', memberId=mid)
    return errors


def validate_source_content(structure: dict) -> list[dict]:
    policy = source_model_policy()
    errors = []

    def error(code, path, **detail):
        errors.append({'code': code, 'path': path, **detail})

    if structure.get('sourceMigration') is not None:
        error('SOURCE_MIGRATION_CANDIDATE', 'sourceMigration',
              message='migration review material cannot be used as current source')

    if not _text(structure.get('projectId')) or not _text(structure.get('bookName')) or type(structure.get('revision')) is not int or structure['revision'] < 0:
        error('SOURCE_HEADER_INVALID', 'project')

    for name, spec in policy['collections'].items():
        rows = structure.get(name)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            error('SOURCE_COLLECTION_INVALID', name)
        else:
            record_fields, required_fields = RECORD_FIELDS[name], REQUIRED_FIELDS[name]
            for row in rows:
                if set(row) - record_fields or required_fields - set(row):
                    error('SOURCE_RECORD_FIELDS_INVALID', name, unknown=sorted(set(row) - record_fields),
                          missing=sorted(required_fields - set(row)))
    for collection in BASE_COLLECTIONS:
        for row in structure.get(collection, []):
            label = 'name' if collection == 'entities' else 'title'
            if not _text(row.get(label)):
                error('SOURCE_OBJECT_LABEL_REQUIRED', collection)
            if collection == 'entities' and not _strings(row.get('aliases', [])):
                error('SOURCE_ALIASES_INVALID', collection)
            if collection == 'arcs' and not _strings(row.get('tags', [])):
                error('SOURCE_ARC_TAGS_INVALID', collection)
    if errors:
        return errors

    # Refuse two current values even if they happen to agree today.
    for row in structure.get('entities', []):
        if row.get('fields'):
            error('SOURCE_DUPLICATE_AUTHORITY', 'entities.' + str(row.get('entityId')) + '.fields')
    for name in ('customFields', 'managedViews', 'characterStates', 'pairStates', 'knowledgeStates',
                 'decisions', 'foreshadowing', 'evolutionLedger', 'timeAnchors', 'storyTimeAnchors'):
        if structure.get(name):
            error('SOURCE_DUPLICATE_AUTHORITY', name)

    patterns = content_id_patterns()
    identities = {structure.get('projectId'): 'project'}
    for collection, (key, _) in BASE_COLLECTIONS.items():
        identities.update({r.get(key): collection for r in structure.get(collection, [])})
    identities.update({r.get('relationId'): 'relations' for r in structure.get('relations', [])})
    for row in structure.get('pairs', []):
        rid = row['pairId']
        if rid in identities:
            error('GLOBAL_ID_COLLISION', 'pairs', id=rid, previous=identities[rid])
        identities[rid] = 'pairs'

    def identity(row, key, path):
        rid = row.get(key)
        if not isinstance(rid, str) or not patterns[key].fullmatch(rid):
            error('SOURCE_ID_INVALID', path, id=rid)
            return
        if rid in identities:
            error('GLOBAL_ID_COLLISION', path, id=rid, previous=identities[rid])
        identities[rid] = path

    for collection, spec in policy['collections'].items():
        for row in structure[collection]:
            identity(row, spec['idField'], collection)

    for attribute in structure['attributes']:
        path = 'attributes.' + str(attribute.get('attributeId'))
        values = attribute.get('values')
        if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
            error('SOURCE_VALUES_INVALID', path)
            continue
        for value in values:
            identity(value, 'valueId', path)
            if set(value) - {'valueId', 'value', 'state', 'validDuring'}:
                error('SOURCE_RECORD_FIELDS_INVALID', path, unknown=sorted(set(value) - {'valueId', 'value', 'state', 'validDuring'}))
    if errors:
        return errors

    index = SourceIndex.build(structure)
    from .source_time import validate_time_specs
    errors.extend(validate_time_specs(structure, index))
    if errors:
        return errors
    allowed_owners = owner_types()
    keys = set()
    for fid, field in index.fields.items():
        path = 'fieldDefinitions.' + fid
        if not _text(field.get('key')) or not _text(field.get('label')):
            error('SOURCE_FIELD_INVALID', path)
        key = field.get('key')
        if isinstance(key, str):
            if key in keys:
                error('SOURCE_FIELD_KEY_DUPLICATE', path, key=key)
            keys.add(key)
        if field.get('valueType') not in policy['valueTypes']:
            error('SOURCE_VALUE_TYPE_INVALID', path)
        if not _strings(field.get('appliesTo'), nonempty=True) or not set(field['appliesTo']) <= allowed_owners:
            error('SOURCE_FIELD_SCOPE_INVALID', path)
    if errors:
        return errors

    def period(record, path):
        validity = record.get('validDuring', {})
        if not isinstance(validity, dict) or set(validity) - {'fromId', 'toId'}:
            error('SOURCE_TIME_INVALID', path)
            return False
        for name, value in validity.items():
            if value is not None and (not isinstance(value, str) or index.owner_types.get(value) not in {'event', 'time_anchor'}):
                error('SOURCE_TIME_REFERENCE_INVALID', path + '.' + name, target=value)
                return False
        lower, upper = index.interval(record)
        same = validity.get('fromId') is not None and validity.get('fromId') == validity.get('toId')
        if same or (lower is not None and upper is not None and lower >= upper):
            error('SOURCE_TIME_ORDER_INVALID', path)
            return False
        return True

    used_slots = set()
    for aid, attribute in index.attributes.items():
        path = 'attributes.' + aid
        owner, fid = attribute.get('ownerId'), attribute.get('fieldId')
        if not isinstance(owner, str) or owner not in index.objects:
            error('SOURCE_OWNER_INVALID', path, ownerId=owner)
        if not isinstance(fid, str) or fid not in index.fields:
            error('SOURCE_FIELD_REFERENCE_INVALID', path, fieldId=fid)
            continue
        field = index.fields[fid]
        applies = field.get('appliesTo')
        if not isinstance(applies, list) or _owner_kind(index, owner) not in applies:
            error('SOURCE_ATTRIBUTE_SCOPE_INVALID', path)
        slot = (str(owner), fid)
        if slot in used_slots:
            error('SOURCE_ATTRIBUTE_DUPLICATE', path)
        used_slots.add(slot)
        kind = field.get('valueType')
        if kind == 'topic' and attribute['values']:
            error('SOURCE_TOPIC_VALUE_FORBIDDEN', path)
        valid_periods = []
        for value in attribute['values']:
            vpath = path + '.' + value['valueId']
            state = value.get('state', 'known')
            if state not in policy['valueStates']:
                error('SOURCE_VALUE_STATE_INVALID', vpath)
            elif state != 'known':
                if 'value' in value:
                    error('SOURCE_UNKNOWN_VALUE_PRESENT', vpath)
            else:
                actual = value.get('value')
                valid = {'text': isinstance(actual, str), 'number': finite_number(actual),
                         'boolean': type(actual) is bool,
                         'reference': isinstance(actual, str) and actual in index.records,
                         'measurement': isinstance(actual, dict) and set(actual) == {'number', 'unit'}
                         and finite_number(actual.get('number')) and _text(actual.get('unit'))}.get(kind, False)
                if not valid:
                    error('SOURCE_VALUE_INVALID', vpath, valueType=kind)
            if period(value, vpath):
                valid_periods.append(value)
        for left, right in combinations(valid_periods, 2):
            a, b = index.interval(left)
            c, d = index.interval(right)
            same = left.get('validDuring', {}) == right.get('validDuring', {})
            overlap = all(x is not None for x in (a, b, c, d)) and max(a, c) < min(b, d)
            if same or overlap:
                error('SOURCE_VALUE_PERIOD_OVERLAP', path, valueIds=[left['valueId'], right['valueId']])

    for fid, fragment in index.fragments.items():
        path = 'fragments.' + fid
        if not isinstance(fragment.get('ownerId'), str) or fragment['ownerId'] not in index.objects:
            error('SOURCE_OWNER_INVALID', path)
        bindings = fragment.get('contentBindings')
        if not _strings(bindings, nonempty=True):
            error('SOURCE_BINDINGS_REQUIRED', path, field='contentBindings',
                  expected={'type': 'array', 'minItems': 1, 'items': 'existing attributeId'},
                  schemaAction={'operation': 'source.schema', 'arguments': {}},
                  instruction='绑定已有属性ID；新章与推演用workflow.chapter-preview生成结构绑定，既有片段修改走源预览与批准提交。')
        else:
            for bid in bindings:
                if bid not in index.attributes:
                    error('SOURCE_BINDING_INVALID', path, target=bid, field='contentBindings',
                          expectedTarget='existing attributeId',
                          schemaAction={'operation': 'source.schema', 'arguments': {}},
                          instruction='contentBindings引用属性实例ID，不是字段定义ID或正文引证ID；核对后通过源预览与批准提交修订。')
        dependencies = fragment.get('dependsOn', [])
        if not _strings(dependencies):
            error('SOURCE_DEPENDENCIES_INVALID', path)
        else:
            for target in dependencies:
                if target not in index.records:
                    error('SOURCE_DEPENDENCY_INVALID', path, target=target)
        if not _text(fragment.get('text')):
            error('SOURCE_TEXT_REQUIRED', path)
        if type(fragment.get('order')) is not int or fragment['order'] < 0:
            error('SOURCE_ORDER_INVALID', path)
        if fragment.get('contentRole') not in policy['contentRoles']:
            error('SOURCE_ROLE_INVALID', path)
        if fragment.get('status', 'active') not in policy['recordStatuses']:
            error('SOURCE_STATUS_INVALID', path)
        if 'chapterContract' in fragment:
            from .chapter_contract import contract_error_details, validate_contract
            if fragment.get('contentRole') != 'plan' or index.owner_types.get(fragment.get('ownerId')) != 'chapter':
                error('CHAPTER_CONTRACT_SCOPE_INVALID', path)
            contract_errors = validate_contract(fragment['chapterContract'])
            for code in contract_errors:
                error(code, path + '.chapterContract', **contract_error_details(code))
            if not contract_errors:
                from .relationship_causality import validate_source_declarations
                errors.extend(validate_source_declarations(fragment, index))
        role = fragment.get('contentRole')
        perspective = _owner_kind(index, fragment.get('perspectiveId'))
        if role == 'belief' and perspective != 'character':
            error('SOURCE_PERSPECTIVE_REQUIRED', path)
        elif role == 'in_world_text' and perspective not in {'character', 'faction'}:
            error('SOURCE_PERSPECTIVE_REQUIRED', path)
        elif (role == 'fact' or role == 'plan') and fragment.get('perspectiveId') is not None:
            error('SOURCE_PERSPECTIVE_FORBIDDEN', path)
        period(fragment, path)

    errors.extend(_validate_units(structure, index, policy))
    for row in structure['presentations']:
        path = 'presentations.' + row['presentationId']
        if (_owner_kind(index, row.get('eventId')) != 'event'
                or _owner_kind(index, row.get('chapterId')) != 'chapter'
                or row.get('kind') not in policy['presentationKinds']):
            error('SOURCE_PRESENTATION_INVALID', path)
        if type(row.get('order')) is not int or row['order'] < 0:
            error('SOURCE_ORDER_INVALID', path)
    for row in structure.get('relations', []):
        if not _text(row.get('kind')):
            error('SOURCE_RELATION_KIND_REQUIRED', 'relations.' + str(row.get('relationId')))
    from .source_rules import validate_rules
    errors.extend(validate_rules(structure, index))
    if not errors:
        from .pair_classification import validate_pair_dependencies
        errors.extend(validate_pair_dependencies(structure, index))
    if not errors:
        from .catalog_commands import _validate_catalog
        from .structure_store import StructureError
        try:
            _validate_catalog(structure)
        except StructureError as exc:
            error(exc.code, 'catalog', **exc.details)
        except (TypeError, KeyError) as exc:
            error('SOURCE_CATALOG_INVALID', 'catalog', message=str(exc))
    return errors
