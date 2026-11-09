"""One selection semantics for machine queries and human reading snapshots."""
from __future__ import annotations

import hashlib
from copy import deepcopy

from .project import source_model_policy
from .source_model import SourceIndex, owner_types, snapshot_hash
from .structure_store import StructureError, canonical_json, validate_structure

SELECTION_KEYS = {'ownerIds', 'ownerTypes', 'includeFields', 'excludeFields', 'includeAttributes',
                  'excludeAttributes', 'roles', 'perspectiveIds', 'knowledgeMode',
                  'includeArchived', 'asOf', 'unknownTime'}
LIST_KEYS = SELECTION_KEYS - {'includeArchived', 'asOf', 'unknownTime', 'knowledgeMode'}

# Perspective text is authored for that reader; provenance and dependency notes
# belong to the author. An explicit projection also keeps future metadata from
# becoming character knowledge merely because the fragment itself is visible.
PERSPECTIVE_FRAGMENT_FIELDS = {
    'fragmentId', 'ownerId', 'text', 'order', 'contentBindings', 'validDuring',
    'contentRole', 'status', 'perspectiveId',
}


def metadata_visible(record: dict, choice: dict) -> bool:
    """A selected belief grants its text, never its owner's author-only labels."""
    if choice['knowledgeMode'] != 'perspective':
        return True
    from .context_builder import _explicitly_visible
    return any(_explicitly_visible(record, perspective) for perspective in choice['perspectiveIds'])


def metadata_label(record: dict, identity: str, choice: dict) -> str:
    if not metadata_visible(record, choice):
        return identity
    return record.get('name') or record.get('title') or record.get('bookName') or identity


def normalize_selection(selection: dict | None, index: SourceIndex) -> dict:
    if selection is None:
        selection = {}
    if not isinstance(selection, dict) or set(selection) - SELECTION_KEYS:
        raise StructureError('SOURCE_SELECTION_INVALID', 'unknown selection fields')
    result = deepcopy(selection)
    for key in LIST_KEYS & set(result):
        values = result[key]
        if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
            raise StructureError('SOURCE_SELECTION_INVALID', key + ' must be a string list')
        result[key] = sorted(set(values))
    if 'includeArchived' in result and type(result['includeArchived']) is not bool:
        raise StructureError('SOURCE_SELECTION_INVALID', 'includeArchived must be a boolean')
    if result.get('asOf') is not None and (not isinstance(result['asOf'], str) or index.owner_types.get(result['asOf']) not in {'event', 'time_anchor'}):
        raise StructureError('SOURCE_SELECTION_INVALID', 'asOf must identify an event or time anchor, or be null for an undetermined time')
    if not isinstance(result.get('unknownTime', 'include'), str) or result.get('unknownTime', 'include') not in {'include', 'exclude'}:
        raise StructureError('SOURCE_SELECTION_INVALID', 'unknownTime must be include or exclude')
    if result.get('knowledgeMode', 'author') not in source_model_policy()['knowledgeModes']:
        raise StructureError('SOURCE_SELECTION_INVALID', 'knowledgeMode must be author or perspective')
    known = {'ownerIds': set(index.objects), 'ownerTypes': owner_types(),
        'includeAttributes': set(index.attributes), 'excludeAttributes': set(index.attributes),
        'includeFields': set(index.fields) | {x['key'] for x in index.fields.values()},
        'excludeFields': set(index.fields) | {x['key'] for x in index.fields.values()},
        'roles': set(source_model_policy()['contentRoles']),
        'perspectiveIds': {rid for rid, kind in index.owner_types.items() if kind in {'character', 'faction'}}}
    for key, allowed in known.items():
        unknown = set(result.get(key, [])) - allowed
        if unknown:
            raise StructureError('SOURCE_SELECTION_UNKNOWN', 'selection names unknown identities', {'field': key, 'unknown': sorted(unknown)})
    result.setdefault('includeArchived', False)
    result.setdefault('unknownTime', 'include')
    result.setdefault('knowledgeMode', 'author')
    if result['knowledgeMode'] == 'perspective' and not result.get('perspectiveIds'):
        raise StructureError('SOURCE_SELECTION_INVALID', 'perspective knowledge requires perspectiveIds')
    return result


