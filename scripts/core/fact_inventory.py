"""Chapter fact assessment derived from B artifacts, retained findings and source receipts.

This is an execution obligation, not a second fact store. Prose acceptance and
continuity confirmation may finish before its source/evidence transactions.
"""
from copy import deepcopy

from .artifacts import sha256_text
from .review_artifacts import ReviewArtifactError, require_completed_text

ASSESSMENTS = {'unassessed', 'insufficient_material', 'no_change', 'changes'}


def fact_findings(payload):
    return {row['findingId']: row for row in payload.get('findings', []) if 'factChange' in row}


def validate_shape(payload):
    assessment = payload.get('factAssessment')
    if assessment is not None and (not isinstance(assessment, dict) or set(assessment) != {'status'}
                                  or assessment.get('status') not in ASSESSMENTS):
        raise ReviewArtifactError('FACT_ASSESSMENT_INVALID', 'factAssessment needs an explicit status')
    for row in payload.get('findings', []):
        if 'factChange' not in row:
            continue
        value = row['factChange']
        if (not isinstance(row.get('findingId'), str) or not row['findingId'].strip()
                or not isinstance(value, dict) or value.get('status') not in {'pending', 'not_required'}
                or set(value) - {'status', 'requestId', 'targetIds', 'reason', 'evidence'}):
            raise ReviewArtifactError('FACT_FINDING_INVALID', 'fact changes require stable findingId and explicit disposition')
        if not isinstance(value.get('evidence'), list) or not value['evidence']:
            raise ReviewArtifactError('FACT_FINDING_EVIDENCE_REQUIRED', 'fact findings need exact prose evidence')
        require_completed_text(value.get('reason'), 'factChange.reason')
        if value['status'] == 'pending':
            request, targets = value.get('requestId'), value.get('targetIds')
            if (request is None) == (targets is None):
                raise ReviewArtifactError('FACT_FINDING_ROUTE_REQUIRED', 'pending facts need exactly one requestId or existing targetIds route')
            if request is not None and (not isinstance(request, str) or not request.strip()):
                raise ReviewArtifactError('FACT_FINDING_ROUTE_REQUIRED', 'requestId must be nonempty')
            if targets is not None and (not isinstance(targets, list) or not targets
                    or any(not isinstance(rid, str) or not rid.strip() for rid in targets) or len(targets) != len(set(targets))):
                raise ReviewArtifactError('FACT_FINDING_ROUTE_REQUIRED', 'targetIds must be distinct existing source IDs')
        elif 'requestId' in value or 'targetIds' in value:
            raise ReviewArtifactError('FACT_FINDING_INVALID', 'not_required records the disposition, not a source request')


def validate_completion(payload, text):
    from .evidence_quotes import quote_span
    validate_shape(payload)
    status = payload.get('factAssessment', {}).get('status')
    if status not in {'no_change', 'changes'}:
        raise ReviewArtifactError('FACT_ASSESSMENT_REQUIRED', 'B must assess persistent facts and changes before completion')
    pending = [r for r in fact_findings(payload).values() if r['factChange']['status'] == 'pending']
    if (status == 'changes') != bool(pending):
        raise ReviewArtifactError('FACT_ASSESSMENT_CONFLICT', 'changes requires pending fact findings; no_change cannot hide them')
    for row in fact_findings(payload).values():
        for quote in row['factChange']['evidence']:
            quote_span(text, quote)


def retained_findings(root, source, workflow, chapter_id):
    from .repair_history import read_versions
    versions, unavailable = read_versions(root, source, workflow, chapter_id, 'stage_b_review')
    result = {}
    for version in versions:
        result.update(deepcopy(fact_findings(version['report']['payload'])))
    return result, unavailable


def validate_retention(root, source, workflow, chapter_id, payload, text):
    from .evidence_quotes import quote_span
    validate_shape(payload)
    retained, unavailable = retained_findings(root, source, workflow, chapter_id)
    if unavailable:
        raise ReviewArtifactError('FACT_HISTORY_UNAVAILABLE', 'retained B reports cannot be read; fact coverage is unknown',
                                  {'unavailableReports': unavailable})
    supplied = fact_findings(payload)
    missing = {identity for identity, row in retained.items() if row['factChange']['status'] == 'pending'} - set(supplied)
    if missing:
        raise ReviewArtifactError('FACT_FINDINGS_DROPPED', 'known fact findings must be retained or explicitly disposed of',
                                  {'findingIds': sorted(missing)})
    for identity, row in supplied.items():
        # Repairing a sentence never implicitly retires a persistence obligation.
        # An explicit semantic disposition is bound to the newly reviewed prose.
        for quote in row['factChange']['evidence']:
            quote_span(text, quote)
        for rid in row['factChange'].get('targetIds', []):
            from .source_model import SourceIndex
            record = SourceIndex.build(source).records.get(rid)
            if not fact_target(source, record):
                raise ReviewArtifactError('FACT_TARGET_INVALID', 'select a precise fact fragment or an attribute with canonical values, not its owner or field')


