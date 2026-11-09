"""Read-only validation and preparation shared by receipts and finalization."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import sha256_bytes, sha256_json, sha256_text
from .jsonio import read_json
from .paths import ProjectPathError, safe_project_path
from .review_artifacts import ReviewArtifactError, validate_artifact_binding
from .structure_store import StructureError, load_structure, validate_structure
from .workflow import WorkflowError, transition_chapter_status


@dataclass
class FinalizationPreflight:
    blockers: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def ownership(self) -> dict[str, Any] | None:
        """The current-binding ownership receipt, or None on a legacy-lineage schema.

        Exposed so the caller can read the chapter's binding generation instead of
        deriving ownership itself（契约 §16/§21）。
        """
        return self.context.get("ownership")

    def block(self, code: str, message: str, **details: Any) -> None:
        item = {"code": code, "message": message, "details": details}
        if item not in self.blockers:
            self.blockers.append(item)

    def error(self, exc: Exception, fallback: str, **details: Any) -> None:
        self.block(getattr(exc, "code", fallback), str(exc), **{**getattr(exc, "details", {}), **details})

    def raise_if_blocked(self) -> None:
        if self.blockers:
            first = self.blockers[0]
            raise WorkflowError(first["code"], first["message"], {**first["details"], "blockers": self.blockers})


def _artifacts(root: Path, workflow: dict, chapter_id: str, project_id: str,
               required: tuple[str, ...], result: FinalizationPreflight) -> dict[str, dict]:
    from .finalization import REQUIRED_FINAL_ARTIFACTS, _artifact_path

    registrations = workflow.get("artifacts", {}).get(chapter_id, {})
    approval = workflow.get("approvals", {}).get(chapter_id, {})
    if not isinstance(registrations, dict) or not isinstance(approval, dict):
        result.block("WORKFLOW_STATE_INVALID", "chapter artifacts and approval must be objects", chapterId=chapter_id)
        return {}
    hashes = approval.get("artifactHashes", {})
    if not isinstance(hashes, dict):
        result.block("WORKFLOW_STATE_INVALID", "approval artifactHashes must be an object", chapterId=chapter_id)
        hashes = {}
    missing = sorted(set(required) - set(registrations))
    if missing:
        result.block("FINAL_ARTIFACTS_MISSING", "required finalization artifacts are missing",
                     chapterId=chapter_id, missing=missing)
    artifacts = {}
    kinds = list(dict.fromkeys((*REQUIRED_FINAL_ARTIFACTS, "title_candidates", *sorted(registrations))))
    for kind in kinds:
        entry = registrations.get(kind)
        row = {"artifactType": kind, "required": kind in required, "approvalRequired": kind in required,
               "registered": kind in registrations, "approved": False, "fresh": False,
               "artifactSha256": entry.get("artifactSha256") if isinstance(entry, dict) else None,
               "artifactPath": entry.get("artifactPath") if isinstance(entry, dict) else None,
               "approvedArtifactSha256": hashes.get(kind),
               "invalidated": bool(isinstance(entry, dict) and entry.get('invalidatedBy'))}
        result.artifacts.append(row)
        if kind not in registrations:
            continue
        if not isinstance(entry, dict):
            result.block("ARTIFACT_INVALID", "artifact registration must be an object", artifactType=kind)
            continue
        details = {'chapterId': chapter_id, 'artifactType': kind,
                   'artifactPath': row['artifactPath'], 'registeredArtifactSha256': row['artifactSha256']}
        row["approved"] = bool(not row['invalidated'] and approval.get("approved")
                               and hashes.get(kind) and hashes[kind] == entry.get("artifactSha256"))
        if row['invalidated']:
            row['invalidatedBy'] = deepcopy(entry['invalidatedBy'])
            if row['required']:
                result.block('ARTIFACT_SOURCE_CHANGED', 'registered artifact dependencies were invalidated',
                             **details, reason='dependencies_invalidated', invalidatedBy=row['invalidatedBy'])
        try:
            path = _artifact_path(root, entry)
            # Hash and parse the same bytes, even if a producer replaces the file.
            raw = path.read_bytes()
            row["fileSha256"] = sha256_bytes(raw)
            row["fresh"] = not entry.get("artifactFileSha256") or row["fileSha256"] == entry["artifactFileSha256"]
            if not row["fresh"]:
                result.block("ARTIFACT_SOURCE_CHANGED", "registered artifact source changed", **details,
                             reason='artifact_file_changed', expectedFileSha256=entry.get('artifactFileSha256'),
                             observedFileSha256=row['fileSha256'])
            data = json.loads(raw.decode("utf-8"))
            validate_artifact_binding(data, project_id=project_id, chapter_id=chapter_id)
            if data.get("artifactType") != kind:
                result.block("ARTIFACT_TYPE_MISMATCH", "registered artifact type does not match its envelope", artifactType=kind, actual=data.get("artifactType"))
                continue
            row["envelopeValid"] = data.get("artifactSha256") == entry.get("artifactSha256")
            if not row["envelopeValid"]:
                result.block("ARTIFACT_APPROVAL_HASH_MISMATCH", "registered artifact hash changed", **details,
                             reason='registered_envelope_changed', observedArtifactSha256=data.get('artifactSha256'))
            row["payload"] = data["payload"]
            artifacts[kind] = data
        except (WorkflowError, ProjectPathError, ReviewArtifactError, OSError, ValueError, TypeError) as exc:
            row["error"] = str(exc)
            row["envelopeValid"] = False
            result.error(exc, "ARTIFACT_SOURCE_MISSING" if isinstance(exc, OSError) else "ARTIFACT_INVALID", **details)
    if not approval.get("approved"):
        result.block("ARTIFACTS_NOT_APPROVED", "finalization requires approved artifacts",
                     chapterId=chapter_id, artifactTypes=[kind for kind in required if kind in registrations],
                     reason='chapter_approval_not_current')
    # Author approval of a stage B report covers the exact diagnostics that it reviewed.
    # This records coverage, never manufactures a standalone diagnostics approval.
    stage_b = artifacts.get('stage_b_review')
    diagnostics = artifacts.get('diagnostics')
    by_kind = {row['artifactType']: row for row in result.artifacts}
    covered = bool(stage_b and diagnostics and approval.get('approved')
                   and all(by_kind[kind].get('fresh') and by_kind[kind].get('envelopeValid')
                           and not by_kind[kind].get('invalidated') for kind in ('stage_b_review', 'diagnostics'))
                   and hashes.get('stage_b_review') == stage_b['artifactSha256']
                   and stage_b['payload'].get('diagnosticsArtifactSha256') == diagnostics['artifactSha256'])
    if covered:
        for row in result.artifacts:
            if row['artifactType'] == 'diagnostics':
                row['approvalCoveredBy'] = 'stage_b_review'
                row['approvalRequired'] = False
    for kind in required:
        if kind == 'diagnostics' and covered: continue
        entry = registrations.get(kind)
        if isinstance(entry, dict) and (not hashes.get(kind) or hashes[kind] != entry.get("artifactSha256")):
            result.block("ARTIFACT_APPROVAL_HASH_MISMATCH", "approved artifact set is incomplete or stale",
                         chapterId=chapter_id, artifactType=kind, artifactPath=entry.get('artifactPath'),
                         registeredArtifactSha256=entry.get('artifactSha256'), approvedArtifactSha256=hashes.get(kind),
                         reason='approval_missing' if not hashes.get(kind) else 'approved_version_changed')
    return artifacts




def _closeout_recovery(root, structure, workflow, chapter, preflight, blockers):
    """Attach domain-owned recovery to the original blockers, never execute it.

    Read actions are usable now. Conditional actions retain exact parameters but
    are not permission or a claim that their material/review conditions hold.
    """
    from .artifacts import sha256_json
    from .chapter_actions import describe_action

    cid = chapter['chapterId']
    by_kind = {row['artifactType']: row for row in preflight.artifacts}
    inferred_types = {
        'STAGE_B_NOT_READY': 'stage_b_review', 'STAGE_B_PROSE_MISMATCH': 'stage_b_review',
        'STAGE_A_NOT_ADOPTED': 'stage_a_review', 'STAGE_A_OUTLINE_MISMATCH': 'stage_a_review',
        'OUTLINE_BASE_MISMATCH': 'state_candidate', 'STATE_BASE_MISMATCH': 'state_candidate',
        'DESIGN_BASE_MISMATCH': 'state_candidate', 'PROSE_HASH_MISMATCH': 'state_candidate',
        'PROSE_PAYLOAD_INVALID': 'candidate_prose',
    }
    for blocker in blockers:
        details = blocker.setdefault('details', {})
        details.setdefault('chapterId', cid)
        kind = details.get('artifactType') or inferred_types.get(blocker['code'])
        if kind:
            details.setdefault('artifactType', kind)
            row = by_kind.get(kind, {})
            for key in ('artifactPath', 'artifactSha256'):
                if row.get(key) is not None:
                    details.setdefault(key, row[key])

    def action(operation, arguments=None, *, when=None, needs_approval=False, missing=()):
        value = describe_action(root, operation, arguments or {}, needs_approval=needs_approval,
                                semantic_inputs=missing)
        if when is not None:
            value.update(when=when, executable=False)
            if value['execution'] == 'read_only':
                value['execution'] = 'condition_required'
        return value

    plan_read = lambda: action('source.chapter-plan', {'chapterId': cid, 'view': 'detail'})
    definition = lambda operation: action('capabilities.list', {'operation': operation})
    state_id = None

    def state_action():
        nonlocal state_id
        if state_id is None:
            state_id = 'closeout-state-' + sha256_json({'projectId': structure['projectId'], 'chapterId': cid,
                                                       'source': structure, 'workflow': workflow})
        return action('workflow.state-prepare', {'chapterId': cid, 'requestId': state_id},
                      when='after_current_candidate_checked_and_candidate_write_authorized')

    def current_artifact(kind):
        row = by_kind.get(kind, {})
        if not (row.get('registered') and row.get('fresh') and row.get('envelopeValid')
                and not row.get('invalidated') and row.get('artifactSha256')):
            return False
        ignored = {'ARTIFACTS_NOT_APPROVED', 'ARTIFACT_APPROVAL_HASH_MISMATCH',
                   'TITLE_DECISION_REQUIRED', 'TITLE_CANDIDATE_UNKNOWN'}
        return not any(b['code'] not in ignored and b.get('details', {}).get('artifactType') == kind
                       for b in blockers)

    for blocker in blockers:
        code, details = blocker['code'], blocker['details']
        kind = details.get('artifactType')
        recovery = {'readActions': [], 'conditionalActions': [], 'automatic': False}
        if code in {'PROSE_TARGET_NOT_OWNED', 'PROSE_LOCATOR_CONFLICT', 'PROSE_LOCATOR_DRIFT',
                    'PROSE_EXTERNAL_MODIFICATION', 'OWNERSHIP_BINDING_INVALID', 'OWNERSHIP_CONFIRMATION_REQUIRED'}:
            ownership = preflight.ownership or {}
            details.setdefault('currentLocator', ownership.get('currentLocator', chapter.get('filePath')))
            details.setdefault('observedState', ownership.get('state'))
            recovery.update(needsAuthorDecision=True, missingInputs=['author_prose_target_decision'],
                instruction='保留目标文件并核对来源和当前归属，交作者决定受控处理；不删除、覆盖或自动接管。首次定稿会从候选工件创建正式正文，不要提前同步到正式目标；字节相同也不授予归属。')
        elif code in {'ARTIFACT_APPROVAL_HASH_MISMATCH', 'ARTIFACTS_NOT_APPROVED'}:
            kinds = [kind] if kind else details.get('artifactTypes', [])
            approvable = [item for item in kinds if current_artifact(item)]
            recovery['instruction'] = '只核对列出的当前工件；完成实际审查并取得对应批准后再执行批准动作。失效或损坏工件先重建／重审，不能重批旧版本。'
            for item in approvable:
                row = by_kind[item]
                recovery['conditionalActions'].append(action('project.workflow.artifact-approve',
                    {'chapterId': cid, 'artifactHash': [item + '=' + row['artifactSha256']]},
                    when='after_this_exact_artifact_reviewed_and_author_approved', needs_approval=True))
            if not approvable:
                recovery['missingInputs'] = ['current_reviewed_artifacts']
        elif code == 'FINAL_ARTIFACTS_MISSING':
            missing = details.get('missing', [])
            recovery['instruction'] = '补齐列出的工件；工具可以生成状态绑定，正文、审校和标题内容仍由工位提供，登记后按实际内容批准。'
            if 'state_candidate' in missing:
                recovery['conditionalActions'].append(state_action())
            if 'diagnostics' in missing:
                recovery['conditionalActions'].append(action('diagnostics.aggregate',
                    {'chapterId': cid, 'register': True, 'view': 'model'},
                    when='after_current_candidate_and_run_inputs_checked_and_candidate_write_authorized'))
            for item in ('stage_a_review', 'stage_b_review'):
                if item in missing:
                    recovery['conditionalActions'].append(action('workflow.review-form',
                        {'chapterId': cid, 'artifactType': item}, when='after_required_current_materials_available'))
            if set(missing) & {'candidate_prose', 'title_candidates'}:
                recovery['readActions'].append(definition('workflow.submit'))
                recovery['missingInputs'] = [item + '_content' for item in ('candidate_prose', 'title_candidates') if item in missing]
        elif code in {'STATE_BASE_MISMATCH', 'DESIGN_BASE_MISMATCH', 'PROSE_HASH_MISMATCH', 'OUTLINE_BASE_MISMATCH'}:
            recovery['readActions'] = [plan_read()]
            recovery['conditionalActions'] = [state_action()]
            recovery['instruction'] = '状态工件绑定已过期；核对当前计划与候选后由工具重新生成，再按新工件实际批准，不手改 hash。'
        elif code in {'DIAGNOSTICS_STALE', 'DIAGNOSTICS_PROSE_MISMATCH', 'DIAGNOSTICS_UNAVAILABLE'}:
            recovery['readActions'] = [plan_read()]
            recovery['conditionalActions'] = [action('diagnostics.aggregate',
                {'chapterId': cid, 'register': True, 'view': 'model'},
                when='after_current_candidate_and_run_inputs_checked_and_candidate_write_authorized')]
            recovery['instruction'] = '先核对本章当前候选与运行参数，再重新运行并登记机械诊断；新诊断不自动获得旧批准，须核对其结果与阶段 B 的绑定。'
        elif code in {'TITLE_DECISION_REQUIRED', 'TITLE_CANDIDATE_UNKNOWN', 'TITLE_CANDIDATES_MISSING'}:
            candidates = ((by_kind.get('title_candidates') or {}).get('payload') or {}).get('candidates', [])
            recovery.update(needsAuthorDecision=True, missingInputs=['titleCandidateId_or_explicit_skipTitle'],
                instruction='由作者选定当前标题候选，或明确跳过定名；不要猜选项。选择后重新准备收尾。')
            if candidates:
                details['availableCandidates'] = [
                    {key: row[key] for key in ('candidateId', 'title') if key in row}
                    for row in candidates if isinstance(row, dict)]
            recovery['readActions'] = [definition('workflow.closeout-prepare')]
            recovery['conditionalActions'] = [action('workflow.closeout-prepare', {'chapterId': cid},
                when='after_author_title_decision', missing=('titleCandidateId_or_explicit_skipTitle',))]
        elif code in {'STAGE_B_NOT_READY', 'DIAGNOSTICS_NOT_PASSED'}:
            recovery['readActions'] = [action('workflow.repair-context', {'chapterId': cid})]
            recovery['instruction'] = '读取当前问题与原文证据，完成所需修订和审查；机械或语义未通过不能仅靠重新批准解决。'
        elif code in {'STAGE_A_NOT_ADOPTED', 'STAGE_A_OUTLINE_MISMATCH', 'STAGE_B_PROSE_MISMATCH'}:
            review_kind = 'stage_a_review' if code.startswith('STAGE_A_') else 'stage_b_review'
            recovery['readActions'] = [plan_read()]
            recovery['conditionalActions'] = [action('workflow.review-form',
                {'chapterId': cid, 'artifactType': review_kind}, when='after_changed_materials_read')]
            recovery['instruction'] = '按当前材料重新取得表单并实际复核受影响结论；不要重绑旧审查或只修改 hash。'
        elif code == 'TRANSACTION_INCOMPLETE':
            recovery['readActions'] = [definition('project.workflow.recover')]
            recovery['conditionalActions'] = [action('project.workflow.recover', {'apply': False},
                when='after_pending_transaction_information_checked')]
            recovery['instruction'] = '先核对本回执列出的未完成事务与恢复参数，再读取恢复预览；本回执不执行恢复或回滚。'
        elif code == 'SOURCE_RULES_BLOCKED':
            recovery['readActions'] = [action('source.rules', {'chapterId': cid, 'action': 'publish'})]
            recovery['instruction'] = '读取发布规则的具体阻断及适用范围，按实际要求处理。'
        else:
            recovery.update(missingInputs=['resolution:' + code],
                            instruction='核对该项原始错误及具体范围；未提供可执行修复时保持待处理，不推断通过。')
        blocker['recovery'] = recovery

    return {'blockerCount': len(blockers),
            'proseTarget': {'path': chapter.get('filePath'), 'state': (preflight.ownership or {}).get('state'),
                'finalizeWritesFormalProse': True,
                'instruction': '候选保存在工作台；首次定稿由工具同时写正式正文并建立归属，不预写或同步正式目标。'},
            'continuity': {'inputAlternatives': ['summary', 'summaryUpdate'], 'confirmationFileRequired': False,
                'instruction': '收尾直接提交实际 summary 处置或 summaryUpdate 内容；工具绑定摘要版本与连续性参数，无需自建确认文件路径。approveContinuity 只沿用明确覆盖本次确认的作者授权。'}}

def preflight_finalization(root: Path, chapter_id: str, *, title_candidate_id: str | None = None,
                           skip_title: bool = False, check_lock: bool = True,
                           binding_generation: int | None = None,
                           structure: dict | None = None) -> FinalizationPreflight:
    """Inspect disk without locks, cache rebuilds, or writes; readiness is a snapshot.

    Title inputs have exactly the same meaning as finalize_chapter. A previous
    titleDecision is history, not authorization to reuse a choice on new artifacts.
    check_lock=False is reserved for the finalizer already holding project_lock.
    structure is an already validated snapshot the caller loaded (a per-chapter
    aggregate); every check, including the state/design projections, reads it
    instead of reloading. It is never mutated.
    """
    from .finalization import (REQUIRED_FINAL_ARTIFACTS, _load_workflow,
                               _resolve_title, pending_transactions)
    from .locks import project_lock_observation
    try:
        from scripts import state_cache
    except (ModuleNotFoundError, ImportError):
        import state_cache

    root = root.expanduser().resolve()
    result = FinalizationPreflight()
    try:
        lock_path = safe_project_path(root, ".gnovel/structure.lock")
        safe_project_path(root, ".gnovel/transactions")
        if check_lock and project_lock_observation(root)["locked"]:
            result.block("PROJECT_LOCKED", "another client is changing this project")
        result.transactions = pending_transactions(root)
        if result.transactions:
            result.block("TRANSACTION_INCOMPLETE", "an earlier finalization did not finish; run workflow recover first",
                         transactions=[item.get("transactionId") for item in result.transactions])
    except (ProjectPathError, OSError, ValueError) as exc:
        result.error(exc, "TRANSACTION_INVALID")
    if structure is None:
        try:
            structure = load_structure(root)
        except (StructureError, OSError, ValueError, TypeError) as exc:
            result.error(exc, "STRUCTURE_INVALID")
            return result
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        result.block("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}")
        return result
    current_binding = True
    if chapter.get("status") != "review":
        result.block("INVALID_STATE_TRANSITION", "only a chapter in review can be finalized", status=chapter.get("status"))
    from .source_rules import require_rules
    try:
        require_rules(structure, chapter_id, 'publish', root=root)
    except StructureError as exc:
        result.error(exc, 'SOURCE_RULES_BLOCKED')
    workflow = None
    try:
        workflow = _load_workflow(root)
    except (WorkflowError, OSError, ValueError, TypeError) as exc:
        result.error(exc, "WORKFLOW_STATE_INVALID")
    artifacts = {}
    if workflow is not None:
        from .continuity import require_predecessors_ready
        try:
            require_predecessors_ready(structure, workflow, chapter, root=root, formal=True)
        except ValueError as exc:
            result.error(exc, "CONTINUITY_REQUIRED")
        if chapter_id not in workflow.get("approvedChapterIds", []):
            result.block("CHAPTER_OUTSIDE_APPROVED_RANGE", f"chapter is not approved: {chapter_id}")
        required = REQUIRED_FINAL_ARTIFACTS if skip_title else (*REQUIRED_FINAL_ARTIFACTS, "title_candidates")
        artifacts = _artifacts(root, workflow, chapter_id, structure["projectId"], required, result)
    payloads = {kind: item["payload"] for kind, item in artifacts.items()}
    stage_b = payloads.get("stage_b_review")
    if stage_b is not None and stage_b.get("verdict") != "POLISH_READY":
        result.block("STAGE_B_NOT_READY", "stage B review must be POLISH_READY before finalization")
    prose_sha = None
    candidate = payloads.get("candidate_prose")
    if "candidate_prose" in payloads:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("text"), str) or not candidate["text"].strip():
            result.block("PROSE_PAYLOAD_INVALID", "candidate_prose payload.text must be non-empty")
        else:
            prose_sha = sha256_text(candidate["text"])
            from .writing_notes import validate_candidate_binding
            try:
                binding_status = validate_candidate_binding(root, structure, workflow, chapter_id, candidate)
                result.context['candidatePlanBinding'] = binding_status
                from .current_candidate import require_candidate_selection
                result.context['candidateSelectionGuards'] = require_candidate_selection(root, structure, chapter_id, candidate)['guards']
                if candidate.get('candidateRecipe') is not None:
                    from .candidate_batch import recipe_guards
                    result.context['candidateSelectionGuards'].extend(recipe_guards(root, structure, workflow, chapter_id, candidate['candidateRecipe']))
            except (ReviewArtifactError, StructureError, OSError) as exc:
                result.error(exc, 'CANDIDATE_PLAN_STALE', artifactType='candidate_prose')
    diagnostics = payloads.get("diagnostics")
    if diagnostics is not None:
        from .mechanical_evidence import evidence_current
        diagnostic_details = {'chapterId': chapter_id, 'artifactType': 'diagnostics',
                              'artifactPath': next(row['artifactPath'] for row in result.artifacts
                                                   if row['artifactType'] == 'diagnostics')}
        try:
            current = evidence_current(root, chapter_id, structure=structure)
            result.context['diagnosticsCurrent'] = current
            if not current['fresh']:
                result.block('DIAGNOSTICS_STALE', current['reason'], **diagnostic_details,
                             reason=current['reason'])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result.error(exc, 'DIAGNOSTICS_UNAVAILABLE', **diagnostic_details, assessment='unknown')
        if diagnostics.get("passed") is not True:
            result.block("DIAGNOSTICS_NOT_PASSED", "mechanical diagnostics did not pass", **diagnostic_details)
        if prose_sha is not None and diagnostics.get("draftSha256") != prose_sha:
            result.block("DIAGNOSTICS_PROSE_MISMATCH", "diagnostics were produced for a different draft",
                         **diagnostic_details, expected=prose_sha, actual=diagnostics.get("draftSha256"))
    # Legacy stage B envelopes omit this field. When supplied it is binding,
    # never advisory; diagnostics and state_candidate always require prose hashes.
    if stage_b is not None and "finalProseSha256" in stage_b and prose_sha is not None and stage_b["finalProseSha256"] != prose_sha:
        result.block("STAGE_B_PROSE_MISMATCH", "stage B review is not bound to candidate prose",
                     expected=prose_sha, actual=stage_b["finalProseSha256"])
    if stage_b is not None and prose_sha is not None:
        from .review_artifacts import validate_stage_b_completion
        try:
            validate_stage_b_completion(stage_b, candidate['text'])
            from .cognition_review import validate_review as validate_cognition_review
            result.context['cognitionSourceGuards'] = validate_cognition_review(
                root, structure, workflow, chapter_id, stage_b, candidate['text'])
            from .relationship_causality_review import validate_causality_completion
            validate_causality_completion(structure, chapter_id, stage_b, 'stage_b_review', candidate['text'])
            from .fact_inventory import validate_retention
            validate_retention(root, structure, workflow, chapter_id, stage_b, candidate['text'])
            from .behavior_review import validate_retention as validate_behavior_retention
            validate_behavior_retention(root, structure, workflow, chapter_id, stage_b, candidate['text'])
            from .writing_notes import validate_writing_decisions
            validate_writing_decisions(stage_b, artifacts['candidate_prose'])
            from .repair_history import require_obligations_closed
            require_obligations_closed(root, structure, workflow, chapter_id, stage_b, artifacts['candidate_prose'])
        except (ReviewArtifactError, StructureError) as exc:
            result.error(exc, 'REVIEW_EVIDENCE_INVALID', artifactType='stage_b_review')

    state_candidate = payloads.get("state_candidate")
    for name, field_name, code, builder in (
        ("state", "baseStateSha256", "STATE_BASE_MISMATCH", state_cache.build_state),
        ("design", "designSha256", "DESIGN_BASE_MISMATCH", state_cache.build_design),
    ):
        try:
            # Build projections in memory: load() would silently publish a cache.
            current = builder(root, structure=structure)
            # Only structure.json owns the source-mode state; legacy caches
            # may be absent or belong to the revision before a transition.
            cached = current
            if current.get("projectId") != structure["projectId"]:
                result.block("STATE_CACHE_INVALID", "project marker and structure projectId differ")
            if state_candidate is not None and (sha256_json(cached) != state_candidate[field_name] or sha256_json(current) != state_candidate[field_name]):
                result.block(code, f"{name} changed after state candidate was produced",
                             artifactType='state_candidate', field=field_name,
                             expected=sha256_json(current), actual=state_candidate[field_name])
        except (ProjectPathError, StructureError, OSError, ValueError, TypeError) as exc:
            result.error(exc, f"{name.upper()}_CACHE_INVALID")
    if state_candidate is not None and prose_sha is not None and state_candidate["finalProseSha256"] != prose_sha:
        result.block("PROSE_HASH_MISMATCH", "state candidate is not bound to candidate prose",
                     artifactType='state_candidate', field='finalProseSha256',
                     expected=prose_sha, actual=state_candidate['finalProseSha256'])
    if isinstance(candidate, dict) and (candidate.get('candidateSelection') is not None or candidate.get('candidateRecipe') is not None):
        for kind, payload in (('stage_b_review', stage_b), ('state_candidate', state_candidate)):
            if payload is not None and payload.get('candidateArtifactSha256') != artifacts['candidate_prose']['artifactSha256']:
                result.block('CURRENT_CANDIDATE_IDENTITY_MISMATCH', 'selected or batch prose requires exact candidate identity in every downstream artifact', artifactType=kind)
    if state_candidate is not None and isinstance(candidate, dict):
        facts = candidate.get('writingNotes', {}).get('newFacts', [])
        if facts and True:
            result.block('SOURCE_FACTS_REQUIRE_PROPOSAL', 'single-source new facts require source proposals')
        if facts and (state_candidate.get('proposedFacts') != facts
                      or state_candidate.get('candidateArtifactSha256') != artifacts['candidate_prose']['artifactSha256']):
            result.block('WRITING_FACTS_NOT_HANDED_OFF', 'state candidate must retain every proposed fact from the current prose notes')
    outline_sha = None
    plan_field = 'outlineSha256'
    try:
        from .source_plan import outline_binding
        binding = outline_binding(root, structure, chapter)
        outline_sha, plan_field = binding['hash'], binding['field']
    except (ProjectPathError, OSError, ValueError) as exc:
        result.error(exc, "OUTLINE_INVALID")
    if outline_sha is not None and state_candidate is not None and outline_sha != state_candidate.get(plan_field):
        result.block("OUTLINE_BASE_MISMATCH", "outline changed after state candidate was produced",
                     artifactType='state_candidate', field=plan_field,
                     expected=outline_sha, actual=state_candidate.get(plan_field))
    stage_a = payloads.get("stage_a_review")
    if stage_a is not None:
        if outline_sha is not None and stage_a.get(plan_field) != outline_sha:
            result.block("STAGE_A_OUTLINE_MISMATCH", "approved stage A review is not bound to the current outline",
                         expected=outline_sha, actual=stage_a.get(plan_field))
        if stage_a.get("decision") not in {"adopt", "modify"}:
            result.block("STAGE_A_NOT_ADOPTED", "stage A decision must be adopt or modify", decision=stage_a.get("decision"))
        try:
            from .review_plan import require_stage_a_completion
            require_stage_a_completion(structure, chapter_id, stage_a)
            from .relationship_causality_review import validate_causality_completion
            validate_causality_completion(structure, chapter_id, stage_a, 'stage_a_review')
        except (ReviewArtifactError, StructureError) as exc:
            result.error(exc, 'CAUSALITY_REVIEW_INVALID', artifactType='stage_a_review')
    resolved_title, title_decision = None, None
    try:
        resolved_title, title_decision = _resolve_title(artifacts, chapter, title_candidate_id=title_candidate_id, skip_title=skip_title)
    except WorkflowError as exc:
        result.error(exc, "TITLE_DECISION_REQUIRED", chapterId=chapter_id, artifactType='title_candidates',
                     titleCandidateId=title_candidate_id, skipTitle=skip_title)

    proposed_structure = deepcopy(structure)
    if chapter.get("status") == "review":
        transition_chapter_status(proposed_structure, chapter_id, "stable")
    target = next(item for item in proposed_structure["chapters"] if item["chapterId"] == chapter_id)
    if resolved_title:
        target["title"] = resolved_title
    if state_candidate is not None:
        if state_candidate["changes"]:
            result.block("SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION",
                         "setting changes must be committed through source change-preview before finalization")
        try:
            validate_structure(proposed_structure)
        except (StructureError, ValueError, TypeError, KeyError, AttributeError) as exc:
            result.error(exc, "STRUCTURE_INVALID")
    prose_path = None
    validated_prose_state = None
    validated_prose_object = None
    binding_transition = None
    ownership = None
    try:
        prose_path = safe_project_path(root, chapter["filePath"])
        candidate_path = safe_project_path(root, f"工作台/{chapter_id}-设定回写候选.md")
        # Phase 2（契约 §9–§20）：归属判定完全来自 current binding — 本章当前持久化的
        # proseBinding + 当前 filePath + 当前盘上字节。绝不读 continuity、operations、
        # backups 或血缘证据，也绝不 fallback 到 prose_ownership。
        from .prose_binding import (OWNED_CLEAN, UNBOUND, observe_chapter,
                                    observation_error_code, request_generation_current)
        from .write_sets import file_state
        from .file_preservation import observation
        ownership = observe_chapter(root, structure, chapter,
                                    pending=bool(result.transactions))
        if binding_generation is not None and not request_generation_current(chapter, binding_generation):
            # 代次 CAS（契约 §7 / §23）：请求必须回显**本章**当前 binding 的 generation，
            # 而不是全局 structure.revision。旧令牌（哪怕 filePath 与哈希都还吻合）
            # 一律被 OWNERSHIP_GENERATION_CONFLICT 挡下 —— ABA 复活在此终结。
            result.block("OWNERSHIP_GENERATION_CONFLICT",
                         "request binding generation is stale for this chapter",
                         chapterId=chapter_id, requestBindingGeneration=binding_generation,
                         currentBindingGeneration=ownership["bindingGeneration"])
        if ownership["state"] not in (UNBOUND, OWNED_CLEAN):
            result.block(observation_error_code(ownership["state"]),
                         f"finalization is not allowed while the chapter is {ownership['state']}",
                         chapterId=chapter_id, observedState=ownership["state"],
                         currentLocator=ownership["currentLocator"],
                         expectedContentDigest=ownership["expectedContentDigest"],
                         observedContentDigest=ownership["observedContentDigest"],
                         blockers=ownership["blockers"])
        elif prose_sha is None:
            result.block("PROSE_PAYLOAD_INVALID",
                         "finalization requires candidate prose to bind the ownership token")
        else:
            # 一个原子写集同时装下「正文字节」与「binding 流转」。first_finalize 从
            # unbound + 空槽出发（正文字节在这一步创建），refinalize 从 managed + H1
            # 出发（H1 必须仍在盘上）。同一份 observation 给出的盘上状态随后成为写集的
            # prose before-image，归属判定与事务提交指向的正是同一批字节。
            validated_prose_object, _ = observation(prose_path)
            validated_prose_state = {key: validated_prose_object[key] for key in ('exists', 'sha256', 'mode')}
            binding_transition = {
                "kind": "first_finalize" if ownership["state"] == UNBOUND else "refinalize",
                "chapterId": chapter_id,
                "contentDigest": f"sha256:{prose_sha}",
                "bindingGeneration": ownership["bindingGeneration"],
                "observedContentDigest": ownership["observedContentDigest"],
            }
        targets = ([prose_path, safe_project_path(root, '.gnovel/structure.json'),
                    safe_project_path(root, '.gnovel/workflow.json')])
        for path in targets:
            try:
                safe_project_path(root, path.relative_to(root))
                if path.exists() and not path.is_file():
                    result.block("TRANSACTION_TARGET_INVALID", "transaction target must be a regular file", path=str(path))
                for parent in path.parents:
                    if parent == root:
                        break
                    if parent.exists() and not parent.is_dir():
                        result.block("TRANSACTION_TARGET_INVALID", "transaction parent must be a directory", path=str(parent))
                        break
            except (ProjectPathError, OSError, ValueError) as exc:
                result.error(exc, "TRANSACTION_TARGET_INVALID")
    except (ProjectPathError, OSError, ValueError) as exc:
        result.error(exc, "TRANSACTION_TARGET_INVALID")
    result.context.update({"structure": structure, "chapter": chapter, "workflow": workflow, "artifacts": artifacts,
                      "proposed_structure": proposed_structure, "prose_path": prose_path,
                      "validatedProseTargetState": validated_prose_state,
                      "validatedProseTargetObject": validated_prose_object,
                      "bindingTransition": binding_transition, "ownership": ownership,
                      "currentBinding": current_binding,
                      "resolved_title": resolved_title, "title_decision": title_decision})
    result.context['guidance'] = _closeout_recovery(root, structure, workflow, chapter, result, result.blockers)
    return result
