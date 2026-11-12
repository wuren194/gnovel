"""Identity-safe authoring refactors.

Stable identity is not display metadata, not an ordinal, not a file name, not a
file path and not a project folder. This module is the one place that turns an
author's decision to change any of those into a preview-first transaction on top
of the shared write-set engine — it does not re-implement locking, before-images,
rollback or receipts.

Every refactor here preserves identity by construction: the plan carries a whole
proposed structure snapshot in which only display fields and locators differ, and
`validate_structure` plus the ID-pattern guards reject any plan that would mint,
reuse or drop a stable ID. Chapter split and merge use their own manuscript
materialization transaction; project fork remains deferred.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

from . import refactor_paths
from .clock import utc_now
from .generated import is_generated
from .project_relocate import RECEIPT_PATH, locator_receipt_bytes
from .refactor_paths import RefactorPathError
from .structure_store import StructureError, load_structure, validate_structure
from .write_sets import digest, json_bytes

# Operations that keep identity. The value is the contract this module enforces.
OPERATIONS = {
    'rename': 'Stable ID unchanged; only a display field changes.',
    'relocate': 'Stable ID unchanged; only a locator changes and bytes move with it.',
    'reorder': 'Stable ID unchanged; only displayOrdinal/displayCode change.',
    'reparent': 'Stable ID unchanged; only placement and membership change.',
    'alias': 'Stable ID unchanged; only the alias list changes.',
    'archive': 'Stable ID unchanged; only status changes.',
    'restore': 'Stable ID unchanged; the archived record keeps its ID.',
}

# Reserved for a later round: each of these creates identity or lineage, so it
# needs its own contract rather than a display-field edit.
DEFERRED_OPERATIONS = {
    'fork': 'new projectId plus parent project lineage.',
}

IDENTITY_CREATING_OPERATIONS = {
    'split': {'operation': 'chapter.split-preview', 'rule': 'Archive the source chapter; create new IDs with exact source ranges.'},
    'merge': {'operation': 'chapter.merge-preview', 'rule': 'Archive source chapters; create a new ID and preserve their original prose.'},
}

# Display metadata a metadata-only migration rebase may re-bind. Anything outside
# this set makes the rebase unsafe.
MIGRATION_DISPLAY_FIELDS = {
    'book': ('bookName',),
    'volume': ('title',),
    'arc': ('title',),
    'unit': ('title',),
    'chapter': ('title',),
}

FILE_NAME_STRATEGIES = ('display_only', 'sync_safe_paths')

_COLLECTIONS = {
    'volume': ('volumes', 'volumeId'),
    'arc': ('arcs', 'arcId'),
    'unit': ('units', 'unitId'),
    'chapter': ('chapters', 'chapterId'),
}


def operation_contract() -> dict:
    """The published identity contract, for docs, CLI help and tests."""
    return {'supported': dict(OPERATIONS), 'deferred': dict(DEFERRED_OPERATIONS),
            'identityCreating': deepcopy(IDENTITY_CREATING_OPERATIONS),
            'identityInvariants': [
                'projectId', 'volumeId', 'arcId', 'unitId', 'chapterId', 'entityId',
                'relationId', 'candidateId', 'decisionId'],
            'fileNameStrategies': list(FILE_NAME_STRATEGIES),
            'migrationDisplayFields': {kind: list(fields) for kind, fields in MIGRATION_DISPLAY_FIELDS.items()}}


# ----------------------------------------------------------------------------
# plan construction
# ---------------------------------------------------------------------------

def _marker_bytes(root: Path) -> bytes | None:
    path = root / '.govel' / 'project.json'
    return path.read_bytes() if path.is_file() else None


def _change(object_type: str, object_id: str, field: str, before, after, **extra) -> dict:
    row = {'objectType': object_type, 'objectId': object_id, 'field': field, 'before': before, 'after': after}
    row.update(extra)
    return row


def _find(structure: dict, kind: str, item_id: str) -> dict:
    collection, field = _COLLECTIONS[kind]
    item = next((row for row in structure.get(collection, []) if row.get(field) == item_id), None)
    if item is None:
        raise StructureError(f'{kind.upper()}_NOT_FOUND', f'unknown {kind}: {item_id}')
    return item


def _nonempty(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructureError('FIELD_INVALID', f'{field} must be a nonempty string')
    return value.strip()


def _refactor_plan(root: Path, structure: dict, *, operation: str, actor: str, parameters: dict,
                   changes: list[dict], proposed: dict, impact: dict, path_moves: list[dict] | None = None,
                   binding_transitions: list[dict] | None = None, operation_id: str | None = None,
                   request_id: str | None = None, request_fingerprint: str | None = None,
                   external_review_materials: dict | None = None, external_review_guards: list | None = None) -> dict:
    from .structure_commands import _base_plan, _write_plan
    marker = _marker_bytes(root)
    plan = _base_plan(structure, operation=operation, actor=actor, parameters=parameters, impact=impact, proposed=proposed)
    if operation_id is not None:
        plan['operationId'] = operation_id
        plan['requestId'] = request_id
        plan['requestFingerprint'] = request_fingerprint
    if external_review_materials is not None:
        plan['externalReviewMaterials'] = external_review_materials
        plan['externalReviewGuards'] = external_review_guards
    plan['schemaVersion'] = 2
    plan['baseStructureHash'] = plan['baseHash']
    plan['baseProjectMarkerHash'] = digest(marker) if marker is not None else None
    plan['changes'] = changes
    plan['pathMoves'] = path_moves or []
    if binding_transitions:
        # 受保护字段（filePath/proseBinding）的变更不是 display metadata：它们以显式
        # binding transition 声明随计划一起被批准，并在 preview 与 commit 两端各自用
        # 当前结构重推（structure_transaction._enforce_transaction_boundary）。计划是
        # hash 绑定的，所以作者批准的那一份 declaration 就是被重推的那一份。
        plan['bindingTransitions'] = binding_transitions
    plan['requiresApproval'] = True
    from .locks import current_thread_owns_lock
    if current_thread_owns_lock(root):
        from .structure_commands import _write_plan_locked
        return _write_plan_locked(root, plan)
    return _write_plan(root, plan)


def _display_impact(structure: dict, *, kind: str, item_id: str, operation: str, note: str) -> dict:
    """Conservative, accurate invalidation scope for a display-only refactor.

    A rename refreshes generated views and anything that renders the name. It does
    not touch canon facts, prose bytes or unrelated author decisions, so the
    affected set is the renamed record's own chapters and nothing else.
    """
    affected: set[str] = set()
    if kind == 'chapter':
        affected.add(item_id)
    elif kind == 'volume':
        from .source_placement import chapter_volume_id
        version = structure.get('schemaVersion')
        affected.update(row['chapterId'] for row in structure.get('chapters', [])
                        if chapter_volume_id(row, version) == item_id)
    elif kind == 'arc':
        affected.update(row['chapterId'] for row in structure.get('chapterArcLinks', []) if row.get('arcId') == item_id)
    elif kind == 'unit':
        for unit in structure.get('units', []):
            if unit.get('unitId') == item_id:
                affected.update(m['memberId'] for m in unit.get('members', [])
                                if m.get('memberType') == 'chapter')
    elif kind == 'entity':
        chapter_ids = {row['chapterId'] for row in structure.get('chapters', [])}
        affected.update(ref['sourceId'] for ref in structure.get('references', [])
                        if ref.get('targetId') == item_id and ref.get('sourceId') in chapter_ids)
    chapters = sorted(affected)
    return {f'{kind}Id': item_id, 'operation': operation, 'refactorNote': note,
            'staleCandidates': chapters, 'approvalInvalidations': [],
            'manualReview': '显示名与定位符变化只刷新生成视图；正文字节、Canon 事实与无关作者决定不受影响。'}


# --------------------------------------------------------------------------
# transaction hooks (called from structure_transaction)
# --------------------------------------------------------------------------

def prepare_refactor_writes(root: Path, current: dict, plan: dict, extras: dict[str, bytes | None]) -> None:
    """Annotate impact and stage locator writes for an approved refactor plan.

    The owning impact computation may replace `plan['impact']` with its own
    projection; the refactor declarations are re-attached from the plan parameters,
    which are the real record of what the author approved.
    """
    impact = plan.get('impact')
    if not isinstance(impact, dict):
        return
    parameters = plan.get('parameters') or {}
    impact['refactor'] = {
        'operation': plan.get('operation'),
        'changes': plan.get('changes', []),
        'pathMoves': plan.get('pathMoves', []),
        'fileNameStrategy': parameters.get('fileNameStrategy'),
        'identityInvariants': operation_contract()['identityInvariants'],
    }
    relocation = parameters.get('folderRelocation')
    if relocation:
        impact['projectFolderRelocation'] = relocation
        # The receipt is part of what the author approved, so it is staged here and
        # lands in the same write set as `structure.json`. The folder move is the one
        # thing outside that write set, and the engine's before-images then roll the
        # receipt back with everything else. A receipt written after the commit instead
        # of inside it would leave a renamed book in an unmoved folder whenever the
        # write fails. `actor` and `relocatedAt` are therefore the approved plan's, not
        # the executing call's — that is what makes them auditable.
        extras[RECEIPT_PATH] = locator_receipt_bytes(
            root, root_at_operation=relocation['sourceRoot'], current_locator=relocation['targetRoot'],
            project_id=current['projectId'], actor=plan['actor'], operation_id=plan['operationId'],
            relocated_at=utc_now())
    if plan.get('changes'):
        impact['displayMetadataChanges'] = [row for row in plan['changes']
                                            if not row.get('sourcePath')]
    for move in plan.get('pathMoves', []):
        row = refactor_paths.check_target(root, move['sourcePath'], move['targetPath'])
        if row['action'] == 'move':
            source = refactor_paths.resolve(root, move['sourcePath'])
            extras[move['targetPath']] = source.read_bytes()
            extras[move['sourcePath']] = None
        elif row['action'] == 'in_place':
            move['appliedAs'] = 'in_place_rename'
    if plan.get('operation') == 'accept_external_modification':
        # 接受外部修改不是定位符重构：正文字节没动，但依赖正文字节的审校工件与审批必须
        # 在**同一次写集**里作废（契约 §38/§95）。这条不走 source_impact——它是 filePath
        # 中立的，用它来跳过正文审校失效正是 §95 明确禁止的误用。
        from .finalization import accept_external_workflow, _load_workflow
        chapter_id = (plan.get('parameters') or {}).get('chapterId')
        extras['.govel/workflow.json'] = json_bytes(
            accept_external_workflow(_load_workflow(root), chapter_id,
                                     updated_at=plan['proposedStructure']['updatedAt']))
    if plan.get('operation') == 'refactor_rename_book':
        extras['.govel/project.json'] = _renamed_marker(root, plan['proposedStructure']['bookName'])


def _in_place_moves(plan: dict) -> list[dict]:
    return [move for move in plan.get('pathMoves', []) if move.get('appliedAs') == 'in_place_rename']


def apply_locator_renames(root: Path, plan: dict) -> list[dict]:
    """Relabel case-only / normalization-only locators; return what was relabelled.

    Source and target are one directory entry here, so the write set cannot carry the
    move — it would need two rows naming one file. That does not make the rename harmless:
    the directory entry's *spelling* changes, and `structure.json` names that
    spelling. The caller therefore reverts the returned rows if the write set fails,
    and this function reverts its own if a later rename in the same plan cannot be
    applied.
    """
    applied: list[dict] = []
    for move in _in_place_moves(plan):
        source = refactor_paths.resolve(root, move['sourcePath'])
        target = refactor_paths.resolve(root, move['targetPath'])
        if (not refactor_paths.same_file(source, target)
                or refactor_paths.separate_directory_entry(source, target)):
            # `appliedAs` was decided when the plan was previewed. Either the file has
            # since moved off that inode, or a second entry now carries it — a hard link,
            # whose rename would succeed while changing nothing and commit a locator the
            # disk does not hold.
            refusal = RefactorPathError('REFACTOR_CASE_RENAME_UNSAFE',
                                        'locator no longer resolves to one directory entry',
                                        {'path': move['targetPath']})
            revert_locator_renames(root, applied, failure=refusal)
            raise refusal
        try:
            os.rename(source, target)
        except BaseException as failure:
            revert_locator_renames(root, applied, failure=failure)
            raise
        applied.append(move)
    return applied


def revert_locator_renames(root: Path, applied: list[dict], *, failure: BaseException | None = None) -> None:
    """Move every applied in-place rename back, failing closed.

    `structure.json` still names the old locator, so a file left under its new
    spelling is a pointer to nothing — worse than the state before the transaction.
    A reverse rename that raises is therefore reported as `ROLLBACK_INCOMPLETE` with
    the original failure carried in the message, never swallowed. The write-set
    engine may already be raising its own `rollback incomplete`; both names appear,
    because an operator has to undo both.
    """
    stranded: dict[str, str] = {}
    for move in reversed(applied):
        source = refactor_paths.resolve(root, move['sourcePath'])
        target = refactor_paths.resolve(root, move['targetPath'])
        # Both spellings resolve to the same inode on the volume that made this an
        # in-place rename, so `same_file` cannot tell the two states apart; only the
        # directory entry can say whether the file is still addressable at all.
        try:
            os.rename(target, source)
        except OSError as reverse:
            stranded[move['sourcePath']] = f'反向改名失败：{reverse}'
            continue
        if refactor_paths.sibling_keys(source.parent).get(refactor_paths.canonical_key(source.name)) is None:
            stranded[move['sourcePath']] = '撤回后该目录下已无此文件'
    if stranded:
        detail = f'（原始失败：{failure}）' if failure is not None else ''
        reasons = '；'.join(f'{path}：{reason}' for path, reason in sorted(stranded.items()))
        raise StructureError('ROLLBACK_INCOMPLETE',
                             '原地改名未能撤回，structure 指向的定位符与磁盘不一致，需人工核对：'
                             + reasons + detail,
                             {'paths': sorted(stranded), 'reasons': dict(sorted(stranded.items()))})


def _renamed_marker(root: Path, book_name: str) -> bytes:
    from .jsonio import read_json
    marker = read_json(root / '.govel' / 'project.json')
    marker['bookName'] = book_name
    return json_bytes(marker)


# --------------------------------------------------------------------------
# book rename
# --------------------------------------------------------------------------

def _validate_folder_target(root: Path, target: str) -> dict:
    if not isinstance(target, str) or not target.strip():
        raise StructureError('REFACTOR_FOLDER_TARGET_INVALID', 'project folder target must be a nonempty path')
    raw = Path(target).expanduser()
    if not raw.is_absolute():
        raise StructureError('REFACTOR_FOLDER_TARGET_INVALID', 'project folder target must be absolute',
                             {'target': target})
    resolved = raw.resolve()
    source = root.expanduser().resolve()
    if resolved == source:
        raise StructureError('REFACTOR_FOLDER_TARGET_INVALID', 'project folder target is the current folder',
                             {'target': str(resolved)})
    if source == resolved.parent or source in resolved.parents:
        raise StructureError('REFACTOR_FOLDER_TARGET_INVALID', 'project folder target is inside the project',
                             {'target': str(resolved)})
    if resolved in source.parents:
        raise StructureError('REFACTOR_FOLDER_TARGET_INVALID', 'project folder target contains the project',
                             {'target': str(resolved)})
    if resolved.exists():
        raise StructureError('REFACTOR_FOLDER_TARGET_EXISTS', 'project folder target already exists',
                             {'target': str(resolved)})
    parent = resolved.parent
    if not parent.is_dir():
        raise StructureError('REFACTOR_FOLDER_PARENT_MISSING', 'target parent directory does not exist',
                             {'target': str(parent)})
    try:
        same_device = os.stat(source).st_dev == os.stat(parent).st_dev
    except OSError as exc:
        raise StructureError('REFACTOR_FOLDER_UNREADABLE', 'cannot inspect project folder', {'error': str(exc)}) from exc
    if not same_device:
        raise StructureError('PROJECT_RELOCATE_CROSS_DEVICE_UNSUPPORTED',
                             'cross-device project relocation is not supported in this version',
                             {'source': str(source), 'target': str(resolved)})
    return {'sourceRoot': str(source), 'targetRoot': str(resolved)}


def preview_rename_book(root: Path, *, new_name: str, project_folder_target: str | None = None,
                        actor: str = 'human_ui') -> dict:
    """Preview a book rename; optionally combined with a project folder relocation.

    The two are different semantics — one mutates display metadata, one mutates a
    filesystem locator — but the author sees both impacts in one preview and the
    commit either lands both or restores both.
    """
    root = root.expanduser().resolve()
    structure = load_structure(root)
    name = _nonempty(new_name, 'newName')
    if name == structure.get('bookName'):
        raise StructureError('REFACTOR_NAME_UNCHANGED', 'new book name is identical to the current one')
    relocation = _validate_folder_target(root, project_folder_target) if project_folder_target else None
    proposed = deepcopy(structure)
    proposed['bookName'] = name
    validate_structure(proposed)
    changes = [_change('project', structure['projectId'], 'bookName', structure.get('bookName'), name)]
    if relocation:
        changes.append(_change('project', structure['projectId'], 'projectFolder',
                               relocation['sourceRoot'], relocation['targetRoot']))
    parameters = {'newName': name, 'previousName': structure.get('bookName', ''),
                  'fileNameStrategy': 'display_only'}
    if relocation:
        parameters['folderRelocation'] = relocation
    impact = _display_impact(structure, kind='book', item_id=structure['projectId'],
                             operation='refactor_rename_book',
                             note='书名是显示元数据；projectId、stable ID 与正文字节不变。')
    impact['projectFolderRelocation'] = relocation
    return _refactor_plan(root, structure, operation='refactor_rename_book', actor=actor,
                          parameters=parameters, changes=changes, proposed=proposed, impact=impact)


def commit_refactor(root: Path, *, operation_id: str, plan_hash: str, approved: bool,
                    actor: str = 'human_ui') -> dict:
    """Commit a refactor plan, relocating the project folder inside the same step.

    Book rename, folder move and locator receipt are one logical transaction. The
    receipt is staged in the approved plan, so the write-set engine lands it together
    with `structure.json`, `project.json`, the derived views and the journal, and
    restores all of them if anything fails; only then is the folder rename
    compensated back. The project lock is held across both, and it travels with the
    folder rather than being deleted under an in-flight operation.

    The folder rename runs first because it is the cheap compensating action: every
    in-project byte has a recorded before-image, while a moved folder is the one thing
    the engine cannot restore on its own.
    """
    from .structure_commands import _operation_paths, _plan_hash
    from .structure_transaction import _commit_locked, commit as _commit_plan
    from .jsonio import read_json
    from .locks import relocation_lock
    root = root.expanduser().resolve()
    plan_path, _, _ = _operation_paths(root, operation_id)
    if not plan_path.is_file():
        return _commit_plan(root, operation_id=operation_id, plan_hash=plan_hash, approved=approved, actor=actor)
    plan = read_json(plan_path)
    if plan.get('operationId') != operation_id:
        raise StructureError('OPERATION_ID_MISMATCH', 'plan operationId does not match its filename')
    if str(plan.get('operation', '')).startswith('migration_'):
        # Migration plans bind evidence, not the live structure: their commit path
        # re-checks the reviewed inventory and writes only what the receipt covers.
        # Letting the generic transaction rewrite derived views would move bytes the
        # migration inventory hashes.
        raise StructureError('OPERATION_ROUTE_REQUIRED',
                             'migration plans commit through their own migration command',
                             {'operation': plan.get('operation'), 'operationId': operation_id})
    expected = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
    if plan.get('planHash') != expected or plan_hash != expected:
        raise StructureError('PLAN_HASH_MISMATCH', 'plan changed after preview')
    relocation = (plan.get('parameters') or {}).get('folderRelocation')
    if not relocation:
        return _commit_plan(root, operation_id=operation_id, plan_hash=plan_hash, approved=approved, actor=actor)

    source = Path(relocation['sourceRoot']).expanduser().resolve()
    target = Path(relocation['targetRoot']).expanduser().resolve()
    if not approved:
        # Refuse before moving anything: an unapproved commit must not touch the tree.
        raise StructureError('APPROVAL_REQUIRED', 'commit requires explicit approval')
    # 文件夹改名是有副作用的：高风险所有权流转的 authority 门槛必须先于任何副作用成立，
    # 不能靠 `_commit_locked` 事后拒绝再改名回来（契约 §10.1，Phase 2 返修）。
    from .prose_binding import require_privileged_approval
    require_privileged_approval(plan, operation_id=operation_id)
    with relocation_lock(source) as relocated:
        # Re-validate under the lock: the preview may be minutes old.
        _validate_folder_target(source, str(target))
        os.rename(source, target)
        # The lock file sits inside the tree, so it now lives at the target; the
        # transaction below runs under this same lock instead of taking a second one.
        relocated(target)
        try:
            return _commit_locked(target, operation_id=operation_id, plan_hash=plan_hash,
                                  approved=approved, actor=actor)
        except BaseException as failure:
            try:
                os.rename(target, source)
            except OSError as compensate:
                raise OSError(
                    f'项目文件夹已改名为 {target}，但入库事务失败且未能改名回去；'
                    f'请手工移回 {source}（原始失败：{failure}）') from compensate
            relocated(source)
            raise


# --------------------------------------------------------------------------
# display renames
# --------------------------------------------------------------------------

def _preview_display_rename(root: Path, *, kind: str, item_id: str, new_title: str, actor: str,
                            proposed: dict | None = None, changes: list[dict] | None = None,
                            path_moves: list[dict] | None = None,
                            extra_parameters: dict | None = None,
                            binding_transitions: list[dict] | None = None) -> dict:
    """Shared plan builder for a display rename.

    `proposed`, `changes` and `path_moves` may be supplied by a caller that already
    computed them (a chapter rename with synced file names); otherwise the rename
    is the title field alone.
    """
    root = root.expanduser().resolve()
    structure = load_structure(root)
    item = _find(structure, kind, item_id)
    title = _nonempty(new_title, 'newTitle')
    if title == str(item.get('title', '')).strip():
        raise StructureError('REFACTOR_NAME_UNCHANGED', f'new {kind} title is identical to the current one')
    if proposed is None:
        proposed = deepcopy(structure)
        _find(proposed, kind, item_id)['title'] = title
    validate_structure(proposed)
    record = changes if changes is not None else [
        _change(kind, item_id, 'title', item.get('title', ''), title)]
    parameters = {'newTitle': title, 'previousTitle': item.get('title', ''), 'fileNameStrategy': 'display_only'}
    parameters.update(extra_parameters or {})
    impact = _display_impact(structure, kind=kind, item_id=item_id, operation=f'refactor_rename_{kind}',
                             note=f'{kind} 标题是显示元数据；稳定 ID、归属与正文字节不变。')
    if path_moves:
        impact['pathMoves'] = path_moves
    return _refactor_plan(root, structure, operation=f'refactor_rename_{kind}', actor=actor,
                          parameters=parameters, changes=record, proposed=proposed, impact=impact,
                          path_moves=path_moves, binding_transitions=binding_transitions)


def preview_rename_volume(root: Path, *, volume_id: str, new_title: str, actor: str = 'human_ui') -> dict:
    return _preview_display_rename(root, kind='volume', item_id=volume_id, new_title=new_title, actor=actor)


def preview_rename_arc(root: Path, *, arc_id: str, new_title: str, actor: str = 'human_ui') -> dict:
    return _preview_display_rename(root, kind='arc', item_id=arc_id, new_title=new_title, actor=actor)


def preview_rename_unit(root: Path, *, unit_id: str, new_title: str, actor: str = 'human_ui') -> dict:
    return _preview_display_rename(root, kind='unit', item_id=unit_id, new_title=new_title, actor=actor)


def preview_rename_chapter(root: Path, *, chapter_id: str, new_title: str,
                           file_name_strategy: str = 'display_only', actor: str = 'human_ui') -> dict:
    """Rename a chapter title, optionally syncing its body and outline locators.

    `file_name_strategy` is the only thing that decides whether files move:
    `display_only` changes the title and nothing else, `sync_safe_paths` also
    proposes deterministic `正文/NNN-标题.md` / `规划/NNN-标题-推演.md` locators.
    The default never guesses: an author who wants files renamed asks for it.
    """
    if file_name_strategy not in FILE_NAME_STRATEGIES:
        raise StructureError('REFACTOR_FILE_NAME_STRATEGY_INVALID',
                             f'fileNameStrategy must be one of {", ".join(FILE_NAME_STRATEGIES)}')
    root = root.expanduser().resolve()
    structure = load_structure(root)
    chapter = _find(structure, 'chapter', chapter_id)
    title = _nonempty(new_title, 'newTitle')
    if title == str(chapter.get('title', '')).strip():
        raise StructureError('REFACTOR_NAME_UNCHANGED', 'new chapter title is identical to the current one')
    proposed = deepcopy(structure)
    target = _find(proposed, 'chapter', chapter_id)
    target['title'] = title
    changes = [_change('chapter', chapter_id, 'title', chapter.get('title', ''), title)]
    path_moves: list[dict] = []
    transitions: list[dict] = []
    if file_name_strategy == 'sync_safe_paths':
        # Phase 2：`filePath` 是受保护所有权字段，同步改名同样必须走 formal binding
        # transition，不能是 generic structure mutation（契约 §60）。正文字节的新旧
        # 位置与 title 由同一次提交落地。
        proposed, path_moves, transitions = _binding_sync_path_moves(
            root, structure, chapter, target, proposed, changes)
        validate_structure(proposed)
        return _preview_display_rename(root, kind='chapter', item_id=chapter_id, new_title=title,
                                       actor=actor, proposed=proposed, changes=changes,
                                       path_moves=path_moves,
                                       extra_parameters={'fileNameStrategy': file_name_strategy},
                                       binding_transitions=transitions)
    validate_structure(proposed)
    return _preview_display_rename(root, kind='chapter', item_id=chapter_id, new_title=title, actor=actor,
                                   proposed=proposed, changes=changes, path_moves=path_moves,
                                   extra_parameters={'fileNameStrategy': file_name_strategy})


def _binding_locator_moves(root: Path, structure: dict, chapter: dict, *,
                           file_path: str | None) -> tuple[dict, list[dict], list[dict], list[dict], dict]:
    """Preview a managed/unbound locator move on a current-binding schema（Phase 2）。

    Returns `(proposed, changes, path_moves, binding_transitions, observation)`. The only
    locator that is a *protected ownership field* is `filePath`; moving it therefore goes
    through an explicit binding transition rather than a generic structure mutation（契约 §60）。

    The move kind is decided by the observed current state, never by the caller:
    `managed` + OWNED_CLEAN moves the owned bytes (`move_managed`); `unbound` + UNBOUND
    repositions an EMPTY reservation (`move_unbound`). Any other observation — externally
    modified, locator drift, unresolved, unknown target, corrupt binding, conflict, pending
    transaction — is refused with its own deterministic code and is never "repaired" here
    （契约 §16/§17/§42/§49/§50/§51）。
    """
    from .prose_binding import (OWNED_CLEAN, UNBOUND, apply_locator_move_plan,
                                observe_chapter, observation_error_code)
    chapter_id = chapter['chapterId']
    observation = observe_chapter(root, structure, chapter)
    if observation['state'] not in (UNBOUND, OWNED_CLEAN):
        raise StructureError(observation_error_code(observation['state']),
                             f'locator move is not allowed while the chapter is {observation["state"]}',
                             {'chapterId': chapter_id, 'observedState': observation['state'],
                              'currentLocator': observation['currentLocator'],
                              'expectedContentDigest': observation['expectedContentDigest'],
                              'observedContentDigest': observation['observedContentDigest']})
    kind = 'move_managed' if observation['state'] == OWNED_CLEAN else 'move_unbound'
    proposed = deepcopy(structure)
    target = _find(proposed, 'chapter', chapter_id)
    changes: list[dict] = []
    path_moves: list[dict] = []
    transitions: list[dict] = []
    for field, requested in (('filePath', file_path),):
        if requested is None:
            continue
        current_value = str(chapter.get(field, ''))
        if not current_value:
            raise StructureError('REFACTOR_LOCATOR_ABSENT', f'chapter has no {field} to relocate',
                                 {'chapterId': chapter_id})
        after = requested.strip()
        if (field == 'filePath' and current_value != after
                and refactor_paths.canonical_key(current_value) == refactor_paths.canonical_key(after)):
            # 契约 §13/§54：纯大小写/规范化-only 改名在 R1 延期。Phase 2 的 refactor 切刀
            # 不重新启用它，也绝不静默走非持久化原地改名。
            raise StructureError(
                'REFACTOR_CASE_ONLY_RENAME_DEFERRED',
                'case-only and normalization-only locator renames are deferred in R1; '
                'use an intermediate distinct locator or wait for the R2 transaction engine upgrade',
                {'chapterId': chapter_id, 'filePath': current_value, 'requested': after})
        if field == 'filePath':
            # 代次匹配 + 目标槽未被占用 + 目标定位符未被其他章保留（含 unresolved/archived）。
            apply_locator_move_plan(root, structure, chapter, new_path=after, kind=kind)
            transitions.append({'kind': kind, 'chapterId': chapter_id, 'newFilePath': after,
                                'bindingGeneration': observation['bindingGeneration'],
                                'expectedContentDigest': observation['expectedContentDigest']})
        row = refactor_paths.check_target(root, current_value, after)
        if row['action'] == 'in_place':
            # 目录项拼写原地改名（同 inode 两种写法）：同一条 DEFERRED 裁决。
            raise StructureError(
                'REFACTOR_CASE_ONLY_RENAME_DEFERRED',
                'case-only and normalization-only locator renames are deferred in R1; '
                'use an intermediate distinct locator or wait for the R2 transaction engine upgrade',
                {'chapterId': chapter_id, 'field': field, 'source': current_value, 'requested': after})
        target[field] = after
        changes.append(_change('chapter', chapter_id, field, current_value, after,
                               sourcePath=current_value, targetPath=after,
                               sourceHash=row['sourceHash'], locatorAction=row['action']))
        path_moves.append({'chapterId': chapter_id, 'field': field, **row})
    if not changes:
        raise StructureError('REFACTOR_TARGET_REQUIRED', 'provide the chapter filePath')
    proposed['revision'] = structure['revision'] + 1
    if transitions:
        from .prose_binding import apply_binding_transitions, enforce_writer_boundary
        proposed, authority = apply_binding_transitions(structure, transitions, proposed=proposed)
        # Producer seals, boundary re-derives — the same exact-coverage check the commit
        # repeats on the freshly loaded current state（契约 §64）。
        enforce_writer_boundary(structure, proposed, authority=authority, root=root)
    return proposed, changes, path_moves, transitions, observation


def _binding_sync_path_moves(root: Path, structure: dict, chapter: dict, target: dict,
                             proposed: dict, changes: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Rename a current-binding chapter's title AND sync its locators, in one transition.

    Same authority and same preconditions as `refactor_relocate_chapter_paths`; the
    destination is derived from the new title instead of being supplied by the author
    （契约 §60）。Returns `(proposed, path_moves, binding_transitions)`.
    """
    from .prose_binding import (OWNED_CLEAN, UNBOUND, apply_binding_transitions,
                                apply_locator_move_plan, enforce_writer_boundary,
                                observe_chapter, observation_error_code)
    chapter_id = chapter['chapterId']
    observation = observe_chapter(root, structure, chapter)
    if observation['state'] not in (UNBOUND, OWNED_CLEAN):
        raise StructureError(observation_error_code(observation['state']),
                             f'locator-syncing rename is not allowed while the chapter is {observation["state"]}',
                             {'chapterId': chapter_id, 'observedState': observation['state'],
                              'currentLocator': observation['currentLocator']})
    kind = 'move_managed' if observation['state'] == OWNED_CLEAN else 'move_unbound'
    destinations = {'filePath': refactor_paths.synced_body_path(root, target)}
    path_moves: list[dict] = []
    transitions: list[dict] = []
    for field, after in destinations.items():
        before = str(chapter.get(field, ''))
        if not before or after == before:
            continue
        if field == 'filePath':
            if refactor_paths.canonical_key(before) == refactor_paths.canonical_key(after):
                raise StructureError(
                    'REFACTOR_CASE_ONLY_RENAME_DEFERRED',
                    'case-only and normalization-only locator renames are deferred in R1; '
                    'use an intermediate distinct locator or wait for the R2 transaction engine upgrade',
                    {'chapterId': chapter_id, 'filePath': before, 'requested': after})
            apply_locator_move_plan(root, structure, chapter, new_path=after, kind=kind)
        row = refactor_paths.check_target(root, before, after)
        if row['action'] == 'in_place':
            raise StructureError(
                'REFACTOR_CASE_ONLY_RENAME_DEFERRED',
                'case-only and normalization-only locator renames are deferred in R1; '
                'use an intermediate distinct locator or wait for the R2 transaction engine upgrade',
                {'chapterId': chapter_id, 'field': field, 'source': before, 'requested': after})
        target[field] = after
        changes.append(_change('chapter', chapter_id, field, before, after,
                               sourcePath=before, targetPath=after,
                               sourceHash=row['sourceHash'], locatorAction=row['action']))
        path_moves.append({'chapterId': chapter_id, 'field': field, **row})
        if field == 'filePath':
            transitions.append({'kind': kind, 'chapterId': chapter_id, 'newFilePath': after,
                                'bindingGeneration': observation['bindingGeneration'],
                                'expectedContentDigest': observation['expectedContentDigest']})
    body_row = next((row for row in path_moves if row['field'] == 'filePath'), None)
    if body_row is not None and not body_row['sourceExists'] and refactor_paths.check_body_required(chapter):
        raise RefactorPathError('REFACTOR_SOURCE_MISSING',
                                'stable chapter body file is missing; restore it before renaming its path',
                                {'chapterId': chapter_id, 'path': body_row['sourcePath']})
    if not transitions:
        return proposed, path_moves, transitions
    proposed['revision'] = structure['revision'] + 1
    proposed, authority = apply_binding_transitions(structure, transitions, proposed=proposed)
    enforce_writer_boundary(structure, proposed, authority=authority, root=root)
    return proposed, path_moves, transitions


