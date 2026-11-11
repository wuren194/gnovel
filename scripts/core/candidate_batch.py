"""Frozen candidate commissions and derived dependency recipes; never a second Canon.

The request owns only the author's existing scope and the frozen inputs. Current
progress is read from the original A/candidate/diagnostic/B and closeout receipts.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .paths import stored_project_path
from .artifacts import sha256_json, sha256_text
from .jsonio import read_json
from .locks import project_lock_reentrant
from .structure_store import StructureError, load_structure, ordered_chapters
from .write_sets import apply_write_set, capture_guards, json_bytes, safe_project_path, validate_guards


def _workflow(root):
    from .finalization import _load_workflow
    return _load_workflow(root)


def _record_path(root, request_id, project_id):
    identity = sha256_json({'projectId': project_id, 'requestId': request_id, 'purpose': 'candidate-batch'})
    return f'.govel/artifact-submissions/candidate-batch-{identity}.json'


def prepare_candidate_batch(root, *, requestId, chapterIds=None):
    from .review_artifacts import require_completed_text
    from .source_plan import chapter_plan
    root = Path(root).expanduser().resolve()
    require_completed_text(requestId, 'requestId')
    with project_lock_reentrant(root):
        source, workflow = load_structure(root), _workflow(root)
        relative = _record_path(root, requestId, source['projectId'])
        path = safe_project_path(root, relative)
        if path.exists():
            record = read_json(path)
            _validate_record(root, source, record)
            if chapterIds is not None and chapterIds != record['chapterIds']:
                raise StructureError('CANDIDATE_BATCH_REQUEST_REUSED', 'original candidate commission has a different range')
        else:
            if not isinstance(chapterIds, list) or not chapterIds or len(set(chapterIds)) != len(chapterIds):
                raise StructureError('CANDIDATE_BATCH_RANGE_REQUIRED', 'provide an explicit, nonempty, distinct ordered chapter range')
            ordered = [c['chapterId'] for c in ordered_chapters(source) if c.get('status') != 'archived']
            if any(cid not in ordered or cid not in workflow.get('approvedChapterIds', []) for cid in chapterIds):
                raise StructureError('CHAPTER_OUTSIDE_APPROVED_RANGE', 'candidate commission must be within existing host-authorized chapter scope')
            if sorted(chapterIds, key=ordered.index) != chapterIds:
                raise StructureError('CANDIDATE_BATCH_ORDER_INVALID', 'candidate range must follow chapter order')
            record = {'kind': 'candidate_batch', 'version': 1, 'projectRoot': str(root), 'projectId': source['projectId'],
                      'requestId': requestId, 'chapterIds': chapterIds,
                      'authorization': {'approvedChapterIds': deepcopy(workflow['approvedChapterIds'])},
                      'sourcePlans': {cid: chapter_plan(source, cid)['sourcePlanHash'] for cid in chapterIds}}
            record['requestHash'] = sha256_json(record)
            apply_write_set(root, {relative: json_bytes(record)}, operation='candidate_batch_request',
                guards=capture_guards(root, ['.govel/structure.json', '.govel/workflow.json']))
        reference = {'path': relative, 'requestHash': record['requestHash']}
        return {'ok': True, 'code': 'CANDIDATE_BATCH_PREPARED', 'batchRef': reference,
                'chapterIds': record['chapterIds'], 'requestId': requestId, 'formalApprovalPerformed': False,
                'nextAction': {'operation': 'workflow.candidate-recipe', 'arguments': {'batchRef': reference, 'chapterId': record['chapterIds'][0]}},
                'instruction': '本请求仅冻结候选委托；每章实际审校后可供后章候选使用。正式采用仍逐章走原批准与closeout。'}


def _validate_record(root, source, record):
    from .paths import stored_root_matches
    if (not isinstance(record, dict) or record.get('kind') != 'candidate_batch'
            or type(record.get('version')) is not int or record['version'] != 1 or not stored_root_matches(root, record.get('projectRoot'))
            or record.get('projectId') != source['projectId']
            or record.get('requestHash') != sha256_json({k: v for k, v in record.items() if k != 'requestHash'})):
        raise StructureError('CANDIDATE_BATCH_INVALID', 'candidate commission record is unavailable or changed')
    chapters = record.get('chapterIds')
    authorization = record.get('authorization', {})
    if (not isinstance(record.get('requestId'), str) or not record['requestId'].strip()
            or not isinstance(chapters, list) or not chapters
            or any(not isinstance(cid, str) or not cid for cid in chapters)
            or len(set(chapters)) != len(chapters)
            or not isinstance(record.get('sourcePlans'), dict) or set(record['sourcePlans']) != set(chapters)
            or any(not isinstance(value, str) or not value for value in record['sourcePlans'].values())
            or not isinstance(authorization, dict) or not isinstance(authorization.get('approvedChapterIds'), list)
            or any(cid not in authorization['approvedChapterIds'] for cid in chapters)):
        raise StructureError('CANDIDATE_BATCH_INVALID', 'candidate commission range and original authorization are incomplete')


def batch_record(root, source, reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'requestHash'}:
        raise StructureError('CANDIDATE_BATCH_INVALID', 'use the original candidate batch reference')
    record = read_json(safe_project_path(root, reference['path']))
    _validate_record(root, source, record)
    if reference['path'] != _record_path(root, record['requestId'], source['projectId']) or reference['requestHash'] != record['requestHash']:
        raise StructureError('CANDIDATE_BATCH_INVALID', 'candidate batch reference does not bind the original request')
    return record


def _artifact(root, source, workflow, cid, kind):
    from .mechanical_evidence import _registered
    artifact = _registered(root, source, workflow, cid, kind)
    if artifact is None:
        raise StructureError('CANDIDATE_PREDECESSOR_UNREADY', 'candidate continuation requires current real artifacts', {'chapterId': cid, 'artifactType': kind})
    return artifact


def _current_recipe(root, source, workflow, cid):
    entry = workflow.get('artifacts', {}).get(cid, {}).get('stage_a_review', {})
    if not entry.get('candidateRecipeHash'):
        # No batch authority is granted. The ordinary A gate retains its own
        # original freshness/error contract for non-batch chapters.
        return None
    from .mechanical_evidence import _registered
    stage_a = _registered(root, source, workflow, cid, 'stage_a_review')
    recipe = stage_a['payload'].get('candidateRecipe') if stage_a else None
    if not isinstance(recipe, dict) or sha256_json(recipe) != entry['candidateRecipeHash']:
        raise StructureError('CANDIDATE_RECIPE_INVALID', 'stage A candidate recipe differs from its registration')
    return recipe


def candidate_handoff(root, source, workflow, cid, *, visited=()):
    """Read reviewed conditional material; never registers source facts or B approval."""
    from .source_plan import chapter_plan
    from .review_artifacts import validate_registered_stage_b
    from .mechanical_evidence import evidence_current
    candidate = _artifact(root, source, workflow, cid, 'candidate_prose')
    a = _artifact(root, source, workflow, cid, 'stage_a_review')
    b = _artifact(root, source, workflow, cid, 'stage_b_review')
    diagnostic = _artifact(root, source, workflow, cid, 'diagnostics')
    from .chapter_state import _gate_planned
    _gate_planned(root, next(chapter for chapter in source['chapters'] if chapter['chapterId'] == cid), workflow)
    if cid in visited:
        raise StructureError('CANDIDATE_DEPENDENCY_CYCLE', 'candidate chain must be acyclic')
    recipe = a['payload'].get('candidateRecipe')
    if recipe:
        validate_recipe(root, source, workflow, cid, recipe, visited=(*visited, cid))
    plan_hash = chapter_plan(source, cid)['sourcePlanHash']
    from .workflow import require_approved_artifact
    if (require_approved_artifact(workflow, cid, 'stage_a_review') != a['artifactSha256']
            or a['payload'].get('sourcePlanHash') != plan_hash or candidate['payload'].get('sourcePlanHash') != plan_hash
            or candidate['payload'].get('stageAArtifactSha256') != a['artifactSha256']
            or b['payload'].get('sourcePlanHash') != plan_hash
            or b['payload'].get('candidateArtifactSha256') != candidate['artifactSha256']
            or b['payload'].get('finalProseSha256') != sha256_text(candidate['payload']['text'])
            or b['payload'].get('diagnosticsArtifactSha256') != diagnostic['artifactSha256']
            or not evidence_current(root, cid, structure=source)['current']):
        raise StructureError('CANDIDATE_PREDECESSOR_UNREADY', 'candidate predecessor inputs or actual review evidence are not current', {'chapterId': cid})
    validate_registered_stage_b(root, source, workflow, cid, b['payload'])
    if b['payload'].get('verdict') != 'POLISH_READY':
        raise StructureError('CANDIDATE_PREDECESSOR_UNREADY', 'candidate predecessor has unresolved review work', {'chapterId': cid})
    from .fact_inventory import fact_findings
    conditional = [{'findingId': key, 'summary': row['summary'], 'factChange': deepcopy(row['factChange'])}
                   for key, row in fact_findings(b['payload']).items()]
    return {'chapterId': cid, 'candidateArtifactSha256': candidate['artifactSha256'],
            'sourcePlanHash': plan_hash, 'effectivePlanHash': (recipe or {}).get('effectivePlanHash'),
            'stageAArtifactSha256': a['artifactSha256'], 'stageBArtifactSha256': b['artifactSha256'],
            'diagnosticsArtifactSha256': diagnostic['artifactSha256'],
            'candidateSelection': candidate['payload'].get('candidateSelection'),
            'bodySha256': sha256_text(candidate['payload']['text']),
            'readSources': {kind: {key: workflow['artifacts'][cid][kind][key] for key in ('artifactPath', 'artifactSha256', 'artifactFileSha256')}
                            for kind in ('candidate_prose', 'stage_a_review', 'stage_b_review', 'diagnostics')},
            'factAssessment': deepcopy(b['payload']['factAssessment']), 'conditionalFacts': conditional,
            'materialRole': 'conditional-candidate-not-canon'}


def prepare_candidate_recipe(root, *, batchRef, chapterId):
    from .source_plan import chapter_plan
    from .transport_cache import put
    root = Path(root).expanduser().resolve()
    with project_lock_reentrant(root):
        source, workflow = load_structure(root), _workflow(root)
        batch = batch_record(root, source, batchRef)
        if chapterId not in batch['chapterIds'] or chapterId not in workflow.get('approvedChapterIds', []):
            raise StructureError('CHAPTER_OUTSIDE_APPROVED_RANGE', 'chapter is outside this frozen candidate commission')
        chapter = next(c for c in source['chapters'] if c['chapterId'] == chapterId)
        plan = chapter_plan(source, chapterId)
        if plan['sourcePlanHash'] != batch['sourcePlans'][chapterId]:
            raise StructureError('CANDIDATE_BATCH_PLAN_STALE', 'source plan changed after the commission; review before a new request')
        inputs = []
        for previous in ordered_chapters(source):
            cid = previous['chapterId']
            if previous['displayOrdinal'] >= chapter['displayOrdinal']:
                break
            if previous.get('status') == 'stable':
                from .continuity import continuity_status, finalization_evidence
                if not continuity_status(root, source, workflow, previous)['readyForContinuation']:
                    raise StructureError('CONTINUITY_REQUIRED', 'stable predecessor handoff is incomplete', {'chapterId': cid})
                evidence = finalization_evidence(root, source, workflow, previous)
                inputs.append({'chapterId': cid, 'formalBinding': deepcopy(previous['proseBinding']),
                               'filePath': previous['filePath'], 'finalization': evidence})
            else:
                if cid not in batch['chapterIds']:
                    raise StructureError('PREDECESSOR_UNSTABLE', 'unstable predecessor is outside the frozen commission', {'chapterId': cid})
                if cid not in workflow.get('approvedChapterIds', []):
                    raise StructureError('CHAPTER_OUTSIDE_APPROVED_RANGE', 'candidate predecessor authorization has been withdrawn', {'chapterId': cid})
                inputs.append(candidate_handoff(root, source, workflow, cid))
        recipe = {'basis': 'candidate-recipe-v1', 'batchRef': deepcopy(batchRef), 'projectId': source['projectId'],
                  'chapterId': chapterId, 'sourcePlanHash': plan['sourcePlanHash'], 'inputs': inputs}
        recipe['effectivePlanHash'] = sha256_json(recipe)
        reference = put(root, 'candidate-recipe', recipe)
        return {'ok': True, 'code': 'CANDIDATE_RECIPE_PREPARED', 'chapterId': chapterId,
                'recipeRef': reference, 'recipe': recipe, 'approved': False,
                'instruction': '阶段A和Writer须阅读这些确切候选输入；条件事实不进入Canon。上游变化需要重核，不能换绑原引用。'}


def resolve_recipe(root, source, workflow, chapter_id, reference):
    from .transport_cache import get
    recipe = get(root, reference, kind='candidate-recipe')
    validate_recipe(root, source, workflow, chapter_id, recipe)
    return recipe


def validate_recipe(root, source, workflow, chapter_id, recipe, *, visited=()):
    from .source_plan import chapter_plan
    if (not isinstance(recipe, dict) or recipe.get('basis') != 'candidate-recipe-v1'
            or recipe.get('projectId') != source['projectId'] or recipe.get('chapterId') != chapter_id
            or recipe.get('effectivePlanHash') != sha256_json({k: v for k, v in recipe.items() if k != 'effectivePlanHash'})):
        raise StructureError('CANDIDATE_RECIPE_INVALID', 'candidate recipe identity is invalid')
    batch = batch_record(root, source, recipe['batchRef'])
    if chapter_id not in batch['chapterIds'] or chapter_id not in workflow.get('approvedChapterIds', []):
        raise StructureError('CHAPTER_OUTSIDE_APPROVED_RANGE', 'candidate recipe chapter is outside the frozen authorized range')
    if recipe['sourcePlanHash'] != chapter_plan(source, chapter_id)['sourcePlanHash']:
        raise StructureError('CANDIDATE_RECIPE_STALE', 'candidate recipe source plan changed')
    if recipe['sourcePlanHash'] != batch['sourcePlans'].get(chapter_id):
        raise StructureError('CANDIDATE_BATCH_PLAN_STALE', 'candidate recipe differs from the original commission plan')
    chapter = next(c for c in source['chapters'] if c['chapterId'] == chapter_id)
    previous = [c for c in ordered_chapters(source) if c['displayOrdinal'] < chapter['displayOrdinal']]
    if [c['chapterId'] for c in previous] != [item.get('chapterId') for item in recipe['inputs']]:
        raise StructureError('CANDIDATE_RECIPE_STALE', 'candidate predecessor order or membership changed')
    for prior, item in zip(previous, recipe['inputs']):
        cid = prior['chapterId']
        if cid in visited or cid == chapter_id:
            raise StructureError('CANDIDATE_DEPENDENCY_CYCLE', 'candidate inputs contain a cycle')
        if 'formalBinding' not in item and (cid not in batch['chapterIds'] or cid not in workflow.get('approvedChapterIds', [])):
            raise StructureError('CHAPTER_OUTSIDE_APPROVED_RANGE', 'candidate predecessor is outside the original commission or current authorization')
        if 'formalBinding' in item:
            from .continuity import finalization_evidence, continuity_status
            if (prior['proseBinding'] != item['formalBinding'] or prior['filePath'] != item['filePath']
                    or finalization_evidence(root, source, workflow, prior) != item['finalization']
                    or not continuity_status(root, source, workflow, prior)['readyForContinuation']):
                raise StructureError('CANDIDATE_RECIPE_STALE', 'formal predecessor changed', {'chapterId': cid})
        elif prior.get('status') == 'stable':
            require_conversion(root, source, workflow, prior, item)
        elif candidate_handoff(root, source, workflow, cid, visited=visited) != item:
            raise StructureError('CANDIDATE_RECIPE_STALE', 'candidate predecessor identity or reviewed material changed', {'chapterId': cid})
    return recipe


def require_artifact_recipe(root, source, workflow, cid, payload):
    recipe = payload.get('candidateRecipe')
    stage = _current_recipe(root, source, workflow, cid)
    if recipe is None and stage is None:
        return None
    if recipe != stage:
        raise StructureError('CANDIDATE_RECIPE_STALE', 'candidate must inherit the exact approved A recipe')
    return validate_recipe(root, source, workflow, cid, recipe)


def require_candidate_predecessors(root, source, workflow, chapter, *, recipe=None):
    recipe = recipe or _current_recipe(root, source, workflow, chapter['chapterId'])
    if recipe is None:
        return False
    validate_recipe(root, source, workflow, chapter['chapterId'], recipe)
    return True


def conversion_witness(source, chapter, artifacts, target):
    candidate = artifacts['candidate_prose']
    recipe = candidate['payload'].get('candidateRecipe')
    if recipe is None:
        return None
    witness = {'basis': 'candidate-formal-conversion-v1', 'projectId': source['projectId'], 'chapterId': chapter['chapterId'],
               'candidateArtifactSha256': candidate['artifactSha256'],
               'sourcePlanHash': candidate['payload']['sourcePlanHash'], 'effectivePlanHash': recipe['effectivePlanHash'],
               'candidateSelection': candidate['payload'].get('candidateSelection'),
               'stageAArtifactSha256': artifacts['stage_a_review']['artifactSha256'],
               'stageBArtifactSha256': artifacts['stage_b_review']['artifactSha256'],
               'diagnosticsArtifactSha256': artifacts['diagnostics']['artifactSha256'],
               'bodySha256': sha256_text(candidate['payload']['text']), 'formalBinding': deepcopy(target['proseBinding']),
               'filePath': target['filePath']}
    witness['witnessHash'] = sha256_json(witness)
    return witness


def require_conversion(root, source, workflow, chapter, original):
    from .continuity import validated_continuity_record, finalization_evidence, continuity_status
    from .repair_history import read_versions
    record = validated_continuity_record(workflow, chapter['chapterId'])
    witness = (record or {}).get('candidateConversion')
    if (not isinstance(witness, dict) or witness.get('witnessHash') != sha256_json({k: v for k, v in witness.items() if k != 'witnessHash'})
            or witness.get('projectId') != source['projectId'] or witness.get('formalBinding') != chapter['proseBinding']
            or witness.get('filePath') != chapter['filePath']
            or any(witness.get(key) != original.get(key) for key in ('chapterId', 'candidateArtifactSha256', 'sourcePlanHash',
                'effectivePlanHash', 'stageAArtifactSha256', 'stageBArtifactSha256', 'diagnosticsArtifactSha256', 'candidateSelection', 'bodySha256'))):
        raise StructureError('CANDIDATE_CONVERSION_UNPROVEN', 'formal identity lacks the exact committed candidate conversion', {'chapterId': chapter['chapterId']})
    final = finalization_evidence(root, source, workflow, chapter)
    from .source_plan import chapter_plan
    if chapter_plan(source, chapter['chapterId'])['sourcePlanHash'] != witness['sourcePlanHash']:
        raise StructureError('CANDIDATE_RECIPE_STALE', 'formal adoption changed authored predecessor content; re-review its dependent candidate chain')
    if final['factReview']['artifactSha256'] != original['stageBArtifactSha256']:
        raise StructureError('CANDIDATE_CONVERSION_UNPROVEN', 'formal review differs from the selected candidate review')
    from .artifacts import file_sha256
    from .review_artifacts import validate_artifact_binding
    retained = {}
    for kind, pointer in original.get('readSources', {}).items():
        path = stored_project_path(root, pointer['artifactPath'])
        artifact = read_json(path)
        validate_artifact_binding(artifact, project_id=source['projectId'], chapter_id=chapter['chapterId'])
        if (artifact['artifactType'] != kind or artifact['artifactSha256'] != pointer['artifactSha256']
                or file_sha256(path) != pointer['artifactFileSha256']):
            raise StructureError('CANDIDATE_CONVERSION_UNPROVEN', 'frozen conversion source changed', {'artifactType': kind})
        retained[kind] = artifact
    if set(retained) != {'candidate_prose', 'stage_a_review', 'stage_b_review', 'diagnostics'}:
        raise StructureError('CANDIDATE_CONVERSION_UNPROVEN', 'conversion must retain all original source artifacts')
    from .fact_inventory import fact_findings
    retained_b = retained['stage_b_review']['payload']
    facts = [{'findingId': key, 'summary': row['summary'], 'factChange': deepcopy(row['factChange'])}
             for key, row in fact_findings(retained_b).items()]
    if original.get('conditionalFacts') != facts or original.get('factAssessment') != retained_b.get('factAssessment'):
        raise StructureError('CANDIDATE_CONVERSION_UNPROVEN', 'conditional handoff differs from the actually finalized B')
    if (record.get('conversionReceipt') != witness['witnessHash']
            or not continuity_status(root, source, workflow, chapter)['readyForContinuation']):
        raise StructureError('CANDIDATE_CONVERSION_HANDOFF_PENDING', 'identity converted but actual continuity/fact handoff is incomplete')
    committed_conversion_receipt(root, chapter['chapterId'], witness, stage='finalize')
    committed_conversion_receipt(root, chapter['chapterId'], witness, stage='continuity')
    for kind, key in (('candidate_prose', 'candidateArtifactSha256'), ('stage_b_review', 'stageBArtifactSha256'), ('diagnostics', 'diagnosticsArtifactSha256')):
        versions, _ = read_versions(root, source, workflow, chapter['chapterId'], kind)
        if not any(v['report']['artifactSha256'] == original[key] for v in versions):
            raise StructureError('CANDIDATE_CONVERSION_UNPROVEN', 'conversion artifact bytes cannot be recovered', {'artifactType': kind})
    return witness


def batch_progress(root, *, batchRef):
    root = Path(root).expanduser().resolve()
    source, workflow = load_structure(root), _workflow(root)
    batch = batch_record(root, source, batchRef)
    rows = []
    for cid in batch['chapterIds']:
        row = {'chapterId': cid, 'state': 'not_started', 'ready': False}
        entry = workflow.get('artifacts', {}).get(cid, {}).get('candidate_prose')
        if entry:
            row.update(state='candidate', artifactPath=entry.get('artifactPath'), artifactSha256=entry.get('artifactSha256'))
            try:
                item = candidate_handoff(root, source, workflow, cid)
                row.update(state='reviewed_candidate', ready=True, input=item)
            except (ValueError, OSError) as exc:
                row.update(state='needs_review', blocker={'code': getattr(exc, 'code', 'CANDIDATE_UNAVAILABLE'), 'message': str(exc)})
        rows.append(row)
    return {'ok': True, 'code': 'CANDIDATE_BATCH_PROGRESS', 'batchRef': batchRef, 'chapters': rows,
            'complete': all(row['ready'] for row in rows), 'formalApprovalPerformed': False,
            'resumeAction': {'operation': 'workflow.candidate-recipe', 'arguments': {
                'batchRef': batchRef, 'chapterId': next((row['chapterId'] for row in rows if not row['ready']), rows[-1]['chapterId'])}}}


def recipe_guards(root, source, workflow, cid, recipe):
    """Freeze physical inputs for this publication, separately from semantic reuse."""
    from .candidate_versions import WORKBENCH_DIR
    from .current_candidate import selected_candidate
    paths = {recipe['batchRef']['path']}
    for item in recipe['inputs']:
        previous = item['chapterId']
        for kind in ('candidate_prose', 'stage_a_review', 'stage_b_review', 'diagnostics'):
            entry = workflow.get('artifacts', {}).get(previous, {}).get(kind, {})
            if entry.get('artifactPath'):
                paths.add(stored_project_path(root, entry['artifactPath']).relative_to(root).as_posix())
        chapter = next(c for c in source['chapters'] if c['chapterId'] == previous)
        if chapter.get('status') == 'stable' and 'formalBinding' not in item:
            witness = require_conversion(root, source, workflow, chapter, item)
            for stage in ('finalize', 'continuity'):
                paths.update(committed_conversion_receipt(root, previous, witness, stage=stage)['proofPaths'])
        paths.add(chapter['filePath'])
        paths.add(f'{WORKBENCH_DIR}/{previous}/index.json')
        selected = selected_candidate(root, source, previous)
        paths.update(row['path'] for row in selected['guards'])
    guards = capture_guards(root, sorted(paths))
    validate_recipe(root, source, workflow, cid, recipe)
    validate_guards(root, guards, stage='candidate-recipe-publication')
    return guards


def committed_conversion_receipt(root, chapter_id, witness, *, stage):
    """Read existing committed write-set proof; self-hashed workflow is insufficient.

    This consumes the terminal manifest and immutable after-images written by the
    original transaction. Prepared/rolled-back work and arbitrary hash assertions
    never prove a publication or a later continuity confirmation.
    """
    from .write_sets import digest, PUBLICATION_PROTOCOL
    operation = {'finalize': 'finalize_chapter', 'continuity': 'complete_continuity'}[stage]
    directory = safe_project_path(root, '.govel/transactions')
    for terminal in sorted(directory.glob('TX-*/completed.json')):
        relative = terminal.relative_to(root).as_posix()
        manifest = read_json(safe_project_path(root, relative))
        if manifest.get('operation') != operation or manifest.get('phase') != 'committed':
            continue
        if manifest.get('schemaVersion') != 2 or manifest.get('publicationProtocol') != PUBLICATION_PROTOCOL:
            continue
        txid = manifest.get('transactionId')
        if not isinstance(txid, str) or terminal.parent.name != txid:
            continue
        rows = manifest.get('files')
        if not isinstance(rows, list) or len({row.get('path') for row in rows}) != len(rows):
            continue
        by_path = {row.get('path'): row for row in rows}
        proofs = [relative]

        def image(path):
            row = by_path.get(path)
            if not row or row.get('after', {}).get('exists') is not True:
                raise ValueError('required output missing')
            location = f'.govel/transactions/{txid}/after/{path}'
            raw = safe_project_path(root, location).read_bytes()
            if digest(raw) != row['after'].get('sha256'):
                raise ValueError('immutable output image changed')
            proofs.append(location)
            return raw

        try:
            workflow = json.loads(image('.govel/workflow.json'))
            record = workflow.get('continuity', {}).get(chapter_id, {})
            if record.get('candidateConversion') != witness:
                continue
            from .continuity import validated_continuity_record
            validated_continuity_record(workflow, chapter_id)
            if stage == 'finalize':
                source = json.loads(image('.govel/structure.json'))
                chapter = next(c for c in source['chapters'] if c['chapterId'] == chapter_id)
                if (source['projectId'] != witness['projectId'] or chapter['proseBinding'] != witness['formalBinding']
                        or chapter['status'] != 'stable' or chapter['filePath'] != witness['filePath']
                        or sha256_text(image(chapter['filePath']).decode('utf-8')) != witness['bodySha256']
                        or record.get('status') != 'pending'
                        or record.get('factReview', {}).get('artifactSha256') != witness['stageBArtifactSha256']):
                    continue
            elif (record.get('status') != 'complete' or record.get('conversionReceipt') != witness['witnessHash']):
                continue
            return {'transactionId': txid, 'operation': operation, 'proofPaths': proofs,
                    'manifestSha256': digest(terminal.read_bytes())}
        except (ValueError, OSError, KeyError, TypeError, StopIteration):
            continue
    raise StructureError('CANDIDATE_CONVERSION_COMMIT_UNPROVEN',
                         'no committed original transaction proves this candidate conversion stage',
                         {'chapterId': chapter_id, 'stage': stage})


def list_candidate_batches(root):
    root = Path(root).expanduser().resolve()
    source = load_structure(root)
    rows = []
    directory = safe_project_path(root, '.govel/artifact-submissions')
    for path in sorted(directory.glob('candidate-batch-*.json')):
        record = read_json(path)
        _validate_record(root, source, record)
        rows.append({'requestId': record['requestId'], 'chapterIds': record['chapterIds'],
                     'batchRef': {'path': path.relative_to(root).as_posix(), 'requestHash': record['requestHash']}})
    return {'ok': True, 'code': 'CANDIDATE_BATCHES', 'batches': rows}


def candidate_reading(root, *, batchRef, _with_dependencies=False):
    """Freeze readable candidate material with explicit current/historical status."""
    from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
    from .transport_cache import put
    from .mechanical_evidence import _registered
    from .current_candidate import selected_candidate
    root = Path(root).expanduser().resolve()
    with project_lock_reentrant(root), observe_reads(root) as observed:
        dependencies = capture_dependencies(root)
        source, workflow = load_structure(root), _workflow(root)
        progress = batch_progress(root, batchRef=batchRef)
        rows = []
        for row in progress['chapters']:
            cid = row['chapterId']
            chapter = next(c for c in source['chapters'] if c['chapterId'] == cid)
            entry = {**row, 'title': chapter['title'], 'displayCode': chapter.get('displayCode', cid),
                     'text': None, 'materialRole': 'candidate-reading-not-formal-prose', 'suggestions': [], 'writingNotes': {}}
            try:
                selected = selected_candidate(root, source, cid)
                candidate = _registered(root, source, workflow, cid, 'candidate_prose', historical=True)
                if selected['selection'] is not None:
                    entry.update(text=selected['text'], candidateSelection=selected['selection'])
                    if not candidate or candidate['payload'].get('candidateSelection') != selected['selection']:
                        entry.update(state='selected_not_registered', ready=False, blocker={
                            'code': 'CURRENT_CANDIDATE_UNALIGNED', 'message': '已激活稿尚未转交登记，历史报告不作为当前审校。'})
                elif candidate:
                    entry['text'] = candidate['payload']['text']
                if candidate:
                    entry['writingNotes'] = deepcopy(candidate['payload'].get('writingNotes', {}))
                report = _registered(root, source, workflow, cid, 'stage_b_review', historical=True)
                if report:
                    entry.update(suggestions=deepcopy(report['payload'].get('findings', [])),
                                 reportArtifactSha256=report['artifactSha256'], reportIsCurrent=bool(row['ready']))
            except (ValueError, OSError) as exc:
                entry.update(state='unavailable', ready=False, text=None, blocker={
                    'code': getattr(exc, 'code', 'CANDIDATE_UNAVAILABLE'), 'message': str(exc)})
            rows.append(entry)
        reading = {'projectId': source['projectId'], 'bookName': source['bookName'], 'batchRef': deepcopy(batchRef),
                   'chapters': rows, 'complete': all(row['ready'] for row in rows),
                   'resumeAction': progress['resumeAction'], 'formalApprovalPerformed': False}
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='CANDIDATE_READING_STALE')
        reference = put(root, 'candidate-reading', reading)
        report = {'ok': True, 'code': 'CANDIDATE_READING_PREPARED', **reading, 'readingRef': reference,
                  'readAction': {'operation': 'task.snapshot', 'arguments': {'reference': reference}}}
        return (report, dependencies) if _with_dependencies else report
