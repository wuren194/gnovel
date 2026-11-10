"""Exact new-chapter manuscript authority shared by append and chapter reshaping.

Domain validators reconstruct the permitted source delta. This module binds
their frozen bytes to new, empty locators; the existing write-set engine owns
publication and recovery. Existing chapters never acquire a new binding here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .file_preservation import observation
from .jsonio import read_json
from .structure_store import StructureError
from .write_sets import GUARD_KEYS, digest, json_bytes, safe_project_path, validate_guard_groups, validate_guards


OPERATIONS = frozenset({'append_manuscript', 'chapter_split', 'chapter_merge'})


@dataclass
class Materialization:
    content_digests: dict[str, str]
    outputs: dict[str, bytes]
    guards: list[dict]
    guard_groups: list[dict]


def materialization_inputs(root: Path, current: dict, plan: dict) -> Materialization | None:
    operation = plan.get('operation')
    if operation not in OPERATIONS:
        if any(key in plan for key in ('manuscriptMaterialization', 'manuscriptGuards', 'manuscriptGuardGroups')):
            raise StructureError('OWNERSHIP_UNAUTHORIZED_MUTATION', 'this operation cannot materialize manuscript content')
        return None
    rows = plan.get('manuscriptMaterialization')
    if not isinstance(rows, list) or not rows:
        raise StructureError('MANUSCRIPT_MATERIALIZATION_INVALID', 'new chapter manuscripts are required')
    if plan.get('bindingTransitions') or plan.get('pathMoves'):
        raise StructureError('MANUSCRIPT_MATERIALIZATION_INVALID', 'intake cannot change existing prose bindings or paths')
    if operation == 'append_manuscript':
        from .manuscript_append import validate_append_plan, append_evidence_outputs, append_guard_groups
        guards = list(validate_append_plan(root, current, plan))
        outputs = append_evidence_outputs(root, plan)
        groups = append_guard_groups(root, plan)
    else:
        from .chapter_reshape import validate_reshape_plan
        guards = list(validate_reshape_plan(root, current, plan))
        outputs, groups = {}, []
    existing = {row['chapterId'] for row in current['chapters']}
    proposed = {row['chapterId']: row for row in plan['proposedStructure']['chapters']}
    content = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'chapterId', 'filePath', 'sourcePath', 'contentDigest'}:
            raise StructureError('MANUSCRIPT_MATERIALIZATION_INVALID', 'invalid manuscript declaration')
        cid = row['chapterId']
        if not isinstance(cid, str) or cid in existing or cid in content or cid not in proposed:
            raise StructureError('MANUSCRIPT_MATERIALIZATION_INVALID', 'manuscripts must name distinct new chapter IDs')
        expected_source = f".govel/plans/{plan['operationId']}.intake/chapters/{cid}.md"
        if (row['sourcePath'] != expected_source or row['filePath'] != proposed[cid]['filePath']
                or not isinstance(row['contentDigest'], str) or not re.fullmatch('[0-9a-f]{64}', row['contentDigest'])):
            raise StructureError('MANUSCRIPT_MATERIALIZATION_INVALID', 'manuscript identity, path or digest differs')
        state, payload = observation(safe_project_path(root, row['sourcePath']))
        if payload is None or digest(payload) != row['contentDigest']:
            raise StructureError('MANUSCRIPT_INPUT_CHANGED', 'frozen chapter manuscript changed', {'chapterId': cid})
        try:
            payload.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise StructureError('MANUSCRIPT_MATERIALIZATION_INVALID', 'new manuscript is not UTF-8') from exc
        if row['filePath'] in outputs:
            raise StructureError('WRITE_TARGET_CONFLICT', 'manuscript producers share a destination')
        outputs[row['filePath']] = payload
        content[cid] = 'sha256:' + row['contentDigest']
        guards.append({'path': row['sourcePath'], **{key: state[key] for key in GUARD_KEYS}})
    if set(content) != set(proposed) - existing:
        raise StructureError('OWNERSHIP_UNAUTHORIZED_MUTATION', 'manuscript authority must exactly cover added chapters')
    # A repeated capture must still describe the original objects, not bless a
    # same-content replacement made since the author saw the preview.
    by_path = {}
    for guard in guards:
        if guard['path'] in by_path and by_path[guard['path']] != guard:
            raise StructureError('MANUSCRIPT_INPUT_CHANGED', 'inconsistent source observations')
        by_path[guard['path']] = guard
    guards = [by_path[key] for key in sorted(by_path)]
    for key, actual in (('manuscriptGuards', guards), ('manuscriptGuardGroups', groups)):
        if key in plan and plan[key] != actual:
            raise StructureError('MANUSCRIPT_INPUT_CHANGED', 'manuscript inputs changed after preview', {'part': key})
    validate_guards(root, guards, stage='manuscript-inputs')
    validate_guard_groups(groups, stage='manuscript-inputs')
    return Materialization(content, outputs, guards, groups)


def validate_materialization_outputs(root: Path, current: dict, plan: dict,
                                     outputs: dict, material: Materialization) -> None:
    """Reconstruct every allowed output; a rehashed plan cannot add a prose write."""
    from .source_impact import review_outputs, source_impact
    proposed = plan['proposedStructure']
    impact = source_impact(current, proposed, root=root)
    expected = {'.govel/structure.json': json_bytes(proposed),
                **review_outputs(root, current, proposed, impact), **material.outputs}
    if outputs != expected:
        raise StructureError('MANUSCRIPT_WRITE_SET_INVALID', 'staged writes differ from the authorized manuscript operation')
    if set(outputs) & {row['path'] for row in material.guards}:
        raise StructureError('MANUSCRIPT_GUARD_IS_OUTPUT', 'original and frozen source manuscripts are read-only')


def commit_manuscript_plan(root: Path, *, operationId: str, planHash: str, approve: bool) -> dict:
    """Resume the same approved plan without ever repeating a committed intake."""
    from .locks import project_lock
    from .structure_commands import _operation_paths
    from .structure_transaction import _commit_locked
    from .source_reports import public_report
    if approve is not True:
        raise StructureError('APPROVAL_REQUIRED', 'manuscript commit requires explicit author approval')
    root = root.expanduser().resolve()
    with project_lock(root):
        plan_path, journal_path, _ = _operation_paths(root, operationId)
        path = journal_path if journal_path.is_file() else plan_path
        if not path.is_file():
            raise StructureError('PLAN_NOT_FOUND', 'unknown manuscript operation')
        record = read_json(path)
        if record.get('operation') not in OPERATIONS or record.get('operationId') != operationId:
            raise StructureError('OPERATION_ROUTE_REQUIRED', 'operation is not a manuscript intake or chapter reshape')
        if record.get('planHash') != planHash:
            raise StructureError('PLAN_HASH_MISMATCH', 'approved manuscript plan hash differs')
        if path == journal_path:
            from .operation_receipts import require_committed_operation
            record = require_committed_operation(root, operationId, plan_hash=planHash)
            from .source_requests import operation_undone
            state = 'undone' if operation_undone(root, operationId) else 'committed'
            return public_report({**record, 'ok': True, 'code': 'MANUSCRIPT_ALREADY_COMMITTED',
                'alreadyCommitted': True, 'replayed': True, 'historical': True,
                'operationState': state}, root=root)
        return _commit_locked(root, operation_id=operationId, plan_hash=planHash, approved=True, actor='author')
