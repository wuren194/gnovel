"""Bound evidence handoffs; progress is derived from the existing domain receipts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .artifacts import sha256_json
from .locks import project_lock_reentrant
from .project import query_policy
from .query import _size
from .structure_store import StructureError
from .transport_cache import get, put
from .write_sets import apply_write_set, capture_guards, json_bytes


def _identity(request_id):
    return sha256_json({'evidenceRequestId': request_id})


def _ledger(root, operation):
    if operation == 'source.review-submit':
        from .source_review import _receipts, RECEIPTS_PATH
        return RECEIPTS_PATH, _receipts(root)
    from .source_evidence import read_evidence, EVIDENCE_PATH
    return EVIDENCE_PATH, read_evidence(root)


def _key(operation):
    return 'reviews' if operation == 'source.review-submit' else 'citations'


def _item_ids(operation, rows):
    if operation == 'source.review-submit':
        return [row['recordId'] for row in rows]
    return [sha256_json({**{k: row[k] for k in ('start', 'end', 'text', 'kind')},
                         'targetIds': sorted(set(row['targetIds']))}) for row in rows]


def submission_budget(max_bytes=None):
    maximum = query_policy()['maxBytes']['detail']
    budget = maximum if max_bytes is None else max_bytes
    if type(budget) is not int or not 1 <= budget <= maximum:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid evidence submission byte budget')
    return budget


def bind_submission(root, snapshot, result, *, max_bytes=None):
    budget = submission_budget(max_bytes)
    operation, arguments = result['operation'], result['arguments']
    batches = []
    if operation != 'project.workflow.continuity-complete':
        key = _key(operation)
        rows = arguments[key]
        ids = _item_ids(operation, rows)
        if len(ids) != len(set(ids)):
            raise StructureError('SOURCE_EVIDENCE_DUPLICATE', 'duplicate evidence across the selected request')
        base = {k: v for k, v in arguments.items() if k != key}
        for row in rows:
            candidate = batches[-1] + [row] if batches else [row]
            index = max(0, len(batches) - 1)
            child_id = f"{arguments['requestId']}:evidence:{index}:{sha256_json(candidate)[:16]}"
            if batches and _size({**base, key: candidate, 'requestId': child_id}) <= budget:
                batches[-1].append(row)
            else:
                batches.append([row])
    value = {'snapshot': snapshot, 'operation': operation, 'arguments': arguments,
             'requestId': arguments.get('requestId'), 'maxBytes': budget,
             'batches': [], 'pendingRecordIds': result.get('pendingRecordIds', []),
             'selectionComplete': result.get('selectionComplete', True)}
    for index, rows in enumerate(batches):
        request_id = f"{arguments['requestId']}:evidence:{index}:{sha256_json(rows)[:16]}"
        packed = {**arguments, _key(operation): rows, 'requestId': request_id}
        value['batches'].append({'arguments': packed, 'itemIds': _item_ids(operation, rows),
                                 'oversizedItem': _size(packed) > budget})
    value['bindingHash'] = sha256_json(value)
    reference = put(root, 'evidence-submission', value)
    pending = value['pendingRecordIds']
    return {'ok': True, 'code': 'EVIDENCE_PACKED', 'operation': operation, 'submissionRef': reference,
            'submitOperation': 'evidence.confirm-continuity' if snapshot['kind'] == 'continuity' else 'evidence.submit',
            'submitted': False, 'approved': False, 'batchCount': len(batches),
            'itemCount': sum(len(batch) for batch in batches),
            'pendingRecordCount': len(pending), 'pendingRecordIdsRef': put(root, 'evidence-pending', pending),
            'selectionComplete': value['selectionComplete'],
            'oversizedItemsByReference': sum(row['oversizedItem'] for row in value['batches']),
            'transactionScope': 'each batch is atomic; completed batches survive later failure'}


def _validate(value):
    if not isinstance(value, dict) or value.get('bindingHash') != sha256_json({k: v for k, v in value.items() if k != 'bindingHash'}):
        raise StructureError('EVIDENCE_BINDING_INVALID', 'stored evidence handoff changed')
    return value


def _resolve(root, reference=None, request_id=None):
    from .structure_store import load_structure
    project_id = load_structure(root)['projectId']
    if reference is not None:
        value = _validate(get(root, reference, kind='evidence-submission'))
        if value['snapshot']['projectId'] != project_id:
            raise StructureError('EVIDENCE_BINDING_INVALID', 'evidence belongs to another project identity')
        if request_id is not None and value['requestId'] != request_id:
            raise StructureError('SOURCE_REQUEST_REUSED', 'requestId differs from the bound handoff')
        return value
    if not isinstance(request_id, str) or not request_id.strip():
        raise StructureError('EVIDENCE_REQUEST_INVALID', 'supply submissionRef or a previously started requestId')
    found = []
    for operation in ('source.review-submit', 'source.evidence-register'):
        _, ledger = _ledger(root, operation)
        value = ledger.get('requests', {}).get(_identity(request_id))
        if value is not None:
            value = _validate(value)
            if value['snapshot']['projectId'] != project_id:
                raise StructureError('EVIDENCE_BINDING_INVALID', 'receipt belongs to another project identity')
            found.append(value)
    if len(found) != 1:
        raise StructureError('EVIDENCE_REQUEST_NOT_FOUND', 'no unique started evidence request with this ID')
    return found[0]


def _assert_current(root, value):
    from .evidence_pack import _prepare_snapshot
    snapshot = value['snapshot']
    arguments = {'kind': snapshot['kind']}
    if snapshot['kind'] == 'review':
        arguments['recordIds'] = snapshot['recordIds']
        if 'selection' in snapshot:
            arguments['selection'] = snapshot['selection']
        if snapshot.get('reviewContext'):
            arguments['reviewContext'] = True
    elif snapshot['kind'] == 'continuity':
        arguments.update(chapterId=snapshot['chapterId'], paths=[row['path'] for row in snapshot['documents']])
    else:
        arguments['chapterId'] = snapshot['chapterId']
    if _prepare_snapshot(root, **arguments)[0] != snapshot:
        raise StructureError('EVIDENCE_SNAPSHOT_STALE', 'unsubmitted evidence no longer matches the selected inputs')


def _completed(root, value):
    from .source_review import fingerprint
    _, ledger = _ledger(root, value['operation'])
    kind = value['operation']
    receipts = ledger['receipts'] if kind == 'source.review-submit' else ledger['batches']
    result = {}
    for index, batch in enumerate(value['batches']):
        arguments = batch['arguments']
        prefix = 'SR-' if kind == 'source.review-submit' else 'PE-'
        rid = prefix + fingerprint({'projectId': value['snapshot']['projectId'], 'requestId': arguments['requestId']}).upper()
        prior = receipts.get(rid)
        if prior is None:
            continue
        if kind == 'source.review-submit':
            expected = fingerprint({'actor': 'app', 'reviews': arguments['reviews']})
        else:
            expected = fingerprint({**{key: arguments[key] for key in ('chapterId', 'fileHash', 'sourceHash', 'citations')}, 'actor': 'app'})
        if prior.get('requestHash') != expected:
            raise StructureError('SOURCE_REQUEST_REUSED', 'child receipt belongs to different evidence')
        result[index] = rid
    return result


def _progress(root, value, done, budget, error=None):
    result = {'ok': error is None, 'code': 'EVIDENCE_SUBMISSION_INCOMPLETE',
              'requestId': value['requestId'], 'submitted': len(done) == len(value['batches']),
              'approved': False, 'completedBatches': len(done), 'totalBatches': len(value['batches']),
              'recordedCount': sum(len(value['batches'][i]['itemIds']) for i in done),
              'pendingCount': sum(len(batch['itemIds']) for i, batch in enumerate(value['batches']) if i not in done),
              'notSelectedCount': len(value['pendingRecordIds']), 'selectionComplete': value['selectionComplete'],
              'semanticReviewComplete': False, 'historicalReceipt': True,
              'detailsOperation': 'evidence.status'}
    if error is not None:
        result['errorRef'] = put(root, 'evidence-error', {
            'code': getattr(error, 'code', 'EVIDENCE_SUBMISSION_FAILED'), 'message': str(error),
            'details': getattr(error, 'details', {})})
    if _size(result) > budget:
        raise StructureError('EVIDENCE_BUDGET_EXCEEDED', 'progress metadata exceeds the byte budget')
    return result


def _status(root, value, *, error=None, max_bytes=None, cursor=None, _done=None):
    from .text_pages import text_page
    import json
    done = _completed(root, value) if _done is None else _done
    rows = [{'itemId': identity, 'batchIndex': index, 'status': 'recorded' if index in done else 'pending',
             'receiptId': done.get(index)} for index, batch in enumerate(value['batches']) for identity in batch['itemIds']]
    rows += [{'itemId': rid, 'status': 'not_selected', 'receiptId': None} for rid in value['pendingRecordIds']]
    budget = submission_budget(max_bytes)
    completed = len(done) == len(value['batches'])
    envelope = {'ok': error is None, 'code': 'EVIDENCE_SUBMITTED' if completed else 'EVIDENCE_SUBMISSION_INCOMPLETE',
                'requestId': value['requestId'], 'submitted': completed, 'approved': False,
                'completedBatches': len(done), 'totalBatches': len(value['batches']),
                'recordedCount': sum(len(value['batches'][i]['itemIds']) for i in done),
                'pendingCount': sum(len(batch['itemIds']) for i, batch in enumerate(value['batches']) if i not in done),
                'notSelectedCount': len(value['pendingRecordIds']), 'selectionComplete': value['selectionComplete'],
                'semanticReviewComplete': False, 'historicalReceipt': True, 'transactionScope': 'per-batch atomic'}
    if error is not None:
        envelope['error'] = {'code': getattr(error, 'code', 'EVIDENCE_SUBMISSION_FAILED'), 'message': str(error)}
    try:
        return text_page(envelope, [{'id': row['itemId'], **row, 'text': json.dumps(row, ensure_ascii=False)} for row in rows],
                         budget=budget, limit=query_policy()['maxLimit'], cursor=cursor,
                         binding=sha256_json([value['bindingHash'], budget]), source=sha256_json(done), key='items')
    except StructureError as exc:
        if error is None or exc.code != 'EVIDENCE_BUDGET_EXCEEDED':
            raise
        return _progress(root, value, done, budget, error)


def submit_evidence(root: Path, *, submissionRef=None, requestId=None, maxBytes=None):
    root = root.expanduser().resolve()
    budget = submission_budget(maxBytes)
    with project_lock_reentrant(root):
        value = _resolve(root, submissionRef, requestId)
        if value['operation'] not in {'source.review-submit', 'source.evidence-register'}:
            raise StructureError('EVIDENCE_APPROVAL_REQUIRED', 'continuity requires its separate explicit author confirmation')
        from .source_review import fingerprint
        prefix = 'SR-' if value['operation'] == 'source.review-submit' else 'PE-'
        completed = {index: prefix + fingerprint({'projectId': value['snapshot']['projectId'],
                     'requestId': batch['arguments']['requestId']}).upper()
                     for index, batch in enumerate(value['batches'])}
        _status(root, value, max_bytes=budget)
        _status(root, value, max_bytes=budget, _done=completed)
        _progress(root, value, completed, budget,
                  StructureError('EVIDENCE_SUBMISSION_FAILED', 'submission may stop at a later batch'))
        path, ledger = _ledger(root, value['operation'])
        key = _identity(value['requestId'])
        prior = ledger.get('requests', {}).get(key)
        if prior is not None and _validate(prior) != value:
            raise StructureError('SOURCE_REQUEST_REUSED', 'requestId identifies another frozen evidence request')
        if prior is None:
            other_operation = 'source.evidence-register' if value['operation'] == 'source.review-submit' else 'source.review-submit'
            if key in _ledger(root, other_operation)[1].get('requests', {}):
                raise StructureError('SOURCE_REQUEST_REUSED', 'requestId already identifies a different evidence kind')
            guards = capture_guards(root, ['.gnovel/structure.json'])
            _assert_current(root, value)
            ledger.setdefault('requests', {})[key] = deepcopy(value)
            apply_write_set(root, {path: json_bytes(ledger)}, operation='evidence_request', guards=guards)
        done = _completed(root, value)
        for index, batch in enumerate(value['batches']):
            if index in done:
                continue
            try:
                guards = capture_guards(root, ['.gnovel/structure.json'] +
                    ([value['snapshot']['path']] if value['snapshot']['kind'] == 'prose' else []))
                _assert_current(root, value)
                if value['operation'] == 'source.review-submit':
                    from .source_review import submit_review
                    submit_review(root, **batch['arguments'], _input_guards=guards)
                else:
                    from .source_evidence import register_evidence
                    register_evidence(root, **batch['arguments'], _input_guards=guards)
            except (ValueError, OSError, RuntimeError) as exc:
                return _status(root, value, error=exc, max_bytes=maxBytes)
        return _status(root, value, max_bytes=maxBytes)


def evidence_status(root: Path, *, requestId: str, maxBytes=None, cursor=None):
    from .consistency import require_no_pending
    root = root.expanduser().resolve()
    require_no_pending(root)
    return _status(root, _resolve(root, request_id=requestId), max_bytes=maxBytes, cursor=cursor)


def confirm_continuity(root: Path, *, submissionRef: str, approve: bool = False):
    from .continuity import complete_continuity
    value = _resolve(root, submissionRef)
    if value['operation'] != 'project.workflow.continuity-complete':
        raise StructureError('EVIDENCE_KIND_INVALID', 'select a bound continuity confirmation')
    args = value['arguments']
    return complete_continuity(root, args['chapterId'], batch_hash=args['batchHash'],
                               confirmation=args['confirmation'], approved=approve)
