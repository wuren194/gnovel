"""Bounded read API over the canonical content selection."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .project import query_policy, source_model_policy
from .query import _cursor_decode, _page, _size, query_bounds
from .source_selection import select_project_content
from .source_model import SourceIndex, snapshot_hash
from .structure_store import StructureError, canonical_json, load_structure


def query_source(root: Path, *, selection: dict | None = None, presetId: str | None = None, view: str = 'overview',
                 limit: int | None = None, maxBytes: int | None = None, cursor: str | None = None) -> dict:
    if view not in {'overview', 'list', 'detail'}:
        raise StructureError('QUERY_VIEW_INVALID', 'source view must be overview, list or detail')
    policy = query_policy()
    limit, budget = query_bounds(limit=limit, maxBytes=maxBytes, view=view)
    from .source_presets import resolve_preset
    selection, preset = resolve_preset(root, selection=selection, presetId=presetId)
    result = select_project_content(root, selection)
    snapshot = result['snapshot']
    binding = hashlib.sha256(canonical_json({'selection': result['selection'], 'presetHash': preset['presetHash'] if preset else None,
                                            'view': view, 'limit': limit, 'maxBytes': budget}).encode()).hexdigest()
    source = hashlib.sha256(canonical_json({'snapshot': snapshot, 'policy': policy, 'model': source_model_policy()}).encode()).hexdigest()
    offset = 0
    if cursor is not None:
        saved = _cursor_decode(cursor)
        if saved.get('source') != source:
            raise StructureError('CURSOR_STALE', 'source changed after this query page')
        if saved.get('binding') != binding:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'cursor belongs to different filters or budgets')
        offset = saved['offset']
    if view == 'overview':
        by_owner = {}
        for record in result['records']:
            counts = by_owner.setdefault(record['ownerId'], {'values': 0, 'fragments': 0})
            counts['values' if record['kind'] == 'value' else 'fragments'] += 1
        items = [{**owner, **by_owner.get(owner['id'], {'values': 0, 'fragments': 0})}
                 for owner in result['owners']]
    elif view == 'list':
        items = [{k: v for k, v in record.items() if k != 'content'} for record in result['records']]
    else:
        items = result['records']
    envelope = {'ok': True, 'code': 'SOURCE_QUERY', 'snapshot': snapshot, 'view': view,
                'selection': result['selection'], 'coverage': result['coverage'], 'warnings': result['warnings']}
    if preset is not None:
        envelope['preset'] = preset
    return _page(envelope, items, limit=limit, budget=budget, offset=offset, binding=binding, source=source)


def read_fragment(root: Path, *, fragmentId: str, offset: int = 0, maxBytes: int | None = None,
                  sourceHash: str | None = None) -> dict:
    from .consistency import require_no_pending
    require_no_pending(root)
    source = load_structure(root)
    require_no_pending(root)
    fingerprint = snapshot_hash(source)
    if sourceHash is not None and sourceHash != fingerprint:
        raise StructureError('CURSOR_STALE', 'source changed before this fragment slice')
    if type(offset) is not int or offset < 0:
        raise StructureError('SOURCE_RANGE_INVALID', 'offset must be a nonnegative character offset')
    if offset and sourceHash is None:
        raise StructureError('SOURCE_SNAPSHOT_REQUIRED', 'continuation reads must bind sourceHash')
    fragment = SourceIndex.build(source).fragments.get(fragmentId)
    if fragment is None:
        raise StructureError('SOURCE_RECORD_NOT_FOUND', 'fragment does not exist')
    content = fragment['text']
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if offset > len(content):
        raise StructureError('SOURCE_RANGE_INVALID', 'offset exceeds the fragment length')
    maximum = query_policy()['maxBytes']['detail']
    budget = maximum if maxBytes is None else maxBytes
    if type(budget) is not int or not 1 <= budget <= maximum:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid fragment byte budget')
    def response(end):
        return {'ok': True, 'code': 'SOURCE_FRAGMENT_SLICE', 'fragmentId': fragmentId,
                'sourceHash': fingerprint, 'contentHash': content_hash,
                'offset': offset, 'endOffset': end, 'totalCharacters': len(content),
                'hasMore': end < len(content), 'text': content[offset:end]}
    if _size(response(offset)) > budget:
        raise StructureError('FIELD_TOO_LARGE', 'fragment metadata exceeds byte budget')
    lower, upper = offset, min(len(content), offset + budget)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if _size(response(middle)) <= budget:
            lower = middle
        else:
            upper = middle - 1
    if lower == offset and offset < len(content):
        raise StructureError('FIELD_TOO_LARGE', 'budget cannot fit a character plus metadata')
    return response(lower)
