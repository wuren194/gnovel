"""Read-only coverage of a workbench checklist against existing receipts and reviews."""
from __future__ import annotations

from pathlib import Path

from .consistency import require_no_pending
from .source_impact import ReviewInputs, read_review, fragment_review_status
from .source_model import snapshot_hash
from .source_review import bounded_page, fingerprint, source_snapshot
from .write_sets import validate_guards
from .source_review_triage import review_origin
from .source_maintenance_inputs import load_checklist
from .source_maintenance_receipts import bound_records, discover_receipts, load_operation, resolve_item_handles


def check_maintenance(root: Path, *, checklistPath: str, limit=None, maxBytes=None, cursor=None) -> dict:
    root = root.expanduser().resolve()
    source = source_snapshot(root)
    checklist, guards = load_checklist(root, checklistPath)
    items = checklist['items']
    document_hash = checklist['source']['sha256']
    inputs, reviews = ReviewInputs(source), read_review(root)
    automatic, receipts = discover_receipts(root, checklist)
    rows = []
    review_operations = {}
    for item in items:
        changed, impacted, unavailable, undone, unrelated = set(), set(), [], [], []
        operation_ids = sorted(set(item['operationIds']) | set(automatic[item['itemId']]))
        for oid in operation_ids:
            if oid not in receipts:
                receipts[oid] = load_operation(root, oid)
            state = receipts[oid]
            if state['receipt'] is None:
                unavailable.append(oid)
                continue
            if state['undone']:
                undone.append(oid)
                continue
            binding = bound_records(state['receipt'], checklist['source'], item)
            if binding == set():
                unrelated.append(oid)
                continue
            impact = state['receipt'].get('impact', {})
            changed.update(impact.get('changedIds', []) if binding is None else binding)
            impacted.update(impact.get('reviewFragmentIds', []))
        targets, handles = resolve_item_handles(checklist['source'], item, {oid: receipts[oid] for oid in operation_ids})
        review_rows = []
        for rid in sorted(impacted | (targets & inputs.index.fragments.keys())):
            status = fragment_review_status(inputs, reviews, rid)
            review_rows.append({'recordId': rid, 'status': status,
                                **review_origin(root, reviews, rid, review_operations)})
        missing = sorted(targets - changed)
        unknown = sorted(targets - inputs.index.records.keys())
        rows.append({'itemId': item['itemId'], 'quote': item['quote'],
                     'recordIds': sorted(targets), 'operationIds': operation_ids, 'automaticallyLinkedOperationIds': automatic[item['itemId']],
                     **({'declaredRecordIds': item['recordIds'], 'handleResolution': handles}
                        if handles['resolved'] or handles['unresolved'] else {}),
                     'historicallySubmitted': bool(targets) and not (missing or unavailable or unrelated),
                     'uncoveredRecordIds': missing, 'unknownCurrentRecordIds': unknown,
                     'unavailableOperationIds': unavailable, 'undoneOperationIds': undone,
                     'unrelatedOperationIds': unrelated,
                     'currentReviews': review_rows,
                     'currentReviewsReady': bool(review_rows) and not unknown and all(
                         row['status'] in {'compatible', 'resolved'} for row in review_rows)})
    require_no_pending(root)
    validate_guards(root, guards, stage='maintenance-check')
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'documentHash': document_hash,
                                       'checklistHash': fingerprint(checklist), 'reviewHash': fingerprint(reviews),
                                       'receiptsHash': fingerprint(receipts),
                                       'reviewOperationsHash': fingerprint(review_operations)},
                        selection={'checklistPath': checklistPath}, code='SOURCE_MAINTENANCE_CHECK',
                        limit=limit, maxBytes=maxBytes, cursor=cursor,
                        summary={'itemCount': len(rows), 'needsMapping': not rows or any(not row['recordIds'] for row in rows),
                                 'submittedItemCount': sum(row['historicallySubmitted'] for row in rows),
                                 'reviewReadyItemCount': sum(row['currentReviewsReady'] for row in rows)},
                        coverage={'scope': 'explicit checklist mappings and registered receipt impacts',
                                  'documentRequirementsFullyEnumerated': 'not_verified',
                                  'mappingSemanticsVerified': False, 'proseScanned': False,
                                  'semanticReviewComplete': False,
                                  'absenceMeaning': 'receipt coverage does not prove current text matches the requirement'})
