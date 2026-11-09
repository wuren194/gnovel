"""Prepare and assemble evidence without inventing judgments or approvals."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .artifacts import sha256_json
from .write_sets import digest
from .consistency import require_no_pending
from .evidence_quotes import quote_span
from .structure_store import StructureError


def review_policy_hash():
    from .static_cache import toolbox_signature
    return sha256_json(toolbox_signature())


def _prepare_snapshot(root: Path, *, kind: str, recordIds: list[str] | None = None,
                     chapterId: str | None = None, paths: list[str] | None = None, selection: dict | None = None,
                     reviewContext: bool = False) -> dict:
    root = root.expanduser().resolve()
    require_no_pending(root)
    if kind == 'review':
        from .source_impact import ReviewInputs
        from .source_model import snapshot_hash
        from .source_review import source_snapshot
        source = source_snapshot(root)
        inputs = ReviewInputs(source)
        if (not isinstance(recordIds, list) or not recordIds
                or any(not isinstance(rid, str) or rid not in inputs.index.fragments for rid in recordIds)
                or len(set(recordIds)) != len(recordIds)):
            raise StructureError('EVIDENCE_SELECTION_INVALID', 'select distinct active source fragments')
        visible = None
        if selection is not None:
            from .source_review_materials import review_visibility
            selection, visible = review_visibility(source, recordIds, selection)
        items = []
        for rid in recordIds:
            fragment = inputs.index.fragments[rid]
            if fragment.get('status') == 'archived':
                raise StructureError('EVIDENCE_SELECTION_INVALID', 'archived content cannot receive a current review')
            items.append({'recordId': rid, 'inputHash': inputs.fingerprint(rid),
                          'contentHash': digest(fragment['text'].encode()), 'text': fragment['text'],
                          'inputIds': sorted(inputs.incoming[rid] if visible is None else inputs.incoming[rid] & visible.keys()),
                          'locator': {'fragmentId': rid, 'sourceHash': snapshot_hash(source), 'offset': 0}})
        snapshot = {'kind': kind, 'projectId': source['projectId'], 'sourceHash': snapshot_hash(source),
                    'recordIds': recordIds, 'items': items}
        if selection is not None:
            snapshot['selection'] = selection
        if reviewContext or selection is not None:
            snapshot.update(reviewContext=True, contextPolicyHash=review_policy_hash())
    elif kind == 'prose':
        from .source_evidence import _manuscript
        from .source_model import SourceIndex, snapshot_hash
        from .source_review import source_snapshot
        source = source_snapshot(root)
        path, file_hash, text = _manuscript(root, SourceIndex.build(source), chapterId)
        snapshot = {'kind': kind, 'projectId': source['projectId'], 'sourceHash': snapshot_hash(source),
                    'chapterId': chapterId, 'path': path, 'fileHash': file_hash, 'text': text}
    elif kind == 'continuity':
        from .continuity import continuity_preview
        from .generated import is_generated
        from .paths import safe_project_path
        from .structure_store import load_structure
        preview = continuity_preview(root, chapterId)
        if preview.get('blockers') or 'batch' not in preview:
            raise StructureError('EVIDENCE_CONTINUITY_BLOCKED', 'continuity batch is unavailable or blocked', preview)
        if not isinstance(paths or [], list) or any(not isinstance(path, str) for path in paths or []):
            raise StructureError('EVIDENCE_SELECTION_INVALID', 'paths must be a string list')
        documents = []
        for relative in sorted(set(paths or [])):
            path = safe_project_path(root, relative)
            name = path.relative_to(root).as_posix()
            summary = name == '.govel/aux/story-summary.json'
            if not summary and (path.suffix != '.md' or name.split('/')[0] not in {'设定', '规划'} or is_generated(path.read_text(encoding='utf-8'))):
                raise StructureError('CONTINUITY_EVIDENCE_INVALID', 'only authored continuity documents may be selected')
            raw = path.read_bytes()
            documents.append({'path': name, 'sha256': digest(raw),
                              'text': raw.decode('utf-8')})
        snapshot = {'kind': kind, 'projectId': load_structure(root)['projectId'], 'chapterId': chapterId,
                    'batchHash': preview['batchHash'], 'documents': documents,
                    'itemIds': [row['itemId'] for row in preview['batch']['items']]}
    else:
        raise StructureError('EVIDENCE_KIND_INVALID', 'supported kinds are review, prose and continuity')
    # Text is delivered for reading once. The caller returns only the small
    # fingerprint/locator snapshot to pack; it need not transcribe the body.
    materials = []
    if kind == 'review':
        for item in snapshot['items']:
            materials.append({'recordId': item['recordId'], 'contentHash': item['contentHash'],
                              'text': item.pop('text'), 'inputIds': item['inputIds']})
    elif kind == 'prose':
        materials.append({'path': snapshot['path'], 'fileHash': snapshot['fileHash'], 'text': snapshot.pop('text')})
    else:
        for item in snapshot['documents']:
            materials.append({'path': item['path'], 'sha256': item['sha256'], 'text': item.pop('text')})
    snapshot['snapshotHash'] = sha256_json(snapshot)
    return snapshot, materials


def prepare_evidence(root: Path, *, kind: str, recordIds: list[str] | None = None,
                     chapterId: str | None = None, paths: list[str] | None = None,
                     maxBytes: int | None = None, limit: int | None = None, cursor: str | None = None,
                     view: str = 'full', snapshotRef: str | None = None, selection: dict | None = None,
                     includeContext: bool = False, aggregateRef: str | None = None,
                     resumeRequestIds: list[str] | None = None, priorReceipt: str | None = None,
                     holdMaterials: bool = False) -> dict:
    from .project import query_policy
    from .query import _size
    from .text_pages import text_page
    from .reading_handoff import encode_receipt
    root = root.expanduser().resolve()
    if type(holdMaterials) is not bool:
        raise StructureError('EVIDENCE_SELECTION_INVALID', 'holdMaterials must be a boolean')
    track_reading = holdMaterials or priorReceipt is not None
    if track_reading and kind != 'review':
        raise StructureError('EVIDENCE_SELECTION_INVALID', 'material reading receipts currently require kind=review')
    from .transport_cache import get
    from .query import _cursor_decode
    continuation = None
    resumed_dependencies = None
    resumed_page = None
    if resumeRequestIds is not None:
        if not cursor or kind != 'review':
            raise StructureError('EVIDENCE_RESUME_INVALID', 'resumeRequestIds requires the original review reading cursor')
        resumed_page = get(root, _cursor_decode(cursor)['source'], kind='evidence-page')
        from .evidence_continuation import resume_reading
        resumed_dependencies, progress_states, continuation = resume_reading(root, resumed_page, resumeRequestIds)
    if type(includeContext) is not bool or kind != 'review' and (selection is not None or includeContext or aggregateRef is not None):
        raise StructureError('EVIDENCE_SELECTION_INVALID', 'source selection and review context require kind=review')
    if aggregateRef is not None:
        if resumed_page is not None:
            if resumed_page.get('aggregateRef') != aggregateRef:
                raise StructureError('EVIDENCE_RESUME_INVALID', 'retain the original aggregate reference')
            from .evidence_continuation import revalidate_with_progress
            aggregate = get(root, aggregateRef, kind='aggregate-selection')
            revalidate_with_progress(root, aggregate['dependencies'], progress_states)
            original_selection = resumed_page['arguments'].get('selection')
            if selection is not None and selection != original_selection:
                raise StructureError('EVIDENCE_RESUME_INVALID', 'retain the original source selection')
            selection = original_selection
        else:
            from .source_review_materials import select_aggregate
            selection = select_aggregate(root, aggregateRef, recordIds, selection)
        includeContext = True
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    count = policy['defaultLimit'] if limit is None else limit
    if (type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']
            or type(count) is not int or not 1 <= count <= policy['maxLimit']):
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid evidence reading budget')
    from .transport_cache import put, get
    if view not in {'full', 'model'}:
        raise StructureError('ARGUMENTS_INVALID', 'view must be full or model')
    args = dict(kind=kind, recordIds=recordIds, chapterId=chapterId, paths=paths)
    if selection is not None:
        args['selection'] = selection
    if includeContext:
        args['reviewContext'] = True
    binding = sha256_json({'selection': args, 'maxBytes': budget, 'limit': count, 'root': str(root), 'view': view,
                          'includeContext': includeContext, 'aggregateRef': aggregateRef,
                          'priorReceipt': priorReceipt, 'holdMaterials': track_reading})
    from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
    from .query import _cursor_decode
    page_reference = _cursor_decode(cursor).get('source') if cursor else None
    if isinstance(page_reference, str) and page_reference.startswith('gn:evidence-page:'):
        saved = get(root, page_reference, kind='evidence-page')
        if saved['arguments'] != args:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'evidence selection changed')
        dependencies = resumed_dependencies if resumed_dependencies is not None else saved['dependencies']
        validate_dependencies(root, dependencies, code='CURSOR_STALE')
        snapshot, materials = saved['snapshot'], saved['materials']
        reference = saved['snapshotRef']
        if snapshotRef is not None and snapshotRef != reference:
            raise StructureError('EVIDENCE_SNAPSHOT_INVALID', 'cursor and snapshotRef select different preparations')
    elif cursor:
        raise StructureError('CURSOR_STALE', 'old evidence cursor requires a new preparation')
    else:
        dependencies = capture_dependencies(root, evidence=args)
        baseline = None
        with observe_reads(root) as observed:
            snapshot, materials = _prepare_snapshot(root, **args)
            if includeContext:
                from .source_review_materials import review_materials
                materials = review_materials(root, recordIds, selection, materials, dependencies)
                from .evidence_continuation import review_baseline
                baseline = review_baseline(root)
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='EVIDENCE_SNAPSHOT_STALE')
        if snapshotRef is not None and get(root, snapshotRef, kind='evidence-snapshot') != snapshot:
            raise StructureError('EVIDENCE_SNAPSHOT_STALE', 'evidence inputs changed after selection')
        reference = put(root, 'evidence-snapshot', snapshot)
        page_reference = put(root, 'evidence-page', {'arguments': args, 'snapshot': snapshot,
            'materials': materials, 'dependencies': dependencies, 'snapshotRef': reference,
            'reviewBaseline': baseline, 'aggregateRef': aggregateRef})
    envelope = {'ok': True, 'code': 'EVIDENCE_PREPARED',
                **({'snapshot': snapshot} if view == 'full' else {'snapshotRef': reference}),
                'semanticReviewComplete': False, 'approvalGranted': False}
    if continuation is not None:
        envelope['continuation'] = continuation
    if includeContext:
        envelope['packAction'] = {'operation': 'evidence.pack', 'arguments': {'snapshotRef': reference},
                                  'missingInputs': ['requestId', 'decisions'], 'submitted': False}
    if view == 'full' and _size(envelope) >= budget:
        envelope['snapshot'] = {'encoded': encode_receipt(snapshot)}
    ranges, transform = None, None
    if kind == 'review' and track_reading:
        from .evidence_reading import material_delivery
        ranges, transform, reused = material_delivery(root, snapshot, materials, dependencies, priorReceipt,
                                                      source=page_reference, binding=binding, cursor=cursor)
        if priorReceipt is not None:
            envelope['reusedMaterials'] = reused
    result = text_page(envelope, materials, budget=budget, limit=count, cursor=cursor,
                       binding=binding, source=page_reference, ranges=ranges, transform=transform)
    validate_dependencies(root, dependencies, code='EVIDENCE_SNAPSHOT_STALE')
    require_no_pending(root)
    return result


def pack_evidence(root: Path, *, decisions: list | dict, snapshot: dict | None = None,
                  snapshotRef: str | None = None, requestId: str | None = None, view: str = 'full',
                  maxBytes: int | None = None) -> dict:
    root = root.expanduser().resolve()
    from .evidence_submission import submission_budget
    budget = submission_budget(maxBytes)
    if snapshotRef is not None:
        from .transport_cache import get
        if snapshot is not None:
            raise StructureError('EVIDENCE_SNAPSHOT_INVALID', 'select snapshot or snapshotRef, not both')
        snapshot = get(root, snapshotRef, kind='evidence-snapshot')
    if view not in {'full', 'model'}:
        raise StructureError('ARGUMENTS_INVALID', 'view must be full or model')
    if isinstance(snapshot, dict) and set(snapshot) == {'encoded'}:
        from .reading_handoff import decode_receipt
        snapshot = decode_receipt(snapshot['encoded'])
    if not isinstance(snapshot, dict) or snapshot.get('snapshotHash') != sha256_json({k: v for k, v in snapshot.items() if k != 'snapshotHash'}):
        raise StructureError('EVIDENCE_SNAPSHOT_INVALID', 'use the complete prepare snapshot unchanged')
    kind = snapshot.get('kind')
    if kind != 'continuity' and (not isinstance(requestId, str) or not requestId.strip()):
        raise StructureError('EVIDENCE_REQUEST_INVALID', 'an explicit submission requestId is required')
    args = {'kind': kind}
    if kind == 'review':
        args['recordIds'] = snapshot.get('recordIds')
        if 'selection' in snapshot:
            args['selection'] = snapshot['selection']
        if snapshot.get('reviewContext'):
            args['reviewContext'] = True
    elif kind == 'continuity':
        args.update(chapterId=snapshot.get('chapterId'), paths=[row['path'] for row in snapshot.get('documents', [])])
    else:
        args['chapterId'] = snapshot.get('chapterId')
    current = _prepare_snapshot(root, **args)[0]
    if current != snapshot:
        raise StructureError('EVIDENCE_SNAPSHOT_STALE', 'evidence inputs changed; read again before deciding')
    if kind == 'review':
        from .source_review import source_snapshot, DECISIONS
        from .source_model import SourceIndex
        fragments = SourceIndex.build(source_snapshot(root)).fragments
        if not isinstance(decisions, list) or not decisions:
            raise StructureError('EVIDENCE_DECISIONS_INCOMPLETE', 'provide explicit decisions for the processed records')
        by_id = {row['recordId']: row for row in snapshot['items']}
        packed = []
        seen = set()
        for row in decisions:
            if (not isinstance(row, dict) or set(row) != {'recordId', 'decision', 'reason', 'quotes'}
                    or not isinstance(row['recordId'], str) or row['recordId'] not in by_id or row['recordId'] in seen
                    or not isinstance(row['decision'], str) or row['decision'] not in DECISIONS or not isinstance(row['reason'], str) or not row['reason'].strip()
                    or not isinstance(row['quotes'], list) or (row['decision'] != 'not_checked' and not row['quotes'])):
                raise StructureError('EVIDENCE_DECISION_INVALID', 'explicit decision, reason and selected quotes required')
            seen.add(row['recordId'])
            packed.append({k: by_id[row['recordId']][k] for k in ('recordId', 'inputHash', 'contentHash')} |
                          {'decision': row['decision'], 'reason': row['reason'],
                           'evidence': [quote_span(fragments[row['recordId']]['text'], quote) for quote in row['quotes']]})
        result = {'operation': 'source.review-submit', 'arguments': {'reviews': packed},
                  'pendingRecordIds': [rid for rid in snapshot['recordIds'] if rid not in seen],
                  'selectionComplete': seen == set(snapshot['recordIds'])}
    elif kind == 'prose':
        from .source_evidence import _manuscript, KINDS
        from .source_review import source_snapshot
        from .source_model import SourceIndex
        _, _, text = _manuscript(root, SourceIndex.build(source_snapshot(root)), snapshot['chapterId'])
        if not isinstance(decisions, list) or not decisions:
            raise StructureError('EVIDENCE_DECISION_INVALID', 'explicit citations required')
        packed = []
        for row in decisions:
            if (not isinstance(row, dict) or set(row) != {'quote', 'targetIds', 'kind', 'reason'}
                    or not isinstance(row['kind'], str) or row['kind'] not in KINDS or not isinstance(row['reason'], str) or not row['reason'].strip()):
                raise StructureError('EVIDENCE_DECISION_INVALID', 'typed dependency and reason required')
            source = source_snapshot(root)
            records = SourceIndex.build(source).records
            if not isinstance(row['targetIds'], list) or not row['targetIds'] or any(not isinstance(rid, str) or rid not in records for rid in row['targetIds']):
                raise StructureError('SOURCE_EVIDENCE_TARGET_INVALID', 'select registered targets')
            packed.append(quote_span(text, row['quote']) | {k: deepcopy(row[k]) for k in ('targetIds', 'kind', 'reason')})
        result = {'operation': 'source.evidence-register', 'arguments': {
            'chapterId': snapshot['chapterId'], 'fileHash': snapshot['fileHash'],
            'sourceHash': snapshot['sourceHash'], 'citations': packed}}
    else:
        from .continuity import continuity_preview, _validate_confirmation
        if not isinstance(decisions, dict):
            raise StructureError('EVIDENCE_DECISION_INVALID', 'explicit continuity confirmation required')
        confirmation = deepcopy(decisions)
        documents = {row['path']: row['sha256'] for row in snapshot['documents']}
        if (not isinstance(confirmation.get('decisions'), list)
                or any(not isinstance(row, dict) or not isinstance(row.get('evidence', []), list)
                       or any(not isinstance(item, dict) for item in row.get('evidence', []))
                       for row in confirmation['decisions'])
                or not isinstance(confirmation.get('summary'), dict)):
            raise StructureError('EVIDENCE_DECISION_INVALID', 'explicit continuity decisions and summary required')
        for row in confirmation['decisions']:
            for evidence in row.get('evidence', []):
                path = evidence.get('path')
                if path not in documents or set(evidence) - {'path', 'sha256'}:
                    raise StructureError('EVIDENCE_SELECTION_INVALID', 'document was not selected in the read snapshot')
                if evidence.get('sha256', documents[path]) != documents[path]:
                    raise StructureError('EVIDENCE_SNAPSHOT_STALE', 'explicit document hash differs')
                evidence['sha256'] = documents[path]
        if confirmation.get('summary', {}).get('resolution') == 'refreshed':
            path = '.govel/aux/story-summary.json'
            if path not in documents:
                raise StructureError('EVIDENCE_SELECTION_INVALID', 'select the refreshed summary snapshot')
            confirmation['summary']['sha256'] = documents[path]
        _validate_confirmation(root, continuity_preview(root, snapshot['chapterId']), confirmation)
        result = {'operation': 'project.workflow.continuity-complete', 'arguments': {
            'chapterId': snapshot['chapterId'], 'batchHash': snapshot['batchHash'], 'confirmation': confirmation}}
    if kind != 'continuity':
        result['arguments']['requestId'] = requestId
    if _prepare_snapshot(root, **args)[0] != snapshot:
        raise StructureError('EVIDENCE_SNAPSHOT_STALE', 'evidence inputs changed during assembly')
    from .project import query_policy
    from .query import _size
    report = {'ok': True, 'code': 'EVIDENCE_PACKED', **result, 'submitted': False, 'approved': False}
    if view == 'model' or _size(report) > budget:
        from .evidence_submission import bind_submission
        return bind_submission(root, snapshot, result, max_bytes=maxBytes)
    return report
