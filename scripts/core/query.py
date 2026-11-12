"""Four read-only dimensions over one structure snapshot, with bounded pagination."""
from __future__ import annotations

import base64
import json
from collections import Counter, defaultdict
from pathlib import Path

from .artifacts import sha256_json
from .entity_registry import resolve_entity
from .project import query_policy
from .settings_schema import settings_schema
from .source_model import owner_types
from .structure_store import StructureError, load_structure

DIMENSIONS = ('logic', 'timeline', 'attributes', 'nodes')
QUERY_PROPERTIES = {
    'projectRoot': {'type': 'string'},
    'view': {'type': 'string', 'enum': ['overview', 'list', 'detail', 'traverse', 'schema']},
    'selector': {'type': 'string'}, 'kind': {'type': 'string', 'enum': sorted(owner_types())},
    'query': {'type': 'string'}, 'status': {'type': 'string'},
    'field': {'type': 'array', 'items': {'type': 'string'}},
    'hasField': {'type': 'string'}, 'fieldEq': {'type': 'object', 'additionalProperties': {'type': 'string'}},
    'unfilled': {'type': 'boolean'}, 'unused': {'type': 'boolean'},
    'chapterId': {'type': 'string'}, 'volumeId': {'type': 'string'}, 'entityId': {'type': 'string'},
    'referencedBy': {'type': 'string'}, 'fromAnchor': {'type': 'string'}, 'toAnchor': {'type': 'string'},
    'unmapped': {'type': 'boolean'}, 'problems': {'type': 'boolean'},
    'direction': {'type': 'string', 'enum': ['in', 'out', 'both']},
    'edgeKind': {'type': 'string'}, 'depth': {'type': 'integer'},
    'limit': {'type': 'integer'}, 'maxBytes': {'type': 'integer'}, 'cursor': {'type': 'string'},
}
QUERY_SCHEMA = {'type': 'object', 'properties': QUERY_PROPERTIES, 'required': [], 'additionalProperties': False}


def typed_edges(structure: dict) -> list[dict]:
    rows = []
    def edge(source, target, kind, origin, **extra):
        if source and target:
            row = {'sourceId': source, 'targetId': target, 'kind': kind, 'origin': origin, **extra}
            row['edgeId'] = 'EDGE-' + sha256_json(row)[:20]
            rows.append(row)
    for rel in structure.get('relations', []):
        edge(rel['fromEntity'], rel['toEntity'], 'relation', rel.get('origin', 'author'), relationKind=rel.get('kind'), relationId=rel.get('relationId'))
    for ref in structure.get('references', []):
        edge(ref['sourceId'], ref['targetId'], 'reference', ref.get('origin', 'registered'), referenceKind=ref.get('kind'))
    for item in [*structure['chapters'], *structure['entities']]:
        is_chapter = 'chapterId' in item
        source = item['chapterId'] if is_chapter else item['entityId']
        keys = ('storyTimeStartId', 'storyTimeEndId') if is_chapter else ('timeStartId', 'timeEndId')
        for key in keys:
            edge(source, item.get(key), 'time-mount', 'structure', field=key)
        if is_chapter:
            from .source_placement import placement_volume_id
            edge(source, placement_volume_id(item.get('placement')), 'chapter-volume', 'structure')
    for link in structure['chapterArcLinks']:
        edge(link['chapterId'], link['arcId'], 'chapter-arc', 'structure', role=link.get('role'))
    return sorted({r['edgeId']: r for r in rows}.values(), key=lambda r: (r['sourceId'], r['targetId'], r['edgeId']))


def links_for(item: dict) -> list[dict]:
    selector = item['id']
    links = [{'dimension': 'nodes', 'view': 'traverse', 'selector': selector, 'edgeKind': 'reference'},
             {'dimension': 'timeline', 'view': 'detail', 'selector': selector}]
    if item['type'] == 'rule':
        links.append({'dimension': 'logic', 'view': 'detail', 'selector': selector})
    if item['type'] in settings_schema()['kinds']:
        links.append({'dimension': 'attributes', 'view': 'detail', 'selector': selector})
    return links