def fact_target(source, record):
    if not isinstance(record, dict) or record.get('status') == 'archived':
        return False
    if record.get('fragmentId'):
        return record.get('contentRole') == 'fact' and bool(record.get('text', '').strip())
    if record.get('attributeId'):
        from .source_model import SourceIndex
        field = SourceIndex.build(source).fields.get(record.get('fieldId'), {})
        return field.get('valueType') != 'topic' and bool(record.get('values'))
    return False


def _target_complete(root, source, chapter_id, text, change):
    from .source_evidence import evidence_rows
    from .evidence_quotes import quote_span
    rows, _ = evidence_rows(root, source)
    targets = set(change['targetIds'])
    quotes = [quote_span(text, quote) for quote in change['evidence']]
    matched = set()
    for row in rows:
        if row.get('chapterId') != chapter_id or row.get('status') != 'registered' or row.get('kind') != 'depends_on':
            continue
        locator = row.get('locator', {})
        if any(locator.get('start') == quote['start'] and locator.get('end') == quote['end'] for quote in quotes):
            matched.update(targets.intersection(row['targetIds']))
    return matched == targets


def _quote_set(text, quotes):
    from .evidence_quotes import quote_span
    result = {}
    for quote in quotes:
        resolved = quote_span(text, quote)
        result.setdefault((resolved['start'], resolved['end'], resolved['text']), resolved)
    return result


def _request_binding_evidence(report, chapter, identity, required, text):
    """One exact comparison owns both binding validity and the caller's explanation."""
    manuscript_matches = (report.get('chapterId') == chapter['chapterId'] and report.get('manuscript') == {
            'path': chapter['filePath'], 'sha256': sha256_text(text),
            'bindingGeneration': chapter['proseBinding']['generation']})
    rows = [row for row in report.get('facts', []) if row.get('findingId') == identity]
    try:
        provided = _quote_set(text, [row.get('quote') for row in rows])
    except ValueError as exc:
        return False, {'manuscriptMatches': manuscript_matches, 'evidenceReadable': False,
                       'errorCode': getattr(exc, 'code', 'EVIDENCE_QUOTE_INVALID')}
    explanation = {'manuscriptMatches': manuscript_matches, 'evidenceReadable': True,
                   'providedEvidenceCount': len(provided),
                   'matchedEvidenceCount': len(required.keys() & provided.keys()),
                   'missingRequiredIndexes': [index for index, key in enumerate(required) if key not in provided],
                   'unexpectedEvidence': [quote for key, quote in provided.items() if key not in required]}
    return bool(rows) and manuscript_matches and required.keys() == provided.keys(), explanation


