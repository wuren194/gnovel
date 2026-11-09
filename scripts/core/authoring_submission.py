"""Durable station submissions over the existing artifact and diagnostic owners.

The request stores caller inputs, not a second workflow. Each artifact's existing
request receipt remains the authority for completed work. No approval or semantic
conclusion is synthesized, and no arbitrary operation/script can be executed.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .paths import stored_project_path
from .artifact_pack import (PACKABLE_TYPES, artifact_request_path, candidate_structure_identity,
                            bounded_authoring_report, pack_artifact)
from .artifacts import sha256_json
from .consistency import require_no_pending
from .jsonio import read_json
from .locks import project_lock_reentrant
from .structure_store import StructureError, load_structure
from .write_sets import (apply_write_set, capture_guards, digest, file_state, json_bytes,
                         safe_project_path, validate_guards)

# Dependencies determine order; caller array order does not grant lifecycle credit.
ORDER = ('stage_a_review', 'candidate_prose', 'repair_plan', 'stage_b_review',
         'state_candidate', 'title_candidates')


def submission_artifact_schema():
    from .operation_schemas import TEXT, object_schema
    from .current_candidate import selection_review_schema
    return {'type': 'object', 'oneOf': [
        object_schema({'artifactType': {'const': 'candidate_prose'}, 'text': TEXT,
                       'writingNotes': {'type': 'object'}, 'selectionRef': {'type': 'string', 'minLength': 1},
                       'selectionReview': selection_review_schema()}, ('artifactType', 'text')),
        object_schema({'artifactType': {'type': 'string', 'enum': [k for k in ORDER if k != 'candidate_prose']},
                       'payload': {'type': 'object'}}, ('artifactType', 'payload')),
        object_schema({'artifactType': {'type': 'string', 'enum': ['stage_a_review', 'stage_b_review']},
                       'formRef': {'type': 'string', 'minLength': 1},
                       'edits': {'type': 'object', 'description':
                           '只提交 review-form.fields 中实际填写的语义字段；对象按键、既有条目按原ID合并，漏项不删除。'
                           '新增B发现显式用append.findings/behaviorReviews。绑定与席位由冻结表单保管。'}},
                      ('artifactType', 'formRef', 'edits')),
    ]}


def _path(root, request_id):
    if not isinstance(request_id, str) or not request_id.strip():
        raise StructureError('SUBMISSION_REQUEST_INVALID', 'requestId must be a nonempty string')
    source = load_structure(root)
    identity = sha256_json({'projectId': source['projectId'], 'requestId': request_id})
    return safe_project_path(root, f'.gnovel/artifact-submissions/{identity}.json'), identity


def _basis(root, chapter_id, advance):
    from .derived_views import source_files
    from .static_cache import toolbox_signature
    source = load_structure(root)
    chapter = next((c for c in source['chapters'] if c['chapterId'] == chapter_id), None)
    if chapter is None or chapter['status'] == 'archived':
        raise StructureError('ARTIFACT_CHAPTER_UNKNOWN', 'submission requires an active chapter')
    sources = source_files(root)
    sources.pop('.gnovel/structure.json', None)
    # Single-source source_files covers prose citations; these other consumed
    # inputs must also remain fixed while a multi-stage submission is resumed.
    for relative in ('AGENTS.md', '.gnovel/project.json', '.gnovel/aux/story-summary.json',
                     '.gnovel/source-review.json', '.gnovel/source-review-receipts.json',
                     '.gnovel/source-rule-issues.json', chapter['filePath']):
        sources[relative] = file_state(safe_project_path(root, relative))
    return {'projectId': source['projectId'],
            'structure': candidate_structure_identity(source, chapter_id) if advance else sha256_json(source),
            'sources': sources, 'toolbox': sha256_json(toolbox_signature())}


def _validate_record(record, request_id, project_id):
    if (not isinstance(record, dict) or record.get('schemaVersion') != 1
            or record.get('requestId') != request_id or record.get('projectId') != project_id
            or record.get('recordHash') != sha256_json({k: v for k, v in record.items() if k != 'recordHash'})):
        raise StructureError('SUBMISSION_REQUEST_INVALID', 'stored submission is missing or changed')
    return record


def _registered_input(root, chapter_id, kind):
    from .finalization import _load_workflow
    workflow = _load_workflow(root)
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get(kind, {})
    if not entry or entry.get('invalidatedBy'):
        return None
    from .mechanical_evidence import _registered
    artifact = _registered(root, load_structure(root), workflow, chapter_id, kind)
    return {'artifactSha256': artifact['artifactSha256'], 'artifactFileSha256': entry.get('artifactFileSha256')}


def _target_inputs(root, chapter_id, kinds):
    from .finalization import _load_workflow
    entries = _load_workflow(root).get('artifacts', {}).get(chapter_id, {})
    return {kind: sha256_json(entries.get(kind)) for kind in kinds}


def _authorization_inputs(root, chapter_id, advance, record=None):
    from .finalization import _load_workflow
    workflow = _load_workflow(root)
    selected = deepcopy(workflow)
    selected.pop('updatedAt', None)
    selected.pop('artifacts', None)
    if advance:
        # Only this chapter's candidate/diagnostic lifecycle is performed by the
        # request. Other authorization, approvals, repair limits and chapters
        # remain exact frozen inputs.
        selected.get('chapterStates', {}).pop(chapter_id, None)
    if record is not None and record['arguments'].get('automaticRepair'):
        _, identity = _path(root, record['requestId'])
        receipt = _artifact_receipt(root, record, identity, 'candidate_prose')
        if receipt is not None:
            from .workflow import validate_automatic_repair
            accounting = receipt.get('repairAccounting')
            try:
                validate_automatic_repair(workflow, chapter_id, accounting,
                                          request_id=_child_id(identity, 'candidate_prose'))
            except ValueError as exc:
                raise StructureError('SUBMISSION_INPUTS_STALE', 'repair accounting changed outside this submission',
                                     {'reason': getattr(exc, 'code', 'REPAIR_ACCOUNTING_INVALID')}) from exc
            # Restore exactly the frozen pre-submission value for comparison.
            # Other chapters, later rounds and all approval fields remain exact.
            if accounting['previousRound'] is None:
                selected['repairRounds'].pop(chapter_id, None)
            else:
                selected['repairRounds'][chapter_id] = accounting['previousRound']
    return sha256_json(selected)


def _started_target_expectations(root, record):
    """Use each child's durable commit receipt, never its mere current filename."""
    _, identity = _path(root, record['requestId'])
    expected = deepcopy(record['targets'])
    for item in record['arguments']['artifacts']:
        kind = item['artifactType']
        path = safe_project_path(root, artifact_request_path(record['projectId'], _child_id(identity, kind)))
        if path.exists():
            _artifact_receipt(root, record, identity, kind)
            prior = read_json(path)
            states = prior.get('artifactStateHashes')
            if not isinstance(states, dict):
                raise StructureError('SUBMISSION_REQUEST_INVALID', 'child receipt lacks its committed artifact states')
            for target in expected:
                if target == kind or kind == 'candidate_prose' and target in {'stage_b_review', 'state_candidate', 'diagnostics'}:
                    expected[target] = states.get(target, sha256_json(None))
    return expected


