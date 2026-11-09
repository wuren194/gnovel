"""Bind explicit relationship decisions to existing A/B review evidence.

The gate verifies coverage and quotations, never whether a motive or cost is
persuasive. Literary judgments and author approval remain separate actions.
"""
from __future__ import annotations

from .review_artifacts import ReviewArtifactError, require_completed_text

REVIEW_FIELD = 'relationshipCausalityReviews'
REVIEW_FIELDS = frozenset(('id', 'conclusion', 'evidence'))
DECLARATION_QUOTE_FIELDS = frozenset(('capabilityAssessment', 'resistanceCost', 'complianceCost',
                                     'choiceReason', 'scope', 'continuation.reason'))


def validate_review_shape(payload: dict) -> None:
    if REVIEW_FIELD not in payload:
        return
    rows = payload[REVIEW_FIELD]
    if not isinstance(rows, list):
        raise ReviewArtifactError('CAUSALITY_REVIEW_INVALID', 'relationship reviews must be an array')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != REVIEW_FIELDS
                or not isinstance(row['id'], str) or not row['id'].strip()
                or row['id'] in seen or not isinstance(row['conclusion'], str)
                or not isinstance(row['evidence'], list)):
            raise ReviewArtifactError('CAUSALITY_REVIEW_INVALID',
                                      'each relationship review needs a unique id, conclusion and evidence array')
        seen.add(row['id'])


def validate_causality_completion(source: dict, chapter_id: str, payload: dict,
                                  artifact_type: str, text: str | None = None) -> None:
    """Shared registration, drafting and finalization check; no new receipt store."""
    from .evidence_quotes import quote_span
    from .relationship_causality import chapter_causality
    from .source_plan import chapter_plan

    validate_review_shape(payload)
    complete = (payload.get('decision') in {'adopt', 'modify'} if artifact_type == 'stage_a_review'
                else payload.get('verdict') == 'POLISH_READY')
    if not complete:
        return
    declarations = chapter_causality(source, chapter_id)
    if payload.get('sourcePlanHash') != chapter_plan(source, chapter_id)['sourcePlanHash']:
        raise ReviewArtifactError('CAUSALITY_REVIEW_PLAN_STALE',
                                  'relationship review must bind the current source plan')
    rows = payload.get(REVIEW_FIELD, [])
    by_id = {row['id']: row for row in declarations}
    expected = set(by_id)
    supplied = {row['id'] for row in rows}
    if supplied != expected:
        raise ReviewArtifactError('CAUSALITY_REVIEW_REQUIRED',
                                  'every declared relationship decision needs its own review',
                                  {'missing': sorted(expected - supplied), 'unexpected': sorted(supplied - expected)})
    if artifact_type == 'stage_a_review':
        plans = sorted((f for f in source['fragments'] if f['ownerId'] == chapter_id
                        and f['contentRole'] == 'plan' and f.get('status') != 'archived'),
                       key=lambda f: (f['order'], f['fragmentId']))
        text = '\n\n'.join(f['text'] for f in plans)
    if rows and (not isinstance(text, str) or not text.strip()):
        raise ReviewArtifactError('CAUSALITY_REVIEW_MATERIAL_REQUIRED',
                                  'completed relationship review requires current review material')
    from .behavior_review import validate_completion
    validate_completion(source, chapter_id, payload, artifact_type, text)
    for row in rows:
        require_completed_text(row['conclusion'], f'{REVIEW_FIELD}.{row["id"]}.conclusion')
        if not row['evidence']:
            raise ReviewArtifactError('CAUSALITY_REVIEW_EVIDENCE_REQUIRED',
                                      'relationship review requires exact quotations from current material',
                                      {'id': row['id']})
        for quote in row['evidence']:
            if isinstance(quote, dict) and 'declarationField' in quote:
                field = quote['declarationField']
                if (artifact_type != 'stage_a_review' or not isinstance(field, str)
                        or field not in DECLARATION_QUOTE_FIELDS):
                    raise ReviewArtifactError('CAUSALITY_REVIEW_FIELD_INVALID',
                                              'only stage A can quote a declared reasoning field of the same item')
                declaration = by_id[row['id']]
                material = (declaration['continuation']['reason'] if field == 'continuation.reason'
                            else declaration[field])
                quote_span(material, {key: value for key, value in quote.items() if key != 'declarationField'})
            else:
                quote_span(text, quote)
