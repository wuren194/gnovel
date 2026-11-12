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
QUARANTINE_REASON = {
    'content_boundary': '内容边界待人工复核',
    'underage_explicit_content': '涉及未成年角色的露骨内容，隔离待人工复核',
    'other': '隔离内容待人工复核',
}


def _privacy_safe_content(content):
    if not isinstance(content, dict) or not isinstance(content.get('chapters'), list):
        return content
    safe = deepcopy(content)
    for index, chapter in enumerate(safe.get('chapters', [])):
        if not isinstance(chapter, dict) or chapter.get('quarantineReason') is None:
            continue
        category = chapter.get('quarantineCategory')
        if category not in QUARANTINE_REASON:
            continue
        chapter.update(synopsis='隔离章节不提炼回溯梗概。', quotes=[],
                       review={key: '隔离状态下不记录正文语义。'
                               for key in ('facts', 'relationships', 'continuity', 'structure')},
                       unresolved=[], existingRecordIds=[],
                       quarantineReason=QUARANTINE_REASON[category], adopt=False)
    return safe


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
        if chapter is None or origin is None or chapter['status'] not in {'planning_required', 'stable', 'stale', 'quarantined'}:
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
        if row['chapterId'] in imported_ids and row.get('status') not in {'archived', 'quarantined'} and not (history_entry(workflow, row['chapterId']) and
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


def inspect_organization_entities(root: Path, *, operationId, planHash, offset=0, limit=None):
    from .jsonio import read_json
    from .structure_commands import _operation_paths, _plan_hash
    from .command_help import cli_action
    root = root.expanduser().resolve()
    count = query_policy()['defaultLimit'] if limit is None else limit
    if type(offset) is not int or offset < 0 or type(count) is not int or not 1 <= count <= query_policy()['maxLimit']:
        raise StructureError('ARGUMENTS_INVALID', 'invalid entity-reference page')
    path, _, _ = _operation_paths(root, operationId)
    if not path.is_file():
        raise StructureError('PLAN_NOT_FOUND', 'organization plan does not exist', {'operationId': operationId})
    plan = read_json(path)
    if plan.get('operation') != 'import_organization':
        raise StructureError('IMPORT_ORGANIZATION_PLAN_INVALID', 'operation is not an organization preview')
    if plan.get('planHash') != planHash or _plan_hash({key: value for key, value in plan.items() if key != 'planHash'}) != planHash:
        raise StructureError('PLAN_HASH_MISMATCH', 'organization preview changed; use the original preview')
    parameters = plan.get('parameters', {})
    references = parameters.get('entityReferences', {})
    identities = parameters.get('identityMap', {})
    if not isinstance(references, dict) or not isinstance(identities, dict):
        raise StructureError('IMPORT_ORGANIZATION_PLAN_INVALID', 'entity reference map is invalid')
    entity_handles = set(parameters.get('entityRefs', []))
    rows = [{'ref': ref, 'entityId': references[ref], 'created': ref in identities}
            for ref in sorted(entity_handles) if ref in references]
    if offset > len(rows):
        raise StructureError('ARGUMENTS_INVALID', 'offset exceeds the entity-reference count')
    items = rows[offset:offset + count]
    next_offset = offset + len(items)
    result = {'ok': True, 'code': 'IMPORT_ORGANIZATION_ENTITIES', 'operationId': operationId,
              'planHash': planHash, 'total': len(rows), 'offset': offset, 'items': items,
              'nextOffset': next_offset if next_offset < len(rows) else None}
    if next_offset < len(rows):
        result['readAction'] = cli_action('import.organization-plan-inspect', {
            'projectRoot': str(root), 'operationId': operationId, 'planHash': planHash,
            'offset': next_offset, 'limit': count})
    return result


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
        limit = canon_proposal_batch_max_items()
        max_bytes = query_policy()['maxBytes']['schema'] * limit
        current = sum(len(content.get(key, [])) for key in ('chapters', 'entities', 'facts', 'relations')
                      if isinstance(content, dict) and isinstance(content.get(key, []), list))
        current_bytes = len(json_bytes(content))
        if current > limit or current_bytes > max_bytes:
            chapter_count = len(content.get('chapters', [])) if isinstance(content, dict) and isinstance(content.get('chapters'), list) else 0
            item_ratio = limit / max(current, 1)
            byte_ratio = max_bytes / max(current_bytes, 1)
            max_chapters = max(1, min(chapter_count or 1, int((chapter_count or 1) * min(item_ratio, byte_ratio))))
            chapter_ids = snapshot['chapterIds']
            details = {'limit': limit, 'current': current, 'maxBytes': max_bytes, 'currentBytes': current_bytes,
                       'suggestion': {'strategy': 'split_by_chapter', 'maxChapters': max_chapters,
                                      'chapterBatches': [chapter_ids[index:index + max_chapters]
                                                         for index in range(0, len(chapter_ids), max_chapters)]}}
            reason = 'split observations into smaller batches' if current > limit else 'split extraction into smaller chapter batches'
            raise StructureError('IMPORT_ORGANIZATION_TOO_LARGE', reason, details)
        materials = {row['chapterId']: row for row in snapshot['materials']}
        from .import_history import history_entry, history_status
        _, workflow_raw = _observed(root, WORKFLOW)
        workflow = json.loads(workflow_raw or b'{}')
        if any(row['status'] == 'stable' and history_entry(workflow, row['chapterId'])
               and history_status(root, source, workflow, row)['complete']
               for row in source['chapters'] if row['chapterId'] in materials):
            raise StructureError('IMPORT_ALREADY_ACCEPTED', 'current history is already accepted; revise facts through reviewed source proposals')
        safe_content = _privacy_safe_content(content)
        proposed, identities, references, citations, reviews = assemble(source, safe_content, materials)
        accepted_reviews = {cid: review for cid, review in reviews.items()
                            if safe_content.get('acceptHistory') and review.get('adopt')
                            and review.get('quarantineReason') is None}
        quarantined_ids = [cid for cid, review in reviews.items() if review.get('quarantineReason') is not None]
        binding = {'content': safe_content, 'chapterIds': snapshot['chapterIds'], 'guards': snapshot['guards'],
                   'citations': citations, 'reviews': reviews, 'references': references, 'accepted': accepted_reviews,
                   'quarantinedChapterIds': quarantined_ids}
        plan = _base_plan(source, operation='import_organization', actor='app', proposed=proposed,
                         parameters={'importOrganization': binding, 'identityMap': identities,
                                     'entityReferences': references,
                                     'entityRefs': [row['ref'] for row in safe_content.get('entities', [])]},
                         impact={'acceptedImportChapterIds': sorted(accepted_reviews),
                                 'quarantinedChapterIds': quarantined_ids,
                                 'historicalStageAClaimed': False, 'retrospectiveReview': True})
        plan.update(operationId=operation_id, requestId=requestId, requestFingerprint=fingerprint)
        report = _write_plan_locked(root, plan)
        from .query import _size
        policy = query_policy()
        entity_refs = plan['parameters']['entityRefs']
        refs = [(ref, references[ref]) for ref in entity_refs]
        budget = policy['maxBytes']['list']
        pages = []
        for index, (ref, entity_id) in enumerate(refs):
            candidate = {'ref': ref, 'entityId': entity_id, 'created': ref in identities}
            pages.append(candidate)
            projected = {**report, 'entityReferences': pages}
            if _size(projected) > budget:
                pages.pop()
                break
        report['entityReferences'] = pages
        report['entityReferenceCount'] = len(refs)
        if len(pages) < len(refs):
            report['entityReferencesTruncated'] = True
            report['entityReferencesAction'] = {'operation': 'import.organization-plan-inspect',
                'arguments': {'projectRoot': str(root), 'operationId': operation_id,
                              'planHash': report['planHash'], 'offset': len(pages),
                              'limit': policy['defaultLimit']}}
        else:
            report['entityReferencesAction'] = {'operation': 'import.organization-plan-inspect',
                'arguments': {'projectRoot': str(root), 'operationId': operation_id,
                              'planHash': report['planHash'], 'offset': 0,
                              'limit': policy['defaultLimit']}}
        return report


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
    safe_content = _privacy_safe_content(binding['content'])
    if safe_content != binding['content']:
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'quarantined review payload contains semantic content')
    expected, identities, references, citations, reviews = assemble(
        current, safe_content, materials, plan['parameters']['identityMap'])
    accepted = {cid: review for cid, review in reviews.items()
                if binding['content'].get('acceptHistory') and review.get('adopt')
                and review.get('quarantineReason') is None}
    quarantined = sorted(cid for cid, review in reviews.items()
                         if review.get('quarantineReason') is not None)
    if (plan['parameters'].get('entityReferences') != references
            or binding.get('references') != references
            or binding.get('accepted') != accepted or binding.get('quarantinedChapterIds') != quarantined
            or plan['impact'].get('acceptedImportChapterIds') != sorted(accepted)
            or plan['impact'].get('quarantinedChapterIds') != quarantined):
        raise StructureError('IMPORT_ORGANIZATION_INVALID', 'chapter adoption and quarantine scope differs from reviewed input')
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
        if not rows:
            continue
        chapter = next(row for row in proposed['chapters'] if row['chapterId'] == cid)
        evidence['batches'][batch_id] = {'batchId': batch_id, 'chapterId': cid, 'path': chapter['filePath'],
            'fileHash': materials[cid]['proseHash'], 'sourceHash': snapshot_hash(proposed),
            'requestHash': sha256_json(rows), 'actor': 'import_organization', 'citations': rows}
    result = {**extras, EVIDENCE_PATH: json_bytes(evidence)}
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