def time_match(index: SourceIndex, record: dict, as_of: str | None) -> str:
    """Half-open validity; missing order is unknown, never an invented date."""
    if as_of is None:
        return 'all'
    period = record.get('validDuring') or {}
    if not period.get('fromId') and not period.get('toId'):
        return 'inside'
    if period.get('toId') == as_of:
        return 'outside'
    if period.get('fromId') == as_of and not period.get('toId'):
        return 'inside'
    if index.time_states is None:
        from .source_time import story_time_states
        index.time_states = story_time_states({}, index=index)
    point = index.time_states.get(as_of, {})
    start = index.time_states.get(period.get('fromId'), {})
    end = index.time_states.get(period.get('toId'), {})

    def before(left, right):
        high, low = left.get('upper'), right.get('lower')
        return high is not None and low is not None and (
            high < low or (high == low and
                           (left.get('upperExclusive', False) or right.get('lowerExclusive', False))))

    def at_or_after(left, right):
        low, high = left.get('lower'), right.get('upper')
        return low is not None and high is not None and low >= high

    # Bounds can prove a whole possible occurrence window inside/outside the
    # validity interval. Overlap remains unknown; no representative date is used.
    if before(point, start) or at_or_after(point, end):
        return 'outside'
    after_start = (not period.get('fromId') or period['fromId'] == as_of
                   or at_or_after(point, start))
    before_end = not period.get('toId') or before(point, end)
    # Relative constraints can establish validity even without any absolute
    # date. Compare only still-unresolved pairs and cache them in this snapshot.
    def difference(left, right):
        from .source_time import time_point_order
        pair = (left, right)
        if pair not in index.time_comparisons:
            index.time_comparisons[pair] = time_point_order({}, left, right, index=index)['difference']
        return index.time_comparisons[pair]

    if not after_start:
        delta = difference(as_of, period['fromId'])
        low, high = delta['lower'], delta['upper']
        if high is not None and (high < 0 or high == 0 and delta['upperExclusive']):
            return 'outside'
        after_start = low is not None and low >= 0
    if not before_end:
        delta = difference(as_of, period['toId'])
        low, high = delta['lower'], delta['upper']
        if low is not None and low >= 0:
            return 'outside'
        before_end = high is not None and (high < 0 or high == 0 and delta['upperExclusive'])
    return 'inside' if after_start and before_end else 'unknown'


