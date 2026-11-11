"""Package station-authored content using the existing binding and registration gates.

No review conclusions, approvals or story facts are inferred. The input and all
binding dependencies are guarded until the artifact/workflow write set commits.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .paths import stored_project_path
from .artifact_bindings import BINDABLE_FIELDS, bind_fields
from .artifact_templates import ARTIFACT_TYPES
from .artifacts import envelope, sha256_json
from .clock import utc_now
from .jsonio import read_json
from .locks import project_lock_reentrant
from .review_artifacts import validate_review_artifact
from .structure_store import StructureError, load_structure
from .workflow import default_workflow, normalize_workflow, record_artifact
from .write_sets import (apply_write_set, digest, guard_observation, inventory, json_bytes,
                         safe_project_path, validate_guards)

# Mechanical evidence already has its own runner and must not be manufactured here.
PACKABLE_TYPES = tuple(kind for kind in ARTIFACT_TYPES if kind != 'diagnostics')


def add_review_recovery(error, *, chapter_id, artifact_type):
    """Attach read-only next steps without changing the original rejection.

    These actions inspect material or prepare an unapproved blank form. They
    cannot correct source data, copy old conclusions onto new inputs or approve.
    No project reads are attempted while handling an invalid project.
    """
    if artifact_type not in {'stage_a_review', 'stage_b_review'} or not isinstance(chapter_id, str):
        return
    code = getattr(error, 'code', None)
    detail = dict(getattr(error, 'details', {}) or {})
    schema = {'operation': 'source.schema', 'arguments': {}}
    form = {'operation': 'workflow.review-form', 'arguments': {
        'chapterId': chapter_id, 'artifactType': artifact_type}}
    plan = {'operation': 'source.chapter-plan', 'arguments': {'chapterId': chapter_id, 'view': 'detail'}}
    if code == 'STRUCTURE_INVALID':
        recovery = {'kind': 'source_invalid', 'readActions': [schema],
                    'notChecked': ['chapter_plan', 'candidate_binding', 'review_completion', 'author_approval'],
                    'requiresMaintenance': True, 'ordinarySourcePreviewBlocked': True,
                    'instruction': '当前源文件校验失败，普通创作与源预览均已阻断。先检查 errors 和源字段说明，转工具维护核验现有备份或事务证据，受控恢复已知有效基线；恢复前不要循环提交预览或直接手改 structure.json。恢复有效源后才继续常规预览和实际审校。'}
    elif code == 'SOURCE_CHAPTER_PLAN_MISSING':
        recovery = {'kind': 'plan_required', 'readActions': [schema, plan],
                    'instruction': '本章已有条目但没有有效推演。先在本章建立受审的源计划；新章使用 workflow.chapter-preview 一并创建条目与推演，已有章不要重复创建。计划就绪后再取审校表单。',
                    'afterPlanReady': form}
    elif code == 'ABSOLUTE_PATH_FORBIDDEN':
        recovery = {'kind': 'input_path', 'readActions': [
            {'operation': 'capabilities.list', 'arguments': {'operation': 'workflow.artifact-pack'}}],
            'instruction': '已有文件的 input 使用项目内工作台相对路径；也可直接传已完成的 payload 对象，由工具保存、封装与绑定。'}
    elif code == 'ARTIFACT_BINDING_STALE':
        recovery = {'kind': 'review_inputs_changed', 'readActions': [plan, form],
                    'instruction': '读取本次计划和材料变化，重新取当前表单并复核受影响结论；保留工具填入的绑定。不要只改 hash 或删除旧绑定来复用未经重审的结论。'}
    elif code == 'BEHAVIOR_PLAN_COVERAGE_REQUIRED':
        recovery = {'kind': 'plan_coverage_review', 'readActions': [plan, schema],
                    'instruction': '先核对当前计划合同的适用性声明及本次审查判断。计划尚未完成评估或确需修改时，实际评估后经受审源预览／提交更新；报告写错时修正实际判断。计划和报告一致后再取表完成审校，单独反复取空表不能解决计划缺项。',
                    'afterPlanReady': form}
    elif code == 'BEHAVIOR_COVERAGE_CONFLICT':
        recovery = {'kind': 'coverage_evidence_conflict', 'readActions': [plan, schema],
                    'instruction': '按 details.domain 核对适用性与实际声明／正文发现条目。补齐真实依据或修正错误判断；计划需变更时先走受审源事务。保留已有审查与发现，不为通过校验改成不适用或删除必要条目。',
                    'afterAssessment': form}
    elif isinstance(code, str) and code.startswith('COGNITION_'):
        recovery = {'kind': 'cognition_evidence_required', 'readActions': [form, plan],
                    'instruction': '核对定位的信息使用点、使用角色及当时可接触的实际来源。保留未知和既有finding；缺材料不能改成通过，无关引文不能代替来源。前章引用绑定变化时重新阅读实际正文并重审，不只替换hash，不自动补写获知情节。'}
    elif isinstance(code, str) and code.startswith('EVIDENCE_QUOTE_'):
        detail.setdefault('chapterId', chapter_id)
        detail.setdefault('artifactType', artifact_type)
        if isinstance(detail.get('readAction'), dict):
            detail['readAction'].setdefault('arguments', {})['chapterId'] = chapter_id
        recovery = {'kind': 'exact_quote_required', 'readActions': [detail.get('readAction') or form],
                    'instruction': '按 field/slot/quoteIndex 核对冻结材料中的原字符；重复原句须给准确区间。空白、标点和旧版本不模糊放过，不修改绑定来复用旧结论。'}
    elif isinstance(code, str) and code.startswith(('BEHAVIOR_', 'REVIEW_', 'STAGE_A_', 'CAUSALITY_REVIEW_')):
        recovery = {'kind': 'review_form_required', 'readActions': [form],
                    'instruction': '使用当前表单和 guide 中的字段说明，填写实际席位判断、适用性与原文证据。空槽不算通过；适用性须依据计划或正文，不为消除错误改成不适用。'}
    else:
        return
    recovery.update(automatic=False, preservesExistingEvidence=True)
    detail.setdefault('recovery', recovery)
    error.details = detail


def candidate_structure_identity(structure: dict, chapter_id: str) -> str:
    """Ignore only bookkeeping and this candidate's allowed lifecycle changes."""
    source = deepcopy(structure)
    for field in ('revision', 'updatedAt', 'lastOperation'):
        source.pop(field, None)
    chapter = next(row for row in source['chapters'] if row['chapterId'] == chapter_id)
    if chapter['status'] in {'planned', 'draft', 'review'}:
        chapter['status'] = 'candidate-lifecycle'
    return sha256_json(source)


