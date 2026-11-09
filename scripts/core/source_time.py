"""Story-time constraints and reader-order projections for the source model."""
from __future__ import annotations

from pathlib import Path

from .source_model import SourceIndex, finite_number, snapshot_hash
from .project import source_model_policy
from .structure_store import StructureError

TIME_KINDS = set(source_model_policy()['timeSpecKinds'])


def validate_time_specs(source: dict, index: SourceIndex) -> list[dict]:
    errors = []
    time_ids = {rid for rid, kind in index.owner_types.items() if kind in {'event', 'time_anchor'}}
    for rid in sorted(time_ids):
        row = index.objects[rid]; spec = row.get('timeSpec')
        if row.get('timeOrdinal') is not None and not finite_number(row['timeOrdinal']):
            errors.append({'code': 'SOURCE_TIME_ORDINAL_INVALID', 'path': 'entities.' + rid + '.timeOrdinal'})
        if spec is None:
            continue
        path = 'entities.' + rid + '.timeSpec'
        if row.get('timeOrdinal') is not None:
            errors.append({'code': 'SOURCE_TIME_AUTHORITY_DUPLICATE', 'path': path}); continue
        if (not isinstance(spec, dict) or not isinstance(spec.get('kind'), str)
                or spec['kind'] not in TIME_KINDS):
            errors.append({'code': 'SOURCE_TIME_SPEC_INVALID', 'path': path}); continue
        kind = spec['kind']
        if kind == 'relative':
            if (set(spec) != {'kind', 'anchorId', 'offset'} or not isinstance(spec.get('anchorId'), str)
                    or spec['anchorId'] not in time_ids
                    or not finite_number(spec.get('offset'))):
                errors.append({'code': 'SOURCE_TIME_SPEC_INVALID', 'path': path}); continue
        elif kind == 'range':
            if set(spec) - {'kind', 'afterId', 'beforeId'} or not ({'afterId', 'beforeId'} & set(spec)):
                errors.append({'code': 'SOURCE_TIME_SPEC_INVALID', 'path': path}); continue
            refs = [spec.get(k) for k in ('afterId', 'beforeId') if spec.get(k) is not None]
            if any(not isinstance(v, str) or v not in time_ids or v == rid for v in refs):
                errors.append({'code': 'SOURCE_TIME_SPEC_INVALID', 'path': path}); continue
        elif kind == 'coarse':
            if set(spec) != {'kind', 'label'} or not isinstance(spec.get('label'), str) or not spec['label'].strip():
                errors.append({'code': 'SOURCE_TIME_SPEC_INVALID', 'path': path})
        elif set(spec) != {'kind'}:
            errors.append({'code': 'SOURCE_TIME_SPEC_INVALID', 'path': path})

    for rid, row in index.objects.items():
        chapter = index.owner_types[rid] == 'chapter'
        if not chapter and 'entityId' not in row:
            continue
        fields = ('storyTimeStartId', 'storyTimeEndId') if chapter else ('timeStartId', 'timeEndId')
        prefix = ('chapters.' if chapter else 'entities.') + rid
        for field in fields:
            value = row.get(field)
            if value is not None and (not isinstance(value, str) or value not in time_ids):
                errors.append({'code': 'SOURCE_TIME_MOUNT_INVALID', 'path': prefix + '.' + field,
                               'reason': 'endpoint must reference an existing event or time_anchor'})
    if errors:
        return errors
    from .source_time_constraints import solve_time_constraints
    _, conflict = solve_time_constraints(index)
    return [conflict] if conflict else []


def story_time_states(source: dict, *, index: SourceIndex | None = None) -> dict[str, dict]:
    index = index or SourceIndex.build(source)
    from .source_time_constraints import solve_time_constraints
    bounds, conflict = solve_time_constraints(index)
    if conflict:
        return {rid: {'status': 'conflict', 'reason': conflict['reason']}
                for rid, kind in index.owner_types.items() if kind in {'event', 'time_anchor'}}
    states = {}
    for rid, bound in bounds.items():
        row = index.objects[rid]
        ordinal, spec = row.get('timeOrdinal'), row.get('timeSpec')
        lower, upper = bound['lower'], bound['upper']
        if bound.pop('exact'):
            state = {'status': 'exact', 'lower': lower, 'upper': upper,
                     'basis': 'timeOrdinal' if finite_number(ordinal) else 'point_constraints'}
        elif lower is not None or upper is not None:
            state = {'status': 'range', **bound, 'basis': 'point_constraints'}
        elif spec and spec.get('kind') == 'coarse':
            state = {'status': 'coarse', 'label': spec['label'],
                     'reason': 'coarse story phase has no invented numeric date'}
        else:
            state = {'status': 'unknown', 'reason': 'registered constraints do not determine numeric story time'}
        if spec and spec.get('kind') == 'relative':
            state.update(anchorId=spec['anchorId'], offset=spec['offset'])
            if state['status'] == 'exact':
                state['basis'] = 'relative'
        elif spec and spec.get('kind') == 'range':
            state.update(afterId=spec.get('afterId'), beforeId=spec.get('beforeId'),
                         meaning='possible_occurrence_point')
        if spec and spec.get('kind') == 'coarse' and state['status'] != 'coarse':
            state.update(label=spec['label'], declaredKind='coarse')
        states[rid] = state
    return states


