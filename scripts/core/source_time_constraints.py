"""Difference constraints for authored time points and explicit span endpoints.

An edge u -> v bounds v - u. Its second weight component is a symbolic
infinitesimal: -1 means strict '<', 0 means '<='. This detects zero-weight
strict cycles without inventing a numerical epsilon or rounding dates.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction


_ORIGIN = ''  # Authored stable IDs cannot be empty.
_ZERO = (Fraction(0), 0)


@dataclass(frozen=True)
class _Edge:
    origin: str
    target: str
    weight: tuple[Fraction, int]
    path: str


def _weight(value, strict=False):
    # Decimal spellings preserve JSON numbers such as 0.1 + 0.2 == 0.3.
    return Fraction(str(value)), -int(strict)


def _shortest(nodes, edges, *, all_sources=False, start=_ORIGIN):
    """Queue-based Bellman–Ford, also returning a contradictory cycle."""
    adjacency = {rid: [] for rid in nodes}
    for edge in edges:
        adjacency[edge.origin].append(edge)
    distances = {rid: _ZERO for rid in nodes} if all_sources else {start: _ZERO}
    pending = deque(distances)
    queued = set(pending)
    depth, previous = dict.fromkeys(distances, 0), {}
    while pending:
        origin = pending.popleft()
        queued.remove(origin)
        for edge in adjacency[origin]:
            current = distances[origin]
            candidate = (current[0] + edge.weight[0], current[1] + edge.weight[1])
            if edge.target in distances and distances[edge.target] <= candidate:
                continue
            distances[edge.target] = candidate
            previous[edge.target] = edge
            depth[edge.target] = depth[origin] + 1
            if depth[edge.target] >= len(nodes):
                # Follow predecessors into the cycle, excluding any tail.
                member = edge.target
                for _ in nodes:
                    member = previous[member].origin
                cycle, cursor = [], member
                while not cycle or cursor != member:
                    cycle.append(previous[cursor])
                    cursor = previous[cursor].origin
                return distances, cycle
            if edge.target not in queued:
                pending.append(edge.target)
                queued.add(edge.target)
    return distances, []


def _number(value):
    return value.numerator if value.denominator == 1 else float(value)


def _constraint_graph(index):
    """Build validated declarations without assigning an owner's span to its point."""
    time_ids = sorted(rid for rid, kind in index.owner_types.items()
                      if kind in {'event', 'time_anchor'})
    time_set = set(time_ids)
    nodes, edges = [_ORIGIN, *time_ids], []

    def edge(origin, target, value, path, *, strict=False):
        edges.append(_Edge(origin, target, _weight(value, strict), path))

    for rid in time_ids:
        row = index.objects[rid]
        path = 'entities.' + rid
        if row.get('timeOrdinal') is not None:
            value = row['timeOrdinal']
            edge(_ORIGIN, rid, value, path + '.timeOrdinal')
            edge(rid, _ORIGIN, -Fraction(str(value)), path + '.timeOrdinal')
        spec = row.get('timeSpec') or {}
        if spec.get('kind') == 'relative':
            edge(spec['anchorId'], rid, spec['offset'], path + '.timeSpec')
            edge(rid, spec['anchorId'], -Fraction(str(spec['offset'])), path + '.timeSpec')
        elif spec.get('kind') == 'range':
            if spec.get('afterId'):
                edge(rid, spec['afterId'], 0, path + '.timeSpec.afterId', strict=True)
            if spec.get('beforeId'):
                edge(spec['beforeId'], rid, 0, path + '.timeSpec.beforeId', strict=True)
    for rid, row in index.objects.items():
        chapter = index.owner_types[rid] == 'chapter'
        if not chapter and 'entityId' not in row:
            continue
        start_key, end_key = (('storyTimeStartId', 'storyTimeEndId') if chapter
                              else ('timeStartId', 'timeEndId'))
        if (isinstance(row.get(start_key), str) and row[start_key] in time_set
                and isinstance(row.get(end_key), str) and row[end_key] in time_set):
            path = ('chapters.' if chapter else 'entities.') + rid + '.' + end_key
            edge(row[end_key], row[start_key], 0, path)

    return nodes, edges


def _conflict(nodes, edges):
    _, cycle = _shortest(nodes, edges, all_sources=True)
    if cycle:
        paths = sorted({item.path for item in cycle})
        return {'code': 'SOURCE_TIME_CONFLICT', 'path': paths[0],
                'reason': 'registered point constraints and span endpoints have no common solution',
                'constraintPaths': paths,
                'timeIds': sorted({rid for item in cycle for rid in (item.origin, item.target)
                                   if rid != _ORIGIN})}
    return None


def solve_time_constraints(index):
    """Solve validated declarations; spans never assign their owner's time point."""
    nodes, edges = _constraint_graph(index)
    conflict = _conflict(nodes, edges)
    if conflict:
        return {}, conflict
    upper, _ = _shortest(nodes, edges)
    reverse = [_Edge(item.target, item.origin, item.weight, item.path) for item in edges]
    lower, _ = _shortest(nodes, reverse)
    bounds = {}
    for rid in nodes[1:]:
        lo, hi = lower.get(rid), upper.get(rid)
        bounds[rid] = {'lower': None if lo is None else _number(-lo[0]),
                       'upper': None if hi is None else _number(hi[0]),
                       'lowerExclusive': lo is not None and lo[1] < 0,
                       'upperExclusive': hi is not None and hi[1] < 0,
                       'exact': lo is not None and hi is not None and -lo[0] == hi[0]
                                and lo[1] == hi[1] == 0}
    return bounds, None


def compare_time_points(index, left_id, right_id):
    """Compare a single pair, including order known without an absolute date."""
    nodes, edges = _constraint_graph(index)
    conflict = _conflict(nodes, edges)
    if conflict:
        return None, conflict
    from_left, _ = _shortest(nodes, edges, start=left_id)
    from_right, _ = _shortest(nodes, edges, start=right_id)
    # These bound left - right. Their signs alone establish strict order.
    lower, upper = from_left.get(right_id), from_right.get(left_id)
    relation = ('before' if upper is not None and upper < _ZERO else
                'after' if lower is not None and lower < _ZERO else
                'equal' if lower == upper == _ZERO else 'unknown')
    return {'leftId': left_id, 'rightId': right_id, 'relation': relation,
            'difference': {'lower': None if lower is None else _number(-lower[0]),
                           'upper': None if upper is None else _number(upper[0]),
                           'lowerExclusive': lower is not None and lower[1] < 0,
                           'upperExclusive': upper is not None and upper[1] < 0},
            'basis': 'registered_point_constraints'}, None
