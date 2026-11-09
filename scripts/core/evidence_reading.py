"""Explicitly held material ranges reduce delivery, never semantic obligations."""
from copy import deepcopy
import base64
import json

from .artifacts import sha256_json
from .reading_receipts import merged, missing_ranges
from .query import _cursor_decode
from .structure_store import StructureError
from .transport_cache import get, put


def _prior(root, reference):
    if reference is None:
        return None
    if not isinstance(reference, str) or not reference:
        raise StructureError('EVIDENCE_READ_RECEIPT_INVALID', 'use the original materialReadReceipt reference')
    receipt = get(root, reference, kind='evidence-read-receipt')
    if (not isinstance(receipt, dict) or set(receipt) != {'version', 'scope', 'entries', 'page'}
            or type(receipt['version']) is not int or receipt['version'] != 1
            or not isinstance(receipt['scope'], str) or not isinstance(receipt['entries'], list)
            or not isinstance(receipt['page'], dict) or set(receipt['page']) != {'source', 'binding', 'offset'}
            or any(not isinstance(receipt['page'][key], str) for key in ('source', 'binding'))
            or type(receipt['page']['offset']) is not int or receipt['page']['offset'] < 0):
        raise StructureError('EVIDENCE_READ_RECEIPT_INVALID', 'invalid material reading receipt')
    for row in receipt['entries']:
        if (not isinstance(row, dict) or set(row) != {'id', 'version', 'totalCharacters', 'ranges'}
                or any(not isinstance(row[key], str) for key in ('id', 'version'))
                or type(row['totalCharacters']) is not int or row['totalCharacters'] < 0
                or not isinstance(row['ranges'], list)
                or any(not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                       or not 0 <= span[0] <= span[1] <= row['totalCharacters'] for span in row['ranges'])):
            raise StructureError('EVIDENCE_READ_RECEIPT_INVALID', 'invalid held character ranges')
    return receipt


def _with_receipt(result, reference):
    result = {**result, 'materialReadReceipt': reference}
    if result['nextCursor']:
        cursor = _cursor_decode(result['nextCursor'])
        cursor['materialReadReceipt'] = reference
        result['nextCursor'] = base64.urlsafe_b64encode(json.dumps(cursor, separators=(',', ':')).encode()).decode()
    return result


def material_delivery(root, snapshot, materials, dependencies, prior_reference, *, source, binding, cursor):
    selection = {key: value for key, value in snapshot.get('selection', {}).items()
                 if key not in {'ownerIds', 'ownerTypes'}}
    scope = sha256_json({'projectId': snapshot['projectId'], 'sourceHash': snapshot['sourceHash'],
                        'selection': selection, 'runtime': dependencies['toolbox'],
                        'authorization': {key: row for key, row in dependencies['files'].items()
                                          if row['kind'] == 'authorization'}})
    prior = _prior(root, prior_reference)
    entries = prior['entries'] if prior is not None and prior['scope'] == scope else []
    previous = None
    if cursor:
        position = _cursor_decode(cursor)
        previous = _prior(root, position.get('materialReadReceipt'))
        if (previous is None or previous['scope'] != scope or previous['page'] != {
                'source': source, 'binding': binding, 'offset': position['offset']}):
            raise StructureError('EVIDENCE_READ_CURSOR_INVALID', 'cursor must retain its exact preceding delivery receipt')
    held = {}
    for row in entries:
        key = (row['id'], row['version'])
        if key in held and held[key]['totalCharacters'] != row['totalCharacters']:
            raise StructureError('EVIDENCE_READ_RECEIPT_INVALID', 'conflicting material lengths')
        held[key] = {**row, 'ranges': merged(held.get(key, {}).get('ranges', []) + row['ranges'])}
    current, ranges, reused = [], [], []
    for material in materials:
        identity = material.get('id') or 'record:' + material['recordId']
        row = {'id': identity, 'version': sha256_json(material), 'totalCharacters': len(material['text'])}
        old = held.get((identity, row['version']))
        if old is not None and old['totalCharacters'] != row['totalCharacters']:
            raise StructureError('EVIDENCE_READ_RECEIPT_INVALID', 'material length differs from its receipt')
        # Current status and empty decision inputs are always delivered, even
        # when all of the source text and shared dependencies are still held.
        allowed = old['ranges'] if old is not None and material.get('materialKind') != 'review-task' else []
        gaps = missing_ranges(0, row['totalCharacters'], allowed)
        if allowed:
            reused.append({**row, 'ranges': allowed})
        current.append(row); ranges.append(gaps)
    summary = {'materials': len(reused), 'characters': sum(
        row['totalCharacters'] - sum(end - start for start, end in gaps) for row, gaps in zip(current, ranges)),
        'scopeChanged': prior is not None and prior['scope'] != scope}
    if reused:
        summary['detailsRef'] = put(root, 'evidence-reading', {'reused': reused, 'semanticReviewComplete': False})
    by_id = {row['id']: row for row in current}
    if len(by_id) != len(current):
        raise StructureError('EVIDENCE_READ_RECEIPT_INVALID', 'material identities must be distinct')

    def preview(result):
        return _with_receipt(result, 'gn:evidence-read-receipt:' + '0' * 32)

    def complete(result, consumed):
        known = deepcopy(held)
        for row in previous['entries'] if previous is not None else []:
            key = (row['id'], row['version'])
            known[key] = {**row, 'ranges': merged(known.get(key, {}).get('ranges', []) + row['ranges'])}
        for delivered in result['materials']:
            row = by_id[delivered.get('id') or 'record:' + delivered['recordId']]
            key = (row['id'], row['version'])
            known[key] = {**row, 'ranges': merged(known.get(key, {}).get('ranges', []) + [[delivered['start'], delivered['end']]])}
        receipt = {'version': 1, 'scope': scope, 'page': {'source': source, 'binding': binding, 'offset': consumed},
                   'entries': [known[key] for key in sorted(known)]}
        return _with_receipt(result, put(root, 'evidence-read-receipt', receipt))

    preview.preview = preview
    preview.complete = complete
    return ranges, preview, summary
