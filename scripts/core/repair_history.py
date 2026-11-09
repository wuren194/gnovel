"""Repair provenance over registered artifacts and patch receipts, not a task ledger."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json

from .paths import stored_project_path
from .artifacts import sha256_json, sha256_text
from .paths import safe_project_path
from .review_artifacts import validate_artifact_binding
from .structure_store import StructureError
from .write_sets import digest

REPAIR_ARTIFACTS = {'candidate_prose', 'diagnostics', 'stage_b_review'}
REFERENCE_FIELDS = ('artifactPath', 'artifactSha256', 'artifactFileSha256', 'patchReceipt')


def retain_history(entry, previous):
    if not isinstance(previous, dict):
        return
    history = deepcopy(previous.get('repairHistory', []))
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise StructureError('REPAIR_HISTORY_INVALID', 'registered repair history is unreadable')
    if previous.get('artifactSha256') != entry['artifactSha256'] and previous.get('artifactPath'):
        reference = {key: deepcopy(previous[key]) for key in REFERENCE_FIELDS if key in previous}
        # Re-registering an older envelope gives it a new position in the
        # execution sequence. Move its reference, preserving any invalidation
        # annotations, instead of leaving it before a later resolution.
        matching = [row for row in history if row.get('artifactSha256') == reference['artifactSha256']]
        history = [row for row in history if row.get('artifactSha256') != reference['artifactSha256']]
        history.append({**(matching[-1] if matching else {}), **reference})
    if history:
        entry['repairHistory'] = history


def finding_ids(report):
    """Legacy findings keep identity when unchanged; explicit findingId survives rewrites."""
    duplicates, seen = Counter(), set()
    result = []
    for finding in report['payload'].get('findings', []):
        identity = finding.get('findingId')
        if identity is None:
            key = sha256_json(finding)
            identity = 'F-' + sha256_json({'projectId': report['projectId'], 'chapterId': report['chapterId'],
                'kind': report['artifactType'], 'finding': key, 'occurrence': duplicates[key]})
            duplicates[key] += 1
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise StructureError('REPAIR_IDENTITY_INVALID', 'findingIds must be distinct nonempty strings')
        seen.add(identity)
        result.append(identity)
    return result


def read_versions(root, structure, workflow, chapter_id, kind):
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get(kind)
    if not isinstance(entry, dict):
        return [], []
    history = entry.get('repairHistory', [])
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise StructureError('REPAIR_HISTORY_INVALID', 'registered repair history is unreadable')
    versions, unavailable = [], []
    references = [*history, entry]
    last_position = {reference.get('artifactSha256'): position for position, reference in enumerate(references)}
    for position, reference in enumerate(references):
        # A historical envelope may be explicitly registered again. Its latest
        # position is authoritative for pending-item replay; deduplicating the
        # current entry against its earlier occurrence would hide reopened work.
        if last_position[reference.get('artifactSha256')] != position:
            continue
        try:
            path = stored_project_path(root, reference['artifactPath'])
            raw = path.read_bytes()
            report = json.loads(raw)
            validate_artifact_binding(report, project_id=structure['projectId'], chapter_id=chapter_id)
            if (report['artifactType'] != kind or report['artifactSha256'] != reference.get('artifactSha256')
                    or digest(raw) != reference.get('artifactFileSha256')):
                raise ValueError('registered historical artifact bytes changed')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            unavailable.append({'artifactType': kind, 'artifactSha256': reference.get('artifactSha256'),
                                'reason': str(exc)})
            continue
        versions.append({'report': report, 'reference': reference,
                         'latest': reference is entry, 'invalidated': bool(entry.get('invalidatedBy'))})
    return versions, unavailable


def blocking_finding(finding: dict) -> bool:
    return finding.get('severity') in {'S1', 'S2', 'blocking'}


def stage_b_obligations(root, structure, workflow, chapter_id, candidate) -> dict:
    """Fold the original B history in order; omission is never a disposition.

    A resolution can survive an unrelated newer report, but only for the exact
    candidate artifact and authored plan it reviewed. A newer reported problem
    reopens its ID; an older resolution cannot reach past it.
    """
    from .source_plan import chapter_plan
    from .evidence_quotes import quote_span
    versions, unavailable = read_versions(root, structure, workflow, chapter_id, 'stage_b_review')
    body = candidate['payload']['text']
    body_hash = sha256_text(body)
    plan_hash = chapter_plan(structure, chapter_id)['sourcePlanHash']
    pending, resolved = {}, {}
    for version in versions:
        report, reference = version['report'], version['reference']
        payload = report['payload']
        repair = payload.get('repairReview', {})
        current = (not reference.get('invalidatedBy')
                   and payload.get('sourcePlanHash') == plan_hash
                   and repair.get('candidateSha256') == candidate['artifactSha256']
                   and repair.get('bodyHash') == body_hash)
        for identity, finding in zip(finding_ids(report), payload.get('findings', [])):
            previous = pending.get(identity)
            # A renamed severity is not an explicit disposition of an already
            # established blocker. Retain its actual evidence until resolved.
            if previous is None or not blocking_finding(previous['finding']) or blocking_finding(finding):
                pending[identity] = {'id': identity, 'finding': deepcopy(finding),
                    'bodyHash': payload.get('finalProseSha256'), 'artifactSha256': report['artifactSha256']}
            resolved.pop(identity, None)
        for decision in repair.get('decisions', []):
            identity = decision['issueId']
            row = pending.setdefault(identity, {'id': identity, 'finding': deepcopy(decision['finding']),
                'bodyHash': decision['bodyHash'], 'artifactSha256': report['artifactSha256']})
            valid = current
            if valid:
                try:
                    for quote in decision['evidence']:
                        quote_span(body, quote)
                except (ValueError, TypeError, KeyError):
                    valid = False
            row['previousDecision'] = {**deepcopy(decision), 'current': valid,
                                       'artifactSha256': report['artifactSha256']}
            if valid and decision['decision'] == 'resolved':
                resolved[identity] = row
                pending.pop(identity, None)
            else:
                resolved.pop(identity, None)
    # Facts and cognition own their explicit, nonblocking dispositions. A
    # generic history projection must not reopen one after that owner has
    # accepted its resolution. Blocking prose findings still require the
    # candidate-bound repair decisions above, even when they share a domain ID.
    fact_ids = {identity for version in versions
                for identity, finding in zip(finding_ids(version['report']),
                                             version['report']['payload'].get('findings', []))
                if finding.get('factChange')}
    cognition_ids = {row['findingId'] for version in versions
                     for row in version['report']['payload'].get('cognitionReviews', [])
                     if row.get('findingId')}
    specialist_pending = set()
    if fact_ids:
        from .fact_inventory import retained_findings
        facts, _ = retained_findings(root, structure, workflow, chapter_id)
        specialist_pending.update(identity for identity, finding in facts.items()
                                  if finding['factChange']['status'] == 'pending')
    if cognition_ids:
        from .cognition_review import retained_findings
        specialist_pending.update(row['findingId'] for row in retained_findings(root, structure, workflow, chapter_id))
    for identity in (fact_ids | cognition_ids) - specialist_pending:
        if identity in pending and not blocking_finding(pending[identity]['finding']):
            resolved[identity] = pending.pop(identity)
    return {'pending': pending, 'resolved': resolved, 'unavailableReports': unavailable,
            'candidateSha256': candidate['artifactSha256'], 'sourcePlanHash': plan_hash,
            'blockingIssueIds': sorted(rid for rid, row in pending.items() if blocking_finding(row['finding']))}


def require_obligations_closed(root, structure, workflow, chapter_id, payload, candidate) -> None:
    if payload.get('verdict') != 'POLISH_READY':
        return
    from .evidence_quotes import quote_span
    obligations = stage_b_obligations(root, structure, workflow, chapter_id, candidate)
    repair = payload.get('repairReview', {})
    body = candidate['payload']['text']
    decisions = {row['issueId']: row for row in repair.get('decisions', [])}
    open_ids = []
    for identity in obligations['blockingIssueIds']:
        decision = decisions.get(identity)
        valid = (decision is not None and decision.get('decision') == 'resolved'
                 and repair.get('candidateSha256') == candidate['artifactSha256']
                 and repair.get('bodyHash') == sha256_text(body)
                 and payload.get('sourcePlanHash') == obligations['sourcePlanHash'])
        if valid:
            for quote in decision['evidence']:
                quote_span(body, quote)
        else:
            open_ids.append(identity)
    if open_ids or obligations['unavailableReports']:
        raise StructureError('REPAIR_REVIEW_INCOMPLETE', 'previous blocking findings require explicit current dispositions',
            {'pendingIssueIds': open_ids, 'unavailableReports': obligations['unavailableReports'],
             'readAction': {'operation': 'workflow.repair-context', 'arguments': {'chapterId': chapter_id}}})


def candidate_history(root, structure, workflow, chapter_id, current):
    versions, unavailable = read_versions(root, structure, workflow, chapter_id, 'candidate_prose')
    by_artifact = {row['report']['artifactSha256']: row['report']['payload']['text'] for row in versions}
    bodies = {sha256_text(text): text for text in by_artifact.values()}
    bodies[sha256_text(current['text'])] = current['text']
    transitions = {}
    for row in versions:
        pointer = row['reference'].get('patchReceipt')
        if pointer is None:
            continue
        try:
            before_hash, transition = _patch_transition(root, pointer, row['report']['payload']['text'], by_artifact)
            transitions[before_hash] = transition
        except (OSError, ValueError, KeyError, TypeError) as exc:
            unavailable.append({'artifactType': 'patch_receipt', 'artifactSha256': row['report']['artifactSha256'],
                                'reason': str(exc)})
    return bodies, transitions, unavailable


def _patch_transition(root, pointer, after, by_artifact):
    raw = safe_project_path(root, pointer['path']).read_bytes()
    if digest(raw) != pointer['sha256']:
        raise StructureError('REPAIR_PATCH_INVALID', 'registered patch receipt changed')
    receipt = json.loads(raw)
    before = by_artifact.get(receipt.get('baseCandidateSha256'))
    if before is None:
        raise StructureError('REPAIR_PATCH_INVALID', 'patch baseline is not retained')
    patches = receipt.get('patches', [])
    parts, offset = [], 0
    for patch in patches:
        start, end = patch['start'], patch['end']
        if not 0 <= offset <= start < end <= len(before) or before[start:end] != patch['text']:
            raise StructureError('REPAIR_PATCH_INVALID', 'patch no longer matches its baseline')
        parts.extend((before[offset:start], patch['replacement']))
        offset = end
    if ''.join([*parts, before[offset:]]) != after:
        raise StructureError('REPAIR_PATCH_INVALID', 'patch receipt does not reconstruct its candidate')
    return sha256_text(before), {'afterHash': sha256_text(after), 'patches': patches, 'path': pointer['path']}


def map_span(start, end, issue_id, before_hash, after_hash, transitions):
    visited, receipts, state = set(), [], 'unchanged'
    while before_hash != after_hash:
        if before_hash in visited or before_hash not in transitions:
            return None
        visited.add(before_hash)
        transition = transitions[before_hash]
        overlapping = [patch for patch in transition['patches'] if patch['start'] < end and patch['end'] > start]
        if any(patch['issueId'] != issue_id for patch in overlapping):
            return None

        def shifted(position, *, right_boundary=False):
            delta = 0
            for patch in transition['patches']:
                left, right = patch['start'], patch['end']
                if position >= right:
                    delta += len(patch['replacement']) - (right - left)
                elif position <= left:
                    break
                else:
                    return left + delta + (len(patch['replacement']) if right_boundary else 0)
            return position + delta

        start, end = shifted(start), shifted(end, right_boundary=True)
        if overlapping:
            state = 'patched'
        receipts.append(transition['path'])
        before_hash = transition['afterHash']
    return {'start': start, 'end': end, 'state': state, 'receipts': receipts}
