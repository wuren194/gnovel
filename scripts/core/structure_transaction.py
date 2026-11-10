"""Structure preview/commit/undo using the shared durable write-set engine."""
from __future__ import annotations

import json
import shutil
import time
from copy import deepcopy
from pathlib import Path

from .clock import utc_now
from .derived_views import derived_outputs, renderer_fingerprint, source_files
from .jsonio import read_json, write_bytes, write_json
from .locks import project_lock
from .write_sets import safe_project_path
from .structure_store import StructureError, load_structure, structure_hash, validate_structure
from .write_sets import apply_write_set, digest, inventory, json_bytes, file_state, validate_inventory
from .source_model import require_current_source
from .canon_guard import require_canon_proposal
from .undo_receipt import receipt_bytes, receipt_relative_path
from .undo_evidence import authority_bytes, authority_relative_path
from .undo_journal import (UNDO_OPERATION_NAME, undo_backup_relative_path, undo_journal_bytes,
                           undo_journal_relative_path)


def _materialization_capability(plan: dict) -> frozenset[str]:
    """The exact chapter-ID materialization capability a transaction plan carries.

    Only an explicit chapter-creation operation has one, and it covers exactly the
    single identity the plan declares（实施轮 §34/§35）：`operation` 名本身不是安全条件，
    `plan.parameters.chapterId` 才是被授权的那一个 chapter。非建章 operation（move、
    rename、metadata、source …）的能力集为空，任何 added chapter 一律拒绝。
    """
    from .manuscript_materialization import OPERATIONS as MANUSCRIPT_OPERATIONS
    if plan.get('operation') in MANUSCRIPT_OPERATIONS:
        return frozenset(row['chapterId'] for row in plan.get('manuscriptMaterialization', []))
    if plan.get('operation') not in {'insert_chapter', 'planning_candidate_materialize'}:
        return frozenset()
    parameters = plan.get('parameters')
    chapter_id = parameters.get('chapterId') if isinstance(parameters, dict) else None
    if not isinstance(chapter_id, str) or not chapter_id:
        return frozenset()
    return frozenset({chapter_id})


def _plan_binding_authority(plan: dict):
    """The formal binding-transition capability a plan declares, or None.

    Phase 2 plans that change a chapter's protected ownership fields (a locator move on a
    current-binding schema, an accepted external modification) carry an explicit
    `bindingTransitions` list. The plan is hash-bound, and the boundary re-derives every
    declared transition's canonical result from the freshly loaded current structure and
    the commit revision — declaring a kind is not authority, producing exactly that
    result is（契约 §64/§66）。
    """
    transitions = plan.get('bindingTransitions')
    if not transitions:
        return None
    from .prose_binding import binding_authority
    return binding_authority(transitions)


def _enforce_transaction_boundary(plan: dict, current: dict, proposed: dict, *, root: Path):
    """The one protected-field boundary both preview and commit run（实施轮 §15/§36）。

    The capability is re-derived from the plan at each side, so the two sides cannot
    diverge: preview 与 commit 检查的是同一条 exact authority（added chapter IDs 必须
    精确等于 plan 声明的 chapterId），也都不信对方「已经查过」。
    """
    from .prose_binding import enforce_writer_boundary
    from .manuscript_materialization import materialization_inputs
    manuscript = materialization_inputs(root, current, plan)
    if plan.get('operation') == 'planning_candidate_materialize':
        parameters = plan.get('parameters', {})
        before = next((r for r in current.get('planningCandidates', []) if r['candidateId'] == parameters.get('candidateId')), None)
        after = next((r for r in proposed.get('planningCandidates', []) if r['candidateId'] == parameters.get('candidateId')), None)
        if (before is None or before.get('status') != 'approved' or after is None
                or after.get('status') != 'materialized' or after.get('materializedChapterId') != parameters.get('chapterId')):
            raise StructureError('PLANNING_CANDIDATE_NOT_APPROVED', 'materialization must consume the selected approved candidate')
    enforce_writer_boundary(current, proposed, root=root,
                            authority=_plan_binding_authority(plan),
                            materialization_chapter_ids=_materialization_capability(plan),
                            materialization_content_digests=manuscript.content_digests if manuscript else None)
    from .source_identity import require_fresh_source_identities
    require_fresh_source_identities(root, current, proposed)
    from .author_decisions import validate_decision_transition
    validate_decision_transition(current, proposed, operation=plan['operation'],
                                 operation_id=plan['operationId'], parameters=plan.get('parameters', {}))
    return manuscript


def prepare_writes(root: Path, plan: dict, current: dict) -> dict:
    proposed = plan['proposedStructure']
    require_current_source(current)
    require_current_source(proposed)
    require_canon_proposal(current, proposed, plan)
    proposed['revision'] = current['revision'] + 1
    from .author_decisions import prepare_decision_transition
    prepare_decision_transition(current, proposed, operation=plan['operation'],
                                operation_id=plan['operationId'], parameters=plan.get('parameters', {}))
    # Option B 同样约束事务路径：write-set 直接落盘 structure.json，绕过了
    # save_structure，所以受保护字段边界必须在 preview 与 commit 两端各自执行。
    # 事务通道唯一授权的 binding 变更是新建章 materialization（密封 unbound），且只
    # 覆盖本计划自己申报的那一个 chapterId——不存在 broad「本事务可以建章」能力。
    manuscript = _enforce_transaction_boundary(plan, current, proposed, root=root)
    proposed['updatedAt'] = utc_now()
    proposed['lastOperation'] = {'operationId': plan['operationId'], 'operation': plan['operation'],
                                 'actor': plan['actor'], 'committedAt': proposed['updatedAt']}
    validate_structure(proposed)
    from .source_impact import source_impact, review_outputs
    from .source_diff import source_diff
    plan['impact'] = {**plan.get('impact', {}), **source_impact(current, proposed, root=root)}
    if 'diff' not in plan:
        plan['diff'] = source_diff(current, proposed)
    extras = review_outputs(root, current, proposed, plan['impact'])
    # 重构计划自己申报定位符移动与显示元数据写集；这里只做装配，不复制事务逻辑。
    from .refactor import prepare_refactor_writes
    prepare_refactor_writes(root, current, plan, extras)
    if manuscript:
        if set(extras) & set(manuscript.outputs):
            raise StructureError('WRITE_TARGET_CONFLICT', 'multiple manuscript output producers')
        extras.update(manuscript.outputs)
        plan['manuscriptGuards'] = manuscript.guards
        plan['manuscriptGuardGroups'] = manuscript.guard_groups
    from .import_organization import organization_outputs
    organization = organization_outputs(root, current, plan, extras)
    if organization is not None:
        extras, plan['organizationGuards'], plan['importOrganization'] = organization
    sources = source_files(root)
    outputs, owners = derived_outputs(root, proposed, extras=extras, current=current)
    plan['writeSet'] = inventory(root, outputs, owners=owners)
    plan['sourceFiles'] = sources
    plan['rendererFingerprint'] = renderer_fingerprint()
    from .source_maintenance_inputs import bind_maintenance_inputs
    bind_maintenance_inputs(root, plan)
    from .external_review_materials import validate_external_materials
    validate_external_materials(root, plan, stage='external-preview')
    if 'chapterFacts' in plan.get('parameters', {}):
        from .chapter_facts import validate_fact_plan_inputs
        plan['chapterFactGuards'] = validate_fact_plan_inputs(root, plan, current=current)
    for row in plan['writeSet']:
        if outputs[row['path']] is not None:
            path = safe_project_path(root, f".govel/plans/{plan['operationId']}.files/{row['path']}")
            write_bytes(path, outputs[row['path']])
    return plan


