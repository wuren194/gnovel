"""Multi-domain project health aggregation.

Doctor is an aggregator, not a second runtime. Every domain consumes the
health or readiness computation that already owns the rule:

    context      -> context_health.assess_context (via query_context_health),
                    which also owns each issue's status/recommendedAction — the
                    doctor passes those through, it does not re-guess them
    knowledge    -> knowledge_store.knowledge_health + knowledge_index.index_state
    writer       -> task_prepare.prepare_task(task="writer"): its readiness.taskReady
                    and blocking are the final go/no-go. work_voice_profile and
                    author_decisions stay only as explanation issues, never as a
                    second readiness gate.
    canon        -> pending structure plans (the same baseRevision/planHash the
                    commit path enforces) and canon_guard's fail-closed contract
    workflow     -> finalization.pending_transactions + workflow/structure cross-check
    tooling      -> generate_wrappers.check; release and host registration checks
                    remain explicit maintenance operations

Re-deriving any of those rules here would create a second, silently divergent
source of truth — which is the failure mode this module exists to replace. A
single `healthy` boolean cannot say "Canon Ready but Writer Ready is not",
so domains carry independent status and every issue carries a typed status
(`mechanical_error`, `stale`, `needs_rebuild`, `author_decision_required`,
`semantic_review_required`, `informational`) instead of one flat string.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .project import (continuation_policy, default_approval_mode, protected_paths,
                      repair_policy, serial_planning_policy, toolbox_manifest)
from .settings import check_settings
from .source_model import snapshot_hash
from .structure_store import StructureError, load_structure, ordered_chapters
from .workflow import WorkflowError, validate_next_action_for_project

DOMAINS = ('project', 'planning', 'writerReadiness', 'canon',
           'context', 'knowledge', 'workflow', 'tooling')

SEVERITIES = ('blocking', 'warning', 'informational')
ISSUE_STATUSES = ('mechanical_error', 'stale', 'needs_rebuild',
                  'author_decision_required', 'semantic_review_required', 'informational')

# Domain status precedence: a domain reports the most serious thing wrong with it,
# never a green dot over a local blocker.
_STATUS_ORDER = ('blocked', 'stale', 'needs_rebuild', 'author_decision_required',
                 'semantic_review_required', 'warning', 'informational', 'healthy')

# An issue's `status` says *what kind* of problem it is; a domain's `status` says
# how bad the domain is. They are different vocabularies and must not be compared
# directly — conflating them is how "mechanical_error" used to read as "healthy".
_ISSUE_STATUS_TO_DOMAIN = {
    'mechanical_error': 'blocked',
    'stale': 'stale',
    'needs_rebuild': 'needs_rebuild',
    'author_decision_required': 'author_decision_required',
    'semantic_review_required': 'semantic_review_required',
    'informational': 'informational',
}

# Canon-family operations whose staged plans are author decisions, not mechanics.
CANON_PLAN_OPERATIONS = {
    'canon_change_proposal': 'entity',
    'canon_content_proposal': 'content',
    'canon_relation_proposal': 'relation',
}

# Writer readiness is owned by task_prepare.prepare_task(task="writer"): the doctor
# consumes its taskReady/blocking/missing as the final go/no-go, it does not keep a
# second gate. Each blocker code keeps the *kind* of problem it is so the domain
# status stays honest — an unapproved range/plan/Stage A is the author's decision, a
# missing chapter or unresolved capability is a mechanical fault, a blocked source
# rule needs review, a pending transaction or unready context needs a rebuild.
WRITER_BLOCKER_STATUS = {
    'CHAPTER_OUTSIDE_APPROVED_RANGE': 'author_decision_required',
    'SOURCE_CHAPTER_PLAN_MISSING': 'author_decision_required',
    'STAGE_A_NOT_APPROVED': 'author_decision_required',
    'STAGE_A_OUTLINE_MISMATCH': 'author_decision_required',
    'OUTLINE_MISSING': 'author_decision_required',
    'CANDIDATE_PROSE_MISSING': 'author_decision_required',
    'CONTINUITY_REQUIRED': 'author_decision_required',
    'CONTINUITY_CHAPTER_REQUIRED': 'author_decision_required',
    'PREDECESSOR_UNSTABLE': 'author_decision_required',
    'PREDECESSOR_INVALID': 'author_decision_required',
    'TOOL_ACTION_REQUIRED': 'author_decision_required',
    'SETTING_CARD_MISSING': 'author_decision_required',
    'SOURCE_RULES_BLOCKED': 'semantic_review_required',
    'TRANSACTION_INCOMPLETE': 'needs_rebuild',
    'CONTEXT_HEALTH_BLOCKED': 'needs_rebuild',
    'CONTEXT_NOT_READY': 'needs_rebuild',
    'CHAPTER_NOT_FOUND': 'mechanical_error',
    'SCOPE_UNRESOLVED': 'mechanical_error',
    'ENTITY_UNKNOWN': 'mechanical_error',
    'OUTLINE_PATH_INVALID': 'mechanical_error',
    'CONTRACT_PLAN_INCOMPLETE': 'mechanical_error',
    'CAPABILITY_UNRESOLVED': 'mechanical_error',
    'CONTEXT_INVALID': 'mechanical_error',
    'POV_HARD_CONSTRAINTS_UNAVAILABLE': 'mechanical_error',
    'STRUCTURE_INVALID': 'mechanical_error',
    'SETTINGS_INVALID': 'mechanical_error',
}


def _issue(*, code: str, domain: str, severity: str, status: str, object_type: str,
           object_id: str, message: str, evidence: dict | None = None,
           affected_operations: list[str] | None = None,
           recommended_action: str = '', **optional: Any) -> dict:
    """Build one structured issue.

    `message` keeps the historical wording verbatim: the legacy `problems`
    projection is derived from it, and downstream assertions match on that text.
    """
    row: dict[str, Any] = {
        'code': code, 'domain': domain, 'severity': severity, 'status': status,
        'objectType': object_type, 'objectId': object_id, 'message': message,
        'evidence': evidence if evidence is not None else {},
        'affectedOperations': list(affected_operations or []),
        'recommendedAction': recommended_action,
    }
    for key in ('sourceHash', 'receiptHash', 'chapterId', 'candidateId', 'assetId'):
        value = optional.get(key)
        if value is not None:
            row[key] = value
    return row


def _domain_status(issues: list[dict]) -> str:
    """A domain reports the most serious *kind* of problem it has.

    Deliberately derived from the issue `status` vocabulary, not from `severity`:
    a pending transaction and a missing chapter plan are both blocking work, but
    one is `needs_rebuild` (run `workflow recover`) and the other is
    `author_decision_required` (the author must plan). Collapsing both into
    `blocked` would tell the author to go fix the engine when the actual next
    step is different. `severity` drives the project-level `healthy` gate instead.
    """
    if not issues:
        return 'healthy'
    mapped = {_ISSUE_STATUS_TO_DOMAIN.get(row['status'], 'blocked') for row in issues}
    for candidate in _STATUS_ORDER:
        if candidate in mapped:
            return candidate
    return 'healthy'


def _finalize_domain(domain: str, section: dict, issues: list[dict]) -> dict:
    """A domain may declare its own status when it could not assess at all.

    "Structure unreadable" is not `healthy` — claiming a green dot over a domain
    nobody looked at is exactly the failure this module replaces. Such a domain
    reports `unknown` and says why.
    """
    declared = section.get('status') if isinstance(section, dict) else None
    status = declared if declared in _STATUS_ORDER or declared in {'unknown', 'not_applicable'} else _domain_status(issues)
    row: dict[str, Any] = {'status': status, 'issueCount': len(issues), 'issues': issues}
    if isinstance(section, dict):
        row.update({key: value for key, value in section.items() if key != 'status'})
    return row


def _safe_domain(domain: str, producer) -> tuple[dict, list[dict], dict]:
    """Run one domain so its failure cannot take down the whole report.

    A domain that raises is reported as `unknown` with the error attached, and its
    exception becomes a `mechanical_error` issue in its own domain. Letting one
    domain's crash blank out the other eight is the same failure mode as a single
    green dot: it hides eight facts behind one crash.
    """
    try:
        result = producer()
        section = result[0]
        domain_issues = list(result[1])
        extra = result[2] if len(result) > 2 and isinstance(result[2], dict) else {}
        return section, domain_issues, extra
    except Exception as exc:  # noqa: BLE001 - 诊断必须容错：一域崩溃不得吞掉其余域
        return ({'status': 'unknown', 'applicable': False,
                 'reason': f'{domain} assessment failed: {exc}'},
                [_issue(code=domain.upper() + '_ASSESSMENT_FAILED', domain=domain,
                        severity='blocking', status='mechanical_error', object_type='domain',
                        object_id=domain, message=f'{domain} 域评估失败：{exc}',
                        evidence={'error': str(exc), 'type': type(exc).__name__},
                        affected_operations=['project.doctor'],
                        recommended_action='按错误修复该域依赖；其余域的结果仍然有效')],
                {})


# --------------------------------------------------------------------------
# project domain (structure, prose, settings, policy, timeline, cache)
# --------------------------------------------------------------------------

def _project_json(root: Path) -> dict:
    """Read and version-gate the project marker.

    Unsupported formats are rejected with the manifest-owned error code.
    """
    from .jsonio import read_json
    from .structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION
    path = root / '.govel' / 'project.json'
    try:
        marker = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise StructureError('PROJECT_NOT_INITIALIZED', 'missing or corrupt project marker') from exc
    if (marker.get('schemaVersion') != PROJECT_SCHEMA_VERSION
            or marker.get('workflowVersion') != WORKFLOW_VERSION):
        from .project import unsupported_error_code
        raise StructureError(unsupported_error_code(),
                             '仅支持当前项目格式；旧项目请按外部小说人工整理后迁入新项目')
    return marker


def _read_workflow(root: Path) -> dict:
    path = root / '.govel' / 'workflow.json'
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def prose_drift(root: Path, structure: dict) -> list[dict]:
    """Prose/structure drift as structured issues; wording matches the old strings."""
    issues = []
    tracked = set()
    for chapter in structure.get('chapters', []):
        rel = str(chapter.get('filePath', '')).strip()
        tracked.add(rel)
        if chapter.get('status') in {'stable', 'stale'} and rel and not (root / rel).is_file():
            issues.append(_issue(
                code='PROSE_FILE_MISSING', domain='project', severity='blocking',
                status='mechanical_error', object_type='chapter',
                object_id=chapter['chapterId'],
                message=f"missing prose file for {chapter['chapterId']} ({chapter.get('displayCode', '')}): {rel}",
                evidence={'filePath': rel}, chapterId=chapter['chapterId'],
                affected_operations=['project.prose', 'context.read'],
                recommended_action='恢复正文文件，或用 structure 事务把该章状态改回未完成'))
    prose_dir = root / '正文'
    if prose_dir.is_dir():
        for path in sorted(prose_dir.rglob('*.md')):
            rel = str(path.relative_to(root))
            if rel not in tracked:
                issues.append(_issue(
                    code='PROSE_ORPHAN', domain='project', severity='warning',
                    status='semantic_review_required', object_type='file', object_id=rel,
                    message=f'orphan prose file not tracked by structure: {rel}',
                    evidence={'filePath': rel},
                    affected_operations=['project.prose'],
                    recommended_action='确认该文件是历史稿还是漏登记章节；漏登记需走结构事务'))
    return issues


def refactor_path_issues(root: Path, structure: dict) -> list[dict]:
    """Locator health for the authoring-refactor contract.

    Identity is not a locator, but a locator still has to be safe: inside the
    project, unique, and not a second spelling of a name another record already
    owns. These checks own that dimension only — prose and outline existence stay
    with their owning rules (`prose_drift`, `OUTLINE_MISSING`), so no rule is
    computed twice.
    """
    from . import refactor_paths
    from .refactor_paths import RefactorPathError
    issues: list[dict] = []
    owners: dict[str, str] = {}
    keys: dict[str, str] = {}
    for chapter in structure.get('chapters', []):
        chapter_id = str(chapter.get('chapterId', ''))
        for field in ('filePath',):
            relative = str(chapter.get(field, '') or '').strip()
            if not relative:
                continue
            try:
                canonical = refactor_paths.is_canonical(root, relative)
            except (RefactorPathError, ValueError, OSError):
                canonical = False
            if not canonical:
                issues.append(_issue(
                    code='REFACTOR_PATH_ESCAPES_PROJECT', domain='project', severity='blocking',
                    status='mechanical_error', object_type='chapter', object_id=chapter_id,
                    message=f'{field} is not a safe project-relative locator: {relative}',
                    evidence={'field': field, 'filePath': relative}, chapterId=chapter_id,
                    affected_operations=['refactor_relocate_chapter_paths'],
                    recommended_action='用 refactor 事务把定位符改回项目内规范路径；不要手改 structure.json'))
                continue
            previous = owners.get(relative)
            if previous is not None and previous != chapter_id:
                issues.append(_issue(
                    code='REFACTOR_DUPLICATE_LOCATOR', domain='project', severity='blocking',
                    status='mechanical_error', object_type='chapter', object_id=chapter_id,
                    message=f'two records point to the same file: {relative}',
                    evidence={'filePath': relative, 'otherChapterId': previous}, chapterId=chapter_id,
                    affected_operations=['refactor_relocate_chapter_paths'],
                    recommended_action='用 refactor 事务给其中一条记录换一个未被占用的定位符'))
            owners[relative] = chapter_id
            key = refactor_paths.canonical_key(relative)
            clash = keys.get(key)
            if clash is not None and clash != relative:
                issues.append(_issue(
                    code='REFACTOR_PATH_CANONICAL_COLLISION', domain='project', severity='blocking',
                    status='mechanical_error', object_type='chapter', object_id=chapter_id,
                    message=f'locators collide under Unicode/case folding: {relative} vs {clash}',
                    evidence={'filePath': relative, 'collidesWith': clash,
                              'canonicalKey': key}, chapterId=chapter_id,
                    affected_operations=['refactor_relocate_chapter_paths'],
                    recommended_action='两个定位符在大小写/Unicode 折叠后同名，改用一个不会碰撞的文件名'))
            keys[key] = relative
    # Plans live in structure.json and do not require a Markdown locator.
    from .source_plan import chapter_plan
    for chapter in structure.get('chapters', []):
        if chapter.get('status') in {'planning_required', 'archived'}:
            continue
        # Imported retrospective synopses are facts about written prose, not
        # pre-writing plans. Their acceptance proof is checked in workflowHealth.
        from .import_history import history_entry
        if chapter.get('status') == 'stable' and history_entry(_read_workflow(root), chapter['chapterId']) is not None:
            continue
        if not chapter_plan(structure, chapter['chapterId'])['ready']:
            issues.append(_issue(
                code='SOURCE_CHAPTER_PLAN_MISSING', domain='project', severity='blocking',
                status='mechanical_error', object_type='chapter', object_id=chapter['chapterId'],
                chapterId=chapter['chapterId'], message='本章缺少有效的源计划片段',
                evidence={'source': '.govel/structure.json'},
                affected_operations=['source.chapter-plan', 'task.prepare'],
                recommended_action='通过源事务补齐本章 plan 片段；阅读导出不提供计划权威'))
    return issues


def _refactor_state_issues(root: Path) -> list[dict]:
    """Pending refactor plans and stale locator receipts."""
    from .jsonio import read_json
    from .structure_commands import list_pending_plans
    issues: list[dict] = []
    for plan in list_pending_plans(root):
        if not str(plan.get('operation', '')).startswith('refactor_'):
            continue
        issues.append(_issue(
            code='REFACTOR_PLAN_PENDING', domain='workflow', severity='warning',
            status='author_decision_required', object_type='refactorPlan',
            object_id=str(plan.get('operationId', '')),
            message=f"pending {plan.get('operation')} plan awaits explicit approval",
            evidence={'operation': plan.get('operation'), 'expired': plan.get('expired')},
            affected_operations=['project.structure.commit'],
            recommended_action='确认该重构计划后 commit，或让它过期；未批准的重改不改变任何真源'))
    from .project_relocate import RECEIPT_PATH
    receipt_path = root / RECEIPT_PATH
    if receipt_path.is_file():
        try:
            receipt = read_json(receipt_path)
        except (OSError, ValueError):
            receipt = None
        if not isinstance(receipt, dict):
            issues.append(_issue(
                code='PROJECT_LOCATOR_RECEIPT_INVALID', domain='project', severity='blocking',
                status='mechanical_error', object_type='locatorReceipt', object_id=RECEIPT_PATH,
                message='project locator receipt is unreadable', evidence={'path': RECEIPT_PATH},
                recommended_action='删除损坏的定位符收据；它不是真源，项目不依赖它运行'))
        elif receipt.get('currentLocator') != str(root):
            issues.append(_issue(
                code='PROJECT_LOCATOR_RECEIPT_STALE', domain='project', severity='warning',
                status='stale', object_type='locatorReceipt', object_id=RECEIPT_PATH,
                message='project locator receipt does not name the current folder',
                evidence={'currentLocator': receipt.get('currentLocator'), 'actualRoot': str(root)},
                affected_operations=['project.relocate'],
                recommended_action='项目后来被移动过；历史收据不回写，重新 relocate 会追加新条目'))
    return issues






def policy_report(root: Path, structure: dict | None) -> tuple[dict, list[dict]]:
    """校验工作流策略并报告有效来源。

    RUNTIME-CONFIG 要求报告必须包含有效配置来源；这些字段曾经零消费者，
    改值不会改变任何行为，等于伪配置。这里既做校验也做溯源。
    """
    issues: list[dict] = []
    try:
        serial = serial_planning_policy()
        protected = protected_paths()
        approval = default_approval_mode()
        continuation = continuation_policy()
        repair = repair_policy()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return {'source': 'shared/registries/workflow.json', 'valid': False, 'error': str(exc)}, [_issue(
            code='WORKFLOW_POLICY_INVALID', domain='project', severity='blocking',
            status='mechanical_error', object_type='registry',
            object_id='shared/registries/workflow.json', message=str(exc),
            evidence={'error': str(exc)},
            recommended_action='修复 shared/registries/workflow.json')]

    manifest_protected = toolbox_manifest().get('agileWorkbench', {}).get('protectedCheckoutPaths')
    if manifest_protected is not None and list(manifest_protected) != protected:
        issues.append(_issue(
            code='PROTECTED_PATHS_DIVERGE', domain='project', severity='blocking',
            status='mechanical_error', object_type='registry',
            object_id='workflow.json.protectedPaths',
            message='protected paths diverge: workflow.json.protectedPaths '
                    f'{protected} != manifest.agileWorkbench.protectedCheckoutPaths {list(manifest_protected)}',
            evidence={'workflowJson': protected, 'manifest': list(manifest_protected)},
            recommended_action='两处受保护路径必须一致；修正其中一处'))

    return {
        'source': 'shared/registries/workflow.json', 'valid': True, 'approvalMode': approval,
        'serialPlanning': serial, 'protectedPaths': protected,
        'continuation': continuation, 'repair': repair,
    }, issues


def workflow_integrity(root: Path, structure: dict, workflow: dict) -> list[dict]:
    """workflow 与 structure 是两处状态记录，必须交叉验证。

    structure 保存章节生命周期，workflow 保存审批与事务状态；两边都能被独立
    修改，此前没有任何检查会发现它们已经分叉，也没人发现指向不存在章节的
    artifact/approval 条目。
    """
    issues = []
    known = {item['chapterId'] for item in structure.get('chapters', [])}
    statuses = {item['chapterId']: item.get('status') for item in structure.get('chapters', [])}
    for field in ('artifacts', 'approvals', 'chapterStates'):
        for chapter_id in sorted(set(workflow.get(field, {})) - known):
            issues.append(_issue(
                code='WORKFLOW_UNKNOWN_CHAPTER', domain='workflow', severity='blocking',
                status='mechanical_error', object_type='chapter', object_id=chapter_id,
                message=f'workflow.{field} references unknown chapter: {chapter_id}',
                evidence={'field': field}, chapterId=chapter_id,
                affected_operations=['project.workflow.recover'],
                recommended_action='清理指向不存在章节的 workflow 记录'))
    for chapter_id, recorded in sorted(workflow.get('chapterStates', {}).items()):
        status = recorded.get('status') if isinstance(recorded, dict) else None
        if chapter_id in statuses and status and status != statuses[chapter_id]:
            issues.append(_issue(
                code='CHAPTER_STATE_DIVERGED', domain='workflow', severity='blocking',
                status='mechanical_error', object_type='chapter', object_id=chapter_id,
                message=f'chapter state diverged for {chapter_id}: structure={statuses[chapter_id]} workflow={status}',
                evidence={'structure': statuses[chapter_id], 'workflow': status},
                chapterId=chapter_id, affected_operations=['project.workflow.reconcile'],
                recommended_action='运行 project workflow reconcile 由确定性状态重建唯一 nextAction'))
    from .finalization import pending_transactions
    for item in pending_transactions(root):
        issues.append(_issue(
            code='TRANSACTION_INCOMPLETE', domain='workflow', severity='blocking',
            status='needs_rebuild', object_type='transaction',
            object_id=item.get('transactionId'),
            message=f"TRANSACTION_INCOMPLETE: {item.get('transactionId')} stopped at phase={item.get('phase')}; "
                    'run workflow recover',
            evidence={'phase': item.get('phase'), 'files': item.get('files', [])},
            chapterId=item.get('chapterId'),
            affected_operations=['project.workflow.recover'],
            recommended_action='运行 project workflow recover 查看，由作者确认后 --apply 回滚'))
    return issues




def _project_domain(root: Path, *, strict: bool, structure, workflow, marker) -> tuple[dict, list[dict], list[dict]]:
    issues: list[dict] = []
    sections: dict[str, Any] = {}
    if structure is None:
        issues.append(_issue(
            code='PROJECT_UNREADABLE', domain='project', severity='blocking',
            status='mechanical_error', object_type='project', object_id=str(root),
            message='结构不可读；无法评估项目健康',
            evidence={}, affected_operations=['project.doctor'],
            recommended_action='修复 .govel/structure.json 或重新初始化'))
        sections['structure'] = None
        return sections, issues, []

    if marker and 'knowledgeMode' in marker:
        from .project import resolve_knowledge_mode
        try:
            resolve_knowledge_mode(project_root=root)
        except (StructureError, ValueError) as exc:
            issues.append(_issue(
                code='KNOWLEDGE_MODE_INVALID', domain='project', severity='blocking',
                status='mechanical_error', object_type='project', object_id=structure['projectId'],
                message=str(exc), evidence={'knowledgeMode': marker['knowledgeMode']},
                affected_operations=['task.prepare', 'context.read', 'knowledge.pack'],
                recommended_action='将 project.json knowledgeMode 设为 off / broad / curated'))

    if marker and marker['projectId'] != structure['projectId']:
        issues.append(_issue(
            code='PROJECT_ID_MISMATCH', domain='project', severity='blocking',
            status='mechanical_error', object_type='project',
            object_id=structure['projectId'],
            message='projectId mismatch between marker and structure',
            evidence={'marker': marker['projectId'], 'structure': structure['projectId']},
            recommended_action='marker 与 structure 必须是同一项目；修正其一'))
    # 书名有两个合法真源，必须永远相等；不一致时不猜哪个才是真名。
    if marker and str(marker.get('bookName', '')) != str(structure.get('bookName', '')):
        issues.append(_issue(
            code='PROJECT_BOOK_NAME_DIVERGES', domain='project', severity='blocking',
            status='mechanical_error', object_type='project',
            object_id=structure['projectId'],
            message='bookName diverges between project marker and structure',
            evidence={'marker': marker.get('bookName'), 'structure': structure.get('bookName')},
            affected_operations=['refactor_rename_book'],
            recommended_action='跑 book rename 事务让两边原子对齐；不要手改其中一个文件'))
    issues.extend(prose_drift(root, structure))
    issues.extend(refactor_path_issues(root, structure))
    issues.extend(_refactor_state_issues(root))
    issues.extend(workflow_integrity(root, structure, workflow))
    if workflow.get('nextAction'):
        try:
            validate_next_action_for_project(
                workflow['nextAction'], structure, workflow,
                root=root,
                chapter_range_required=serial_planning_policy()['chapterRangeRequired'],
                current_volume_only=serial_planning_policy()['currentVolumeOnly'],
                prewarm_next_chapter=serial_planning_policy()['prewarmNextChapter'],
            )
        except WorkflowError as exc:
            issues.append(_issue(
                code=exc.code, domain='workflow', severity='blocking', status='mechanical_error',
                object_type='nextAction', object_id=str((workflow.get('nextAction') or {}).get('chapterId') or ''),
                message=f'{exc.code}: {exc}', evidence=exc.details,
                chapterId=(workflow.get('nextAction') or {}).get('chapterId'),
                affected_operations=['project.workflow.reconcile'],
                recommended_action='运行 project workflow reconcile 重建唯一 nextAction'))

    settings_report = check_settings(root, structure=structure) if structure is not None else None
    if settings_report:
        for row in settings_report['errors']:
            issues.append(_issue(
                code=row['code'], domain='project', severity='blocking', status='mechanical_error',
                object_type='settingCard', object_id=row.get('path', ''), message=f"{row['code']}: {row['path']}: {row['message']}",
                evidence=dict(row), affected_operations=['project.settings.check'],
                recommended_action='按 settings check 的报告修正设定卡片'))
        if strict and settings_report.get('warnings'):
            for row in settings_report['warnings']:
                issues.append(_issue(
                    code=row['code'], domain='project', severity='warning', status='informational',
                    object_type='settingCard', object_id=row.get('path', ''),
                    message=f"{row['code']}: {row['path']}: {row['message']}", evidence=dict(row),
                    recommended_action='设定卡片待填充或含未定字段；属作者工作，不是机械错误'))
    sections['settings'] = settings_report

    policy, policy_issues = policy_report(root, structure)
    issues.extend(policy_issues)
    sections['workflowPolicy'] = policy

    # 派生缓存：stale 是可重建状态，不是项目损坏。旧 doctor 把 cache 的 problems
    # 并入 problems（都是真错误），但把 cacheFresh 单独留作字段——这里保持同一口径，
    # 只是把「可重建」显式标成 needs_rebuild，好让它不与机械错误混为一谈。
    from .project import TOOLBOX_ROOT
    import importlib
    import sys
    _scripts = str(TOOLBOX_ROOT / 'scripts')
    _added = _scripts not in sys.path
    if _added:
        sys.path.insert(0, _scripts)
    try:
        try:
            cache = importlib.import_module('state_cache').status(root)
        except Exception as exc:  # noqa: BLE001 - 缓存不可读由下一段统一报告
            cache = {'status': 'invalid', 'cacheFresh': False, 'problems': [str(exc)]}
    finally:
        if _added:
            try:
                sys.path.remove(_scripts)
            except ValueError:
                pass
    sections['stateCache'] = cache
    for item in cache.get('problems', []):
        issues.append(_issue(
            code='STATE_CACHE_INVALID', domain='project', severity='blocking',
            status='needs_rebuild', object_type='stateCache',
            object_id='.govel/cache', message=str(item), evidence={'problem': item},
            affected_operations=['project.state.rebuild'],
            recommended_action='运行 project state rebuild 重建派生缓存'))

    # 时间线倒挂检测：非阻断字段。写到一半改时间锚点后，这里与 `structure timeline` 同源
    # 报告重排结果，避免「时间线只在专门视图里才可见」的盲区（阻断判定仍在结构事务与 timeline）。
    timeline = None
    from .commands import story_timeline
    try:
        timeline = story_timeline(root)
        if strict and any(row.get('code') == 'TIME_ORDER_INVERSION' for row in timeline.get('problems', [])):
            issues.append(_issue(
                code='TIMELINE_INVERSION', domain='planning', severity='blocking',
                status='semantic_review_required', object_type='timeline', object_id='',
                message='TIMELINE_INVERSION: 章节故事时间早于前一章；改过时间锚点后请复查 `structure timeline`',
                evidence={'problems': timeline.get('problems', [])},
                affected_operations=['project.structure.timeline'],
                recommended_action='改过时间锚点后复查 structure timeline；这是文学顺序问题，不是机械错误'))
    except (OSError, ValueError, StructureError) as exc:
        timeline = {'error': str(exc)}
        if strict:
            issues.append(_issue(
                code='TIMELINE_ERROR', domain='planning', severity='blocking',
                status='mechanical_error', object_type='timeline', object_id='',
                message=f'TIMELINE_ERROR: {exc}', evidence={'error': str(exc)},
                recommended_action='检查时间锚点与章节挂载'))
    sections['timeline'] = timeline

    pending = workflow.get('nextAction') or {}
    if policy['valid'] and policy['serialPlanning']['chapterRangeRequired'] and pending and not workflow.get('approvedChapterIds'):
        issues.append(_issue(
            code='CHAPTER_RANGE_REQUIRED', domain='planning', severity='blocking',
            status='author_decision_required', object_type='approvalScope',
            object_id=str(pending.get('chapterId') or ''),
            message='chapterRangeRequired: nextAction is set without any approved chapter range',
            evidence={'nextAction': pending}, chapterId=pending.get('chapterId'),
            affected_operations=['project.workflow.approve-chapters'],
            recommended_action='串行规划要求作者先批准章节范围'))
    return sections, issues, []


# --------------------------------------------------------------------------
# planning / writer readiness / canon / context / knowledge domains
# --------------------------------------------------------------------------

def _planning_domain(root: Path, structure: dict) -> tuple[dict, list[dict], dict]:
    issues: list[dict] = []
    objects: dict[str, Any] = {}

    from .agents_consistency import check_agents_decisions
    agents = check_agents_decisions(root, structure)
    objects['agentsConsistency'] = agents
    for row in agents['issues']:
        issues.append(_issue(
            code=row['code'], domain='planning', severity='warning', status='stale',
            object_type='authorDecision', object_id=row['decisionId'],
            message=f"AGENTS.md:{row['line']} 引用的作者决定 {row['decisionId']} 当前为 {row['status']}"
                    + ('，绑定输入已变化' if row['stale'] else ''),
            evidence=row, affected_operations=['task.prepare'],
            recommended_action='对照当前作者决定及替代关系核对这一行；历史说明与当前要求需明确区分'))

    from .planning_candidates import validate_candidates
    from .project import source_model_policy
    statuses = source_model_policy()['planningCandidates']['statuses']
    errors = validate_candidates(structure)
    for row in errors:
        issues.append(_issue(
            code=row.get('code', 'PLANNING_CANDIDATE_INVALID'), domain='planning',
            severity='blocking', status='mechanical_error', object_type='planningCandidate',
            object_id=str(row.get('candidateId') or ''),
            message=json.dumps(row, ensure_ascii=False, sort_keys=True), evidence=row,
            candidateId=row.get('candidateId'),
            affected_operations=['project.source.candidate-preview'],
            recommended_action='按候选校验错误修正 planningCandidates'))

    candidates = structure.get('planningCandidates', [])
    by_status = {status: [] for status in statuses}
    unplaced = []
    for row in candidates:
        by_status.setdefault(row.get('status'), []).append(row)
        placement = row.get('placement')
        if placement is None:
            unplaced.append(row['candidateId'])
    objects['planningCandidates'] = {
        'total': len(candidates),
        'byStatus': {key: len(value) for key, value in by_status.items()},
        'unplaced': sorted(unplaced),
        'unmaterialized': sorted(row['candidateId'] for row in candidates
                                 if row.get('status') in {'draft', 'approved'}),
    }
    for candidate_id in sorted(unplaced):
        issues.append(_issue(
            code='PLANNING_CANDIDATE_UNPLACED', domain='planning', severity='warning',
            status='author_decision_required', object_type='planningCandidate',
            object_id=candidate_id,
            message=f'规划候选还没有定位（卷/项目域、弧线、锚点）：{candidate_id}',
            evidence={}, candidateId=candidate_id,
            affected_operations=['project.source.candidate-preview'],
            recommended_action='未定位候选不能批准或物化；先定卷/项目域、弧线与锚点'))
    for row in candidates:
        if row.get('status') == 'approved':
            approval = row.get('approval') or {}
            if approval.get('verifiedHuman') is False:
                issues.append(_issue(
                    code='PLANNING_CANDIDATE_APPROVAL_UNVERIFIED', domain='planning',
                    severity='warning', status='author_decision_required',
                    object_type='planningCandidate', object_id=row['candidateId'],
                    message=f'已批准候选没有人工核验标记：{row["candidateId"]}',
                    evidence={'actorId': approval.get('actorId'), 'declaredAtRevision': approval.get('declaredAtRevision')},
                    candidateId=row['candidateId'],
                    affected_operations=['project.source.candidate-preview'],
                    recommended_action='批准记录未标记人工核验；由作者确认该批准'))

    chapters = structure.get('chapters', [])
    by_chapter_status: dict[str, list[str]] = {}
    for chapter in chapters:
        by_chapter_status.setdefault(chapter.get('status'), []).append(chapter['chapterId'])
    objects['chapters'] = {
        'total': len(chapters),
        'byStatus': {key: len(value) for key, value in sorted(by_chapter_status.items())},
        'stable': sorted(by_chapter_status.get('stable', [])),
        'stale': sorted(by_chapter_status.get('stale', [])),
        'planningRequired': sorted(by_chapter_status.get('planning_required', [])),
        'quarantined': sorted(by_chapter_status.get('quarantined', [])),
        'quarantinedReasons': {row['chapterId']: row.get('quarantineReason')
                               for row in chapters if row.get('status') == 'quarantined'},
        'archived': sorted(by_chapter_status.get('archived', [])),
    }
    for chapter_id in sorted(by_chapter_status.get('stale', [])):
        issues.append(_issue(
            code='CHAPTER_STALE', domain='planning', severity='warning', status='stale',
            object_type='chapter', object_id=chapter_id,
            message=f'章节来源已变、标记为 stale：{chapter_id}', evidence={}, chapterId=chapter_id,
            affected_operations=['project.workflow.repair-round'],
            recommended_action='来源变动使该章过期；返修后重新入库'))
    return {'applicable': True, 'candidateStatuses': statuses}, issues, objects


def _writer_domain(root: Path, structure: dict, workflow: dict, chapter_id: str | None) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    report: dict[str, Any] = {'scopedChapterId': chapter_id, 'readinessSource': 'task_prepare(writer)',
                              'applicable': chapter_id is not None}
    if chapter_id is None:
        return ({**report, 'status': 'not_applicable', 'ready': None, 'taskReady': None,
                 'blocking': [], 'missing': [], 'reason': 'no current writing task'}, [])

    # --- structured explanation (kept; does NOT define the final `ready`) ---
    # Work Voice / stale Author Decisions stay as explanation issues so the author
    # sees them, but the go/no-go comes from prepare_task, not from these alone.
    profile = structure.get('workVoiceProfile')
    report['workVoiceProfile'] = {
        'present': profile is not None,
        'status': (profile or {}).get('status'),
        'profileHash': None,
    }
    if profile is None:
        issues.append(_issue(
            code='WORK_VOICE_PROFILE_MISSING', domain='writerReadiness', severity='warning',
            status='author_decision_required', object_type='workVoiceProfile', object_id='',
            message='项目还没有作品级叙述风格档案', evidence={},
            affected_operations=['project.source.voice-profile-preview'],
            recommended_action='作品级叙述风格未登记；作者拟定六维轴向后显式批准'))
    elif profile.get('status') != 'active':
        issues.append(_issue(
            code='WORK_VOICE_PROFILE_UNAPPROVED', domain='writerReadiness', severity='warning',
            status='author_decision_required', object_type='workVoiceProfile', object_id='',
            message=f"作品叙述风格还是 {profile.get('status')}，未批准", evidence={},
            affected_operations=['project.source.voice-profile-preview'],
            recommended_action='未批准的叙述风格只作为候选注入；作者批准后才进入 APPROVED PLAN 层'))

    decisions = structure.get('authorDecisions', [])
    stale = [row['decisionId'] for row in decisions if isinstance(row, dict) and row.get('stale')]
    report['authorDecisions'] = {'total': len(decisions), 'stale': sorted(stale)}
    for decision_id in sorted(stale):
        issues.append(_issue(
            code='AUTHOR_DECISION_STALE', domain='writerReadiness', severity='warning',
            status='stale', object_type='authorDecision', object_id=decision_id,
            message=f'作者决定的作用域在批准后发生过变化：{decision_id}', evidence={},
            affected_operations=['project.source.author-decision-preview'],
            recommended_action='决定的作用域已变；由作者重新批准或废止，工具不自动续期'))

    # --- consume the OWNING readiness rule; never re-derive it here ---
    # prepare_task(task="writer") is the single authority on writer readiness: it
    # checks pending transactions, approved range, predecessor/continuity, source
    # rules, Stage A approval + plan binding, context health, prompt plan and the
    # required tool actions. The doctor mirrors its taskReady/blocking verbatim so
    # `writerReadiness.ready == prepare_task(writer).readiness.taskReady` holds for
    # the same chapter and snapshot; adding a second `if` here is forbidden.
    from .task_prepare import TaskPrepareError, prepare_task
    try:
        prep = prepare_task(root, 'writer', chapter_id=chapter_id)
    except (TaskPrepareError, StructureError, ValueError, OSError) as exc:
        report['ready'] = False
        report['taskReady'] = False
        issues.append(_issue(
            code=getattr(exc, 'code', 'WRITER_READINESS_UNAVAILABLE'), domain='writerReadiness',
            severity='blocking', status='mechanical_error', object_type='chapter', object_id=chapter_id,
            message=str(exc), evidence=getattr(exc, 'details', {}) or {}, chapterId=chapter_id,
            affected_operations=['project.prepare'],
            recommended_action='本章开工判定依赖 project prepare (writer)；按错误修复后重试'))
        return report, issues

    readiness = prep['readiness']
    report['readiness'] = readiness
    report['ready'] = bool(readiness['taskReady'])
    report['taskReady'] = bool(readiness['taskReady'])
    report['blocking'] = [row['code'] for row in prep.get('blocking', [])]
    report['missing'] = [row['code'] for row in prep.get('missing', [])]

    # Surface each owning-rule blocker as an issue so "not ready" is always
    # explainable, classified by the blocker's own kind — consumed, not re-derived.
    for row in prep.get('blocking', []):
        code = row['code']
        issues.append(_issue(
            code=code, domain='writerReadiness', severity='blocking',
            status=WRITER_BLOCKER_STATUS.get(code, 'author_decision_required'),
            object_type='chapter', object_id=chapter_id,
            message=row.get('message') or code,
            evidence=row.get('details') if isinstance(row.get('details'), dict) else {},
            chapterId=chapter_id, affected_operations=['project.prepare'],
            recommended_action='本章开工依据来自 project prepare (writer)；补齐该阻断项后重试'))
    return report, issues


def _canon_domain(root: Path, structure: dict) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    report: dict[str, Any] = {}

    from .structure_commands import list_pending_plans
    pending = [row for row in list_pending_plans(root)
               if row.get('operation') in CANON_PLAN_OPERATIONS]
    report['pendingProposals'] = [
        {'operationId': row['operationId'], 'operation': row['operation'],
         'expired': row.get('expired'), 'baseRevision': row.get('baseRevision'),
         'planHash': row.get('planHash')} for row in pending]
    for row in pending:
        collection = CANON_PLAN_OPERATIONS[row['operation']]
        if row.get('expired'):
            issues.append(_issue(
                code='CANON_PROPOSAL_EXPIRED', domain='canon', severity='warning', status='stale',
                object_type='canonProposal', object_id=row['operationId'],
                message=f'暂存的 {row["operation"]} 计划已过期：{row["operationId"]}',
                evidence={'planHash': row.get('planHash'), 'baseRevision': row.get('baseRevision')},
                affected_operations=['project.structure.plans'],
                recommended_action='过期计划不能提交；重新 resolve + preview'))
        elif row.get('baseRevision') != structure['revision']:
            issues.append(_issue(
                code='CANON_PROPOSAL_STALE', domain='canon', severity='warning', status='stale',
                object_type='canonProposal', object_id=row['operationId'],
                message=f'暂存的 {row["operation"]} 计划早于当前真源，会话已失效：{row["operationId"]}',
                evidence={'planHash': row.get('planHash'), 'baseRevision': row.get('baseRevision'),
                          'currentRevision': structure['revision']},
                sourceHash=snapshot_hash(structure),
                affected_operations=['project.structure.commit'],
                recommended_action='源在解析后发生过变化，sessionRef 已失效；重新 resolve + preview'))
        else:
            issues.append(_issue(
                code='CANON_PROPOSAL_PENDING', domain='canon', severity='warning',
                status='semantic_review_required', object_type='canonProposal',
                object_id=row['operationId'],
                message=f'{row["operation"]} 提案等待作者批准：{row["operationId"]}',
                evidence={'planHash': row.get('planHash'), 'collection': collection,
                          'baseRevision': row.get('baseRevision')},
                sourceHash=snapshot_hash(structure),
                affected_operations=['project.structure.commit'],
                recommended_action='核对计划中的旧值、证据与影响后，由作者明确批准再 commit'))
    referenced = {row['fieldId'] for row in structure.get('attributes', [])}
    field_ids = {row['fieldId'] for row in structure.get('fieldDefinitions', [])}
    if referenced & field_ids:
        issues.append(_issue(
            code='FIELD_DEFINITION_MIGRATION_FAIL_CLOSED', domain='canon', severity='informational',
            status='informational', object_type='fieldDefinition',
            object_id=','.join(sorted(referenced & field_ids)),
            message='仅修改已引用字段定义本身时拒绝；普通设定内容修订不受此提示阻断',
            evidence={'fieldIds': sorted(referenced & field_ids)},
            affected_operations=['project.source.change-preview'],
            recommended_action='继续普通内容修订；仅 FieldDefinition 语义迁移尚未开放，不要据此阻断整个 change-preview'))
    return {'applicable': True, 'pendingProposalCount': len(pending)}, issues


# context_health 的 code → 人读信息。它的 issue 行只有 code + severity + 细节，
# 直接透射会让 legacy `problems` 投影变成一串代号，作者看不出该做什么。
CONTEXT_ISSUE_MESSAGES = {
    'CONTEXT_SNAPSHOT_STALE': '上下文快照与预期 hash 不一致：输入在构建后发生过变化',
    'CONTEXT_MANDATORY_UNAVAILABLE': '上下文的必填层不可用，工位不能在缺失硬约束下开工',
    'CONTEXT_BUDGET_EXCEEDED': '上下文超出字符预算',
    'CONTEXT_SUMMARY_STALE': '故事摘要落后于当前章节，续写会缺少近期事实',
    'CONTEXT_CHAPTER_PLAN_MISSING': '本章计划未就绪，上下文缺少已批准的规划输入',
    'CONTEXT_RULES_BLOCKED': '有登记规则在本章被阻断，需先解决',
    'CONTEXT_DEPENDENCY_MISSING': '上下文依赖清单缺失或与源计划不一致，无法证明输入新鲜',
    'CONTEXT_KNOWLEDGE_PROVENANCE_INVALID': '参考资料层的来源/版本与依赖清单不符',
    'CONTEXT_KNOWLEDGE_OMITTED': '部分参考资料因预算被省略，检索结论可能不完整',
    'CONTEXT_DECISIONS_STALE': '注入的作者决定已过期，不能当作有效指导',
    'CONTEXT_DECISION_PROVENANCE_INVALID': '作者决定的版本出处与依赖清单不符',
    'CONTEXT_VOICE_PROFILE_INVALID': '作品声纹层无效或未登记批准版本',
    'CONTEXT_POV_PROVENANCE_MISSING': '做了 POV 限定却没有过滤出处，无法证明屏蔽了禁忌信息',
    'CONTEXT_LAYER_CONTRACT_INVALID': '认识层契约被破坏：某一层在冒充另一层的认识地位',
    'CONTEXT_VALUE_CONFLICT': '记忆包中同一字段出现互相冲突的值',
    'CONTEXT_DUPLICATE': '记忆包中同一字段重复出现',
    'CONTEXT_OPTIONAL_OMITTED': '可选层因预算被省略',
}


def _context_domain(root: Path, structure: dict, chapter_id: str | None) -> tuple[dict, list[dict]]:
    if chapter_id is None:
        return ({'status': 'not_applicable', 'applicable': False, 'reason': 'no target chapter'}, [])
    from .context_health import query_context_health
    try:
        health = query_context_health(root, chapterId=chapter_id)
    except (StructureError, ValueError) as exc:
        return ({'applicable': True, 'scopedChapterId': chapter_id, 'error': exc.code},
                [_issue(code=exc.code, domain='context', severity='blocking',
                        status='mechanical_error', object_type='chapter', object_id=chapter_id,
                        message=str(exc), evidence=exc.details, chapterId=chapter_id,
                        sourceHash=snapshot_hash(structure),
                        affected_operations=['project.context.read'],
                        recommended_action='修复后重试 context read；Builder 是唯一装配入口')])
    issues = []
    for row in health.get('issues', []):
        code = row['code']
        # Context Health owns the classification. The doctor consumes the issue's
        # own `status`/`recommendedAction` instead of guessing them from the code
        # substring — a missing chapter plan is an author decision, not a rebuild,
        # and a broken layer/POV/decision contract is mechanical, not a semantic
        # review. Re-deriving that here is exactly the second-runtime this module
        # exists to remove.
        status = row.get('status', 'semantic_review_required')
        severity = row.get('severity', 'warning')
        detail = {key: value for key, value in row.items()
                  if key not in {'code', 'severity', 'status', 'recommendedAction'}}
        issues.append(_issue(
            code=code, domain='context', severity=severity, status=status,
            object_type='context', object_id=chapter_id,
            message=CONTEXT_ISSUE_MESSAGES.get(code, code),
            evidence=detail, chapterId=chapter_id, sourceHash=snapshot_hash(structure),
            affected_operations=['project.context.read', 'project.source.context-health'],
            recommended_action=(row.get('recommendedAction')
                                or ('重建或刷新上下文' if severity == 'blocking'
                                    else '检查该警告是否影响本章判断'))))
    report = {'applicable': True, 'scopedChapterId': chapter_id, 'ready': health.get('ready'),
              'contextHash': health.get('contextHash'),
              'summaryFreshness': health.get('summaryFreshness'),
              'budget': health.get('budget'),
              'coverage': health.get('coverage')}
    if not issues:
        return report, []
    return report, issues


def _knowledge_domain() -> tuple[dict, list[dict]]:
    from .knowledge_store import knowledge_health
    health = knowledge_health()
    issues = []
    for row in health.get('problems', []):
        code = row['code']
        status = 'stale' if code == 'KNOWLEDGE_SOURCE_MISMATCH' else 'mechanical_error'
        asset_id = row.get('assetId') or row.get('source') or ''
        severity = 'blocking' if code in {'KNOWLEDGE_SOURCE_MISMATCH', 'KNOWLEDGE_MIGRATION_SOURCE_MISMATCH',
                                          'KNOWLEDGE_MIGRATION_ASSET_MISMATCH',
                                          'KNOWLEDGE_COMPAT_LINK_MISMATCH',
                                          'KNOWLEDGE_MIGRATION_MAP_INVALID'} else 'warning'
        issues.append(_issue(
            code=code, domain='knowledge', severity=severity, status=status,
            object_type='knowledgeAsset', object_id=asset_id,
            message=json.dumps(row, ensure_ascii=False, sort_keys=True), evidence=row,
            assetId=asset_id or None,
            affected_operations=['project.knowledge.health', 'project.knowledge.index-rebuild'],
            recommended_action='核对资料 hash 与来源路径；来源文件被改动需重新登记版本'))
    index = health.get('index') or {}
    if not index.get('current'):
        reason = index.get('reason', 'unknown')
        issues.append(_issue(
            code='KNOWLEDGE_INDEX_STALE', domain='knowledge',
            severity='blocking' if reason == 'invalid' else 'warning', status='needs_rebuild',
            object_type='knowledgeIndex', object_id='knowledge/.cache/search.sqlite3',
            message=f'资料搜索索引{reason}', evidence=index,
            affected_operations=['project.knowledge.index-rebuild'],
            recommended_action='运行 project knowledge index-rebuild 重建本地搜索索引'))
    return {'applicable': True, 'total': health.get('total'), 'active': health.get('active'),
            'index': index, 'migrationSources': health.get('migrationSources')}, issues






def _tooling_domain() -> tuple[dict, list[dict]]:
    """Check runtime wrappers without inferring an installation from Git metadata.

    Plugin caches can retain .git. Release candidates and unrelated global host
    registrations are maintenance concerns, not evidence of project health.
    The existing wrapper checker still owns runtime drift and missing checks.
    """
    import importlib
    import sys

    from .project import TOOLBOX_ROOT
    scripts_dir = str(TOOLBOX_ROOT / 'scripts')
    added = scripts_dir not in sys.path
    if added:
        sys.path.insert(0, scripts_dir)
    issues: list[dict] = []
    report: dict[str, Any] = {'toolboxRoot': str(TOOLBOX_ROOT)}
    try:
        try:
            wrappers = importlib.import_module('generate_wrappers').check()
        except Exception as exc:  # noqa: BLE001 - 维护检查缺失本身要被报出来
            wrappers = {'ok': False, 'drift': [], 'error': str(exc)}
        report['wrappers'] = wrappers
        for path in wrappers.get('drift', []):
            issues.append(_issue(
                code='WRAPPER_DRIFT', domain='tooling', severity='warning', status='needs_rebuild',
                object_type='wrapper', object_id=path,
                message=f'客户端包装与技能清单漂移：{path}',
                evidence={'path': path}, affected_operations=['project.tools.station-cards'],
                recommended_action='运行 python3 scripts/generate_wrappers.py 重新生成'))
        if wrappers.get('error'):
            issues.append(_issue(
                code='WRAPPER_CHECK_FAILED', domain='tooling', severity='warning',
                status='mechanical_error', object_type='wrapper', object_id='generate_wrappers.py',
                message=str(wrappers['error']), evidence={'error': wrappers['error']},
                recommended_action='检查 generate_wrappers.py 与 toolbox.manifest.json'))

        report['releaseContents'] = {
            'applicable': False, 'ok': None, 'status': 'not_applicable',
            'reason': '发布候选由 scripts/check_release_contents.py --public 单独核验',
        }
        report['antigravityInstall'] = {
            'applicable': False, 'ok': None, 'status': 'not_applicable',
            'hostConnection': 'not_observed',
            'reason': '全局软链由 scripts/install_antigravity.py --check 单独核验；当前宿主连接需另行验证',
        }
    finally:
        if added:
            try:
                sys.path.remove(scripts_dir)
            except ValueError:
                pass
    return {'applicable': True, **report}, issues


# --------------------------------------------------------------------------
# aggregator
# --------------------------------------------------------------------------

def _stable_chapters_domain(root: Path, structure: dict, workflow: dict) -> tuple[dict, list[dict]]:
    """Consume current closeout receipts without rerunning historical Writer gates."""
    from .chapter_receipt import _chapter_receipt
    from .continuity import finalization_evidence
    from .prose_binding import OWNED_CLEAN, observation_error_code

    issues, chapters = [], []
    for chapter in ordered_chapters(structure):
        if chapter.get('status') == 'quarantined':
            chapters.append({'chapterId': chapter['chapterId'], 'status': 'quarantined',
                             'quarantineReason': chapter.get('quarantineReason'),
                             'excludedFromContinuation': True})
            continue
        if chapter.get('status') != 'stable':
            continue
        chapter_id = chapter['chapterId']

        def problem(code, message, evidence, status='author_decision_required', recommended_action=None):
            issues.append(_issue(code=code, domain='workflow', severity='blocking', status=status,
                                 object_type='chapter', object_id=chapter_id, chapterId=chapter_id,
                                 message=message, evidence=evidence,
                                 affected_operations=['project.workflow.chapter-receipt'],
                                 recommended_action=recommended_action or '按本章 chapter-receipt 处理当前归属或收尾待办；历史审批不自动重批'))

        try:
            receipt = _chapter_receipt(root, structure, workflow, chapter, view='model')
        except (ValueError, OSError) as exc:
            problem(getattr(exc, 'code', 'CHAPTER_RECEIPT_UNAVAILABLE'), str(exc),
                    getattr(exc, 'details', {}) or {}, 'mechanical_error')
            continue
        ownership = receipt['ownership'] or {}
        continuity = receipt['continuity']
        inventory = receipt['factInventory'] or {}
        chapters.append({'chapterId': chapter_id, 'ownership': ownership.get('state'),
                         'continuity': continuity, 'factInventory': inventory,
                         'readyForContinuation': receipt['readyForContinuation']})
        if ownership.get('state') != OWNED_CLEAN:
            problem(observation_error_code(ownership.get('state')),
                    '已入库正文的当前归属或字节需要处理', ownership, 'mechanical_error')
        else:
            try:
                chapters[-1]['finalizationEvidence'] = finalization_evidence(root, structure, workflow, chapter)
            except (ValueError, OSError) as exc:
                problem(getattr(exc, 'code', 'FINALIZATION_EVIDENCE_UNAVAILABLE'), str(exc),
                        getattr(exc, 'details', {}) or {}, 'mechanical_error',
                        '核对原定稿事务与封存工件；不要补写 finalized 或重批旧工件来替代缺失证据')
        if continuity.get('status') != 'complete':
            problem('CONTINUITY_REQUIRED', '已入库章节的连续性尚未确认完成', continuity)
        if not inventory.get('complete'):
            for blocker in inventory.get('blockers') or [{'code': 'FACT_INVENTORY_UNAVAILABLE'}]:
                problem(blocker['code'], blocker.get('message') or '已入库章节的事实盘点或回流证据尚未完成',
                        blocker, 'semantic_review_required')
    return {'applicable': bool(chapters or issues), 'readinessSource': 'chapter_receipt',
            'chapters': chapters}, issues


def assess_project(root: Path, *, strict: bool = False, chapter_id: str | None = None) -> dict:
    """Aggregate every health domain into one report.

    Domains are assessed independently so each blocker retains its owning rule and action.
    """
    root = Path(root).expanduser().resolve()
    marker = None
    structure = None
    workflow = {}
    load_error = None
    try:
        marker = _project_json(root)
        structure = load_structure(root)
        workflow = _read_workflow(root)
    except (StructureError, OSError, json.JSONDecodeError, ValueError) as exc:
        load_error = str(exc)

    domains: dict[str, dict] = {}
    issues: list[dict] = []
    sections: dict[str, Any] = {}
    objects: dict[str, Any] = {}

    project_sections, project_issues, _ = _project_domain(
        root, strict=strict, structure=structure, workflow=workflow, marker=marker)
    sections.update(project_sections)
    issues.extend(project_issues)
    if load_error:
        issues.append(_issue(
            code='PROJECT_LOAD_FAILED', domain='project', severity='blocking',
            status='mechanical_error', object_type='project', object_id=str(root),
            message=load_error, evidence={'error': load_error},
            recommended_action='项目结构或 marker 不可读；先修复再谈其他域'))

    target = chapter_id or ((workflow.get('nextAction') or {}).get('chapterId'))
    # Explicit chapter checks retain Writer's strict revision gates. Default
    # scope follows unfinished work; a stable continuity task is not drafting.
    scope = target
    if chapter_id is None and structure is not None:
        statuses = {row['chapterId']: row.get('status') for row in structure.get('chapters', [])}
        if scope is None or statuses.get(scope) in {'stable', 'archived'}:
            scope = next((row['chapterId'] for row in ordered_chapters(structure)
                          if row.get('status') != 'stable'), None)
    if target is None and structure is not None:
        pending = [row for row in structure.get('chapters', []) if row.get('status') in {'draft', 'review'}]
        target = pending[0]['chapterId'] if pending else None

    if structure is not None:
        for domain, section_key, producer in (
            ('planning', 'planning', lambda: _planning_domain(root, structure)),
            ('writerReadiness', 'writerReadiness', lambda: _writer_domain(root, structure, workflow, scope)),
            ('canon', 'canon', lambda: _canon_domain(root, structure)),
            ('context', 'context', lambda: _context_domain(root, structure, scope)),
            ('workflow', 'workflowHealth', lambda: _stable_chapters_domain(root, structure, workflow)),
        ):
            section, domain_issues, extra = _safe_domain(domain, producer)
            sections[section_key] = section
            issues.extend(domain_issues)
            if extra:
                objects.update(extra)
    else:
        # 结构不可读时这些域根本没有被评估；报 healthy 就是用绿点掩盖没人看过。
        for domain in ('planning', 'writerReadiness', 'canon', 'context'):
            sections[domain] = {'status': 'unknown', 'applicable': False,
                                'reason': 'structure is unreadable'}

    knowledge_section, knowledge_issues, _ = _safe_domain('knowledge', _knowledge_domain)
    sections['knowledge'] = knowledge_section
    issues.extend(knowledge_issues)
    tooling_section, tooling_issues, _ = _safe_domain('tooling', _tooling_domain)
    sections['tooling'] = tooling_section
    issues.extend(tooling_issues)
    # _project_domain emits issues owned by several domains. They were already
    # appended above; append neither them nor a filtered copy a second time.
    project_section, _, _ = _safe_domain('project', lambda: (project_sections, [], []))
    sections['project'] = project_section

    if structure is None:
        sections['project'] = {'status': 'blocked', 'applicable': True,
                               'reason': 'structure is unreadable'}
    domains = {domain: _finalize_domain(domain, sections.get('workflowHealth' if domain == 'workflow' else domain, {}),
                                        [row for row in issues if row['domain'] == domain])
               for domain in DOMAINS}
    domains['project'] = _finalize_domain('project', project_section,
                                          [row for row in issues if row['domain'] == 'project'])

    suggested_actions = _suggested_actions(root, structure, workflow, target)

    blocking = [row for row in issues if row['severity'] == 'blocking']
    integrity_issues = [row for row in issues if row['status'] == 'mechanical_error']
    authoring_issues = [row for row in blocking if row['status'] != 'mechanical_error']
    actionable = [row for row in issues if row['severity'] != 'informational']
    report = {
        'ok': True,
        'code': 'DOCTOR_OK' if not blocking else 'DOCTOR_ERRORS',
        'healthy': not blocking,
        'dataIntegrity': {'healthy': not integrity_issues, 'issueCount': len(integrity_issues)},
        'authoringReadiness': {'ready': not blocking, 'pendingCount': len(authoring_issues)},
        'assessment': ('data_error' if integrity_issues else 'authoring_pending' if blocking else 'ready'),
        'message': ('数据或工程完整性需要处理。' if integrity_issues else
                    '项目数据检查通过；仍有创作准备或作者决定待完成，不能据此判为导入失败。' if blocking else '当前检查通过。'),
        # Legacy projection: warnings and blockers only, so `problems` stays a
        # list of things the author might act on. Purely informational rows
        # ("this domain does not apply to a Markdown project") live in `issues`.
        # The structured issues are the business truth.
        'problems': [row['message'] for row in actionable],
        'blockingProblems': [row['message'] for row in blocking],
        'schemaVersion': 2,
        'project': marker,
        'structureRevision': structure.get('revision') if structure else None,
        'workflow': workflow,
        'domains': domains,
        'issues': issues,
        'issueCounts': {
            'total': len(issues),
            'blocking': len(blocking),
            'warning': len([row for row in issues if row['severity'] == 'warning']),
            'informational': len([row for row in issues if row['severity'] == 'informational']),
        },
        'scopedChapterId': scope,
        'lifecycleObjects': objects,
        'suggestedActions': suggested_actions,
        'strict': bool(strict),
    }
    report.update(sections)
    if structure is not None and (root / '.govel/import-receipt.json').exists():
        from .import_status import status_from_source
        try:
            report['importFollowup'] = status_from_source(root, structure, workflow)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            report['importFollowup'] = {'ok': False, 'code': getattr(exc, 'code', 'IMPORT_FOLLOWUP_UNAVAILABLE'),
                                       'message': str(exc)}
    return report


def _suggested_actions(root: Path, structure, workflow, target) -> list[dict]:
    """运行反馈途径：有界、只读的能力建议（发现工具，不授权执行）。

    用默认任务与当前 nextAction 的章节做范围，未初始化或结构损坏时返回空表而不是猜测。
    """
    if structure is None:
        return []
    try:
        from .tool_plan import suggested_actions as suggest
    except ImportError:
        return []
    focus = target
    if focus is None:
        pending = [row for row in structure.get('chapters', []) if row.get('status') in {'draft', 'review'}]
        focus = pending[0]['chapterId'] if pending else None
    try:
        return suggest(root, 'editor' if focus else 'planner', chapter_id=focus)
    except Exception:  # noqa: BLE001 - 建议失败绝不让 doctor 失败
        return []


def project_doctor(root: Path, *, strict: bool = False) -> dict:
    """Compatibility facade over :func:`assess_project`.

    Existing callers (novelctl `project doctor`, dashboard `/api/doctor`,
    tests) import this name. It returns the full multi-domain report; the
    legacy `problems` / `healthy` keys are projections of the same data.
    """
    return assess_project(root, strict=strict)
