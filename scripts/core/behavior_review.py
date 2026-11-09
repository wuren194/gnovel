"""Explicit applicability and bounded living-arrangement review; no semantic classifier."""
from copy import deepcopy

from .review_artifacts import ReviewArtifactError, require_completed_text

COVERAGE_STATUSES = {'unassessed', 'insufficient_material', 'applicable', 'not_applicable'}
COMPLETED_COVERAGE_STATUSES = COVERAGE_STATUSES - {'unassessed', 'insufficient_material'}
DOMAINS = ('relationship', 'living')
TRIGGERS = {'first_sustained', 'time_or_resource_cost', 'alternative_excluded', 'exception_becomes_obligation'}
ARRANGEMENT_FIELDS = {'id', 'characterIds', 'triggers', 'arrangement', 'basisIds', 'constraints', 'choiceReason', 'scope'}
REVIEW_FIELDS = {'id', 'kind', 'origin', 'disposition', 'conclusion', 'evidence', 'basisIds'}
DISPOSITIONS = {'within_plan', 'local_repair', 'replan_required', 'needs_material'}
BINDING_REQUIREMENTS = {
    'replan_required': ('sourcePlanHash', 'BEHAVIOR_REPLAN_REQUIRED',
                        'a structural choice requires a new approved plan before completion'),
    'local_repair': ('finalProseSha256', 'BEHAVIOR_REPAIR_REQUIRED',
                     'repair the identified wording before closing its disposition'),
}


def blank_coverage():
    return {domain: 'unassessed' for domain in DOMAINS}


def coverage_schema():
    """The same domain/status vocabulary used by the runtime validator."""
    return {'type': 'object', 'properties': {
                domain: {'type': 'string', 'enum': sorted(COVERAGE_STATUSES)} for domain in DOMAINS},
            'required': list(DOMAINS), 'additionalProperties': False}


def coverage_error_details():
    return {'field': 'behaviorCoverage', 'required': list(DOMAINS),
            'allowed': sorted(COVERAGE_STATUSES),
            'completionAllowed': sorted(COMPLETED_COVERAGE_STATUSES),
            'schemaAction': {'operation': 'source.schema', 'arguments': {}},
            'instruction': '先用workflow.review-form取得当前绑定和空白表单，再填写两个领域的真实判断；空数组不表示不适用。'}


def validate_coverage(value):
    return (isinstance(value, dict) and set(value) == set(DOMAINS)
            and all(isinstance(status, str) and status in COVERAGE_STATUSES for status in value.values()))


def validate_arrangements(rows):
    errors, seen = [], set()
    if not isinstance(rows, list):
        return ['LIVING_ARRANGEMENTS_INVALID']
    from .relationship_causality import _text, _references
    for row in rows:
        if (not isinstance(row, dict) or set(row) != ARRANGEMENT_FIELDS
                or any(not _text(row.get(key)) for key in ('id', 'arrangement', 'constraints', 'choiceReason', 'scope'))
                or not _references(row.get('characterIds'), nonempty=True)
                or not _references(row.get('basisIds'), nonempty=False)
                or not _references(row.get('triggers'), nonempty=True)
                or set(row.get('triggers', [])) - TRIGGERS):
            errors.append('LIVING_ARRANGEMENT_INVALID')
            continue
        if row['id'] in seen:
            errors.append('LIVING_ARRANGEMENT_ID_DUPLICATE')
        seen.add(row['id'])
    return errors


def contract_for(source, chapter_id):
    from .structure_store import StructureError
    rows = [f['chapterContract'] for f in source.get('fragments', [])
            if f.get('ownerId') == chapter_id and f.get('contentRole') == 'plan'
            and f.get('status') != 'archived' and 'chapterContract' in f]
    if len(rows) > 1:
        raise StructureError('CHAPTER_CONTRACT_AMBIGUOUS', 'behavior review needs a unique chapter contract')
    return rows[0] if rows else {}


