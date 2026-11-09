"""Version-bound semantic receipts; model conclusions remain model evidence."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .consistency import require_no_pending
from .locks import project_lock_reentrant
from .project import query_policy
from .query import _cursor_decode, _page, query_bounds
from .source_impact import REVIEW_PATH, ReviewInputs, read_review, fragment_review_status
from .source_model import snapshot_hash
from .structure_store import StructureError, canonical_json, load_structure
from .write_sets import apply_write_set, digest, json_bytes, safe_project_path
from .jsonio import read_json

RECEIPTS_PATH = '.gnovel/source-review-receipts.json'
DECISIONS = {'compatible', 'conflict', 'needs_information', 'not_checked'}


def fingerprint(value) -> str:
    return digest(canonical_json(value).encode())


def source_snapshot(root: Path) -> dict:
    require_no_pending(root)
    source = load_structure(root)
    require_no_pending(root)
    return source


def bounded_page(items, *, snapshot, selection, code, limit=None, maxBytes=None, cursor=None, **metadata):
    policy = query_policy()
    limit, budget = query_bounds(limit=limit, maxBytes=maxBytes)
    source = fingerprint({'snapshot': snapshot, 'policy': policy})
    binding = fingerprint({'selection': selection, 'limit': limit, 'budget': budget, 'code': code})
    offset = 0
    if cursor is not None:
        saved = _cursor_decode(cursor)
        if saved.get('source') != source:
            raise StructureError('CURSOR_STALE', 'source or runtime evidence changed')
        if saved.get('binding') != binding:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'runtime cursor belongs to a different query')
        offset = saved['offset']
    return _page({'ok': True, 'code': code, 'snapshot': snapshot, **metadata}, items,
                 limit=limit, budget=budget, offset=offset, binding=binding, source=source)


def query_reviews(root: Path, *, ownerIds=None, recordIds=None, statuses=None, limit=None, maxBytes=None, cursor=None) -> dict:
    source = source_snapshot(root)
    inputs, state = ReviewInputs(source), read_review(root)
    for values in (ownerIds, recordIds, statuses):
        if values is not None and (not isinstance(values, list) or any(not isinstance(v, str) for v in values)):
            raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'review filters must be string lists')
    allowed = DECISIONS | {'unreviewed', 'pending', 'stale', 'resolved'}
    if statuses is not None and set(statuses) - allowed:
        raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'unknown review status')
    if ownerIds is not None and set(ownerIds) - inputs.index.objects.keys():
        raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'unknown owner')
    if recordIds is not None and set(recordIds) - inputs.index.fragments.keys():
        raise StructureError('SOURCE_REVIEW_FILTER_INVALID', 'unknown fragment')
    rows = []
    for rid, fragment in sorted(inputs.index.fragments.items()):
        if recordIds is not None and rid not in recordIds:
            continue
        if fragment.get('status') == 'archived' or (ownerIds is not None and fragment['ownerId'] not in ownerIds):
            continue
        input_hash = inputs.fingerprint(rid)
        prior = state['items'].get(rid)
        status = fragment_review_status(inputs, state, rid, input_hash=input_hash)
        if statuses is not None and status not in statuses:
            continue
        rows.append({'recordId': rid, 'ownerId': fragment['ownerId'], 'status': status,
                     'inputHash': input_hash, 'contentHash': digest(fragment['text'].encode()),
                     'inputIds': sorted(inputs.incoming[rid]),
                     'locator': {'fragmentId': rid, 'sourceHash': snapshot_hash(source), 'offset': 0},
                     'receiptId': prior.get('receiptId') if prior else None})
    require_no_pending(root)
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'reviewHash': fingerprint(state)},
                        selection={'ownerIds': sorted(set(ownerIds)) if ownerIds is not None else None,
                                   'statuses': sorted(set(statuses)) if statuses is not None else None,
                                   'recordIds': sorted(set(recordIds)) if recordIds is not None else None},
                        code='SOURCE_REVIEWS', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={'scope': 'active source fragments', 'proseScanned': False,
                                  'semanticJudgmentsMadeBy': 'app', 'machineProvesSemanticCorrectness': False})


def _receipts(root: Path) -> dict:
    path = safe_project_path(root, RECEIPTS_PATH)
    state = read_json(path) if path.is_file() else {'receipts': {}}
    if not isinstance(state, dict) or not isinstance(state.get('receipts'), dict):
        raise StructureError('SOURCE_RECEIPTS_INVALID', 'semantic receipt history is unreadable')
    for rid, receipt in state['receipts'].items():
        if (not isinstance(receipt, dict) or receipt.get('receiptId') != rid or
                not isinstance(receipt.get('actor'), str) or not isinstance(receipt.get('reviews'), list)
                or not receipt['reviews']):
            raise StructureError('SOURCE_RECEIPTS_INVALID', 'invalid semantic receipt')
        for row in receipt['reviews']:
            if (not isinstance(row, dict) or set(row) != {'recordId', 'inputHash', 'contentHash', 'decision', 'reason', 'evidence'}
                    or any(not isinstance(row[key], str) for key in ('recordId', 'inputHash', 'contentHash', 'decision', 'reason'))
                    or row['decision'] not in DECISIONS or not isinstance(row['evidence'], list)):
                raise StructureError('SOURCE_RECEIPTS_INVALID', 'invalid semantic receipt item')
        if receipt.get('requestHash') != fingerprint({'actor': receipt['actor'], 'reviews': receipt['reviews']}):
            raise StructureError('SOURCE_RECEIPTS_INVALID', 'semantic receipt content fingerprint differs')
    return state


def review_matches_current(inputs: ReviewInputs, review: dict, *, input_hash: str | None = None) -> bool:
    """An exact input match also needs the reviewed text and its quoted evidence."""
    fragment = inputs.index.fragments.get(review['recordId'])
    if fragment is None or fragment.get('status') == 'archived':
        return False
    if (review['inputHash'] != (input_hash if input_hash is not None else inputs.fingerprint(review['recordId']))
            or review['contentHash'] != digest(fragment['text'].encode())):
        return False
    evidence = review['evidence']
    if not isinstance(evidence, list) or (review['decision'] != 'not_checked' and not evidence):
        return False
    for quote in evidence:
        if not isinstance(quote, dict) or set(quote) != {'start', 'end', 'text'}:
            return False
        start, end = quote['start'], quote['end']
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(fragment['text'])
                or fragment['text'][start:end] != quote['text']):
            return False
    return True


def query_receipt(root: Path, *, receiptId: str, limit=None, maxBytes=None, cursor=None) -> dict:
    source = source_snapshot(root)
    if not isinstance(receiptId, str):
        raise StructureError('SOURCE_RECEIPT_NOT_FOUND', 'receiptId must be a string')
    receipt = _receipts(root)['receipts'].get(receiptId)
    if receipt is None:
        raise StructureError('SOURCE_RECEIPT_NOT_FOUND', 'unknown semantic receipt')
    inputs = ReviewInputs(source)
    rows = [{**review, 'inputsStillCurrent': review_matches_current(inputs, review)}
            for review in receipt['reviews']]
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'receiptHash': fingerprint(receipt)},
                        selection={'receiptId': receiptId}, code='SOURCE_REVIEW_RECEIPT',
                        limit=limit, maxBytes=maxBytes, cursor=cursor, actor=receipt['actor'], historical=True)


def review_state_item(source, review, receipt_id, fragment):
    return {'recordId': review['recordId'], 'ownerId': fragment['ownerId'], 'inputHash': review['inputHash'],
            'status': review['decision'], 'receiptId': receipt_id, 'revision': source['revision']}


def review_receipt(source, request_id, reviews, actor):
    return {'receiptId': 'SR-' + fingerprint({'projectId': source['projectId'], 'requestId': request_id}).upper(),
            'requestHash': fingerprint({'actor': actor, 'reviews': reviews}), 'actor': actor,
            'sourceHash': snapshot_hash(source), 'revision': source['revision'], 'reviews': deepcopy(reviews)}


def submit_review(root: Path, *, requestId: str, reviews: list, actor: str = 'app', _input_guards=None) -> dict:
    """Record only the explicitly reviewed fragments at their exact inputs."""
    if not isinstance(requestId, str) or not requestId.strip() or not isinstance(actor, str) or not actor.strip():
        raise StructureError('SOURCE_REVIEW_INVALID', 'requestId and actor must be nonempty strings')
    if not isinstance(reviews, list) or not reviews:
        raise StructureError('SOURCE_REVIEW_INVALID', 'reviews must be a nonempty list')
    with project_lock_reentrant(root):
        source = source_snapshot(root)
        inputs = ReviewInputs(source)
        receipts = _receipts(root)
        receipt_id = 'SR-' + fingerprint({'projectId': source['projectId'], 'requestId': requestId}).upper()
        request_hash = fingerprint({'actor': actor, 'reviews': reviews})
        prior = receipts['receipts'].get(receipt_id)
        if prior is not None:
            if prior.get('requestHash') != request_hash:
                raise StructureError('SOURCE_REQUEST_REUSED', 'review requestId was reused with different content')
            current = all(review_matches_current(inputs, row) for row in prior['reviews'])
            return {'ok': True, 'code': 'SOURCE_REVIEW_ALREADY_RECORDED', 'receiptId': receipt_id,
                    'alreadyRecorded': True, 'inputsStillCurrent': current, 'reviewedCount': len(prior['reviews'])}
        state = deepcopy(read_review(root))
        seen = set()
        required = {'recordId', 'inputHash', 'contentHash', 'decision', 'reason', 'evidence'}
        for review in reviews:
            if not isinstance(review, dict) or set(review) != required:
                raise StructureError('SOURCE_REVIEW_INVALID', 'review fields must match the receipt schema')
            rid = review['recordId']
            if not isinstance(rid, str) or rid not in inputs.index.fragments or rid in seen:
                raise StructureError('SOURCE_REVIEW_INVALID', 'unknown or duplicate fragment')
            seen.add(rid)
            fragment = inputs.index.fragments[rid]
            if fragment.get('status') == 'archived':
                raise StructureError('SOURCE_REVIEW_INVALID', 'archived fragment cannot receive a current review')
            if review['inputHash'] != inputs.fingerprint(rid) or review['contentHash'] != digest(fragment['text'].encode()):
                raise StructureError('SOURCE_REVIEW_STALE', 'fragment or registered inputs changed', {'recordId': rid})
            if not isinstance(review['decision'], str) or review['decision'] not in DECISIONS or not isinstance(review['reason'], str) or not review['reason'].strip():
                raise StructureError('SOURCE_REVIEW_INVALID', 'decision and substantive reason are required')
            evidence = review['evidence']
            if not isinstance(evidence, list) or (review['decision'] != 'not_checked' and not evidence):
                raise StructureError('SOURCE_REVIEW_EVIDENCE_REQUIRED', 'checked content requires exact textual evidence')
            for quote in evidence:
                if not isinstance(quote, dict) or set(quote) != {'start', 'end', 'text'}:
                    raise StructureError('SOURCE_REVIEW_EVIDENCE_INVALID', 'evidence requires character bounds and quote')
                start, end = quote['start'], quote['end']
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(fragment['text']) or fragment['text'][start:end] != quote['text']:
                    raise StructureError('SOURCE_REVIEW_EVIDENCE_INVALID', 'quoted text does not match the current fragment')
            state['items'][rid] = review_state_item(source, review, receipt_id, fragment)
        receipt = review_receipt(source, requestId, reviews, actor)
        receipts['receipts'][receipt_id] = receipt
        state['sourceHash'] = snapshot_hash(source)
        apply_write_set(root, {REVIEW_PATH: json_bytes(state), RECEIPTS_PATH: json_bytes(receipts)},
                        operation='source_review', guards=_input_guards)
        return {'ok': True, 'code': 'SOURCE_REVIEW_RECORDED', 'receiptId': receipt_id,
                'reviewedCount': len(reviews), 'sourceHash': snapshot_hash(source),
                'machineProvesSemanticCorrectness': False, 'proseWrite': False}
