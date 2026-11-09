"""Sparse information-use review within B artifacts; never a knowledge fact store."""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy

from .artifacts import sha256_json, sha256_text
from .evidence_quotes import quote_span
from .review_artifacts import ReviewArtifactError, require_completed_text

DISPOSITIONS = {'unassessed', 'supported', 'not_applicable', 'needs_material', 'repair_required'}
PENDING = {'needs_material', 'repair_required'}
COMPLETE = {'supported', 'not_applicable'}
ROW_FIELDS = {'id', 'origin', 'use', 'mentions', 'speakerHint', 'characterId', 'information',
              'disposition', 'reason', 'sources', 'findingId'}
SOURCE_FIELDS = {
    'current_prose': {'kind', 'quote'},
    'prior_prose': {'kind', 'quote', 'chapterId', 'proseDigest', 'bindingGeneration'},
    'source_fragment': {'kind', 'quote', 'fragmentId'},
}


def _error(code, message, **details):
    raise ReviewArtifactError(code, message, details)


def scan_candidates(source, chapter_id, text):
    """Locate registered names inside supported quote syntax, never infer knowledge."""
    try:
        from scripts.check_voice_lexicon import QUOTE_RE, attribute_dialogue
    except ImportError:
        from check_voice_lexicon import QUOTE_RE, attribute_dialogue
    people = [row for row in source['entities']
              if row['entityType'] == 'character' and row.get('status') != 'archived']
    names = defaultdict(set)
    for person in people:
        for name in [person['name'], *person.get('aliases', [])]:
            if name.strip():
                names[name].add(person['entityId'])
    if not names:
        return []
    hints = defaultdict(deque)
    for hint in attribute_dialogue(text, [{'characterId': p['entityId'], 'name': p['name']} for p in people]):
        hints[(hint.line, hint.quote)].append(hint)
    candidates, offset = [], 0
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        if line.strip().startswith(('#', '```', '|', '>')):
            offset += len(line)
            continue
        for match in QUOTE_RE.finditer(line):
            quote = match.group(1)
            mentions = [{'term': term, 'entityIds': sorted(ids)} for term, ids in sorted(names.items()) if term in quote]
            if not mentions:
                continue
            queue = hints[(line_number, quote)]
            hint = queue.popleft() if queue else None
            # A preceding quotation may be a label or a false positive. It must
            # not hide a later genuine information use, even by the same speaker.
            use = {'text': quote, 'start': offset + match.start(1), 'end': offset + match.end(1)}
            identity = 'CG-' + sha256_json({'chapterId': chapter_id, 'use': use, 'mentions': mentions})
            candidates.append({'id': identity, 'origin': 'scan', 'use': use, 'mentions': mentions,
                'speakerHint': {'characterId': hint.character_id if hint else None,
                                'confidence': hint.confidence if hint else 'unattributed',
                                'certain': bool(hint and hint.speaker_certain)},
                'characterId': None, 'information': '', 'disposition': 'unassessed', 'reason': '', 'sources': []})
        offset += len(line)
    return candidates


def validate_shape(payload):
    rows = payload.get('cognitionReviews', [])
    if not isinstance(rows, list):
        _error('COGNITION_REVIEW_INVALID', 'cognitionReviews must be an array')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) - ROW_FIELDS
                or any(key not in row for key in ('id', 'origin', 'use', 'characterId', 'information', 'disposition', 'reason', 'sources'))
                or not isinstance(row['id'], str) or not row['id'].strip() or row['id'] in seen
                or not isinstance(row['origin'], str) or row['origin'] not in {'scan', 'semantic'}
                or not isinstance(row['disposition'], str) or row['disposition'] not in DISPOSITIONS
                or row['characterId'] is not None and not isinstance(row['characterId'], str)
                or not isinstance(row['information'], str) or not isinstance(row['reason'], str)
                or not isinstance(row['sources'], list)):
            _error('COGNITION_REVIEW_INVALID', 'invalid information-use identity, disposition or fields')
        seen.add(row['id'])
        if 'findingId' in row and (not isinstance(row['findingId'], str) or not row['findingId'].strip()):
            _error('COGNITION_REVIEW_INVALID', 'findingId must be a nonempty existing issue identity')
        for citation in row['sources']:
            if (not isinstance(citation, dict) or not isinstance(citation.get('kind'), str)
                    or citation['kind'] not in SOURCE_FIELDS):
                _error('COGNITION_SOURCE_INVALID', 'select current_prose, prior_prose or source_fragment evidence')
            kind = citation['kind']
            required = {'kind', 'quote'} | ({'chapterId'} if kind == 'prior_prose' else {'fragmentId'} if kind == 'source_fragment' else set())
            if set(citation) - SOURCE_FIELDS[kind] or required - set(citation):
                _error('COGNITION_SOURCE_INVALID', 'source citation fields do not match its kind')
            for field in ('chapterId', 'fragmentId', 'proseDigest'):
                if field in citation and (not isinstance(citation[field], str) or not citation[field].strip()):
                    _error('COGNITION_SOURCE_INVALID', 'source identities and digests must be nonempty strings', field=field)
            if 'bindingGeneration' in citation and (type(citation['bindingGeneration']) is not int or citation['bindingGeneration'] < 0):
                _error('COGNITION_SOURCE_INVALID', 'bindingGeneration must be a nonnegative integer')