def _dependencies(root, chapter_id, kinds, diagnostics):
    selected = set()
    if 'candidate_prose' in kinds:
        selected.add('stage_a_review')
    if kinds & {'stage_b_review', 'state_candidate', 'title_candidates', 'repair_plan'}:
        selected.add('candidate_prose')
    if 'stage_b_review' in kinds and diagnostics is None:
        selected.add('diagnostics')
    return {kind: _registered_input(root, chapter_id, kind) for kind in sorted(selected - kinds)}


def _assert_current(root, record):
    args = record['arguments']
    original_guards = capture_guards(root, ['.gnovel/structure.json', '.gnovel/workflow.json'])
    _assert_form_inputs(root, record)
    current = _basis(root, args['chapterId'], args['advanceLifecycle'])
    if current != record['basis']:
        changed = [key for key in current if current[key] != record['basis'].get(key)]
        raise StructureError('SUBMISSION_INPUTS_STALE', 'submission inputs changed; review before using a new requestId',
                             {'changedInputs': changed})
    for kind, expected in record['dependencies'].items():
        if _registered_input(root, args['chapterId'], kind) != expected:
            raise StructureError('SUBMISSION_INPUTS_STALE', 'a submitted conclusion refers to another artifact version',
                                 {'artifactType': kind})
    if _authorization_inputs(root, args['chapterId'], args['advanceLifecycle'], record) != record['authorization']:
        raise StructureError('SUBMISSION_INPUTS_STALE', 'authorization or unrelated workflow state changed')
    targets = _target_inputs(root, args['chapterId'], record['targets'])
    expected = _started_target_expectations(root, record)
    if targets != expected:
        changed = [kind for kind in targets if targets[kind] != expected[kind]]
        raise StructureError('SUBMISSION_SUPERSEDED', 'another operation changed a pending or completed submission target',
                             {'artifactTypes': changed})
    from .finalization import _load_workflow
    workflow = _load_workflow(root)
    relatives = [*current['sources'], '.gnovel/structure.json', '.gnovel/workflow.json',
                 *(row['path'] for row in record.get('formGuards', []))]
    for kind in record['dependencies']:
        entry = workflow.get('artifacts', {}).get(args['chapterId'], {}).get(kind, {})
        if entry.get('artifactPath'):
            path = stored_project_path(root, entry['artifactPath'])
            relatives.append(path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix())
    guards = capture_guards(root, relatives)
    # Recheck after capturing so a concurrent writer cannot become our new baseline.
    if _basis(root, args['chapterId'], args['advanceLifecycle']) != current:
        raise StructureError('SUBMISSION_INPUTS_STALE', 'submission inputs changed during validation')
    if (targets != _target_inputs(root, args['chapterId'], record['targets'])
            or workflow != _load_workflow(root)
            or _authorization_inputs(root, args['chapterId'], args['advanceLifecycle'], record) != record['authorization']):
        raise StructureError('SUBMISSION_INPUTS_STALE', 'submission workflow changed during validation')
    for kind, expected in record['dependencies'].items():
        if _registered_input(root, args['chapterId'], kind) != expected:
            raise StructureError('SUBMISSION_INPUTS_STALE', 'submission dependency changed during validation', {'artifactType': kind})
    _assert_lifecycle(root, record, workflow)
    _assert_form_inputs(root, record)
    validate_guards(root, original_guards, stage='artifact-submission-freeze')
    validate_guards(root, guards, stage='artifact-submission-freeze')
    return guards