def preview_relocate_chapter_paths(root: Path, *, chapter_id: str, file_path: str | None = None,
                                   actor: str = 'human_ui') -> dict:
    """Change only a chapter's locators. Bytes move in the same transaction."""
    if file_path is None:
        raise StructureError('REFACTOR_TARGET_REQUIRED', 'provide the chapter filePath')
    root = root.expanduser().resolve()
    structure = load_structure(root)
    # Phase 2：schema 4/5 的定位符变更走 formal binding transition。归属判定只看
    # current binding（proseBinding + filePath + 当前盘上字节），绝不 fallback 到
    # prose_ownership 的历史血缘。
    chapter = _find(structure, 'chapter', chapter_id)
    proposed, changes, path_moves, transitions, observation = _binding_locator_moves(
        root, structure, chapter, file_path=file_path)
    validate_structure(proposed)
    impact = _display_impact(structure, kind='chapter', item_id=chapter_id,
                             operation='refactor_relocate_chapter_paths',
                             note='只改定位符；正文字节与稳定 ID 不变，缓存与派生视图在同一事务内刷新。')
    # 契约 §94：纯定位符移动不制造创作语义影响。filePath 是所有权定位符，不是故事内容，
    # 因此这次移动本身既不产生 staleCandidates，也不作废任何审批。
    impact['staleCandidates'] = []
    impact['approvalInvalidations'] = []
    impact['pathMoves'] = path_moves
    # 契约 §62：预览必须把归属判定摊开给作者看——旧/新定位符、代次、期望摘要、**观测到的**
    # 盘上摘要与该章的观测状态，以及（若有）阻断原因。低能力调用方照抄这张 receipt，不自己推导。
    impact['ownership'] = observation
    return _refactor_plan(root, structure, operation='refactor_relocate_chapter_paths', actor=actor,
                          parameters={'chapterId': chapter_id, 'filePath': file_path,
                                      'fileNameStrategy': 'display_only'},
                          changes=changes, proposed=proposed, impact=impact, path_moves=path_moves,
                          binding_transitions=transitions)