def artifact_request_path(project_id: str, request_id: str) -> str:
    identity = sha256_json({'projectId': project_id, 'requestId': request_id})
    return f'.govel/artifact-requests/{identity}.json'


def bounded_authoring_report(root: Path, report: dict) -> dict:
    """Keep the action receipt small; preserve every detail in a pageable snapshot."""
    from .project import query_policy
    from .query import _size
    from .transport_cache import put
    budget = query_policy()['maxBytes']['detail']
    if _size(report) <= budget:
        return report
    reference = put(root, 'authoring-report', report)
    keep = {'ok', 'code', 'chapterId', 'artifactType', 'artifactPath', 'artifactSha256',
            'registered', 'approved', 'approvalPerformed', 'inputPath', 'draftSha256',
            'stageAArtifactSha256', 'lifecycleStatus', 'replayed', 'requestId',
            'submissionComplete', 'approvalsPerformed', 'semanticReviewComplete',
            'resumeAction', 'receiptAction', 'approvalAction', 'transactionScope', 'repairAccounting',
            'submitAction', 'prepareAction', 'runAction', 'summaryAction', 'nextPrepareAction', 'prepareRef',
            'readyForContinuation', 'artifactApprovalPerformed', 'factsAssessed', 'missingInputs',
            'selectionRef', 'selection', 'candidateSelection', 'stageAPlanCurrent', 'stageAApproved', 'replacement',
            'completedStages', 'factsAssessment', 'waitingForAuthorization', 'nextPreparationError'}
    compact = {key: value for key, value in report.items() if key in keep}
    compact.update(reportRef=reference, responseCompacted=True,
                   readAction={'operation': 'task.snapshot', 'arguments': {'reference': reference}})
    if report.get('view') == 'model' and ('chapter' in report or 'chapters' in report):
        for key in ('schemaVersion', 'projectId', 'view', 'chapter', 'approvedRange', 'approvalScope',
                    'nextAction', 'titleDecision', 'finalizationOptions', 'finalizeAction',
                    'ownership', 'bindingGeneration', 'readyToFinalize', 'healthy', 'detailsOperation',
                    'chapterReferences'):
            if key in report:
                compact[key] = report[key]
        if isinstance(report.get('continuity'), dict):
            compact['continuity'] = {key: value for key, value in report['continuity'].items()
                                     if key != 'factInventory'}
            compact['continuity']['detailsRef'] = reference
        if isinstance(report.get('finalization'), dict):
            compact['finalization'] = {'applicable': report['finalization'].get('applicable'),
                                      'blockerCount': len(report['finalization'].get('blockers', [])),
                                      'detailsRef': reference}
        for key in ('blockers', 'transactions', 'chapters'):
            if isinstance(report.get(key), list):
                compact[key + 'Count'] = len(report[key])
    for key in ('error', 'diagnosticsError'):
        if isinstance(report.get(key), dict):
            compact[key] = {'code': report[key].get('code'), 'detailsRef': reference}
    if isinstance(report.get('diagnostics'), dict):
        diagnostic = report['diagnostics']
        compact['diagnostics'] = {key: diagnostic[key] for key in (
            'ok', 'code', 'registered', 'artifactPath', 'artifactSha256', 'passed', 'reused',
            'reportRef', 'findingCount') if key in diagnostic}
    if 'stages' in report:
        compact['stages'] = [{key: row[key] for key in ('artifactType', 'status', 'artifactPath', 'artifactSha256')
                              if key in row} | ({'errorCode': row['error'].get('code')} if row.get('error') else {})
                             for row in report['stages']]
    if 'results' in report:
        compact['results'] = [{'artifactType': row.get('artifactType'), 'ok': row.get('ok'),
                              'code': row.get('code'), 'detailsRef': reference} for row in report['results']]
    if 'writingNotes' in report:
        compact['writingNotesSummary'] = {key: len(report['writingNotes'].get(key, []))
                                          for key in ('deviations', 'newFacts')}
    if isinstance(report.get('factInventory'), dict):
        compact['factInventory'] = {key: report['factInventory'][key] for key in ('assessed', 'complete', 'status') if key in report['factInventory']}
        compact['factInventory']['detailsRef'] = reference
    if 'outlineFacts' in report:
        facts = report['outlineFacts']
        compact['outlineFacts'] = {key: facts[key] for key in ('available', 'valid', 'issues', 'sourcePlanHash') if key in facts}
    if _size(compact) > budget:
        raise StructureError('AUTHORING_REPORT_BUDGET_INVALID', 'authoring action metadata exceeds the configured response budget',
                             {'reportRef': reference})
    return compact


