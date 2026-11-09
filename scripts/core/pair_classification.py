"""Author-declared pair classification in structure.json; never infer it from prose."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .artifacts import sha256_json
from .project import pair_policy, toolbox_manifest

PAIR_FIELDS = {'pairId', 'characterIds', 'visualPolarityState', 'visualPolarity',
               'dominantRole', 'reason', 'status', 'visibility', 'visibleTo', 'scopeIds'}


def assessment(source: dict) -> dict:
    """An absent assessment is a missing decision, not a negative decision."""
    return deepcopy(source.get('pairAssessment', {'status': 'unassessed'}))


def pair_schema() -> dict:
    return {**pair_policy(), 'requiredFields': ['pairId', 'characterIds'],
            'optionalFields': sorted(PAIR_FIELDS - {'pairId', 'characterIds'}),
            'classification': toolbox_manifest()['classification'],
            'scopeIds': 'optional nonempty project/volume/arc/chapter IDs; omission declares project-wide applicability',
            'authority': 'structure.pairs; pairAssessment describes inventory completeness only'}


def validate_pairs(source: dict, *, check_endpoints: bool = True) -> list[dict]:
    policy = pair_policy()
    errors = []

    def error(code, path, **details):
        errors.append({'code': code, 'path': path, **details})

    coverage = assessment(source)
    if (not isinstance(coverage, dict) or set(coverage) - {'status', 'reason'}
            or coverage.get('status') not in policy['assessmentStates']):
        error('PAIR_ASSESSMENT_INVALID', 'pairAssessment')
    elif (coverage['status'] != 'unassessed' or 'reason' in coverage) and (
            not isinstance(coverage.get('reason'), str) or not coverage['reason'].strip()):
        error('PAIR_ASSESSMENT_REASON_REQUIRED', 'pairAssessment.reason')
    rows = source.get('pairs', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        error('PAIR_COLLECTION_INVALID', 'pairs')
        return errors
    pattern = re.compile('^' + re.escape(policy['idPrefix']) + '-' + policy['idSuffixPattern'] + '$')
    characters = {row.get('entityId') for row in source.get('entities', [])
                  if isinstance(row, dict) and row.get('entityType') == 'character'}
    scopes = {source.get('projectId')}
    for collection, key in (('chapters', 'chapterId'), ('volumes', 'volumeId'), ('arcs', 'arcId')):
        scopes.update(row.get(key) for row in source.get(collection, []))
    ids, endpoints = set(), set()
    active = []
    for number, row in enumerate(rows):
        path = f'pairs.{number}'
        rid = row.get('pairId')
        if not isinstance(rid, str) or not pattern.fullmatch(rid):
            error('PAIR_ID_INVALID', path + '.pairId')
        elif rid in ids:
            error('DUPLICATE_ID', path + '.pairId', id=rid)
        else:
            ids.add(rid)
        if set(row) - PAIR_FIELDS:
            error('PAIR_FIELDS_INVALID', path, unknown=sorted(set(row) - PAIR_FIELDS))
        members = row.get('characterIds')
        if (not isinstance(members, list) or len(members) != 2
                or any(not isinstance(item, str) or not item for item in members)
                or len(set(members)) != 2):
            error('PAIR_ENDPOINTS_INVALID', path + '.characterIds')
        else:
            if check_endpoints and set(members) - characters:
                error('PAIR_CHARACTER_REQUIRED', path + '.characterIds', ids=sorted(set(members) - characters))
            key = tuple(sorted(members))
            if row.get('status', 'active') != 'archived':
                if key in endpoints:
                    error('PAIR_ENDPOINTS_DUPLICATE', path + '.characterIds')
                endpoints.add(key)
        if row.get('status', 'active') not in policy['recordStatuses']:
            error('PAIR_STATUS_INVALID', path + '.status')
        elif row.get('status', 'active') == 'active':
            active.append(row)
        if 'scopeIds' in row:
            declared = row['scopeIds']
            if (not isinstance(declared, list) or not declared
                    or any(not isinstance(item, str) or not item for item in declared)
                    or len(set(declared)) != len(declared)
                    or check_endpoints and set(declared) - scopes):
                error('PAIR_SCOPE_INVALID', path + '.scopeIds')
        state = row.get('visualPolarityState', 'unassessed')
        if state not in policy['classificationStates']:
            error('PAIR_CLASSIFICATION_STATE_INVALID', path + '.visualPolarityState')
        for field, values in toolbox_manifest()['classification'].items():
            if field in row and (not isinstance(row[field], str) or row[field] not in values):
                error('PAIR_CLASSIFICATION_INVALID', path + '.' + field)
        if state == 'known' and 'visualPolarity' not in row:
            error('PAIR_CLASSIFICATION_REQUIRED', path + '.visualPolarity')
        if state != 'known' and 'visualPolarity' in row:
            error('PAIR_UNKNOWN_VALUE_PRESENT', path + '.visualPolarity')
        if (state == 'not_applicable' or 'reason' in row) and (
                not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            error('PAIR_REASON_REQUIRED', path + '.reason')
        if 'visibility' in row and row['visibility'] not in ('public', 'private'):
            error('PAIR_VISIBILITY_INVALID', path + '.visibility')
        if 'visibleTo' in row:
            visible = row['visibleTo']
            if (not isinstance(visible, list) or any(not isinstance(item, str) for item in visible)
                    or len(set(visible)) != len(visible)
                    or check_endpoints and set(visible) - characters):
                error('PAIR_VISIBILITY_INVALID', path + '.visibleTo')
    if isinstance(coverage, dict):
        if coverage.get('status') == 'not_applicable' and active:
            error('PAIR_ASSESSMENT_CONFLICT', 'pairAssessment', message='not_applicable requires no active pairs')
        if coverage.get('status') == 'assessed' and not active:
            error('PAIR_ASSESSMENT_CONFLICT', 'pairAssessment', message='empty inventory requires unassessed or not_applicable')
    return errors


def chapter_scope(source: dict, chapter_id: str) -> set[str]:
    from .source_placement import chapter_volume_id
    chapter = next((row for row in source.get('chapters', []) if row['chapterId'] == chapter_id), None)
    if chapter is None:
        from .structure_store import StructureError
        raise StructureError('CHAPTER_NOT_FOUND', 'unknown chapter for pair applicability')
    scope = {source['projectId'], chapter_id}
    volume = chapter_volume_id(chapter, source.get('schemaVersion'))
    if volume:
        scope.add(volume)
    arcs = {row['arcId']: row for row in source.get('arcs', [])}
    pending = [row['arcId'] for row in source.get('chapterArcLinks', []) if row['chapterId'] == chapter_id]
    while pending:
        rid = pending.pop()
        if rid in scope:
            continue
        scope.add(rid)
        parent = arcs.get(rid, {}).get('parentArcId')
        if parent:
            pending.append(parent)
    return scope


def pair_inputs(source: dict, chapter_id: str | None = None, *, scope_ids: set | None = None) -> dict:
    """Stable shared input for chapter bindings and applicability consumers."""
    scope = chapter_scope(source, chapter_id) if chapter_id is not None else scope_ids
    result = {'assessment': assessment(source), 'pairs': sorted(
        [deepcopy(row) for row in source.get('pairs', []) if row.get('status', 'active') == 'active'
         and (scope is None or not row.get('scopeIds') or scope.intersection(row['scopeIds']))],
        key=lambda row: row['pairId'])}
    return {**result, 'inputHash': sha256_json(result)}


def query_pairs(root: Path, *, pairId: str | None = None, includeArchived: bool = False,
                limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_review import bounded_page, source_snapshot
    from .structure_store import StructureError
    source = source_snapshot(root)
    inputs = pair_inputs(source)
    rows = sorted([row for row in source.get('pairs', [])
                   if (includeArchived or row.get('status', 'active') != 'archived')
                   and (pairId is None or row['pairId'] == pairId)], key=lambda row: row['pairId'])
    if pairId is not None and not rows:
        raise StructureError('PAIR_NOT_FOUND', 'unknown pair in selected scope')
    from .source_model import snapshot_hash
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'revision': source['revision']},
                        selection={'pairId': pairId, 'includeArchived': includeArchived}, code='SOURCE_PAIRS',
                        limit=limit, maxBytes=maxBytes, cursor=cursor,
                        assessment=inputs['assessment'], inputHash=inputs['inputHash'])


def validate_pair_dependencies(source: dict, index) -> list[dict]:
    """An explicit plan dependency cannot contradict the authored applicability scope."""
    if not source.get('pairs'):
        return []
    from .source_impact import ReviewInputs
    from .source_plan import chapter_plan
    inputs = ReviewInputs(source)
    incoming = inputs.incoming
    errors = []
    for fid, fragment in index.fragments.items():
        if fragment.get('contentRole') != 'plan' or fragment.get('status') == 'archived':
            continue
        owner = fragment['ownerId']
        allowed = pair_inputs(source, owner if index.owner_types.get(owner) == 'chapter' else None,
                              scope_ids={source['projectId'], owner})['pairs']
        allowed_ids = {row['pairId'] for row in allowed}
        if index.owner_types.get(owner) == 'pair':
            allowed_ids.add(owner)
        seen, pending = set(), [fid]
        while pending:
            rid = pending.pop()
            if rid in seen:
                continue
            seen.add(rid)
            pending.extend(incoming[rid] - seen)
        conflicts = sorted(rid for rid in seen if index.owner_types.get(rid) == 'pair' and rid not in allowed_ids)
        if conflicts:
            errors.append({'code': 'PAIR_DEPENDENCY_SCOPE_CONFLICT', 'path': 'fragments.' + fid,
                           'pairIds': conflicts, 'ownerId': owner,
                           'message': 'plan dependency contradicts pair scope; revise scope or dependency in the same preview'})
    # Chapter references can select whole cards and their reference-valued attributes
    # without a fragment.dependsOn edge. Use the actual plan closure for this path.
    for chapter in source.get('chapters', []):
        cid = chapter['chapterId']
        if chapter.get('status') == 'archived':
            continue
        allowed_ids = {row['pairId'] for row in pair_inputs(source, cid)['pairs']}
        actual = chapter_plan(source, cid, _inputs=inputs)['records']
        conflicts = sorted(row['id'] for row in actual if index.owner_types.get(row['id']) == 'pair'
                           and row['id'] not in allowed_ids)
        if conflicts:
            errors.append({'code': 'PAIR_DEPENDENCY_SCOPE_CONFLICT', 'path': 'chapters.' + cid,
                           'pairIds': conflicts, 'ownerId': cid,
                           'message': 'chapter reference closure contradicts pair scope; revise scope or reference'})
    return errors