# --------------------------------------------------------------------------
# entity rename / alias
# --------------------------------------------------------------------------

def preview_rename_entity(root: Path, *, selector: str, new_name: str, keep_old_name_as_alias: bool,
                          actor: str = 'human_ui') -> dict:
    """Rename an entity. `keep_old_name_as_alias` has no default on purpose.

    The framework never guesses whether the old name should survive as an alias:
    that is an authorial decision, and the preview shows the resulting alias list
    before anything is committed. Prose is never rewritten — use
    `refactor_reference_candidates` to see where the old name still appears.
    """
    from .entity_registry import resolve_entity
    root = root.expanduser().resolve()
    structure = load_structure(root)
    entity = resolve_entity(structure, selector)
    name = _nonempty(new_name, 'newName')
    old_name = str(entity.get('name', '')).strip()
    if name == old_name:
        raise StructureError('REFACTOR_NAME_UNCHANGED', 'new entity name is identical to the current one')
    aliases = [str(value).strip() for value in entity.get('aliases', []) if str(value).strip()]
    if keep_old_name_as_alias:
        aliases = list(dict.fromkeys([*aliases, old_name]))
    aliases = [value for value in dict.fromkeys(aliases) if value != name]
    changes = [_change('entity', entity['entityId'], 'name', old_name, name,
                       keepOldNameAsAlias=keep_old_name_as_alias)]
    if aliases != list(entity.get('aliases', [])):
        changes.append(_change('entity', entity['entityId'], 'aliases', list(entity.get('aliases', [])), aliases))
    proposed = deepcopy(structure)
    from .entity_registry import replace_entity_fields
    replace_entity_fields(proposed, entity['entityId'], {'name': name, 'aliases': aliases})
    validate_structure(proposed)
    from .impact_analysis import entity_change_impact
    impact = entity_change_impact(structure, entity['entityId'], {'name': name, 'aliases': aliases})
    impact['refactor'] = {'operation': 'refactor_rename_entity', 'changes': changes,
                          'identityInvariants': operation_contract()['identityInvariants'],
                          'refactorNote': '实体改名不自动替换正文；旧称引用由作者逐处决定。'}
    return _refactor_plan(root, structure, operation='refactor_rename_entity', actor=actor,
                          parameters={'entityId': entity['entityId'], 'newName': name, 'previousName': old_name,
                                      'keepOldNameAsAlias': keep_old_name_as_alias, 'aliases': aliases},
                          changes=changes, proposed=proposed, impact=impact)


