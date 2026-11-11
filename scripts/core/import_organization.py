"""Retrospective import preparation and source transactions with exact prose guards."""
from copy import deepcopy
import json
from pathlib import Path

from .artifacts import sha256_json
from .file_preservation import observation
from .import_commit import RECEIPT
from .locks import project_lock
from .project import query_policy, canon_proposal_batch_max_items
from .source_model import snapshot_hash
from .structure_store import StructureError, load_structure
from .write_sets import GUARD_KEYS, json_bytes, safe_project_path, validate_guards

WORKFLOW = '.govel/workflow.json'


def _observed(root, relative):
    state, raw = observation(safe_project_path(root, relative))
    return {'path': relative, **{key: state[key] for key in GUARD_KEYS}}, raw


def imported_materials(root, source, chapter_ids):
    from .import_verification import import_receipt
    from .prose_binding import OWNED_CLEAN, require_observation
    guard, raw = _observed(root, RECEIPT)
    try:
        receipt = import_receipt(root, source)
        imported = {row['chapterId']: row for row in receipt['chapters']}
    except (ValueError, KeyError, TypeError) as exc:
        raise StructureError('IMPORT_ORIGIN_REQUIRED', 'organization requires a readable import receipt; do not manufacture provenance') from exc
    chapters = {row['chapterId']: row for row in source['chapters']}
    materials, guards = {}, [guard]
    for cid in chapter_ids:
        chapter, origin = chapters.get(cid), imported.get(cid)
        if chapter is None or origin is None or chapter['status'] not in {'planning_required', 'stable', 'stale'}:
            raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'select unchanged imported chapters', {'chapterId': cid})
        require_observation(root, source, chapter, (OWNED_CLEAN,), action='import organization')
        prose_guard, raw = _observed(root, chapter['filePath'])
        if prose_guard['sha256'] != origin['contentHash']:
            raise StructureError('IMPORT_PROSE_CHANGED', 'original imported prose changed; use the normal revision review', {'chapterId': cid})
        guards.append(prose_guard)
        materials[cid] = {'id': cid, 'chapterId': cid, 'title': chapter['title'], 'text': raw.decode('utf-8'),
                          'proseHash': prose_guard['sha256'], 'origin': 'imported_manuscript'}
    validate_guards(root, guards, stage='import-organization-read')
    return materials, guards


def prepare_organization(root: Path, *, chapterIds=None, offset=0, limit=None):
    from .consistency import require_no_pending
    from .transport_cache import put
    from .import_history import history_entry, history_status
    root = root.expanduser().resolve()
    require_no_pending(root)
    source = load_structure(root)
    guard, raw = _observed(root, WORKFLOW)
    workflow = json.loads(raw or b'{}')
    policy = query_policy()
    limit = policy['defaultLimit'] if limit is None else limit
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= policy['maxLimit']:
        raise StructureError('ARGUMENTS_INVALID', 'invalid organization page')
    try:
        from .import_verification import import_receipt
        origin = import_receipt(root, source)
        imported_ids = {row['chapterId'] for row in origin['chapters']}
    except (ValueError, KeyError, TypeError) as exc:
        raise StructureError('IMPORT_ORIGIN_REQUIRED', 'organization requires the actual import receipt') from exc
    ids = chapterIds if chapterIds is not None else [row['chapterId'] for row in sorted(source['chapters'], key=lambda r: r['displayOrdinal'])
        if row['chapterId'] in imported_ids and row.get('status') != 'archived' and not (history_entry(workflow, row['chapterId']) and
                                                  history_status(root, source, workflow, row)['complete'])]
    if (not isinstance(ids, list) or any(not isinstance(cid, str) for cid in ids) or len(set(ids)) != len(ids)
            or chapterIds is not None and not ids or offset > len(ids)):
        raise StructureError('IMPORT_ORGANIZATION_SCOPE', 'chapterIds must be distinct current identities')
    selected = ids[offset:offset + limit]
    if not selected:
        return {'ok': True, 'code': 'IMPORT_ORGANIZATION_PAGE_COMPLETE' if ids else 'IMPORT_ORGANIZATION_NO_PENDING',
                'selectedChapterIds': [], 'remaining': len(ids),
                'nextActions': followup_actions(root), 'semanticCompletenessProven': False}
    materials, guards = imported_materials(root, source, selected)
    snapshot = {'projectId': source['projectId'], 'sourceHash': snapshot_hash(source), 'chapterIds': selected,
                'guards': [*guards, guard], 'materials': list(materials.values())}
    reference = put(root, 'import-organization', snapshot)
    result = {'ok': True, 'code': 'IMPORT_ORGANIZATION_PREPARED', 'prepareRef': reference, 'baseRevision': source['revision'],
            'selectedChapterIds': selected, 'remaining': len(ids) - offset - len(selected),
            'nextOffset': offset + len(selected) if offset + len(selected) < len(ids) else None,
            'readAction': {'operation': 'import.organization-read', 'arguments': {'projectRoot': str(root), 'prepareRef': reference}},
            'fields': {'chapters': [{'chapterId': cid, 'synopsis': '', 'quotes': [],
                        'review': {'facts': '', 'relationships': '', 'continuity': '', 'structure': ''}, 'unresolved': [], 'existingRecordIds': []} for cid in selected],
                       'entities': [], 'facts': [], 'relations': [], 'acceptHistory': False},
            'instruction': '分页读完本批原稿，先查询已有实体；提取有引文的事实、关系与回溯梗概，填写实际审阅结论。未确定项放 unresolved。acceptHistory=true 表示提请作者采用已写原稿及本批事实盘点，不生成历史 A/B 或未来续写授权。',
            'nextActions': followup_actions(root)}
    from .query import _size
    if _size(result) > policy['maxBytes']['detail']:
        raise StructureError('IMPORT_PAGE_TOO_LARGE', 'prepare a smaller chapter batch with a lower limit')
    return result


