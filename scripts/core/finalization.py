"""Chapter finalization and workflow reconciliation helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .artifacts import file_sha256, sha256_text, sha256_json
from .clock import utc_now
from .jsonio import append_text, read_json, write_json, write_text
from .locks import project_lock
from .paths import ProjectPathError, safe_project_path, stored_project_path
from .finalization_preflight import preflight_finalization
from .structure_store import StructureError, load_structure, save_structure, validate_structure
from .summary_status import summary_freshness
from .workflow import WorkflowError, validate_next_action

REQUIRED_FINAL_ARTIFACTS = ("stage_a_review", "candidate_prose", "diagnostics", "stage_b_review", "state_candidate")
TRANSACTION_PHASES = ("prepare", "prose", "structure", "indexes", "rewrite", "workflow", "cache", "committed")


def _workflow_path(root: Path) -> Path:
    return root / ".govel" / "workflow.json"


def _transactions_dir(root: Path) -> Path:
    return root / ".govel" / "transactions"


def _load_workflow(root: Path) -> dict[str, Any]:
    from .workflow import default_workflow, normalize_workflow
    path = _workflow_path(root)
    if not path.is_file():
        return default_workflow()
    return normalize_workflow(read_json(path))


def _artifact_path(root: Path, row: dict[str, Any]) -> Path:
    raw = row.get("artifactPath")
    if not raw:
        raise WorkflowError("ARTIFACT_SOURCE_MISSING", "registered artifact has no source path")
    try:
        return stored_project_path(root, raw)
    except ProjectPathError as exc:
        raise WorkflowError(exc.code, str(exc), exc.details) from exc




















def pending_transactions(root: Path) -> list[dict[str, Any]]:
    """未完成事务 = active manifest；终结manifest移入同TX目录，不再列入待恢复。"""
    directory = _transactions_dir(root)
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.json")):
        try:
            safe_project_path(root, path.relative_to(root))
            manifest = read_json(path)
            if not isinstance(manifest, dict) or manifest.get("transactionId") != path.stem:
                raise ValueError("transaction identity does not match manifest filename")
            rows.append(manifest)
        except (OSError, ValueError):
            rows.append({"transactionId": path.stem, "phase": "unreadable", "files": []})
    return rows


def recover_transactions(root: Path, *, apply: bool = False) -> dict[str, Any]:
    """报告或回滚被中断的最终化事务。"""
    root = root.expanduser().resolve()
    if not apply:
        rows = pending_transactions(root)
        if not rows:
            return {"ok": True, "code": "NO_PENDING_TRANSACTION", "transactions": [], "applied": False}
        return {
            "ok": True,
            "code": "TRANSACTION_INCOMPLETE",
            "transactions": [
                {"transactionId": item.get("transactionId"), "chapterId": item.get("chapterId"), "phase": item.get("phase"), "startedAt": item.get("startedAt")}
                for item in rows
            ],
            "applied": False,
            "hint": "确认无误后使用 workflow recover --apply 回滚到事务开始前的状态",
        }
    recovered = []
    with project_lock(root, allow_pending=True):
        # 必须在拿到锁后重新读取，避免恢复命令处理一个已经被另一个客户端
        # 成功提交并清除的旧 manifest。
        rows = pending_transactions(root)
        if not rows:
            return {"ok": True, "code": "NO_PENDING_TRANSACTION", "transactions": [], "applied": False}
        from .write_sets import validate_recovery, recover_write_set, _require_protocol
        for manifest in rows:
            _require_protocol(manifest)
            if manifest.get('phase') != 'committed':
                validate_recovery(root, manifest)
        for manifest in rows:
            actions = recover_write_set(root, manifest)
            recovered.append({'transactionId': manifest['transactionId'], 'chapterId': manifest.get('chapterId'),
                              'preservationPath': f".govel/transactions/{manifest['transactionId']}", 'actions': actions})
    return {"ok": True, "code": "TRANSACTION_RECOVERED", "transactions": recovered, "applied": True}


def _resolve_title(
    artifacts: dict[str, dict],
    chapter: dict[str, Any],
    *,
    title_candidate_id: str | None,
    skip_title: bool,
) -> tuple[str | None, dict[str, Any]]:
    """定稿标题只能来自已批准的 Titler 候选，或显式跳过。

    CHAPTER-PIPELINE「章节标题」把定名固定在 Editor 放行后、入库前；
    此前 CLI 允许直接传任意字符串，等于绕过 Titler 与作者选择。
    """
    if skip_title:
        return None, {"finalized": False, "reason": "author skipped naming", "workingTitle": chapter.get("title", "")}
    if not title_candidate_id:
        raise WorkflowError(
            "TITLE_DECISION_REQUIRED",
            "finalization needs --title-candidate-id or --skip-title",
            {"chapterId": chapter.get("chapterId")},
        )
    artifact = artifacts.get("title_candidates")
    if artifact is None:
        raise WorkflowError(
            "TITLE_CANDIDATES_MISSING",
            "no approved title_candidates artifact is registered",
            {"chapterId": chapter.get("chapterId")},
        )
    candidates = artifact.get("payload", {}).get("candidates", [])
    selected = next((item for item in candidates if str(item.get("candidateId")) == title_candidate_id), None)
    if selected is None:
        raise WorkflowError(
            "TITLE_CANDIDATE_UNKNOWN",
            f"candidate not found in approved title artifact: {title_candidate_id}",
            {"available": [str(item.get("candidateId")) for item in candidates]},
        )
    decision = {
        "finalized": True,
        "candidateId": title_candidate_id,
        "title": str(selected["title"]).strip(),
        "artifactSha256": artifact.get("artifactSha256"),
    }
    return decision["title"], decision


def _finalize_chapter_locked(
    root: Path,
    chapter_id: str,
    *,
    title_candidate_id: str | None = None,
    skip_title: bool = False,
    binding_generation: int | None = None,
    _closeout_request: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    from .write_sets import file_state, validate_inventory
    # These are write targets, so retain their validated before-images rather
    # than read-only guards. A client ignoring the lock must not have its newer
    # source bindings or approval choices adopted by a late output inventory.
    metadata_before = {relative: file_state(safe_project_path(root, relative))
                       for relative in ('.govel/structure.json', '.govel/workflow.json')}
    preflight = preflight_finalization(
        root, chapter_id, title_candidate_id=title_candidate_id, skip_title=skip_title,
        check_lock=False, binding_generation=binding_generation,
    )
    preflight.raise_if_blocked()
    validate_inventory(root, [{'path': path, 'before': state} for path, state in metadata_before.items()])
    context = preflight.context
    structure, chapter, workflow = context["structure"], context["chapter"], context["workflow"]
    from .source_model import require_current_source
    require_current_source(structure)
    artifacts = context["artifacts"]
    candidate = artifacts["candidate_prose"]["payload"]
    state_candidate = artifacts["state_candidate"]["payload"]
    prose_path = context["prose_path"]
    validated_prose_state = context.get("validatedProseTargetState")
    binding_transition = context.get("bindingTransition")
    title_decision = context["title_decision"]
    proposed_structure = context["proposed_structure"]
    proposed_structure["revision"] = structure["revision"] + 1
    proposed_structure["lastOperation"] = {
        "operationId": f"FINALIZE-{chapter_id}",
        "operation": "finalize_chapter",
        "actor": "workflow",
        "committedAt": utc_now(),
    }
    if binding_transition is not None:
        # Current-binding commit path（Phase 2）：正文字节的落盘与 unbound/managed → managed
        # 的 binding 流转由**同一个**写集承担，两者不拆成两步。流转经唯一的 transition
        # sealer 封签（generation = 本次提交 revision），随后由 writer boundary 从当前结构
        # 独立重推一遍——提案必须与推导结果逐字段相同，手搓的 digest / 伪造的 generation
        # 都在这里被拒。schema 4/5 的定稿绝不调用 prose_ownership（契约 §58/§70）。
        from .prose_binding import (OWNED_CLEAN, UNBOUND, apply_binding_transitions,
                                    enforce_writer_boundary, require_observation)
        expected_state = UNBOUND if binding_transition["kind"] == "first_finalize" else OWNED_CLEAN
        require_observation(root, structure, chapter, (expected_state,), action="finalization")
        proposed_structure, authority = apply_binding_transitions(
            structure, [binding_transition], proposed=proposed_structure)
        enforce_writer_boundary(structure, proposed_structure, authority=authority, root=root)
    target = next(item for item in proposed_structure["chapters"] if item["chapterId"] == chapter_id)
    next_action = {"chapterId": chapter_id, "action": "continuity", "reason": "chapter finalized; continuity confirmation pending", "needsApproval": True}
    workflow["chapterStates"][chapter_id] = {"status": "stable", "updatedAt": utc_now(), "sourceArtifactSha256": artifacts["state_candidate"]["artifactSha256"]}
    workflow["nextAction"] = next_action
    workflow["approvals"][chapter_id]["finalized"] = True
    workflow["approvals"][chapter_id]["titleDecision"] = title_decision

    from .derived_views import derived_outputs
    from .write_sets import apply_write_set, json_bytes, digest
    from .continuity import new_continuity_record
    proposed_structure["updatedAt"] = utc_now()
    from .canon_guard import require_canon_proposal
    require_canon_proposal(structure, proposed_structure,
                           {'operation': 'finalize_chapter', 'parameters': {}})
    prose_relative = str(prose_path.relative_to(root))
    candidate_relative, rows = None, []
    sync = {"candidatePath": None, "rows": rows, "unmappedCount": len(rows)}
    extras = {prose_relative: candidate["text"].encode()}
    source_hashes = {relative: digest(data) for relative, data in extras.items()}
    source_hashes['.govel/structure.json'] = digest(json_bytes(proposed_structure))
    from .candidate_batch import conversion_witness
    workflow.setdefault("continuity", {})[chapter_id] = new_continuity_record(
        root, chapter, sync, artifacts["state_candidate"]["artifactSha256"],
        candidate_conversion=conversion_witness(structure, chapter, artifacts, target),
        source_hashes=source_hashes, fact_review={
            'artifactSha256': artifacts['stage_b_review']['artifactSha256'],
            'proseSha256': sha256_text(candidate['text']),
            'bindingGeneration': target['proseBinding']['generation']})
    extras[".govel/workflow.json"] = json_bytes(workflow)
    outputs, _ = derived_outputs(root, proposed_structure, extras=extras, current=structure)
    # TOCTOU close: the locked preflight above proved the prose target was an empty or owned
    # slot while holding project_lock, and captured that exact validated state. A client that
    # ignores the lock could still drop or edit a file onto the target after that proof but
    # before this write. Re-sampling the target now (a late inventory) would adopt those
    # foreign bytes as a legitimate before-image, so we bind the write set's prose before-image
    # to the state that was VALIDATED, not to whatever happens to be on disk at commit time.
    # apply_write_set then compares the current bytes against that validated before-image and
    # refuses (WRITE_SET_CONFLICT) if the target changed since — the raced-in or externally
    # edited bytes are never overwritten, and the conflict fires before any backup or write so
    # rollback never touches them. Source/workflow metadata also retain their
    # preflight before-images; after-images still come solely from the proposal.
    from .write_sets import inventory
    expected = inventory(root, outputs)
    for row in expected:
        if row['path'] in metadata_before:
            row['before'] = metadata_before[row['path']]
    if validated_prose_state is not None:
        for row in expected:
            if row["path"] == prose_relative:
                row["before"] = validated_prose_state
                row["expectedObject"] = context.get("validatedProseTargetObject")
                break
    cognition_guards = [*context.get('cognitionSourceGuards', []), *context.get('candidateSelectionGuards', [])]
    guards = list(cognition_guards)
    if _closeout_request is not None:
        from .chapter_closeout import receipt_outputs
        receipt, guards, expected = receipt_outputs(root, _closeout_request, 'finalize', outputs,
            {'chapterId': chapter_id, 'bindingGeneration': target['proseBinding']['generation'],
             'titleDecision': title_decision, 'batchHash': workflow['continuity'][chapter_id]['batchHash'],
             **({'candidateConversionHash': workflow['continuity'][chapter_id]['candidateConversion']['witnessHash']}
                if workflow['continuity'][chapter_id].get('candidateConversion') else {}),
             'proseSha256': source_hashes[prose_relative]}, expected=expected)
        by_path = {row['path']: row for row in guards}
        for guard in cognition_guards:
            if guard['path'] in by_path and by_path[guard['path']] != guard:
                raise StructureError('WRITE_SET_CONFLICT', 'cognition source changed during finalization receipt assembly',
                                     {'path': guard['path']})
            by_path[guard['path']] = guard
        guards = list(by_path.values())
        outputs.update(receipt)
    apply_write_set(root, outputs, expected=expected, operation="finalize_chapter", guards=guards)
    freshness = summary_freshness(root, proposed_structure, target_chapter_id=chapter_id)
    return {
        "ok": True,
        "code": "CHAPTER_FINALIZED",
        "chapterId": chapter_id,
        "status": "stable",
        "nextAction": next_action,
        "summaryFreshness": freshness,
        "summaryRefreshRequired": freshness["refreshRequired"],
        "summaryRefreshRecommended": freshness["refreshRecommended"],
        "title": target["title"],
        "titleFinalized": title_decision["finalized"],
        "settingRewriteCandidate": sync["candidatePath"],
        "sourceChangesRequired": False,
        "continuity": {"status": "pending", "batchHash": workflow["continuity"][chapter_id]["batchHash"]},
        "readyForContinuation": False,
        "unmappedStateChanges": sync["unmappedCount"],
    }


def finalize_chapter(
    root: Path,
    chapter_id: str,
    *,
    title_candidate_id: str | None = None,
    skip_title: bool = False,
    binding_generation: int | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    # Current-binding schemas require the ownership token to be echoed back（契约 §22/§23）。
    # Reading it from the receipt and passing it here is the request's proof that it was
    # prepared against THIS binding generation; a finalization that omits it, or that
    # carries a stale one, is refused before anything else is even inspected — a destructive
    # prose write is never authorized by "the caller did not say which state it saw".
    structure = _load_structure_or_none(root)
    from .prose_binding import request_generation_current
    chapter = next((row for row in structure.get("chapters", [])
                    if row.get("chapterId") == chapter_id), None)
    if chapter is not None:
        if binding_generation is None:
            raise WorkflowError(
                "OWNERSHIP_GENERATION_CONFLICT",
                "a current-binding finalization must echo the chapter's binding generation "
                "(read it from workflow chapter-receipt before calling)",
                {"chapterId": chapter_id})
        if not request_generation_current(chapter, binding_generation):
            raise WorkflowError(
                "OWNERSHIP_GENERATION_CONFLICT",
                "request binding generation is stale for this chapter",
                {"chapterId": chapter_id, "requestBindingGeneration": binding_generation,
                 "currentBindingGeneration": chapter["proseBinding"].get("generation")})
    preflight_finalization(
        root, chapter_id, title_candidate_id=title_candidate_id, skip_title=skip_title,
        binding_generation=binding_generation,
    ).raise_if_blocked()
    with project_lock(root):
        return _finalize_chapter_locked(root, chapter_id, title_candidate_id=title_candidate_id,
                                        skip_title=skip_title, binding_generation=binding_generation)


def _load_structure_or_none(root: Path) -> dict:
    try:
        return load_structure(root)
    except (StructureError, OSError, ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Accept External Modification（契约 §35–§38 / §95）
# ---------------------------------------------------------------------------

def accept_external_workflow(workflow: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    """Apply the workflow half of the external-modification invalidation matrix（§10.3）。

    Prose-dependent evidence is voided:机械门报告、Editor 终审、设定回写候选、候选正文
    工件与它们绑定的审批全部作废，章节生命周期回退到未审校。Deliberately NOT done here
    （契约 §10.2/§38）：不写 continuity 成功记录、不替 Editor/Canon 下结论、不自动定稿、
    不把 H2 的叙事事实写进设定台账。历史档案（`continuity`、`operations/`、撤销证据）作为
    历史事实原样保留。`stage_a_review` 只依赖大纲推演，正文外部改动不推定它失效。
    """
    after = deepcopy(workflow)
    approvals = after.setdefault("approvals", {})
    entry = approvals.get(chapter_id)
    if isinstance(entry, dict):
        entry["approved"] = False
        entry.pop("artifactHashes", None)
        entry["invalidatedBy"] = "prose_external_modification"
    for kind, record in (after.setdefault("artifacts", {}).get(chapter_id) or {}).items():
        if isinstance(record, dict) and kind != "stage_a_review":
            record["invalidatedBy"] = "prose_external_modification"
    states = after.setdefault("chapterStates", {})
    state = states.get(chapter_id)
    if isinstance(state, dict):
        state["status"] = "stale"
        state["updatedAt"] = utc_now()
        state.pop("sourceArtifactSha256", None)
    after["nextAction"] = {"chapterId": chapter_id, "action": "planner",
                           "reason": "prose changed outside finalization; re-review is required",
                           "needsApproval": True}
    return after


def preview_accept_external_modification(root: Path, *, chapter_id: str,
                                         actor: str = "human_ui", request_id: str | None = None) -> dict:
    from .locks import project_lock_reentrant
    with project_lock_reentrant(root):
        return _preview_accept_external_locked(root, chapter_id=chapter_id, actor=actor,
                                               request_id=request_id or 'external-' + uuid.uuid4().hex)


def _preview_accept_external_locked(root: Path, *, chapter_id: str, actor: str, request_id: str) -> dict:
    """Preview an explicit Accept External Modification（契约 §35–§37）。

    This is NOT a finalization（契约 §10.2/§92）：它把**当前盘上**的已被显式接受的哈希
    写进 binding、推进代次并重新密封 integrityDigest，正文字节一个都不重写，也不产生
    连续性定稿记录或任何审校结论。

    The preview returns the whole ownership token — old accepted digest H1, current
    external digest H2, binding generation, locator — so the author approves exactly the
    transition that will be committed, and the commit re-observes all of it under the lock
    （契约 §36/§63）。Preview is open to every channel; the commit is not: plan integrity
    （返回的 planHash + 锁内重验）只证明「提交的还是被预览的那一份、盘上 H2 没变」，它**不能**
    证明「独立特权批准通道授权了这个动作」。本构建没有这样的通道，因此 accept_external 的
    commit 在锁内、任何写入之前一律以 `APPROVAL_CAPABILITY_REQUIRED` fail closed
    （契约 §10.1/§39）——preview 语义与全部 ownership token 保持不变。
    """
    from .prose_binding import (OWNED_MODIFIED_EXTERNALLY, apply_binding_transitions,
                                enforce_writer_boundary, require_observation)
    from .refactor import _change, _refactor_plan
    root = root.expanduser().resolve()
    structure = load_structure(root)
    from .source_requests import request_identity, resume_request
    from .source_review import fingerprint
    operation_id, _ = request_identity(structure, request_id)
    request_fingerprint = fingerprint({'operation': 'accept_external_modification', 'chapterId': chapter_id,
                                       'projectRoot': str(root), 'actor': actor})
    prior = resume_request(root, structure, operation_id, request_fingerprint)
    if prior is not None:
        return prior
    chapter = next((row for row in structure["chapters"] if row.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise StructureError('CHAPTER_NOT_FOUND', f'unknown chapter: {chapter_id}')
    observation = require_observation(root, structure, chapter, (OWNED_MODIFIED_EXTERNALLY,),
                                      action="accept external modification")
    from .external_review_materials import freeze_external_materials
    material, material_guards = freeze_external_materials(root, structure, chapter, operation_id,
                                                          request_fingerprint=request_fingerprint)
    if material['observedContentDigest'] != observation['observedContentDigest']:
        raise StructureError('PROSE_CONTENT_CHANGED', 'external prose changed while preparing review material')
    transition = {"kind": "accept_external", "chapterId": chapter_id,
                  "observedContentDigest": observation["observedContentDigest"],
                  "bindingGeneration": observation["bindingGeneration"],
                  "expectedContentDigest": observation["expectedContentDigest"],
                  "filePath": observation["currentLocator"]}
    proposed = deepcopy(structure)
    proposed["revision"] = structure["revision"] + 1
    proposed, authority = apply_binding_transitions(structure, [transition], proposed=proposed)
    enforce_writer_boundary(structure, proposed, authority=authority, root=root)
    target = next(row for row in proposed["chapters"] if row["chapterId"] == chapter_id)
    # §10.3：正文字节在定稿之外被改动，章节必须重走审校流水线（status 回退），
    # 而 continuity / operations / 撤销证据作为历史事实原样保留。
    target["status"] = "stale"
    proposed["updatedAt"] = utc_now()
    proposed["lastOperation"] = {"operationId": f"ACCEPT-EXTERNAL-{chapter_id}",
                                 "operation": "accept_external_modification",
                                 "actor": actor, "committedAt": utc_now()}
    validate_structure(proposed)
    impact = {"chapterId": chapter_id, "operation": "accept_external_modification",
              "refactorNote": "正文在定稿之外被改动：接受它只更新已接受的哈希并作废依赖正文字节的工件，不自动定稿。",
              "staleCandidates": [], "approvalInvalidations": [chapter_id],
              "previousContentDigest": observation["expectedContentDigest"],
              "observedContentDigest": observation["observedContentDigest"],
              # 契约 §62/§16：预览把归属判定摊开（定位符、代次、期望摘要、观测摘要、观测状态）。
              "ownership": observation,
              "reviewFragmentIds": []}
    return _refactor_plan(root, structure, operation='accept_external_modification', actor=actor,
                          parameters={"chapterId": chapter_id,
                                      "bindingGeneration": observation["bindingGeneration"],
                                      "expectedContentDigest": observation["expectedContentDigest"],
                                      "observedContentDigest": observation["observedContentDigest"],
                                      "filePath": observation["currentLocator"]},
                          changes=[_change('chapter', chapter_id, 'expectedContentDigest',
                                           observation["expectedContentDigest"],
                                           observation["observedContentDigest"])],
                          proposed=proposed, impact=impact,
                          binding_transitions=[transition], operation_id=operation_id,
                          request_id=request_id, request_fingerprint=request_fingerprint,
                          external_review_materials=material, external_review_guards=material_guards)



def _write_workflow(root: Path, workflow: dict[str, Any]) -> None:
    write_text(_workflow_path(root), json.dumps({**workflow, "updatedAt": utc_now()}, ensure_ascii=False, indent=2) + "\n")


def _status_action(status: str) -> str | None:
    return {
        "planning_required": "planner",
        "planned": "writer",
        "draft": "mechanical",
        "review": "editor",
        "stable": "continuity",
        "stale": "planner",
    }.get(status)


def pending_fact_action(chapter_id: str) -> dict:
    """Canonical tool-derived handoff, distinguishable from a custom next action."""
    return {'chapterId': chapter_id, 'action': 'continuity',
            'reason': 'B fact inventory or source evidence is pending', 'needsApproval': False}


def derive_next_action(structure: dict, workflow: dict, *, root=None) -> dict | None:
    from .continuity import pending_continuity

    approved = set(workflow.get("approvedChapterIds", []))
    pending = sorted((row for row in structure.get("chapters", [])
                      if row["chapterId"] in approved and row.get("status") not in {"stable", "archived"}),
                     key=lambda row: row["displayOrdinal"])
    selected = pending[0] if pending else None
    continuity = pending_continuity(structure, workflow, before_ordinal=selected["displayOrdinal"] if selected else None)
    if continuity:
        return {"chapterId": continuity[0]["chapterId"], "action": "continuity",
                "reason": "continuity confirmation is pending", "needsApproval": True}
    if root is not None:
        from .fact_inventory import inventory_status
        for chapter in sorted(structure.get('chapters', []), key=lambda r: r['displayOrdinal']):
            if chapter.get('status') == 'stable' and (selected is None or chapter['displayOrdinal'] < selected['displayOrdinal']):
                if not inventory_status(root, structure, workflow, chapter['chapterId'])['complete']:
                    return pending_fact_action(chapter['chapterId'])
    if selected:
        if root is not None:
            from .candidate_batch import _current_recipe, candidate_handoff
            completed_candidates = []
            for row in pending:
                if _current_recipe(root, structure, workflow, row['chapterId']) is None:
                    break
                try:
                    candidate_handoff(root, structure, workflow, row['chapterId'])
                except (ValueError, OSError):
                    selected = row
                    break
                completed_candidates.append(row)
            if completed_candidates:
                from .candidate_batch import batch_record
                first_recipe = _current_recipe(root, structure, workflow, completed_candidates[0]['chapterId'])
                commissioned = batch_record(root, structure, first_recipe['batchRef'])['chapterIds']
                remaining_commission = [row for row in pending if row['chapterId'] in commissioned and row not in completed_candidates]
                if not remaining_commission:
                    return {'chapterId': completed_candidates[0]['chapterId'], 'action': 'await_author',
                            'reason': 'frozen candidate commission is ready for author reading; formal acceptance is pending', 'needsApproval': True}
            if completed_candidates and selected in completed_candidates:
                remaining = [row for row in pending if row not in completed_candidates]
                selected = remaining[0] if remaining else selected
        action = _status_action(selected.get("status", ""))
        if action:
            return {"chapterId": selected["chapterId"], "action": action,
                    "reason": f"reconciled from status={selected.get('status')}", "needsApproval": False}
    return None


def reconcile_workflow(root: Path, *, persist: bool = False) -> dict[str, Any]:
    """Rebuild the single next action, or refuse when the project state is ambiguous.

    此前实现先取 pending[0] 再判断 len(candidates) > 1，候选恒为 1 条，
    WORKFLOW_AMBIGUOUS 永不可达——CHAPTER-PIPELINE 要求的「多候选时停下等作者裁决」
    在运行时没有对应行为。现在先收集全部冲突信号，再决定是否给出唯一下一步。
    """
    root = root.expanduser().resolve()
    if persist:
        with project_lock(root):
            return _reconcile_workflow_locked(root, persist=True)
    return _reconcile_workflow_locked(root, persist=False)


def _reconcile_workflow_locked(root: Path, *, persist: bool) -> dict[str, Any]:
    structure = load_structure(root)
    workflow = _load_workflow(root)
    approved = set(workflow.get("approvedChapterIds", []))
    eligible = [
        chapter for chapter in sorted(structure.get("chapters", []), key=lambda item: item.get("displayOrdinal", 0))
        if chapter.get("chapterId") in approved and chapter.get("status") != "archived"
    ]
    pending = [chapter for chapter in eligible if chapter.get("status") != "stable"]
    conflicts: list[dict[str, Any]] = []

    for item in pending_transactions(root):
        conflicts.append({
            "code": "TRANSACTION_INCOMPLETE",
            "transactionId": item.get("transactionId"),
            "chapterId": item.get("chapterId"),
            "phase": item.get("phase"),
        })

    # 同时有多章处于执笔或审校中间态：串行纪律已被打破，不能替作者猜哪一章优先。
    in_flight = [item for item in pending if item.get("status") in {"planned", "draft", "review"}]
    if len(in_flight) > 1:
        try:
            from .candidate_batch import _current_recipe, validate_recipe
            recipes = [_current_recipe(root, structure, workflow, item['chapterId']) for item in in_flight]
            if any(recipe is None for recipe in recipes) or len({sha256_json(recipe['batchRef']) for recipe in recipes}) != 1:
                raise StructureError('CANDIDATE_BATCH_UNDECLARED', 'multiple active chapters need the same frozen candidate commission')
            for item, recipe in zip(in_flight, recipes):
                validate_recipe(root, structure, workflow, item['chapterId'], recipe)
        except (ValueError, OSError) as exc:
            conflicts.append({"code": "MULTIPLE_CHAPTERS_IN_FLIGHT",
                "chapters": [{"chapterId": item["chapterId"], "status": item.get("status")} for item in in_flight],
                'reason': getattr(exc, 'code', 'CANDIDATE_BATCH_INVALID')})

    for chapter in structure.get("chapters", []):
        recorded = workflow.get("chapterStates", {}).get(chapter["chapterId"], {}).get("status")
        if recorded and recorded != chapter.get("status"):
            conflicts.append({
                "code": "CHAPTER_STATE_DIVERGED",
                "chapterId": chapter["chapterId"],
                "structureStatus": chapter.get("status"),
                "workflowStatus": recorded,
            })

    next_action = derive_next_action(structure, workflow, root=root)

    existing = workflow.get("nextAction")
    completed_fact_handoff = False
    if isinstance(existing, dict) and existing == pending_fact_action(existing.get('chapterId')):
        from .continuity import continuity_status
        chapter = next((row for row in structure['chapters']
                        if row['chapterId'] == existing['chapterId'] and row.get('status') == 'stable'), None)
        completed_fact_handoff = bool(chapter and
            continuity_status(root, structure, workflow, chapter)['readyForContinuation'])
    if existing and not completed_fact_handoff:
        if (next_action is None or existing.get("chapterId") != next_action["chapterId"]
                or existing.get("action") != next_action["action"]):
            conflicts.append({"code": "NEXT_ACTION_DIVERGED", "stored": existing, "derived": next_action})
        else:
            # A matching route does not erase a caller's reason or approval
            # condition. Only the completed canonical fact handoff is replaced.
            next_action = deepcopy(existing)

    if conflicts:
        return {
            "ok": False,
            "code": "WORKFLOW_AMBIGUOUS",
            "conflicts": conflicts,
            "derivedNextAction": next_action,
            "hint": "先解决冲突（必要时运行 workflow recover），再重新 reconcile；不要凭会话记忆选择",
        }
    if next_action:
        validate_next_action(next_action)
    if persist and workflow.get("nextAction") != next_action:
        workflow["nextAction"] = next_action
        _write_workflow(root, workflow)
    return {"ok": True, "code": "WORKFLOW_RECONCILED", "nextAction": next_action, "persisted": persist}