def _load_outputs(root: Path, plan: dict) -> dict:
    outputs = {}
    for row in plan['writeSet']:
        data = None
        if row['after']['exists']:
            path = safe_project_path(root, f".govel/plans/{plan['operationId']}.files/{row['path']}")
            data = path.read_bytes()
            if digest(data) != row['after']['sha256']:
                raise StructureError('PLAN_HASH_MISMATCH', 'staged content differs from approved write set')
        outputs[row['path']] = data
    return outputs


def _reject_unsafe_plan(root: Path, operation_id: str, plan_hash: str) -> None:
    """Refuse a substituted, expired or path-unsafe plan *before* the lock is taken.

    Acquiring the lock creates `.govel/structure.lock` inside the project, so in a
    tree someone swapped for a symlink that check would already have written through
    the link. The same validations run again under the lock; this pass exists to keep
    lock-free callers — and lock-free refusals — honest.
    """
    from .structure_commands import _operation_paths, _plan_hash, _backup_path
    plan_path, _, backup = _operation_paths(root, operation_id)
    if not plan_path.is_file():
        return
    initial = read_json(plan_path)
    if initial.get('operationId') != operation_id:
        raise StructureError('OPERATION_ID_MISMATCH', 'plan operationId does not match its filename')
    initial_hash = _plan_hash({k: v for k, v in initial.items() if k != 'planHash'})
    if initial.get('planHash') != initial_hash or plan_hash != initial_hash:
        raise StructureError('PLAN_HASH_MISMATCH', 'plan changed after preview')
    if int(time.time()) > initial.get('expiresAtEpoch', 0):
        raise StructureError('PLAN_EXPIRED', 'request a fresh preview')
    for row in initial.get('writeSet', []):
        safe_project_path(root, row['path'])
        safe_project_path(root, _backup_path(backup, row['path']).relative_to(root))


def commit(root: Path, *, operation_id: str, plan_hash: str, approved: bool, actor: str) -> dict:
    if not approved:
        raise StructureError('APPROVAL_REQUIRED', 'commit requires explicit approval')
    root = root.resolve()
    _reject_unsafe_plan(root, operation_id, plan_hash)
    with project_lock(root):
        return _commit_locked(root, operation_id=operation_id, plan_hash=plan_hash,
                              approved=approved, actor=actor)