def preview_alias_entity(root: Path, *, selector: str, aliases: list[str], actor: str = 'human_ui') -> dict:
    """Replace an entity's alias list without touching its name."""
    from .entity_registry import resolve_entity
    root = root.expanduser().resolve()
    structure = load_structure(root)
    entity = resolve_entity(structure, selector)
    if not isinstance(aliases, list) or any(not isinstance(value, str) or not value.strip() for value in aliases):
        raise StructureError('FIELD_INVALID', 'aliases must be a list of nonempty strings')
    cleaned = list(dict.fromkeys(value.strip() for value in aliases))
    name = str(entity.get('name', '')).strip()
    if name in cleaned:
        raise StructureError('REFACTOR_ALIAS_DUPLICATES_NAME', 'an alias cannot repeat the current name')
    before = list(entity.get('aliases', []))
    if cleaned == before:
        raise StructureError('REFACTOR_ALIASES_UNCHANGED', 'alias list is identical to the current one')
    changes = [_change('entity', entity['entityId'], 'aliases', before, cleaned)]
    proposed = deepcopy(structure)
    from .entity_registry import replace_entity_fields
    replace_entity_fields(proposed, entity['entityId'], {'aliases': cleaned})
    validate_structure(proposed)
    from .impact_analysis import entity_change_impact
    impact = entity_change_impact(structure, entity['entityId'], {'aliases': cleaned})
    impact['refactor'] = {'operation': 'refactor_alias_entity', 'changes': changes,
                          'identityInvariants': operation_contract()['identityInvariants'],
                          'refactorNote': '别名只影响检索与渲染；实体身份与正文不变。'}
    return _refactor_plan(root, structure, operation='refactor_alias_entity', actor=actor,
                          parameters={'entityId': entity['entityId'], 'aliases': cleaned},
                          changes=changes, proposed=proposed, impact=impact)


