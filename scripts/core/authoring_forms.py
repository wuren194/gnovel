"""Tool-owned review forms and state bindings over existing artifact receipts."""
from __future__ import annotations

from pathlib import Path

from .paths import stored_project_path
from .artifact_pack import add_review_recovery, artifact_request_path, bounded_authoring_report, pack_artifact
from .artifact_templates import artifact_guide, review_skeleton_payload
from .artifacts import sha256_json
from .jsonio import read_json
from .locks import project_lock_reentrant
from .structure_store import StructureError, load_structure
from .write_sets import safe_project_path


def form_report(report, reference, view):
    """Pure projection also used when paging the same frozen form snapshot."""
    from .review_form_edits import model_guide, semantic_fields

    common = {key: report[key] for key in ('ok', 'code', 'chapterId', 'artifactType',
              'registered', 'approved', 'semanticReviewComplete')}
    common.update(formRef=reference, view=view,
        instruction='只填写实际判断、引文、适用性和裁决；未填仍未填。材料变化须重新取表并重审，不能把旧结论重新绑定。',
        submitAction={'operation': 'workflow.submit', 'arguments': {'chapterId': report['chapterId']},
                      'requiredInputs': ['requestId', 'artifacts'], 'artifactType': report['artifactType'],
                      'formRef': reference, 'editsSource': 'fields'},
        fullAction={'operation': 'task.snapshot', 'arguments': {'reference': reference}})
    if 'planPreflight' in report:
        common['planPreflight'] = report['planPreflight']
    if view == 'full':
        result = {**report, **common}
    else:
        plan = report['reviewPlan']
        result = {**common, 'fields': semantic_fields(report['payload'], report['artifactType']),
                  'guide': model_guide(report['guide'], report['payload'], report['artifactType']),
                  'reviewPlan': {key: plan[key] for key in ('mode', 'chapterReviewClass',
                                  'inactiveSeats', 'inactiveReasons', 'auxiliaryGates') if key in plan}}
    return result


def _present_form(root, report, reference, view):
    from .project import query_policy
    from .query import _size

    result = form_report(report, reference, view)
    budget = query_policy()['maxBytes']['detail']
    if _size(result) <= budget:
        return result
    # Reuse this exact snapshot instead of copying the full form into another
    # report cache. A long form is pageable; no omitted seat receives credit.
    common = {key: value for key, value in result.items() if key not in {'payload', 'fields', 'guide', 'reviewPlan'}}
    compact = {**common, 'responseCompacted': True, 'fieldsIncluded': False, 'payloadIncluded': False,
            'readAction': {'operation': 'task.snapshot', 'arguments': {'reference': reference, 'view': view}},
            'instruction': '表单超过当前返回预算；先分页读 readAction 的同一冻结表单，再逐席填写。未交付内容不算已审。'}
    if _size(compact) > budget:
        raise StructureError('AUTHORING_REPORT_BUDGET_INVALID', 'review form action metadata exceeds the configured response budget',
                             {'formRef': reference, 'maxBytes': budget})
    return compact


def _review_form(root: Path, *, chapterId: str, artifactType: str, repairRef: str | None = None,
                 view: str = 'model', batchRef: dict | None = None) -> dict:
    """Return an unregistered form bound to the material available when requested."""
    from .artifact_bindings import BINDABLE_FIELDS, bind_fields
    from .artifacts import envelope
    from .finalization import _load_workflow
    from .mechanical_evidence import _registered
    from .prepare_dependencies import bind_observed, capture_dependencies, observe_reads, validate_dependencies
    from .transport_cache import put
    from .write_sets import capture_guards, validate_guards

    if view not in {'model', 'full'}:
        raise StructureError('REVIEW_FORM_VIEW_INVALID', 'review form view must be model or full')
    root = root.expanduser().resolve()
    with project_lock_reentrant(root), observe_reads(root) as observed:
        dependencies = capture_dependencies(root, chapter_id=chapterId)
        guards = capture_guards(root, ['.govel/structure.json', '.govel/workflow.json'])
        structure, workflow = load_structure(root), _load_workflow(root)
        if artifactType == 'stage_b_review' and repairRef is None:
            from copy import deepcopy
            from .repair_history import stage_b_obligations
            candidate = _registered(root, structure, workflow, chapterId, 'candidate_prose')
            if candidate is not None and stage_b_obligations(root, structure, workflow, chapterId, candidate)['blockingIssueIds']:
                from .repair_context import _prepare_repair
                saved = _prepare_repair(root, chapterId)
                repair_dependencies = deepcopy(dependencies)
                bind_observed(repair_dependencies, observed)
                validate_dependencies(root, repair_dependencies, code='REPAIR_INPUT_CHANGED')
                saved['dependencies'] = repair_dependencies
                repairRef = put(root, 'repair', saved)
        payload, plan = review_skeleton_payload(root, chapterId=chapterId, artifactType=artifactType,
                                              repairRef=repairRef)
        if batchRef is not None:
            if artifactType != 'stage_a_review':
                raise StructureError('CANDIDATE_RECIPE_INVALID', 'candidate commission is prepared from stage A')
            from .candidate_batch import prepare_candidate_recipe
            payload['candidateRecipe'] = prepare_candidate_recipe(root, batchRef=batchRef, chapterId=chapterId)['recipe']
        if artifactType == 'stage_b_review':
            candidate = _registered(root, structure, workflow, chapterId, 'candidate_prose')
            if candidate is None:
                raise StructureError('BIND_DEPENDENCY_MISSING', 'stage B form requires a registered candidate')
            payload['candidateArtifactSha256'] = candidate['artifactSha256']
            from .current_candidate import require_candidate_selection
            guards.extend(require_candidate_selection(root, structure, chapterId, candidate['payload'])['guards'])
            if candidate['payload'].get('writingNotes'):
                payload['candidateArtifactSha256'] = candidate['artifactSha256']
                notes = candidate['payload']['writingNotes']
                payload['writingDecisions'] = [{'id': row['id'], 'decision': '', 'reason': ''}
                                               for row in (*notes.get('deviations', []), *notes.get('newFacts', []))]
            diagnostics = _registered(root, structure, workflow, chapterId, 'diagnostics')
            if diagnostics is not None:
                payload['diagnosticsArtifactSha256'] = diagnostics['artifactSha256']
            for kind in ('candidate_prose', 'diagnostics'):
                entry = workflow.get('artifacts', {}).get(chapterId, {}).get(kind, {})
                if entry.get('artifactPath') and not entry.get('invalidatedBy'):
                    path = stored_project_path(root, entry['artifactPath'])
                    relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
                    guards.extend(capture_guards(root, [relative]))
                    # Capture, then check again: a concurrent edit must not become
                    # the reference material after its initial integrity check.
                    _registered(root, structure, workflow, chapterId, kind)
        artifact, _ = bind_fields(root, structure=structure, workflow=workflow, chapter_id=chapterId,
            artifact=envelope(artifactType, payload, project_id=structure['projectId'], chapter_id=chapterId),
            bindings=[(field, None) for field in sorted(BINDABLE_FIELDS.get(artifactType, ()))])
        validate_guards(root, guards, stage='review-form')
        report = {
            'ok': True, 'code': 'REVIEW_FORM_PREPARED', 'chapterId': chapterId, 'artifactType': artifactType,
            'payload': artifact['payload'], 'guide': artifact_guide(artifactType), 'reviewPlan': plan,
            'registered': False, 'approved': False, 'semanticReviewComplete': False,
            'instruction': '保留工具填入的绑定，填写实际结论、引文和裁决后通过 workflow.submit 提交；表单不代表审校通过。',
            'submitAction': {'operation': 'workflow.submit', 'arguments': {'chapterId': chapterId},
                             'requiredInputs': ['requestId', 'artifacts'],
                             'artifactType': artifactType, 'payloadSource': 'payload'}}
        if artifactType == 'stage_a_review':
            from .outline_facts import plan_readiness
            report['planPreflight'] = plan_readiness(root, structure, chapterId)
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='REVIEW_FORM_STALE')
        reference = put(root, 'review-form', {'projectId': structure['projectId'],
                        'report': report, 'dependencies': dependencies})
        return _present_form(root, report, reference, view)