def _pack_artifact(root: Path, *, chapterId: str, artifactType: str, input: str,
                  requestId: str | None = None, advanceLifecycle: bool = False, automaticRepair: bool = False,
                  notes: str | None = None, _prepared_input: bytes | None = None,
                  _input_guards: list[dict] | None = None,
                  _expected_workflow_sha256: str | None = None,
                  _input_receipt: dict | None = None, writingNotes: dict | None = None,
                  selectionRef: str | None = None, selectionReview: dict | None = None,
                  recipeRef: str | None = None,
                  _stage_guards: list[dict] | None = None) -> dict:
    root = root.expanduser().resolve()
    if artifactType not in PACKABLE_TYPES:
        raise StructureError('ARTIFACT_TYPE_UNSUPPORTED', 'use the mechanical runner for diagnostics',
                             {'supported': list(PACKABLE_TYPES)})
    with project_lock_reentrant(root):
        guards = {row['path']: row for row in (_input_guards or [])}
        stage_guards = {row['path']: row for row in (_stage_guards or [])}
        validate_guards(root, list(stage_guards.values()), stage='artifact-pack-stage-inputs')

        def observe(relative: str) -> bytes:
            path = safe_project_path(root, relative)
            state, raw = guard_observation(path)
            if raw is None:
                raise StructureError('ARTIFACT_INPUT_INVALID', 'input must be a readable regular file', {'path': relative})
            if relative in guards and guards[relative] != {'path': relative, **state}:
                raise StructureError('WRITE_SET_CONFLICT', 'pack input changed after patch selection', {'path': relative})
            if relative in stage_guards and stage_guards[relative] != {'path': relative, **state}:
                raise StructureError('WRITE_SET_CONFLICT', 'submission inputs changed before packaging', {'path': relative})
            guards[relative] = {'path': relative, **state}
            return raw

        observe('.govel/structure.json')
        structure = load_structure(root)
        chapter = next((row for row in structure['chapters'] if row['chapterId'] == chapterId), None)
        if chapter is None or chapter.get('status') == 'archived':
            raise StructureError('ARTIFACT_CHAPTER_UNKNOWN', 'pack requires an active chapter')
        # Resolve relative input against the project; no output can replace it.
        input_path = safe_project_path(root, input)
        input_relative = str(input_path.relative_to(root))
        if Path(input_relative).parts[0] != '工作台':
            raise StructureError('ARTIFACT_INPUT_INVALID', 'save station content under 工作台 before packaging')
        raw = observe(input_relative) if _prepared_input is None else _prepared_input
        if _prepared_input is not None and input_path.exists() and observe(input_relative) != raw:
            raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'prepared input path already has different content')
        prepared_notes = None
        if writingNotes is not None:
            if notes is not None or artifactType != 'candidate_prose' or not isinstance(writingNotes, dict):
                raise StructureError('ARTIFACT_INPUT_INVALID', 'writingNotes is a candidate object; choose notes or writingNotes')
            prepared_notes = json_bytes(json.loads(json.dumps(writingNotes, ensure_ascii=False, sort_keys=True)))
            notes = f'工作台/inputs/notes-{digest(prepared_notes)}.json'
            if safe_project_path(root, notes).exists() and observe(notes) != prepared_notes:
                raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'prepared notes path already has different content')
        workflow_path = safe_project_path(root, '.govel/workflow.json')
        workflow_raw = workflow_path.read_bytes() if workflow_path.is_file() else None
        if '.govel/workflow.json' in stage_guards:
            expected_workflow = stage_guards['.govel/workflow.json']['sha256']
            if (digest(workflow_raw) if workflow_raw is not None else None) != expected_workflow:
                raise StructureError('WRITE_SET_CONFLICT', 'submission workflow changed before packaging')
        if _expected_workflow_sha256 is not None and (workflow_raw is None or digest(workflow_raw) != _expected_workflow_sha256):
            raise StructureError('PATCH_BASE_STALE', 'workflow changed during patch assembly')
        workflow = normalize_workflow(json.loads(workflow_raw)) if workflow_raw is not None else default_workflow()
        if artifactType != 'candidate_prose' and (selectionRef is not None or selectionReview is not None):
            raise StructureError('ARTIFACT_INPUT_INVALID', 'selection handoff applies only to candidate prose')
        if artifactType == 'candidate_prose':
            from .writing_checks import split_beat_markers
            text, spans = split_beat_markers(raw.decode('utf-8'))
            payload = {'text': text}
            if spans: payload['beatSpans'] = spans
            if not payload['text'].strip():
                raise StructureError('CANDIDATE_BODY_INVALID', 'candidate prose is empty')
            if notes is None:
                from .mechanical_evidence import _registered
                previous = _registered(root, structure, workflow, chapterId, 'candidate_prose', historical=True)
                if previous and previous['payload'].get('writingNotes'):
                    raise StructureError('WRITING_NOTES_REQUIRED',
                        '当前候选有待裁决写作说明；重新包装须显式传 notes，撤回全部说明时传空对象文件，不能因漏参丢失。')
            if notes is not None:
                from .writing_notes import validate_notes
                if not isinstance(notes, str) or Path(notes).parts[:1] != ('工作台',) or notes.lstrip().startswith(('{', '[')):
                    raise StructureError('WRITING_NOTES_PATH_REQUIRED', 'notes 是工作台下的 JSON 文件路径，不是正文或 JSON 对象',
                        {'example': {'notes': '工作台/写作说明.json'},
                         'contentExample': {'deviations': [], 'newFacts': []}})
                note_path = safe_project_path(root, notes)
                note_relative = str(note_path.relative_to(root))
                if Path(note_relative).parts[0] != '工作台':
                    raise StructureError('ARTIFACT_INPUT_INVALID', 'save writing notes under 工作台')
                payload['writingNotes'] = json.loads(observe(note_relative) if prepared_notes is None else prepared_notes)
                validate_notes(payload['writingNotes'], text)
                if payload['writingNotes'].get('newFacts'):
                    raise StructureError('SOURCE_FACTS_REQUIRE_PROPOSAL',
                        '新增事实由 B 的 factAssessment/findings 盘点，再走源提案与正文证据入口；writingNotes.newFacts 不可提交',
                        {'nextOperations': ['source.schema', 'source.change-preview', 'source.evidence-register'],
                         'factIds': [row['id'] for row in payload['writingNotes']['newFacts']],
                         'instruction': '保留事实候选；明确 owner/contentBindings 后预览源事务，作者批准后提交。不要把未映射当作 no_change。'})
                for fact in payload['writingNotes'].get('newFacts', []):
                    if fact.get('target'):
                        raw_target = observe(fact['target']['document']).decode('utf-8')
                        if '> GENERATED:' in raw_target:
                            raise StructureError('WRITING_FACT_TARGET_INVALID', 'target cannot be a generated view')
                from .outline_facts import inspect_outline
                from .writing_checks import outline_requirements
                outline = inspect_outline(root, chapterId=chapterId, structure=structure)
                if outline.get('available'):
                    from .chapter_contract import outline_text as read_outline_text
                    observe(outline['path'])
                    outline_text = read_outline_text(root, outline)
                    required_ids = {row['id'] for row in (outline.get('requirements') or outline_requirements(outline_text))['requirements']}
                    for item in payload['writingNotes'].get('deviations', []):
                        if item['kind'] == 'wording' and item['plannedText'] not in outline_text:
                            raise StructureError('WRITING_PLANNED_TEXT_MISSING', 'wording note must quote current plan text')
                        if item['kind'] == 'requirement' and item['requirementId'] not in required_ids:
                            raise StructureError('WRITING_REQUIREMENT_UNKNOWN', '偏离须指向当前必须落地清单中的条目', {'id': item['requirementId']})
                        if item['kind'] == 'intensityLevel' and outline.get('intensityLevel') is not None and item['intensityLevel'] > outline['intensityLevel']:
                            raise StructureError('WRITING_LEVEL_INCREASE_REQUIRES_PLAN', '候选说明只登记降档；提高上限须先修订推演')
            from .current_candidate import bind_candidate_handoff, require_candidate_selection
            if selectionRef is not None:
                handoff, selection_guards = bind_candidate_handoff(root, structure, workflow, chapterId,
                    reference=selectionRef, review=selectionReview, text=text, notes=payload.get('writingNotes'))
                payload.update(handoff)
                for guard in selection_guards:
                    guards[guard['path']] = guard
            else:
                if selectionReview is not None:
                    raise StructureError('CANDIDATE_HANDOFF_REQUIRED', 'selectionReview requires its frozen selectionRef')
                selected = require_candidate_selection(root, structure, chapterId, payload)
                for guard in selected['guards']:
                    guards[guard['path']] = guard
            stage_a = workflow.get('artifacts', {}).get(chapterId, {}).get('stage_a_review', {})
            if stage_a.get('artifactPath') and not stage_a.get('invalidatedBy'):
                from .mechanical_evidence import _registered
                current = _registered(root, structure, workflow, chapterId, 'stage_a_review')
                if current['payload'].get('candidateRecipe') is not None:
                    payload['candidateRecipe'] = deepcopy(current['payload']['candidateRecipe'])
                if selectionRef is None:
                    payload['stageAArtifactSha256'] = current['artifactSha256']
                stage_path = stored_project_path(root, stage_a['artifactPath'])
                observe(str(stage_path.relative_to(root)))
        else:
            if notes is not None:
                raise StructureError('ARTIFACT_INPUT_INVALID', 'notes is only supported for candidate prose')
            payload = json.loads(raw)
            if not isinstance(payload, dict) or 'artifactSha256' in payload or 'artifactType' in payload:
                raise StructureError('ARTIFACT_PAYLOAD_REQUIRED', 'supply the content object, not an envelope')
            payload.pop('_guide', None)
        if recipeRef is not None:
            if artifactType != 'stage_a_review':
                raise StructureError('CANDIDATE_RECIPE_INVALID', 'recipeRef is frozen by stage A; later artifacts inherit A')
            from .candidate_batch import resolve_recipe
            recipe = resolve_recipe(root, structure, workflow, chapterId, recipeRef)
            if payload.get('candidateRecipe', recipe) != recipe:
                raise StructureError('CANDIDATE_RECIPE_INVALID', 'explicit A recipe does not match recipeRef')
            payload['candidateRecipe'] = recipe
        if payload.get('candidateRecipe') is not None:
            from .candidate_batch import recipe_guards
            for guard in recipe_guards(root, structure, workflow, chapterId, payload['candidateRecipe']):
                guards[guard['path']] = guard
        repair_dependencies = None
        if artifactType == 'stage_b_review' and 'repairRef' in payload:
            from .repair_review import assemble_review, replay_review
            completed_hash = None
            restored = None
            if requestId is not None:
                prior_path = safe_project_path(root, '.govel/artifact-requests/' +
                    sha256_json({'projectId': structure['projectId'], 'requestId': requestId}) + '.json')
                if prior_path.is_file():
                    prior = read_json(prior_path)
                    if (prior.get('requestHash') == sha256_json({k: v for k, v in prior.items() if k != 'requestHash'})
                            and prior.get('inputIdentity') == {'chapterId': chapterId, 'artifactType': artifactType,
                                'inputPath': input_relative, 'inputSha256': digest(raw)}):
                        completed_hash = prior.get('receipt', {}).get('artifactSha256')
                        restored = replay_review(root, chapterId, workflow, prior)
            payload, repair_guards, repair_dependencies = (restored if restored is not None else
                assemble_review(root, chapterId, payload, completed_hash=completed_hash))
            for guard in repair_guards:
                if guard['path'] in guards and guards[guard['path']] != guard:
                    raise StructureError('WRITE_SET_CONFLICT', 'repair inputs changed during review assembly')
                guards[guard['path']] = guard
        if artifactType == 'title_candidates':
            candidates = payload.get('candidates', [])
            if not isinstance(candidates, list):
                raise StructureError('TITLE_CANDIDATES_REQUIRED', 'candidates must be an array')
            for index, row in enumerate(candidates):
                if isinstance(row, dict) and 'candidateId' not in row:
                    row['candidateId'] = f'title-{index + 1}'
        if artifactType in {'stage_b_review', 'state_candidate'}:
            from .mechanical_evidence import _registered
            candidate = _registered(root, structure, workflow, chapterId, 'candidate_prose')
            if candidate:
                from .current_candidate import require_candidate_selection
                selected = require_candidate_selection(root, structure, chapterId, candidate['payload'])
                for guard in selected['guards']:
                    guards[guard['path']] = guard
                expected_candidate = payload.get('candidateArtifactSha256')
                if expected_candidate is not None and expected_candidate != candidate['artifactSha256']:
                    raise StructureError('ARTIFACT_BINDING_STALE', 'review/state refers to a different candidate identity')
                payload['candidateArtifactSha256'] = candidate['artifactSha256']
            if candidate and candidate['payload'].get('writingNotes'):
                if payload.get('candidateArtifactSha256', candidate['artifactSha256']) != candidate['artifactSha256']:
                    raise StructureError('ARTIFACT_BINDING_STALE', 'writing decisions refer to an old candidate')
                payload['candidateArtifactSha256'] = candidate['artifactSha256']
                if artifactType == 'state_candidate':
                    facts = candidate['payload']['writingNotes'].get('newFacts', [])
                    if payload.get('proposedFacts', facts) != facts:
                        raise StructureError('ARTIFACT_BINDING_STALE', 'new facts must match the registered candidate notes')
                    payload['proposedFacts'] = facts
        if artifactType == 'stage_b_review':
            from .cognition_sources import bind_prior_sources
            bind_prior_sources(root, structure, chapterId, payload, observe)
            from .mechanical_evidence import _registered
            diagnostics = _registered(root, structure, workflow, chapterId, 'diagnostics')
            if diagnostics:
                if payload.get('diagnosticsArtifactSha256', diagnostics['artifactSha256']) != diagnostics['artifactSha256']:
                    raise StructureError('ARTIFACT_BINDING_STALE', 'review refers to different diagnostics')
                payload['diagnosticsArtifactSha256'] = diagnostics['artifactSha256']
                entry = workflow['artifacts'][chapterId]['diagnostics']
                diagnostic_path = stored_project_path(root, entry['artifactPath'])
                observe(str(diagnostic_path.relative_to(root)))
        if artifactType == 'stage_a_review':
            from .outline_facts import inspect_outline
            constraints = inspect_outline(root, chapterId=chapterId, structure=structure).get('projectConstraints', {})
            if constraints.get('conflicts'):
                payload['projectExceptions'] = constraints
                observe('AGENTS.md')
        fields = set(BINDABLE_FIELDS.get(artifactType, ()))
        if 'finalProseSha256' in fields:
            entry = workflow.get('artifacts', {}).get(chapterId, {}).get('candidate_prose', {})
            if entry.get('artifactPath'):
                prose_path = stored_project_path(root, entry['artifactPath'])
                relative = str(prose_path.relative_to(root)) if prose_path.is_absolute() else str(prose_path)
                observe(relative)
                # Reuse approval's freshness checks without granting an approval.
                from .review_artifacts import validate_artifact_binding
                prose = read_json(safe_project_path(root, relative))
                validate_artifact_binding(prose, project_id=structure['projectId'], chapter_id=chapterId)
                if (prose['artifactSha256'] != entry.get('artifactSha256') or entry.get('invalidatedBy')
                        or entry.get('artifactFileSha256') != guards[relative]['sha256']):
                    raise StructureError('BIND_DEPENDENCY_STALE', 'registered candidate prose changed')
        artifact = envelope(artifactType, payload, project_id=structure['projectId'], chapter_id=chapterId,
                            source_sha256=digest(raw))
        artifact, bindings = bind_fields(root, structure=structure, workflow=workflow, chapter_id=chapterId,
                                         artifact=artifact, bindings=[(field, None) for field in sorted(fields)])
        for field, binding in bindings.items():
            if binding['previousValue'] is not None and binding['previousValue'] != binding['contentSha256']:
                raise StructureError('ARTIFACT_BINDING_STALE', 'supplied binding differs from current truth', {'field': field})
        validate_review_artifact(artifact)
        if artifactType == 'stage_a_review':
            from .outline_facts import require_mode_level_review
            require_mode_level_review(root, structure, chapterId, payload)
            from .outline_constraints import stage_a_seats
            stage_a_seats(root, structure, chapterId, artifact['payload'])
            artifact['artifactSha256'] = sha256_json({k: v for k, v in artifact.items() if k != 'artifactSha256'})
        if artifactType == 'state_candidate':
            if payload['changes']:
                raise StructureError('SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION', 'use source transactions for setting changes')
        dependency_guards = deepcopy(guards)
        # The mutable index is a commit guard. Saving an unselected version does
        # not change the selected content dependency of an original request.
        selection_dependency = None
        if artifactType in {'candidate_prose', 'stage_b_review', 'state_candidate'}:
            from .candidate_versions import WORKBENCH_DIR
            from .current_candidate import selected_candidate
            dependency_guards.pop(f'{WORKBENCH_DIR}/{chapterId}/index.json', None)
            selection_dependency = selected_candidate(root, structure, chapterId)['selection']
        # Tool-owned content-addressed inputs are compared byte-for-byte above;
        # their first creation must not make an identical retry look stale.
        if _prepared_input is not None:
            dependency_guards.pop(input_relative, None)
        if prepared_notes is not None:
            dependency_guards.pop(notes, None)
        if artifactType == 'stage_b_review' and 'repairRef' in json.loads(raw):
            # The saved artifact and candidate are checked on replay. Our own
            # report registration changes workflow, not the reviewed content.
            dependency_guards.pop('.govel/workflow.json', None)
        if advanceLifecycle:
            if artifactType != 'candidate_prose' or chapter['status'] not in {'planned', 'draft', 'review'}:
                raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'candidate lifecycle requires planned, draft or review')
            # A lifecycle retry may see our own new revision/inode. Bind every story field,
            # while normalizing only bookkeeping and this chapter's allowed candidate stages.
            dependency_guards['.govel/structure.json'] = {
                'lifecycleContentHash': candidate_structure_identity(structure, chapterId)}
        dependency_hash = sha256_json({'guards': dependency_guards, 'candidateSelection': selection_dependency, 'bindings': {
            key: value['contentSha256'] for key, value in bindings.items()}})
        request_path = None
        request = None
        if requestId is not None:
            if not isinstance(requestId, str) or not requestId.strip():
                raise StructureError('ARTIFACT_REQUEST_INVALID', 'requestId must be a nonempty string')
            request_path = artifact_request_path(structure['projectId'], requestId)
            path = safe_project_path(root, request_path)
            if path.exists():
                request = read_json(path)
                if (not isinstance(request, dict) or request.get('schemaVersion') != 1
                        or request.get('requestId') != requestId
                        or request.get('requestHash') != sha256_json({k: v for k, v in request.items() if k != 'requestHash'})):
                    raise StructureError('ARTIFACT_REQUEST_INVALID', 'stored request receipt is invalid')
                if request.get('inputIdentity') != {
                        'chapterId': chapterId, 'artifactType': artifactType,
                        'inputPath': input_relative, 'inputSha256': digest(raw),
                        **({'selectionRef': selectionRef, 'selectionReview': selectionReview} if selectionRef is not None else {}),
                        **({'advanceLifecycle': True} if advanceLifecycle else {}),
                        **({'automaticRepair': True} if automaticRepair else {})}:
                    raise StructureError('ARTIFACT_REQUEST_REUSED', 'requestId identifies different content')
                prior_path = request['receipt']['artifactPath']
                prior_raw = observe(prior_path)
                prior = json.loads(prior_raw)
                from .review_artifacts import validate_artifact_binding
                validate_artifact_binding(prior, project_id=structure['projectId'], chapter_id=chapterId)
                if (prior['artifactSha256'] != request['receipt']['artifactSha256']
                        or digest(prior_raw) != request['artifactFileSha256']):
                    raise StructureError('ARTIFACT_REQUEST_INVALID', 'recorded artifact was changed')
                if (request.get('dependencyHash') != dependency_hash or prior['payload'] != artifact['payload']
                        or prior.get('sourceSha256') != digest(raw)):
                    raise StructureError('ARTIFACT_REQUEST_STALE', 'request binding dependencies changed; use a new requestId')
                validate_guards(root, list(guards.values()), stage='artifact-pack-replay')
                validate_guards(root, list(stage_guards.values()), stage='artifact-pack-stage-replay')
                current = workflow.get('artifacts', {}).get(chapterId, {}).get(artifactType, {})
                current_hash = current.get('artifactSha256') if not current.get('invalidatedBy') else None
                if automaticRepair:
                    from .workflow import validate_automatic_repair
                    validate_automatic_repair(workflow, chapterId, request['receipt'].get('repairAccounting'),
                                              request_id=requestId)
                return {**request['receipt'], 'replayed': True,
                        'registered': current_hash == prior['artifactSha256'],
                        'approved': current_hash == prior['artifactSha256'] and
                            workflow.get('approvals', {}).get(chapterId, {}).get('artifactHashes', {}).get(artifactType) == prior['artifactSha256'],
                        'approvalPerformed': False}
        repair_accounting = None
        if automaticRepair:
            from .workflow import record_automatic_repair
            repair_accounting = record_automatic_repair(workflow, chapterId, request_id=requestId)
        output = f'工作台/{chapterId}-{artifactType}-{artifact["artifactSha256"]}.json'
        if output == input_relative:
            raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'pack cannot replace its input')
        artifact_bytes = json_bytes(artifact)
        from .workflow import artifact_replacement
        replacement = artifact_replacement(workflow, chapterId, artifact)
        record_artifact(workflow, chapterId, artifact, structure=structure, project_id=structure['projectId'],
                        project_root=root, artifact_path=str(safe_project_path(root, output)), artifact_bytes=artifact_bytes)
        workflow['artifacts'][chapterId][artifactType]['bindings'] = bindings
        if _input_receipt is not None:
            workflow['artifacts'][chapterId][artifactType]['patchReceipt'] = {
                'path': str(Path(input_relative).with_suffix('.receipt.json')),
                'sha256': digest(json_bytes(_input_receipt))}
        proposed = None
        if advanceLifecycle:
            if artifactType != 'candidate_prose':
                raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'only candidate prose can advance to draft')
            if chapter['status'] == 'planned':
                from .chapter_state import prepare_transition
                proposed, _ = prepare_transition(root, structure, workflow, chapterId, 'draft', actor='artifact_pack', pending_candidate=payload)
            elif chapter['status'] not in {'draft', 'review'}:
                raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'candidate lifecycle requires planned, draft or review')
        workflow['updatedAt'] = utc_now()
        outputs = {output: artifact_bytes, '.govel/workflow.json': json_bytes(workflow)}
        created_paths = set()
        if _prepared_input is not None:
            if input_path.exists():
                if observe(input_relative) != raw:
                    raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'prepared input path already has different content')
            else:
                outputs[input_relative] = raw
                created_paths.add(input_relative)
        if prepared_notes is not None:
            note_path = safe_project_path(root, notes)
            if note_path.exists():
                if observe(notes) != prepared_notes:
                    raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'prepared notes path already has different content')
            else:
                outputs[notes] = prepared_notes
                created_paths.add(notes)
        if _input_receipt is not None:
            receipt_path = str(Path(input_relative).with_suffix('.receipt.json'))
            receipt_bytes = json_bytes(_input_receipt)
            existing = safe_project_path(root, receipt_path)
            if existing.exists():
                if observe(receipt_path) != receipt_bytes:
                    raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'patch receipt already has different content')
            else:
                outputs[receipt_path] = receipt_bytes
                created_paths.add(receipt_path)
        receipt = {'ok': True, 'code': 'ARTIFACT_PACKED', 'chapterId': chapterId, 'artifactType': artifactType,
                   'artifactPath': output, 'artifactSha256': artifact['artifactSha256'], 'bindings': bindings,
                   'registered': True, 'approved': False, 'approvalPerformed': False, 'inputPath': input_relative}
        if replacement is not None:
            receipt['replacement'] = replacement
        if artifactType == 'candidate_prose':
            receipt.update(draftSha256=digest(payload['text'].encode()),
                           stageAArtifactSha256=payload.get('stageAArtifactSha256'), writingNotes=payload.get('writingNotes', {}),
                           candidateSelection=payload.get('candidateSelection'))
        if repair_accounting is not None:
            receipt['repairAccounting'] = repair_accounting
        if request_path is not None:
            request = {'schemaVersion': 1, 'requestId': requestId,
                       'inputIdentity': {'chapterId': chapterId, 'artifactType': artifactType,
                                         'inputPath': input_relative, 'inputSha256': digest(raw),
                                         **({'selectionRef': selectionRef, 'selectionReview': selectionReview} if selectionRef is not None else {}),
                                         **({'advanceLifecycle': True} if advanceLifecycle else {}),
                                         **({'automaticRepair': True} if automaticRepair else {})},
                       'dependencyHash': dependency_hash, 'artifactFileSha256': digest(artifact_bytes), 'receipt': receipt}
            if _input_receipt is not None:
                request['inputReceiptSha256'] = digest(json_bytes(_input_receipt))
            if repair_dependencies is not None:
                request['repairDependencies'] = repair_dependencies
            request['artifactStateHashes'] = {
                kind: sha256_json(entry) for kind, entry in workflow.get('artifacts', {}).get(chapterId, {}).items()}
            request['structureStateHash'] = sha256_json(proposed if proposed is not None else structure)
            request['chapterStateHash'] = sha256_json(workflow.get('chapterStates', {}).get(chapterId))
            request['requestHash'] = sha256_json(request)
            outputs[request_path] = json_bytes(request)
            created_paths.add(request_path)
        if proposed is not None:
            from .derived_views import derived_outputs
            outputs, _ = derived_outputs(root, proposed, current=structure, extras=outputs)
        expected = inventory(root, outputs)
        for row in expected:
            if row['path'] in created_paths and row['before']['exists']:
                raise StructureError('ARTIFACT_INPUT_OVERWRITE', 'a new input or receipt appeared during packaging', {'path': row['path']})
        old_workflow = next(row['before'] for row in expected if row['path'] == '.govel/workflow.json')
        if old_workflow['sha256'] != (digest(workflow_raw) if workflow_raw is not None else None):
            raise StructureError('WRITE_SET_CONFLICT', 'workflow changed while packaging')
        validate_guards(root, list(guards.values()), stage='artifact-pack')
        validate_guards(root, list(stage_guards.values()), stage='artifact-pack-stage-publication')
        for row in expected:
            observed = stage_guards.get(row['path'], guards.get(row['path']))
            if observed and row['before']['sha256'] != observed['sha256']:
                raise StructureError('WRITE_SET_CONFLICT', 'lifecycle input changed before publication')
        apply_write_set(root, outputs, expected=expected,
                        guards=[g for g in {**guards, **stage_guards}.values() if g['path'] not in outputs], operation='artifact_pack')
        approved_hash = workflow.get('approvals', {}).get(chapterId, {}).get('artifactHashes', {}).get(artifactType)
        return {**receipt, 'approved': approved_hash == artifact['artifactSha256']}


