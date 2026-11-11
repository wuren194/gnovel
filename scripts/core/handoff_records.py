"""Traceable host handoff records（§35-§41 / §63）.

浏览器不运行模型。作者在浏览器里完成 prepare（只读）、明确选择「交给工位执行」时
创建 handoff record；宿主工位执行完毕后，返回在这里登记核验。record 是磁盘上的
一等事实：页面重开、宿主失联、面板关闭都不会让任务消失或凭计时器推进。

- `handoff.create` 受 single-track 约束：已有 `created` 状态的 record 时拒绝新建，
  作者必须先停止/完成旧任务，或显式创建 rerun（关联原 record）。
- 返回登记按 `returnRequestId` 幂等：同 ID 同输出 → 原收据；同 ID 不同输出 → 拒绝。
- 迟到返回只追加为新 candidate 版本（不激活、不覆盖新 candidate、不借任何批准），
  依赖已变化时标记 stale。
- 不消费上下文的任务显式记录 `contextApplicable: false`，不伪造 contextHash。

存储：`.govel/handoffs/index.json`（单文件，容量受 single-track 与任务数天然约束）。
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from .artifacts import sha256_text
from .clock import utc_now
from .jsonio import read_json
from .locks import project_lock_reentrant
from .paths import ProjectPathError, safe_project_path
from .project import resolve_knowledge_mode
from .structure_store import StructureError, load_structure

HANDOFF_INDEX_SCHEMA_VERSION = 1
HANDOFFS_PATH = ".govel/handoffs/index.json"
OPEN_STATUSES = {"created"}
TERMINAL_STATUSES = {"returned", "stopped", "failed"}
KNOWLEDGE_MODE_SOURCES = {"project_default", "task_override", "unknown"}


def _index_path(root: Path) -> Path:
    return safe_project_path(root, HANDOFFS_PATH)


def _load_index(root: Path) -> dict[str, Any]:
    path = _index_path(root)
    if not path.is_file():
        return {"schemaVersion": HANDOFF_INDEX_SCHEMA_VERSION, "projectId": "", "sequence": 0, "handoffs": {}, "returns": {}}
    index = read_json(path)
    if not isinstance(index, dict) or index.get("schemaVersion") != HANDOFF_INDEX_SCHEMA_VERSION:
        raise StructureError("HANDOFF_INDEX_INVALID", "handoff index is unreadable or unsupported")
    return index


def _save_index(root: Path, index: dict[str, Any]) -> None:
    from .jsonio import write_json
    write_json(_index_path(root), index)


def _chapter_id_or_none(root: Path, chapter_id: str | None) -> str | None:
    if chapter_id is None:
        return None
    structure = load_structure(root)
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}", {"chapterId": chapter_id})
    return chapter_id


def _resolve_mode(effective_knowledge_mode: str | None, knowledge_mode_source: str | None, root: Path) -> tuple[str, str]:
    if effective_knowledge_mode is None:
        return resolve_knowledge_mode(None, project_root=root), "project_default"
    mode = resolve_knowledge_mode(effective_knowledge_mode, project_root=root)
    source = knowledge_mode_source or "task_override"
    if source not in KNOWLEDGE_MODE_SOURCES:
        raise StructureError("HANDOFF_MODE_SOURCE_INVALID", "unsupported knowledgeModeSource",
                             {"supported": sorted(KNOWLEDGE_MODE_SOURCES)})
    return mode, source


def _normalize_dependencies(root: Path, dependency_manifest: Any) -> list[dict[str, str]]:
    if dependency_manifest is None:
        return []
    if not isinstance(dependency_manifest, list) or len(dependency_manifest) > 200:
        raise StructureError("HANDOFF_DEPENDENCIES_INVALID", "dependencyManifest must be a list of at most 200 rows")
    rows = []
    for row in dependency_manifest:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("sha256"), str):
            raise StructureError("HANDOFF_DEPENDENCIES_INVALID", "dependency rows need path and sha256")
        try:
            safe_project_path(root, row["path"])
        except ProjectPathError as exc:
            raise StructureError(exc.code, str(exc), exc.details) from exc
        rows.append({"path": row["path"], "sha256": row["sha256"]})
    return rows


def create_handoff(
    root: Path,
    *,
    chapter_id: str | None,
    task: str,
    station: str,
    produces_candidate: bool = False,
    candidate_version: int | None = None,
    candidate_active_token: int | None = None,
    context_hash: str | None = None,
    context_applicable: bool = True,
    prompt_plan_fingerprint: str | None = None,
    dependency_manifest: Any = None,
    pov: str | None = None,
    effective_knowledge_mode: str | None = None,
    knowledge_mode_source: str | None = None,
    input_scope: Any = None,
    rerun_of: str | None = None,
) -> dict[str, Any]:
    """Create one handoff record. Refuses when another handoff is still open (§41)."""
    root = root.expanduser().resolve()
    if not isinstance(task, str) or not task.strip():
        raise StructureError("HANDOFF_TASK_REQUIRED", "task is required")
    if not isinstance(station, str) or not station.strip():
        raise StructureError("HANDOFF_STATION_REQUIRED", "station is required")
    if context_applicable and (context_hash is None or not str(context_hash).strip()):
        raise StructureError("HANDOFF_CONTEXT_PROOF_REQUIRED",
                             "contextApplicable handoffs need a contextHash; record not applicable explicitly instead")
    if rerun_of is not None and not isinstance(rerun_of, str):
        raise StructureError("HANDOFF_RERUN_INVALID", "rerunOf must be a handoff id")
    mode, mode_source = _resolve_mode(effective_knowledge_mode, knowledge_mode_source, root)
    dependencies = _normalize_dependencies(root, dependency_manifest)
    resolved_chapter = _chapter_id_or_none(root, chapter_id)
    # §36「candidate version/token if applicable」：产出候选正文的交接在创建时绑定
    # 当时的最新候选版本；未绑定的返回无法通过基线校验（fail closed），所以这里
    # 显式取当前值，迟到的返回以 allow_stale_base 追加、绝不覆盖。
    if produces_candidate and candidate_version is None and resolved_chapter:
        from .candidate_versions import load_candidate_index
        index = load_candidate_index(root, resolved_chapter)
        candidate_version = index["versions"][-1]["version"] if index["versions"] else None
        candidate_active_token = index["activeToken"] if index["versions"] else None
    with project_lock_reentrant(root):
        index = _load_index(root)
        if not index.get("projectId"):
            index["projectId"] = load_structure(root).get("projectId", "")
        open_rows = [row for row in index["handoffs"].values()
                     if row.get("status") in OPEN_STATUSES and not row.get("supersededBy")]
        if open_rows:
            raise StructureError(
                "HANDOFF_SINGLE_TRACK",
                "another handoff is still open; stop or complete it before starting a new one",
                {"openHandoffIds": sorted(row["handoffId"] for row in open_rows)},
            )
        if rerun_of is not None and rerun_of not in index["handoffs"]:
            raise StructureError("HANDOFF_NOT_FOUND", f"unknown handoff: {rerun_of}")
        index["sequence"] = int(index.get("sequence", 0)) + 1
        handoff_id = f"HO-{index['sequence']:04d}-{secrets.token_hex(3)}"
        created_at = utc_now()
        record = {
            "handoffId": handoff_id,
            "projectId": index["projectId"],
            "chapterId": resolved_chapter,
            "task": task.strip(),
            "station": station.strip(),
            "producesCandidate": bool(produces_candidate),
            "candidateVersion": candidate_version if isinstance(candidate_version, int) else None,
            "candidateActiveToken": candidate_active_token if isinstance(candidate_active_token, int) else None,
            "contextHash": str(context_hash).strip() if context_applicable and context_hash else None,
            "contextApplicable": bool(context_applicable),
            "promptPlanFingerprint": str(prompt_plan_fingerprint) if prompt_plan_fingerprint else None,
            "dependencyManifest": dependencies,
            "pov": pov,
            "effectiveKnowledgeMode": mode,
            "knowledgeModeSource": mode_source,
            "inputScope": input_scope if isinstance(input_scope, dict) else {},
            "createdAt": created_at,
            "status": "created",
            "returnedAt": None,
            "returnRequestIds": [],
            "returnSummary": None,
            "rerunOf": rerun_of,
            "supersededBy": None,
        }
        index["handoffs"][handoff_id] = record
        _save_index(root, index)
        return {"ok": True, "code": "HANDOFF_CREATED", "handoff": record}


def read_handoffs(
    root: Path,
    *,
    chapter_id: str | None = None,
    handoff_id: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    index = _load_index(root)
    rows = sorted(index["handoffs"].values(), key=lambda row: row.get("createdAt", ""))
    if handoff_id is not None:
        record = index["handoffs"].get(handoff_id)
        if record is None:
            raise StructureError("HANDOFF_NOT_FOUND", f"unknown handoff: {handoff_id}", {"handoffId": handoff_id})
        return {"ok": True, "code": "HANDOFF_READ", "handoff": record}
    if chapter_id is not None:
        rows = [row for row in rows if row.get("chapterId") == chapter_id]
    return {"ok": True, "code": "HANDOFF_LIST", "handoffs": rows,
            "openHandoffId": next((row["handoffId"] for row in rows if row.get("status") in OPEN_STATUSES and not row.get("supersededBy")), None)}


def stop_handoff(root: Path, handoff_id: str, *, reason: str = "") -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not isinstance(reason, str):
        raise StructureError("HANDOFF_STOP_INVALID", "reason must be a string")
    with project_lock_reentrant(root):
        index = _load_index(root)
        record = index["handoffs"].get(handoff_id)
        if record is None:
            raise StructureError("HANDOFF_NOT_FOUND", f"unknown handoff: {handoff_id}")
        if record["status"] != "created":
            raise StructureError("HANDOFF_NOT_OPEN", f"handoff is already {record['status']}")
        record["status"] = "stopped"
        record["stopReason"] = reason.strip()
        record["stoppedAt"] = utc_now()
        _save_index(root, index)
        return {"ok": True, "code": "HANDOFF_STOPPED", "handoff": record}


def _dependency_staleness(root: Path, record: dict[str, Any]) -> list[str]:
    from .artifacts import file_sha256
    reasons = []
    for row in record.get("dependencyManifest", []):
        relative, claimed = row["path"], row["sha256"]
        try:
            path = safe_project_path(root, relative)
        except ProjectPathError as exc:
            reasons.append(f"{relative}: {exc.code}")
            continue
        if not path.is_file():
            reasons.append(f"{relative}: missing")
            continue
        try:
            actual = file_sha256(path)
        except Exception:  # noqa: BLE001 - 不可读即视为漂移
            reasons.append(f"{relative}: unreadable")
            continue
        if actual != claimed:
            reasons.append(f"{relative}: changed")
    return reasons


def register_handoff_return(
    root: Path,
    *,
    handoff_id: str,
    return_request_id: str,
    body: str | None,
) -> dict[str, Any]:
    """Register one host return. Idempotent per returnRequestId; never auto-activates."""
    root = root.expanduser().resolve()
    if not isinstance(return_request_id, str) or not return_request_id.strip():
        raise StructureError("HANDOFF_RETURN_REQUEST_REQUIRED", "returnRequestId is required")
    return_request_id = return_request_id.strip()
    if body is not None and not isinstance(body, str):
        raise StructureError("HANDOFF_RETURN_BODY_INVALID", "body must be a string when present")
    with project_lock_reentrant(root):
        index = _load_index(root)
        record = index["handoffs"].get(handoff_id)
        if record is None:
            raise StructureError("HANDOFF_NOT_FOUND", f"unknown handoff: {handoff_id}")
        if record.get("projectId") and record["projectId"] != load_structure(root).get("projectId", ""):
            raise StructureError("HANDOFF_PROJECT_MISMATCH", "handoff belongs to a different project")
        prior = index["returns"].get(return_request_id)
        if prior is not None:
            if prior.get("handoffId") != handoff_id:
                raise StructureError("HANDOFF_RETURN_CONFLICT", "returnRequestId belongs to another handoff",
                                     {"returnRequestId": return_request_id, "handoffId": prior.get("handoffId")})
            if prior.get("outputSha256") != (sha256_text(body) if body is not None else None):
                raise StructureError("HANDOFF_RETURN_CONFLICT",
                                     "returnRequestId was already used with different output",
                                     {"returnRequestId": return_request_id})
            return {**prior["receipt"], "replayed": True}
        if record["status"] != "created":
            raise StructureError("HANDOFF_ALREADY_RETURNED", f"handoff is already {record['status']}")

        output_sha = sha256_text(body) if body is not None else None
        stale_reasons = _dependency_staleness(root, record)
        current = not stale_reasons

        candidate_receipt = None
        if record.get("producesCandidate"):
            if body is None or not body.strip():
                raise StructureError("HANDOFF_RETURN_BODY_REQUIRED", "this handoff produces a candidate; body is required")
            from .candidate_versions import save_candidate_version
            candidate_receipt = save_candidate_version(
                root, record["chapterId"], body=body,
                request_id=f"handoff:{handoff_id}:{return_request_id}",
                base_candidate_version=record.get("candidateVersion"),
                source="handoff", handoff_id=handoff_id, allow_stale_base=True,
            )
            if record.get("candidateActiveToken") is not None and \
                    candidate_receipt.get("activeToken") != record.get("candidateActiveToken"):
                stale_reasons.append("candidate slot moved since handoff was created")
                current = False

        returned_at = utc_now()
        summary = {
            "returnRequestId": return_request_id,
            "outputSha256": output_sha,
            "byteLength": len(body.encode("utf-8")) if body is not None else 0,
            "current": current,
            "staleReasons": stale_reasons,
            "candidateVersion": candidate_receipt.get("version") if candidate_receipt else None,
            "candidateBodySha256": candidate_receipt.get("bodySha256") if candidate_receipt else None,
            "returnedAt": returned_at,
        }
        record["status"] = "returned"
        record["returnedAt"] = returned_at
        record["returnRequestIds"].append(return_request_id)
        record["returnSummary"] = summary
        receipt = {"ok": True, "code": "HANDOFF_RETURNED", "handoffId": handoff_id,
                   "chapterId": record.get("chapterId"), "stale": not current, "return": summary,
                   "candidate": candidate_receipt}
        index["returns"][return_request_id] = {"handoffId": handoff_id, "outputSha256": output_sha, "receipt": receipt}
        _save_index(root, index)
        return receipt


def handoff_result_status(root: Path, handoff_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    index = _load_index(root)
    record = index["handoffs"].get(handoff_id)
    if record is None:
        raise StructureError("HANDOFF_NOT_FOUND", f"unknown handoff: {handoff_id}")
    return {
        "ok": True,
        "code": "HANDOFF_STATUS",
        "handoffId": handoff_id,
        "status": record["status"],
        "chapterId": record.get("chapterId"),
        "task": record.get("task"),
        "station": record.get("station"),
        "effectiveKnowledgeMode": record.get("effectiveKnowledgeMode"),
        "contextApplicable": record.get("contextApplicable"),
        "contextHash": record.get("contextHash"),
        "createdAt": record.get("createdAt"),
        "returnedAt": record.get("returnedAt"),
        "returnSummary": record.get("returnSummary"),
        "rerunOf": record.get("rerunOf"),
        "supersededBy": record.get("supersededBy"),
    }
