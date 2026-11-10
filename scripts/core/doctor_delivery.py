"""Bounded doctor reports and explicit comparisons, without weakening checks."""
from collections import Counter, defaultdict

from .artifacts import sha256_json
from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
from .project import query_policy
from .query import _cursor_decode
from .static_cache import toolbox_signature
from .structure_store import StructureError, canonical_json
from .text_pages import text_page
from .transport_cache import get, put


def _groups(report):
    groups = defaultdict(list)
    for issue in report['issues']:
        identity = {key: issue.get(key) for key in ('domain', 'code', 'objectType', 'objectId', 'chapterId')}
        groups['DI-' + sha256_json(identity)].append(issue)
    return groups


def _comparison(current, previous):
    if previous is None:
        return {'requested': False, 'comparable': False}, []
    if (not current['scope'].get('projectId') or previous.get('scope') != current['scope']
            or previous.get('runtime') != current['runtime']):
        return {'requested': True, 'comparable': False, 'reason': 'scope_or_runtime_changed'}, []
    before, after = _groups(previous['report']), _groups(current['report'])
    changes, counts = [], Counter()
    for identity in sorted(before.keys() | after.keys()):
        old, new = before.get(identity, []), after.get(identity, [])
        category = ('added' if not old else 'no_longer_reported' if not new else
                    'unchanged' if sorted(map(canonical_json, old)) == sorted(map(canonical_json, new)) else 'changed')
        counts[category] += 1
        if category != 'unchanged':
            changes.append({'issueId': identity, 'change': category, 'before': old, 'current': new})
    return {'requested': True, 'comparable': True, 'groupCounts': dict(counts),
            'removalIsSemanticResolution': False}, changes


def _page(root, saved, reference, *, against_ref, limit, budget, cursor):
    previous, unavailable = None, None
    if against_ref:
        try:
            previous = get(root, against_ref, kind='doctor-report')
        except StructureError as exc:
            unavailable = exc.code
    comparison, changes = _comparison(saved, previous)
    if unavailable:
        comparison = {'requested': True, 'comparable': False, 'reason': unavailable}
    report = saved['report']
    envelope = {key: report[key] for key in ('ok', 'code', 'healthy', 'strict', 'structureRevision',
                                            'scopedChapterId', 'issueCounts', 'dataIntegrity', 'authoringReadiness', 'assessment', 'message')}
    domains = {key: {'status': value['status'], 'issueCount': value['issueCount']}
               for key, value in report['domains'].items()}
    envelope.update(view='model', doctorRef=reference, comparison=comparison, domains=domains,
                    detailsAction={'operation': 'task.snapshot', 'arguments': {'reference': reference}},
                    coverage={'checks': 'complete doctor assessment', 'semanticConsistencyProven': False})
    if 'importFollowup' in report:
        envelope['importFollowupAction'] = {'operation': 'import.organization-status', 'arguments': {'projectRoot': str(root)}}
    rows = ([{'id': row['issueId'], 'text': canonical_json(row)} for row in changes]
            if comparison['comparable'] else
            [{'id': identity, 'text': canonical_json({'issueId': identity, 'current': issues})}
             for identity, issues in sorted(_groups(report).items())])
    # Actions retain their own material identities, so even a long suggestion is pageable.
    rows += [{'id': 'action-' + sha256_json(row), 'text': canonical_json(row), 'materialKind': 'suggested-action'}
             for row in report.get('suggestedActions', [])]
    binding = sha256_json({'scope': saved['arguments'], 'againstRef': against_ref, 'limit': limit, 'maxBytes': budget})
    return text_page(envelope, rows, key='issues', limit=limit, budget=budget, cursor=cursor,
                     source=reference, binding=binding)


def doctor_report(root, *, strict=False, chapterId=None, view='full', againstRef=None,
                  limit=None, maxBytes=None, cursor=None):
    from .project_doctor import assess_project
    root = root.expanduser().resolve()
    if view not in {'full', 'model'}:
        raise StructureError('ARGUMENTS_INVALID', 'unknown doctor view')
    if view == 'full':
        if any(value is not None for value in (againstRef, limit, maxBytes, cursor)):
            raise StructureError('ARGUMENTS_INVALID', 'doctor pagination and comparison require view=model')
        return assess_project(root, strict=bool(strict), chapter_id=chapterId)
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    count = policy['defaultLimit'] if limit is None else limit
    if (type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']
            or type(count) is not int or not 1 <= count <= policy['maxLimit']):
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid doctor page budget')
    args = {'strict': bool(strict), 'chapterId': chapterId}
    if cursor:
        reference = _cursor_decode(cursor)['source']
        saved = get(root, reference, kind='doctor-report')
        if saved['arguments'] != args:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'doctor scope changed')
        validate_dependencies(root, saved['dependencies'], code='CURSOR_STALE')
    else:
        dependencies = capture_dependencies(root, chapter_id=chapterId)
        with observe_reads(root) as observed:
            report = assess_project(root, strict=bool(strict), chapter_id=chapterId)
            from .structure_store import load_structure
            try:
                project_id = load_structure(root)['projectId']
            except (StructureError, OSError, ValueError):
                project_id = None
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='DOCTOR_INPUTS_CHANGED')
        saved = {'arguments': args, 'scope': {**args, 'scopedChapterId': report['scopedChapterId'],
                 'projectId': project_id},
                 'report': report, 'dependencies': dependencies, 'runtime': sha256_json(toolbox_signature())}
        reference = put(root, 'doctor-report', saved)
    result = _page(root, saved, reference, against_ref=againstRef, limit=count, budget=budget, cursor=cursor)
    validate_dependencies(root, saved['dependencies'], code='DOCTOR_INPUTS_CHANGED')
    return result