def review_form(root: Path, *, chapterId: str, artifactType: str, repairRef: str | None = None,
                view: str = 'model', batchRef: dict | None = None) -> dict:
    """Keep the blank-form rejection actionable even when the source is invalid."""
    try:
        return _review_form(root, chapterId=chapterId, artifactType=artifactType, repairRef=repairRef, view=view, batchRef=batchRef)
    except (ValueError, OSError) as exc:
        add_review_recovery(exc, chapter_id=chapterId, artifact_type=artifactType)
        raise


def prepare_state_candidate(root: Path, *, requestId: str, chapterId: str | None = None) -> dict:
    """Register mechanical current-state bindings; the pack receipt owns retries."""
    from .consistency import require_no_pending

    root = root.expanduser().resolve()
    if not isinstance(requestId, str) or not requestId.strip():
        raise StructureError('STATE_REQUEST_INVALID', 'requestId must be a nonempty string')
    internal_id = 'state-prepare:' + requestId
    with project_lock_reentrant(root):
        require_no_pending(root)
        structure = load_structure(root)
        path = safe_project_path(root, artifact_request_path(structure['projectId'], internal_id))
        if path.exists():
            request = read_json(path)
            identity = request.get('inputIdentity', {}) if isinstance(request, dict) else {}
            if (not isinstance(request, dict) or request.get('schemaVersion') != 1
                    or request.get('requestId') != internal_id
                    or request.get('requestHash') != sha256_json({k: v for k, v in request.items() if k != 'requestHash'})
                    or not isinstance(identity, dict)
                    or identity.get('artifactType') != 'state_candidate'
                    or not isinstance(identity.get('chapterId'), str)):
                raise StructureError('STATE_REQUEST_INVALID', 'stored state preparation receipt is invalid')
            if chapterId is not None and chapterId != identity['chapterId']:
                raise StructureError('ARTIFACT_REQUEST_REUSED', 'requestId belongs to another chapter')
            chapterId = identity['chapterId']
        elif chapterId is None:
            raise StructureError('STATE_RESUME_UNAVAILABLE', 'provide chapterId for a new state preparation')
        result = pack_artifact(root, chapterId=chapterId, artifactType='state_candidate',
                               requestId=internal_id, payload={'changes': []}, _full_report=True)
        if not result.get('registered'):
            raise StructureError('STATE_REQUEST_SUPERSEDED', 'prepared state artifact is no longer registered')
        return bounded_authoring_report(root, {
            **result, 'code': 'STATE_CANDIDATE_PREPARED', 'requestId': requestId,
            'semanticReviewComplete': False, 'factsAssessment': 'not_performed',
            'instruction': '只登记当前状态、计划和候选正文的机械绑定；空 changes 不证明没有新增或修订事实。事实仍须源提案与正文引证。',
            'resumeAction': {'operation': 'workflow.state-prepare', 'arguments': {'requestId': requestId}},
            'receiptAction': {'operation': 'project.workflow.chapter-receipt',
                              'arguments': {'chapterId': chapterId, 'view': 'model'}},
            'approvalAction': {'operation': 'project.workflow.artifact-approve',
                               'arguments': {'chapterId': chapterId, 'artifact': ['@' + result['artifactPath']]},
                               'requiresExplicitApproval': True}})
