"""Resume frozen reading only across explicitly identified, proven own submissions."""
from copy import deepcopy

from .prepare_dependencies import _state, validate_dependencies
from .source_impact import REVIEW_PATH, read_review
from .source_review import RECEIPTS_PATH, _receipts, source_snapshot, review_receipt, review_state_item
from .structure_store import StructureError

PROGRESS_FILES = (REVIEW_PATH, RECEIPTS_PATH)


def review_baseline(root):
    return {'state': read_review(root), 'ledger': _receipts(root)}


def revalidate_with_progress(root, dependencies, states):
    updated = deepcopy(dependencies)
    for path, state in states.items():
        if path in updated['files']:
            updated['files'][path]['state'] = state
    validate_dependencies(root, updated, code='CURSOR_STALE')
    return updated


def resume_reading(root, saved, request_ids):
    from .evidence_submission import _resolve, _identity, _completed, _assert_current
    from .source_model import snapshot_hash
    from .transport_cache import put
    if (not isinstance(request_ids, list) or not request_ids
            or any(not isinstance(rid, str) or not rid for rid in request_ids)
            or len(set(request_ids)) != len(request_ids) or not saved.get('reviewBaseline')):
        raise StructureError('EVIDENCE_RESUME_INVALID', 'resume needs distinct own request IDs and a review reading baseline')
    states = {path: _state(root / path) for path in PROGRESS_FILES}
    dependencies = revalidate_with_progress(root, saved['dependencies'], states)
    baseline = deepcopy(saved['reviewBaseline'])
    source = source_snapshot(root)
    fragments = {row['fragmentId']: row for row in source['fragments']}
    progress = {}
    for request_id in request_ids:
        value = _resolve(root, request_id=request_id)
        if value['operation'] != 'source.review-submit' or value['snapshot'] != saved['snapshot']:
            raise StructureError('EVIDENCE_RESUME_INVALID', 'submission must belong to this exact prepared selection')
        _assert_current(root, value)
        key = _identity(request_id)
        prior = baseline['ledger'].get('requests', {}).get(key)
        if prior is not None and prior != value:
            raise StructureError('EVIDENCE_RESUME_INVALID', 'the frozen submission request changed')
        baseline['ledger'].setdefault('requests', {})[key] = value
        for index, receipt_id in _completed(root, value).items():
            arguments = value['batches'][index]['arguments']
            receipt = review_receipt(source, arguments['requestId'], arguments['reviews'], 'app')
            baseline['ledger']['receipts'][receipt_id] = receipt
            for review in arguments['reviews']:
                row = review_state_item(source, review, receipt_id, fragments[review['recordId']])
                baseline['state']['items'][review['recordId']] = row
                progress[review['recordId']] = row
            baseline['state']['sourceHash'] = snapshot_hash(source)
    if review_baseline(root) != baseline or any(_state(root / path) != states[path] for path in PROGRESS_FILES):
        raise StructureError('EVIDENCE_RESUME_CHANGED', 'review history changed beyond the selected own submissions')
    validate_dependencies(root, dependencies, code='CURSOR_STALE')
    reference = put(root, 'evidence-progress', {'requestIds': request_ids, 'currentRecords': progress})
    return dependencies, states, {'materialsAtPreparation': True, 'recordedCount': len(progress),
                                  'currentProgressRef': reference, 'approvalGranted': False}
