"""Disposable edit coordinates bound to one registered candidate; never review credit."""
from __future__ import annotations

from .artifacts import sha256_text
from .structure_store import StructureError
from .transport_cache import get, put

VERSION_KEYS = ('projectId', 'chapterId', 'artifactSha256', 'bodyHash')


def create_anchor(root, *, binding, text, start, end, issue_ids, scope):
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise StructureError('EDIT_ANCHOR_INVALID', 'anchor bounds must select exact candidate text')
    value = {**binding, 'start': start, 'end': end, 'quoteHash': sha256_text(text[start:end]),
             'scope': scope, 'issueIds': sorted(set(issue_ids))}
    _validate(value)
    return put(root, 'edit-anchor', value)


def _validate(value):
    fields = {*VERSION_KEYS, 'start', 'end', 'quoteHash', 'scope', 'issueIds'}
    if (not isinstance(value, dict) or set(value) != fields
            or any(not isinstance(value[key], str) or not value[key].strip()
                   for key in (*VERSION_KEYS, 'quoteHash'))
            or value['scope'] not in ('location', 'window')
            or type(value['start']) is not int or type(value['end']) is not int
            or not 0 <= value['start'] < value['end']
            or not isinstance(value['issueIds'], list) or not value['issueIds']
            or any(not isinstance(issue, str) or not issue.strip() for issue in value['issueIds'])):
        raise StructureError('EDIT_ANCHOR_INVALID', 'edit anchor is malformed; prepare repair context again')


def read_anchor(root, reference):
    value = get(root, reference, kind='edit-anchor')
    _validate(value)
    return value


def resolve_anchor(anchor, text, *, binding, issue_id, selected_range=None):
    """Resolve only after the caller verifies the current registered candidate.

    Bounds in the snapshot are absolute Python character offsets. A requested
    range is relative to the whole selected anchor, never to a transport page.
    """
    _validate(anchor)
    if any(anchor[key] != binding[key] for key in ('projectId', 'chapterId')):
        raise StructureError('EDIT_ANCHOR_SCOPE_MISMATCH', 'anchor belongs to another project or chapter')
    if any(anchor[key] != binding[key] for key in ('artifactSha256', 'bodyHash')):
        raise StructureError('PATCH_BASE_STALE', 'anchor candidate version changed; prepare repair context again')
    if issue_id not in anchor['issueIds']:
        raise StructureError('EDIT_ANCHOR_ISSUE_MISMATCH', 'select an anchor linked to this issue')
    start, end = anchor['start'], anchor['end']
    if end > len(text) or sha256_text(text[start:end]) != anchor['quoteHash']:
        raise StructureError('EDIT_ANCHOR_MISMATCH', 'anchor no longer matches the exact candidate text')
    if selected_range is not None:
        if (not isinstance(selected_range, dict) or set(selected_range) != {'start', 'end'}
                or type(selected_range['start']) is not int or type(selected_range['end']) is not int
                or not 0 <= selected_range['start'] < selected_range['end'] <= end - start):
            raise StructureError('EDIT_ANCHOR_RANGE_INVALID', 'range must lie within the selected anchor')
        start, end = start + selected_range['start'], start + selected_range['end']
    return {'text': text[start:end], 'start': start, 'end': end}