def _assert_form_inputs(root, record):
    """Retain only inputs not already owned by the submission's existing basis.

    Workflow/structure changes made by completed children are still adjudicated
    by their durable artifact receipts, never by refreshing an old form.
    """
    from .prepare_dependencies import _members
    validate_guards(root, record.get('formGuards', []), stage='review-form-submission')
    for query in record.get('formQueries', []):
        if _members(root, query['directory'], query['pattern']) != query['members']:
            raise StructureError('SUBMISSION_INPUTS_STALE', 'review form input membership changed; review again')


def _resolve_forms(root, chapter_id, artifacts):
    from .prepare_dependencies import validate_dependencies
    from .review_form_edits import resolve_form
    resolved, snapshots, guards = [], [], {}
    for item in artifacts:
        if 'formRef' not in item:
            resolved.append(item)
            continue
        payload, dependencies = resolve_form(root, chapter_id=chapter_id, artifact_type=item['artifactType'],
                                             reference=item['formRef'], edits=item['edits'])
        selected = capture_guards(root, list(dependencies['files']))
        validate_dependencies(root, dependencies, code='REVIEW_FORM_STALE')
        for guard in selected:
            if guard['path'] in guards and guards[guard['path']] != guard:
                raise StructureError('REVIEW_FORM_STALE', 'review forms do not share the same frozen inputs')
            guards[guard['path']] = guard
        snapshots.append(dependencies)
        resolved.append({'artifactType': item['artifactType'], 'payload': payload})
    return resolved, snapshots, list(guards.values())