# --------------------------------------------------------------------------
# reorder / reparent / archive / restore
# --------------------------------------------------------------------------

def preview_reorder_chapter(root: Path, *, chapter_id: str, before_chapter_id: str | None = None,
                            actor: str = 'human_ui') -> dict:
    """Reading order is not identity. Delegates to the existing chapter move."""
    from .structure_commands import preview_move_chapter
    root = root.expanduser().resolve()
    plan = preview_move_chapter(root, chapter_id=chapter_id, before_chapter_id=before_chapter_id, actor=actor)
    plan['refactor'] = {'operation': 'reorder', 'identityInvariants': operation_contract()['identityInvariants'],
                        'note': 'displayOrdinal/displayCode 改变；chapterId、正文与文件名不变。'}
    return plan


def preview_reparent_chapter(root: Path, *, chapter_id: str, primary_arc_id: str, volume_id: str | None = None,
                             placement: dict | None = None, secondary_arc_ids: list[str] | None = None,
                             actor: str = 'human_ui') -> dict:
    """Belonging is not identity. Delegates to the existing chapter relink."""
    from .catalog_commands import preview_relink_chapter
    root = root.expanduser().resolve()
    plan = preview_relink_chapter(root, chapter_id=chapter_id, primary_arc_id=primary_arc_id, volume_id=volume_id,
                                  placement=placement, secondary_arc_ids=secondary_arc_ids, actor=actor)
    plan['refactor'] = {'operation': 'reparent', 'identityInvariants': operation_contract()['identityInvariants'],
                        'note': 'placement 与弧线归属改变；chapterId 与正文不变，规划与上下文需重新评估。'}
    return plan


