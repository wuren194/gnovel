"""Request identity and resume state derived from existing structure plans and journals."""
from __future__ import annotations

import time
from pathlib import Path

from .derived_views import renderer_fingerprint, source_files
from .jsonio import read_json, write_json
from .source_reports import public_report
from .source_review import fingerprint, source_snapshot
from .structure_commands import _operation_paths, _plan_hash
from .structure_store import StructureError, structure_hash
from .write_sets import safe_project_path, validate_guards


def request_identity(source: dict, request_id: str) -> tuple[str, str]:
    if not isinstance(request_id, str) or not request_id.strip():
        raise StructureError('SOURCE_REQUEST_INVALID', 'nonempty requestId required')
    key = fingerprint({'projectId': source['projectId'], 'requestId': request_id})
    return 'OP-' + key[:16].upper(), key


def operation_undone(root: Path, operation_id: str) -> bool:
    directory = safe_project_path(root, '.govel/undo-receipts')
    return directory.is_dir() and any(directory.glob(operation_id + '-*.json'))


def stale_reasons(root: Path, source: dict, prior: dict) -> list[str]:
    from .write_sets import validate_inventory
    from .structure_transaction import _load_outputs
    from .source_maintenance_inputs import validate_maintenance_binding
    reasons = []
    if source['revision'] != prior['baseRevision'] or structure_hash(source) != prior['baseHash']:
        reasons.append('source_changed')
    if time.time() > prior['expiresAtEpoch']:
        reasons.append('expired')
    if prior.get('sourceFiles') != source_files(root):
        reasons.append('source_files_changed')
    if prior.get('rendererFingerprint') != renderer_fingerprint():
        reasons.append('renderer_changed')
    try:
        validate_maintenance_binding(root, prior)
        from .external_review_materials import validate_external_materials
        validate_external_materials(root, prior, stage='external-request-status')
        validate_guards(root, prior.get('maintenanceGuards', []), stage='request-status')
        validate_guards(root, prior.get('chapterFactGuards', []), stage='request-status')
        validate_guards(root, prior.get('organizationGuards', []), stage='request-status')
        validate_guards(root, prior.get('manuscriptGuards', []), stage='request-status')
        from .write_sets import validate_guard_groups
        validate_guard_groups(prior.get('manuscriptGuardGroups', []), stage='request-status')
        if 'writeSet' not in prior:
            raise StructureError('PLAN_REPREVIEW_REQUIRED', 'plan has no frozen write set')
        validate_inventory(root, prior['writeSet'])
        _load_outputs(root, prior)
    except (StructureError, OSError) as exc:
        reasons.append(getattr(exc, 'code', 'staged_files_unavailable'))
    return reasons


def request_state(root: Path, source: dict, operation_id: str) -> tuple[str, dict | None]:
    plan_path, journal_path, _ = _operation_paths(root, operation_id)
    if not journal_path.is_file() and not plan_path.is_file():
        return 'not_started', None
    committed = journal_path.is_file()
    prior = read_json(journal_path if committed else plan_path)
    if not isinstance(prior, dict) or prior.get('operationId') != operation_id or not prior.get('requestFingerprint'):
        raise StructureError('SOURCE_REQUEST_INVALID', 'operation is not a valid request-bound record')
    if committed:
        return ('undone' if operation_undone(root, operation_id) else 'committed'), prior
    if prior.get('planHash') != _plan_hash({k: v for k, v in prior.items() if k != 'planHash'}):
        raise StructureError('PLAN_HASH_MISMATCH', 'stored request plan was changed')
    return ('stale' if stale_reasons(root, source, prior) else 'prepared'), prior