def pack_artifact(root: Path, *, advanceLifecycle: bool = False, diagnostics: dict | None = None,
                  text: str | None = None, payload: dict | None = None, automaticRepair: bool = False,
                  _full_report: bool = False, **kwargs) -> dict:
    """One call for packaging and optional diagnostics, with recoverable atomic stages."""
    if type(automaticRepair) is not bool or automaticRepair and kwargs.get('artifactType') != 'candidate_prose':
        raise StructureError('AUTOMATIC_REPAIR_INVALID', 'automaticRepair is a boolean for candidate prose only')
    if automaticRepair and (not isinstance(kwargs.get('requestId'), str) or not kwargs['requestId'].strip()):
        raise StructureError('AUTOMATIC_REPAIR_REQUEST_REQUIRED', 'automatic repair requires a requestId')
    if text is not None or payload is not None:
        if 'input' in kwargs or '_prepared_input' in kwargs or (text is not None and payload is not None):
            raise StructureError('ARTIFACT_INPUT_INVALID', 'choose exactly one of input, text or payload')
        if text is not None:
            if kwargs.get('artifactType') != 'candidate_prose' or not isinstance(text, str):
                raise StructureError('ARTIFACT_INPUT_INVALID', 'text is only supported for candidate prose')
            raw, suffix = text.encode('utf-8'), 'txt'
        else:
            if kwargs.get('artifactType') == 'candidate_prose' or not isinstance(payload, dict):
                raise StructureError('ARTIFACT_INPUT_INVALID', 'payload is a non-prose content object')
            # Canonical bytes keep identical JSON objects independent of key order.
            raw, suffix = json_bytes(json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))), 'json'
        kwargs.update(input=f'工作台/inputs/{digest(raw)}.{suffix}', _prepared_input=raw)
    if 'input' not in kwargs:
        raise StructureError('ARTIFACT_INPUT_INVALID', 'provide input, text or payload')
    if type(advanceLifecycle) is not bool or diagnostics is not None and (not advanceLifecycle or kwargs.get('artifactType') != 'candidate_prose'):
        raise StructureError('LIFECYCLE_ADVANCE_INVALID', 'diagnostics requires candidate prose and advanceLifecycle')
    if diagnostics is not None:
        from .operations import validate_arguments
        from .operation_schemas import DIAGNOSTICS
        validate_arguments(DIAGNOSTICS, diagnostics)
    try:
        result = _pack_artifact(root, advanceLifecycle=advanceLifecycle, automaticRepair=automaticRepair, **kwargs)
    except (ValueError, OSError) as exc:
        add_review_recovery(exc, chapter_id=kwargs.get('chapterId'), artifact_type=kwargs.get('artifactType'))
        raise
    if diagnostics is not None:
        from .mechanical_evidence import run_diagnostics
        try:
            result['diagnostics'] = run_diagnostics(root, chapter_id=kwargs['chapterId'],
                mode=diagnostics.get('mode'), intensity_level=diagnostics.get('intensityLevel'),
                scene_paragraphs=diagnostics.get('sceneParagraphs', ''), strict_floor=diagnostics.get('strictFloor', False),
                register=True, advance_lifecycle=True, _expected_candidate_sha256=result['artifactSha256'], view='model')
        except (ValueError, OSError) as exc:
            result = {**result, 'ok': False, 'code': 'ARTIFACT_PACK_DIAGNOSTICS_INCOMPLETE',
                    'diagnosticsError': {'code': getattr(exc, 'code', 'DIAGNOSTICS_FAILED'), 'message': str(exc)},
                    'instruction': '候选登记已完成；修正诊断参数或故障后重试 diagnostics.aggregate。'}
            return result if _full_report else bounded_authoring_report(root, result)
    if kwargs.get('artifactType') == 'stage_a_review':
        from .outline_facts import inspect_outline
        result['outlineFacts'] = inspect_outline(root.resolve(), chapterId=kwargs['chapterId'])
    if advanceLifecycle:
        result['lifecycleStatus'] = next(r['status'] for r in load_structure(root)['chapters'] if r['chapterId'] == kwargs['chapterId'])
    return result if _full_report else bounded_authoring_report(root, result)