def _commit_locked(root: Path, *, operation_id: str, plan_hash: str, approved: bool, actor: str,
                   privileged_approval=None) -> dict:
    """The transaction body; the caller already holds this project's lock.

    Split out because a caller that holds the lock has to run this without taking a
    second one: a book rename combined with a folder relocation renames the tree, and
    the lock file lives inside it, so the commit that follows the move is covered by
    the lock the relocating caller took — not by a fresh lock at a path the old
    operation had already deleted mid-flight.
    """
    from .structure_commands import _operation_paths, _plan_hash, _backup_path
    if not approved:
        raise StructureError('APPROVAL_REQUIRED', 'commit requires explicit approval')
    root = root.resolve()
    plan_path, journal_path, backup = _operation_paths(root, operation_id)
    if not plan_path.is_file():
        raise StructureError('PLAN_NOT_FOUND', 'unknown or consumed plan')
    plan = read_json(plan_path)
    if plan.get('operationId') != operation_id:
        raise StructureError('OPERATION_ID_MISMATCH', 'plan operationId does not match its filename')
    expected_hash = _plan_hash({k: v for k, v in plan.items() if k != 'planHash'})
    if plan.get('planHash') != expected_hash or plan_hash != expected_hash:
        raise StructureError('PLAN_HASH_MISMATCH', 'plan changed after preview')
    if int(time.time()) > plan['expiresAtEpoch']:
        raise StructureError('PLAN_EXPIRED', 'request a fresh preview')
    if 'writeSet' not in plan:
        raise StructureError('PLAN_REPREVIEW_REQUIRED', 'legacy preview does not bind derived writes')
    current = load_structure(root)
    if current['revision'] != plan['baseRevision'] or structure_hash(current) != plan['baseHash']:
        raise StructureError('REVISION_CONFLICT', 'project changed after preview')
    # 提交版本在 commit 端也是硬不变量，而不是传递结论：baseRevision 一致只证明计划是
    # 针对当前状态准备的，proposedStructure 还必须是恰好的 next revision（预备阶段
    # 从 current 推导，这里对伪造的 staged revision 显式 fail closed）。
    from .prose_binding import require_next_revision
    require_next_revision(current, plan.get('proposedStructure'))
    # Phase 2：声明了受保护字段流转的计划在锁内对**当前盘上状态**重验一遍——观察到的 state、
    # binding generation、expected digest、目标槽占用与定位符保留全部从刚读到的 current 与
    # 当前磁盘重新推导。这条排在派生根因检查之前：归属才是第一权威（契约 §63/§10.1）。
    if plan.get('bindingTransitions'):
        from .prose_binding import require_plan_transitions_current
        require_plan_transitions_current(root, current, plan)
    renderer, sources = renderer_fingerprint(), source_files(root)
    if plan['rendererFingerprint'] != renderer or plan['sourceFiles'] != sources:
        changed = [{'path': path, 'before': plan['sourceFiles'].get(path), 'after': sources.get(path)}
                   for path in sorted(set(plan['sourceFiles']) | set(sources)) if plan['sourceFiles'].get(path) != sources.get(path)]
        raise StructureError('PLAN_INPUT_CHANGED', 'source or renderer changed after preview',
                             {'sourceChanges': changed, 'rendererChanged': plan['rendererFingerprint'] != renderer,
                              'rendererBefore': plan['rendererFingerprint'], 'rendererAfter': renderer,
                              'chapterId': plan.get('parameters', {}).get('chapterId'),
                              'instruction': '此计划尚未提交；重新预览会生成新的计划及可能的新章节 ID，以新预览返回值为准。'})
    from .source_maintenance_inputs import validate_maintenance_binding
    validate_maintenance_binding(root, plan)
    from .external_review_materials import validate_external_materials
    validate_external_materials(root, plan, stage='external-commit')
    from .write_sets import validate_guards
    validate_guards(root, plan.get('maintenanceGuards', []), stage='structure-commit')
    chapter_fact_guards = []
    if 'chapterFacts' in plan.get('parameters', {}):
        from .chapter_facts import validate_fact_plan_inputs
        chapter_fact_guards = validate_fact_plan_inputs(root, plan, current=current)
        if chapter_fact_guards != plan.get('chapterFactGuards'):
            raise StructureError('CHAPTER_FACTS_INPUT_CHANGED', 'chapter fact inputs changed after preview')
        validate_guards(root, chapter_fact_guards, stage='chapter-facts-commit')
    validate_inventory(root, plan['writeSet'])
    outputs = _load_outputs(root, plan)
    proposed = plan['proposedStructure']
    if plan['operation'] == 'import_organization':
        from .import_organization import organization_outputs
        from .source_impact import source_impact, review_outputs
        extras = review_outputs(root, current, proposed, source_impact(current, proposed, root=root))
        expected_extras, organization_guards, organization_review = organization_outputs(root, current, plan, extras)
        if (outputs != {'.govel/structure.json': json_bytes(proposed), **expected_extras}
                or plan.get('organizationGuards') != organization_guards or plan.get('importOrganization') != organization_review):
            raise StructureError('IMPORT_ORGANIZATION_INVALID', 'write set or review differs from the reviewed import batch')
    require_canon_proposal(current, proposed, plan)
    validate_structure(proposed)
    # Commit-time replay of the protected-field boundary: the plan hash binds the
    # proposal, but the boundary is the ownership invariant itself and re-runs
    # against the freshly loaded current state before anything is written——same exact
    # materialization capability as the preview（实施轮 §15/§36/§55）。
    manuscript = _enforce_transaction_boundary(plan, current, proposed, root=root)
    if manuscript:
        if 'manuscriptGuards' not in plan or 'manuscriptGuardGroups' not in plan:
            raise StructureError('MANUSCRIPT_INPUT_CHANGED', 'approved manuscript guards are missing')
        from .manuscript_materialization import validate_materialization_outputs
        validate_materialization_outputs(root, current, plan, outputs, manuscript)
    if outputs['.govel/structure.json'] != json_bytes(proposed):
        raise StructureError('PLAN_HASH_MISMATCH', 'staged structure does not match semantic preview')
    # The historical ID inventory is copied from impact into the committed ledger.
    # Bind its three identity lists to the actual source delta, even if somebody
    # changed the preview and recomputed its plan hash. No semantic scan is needed.
    from .source_identity import require_identity_impact
    require_identity_impact(current, proposed, plan)
    # 契约 §10.1（Phase 2 返修）：高风险所有权流转的 authority 门槛。exact-plan 与锁内
    # current-state 验证到此已经全部通过，但那些只证明「这是被预览的计划、盘上还没变」，
    # 不证明「独立特权通道批准了它」。这条检查排在**任何业务写入之前**，是 commit 的
    # 必要条件：计划哈希、actor 字符串、approved 布尔与 CLI flag 都不是 authority。
    from .prose_binding import require_privileged_approval
    if plan.get('operation') == 'accept_external_modification' and privileged_approval is not None:
        from .external_approval import validate_external_plan
        validate_external_plan(root, current, plan, outputs)
    local_approval = require_privileged_approval(plan, operation_id=operation_id,
                                                approval=privileged_approval, root=root)
    for row in plan['writeSet']:
        if row['before']['exists']:
            target = safe_project_path(root, _backup_path(backup, row['path']).relative_to(root))
            if target.exists() and digest(target.read_bytes()) != row['before']['sha256']:
                raise StructureError('BACKUP_HASH_MISMATCH', 'existing retry backup differs')
            write_bytes(target, safe_project_path(root, row['path']).read_bytes(), mode=row['before']['mode'])
    rows = plan['writeSet']
    journal = {'schemaVersion': 4, 'operationId': operation_id, 'operation': plan['operation'], 'actor': actor,
               'planHash': plan['planHash'],
               'beforeHash': plan['baseHash'], 'afterHash': structure_hash(proposed),
               'beforeRevision': current['revision'], 'afterRevision': proposed['revision'], 'impact': plan['impact'],
               'backupPath': str(backup.relative_to(root)), 'backupFiles': [r['path'] for r in rows],
               'beforeFiles': {r['path']: r['before'] for r in rows}, 'afterFiles': {r['path']: r['after'] for r in rows},
               'existingFiles': [r['path'] for r in rows if r['before']['exists']],
               'createdFiles': [r['path'] for r in rows if not r['before']['exists'] and r['after']['exists']],
               'writtenIndexes': [r['path'] for r in rows if r['owner'].endswith('-index')], 'refusedIndexes': [],
               'committedAt': utc_now(), 'reversible': True}
    journal['changedFiles'] = [r['path'] for r in rows if r['before'] != r['after']]
    journal['unchangedFiles'] = [r['path'] for r in rows if r['before'] == r['after']]
    if local_approval is not None:
        journal['authorConfirmation'] = local_approval
    if manuscript:
        journal['reversible'] = False
        journal['recovery'] = {'mode': 'preserve-identities',
            'instruction': '新章节身份和原文须保留；可归档新增章节，并按需要恢复来源章节。',
            'newChapterIds': sorted(manuscript.content_digests)}
    if plan['operation'] == 'import_organization':
        journal['importOrganization'] = deepcopy(plan['importOrganization'])
    # 重构收据必须携带逐字段变更与定位符移动，名称历史以 receipt 为准。
    if plan.get('changes'):
        journal['changes'] = deepcopy(plan['changes'])
    if plan.get('pathMoves'):
        journal['pathMoves'] = deepcopy(plan['pathMoves'])
    if 'diff' in plan:
        journal['diff'] = deepcopy(plan['diff'])
    proposal_key = {'canon_change_proposal': 'canonProposal',
                    'canon_content_proposal': 'contentProposal',
                    'canon_relation_proposal': 'relationProposal'}.get(plan['operation'])
    if proposal_key is not None:
        journal['proposal'] = deepcopy(plan['parameters'][proposal_key])
    if plan['operation'] == 'source_change' and 'reason' in plan['parameters']:
        journal['reason'] = plan['parameters']['reason']
    if plan['operation'] == 'canon_fact_patch':
        journal['proposals'] = deepcopy(plan['parameters']['contentProposals'])
    if plan['operation'] == 'canon_proposal_batch':
        journal['proposals'] = deepcopy(plan['parameters']['proposals'])
    if 'maintenance' in plan:
        journal['maintenance'] = deepcopy(plan['maintenance'])
    if 'externalReviewMaterials' in plan:
        journal['externalReviewMaterials'] = deepcopy(plan['externalReviewMaterials'])
        journal['externalReviewGuards'] = deepcopy(plan['externalReviewGuards'])
    if 'requestId' in plan:
        journal['requestId'] = plan['requestId']
    if 'requestFingerprint' in plan:
        journal['requestFingerprint'] = plan['requestFingerprint']
        journal['identityMap'] = plan['parameters'].get('identityMap', {})
    if 'chapterFacts' in plan['parameters']:
        journal['chapterFacts'] = deepcopy(plan['parameters']['chapterFacts'])
        from .source_model import snapshot_hash
        journal['chapterFacts']['committedSourceHash'] = snapshot_hash(proposed)
    outputs[str(journal_path.relative_to(root))] = json_bytes(journal)
    outputs[str(plan_path.relative_to(root))] = None
    # case-only / normalization-only 改名是同一 inode 的原地 rename，写集带不了它
    # （两行会指向同一个文件），可目录项的拼写确实变了，而 structure 记的就是拼写。
    # 所以原地改名与写集同生共死：写集失败就把已改的名倒序改回去，撤回失败报
    # ROLLBACK_INCOMPLETE，不假装无事发生。
    from .refactor import apply_locator_renames, revert_locator_renames
    applied = apply_locator_renames(root, plan)
    try:
        fact_expected = None
        if chapter_fact_guards or manuscript or plan.get('organizationGuards'):
            # A chapter-owned fact can invalidate workflow approvals. That workflow
            # is then an output, protected by the approved before-image instead of
            # a read-only guard that would reject our own legitimate write.
            fact_expected = inventory(root, outputs)
            approved_before = {row['path']: row['before'] for row in plan['writeSet']}
            for row in fact_expected:
                if row['path'] in approved_before:
                    row['before'] = approved_before[row['path']]
        apply_write_set(root, outputs, operation=plan['operation'],
                        expected=fact_expected, guards=[*plan.get('maintenanceGuards', []),
                            *plan.get('externalReviewGuards', []),
                            *(row for row in plan.get('organizationGuards', []) if row['path'] not in outputs),
                            *(manuscript.guards if manuscript else []),
                            *(row for row in chapter_fact_guards if row['path'] not in outputs)],
                        guard_groups=manuscript.guard_groups if manuscript else [])
    except BaseException as failure:
        revert_locator_renames(root, applied, failure=failure)
        raise
    chapter = next((c for c in proposed['chapters'] if c['chapterId'] == plan['parameters'].get('chapterId')), None)
    result = {'ok': True, 'code': 'STRUCTURE_COMMITTED', **journal, **({'chapter': chapter} if chapter else {}),
            'semanticFollowUp': ['refresh_context', 'regenerate_affected_outline', 'repeat_review_and_approval']
            if '.govel/workflow.json' in journal['backupFiles'] else []}
    from .source_reports import public_report
    result['semanticFollowUp'] = ['query_source_reviews', 'query_source_prose_evidence']
    return public_report(result, root=root)