def _renew(root: Path, source: dict, prior: dict) -> dict:
    """Renew only the deadline; keep the exact staged bytes and replay all semantic gates."""
    from .structure_commands import PLAN_TTL_SECONDS
    from .structure_transaction import _enforce_transaction_boundary
    from .canon_guard import require_canon_proposal
    from .source_model import require_current_source
    reasons = stale_reasons(root, source, prior)
    if reasons != ['expired']:
        raise StructureError('SOURCE_REQUEST_STALE', 'inputs changed; reread before creating a new request',
                             {'staleReasons': reasons, 'renewable': False})
    require_current_source(source)
    require_canon_proposal(source, prior['proposedStructure'], prior)
    _enforce_transaction_boundary(prior, source, prior['proposedStructure'], root=root)
    renewed = {**prior, 'expiresAtEpoch': int(time.time()) + PLAN_TTL_SECONDS}
    renewed['planHash'] = _plan_hash({k: v for k, v in renewed.items() if k != 'planHash'})
    plan_path, _, _ = _operation_paths(root, prior['operationId'])
    write_json(plan_path, renewed)
    return public_report(renewed, root=root)


def resume_request(root: Path, source: dict, operation_id: str, request_fingerprint: str) -> dict | None:
    state, prior = request_state(root, source, operation_id)
    if prior is None:
        return None
    if prior['requestFingerprint'] != request_fingerprint:
        raise StructureError('SOURCE_REQUEST_REUSED', 'requestId already identifies a different payload')
    if state == 'stale':
        return _renew(root, source, prior)
    if state in {'committed', 'undone'}:
        return public_report({**prior, 'ok': True, 'code': 'SOURCE_ALREADY_COMMITTED', 'alreadyCommitted': True,
                              'operationState': state, 'historical': True, 'currentRevision': source['revision']}, root=root)
    return public_report(prior, root=root)


def renew_request(root: Path, *, requestId: str) -> dict:
    from .locks import project_lock
    root = root.expanduser().resolve()
    with project_lock(root):
        source = source_snapshot(root)
        operation_id, _ = request_identity(source, requestId)
        state, prior = request_state(root, source, operation_id)
        if prior is None:
            raise StructureError('PLAN_NOT_FOUND', 'request has no saved preview')
        if state in {'committed', 'undone'}:
            return resume_request(root, source, operation_id, prior['requestFingerprint'])
        return _renew(root, source, prior) if state == 'stale' else public_report(prior, root=root)


def query_request_status(root: Path, *, requestId: str) -> dict:
    source = source_snapshot(root)
    operation_id, _ = request_identity(source, requestId)
    state, prior = request_state(root, source, operation_id)
    reasons = stale_reasons(root, source, prior) if state == 'stale' else []
    renewable = reasons == ['expired']
    report = public_report(prior, root=root) if prior is not None else {}
    unavailable = report.get('execution', {}).get('executable') is False
    return {'ok': True, 'code': 'SOURCE_REQUEST_STATUS', 'requestId': requestId, 'operationId': operation_id,
            'operationState': state, 'currentRevision': source['revision'],
            'staleReasons': reasons, 'renewable': renewable,
            'nextAction': ({'operation': 'source.request-renew', 'tool': 'renew_source_request',
                            'arguments': {'projectRoot': str(root.resolve()), 'requestId': requestId}}
                           if renewable else report.get('nextAction') if state == 'prepared' else None),
            **({'execution': report['execution'], 'materialsAction': report.get('diffAction'),
                **({'approvalAction': report['approvalAction']} if report.get('approvalAction') else {}),
                'externalReviewMaterials': report.get('externalReviewMaterials')} if unavailable else {}),
            'planHash': prior['planHash'] if state == 'prepared' else None,
            'afterRevision': prior.get('afterRevision') if prior else None,
            'nextStep': {'not_started': 'preview_saved_request', 'prepared':
                         ('confirm_external_on_device' if report.get('approvalAction') else 'read_frozen_external_materials')
                         if unavailable else 'commit_existing_plan',
                         'stale': 'renew_existing_preview' if renewable else 'reread_inputs_and_use_new_request_id', 'committed': 'check_current_reviews',
                         'undone': 'do_not_reapply_without_new_request'}[state]}