def _assert_lifecycle(root, record, workflow):
    args = record['arguments']
    if not args['advanceLifecycle']:
        return
    source = load_structure(root)
    current = {'structure': sha256_json(source),
               'chapterState': sha256_json(workflow.get('chapterStates', {}).get(args['chapterId']))}
    if current == record['lifecycle']:
        return
    _, identity = _path(root, record['requestId'])
    path = safe_project_path(root, artifact_request_path(record['projectId'], _child_id(identity, 'candidate_prose')))
    if not path.exists():
        raise StructureError('SUBMISSION_INPUTS_STALE', 'lifecycle changed before this submission registered its candidate')
    receipt = _artifact_receipt(root, record, identity, 'candidate_prose')
    child = read_json(path)
    own = {'structure': child.get('structureStateHash'), 'chapterState': child.get('chapterStateHash')}
    if current == own:
        return
    # The runner's registered report is its durable proof that the source after
    # its own draft -> review transition and this exact candidate were measured.
    if args['diagnostics'] is not None:
        from .mechanical_evidence import _registered, evidence_current
        diagnostic = _registered(root, source, workflow, args['chapterId'], 'diagnostics')
        chapter_state = workflow.get('chapterStates', {}).get(args['chapterId'], {})
        chapter = next(c for c in source['chapters'] if c['chapterId'] == args['chapterId'])
        if (diagnostic is not None and diagnostic['payload'].get('candidateArtifactSha256') == receipt['artifactSha256']
                and chapter['status'] == 'review' and chapter_state.get('status') == 'review'
                and chapter_state.get('actor') == 'diagnostics'
                and evidence_current(root, args['chapterId'], structure=source).get('fresh')):
            return
    raise StructureError('SUBMISSION_INPUTS_STALE', 'lifecycle changes are not explained by this submission receipts')


def _child_id(identity, kind):
    return f'submission:{identity}:{kind}'


def _artifact_receipt(root, record, identity, kind):
    child_id = _child_id(identity, kind)
    path = safe_project_path(root, artifact_request_path(record['projectId'], child_id))
    if not path.exists():
        return None
    prior = read_json(path)
    if (prior.get('requestId') != child_id or
            prior.get('requestHash') != sha256_json({k: v for k, v in prior.items() if k != 'requestHash'})):
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'child artifact receipt changed')
    receipt = prior['receipt']
    if receipt['chapterId'] != record['arguments']['chapterId'] or receipt['artifactType'] != kind:
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'child artifact receipt has another scope')
    if kind == 'candidate_prose' and bool(prior.get('inputIdentity', {}).get('automaticRepair')) != bool(record['arguments'].get('automaticRepair')):
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'child artifact receipt has another repair intent')
    raw = safe_project_path(root, receipt['artifactPath']).read_bytes()
    if digest(raw) != prior['artifactFileSha256']:
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'submitted artifact file changed')
    return receipt