def validate_source_arrangements(fragment, index):
    errors = []
    for row in fragment.get('chapterContract', {}).get('livingArrangements', []):
        path = 'fragments.' + fragment['fragmentId'] + '.chapterContract.livingArrangements.' + row['id']
        for rid in row['characterIds']:
            if index.owner_types.get(rid) != 'character':
                errors.append({'code': 'LIVING_CHARACTER_INVALID', 'path': path, 'recordId': rid})
        for rid in [*row['characterIds'], *row['basisIds']]:
            if rid not in index.records or index.records[rid].get('status') == 'archived':
                errors.append({'code': 'LIVING_BASIS_MISSING', 'path': path, 'recordId': rid})
            if rid not in fragment.get('dependsOn', []):
                errors.append({'code': 'LIVING_DEPENDENCY_MISSING', 'path': path, 'recordId': rid})
    return errors


def schema():
    return {'behaviorCoverage': {'domains': list(DOMAINS), 'statuses': sorted(COVERAGE_STATUSES),
                'authority': 'explicit applicability, never inferred from an empty declaration array'},
            'livingArrangements': {'requiredFields': sorted(ARRANGEMENT_FIELDS), 'triggers': sorted(TRIGGERS),
                'dependencies': 'characterIds and basisIds require exact fragment.dependsOn edges',
                'basisIds': 'may be empty when unknown; constraints must preserve uncertainty, not invent absence'}}


def blank_reviews(source, chapter_id):
    return [{'id': row['id'], 'kind': 'living', 'origin': 'plan', 'disposition': 'needs_material',
             'conclusion': '', 'evidence': [], 'basisIds': deepcopy(row['basisIds'])}
            for row in contract_for(source, chapter_id).get('livingArrangements', [])]


def validate_shape(payload):
    if 'behaviorCoverage' in payload and not validate_coverage(payload['behaviorCoverage']):
        raise ReviewArtifactError('BEHAVIOR_COVERAGE_INVALID', 'coverage needs an explicit status for relationship and living',
                                  coverage_error_details())
    rows = payload.get('behaviorReviews', [])
    if not isinstance(rows, list):
        raise ReviewArtifactError('BEHAVIOR_REVIEW_INVALID', 'behaviorReviews must be an array')
    from .relationship_causality import _text, _references
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != REVIEW_FIELDS or not _text(row.get('id'))
                or row['id'] in seen or row.get('kind') not in DOMAINS or row.get('origin') not in {'plan', 'prose'}
                or row.get('disposition') not in DISPOSITIONS or not isinstance(row.get('conclusion'), str)
                or not isinstance(row.get('evidence'), list) or not _references(row.get('basisIds'), nonempty=False)):
            raise ReviewArtifactError('BEHAVIOR_REVIEW_INVALID', 'invalid behavior review identity, origin or disposition')
        seen.add(row['id'])


def coverage_item_issues(coverage, count):
    """One coverage/item-count contract for early guidance and final enforcement."""
    if not validate_coverage(coverage):
        return [{'code': 'BEHAVIOR_COVERAGE_REQUIRED',
                 'message': 'each chapter requires actual relationship and living applicability assessment',
                 **coverage_error_details(), 'notChecked': ['coverage_item_counts']}]
    return [{'code': 'BEHAVIOR_COVERAGE_CONFLICT',
             'message': 'applicable coverage requires items; not_applicable forbids them',
             'domain': domain, 'declaredCoverage': coverage[domain], 'itemCount': count[domain],
             'instruction': '核对实际内容与适用性声明；applicable需有对应条目，not_applicable不能保留适用条目，不为消除报错虚构判断。'}
            for domain in DOMAINS if coverage[domain] in COMPLETED_COVERAGE_STATUSES
            and (coverage[domain] == 'applicable') != bool(count[domain])]


def plan_coverage_issues(source, chapter_id):
    contract = contract_for(source, chapter_id)
    coverage = contract.get('behaviorCoverage')
    if not validate_coverage(coverage) or any(v not in COMPLETED_COVERAGE_STATUSES for v in coverage.values()):
        return [{'code': 'BEHAVIOR_PLAN_COVERAGE_REQUIRED',
                 'message': 'plan applicability assessment is incomplete', 'planCoverage': deepcopy(coverage),
                 **coverage_error_details(), 'notChecked': ['coverage_item_counts']}]
    return coverage_item_issues(coverage, {'relationship': len(contract.get('relationshipCausality', [])),
                                          'living': len(contract.get('livingArrangements', []))})