def inventory_status(root, source, workflow, chapter_id):
    from .import_history import history_entry, history_status
    if history_entry(workflow, chapter_id) is not None and chapter_id not in workflow.get('continuity', {}):
        chapter = next((row for row in source['chapters'] if row['chapterId'] == chapter_id), None)
        if chapter is not None:
            return history_status(root, source, workflow, chapter)
    from .repair_history import read_versions
    from .prose_binding import OWNED_CLEAN, require_observation
    from .write_sets import safe_project_path
    from .chapter_facts import complete_chapter_facts
    versions, unavailable = read_versions(root, source, workflow, chapter_id, 'stage_b_review')
    latest = next((v['report'] for v in versions if v['latest']), None)
    blockers, items, requests = [], [], {}
    base = {'assessed': False, 'complete': False, 'status': 'unassessed', 'items': items, 'blockers': blockers}
    if unavailable or latest is None:
        blockers.append({'code': 'FACT_INVENTORY_UNAVAILABLE', 'unavailableReports': unavailable})
        return base
    chapter = next(r for r in source['chapters'] if r['chapterId'] == chapter_id)
    try:
        require_observation(root, source, chapter, (OWNED_CLEAN,), action='fact inventory handoff')
        text = safe_project_path(root, chapter['filePath']).read_bytes().decode('utf-8')
        if sha256_text(text) != latest['payload'].get('finalProseSha256'):
            raise ReviewArtifactError('FACT_INVENTORY_PROSE_STALE', 'fact inventory belongs to different prose')
        approved = workflow.get('approvals', {}).get(chapter_id, {})
        from .continuity import validated_continuity_record
        finalized = (validated_continuity_record(workflow, chapter_id) or {}).get('factReview', {})
        finalized_match = finalized == {'artifactSha256': latest['artifactSha256'], 'proseSha256': sha256_text(text),
                                       'bindingGeneration': chapter['proseBinding']['generation']}
        from .source_plan import chapter_plan
        current_approval = (approved.get('approved') and approved.get('artifactHashes', {}).get('stage_b_review') == latest['artifactSha256']
                            and not workflow['artifacts'][chapter_id]['stage_b_review'].get('invalidatedBy')
                            and latest['payload'].get('proseBindingGeneration') == chapter['proseBinding']['generation']
                            and latest['payload'].get('sourcePlanHash') == chapter_plan(source, chapter_id)['sourcePlanHash'])
        if not finalized_match and not current_approval:
            raise ReviewArtifactError('FACT_INVENTORY_UNAPPROVED', 'fact assessment requires the author-approved B report')
        validate_completion(latest['payload'], text)
        validate_retention(root, source, workflow, chapter_id, latest['payload'], text)
    except (ValueError, OSError) as exc:
        blockers.append({'code': getattr(exc, 'code', 'FACT_INVENTORY_INVALID'), 'message': str(exc)})
        return base
    for identity, finding in fact_findings(latest['payload']).items():
        change = finding['factChange']
        item = {'findingId': identity, 'summary': finding['summary'], 'status': change['status'], 'complete': change['status'] == 'not_required'}
        if change['status'] == 'pending':
            required = _quote_set(text, change['evidence'])
            item.update(requiredEvidence=list(required.values()), requiredEvidenceCount=len(required))
            if 'targetIds' in change:
                item['complete'] = _target_complete(root, source, chapter_id, text, change)
                item['targetIds'] = change['targetIds']
                item['nextAction'] = {'operation': 'evidence.prepare', 'arguments': {'kind': 'prose', 'chapterId': chapter_id}}
            else:
                request_id = change['requestId']; item['requestId'] = request_id
                if request_id not in requests:
                    try:
                        report = complete_chapter_facts(root, requestId=request_id)
                        if report.get('factsIncluded') is False:
                            from .transport_cache import get
                            report = get(root, report['reportRef'], kind='authoring-report')
                        requests[request_id] = report
                    except (ValueError, OSError) as exc:
                        requests[request_id] = {'complete': False, 'code': getattr(exc, 'code', 'CHAPTER_FACTS_PENDING')}
                report = requests[request_id]
                matches, comparison = _request_binding_evidence(report, chapter, identity, required, text)
                item['complete'] = report.get('complete') is True and matches
                if report.get('requestId') == request_id:
                    item['requestEvidence'] = comparison
                item['nextAction'] = report.get('nextAction') or {'operation': 'source.chapter-facts-preview', 'arguments': {
                    'chapterId': chapter_id, 'requestId': request_id}, 'requiredInputs': ['facts including findingId']}
                if report.get('requestId') == request_id and not matches:
                    item['requestStatus'] = 'stale'
                    blockers.append({'code': 'FACT_REQUEST_BINDING_STALE', 'findingId': identity,
                                     'requestId': request_id})
                    item['nextAction'] = {'operation': 'workflow.review-form', 'arguments': {
                        'chapterId': chapter_id, 'artifactType': 'stage_b_review'},
                        'instruction': '对照 requiredEvidence 完整引文集合与 requestEvidence 差异；复核原义务，或为新发现选择新的请求/事项身份。不能改写旧请求绑定或自动批准。'}
            if not item['complete']:
                blockers.append({'code': 'CHAPTER_FACTS_PENDING', 'findingId': identity})
        items.append(item)
    return {**base, 'assessed': True, 'complete': not blockers, 'status': latest['payload']['factAssessment']['status'],
            'reviewArtifactSha256': latest['artifactSha256'], 'proseSha256': sha256_text(text),
            'bindingGeneration': chapter['proseBinding']['generation'], 'requestIds': sorted(requests)}


def require_predecessor_facts(root, source, workflow, *, before_ordinal=None):
    from .continuity import ContinuityError
    pending = []
    for chapter in source['chapters']:
        if chapter.get('status') != 'stable' or before_ordinal is not None and chapter['displayOrdinal'] >= before_ordinal:
            continue
        inventory = inventory_status(root, source, workflow, chapter['chapterId'])
        if not inventory['complete']:
            pending.append({'chapterId': chapter['chapterId'], 'inventory': inventory})
    if pending:
        raise ContinuityError('CHAPTER_FACTS_REQUIRED', 'complete the preceding B fact inventory and source evidence before continuing', {'pending': pending})