def validate_undo(root: Path, operation_id: str, journal: dict, *, preserve_source_runtime: bool = False) -> tuple[Path, dict]:
    from .structure_commands import _operation_paths, _backup_path
    _, _, backup = _operation_paths(root, operation_id)
    if journal.get('operationId') != operation_id:
        raise StructureError('OPERATION_ID_MISMATCH', 'journal operationId does not match its filename')
    # A formal undo operation is a ledger entry, not an undoable one: this toolbox has no
    # undo-of-undo, so the ledger's undo path refuses it outright instead of offering a reversal
    # the engine does not implement. Checked before the inventory so the refusal never depends on
    # how complete the undo journal's own write-set record happens to be.
    if journal.get('operation') == UNDO_OPERATION_NAME or journal.get('reversible') is False:
        raise StructureError('OPERATION_NOT_REVERSIBLE', 'this operation cannot be undone',
                             {'operationId': operation_id, 'operation': journal.get('operation')})
    if journal.get('backupPath') != str(backup.relative_to(root)):
        raise StructureError('BACKUP_PATH_INVALID', 'journal backupPath does not match its operationId')
    files = journal.get('backupFiles', [])
    if not files or set(files) != set(journal.get('beforeFiles', {})) or set(files) != set(journal.get('afterFiles', {})):
        raise StructureError('UNDO_UNVERIFIABLE', 'incomplete inventory')
    existed = {}
    for relative in files:
        if preserve_source_runtime and relative in {'.govel/source-review.json', '.govel/workflow.json'}:
            continue
        before, after = journal['beforeFiles'][relative], journal['afterFiles'][relative]
        current = file_state(safe_project_path(root, relative))
        if current != after:
            if relative == '.govel/structure.json':
                # 撤销本身会把 revision +1：内容等价（忽略 revision/时间戳）视为未冲突。
                try:
                    from .author_decisions import undo_content_equivalent
                    equivalent = undo_content_equivalent(root, load_structure(root), journal)
                except (StructureError, OSError, ValueError):
                    equivalent = False
                if not equivalent:
                    raise StructureError('UNDO_FILE_CONFLICT', 'structure changed after commit', {'path': relative})
            elif Path(relative).parent == Path('.govel/cache'):
                pass  # 派生缓存按新 revision 重建，不算作者改动
            else:
                raise StructureError('UNDO_FILE_CONFLICT', 'newer edits would be overwritten', {'path': relative})
        if before['exists']:
            source = safe_project_path(root, _backup_path(backup, relative).relative_to(root))
            if not source.is_file():
                raise StructureError('BACKUP_MISSING', 'original bytes unavailable', {'path': relative})
            if digest(source.read_bytes()) != before['sha256']:
                raise StructureError('BACKUP_HASH_MISMATCH', 'original bytes damaged', {'path': relative})
        existed[relative] = before['exists']
    return backup, existed


