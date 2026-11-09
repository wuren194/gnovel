"""Assemble a review from explicit deltas; keep unaddressed findings in the report."""
from copy import deepcopy

from .evidence_quotes import quote_span
from .prepare_dependencies import validate_dependencies
from .review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
from .structure_store import StructureError
from .transport_cache import get

DECISIONS = {'resolved', 'open', 'needs_information'}


def review_input(reference):
    return {'repairRef': reference, 'verdict': 'INCOMPLETE',
            'slots': {slot: {'conclusion': ''} for slot in (*STAGE_B_SLOTS, *AUXILIARY_GATES)},
            'repairDecisions': [], 'findings': []}


def assemble_review(root, chapter_id, payload, *, completed_hash=None):
    saved = get(root, payload['repairRef'], kind='repair')
    if saved['chapterId'] != chapter_id or saved['candidateSha256'] is None:
        raise StructureError('REPAIR_REVIEW_SCOPE_INVALID', 'repair reference must bind this chapter candidate')
    dependencies = deepcopy(saved['dependencies'])
    if completed_hash is not None:
        from .jsonio import read_json
        workflow = read_json(root / '.gnovel/workflow.json')
        entries = workflow.get('artifacts', {}).get(chapter_id, {})
        current = entries.get('stage_b_review', {})
        if (current.get('artifactSha256') == completed_hash and not current.get('invalidatedBy')
                and entries.get('candidate_prose', {}).get('artifactSha256') == saved['candidateSha256']):
            dependencies['files'].pop('.gnovel/workflow.json', None)
    validate_dependencies(root, dependencies, code='REPAIR_REVIEW_STALE')
    rows = {row['id']: row for row in saved['rows'] if row['source'] in {'diagnostics', 'stage_b_review'}}
    supplied = payload.get('repairDecisions', [])
    if not isinstance(supplied, list) or 'repairReview' in payload:
        raise StructureError('REPAIR_DECISION_INVALID', 'provide semantic decisions, not a manufactured repair receipt')
    decisions = {}
    for item in supplied:
        if (not isinstance(item, dict) or set(item) != {'issueId', 'decision', 'reason', 'quotes'}
                or not isinstance(item['issueId'], str) or item['issueId'] not in rows or item['issueId'] in decisions
                or not isinstance(item['decision'], str) or item['decision'] not in DECISIONS
                or not isinstance(item['reason'], str) or not item['reason'].strip()
                or not isinstance(item['quotes'], list) or item['decision'] != 'needs_information' and not item['quotes']):
            raise StructureError('REPAIR_DECISION_INVALID', 'each selected issue needs an explicit decision, reason and current quotations')
        decisions[item['issueId']] = {'issueId': item['issueId'], 'decision': item['decision'], 'reason': item['reason'],
            'evidence': [quote_span(saved['body'], quote) for quote in item['quotes']],
            'bodyHash': saved['draftSha256'], 'finding': deepcopy(rows[item['issueId']]['finding'])}
    result = deepcopy(payload)
    result.pop('repairRef')
    result.pop('repairDecisions', None)
    findings = result.setdefault('findings', [])
    if not isinstance(findings, list):
        raise StructureError('REPAIR_DECISION_INVALID', 'new findings must be an array')
    pending = []
    from .repair_history import blocking_finding
    for identity, row in rows.items():
        decision = decisions.get(identity)
        persistence = row['finding'].get('factChange')
        resolved = decision is not None and decision['decision'] == 'resolved'
        if persistence and (resolved or row['finding'].get('severity') not in {'S1', 'S2', 'blocking'}):
            if not any(f.get('findingId') == identity for f in findings):
                retained = deepcopy(row['finding']); retained['findingId'] = identity
                if resolved:
                    retained['severity'] = 'info'
                findings.append(retained)
            continue
        if resolved:
            continue
        if blocking_finding(row['finding']):
            pending.append(identity)
        if any(f.get('findingId') == identity for f in findings if isinstance(f, dict)):
            # An explicitly supplied current finding is checked by its domain
            # validator below. Keep the old blocking obligation above, without
            # appending a second finding with the same stable identity.
            continue
        finding = deepcopy(row['finding'])
        finding['findingId'] = identity
        finding.setdefault('severity', 'unclassified')
        finding.setdefault('summary', finding.get('message') or str(finding.get('ruleId', 'Unreviewed finding')))
        finding.setdefault('ruleId', row['source'])
        finding.setdefault('repairSuggestion', 'Review the retained issue against the current manuscript')
        quotes = decision['evidence'] if decision else []
        spans = row['location']['spans']
        current_quote = quotes[0] if quotes else ({**spans[0], 'text': saved['body'][spans[0]['start']:spans[0]['end']]}
                                                if len(spans) == 1 else None)
        if current_quote and current_quote['text']:
            finding.update(location=current_quote['text'], start=current_quote['start'], end=current_quote['end'])
        finding['repairState'] = (decision['decision'] if decision else 'needs_recheck') if current_quote and current_quote['text'] else 'needs_location'
        findings.append(finding)
    if result.get('verdict') == 'POLISH_READY' and (pending or saved['unavailableReports']):
        raise StructureError('REPAIR_REVIEW_INCOMPLETE', 'unaddressed findings or unavailable history cannot become POLISH_READY',
                             {'pendingIssueIds': pending, 'unavailableReports': saved['unavailableReports']})
    if pending and result.get('verdict') == 'REPAIR_REQUIRED' and not result.get('repairScope'):
        raise StructureError('REPAIR_SCOPE_REQUIRED', 'the station must choose the repair scope')
    result['repairReview'] = {'candidateSha256': saved['candidateSha256'], 'bodyHash': saved['draftSha256'],
                             'decisions': list(decisions.values()), 'pendingIssueIds': pending,
                             'unavailableReportCount': len(saved['unavailableReports'])}
    from .write_sets import capture_guards
    guards = capture_guards(root, list(dependencies['files']))
    validate_dependencies(root, dependencies, code='REPAIR_REVIEW_STALE')
    dependencies['files'].pop('.gnovel/workflow.json', None)
    return result, guards, dependencies


