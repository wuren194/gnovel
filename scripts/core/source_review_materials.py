"""Assemble source-review reading material using existing selection and receipt semantics."""
from copy import deepcopy
import json

from .source_impact import ReviewInputs, read_review, fragment_review_status
from .source_review import _receipts, source_snapshot
from .source_selection import select_content
from .structure_store import StructureError


def review_visibility(source, record_ids, selection):
    selected = select_content(source, selection)
    visible = {row['id'] for row in selected['records'] if row['kind'] == 'fragment'}
    if set(record_ids) - visible:
        raise StructureError('EVIDENCE_SELECTION_INVALID', 'selected review records are outside the current source selection')
    # Owner selection chooses the tasks. Registered dependencies may have other
    # owners, but content exclusions, time and epistemic boundaries still apply.
    choice = {key: value for key, value in selected['selection'].items() if key not in {'ownerIds', 'ownerTypes'}}
    dependencies = select_content(source, choice)
    records = {row['id']: row['content'] for row in dependencies['records']}
    if choice['knowledgeMode'] == 'perspective':
        records = {rid: {key: value for key, value in row.items()
                         if key in {'fragmentId', 'ownerId', 'text', 'contentRole', 'perspectiveId'}}
                   for rid, row in records.items()}
    else:
        inputs = ReviewInputs(source)
        for aid in dependencies['selectedAttributeIds']:
            attribute = inputs.index.attributes[aid]
            records[aid] = {**{key: value for key, value in attribute.items() if key != 'values'},
                            'valueIds': [value['valueId'] for value in attribute['values'] if value['valueId'] in records]}
            field = inputs.index.fields[attribute['fieldId']]
            records[field['fieldId']] = field
        records.update({row['id']: row for row in dependencies['owners']})
    return selected['selection'], records


def review_materials(root, record_ids, selection, current_materials, dependencies):
    from .source_review_triage import review_origin, OPERATION_ID_PATTERN
    from .source_rules import rule_inputs, evaluate_rule
    source = source_snapshot(root)
    inputs, state = ReviewInputs(source), read_review(root)
    choice, visible = review_visibility(source, record_ids, selection)
    perspective = choice['knowledgeMode'] == 'perspective'
    receipts = {} if perspective else _receipts(root)['receipts']
    operations, operation_scopes, delivered, materials = {}, {}, set(record_ids), []
    current = {row['recordId']: row for row in current_materials}
    prior_by_record = {}
    for receipt_id, receipt in sorted(receipts.items()):
        for review in receipt['reviews']:
            prior_by_record.setdefault(review['recordId'], []).append((receipt_id, review))

    def add(kind, identity, value, **metadata):
        materials.append({'id': identity, 'materialKind': kind, **metadata,
                          'text': json.dumps(value, ensure_ascii=False, separators=(',', ':'))})

    for rid in record_ids:
        materials.append({key: value for key, value in current[rid].items() if key != 'inputIds'})
        roles = inputs.closure([rid])
        closure = set(roles) - {rid}
        allowed = closure & visible.keys()
        operation_id = state['items'].get(rid, {}).get('operationId')
        if not perspective and isinstance(operation_id, str) and OPERATION_ID_PATTERN.fullmatch(operation_id):
            from .prepare_dependencies import capture_query
            capture_query(root, dependencies, '.govel/undo-receipts', operation_id + '-*.json')
        origin = {} if perspective else review_origin(root, state, rid, operations)
        input_hash = inputs.fingerprint(rid)
        status = fragment_review_status(inputs, state, rid, input_hash=input_hash)
        add('review-task', 'task:' + rid, {
            'recordId': rid, 'status': status, 'dependencyIds': sorted(allowed),
            'omittedDependencyCount': len(closure - visible.keys()), 'origin': origin,
            'priorJudgmentsAreHistorical': True, 'authorContextOmitted': perspective,
            'decisionInput': {'recordId': rid, 'decision': None, 'reason': '', 'quotes': []},
            'perItemReviewRequired': True}, recordId=rid)
        for dependency in sorted(allowed - delivered):
            content = visible[dependency]
            if roles[dependency]:
                from .source_impact import time_constraint_content
                content = time_constraint_content(content)
            add('dependency', 'dependency:' + dependency, content, dependencyId=dependency)
            delivered.add(dependency)
        if perspective:
            continue
        for receipt_id, review in prior_by_record.get(rid, []):
            add('prior-review', receipt_id + ':' + rid, {**review, 'receiptId': receipt_id,
                'historical': True, 'inputsStillCurrent': review['inputHash'] == input_hash,
                'invalidationReason': None if review['inputHash'] == input_hash
                                      else 'fragment_or_registered_dependencies_changed'}, recordId=rid)
        operation_id = origin.get('operationId')
        if operation_id:
            operation_scopes.setdefault(operation_id, set()).update(closure | {rid})
        for rule in inputs.rules:
            rule_id = rule['entityId']
            scope = closure | {rid, inputs.index.fragments[rid]['ownerId'], source['projectId']}
            if (rule.get('ruleSpec') and rule.get('status') != 'archived' and rule_id not in delivered
                    and (set(rule_inputs(rule)) & scope or set(rule['ruleSpec']['scopeIds']) & scope)):
                add('rule', rule_id, {'rule': rule, 'evaluation': evaluate_rule(rule, inputs),
                                    'evaluationScope': 'source inputs only; chapter/action receipts are not inferred',
                                    'semanticReviewComplete': False})
                delivered.add(rule_id)
    for operation_id, scope in sorted(operation_scopes.items()):
        add('source-change', operation_id, change_material(root, operations[operation_id], operation_id,
                                                         scope, visible, selection))
    return materials