def undo(root: Path, operation_id: str, *, actor: str,
         binding_generation: int | None = None) -> dict:
    from .structure_commands import _operation_paths
    root = root.resolve()
    _, path, _ = _operation_paths(root, operation_id)
    if not path.is_file():
        raise StructureError('OPERATION_NOT_FOUND', f'unknown operation: {operation_id}')
    with project_lock(root):
        journal = read_json(path)
        current = load_structure(root)
        # Phase 3 分流：current-binding 结构走单调逆向事务；legacy schema 1/3 维持
        # 既有整包恢复 runtime，行为一字不动（契约 §4 Formal Undo 行 / 阶段隔离）。
        return _monotonic_undo_locked(root, operation_id, journal=journal, current=current,
                                      actor=actor, binding_generation=binding_generation)




# ---------------------------------------------------------------------------
# Phase 3 — Formal Undo: one new, forward-committed reverse transaction
# ---------------------------------------------------------------------------
#
# Current-binding schemas never restore a historical snapshot. A formal undo reads the
# target operation's committed evidence (journal + before-image) to prove *what it did*,
# then commits the exact reverse as a NEW forward transaction on top of the current
# state: new revision, new generation, freshly sealed integrityDigest. The protected
# semantics it restores are the ones the operation actually changed, derived by the same
# central transition sealer and writer boundary as every other formal transition, so
# neither a stale token nor a forged journal can resurrect an old generation（契约 §4/§9）.


def _chapter_row(structure: dict, chapter_id: object) -> dict | None:
    if not isinstance(chapter_id, str):
        return None
    for row in structure.get('chapters', []):
        if isinstance(row, dict) and row.get('chapterId') == chapter_id:
            return row
    return None


def _protected_semantics(chapter: dict):
    """`(locator, state, expectedContentDigest)` of a chapter binding, or None if uninterpretable.

    `generation` / `integrityDigest` are deliberately absent: they seal a transaction, they
    are not part of what an operation *changed*. Comparing them would read every later
    commit as a protected delta and would invite replaying a historical seal（契约 §4）。
    """
    binding = chapter.get('proseBinding') if isinstance(chapter, dict) else None
    if not isinstance(binding, dict):
        return None
    from .prose_binding import BINDING_STATES
    state = binding.get('state')
    if state not in BINDING_STATES:
        return None
    return (chapter.get('filePath'), state, binding.get('expectedContentDigest'))


def _reverse_transitions(current: dict, before: dict, *, operation_id: str,
                         journal_digest: str) -> list[dict]:
    """The target operation's actual protected delta, as one reverse declaration per chapter.

    Read from the two structures the evidence already pins — the operation's committed
    before-image and the current structure — never from a journal field that could claim
    a change it did not make. A chapter the operation did not semantically touch gets no
    declaration, so its binding is left byte-identical and its generation does NOT advance（契约 §9）。
    """
    current_rows = {row['chapterId']: row for row in current.get('chapters', []) if isinstance(row, dict)}
    before_rows = {row['chapterId']: row for row in before.get('chapters', []) if isinstance(row, dict)}
    if set(current_rows) != set(before_rows):
        raise StructureError('UNDO_UNSUPPORTED_OPERATION',
                             'the target operation changed the chapter identity set; formal undo never creates or removes a chapter',
                             {'added': sorted(set(current_rows) - set(before_rows)),
                              'removed': sorted(set(before_rows) - set(current_rows))})
    transitions: list[dict] = []
    for chapter_id in sorted(current_rows):
        current_key = _protected_semantics(current_rows[chapter_id])
        before_key = _protected_semantics(before_rows[chapter_id])
        if current_key == before_key:
            continue
        if current_key is None or before_key is None:
            # 一方的 binding 连语义都解释不出来：这次变更无法被精确逆推。损坏的兄弟章若
            # 语义字段未受影响（上面的等值分支）照旧隔离，绝不因此锁死整个 undo（契约 §17/§18）。
            raise StructureError('UNDO_UNVERIFIABLE',
                                 'the target operation changed protected ownership fields on a chapter whose '
                                 'binding is not interpretable in both images; the exact reverse is not provable',
                                 {'chapterId': chapter_id})
        transitions.append({
            'kind': 'formal_undo',
            'chapterId': chapter_id,
            'reversedOperationId': operation_id,
            'reversedJournalDigest': journal_digest,
            'currentFilePath': current_key[0],
            'currentBindingGeneration': current_rows[chapter_id]['proseBinding'].get('generation'),
            'currentExpectedContentDigest': current_key[2],
            'restoreFilePath': before_key[0],
            'restoreState': before_key[1],
            'restoreExpectedContentDigest': before_key[2],
        })
    return transitions