def replay_review(root, chapter_id, workflow, request):
    """A completed request retains its inputs in the existing artifact receipt."""
    if 'repairDependencies' not in request:
        return None
    from .artifacts import validate_envelope
    from .write_sets import capture_guards, digest, safe_project_path
    import json
    receipt = request['receipt']
    current = workflow.get('artifacts', {}).get(chapter_id, {}).get('stage_b_review', {})
    if current.get('artifactSha256') != receipt['artifactSha256'] or current.get('invalidatedBy'):
        raise StructureError('REPAIR_REVIEW_STALE', 'the completed review is no longer current')
    dependencies = request['repairDependencies']
    validate_dependencies(root, dependencies, code='REPAIR_REVIEW_STALE')
    raw = safe_project_path(root, receipt['artifactPath']).read_bytes()
    artifact = validate_envelope(json.loads(raw), artifact_type='stage_b_review')
    if digest(raw) != request['artifactFileSha256'] or artifact['artifactSha256'] != receipt['artifactSha256']:
        raise StructureError('ARTIFACT_REQUEST_INVALID', 'the completed review file changed')
    guards = capture_guards(root, list(dependencies['files']))
    validate_dependencies(root, dependencies, code='REPAIR_REVIEW_STALE')
    return deepcopy(artifact['payload']), guards, deepcopy(dependencies)


def validate_repair_review(payload):
    value = payload.get('repairReview')
    if 'repairReview' not in payload:
        return
    if (not isinstance(value, dict) or set(value) != {'candidateSha256', 'bodyHash', 'decisions', 'pendingIssueIds', 'unavailableReportCount'}
            or value['bodyHash'] != payload.get('finalProseSha256') or not isinstance(value['candidateSha256'], str)
            or not isinstance(value['decisions'], list) or not isinstance(value['pendingIssueIds'], list)
            or any(not isinstance(rid, str) for rid in value['pendingIssueIds'])
            or type(value['unavailableReportCount']) is not int or value['unavailableReportCount'] < 0):
        raise StructureError('REPAIR_REVIEW_INVALID', 'repair review must bind the current prose and explicit item decisions')
    seen = set()
    for row in value['decisions']:
        if (not isinstance(row, dict) or set(row) != {'issueId', 'decision', 'reason', 'evidence', 'bodyHash', 'finding'}
                or not isinstance(row['issueId'], str) or row['issueId'] in seen
                or not isinstance(row['decision'], str) or row['decision'] not in DECISIONS
                or not isinstance(row['reason'], str) or not row['reason'].strip() or not isinstance(row['evidence'], list)
                or row['decision'] != 'needs_information' and not row['evidence']
                or row['bodyHash'] != value['bodyHash'] or not isinstance(row['finding'], dict)):
            raise StructureError('REPAIR_REVIEW_INVALID', 'invalid per-issue repair decision')
        seen.add(row['issueId'])
        for evidence in row['evidence']:
            if (not isinstance(evidence, dict) or set(evidence) != {'start', 'end', 'text'}
                    or type(evidence['start']) is not int or type(evidence['end']) is not int
                    or not isinstance(evidence['text'], str)
                    or not 0 <= evidence['start'] < evidence['end']
                    or evidence['end'] - evidence['start'] != len(evidence['text'])):
                raise StructureError('REPAIR_REVIEW_INVALID', 'repair evidence requires exact character bounds')
    finding_ids = {row.get('findingId') for row in payload.get('findings', [])}
    if set(value['pendingIssueIds']) - finding_ids:
        raise StructureError('REPAIR_REVIEW_INVALID', 'pending issues must retain their findings')
    if payload.get('verdict') == 'POLISH_READY' and (value['pendingIssueIds'] or value['unavailableReportCount']):
        raise StructureError('REPAIR_REVIEW_INCOMPLETE', 'pending repair items remain')


def validate_current_repair_evidence(root, structure, workflow, chapter_id, payload):
    value = payload.get('repairReview')
    if value is None:
        return
    from .mechanical_evidence import _registered
    from .artifacts import sha256_text
    current = _registered(root, structure, workflow, chapter_id, 'candidate_prose')
    if (current is None or current['artifactSha256'] != value['candidateSha256']
            or sha256_text(current['payload']['text']) != value['bodyHash']):
        raise StructureError('REPAIR_REVIEW_STALE', 'repair decisions belong to another candidate')
    for decision in value['decisions']:
        for evidence in decision['evidence']:
            quote_span(current['payload']['text'], evidence)
