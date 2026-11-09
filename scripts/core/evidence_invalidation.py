"""Invalidate dependent evidence without transferring or erasing historical approvals."""
from __future__ import annotations

from copy import deepcopy

PROSE_DEPENDENT_EVIDENCE = ('diagnostics', 'stage_b_review', 'state_candidate')


def invalidate_prose_evidence(workflow, chapter_id, reason):
    entries = workflow.setdefault('artifacts', {}).setdefault(chapter_id, {})
    invalidated = {}
    for kind in PROSE_DEPENDENT_EVIDENCE:
        entry = entries.get(kind)
        if isinstance(entry, dict) and not entry.get('invalidatedBy'):
            invalidated[kind] = entry.get('artifactSha256') or ''
            entry['invalidatedBy'] = deepcopy(reason)
        if isinstance(entry, dict):
            # Historical resolutions keep their text, but a candidate change
            # permanently retires their former current-input qualification. An
            # old candidate re-registration cannot revive an earlier resolution.
            history = entry.get('repairHistory', [])
            if not isinstance(history, list) or any(not isinstance(reference, dict) for reference in history):
                from .structure_store import StructureError
                raise StructureError('REPAIR_HISTORY_INVALID', 'retained evidence history is unreadable')
            for reference in history:
                if not reference.get('invalidatedBy'):
                    reference['invalidatedBy'] = deepcopy(reason)
    return invalidated
