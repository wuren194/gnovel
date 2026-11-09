"""Find item links in existing journals; there is no separate maintenance ledger."""
from __future__ import annotations

from pathlib import Path

from .jsonio import read_json
from .source_requests import operation_undone
from .structure_commands import _operation_paths
from .structure_store import StructureError
from .write_sets import safe_project_path


def bound_records(receipt: dict, origin: dict, item: dict) -> set[str] | None:
    binding = receipt.get('maintenance')
    if binding is None:
        return None  # Explicitly selected historical receipt, before item linkage existed.
    if not isinstance(binding, dict) or not isinstance(binding.get('items'), list):
        raise StructureError('MAINTENANCE_RECEIPT_INVALID', 'invalid item binding')
    if binding.get('source') != origin:
        return set()
    records = set()
    for row in binding['items']:
        if not isinstance(row, dict) or not isinstance(row.get('recordIds'), list) or \
                any(not isinstance(rid, str) for rid in row['recordIds']):
            raise StructureError('MAINTENANCE_RECEIPT_INVALID', 'invalid bound targets')
        if row.get('itemId') == item['itemId'] and row.get('quote') == item['quote']:
            records.update(row['recordIds'])
    if records - set(receipt.get('impact', {}).get('changedIds', [])):
        raise StructureError('MAINTENANCE_RECEIPT_INVALID', 'item binding exceeds changed records')
    return records


def load_operation(root: Path, operation_id: str) -> dict:
    _, journal, _ = _operation_paths(root, operation_id)
    receipt = read_json(journal) if journal.is_file() else None
    if receipt is not None and (not isinstance(receipt, dict) or receipt.get('operationId') != operation_id
                                or not receipt.get('committedAt')):
        raise StructureError('MAINTENANCE_RECEIPT_INVALID', 'invalid committed receipt', {'operationId': operation_id})
    return {'receipt': receipt, 'undone': operation_undone(root, operation_id)}


def discover_receipts(root: Path, checklist: dict) -> tuple[dict, dict]:
    matches = {item['itemId']: [] for item in checklist['items']}
    receipts = {}
    directory = safe_project_path(root, '.govel/operations')
    if not directory.is_dir():
        return matches, receipts
    for path in sorted(directory.glob('OP-*.json')):
        state = load_operation(root, path.stem)
        receipt = state['receipt']
        if receipt is None or not receipt.get('maintenance'):
            continue
        matched = False
        for item in checklist['items']:
            if bound_records(receipt, checklist['source'], item):
                matches[item['itemId']].append(path.stem)
                matched = True
        if matched:
            receipts[path.stem] = state
    return matches, receipts


def resolve_item_handles(origin: dict, item: dict, operations: dict) -> tuple[set, dict]:
    """Only a current, item-bound creation receipt can resolve a temporary identity."""
    handles = {rid for rid in item['recordIds'] if rid.startswith('$')}
    if not handles:
        return set(item['recordIds']), {'resolved': {}, 'unresolved': {}}
    candidates = {handle: set() for handle in handles}
    for state in operations.values():
        receipt = state['receipt']
        if receipt is None or state['undone']:
            continue
        bound = bound_records(receipt, origin, item)
        if not bound:
            continue
        identities = receipt.get('identityMap', {})
        if not isinstance(identities, dict):
            raise StructureError('MAINTENANCE_RECEIPT_INVALID', 'invalid creation identity map')
        for handle in handles:
            target = identities.get(handle)
            if target is not None:
                if not isinstance(target, str) or target.startswith('$'):
                    raise StructureError('MAINTENANCE_RECEIPT_INVALID', 'invalid resolved identity')
                if target in bound:
                    candidates[handle].add(target)
    resolved = {handle: next(iter(ids)) for handle, ids in candidates.items() if len(ids) == 1}
    unresolved = {handle: sorted(ids) for handle, ids in candidates.items() if len(ids) != 1}
    return {resolved.get(rid, rid) for rid in item['recordIds']}, {'resolved': resolved, 'unresolved': unresolved}
