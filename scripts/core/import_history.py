"""Current applicability of author-accepted retrospective manuscript reviews.

This evidence never supplies prose ownership or pretends a pre-writing A/B existed.
"""
from .artifacts import sha256_json
from .structure_store import StructureError


def history_entry(workflow, chapter_id):
    entries = workflow.get('importReviews', {})
    if not isinstance(entries, dict):
        raise StructureError('IMPORT_REVIEW_INVALID', 'import review index must be an object')
    return entries.get(chapter_id)


def chapter_review_shape(source, chapter):
    return sha256_json({'chapter': {k: v for k, v in chapter.items() if k not in {'status', 'proseBinding', 'filePath'}},
                       'arcs': [row for row in source['chapterArcLinks'] if row['chapterId'] == chapter['chapterId']]})


def require_history(root, source, workflow, chapter):
    from .operation_receipts import require_committed_operation
    from .prose_binding import OWNED_CLEAN, require_observation
    from .source_model import SourceIndex
    row = history_entry(workflow, chapter['chapterId'])
    if not isinstance(row, dict) or not isinstance(row.get('operationId'), str):
        raise StructureError('IMPORT_REVIEW_MISSING', 'chapter has no accepted retrospective review')
    receipt = require_committed_operation(root, row['operationId'])
    review = receipt.get('importOrganization', {}).get('accepted', {}).get(chapter['chapterId'])
    if not isinstance(review, dict) or row != {'operationId': receipt['operationId'], 'reviewHash': sha256_json(review)}:
        raise StructureError('IMPORT_REVIEW_INVALID', 'review index does not match committed evidence')
    from .source_requests import operation_undone
    if operation_undone(root, receipt['operationId']):
        raise StructureError('IMPORT_REVIEW_INVALID', 'retrospective acceptance was undone')
    observed = require_observation(root, source, chapter, (OWNED_CLEAN,), action='import retrospective handoff')
    if (chapter.get('status') != 'stable' or review['proseHash'] != observed['observedContentDigest'].removeprefix('sha256:')
            or review['bindingGeneration'] != observed['bindingGeneration']
            or review.get('chapterShapeHash') != chapter_review_shape(source, chapter)):
        raise StructureError('IMPORT_REVIEW_STALE', 'accepted review belongs to different prose or lifecycle')
    index = SourceIndex.build(source)
    for rid, expected in review['recordHashes'].items():
        if sha256_json(index.records.get(rid)) != expected:
            raise StructureError('IMPORT_REVIEW_STALE', 'a record used by the retrospective review changed', {'recordId': rid})
    from .source_evidence import read_evidence
    batch_id = 'PE-' + sha256_json({'operationId': receipt['operationId'], 'chapterId': chapter['chapterId']}).upper()
    batch = read_evidence(root)['batches'].get(batch_id)
    expected_citations = [row for row in receipt['importOrganization']['citations'] if row['chapterId'] == chapter['chapterId']]
    if (not isinstance(batch, dict) or batch.get('fileHash') != review['proseHash']
            or [{key: row.get(key) for key in ('start', 'end', 'text', 'targetIds', 'kind', 'reason')} for row in batch['citations']]
            != [{key: row[key] for key in ('start', 'end', 'text', 'targetIds', 'kind', 'reason')} for row in expected_citations]):
        raise StructureError('IMPORT_REVIEW_STALE', 'registered prose evidence is missing or differs from accepted review')
    return {'kind': 'accepted_import_review', 'operationId': receipt['operationId'], 'reviewHash': row['reviewHash'],
            'semanticReviewComplete': True, 'historicalStageAClaimed': False,
            'bindingGeneration': review['bindingGeneration'], 'proseSha256': review['proseHash']}


def history_status(root, source, workflow, chapter):
    try:
        proof = require_history(root, source, workflow, chapter)
        return {'status': 'complete', 'readyForContinuation': True, 'continuityComplete': True,
                'assessed': True, 'complete': True, 'items': [], 'blockers': [], 'importReview': proof,
                'batchHash': proof['reviewHash'], 'completedAt': None}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'status': 'review_required', 'readyForContinuation': False, 'continuityComplete': False,
                'assessed': False, 'complete': False, 'items': [],
                'blockers': [{'code': getattr(exc, 'code', 'IMPORT_REVIEW_INVALID'), 'message': str(exc)}]}
