"""Lossless document segments and explicitly selected maintenance checklist drafts."""
from __future__ import annotations

from pathlib import Path
import re

from .artifacts import sha256_json
from .source_maintenance_inputs import observe_input
from .structure_store import StructureError

HEADING = re.compile(r'^ {0,3}#{1,6}(?:[ \t]+|$)')
LIST_ITEM = re.compile(r'^[ \t]*(?:(?:[-+*]|[0-9]+[.)])[ \t]+|(?:[0-9]+|[一二三四五六七八九十]+)[、）][ \t]*)')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


def decode_document(payload):
    try:
        return payload.decode('utf-8')
    except UnicodeError as exc:
        raise StructureError('MAINTENANCE_INPUT_INVALID', 'source document must be UTF-8') from exc


def split_document(text, origin):
    """Partition original characters, not inferred requirements; fenced content stays together."""
    rows = []
    start = offset = 0
    start_line = 1
    kind = 'paragraph'
    fence = None
    after_blank = after_fence = False
    lines = text.splitlines(keepends=True)

    def append(end, end_line):
        rows.append({'id': 'segment-' + sha256_json({'source': origin, 'start': start, 'end': end})[:32],
                     'kind': kind, 'fileStart': start, 'fileEnd': end,
                     'startLine': start_line, 'endLine': end_line})

    for index, line in enumerate(lines):
        body = line.rstrip('\r\n')
        if fence is not None:
            marker, width = fence
            if re.fullmatch(r' {0,3}' + re.escape(marker) + '{' + str(width) + r',}[ \t]*', body):
                fence = None
                after_fence = True
            offset += len(line)
            continue
        opening = FENCE.match(body)
        current_kind = ('code' if opening else 'heading' if HEADING.match(body)
                        else 'list-item' if LIST_ITEM.match(body) else 'paragraph')
        if body.strip():
            boundary = current_kind != 'paragraph' or after_blank or after_fence or kind == 'heading'
            if offset > start and boundary:
                append(offset, index)
                start, start_line = offset, index + 1
                kind = current_kind
            elif offset == start:
                kind = current_kind
            after_blank = after_fence = False
        else:
            after_blank = True
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker))
        offset += len(line)
    if offset > start:
        append(offset, len(lines))
    return rows


def prepare_document(root, payload, guard):
    from .transport_cache import put
    text = decode_document(payload)
    origin = {'path': guard['path'], 'sha256': guard['sha256']}
    segments = split_document(text, origin)
    reference = put(root, 'maintenance-document', {'source': guard, 'segments': segments})
    return reference, [{**row, 'text': text[row['fileStart']:row['fileEnd']]} for row in segments]


def _load_document(root, reference):
    from .transport_cache import get
    saved = get(root, reference, kind='maintenance-document')
    payload, guard = observe_input(root, saved['source']['path'])
    if guard != saved['source']:
        raise StructureError('MAINTENANCE_SOURCE_STALE', 'document bytes or file identity changed; prepare again')
    text = decode_document(payload)
    origin = {'path': guard['path'], 'sha256': guard['sha256']}
    segments = split_document(text, origin)
    if segments != saved['segments']:
        raise StructureError('MAINTENANCE_SEGMENTS_STALE', 'document segmentation changed; prepare again')
    return text, origin, segments, guard