def _records(structure: dict, schema: dict) -> dict:
    from .source_fields import projections
    source_values = projections(structure)
    records = {}
    for row in structure['entities']:
        records[row['entityId']] = {'id': row['entityId'], 'type': row['entityType'], 'name': row['name'],
            'status': row.get('status'), 'aliases': row.get('aliases', []), 'record': row}
    for collection, key, kind in (('chapters', 'chapterId', 'chapter'), ('volumes', 'volumeId', 'volume'),
                                  ('arcs', 'arcId', 'arc'), ('units', 'unitId', 'unit')):
        for row in structure.get(collection, []):
            records[row[key]] = {'id': row[key], 'type': kind, 'name': row.get('title', ''),
                                'status': row.get('status'), 'aliases': [], 'record': row}
    records[structure['projectId']] = {'id': structure['projectId'], 'type': 'project', 'name': structure['bookName'],
        'status': None, 'aliases': [], 'record': {'projectId': structure['projectId']}}
    for rid, row in records.items():
        projected = source_values[rid]
        row.update({'fields': projected['fields'], 'sourceFragments': projected['fragments'],
                    'fieldPresence': sorted(projected['fieldPresence'])})
    return records


def _cursor_decode(value: str) -> dict:
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode('ascii')))
        if not isinstance(data, dict) or type(data.get('offset')) is not int or data['offset'] < 0:
            raise ValueError('invalid cursor')
        return data
    except (ValueError, UnicodeError, TypeError) as exc:
        raise StructureError('CURSOR_INVALID', 'invalid query cursor') from exc


def _cursor(binding: str, source: str, offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({'binding': binding, 'source': source, 'offset': offset}, separators=(',', ':')).encode()).decode()