def _response(root, record, identity, results, error=None):
    from .finalization import _load_workflow
    args = record['arguments']
    workflow = _load_workflow(root)
    entries = workflow.get('artifacts', {}).get(args['chapterId'], {})
    stages, approval_files, repair_accounting = [], [], None
    for item in args['artifacts']:
        kind = item['artifactType']
        try:
            receipt = _artifact_receipt(root, record, identity, kind)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # A damaged child is neither pending nor successfully registered.
            stages.append({'artifactType': kind, 'status': 'invalid',
                           'error': {'code': getattr(exc, 'code', 'ARTIFACT_REQUEST_INVALID'), 'message': str(exc)}})
            error = error or exc
            continue
        current = entries.get(kind, {})
        if kind == 'candidate_prose' and receipt and args.get('automaticRepair'):
            repair_accounting = receipt.get('repairAccounting')
        registered = bool(receipt and not current.get('invalidatedBy')
                          and current.get('artifactSha256') == receipt['artifactSha256'])
        stages.append({'artifactType': kind, 'status': 'registered' if registered else 'superseded' if receipt else 'pending',
                       **({'artifactPath': receipt['artifactPath'], 'artifactSha256': receipt['artifactSha256']} if receipt else {})})
        if registered:
            approval_files.append('@' + receipt['artifactPath'])
    completed = all(row['status'] == 'registered' for row in stages) and error is None
    diagnostic = next((r.get('diagnostics') for r in results if r.get('diagnostics')), None)
    if args['diagnostics'] is not None:
        completed = completed and bool(diagnostic and diagnostic.get('registered'))
        if diagnostic and diagnostic.get('registered'):
            approval_files.append('@' + diagnostic['artifactPath'])
    response = {'ok': bool(completed), 'code': 'ARTIFACTS_SUBMITTED' if completed else 'ARTIFACT_SUBMISSION_INCOMPLETE',
                'requestId': record['requestId'], 'chapterId': args['chapterId'],
                'stages': stages, 'results': results, 'submissionComplete': bool(completed),
                'approvalsPerformed': False, 'semanticReviewComplete': False,
                'resumeAction': {'operation': 'workflow.submit', 'arguments': {'requestId': record['requestId']}},
                'receiptAction': {'operation': 'project.workflow.chapter-receipt',
                                  'arguments': {'chapterId': args['chapterId'], 'view': 'model'}},
                'transactionScope': 'each artifact and diagnostics stage is atomic; completed stages are retained'}
    if repair_accounting is not None:
        response['repairAccounting'] = repair_accounting
    if error:
        response['error'] = {'code': getattr(error, 'code', 'SUBMISSION_FAILED'), 'message': str(error),
                             'details': getattr(error, 'details', {})}
    if completed:
        response['approvalAction'] = {'operation': 'project.workflow.artifact-approve',
                                     'arguments': {'chapterId': args['chapterId'], 'artifact': approval_files},
                                     'requiresExplicitApproval': True}
    return bounded_authoring_report(root, response)