def _pending_history(root, source, workflow, chapter_id):
    from .repair_history import read_versions
    versions, unavailable = read_versions(root, source, workflow, chapter_id, 'stage_b_review')
    if unavailable:
        _error('COGNITION_HISTORY_UNAVAILABLE', 'restore retained review reports before replacing cognition findings', unavailableReports=unavailable)
    pending = {}
    for version in versions:
        payload = version['report']['payload']
        findings = {row.get('findingId'): row for row in payload.get('findings', [])}
        for row in payload.get('cognitionReviews', []):
            identity = row.get('findingId')
            if row['disposition'] in PENDING and identity in findings:
                entry = pending.setdefault(identity, {'finding': deepcopy(findings[identity]), 'requiredProseHashes': set()})
                if row['disposition'] == 'repair_required':
                    entry['requiredProseHashes'].add(payload.get('finalProseSha256'))
            elif identity in pending and row['disposition'] in COMPLETE and payload.get('verdict') == 'POLISH_READY':
                if payload.get('finalProseSha256') not in pending[identity]['requiredProseHashes']:
                    del pending[identity]
        if payload.get('verdict') == 'POLISH_READY':
            for decision in payload.get('repairReview', {}).get('decisions', []):
                identity = decision['issueId']
                if (decision['decision'] == 'resolved' and identity in pending
                        and payload.get('finalProseSha256') not in pending[identity]['requiredProseHashes']):
                    del pending[identity]
    return pending


def retained_findings(root, source, workflow, chapter_id):
    return [deepcopy(row['finding']) for row in _pending_history(root, source, workflow, chapter_id).values()]


def validate_review(root, source, workflow, chapter_id, payload, text):
    """Shared registration/finalization gate; returns guards for cited prior prose."""
    from .cognition_sources import validate_sources
    from .source_plan import chapter_plan
    validate_shape(payload)
    rows = payload.get('cognitionReviews', [])
    expected = {row['id']: row for row in scan_candidates(source, chapter_id, text)}
    supplied = {row['id']: row for row in rows}
    completed = payload.get('verdict') == 'POLISH_READY'
    if completed and expected.keys() - supplied.keys():
        _error('COGNITION_REVIEW_REQUIRED', 'review each located information use; unrelated slot evidence cannot close it',
               missingReviewIds=sorted(expected.keys() - supplied.keys()))
    if rows and (payload.get('finalProseSha256') != sha256_text(text)
                 or payload.get('sourcePlanHash') != chapter_plan(source, chapter_id)['sourcePlanHash']):
        _error('COGNITION_REVIEW_STALE', 'information-use reviews must bind the current prose and chapter inputs')
    findings = {row.get('findingId'): row for row in payload.get('findings', [])}
    characters = {row['entityId'] for row in source['entities']
                  if row['entityType'] == 'character' and row.get('status') != 'archived'}
    guards, issue_rows = [], {}
    for row in rows:
        use = quote_span(text, row['use'])
        if row['id'] in expected and row['origin'] != 'scan':
            _error('COGNITION_USE_STALE', 'a located scan identity cannot be replaced by a semantic item', reviewId=row['id'])
        if row['origin'] == 'scan':
            original = expected.get(row['id'])
            if original is None or any(row.get(key) != original[key] for key in ('use', 'mentions', 'speakerHint')):
                _error('COGNITION_USE_STALE', 'use the current scan identity and frozen quote annotations', reviewId=row['id'])
        if row.get('findingId'):
            if row['findingId'] in issue_rows:
                _error('COGNITION_FINDING_DUPLICATE', 'resolve each retained issue once', findingId=row['findingId'])
            issue_rows[row['findingId']] = row
        if row['disposition'] != 'unassessed':
            require_completed_text(row['reason'], 'cognitionReviews.' + row['id'] + '.reason')
        if row['characterId'] is not None and row['characterId'] not in characters:
            _error('COGNITION_CHARACTER_INVALID', 'select an existing active character or retain unknown attribution', reviewId=row['id'])
        if row['disposition'] == 'supported':
            if row['characterId'] not in characters or not row['sources']:
                _error('COGNITION_SOURCE_REQUIRED', 'supported use needs its character and actual source citations', reviewId=row['id'])
            require_completed_text(row['information'], 'cognitionReviews.' + row['id'] + '.information')
        elif row['sources'] and row['characterId'] not in characters:
            _error('COGNITION_CHARACTER_INVALID', 'source evidence needs the actual information-using character', reviewId=row['id'])
        guards.extend(validate_sources(root, source, chapter_id, row, text, use))
        if row['disposition'] in PENDING:
            identity = row.get('findingId')
            if identity not in findings:
                _error('COGNITION_FINDING_REQUIRED', 'unresolved cognition needs its existing finding and repair route', reviewId=row['id'])
        if completed and row['disposition'] not in COMPLETE:
            _error('COGNITION_REVIEW_PENDING', 'unassessed, uncertain or repair-required uses cannot be marked ready', reviewId=row['id'], disposition=row['disposition'])
    pending = _pending_history(root, source, workflow, chapter_id)
    decisions = {row['issueId']: row for row in payload.get('repairReview', {}).get('decisions', [])}
    for identity, obligation in pending.items():
        row = issue_rows.get(identity)
        resolved = bool(row and row['disposition'] in COMPLETE) or decisions.get(identity, {}).get('decision') == 'resolved'
        if resolved:
            if payload.get('finalProseSha256') in obligation['requiredProseHashes']:
                _error('COGNITION_REPAIR_REQUIRED', 'a known prose defect requires changed prose and an explicit current review', findingId=identity)
        elif identity not in findings or completed:
            _error('COGNITION_FINDING_DROPPED', 'keep or explicitly resolve known cognition findings', findingId=identity)
    unique = {}
    for guard in guards:
        if guard['path'] in unique and unique[guard['path']] != guard:
            _error('COGNITION_SOURCE_STALE', 'a shared evidence file changed between citations', path=guard['path'])
        unique[guard['path']] = guard
    return list(unique.values())
