"""Prevent new source records from borrowing identities in the committed ledger.

The operation ledger already records every changed source ID, including removed
nested values and formal undo changes. It survives backup GC and is the only
historical authority here: previews and disposable indexes do not reserve IDs.
"""
from __future__ import annotations

import re
from pathlib import Path

from .jsonio import read_json
from .source_model import SourceIndex
from .structure_store import StructureError
from .undo_journal import OPERATION_ID_PATTERN, OPERATIONS_DIR
from .write_sets import safe_project_path


def source_identities(source: dict, *, index: SourceIndex | None = None) -> set[str]:
    """All authored stable IDs, including values nested inside attributes."""
    return (set((index or SourceIndex.build(source)).records)
            | {row['candidateId'] for row in source.get('planningCandidates', [])}
            | {row['decisionId'] for row in source.get('authorDecisions', [])})


def _operation_identities(path: Path, *, relative: str) -> set[str]:
    """Read only the committed operation's existing identity projection.

    Before-images may have been collected, so they are deliberately not required.
    Malformed ledger evidence is unknown, never an empty identity inventory.
    """
    def invalid(reason):
        return StructureError('SOURCE_IDENTITY_HISTORY_INVALID',
                              'committed source identity evidence cannot be verified',
                              {'path': relative, 'reason': reason})

    if not path.is_file():
        raise invalid('operation_not_regular_file')
    try:
        record = read_json(path)
    except (OSError, ValueError, UnicodeError) as exc:
        raise invalid('operation_unreadable') from exc
    if (not isinstance(record, dict) or record.get('schemaVersion') != 4
            or not OPERATION_ID_PATTERN.fullmatch(path.stem)
            or record.get('operationId') != path.stem):
        raise invalid('operation_identity_invalid')
    if any(not isinstance(record.get(key), str) or not record[key].strip()
           for key in ('operation', 'committedAt')):
        raise invalid('operation_commit_evidence_invalid')
    before, after = record.get('beforeRevision'), record.get('afterRevision')
    if type(before) is not int or before < 0 or type(after) is not int or after != before + 1:
        raise invalid('operation_revision_invalid')
    if any(not isinstance(record.get(key), str)
           or not re.fullmatch(r'[0-9a-f]{64}', record[key]) for key in ('beforeHash', 'afterHash')):
        raise invalid('operation_hash_invalid')
    impact = record.get('impact')
    if not isinstance(impact, dict):
        raise invalid('operation_impact_invalid')
    identities = set()
    for key in ('changedIds', 'planningCandidateChanges', 'authorDecisionChanges'):
        values = impact.get(key)
        if (not isinstance(values, list)
                or any(not isinstance(value, str) or not value.strip() for value in values)):
            raise invalid('operation_identity_list_invalid:' + key)
        identities.update(values)
    return identities


def require_identity_impact(current: dict, proposed: dict, plan: dict) -> None:
    """A rehashed plan cannot publish an incomplete or fabricated identity ledger."""
    from .source_impact import identity_changes
    expected = identity_changes(current, proposed)
    impact = plan.get('impact')
    mismatched = [key for key, values in expected.items()
                  if not isinstance(impact, dict) or impact.get(key) != values]
    if mismatched:
        raise StructureError('SOURCE_IDENTITY_IMPACT_MISMATCH',
                             'plan identity impact does not match the current and proposed source',
                             {'operationId': plan.get('operationId'), 'fields': mismatched,
                              'instruction': '身份变更清单与实际源差异不符；重新预览，不修改或补写旧计划。'})


def require_fresh_source_identities(root: Path, current: dict, proposed: dict) -> None:
    """Preview and commit share this new-record boundary.

    An existing record keeps its ID through normal edits. Formal undo restores a
    verified before-image through its own engine, rather than creating a record
    through this boundary. An undone creation still appears in the ledger and
    cannot subsequently lend its ID to a different new record.
    """
    old, new = SourceIndex.build(current), SourceIndex.build(proposed)
    reassigned = sorted(rid for rid in old.value_attributes.keys() & new.value_attributes.keys()
                        if old.value_attributes[rid] != new.value_attributes[rid])
    if reassigned:
        raise StructureError('SOURCE_VALUE_ID_REASSIGNED',
                             'an attribute value ID cannot be transferred to another attribute',
                             {'recordIds': reassigned,
                              'changes': [{'valueId': rid, 'beforeAttributeId': old.value_attributes[rid],
                                           'afterAttributeId': new.value_attributes[rid]} for rid in reassigned]})
    added = source_identities(proposed, index=new) - source_identities(current, index=old)
    if not added:
        return
    directory = safe_project_path(root, OPERATIONS_DIR)
    if not directory.exists():
        return
    if not directory.is_dir():
        raise StructureError('SOURCE_IDENTITY_HISTORY_INVALID', 'operation ledger is not a directory',
                             {'path': OPERATIONS_DIR, 'reason': 'ledger_not_directory'})
    for candidate in sorted(directory.glob('OP-*.json')):
        relative = candidate.relative_to(root.resolve()).as_posix()
        path = safe_project_path(root, relative)
        reused = added & _operation_identities(path, relative=relative)
        if reused:
            raise StructureError('SOURCE_ID_RETIRED',
                                 'new content must use a fresh ID; use formal undo to restore a deleted record',
                                 {'recordIds': sorted(reused), 'operationId': path.stem,
                                  'path': relative, 'instruction': '新内容省略ID或使用临时句柄；恢复原记录使用可验证的正式撤销。'})