def time_span_states(source: dict, *, index: SourceIndex | None = None,
                     time_states: dict | None = None) -> dict[str, dict]:
    """Project declared endpoints without substituting an owner's occurrence point."""
    index = index or SourceIndex.build(source)
    states = story_time_states(source, index=index) if time_states is None else time_states
    spans = {}
    for rid, row in index.objects.items():
        chapter = index.owner_types[rid] == 'chapter'
        if not chapter and 'entityId' not in row:
            continue
        start_key, end_key = (('storyTimeStartId', 'storyTimeEndId') if chapter
                              else ('timeStartId', 'timeEndId'))
        start, end = row.get(start_key), row.get(end_key)
        if start is None and end is None:
            continue
        spans[rid] = {'startId': start, 'endId': end,
                      'startTime': states.get(start, {'status': 'unknown', 'reason': 'start endpoint is not registered'}),
                      'endTime': states.get(end, {'status': 'unknown', 'reason': 'end endpoint is not registered'}),
                      'meaning': 'declared_endpoints', 'ownerPointInferred': False}
    return spans


def time_point_order(source: dict, left_id: str, right_id: str, *,
                     index: SourceIndex | None = None) -> dict:
    """Strict before/after, equal, or unknown for two explicitly selected points.

This never substitutes a duration's beginning or end for the activity itself.
It can establish order from relative or range constraints without dated anchors.
"""
    from .source_time_constraints import compare_time_points
    index = index or SourceIndex.build(source)
    for rid in (left_id, right_id):
        if not isinstance(rid, str) or index.owner_types.get(rid) not in {'event', 'time_anchor'}:
            raise StructureError('SOURCE_TIME_REFERENCE_INVALID', 'select an existing event or time_anchor')
    result, conflict = compare_time_points(index, left_id, right_id)
    if conflict:
        raise StructureError('SOURCE_TIME_CONFLICT', conflict['reason'], conflict)
    return result


def query_timeline(root: Path, *, readerChapterId=None, limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_review import source_snapshot, bounded_page
    source = source_snapshot(root); index = SourceIndex.build(source)
    if readerChapterId is not None and index.owner_types.get(readerChapterId) != 'chapter':
        raise StructureError('SOURCE_READER_CHAPTER_INVALID', 'readerChapterId must identify a chapter')
    chapters = {row['chapterId']: row for row in source['chapters']}
    cutoff = chapters[readerChapterId]['displayOrdinal'] if readerChapterId else None
    if readerChapterId is not None and (type(cutoff) is not int or cutoff < 1):
        raise StructureError('SOURCE_READER_CHAPTER_INVALID', 'reader chapter has no active reading position')
    def chapter_order(row):
        value = chapters[row['chapterId']].get('displayOrdinal')
        return value if type(value) is int and value > 0 else float('inf')
    mounts = {}
    for row in source['presentations']:
        mounts.setdefault(row['eventId'], []).append(row)
    states, rows = story_time_states(source, index=index), []
    spans = time_span_states(source, index=index, time_states=states)
    for rid in sorted(states):
        obj = index.objects[rid]
        presentations = sorted(mounts.get(rid, []), key=lambda r: (chapter_order(r), r['order']))
        item = {'timeId': rid, 'kind': index.owner_types[rid], 'label': obj.get('name', rid),
                'storyTime': states[rid], 'presentations': presentations}
        if rid in spans:
            item['timeSpan'] = spans[rid]
        if cutoff is not None and index.owner_types[rid] == 'event':
            visible = [r for r in presentations if chapter_order(r) <= cutoff]
            item['readerState'] = 'revealed' if visible else 'unrevealed' if presentations else 'unregistered'
            item['revealedPresentationIds'] = [r['presentationId'] for r in visible]
        rows.append(item)
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'revision': source['revision']},
                        selection={'readerChapterId': readerChapterId}, code='SOURCE_TIMELINE',
                        limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={'storyTimeDoesNotInventDates': True,
                                  'rangeMeaning': 'possible occurrence point; not duration',
                                  'spanMeaning': 'explicit endpoints; owner point is not inferred',
                                  'readerOrderBasis': 'registered event presentations only'})
