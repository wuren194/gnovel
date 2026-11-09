"""Apply a batch of explicit anchored repairs to one registered candidate."""
from __future__ import annotations

import json
from pathlib import Path

from .paths import stored_project_path
from .artifact_pack import pack_artifact
from .artifacts import sha256_json, sha256_text
from .edit_anchors import VERSION_KEYS, read_anchor, resolve_anchor
from .evidence_quotes import quote_span
from .locks import project_lock_reentrant
from .review_artifacts import validate_artifact_binding
from .structure_store import StructureError
from .write_sets import guard_observation, safe_project_path


def patch_candidate(root: Path, *, patches: list | None = None, chapterId: str | None = None,
                    candidateSha256: str | None = None, credential: str | None = None,
                    requestId: str | None = None, advanceLifecycle: bool = False,
                    diagnostics: dict | None = None, automaticRepair: bool = False) -> dict:
    root = root.expanduser().resolve()
    if type(automaticRepair) is not bool:
        raise StructureError('AUTOMATIC_REPAIR_INVALID', 'automaticRepair must be a boolean')
    if automaticRepair and (not isinstance(requestId, str) or not requestId.strip()):
        raise StructureError('AUTOMATIC_REPAIR_REQUEST_REQUIRED', 'automatic repair requires a requestId')
    if patches is None:
        if any(value is not None for value in (chapterId, candidateSha256, credential, diagnostics)) or advanceLifecycle or automaticRepair:
            raise StructureError('PATCH_RESUME_INVALID', 'resume with requestId only, without changing the frozen inputs')
        arguments = _resume_arguments(root, requestId)
        return patch_candidate(root, **arguments)
    original_arguments = {'chapterId': chapterId, 'candidateSha256': candidateSha256,
                          'credential': credential, 'diagnostics': diagnostics,
                          **({'automaticRepair': True} if automaticRepair else {})}
    request_hash = sha256_json({'chapterId': chapterId, 'candidateSha256': candidateSha256,
                               'credential': credential, 'patches': patches,
                               'advanceLifecycle': advanceLifecycle, 'diagnostics': diagnostics,
                               **({'automaticRepair': True} if automaticRepair else {})})
    with project_lock_reentrant(root):
        if requestId is not None:
            replay = _replay_patch(root, requestId, request_hash, advanceLifecycle, diagnostics, automaticRepair)
            if replay is not None:
                return replay
        if not isinstance(patches, list) or not patches:
            raise StructureError('PATCH_INVALID', 'provide a nonempty batch of explicit repairs')
        anchors = []
        anchor_version = None
        for row in patches:
            if (not isinstance(row, dict) or not {'issueId', 'replacement'} <= set(row)
                    or not isinstance(row['issueId'], str) or not row['issueId'].strip()
                    or not isinstance(row['replacement'], str)
                    or not (set(row) == {'issueId', 'original', 'replacement'}
                            or {'issueId', 'anchorRef', 'replacement'} <= set(row)
                            <= {'issueId', 'anchorRef', 'replacement', 'range'})
                    or 'range' in row and row['range'] is None):
                raise StructureError('PATCH_INVALID', 'each repair requires issueId, replacement and either original or anchorRef')
            bound = read_anchor(root, row['anchorRef']) if 'anchorRef' in row else None
            anchors.append(bound)
            if bound is not None:
                version = {key: bound[key] for key in VERSION_KEYS}
                if anchor_version is not None:
                    if any(version[key] != anchor_version[key] for key in ('projectId', 'chapterId')):
                        raise StructureError('EDIT_ANCHOR_SCOPE_MISMATCH', 'all anchors must select the same project and chapter')
                    if version != anchor_version:
                        raise StructureError('PATCH_BASE_STALE', 'all anchors must select the same candidate version')
                anchor_version = version
        if credential is not None:
            from .reading_handoff import decode_receipt
            from .task_prepare import prepare_task
            scope = decode_receipt(credential, root).get('scope', {})
            arguments = scope.get('arguments', {})
            if (scope.get('root') != str(root) or arguments.get('task') not in {'editor', 'repair'}
                    or not arguments.get('chapterId') or not scope.get('candidateSha256')
                    or candidateSha256 is not None or chapterId not in {None, arguments['chapterId']}):
                raise StructureError('PATCH_CREDENTIAL_INVALID', 'use this chapter editor/repair preparation credential')
            prepared = prepare_task(root, arguments['task'], chapter_id=arguments['chapterId'],
                                    pov_character=arguments.get('povCharacter'), chapter_goal=arguments.get('chapterGoal', ''),
                                    knowledge_mode=arguments.get('knowledgeMode'))
            if scope != decode_receipt(prepared['reading']['credential'], root)['scope']:
                raise StructureError('PATCH_BASE_STALE', 'prepared candidate or task inputs changed')
            chapterId, candidateSha256 = arguments['chapterId'], scope['candidateSha256']
        if anchor_version is not None:
            if chapterId is not None and chapterId != anchor_version['chapterId']:
                raise StructureError('EDIT_ANCHOR_SCOPE_MISMATCH', 'explicit chapter selection differs from the anchor')
            if candidateSha256 is not None and candidateSha256 != anchor_version['artifactSha256']:
                raise StructureError('PATCH_BASE_STALE', 'explicit candidate version differs from the anchor')
            chapterId, candidateSha256 = anchor_version['chapterId'], anchor_version['artifactSha256']
        if not isinstance(chapterId, str) or not isinstance(candidateSha256, str):
            raise StructureError('PATCH_CREDENTIAL_INVALID', 'supply edit anchors, a preparation credential or explicit chapter/candidate version')
        guards = []

        def observe(relative):
            state, raw = guard_observation(safe_project_path(root, relative))
            if raw is None:
                raise StructureError('PATCH_BASE_INVALID', 'candidate inputs must be readable regular files')
            guards.append({'path': relative, **state})
            return json.loads(raw)

        source = observe('.govel/structure.json')
        if anchor_version is not None and anchor_version['projectId'] != source['projectId']:
            raise StructureError('EDIT_ANCHOR_SCOPE_MISMATCH', 'anchor belongs to another project')
        workflow = observe('.govel/workflow.json')
        workflow_hash = guards.pop()['sha256']
        entry = workflow.get('artifacts', {}).get(chapterId, {}).get('candidate_prose', {})
        if entry.get('artifactSha256') != candidateSha256 or entry.get('invalidatedBy'):
            raise StructureError('PATCH_BASE_STALE', 'selected candidate is no longer the active version')
        path = stored_project_path(root, entry['artifactPath'])
        relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        base = observe(relative)
        validate_artifact_binding(base, project_id=source['projectId'], chapter_id=chapterId)
        if (base['artifactType'] != 'candidate_prose' or base['artifactSha256'] != candidateSha256
                or guards[-1]['sha256'] != entry.get('artifactFileSha256')):
            raise StructureError('PATCH_BASE_STALE', 'registered candidate file changed')
        text = base['payload']['text']
        binding = {'projectId': source['projectId'], 'chapterId': chapterId,
                   'artifactSha256': candidateSha256, 'bodyHash': sha256_text(text)}
        selected = []
        for row, bound in zip(patches, anchors):
            span = (resolve_anchor(bound, text, binding=binding, issue_id=row['issueId'],
                                   selected_range=row.get('range')) if bound is not None
                    else quote_span(text, row['original']))
            selected.append({**span, 'issueId': row['issueId'], 'replacement': row['replacement']})
        selected.sort(key=lambda row: (row['start'], row['end']))
        if any(left['end'] > right['start'] for left, right in zip(selected, selected[1:])):
            raise StructureError('PATCH_OVERLAP', 'patches overlap on the selected baseline')
        pieces, offset = [], 0
        for row in selected:
            pieces.extend((text[offset:row['start']], row['replacement']))
            offset = row['end']
        revised = ''.join([*pieces, text[offset:]])
        identity = sha256_json({'base': candidateSha256, 'patches': selected})
        patch_receipt = {'baseCandidateSha256': candidateSha256, 'patches': selected,
                         'chapterId': chapterId, 'patchHash': identity, 'requestHash': request_hash,
                         'baseGuard': guards[-1]}
        if requestId is not None:
            # Keep the original request, including opaque anchors, for an exact
            # replay. Resumption validates the durable receipt before using it.
            patch_receipt['resumeArguments'] = {
                'patches': patches, 'requestId': requestId, 'advanceLifecycle': advanceLifecycle,
                **{key: value for key, value in original_arguments.items() if value is not None}}
        result = pack_artifact(root, chapterId=chapterId, artifactType='candidate_prose',
                               input=f'工作台/patch-{identity}.txt', requestId=requestId,
                               advanceLifecycle=advanceLifecycle, diagnostics=diagnostics,
                               automaticRepair=automaticRepair,
                               _prepared_input=revised.encode(), _input_guards=guards,
                               _expected_workflow_sha256=workflow_hash,
                               _input_receipt=patch_receipt)
        return _finish_patch(root, result, patch_receipt, diagnostics)


