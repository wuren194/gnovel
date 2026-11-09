"""Observed maintenance inputs and item provenance shared by preview, commit and reports."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .structure_store import StructureError
from .write_sets import guard_observation, safe_project_path, validate_guards


def observe_input(root: Path, relative: str) -> tuple[bytes, dict]:
    if not isinstance(relative, str) or not relative:
        raise StructureError('MAINTENANCE_INPUT_INVALID', 'project-relative input path required')
    path = safe_project_path(root, relative)
    observed, payload = guard_observation(path)
    if payload is None:
        raise StructureError('MAINTENANCE_INPUT_INVALID', 'input must be a readable regular file', {'path': relative})
    return payload, {'path': str(path.relative_to(root)), **observed}


def load_checklist(root: Path, checklist_path: str) -> tuple[dict, list[dict]]:
    raw, guard = observe_input(root, checklist_path)
    if Path(guard['path']).parts[0] != '工作台':
        raise StructureError('MAINTENANCE_CHECKLIST_INVALID', 'checklist must be in 工作台')
    try:
        checklist = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise StructureError('MAINTENANCE_CHECKLIST_INVALID', 'checklist must be UTF-8 JSON') from exc
    if not isinstance(checklist, dict) or set(checklist) != {'source', 'items'}:
        raise StructureError('MAINTENANCE_CHECKLIST_INVALID', 'source and items required')
    origin, items = checklist['source'], checklist['items']
    if not isinstance(origin, dict) or set(origin) != {'path', 'sha256'} or \
            not isinstance(origin['sha256'], str) or not isinstance(items, list):
        raise StructureError('MAINTENANCE_CHECKLIST_INVALID', 'source path/hash and items list required')
    document_bytes, document_guard = observe_input(root, origin['path'])
    if document_guard['sha256'] != origin['sha256']:
        raise StructureError('MAINTENANCE_SOURCE_STALE', 'source document changed; review checklist mappings')
    try:
        document = document_bytes.decode('utf-8')
    except UnicodeError as exc:
        raise StructureError('MAINTENANCE_INPUT_INVALID', 'source document must be UTF-8') from exc
    origin['path'] = document_guard['path']
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'itemId', 'quote', 'recordIds', 'operationIds'} or \
                not isinstance(item['itemId'], str) or not item['itemId'].strip() or item['itemId'] in seen or \
                not isinstance(item['quote'], str) or not item['quote'].strip() or item['quote'] not in document or \
                any(not isinstance(item[key], list) or any(not isinstance(v, str) or not v for v in item[key])
                    for key in ('recordIds', 'operationIds')):
            raise StructureError('MAINTENANCE_CHECKLIST_INVALID', 'unique items, exact quotes and ID lists required')
        seen.add(item['itemId'])
    guards = [guard, document_guard]
    validate_guards(root, guards, stage='maintenance-read')
    return checklist, guards


def bind_maintenance_inputs(root: Path, plan: dict) -> None:
    reference = plan.get('parameters', {}).get('maintenanceReference')
    if reference is None:
        return
    if not isinstance(reference, dict) or set(reference) != {'checklistPath', 'itemIds'} or \
            not isinstance(reference['itemIds'], list) or not reference['itemIds'] or \
            any(not isinstance(v, str) or not v for v in reference['itemIds']):
        raise StructureError('MAINTENANCE_REFERENCE_INVALID', 'checklistPath and nonempty itemIds required')
    checklist, guards = load_checklist(root, reference['checklistPath'])
    by_id = {item['itemId']: item for item in checklist['items']}
    if set(reference['itemIds']) - by_id.keys():
        raise StructureError('MAINTENANCE_REFERENCE_INVALID', 'unknown checklist item')
    changed = set(plan['impact']['changedIds'])
    identities = plan.get('parameters', {}).get('identityMap', {})
    bindings, mapped = [], set()
    for item_id in sorted(set(reference['itemIds'])):
        item = by_id[item_id]
        targets = {identities.get(rid, rid) for rid in item['recordIds']} & changed
        if not targets:
            raise StructureError('MAINTENANCE_SCOPE_MISMATCH', 'selected item has no changed target', {'itemId': item_id})
        bindings.append({'itemId': item_id, 'quote': item['quote'], 'recordIds': sorted(targets)})
        mapped.update(targets)
    if {row['path'] for row in guards} & {row['path'] for row in plan['writeSet']}:
        raise StructureError('MAINTENANCE_INPUT_IS_OUTPUT', 'a source transaction cannot rewrite its maintenance inputs')
    plan['maintenance'] = {'source': deepcopy(checklist['source']), 'checklistPath': guards[0]['path'],
                           'checklistHash': guards[0]['sha256'], 'items': bindings,
                           'unmappedChangedIds': sorted(changed - mapped)}
    plan['maintenanceGuards'] = guards


def validate_maintenance_binding(root: Path, plan: dict) -> None:
    """Rebuild the provenance from the guarded inputs, rather than trusting plan metadata."""
    reference = plan.get('parameters', {}).get('maintenanceReference')
    if reference is None:
        if 'maintenance' in plan or 'maintenanceGuards' in plan:
            raise StructureError('MAINTENANCE_REFERENCE_INVALID', 'binding has no declared reference')
        return
    observed = {key: plan[key] for key in ('parameters', 'impact', 'writeSet')}
    bind_maintenance_inputs(root, observed)
    if any(observed[key] != plan.get(key) for key in ('maintenance', 'maintenanceGuards')):
        raise StructureError('MAINTENANCE_BINDING_STALE', 'maintenance binding differs from its approved snapshot')
