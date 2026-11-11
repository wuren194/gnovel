"""Immutable workbench candidate versions for one chapter's prose.

浏览器工作台的候选正文生命周期（§22-§28）：

- **编辑缓冲**永远在浏览器本地，不落服务端；服务端只认识「已保存候选版本」。
- **已保存候选版本**不可变：正文文件按内容命名，写入后绝不改写；index 中的版本行
  追加后也不再修改。
- **当前登记候选**（active）由显式激活产生，需要回显 `activeToken`。这个 token 是
  candidate 版本槽自己的代次，与 `proseBinding.generation` 无关，禁止混用。
- **激活 = 原子失效**：激活新正文与失效旧证据（diagnostics / stage_b_review /
  state_candidate 工件条目）必须落在同一个写集事务里；事务中断要么完整回滚到旧状态，
  要么等 `workflow recover` 恢复，绝不停在「新 active + 旧有效证据」。
- **保存不是审校，激活不是批准**：本模块不改章节状态、不产生任何批准。
- CLI / MCP / HTTP 共用本模块（§59），不存在只有浏览器安全的路径。

存储布局（全部在 `.govel/` 下，core 受控）：

    .govel/workbench/candidates/<chapterId>/index.json
    .govel/workbench/candidates/<chapterId>/v0001-<sha16>.md
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .artifacts import sha256_text
from .clock import utc_now
from .jsonio import read_json
from .locks import project_lock_reentrant
from .paths import safe_project_path
from .structure_store import StructureError, load_structure

CANDIDATE_INDEX_SCHEMA_VERSION = 1
# 激活时必须失效的旧证据类型。candidate_prose 工件属于宿主工位登记流（§46），
# 不在本列：它由宿主在产出新登记时自然被新 hash 取代，而它的批准单独不构成
# finalize 能力（finalization_preflight 同时要求 diagnostics 与 stage B）。
from .evidence_invalidation import PROSE_DEPENDENT_EVIDENCE, invalidate_prose_evidence

ACTIVATION_INVALIDATED_EVIDENCE = PROSE_DEPENDENT_EVIDENCE
WORKBENCH_DIR = ".govel/workbench/candidates"


def _chapter(root: Path, chapter_id: str) -> dict[str, Any]:
    structure = load_structure(root)
    chapter = next((item for item in structure.get("chapters", []) if item.get("chapterId") == chapter_id), None)
    if chapter is None:
        raise StructureError("CHAPTER_NOT_FOUND", f"unknown chapter: {chapter_id}", {"chapterId": chapter_id})
    if chapter.get("status") == "archived":
        raise StructureError("CHAPTER_ARCHIVED", f"chapter is archived: {chapter_id}", {"chapterId": chapter_id})
    return chapter


def _chapter_dir(root: Path, chapter_id: str) -> Path:
    return safe_project_path(root, f"{WORKBENCH_DIR}/{chapter_id}")


def _index_path(root: Path, chapter_id: str) -> Path:
    return _chapter_dir(root, chapter_id) / "index.json"


def _empty_index(chapter_id: str, project_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": CANDIDATE_INDEX_SCHEMA_VERSION,
        "chapterId": chapter_id,
        "projectId": project_id,
        "versions": [],
        "activeVersion": None,
        "activeToken": 0,
        "invalidations": [],
        "requests": {},
    }


def load_candidate_index(root: Path, chapter_id: str, *, recovery_view: bool = False) -> dict[str, Any]:
    if not recovery_view:
        from .consistency import require_no_pending
        require_no_pending(root)
    path = _index_path(root, chapter_id)
    if not path.is_file():
        return _empty_index(chapter_id, "")
    index = read_json(path)
    return validate_candidate_index(index, chapter_id, project_id=load_structure(root)['projectId'])


def validate_candidate_index(index: object, chapter_id: str, *, project_id: str) -> dict[str, Any]:
    """Validate the same current index for readers and observed report snapshots."""
    if (not isinstance(index, dict) or type(index.get('schemaVersion')) is not int
            or index['schemaVersion'] != CANDIDATE_INDEX_SCHEMA_VERSION):
        raise StructureError("CANDIDATE_INDEX_INVALID", "candidate index is unreadable or unsupported")
    if (index.get('chapterId') != chapter_id or index.get('projectId') != project_id
            or not isinstance(index.get('versions'), list) or not isinstance(index.get('requests'), dict)
            or not isinstance(index.get('invalidations'), list) or 'activeVersion' not in index
            or type(index.get('activeToken')) is not int or index['activeToken'] < 0):
        raise StructureError('CANDIDATE_INDEX_INVALID', 'candidate index identity or activation state is invalid')
    versions = []
    for row in index['versions']:
        if not isinstance(row, dict) or type(row.get('version')) is not int or row['version'] < 1:
            raise StructureError('CANDIDATE_INDEX_INVALID', 'candidate versions must have positive integer identities')
        versions.append(row['version'])
    active = index['activeVersion']
    if (versions != sorted(set(versions))
            or (active is None and index['activeToken'] != 0)
            or (active is not None and (type(active) is not int or active not in versions or index['activeToken'] == 0))):
        raise StructureError('CANDIDATE_INDEX_INVALID', 'candidate active version and token are inconsistent')
    return index


def _version_row(index: dict[str, Any], version: int) -> dict[str, Any]:
    row = next((item for item in index["versions"] if item.get("version") == version), None)
    if row is None:
        raise StructureError("CANDIDATE_VERSION_NOT_FOUND", f"unknown candidate version: v{version}",
                             {"chapterId": index["chapterId"], "version": version})
    return row


def save_candidate_version(
    root: Path,
    chapter_id: str,
    *,
    body: str,
    request_id: str,
    base_candidate_version: int | None,
    source: str = "browser",
    handoff_id: str | None = None,
    allow_stale_base: bool = False,
) -> dict[str, Any]:
    """Save one immutable candidate version. Idempotent per (request_id, body).

    - 同 `request_id` + 同内容：原样返回首次收据（重放安全）。
    - 同 `request_id` + 不同内容：拒绝（CANDIDATE_REQUEST_CONFLICT）。
    - `base_candidate_version` 必须等于当前最新版本，否则 CANDIDATE_BASE_STALE；
      仅宿主迟到回执路径（`allow_stale_base=True`，handoff 来源）允许落后基线追加
      ——版本只追加、不覆盖，绝不改动已激活槽位。
    """
    root = root.expanduser().resolve()
    if not isinstance(body, str):
        raise StructureError("CANDIDATE_BODY_INVALID", "candidate body must be a string")
    if not isinstance(request_id, str) or not request_id.strip():
        raise StructureError("CANDIDATE_REQUEST_ID_REQUIRED", "requestId is required")
    request_id = request_id.strip()
    if len(request_id) > 200:
        raise StructureError("CANDIDATE_REQUEST_ID_INVALID", "requestId is too long")
    with project_lock_reentrant(root):
        chapter = _chapter(root, chapter_id)
        index = load_candidate_index(root, chapter_id)
        if not index.get("projectId"):
            index["projectId"] = load_structure(root).get("projectId", "")

        body_sha = sha256_text(body)
        from .structure_store import canonical_json
        request_hash = sha256_text(canonical_json({
            'bodySha256': body_sha, 'baseCandidateVersion': base_candidate_version,
            'source': source, 'handoffId': handoff_id, 'allowStaleBase': allow_stale_base,
        }))
        prior = index["requests"].get(request_id)
        if prior is not None:
            if prior.get("requestHash") == request_hash:
                return {**prior["receipt"], "replayed": True}
            raise StructureError(
                "CANDIDATE_REQUEST_CONFLICT",
                "requestId was already used with different frozen content or base",
                {"requestId": request_id, "recordedBodySha256": prior.get("bodySha256"), "bodySha256": body_sha},
            )

        latest = index["versions"][-1]["version"] if index["versions"] else 0
        if base_candidate_version is None:
            if latest != 0:
                raise StructureError(
                    "CANDIDATE_BASE_REQUIRED",
                    "candidate versions already exist; baseCandidateVersion is required",
                    {"latestVersion": latest},
                )
            base_was_stale = False
        else:
            if type(base_candidate_version) is not int or base_candidate_version < 0:
                raise StructureError("CANDIDATE_BASE_INVALID", "baseCandidateVersion must be a non-negative integer")
            if base_candidate_version > latest:
                raise StructureError("CANDIDATE_BASE_UNKNOWN", "baseCandidateVersion is ahead of the server",
                                     {"latestVersion": latest, "baseCandidateVersion": base_candidate_version})
            if base_candidate_version != latest and not allow_stale_base:
                raise StructureError(
                    "CANDIDATE_BASE_STALE",
                    "another candidate version was saved after this buffer was based",
                    {"baseCandidateVersion": base_candidate_version, "latestVersion": latest},
                )
            base_was_stale = base_candidate_version != latest

        version = latest + 1
        file_name = f"v{version:04d}-{body_sha[:16]}.md"
        body_path = _chapter_dir(root, chapter_id) / file_name
        payload = body.encode("utf-8")

        saved_at = utc_now()
        index["versions"].append({
            "version": version,
            "requestId": request_id,
            "bodySha256": body_sha,
            "fileName": file_name,
            "byteLength": len(payload),
            "createdAt": saved_at,
            "baseCandidateVersion": base_candidate_version,
            "baseWasStale": base_was_stale,
            "source": source,
            "handoffId": handoff_id,
        })
        receipt = {
            "ok": True,
            "code": "CANDIDATE_SAVED",
            "chapterId": chapter_id,
            "version": version,
            "requestId": request_id,
            "bodySha256": body_sha,
            "fileName": file_name,
            "byteLength": len(payload),
            "activeVersion": index["activeVersion"],
            "activeToken": index["activeToken"],
            "active": False,
            "baseCandidateVersion": base_candidate_version,
            "baseWasStale": base_was_stale,
            "savedAt": saved_at,
        }
        index["requests"][request_id] = {"version": version, "bodySha256": body_sha, "requestHash": request_hash, "receipt": receipt}
        from .write_sets import apply_write_set, json_bytes
        if body_path.exists():
            raise StructureError('CANDIDATE_VERSION_OCCUPIED', 'unregistered body occupies this version slot',
                                 {'chapterId': chapter_id, 'version': version, 'path': str(body_path)})
        apply_write_set(root, {
            str(body_path.relative_to(root)): payload,
            str(_index_path(root, chapter_id).relative_to(root)): json_bytes(index),
        }, operation='candidate_save')
        return receipt


def activate_candidate_version(
    root: Path,
    chapter_id: str,
    *,
    candidate_version: int,
    expected_active_token: int,
) -> dict[str, Any]:
    """Explicitly activate one saved version and atomically invalidate stale evidence.

    中断语义（§28）：index 与 workflow.json 走同一个写集事务；进程在任意点中断，
    `workflow recover` 把项目还原为完整旧状态，或恢复为完整新状态——绝不留下
    「新 active candidate + 旧有效证据」。存在未完成事务时，finalization 被
    `project_lock` 的 pending 检查挡住（TRANSACTION_INCOMPLETE）。
    """
    root = root.expanduser().resolve()
    from .locks import project_lock
    from .write_sets import apply_write_set, capture_guards, inventory, json_bytes

    if not isinstance(candidate_version, int) or not isinstance(expected_active_token, int):
        raise StructureError("CANDIDATE_ACTIVATE_INVALID", "candidateVersion and expectedActiveToken must be integers")
    # 激活会写 workflow.json（受保护写），必须拿互斥锁；重入形态让 CLI/MCP 外层锁不冲突。
    with project_lock(root):
        _chapter(root, chapter_id)
        index = load_candidate_index(root, chapter_id)
        row = _version_row(index, candidate_version)
        if index["activeToken"] != expected_active_token:
            raise StructureError(
                "CANDIDATE_ACTIVE_TOKEN_CONFLICT",
                "active token changed; another client activated a different candidate",
                {"expectedActiveToken": expected_active_token, "currentActiveToken": index["activeToken"],
                 "currentActiveVersion": index["activeVersion"]},
            )
        changed = candidate_version != index["activeVersion"]
        if not changed:
            return {
                "ok": True, "code": "CANDIDATE_ACTIVATED", "chapterId": chapter_id,
                "activatedVersion": candidate_version, "previousActiveVersion": index["activeVersion"],
                "activeVersion": index["activeVersion"], "activeToken": index["activeToken"],
                "invalidatedArtifacts": {}, "invalidationId": None, "changed": False,
            }

        workflow_path = safe_project_path(root, ".govel/workflow.json")
        workflow = read_json(workflow_path) if workflow_path.is_file() else None
        invalidated: dict[str, str] = {}
        invalidation_id = f"INV-{candidate_version:04d}-{utc_now().replace('-', '').replace(':', '').replace('.', '')}"
        if workflow is not None:
            invalidated = invalidate_prose_evidence(workflow, chapter_id, {
                'reason': 'candidate_activation', 'invalidationId': invalidation_id,
                'candidateVersion': candidate_version, 'invalidatedAt': utc_now()})
            approval = workflow.get("approvals", {}).get(chapter_id)
            if isinstance(approval, dict) and invalidated:
                # 历史保留：不删除、不改 approved 之外的既有字段，只追加失效印记。
                approval["invalidatedByCandidateActivation"] = {
                    "invalidationId": invalidation_id,
                    "candidateVersion": candidate_version,
                    "invalidatedAt": utc_now(),
                }

        new_index = {
            **index,
            "activeVersion": candidate_version,
            "activeToken": index["activeToken"] + 1,
            "invalidations": [*index["invalidations"], {
                "invalidationId": invalidation_id,
                "activatedAt": utc_now(),
                "activatedVersion": candidate_version,
                "previousActiveVersion": index["activeVersion"],
                "previousActiveToken": index["activeToken"],
                "invalidatedArtifacts": invalidated,
            }],
        }

        outputs = {
            f"{WORKBENCH_DIR}/{chapter_id}/index.json": json_bytes(new_index),
            ".govel/workflow.json": json_bytes(workflow) if workflow is not None else None,
        }
        # 激活的版本正文文件是本事务的前提证据：提交窗口内被外部改动则整体失败。
        guards = capture_guards(root, [f"{WORKBENCH_DIR}/{chapter_id}/{row['fileName']}"])
        apply_write_set(root, outputs, operation="candidate_activate", guards=guards)
        return {
            "ok": True,
            "code": "CANDIDATE_ACTIVATED",
            "chapterId": chapter_id,
            "activatedVersion": candidate_version,
            "previousActiveVersion": index["activeVersion"],
            "activeVersion": candidate_version,
            "activeToken": new_index["activeToken"],
            "invalidatedArtifacts": invalidated,
            "invalidationId": invalidation_id,
            "changed": True,
        }


def list_candidate_versions(root: Path, chapter_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    _chapter(root, chapter_id)
    index = load_candidate_index(root, chapter_id)
    return {
        "ok": True,
        "code": "CANDIDATE_VERSIONS",
        "chapterId": chapter_id,
        "projectId": index.get("projectId", ""),
        "versions": index["versions"],
        "activeVersion": index["activeVersion"],
        "activeToken": index["activeToken"],
        "recovery": candidate_recovery_status(root, chapter_id),
    }


def read_candidate_version(root: Path, chapter_id: str, version: int) -> dict[str, Any]:
    root = root.expanduser().resolve()
    _chapter(root, chapter_id)
    index = load_candidate_index(root, chapter_id)
    row = _version_row(index, version)
    path = _chapter_dir(root, chapter_id) / row["fileName"]
    if not path.is_file():
        raise StructureError("CANDIDATE_VERSION_CORRUPT", "candidate body file is missing",
                             {"chapterId": chapter_id, "version": version, "fileName": row["fileName"]})
    payload = path.read_bytes()
    body = payload.decode("utf-8")
    body_sha = sha256_text(body)
    if body_sha != row["bodySha256"]:
        raise StructureError("CANDIDATE_VERSION_CORRUPT", "candidate body bytes no longer match the recorded hash",
                             {"chapterId": chapter_id, "version": version,
                              "recordedSha256": row["bodySha256"], "actualSha256": body_sha})
    return {
        "ok": True,
        "code": "CANDIDATE_READ",
        "chapterId": chapter_id,
        "version": version,
        "bodySha256": body_sha,
        "byteLength": row["byteLength"],
        "createdAt": row["createdAt"],
        "source": row.get("source", ""),
        "handoffId": row.get("handoffId"),
        "baseWasStale": row.get("baseWasStale", False),
        "active": index["activeVersion"] == version,
        "activeVersion": index["activeVersion"],
        "activeToken": index["activeToken"],
        "body": body,
    }


def candidate_recovery_status(root: Path, chapter_id: str) -> dict[str, Any]:
    """已保存未激活 / 激活中断的恢复视图（§28）。

    `pendingTransactions` 非空即表示激活写集中断：恢复完成前 finalization 被
    project_lock 拦截；UI 必须显示可恢复状态而不是假装一切正常。
    """
    root = root.expanduser().resolve()
    _chapter(root, chapter_id)
    index = load_candidate_index(root, chapter_id, recovery_view=True)
    transactions_dir = safe_project_path(root, ".govel/transactions")
    pending = []
    if transactions_dir.is_dir():
        for tx_path in sorted(transactions_dir.glob("*.json")):
            try:
                manifest = read_json(tx_path)
            except (OSError, ValueError):
                pending.append({"transactionId": tx_path.stem, "phase": "unreadable", "touchesWorkbench": None})
                continue
            rows = manifest.get("files") or []
            touches = any(f"{WORKBENCH_DIR}/{chapter_id}/" in str(row.get("path", "")) or row.get("path") == ".govel/workflow.json" for row in rows)
            pending.append({"transactionId": manifest.get("transactionId", tx_path.stem),
                            "phase": manifest.get("phase", "unknown"), "operation": manifest.get("operation", ""),
                            "touchesWorkbench": touches})
    chapter_dir = _chapter_dir(root, chapter_id)
    recorded = {row["fileName"] for row in index["versions"]}
    orphans = sorted(path.name for path in chapter_dir.glob("v*.md") if path.name not in recorded) if chapter_dir.is_dir() else []
    latest = index["versions"][-1]["version"] if index["versions"] else 0
    return {
        "latestVersion": latest,
        "activeVersion": index["activeVersion"],
        "activeToken": index["activeToken"],
        "savedNotActive": latest > 0 and (index["activeVersion"] or 0) < latest,
        "pendingTransactions": pending,
        "blockedByRecovery": any(item.get("phase") in {"prepared", "rollback"} or item.get("phase") == "unreadable" for item in pending),
        "orphanBodyFiles": orphans,
        "consistent": not pending and not orphans,
    }
