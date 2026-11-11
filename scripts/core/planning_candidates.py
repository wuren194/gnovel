"""Future chapter intentions remain separate from materialized chapter IDs."""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from .consistency import require_no_pending
from .project import source_model_policy
from .source_model import require_current_source, snapshot_hash
from .source_placement import placement_error, placement_volume_id
from .source_review import bounded_page
from .structure_commands import _base_plan, _new_chapter, _write_plan
from .structure_store import StructureError, load_structure, new_id, validate_structure

# schema 2 candidates carried a bare volumeId; schema 3 carries a placement (or null when unplaced).
PLACEMENT_FIELDS = {'title', 'summary', 'placement', 'primaryArcId', 'secondaryArcIds', 'beforeChapterId'}


def _candidate_placement_errors_v3(row: dict, volumes: set, arcs: dict) -> bool:
    """True when a schema-3 candidate's placement/arc binding is invalid.

    Legal: unplaced (placement null, no arc), a volume placement bound to an arc
    scoped to that volume (or a global arc), or a project placement bound only to
    a global arc. Project chapters cannot bind a volume-specific arc (fail closed).
    """
    placement = row.get('placement')
    primary_id = row.get('primaryArcId')
    secondary = row.get('secondaryArcIds')
    if not isinstance(secondary, list) or any(not isinstance(aid, str) for aid in secondary) or len(set(secondary)) != len(secondary):
        return True
    if placement is None:
        # Unplaced: no arc, no anchor, and cannot be approved or materialized yet.
        return (primary_id is not None or bool(secondary) or row.get('beforeChapterId') is not None
                or row.get('status') in {'approved', 'materialized'})
    if placement_error(placement) is not None:
        return True
    volume_id = placement_volume_id(placement)  # None for project scope
    if volume_id is not None and volume_id not in volumes:
        return True
    if not isinstance(primary_id, str) or primary_id not in arcs or primary_id in secondary:
        return True
    for aid in [primary_id, *secondary]:
        arc = arcs.get(aid)
        if arc is None or arc.get('status') == 'archived':
            return True
        arc_volumes = arc.get('volumeIds')
        if volume_id is None:
            # Project scope binds global arcs only (arc.volumeIds == []).
            if arc_volumes:
                return True
        elif arc_volumes and volume_id not in arc_volumes:
            return True
    return False


