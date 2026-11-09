"""Authorial planning decisions, distinct from world facts and legacy state decisions."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path

from .consistency import require_no_pending
from .project import source_model_policy
from .source_model import SourceIndex, require_current_source, snapshot_hash
from .source_review import bounded_page
from .structure_commands import _base_plan, _write_plan
from .structure_store import StructureError, canonical_json, load_structure, new_id, validate_structure


def _scoped_records(source: dict) -> dict[str, dict]:
    return {**SourceIndex.build(source).records,
            **{row['candidateId']: row for row in source.get('planningCandidates', [])}}


SCOPE_SNAPSHOT_BASIS = 'author-content'


def _scope_hashes(source: dict, scope_ids: list[str]) -> dict[str, str]:
    """One domain-separated content proof; never use the effective plan here."""
    records = _scoped_records(source)
    index = SourceIndex.build(source)
    return {rid: hashlib.sha256(canonical_json({
                'purpose': 'gnovel:author-decision:scope',
                'basis': SCOPE_SNAPSHOT_BASIS, 'recordId': rid,
                'content': _scope_content(source, index, records, rid)
            }).encode()).hexdigest()
            for rid in scope_ids if rid in records}


def _scope_content(source, index, records, record_id):
    if index.owner_types.get(record_id) == 'chapter':
        from .source_plan import base_chapter_plan
        plan = base_chapter_plan(source, record_id)
        return {'recipe': plan['recipe'], 'records': plan['records']}
    selected = {record_id}
    # An owner scope includes its actual authored fields; a precise fragment or
    # value scope remains precise. This does not include author decisions.
    if record_id in index.objects:
        attributes = {rid for rid, row in index.attributes.items() if row['ownerId'] == record_id}
        selected.update(attributes)
        selected.update(rid for rid, row in index.fragments.items()
                        if row['ownerId'] == record_id or attributes.intersection(row['contentBindings']))
    # Explicit dependencies include field meanings, value inputs, time mounts,
    # relations and fragment dependencies. Never expand an unrelated sibling just
    # because it shares a project/volume owner.
    from .source_impact import ReviewInputs
    inputs = ReviewInputs(source)
    closure = inputs.closure(selected)
    return {rid: inputs.content(rid, closure[rid], authored=True) if rid in index.records else records[rid]
            for rid in sorted(closure) if rid in records}


def relevant_planning_candidate_ids(source: dict, chapter_id: str) -> set[str]:
    from .source_placement import chapter_volume_id, placement_volume_id
    chapter = next(row for row in source['chapters'] if row['chapterId'] == chapter_id)
    scope = {chapter_id, chapter_volume_id(chapter, source.get('schemaVersion'))}
    scope.update(row['arcId'] for row in source.get('chapterArcLinks', []) if row['chapterId'] == chapter_id)
    scope.discard(None)
    return {row['candidateId'] for row in source.get('planningCandidates', [])
            if ({row.get('primaryArcId'), row.get('beforeChapterId'), row.get('materializedChapterId'),
                 placement_volume_id(row.get('placement'))} | set(row.get('secondaryArcIds', []))) & scope
            or (row.get('placement') is None and row.get('primaryArcId') is None)}


def chapter_decision_scope(source: dict, chapter_id: str, base_plan: dict) -> set[str]:
    """Registered applicability scope shared by context, effective plan and reports."""
    scope = set(base_plan['recipe']['scopeIds']) | {row['id'] for row in base_plan['records'] if row.get('dependencyRole') != 'time_constraint'}
    scope.update(relevant_planning_candidate_ids(source, chapter_id))
    return scope


def chapter_author_inputs(source: dict, chapter_id: str, base_plan: dict) -> dict:
    """Use the base source closure before adding decisions to the effective plan."""
    scope = chapter_decision_scope(source, chapter_id, base_plan)
    by_id = {row['decisionId']: row for row in source.get('authorDecisions', [])}
    active, unavailable = [], []
    for row in by_id.values():
        if row['status'] != 'active' or row['scopeIds'] and not scope.intersection(row['scopeIds']):
            continue
        status = decision_validity(source, by_id, row['decisionId'])
        if status['validity'] == 'current':
            active.append(deepcopy(row))
        else:
            unavailable.append({'decisionId': row['decisionId'], 'statement': row['statement'],
                                'scopeIds': row['scopeIds'], **status})
    profile = source.get('workVoiceProfile')
    return {'decisions': sorted(active, key=lambda row: row['decisionId']),
            'unavailableDecisions': sorted(unavailable, key=lambda row: row['decisionId']),
            'workVoiceProfile': deepcopy(profile) if isinstance(profile, dict) and profile.get('status') == 'active' else None}


def validate_decisions(structure: dict) -> list[dict]:
    rows = structure.get('authorDecisions', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return [{'code': 'AUTHOR_DECISIONS_INVALID'}]
    policy = source_model_policy()['authorDecisions']
    pattern = re.compile(r'^' + re.escape(policy['idPrefix']) + '-' + source_model_policy()['idSuffixPattern'] + '$')
    seen, errors = set(), []
    for row in rows:
        rid = row.get('decisionId')
        if not isinstance(rid, str) or not pattern.fullmatch(rid) or rid in seen:
            errors.append({'code': 'AUTHOR_DECISION_ID_INVALID', 'decisionId': rid})
        seen.add(rid)
        if set(row) - {'decisionId', 'statement', 'rationale', 'scopeIds', 'dependsOn', 'status', 'approval', 'scopeSnapshot', 'scopeSnapshotBasis', 'scopeInvalidatedBy',
                       'alternatives', 'constraints', 'evidenceRefs', 'supersedes'}:
            errors.append({'code': 'AUTHOR_DECISION_FIELDS_INVALID', 'decisionId': rid})
        options = row.get('alternatives', [])
        if (not isinstance(options, list) or any(not isinstance(option, dict)
                or set(option) != {'label', 'outcome', 'reason'}
                or not all(isinstance(option[key], str) and option[key].strip() for key in option)
                or option['outcome'] not in {'chosen', 'rejected', 'deferred'} for option in options)
                or (options and sum(option['outcome'] == 'chosen' for option in options) != 1)):
            errors.append({'code': 'AUTHOR_DECISION_ALTERNATIVES_INVALID', 'decisionId': rid})
        for key in ('constraints', 'evidenceRefs'):
            if key in row and (not isinstance(row[key], list) or any(not isinstance(item, str) or not item.strip() for item in row[key])):
                errors.append({'code': 'AUTHOR_DECISION_DETAIL_INVALID', 'decisionId': rid, 'field': key})
        if row.get('status') not in policy['statuses'] or not isinstance(row.get('statement'), str) or not row['statement'].strip() or \
                not isinstance(row.get('rationale'), str):
            errors.append({'code': 'AUTHOR_DECISION_CONTENT_INVALID', 'decisionId': rid})
        for key in ('scopeIds', 'dependsOn'):
            value = row.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value) or \
                    len(set(value)) != len(value):
                errors.append({'code': 'AUTHOR_DECISION_SCOPE_INVALID', 'decisionId': rid, 'field': key})
        if row.get('status') == 'active':
            approval = row.get('approval')
            if not isinstance(approval, dict) or not isinstance(approval.get('actorId'), str) or not approval['actorId'].strip() or \
                    not isinstance(approval.get('evidence'), str) or not approval['evidence'].strip() or \
                    not isinstance(row.get('scopeSnapshot'), dict):
                errors.append({'code': 'AUTHOR_DECISION_APPROVAL_INVALID', 'decisionId': rid})
        elif row.get('status') == 'draft' and (row.get('approval') is not None or row.get('scopeSnapshot') is not None):
            errors.append({'code': 'AUTHOR_DECISION_DRAFT_APPROVAL_INVALID', 'decisionId': rid})
        marker = row.get('scopeInvalidatedBy')
        if marker is not None and not _valid_invalidation(marker):
            errors.append({'code': 'AUTHOR_DECISION_INVALIDATION_INVALID', 'decisionId': rid})
    for row in rows:
        if isinstance(row.get('dependsOn'), list) and any(dep not in seen or dep == row.get('decisionId') for dep in row['dependsOn']):
            errors.append({'code': 'AUTHOR_DECISION_DEPENDENCY_INVALID', 'decisionId': row.get('decisionId')})
        if row.get('supersedes') is not None and (row['supersedes'] not in seen or row['supersedes'] == row.get('decisionId')):
            errors.append({'code': 'AUTHOR_DECISION_SUPERSEDES_INVALID', 'decisionId': row.get('decisionId')})
    graph = {row.get('decisionId'): row.get('dependsOn', []) for row in rows
             if isinstance(row.get('decisionId'), str) and isinstance(row.get('dependsOn'), list)
             and all(isinstance(dep, str) for dep in row['dependsOn'])}
    visiting, visited = set(), set()
    def visit(rid):
        if rid in visiting:
            return True
        if rid in visited:
            return False
        visiting.add(rid)
        cyclic = any(visit(dep) for dep in graph.get(rid, []) if dep in graph)
        visiting.remove(rid); visited.add(rid)
        return cyclic
    if any(visit(rid) for rid in graph):
        errors.append({'code': 'AUTHOR_DECISION_DEPENDENCY_CYCLE'})
    return errors


def preview_decision_change(root: Path, *, action: str, expectedRevision: int, reason: str,
                            decisionId: str | None = None, statement: str | None = None,
                            rationale: str | None = None, scopeIds: list[str] | None = None,
                            dependsOn: list[str] | None = None, approvalActor: str | None = None,
                            approvalEvidence: str | None = None, alternatives: list[dict] | None = None,
                            constraints: list[str] | None = None, evidenceRefs: list[str] | None = None,
                            supersedes: str | None = None) -> dict:
    root = root.resolve()
    if action == 'create' and dependsOn is None:
        dependsOn = []
    require_no_pending(root)
    source = load_structure(root)
    require_current_source(source)
    if type(expectedRevision) is not int or source['revision'] != expectedRevision:
        raise StructureError('REVISION_CONFLICT', 'decision preview requires current source revision')
    if not isinstance(reason, str) or not reason.strip():
        raise StructureError('AUTHOR_DECISION_INVALID', 'reason is required')
    proposed = deepcopy(source)
    rows = proposed.setdefault('authorDecisions', [])
    if action == 'create':
        if decisionId is not None or approvalActor is not None or approvalEvidence is not None or \
                not isinstance(statement, str) or not statement.strip() or not isinstance(rationale, str) or \
                not isinstance(scopeIds, list) or not isinstance(dependsOn, list):
            raise StructureError('AUTHOR_DECISION_INVALID', 'create requires statement, rationale and explicit scopes')
        unknown = set(scopeIds) - set(_scoped_records(source))
        if unknown:
            raise StructureError('AUTHOR_DECISION_SCOPE_UNKNOWN', 'decision has unknown scope IDs', {'ids': sorted(unknown)})
        if supersedes is not None and not any(item['decisionId'] == supersedes and item['status'] == 'active'
                                              for item in source.get('authorDecisions', [])):
            raise StructureError('AUTHOR_DECISION_SUPERSEDES_INVALID', 'superseded decision must be active')
        row = {'decisionId': new_id('author_decision'), 'statement': statement.strip(),
               'rationale': rationale.strip(), 'scopeIds': scopeIds, 'dependsOn': dependsOn,
               'alternatives': alternatives or [], 'constraints': constraints or [],
               'evidenceRefs': evidenceRefs or [], 'status': 'draft'}
        if supersedes is not None:
            row['supersedes'] = supersedes
        rows.append(row)
    else:
        row = next((item for item in rows if item['decisionId'] == decisionId), None)
        if row is None:
            raise StructureError('AUTHOR_DECISION_NOT_FOUND', 'decision ID not found')
        if any(value is not None for value in (statement, rationale, scopeIds, dependsOn, alternatives,
                                                constraints, evidenceRefs, supersedes)):
            raise StructureError('AUTHOR_DECISION_INVALID', 'only create accepts decision content')
        if action == 'approve':
            if row['status'] != 'draft' or not approvalActor or not approvalEvidence:
                raise StructureError('AUTHOR_DECISION_APPROVAL_INVALID', 'draft decision and declared author approval required')
            by_id = {item['decisionId']: item for item in source.get('authorDecisions', [])}
            if any(by_id[dep]['status'] != 'active' or _decision_stale(source, by_id, dep)
                   for dep in row['dependsOn']):
                raise StructureError('AUTHOR_DECISION_DEPENDENCY_NOT_ACTIVE', 'dependent decisions must be active')
            previous = row.get('supersedes')
            if previous is not None and by_id[previous]['status'] != 'active':
                raise StructureError('AUTHOR_DECISION_SUPERSEDES_INVALID', 'previous decision is no longer active')
            current_hashes = _scope_hashes(source, row['scopeIds'])
            if set(current_hashes) != set(row['scopeIds']):
                raise StructureError('AUTHOR_DECISION_SCOPE_STALE', 'scoped record disappeared before approval')
            row['status'] = 'active'
            row['scopeSnapshot'] = current_hashes
            row['scopeSnapshotBasis'] = SCOPE_SNAPSHOT_BASIS
            row['approval'] = {'actorId': approvalActor.strip(), 'evidence': approvalEvidence.strip(),
                               'verifiedHuman': False}
            if previous is not None:
                next(item for item in rows if item['decisionId'] == previous)['status'] = 'superseded'
        elif action == 'supersede':
            if row['status'] != 'active' or approvalActor is not None or approvalEvidence is not None:
                raise StructureError('AUTHOR_DECISION_STATE_INVALID', 'only active decision can be superseded')
            row['status'] = 'superseded'
        else:
            raise StructureError('AUTHOR_DECISION_ACTION_INVALID', 'unknown decision action')
    validate_structure(proposed)
    plan = _base_plan(source, operation='author_decision_' + action, actor='app',
                      parameters={'decisionId': row['decisionId'], 'action': action, 'reason': reason},
                      impact={}, proposed=proposed)
    return _write_plan(root, plan)


def query_author_decisions(root: Path, *, decisionId: str | None = None, status: str | None = None,
                           scopeId: str | None = None, limit=None, maxBytes=None, cursor=None) -> dict:
    root = root.resolve()
    require_no_pending(root)
    source = load_structure(root)
    require_no_pending(root)
    if status is not None and status not in source_model_policy()['authorDecisions']['statuses']:
        raise StructureError('AUTHOR_DECISION_STATUS_INVALID', 'unknown decision status')
    rows = []
    by_id = {row['decisionId']: row for row in source.get('authorDecisions', [])}
    for row in source.get('authorDecisions', []):
        if (decisionId is not None and row['decisionId'] != decisionId) or (status is not None and row['status'] != status) or \
                (scopeId is not None and scopeId not in row['scopeIds']):
            continue
        validity = decision_validity(source, by_id, row['decisionId'])
        rows.append({**row, **validity, 'stale': row['status'] == 'active' and validity['validity'] != 'current',
                     'epistemicRole': 'authorial_planning_decision'})
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source)},
                        selection={'decisionId': decisionId, 'status': status, 'scopeId': scopeId},
                        code='AUTHOR_DECISIONS', limit=limit, maxBytes=maxBytes, cursor=cursor)


def _decision_stale(source: dict, by_id: dict[str, dict], decision_id: str, seen: set[str] | None = None) -> bool:
    return decision_validity(source, by_id, decision_id, seen)['validity'] != 'current'


def decision_validity(source: dict, by_id: dict[str, dict], decision_id: str,
                      seen: set[str] | None = None) -> dict:
    """Missing/unknown proof domains are historical, never tried as old hashes."""
    seen = set() if seen is None else seen
    if decision_id in seen:
        return {'validity': 'stale', 'reason': 'dependency_cycle', 'staleScopeIds': [], 'staleDecisionIds': [decision_id]}
    seen.add(decision_id)
    row = by_id[decision_id]
    result = {'validity': 'inactive', 'staleScopeIds': [], 'staleDecisionIds': []}
    if row['status'] != 'active':
        return result
    if row.get('scopeSnapshotBasis') != SCOPE_SNAPSHOT_BASIS:
        return {**result, 'validity': 'unproven', 'reason': 'scope_snapshot_basis_unproven',
                'recovery': {'action': 'create_superseding_decision', 'supersedes': decision_id,
                             'approvalRequired': True}}
    marker = row.get('scopeInvalidatedBy')
    if marker is not None:
        return {'validity': 'stale', 'reason': 'committed_scope_invalidation',
                'staleScopeIds': list(marker.get('staleScopeIds', [])),
                'staleDecisionIds': list(marker.get('staleDecisionIds', [])),
                'scopeInvalidatedBy': deepcopy(marker),
                'recovery': {'action': 'create_superseding_decision', 'supersedes': decision_id,
                             'approvalRequired': True}}
    current = _scope_hashes(source, row['scopeIds'])
    previous = row.get('scopeSnapshot', {})
    stale_ids = sorted(rid for rid in set(row['scopeIds']) | set(previous)
                       if rid not in current or rid not in previous or current[rid] != previous[rid])
    stale_deps = sorted(dep for dep in row['dependsOn']
                        if dep not in by_id or _decision_stale(source, by_id, dep, seen.copy()))
    return {'validity': 'stale' if stale_ids or stale_deps else 'current',
            'staleScopeIds': stale_ids, 'staleDecisionIds': stale_deps}


INVALIDATION_KIND = 'author-content-invalidation-v1'


def _valid_invalidation(marker):
    return (isinstance(marker, dict) and set(marker) == {'kind', 'operationId', 'revision', 'reason',
                'staleScopeIds', 'staleDecisionIds'} and marker['kind'] == INVALIDATION_KIND
            and isinstance(marker['operationId'], str) and re.fullmatch(r'OP-[0-9A-F]{16}', marker['operationId'])
            and type(marker['revision']) is int and marker['revision'] > 0
            and marker['reason'] in {'scope_or_dependency_changed', 'historical_approval_restored'}
            and all(isinstance(marker[key], list)
                    and all(isinstance(value, str) and value for value in marker[key])
                    and marker[key] == sorted(set(marker[key]))
                    for key in ('staleScopeIds', 'staleDecisionIds')))


def _without_invalidation(row):
    return {key: value for key, value in row.items() if key != 'scopeInvalidatedBy'}


def _require_authored_transition(before, after, operation, parameters):
    """No generic writer may mutate author-approved content or approval proof."""
    old = {row['decisionId']: _without_invalidation(row) for row in before.get('authorDecisions', [])}
    new = {row['decisionId']: _without_invalidation(row) for row in after.get('authorDecisions', [])}
    if old == new:
        return
    action = {'author_decision_create': 'create', 'author_decision_approve': 'approve',
              'author_decision_supersede': 'supersede'}.get(operation)
    did = parameters.get('decisionId')
    if action is None or parameters.get('action') != action or not isinstance(did, str):
        raise StructureError('AUTHOR_DECISION_WRITE_REQUIRES_TRANSACTION',
                             'author decisions change only through their dedicated reviewed operation')
    expected = deepcopy(old)
    if action == 'create':
        row = new.get(did)
        if did in old or row is None or row.get('status') != 'draft' or any(key in row for key in
                ('approval', 'scopeSnapshot', 'scopeSnapshotBasis')):
            raise StructureError('AUTHOR_DECISION_APPROVAL_INVALID', 'creation cannot carry approval proof')
        expected[did] = row
    elif action == 'approve':
        row, previous = new.get(did), old.get(did)
        if row is None or previous is None or previous.get('status') != 'draft':
            raise StructureError('AUTHOR_DECISION_APPROVAL_INVALID', 'only the exact draft can acquire new approval')
        approval = row.get('approval')
        if (not isinstance(approval, dict) or set(approval) != {'actorId', 'evidence', 'verifiedHuman'}
                or approval['verifiedHuman'] is not False or not all(isinstance(approval[k], str)
                and approval[k].strip() for k in ('actorId', 'evidence'))):
            raise StructureError('AUTHOR_DECISION_APPROVAL_INVALID', 'approval must carry explicit host authorization')
        by_id = {item['decisionId']: item for item in before.get('authorDecisions', [])}
        if any(dep not in by_id or decision_validity(before, by_id, dep)['validity'] != 'current'
               for dep in previous['dependsOn']):
            raise StructureError('AUTHOR_DECISION_DEPENDENCY_NOT_ACTIVE', 'dependent decisions must be current')
        expected[did] = {**deepcopy(previous), 'status': 'active', 'approval': approval,
                         'scopeSnapshot': _scope_hashes(before, previous['scopeIds']),
                         'scopeSnapshotBasis': SCOPE_SNAPSHOT_BASIS}
        prior = previous.get('supersedes')
        if prior is not None:
            if prior not in old or old[prior]['status'] != 'active':
                raise StructureError('AUTHOR_DECISION_SUPERSEDES_INVALID', 'previous decision is no longer active')
            expected[prior]['status'] = 'superseded'
    else:
        if did not in old or old[did]['status'] != 'active':
            raise StructureError('AUTHOR_DECISION_STATE_INVALID', 'only active decisions can be superseded')
        expected[did]['status'] = 'superseded'
    if new != expected:
        raise StructureError('AUTHOR_DECISION_WRITE_SCOPE_INVALID',
                             'decision operation changed content or proof outside its exact transition')


def derived_decision_invalidations(before, after, *, operation_id, revision, restoring=False):
    """Derive monotonic qualification loss inside the original source transaction.

    No original scope/approval bytes are rewritten. Existing invalidations are
    retained even when the source returns to old content or an undo restores a
    historical record. New approval is a different superseding decision.
    """
    result = deepcopy(after)
    old = {row['decisionId']: row for row in before.get('authorDecisions', [])}
    rows = {row['decisionId']: row for row in result.get('authorDecisions', [])}
    for did, row in rows.items():
        previous = old.get(did, {})
        row.pop('scopeInvalidatedBy', None)
        if previous.get('scopeInvalidatedBy') is not None:
            row['scopeInvalidatedBy'] = deepcopy(previous['scopeInvalidatedBy'])
    visited, visiting = set(), set()
    def visit(did):
        if did in visited: return
        if did in visiting:
            raise StructureError('AUTHOR_DECISION_DEPENDENCY_CYCLE', 'author decisions contain a dependency cycle')
        visiting.add(did)
        row = rows[did]
        for dep in row['dependsOn']:
            if dep in rows: visit(dep)
        visiting.remove(did); visited.add(did)
        if (row['status'] != 'active' or row.get('scopeSnapshotBasis') != SCOPE_SNAPSHOT_BASIS
                or row.get('scopeInvalidatedBy') is not None):
            return
        previous = old.get(did)
        resumed = restoring and (previous is None or previous.get('status') != 'active')
        was = decision_validity(before, old, did) if previous and previous.get('status') == 'active' else None
        now = decision_validity(result, rows, did)
        if resumed or now['validity'] == 'stale' or was and was['validity'] == 'stale':
            row['scopeInvalidatedBy'] = {'kind': INVALIDATION_KIND, 'operationId': operation_id,
                'revision': revision, 'reason': 'historical_approval_restored' if resumed else 'scope_or_dependency_changed',
                'staleScopeIds': sorted(set(now.get('staleScopeIds', [])) | set((was or {}).get('staleScopeIds', []))),
                'staleDecisionIds': sorted(set(now.get('staleDecisionIds', [])) | set((was or {}).get('staleDecisionIds', [])))}
    for did in rows: visit(did)
    return result.get('authorDecisions', [])


def prepare_decision_transition(before, after, *, operation, operation_id, parameters):
    _require_authored_transition(before, after, operation, parameters)
    old = {row['decisionId']: row for row in before.get('authorDecisions', [])}
    for row in after.get('authorDecisions', []):
        if row.get('scopeInvalidatedBy') != old.get(row['decisionId'], {}).get('scopeInvalidatedBy'):
            raise StructureError('AUTHOR_DECISION_PROOF_READ_ONLY', 'invalidation proof is tool-derived and cannot be edited')
    if before.get('authorDecisions') or after.get('authorDecisions'):
        after['authorDecisions'] = derived_decision_invalidations(before, after, operation_id=operation_id,
                                                                 revision=before['revision'] + 1)


def validate_decision_transition(before, after, *, operation, operation_id, parameters):
    _require_authored_transition(before, after, operation, parameters)
    expected = derived_decision_invalidations(before, after, operation_id=operation_id, revision=before['revision'] + 1)
    if after.get('authorDecisions', []) != expected:
        raise StructureError('AUTHOR_DECISION_PROOF_READ_ONLY', 'decision invalidation does not match this frozen source operation')


def require_direct_decision_write(before, after):
    if before.get('authorDecisions', []) != after.get('authorDecisions', []):
        raise StructureError('AUTHOR_DECISION_WRITE_REQUIRES_TRANSACTION',
                             'generic structure save cannot rewrite author decisions or their approval proof')
    old = {row['decisionId']: row for row in before.get('authorDecisions', [])}
    new = {row['decisionId']: row for row in after.get('authorDecisions', [])}
    for did, row in old.items():
        if row['status'] != 'active' or row.get('scopeSnapshotBasis') != SCOPE_SNAPSHOT_BASIS or row.get('scopeInvalidatedBy'):
            continue
        previous, current = decision_validity(before, old, did), decision_validity(after, new, did)
        if previous['validity'] == 'stale' or current['validity'] == 'stale':
            raise StructureError('AUTHOR_DECISION_INVALIDATION_REQUIRES_TRANSACTION',
                                 'changed author-decision inputs require a reviewed source transaction', {'decisionId': did})


def _committed_source_image(root, journal, side):
    """Read an existing image pinned by the original operation; never rewrite it."""
    from .jsonio import read_json
    from .write_sets import digest, safe_project_path
    from .structure_store import structure_hash
    relative = '.gnovel/structure.json'
    expected = journal.get(side + 'Files', {}).get(relative, {})
    if not expected.get('exists'):
        return None
    candidates = []
    if side == 'before':
        candidates.append(safe_project_path(root, journal['backupPath'] + '/structure.json'))
    else:
        candidates.append(safe_project_path(root, f".gnovel/plans/{journal['operationId']}.files/{relative}"))
        journal_path = safe_project_path(root, f".gnovel/operations/{journal['operationId']}.json")
        journal_digest = digest(journal_path.read_bytes())
        for terminal in sorted(safe_project_path(root, '.gnovel/transactions').glob('TX-*/completed.json')):
            manifest = read_json(safe_project_path(root, terminal.relative_to(root)))
            if not isinstance(manifest, dict) or manifest.get('phase') != 'committed':
                continue
            rows = {row['path']: row for row in manifest.get('files', [])}
            row = rows.get(str(journal_path.relative_to(root)), {})
            if row.get('after', {}).get('sha256') == journal_digest:
                candidates.append(safe_project_path(root, terminal.parent.relative_to(root)/'after'/relative))
    for path in candidates:
        if path.is_file():
            raw = path.read_bytes()
            if digest(raw) == expected.get('sha256'):
                import json
                source = json.loads(raw)
                if structure_hash(source) == journal.get(side + 'Hash'):
                    return source
    return None


def undo_content_equivalent(root, current, journal):
    """Permit only independently verified monotonic proof additions during undo.

    The normal source/CAS hash stays complete. This exception is confined to
    chain-top/content equivalence and checks each extra proof against its already
    committed operation and exact before/after images. Missing history fails closed.
    """
    from .jsonio import read_json
    from .write_sets import safe_project_path
    from .structure_store import structure_hash
    if structure_hash(current) == journal.get('afterHash'):
        return True
    try:
        after = _committed_source_image(root, journal, 'after')
        if after is None:
            return False
        stripped = lambda source: {**deepcopy(source), 'authorDecisions': [
            _without_invalidation(row) for row in source.get('authorDecisions', [])]}
        if structure_hash(stripped(current)) != structure_hash(stripped(after)):
            return False
        previous = {row['decisionId']: row for row in after.get('authorDecisions', [])}
        for row in current.get('authorDecisions', []):
            marker = row.get('scopeInvalidatedBy')
            prior_marker = previous[row['decisionId']].get('scopeInvalidatedBy')
            if marker == prior_marker:
                continue
            if prior_marker is not None or not _valid_invalidation(marker):
                return False
            proof = read_json(safe_project_path(root, f".gnovel/operations/{marker['operationId']}.json"))
            if proof.get('operationId') != marker['operationId'] or proof.get('afterRevision') != marker['revision']:
                return False
            proof_before = _committed_source_image(root, proof, 'before')
            proof_after = _committed_source_image(root, proof, 'after')
            if proof_before is None or proof_after is None:
                return False
            expected = derived_decision_invalidations(proof_before, proof_after,
                operation_id=marker['operationId'], revision=marker['revision'], restoring=proof.get('operation') == 'undo')
            actual = next((item for item in expected if item['decisionId'] == row['decisionId']), {})
            recorded = next((item for item in proof_after.get('authorDecisions', [])
                             if item['decisionId'] == row['decisionId']), {})
            if actual.get('scopeInvalidatedBy') != marker or recorded.get('scopeInvalidatedBy') != marker:
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def canon_proposal_content(before, after, plan):
    """Separate a proven qualification update from a Canon proposal's content.

    This view is only for the existing exact proposal validator. Full source CAS,
    staged bytes, plan/journal hashes and all execution consumers keep the marker.
    """
    old = {row['decisionId']: row for row in before.get('authorDecisions', [])}
    if not any(row.get('scopeInvalidatedBy') != old.get(row['decisionId'], {}).get('scopeInvalidatedBy')
               for row in after.get('authorDecisions', [])):
        return after
    operation_id = plan.get('operationId')
    if not isinstance(operation_id, str) or not re.fullmatch(r'OP-[0-9A-F]{16}', operation_id):
        return after
    validate_decision_transition(before, after, operation=plan['operation'], operation_id=operation_id,
                                 parameters=plan.get('parameters', {}))
    result = deepcopy(after)
    for row in result.get('authorDecisions', []):
        row.pop('scopeInvalidatedBy', None)
        if old.get(row['decisionId'], {}).get('scopeInvalidatedBy') is not None:
            row['scopeInvalidatedBy'] = deepcopy(old[row['decisionId']]['scopeInvalidatedBy'])
    return result
