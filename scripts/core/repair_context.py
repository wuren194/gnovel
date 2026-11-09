"""Version-bound repair handoff; historical findings never inherit review credit."""
from __future__ import annotations

import json
from pathlib import Path

from .paths import stored_project_path
from .artifacts import sha256_json, sha256_text
from .consistency import require_no_pending
from .jsonio import read_json
from .mechanical_evidence import candidate_prose, evidence_current
from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
from .project import query_policy
from .repair_history import candidate_history, finding_ids, read_versions
from .repair_locations import historical_location, location, quote_text
from .structure_store import StructureError, load_structure
from .transport_cache import get, put
from .workflow import normalize_workflow, default_workflow


def _prepare_repair(root, chapter_id):
    structure = load_structure(root)
    prose = candidate_prose(root, chapter_id, structure=structure)
    body, body_hash = prose['text'], sha256_text(prose['text'])
    workflow_path = root / '.govel/workflow.json'
    workflow = normalize_workflow(read_json(workflow_path)) if workflow_path.is_file() else default_workflow()
    bodies, transitions, unavailable = candidate_history(root, structure, workflow, chapter_id, prose)
    reports, stale, rows, decisions = {}, [], {}, {}
    paragraphs, differences = [], None
    for kind in ('diagnostics',):
        versions, missing = read_versions(root, structure, workflow, chapter_id, kind)
        unavailable.extend(missing)
        for version in versions:
            report, current = version['report'], False
            payload, report_hash = report['payload'], report['artifactSha256']
            before_hash = payload.get('draftSha256' if kind == 'diagnostics' else 'finalProseSha256')
            if version['latest']:
                reports[kind] = report_hash
                current = not version['invalidated'] and before_hash == body_hash
                if kind == 'diagnostics':
                    current = current and evidence_current(root, chapter_id, structure=structure)['fresh']
                if not current:
                    stale.append({'artifactType': kind, 'reason': 'report is invalidated' if version['invalidated']
                                  else 'report does not bind the current inputs'})
            for index, (identity, finding) in enumerate(zip(finding_ids(report), payload.get('findings', []))):
                unresolved = finding.get('repairState') == 'needs_location'
                if unresolved:
                    located = {'state': 'needs_location', 'occurrences': 0, 'spans': []}
                    comparison = {'beforeBodyHash': before_hash, 'before': [{'text': quote_text(finding)}],
                                  'afterBodyHash': body_hash, 'after': [], 'locationVerified': False}
                else:
                    located, comparison = historical_location(body, finding, identity, before_hash, body_hash, bodies, transitions)
                rows[identity] = {'id': identity, 'source': kind, 'finding': finding,
                    'location': location(body, finding) if current and not unresolved else located,
                    'reviewState': 'reported' if current and finding.get('repairState') not in {
                        'needs_recheck', 'needs_location', 'needs_information'} else 'needs_recheck',
                    'evidenceId': f'{kind}:{report_hash}:{index}',
                    'provenance': {'artifactType': kind, 'artifactSha256': report_hash, 'bodyHash': before_hash},
                    **({'comparison': comparison} if not current else {})}
            if kind != 'diagnostics' or not current:
                continue
            calibration = payload.get('calibration', {})
            paragraphs = calibration.get('paragraphs', [])
            for category in ('flat_runs', 'template_hits'):
                for index, detail in enumerate(calibration.get('sepia', {}).get(category, [])):
                    identity = f'sepia:{category}:{index}'
                    finding = {**detail, 'advisory': True, 'ruleId': category}
                    rows[identity] = {'id': identity, 'source': 'calibration', 'finding': finding,
                                      'location': location(body, finding)}
            previous_path = version['reference'].get('previousArtifactPath')
            if previous_path:
                from .paths import safe_project_path
                from .review_artifacts import validate_artifact_binding
                from .diagnostics_diff import diff_findings, model_diff
                prior = read_json(safe_project_path(root, previous_path, allow_absolute=True))
                validate_artifact_binding(prior, project_id=structure['projectId'], chapter_id=chapter_id)
                differences = model_diff(diff_findings(prior['payload'].get('findings', []), payload['findings']))
            for rule, details in payload.get('lineDetails', {}).get('blocks', {}).items():
                for index, detail in enumerate(details):
                    finding = {'ruleId': rule, **detail}
                    located = location(body, finding)
                    identity = f'line:{report_hash}:{rule}:{index}'
                    duplicate = next((row for row in rows.values() if row['source'] == 'diagnostics'
                                      and row['finding'].get('ruleId') == rule
                                      and row['location']['spans'] == located['spans'] and located['spans']), None)
                    if duplicate is not None:
                        duplicate.setdefault('additionalSources', []).append({'id': identity, 'finding': finding})
                    else:
                        rows[identity] = {'id': identity, 'source': 'lineDetails', 'finding': finding, 'location': located}
    from .repair_history import stage_b_obligations
    from .mechanical_evidence import _registered
    candidate = _registered(root, structure, workflow, chapter_id, 'candidate_prose')
    if candidate is not None:
        obligations = stage_b_obligations(root, structure, workflow, chapter_id, candidate)
        unavailable.extend(obligations['unavailableReports'])
        for identity in obligations['resolved']:
            rows.pop(identity, None)
        latest = workflow.get('artifacts', {}).get(chapter_id, {}).get('stage_b_review', {})
        if latest:
            reports['stage_b_review'] = latest.get('artifactSha256')
            if latest.get('invalidatedBy'):
                stale.append({'artifactType': 'stage_b_review', 'reason': 'report is invalidated'})
        for identity, obligation in obligations['pending'].items():
            finding, before_hash = obligation['finding'], obligation['bodyHash']
            located, comparison = historical_location(body, finding, identity, before_hash, body_hash, bodies, transitions)
            if finding.get('repairState') == 'needs_location':
                located = {'state': 'needs_location', 'occurrences': 0, 'spans': []}
            current = (not latest.get('invalidatedBy') and before_hash == body_hash
                       and obligation['artifactSha256'] == latest.get('artifactSha256'))
            if current and finding.get('repairState') != 'needs_location':
                # Current repeated quotations retain all occurrences for an
                # explicit anchor choice; only historical rebasing loses that
                # location credit when its original occurrence is ambiguous.
                located = location(body, finding)
            rows[identity] = {'id': identity, 'source': 'stage_b_review', 'finding': finding,
                'location': located, 'comparison': comparison,
                'reviewState': 'reported' if current and finding.get('repairState') not in {
                    'needs_location', 'needs_recheck', 'needs_information'} else 'needs_recheck',
                'evidenceId': f'stage_b_review:{obligation["artifactSha256"]}:{identity}',
                'provenance': {'artifactType': 'stage_b_review', 'artifactSha256': obligation['artifactSha256'],
                               'bodyHash': before_hash},
                **({'previousDecision': obligation['previousDecision']} if 'previousDecision' in obligation else {})}
    # Recovery points back to the retained domain files; missing content is not
    # reconstructed, discarded or converted into an empty review form.
    from .paths import safe_project_path
    for missing in unavailable:
        kind = 'candidate_prose' if missing['artifactType'] == 'patch_receipt' else missing['artifactType']
        entry = workflow.get('artifacts', {}).get(chapter_id, {}).get(kind, {})
        reference = next((row for row in [*entry.get('repairHistory', []), entry]
                          if row.get('artifactSha256') == missing.get('artifactSha256')), {})
        if missing['artifactType'] == 'patch_receipt':
            pointer = reference.get('patchReceipt', {})
            path, expected = pointer.get('path'), pointer.get('sha256')
        else:
            path, expected = reference.get('artifactPath'), reference.get('artifactFileSha256')
        if path:
            try:
                relative = stored_project_path(root, path).relative_to(root).as_posix()
                missing['restore'] = {'path': relative, 'expectedFileSha256': expected}
            except ValueError:
                pass  # An unsafe historical path is evidence, never a write target.
    return {'chapterId': chapter_id, 'candidateSha256': prose['artifactSha256'], 'draftSha256': body_hash,
            'projectId': structure['projectId'], 'body': body, 'rows': list(rows.values()),
            'paragraphs': paragraphs, 'reports': reports, 'staleReports': stale,
            'unavailableReports': unavailable, 'diff': differences}