def preview_reparent_unit(root: Path, *, unit_id: str, placement: dict, actor: str = 'human_ui') -> dict:
    """Move a unit between volume scope and project scope; members are untouched."""
    from .source_model import require_current_source
    from .source_placement import placement_error, placement_volume_id
    root = root.expanduser().resolve()
    structure = load_structure(root)
    require_current_source(structure)
    reason = placement_error(placement)
    if reason is not None:
        raise StructureError('PLACEMENT_INVALID', f'unit placement is not a legal shape: {reason}')
    unit = _find(structure, 'unit', unit_id)
    before = unit.get('placement')
    if before == placement:
        raise StructureError('REFACTOR_PLACEMENT_UNCHANGED', 'unit placement is identical to the current one')
    volume_id = placement_volume_id(placement)
    proposed = deepcopy(structure)
    target = _find(proposed, 'unit', unit_id)
    target['placement'] = deepcopy(placement)
    if volume_id is not None:
        volumes = {row['volumeId'] for row in structure['volumes']}
        if volume_id not in volumes:
            raise StructureError('VOLUME_NOT_FOUND', f'unknown volume: {volume_id}')
        from .source_placement import chapter_volume_id
        for member in target.get('members', []):
            if member.get('memberType') != 'chapter':
                continue
            chapter = _find(proposed, 'chapter', member['memberId'])
            if chapter_volume_id(chapter, structure.get('schemaVersion')) != volume_id:
                raise StructureError('UNIT_MEMBER_VOLUME_MISMATCH',
                                     'a volume unit may only hold chapters of that volume',
                                     {'unitId': unit_id, 'chapterId': member['memberId']})
    validate_structure(proposed)
    changes = [_change('unit', unit_id, 'placement', before, deepcopy(placement))]
    impact = _display_impact(structure, kind='unit', item_id=unit_id, operation='refactor_reparent_unit',
                             note='归属不是身份；unitId、成员与正文不变。')
    return _refactor_plan(root, structure, operation='refactor_reparent_unit', actor=actor,
                          parameters={'unitId': unit_id, 'placement': deepcopy(placement)},
                          changes=changes, proposed=proposed, impact=impact)