def _undo_target_structure(root: Path, operation_id: str, journal: dict) -> tuple[Path, dict]:
    """The target operation's committed before-image structure, validated against its journal."""
    from .structure_commands import _operation_paths, _backup_path
    _, _, backup = _operation_paths(root, operation_id)
    before_files = journal.get('beforeFiles')
    row = before_files.get('.govel/structure.json') if isinstance(before_files, dict) else None
    if not isinstance(row, dict) or row.get('exists') is not True:
        raise StructureError('UNDO_UNVERIFIABLE',
                             'the target journal does not carry a structure before-image',
                             {'operationId': operation_id})
    source = safe_project_path(root, _backup_path(backup, '.govel/structure.json').relative_to(root))
    if not source.is_file():
        raise StructureError('BACKUP_MISSING', 'the operation before-image is unavailable',
                             {'path': '.govel/structure.json'})
    raw = source.read_bytes()
    if digest(raw) != row.get('sha256'):
        raise StructureError('BACKUP_HASH_MISMATCH', 'the operation before-image is damaged',
                             {'path': '.govel/structure.json'})
    try:
        before = json.loads(raw)
    except ValueError as exc:
        raise StructureError('UNDO_UNVERIFIABLE', 'the operation before-image is not a readable structure',
                             {'operationId': operation_id}) from exc
    if not isinstance(before, dict):
        raise StructureError('UNDO_UNVERIFIABLE', 'the operation before-image is not a structure object',
                             {'operationId': operation_id})
    return backup, before


def _require_reverse_ownership(root: Path, current: dict, transitions: list[dict]) -> None:
    """Locked current-state revalidation of every reverse about to be committed（契约 §12/§15）。

    Re-derived from the freshly loaded current structure and the current disk — never from
    the fact that the locator used to belong to this chapter. A reverse destination that is
    occupied, reserved or aliased is a refusal, never an overwrite or a hash-based claim.
    """
    from .prose_binding import (OWNED_CLEAN, UNBOUND, canonical_locator_key, locator_reservations,
                                prose_slot_occupied, require_observation)
    reservations, fatal = locator_reservations(current)
    if fatal:
        raise StructureError('STRUCTURE_INVALID', 'prose locator invariant violated', {'errors': fatal})
    for transition in transitions:
        chapter_id = transition['chapterId']
        chapter = _chapter_row(current, chapter_id)
        if chapter is None:
            raise StructureError('UNDO_UNVERIFIABLE',
                                 'the reverse target chapter is missing from the current structure',
                                 {'chapterId': chapter_id})
        state = chapter['proseBinding'].get('state')
        allowed = {'managed': (OWNED_CLEAN,), 'unbound': (UNBOUND,)}.get(state)
        if allowed is None:
            raise StructureError('UNDO_UNSUPPORTED_OPERATION',
                                 'formal undo reverses a managed/unbound change; the chapter is in another state now',
                                 {'chapterId': chapter_id, 'state': state})
        require_observation(root, current, chapter, allowed, action='formal undo')
        destination = transition['restoreFilePath']
        if destination == chapter.get('filePath'):
            # 原地反向（locator 没变，只逆 state/digest）：恢复目标就是本章当前受管的槽位，
            # 占用检查属于「搬去别处」的安全条件；这里字节的正当性已由上面的 observation
            # （OWNED_CLEAN / UNBOUND）证明，绝不误判成「目标被占用」。
            continue
        holder = reservations.get(canonical_locator_key(destination))
        if holder is not None and holder != chapter_id:
            raise StructureError('PROSE_LOCATOR_CONFLICT',
                                 'the reverse destination is reserved by another chapter',
                                 {'chapterId': chapter_id, 'targetPath': destination, 'reservedBy': holder})
        if prose_slot_occupied(root, destination):
            raise StructureError('PROSE_TARGET_NOT_OWNED',
                                 'the reverse destination already holds a file; existing bytes are never taken over',
                                 {'chapterId': chapter_id, 'targetPath': destination})


def _reverse_write_set(root: Path, journal: dict, backup: Path) -> tuple[dict, dict]:
    """The target operation's own reversible write set, reversed file by file.

    Every row was re-validated against its committed after-state by `validate_undo` first,
    so a file that moved on since is a refusal, never an overwrite: restore the recorded
    before-image, delete what the operation created. The structure row is excluded (the
    undo publishes the freshly sealed structure) and derived caches are regenerated later
    instead of restored（契约 §10）。
    """
    from .structure_commands import _backup_path
    outputs: dict[str, bytes | None] = {}
    modes: dict[str, int] = {}
    for relative in journal['backupFiles']:
        if relative == '.govel/structure.json':
            continue
        if Path(relative).parent == Path('.govel/cache'):
            continue
        before = journal['beforeFiles'][relative]
        if before.get('exists'):
            source = safe_project_path(root, _backup_path(backup, relative).relative_to(root))
            outputs[relative] = source.read_bytes()
            if before.get('mode') is not None:
                modes[relative] = before['mode']
        else:
            outputs[relative] = None
    return outputs, modes


def prevalidate_undo(root: Path, operation_id: str, journal: dict, current: dict,
                     binding_generation: int | None = None) -> None:
    """Lock-free deterministic refusals for a formal undo（与提交路径同一条纪律）。

    The same evidence checks the locked engine runs, in the same order, so a journal whose
    identity, shape, before-image or chain-top position is already known-bad, and an undo
    request that carries no current binding generation, never even take the project lock.
    The locked pass re-runs every one of them against the current state it loaded — this
    pass keeps refusals honest and lock-free, it never authorizes.
    """
    _undo_evidence_prefix(root, operation_id, journal, current, binding_generation)