def repair_context(root: Path, *, chapterId: str, maxBytes: int | None = None,
                   limit: int | None = None, cursor: str | None = None) -> dict:
    from .edit_anchors import create_anchor
    from .query import _cursor_decode
    from .repair_windows import repair_materials
    from .text_pages import text_page
    root = root.expanduser().resolve()
    require_no_pending(root)
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    count = policy['defaultLimit'] if limit is None else limit
    if type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail'] or type(count) is not int or not 1 <= count <= policy['maxLimit']:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid repair evidence budget')
    if cursor:
        reference = _cursor_decode(cursor)['source']
        saved = get(root, reference, kind='repair')
        if saved['chapterId'] != chapterId:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'repair cursor belongs to another chapter')
        validate_dependencies(root, saved['dependencies'], code='CURSOR_STALE')
    else:
        dependencies = capture_dependencies(root, chapter_id=chapterId)
        with observe_reads(root) as observed:
            saved = _prepare_repair(root, chapterId)
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='REPAIR_INPUT_CHANGED')
        saved['dependencies'] = dependencies
        reference = put(root, 'repair', saved)
    body = saved['body']
    anchor_binding = {'projectId': saved['projectId'], 'chapterId': chapterId,
                      'artifactSha256': saved['candidateSha256'], 'bodyHash': saved['draftSha256']}

    def anchor(start, end, issue_ids, scope):
        return create_anchor(root, binding=anchor_binding, text=body, start=start, end=end,
                             issue_ids=issue_ids, scope=scope)

    materials = repair_materials(body, saved['rows'], saved['paragraphs'],
                                 anchor=anchor if saved['candidateSha256'] is not None else None)
    for row in saved['unavailableReports']:
        materials.append({'id': 'unavailable:' + str(row.get('artifactSha256')), 'source': 'unavailableReport',
                          'location': {'state': 'unavailable', 'spans': []}, 'text': json.dumps(row, ensure_ascii=False)})
    differences = saved['diff']
    if differences is not None:
        differences = {'summary': differences.get('summary'), 'reference': put(root, 'diagnostics-diff', differences)}
    from .artifact_templates import review_skeleton_payload
    from .source_plan import chapter_plan
    review_handoff = {'reviewInputAvailable': not saved['unavailableReports']}
    if saved['unavailableReports']:
        review_handoff.update(reviewInputBlocker='REPAIR_HISTORY_UNAVAILABLE',
            recovery={'instruction': '从已有副本核对并恢复 unavailableReport.restore 指向的原字节，再重新读取；此入口只读，不补造历史。',
                      'recheckAction': {'operation': 'workflow.repair-context', 'arguments': {'chapterId': chapterId}}})
    else:
        review_payload, _ = review_skeleton_payload(root, chapterId=chapterId,
                                                   artifactType='stage_b_review', repairRef=reference)
        review_payload.update(sourcePlanHash=chapter_plan(load_structure(root), chapterId)['sourcePlanHash'],
                              finalProseSha256=saved['draftSha256'])
        review_handoff['reviewInputRef'] = put(root, 'repair-review-input', review_payload)
    result = text_page({'ok': True, 'code': 'REPAIR_CONTEXT', 'chapterId': chapterId,
        'draftSha256': saved['draftSha256'], 'candidateSha256': saved['candidateSha256'], 'repairRef': reference,
        **review_handoff,
        'reports': saved['reports'], 'staleReports': saved['staleReports'], 'diff': differences,
        'coverage': {'totalFindings': len(saved['rows']), 'wholeChapterReviewed': False,
                     'unavailableReportCount': len(saved['unavailableReports']),
                     'historicalPendingCount': sum(row.get('reviewState') == 'needs_recheck' for row in saved['rows']),
                     'semanticFixVerified': False, 'absenceIsConsistencyProof': False}}, materials,
        limit=count, budget=budget, cursor=cursor, binding=sha256_json({'chapterId': chapterId, 'limit': count, 'maxBytes': budget}),
        source=reference, key='items')
    validate_dependencies(root, saved['dependencies'], code='REPAIR_INPUT_CHANGED')
    require_no_pending(root)
    return result
