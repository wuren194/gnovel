"""Bounded workload projections of existing domain judgments, never a new gate."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from pathlib import Path

from .artifacts import sha256_json, sha256_text
from .consistency import require_no_pending
from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
from .project import query_policy
from .source_impact import ReviewInputs, fragment_review_status, read_review
from .source_review import bounded_page
from .source_review_triage import NEEDS_ATTENTION
from .structure_store import StructureError, load_structure

KINDS = ('chapter-issues', 'setting-impact', 'character-reviews', 'decision-plans')


def _reviews(source, root, selection):
    from .source_selection import select_content
    inputs, state = ReviewInputs(source), read_review(root)
    selected = select_content(source, selection)
    visible = {row['id'] for row in selected['records'] if row['kind'] == 'fragment'}
    statuses = {rid: fragment_review_status(inputs, state, rid) for rid in sorted(visible)}
    return inputs, state, selected, statuses


def _review_rows(inputs, statuses, *, chapter_id=None, character_id=None):
    wanted = NEEDS_ATTENTION | {'unreviewed', 'not_checked'}
    rows = []
    for rid, status in statuses.items():
        if status not in wanted:
            continue
        owner = inputs.index.fragments[rid]['ownerId']
        if character_id is not None and owner != character_id:
            continue
        rows.append({'kind': 'source-review', 'recordId': rid, 'ownerId': owner, 'status': status,
                     **({'chapterId': chapter_id} if chapter_id else {}),
                     'detailsOperation': 'source.reviews'})
    return rows


def _chapter_rows(root, source, chapters, selection):
    from .chapter_status import finalization_blockers
    from .source_plan import chapter_plan
    from .source_rules import RULE_ACTIONS, evaluate_rules
    from .continuity import pending_continuity
    from .finalization import _load_workflow
    from .mechanical_evidence import _registered, candidate_prose, evidence_current
    workflow = _load_workflow(root)
    inputs = statuses = None
    perspective = False
    inputs, _, selected, statuses = _reviews(source, root, selection)
    perspective = selected['selection']['knowledgeMode'] == 'perspective'
    rows, groups = [], []
    for chapter in chapters:
        cid = chapter['chapterId']
        start = len(rows)
        if inputs is not None:
            plan = chapter_plan(source, cid, _inputs=inputs)
            ids = {row['id'] for row in plan['records']}
            rows.extend(_review_rows(inputs, {rid: status for rid, status in statuses.items() if rid in ids}, chapter_id=cid))
        if not perspective:
            # 只取与状态板同一判断的阻塞；不为每章重建状态板的工件行和下一步。
            rows.extend({'kind': 'workflow-blocker', 'chapterId': cid, 'code': row['code'],
                         'status': 'blocked', 'detailsOperation': 'project.workflow.chapter-status'}
                        for row in finalization_blockers(root, source, chapter))
            for kind in ('diagnostics', 'stage_b_review'):
                entry = workflow.get('artifacts', {}).get(cid, {}).get(kind)
                if not isinstance(entry, dict):
                    continue
                try:
                    report = _registered(root, source, workflow, cid, kind)
                    if kind == 'diagnostics':
                        fresh = evidence_current(root, cid, structure=source)['fresh']
                    else:
                        fresh = report is not None and report['payload'].get('finalProseSha256') == sha256_text(
                            candidate_prose(root, cid, structure=source)['text'])
                except (ValueError, OSError, KeyError, TypeError):
                    report, fresh = None, False
                if report is None or not fresh:
                    rows.append({'kind': 'stale-report', 'chapterId': cid, 'artifactType': kind,
                                 'status': 'stale', 'detailsOperation': 'workflow.repair-context'})
                    continue
                from .repair_history import finding_ids
                identities = finding_ids(report)
                for index, finding in enumerate(report['payload'].get('findings', [])):
                    rows.append({'kind': 'finding', 'chapterId': cid, 'artifactType': kind,
                                 'issueId': identities[index],
                                 'evidenceId': f'{kind}:{report["artifactSha256"]}:{index}',
                                 'ruleId': finding.get('ruleId'), 'severity': finding.get('severity'),
                                 'status': 'advisory' if finding.get('advisory') else 'reported',
                                 'detailsOperation': 'workflow.repair-context'})
            if inputs is not None:
                for action in RULE_ACTIONS:
                    checks = evaluate_rules(source, cid, action, root=root, _inputs=inputs, _plan=plan)['items']
                    rows.extend({'kind': 'source-rule', 'chapterId': cid, 'action': action,
                                 'ruleId': row['ruleId'], 'status': row['status'], 'blocking': row['blocking'],
                                 'detailsOperation': 'source.rules'} for row in checks if row['status'] != 'passed')
            rows.extend({'kind': 'continuity', 'chapterId': cid, 'status': row.get('status', 'pending'),
                         'detailsOperation': 'project.workflow.continuity-preview'}
                        for row in pending_continuity(source, workflow) if row['chapterId'] == cid)
        local = rows[start:]
        groups.append({'chapterId': cid, 'stage': chapter.get('status'), 'itemCount': len(local),
                       'byKind': dict(sorted(Counter(row['kind'] for row in local).items())),
                       'byStatus': dict(sorted(Counter(row['status'] for row in local).items()))})
    return rows, groups, {'authorDomainsOmitted': perspective}


def _character_rows(root, source, character_id, selection):
    inputs, _, selected, statuses = _reviews(source, root, selection)
    characters = {rid for rid, kind in inputs.index.owner_types.items() if kind == 'character'}
    if character_id is not None and character_id not in characters:
        raise StructureError('AGGREGATE_FILTER_INVALID', 'unknown characterId')
    owners = {row['id'] for row in selected['owners']} & characters
    if character_id is not None:
        owners &= {character_id}
    rows = [row for row in _review_rows(inputs, statuses, character_id=character_id) if row['ownerId'] in owners]
    groups = []
    for owner in sorted(owners):
        current = [row for row in rows if row['ownerId'] == owner]
        groups.append({'characterId': owner, 'itemCount': len(current),
                       'byStatus': dict(sorted(Counter(row['status'] for row in current).items()))})
    return rows, groups, {'knowledgeMode': selected['selection']['knowledgeMode'],
                          'scope': 'selected fragments owned by each character; not inferred participation'}


def _setting_rows(root, operation_id, dependencies):
    from .source_maintenance_receipts import load_operation
    from .write_sets import safe_project_path
    directory = safe_project_path(root, '.gnovel/operations')
    paths = sorted(directory.glob('OP-*.json'))
    dependencies['queries'].append({'directory': '.gnovel/operations', 'pattern': 'OP-*.json',
                                   'members': [path.relative_to(root).as_posix() for path in paths]})
    ids = [operation_id] if operation_id is not None else [path.stem for path in paths]
    undo_directory = safe_project_path(root, '.gnovel/undo-receipts')
    undo_pattern = operation_id + '-*.json' if operation_id is not None else 'OP-*.json'
    dependencies['queries'].append({'directory': '.gnovel/undo-receipts', 'pattern': undo_pattern,
        'members': [path.relative_to(root).as_posix() for path in sorted(undo_directory.glob(undo_pattern))]})
    rows, groups = [], []
    for oid in ids:
        state = load_operation(root, oid)
        report = state['receipt']
        if report is None:
            raise StructureError('PLAN_NOT_FOUND', 'aggregate impact requires an existing committed operation')
        impact = report.get('impact', {})
        counts = {key: len(value) for key, value in impact.items() if isinstance(value, list)}
        if not counts and operation_id is None:
            continue
        for category, values in impact.items():
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                metadata = ({key: value[key] for key in ('recordId', 'causeId', 'chapterId', 'ruleId', 'relationId', 'evidenceId', 'status') if key in value}
                            if isinstance(value, dict) else {'recordId': value})
                rows.append({'operationId': oid, 'kind': category, 'itemIndex': index, **metadata,
                             'sourceOperationUndone': state['undone'], 'historicalSnapshot': True,
                             'detailsOperation': 'source.change-report'})
        groups.append({'operationId': oid, 'counts': counts, 'itemCount': sum(counts.values()),
                       'sourceOperationUndone': state['undone'], 'historicalSnapshot': True})
    return rows, groups, {'scope': 'committed operation impact snapshots; not recomputed current approval',
                          'absenceMeaning': 'no registered impact does not exclude implicit usage'}


def _decision_rows(source, decision_id):
    from .author_decisions import chapter_decision_scope, decision_validity
    from .source_plan import chapter_plan
    inputs = ReviewInputs(source)
    decisions = {row['decisionId']: row for row in source.get('authorDecisions', [])}
    if decision_id is not None and decision_id not in decisions:
        raise StructureError('AGGREGATE_FILTER_INVALID', 'unknown decisionId')
    reverse = defaultdict(set)
    for rid, decision in decisions.items():
        for parent in decision['dependsOn']:
            reverse[parent].add(rid)
    plans = {row['chapterId']: chapter_plan(source, row['chapterId'], _inputs=inputs)
             for row in source['chapters'] if row.get('status') != 'archived'}
    # Applicability is shared with the effective plan; status/credit remains an
    # explicit projection so historical or draft scope does not become approval.
    applicable = {cid: chapter_decision_scope(source, cid, plan) for cid, plan in plans.items()}
    rows, groups = [], []
    for rid, decision in sorted(decisions.items()):
        if decision_id is not None and rid != decision_id:
            continue
        found, pending = set(), deque([rid])
        while pending:
            current = pending.popleft()
            if current in found:
                continue
            found.add(current)
            pending.extend(reverse[current] - found)
        scopes = {scope for did in found for scope in decisions[did]['scopeIds']}
        global_scope = any(not decisions[did]['scopeIds'] for did in found)
        local = []
        for cid, plan in plans.items():
            matching = scopes & applicable[cid]
            for scope in sorted(matching) or ([None] if global_scope else []):
                local.append({'kind': 'chapter-plan', 'decisionId': rid, 'chapterId': cid,
                              'matchedScopeId': scope, 'globalScope': global_scope,
                              'sourcePlanHash': plan['sourcePlanHash'],
                              'detailsOperation': 'source.chapter-plan'})
        for candidate in source.get('planningCandidates', []):
            if candidate['candidateId'] in scopes:
                local.append({'kind': 'planning-candidate', 'decisionId': rid, 'candidateId': candidate['candidateId'],
                              'status': candidate['status'], 'detailsOperation': 'source.planning-candidates'})
        rows.extend(local)
        validity = decision_validity(source, decisions, rid)
        groups.append({'decisionId': rid, 'status': decision['status'], **validity,
                       'stale': validity['validity'] != 'current' if decision['status'] == 'active' else None,
                       'dependentDecisionCount': len(found) - 1,
                       'chapterCount': len({row['chapterId'] for row in local if 'chapterId' in row}),
                       'candidateCount': sum(row['kind'] == 'planning-candidate' for row in local),
                       'itemCount': len(local)})
    return rows, groups, {'epistemicRole': 'authorial_planning_decision',
                          'scope': 'registered decision scope and dependent decisions only; no implicit influence inferred'}


def query_aggregates(root: Path, *, kind, view='summary', chapterId=None, characterId=None,
                     operationId=None, decisionId=None, selection=None, limit=None, maxBytes=None, cursor=None):
    root = root.expanduser().resolve()
    require_no_pending(root)
    if kind not in KINDS or view not in {'summary', 'items'}:
        raise StructureError('AGGREGATE_FILTER_INVALID', 'unknown aggregate kind or view')
    allowed = {'chapter-issues': 'chapterId', 'character-reviews': 'characterId',
               'setting-impact': 'operationId', 'decision-plans': 'decisionId'}
    filters = {'chapterId': chapterId, 'characterId': characterId, 'operationId': operationId, 'decisionId': decisionId}
    if any(value is not None and (key != allowed[kind] or not isinstance(value, str) or not value.strip())
           for key, value in filters.items()):
        raise StructureError('AGGREGATE_FILTER_INVALID', 'filter does not belong to this aggregate kind')
    dependencies = capture_dependencies(root, chapter_id=chapterId)
    with observe_reads(root) as observed:
        source = load_structure(root)
        if kind in {'setting-impact', 'decision-plans'} and selection is not None:
            raise StructureError('AGGREGATE_AUTHOR_SCOPE_REQUIRED', 'historical impacts and author decisions are author-only metadata, not POV material')
        if kind == 'chapter-issues':
            chapters = [row for row in source['chapters'] if row.get('status') != 'archived'
                        and (chapterId is None or row['chapterId'] == chapterId)]
            if chapterId is not None and not chapters:
                raise StructureError('CHAPTER_NOT_FOUND', 'unknown active chapterId')
            rows, groups, coverage = _chapter_rows(root, source, chapters, selection)
        elif kind == 'character-reviews':
            rows, groups, coverage = _character_rows(root, source, characterId, selection)
        elif kind == 'setting-impact':
            rows, groups, coverage = _setting_rows(root, operationId, dependencies)
        else:
            rows, groups, coverage = _decision_rows(source, decisionId)
    bind_observed(dependencies, observed)
    validate_dependencies(root, dependencies, code='AGGREGATE_INPUT_CHANGED')
    from .transport_cache import put
    selection_ref = put(root, 'aggregate-selection', {'dependencies': dependencies, 'selection': selection,
        'recordIds': sorted({row['recordId'] for row in rows if row['kind'] == 'source-review'}), 'kind': kind})
    result = bounded_page(groups if view == 'summary' else rows,
                          snapshot={'projectId': source['projectId'], 'inputsHash': sha256_json(dependencies)},
                          selection={'kind': kind, 'view': view, **filters, 'selection': selection},
                          code='DOMAIN_AGGREGATE', limit=limit, maxBytes=maxBytes, cursor=cursor,
                          kind=kind, view=view, selectionRef=selection_ref,
                          summary={'groupCount': len(groups), 'itemCount': len(rows)},
                          coverage={**coverage, 'semanticReviewComplete': False, 'approvalGranted': False,
                                    'domainStatusesReused': True, 'countsAreNotDistinctSemanticErrors': True})
    require_no_pending(root)
    return result