def _undo_evidence_prefix(root: Path, operation_id: str, journal: dict,
                          current: dict, binding_generation: int | None = None
                          ) -> tuple[Path, dict, list[dict]]:
    """Evidence a formal undo must be able to prove before it may touch anything.

    Returns `(target backup root, operation before-image structure, reverse declarations)`.
    Every refusal here is a property of the committed evidence, the current structure
    content and the request's own echoed generation token — no disk write, no history
    replay beyond the target's own before-image.
    """
    from .prose_binding import pending_transaction_present
    if journal.get('operationId') != operation_id:
        raise StructureError('OPERATION_ID_MISMATCH', 'journal operationId does not match its filename')
    if journal.get('schemaVersion') != 4:
        raise StructureError('UNDO_UNVERIFIABLE',
                             'only schema-4 committed operations carry a reversible write set this engine can invert',
                             {'operationId': operation_id, 'schemaVersion': journal.get('schemaVersion')})
    if journal.get('operation') == UNDO_OPERATION_NAME or journal.get('reversible') is False:
        raise StructureError('OPERATION_NOT_REVERSIBLE', 'this operation cannot be undone',
                             {'operationId': operation_id, 'operation': journal.get('operation')})
    if pending_transaction_present(root):
        raise StructureError('TRANSACTION_INCOMPLETE',
                             'a durable transaction manifest is still on disk; run workflow recover first',
                             {'operationId': operation_id})
    backup, before = _undo_target_structure(root, operation_id, journal)
    # Top-of-chain 是硬条件（契约 §13）：当前结构内容必须仍等于该 operation 的 after 态。
    # 任何后续提交（无论是否进 ledger）都已经把它推下顶端，此时撤销会部分覆盖它的结果。
    from .author_decisions import undo_content_equivalent
    if not undo_content_equivalent(root, current, journal):
        raise StructureError('UNDO_NOT_CHAIN_TOP',
                             'the target operation is not the top of the undoable chain; later changes would be overwritten',
                             {'operationId': operation_id, 'afterRevision': journal.get('afterRevision'),
                              'currentRevision': current.get('revision')})
    from .structure_commands import _operation_paths
    _, journal_path, _ = _operation_paths(root, operation_id)
    transitions = _reverse_transitions(current, before, operation_id=operation_id,
                                       journal_digest=digest(journal_path.read_bytes()))
    _require_request_generation_token(operation_id, current, transitions, binding_generation)
    return backup, before, transitions


def _require_request_generation_token(operation_id: str, current: dict, transitions: list[dict],
                                      binding_generation: int | None) -> None:
    """The undo request itself must prove which state the caller saw（契约 §7/§12/§23.4）.

    An undo of a real binding delta is a CAS against the chapter's *current* generation, and
    the token has to come from the request. Letting an omitted token mean "the engine may go
    read whatever generation is current" would downgrade request CAS to server-side
    current-state observation: any stale caller could then submit an undo and be authorized
    by the very drift it failed to notice. So the request token and the locked current-state
    revalidation are both mandatory, and neither substitutes for the other.

    The delta shape is derived by the engine from the target's committed before-image against
    the current structure, never claimed by the caller — an operation that changed no
    protected semantics has no binding generation to echo, so no token is demanded of it.
    """
    if not transitions:
        return
    if len(transitions) > 1:
        # 一个请求令牌盖不住多章：把它对每个 chapter 都比一遍是伪 CAS（同一个整数只会等于
        # 其中一章的当前代次）。当前 operation model 里没有任何可撤销 operation 一次改动
        # 多章受保护语义，出现这种形态只可能是证据被伪造，一律 fail closed。
        raise StructureError('UNDO_UNSUPPORTED_OPERATION',
                             'this undo would reverse protected ownership semantics for more than one '
                             'chapter, which a single request binding generation cannot authorize; the '
                             'current-binding undo contract accepts one binding target per operation',
                             {'operationId': operation_id,
                              'chapterIds': [transition['chapterId'] for transition in transitions]})
    transition = transitions[0]
    chapter_id = transition['chapterId']
    chapter = _chapter_row(current, chapter_id)
    if binding_generation is None:
        raise StructureError('OWNERSHIP_GENERATION_REQUIRED',
                             "this undo reverses a binding change and must echo that chapter's current "
                             "binding generation: read bindingGeneration from the chapter receipt "
                             "(workflow chapter-receipt) or from the operation metadata in "
                             "project structure operations, then send it as bindingGeneration; "
                             "omitting the token is a refusal, not a request to look it up",
                             {'operationId': operation_id, 'chapterId': chapter_id,
                              'chapterFilePath': chapter.get('filePath') if chapter else None})
    from .prose_binding import request_generation_current
    if not request_generation_current(chapter, binding_generation):
        raise StructureError('OWNERSHIP_GENERATION_CONFLICT',
                             "the undo request's binding generation is stale for this chapter",
                             {'chapterId': chapter_id,
                              'requestBindingGeneration': binding_generation,
                              'currentBindingGeneration': (chapter or {}).get('proseBinding', {}).get('generation')})