def _size(value) -> int:
    return len((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode())


def query_bounds(*, limit=None, maxBytes=None, view='detail', depth=None) -> tuple[int, int]:
    """Resolve registry defaults and explain every rejected bound to every transport."""
    policy = query_policy()
    count = policy['defaultLimit'] if limit is None else limit
    budget = policy['maxBytes'][view] if maxBytes is None else maxBytes
    values = {'limit': (count, policy['maxLimit']), 'maxBytes': (budget, policy['maxBytes'][view])}
    if depth is not None:
        values['depth'] = (depth, policy['maxDepth'])
    invalid = [key for key, (value, maximum) in values.items()
               if type(value) is not int or not 1 <= value <= maximum]
    if invalid:
        bounds = {key: {'minimum': 1, 'maximum': maximum, 'received': value}
                  for key, (value, maximum) in values.items()}
        message = '; '.join(f'{key} must be an integer in 1..{values[key][1]} (received {values[key][0]!r})'
                            for key in invalid)
        raise StructureError('QUERY_BUDGET_INVALID', message,
                             {'view': view, 'invalidFields': invalid, 'bounds': bounds,
                              'nextStep': 'use the allowed bounds and follow nextCursor while hasMore is true'})
    return count, budget


def _page(envelope: dict, items: list[dict], *, limit: int, budget: int, offset: int, binding: str, source: str) -> dict:
    if offset > len(items):
        raise StructureError('CURSOR_INVALID', 'cursor offset exceeds the matching result set')
    count = min(limit, len(items) - offset)
    while True:
        end = offset + count
        result = {**envelope, 'totalMatching': len(items), 'returned': count, 'hasMore': end < len(items),
                  'nextCursor': _cursor(binding, source, end) if end < len(items) else None,
                  'items': items[offset:end]}
        if _size(result) <= budget:
            if count == 0 and offset < len(items):
                break
            return result
        if count == 0:
            break
        count -= 1
    raise StructureError('FIELD_TOO_LARGE', 'one complete record or response metadata exceeds the byte budget; project fewer fields or use an explicit full audit/export',
                         {'id': items[offset].get('id') if offset < len(items) else None, 'maxBytes': budget})


def query_project(root: Path, dimension: str, **arguments) -> dict:
    if dimension not in DIMENSIONS:
        raise StructureError('QUERY_DIMENSION_INVALID', 'unknown query dimension')
    unknown = set(arguments) - set(QUERY_PROPERTIES)
    if unknown:
        raise StructureError('QUERY_ARGUMENT_INVALID', 'unknown query arguments', {'arguments': sorted(unknown)})
    args = {k: v for k, v in arguments.items() if v is not None and k != 'projectRoot'}
    policy = query_policy()
    view = args.get('view', 'detail' if args.get('selector') else 'overview')
    if view not in QUERY_PROPERTIES['view']['enum'] or (view == 'schema' and dimension != 'attributes'):
        raise StructureError('QUERY_VIEW_INVALID', 'unsupported query view')
    if view in ('detail', 'traverse') and not args.get('selector'):
        raise StructureError('QUERY_SELECTOR_REQUIRED', 'this view needs one exact selector')
    if view != 'traverse' and any(k in args for k in ('direction', 'edgeKind', 'depth')):
        raise StructureError('QUERY_ARGUMENT_INVALID', 'traversal options require traverse view')
    for key in ('kind', 'direction'):
        if key in args and args[key] not in QUERY_PROPERTIES[key]['enum']:
            raise StructureError('QUERY_ARGUMENT_INVALID', f'invalid {key}')
    depth = args.get('depth', policy['defaultDepth'])
    limit, budget = query_bounds(limit=args.get('limit'), maxBytes=args.get('maxBytes'), view=view, depth=depth)
    structure = load_structure(root)
    from .consistency import require_no_pending
    require_no_pending(root)
    schema = settings_schema()
    from .source_fields import field_keys, field_specs
    specs = field_specs(structure)
    schema = {**schema, 'kinds': {kind: {**schema['kinds'].get(kind, {}), 'fields': list(specs.get(kind, {}))}
                                 for kind in sorted(schema['kinds'].keys() | specs.keys())}}
    if args.get('field'):
        args['field'] = sorted(field_keys(structure, args['field']))
    for key in ('hasField',):
        if key in args: args[key] = next(iter(field_keys(structure, [args[key]])))
    if 'fieldEq' in args:
        args['fieldEq'] = {next(iter(field_keys(structure, [key]))): value for key, value in args['fieldEq'].items()}
    source = sha256_json({'structure': structure, 'schema': schema, 'policy': policy})
    binding = sha256_json({'dimension': dimension, 'arguments': {k: v for k, v in args.items() if k != 'cursor'}, 'view': view, 'limit': limit, 'maxBytes': budget})
    offset = 0
    if args.get('cursor'):
        cursor = _cursor_decode(args['cursor'])
        if cursor.get('source') != source:
            raise StructureError('CURSOR_STALE', 'query source changed; start a new snapshot')
        if cursor.get('binding') != binding:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'cursor belongs to different filters, view or budgets')
        offset = cursor['offset']
    records = _records(structure, schema)
    edges = typed_edges(structure)
    incoming, incident = defaultdict(list), defaultdict(list)
    for edge in edges:
        incoming[edge['targetId']].append(edge)
        incident[edge['sourceId']].append(edge); incident[edge['targetId']].append(edge)
    registered_chapters = {r['sourceId'] for r in structure.get('references', [])}
    coverage = {'basis': ['registered references', 'entity relations', 'explicit time mounts', 'chapter structure'],
                'complete': False, 'proseScanned': False, 'chaptersWithRegisteredReferences': len(registered_chapters),
                'totalChapters': len(structure['chapters']), 'absenceMeaning': 'not recorded, not proof of never used'}
    envelope = {'ok': True, 'code': 'PROJECT_QUERY', 'schemaVersion': 1, 'dimension': dimension, 'view': view,
                'snapshot': {'structureRevision': structure['revision'], 'sourceFingerprint': source},
                'filters': {k: v for k, v in args.items() if k not in ('cursor', 'limit', 'maxBytes', 'view')},
                'coverage': coverage, 'warnings': [], 'links': []}
    candidates = list(records.values())
    if dimension == 'logic':
        candidates = [r for r in candidates if r['type'] == 'rule']
    elif dimension == 'attributes':
        candidates = [r for r in candidates if r['type'] in schema['kinds']]
    elif dimension == 'timeline':
        candidates = [r for r in candidates if r['type'] not in ('volume', 'arc')]
    def span(row):
        item = row['record']
        if row['type'] == 'time_anchor':
            return row['id'], row['id']
        return (item.get('storyTimeStartId') or item.get('timeStartId'), item.get('storyTimeEndId') or item.get('timeEndId'))
    def number(anchor_id):
        value = records.get(anchor_id, {}).get('record', {}).get('timeOrdinal')
        return value if type(value) in (int, float) else None
    low = number(args.get('fromAnchor')); high = number(args.get('toAnchor'))
    for key in ('fromAnchor', 'toAnchor'):
        if key in args and (args[key] not in records or number(args[key]) is None):
            raise StructureError('TIME_RANGE_INCOMPARABLE', 'range endpoints need numeric timeOrdinal')
    if low is not None and high is not None and low > high:
        raise StructureError('TIME_RANGE_INVALID', 'fromAnchor must not follow toAnchor')
    matched = []
    for row in candidates:
        fields, item = row['fields'], row['record']
        refs = [e for e in incoming[row['id']] if e['kind'] == 'reference']
        mounts = [e for e in incoming[row['id']] if e['kind'] == 'time-mount']
        if args.get('kind') and row['type'] != args['kind']: continue
        if args.get('status') and row['status'] != args['status']: continue
        if args.get('query') and not any(args['query'].casefold() in str(label).casefold() for label in [row['id'], row['name'], *row['aliases']]): continue
        if args.get('hasField') and args['hasField'] not in fields: continue
        if any(fields.get(k) != v for k, v in args.get('fieldEq', {}).items()): continue
        if args.get('unfilled') and not any(k not in row['fieldPresence']
                for k in (args.get('field') or schema['kinds'].get(row['type'], {}).get('fields', []))): continue
        if args.get('unused') and (refs or (mounts if row['type'] == 'time_anchor' else [])): continue
        if args.get('referencedBy') and not any(e['sourceId'] == args['referencedBy'] for e in refs): continue
        if args.get('chapterId') and row['id'] != args['chapterId']: continue
        if args.get('entityId') and row['id'] != args['entityId']: continue
        from .source_placement import placement_volume_id
        if args.get('volumeId') and placement_volume_id(item.get('placement')) != args['volumeId']: continue
        start, end = span(row)
        if args.get('unmapped') and (start or end): continue
        numeric_start, numeric_end = number(start), number(end or start)
        unknown = numeric_start is None or numeric_end is None
        if not unknown and ((low is not None and max(numeric_start, numeric_end) < low)
                            or (high is not None and min(numeric_start, numeric_end) > high)):
            continue
        row = {**row, 'referenceCount': len(refs), 'timeMountCount': len(mounts),
               'recordedUseCount': len({e['sourceId'] for e in refs + mounts}),
               'startAnchorId': start, 'endAnchorId': end or start, 'timeComparable': not unknown}
        matched.append(row)
    if dimension == 'timeline':
        from .commands import story_timeline
        timeline = story_timeline(root, structure=structure)
        problems = timeline['problems']
        if args.get('problems'):
            affected = {p.get('chapterId') or p.get('entityId') or p.get('anchorId') for p in problems}
            matched = [r for r in matched if r['id'] in affected]
    if args.get('selector'):
        selected_id = args['selector']
        if selected_id not in records:
            scoped = {**structure, 'entities': [row['record'] for row in matched if 'entityId' in row['record']]}
            selected_id = resolve_entity(scoped, selected_id)['entityId']
        matched = [row for row in matched if row['id'] == selected_id]
    matched.sort(key=lambda r: (r['type'], number(r['id']) if number(r['id']) is not None else float('inf'), r['name'], r['id']))
    if view == 'overview':
        summary = {'count': len(matched), 'byType': dict(Counter(r['type'] for r in matched)),
                   'byStatus': dict(Counter(r['status'] if r['status'] is not None else 'unspecified' for r in matched)),
                   'withoutRecordedReferences': sum(r['referenceCount'] == 0 for r in matched),
                   'fieldFill': dict(Counter(k for r in matched for k in r['fields'])),
                   'isolatedNodes': sum(not incident[r['id']] for r in matched)}
        if dimension == 'timeline':
            ids = {r['id'] for r in matched}
            unknown_time = (sum(not row['timeComparable'] for row in matched)
                            if low is not None or high is not None else 0)
            summary.update({'unmappedChapters': sum(r['type'] == 'chapter' and not (r['startAnchorId'] or r['endAnchorId']) for r in matched),
                            'unusedAnchors': sum(r['type'] == 'time_anchor' and not r['recordedUseCount'] for r in matched),
                            'unknownTimeCount': unknown_time,
                            'inversionCount': sum(p['code'] == 'TIME_ORDER_INVERSION' and p.get('chapterId') in ids for p in problems)})
        result = {**envelope, 'summary': summary, 'totalMatching': len(matched), 'returned': 0, 'hasMore': False, 'nextCursor': None,
                  'links': [{'dimension': dimension, 'view': 'list'}]}
        if _size(result) > budget:
            raise StructureError('QUERY_OUTPUT_TOO_LARGE', 'overview exceeds byte budget; narrow filters or request a list')
        return result
    if view == 'schema':
        items = [{'id': kind + ':' + name, 'kind': kind, 'field': name, 'fieldKind': field_kind}
                 for kind, fields in specs.items() if not args.get('kind') or kind == args['kind']
                 for name, field_kind in fields.items()]
    elif view == 'traverse':
        if not matched:
            raise StructureError('ENTITY_NOT_FOUND', 'selector does not belong to this dimension or scope')
        visited, frontier, found = {selected_id}, {selected_id}, {}
        for distance in range(1, depth + 1):
            following = set()
            for node in sorted(frontier):
                for edge in incident[node]:
                    direction = args.get('direction', 'both')
                    if direction == 'out' and edge['sourceId'] != node: continue
                    if direction == 'in' and edge['targetId'] != node: continue
                    if args.get('edgeKind') and args['edgeKind'] not in (edge['kind'], edge.get('relationKind'), edge.get('referenceKind')): continue
                    other = edge['targetId'] if edge['sourceId'] == node else edge['sourceId']
                    found.setdefault(edge['edgeId'], {'id': edge['edgeId'], **edge, 'distance': distance,
                                                      'neighbor': {'id': other, 'name': records[other]['name'], 'type': records[other]['type']},
                                                      'links': links_for(records[other])})
                    if other not in visited: following.add(other)
            visited |= following; frontier = following
        items = sorted(found.values(), key=lambda r: (r['distance'], r['id']))
    else:
        items = []
        for row in matched:
            wanted = args.get('field')
            fields = {k: v for k, v in row['fields'].items() if not wanted or k in wanted}
            item = {k: v for k, v in row.items() if k not in ('fields', 'record')}
            item['sourceFragments'] = [fragment if view == 'detail' else {k: v for k, v in fragment.items() if k != 'content'}
                                       for fragment in row['sourceFragments']
                                       if not wanted or set(wanted).intersection(fragment['fieldKeys'])]
            if dimension == 'attributes' or view == 'detail' or wanted:
                item['fields'] = fields
            if view == 'detail':
                item['record'] = {k: v for k, v in row['record'].items() if k != 'fields'}
            item['links'] = links_for(row)
            items.append(item)
    return _page(envelope, items, limit=limit, budget=budget, offset=offset, binding=binding, source=source)
