"""One read-only selection relation for active, registered and formal prose.

An active version never becomes a registered artifact merely because its bytes
match. Handoff snapshots freeze the selection, plan, A and retained writing notes;
the caller supplies the applicability judgment. No approval is produced here.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .paths import stored_project_path
from .artifacts import sha256_json, sha256_text
from .structure_store import StructureError
from .write_sets import guard_observation, safe_project_path, validate_guards


def selected_candidate(root, structure, chapter_id, *, observe=None):
    from .candidate_versions import WORKBENCH_DIR, validate_candidate_index
    root = Path(root).expanduser().resolve()
    guards = []

    def read(relative):
        if observe is not None:
            raw, state = observe(relative)
        else:
            state, raw = guard_observation(safe_project_path(root, relative))
            state = {'path': relative, **state}
        guards.append(state)
        return raw, state

    directory = f'{WORKBENCH_DIR}/{chapter_id}'
    raw, state = read(directory + '/index.json')
    if raw is None:
        if state['exists']:
            raise StructureError('CANDIDATE_INDEX_INVALID', 'candidate index is not a readable regular file')
        return {'selection': None, 'text': None, 'guards': guards}
    index = json.loads(raw)
    validate_candidate_index(index, chapter_id, project_id=structure['projectId'])
    active = index['activeVersion']
    if active is None:
        return {'selection': None, 'text': None, 'guards': guards}
    row = next(item for item in index['versions'] if item['version'] == active)
    name = row.get('fileName')
    if not isinstance(name, str) or Path(name).name != name:
        raise StructureError('CANDIDATE_INDEX_INVALID', 'candidate filename must remain inside its chapter')
    raw, state = read(directory + '/' + name)
    if raw is None or state['sha256'] != row.get('bodySha256') or len(raw) != row.get('byteLength'):
        raise StructureError('CANDIDATE_VERSION_CORRUPT', 'active candidate bytes differ from its saved identity',
                             {'chapterId': chapter_id, 'candidateVersion': active, 'path': state['path']})
    identity = {'basis': 'active-candidate-v1', 'projectRoot': str(root), 'projectId': structure['projectId'],
                'chapterId': chapter_id, 'candidateVersion': active, 'activeToken': index['activeToken'],
                'bodySha256': row['bodySha256']}
    validate_guards(root, guards, stage='current-candidate-read')
    return {'selection': identity, 'text': raw.decode('utf-8'), 'guards': guards}


def same_selection(root, actual, declared):
    if actual == declared:
        return True
    from .paths import stored_root_matches
    return (isinstance(actual, dict) and isinstance(declared, dict)
            and stored_root_matches(root, actual.get('projectRoot'))
            and stored_root_matches(root, declared.get('projectRoot'))
            and {k: v for k, v in actual.items() if k != 'projectRoot'} ==
                {k: v for k, v in declared.items() if k != 'projectRoot'})


def require_candidate_selection(root, structure, chapter_id, payload, *, observe=None):
    selected = selected_candidate(root, structure, chapter_id, observe=observe)
    actual, declared = selected['selection'], payload.get('candidateSelection') if payload else None
    if actual is None and declared is None:
        return selected
    if not same_selection(root, actual, declared) or payload is None or (actual is not None and sha256_text(payload.get('text', '')) != actual['bodySha256']):
        raise StructureError('CURRENT_CANDIDATE_UNALIGNED',
            '激活选择与登记候选未对齐；先读取冻结转交材料并核对计划、阶段A与写作说明。',
            {'chapterId': chapter_id, 'selectedCandidate': actual, 'registeredSelection': declared,
             'recovery': {'automatic': False, 'preservesExistingEvidence': True,
                          'readActions': [{'operation': 'workflow.candidate-handoff', 'arguments': {'chapterId': chapter_id}}]}})
    review = payload.get('selectionReview')
    from .review_artifacts import require_completed_text
    if not isinstance(review, dict):
        raise StructureError('CANDIDATE_SELECTION_REVIEW_REQUIRED', 'selected candidate requires an explicit plan applicability judgment')
    require_completed_text(review.get('planApplicabilityReason'), 'selectionReview.planApplicabilityReason')
    frozen_previous = payload.get('selectionHandoff', {}).get('previousCandidate')
    if frozen_previous and frozen_previous.get('writingNotes'):
        require_completed_text(review.get('notesReason'), 'selectionReview.notesReason')
        disposition = review.get('notesDisposition')
        notes = payload.get('writingNotes')
        if disposition not in {'retained', 'revised', 'withdrawn'} or notes is None:
            raise StructureError('WRITING_NOTES_REQUIRED', 'selected candidate must explicitly address frozen writing notes')
        if disposition == 'retained' and notes != frozen_previous['writingNotes'] or disposition == 'withdrawn' and notes:
            raise StructureError('WRITING_NOTES_DISPOSITION_MISMATCH', 'writing notes differ from their declared disposition')
    # Preserve the original identity used by diagnostics and downstream recipes.
    selected['selection'] = deepcopy(declared)
    return selected


def _workflow(root):
    from .finalization import _load_workflow
    return _load_workflow(root)


def _handoff_snapshot(root, structure, workflow, chapter_id):
    from .mechanical_evidence import _registered
    from .source_plan import chapter_plan
    selected = selected_candidate(root, structure, chapter_id)
    if selected['selection'] is None:
        raise StructureError('CANDIDATE_SELECTION_MISSING', 'there is no active candidate to hand off')
    plan = chapter_plan(structure, chapter_id)
    stage_a = _registered(root, structure, workflow, chapter_id, 'stage_a_review')
    previous = _registered(root, structure, workflow, chapter_id, 'candidate_prose', historical=True)
    a_hash = stage_a['artifactSha256'] if stage_a else None
    a_current = bool(stage_a and stage_a['payload'].get('sourcePlanHash') == plan['sourcePlanHash'])
    return {'selection': selected['selection'], 'text': selected['text'],
            'sourcePlanHash': plan['sourcePlanHash'], 'stageAArtifactSha256': a_hash,
            'stageAPlanCurrent': a_current,
            'stageAApproved': bool(a_current and workflow.get('approvals', {}).get(chapter_id, {}).get('approved')
                and workflow.get('approvals', {}).get(chapter_id, {}).get('artifactHashes', {}).get('stage_a_review') == a_hash),
            'previousCandidate': ({'artifactSha256': previous['artifactSha256'],
                 'artifactPath': workflow['artifacts'][chapter_id]['candidate_prose']['artifactPath'],
                 'artifactFileSha256': workflow['artifacts'][chapter_id]['candidate_prose']['artifactFileSha256'],
                 'writingNotes': deepcopy(previous['payload'].get('writingNotes', {}))} if previous else None)}, selected['guards']


def prepare_candidate_handoff(root, *, chapterId):
    from .consistency import require_no_pending
    from .locks import project_lock_reentrant
    from .structure_store import load_structure
    from .transport_cache import put
    from .write_sets import capture_guards
    root = Path(root).expanduser().resolve()
    with project_lock_reentrant(root):
        require_no_pending(root)
        guards = capture_guards(root, ['.govel/structure.json', '.govel/workflow.json'])
        structure, workflow = load_structure(root), _workflow(root)
        if not any(c['chapterId'] == chapterId and c.get('status') != 'archived' for c in structure['chapters']):
            raise StructureError('CHAPTER_NOT_FOUND', 'handoff requires an active chapter')
        snapshot, selected_guards = _handoff_snapshot(root, structure, workflow, chapterId)
        for kind in ('candidate_prose', 'stage_a_review'):
            entry = workflow.get('artifacts', {}).get(chapterId, {}).get(kind, {})
            if entry.get('artifactPath'):
                guards.extend(capture_guards(root, [stored_project_path(root, entry['artifactPath']).relative_to(root).as_posix()]))
        validate_guards(root, [*guards, *selected_guards], stage='candidate-handoff')
        reference = put(root, 'candidate-handoff', snapshot)
        report = {'ok': True, 'code': 'CANDIDATE_HANDOFF_PREPARED', 'chapterId': chapterId,
                'selectionRef': reference, **snapshot, 'registered': False, 'approved': False,
                'instruction': '读取冻结正文、计划与阶段A，实际判断适用性；显式保留、修订或撤回旧写作说明。转交不自动批准，输入变化需重新核验。',
                'readAction': {'operation': 'task.snapshot', 'arguments': {'reference': reference}},
                'submitAction': {'operation': 'workflow.artifact-pack', 'arguments': {
                    'chapterId': chapterId, 'artifactType': 'candidate_prose', 'selectionRef': reference},
                    'requiredInputs': ['text', 'selectionReview'],
                    'selectionReviewFields': ['planApplicabilityReason', 'notesDisposition', 'notesReason']}}
        from .artifact_pack import bounded_authoring_report
        return bounded_authoring_report(root, report)


def selection_review_schema():
    from .operation_schemas import object_schema, TEXT
    return object_schema({'planApplicabilityReason': TEXT,
                          'notesDisposition': {'type': 'string', 'enum': ['retained', 'revised', 'withdrawn']},
                          'notesReason': TEXT}, ('planApplicabilityReason',))


def bind_candidate_handoff(root, structure, workflow, chapter_id, *, reference, review, text, notes):
    from .mechanical_evidence import _registered
    from .review_artifacts import require_completed_text
    from .source_plan import chapter_plan
    from .transport_cache import get
    from .argument_validation import validate_arguments
    frozen = get(root, reference, kind='candidate-handoff')
    selected = selected_candidate(root, structure, chapter_id)
    stage_a = _registered(root, structure, workflow, chapter_id, 'stage_a_review')
    if (not same_selection(root, frozen.get('selection'), selected['selection']) or frozen.get('text') != text
            or selected['text'] != text or frozen.get('sourcePlanHash') != chapter_plan(structure, chapter_id)['sourcePlanHash']
            or frozen.get('stageAArtifactSha256') != (stage_a['artifactSha256'] if stage_a else None)):
        raise StructureError('CANDIDATE_HANDOFF_STALE', 'frozen handoff selection, prose, plan or A changed; do not rebind the original request')
    if not frozen.get('stageAPlanCurrent') or not frozen.get('stageAApproved') or not stage_a or not workflow.get('approvals', {}).get(chapter_id, {}).get('approved') or (
            workflow.get('approvals', {}).get(chapter_id, {}).get('artifactHashes', {}).get('stage_a_review') != stage_a['artifactSha256']):
        raise StructureError('CANDIDATE_STAGE_A_STALE', 'handoff needs the exact current, approved stage A before applicability can be confirmed')
    validate_arguments(selection_review_schema(), review)
    require_completed_text(review.get('planApplicabilityReason'), 'selectionReview.planApplicabilityReason')
    previous = frozen.get('previousCandidate')
    if previous and previous.get('writingNotes'):
        disposition = review.get('notesDisposition')
        require_completed_text(review.get('notesReason'), 'selectionReview.notesReason')
        if notes is None or disposition not in ('retained', 'revised', 'withdrawn'):
            raise StructureError('WRITING_NOTES_REQUIRED', 'explicitly retain, revise or withdraw the frozen writing notes')
        if disposition == 'retained' and notes != previous['writingNotes'] or disposition == 'withdrawn' and notes:
            raise StructureError('WRITING_NOTES_DISPOSITION_MISMATCH', 'notes do not match the explicit handoff disposition')
    payload = {'candidateSelection': deepcopy(frozen['selection']), 'selectionReview': deepcopy(review),
               'sourcePlanHash': frozen['sourcePlanHash'], 'stageAArtifactSha256': frozen['stageAArtifactSha256'],
               'selectionHandoff': {'snapshotSha256': sha256_json(frozen), 'previousCandidate': deepcopy(previous)}}
    return payload, selected['guards']