def _archive_or_restore(root: Path, *, kind: str, item_id: str, target_status: str, actor: str,
                        selector_based: bool = False) -> dict:
    from .structure_store import derive_display_codes
    root = root.expanduser().resolve()
    structure = load_structure(root)
    if selector_based:
        from .entity_registry import resolve_entity
        item = resolve_entity(structure, item_id)
    else:
        item = _find(structure, kind, item_id)
    real_id = item.get('entityId', item.get(f'{kind}Id'))
    current = str(item.get('status', ''))
    if current == target_status:
        raise StructureError('REFACTOR_STATUS_UNCHANGED', f'{kind} is already {target_status}: {real_id}')
    proposed = deepcopy(structure)
    if selector_based:
        from .entity_registry import resolve_entity
        target = resolve_entity(proposed, real_id)
    else:
        target = _find(proposed, kind, real_id)
    target['status'] = target_status
    if kind == 'chapter':
        derive_display_codes(proposed)
    validate_structure(proposed)
    operation = 'refactor_restore' if target_status != 'archived' else 'refactor_archive'
    changes = [_change(kind, real_id, 'status', current, target_status)]
    note = ('归档只改状态：稳定 ID、原文件与历史引用全部保留，可随时 restore 回同一 ID。'
            if target_status == 'archived' else
            '恢复同一稳定 ID；不新建记录，不改正文。')
    impact = _display_impact(structure, kind=kind, item_id=real_id, operation=operation, note=note)
    impact['archivedStatuses'] = {'before': current, 'after': target_status}
    return _refactor_plan(root, structure, operation=f'{operation}_{kind}', actor=actor,
                          parameters={'entityId' if selector_based else f'{kind}Id': real_id,
                                      'status': target_status},
                          changes=changes, proposed=proposed, impact=impact)