def validate_completion(source, chapter_id, payload, kind, text):
    from .evidence_quotes import quote_span
    from .source_plan import chapter_plan
    validate_shape(payload)
    coverage = payload.get('behaviorCoverage')
    if not validate_coverage(coverage) or any(v not in COMPLETED_COVERAGE_STATUSES for v in coverage.values()):
        raise ReviewArtifactError('BEHAVIOR_COVERAGE_REQUIRED', 'each chapter requires actual relationship and living applicability assessment',
                                  coverage_error_details())
    contract = contract_for(source, chapter_id)
    if kind == 'stage_a_review':
        plan_coverage = contract.get('behaviorCoverage')
        if not validate_coverage(plan_coverage) or plan_coverage != coverage:
            raise ReviewArtifactError('BEHAVIOR_PLAN_COVERAGE_REQUIRED', 'stage A must assess the current explicit plan coverage',
                {**coverage_error_details(), 'planCoverage': deepcopy(plan_coverage),
                 'instruction': '先核对当前计划中的适用性声明；若计划需改动，受审提交后重新取表并审查，不能只换绑定或状态。'})
    if payload.get('sourcePlanHash') != chapter_plan(source, chapter_id)['sourcePlanHash']:
        raise ReviewArtifactError('CAUSALITY_REVIEW_PLAN_STALE', 'behavior review requires the current plan and dependency inputs')
    declarations = contract.get('livingArrangements', [])
    rows = payload.get('behaviorReviews', [])
    expected = {r['id'] for r in declarations}
    planned = {r['id'] for r in rows if r['origin'] == 'plan' and r['kind'] == 'living'}
    if expected != planned:
        raise ReviewArtifactError('BEHAVIOR_REVIEW_REQUIRED', 'each declared living arrangement requires a review',
                                  {'missing': sorted(expected - planned), 'unexpected': sorted(planned - expected)})
    count = {'relationship': len(contract.get('relationshipCausality', [])), 'living': len(declarations)}
    for row in rows:
        if row['origin'] == 'prose':
            if kind != 'stage_b_review':
                raise ReviewArtifactError('BEHAVIOR_ORIGIN_INVALID', 'only B can report a prose-discovered arrangement')
            count[row['kind']] += 1
        elif row['kind'] != 'living':
            raise ReviewArtifactError('BEHAVIOR_ORIGIN_INVALID', 'declared relationship choices use relationshipCausalityReviews')
        if row['disposition'] != 'within_plan':
            raise ReviewArtifactError('BEHAVIOR_DISPOSITION_PENDING', 'resolve local repair, missing material or return to A before completion',
                                      {'id': row['id'], 'disposition': row['disposition']})
        require_completed_text(row['conclusion'], 'behaviorReviews.' + row['id'])
        if not row['evidence']:
            raise ReviewArtifactError('BEHAVIOR_EVIDENCE_REQUIRED', 'triggered arrangements need exact current quotations')
        for quote in row['evidence']:
            quote_span(text, quote)
        # Every reused fact must already belong to the plan input closure, so a
        # changed baseline invalidates the same approval binding as other inputs.
        bound = {r['id'] for r in chapter_plan(source, chapter_id)['records']}
        if set(row['basisIds']) - bound:
            raise ReviewArtifactError('BEHAVIOR_BASIS_UNBOUND', 'register precise source dependencies before relying on this baseline')
    issues = coverage_item_issues(coverage, count)
    if issues:
        first = issues[0]
        raise ReviewArtifactError(first['code'], first['message'],
            {**{key: value for key, value in first.items() if key not in {'code', 'message'}}, 'issues': issues})
    if kind == 'stage_a_review':
        if 4 not in payload.get('activeSeats', []) or count['living'] and 9 not in payload.get('activeSeats', []):
            raise ReviewArtifactError('BEHAVIOR_SEAT_REQUIRED', 'seat 4 screens applicability; triggered living arrangements also require seat 9')
        seats = payload.get('seatReviews', {})
        for seat in (['4', '9'] if count['living'] else ['4']):
            row = seats.get(seat, {})
            require_completed_text(row.get('conclusion'), 'seatReviews.' + seat)
            if not row.get('evidence'):
                raise ReviewArtifactError('BEHAVIOR_EVIDENCE_REQUIRED', 'coverage screening needs current plan evidence in its existing seat')
            for quote in row['evidence']:
                quote_span(text, quote)