def _finish_patch(root, result, receipt, diagnostics):
    if diagnostics is not None and result['ok']:
        from .repair_context import repair_context
        from .project import query_policy
        try:
            result['repairContext'] = repair_context(root, chapterId=receipt['chapterId'],
                                                     maxBytes=query_policy()['maxBytes']['overview'])
        except (ValueError, OSError) as exc:
            result.update(ok=False, code='PATCH_REPAIR_CONTEXT_INCOMPLETE',
                          repairContextError={'code': getattr(exc, 'code', 'REPAIR_CONTEXT_FAILED'), 'message': str(exc)})
    completed = ['candidate-registered']
    if result.get('diagnostics', {}).get('registered'):
        completed.append('diagnostics-registered')
    if result.get('repairContext'):
        completed.append('repair-materials-prepared')
    resume = receipt.get('resumeArguments', {})
    return {**result, 'code': 'CANDIDATE_PATCHED' if result['ok'] else result['code'],
            'baseCandidateSha256': receipt['baseCandidateSha256'],
            'patches': [{k: row[k] for k in ('issueId', 'start', 'end')} for row in receipt['patches']],
            'patchReceiptPath': str(Path(result['inputPath']).with_suffix('.receipt.json')),
            'mechanicalChecksRequired': 'diagnostics' not in result, 'semanticReviewComplete': False,
            'completedStages': completed,
            **({'resumeAction': {'operation': 'workflow.patch-candidate',
                                'arguments': {'projectRoot': str(root), 'requestId': resume['requestId']},
                                'needsApproval': False, 'reusesOriginalAuthorization': True}}
               if resume else {})}


