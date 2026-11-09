"""Typed, resumable chapter closeout over the existing domain transactions.

The request freezes caller text and the inspected version. Stage receipts are
written by their domain owner in the same write set as the work they attest to.
They are execution receipts, never an alternative chapter state or approval.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .artifacts import sha256_json
from .consistency import require_no_pending
from .jsonio import read_json
from .locks import project_lock_reentrant
from .structure_store import StructureError, load_structure
from .transport_cache import get, put
from .write_sets import (apply_write_set, capture_guards, file_state, inventory,
                         json_bytes, safe_project_path, validate_guards)


def _action(root, operation, **arguments):
    return {'operation': operation, 'arguments': {'projectRoot': str(root), **arguments}}


def _toolbox():
    from .static_cache import toolbox_signature
    return sha256_json(toolbox_signature())


def _require_formal_prose(root, chapter_id):
    from .prose_binding import OWNED_CLEAN, require_observation
    structure = load_structure(root)
    chapter = next(row for row in structure['chapters'] if row['chapterId'] == chapter_id)
    require_observation(root, structure, chapter, (OWNED_CLEAN,), action='chapter closeout')


def _basis(root):
    from .prepare_dependencies import capture_dependencies
    dependencies = capture_dependencies(root)
    return {relative: file_state(safe_project_path(root, relative))
            for relative in dependencies['files']}


def _validate_basis(root, basis, toolbox):
    require_no_pending(root)
    changed = [path for path, expected in basis.items()
               if file_state(safe_project_path(root, path)) != expected]
    if changed or toolbox != _toolbox():
        raise StructureError('CLOSEOUT_INPUTS_STALE',
                             'closeout inputs changed; inspect and prepare a new request',
                             {'changedPaths': changed, 'toolboxChanged': toolbox != _toolbox()})
    guards = capture_guards(root, list(basis))
    if any(file_state(safe_project_path(root, path)) != expected for path, expected in basis.items()):
        raise StructureError('CLOSEOUT_INPUTS_STALE', 'closeout inputs changed during validation')
    return guards


def _unused_request_id(root, snapshot):
    """A repeated unstarted preparation offers one ID; consumed IDs stay frozen.

    A stable chapter may legitimately save a no-op request while awaiting a
    summary/approval decision. That does not change its source basis, but later
    new inputs still need a new request rather than amending the old one.
    """
    prefix = 'closeout-' + sha256_json(snapshot)
    candidate, suffix = prefix, 0
    while True:
        path = safe_project_path(root, _path(root, candidate))
        if not path.exists() and not path.is_symlink():
            return candidate
        suffix += 1
        candidate = prefix + '-' + str(suffix)


def prepare_closeout(root: Path, *, chapterId: str, titleCandidateId=None, skipTitle=False,
                     factRequestIds=None):
    from .chapter_receipt import chapter_receipt
    from .continuity import continuity_preview
    root = root.expanduser().resolve()
    require_no_pending(root)
    basis, toolbox = _basis(root), _toolbox()
    # Internal consumers need the complete same-call blocker list. A public
    # model receipt can be paged; that transport wrapper is not domain state.
    receipt = chapter_receipt(root, chapterId, title_candidate_id=titleCandidateId,
                              skip_title=skipTitle, view='model', _bounded=False)
    status = receipt['chapter']['status']
    blockers = deepcopy(receipt['blockers'])
    if status not in {'review', 'stable'}:
        blockers.append({'code': 'CLOSEOUT_CHAPTER_NOT_READY', 'message': 'finish chapter review first'})
    continuity = None
    if status == 'stable':
        try:
            continuity = continuity_preview(root, chapterId)
        except (ValueError, OSError) as exc:
            blockers.append({'code': getattr(exc, 'code', 'CONTINUITY_UNAVAILABLE'),
                             'message': str(exc), 'details': {**getattr(exc, 'details', {}),
                                                           'chapterId': chapterId, 'assessment': 'unknown'}})
    if continuity:
        blockers.extend(continuity['blockers'])
    if status == 'stable':
        try:
            _require_formal_prose(root, chapterId)
        except (ValueError, OSError) as exc:
            blockers.append({'code': getattr(exc, 'code', 'CLOSEOUT_PROSE_UNAVAILABLE'),
                             'message': str(exc), 'details': getattr(exc, 'details', {})})
    fact_ids = [] if factRequestIds is None else factRequestIds
    if (not isinstance(fact_ids, list) or any(not isinstance(rid, str) or not rid.strip() for rid in fact_ids)
            or len(fact_ids) != len(set(fact_ids))):
        raise StructureError('CLOSEOUT_FACTS_INVALID', 'factRequestIds must contain distinct explicit request IDs')
    facts = _facts(root, chapterId, fact_ids)
    from .finalization import _load_workflow
    from .fact_inventory import inventory_status
    structure, workflow = load_structure(root), _load_workflow(root)
    if status == 'stable':
        try:
            fact_inventory = inventory_status(root, structure, workflow, chapterId)
        except (ValueError, OSError) as exc:
            fact_inventory = {'status': 'unknown', 'assessed': False, 'complete': False}
            blockers.append({'code': getattr(exc, 'code', 'FACT_INVENTORY_UNAVAILABLE'),
                             'message': str(exc), 'details': {**getattr(exc, 'details', {}),
                                                           'chapterId': chapterId, 'assessment': 'unknown'}})
    else:
        # Preflight already inspected each file independently. Reopening a bad B
        # here used to raise and discard unrelated title/ownership/approval errors.
        review = next((row for row in receipt['artifacts'] if row['artifactType'] == 'stage_b_review'), {})
        readable = review.get('fresh') and review.get('envelopeValid') and not review.get('invalidated')
        assessment = (review.get('summary', {}).get('factAssessment', {}).get('status', 'unassessed')
                      if readable else 'unknown' if review.get('registered') else 'unassessed')
        fact_inventory = {'status': assessment, 'assessed': assessment in {'no_change', 'changes'}, 'complete': False}
    _validate_basis(root, basis, toolbox)
    snapshot = {'projectId': load_structure(root)['projectId'], 'chapterId': chapterId,
                'basis': basis, 'toolbox': toolbox, 'finalizeAction': receipt['finalizeAction'],
                'initialStatus': status, 'continuityComplete': receipt['continuity'].get('status') == 'complete',
                'blockers': blockers, 'factRequestIds': fact_ids}
    reference = put(root, 'chapter-closeout', snapshot)
    request_id = _unused_request_id(root, snapshot) if not blockers else None
    run_action = _action(root, 'workflow.closeout', prepareRef=reference, requestId=request_id) if not blockers else None
    if run_action is not None:
        run_action.update(effects='protected-write', executable=False,
                          execution='existing_authorization_required', approvalGranted=False)
    return {'ok': not blockers, 'code': 'CLOSEOUT_PREPARED', 'chapterId': chapterId,
            'prepareRef': reference, 'blockers': blockers, 'receipt': receipt,
            'guidance': {**receipt['guidance'], 'blockerCount': len(blockers)},
            'facts': facts, 'factsAssessed': fact_inventory['assessed'], 'factInventory': fact_inventory,
            'steps': ['finalize' if status == 'review' else 'already-finalized',
                      'caller-summary-disposition', 'explicit-continuity-confirmation',
                      'declared-fact-receipts', 'next-task-preparation'],
            'inputGuide': {
                'summary': '显式提供 not_required/refreshed 和实际 reason；refreshed 的 hash 由工具绑定已读版本。',
                'summaryUpdate': '或提供 storySummary、按稳定 ID 的条目增量及实际 reason；工具生成摘要机器字段。',
                'approveContinuity': '只有作者明确批准此次连续性确认时才传 true；不批准任何审校工件。',
                'facts': 'factsAssessed 来自 B 盘点；空 factRequestIds 不能清除已知待办。事实预览在连续性完成后执行，交接仍须全部待办完成。'},
            'runAction': run_action,
            'summaryAction': _action(root, 'summary.prepare'),
            'approvalPerformed': False}


def _path(root, request_id):
    if not isinstance(request_id, str) or not request_id.strip():
        raise StructureError('CLOSEOUT_REQUEST_INVALID', 'requestId must be nonempty')
    identity = sha256_json({'projectId': load_structure(root)['projectId'], 'requestId': request_id})
    return f'.govel/closeout-requests/{identity}.json'


def _seal(record):
    record = deepcopy(record)
    record.pop('recordHash', None)
    record['recordHash'] = sha256_json(record)
    return record


def _load(root, request_id):
    record = read_json(safe_project_path(root, _path(root, request_id)))
    if (record.get('schemaVersion') != 1 or record.get('requestId') != request_id
            or record.get('projectId') != load_structure(root)['projectId']
            or record.get('recordHash') != sha256_json({k: v for k, v in record.items() if k != 'recordHash'})):
        raise StructureError('CLOSEOUT_REQUEST_INVALID', 'closeout request is missing or altered')
    return record


def receipt_outputs(root, request_id, stage, outputs, evidence, *, expected=None):
    """Called only by the three domain owners before their atomic write set."""
    if stage not in {'finalize', 'summary', 'continuity'}:
        raise StructureError('CLOSEOUT_STAGE_INVALID', 'unknown closeout stage')
    relative = _path(root, request_id)
    request_before = file_state(safe_project_path(root, relative))
    request_guards = capture_guards(root, [relative])
    record = _load(root, request_id)
    guards = _validate_basis(root, record['expected'], record['toolbox'])
    if stage in record['receipts']:
        raise StructureError('CLOSEOUT_STAGE_REPEATED', 'domain work already has a durable receipt')
    expected = inventory(root, outputs) if expected is None else deepcopy(expected)
    for row in expected:
        if row['path'] in record['expected']:
            if row['before'] != record['expected'][row['path']]:
                raise StructureError('CLOSEOUT_INPUTS_STALE', 'domain write no longer matches the frozen request',
                                     {'path': row['path']})
            row['before'] = record['expected'][row['path']]
        record['expected'][row['path']] = row['after']
    record['receipts'][stage] = deepcopy(evidence)
    receipt = {relative: json_bytes(_seal(record))}
    receipt_rows = inventory(root, receipt)
    receipt_rows[0]['before'] = request_before
    validate_guards(root, request_guards, stage='closeout-receipt')
    return receipt, [row for row in guards if row['path'] not in outputs], [*expected, *receipt_rows]


def _validate_inputs(summary, summary_update, approve):
    from .continuity import _reason
    if type(approve) is not bool:
        raise StructureError('CLOSEOUT_APPROVAL_INVALID', 'approveContinuity must be an explicit boolean')
    if summary is not None and summary_update is not None:
        raise StructureError('CLOSEOUT_SUMMARY_INVALID', 'select summary or summaryUpdate, not both')
    if summary is not None:
        if (not isinstance(summary, dict) or set(summary) != {'resolution', 'reason'}
                or summary.get('resolution') not in {'refreshed', 'not_required'} or not _reason(summary.get('reason'))):
            raise StructureError('CLOSEOUT_SUMMARY_INVALID', 'summary needs an explicit resolution and actual reason')
    if summary_update is not None:
        if (not isinstance(summary_update, dict) or not {'storySummary', 'reason'} <= set(summary_update)
                or set(summary_update) - {'storySummary', 'plotLine', 'characterSummaries', 'reason'}
                or not _reason(summary_update.get('reason'))
                or not isinstance(summary_update.get('storySummary'), str) or not summary_update['storySummary'].strip()):
            raise StructureError('CLOSEOUT_SUMMARY_INVALID', 'summaryUpdate needs caller-authored summary and actual review reason')


def closeout_chapter(root: Path, *, requestId: str, prepareRef=None, summary=None, summaryUpdate=None,
                     approveContinuity=False, prepareNext=True):
    root = root.expanduser().resolve()
    with project_lock_reentrant(root):
        relative = _path(root, requestId)
        target = safe_project_path(root, relative)
        arguments = {'prepareRef': prepareRef, 'summary': summary, 'summaryUpdate': summaryUpdate,
                     'approveContinuity': approveContinuity, 'prepareNext': prepareNext}
        if target.exists():
            record = _load(root, requestId)
            resume_only = arguments == {'prepareRef': None, 'summary': None, 'summaryUpdate': None,
                                        'approveContinuity': False, 'prepareNext': True}
            if not resume_only and arguments != record['arguments']:
                raise StructureError('CLOSEOUT_REQUEST_REUSED', 'requestId identifies different closeout inputs')
            _validate_basis(root, record['expected'], record['toolbox'])
        else:
            _validate_inputs(summary, summaryUpdate, approveContinuity)
            if type(prepareNext) is not bool:
                raise StructureError('CLOSEOUT_INPUT_INVALID', 'prepareNext must be boolean')
            snapshot = get(root, prepareRef, kind='chapter-closeout')
            guards = _validate_basis(root, snapshot['basis'], snapshot['toolbox'])
            if snapshot['blockers']:
                raise StructureError('CLOSEOUT_BLOCKED', 'resolve preparation blockers first', {'blockers': snapshot['blockers']})
            if snapshot['continuityComplete'] and (summary is not None or summaryUpdate is not None or approveContinuity):
                raise StructureError('CLOSEOUT_ALREADY_CONFIRMED', 'completed continuity cannot receive a new summary confirmation')
            if approveContinuity and summary is None and summaryUpdate is None:
                raise StructureError('CLOSEOUT_SUMMARY_REQUIRED', 'explicit summary disposition is required before confirmation')
            record = {'schemaVersion': 1, 'requestId': requestId, **snapshot,
                      'arguments': deepcopy(arguments), 'expected': snapshot['basis'], 'receipts': {}}
            outputs = {relative: json_bytes(_seal(record))}
            expected = inventory(root, outputs)
            expected[0]['before'] = {'exists': False, 'sha256': None, 'mode': None}
            apply_write_set(root, outputs, guards=guards, expected=expected,
                            operation='closeout_request')
        try:
            _execute(root, requestId)
            return _report(root, requestId)
        except (ValueError, OSError) as exc:
            from .artifact_pack import bounded_authoring_report
            record = _load(root, requestId)
            return bounded_authoring_report(root, {'ok': False, 'code': 'CLOSEOUT_INCOMPLETE',
                'requestId': requestId, 'chapterId': record['chapterId'],
                'completedStages': list(record['receipts']),
                'error': {'code': getattr(exc, 'code', 'CLOSEOUT_STEP_FAILED'), 'message': str(exc),
                          'details': getattr(exc, 'details', {})},
                'resumeAction': _action(root, 'workflow.closeout', requestId=requestId),
                'approvalPerformed': 'continuity' in record['receipts']})


def _execute(root, request_id):
    from .finalization import _finalize_chapter_locked
    from .continuity import continuity_preview, complete_continuity
    from .summary_updates import commit_delta, SUMMARY_PATH
    record = _load(root, request_id)
    args, cid = record['arguments'], record['chapterId']
    if record['initialStatus'] == 'review' and 'finalize' not in record['receipts']:
        action = record['finalizeAction']['arguments']
        _finalize_chapter_locked(root, cid, title_candidate_id=action.get('titleCandidateId'),
                                skip_title=action.get('skipTitle', False),
                                binding_generation=action['bindingGeneration'], _closeout_request=request_id)
        record = _load(root, request_id)
    if args['summaryUpdate'] is not None and 'summary' not in record['receipts']:
        import importlib
        module = importlib.import_module('scripts.story_summarizer' if __package__.startswith('scripts.') else 'story_summarizer')
        prepared = module._prepare_context(root, view='model')
        update = {k: v for k, v in args['summaryUpdate'].items() if k != 'reason'}
        commit_delta(root, prepareRef=prepared['prepareRef'], **update,
                     reviewReason=args['summaryUpdate']['reason'], _closeout_request=request_id)
        record = _load(root, request_id)
    if args['approveContinuity'] and 'continuity' not in record['receipts']:
        preview = continuity_preview(root, cid)
        summary = deepcopy(args['summary']) if args['summary'] is not None else deepcopy(record['receipts']['summary']['continuitySummary'])
        if summary['resolution'] == 'refreshed':
            summary['sha256'] = file_state(safe_project_path(root, SUMMARY_PATH))['sha256']
        # Current source projects have no legacy state-change rows. An unexpected
        # nonempty batch must never receive machine-generated semantic decisions.
        if preview['batch']['items']:
            raise StructureError('CLOSEOUT_DECISIONS_REQUIRED', 'provide explicit per-item continuity decisions through its domain entry')
        complete_continuity(root, cid, batch_hash=preview['batchHash'],
                            confirmation={'decisions': [], 'summary': summary}, approved=True,
                            _closeout_request=request_id)


def _facts(root, chapter_id, request_ids):
    from .chapter_facts import complete_chapter_facts
    reports = []
    for request_id in request_ids:
        report = complete_chapter_facts(root, requestId=request_id)
        if report.get('chapterId') != chapter_id:
            raise StructureError('CLOSEOUT_FACTS_INVALID', 'declared facts belong to a different chapter')
        reports.append(report)
    return reports


def _report(root, request_id):
    from .artifact_pack import bounded_authoring_report
    from .continuity import continuity_preview
    record = _load(root, request_id)
    _validate_basis(root, record['expected'], record['toolbox'])
    cid = record['chapterId']
    _require_formal_prose(root, cid)
    continuity = continuity_preview(root, cid)
    facts = _facts(root, cid, record['factRequestIds'])
    from .fact_inventory import inventory_status
    from .finalization import _load_workflow
    fact_inventory = inventory_status(root, load_structure(root), _load_workflow(root), cid)
    ready = continuity['continuityComplete'] and fact_inventory['complete'] and all(row.get('complete') for row in facts)
    report = {'ok': True, 'code': 'CLOSEOUT_COMPLETED' if ready else 'CLOSEOUT_INPUT_REQUIRED',
              'requestId': request_id, 'chapterId': cid, 'completedStages': list(record['receipts']),
              'receipts': record['receipts'], 'continuity': continuity,
              'facts': facts, 'factsAssessed': fact_inventory['assessed'], 'factInventory': fact_inventory,
              'readyForContinuation': ready, 'artifactApprovalPerformed': False,
              'approvalPerformed': 'continuity' in record['receipts'],
              'resumeAction': _action(root, 'workflow.closeout', requestId=request_id)}
    if ready:
        report['nextPrepareAction'] = _action(root, 'task.prepare', task='continue', view='model')
        if record['arguments']['prepareNext']:
            from .task_prepare import prepare_task
            from .context_transport import task_model_response
            try:
                report['nextPreparation'] = task_model_response(prepare_task(root, 'continue'))
            except (ValueError, OSError) as exc:
                code = getattr(exc, 'code', 'NEXT_PREPARATION_FAILED')
                report['nextPreparation'] = {'available': False, 'code': code,
                    'message': str(exc), 'details': getattr(exc, 'details', {})}
                if code == 'WORKFLOW_NO_NEXT_ACTION':
                    report['waitingForAuthorization'] = True
                    report.pop('nextPrepareAction')
                else:
                    report['nextPreparationError'] = code
    else:
        report['missingInputs'] = (['continuity approval and explicit summary disposition']
                                   if not continuity['continuityComplete'] else ['B fact assessment and all retained source/evidence obligations'])
        report['prepareAction'] = _action(root, 'workflow.closeout-prepare', chapterId=cid,
                                         factRequestIds=record['factRequestIds'])
        if not continuity['continuityComplete']:
            report['summaryAction'] = _action(root, 'summary.prepare')
    return bounded_authoring_report(root, report)
