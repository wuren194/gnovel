"""Top-level project folder relocation.

This filesystem operation changes where the project lives. Its shared CLI/MCP
OperationSpec receives the current root and the proposed destination, and the
domain handler keeps the moving lock and locator receipt together.

A relocation is a locator change. `projectId` is untouched, authored bytes are
untouched, and the receipt records both `rootAtOperation` (where the project was
when the operation ran) and `currentLocator` (where it is now), so history is never
rewritten by a later move.

The commit is one serialized step, not a check-then-act pair. `preview` stages a
durable plan the author approves (`operationId`, `planHash`, TTL — the same plan
contract every other mutation binds to); `commit` re-validates *while holding the
project lock*, renames the folder, verifies the bytes arrived, and lands the
locator receipt through the shared write-set engine. The lock file lives inside the
tree, so it travels with the rename and release follows it: the project is never
left unlocked mid-move; the stable lock anchor moves with the project. If
anything after the rename fails the folder is renamed back; if that itself fails the
error says the rollback is incomplete instead of pretending nothing happened.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from .jsonio import read_json
from .structure_store import StructureError
from .write_sets import apply_write_set, json_bytes

RECEIPT_PATH = '.govel/project-locator.json'
PLAN_TTL_SECONDS = 1800
PLAN_DIRECTORY = '.govel/plans'

# Bookkeeping that is not authored byte and must not participate in the before/after
# inventory comparison. `plans` is here because the relocation stages its own
# approved plan inside the tree it describes: hashing that file would make every
# preview expire itself the moment it was written.
_INVENTORY_EXCLUDES = {'.govel/transactions', '.govel/structure.lock', PLAN_DIRECTORY}


class ProjectRelocateError(StructureError):
    pass


def _hash_tree(root: Path) -> tuple[str, int]:
    """Deterministic fingerprint of every authored byte under the project."""
    import hashlib
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative == excluded or relative.startswith(excluded + '/') for excluded in _INVENTORY_EXCLUDES):
            continue
        if '__pycache__' in relative.split('/'):
            continue
        rows.append(f'{relative}\0{hashlib.sha256(path.read_bytes()).hexdigest()}')
    return hashlib.sha256('\n'.join(rows).encode('utf-8')).hexdigest(), len(rows)


def _validate(root: Path, target: str) -> dict:
    """Decide whether this move may happen at all; raise with the refusal reason."""
    if not isinstance(target, str) or not target.strip():
        raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_INVALID', 'target must be a nonempty path')
    raw = Path(target).expanduser()
    if not raw.is_absolute():
        raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_INVALID', 'target must be an absolute path',
                                   {'target': target})
    resolved = raw.resolve()
    source = root.expanduser().resolve()
    if resolved == source:
        raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_INVALID', 'target is the current project folder')
    if source in resolved.parents:
        raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_INVALID', 'target is inside the project',
                                   {'target': str(resolved)})
    if resolved in source.parents:
        raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_INVALID', 'target contains the project',
                                   {'target': str(resolved)})
    if resolved.exists():
        raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_EXISTS', 'target already exists',
                                   {'target': str(resolved)})
    parent = resolved.parent
    if not parent.is_dir():
        raise ProjectRelocateError('PROJECT_RELOCATE_PARENT_MISSING', 'target parent directory does not exist',
                                   {'target': str(parent)})
    try:
        same_device = os.stat(source).st_dev == os.stat(parent).st_dev
    except OSError as exc:
        raise ProjectRelocateError('PROJECT_RELOCATE_UNREADABLE', 'cannot inspect project folder',
                                   {'error': str(exc)}) from exc
    if not same_device:
        # Fail closed rather than silently degrading to copy + delete: a partial
        # cross-device copy is exactly the half-applied state this operation avoids.
        raise ProjectRelocateError('PROJECT_RELOCATE_CROSS_DEVICE_UNSUPPORTED',
                                   'cross-device relocation is not supported in this version; '
                                   'copy, verify hashes, swap atomically, then delete the source',
                                   {'source': str(source), 'target': str(resolved)})
    marker = source / '.govel' / 'project.json'
    if not marker.is_file():
        raise ProjectRelocateError('PROJECT_RELOCATE_NOT_A_PROJECT', 'source is not a govel project',
                                   {'source': str(source)})
    # No `structure.lock` check: the caller holds that lock across the whole move, so
    # a lock file here means "we are mid-relocation", not "someone else is inside".
    transactions = source / '.govel' / 'transactions'
    if transactions.is_dir() and any(transactions.glob('*.json')):
        raise ProjectRelocateError('TRANSACTION_INCOMPLETE',
                                   'recover the pending transaction before relocating the project')
    return {'sourceRoot': str(source), 'targetRoot': str(resolved)}


def _payload(root: Path, resolved: dict, actor: str) -> dict:
    """The one payload a relocation preview and its commit both hash.

    Ephemeral fields (operationId, timestamps) stay out of it: a plan the author
    approved must describe the same move seconds later, or the commit must refuse.
    """
    inventory_hash, file_count = _hash_tree(root)
    marker = read_json(root / '.govel' / 'project.json')
    return {
        'schemaVersion': 1, 'operation': 'project_relocate', 'actor': actor,
        'parameters': {'sourceRoot': resolved['sourceRoot'], 'targetRoot': resolved['targetRoot']},
        'impact': {'projectId': marker.get('projectId'),
                   'rootAtOperation': resolved['sourceRoot'],
                   'currentLocator': resolved['targetRoot'],
                   'authoredInventoryHash': inventory_hash, 'authoredFileCount': file_count,
                   'authoredBytesUnchanged': True, 'stableIdsUnchanged': True,
                   'crossDevice': False,
                   'refactorNote': '只改文件系统定位符；projectId、稳定 ID 与全部 authored 字节不变。'},
    }


def _plan_relative(operation_id: str) -> str:
    return f'{PLAN_DIRECTORY}/{operation_id}.json'


def preview_relocate_project(root: Path, *, target: str, actor: str = 'human_ui') -> dict:
    """Validate a relocation and stage the plan its commit is bound to.

    The approval is durable, not a value the caller has to keep safe somewhere: the
    commit takes an `operationId` and refuses an expired, substituted or already
    consumed one, so what executes is what the author was shown.
    """
    from .locks import project_lock
    from .structure_commands import _plan_hash
    root = root.expanduser().resolve()
    resolved = _validate(root, target)
    payload = _payload(root, resolved, actor)
    operation_id = f'OP-{uuid.uuid4().hex[:16].upper()}'
    plan = {**payload, 'operationId': operation_id, 'payloadHash': _plan_hash(payload),
            'createdAtEpoch': int(time.time()), 'expiresAtEpoch': int(time.time()) + PLAN_TTL_SECONDS,
            'requiresApproval': True, 'planHash': ''}
    plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
    with project_lock(root):
        apply_write_set(root, {_plan_relative(operation_id): json_bytes(plan)},
                        operation='project_relocate_preview')
    return plan


def commit_relocate_project(root: Path, *, operation_id: str, plan_hash: str, approved: bool,
                            actor: str = 'human_ui') -> dict:
    """Move the project folder and land its receipt as one locked, compensated step.

    The lock is held from the final validation through the rename, the byte check
    and the receipt write, so no ordinary structure mutation can start in the window
    the old check-then-rename left open. A failure after the rename renames the
    folder back; a failure of *that* raises rather than being swallowed, because a
    project stranded in a folder nobody approved is the state this exists to prevent.
    """
    from .locks import relocation_lock
    from .structure_commands import _plan_hash
    if not approved:
        raise ProjectRelocateError('APPROVAL_REQUIRED', 'relocation requires explicit approval')
    root = root.expanduser().resolve()
    plan = _read_plan(root, operation_id, plan_hash)
    if plan.get('operation') != 'project_relocate':
        raise ProjectRelocateError('OPERATION_KIND_MISMATCH', 'plan is not a project relocation')
    with relocation_lock(root) as relocated:
        moved = _validate(root, plan['parameters']['targetRoot'])
        if _plan_hash(_payload(root, moved, plan['actor'])) != plan['payloadHash']:
            raise ProjectRelocateError('PLAN_HASH_MISMATCH',
                                       'preview no longer matches the project; request a fresh preview')
        impact = plan['impact']
        inventory_hash = impact['authoredInventoryHash']
        file_count = impact['authoredFileCount']
        project_id = impact['projectId']
        target_root = Path(moved['targetRoot'])
        os.rename(moved['sourceRoot'], moved['targetRoot'])
        # The lock file sits inside the tree, so from here it lives at the new root
        # and release has to follow it there.
        relocated(target_root)
        try:
            if _hash_tree(target_root) != (inventory_hash, file_count):
                raise ProjectRelocateError('PROJECT_RELOCATE_VERIFY_FAILED',
                                           'authored bytes changed during relocation')
            receipt = locator_receipt_bytes(
                target_root, root_at_operation=moved['sourceRoot'], current_locator=moved['targetRoot'],
                project_id=project_id, actor=actor, operation_id=operation_id, relocated_at=_now(),
                plan_hash=plan_hash, inventory_hash=inventory_hash, file_count=file_count)
            apply_write_set(target_root, {RECEIPT_PATH: receipt, _plan_relative(operation_id): None},
                            operation='project_relocate')
        except BaseException as failure:
            try:
                os.rename(moved['targetRoot'], moved['sourceRoot'])
            except OSError as compensate:
                raise OSError(
                    f'项目文件夹已改名为 {moved["targetRoot"]}，但定位符收据未落盘，且改名回 '
                    f'{moved["sourceRoot"]} 也失败：请手工移回（原始失败：{failure}）') from compensate
            relocated(Path(moved['sourceRoot']))
            raise
    return {'ok': True, 'code': 'PROJECT_RELOCATED', 'operationId': operation_id, 'projectId': project_id,
            'planHash': plan_hash, 'rootAtOperation': moved['sourceRoot'],
            'currentLocator': moved['targetRoot'], 'receiptPath': RECEIPT_PATH,
            'authoredInventoryHash': inventory_hash, 'authoredFileCount': file_count,
            'stableIdsUnchanged': True, 'authoredBytesUnchanged': True}


def _read_plan(root: Path, operation_id: str, plan_hash: str) -> dict:
    """Load the approved relocation plan, refusing a stale or substituted one."""
    from .structure_commands import _operation_paths, _plan_hash
    path, _, _ = _operation_paths(root, operation_id)
    if not path.is_file():
        raise ProjectRelocateError('PLAN_NOT_FOUND', 'unknown or consumed plan')
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise ProjectRelocateError('PLAN_INVALID', 'plan must be a JSON object')
    if plan.get('operationId') != operation_id:
        raise ProjectRelocateError('OPERATION_ID_MISMATCH', 'plan operationId does not match its filename')
    expected = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
    if plan.get('planHash') != expected or plan_hash != expected:
        raise ProjectRelocateError('PLAN_HASH_MISMATCH', 'plan changed after preview')
    if int(time.time()) > plan.get('expiresAtEpoch', 0):
        raise ProjectRelocateError('PLAN_EXPIRED', 'request a fresh preview')
    return plan


def compute_locator_receipt(existing: dict | None, *, project_id: str, root_at_operation: str,
                            current_locator: str, actor: str, operation_id: str, relocated_at: str,
                            plan_hash: str | None = None, inventory_hash: str | None = None,
                            file_count: int | None = None) -> dict:
    """The receipt this operation must write, given what is on disk now.

    Pure: nothing is read or written, so the exact bytes can be staged into a
    preview's write set and landed by the same durable transaction that renames the
    book — which is what makes the folder move and the metadata change one
    operation. Earlier records are never rewritten: each history entry keeps its own
    `rootAtOperation`, because a later move must not retroactively change where an
    earlier one happened.

    `plan_hash` is bound only where the receipt is the sole approval record — a
    standalone relocation. A relocation combined with a book rename has its plan and
    journal in the project already, and its staged receipt cannot contain a plan hash
    that covers those very bytes.
    """
    history = []
    if isinstance(existing, dict) and isinstance(existing.get('history'), list):
        history = existing['history']
    receipt = {
        'schemaVersion': 1, 'kind': 'project_locator', 'projectId': project_id,
        'rootAtOperation': root_at_operation, 'currentLocator': current_locator,
        'relocatedAt': relocated_at, 'actor': actor, 'operationId': operation_id,
        'authoredInventoryHash': inventory_hash, 'authoredFileCount': file_count,
        'reversible': True,
        'history': [*history, {'rootAtOperation': root_at_operation, 'currentLocator': current_locator,
                               'relocatedAt': relocated_at, 'actor': actor, 'operationId': operation_id,
                               'authoredInventoryHash': inventory_hash}],
    }
    if plan_hash is not None:
        receipt['planHash'] = plan_hash
    return receipt


def locator_receipt_bytes(root: Path, *, root_at_operation: str, current_locator: str, project_id: str,
                          actor: str, operation_id: str, relocated_at: str, plan_hash: str | None = None,
                          inventory_hash: str | None = None, file_count: int | None = None) -> bytes:
    """Receipt bytes for the project whose folder currently sits at `root`."""
    path = root / RECEIPT_PATH
    existing = read_json(path) if path.is_file() else None
    return json_bytes(compute_locator_receipt(
        existing, project_id=project_id, root_at_operation=root_at_operation,
        current_locator=current_locator, actor=actor, operation_id=operation_id,
        relocated_at=relocated_at, plan_hash=plan_hash, inventory_hash=inventory_hash,
        file_count=file_count))


def _now() -> str:
    from .clock import utc_now
    return utc_now()