def _resume_arguments(root, request_id):
    from .jsonio import read_json
    from .write_sets import digest
    if not isinstance(request_id, str) or not request_id.strip():
        raise StructureError('PATCH_RESUME_INVALID', 'provide patches or an existing requestId')
    source = read_json(safe_project_path(root, '.govel/structure.json'))
    identity = sha256_json({'projectId': source['projectId'], 'requestId': request_id})
    request = read_json(safe_project_path(root, f'.govel/artifact-requests/{identity}.json'))
    if request.get('requestHash') != sha256_json({k: v for k, v in request.items() if k != 'requestHash'}):
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'stored request receipt changed')
    path = str(Path(request['inputIdentity']['inputPath']).with_suffix('.receipt.json'))
    raw = safe_project_path(root, path).read_bytes()
    if digest(raw) != request.get('inputReceiptSha256'):
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'patch receipt changed')
    arguments = json.loads(raw).get('resumeArguments')
    if not isinstance(arguments, dict) or arguments.get('requestId') != request_id or not arguments.get('patches'):
        raise StructureError('PATCH_RESUME_UNAVAILABLE', 'this older request requires its original patch arguments')
    return arguments


def _replay_patch(root, request_id, request_hash, advance_lifecycle, diagnostics, automatic_repair):
    from .jsonio import read_json
    source = read_json(safe_project_path(root, '.govel/structure.json'))
    identity = sha256_json({'projectId': source['projectId'], 'requestId': request_id})
    path = safe_project_path(root, f'.govel/artifact-requests/{identity}.json')
    if not path.exists():
        return None
    request = read_json(path)
    if request.get('requestHash') != sha256_json({k: v for k, v in request.items() if k != 'requestHash'}):
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'stored request receipt changed')
    relative = request['inputIdentity']['inputPath']
    from .write_sets import digest
    receipt_raw = safe_project_path(root, str(Path(relative).with_suffix('.receipt.json'))).read_bytes()
    if digest(receipt_raw) != request.get('inputReceiptSha256'):
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'patch receipt changed')
    receipt = json.loads(receipt_raw)
    if receipt.get('requestHash') != request_hash:
        raise StructureError('ARTIFACT_REQUEST_REUSED', 'requestId identifies different patch inputs')
    workflow = read_json(safe_project_path(root, '.govel/workflow.json'))
    current = workflow.get('artifacts', {}).get(receipt['chapterId'], {}).get('candidate_prose', {})
    if current.get('invalidatedBy') or current.get('artifactSha256') != request['receipt']['artifactSha256']:
        raise StructureError('PATCH_BASE_STALE', 'the patched candidate is no longer current')
    result = pack_artifact(root, chapterId=receipt['chapterId'], artifactType='candidate_prose', input=relative,
                           requestId=request_id, advanceLifecycle=advance_lifecycle, diagnostics=diagnostics,
                           automaticRepair=automatic_repair,
                           _prepared_input=safe_project_path(root, relative).read_bytes(),
                           _input_guards=[receipt['baseGuard']])
    return _finish_patch(root, result, receipt, diagnostics)