def change_material(root, operation, operation_id, scope, visible, selection):
    from .structure_commands import _operation_paths
    from .paths import safe_project_path
    from .write_sets import digest
    receipt = operation['receipt']
    result = {'operationId': operation_id, 'historical': True, 'undone': operation['undone'],
              'receiptAvailable': receipt is not None, 'beforeAvailable': False, 'changes': []}
    if receipt is None:
        return result
    changed = set(receipt.get('impact', {}).get('changedIds', [])) & scope & visible.keys()
    before_records = {}
    _, _, backup = _operation_paths(root, operation_id)
    before_path = safe_project_path(root, str((backup / 'structure.json').relative_to(root)))
    try:
        raw = before_path.read_bytes()
        if digest(raw) != receipt.get('beforeFiles', {}).get('.govel/structure.json', {}).get('sha256'):
            raise ValueError('before-image differs from its receipt')
        _, before_records = review_visibility(json.loads(raw), [], selection)
        result['beforeAvailable'] = True
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['beforeUnavailableReason'] = str(exc)
    result.update(operation=receipt.get('operation'), reason=receipt.get('reason'),
                  changes=[{'recordId': rid, 'before': before_records.get(rid), 'current': visible[rid]}
                           for rid in sorted(changed)],
                  scope='selected registered inputs; current values are not the historical after-image')
    return result


def select_aggregate(root, reference, record_ids, selection):
    from .prepare_dependencies import validate_dependencies
    from .transport_cache import get
    saved = get(root, reference, kind='aggregate-selection')
    validate_dependencies(root, saved['dependencies'], code='AGGREGATE_SELECTION_STALE')
    if not isinstance(record_ids, list) or not record_ids or set(record_ids) - set(saved['recordIds']):
        raise StructureError('AGGREGATE_SELECTION_INVALID', 'select source-review records from this aggregate, not historical impact rows')
    if selection is not None and selection != saved['selection']:
        raise StructureError('AGGREGATE_SELECTION_INVALID', 'retain the aggregate source selection')
    return deepcopy(saved['selection'])
