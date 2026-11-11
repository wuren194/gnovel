"""Time-scoped cognition and append-only checks over registered source records."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .project import source_model_policy
from .source_impact import ReviewInputs
from .source_model import SourceIndex, snapshot_hash
from .source_review import bounded_page, fingerprint, source_snapshot
from .source_selection import time_match
from .structure_store import StructureError, utc_now

COGNITION_FIELDS = {'holderId', 'targetId', 'attitude', 'acquiredAtId', 'basisIds'}
CHECK_FIELDS = {'checkId', 'targetId', 'targetHash', 'holderId', 'status', 'checkedAtId',
                'recordedAt', 'reason', 'evidence', 'inputHashes'}


def validate_cognition(source, index, period):
    from .source_validation import _strings, _text
    errors = []
    policy = source_model_policy()

    def error(code, path):
        errors.append({'code': code, 'path': path})

    def time_id(value):
        return value is None or isinstance(value, str) and index.owner_types.get(value) in {'event', 'time_anchor'}

    for rid, fragment in index.fragments.items():
        if 'cognition' not in fragment:
            continue
        path = 'fragments.' + rid + '.cognition'
        cognition = fragment['cognition']
        if not isinstance(cognition, dict) or set(cognition) != COGNITION_FIELDS:
            error('SOURCE_COGNITION_FIELDS_INVALID', path)
            continue
        holder, target = cognition['holderId'], cognition['targetId']
        if not isinstance(holder, str) or index.owner_types.get(holder) != 'character':
            error('SOURCE_COGNITION_HOLDER_INVALID', path)
        if (not isinstance(target, str) or target == rid or
                target not in index.values and target not in index.fragments):
            error('SOURCE_COGNITION_TARGET_INVALID', path)
        role, attitude = fragment['contentRole'], cognition['attitude']
        if (attitude not in policy['cognitionAttitudes'] or role not in {'fact', 'belief'} or
                role == 'belief' and (holder != fragment.get('perspectiveId') or attitude in {'knows', 'unaware'})):
            error('SOURCE_COGNITION_ROLE_INVALID', path)
        if not time_id(cognition['acquiredAtId']):
            error('SOURCE_COGNITION_TIME_INVALID', path)
        basis = cognition['basisIds']
        if not _strings(basis) or any(value not in index.records or value == rid for value in basis):
            error('SOURCE_COGNITION_BASIS_INVALID', path)

    for entity in source['entities']:
        designations = entity.get('designations', [])
        path = 'entities.' + entity['entityId'] + '.designations'
        if not isinstance(designations, list):
            error('SOURCE_DESIGNATION_INVALID', path)
            continue
        for row in designations:
            if (not isinstance(row, dict) or set(row) != {'label', 'perspectiveIds', 'validDuring'} or
                    not _text(row.get('label')) or not _strings(row.get('perspectiveIds'), nonempty=True) or
                    any(index.owner_types.get(rid) != 'character' for rid in row['perspectiveIds'])):
                error('SOURCE_DESIGNATION_INVALID', path)
                continue
            period(row, path)

    for row in source['relations']:
        path = 'relations.' + row['relationId']
        if row.get('perspectiveId') is not None and (not isinstance(row['perspectiveId'], str) or
                index.owner_types.get(row['perspectiveId']) not in {'character', 'faction'}):
            error('SOURCE_RELATION_PERSPECTIVE_INVALID', path)
        if not _strings(row.get('basisIds', [])) or any(rid not in index.records for rid in row.get('basisIds', [])):
            error('SOURCE_RELATION_BASIS_INVALID', path)
        period(row, path)

    for check in source['checks']:
        path = 'checks.' + check['checkId']
        target, holder = check['targetId'], check['holderId']
        if (not isinstance(target, str) or target == check['checkId'] or
                target in {row['checkId'] for row in source['checks']}):
            error('SOURCE_CHECK_TARGET_INVALID', path)
        if holder is not None and (not isinstance(holder, str) or index.owner_types.get(holder) != 'character'):
            error('SOURCE_CHECK_HOLDER_INVALID', path)
        if check['status'] not in policy['checkStatuses'] or not time_id(check['checkedAtId']):
            error('SOURCE_CHECK_STATE_INVALID', path)
        if not _text(check['reason']) or not _text(check['recordedAt']):
            error('SOURCE_CHECK_REASON_REQUIRED', path)
        evidence = check['evidence']
        if not isinstance(evidence, list) or any(
                not isinstance(row, dict) or set(row) != {'fragmentId', 'quote'} or
                not isinstance(row['fragmentId'], str) or
                not _text(row['quote']) for row in evidence):
            error('SOURCE_CHECK_EVIDENCE_INVALID', path)
        hashes = check['inputHashes']
        if (not isinstance(hashes, dict) or not isinstance(target, str) or target not in hashes or
                any(not isinstance(rid, str) or not _hash(value) for rid, value in hashes.items()) or
                not _hash(check['targetHash'])):
            error('SOURCE_CHECK_BINDING_INVALID', path)
    return errors


def _hash(value):
    import re
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def designation_label(index, record, choice):
    labels = {row['label'] for row in record.get('designations', [])
              if set(row['perspectiveIds']) & set(choice.get('perspectiveIds', [])) and
              ('asOf' in choice and choice['asOf'] is not None or not any(row['validDuring'].values())) and
              time_match(index, row, choice.get('asOf')) in {'inside', 'all'}}
    return next(iter(labels)) if len(labels) == 1 else None


def cognition_time(index, fragment, as_of):
    if as_of is None:
        return 'unknown'
    match = time_match(index, fragment, as_of)
    cognition = fragment['cognition']
    acquired = cognition['acquiredAtId']
    if match == 'outside':
        return match
    if acquired is None:
        return match if cognition['attitude'] == 'unaware' else 'unknown'
    acquisition = time_match(index, {'validDuring': {'fromId': acquired}}, as_of)
    if acquisition == 'outside':
        return 'outside'
    return 'inside' if match == acquisition == 'inside' else 'unknown'


def query_cognition(root: Path, *, holderId: str, targetId: str, asOf: str | None):
    source = source_snapshot(root)
    index = SourceIndex.build(source)
    if (not isinstance(holderId, str) or index.owner_types.get(holderId) != 'character' or
            not isinstance(targetId, str) or targetId not in index.values and targetId not in index.fragments):
        raise StructureError('SOURCE_COGNITION_QUERY_INVALID', 'select a character and a concrete value or proposition')
    if asOf is not None and (not isinstance(asOf, str) or index.owner_types.get(asOf) not in {'event', 'time_anchor'}):
        raise StructureError('SOURCE_COGNITION_QUERY_INVALID', 'asOf requires a story time point or explicit null')
    records = []
    for fragment in source['fragments']:
        cognition = fragment.get('cognition')
        if (not cognition or fragment.get('status') == 'archived' or
                cognition['holderId'] != holderId or cognition['targetId'] != targetId):
            continue
        match = cognition_time(index, fragment, asOf)
        if match != 'outside':
            records.append({'fragmentId': fragment['fragmentId'], 'contentRole': fragment['contentRole'],
                            'attitude': cognition['attitude'], 'timeMatch': match,
                            'acquiredAtId': cognition['acquiredAtId'], 'basisIds': cognition['basisIds']})
    attitudes = {row['attitude'] for row in records if row['timeMatch'] == 'inside'}
    if not attitudes:
        state = 'time_unknown' if records else 'no_record'
    elif 'unaware' in attitudes and len(attitudes) > 1:
        state = 'inconsistent'
    elif 'unaware' in attitudes:
        state = 'explicitly_unaware'
    elif 'knows' in attitudes:
        state = 'recorded_knowledge'
    elif 'believes' in attitudes:
        state = 'recorded_belief'
    else:
        state = 'recorded_suspicion'
    return {'ok': True, 'code': 'SOURCE_COGNITION', 'holderId': holderId, 'targetId': targetId,
            'asOf': asOf, 'state': state, 'records': records,
            'verification': 'unverified' if not any(row['targetId'] == targetId and row['holderId'] == holderId
                                                   for row in source['checks']) else 'checks_recorded',
            'checkQuery': {'operation': 'source.checks', 'arguments': {'targetId': targetId, 'holderId': holderId, 'asOf': asOf}},
            'grantsObjectiveTruth': False}


def _make_check(source, *, check_id, target_id, holder_id, status, checked_at, reason, evidence, recorded_at):
    from .source_validation import _text
    inputs = ReviewInputs(source)
    index = inputs.index
    check_ids = {row['checkId'] for row in source['checks']}
    if not isinstance(target_id, str) or target_id not in index.records or target_id in check_ids:
        raise StructureError('SOURCE_CHECK_TARGET_INVALID', 'check an existing source record')
    if holder_id is not None and (not isinstance(holder_id, str) or index.owner_types.get(holder_id) != 'character'):
        raise StructureError('SOURCE_CHECK_HOLDER_INVALID', 'holderId requires a character or null for an author check')
    if status not in source_model_policy()['checkStatuses'] or not _text(reason):
        raise StructureError('SOURCE_CHECK_STATE_INVALID', 'provide a declared check status and reason')
    if checked_at is not None and (not isinstance(checked_at, str) or index.owner_types.get(checked_at) not in {'event', 'time_anchor'}):
        raise StructureError('SOURCE_CHECK_TIME_INVALID', 'checkedAtId requires a story time point or explicit null')
    if not isinstance(evidence, list) or (status in {'supported', 'refuted'} and not evidence):
        raise StructureError('SOURCE_CHECK_EVIDENCE_REQUIRED', 'supported and refuted checks require actual source quotations')
    roots = {target_id}
    if checked_at:
        roots.add(checked_at)
    for row in evidence:
        if not isinstance(row, dict) or set(row) != {'fragmentId', 'quote'}:
            raise StructureError('SOURCE_CHECK_EVIDENCE_INVALID', 'select an existing fragment and an exact quote')
        fragment = index.fragments.get(row['fragmentId']) if isinstance(row['fragmentId'], str) else None
        if (not fragment or fragment.get('status') == 'archived' or not _text(row['quote']) or
                row['quote'] not in fragment['text']):
            raise StructureError('SOURCE_CHECK_EVIDENCE_INVALID', 'quote must match a current active source fragment')
        if holder_id is not None:
            cognition = fragment.get('cognition')
            visible = fragment.get('perspectiveId') == holder_id or (
                fragment['contentRole'] == 'fact' and cognition and cognition['holderId'] == holder_id and
                cognition['attitude'] == 'knows')
            if not visible or checked_at is None or time_match(index, fragment, checked_at) != 'inside':
                raise StructureError('SOURCE_CHECK_PERSPECTIVE_INVALID', 'character checks need evidence available to this holder at the check time')
            if cognition and cognition_time(index, fragment, checked_at) != 'inside':
                raise StructureError('SOURCE_CHECK_PERSPECTIVE_INVALID', 'character evidence acquisition is future or undetermined')
        roots.add(row['fragmentId'])
    return {'checkId': check_id, 'targetId': target_id, 'targetHash': fingerprint(index.records[target_id]),
            'holderId': holder_id, 'status': status, 'checkedAtId': checked_at, 'recordedAt': recorded_at,
            'reason': reason, 'evidence': deepcopy(evidence),
            'inputHashes': {rid: inputs.fingerprint(rid) for rid in sorted(roots)}}


def preview_check(root: Path, *, requestId: str, targetId: str, status: str, checkedAtId: str | None,
                  reason: str, evidence: list[dict], holderId: str | None = None):
    from .locks import project_lock
    from .source_ids import new_id
    from .source_requests import request_identity, resume_request
    from .structure_commands import _base_plan, _write_plan_locked
    root = root.resolve()
    with project_lock(root):
        source = source_snapshot(root)
        operation_id, request_key = request_identity(source, requestId)
        arguments = {'target_id': targetId, 'holder_id': holderId, 'status': status,
                     'checked_at': checkedAtId, 'reason': reason, 'evidence': evidence}
        request_hash = fingerprint({'requestKey': request_key, 'operation': 'source_check', **arguments})
        resumed = resume_request(root, source, operation_id, request_hash)
        if resumed is not None:
            return resumed
        check = _make_check(source, check_id=new_id('check'), recorded_at=utc_now(), **arguments)
        proposed = deepcopy(source)
        proposed['checks'].append(check)
        plan = _base_plan(source, operation='source_check', actor='app', parameters={'check': check},
                          impact={}, proposed=proposed)
        plan.update(operationId=operation_id, requestFingerprint=request_hash)
        return _write_plan_locked(root, plan)


def require_check_transition(current, proposed, plan):
    before, after = current['checks'], proposed['checks']
    if plan['operation'] != 'source_check':
        if before != after:
            raise StructureError('SOURCE_CHECK_IMMUTABLE', 'checks are append-only; use the check preview operation')
        return
    check = plan.get('parameters', {}).get('check')
    if not isinstance(check, dict) or set(check) != CHECK_FIELDS or after != [*before, check]:
        raise StructureError('SOURCE_CHECK_SCOPE_INVALID', 'check preview may append exactly its frozen record')
    expected = _make_check(current, check_id=check['checkId'], target_id=check['targetId'],
        holder_id=check['holderId'], status=check['status'], checked_at=check['checkedAtId'], reason=check['reason'],
        evidence=check['evidence'], recorded_at=check['recordedAt'])
    if check != expected:
        raise StructureError('SOURCE_CHECK_BINDING_STALE', 'check bindings differ from the current target or evidence')
    ignored = {'checks', 'revision', 'updatedAt', 'lastOperation'}
    if any(current.get(key) != proposed.get(key) for key in current.keys() | proposed.keys() if key not in ignored):
        raise StructureError('SOURCE_CHECK_SCOPE_INVALID', 'a check cannot also change story facts or plans')


def query_checks(root: Path, *, targetId: str, holderId: str | None = None, asOf: str | None = None,
                 limit: int | None = None, maxBytes: int | None = None, cursor: str | None = None):
    source = source_snapshot(root)
    inputs = ReviewInputs(source)
    if (not isinstance(targetId, str) or targetId not in inputs.index.records and
            not any(row['targetId'] == targetId for row in source['checks'])):
        raise StructureError('SOURCE_CHECK_TARGET_INVALID', 'select a registered target')
    if holderId is not None and (not isinstance(holderId, str) or inputs.index.owner_types.get(holderId) != 'character'):
        raise StructureError('SOURCE_CHECK_HOLDER_INVALID', 'select a character or author checks with null')
    if asOf is not None and (not isinstance(asOf, str) or inputs.index.owner_types.get(asOf) not in {'event', 'time_anchor'}):
        raise StructureError('SOURCE_CHECK_TIME_INVALID', 'asOf requires a story time point')
    rows = []
    for check in source['checks']:
        if check['targetId'] != targetId or check['holderId'] != holderId:
            continue
        match = (time_match(inputs.index, {'validDuring': {'fromId': check['checkedAtId']}}, asOf)
                 if asOf is not None and check['checkedAtId'] is not None else 'unknown')
        stale = [rid for rid, value in check['inputHashes'].items() if inputs.fingerprint(rid) != value]
        rows.append({**deepcopy(check), 'timeMatch': match, 'evidenceState': 'stale' if stale else 'current',
                     'changedInputIds': stale, 'epistemicRole': 'character_check' if holderId else 'author_check',
                     'grantsObjectiveTruth': False})
    selection = {'targetId': targetId, 'holderId': holderId, 'asOf': asOf}
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'revision': source['revision']},
                        selection=selection, code='SOURCE_CHECKS', limit=limit, maxBytes=maxBytes, cursor=cursor)
