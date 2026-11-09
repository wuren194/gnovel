"""Byte-bounded UTF-8 text pages, retaining Python character offsets."""
from __future__ import annotations

import json
from .artifacts import sha256_json
from .query import _cursor, _cursor_decode, _size
from .structure_store import StructureError


def text_schedule(materials, ranges=None):
    if ranges is not None and len(ranges) != len(materials):
        raise StructureError('READ_BATCH_RANGE_INVALID', 'sparse ranges must match the selected materials')
    schedule = []
    for index, row in enumerate(materials):
        selected = [[0, len(row['text'])]] if ranges is None else ranges[index]
        previous = -1
        for start, end in selected:
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start <= end <= len(row['text']) or start < previous):
                raise StructureError('READ_BATCH_RANGE_INVALID', 'invalid sparse delivery range')
            schedule.append((index, start, end))
            previous = end
    return schedule


def text_page(envelope: dict, materials: list[dict], *, budget: int, limit: int,
              cursor: str | None, binding: str, source: str, key: str = 'materials', transform=None,
              ranges=None) -> dict:
    offset = 0
    if cursor:
        saved = _cursor_decode(cursor)
        if saved.get('source') != source:
            raise StructureError('CURSOR_STALE', 'reading snapshot changed; prepare again')
        if saved.get('binding') != binding:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'reading selection or budget changed')
        offset = saved['offset']
    lengths = [len(row['text']) for row in materials]
    schedule = text_schedule(materials, ranges)
    widths = [max(1, end - start) for _, start, end in schedule]
    total = sum(widths)
    if offset > total:
        raise StructureError('CURSOR_INVALID', 'offset exceeds the selected text')
    selected = []
    position = offset

    def response(end, rows, *, final=False):
        consumed, segment = end, 0
        while segment < len(widths) and consumed >= widths[segment]:
            consumed -= widths[segment]
            segment += 1
        index, next_start = (schedule[segment][0], schedule[segment][1] + consumed) if segment < len(schedule) else (len(materials), None)
        result = {**envelope, key: rows, 'returned': len(rows), 'totalMatching': len(materials), 'hasMore': end < total,
                'nextCursor': _cursor(binding, source, end) if end < total else None,
                'coverage': {**envelope.get('coverage', {}), 'totalMaterials': len(materials), 'remainingMaterials': len(materials) - index,
                             'nextMaterialIndex': index if index < len(materials) else None,
                             'nextStart': next_start,
                             'deliveryComplete': end == total, 'semanticReviewComplete': False}}
        if transform:
            if final and hasattr(transform, 'complete'):
                return transform.complete(result, end)
            return transform(result) if final else transform.preview(result)
        return result

    for segment, (index, left, right) in enumerate(schedule):
        row = materials[index]
        width = widths[segment]
        if position >= width:
            position -= width
            continue
        start = left + position
        metadata = {k: v for k, v in row.items() if k != 'text'}

        def part(end, *, include_text=True):
            text = row['text'][start:end]
            return {**metadata, 'start': start, 'end': end, 'totalCharacters': lengths[index],
                    'text': text if include_text else '', 'chunkHash': '0' * 64}

        empty_selected = [{**r, 'text': ''} for r in selected]
        selected_bytes = sum(len(json.dumps(r['text'], ensure_ascii=False).encode()) - 2 for r in selected)
        low, high, best = start, right, None
        while low <= high:
            end = (low + high) // 2
            consumed = max(1, end - start)
            candidate = response(offset + consumed, empty_selected + [part(end, include_text=False)])
            text_bytes = len(json.dumps(row['text'][start:end], ensure_ascii=False).encode()) - 2
            if _size(candidate) + selected_bytes + text_bytes <= budget:
                best = end
                low = end + 1
            else:
                high = end - 1
        if best is None or (best == start and right > start):
            if selected:
                break
            raise StructureError('EVIDENCE_BUDGET_EXCEEDED', 'reading metadata exceeds the byte budget', {'maxBytes': budget})
        selected.append(part(best))
        offset += max(1, best - start)
        position = 0
        if best < right or len(selected) >= limit:
            break
    for item in selected:
        item['chunkHash'] = sha256_json({k: item[k] for k in ('text', 'start', 'end')})
    result = response(offset, selected, final=True)
    if _size(result) > budget:
        raise StructureError('EVIDENCE_BUDGET_EXCEEDED', 'final transport metadata exceeds the byte budget')
    return result