def followup_actions(root):
    return [{'operation': 'source.query', 'arguments': {'projectRoot': str(root), 'view': 'overview'}},
            {'operation': 'source.voice-profile', 'arguments': {'projectRoot': str(root)}},
            {'operation': 'summary.prepare', 'arguments': {'projectRoot': str(root)}},
            {'operation': 'capabilities.help', 'arguments': {'operation': 'source.voice-profile-preview'}},
            {'operation': 'capabilities.help', 'arguments': {'operation': 'project.structure.relink-preview'}}]


def _snapshot(root, reference):
    from .transport_cache import get
    snapshot = get(root, reference, kind='import-organization')
    source = load_structure(root)
    if snapshot['projectId'] != source['projectId'] or snapshot['sourceHash'] != snapshot_hash(source):
        raise StructureError('IMPORT_ORGANIZATION_STALE', 'source changed; prepare the next batch from current records')
    validate_guards(root, snapshot['guards'], stage='import-organization')
    return source, snapshot


def read_organization(root: Path, *, prepareRef, cursor=None, limit=None):
    from .text_pages import text_page
    source, snapshot = _snapshot(root, prepareRef)
    policy = query_policy()
    count = policy['defaultLimit'] if limit is None else limit
    if type(count) is not int or not 1 <= count <= policy['maxLimit']:
        raise StructureError('ARGUMENTS_INVALID', 'invalid reading limit')
    return text_page({'ok': True, 'code': 'IMPORT_ORGANIZATION_TEXT', 'prepareRef': prepareRef}, snapshot['materials'],
                     budget=policy['maxBytes']['detail'], limit=count, cursor=cursor,
                     binding=sha256_json({'limit': count}), source=snapshot['sourceHash'])