def submit_artifacts(root: Path, *, requestId: str, chapterId: str | None = None,
                     artifacts: list | None = None, advanceLifecycle: bool = False,
                     diagnostics: dict | None = None, automaticRepair: bool = False) -> dict:
    root = root.expanduser().resolve()
    if type(automaticRepair) is not bool:
        raise StructureError('AUTOMATIC_REPAIR_INVALID', 'automaticRepair must be a boolean')
    with project_lock_reentrant(root):
        require_no_pending(root)
        path, identity = _path(root, requestId)
        source = load_structure(root)
        prior = _validate_record(read_json(path), requestId, source['projectId']) if path.exists() else None
        if artifacts is None:
            if prior is None or chapterId is not None or advanceLifecycle or diagnostics is not None or automaticRepair:
                raise StructureError('SUBMISSION_RESUME_INVALID', 'resume an existing submission with requestId only')
            record = prior
        else:
            from .argument_validation import validate_arguments
            from .operation_schemas import DIAGNOSTICS
            validate_arguments({'type': 'array', 'minItems': 1, 'maxItems': len(PACKABLE_TYPES),
                                'items': submission_artifact_schema()}, artifacts)
            if diagnostics is not None:
                validate_arguments(DIAGNOSTICS, diagnostics)
            kinds = [row['artifactType'] for row in artifacts]
            if automaticRepair and 'candidate_prose' not in kinds:
                raise StructureError('AUTOMATIC_REPAIR_INVALID', 'automatic repair requires candidate prose')
            if len(kinds) != len(set(kinds)) or 'stage_a_review' in kinds and len(kinds) != 1:
                raise StructureError('SUBMISSION_STAGE_INVALID', 'each artifact type occurs once; stage A is submitted before later stages')
            if type(advanceLifecycle) is not bool or (advanceLifecycle or diagnostics is not None) and 'candidate_prose' not in kinds:
                raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'lifecycle and diagnostics require candidate prose')
            if diagnostics is not None and not advanceLifecycle:
                raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'diagnostics requires advanceLifecycle')
            ordered = sorted(deepcopy(artifacts), key=lambda row: ORDER.index(row['artifactType']))
            args = {'chapterId': chapterId, 'artifacts': ordered,
                    'advanceLifecycle': advanceLifecycle, 'diagnostics': diagnostics,
                    **({'automaticRepair': True} if automaticRepair else {})}
            if prior:
                same = (prior['callerArgumentsSha256'] == sha256_json(args)
                        if 'callerArgumentsSha256' in prior else prior['arguments'] == args)
                if not same:
                    raise StructureError('SUBMISSION_REQUEST_REUSED', 'requestId identifies different content or options')
                record = prior
            else:
                caller_hash = sha256_json(args)
                resolved, forms, form_guards = _resolve_forms(root, chapterId, ordered)
                args = {**args, 'artifacts': resolved}
                record = {'schemaVersion': 1, 'projectId': source['projectId'], 'requestId': requestId,
                          'arguments': args, 'basis': _basis(root, chapterId, advanceLifecycle),
                          'dependencies': _dependencies(root, chapterId, set(kinds), diagnostics),
                          'targets': _target_inputs(root, chapterId, kinds),
                          'authorization': _authorization_inputs(root, chapterId, advanceLifecycle)}
                if forms:
                    # Store the resolved payload exactly once. Its original
                    # formRef+edits identity is a digest, not a second copy of
                    # the station's judgments. Cache eviction cannot rebind it.
                    record['callerArgumentsSha256'] = caller_hash
                    covered = {*record['basis']['sources'], '.gnovel/structure.json', '.gnovel/workflow.json'}
                    record['formGuards'] = [row for row in form_guards if row['path'] not in covered]
                    record['formQueries'] = []
                    for snapshot in forms:
                        for query in snapshot['queries']:
                            if query not in record['formQueries']:
                                record['formQueries'].append(query)
                if advanceLifecycle:
                    from .finalization import _load_workflow
                    record['lifecycle'] = {'structure': sha256_json(source),
                        'chapterState': sha256_json(_load_workflow(root).get('chapterStates', {}).get(chapterId))}
                guards = _assert_current(root, record)
                if forms:
                    from .prepare_dependencies import validate_dependencies
                    for snapshot in forms:
                        validate_dependencies(root, snapshot, code='REVIEW_FORM_STALE')
                    validate_guards(root, form_guards, stage='review-form-freeze')
                    guards = [*guards, *form_guards]
                record['recordHash'] = sha256_json(record)
                apply_write_set(root, {path.relative_to(root).as_posix(): json_bytes(record)},
                                operation='artifact_submission', guards=guards)
        args, results = record['arguments'], []
        try:
            guards = _assert_current(root, record)
            # Validate every already-completed member before writing another one.
            for item in args['artifacts']:
                kind = item['artifactType']
                receipt = _artifact_receipt(root, record, identity, kind)
                if receipt is not None:
                    current = _registered_input(root, args['chapterId'], kind)
                    if current is None or current['artifactSha256'] != receipt['artifactSha256']:
                        raise StructureError('SUBMISSION_SUPERSEDED', 'a submitted artifact was replaced; this request cannot restore it',
                                             {'artifactType': kind})
            for item in args['artifacts']:
                kind = item['artifactType']
                guards = _assert_current(root, record)
                result = pack_artifact(root, chapterId=args['chapterId'], **item,
                    requestId=_child_id(identity, kind), _stage_guards=guards, _full_report=True,
                    advanceLifecycle=args['advanceLifecycle'] if kind == 'candidate_prose' else False,
                    automaticRepair=args.get('automaticRepair', False) if kind == 'candidate_prose' else False,
                    diagnostics=args['diagnostics'] if kind == 'candidate_prose' else None)
                results.append(result)
                if not result.get('ok') or not result.get('registered'):
                    raise StructureError(result.get('code', 'SUBMISSION_FAILED'), 'submission stopped at an incomplete artifact stage',
                                         {'artifactType': kind, 'result': result})
                # The child transaction revalidates its original guards before
                # commit. Its own lifecycle/workflow changes are now legitimate;
                # the next child re-derives progress from durable child receipts.
                _assert_current(root, record)
        except (ValueError, OSError, RuntimeError) as exc:
            return _response(root, record, identity, results, error=exc)
        return _response(root, record, identity, results)