def pending_binding_fields(payload):
    return {BINDING_REQUIREMENTS[row['disposition']][0] for row in payload.get('behaviorReviews', [])
            if row['disposition'] in BINDING_REQUIREMENTS}


def _validate_pending_bindings(source, chapter_id, payload, text):
    """An unfinished obligation still needs its actual baseline before registration."""
    from .artifacts import sha256_text
    from .source_plan import chapter_plan
    for field in sorted(pending_binding_fields(payload)):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReviewArtifactError('BEHAVIOR_PENDING_BINDING_REQUIRED',
                                      'pending behavior obligations require their original material binding', {'field': field})
        expected = (chapter_plan(source, chapter_id)['sourcePlanHash'] if field == 'sourcePlanHash'
                    else sha256_text(text))
        if value != expected:
            raise ReviewArtifactError('BEHAVIOR_PENDING_BINDING_STALE',
                                      'pending behavior obligations must bind the current material', {'field': field})


def validate_retention(root, source, workflow, chapter_id, payload, text):
    """Unresolved B discoveries cannot disappear through a fresh report."""
    from .repair_history import read_versions
    from .evidence_quotes import quote_span
    _validate_pending_bindings(source, chapter_id, payload, text)
    versions, unavailable = read_versions(root, source, workflow, chapter_id, 'stage_b_review')
    if unavailable:
        raise ReviewArtifactError('BEHAVIOR_HISTORY_UNAVAILABLE', 'restore retained review material before replacing it')

    def require_resolution(required, row, review):
        for disposition, (field, code, message) in BINDING_REQUIREMENTS.items():
            bindings = required.get(disposition, set())
            current = review.get(field)
            if bindings and (None in bindings or not isinstance(current, str)
                             or not current.strip() or current in bindings):
                raise ReviewArtifactError(code, message, {'id': row['id']})
        # Changed bindings only prove that material changed. An explicit current
        # judgment and quotations still carry the semantic repair conclusion.
        require_completed_text(row.get('conclusion'), 'behaviorReviews.' + row['id'])
        if not row.get('evidence'):
            raise ReviewArtifactError('BEHAVIOR_EVIDENCE_REQUIRED', 'closing an obligation needs current quotations')

    retained = {}
    for version in versions:
        old = version['report']['payload']
        for row in old.get('behaviorReviews', []):
            identity, disposition = row['id'], row['disposition']
            if disposition == 'within_plan':
                if identity not in retained or old.get('verdict') != 'POLISH_READY':
                    continue
                try:
                    require_resolution(retained[identity], row, old)
                except ReviewArtifactError:
                    # Previously registered intermediate or bypassed reports do
                    # not erase the original plan/prose obligation on replay.
                    continue
                del retained[identity]
            else:
                required = retained.setdefault(identity, {})
                if disposition in BINDING_REQUIREMENTS:
                    binding = old.get(BINDING_REQUIREMENTS[disposition][0])
                    required.setdefault(disposition, set()).add(binding if isinstance(binding, str) and binding.strip() else None)
    current = {row['id']: row for row in payload.get('behaviorReviews', [])}
    for identity, required in retained.items():
        replacement = current.get(identity)
        if replacement is None:
            raise ReviewArtifactError('BEHAVIOR_FINDING_DROPPED', 'an unresolved behavior review needs explicit current disposition', {'id': identity})
        if replacement['disposition'] == 'within_plan':
            require_resolution(required, replacement, payload)
            if 'replan_required' in required:
                from .chapter_state import _gate_planned
                chapter = next(row for row in source['chapters'] if row['chapterId'] == chapter_id)
                _gate_planned(root, chapter, workflow)
    for row in current.values():
        for quote in row['evidence']:
            quote_span(text, quote)
