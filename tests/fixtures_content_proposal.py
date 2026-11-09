"""Test-only builder for exact old-record proposals."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from scripts.core.source_content_proposals import resolve_content_target, preview_content_change
from scripts.core.source_review import fingerprint
from scripts.core.structure_store import load_structure


def attribute_value_proposal(root: Path, attribute_id: str, number: int) -> dict:
    source = load_structure(root)
    resolved = resolve_content_target(root, recordId=attribute_id, kind='attribute')
    old = resolved['record']
    updated = deepcopy(old)
    updated['values'][0]['value']['number'] = number
    bound = next((row for row in source['fragments'] if row.get('contentRole') == 'fact'
                  and row.get('status') != 'archived' and attribute_id in row['contentBindings']), None)
    evidence = ([{'fragmentId': bound['fragmentId'], 'quote': bound['text']}]
                if bound is not None else [{'recordHash': fingerprint(old)}])
    return preview_content_change(root, sessionRef=resolved['sessionRef'], expectedOldRecord=old,
                                  newRecord=updated, reason='作者核对属性', evidence=evidence,
                                  approvalActor='author', approvalEvidence='测试批准声明')