def select_content(structure: dict, selection: dict | None = None) -> dict:
    validate_structure(structure)
    index = SourceIndex.build(structure)
    choice = normalize_selection(selection, index)
    owners = {oid for oid, obj in index.objects.items()
              if ('ownerIds' not in choice or oid in choice['ownerIds'])
              and ('ownerTypes' not in choice or index.owner_types[oid] in choice['ownerTypes'])
              and (choice['includeArchived'] or obj.get('status') != 'archived')}

    def field_selected(aid, key):
        field = index.fields[index.attributes[aid]['fieldId']]
        return field['fieldId'] in choice.get(key, []) or field['key'] in choice.get(key, [])

    all_scoped = {aid for aid, attr in index.attributes.items() if attr['ownerId'] in owners}
    explicit = 'includeFields' in choice or 'includeAttributes' in choice
    selected = {aid for aid in all_scoped
                if (not explicit or field_selected(aid, 'includeFields') or aid in choice.get('includeAttributes', []))
                and not field_selected(aid, 'excludeFields') and aid not in choice.get('excludeAttributes', [])}
    records, unknown_count, excluded_time_count, excluded_knowledge_count = [], 0, 0, 0

    def retain(record):
        nonlocal unknown_count, excluded_time_count
        if 'asOf' in choice and choice['asOf'] is None:
            period = record.get('validDuring') or {}
            match = 'unknown' if period.get('fromId') or period.get('toId') else 'inside'
        else:
            match = time_match(index, record, choice.get('asOf'))
        if match == 'unknown':
            unknown_count += 1
        if match == 'outside' or (match == 'unknown' and choice['unknownTime'] == 'exclude'):
            excluded_time_count += 1
            return None
        return match

    owner_order = {oid: n for n, oid in enumerate(index.objects)}
    for aid, attribute in index.attributes.items():
        if aid not in selected:
            continue
        if choice['knowledgeMode'] == 'perspective':
            excluded_knowledge_count += len(attribute['values'])
            continue
        field = index.fields[attribute['fieldId']]
        for value in attribute['values']:
            match = retain(value)
            if match is None:
                continue
            records.append({'id': value['valueId'], 'kind': 'value', 'ownerId': attribute['ownerId'],
                            'attributeId': aid, 'fieldId': field['fieldId'], 'fieldKey': field['key'],
                            'label': field['label'], 'valueType': field['valueType'], 'timeMatch': match, 'content': deepcopy(value)})
    for fid, fragment in index.fragments.items():
        matches = set(fragment['contentBindings']) & selected
        if not matches or (not choice['includeArchived'] and fragment.get('status') == 'archived'):
            continue
        if 'roles' in choice and fragment['contentRole'] not in choice['roles']:
            continue
        if choice['knowledgeMode'] == 'perspective':
            if fragment['contentRole'] not in {'belief', 'in_world_text'} or fragment.get('perspectiveId') not in choice['perspectiveIds']:
                excluded_knowledge_count += 1
                continue
        match = retain(fragment)
        if match is None:
            continue
        owner = fragment['ownerId'] if fragment['ownerId'] in owners else min(
            (index.attributes[aid]['ownerId'] for aid in matches), key=owner_order.get)
        content = {key: deepcopy(value) for key, value in fragment.items()
                   if choice['knowledgeMode'] != 'perspective' or key in PERSPECTIVE_FRAGMENT_FIELDS}
        records.append({'id': fid, 'kind': 'fragment', 'ownerId': owner,
                        'sourceOwnerId': fragment['ownerId'], 'matchedBindings': sorted(matches),
                        'timeMatch': match, 'content': content})
    records.sort(key=lambda r: (owner_order[r['ownerId']], r['kind'] == 'fragment', r['content'].get('order', 0), r['id']))
    if choice['knowledgeMode'] == 'perspective':
        # Do not reveal names or even enumerate unrelated objects from the author
        # database when no perspective text actually selected those owners.
        owners &= {row['ownerId'] for row in records}
    selected_owners = [oid for oid in index.objects if oid in owners]
    return {'snapshot': {'projectId': structure['projectId'], 'revision': structure['revision'],
                         'sourceHash': snapshot_hash(structure)},
            'selection': choice,
            'owners': [{'id': oid, 'kind': index.owner_types[oid],
                        'label': metadata_label(index.objects[oid], oid, choice)}
                       for oid in selected_owners],
            'records': records, 'selectedAttributeIds': sorted(selected),
            'coverage': {'registeredSourceSelectionComplete': True, 'proseScanned': False,
                         'semanticReviewComplete': False, 'unknownTimeCount': unknown_count,
                         'excludedByTimeCount': excluded_time_count,
                         'excludedByKnowledgeCount': excluded_knowledge_count,
                         **({'temporalScope': ('as_of' if choice.get('asOf') else
                                              'undetermined' if 'asOf' in choice else 'all_periods'),
                             'authorMetadataWithheld': True}
                            if choice['knowledgeMode'] == 'perspective' else {}),
                         'knowledgeBasis': 'explicit fragment role and perspective only'},
            'warnings': ((['整段选择保留混合内容；排除属性不保证该属性的字词完全消失。']
                         if choice.get('excludeFields') or choice.get('excludeAttributes') else [])
                         + (['视角查询只包含明确登记为该人物／势力信念或文献的片段；未登记认知不被推测为已知。']
                            if choice['knowledgeMode'] == 'perspective' else []))}


def select_project_content(root, selection: dict | None = None) -> dict:
    """Snapshot selection plus current review evidence, shared by query and export."""
    from .consistency import require_no_pending
    from .source_impact import read_review, ReviewInputs, fragment_review_status
    from .structure_store import load_structure
    require_no_pending(root)
    source = load_structure(root)
    result = select_content(source, selection)
    review = read_review(root)
    inputs = ReviewInputs(source)
    require_no_pending(root)
    for record in result['records']:
        if record['kind'] != 'fragment':
            continue
        record['reviewStatus'] = fragment_review_status(inputs, review, record['id'])
    result['snapshot']['reviewStateHash'] = hashlib.sha256(canonical_json(review).encode()).hexdigest()
    choice = result['selection']
    result['bookName'] = metadata_label(source, source['projectId'], choice)
    result['sourceUpdatedAt'] = source['updatedAt']
    result['fieldLabels'] = {key: field['label'] if metadata_visible(field, choice) else key
                             for field in source['fieldDefinitions'] for key in (field['fieldId'], field['key'])}
    result['timeLabels'] = {row['entityId']: metadata_label(row, row['entityId'], choice)
                            for row in source['entities'] if row['entityType'] in {'event', 'time_anchor'}}
    index = inputs.index
    labels = {rid: metadata_label(row, rid, choice)
              for rid, row in index.objects.items()}
    for rid in index.records:
        if rid in labels:
            continue
        if choice['knowledgeMode'] == 'perspective':
            labels[rid] = rid
            continue
        if rid in index.fields:
            labels[rid] = index.fields[rid]['label']
        elif rid in index.attributes:
            attr = index.attributes[rid]
            labels[rid] = labels[attr['ownerId']] + ' · ' + index.fields[attr['fieldId']]['label']
        else:
            labels[rid] = labels.get(index.owner(rid), '项目') + '的关联内容'
    result['referenceLabels'] = labels
    return result