def _checklist_items(text, origin, segments, selections, known_ids):
    positions = {row['id']: index for index, row in enumerate(segments)}
    items, identities = [], set()
    for index, selection in enumerate(selections):
        ids = selection['segmentIds']
        if any(identity not in positions for identity in ids):
            raise StructureError('MAINTENANCE_SEGMENT_UNKNOWN', 'select segments from this document', {'itemIndex': index})
        selected = [positions[identity] for identity in ids]
        if selected != list(range(selected[0], selected[-1] + 1)):
            raise StructureError('MAINTENANCE_SEGMENT_RANGE_INVALID', 'merged segments must be contiguous and ordered',
                                 {'itemIndex': index})
        start, end = segments[selected[0]]['fileStart'], segments[selected[-1]]['fileEnd']
        if 'range' in selection:
            span = selection['range']
            if not 0 <= span['start'] < span['end'] <= end - start:
                raise StructureError('MAINTENANCE_SEGMENT_RANGE_INVALID', 'range is outside the selected original characters',
                                     {'itemIndex': index})
            start, end = start + span['start'], start + span['end']
        quote = text[start:end]
        if not quote.strip():
            raise StructureError('MAINTENANCE_QUOTE_EMPTY', 'a checklist item needs nonblank original text', {'itemIndex': index})
        item_id = selection.get('itemId') or 'item-' + sha256_json({'source': origin, 'start': start, 'end': end})[:32]
        if item_id in identities:
            raise StructureError('MAINTENANCE_ITEM_DUPLICATE', 'duplicate item identity; select distinct ranges or explicit item IDs',
                                 {'itemIndex': index, 'itemId': item_id})
        identities.add(item_id)
        targets = list(dict.fromkeys(selection['recordIds']))
        unknown = [rid for rid in targets if rid not in known_ids and not rid.startswith('$')]
        if unknown:
            raise StructureError('SOURCE_RECORD_NOT_FOUND', 'checklist targets must be current IDs or declared temporary handles',
                                 {'itemIndex': index, 'recordIds': unknown})
        items.append({'itemId': item_id, 'quote': quote, 'recordIds': targets, 'operationIds': []})
    return items


def write_checklist(root: Path, *, documentRef: str, items: list, output: str) -> dict:
    """Create or reuse an identical draft; never interpret, register or approve requirements."""
    from .locks import project_lock_reentrant
    from .operation_schemas import MAINTENANCE_SELECTION
    from .operations import validate_arguments
    from .source_model import SourceIndex
    from .source_review import source_snapshot
    from .write_sets import apply_write_set, guard_observation, inventory, json_bytes, safe_project_path, validate_guards
    validate_arguments({'type': 'array', 'minItems': 1, 'items': MAINTENANCE_SELECTION}, items, 'items')
    root = root.expanduser().resolve()
    with project_lock_reentrant(root):
        source_state, _ = guard_observation(root / '.gnovel/structure.json')
        source = source_snapshot(root)
        text, origin, segments, document_guard = _load_document(root, documentRef)
        selected = _checklist_items(text, origin, segments, items, SourceIndex.build(source).records)
        path = safe_project_path(root, output)
        relative = str(path.relative_to(root))
        if not relative.startswith('工作台/'):
            raise StructureError('MAINTENANCE_OUTPUT_INVALID', 'checklist needs a file under 工作台')
        checklist = {'source': origin, 'items': selected}
        outputs = {relative: json_bytes(checklist)}
        guards = [document_guard, {'path': '.gnovel/structure.json', **source_state}]
        output_state, existing = guard_observation(path)
        reused = existing is not None
        if reused:
            if existing != outputs[relative]:
                raise StructureError('MAINTENANCE_OUTPUT_INVALID', 'existing checklist differs; choose a new output file')
            validate_guards(root, [*guards, {'path': relative, **output_state}], stage='maintenance-checklist-reuse')
        else:
            expected = inventory(root, outputs)
            if any(row['before']['exists'] for row in expected):
                raise StructureError('MAINTENANCE_OUTPUT_INVALID', 'output appeared during assembly; it was not overwritten')
            apply_write_set(root, outputs, expected=expected, guards=guards, operation='maintenance_checklist')
    return {'ok': True, 'code': 'SOURCE_MAINTENANCE_CHECKLIST_REUSED' if reused else 'SOURCE_MAINTENANCE_CHECKLIST_WRITTEN',
            'checklistPath': relative, 'reused': reused,
            'itemCount': len(selected), 'needsMapping': any(not row['recordIds'] for row in selected),
            'registered': False, 'approvalGranted': False,
            'coverage': {'documentRequirementsFullyEnumerated': 'not_verified', 'mappingSemanticsVerified': False,
                         'semanticReviewComplete': False}}