def validate_candidates(structure: dict) -> list[dict]:
    rows = structure.get('planningCandidates', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return [{'code': 'PLANNING_CANDIDATES_INVALID'}]
    placement_fields = PLACEMENT_FIELDS
    policy = source_model_policy()['planningCandidates']
    pattern = re.compile(r'^' + re.escape(policy['idPrefix']) + '-' + source_model_policy()['idSuffixPattern'] + '$')
    volumes = {row['volumeId'] for row in structure.get('volumes', [])}
    arcs = {row['arcId']: row for row in structure.get('arcs', [])}
    chapters = {row['chapterId']: row for row in structure.get('chapters', [])}
    seen, materialized = set(), set()
    errors = []
    for row in rows:
        cid = row.get('candidateId')
        if not isinstance(cid, str) or not pattern.fullmatch(cid) or cid in seen:
            errors.append({'code': 'PLANNING_CANDIDATE_ID_INVALID', 'candidateId': cid})
        seen.add(cid)
        if set(row) - (placement_fields | {'candidateId', 'status', 'approval', 'materializedChapterId',
                                          'sourceCandidateIds', 'replacedByCandidateIds'}):
            errors.append({'code': 'PLANNING_CANDIDATE_FIELDS_INVALID', 'candidateId': cid})
        if row.get('status') not in policy['statuses']:
            errors.append({'code': 'PLANNING_CANDIDATE_STATUS_INVALID', 'candidateId': cid})
        if not isinstance(row.get('title'), str) or not row['title'].strip() or not isinstance(row.get('summary'), str):
            errors.append({'code': 'PLANNING_CANDIDATE_TEXT_INVALID', 'candidateId': cid})
        invalid = _candidate_placement_errors_v3(row, volumes, arcs)
        if invalid:
            errors.append({'code': 'PLANNING_CANDIDATE_PLACEMENT_INVALID', 'candidateId': cid})
        before = row.get('beforeChapterId')
        if before is not None and (not isinstance(before, str) or before not in chapters or chapters[before].get('status') == 'archived'):
            errors.append({'code': 'PLANNING_CANDIDATE_ANCHOR_INVALID', 'candidateId': cid})
        approval = row.get('approval')
        if row.get('status') in {'approved', 'materialized'}:
            if (not isinstance(approval, dict) or not isinstance(approval.get('actorId'), str)
                    or not approval['actorId'].strip() or not isinstance(approval.get('evidence'), str)
                    or not approval['evidence'].strip()):
                errors.append({'code': 'PLANNING_CANDIDATE_APPROVAL_MISSING', 'candidateId': cid})
        elif approval is not None:
            errors.append({'code': 'PLANNING_CANDIDATE_APPROVAL_UNEXPECTED', 'candidateId': cid})
        chapter_id = row.get('materializedChapterId')
        if row.get('status') == 'materialized':
            if chapter_id not in chapters or chapter_id in materialized:
                errors.append({'code': 'PLANNING_CANDIDATE_CHAPTER_INVALID', 'candidateId': cid})
            materialized.add(chapter_id)
        elif chapter_id is not None:
            errors.append({'code': 'PLANNING_CANDIDATE_CHAPTER_UNEXPECTED', 'candidateId': cid})
    by_id = {row.get('candidateId'): row for row in rows if isinstance(row.get('candidateId'), str)}
    for row in rows:
        cid = row.get('candidateId')
        sources = row.get('sourceCandidateIds', [])
        replacements = row.get('replacedByCandidateIds', [])
        for field, values in (('sourceCandidateIds', sources), ('replacedByCandidateIds', replacements)):
            if (not isinstance(values, list) or not values
                    and field in row or any(not isinstance(value, str) for value in values)
                    or len(values) != len(set(values)) or cid in values):
                errors.append({'code': 'PLANNING_CANDIDATE_LINEAGE_INVALID', 'candidateId': cid, 'field': field})
                continue
            for target in values:
                other = by_id.get(target)
                reverse = 'replacedByCandidateIds' if field == 'sourceCandidateIds' else 'sourceCandidateIds'
                linked = other.get(reverse, []) if other is not None else []
                if (other is None or not isinstance(linked, list) or cid not in linked
                        or field == 'replacedByCandidateIds' and row.get('status') != 'withdrawn'):
                    errors.append({'code': 'PLANNING_CANDIDATE_LINEAGE_INVALID', 'candidateId': cid, 'targetId': target})
    visited, visiting = set(), set()
    for start in by_id:
        if start in visited:
            continue
        visiting.add(start)
        source_ids = by_id[start].get('sourceCandidateIds', [])
        stack = [(start, iter(source_ids if isinstance(source_ids, list) else []))]
        while stack:
            current, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                stack.pop()
                visiting.remove(current)
                visited.add(current)
            elif isinstance(target, str) and target in by_id:
                if target in visiting:
                    errors.append({'code': 'PLANNING_CANDIDATE_LINEAGE_CYCLE', 'candidateId': target})
                elif target not in visited:
                    visiting.add(target)
                    source_ids = by_id[target].get('sourceCandidateIds', [])
                    stack.append((target, iter(source_ids if isinstance(source_ids, list) else [])))
    return errors


def _placement(source: dict, record: dict) -> dict:
    if not isinstance(record, dict) or set(record) - PLACEMENT_FIELDS:
        raise StructureError('PLANNING_CANDIDATE_INVALID', 'unknown candidate placement fields')
    required = {'title', 'summary'}
    if required - set(record):
        raise StructureError('PLANNING_CANDIDATE_INVALID', 'candidate placement is incomplete')
    if not isinstance(record['title'], str) or not isinstance(record['summary'], str):
        raise StructureError('PLANNING_CANDIDATE_INVALID', 'candidate title and summary must be text')
    normalized = {'title': record['title'].strip(), 'summary': record['summary'].strip(),
                  'placement': record.get('placement'), 'primaryArcId': record.get('primaryArcId'),
                  'secondaryArcIds': record.get('secondaryArcIds', []),
                  'beforeChapterId': record.get('beforeChapterId')}
    probe = {**source, 'planningCandidates': [{**normalized, 'candidateId': 'candidate-VALIDATION', 'status': 'draft'}]}
    errors = validate_candidates(probe)
    if errors:
        raise StructureError('PLANNING_CANDIDATE_INVALID', 'invalid candidate placement', {'errors': errors})
    return normalized


def _candidate_unit_conflict(source: dict, candidate_ids) -> None:
    """Fail closed when a candidate that is a unit member is split, merged or withdrawn.

    There is no unambiguous rule for inheriting a retired candidate's unit slot into
    its successors, so the author must adjust unit membership explicitly first.
    """
    wanted = set(candidate_ids)
    for unit in source.get('units', []):
        for member in unit.get('members', []) or []:
            if member.get('memberType') == 'planning_candidate' and member.get('memberId') in wanted:
                raise StructureError('PLANNING_CANDIDATE_UNIT_MEMBERSHIP_CONFLICT',
                                     'candidate belongs to a unit; adjust unit membership before this change',
                                     {'candidateId': member['memberId'], 'unitId': unit['unitId']})


def preview_candidate_change(root: Path, *, action: str, expectedRevision: int, reason: str,
                             candidateId: str | None = None, record: dict | None = None,
                             approvalActor: str | None = None, approvalEvidence: str | None = None) -> dict:
    root = root.resolve()
    require_no_pending(root)
    source = load_structure(root)
    require_current_source(source)
    if (action == 'materialize' and isinstance(candidateId, str) and type(expectedRevision) is int
            and expectedRevision <= source['revision'] and record is None
            and approvalActor is None and approvalEvidence is None):
        existing = next((item for item in source.get('planningCandidates', [])
                         if item['candidateId'] == candidateId), None)
        if existing and existing['status'] == 'materialized' and isinstance(reason, str) and reason.strip():
            return {'ok': True, 'code': 'PLANNING_CANDIDATE_ALREADY_MATERIALIZED',
                    'candidateId': candidateId, 'chapterId': existing['materializedChapterId'],
                    'sourceRevision': source['revision'], 'sourceHash': snapshot_hash(source),
                    'idempotent': True}
    if type(expectedRevision) is not int or source['revision'] != expectedRevision:
        raise StructureError('REVISION_CONFLICT', 'candidate preview requires current revision')
    if not isinstance(reason, str) or not reason.strip():
        raise StructureError('PLANNING_CANDIDATE_INVALID', 'reason is required')
    proposed = deepcopy(source)
    rows = proposed.setdefault('planningCandidates', [])
    if action == 'create':
        if candidateId is not None or approvalActor is not None or approvalEvidence is not None:
            raise StructureError('PLANNING_CANDIDATE_INVALID', 'create accepts placement only')
        row = {**_placement(source, record), 'candidateId': new_id('planning_candidate'), 'status': 'draft'}
        rows.append(row)
    elif action == 'merge':
        if (candidateId is not None or approvalActor is not None or approvalEvidence is not None
                or not isinstance(record, dict) or set(record) != {'sourceCandidateIds', 'placement'}):
            raise StructureError('PLANNING_CANDIDATE_MERGE_INVALID', 'merge requires sources and one new placement')
        ids = record['sourceCandidateIds']
        if (not isinstance(ids, list) or not 2 <= len(ids) <= source_model_policy()['planningCandidates']['maxMergeSources']
                or any(not isinstance(cid, str) for cid in ids) or len(ids) != len(set(ids))):
            raise StructureError('PLANNING_CANDIDATE_MERGE_INVALID', 'invalid merge source IDs')
        sources = [next((item for item in rows if item['candidateId'] == cid), None) for cid in ids]
        if any(item is None or item['status'] not in {'draft', 'approved'} for item in sources):
            raise StructureError('PLANNING_CANDIDATE_MERGE_INVALID', 'merge sources must be live candidates')
        _candidate_unit_conflict(source, ids)
        row = {**_placement(source, record['placement']), 'candidateId': new_id('planning_candidate'),
               'status': 'draft', 'sourceCandidateIds': ids}
        for item in sources:
            item['status'] = 'withdrawn'
            item.pop('approval', None)
            item['replacedByCandidateIds'] = [row['candidateId']]
        rows.append(row)
    else:
        row = next((item for item in rows if item['candidateId'] == candidateId), None)
        if row is None:
            raise StructureError('PLANNING_CANDIDATE_NOT_FOUND', 'candidate does not exist')
        if action == 'revise':
            if row['status'] == 'materialized' or row.get('replacedByCandidateIds') or approvalActor is not None or approvalEvidence is not None:
                raise StructureError('PLANNING_CANDIDATE_IMMUTABLE', 'materialized or replaced candidate cannot be revised')
            row.update(_placement(source, record))
            row['status'] = 'draft'
            row.pop('approval', None)
        elif action == 'split':
            if (row['status'] not in {'draft', 'approved'} or approvalActor is not None or approvalEvidence is not None
                    or not isinstance(record, dict) or set(record) != {'parts'}
                    or not isinstance(record['parts'], list)
                    or not 2 <= len(record['parts']) <= source_model_policy()['planningCandidates']['maxSplitParts']):
                raise StructureError('PLANNING_CANDIDATE_SPLIT_INVALID', 'split requires two or more new placements')
            _candidate_unit_conflict(source, [candidateId])
            parts = [_placement(source, part) for part in record['parts']]
            created = [{**part, 'candidateId': new_id('planning_candidate'), 'status': 'draft',
                        'sourceCandidateIds': [candidateId]} for part in parts]
            row['status'] = 'withdrawn'
            row.pop('approval', None)
            row['replacedByCandidateIds'] = [item['candidateId'] for item in created]
            rows.extend(created)
        elif action == 'approve':
            if row['status'] != 'draft' or record is not None or not approvalActor or not approvalEvidence:
                raise StructureError('PLANNING_CANDIDATE_APPROVAL_INVALID', 'draft candidate and explicit actor/evidence required')
            if row['placement'] is None or row['primaryArcId'] is None:
                raise StructureError('PLANNING_CANDIDATE_UNPLACED', 'candidate must have an approved placement and arc before approval')
            row['status'] = 'approved'
            row['approval'] = {'actorId': approvalActor.strip(), 'evidence': approvalEvidence.strip(),
                               'declaredAtRevision': source['revision'], 'verifiedHuman': False}
        elif action == 'withdraw':
            if row['status'] == 'materialized' or record is not None or approvalActor is not None or approvalEvidence is not None:
                raise StructureError('PLANNING_CANDIDATE_IMMUTABLE', 'materialized candidate cannot be withdrawn')
            _candidate_unit_conflict(source, [candidateId])
            row['status'] = 'withdrawn'
            row.pop('approval', None)
        elif action == 'materialize':
            if row['status'] != 'approved' or record is not None or approvalActor is not None or approvalEvidence is not None:
                raise StructureError('PLANNING_CANDIDATE_NOT_APPROVED', 'only approved candidate can be materialized')
            proposed, chapter_id, insertion = _new_chapter(proposed, title=row['title'], placement=row['placement'],
                before_chapter_id=row['beforeChapterId'], primary_arc_id=row['primaryArcId'], secondary_arc_ids=row['secondaryArcIds'])
            row = next(item for item in proposed['planningCandidates'] if item['candidateId'] == candidateId)
            row['status'] = 'materialized'
            row['materializedChapterId'] = chapter_id
            # Atomically convert any unit member from the candidate to the new chapter,
            # so no unit is left pointing at a materialized candidate.
            for unit in proposed.get('units', []):
                for member in unit.get('members', []) or []:
                    if member.get('memberType') == 'planning_candidate' and member.get('memberId') == candidateId:
                        member['memberType'] = 'chapter'
                        member['memberId'] = chapter_id
        else:
            raise StructureError('PLANNING_CANDIDATE_ACTION_INVALID', 'unsupported candidate action')
    validate_structure(proposed)
    params = {'reason': reason, 'candidateId': row['candidateId'], 'action': action}
    if action == 'materialize':
        params['chapterId'] = chapter_id
    elif action == 'split':
        params['newCandidateIds'] = row['replacedByCandidateIds']
    elif action == 'merge':
        params['sourceCandidateIds'] = row['sourceCandidateIds']
    plan = _base_plan(source, operation='planning_candidate_' + action, actor='app', parameters=params,
                      impact={}, proposed=proposed)
    return _write_plan(root, plan)


def query_planning_candidates(root: Path, *, status: str | None = None, candidateId: str | None = None,
                              limit=None, maxBytes=None, cursor=None) -> dict:
    root = root.resolve()
    require_no_pending(root)
    source = load_structure(root)
    require_no_pending(root)
    if status is not None and status not in source_model_policy()['planningCandidates']['statuses']:
        raise StructureError('PLANNING_CANDIDATE_STATUS_INVALID', 'unknown candidate status')
    placement_key = 'placement'
    rows = [{**row, 'placementReady': row.get(placement_key) is not None and row.get('primaryArcId') is not None}
            for row in source.get('planningCandidates', []) if (status is None or row['status'] == status)
            and (candidateId is None or row['candidateId'] == candidateId)]
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'revision': source['revision']},
                        selection={'status': status, 'candidateId': candidateId}, code='PLANNING_CANDIDATES',
                        limit=limit, maxBytes=maxBytes, cursor=cursor)
