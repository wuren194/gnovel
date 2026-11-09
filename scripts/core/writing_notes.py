"""Candidate-bound deviations and fact proposals; none of these records grants approval."""
from __future__ import annotations

import re

from .artifacts import sha256_text
from .review_artifacts import ReviewArtifactError, require_completed_text


def validate_notes(notes, text):
    from .writing_checks import text_module
    low, high = text_module('intensity_registry').level_bounds()
    if not isinstance(notes, dict) or set(notes) - {'deviations', 'newFacts'}:
        raise ReviewArtifactError('WRITING_NOTES_INVALID', 'notes accepts deviations and newFacts only')
    ids, levels = set(), 0
    for key in ('deviations', 'newFacts'):
        rows = notes.get(key, [])
        if not isinstance(rows, list):
            raise ReviewArtifactError('WRITING_NOTES_INVALID', f'{key} must be an array')
        for row in rows:
            allowed = {'id', 'kind', 'reason', 'intensityLevel', 'requirementId', 'plannedText', 'quote'} if key == 'deviations' else {'id', 'statement', 'quote', 'target'}
            if not isinstance(row, dict) or set(row) - allowed:
                raise ReviewArtifactError('WRITING_NOTES_INVALID', f'invalid {key} item')
            require_completed_text(row.get('id'), f'{key}.id')
            if row['id'] in ids:
                raise ReviewArtifactError('WRITING_NOTES_INVALID', 'note ids must be unique')
            ids.add(row['id'])
            if key == 'newFacts':
                require_completed_text(row.get('statement'), 'newFacts.statement')
                require_completed_text(row.get('quote'), 'newFacts.quote')
                if row['quote'] not in text:
                    raise ReviewArtifactError('WRITING_NOTE_QUOTE_MISSING', 'fact proposal must quote current prose', {'id': row['id']})
                target = row.get('target')
                if target is not None:
                    from pathlib import PurePosixPath
                    if (not isinstance(target, dict) or set(target) != {'document', 'section'}
                            or not isinstance(target['document'], str) or not isinstance(target['section'], str)
                            or not target['section'].strip()):
                        raise ReviewArtifactError('WRITING_FACT_TARGET_INVALID', 'target requires document and section')
                    path = PurePosixPath(target['document'])
                    if (path.is_absolute() or '..' in path.parts or len(path.parts) < 2
                            or path.parts[0] not in {'设定', '规划'} or path.suffix != '.md'):
                        raise ReviewArtifactError('WRITING_FACT_TARGET_INVALID', 'target must be an authored 设定/ or 规划/ Markdown path')
            else:
                require_completed_text(row.get('reason'), 'deviations.reason')
                if row.get('kind') == 'intensityLevel':
                    levels += 1
                    if type(row.get('intensityLevel')) is not int or not low <= row['intensityLevel'] <= high or 'requirementId' in row:
                        raise ReviewArtifactError('WRITING_NOTES_INVALID', 'intensityLevel deviation needs a valid proposed level')
                elif row.get('kind') == 'requirement':
                    require_completed_text(row.get('requirementId'), 'deviations.requirementId')
                    if 'intensityLevel' in row: raise ReviewArtifactError('WRITING_NOTES_INVALID', 'requirement deviation cannot set intensityLevel')
                elif row.get('kind') == 'wording':
                    for field in ('plannedText', 'quote'):
                        require_completed_text(row.get(field), 'deviations.' + field)
                    if row['quote'] not in text or 'intensityLevel' in row or 'requirementId' in row:
                        raise ReviewArtifactError('WRITING_NOTES_INVALID', 'wording needs a current prose quote, not a level or requirement override')
                else:
                    raise ReviewArtifactError('WRITING_NOTES_INVALID', 'deviation kind must be intensityLevel, requirement or wording')
    if levels > 1: raise ReviewArtifactError('WRITING_NOTES_INVALID', 'only one proposed intensity level per candidate')


def proposed_level(notes, original):
    return next((row['intensityLevel'] for row in notes.get('deviations', []) if row['kind'] == 'intensityLevel'), original)