def _monotonic_undo_locked(root: Path, operation_id: str, *, journal: dict, current: dict,
                           actor: str, binding_generation: int | None) -> dict:
    """Formal Undo for schema 4/5: a NEW forward transaction that reverses one operation.

    Never a snapshot replay（契约 §4/§6）: the proposal starts from the current structure,
    only the protected semantics the target operation actually changed are inverse-patched
    — through the central transition sealer, then re-derived independently by the same
    writer boundary every other formal transition crosses — and every new value (revision,
    generation, integrityDigest) is sealed by THIS commit. The physical reversal is the
    operation's own write set flipped inside one durable write set, so a crash can never
    leave prose moved with the binding still forward, or the reverse.
    """
    from .prose_binding import binding_authority, binding_transition_result, enforce_writer_boundary, validate_binding
    from .structure_commands import _backup_path, _operation_paths, new_operation_id

    backup, before, transitions = _undo_evidence_prefix(
        root, operation_id, journal, current, binding_generation)
    _, journal_path, _ = _operation_paths(root, operation_id)
    # 请求令牌 CAS 已由 _undo_evidence_prefix 对**锁内刚读到的 current** 重跑一遍（不可绕过），
    # 这里继续的是盘上当前态与历史证据的重验（契约 §12），两者缺一都不是完整的授权。
    _require_reverse_ownership(root, current, transitions)
    # 被撤销 operation 的整份写集证据必须完整且仍处于 after 态：文件、备份与摘要一一对上，
    # 否则这次撤销无法证明自己逆向的是哪一批字节（契约 §12/§14）。失败只阻止这一次 Undo。
    validate_undo(root, operation_id, journal, preserve_source_runtime=True)
    undo_operation_id = new_operation_id()
    proposed = deepcopy(before)
    proposed['revision'] = current['revision'] + 1
    proposed['updatedAt'] = utc_now()
    proposed['lastOperation'] = {'operationId': undo_operation_id, 'operation': 'undo',
                                 'actor': actor, 'committedAt': proposed['updatedAt']}
    # 非受保护语义（标题、状态、顺序、placement……）以被撤销 operation 的 before 态为准；
    # 受保护所有权字段则以**当前**持久化值为起点——只有实际发生语义变更的章才被逆转（契约 §6/§9）。
    for row in proposed.get('chapters', []):
        current_row = _chapter_row(current, row.get('chapterId'))
        if current_row is None:
            continue
        row['filePath'] = current_row.get('filePath')
        row['proseBinding'] = deepcopy(current_row.get('proseBinding'))
    for transition in transitions:
        row = _chapter_row(proposed, transition['chapterId'])
        result = binding_transition_result(proposed, _chapter_row(current, transition['chapterId']),
                                           transition, generation=proposed['revision'])
        row['filePath'] = result['filePath']
        row['proseBinding'] = result['proseBinding']
    authority = binding_authority(transitions) if transitions else None
    enforce_writer_boundary(current, proposed, authority=authority, root=root)
    for transition in transitions:
        errors = validate_binding(proposed, _chapter_row(proposed, transition['chapterId']))
        if errors:
            raise StructureError('OWNERSHIP_BINDING_INVALID',
                                 'the reverse seal is not a valid binding',
                                 {'chapters': {transition['chapterId']: errors}})
    from .author_decisions import derived_decision_invalidations
    if current.get('authorDecisions') or proposed.get('authorDecisions'):
        proposed['authorDecisions'] = derived_decision_invalidations(current, proposed,
            operation_id=undo_operation_id, revision=proposed['revision'], restoring=True)
    validate_structure(proposed)
    outputs, modes = _reverse_write_set(root, journal, backup)
    outputs['.govel/structure.json'] = json_bytes(proposed)
    impact = {}
    from .source_impact import source_impact, review_outputs
    impact = source_impact(current, proposed, root=root)
    outputs.update(review_outputs(root, current, proposed, impact))
    undo_backup = safe_project_path(root, undo_backup_relative_path(undo_operation_id))
    rows = inventory(root, outputs)
    try:
        for row in rows:
            if row['before']['exists']:
                write_bytes(_backup_path(undo_backup, row['path']),
                            safe_project_path(root, row['path']).read_bytes(),
                            mode=row['before']['mode'])
    except BaseException:
        shutil.rmtree(undo_backup, ignore_errors=True)
        raise
    # 撤销的持久证明与 legacy 相同三层，同一次 apply_write_set：receipt（索引）、正式 undo
    # operation（共享 ledger 里的根证据，自带 durable before-image）、辅助 authority。
    # 被撤销 journal 的字节摘要与其已提交语义哈希被钉进这份 journal，因此事后无法只靠往
    # evidence 目录多塞自声明 JSON 制造一次从未发生的 undo（契约 §20）。
    receipt_path = receipt_relative_path(operation_id, proposed['revision'])
    receipt = receipt_bytes(operation_id=operation_id, undo_operation_id=undo_operation_id,
                            undo_revision=proposed['revision'], actor=actor,
                            undone_at=proposed['updatedAt'], impact=impact)
    outputs[receipt_path] = receipt
    undo_journal_relative = undo_journal_relative_path(undo_operation_id)
    outputs[undo_journal_relative] = undo_journal_bytes(
        undo_operation_id=undo_operation_id, reversed_operation_id=operation_id,
        reversed_journal_digest=digest(journal_path.read_bytes()), actor=actor,
        before_hash=structure_hash(current), after_hash=structure_hash(proposed),
        before_revision=current['revision'], after_revision=proposed['revision'], impact=impact,
        backup_path=undo_backup_relative_path(undo_operation_id),
        backup_files=[row['path'] for row in rows],
        before_files={row['path']: row['before'] for row in rows},
        after_files={row['path']: row['after'] for row in rows},
        existing_files=[row['path'] for row in rows if row['before']['exists']],
        created_files=[row['path'] for row in rows if not row['before']['exists'] and row['after']['exists']],
        receipt_path=receipt_path, receipt_digest=digest(receipt), committed_at=proposed['updatedAt'])
    authority_path = authority_relative_path(operation_id, proposed['revision'])
    outputs[authority_path] = authority_bytes(
        reversed_operation_id=operation_id, undo_operation_id=undo_operation_id,
        reversed_journal_digest=digest(journal_path.read_bytes()),
        reversed_after_revision=journal['afterRevision'], reversed_after_hash=journal['afterHash'],
        pre_undo_revision=current['revision'], pre_undo_hash=structure_hash(current),
        restored_revision=proposed['revision'], restored_hash=structure_hash(proposed),
        actor=actor, committed_at=proposed['updatedAt'], undone_at=proposed['updatedAt'],
        receipt_path=receipt_path, receipt_digest=digest(receipt))
    try:
        apply_write_set(root, outputs, operation='undo', modes=modes)
    except BaseException:
        shutil.rmtree(undo_backup, ignore_errors=True)
        raise
    result = {'ok': True, 'code': 'STRUCTURE_UNDONE', 'operationId': operation_id,
              'undoOperationId': undo_operation_id, 'revision': proposed['revision'],
              'undoReceiptPath': receipt_path, 'undoAuthorityPath': authority_path,
              'undoJournalPath': undo_journal_relative, 'refusedIndexes': []}
    from .source_reports import public_report
    result['impact'] = impact
    return public_report(result)
