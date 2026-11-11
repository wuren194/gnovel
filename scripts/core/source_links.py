"""Bounded traversal over explicit source relationships and dependencies."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .project import query_policy, source_model_policy
from .source_impact import dependency_edges
from .source_model import SourceIndex, snapshot_hash
from .source_review import bounded_page, fingerprint, source_snapshot
from .structure_store import StructureError

DIRECTIONS = {'in', 'out', 'both'}


def _edges(source: dict, graph: str = 'registered') -> list[dict]:
    rows = []
    if graph == 'planning':
        from .source_placement import chapter_volume_id, placement_volume_id
        schema_version = source.get('schemaVersion')
        def planning_edge(source_id, target_id, kind):
            if target_id:
                payload = {'sourceId': source_id, 'targetId': target_id, 'kind': kind,
                           'origin': 'planning', 'epistemicRole': 'authorial_plan'}
                rows.append({'edgeId': 'SE-' + fingerprint(payload)[:24].upper(), **payload})
        for candidate in source.get('planningCandidates', []):
            cid = candidate['candidateId']
            candidate_volume = placement_volume_id(candidate.get('placement'))
            planning_edge(cid, candidate_volume, 'candidate_volume')
            for field, kind in (('primaryArcId', 'candidate_arc'),
                                ('beforeChapterId', 'candidate_before'),
                                ('materializedChapterId', 'candidate_materialization')):
                planning_edge(cid, candidate.get(field), kind)
            for aid in candidate.get('secondaryArcIds', []):
                planning_edge(cid, aid, 'candidate_secondary_arc')
            for source_id in candidate.get('sourceCandidateIds', []):
                planning_edge(cid, source_id, 'candidate_derived_from')
        # Units are authorial planning containers, not canon: edge to each member.
        for unit in source.get('units', []):
            unit_id = unit['unitId']
            planning_edge(unit_id, placement_volume_id(unit.get('placement')), 'unit_volume')
            for member in unit.get('members', []) or []:
                planning_edge(unit_id, member.get('memberId'), 'unit_' + str(member.get('memberType')))
        for decision in source.get('authorDecisions', []):
            did = decision['decisionId']
            for rid in decision['scopeIds']:
                planning_edge(did, rid, 'decision_scope')
            for dependency in decision['dependsOn']:
                planning_edge(did, dependency, 'decision_dependency')
        return sorted({row['edgeId']: row for row in rows}.values(),
                      key=lambda row: (row['sourceId'], row['targetId'], row['kind'], row['edgeId']))
    fact_fragments = {row['fragmentId'] for row in source.get('fragments', [])
                      if row.get('contentRole') == 'fact' and row.get('status') != 'archived'}
    canon_entities = {row['entityId'] for row in source.get('entities', [])
                      if row.get('status') != 'archived'}
    subjective_ids = {row['fragmentId'] for row in source['fragments'] if row['contentRole'] in {'belief', 'in_world_text'}}
    subjective_ids.update(row['relationId'] for row in source['relations'] if row.get('perspectiveId') is not None)
    for origin, target, kind in dependency_edges(source):
        if graph == 'canon' and {origin, target} & subjective_ids:
            continue
        if graph == 'canon' and not ({origin, target} & fact_fragments or
                                     kind == 'chapter_reference' and origin in canon_entities):
            continue
        payload = {'sourceId': origin, 'targetId': target, 'kind': kind, 'origin': 'registered_source'}
        rows.append({'edgeId': 'SE-' + fingerprint(payload)[:24].upper(), **payload})
    for relation in source.get('relations', []):
        if graph == 'canon' and relation.get('perspectiveId') is not None:
            continue
        payload = {'sourceId': relation['fromEntity'], 'targetId': relation['toEntity'],
                   'kind': 'entity_relation', 'relationKind': relation.get('kind'),
                   'relationId': relation['relationId'], 'origin': relation.get('origin', 'author'),
                   'epistemicRole': 'character_belief' if relation.get('perspectiveId') else 'canon_fact',
                   **{key: relation[key] for key in ('perspectiveId', 'validDuring', 'basisIds') if key in relation}}
        rows.append({'edgeId': 'SE-' + fingerprint(payload)[:24].upper(), **payload})
    return sorted({row['edgeId']: row for row in rows}.values(),
                  key=lambda row: (row['sourceId'], row['targetId'], row['kind'], row['edgeId']))


def query_source_links(root: Path, *, recordId: str, direction: str = 'both',
                       edgeKinds: list[str] | None = None, depth: int | None = None,
                       limit: int | None = None, maxBytes: int | None = None,
                       cursor: str | None = None, graph: str = 'registered') -> dict:
    source = source_snapshot(root)
    index = SourceIndex.build(source)
    if graph not in {'registered', 'canon', 'planning'}:
        raise StructureError('SOURCE_GRAPH_INVALID', 'unknown graph view')
    decision_ids = {row['decisionId'] for row in source.get('authorDecisions', [])}
    planning_ids = (set(index.planning_candidates) | decision_ids) if graph == 'planning' else set()
    if not isinstance(recordId, str) or recordId not in set(index.records) | planning_ids:
        raise StructureError('SOURCE_RECORD_NOT_FOUND', 'link traversal requires a registered source record')
    if direction not in DIRECTIONS:
        raise StructureError('SOURCE_TRAVERSAL_INVALID', 'direction must be in, out or both')
    if edgeKinds is not None and (not isinstance(edgeKinds, list) or any(
            not isinstance(kind, str) or not kind for kind in edgeKinds)):
        raise StructureError('SOURCE_TRAVERSAL_INVALID', 'edgeKinds must be a string list')
    policy = query_policy()
    depth = policy['defaultDepth'] if depth is None else depth
    if type(depth) is not int or not 1 <= depth <= policy['maxDepth']:
        raise StructureError('QUERY_BUDGET_INVALID', 'traversal depth exceeds the configured limit')
    allowed = set(edgeKinds or [])
    edges = [row for row in _edges(source, graph) if not allowed or row['kind'] in allowed
             or row.get('relationKind') in allowed]
    incident = defaultdict(list)
    for row in edges:
        incident[row['sourceId']].append(row); incident[row['targetId']].append(row)

    def follows(row, node):
        return (direction == 'both' or (direction == 'out' and row['sourceId'] == node)
                or (direction == 'in' and row['targetId'] == node))

    visited, frontier, found, cycle_edges = {recordId}, {recordId}, {}, set()
    for distance in range(1, depth + 1):
        following = set()
        for node in sorted(frontier):
            for row in incident[node]:
                if not follows(row, node):
                    continue
                other = row['targetId'] if row['sourceId'] == node else row['sourceId']
                if other in visited:
                    cycle_edges.add(row['edgeId'])
                found.setdefault(row['edgeId'], {**row, 'distance': distance,
                                                  'fromTraversalNode': node, 'neighborId': other,
                                                  'neighborKind': ('planning_candidate' if other in index.planning_candidates else
                                                      'author_decision' if other in decision_ids else index.owner_types.get(other)) or (
                                                      'field' if other in index.fields else
                                                      'attribute' if other in index.attributes else
                                                      'fragment' if other in index.fragments else
                                                      'value' if other in index.values else 'record')})
                if other not in visited:
                    following.add(other)
        visited |= following; frontier = following
    boundary = []
    for node in sorted(frontier):
        for row in incident[node]:
            if not follows(row, node):
                continue
            other = row['targetId'] if row['sourceId'] == node else row['sourceId']
            if other not in visited:
                boundary.append({'edgeId': row['edgeId'], 'fromId': node, 'toId': other,
                                 'kind': row['kind']})
    rows = sorted(found.values(), key=lambda row: (row['distance'], row['edgeId']))
    truncated = bool(boundary)
    next_traversal = None
    if truncated and depth < policy['maxDepth']:
        next_traversal = {'recordId': recordId, 'direction': direction, 'graph': graph,
                          'edgeKinds': sorted(allowed), 'depth': depth + 1}
    sample_limit = policy['maxLimit']
    basis = ('registered typed source edges only' if graph == 'registered' else
             'registered fact-fragment and entity relation edges only' if graph == 'canon' else
             'registered candidate and author-decision edges only')
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source)},
                        selection={'recordId': recordId, 'direction': direction, 'graph': graph,
                                   'edgeKinds': sorted(allowed), 'depth': depth},
                        code='SOURCE_LINKS' if graph == 'registered' else 'SOURCE_GRAPH',
                        limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={'basis': basis,
                                  'graph': graph, 'planningTruthExcluded': graph == 'canon',
                                  'visitedNodeCount': len(visited), 'cycleEdgeCount': len(cycle_edges),
                                  'requestedDepth': depth, 'truncatedByDepth': truncated,
                                  'boundaryCount': len(boundary), 'boundary': boundary[:sample_limit],
                                  'boundaryTruncated': len(boundary) > sample_limit,
                                  'nextTraversal': next_traversal,
                                  'registeredGraphCompleteWithinDepth': True,
                                  'implicitProseRelationshipsScanned': False,
                                  'absenceMeaning': 'no registered path in the covered graph; implicit prose use is not ruled out'})