def validate_writing_decisions(review, candidate):
    """A completed stage B must explicitly review every note on this exact candidate."""
    notes = candidate['payload'].get('writingNotes', {})
    items = [*notes.get('deviations', []), *notes.get('newFacts', [])]
    if not items or review.get('verdict') != 'POLISH_READY': return
    if review.get('candidateArtifactSha256') != candidate['artifactSha256']:
        raise ReviewArtifactError('WRITING_REVIEW_CANDIDATE_MISMATCH', 'notes review must bind the complete candidate artifact')
    decisions = review.get('writingDecisions')
    if not isinstance(decisions, list):
        raise ReviewArtifactError('WRITING_DECISIONS_REQUIRED', 'stage B must address every deviation and new fact')
    seen = set()
    for row in decisions:
        if not isinstance(row, dict) or set(row) != {'id', 'decision', 'reason'} or row.get('id') in seen:
            raise ReviewArtifactError('WRITING_DECISIONS_INVALID', 'decisions require distinct ids, decision and reason')
        seen.add(row['id'])
        require_completed_text(row.get('reason'), 'writingDecisions.reason')
        if row.get('decision') != 'accept':
            raise ReviewArtifactError('WRITING_DECISIONS_UNRESOLVED', 'POLISH_READY requires accepted notes; otherwise revise the candidate or use REPAIR_REQUIRED')
    if seen != {row['id'] for row in items}:
        raise ReviewArtifactError('WRITING_DECISIONS_REQUIRED', 'decisions must cover exactly the candidate notes')


def validate_candidate_binding(root, structure, workflow, chapter_id, payload):
    """Every current candidate is bound to its authored source plan."""
    from .current_candidate import require_candidate_selection
    selected = require_candidate_selection(root, structure, chapter_id, payload)
    from .candidate_batch import require_artifact_recipe
    require_artifact_recipe(root, structure, workflow, chapter_id, payload)
    from .source_plan import outline_binding
    fields = [key for key in ('outlineSha256', 'sourcePlanHash') if key in payload]
    if fields != ['sourcePlanHash']:
        raise ReviewArtifactError('CANDIDATE_PLAN_BINDING_REQUIRED', 'candidate prose requires its current sourcePlanHash')
    chapter = next(row for row in structure['chapters'] if row['chapterId'] == chapter_id)
    binding = outline_binding(root, structure, chapter)
    if fields != [binding['field']] or payload[fields[0]] != binding['hash']:
        raise ReviewArtifactError('CANDIDATE_PLAN_STALE', 'candidate prose was registered against a different plan')
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get('stage_a_review', {})
    if selected['selection'] is not None and (not payload.get('stageAArtifactSha256') or
            not workflow.get('approvals', {}).get(chapter_id, {}).get('approved') or
            workflow.get('approvals', {}).get(chapter_id, {}).get('artifactHashes', {}).get('stage_a_review') != payload['stageAArtifactSha256']):
        raise ReviewArtifactError('CANDIDATE_STAGE_A_STALE', 'selected candidate requires its exact current approved stage A')
    if payload.get('stageAArtifactSha256') and (entry.get('invalidatedBy') or payload['stageAArtifactSha256'] != entry.get('artifactSha256')):
        raise ReviewArtifactError('CANDIDATE_STAGE_A_STALE', 'candidate prose was registered against a different stage A review')
    if payload.get('stageAArtifactSha256'):
        from .mechanical_evidence import _registered
        stage_a = _registered(root, structure, workflow, chapter_id, 'stage_a_review')
        if stage_a is None or stage_a['payload'].get(binding['field']) != binding['hash']:
            raise ReviewArtifactError('CANDIDATE_STAGE_A_STALE', 'stage A does not review the candidate plan')
    return {'status': 'bound', 'current': True, binding['field']: binding['hash'],
            'stageAArtifactSha256': payload.get('stageAArtifactSha256'), 'draftSha256': sha256_text(payload['text']),
            'candidateSelection': selected['selection']}


def validate_beat_spans(payload):
    spans, text = payload.get('beatSpans', []), payload.get('text', '')
    if not isinstance(spans, list) or not isinstance(text, str):
        raise ReviewArtifactError('BEAT_SPANS_INVALID', 'beatSpans must be an array over payload.text')
    ids, end = set(), None
    for row in spans:
        if (not isinstance(row, dict) or set(row) != {'id', 'start', 'end'}
                or not isinstance(row['id'], str) or not re.fullmatch(r'B\d+', row['id']) or row['id'] in ids
                or type(row['start']) is not int or type(row['end']) is not int
                or not 0 <= row['start'] < row['end'] <= len(text)
                or end is not None and row['start'] != end):
            raise ReviewArtifactError('BEAT_SPANS_INVALID', 'beat spans must be unique, ordered, contiguous character ranges')
        ids.add(row['id']); end = row['end']
    if spans and end != len(text):
        raise ReviewArtifactError('BEAT_SPANS_INVALID', 'final beat must cover the end of the body')