def preview_archive_chapter(root: Path, *, chapter_id: str, actor: str = 'human_ui') -> dict:
    return _archive_or_restore(root, kind='chapter', item_id=chapter_id, target_status='archived', actor=actor)


def preview_restore_chapter(root: Path, *, chapter_id: str, status: str = 'planned',
                            actor: str = 'human_ui') -> dict:
    """Restore an archived chapter under its original stable ID.

    `status` is the lifecycle state it returns to; it defaults to `planned` because
    a restored chapter has no approved plan yet, and no other field is guessed.
    """
    from .structure_store import LIFECYCLE_STATUSES
    if status not in LIFECYCLE_STATUSES or status == 'archived':
        raise StructureError('REFACTOR_RESTORE_STATUS_INVALID',
                             f'restore status must be a non-archived lifecycle status: {sorted(LIFECYCLE_STATUSES - {"archived"})}')
    return _archive_or_restore(root, kind='chapter', item_id=chapter_id, target_status=status, actor=actor)


def preview_archive_entity(root: Path, *, selector: str, actor: str = 'human_ui') -> dict:
    return _archive_or_restore(root, kind='entity', item_id=selector, target_status='archived',
                               actor=actor, selector_based=True)


def preview_restore_entity(root: Path, *, selector: str, actor: str = 'human_ui') -> dict:
    return _archive_or_restore(root, kind='entity', item_id=selector, target_status='active',
                               actor=actor, selector_based=True)


# --------------------------------------------------------------------------
# read-only reference candidates
# --------------------------------------------------------------------------

_AUTHORED_TEXT_SUFFIXES = {'.md', '.txt'}
_MAX_EXCERPT = 80


def _authored_text_files(root: Path, relative: str) -> bool:
    """Only authored prose is scanned: generated views restate the current name."""
    path = refactor_paths.resolve(root, relative)
    if not path.is_file() or path.suffix not in _AUTHORED_TEXT_SUFFIXES:
        return False
    try:
        return not is_generated(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError):
        return False


def refactor_reference_candidates(root: Path, *, old_name: str, paths: list[str] | None = None,
                                  limit: int = 200) -> dict:
    """Report where an old display name still appears. Never modifies anything.

    A string hit is a candidate, not a fact. Whether the old name is a nickname, a
    historical form of address, or dialogue that must stay is the author's call;
    this function only makes the occurrences visible with enough context to decide.
    """
    root = root.expanduser().resolve()
    name = _nonempty(old_name, 'oldName')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise StructureError('FIELD_INVALID', 'limit must be a positive integer')
    structure = load_structure(root)
    owners = {str(row.get('filePath', '')): row['chapterId'] for row in structure.get('chapters', [])
              if row.get('filePath')}
    if paths is not None:
        if not isinstance(paths, list) or any(not isinstance(value, str) or not value.strip() for value in paths):
            raise StructureError('FIELD_INVALID', 'paths must be a list of nonempty project-relative strings')
        wanted = [value.strip() for value in paths]
    else:
        wanted = sorted(owners)
        for folder in ('设定', '规划'):
            directory = root / folder
            if not directory.is_dir():
                continue
            wanted.extend(f'{folder}/{entry.name}' for entry in sorted(directory.iterdir())
                          if entry.is_file() and entry.suffix in _AUTHORED_TEXT_SUFFIXES)
    rows: list[dict] = []
    scanned = 0
    for relative in wanted:
        if not refactor_paths.is_canonical(root, relative) or not _authored_text_files(root, relative):
            continue
        try:
            text = refactor_paths.resolve(root, relative).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            start = line.find(name)
            if start < 0:
                continue
            excerpt = line.strip()
            if len(excerpt) > _MAX_EXCERPT:
                half = _MAX_EXCERPT // 2
                excerpt = f'{excerpt[:half]}…{excerpt[-half:]}'
            rows.append({'oldName': name, 'path': relative, 'chapterId': owners.get(relative),
                         'line': number, 'column': start + 1, 'context': excerpt, 'decision': None})
            if len(rows) >= limit:
                return _candidates_result(name, rows, scanned, truncated=True)
    return _candidates_result(name, rows, scanned, truncated=False)


def _candidates_result(name: str, rows: list[dict], scanned: int, *, truncated: bool) -> dict:
    return {'ok': True, 'code': 'REFACTOR_REFERENCE_CANDIDATES', 'oldName': name,
            'candidates': rows, 'matchCount': len(rows), 'filesScanned': scanned, 'truncated': truncated,
            'semanticJudgmentMadeByTool': False,
            'note': '命中只是候选：保留旧称、改成新称、当作昵称或历史称谓，都由作者决定；脚本不改写正文。'}
