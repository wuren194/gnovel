"""Caller-held interval receipts. Never confer read permission or review status."""
from copy import deepcopy

from .artifacts import sha256_json
from .structure_store import StructureError

FIELDS = ('root', 'path', 'fileSha256', 'startLine', 'endLine', 'contentSha256')


def prior_entries(root, receipt):
    if receipt is None:
        return []
    if isinstance(receipt, str):
        if receipt.startswith('gn:'):
            from .transport_cache import get
            receipt = get(root, receipt, kind='receipt')
        else:
            from .reading_handoff import decode_receipt
            receipt = decode_receipt(receipt)
    if (not isinstance(receipt, dict) or set(receipt) - {'projectRoot', 'chunks', 'receiptHash', 'version'}
            or not {'projectRoot', 'chunks', 'receiptHash'} <= set(receipt)
            or receipt.get('version', 1) not in (1, 2) or type(receipt.get('version', 1)) is not int
            or receipt['projectRoot'] != str(root)
            or receipt['receiptHash'] != sha256_json({k: v for k, v in receipt.items() if k != 'receiptHash'})
            or not isinstance(receipt['chunks'], list)):
        raise StructureError('READ_RECEIPT_INVALID', 'invalid transport receipt for this project')
    for row in receipt['chunks']:
        fields = set(FIELDS) | {'ranges', 'totalCharacters'}
        if receipt.get('version') == 2 and isinstance(row, dict) and 'fileStart' in row:
            fields.add('fileStart')
        if (not isinstance(row, dict) or set(row) != fields
                or any(not isinstance(row[k], str) for k in ('root', 'path', 'fileSha256', 'contentSha256'))
                or row['root'] not in {'project', 'toolbox', 'source', 'provider'}
                or any(type(row[k]) is not int for k in ('startLine', 'endLine'))
                or not 1 <= row['startLine'] <= row['endLine']
                or type(row['totalCharacters']) is not int or row['totalCharacters'] < 0
                or 'fileStart' in row and (type(row['fileStart']) is not int or row['fileStart'] < 0)
                or not isinstance(row['ranges'], list)
                or any(not isinstance(r, list) or len(r) != 2 or any(type(n) is not int for n in r)
                       or not 0 <= r[0] <= r[1] <= row['totalCharacters'] for r in row['ranges'])):
            raise StructureError('READ_RECEIPT_INVALID', 'invalid delivered chunk ranges')
    return deepcopy(receipt['chunks'])


def identity(row):
    return tuple(row[k] for k in FIELDS)


def file_identity(row):
    return (row['root'], row['path'], row['fileSha256'], row['contentSha256'] if row['root'] == 'source' else None)


def merged(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def missing_ranges(base, length, held):
    position, gaps = base, []
    for start, end in merged(held):
        if end <= position or start >= base + length:
            continue
        if start > position:
            gaps.append([position - base, min(start, base + length) - base])
        position = max(position, end)
    if position < base + length:
        gaps.append([position - base, length])
    if length == 0:
        gaps = [] if any(start <= base <= end for start, end in held) else [[0, 0]]
    return gaps


def delivered_ranges(chunks, ranges, consumed):
    from .text_pages import text_schedule
    for index, left, right in text_schedule(chunks, ranges):
        if consumed <= 0:
            break
        delivered = min(consumed, max(1, right - left))
        yield index, [left, min(right, left + delivered)]
        consumed -= delivered


def _normalize(prior, raw_files):
    from .write_sets import digest
    for row in prior:
        if 'fileStart' in row or row['root'] == 'source':
            continue
        raw = raw_files.get((row['root'], row['path']))
        if raw is None or raw[0]['sha256'] != row['fileSha256']:
            continue
        lines = raw[1]
        if row['endLine'] > len(lines):
            continue
        text = ''.join(lines[row['startLine'] - 1:row['endLine']])
        if len(text) == row['totalCharacters'] and digest(text.encode()) == row['contentSha256']:
            row['fileStart'] = sum(len(line) for line in lines[:row['startLine'] - 1])


def omit_unchanged(chunks, prior, raw_files=None):
    _normalize(prior, raw_files or {})
    for row in chunks:
        base, length = row['fileStart'], len(row['text'])
        held = []
        for old in prior:
            if file_identity(old) != file_identity(row):
                continue
            if 'fileStart' in old:
                held.extend([[old['fileStart'] + start, old['fileStart'] + end] for start, end in old['ranges']])
            elif identity(old) == identity(row) and old['totalCharacters'] == length:
                held.extend([[base + start, base + end] for start, end in old['ranges']])
        gaps = missing_ranges(base, length, held)
        row['_deliveryRanges'] = gaps
        if not gaps:
            row['omittedAsUnchanged'] = True


def receipt_response(root, prior, chunks, ranges=None):

    def preview(result):
        return {**result, 'sessionReadReceipt': 'gn:receipt:' + '0' * 32}

    def complete(result, consumed):
        known = {}

        def add(row, delivered):
            key = (*identity(row), row.get('fileStart'))
            old = known.get(key)
            payload = {**{k: row[k] for k in FIELDS}, 'totalCharacters': row['totalCharacters'],
                       **({'fileStart': row['fileStart']} if 'fileStart' in row else {})}
            payload['ranges'] = merged((old['ranges'] if old else []) + delivered)
            known[key] = payload

        for old in prior:
            add(old, old['ranges'])
        for index, span in delivered_ranges(chunks, ranges, consumed):
            chunk = chunks[index]
            add({**chunk, 'totalCharacters': len(chunk['text'])}, [span])
        receipt = {'version': 2, 'projectRoot': str(root),
                   'chunks': sorted(known.values(), key=lambda row: (*identity(row), row.get('fileStart', -1)))}
        receipt['receiptHash'] = sha256_json(receipt)
        from .transport_cache import put
        return {**result, 'sessionReadReceipt': put(root, 'receipt', receipt)}

    preview.preview = preview
    preview.complete = complete
    return preview