def preview_organization(root: Path, *, prepareRef, requestId, content):
    from .import_organization_model import assemble
    from .source_requests import request_identity, resume_request
    from .structure_commands import _base_plan, _write_plan_locked
    root = root.expanduser().resolve()
    with project_lock(root):
        current = load_structure(root)
        operation_id, _ = request_identity(current, requestId)
        fingerprint = sha256_json({'prepareRef': prepareRef, 'content': content})
        resumed = resume_request(root, current, operation_id, fingerprint)
        if resumed is not None:
            if resumed.get('alreadyCommitted'):
                from .operation_receipts import require_committed_operation
                require_committed_operation(root, operation_id)
            return resumed
        source, snapshot = _snapshot(root, prepareRef)
        if isinstance(content, dict) and sum(len(content.get(key, [])) for key in ('chapters', 'entities', 'facts', 'relations')
                                            if isinstance(content.get(key, []), list)) > canon_proposal_batch_max_items():
            raise StructureError('IMPORT_ORGANIZATION_TOO_LARGE', 'split observations into smaller batches')
        if len(json_bytes(content)) > query_policy()['maxBytes']['schema'] * canon_proposal_batch_max_items():
            raise StructureError('IMPORT_ORGANIZATION_TOO_LARGE', 'split extraction into smaller batches')
        materials = {row['chapterId']: row for row in snapshot['materials']}
        from .import_history import history_entry, history_status
        _, workflow_raw = _observed(root, WORKFLOW)
        workflow = json.loads(workflow_raw or b'{}')
        if any(row['status'] == 'stable' and history_entry(workflow, row['chapterId'])
               and history_status(root, source, workflow, row)['complete']
               for row in source['chapters'] if row['chapterId'] in materials):
            raise StructureError('IMPORT_ALREADY_ACCEPTED', 'current history is already accepted; revise facts through reviewed source proposals')
        proposed, identities, citations, reviews = assemble(source, content, materials)
        binding = {'content': deepcopy(content), 'chapterIds': snapshot['chapterIds'], 'guards': snapshot['guards'],
                   'citations': citations, 'reviews': reviews}
        plan = _base_plan(source, operation='import_organization', actor='app', proposed=proposed,
                         parameters={'importOrganization': binding, 'identityMap': identities},
                         impact={'acceptedImportChapterIds': snapshot['chapterIds'] if content.get('acceptHistory') else [],
                                 'historicalStageAClaimed': False, 'retrospectiveReview': True})
        plan.update(operationId=operation_id, requestId=requestId, requestFingerprint=fingerprint)
        return _write_plan_locked(root, plan)


def organization_outputs(root, current, plan, extras):
    """Reconstruct exactly the declared delta at both preview and commit."""
    if plan['operation'] != 'import_organization':
        return None
    from .import_organization_model import assemble
    from .source_evidence import EVIDENCE_PATH, read_evidence
    from .source_impact import ReviewInputs
    binding = plan['parameters']['importOrganization']
    materials, original_guards = imported_materials(root, current, binding['chapterIds'])
    validate_guards(root, binding['guards'], stage='import-organization-submit')
    workflow_guard, raw = _observed(root, WORKFLOW)
    if binding['guards'] != [*original_guards, workflow_guard]:
        raise StructureError('IMPORT_ORGANIZATION_STALE', 'original read guards differ')
    expected, identities, citations, reviews = assemble(current, binding['content'], materials, plan['parameters']['identityMap'])
    ignored = {'revision', 'updatedAt', 'lastOperation'}
    if ({k: v for k, v in expected.items() if k not in ignored} !=
            {k: v for k, v in plan['proposedStructure'].items() if k not in ignored}
            or citations != binding['citations'] or reviews != binding['reviews']):
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'preview differs from the exact reviewed source changes')
    proposed = plan['proposedStructure']
    evidence = deepcopy(read_evidence(root))
    inputs = ReviewInputs(proposed)
    for cid in materials:
        batch_id = 'PE-' + sha256_json({'operationId': plan['operationId'], 'chapterId': cid}).upper()
        rows = []
        for n, row in enumerate(q for q in citations if q['chapterId'] == cid):
            rows.append({k: deepcopy(v) for k, v in row.items() if k != 'chapterId'} | {
                'evidenceId': batch_id + '-' + str(n),
                'targetHashes': {rid: inputs.fingerprint(rid) for rid in row['targetIds']}})
        chapter = next(row for row in proposed['chapters'] if row['chapterId'] == cid)
        evidence['batches'][batch_id] = {'batchId': batch_id, 'chapterId': cid, 'path': chapter['filePath'],
            'fileHash': materials[cid]['proseHash'], 'sourceHash': snapshot_hash(proposed),
            'requestHash': sha256_json(rows), 'actor': 'import_organization', 'citations': rows}
    result = {**extras, EVIDENCE_PATH: json_bytes(evidence)}
    accepted = reviews if binding['content'].get('acceptHistory') else {}
    if accepted:
        workflow = json.loads(extras.get(WORKFLOW) or raw or b'{}')
        for cid, review in accepted.items():
            workflow.setdefault('importReviews', {})[cid] = {'operationId': plan['operationId'], 'reviewHash': sha256_json(review)}
            workflow.setdefault('chapterStates', {})[cid] = {'status': 'stable', 'importOperationId': plan['operationId']}
        # Only clear a selected task which is completed by this explicit acceptance.
        if (workflow.get('nextAction') or {}).get('chapterId') in accepted:
            workflow['nextAction'] = None
        result[WORKFLOW] = json_bytes(workflow)
    return result, binding['guards'], {'accepted': accepted, 'reviews': reviews, 'citations': citations,
                                      'semanticReviewBy': 'model', 'historicalStageAClaimed': False}
