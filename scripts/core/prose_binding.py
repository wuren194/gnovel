"""Current Ownership Binding core (R1 Phase 1 — Binding Core & Schema Foundation).

This module is the single home of the current-state binding model:

* `chapter.filePath` is the only authoritative prose locator;
* `chapter.proseBinding` (state / generation / expectedContentDigest /
  integrityDigest) is the current ownership authority;
* `integrityDigest` is a Level-1 partial-corruption detector over a canonical
  JSON payload, never a signature and never ownership identity;
* canonical locator uniqueness is a hard structure invariant.

Discipline frozen by `docs/R1-CURRENT-OWNERSHIP-BINDING.md`:

* this module NEVER reads operation journals, continuity records, undo
  receipts/authority, historical backups or lineage evidence. A healthy
  current-format binding must be explainable from the current structure (and, when
  a check needs it, the current disk state) alone;
* an integrity mismatch is corruption: it is reported (`OWNERSHIP_BINDING_INVALID`),
  never silently recomputed or resealed by load/normalize/save/cache paths;
* There is no fallback from a missing/invalid binding to historical evidence.
"""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path

from .project import source_model_policy
from .structure_store import StructureError, canonical_json

# digest 正文格式：sha256:<64 lowercase hex>。写侧永远产生小写；读侧只接受小写，
# 不做大小写归一（无 silent normalization）。
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# 受保护所有权字段（契约 §5.1）。全部挂在 chapter 条目上；proseBinding 整体受保护。
PROTECTED_CHAPTER_FIELDS = ("filePath", "proseBinding")

BINDING_STATES = frozenset({"unbound", "managed", "unresolved"})

# Formal transition kinds that may change a chapter's protected ownership fields on an
# already-existing chapter (contract §4). `materialize` is the Phase-1 kind for a chapter
# identity the transaction itself creates; Phase 2 adds the five current-binding kinds;
# Phase 3 adds `formal_undo`, the reverse of one committed operation — it restores the
# *semantic* fields (filePath / state / expectedContentDigest) the target operation
# changed and seals them at the undo's own new revision. It is deliberately NOT a kind a
# plan can declare: `require_plan_transitions_current` has no observed state for it and
# refuses such a plan, so the only producer is the monotonic undo runtime.
# There is deliberately ONE set: producers, the applier and the writer boundary all read it.
TRANSITION_KINDS = frozenset({"materialize", "first_finalize", "refinalize",
                              "accept_external", "move_managed", "move_unbound",
                              "formal_undo"})

# The restore targets a formal undo may seal: exactly the persisted before-states the
# forward transitions start from. An undo never invents `unresolved` and never resurrects
# a historical generation/integrityDigest (契约 §4 Formal Undo 行)。
_FORMAL_UNDO_RESTORE_STATES = frozenset({"unbound", "managed"})

# The persisted state each Phase-2 transition requires as its before-state（契约 §4 矩阵）。
_TRANSITION_BEFORE_STATE = {
    "first_finalize": "unbound",
    "refinalize": "managed",
    "accept_external": "managed",
    "move_managed": "managed",
    "move_unbound": "unbound",
}

# 高风险所有权流转必须由**独立特权批准通道**授权（契约 §10.1，Phase 2 返修）。通道要成立的
# 条件是它不可由普通调用者伪造：不是 string、不是 bool、不是 CLI flag、不是 planHash，且
# approval 绑定 exact dangerous action + exact plan。本仓库当前不存在任何这样的通道——
# 既无签名机制也无独立进程/OS 凭据边界，仓库自身的声明式审批一律记 `verifiedHuman: False`
# ——因此这些流转没有任何可提交路径：commit 在锁内、任何写入之前以 `APPROVAL_CAPABILITY_REQUIRED`
# fail closed，直到具名 capability 通道在后续阶段落地。
PRIVILEGED_APPROVAL_TRANSITIONS = frozenset({"accept_external"})
PRIVILEGED_APPROVAL_OPERATIONS = frozenset({"accept_external_modification"})


def binding_format() -> str:
    """PROSE_BINDING_V1 的唯一真源在注册表；这里提供单一读取点。"""
    return source_model_policy()["proseBinding"]["format"]


def is_ownership_aware_structure(structure: dict) -> bool:
    from .source_model import is_current_binding
    return is_current_binding(structure)


# ---------------------------------------------------------------------------
# integrityDigest
# ---------------------------------------------------------------------------

def binding_payload(project_id: str, chapter_id: str, file_path: str, state: str,
                    generation: int, expected_content_digest: str | None) -> dict:
    """Canonical digest payload. The field set is frozen; never add or reorder semantics."""
    return {
        "bindingFormat": binding_format(),
        "projectId": project_id,
        "chapterId": chapter_id,
        "filePath": file_path,
        "state": state,
        "generation": generation,
        "expectedContentDigest": expected_content_digest,
    }


def binding_integrity_digest(project_id: str, chapter_id: str, file_path: str, state: str,
                             generation: int, expected_content_digest: str | None) -> str:
    payload = canonical_json(binding_payload(project_id, chapter_id, file_path, state,
                                             generation, expected_content_digest))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def seal_binding(project_id: str, chapter_id: str, file_path: str, *, state: str,
                 generation: int, expected_content_digest: str | None) -> dict:
    """The only way a proseBinding is ever written: compute and attach integrityDigest.

    Sealing is reserved for formal binding transitions and materialization. Generic
    save paths never call this — that is what makes a recomputed digest meaningful.
    """
    return {
        "state": state,
        "generation": generation,
        "expectedContentDigest": expected_content_digest,
        "integrityDigest": binding_integrity_digest(project_id, chapter_id, file_path, state,
                                                    generation, expected_content_digest),
    }


def materialization_binding(structure: dict, chapter: dict, *, generation: int) -> dict:
    """Sealed `unbound` binding for Chapter Materialization（契约 §4 行 1）。"""
    return seal_binding(structure["projectId"], chapter["chapterId"], chapter["filePath"],
                        state="unbound", generation=generation, expected_content_digest=None)


# ---------------------------------------------------------------------------
# Locator canonicalization — delegates to the project's single canonical key
# ---------------------------------------------------------------------------

def canonical_locator_key(relative: str) -> str:
    """The one canonical prose-locator key（NFC + casefold + 分隔符统一）。"""
    from .refactor_paths import canonical_key
    return canonical_key(relative)


def _locator_interpretable(file_path: object) -> bool:
    """Pure structural safety for a locator, without touching the filesystem.

    Full root-resolution safety（symlink、越界）仍由 safe_project_path 在读写时把关；
    这里只决定一个定位符能否被结构保留（reserve）。
    """
    if not isinstance(file_path, str):
        return False
    text = file_path
    if not text or text != text.strip():
        return False
    if text.startswith("/") or text == "~" or text.startswith("~/"):
        return False
    if "\\" in text or "\x00" in text:
        return False
    parts = text.split("/")
    return all(part not in ("", ".", "..") for part in parts)


# ---------------------------------------------------------------------------
# Binding validation — global-fatal vs chapter-local（实施轮 §27/§28）
# ---------------------------------------------------------------------------

def _binding_error(chapter_id: object, message: str, **details) -> dict:
    label = chapter_id if isinstance(chapter_id, str) and chapter_id else "<unknown>"
    return {"code": "OWNERSHIP_BINDING_INVALID", "chapterId": label,
            "message": message, "severity": "chapter_local", **details}


def validate_binding(structure: dict, chapter: dict) -> list[dict]:
    """Chapter-local binding validation. Returns a list of errors (empty = healthy).

    Never raises and never repairs. Field-shape errors are reported in a fixed
    order and the integrity check runs last, only over well-formed fields, so a
    malformed field never masquerades as (or hides behind) a digest mismatch.
    """
    chapter_id = chapter.get("chapterId")
    binding = chapter.get("proseBinding")
    if binding is None:
        return [_binding_error(chapter_id, "formal chapter has no proseBinding; "
                               "ownership-aware schemas require a binding on every chapter",
                               field="proseBinding")]
    if not isinstance(binding, dict):
        return [_binding_error(chapter_id, "proseBinding must be an object", field="proseBinding")]
    errors: list[dict] = []
    unknown = sorted(set(binding) - {"state", "generation", "expectedContentDigest", "integrityDigest"})
    if unknown:
        errors.append(_binding_error(chapter_id, "proseBinding carries unknown fields",
                                     field="proseBinding", fields=unknown))
    state = binding.get("state")
    if state not in BINDING_STATES:
        errors.append(_binding_error(chapter_id, f"binding state must be one of {sorted(BINDING_STATES)}",
                                     field="state",
                                     value=state if (state is None or isinstance(state, str)) else str(type(state))))
    generation = binding.get("generation")
    if type(generation) is not int or generation < 1:
        errors.append(_binding_error(chapter_id, "binding generation must be an integer >= 1",
                                     field="generation",
                                     value=generation if (generation is None or isinstance(generation, (int, float, str))) else str(type(generation))))
    digest_value = binding.get("expectedContentDigest")
    digest_ok = digest_value is None or (isinstance(digest_value, str) and bool(_DIGEST_PATTERN.fullmatch(digest_value)))
    if not digest_ok:
        errors.append(_binding_error(chapter_id, "expectedContentDigest must be null or sha256:<64 lowercase hex>",
                                     field="expectedContentDigest"))
    if state == "unbound" and digest_value is not None:
        errors.append(_binding_error(chapter_id, "unbound binding must not declare an expected content digest",
                                     field="expectedContentDigest"))
    if state == "managed" and not (isinstance(digest_value, str) and _DIGEST_PATTERN.fullmatch(digest_value)):
        errors.append(_binding_error(chapter_id, "managed binding requires the accepted prose sha256 digest",
                                     field="expectedContentDigest"))
    # unresolved：null 与最后仍可信的 accepted digest 都合法；两种取值都绝不授予
    # 覆盖/定稿/接管权限——那由后续阶段的运行时按 state 本身拒绝，本模块只保证
    # 不把 unresolved 归一成 managed。
    if errors:
        return errors
    integrity = binding.get("integrityDigest")
    if not (isinstance(integrity, str) and _DIGEST_PATTERN.fullmatch(integrity)):
        return [_binding_error(chapter_id, "integrityDigest must be sha256:<64 lowercase hex>",
                               field="integrityDigest")]
    expected = binding_integrity_digest(structure.get("projectId"), chapter_id,
                                        chapter.get("filePath"), state, generation, digest_value)
    if integrity != expected:
        return [_binding_error(chapter_id, "integrityDigest does not match the current binding fields; "
                               "the binding is corrupt or was copied across identities",
                               field="integrityDigest")]
    return []


def binding_token(chapter: dict) -> dict | None:
    """The per-binding request token a client must echo back（契约 §7.1）。

    Staleness compares `request.bindingGeneration == chapter.proseBinding.generation`
    — never the global structure revision.
    """
    binding = chapter.get("proseBinding")
    if not isinstance(binding, dict):
        return None
    return {"chapterId": chapter.get("chapterId"),
            "filePath": chapter.get("filePath"),
            "state": binding.get("state"),
            "bindingGeneration": binding.get("generation"),
            "expectedContentDigest": binding.get("expectedContentDigest")}


def request_generation_current(chapter: dict, request_binding_generation: object) -> bool:
    """True only when the request's binding generation is THIS binding's generation."""
    binding = chapter.get("proseBinding")
    if not isinstance(binding, dict):
        return False
    generation = binding.get("generation")
    return type(request_binding_generation) is int and request_binding_generation == generation


# ---------------------------------------------------------------------------
# Locator reservation & uniqueness（契约 §6 / 实施轮 §29/§30）
# ---------------------------------------------------------------------------

def locator_reservations(structure: dict) -> tuple[dict[str, str], list[dict]]:
    """Canonical key -> chapterId for every formal chapter that can reserve its locator.

    managed / unbound / unresolved / archived 乃至 chapter-local BINDING_INVALID 的章
    都保留定位符（binding 损坏绝不释放槽位）；只有 chapterId 或 filePath 连安全解释
    都做不到的章才保留失败——而那会作为 fatal 上报，绝不会静默跳过。
    """
    reservations: dict[str, str] = {}
    fatal: list[dict] = []
    for chapter in structure.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        chapter_id = chapter.get("chapterId")
        file_path = chapter.get("filePath")
        if not isinstance(chapter_id, str) or not chapter_id:
            fatal.append({"code": "PROSE_LOCATOR_UNRESERVABLE",
                          "message": "chapter identity is not a usable string; its locator cannot be reserved"})
            continue
        if not _locator_interpretable(file_path):
            fatal.append({"code": "PROSE_LOCATOR_UNSAFE",
                          "message": "filePath is not a safe canonical project-relative locator",
                          "chapterId": chapter_id,
                          "filePath": file_path if (file_path is None or isinstance(file_path, str)) else str(type(file_path))})
            continue
        key = canonical_locator_key(file_path)
        previous = reservations.get(key)
        if previous is not None and previous != chapter_id:
            fatal.append({"code": "PROSE_LOCATOR_CONFLICT",
                          "message": "two chapters reserve the same canonical prose locator",
                          "canonicalKey": key, "chapters": sorted({previous, chapter_id})})
        reservations[key] = chapter_id
    return reservations, fatal


def require_unique_locators(structure: dict) -> dict[str, str]:
    """Hard structure invariant for current-format: raise on any canonical collision."""
    reservations, fatal = locator_reservations(structure)
    if fatal:
        raise StructureError("STRUCTURE_INVALID", "prose locator invariant violated",
                             {"errors": fatal})
    return reservations


def hardlink_object_conflicts(root: Path, structure: dict) -> list[dict]:
    """Object-alias occupancy conflicts: two distinct locators resolving to one inode.

    Filesystem-dependent by nature, so it is deliberately NOT part of the pure
    structure validator（实施轮 §32）；未来的 commit-time transaction 在落地定位符
    变更前必须把它与 `require_unique_locators` 一起运行。
    """
    from .refactor_paths import resolve, same_file
    resolved: list[tuple[str, str, Path]] = []
    for chapter in structure.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        file_path = chapter.get("filePath")
        if not _locator_interpretable(file_path):
            continue
        try:
            path = resolve(root, file_path)
        except StructureError:
            continue
        if path.is_file():
            resolved.append((chapter.get("chapterId", ""), file_path, path))
    conflicts: list[dict] = []
    for index, (chapter_id, relative, path) in enumerate(resolved):
        for other_id, other_relative, other in resolved[index + 1:]:
            if same_file(path, other):
                conflicts.append({"code": "PROSE_LOCATOR_CONFLICT",
                                  "message": "two locators alias the same physical file object",
                                  "chapters": sorted({chapter_id, other_id}),
                                  "locators": [relative, other_relative]})
    return conflicts


# ---------------------------------------------------------------------------
# Ownership validation report — the shared strict validator（实施轮 §28）
# ---------------------------------------------------------------------------

def ownership_validation_report(structure: dict) -> dict:
    """Strict ownership validation, aggregated so callers can separate the two severities.

    * `fatal`: identity/locator-level corruption — the project's authority as a
      whole cannot be interpreted（projectId、chapterId、filePath 安全性、规范定位符
      唯一性）。`validate_structure` 对这些直接 STRUCTURE_INVALID。
    * `chapterLocal`: per-chapter binding corruption on an otherwise interpretable
      chapter — 该章保持 BINDING_INVALID、继续保留定位符，健康兄弟章继续工作。
      `validate_structure` 有意不对这些抛错；需要完整 binding 健康的调用方走
      `require_binding_health`。
    """
    fatal: list[dict] = []
    chapter_local: dict[str, list[dict]] = {}
    project_id = structure.get("projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        fatal.append({"code": "PROJECT_IDENTITY_INVALID",
                      "message": "projectId must be a non-empty string for ownership-aware schemas"})
    for chapter in structure.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        errors = validate_binding(structure, chapter)
        if errors:
            chapter_id = chapter.get("chapterId")
            key = chapter_id if isinstance(chapter_id, str) and chapter_id else "<unknown>"
            chapter_local.setdefault(key, []).extend(errors)
    reservations, locator_fatal = locator_reservations(structure)
    fatal.extend(locator_fatal)
    return {"fatal": fatal, "chapterLocal": chapter_local,
            "reservations": reservations,
            "schemaVersion": structure.get("schemaVersion")}


def ownership_fatal_errors(structure: dict) -> list[dict]:
    """Errors `validate_structure` must treat as STRUCTURE_INVALID for current-format."""
    return ownership_validation_report(structure)["fatal"]


def require_global_ownership_safety(structure: dict) -> dict:
    """Raise only for project-fatal ownership corruption (identity / locator level).

    This is the gate every formal transition needs: the project's authority as a whole
    must be interpretable. Chapter-local binding damage is deliberately NOT fatal —
    it stays isolated in its own chapter (BINDING_INVALID, locator reserved) while
    healthy siblings keep working（契约 §2.2 Level 2 / Phase 0.2）。
    """
    report = ownership_validation_report(structure)
    if report["fatal"]:
        raise StructureError("STRUCTURE_INVALID", "structure failed ownership validation",
                             {"errors": report["fatal"]})
    return report


def require_all_binding_health(structure: dict) -> dict:
    """Raise unless EVERY chapter binding is healthy.

    Only for callers that genuinely own the whole structure's health — a fresh
    import sealing every binding at once. Formal transitions must NOT use this
    (实施轮 §43): one damaged chapter never blocks a healthy chapter's transition.
    """
    report = require_global_ownership_safety(structure)
    if report["chapterLocal"]:
        raise StructureError("OWNERSHIP_BINDING_INVALID",
                             "chapter binding is corrupt; explicit repair is required",
                             {"chapters": report["chapterLocal"]})
    return report


def require_target_binding_health(structure: dict, chapter_id: object) -> dict:
    """Raise unless THIS chapter's binding is healthy; unrelated damage is ignored."""
    chapter = _chapter_by_id(structure, chapter_id)
    if chapter is None:
        _reject("OWNERSHIP_BINDING_INVALID", "binding health target chapter does not exist",
                chapterId=chapter_id)
    errors = validate_binding(structure, chapter)
    if errors:
        raise StructureError("OWNERSHIP_BINDING_INVALID",
                             "chapter binding is corrupt; explicit repair is required",
                             {"chapters": {chapter_id: errors}})
    return {"chapterId": chapter_id, "errors": []}


# ---------------------------------------------------------------------------
# Protected writer boundary（契约 §5.3 Option B / 实施轮 §24–§26）
# ---------------------------------------------------------------------------

def _protected_projection(chapter: dict) -> dict:
    return {field: chapter.get(field) for field in PROTECTED_CHAPTER_FIELDS}


def _protected_diff(current: dict, proposed: dict) -> tuple[set[str], set[str], set[str]]:
    """(changed existing chapters, removed chapters, added chapters) by chapterId."""
    current_rows = {row.get("chapterId"): row for row in current.get("chapters", [])
                    if isinstance(row, dict)}
    proposed_rows = {row.get("chapterId"): row for row in proposed.get("chapters", [])
                     if isinstance(row, dict)}
    changed = {cid for cid in current_rows.keys() & proposed_rows.keys()
               if canonical_json(_protected_projection(current_rows[cid]))
               != canonical_json(_protected_projection(proposed_rows[cid]))}
    return changed, set(current_rows) - set(proposed_rows), set(proposed_rows) - set(current_rows)


def _chapter_by_id(structure: dict, chapter_id: object) -> dict | None:
    if not isinstance(chapter_id, str):
        return None
    for row in structure.get("chapters", []):
        if isinstance(row, dict) and row.get("chapterId") == chapter_id:
            return row
    return None


class _BindingAuthority:
    """Module-internal capability carried by a formal transition application.

    Not a security boundary against arbitrary Python（Level-1 threat model）；它的
    作用是让通用保存路径没有任何途径在不经过本模块 transition 逻辑的情况下授权
    受保护字段变更（契约 §5.3 Option B）。
    """

    __slots__ = ("transitions",)

    def __init__(self, transitions: dict[str, dict]) -> None:
        self.transitions = transitions


class _InitializerAuthority:
    """The capability that lets a GREENFIELD initial structure land.

    R1 Phase 5 makes current-binding the greenfield default, so `_initialize` must be able
    to write the *first* `structure.json` of an ownership-aware project — a file that
    `save_structure`'s Option A backstop otherwise refuses to create for anyone. The
    capability is an object, not a boolean: the caller must hold something only this module
    can mint, and it authorizes exactly one thing — a project-level creation under the
    sanctioned initializer channel. It never authorizes a binding claim.
    """

    __slots__ = ("channel",)

    def __init__(self, channel: str) -> None:
        self.channel = channel


def initializer_authority(channel: str) -> _InitializerAuthority:
    """Mint the sanctioned-initializer capability. Only the project initializer calls this."""
    if not isinstance(channel, str) or not channel.strip():
        raise StructureError("OWNERSHIP_UNAUTHORIZED_MUTATION",
                             "a sanctioned initializer must name its channel")
    return _InitializerAuthority(channel)


def is_initializer_authority(value: object) -> bool:
    """True only for a capability this module minted（`structure_store` 的 Option A 判据）。"""
    return isinstance(value, _InitializerAuthority)


def _reject(code: str, message: str, **details) -> None:
    raise StructureError(code, message, details)


def _check_materialized_chapter(proposed: dict, chapter: dict, *, root: Path | None,
                                require_disk_absent: bool, content_digest: str | None = None) -> None:
    """Seal a new empty chapter, or the exact manuscript validated by its intake transaction."""
    chapter_id = chapter.get("chapterId")
    errors = validate_binding(proposed, chapter)
    if errors:
        _reject("OWNERSHIP_BINDING_INVALID", "materialized chapter binding is not valid",
                chapterId=chapter_id, errors=errors)
    binding = chapter["proseBinding"]
    revision = proposed.get("revision")
    if type(revision) is not int or binding.get("generation") != revision:
        _reject("OWNERSHIP_BINDING_INVALID",
                "materialization generation must equal the committed structure revision",
                chapterId=chapter_id, generation=binding.get("generation"), revision=revision)
    expected_state = 'managed' if content_digest is not None else 'unbound'
    if content_digest is not None:
        _require_content_digest(content_digest, field='materializationContentDigest', chapter_id=chapter_id)
    if binding.get("state") != expected_state or binding.get("expectedContentDigest") != content_digest:
        _reject("OWNERSHIP_BINDING_INVALID",
                "materialization must seal the state and exact content authorized by its creation operation",
                chapterId=chapter_id)
    if require_disk_absent:
        if root is None:
            _reject("OWNERSHIP_BINDING_INVALID",
                    "materialization requires the project root to verify the target slot is empty")
        from .refactor_paths import resolve
        target = resolve(root, str(chapter.get("filePath")))
        if target.exists() or target.is_symlink():
            _reject("PROSE_TARGET_NOT_OWNED",
                    "materialization target already holds a file; existing prose is never auto-adopted",
                    chapterId=chapter_id, filePath=chapter.get("filePath"))


def enforce_writer_boundary(current: dict | None, proposed: dict, *,
                            authority: _BindingAuthority | None = None,
                            root: Path | None = None,
                            materialization_chapter_ids: frozenset[str] | set[str] | None = None,
                            materialization_content_digests: dict[str, str] | None = None) -> None:
    """Option B boundary: generic writers may never mutate protected ownership fields.

    Runs whenever either side of a save is ownership-aware. Allowed protected-field
    changes, and nothing else:

    * chapters listed in a formal transition authority, whose sealed binding must match
      exactly what the transition produced（`authority.transitions == changed | added`）；
    * chapters NEW in the proposal covered by the caller's exact materialization
      capability — `materialization_chapter_ids` must equal the added chapter-ID set
      **exactly**：不是 ⊆（不能多夹带），也不是 ⊇（不能少建/换建）。空集与 None 都表示
      「本通道没有任何建章能力」，此时任何 added chapter 一律拒绝（实施轮 §38）；反向同样
      成立：capability 非空而实际一个没建（`added ⊂ authorized`）也是越权提案，拒绝。

    通用写者的 `materialization_chapter_ids` 恒为空：`save_structure` 与写集事务之外的
    任何调用方都没有途径新增 chapter。事务通道的能力由 `structure_transaction` 从
    「operation 名 + `plan.parameters.chapterId`」精确导出，绝不是一个 broad bool。

    Everything else — filePath 改动、binding 改动、删章、加章、projectId/chapterId 换
    identity、整模型升降级 — fail closed，抛 OWNERSHIP_UNAUTHORIZED_MUTATION。projectId
    是 immutable identity（实施轮 §22）。
    """
    proposed_aware = is_ownership_aware_structure(proposed)
    current_aware = current is not None and is_ownership_aware_structure(current)
    if not proposed_aware and not current_aware:
        return
    if current is not None and proposed_aware != current_aware:
        # R1 Phase 5：ownership model 是项目级事实，generic save 从来不是它的 transition API
        # （契约 §5.3）。此前这条只在「提案里带了 binding / 加了章」时才被间接拦住，于是一个
        # 只改 `schemaVersion` 的通用保存能把 legacy 项目「升格」成 current-binding：模型换了，
        # 却没有任何一次正式 transition、也没有任何一枚 binding 被密封。这里按模型本身 fail closed。
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "the ownership model of an existing project is not a generic writer's to change; "
                "it moves only through the ownership-migration cutover (or its rollback)",
                current={"schemaVersion": current.get("schemaVersion"),
                         "ownershipAware": current_aware},
                proposed={"schemaVersion": proposed.get("schemaVersion"),
                          "ownershipAware": proposed_aware})
    if current is not None and current.get("projectId") != proposed.get("projectId"):
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION", "projectId is immutable identity",
                current=current.get("projectId"), proposed=proposed.get("projectId"))
    changed, removed, added = _protected_diff(current if current is not None else {"chapters": []},
                                              proposed)
    if removed:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "removing a chapter would release its reserved locator; no such generic path exists",
                chapters=sorted(removed))
    if changed and authority is None:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "protected ownership fields changed outside a formal binding transition",
                chapters=sorted(changed))
    if added:
        unsealed = sorted(cid for cid in added
                          if not isinstance(_chapter_by_id(proposed, cid).get("proseBinding"), dict))
        if unsealed:
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                    "new chapters on ownership-aware schemas require a sealed materialization binding",
                    chapters=unsealed)
    if authority is not None:
        authorized = set(authority.transitions)
        if authorized != (changed | added):
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                    "transition authority does not exactly cover the protected-field changes",
                    authorized=sorted(authorized), changed=sorted(changed | added))
        for chapter_id, record in authority.transitions.items():
            kind = record.get("kind")
            if kind not in TRANSITION_KINDS:
                _reject("OWNERSHIP_MODEL_UNSUPPORTED_OPERATION",
                        "transition kind is not implemented", kind=kind, chapterId=chapter_id)
            chapter = _chapter_by_id(proposed, chapter_id)
            if chapter is None:
                _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                        "authorized chapter is missing from the proposed structure",
                        chapterId=chapter_id)
            before = _chapter_by_id(current, chapter_id) if current is not None else None
            if kind == "materialize":
                if before is not None and before.get("filePath") != chapter.get("filePath"):
                    _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                            "materialization never moves a locator", chapterId=chapter_id)
                _check_materialized_chapter(proposed, chapter, root=root, require_disk_absent=True)
                continue
            # 精确覆盖不是「章在这份列表里」：声明的 transition 必须在**当前结构 + 提交版本**
            # 上重新推导出与提案完全相同的 filePath / proseBinding。提案里手搓的 digest、
            # 伪造的 generation、或「挂个 kind 但其实改了别的字段」都在这里露馅（契约 §64/§66）。
            if before is None:
                _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                        "a current-binding transition applies to an existing chapter, never a new identity",
                        chapterId=chapter_id, kind=kind)
            result = binding_transition_result(proposed, before, record,
                                               generation=proposed.get("revision"))
            if (chapter.get("filePath") != result["filePath"]
                    or chapter.get("proseBinding") != result["proseBinding"]):
                _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                        "declared transition does not produce the proposed protected fields",
                        chapterId=chapter_id, kind=kind)
        return
    if current is None:
        # 初次创建（磁盘上尚无 structure.json）：没有可保护的旧状态，但仍要求每个自带
        # binding 的新章都是合法密封，绝不允许腐败 binding 出生。（ownership-aware 初次
        # 创建在 save_structure 层已被 Option A 整类阻断，这里是纯自洽检查。）
        for chapter_id in sorted(added):
            errors = validate_binding(proposed, _chapter_by_id(proposed, chapter_id))
            if errors:
                _reject("OWNERSHIP_BINDING_INVALID", "initial binding is not valid",
                        chapterId=chapter_id, errors=errors)
        return
    # 既有 ownership-aware 工程：added chapter 集合必须与 capability **精确相等**，否则拒绝。
    # 比较无条件执行——`authorized = {A}` 而实际零新增（added ⊂ authorized）与 added 非空一样
    # 是越权提案，不能因为「没有新增」就提前放行；空的 capability 与空的实际新增（普通元数据
    # 保存）才互为合法对被。合法的密封 binding 只说明「这看起来像一次 materialization」，
    # 不构成授权——generic save != materialization authority（实施轮 §23–§26）。
    allowed = set(materialization_chapter_ids or ())
    actual = set(added)
    if actual != allowed:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "new chapters on an existing ownership-aware project require an exact "
                "materialization capability bound to the operation's declared chapterId",
                added=sorted(actual), authorized=sorted(allowed))
    content = materialization_content_digests or {}
    if content and set(content) != actual:
        _reject('OWNERSHIP_UNAUTHORIZED_MUTATION',
                'manuscript content authority must exactly cover the new chapters',
                added=sorted(actual), authorized=sorted(content))
    for chapter_id in sorted(actual):
        _check_materialized_chapter(proposed, _chapter_by_id(proposed, chapter_id),
                                    root=root, require_disk_absent=True, content_digest=content.get(chapter_id))


# ---------------------------------------------------------------------------
# Formal binding transitions（Phase 1：materialize foundation only）
# ---------------------------------------------------------------------------

def _materialization_target(current: dict, snapshot: dict, chapter_id: object) -> dict:
    """The before-state a `materialize` transition requires: a NEW chapter identity.

    Materialization only ever seals a chapter this transaction creates（契约 §4 行 1）:
    the chapterId must be absent from `current` and first appear in the snapshot the
    transaction is building. An *existing* chapter with a missing `proseBinding` is
    corruption — BINDING_INVALID, locator reserved — and its only future path is
    Binding Repair, which Phase 1 deliberately does not implement. It is therefore
    never materialized, with or without prose on disk（`missing managed file != unbound`）.
    """
    if not isinstance(chapter_id, str) or not chapter_id:
        _reject("OWNERSHIP_BINDING_INVALID", "materialize requires a chapterId", chapterId=chapter_id)
    before = _chapter_by_id(current, chapter_id)
    if before is not None and isinstance(before.get("proseBinding"), dict):
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "materialize target already carries a binding; materialization is only for a "
                "chapter identity this transaction creates", chapterId=chapter_id)
    if before is not None:
        _reject("OWNERSHIP_BINDING_INVALID",
                "materialize target already exists in the current structure; an existing chapter "
                "without a proseBinding is corrupt (Binding Repair domain), never re-materialized",
                chapterId=chapter_id)
    chapter = _chapter_by_id(snapshot, chapter_id)
    if chapter is None:
        _reject("OWNERSHIP_BINDING_INVALID", "materialize target chapter does not exist",
                chapterId=chapter_id)
    if isinstance(chapter.get("proseBinding"), dict):
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "materialize requires a chapter without an existing binding", chapterId=chapter_id)
    return chapter


def require_next_revision(current: dict, proposed: dict) -> int:
    """Every Phase 1 formal binding transition commits at exactly the next revision.

    The authoritative current revision decides it — a caller never names it. A stale
    proposal（`== current`，§48）、a skipped one（`current + 2`）or a forged one（`999`，
    §49）is rejected outright, never silently normalized to `current + 1`（§57/§58），
    because a wrong revision is either a stale/forged proposal or a broken caller.
    Returns the validated next revision so callers seal `binding.generation` from the
    same number that is persisted.
    """
    current_revision = current.get("revision") if isinstance(current, dict) else None
    proposed_revision = proposed.get("revision") if isinstance(proposed, dict) else None
    if (type(current_revision) is not int or type(proposed_revision) is not int
            or proposed_revision != current_revision + 1):
        _reject("REVISION_CONFLICT",
                "formal binding transitions commit at exactly the current revision + 1",
                currentRevision=current_revision, proposedRevision=proposed_revision)
    return proposed_revision


def apply_binding_transitions(current: dict, transitions: list[dict], *,
                              proposed: dict | None = None) -> tuple[dict, _BindingAuthority]:
    """Apply formal binding transitions to a snapshot and return (proposed, authority).

    Phase 1 implements exactly one kind — `materialize`, and only for a chapter
    identity that is NEW in this transaction. `proposed` is the snapshot the caller is
    building (the transaction's structure); when omitted the applier works on the
    current snapshot alone, where no new identity can exist, so every materialize
    fails closed. Either way the committed revision is `current.revision + 1`, the
    materialized `binding.generation` is sealed from that same number, and a proposal
    naming any other revision is rejected with REVISION_CONFLICT（实施轮 §19–§22）.
    finalization/refactor/undo runtimes belong to later phases and never silently fall
    back to legacy lineage.

    Fault isolation（契约 §2.2 Level 2 / Phase 0.2）：only project-fatal ownership
    corruption blocks a transition. A chapter-local-damaged chapter elsewhere neither
    blocks nor is repaired — it keeps its locator and its exact payload, because the
    transition only ever deep-copies and writes the chapters it authorizes.
    """
    if not is_ownership_aware_structure(current):
        _reject("OWNERSHIP_MODEL_UNSUPPORTED_OPERATION",
                "binding transitions require an ownership-aware structure; schema migration is a Phase 4 cutover")
    require_global_ownership_safety(current)
    if proposed is None:
        snapshot = deepcopy(current)
        snapshot["revision"] = current["revision"] + 1
    else:
        snapshot = deepcopy(proposed)
    revision = require_next_revision(current, snapshot)
    before_rows = {row.get("chapterId"): row for row in current.get("chapters", [])
                   if isinstance(row, dict)}
    authority_transitions: dict[str, dict] = {}
    for transition in transitions:
        if not isinstance(transition, dict):
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION", "transition must be an object")
        kind = transition.get("kind")
        chapter_id = transition.get("chapterId")
        if kind not in TRANSITION_KINDS:
            _reject("OWNERSHIP_MODEL_UNSUPPORTED_OPERATION",
                    "transition kind is not implemented", kind=kind)
        if kind == "materialize":
            chapter = _materialization_target(current, snapshot, chapter_id)
            # §20：binding.generation == proposed.revision == current.revision + 1 由同一个
            # 已验证的 revision 数字封签，三者是同一个数，不是两次独立比较。
            chapter["proseBinding"] = materialization_binding(snapshot, chapter, generation=revision)
            authority_transitions[chapter_id] = {"kind": "materialize"}
            continue
        # Phase 2 current-binding kinds apply to a chapter that already exists in `current`:
        # their before-state is the persisted binding, which a caller cannot talk it out of.
        before = _chapter_by_id(current, chapter_id)
        if before is None:
            _reject("OWNERSHIP_BINDING_INVALID",
                    "a current-binding transition applies to an existing chapter, never a new identity",
                    chapterId=chapter_id, kind=kind)
        result = binding_transition_result(snapshot, before, transition, generation=revision)
        chapter = _chapter_by_id(snapshot, chapter_id)
        if chapter is None:
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                    "transition target chapter is missing from the proposal", chapterId=chapter_id)
        chapter["filePath"] = result["filePath"]
        chapter["proseBinding"] = result["proseBinding"]
        authority_transitions[chapter_id] = {key: value for key, value in transition.items()
                                             if key != "chapterId"}
    # §45：未授权的章一个字节都不许动（尤其不得顺手 reseal / normalize / repair digest）。
    for chapter_id, row in before_rows.items():
        if chapter_id in authority_transitions:
            continue
        if _chapter_by_id(snapshot, chapter_id) != row:
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                    "transition modified an unrelated chapter payload", chapterId=chapter_id)
    require_global_ownership_safety(snapshot)
    require_unique_locators(snapshot)
    return snapshot, _BindingAuthority(authority_transitions)


def commit_binding_transitions(root: Path, transitions: list[dict], *, actor: str,
                               expected_revision: int | None = None,
                               proposed: dict | None = None) -> dict:
    """The formal Phase-1 transition committer: apply → seal → boundary-checked persist.

    Pass `proposed` when the transaction is also creating chapter identities; without
    it the committer sees only the current snapshot, so materialize targets cannot be
    new and are refused (new-chapter materialization normally rides the shared
    transaction channel, `structure_commands._new_chapter`).

    Revision monotonicity never depends on `expected_revision`（实施轮 §21）: the
    authoritative current revision is the one loaded under the lock, the snapshot must
    be exactly `current + 1`, and `expected_revision` — when given — is only the extra
    optimistic-lock assertion `save_structure` re-checks.
    """
    from .locks import project_lock
    from .structure_store import load_structure, save_structure
    root = root.resolve()
    with project_lock(root):
        current = load_structure(root)
        snapshot, authority = apply_binding_transitions(current, transitions, proposed=proposed)
        return save_structure(root, snapshot, expected_revision=expected_revision,
                              _binding_authority=authority)


# ---------------------------------------------------------------------------
# Phase 2 — current-state observation（运行时观测；绝不持久化，见 §11）
# ---------------------------------------------------------------------------

UNBOUND = "UNBOUND"
UNKNOWN_TARGET = "UNKNOWN_TARGET"
OWNED_CLEAN = "OWNED_CLEAN"
OWNED_MODIFIED_EXTERNALLY = "OWNED_MODIFIED_EXTERNALLY"
LOCATOR_DRIFT = "LOCATOR_DRIFT"
NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
BINDING_INVALID = "BINDING_INVALID"
CONFLICT = "CONFLICT"
TRANSACTION_PENDING = "TRANSACTION_PENDING"

OBSERVED_STATES = frozenset({UNBOUND, UNKNOWN_TARGET, OWNED_CLEAN,
                             OWNED_MODIFIED_EXTERNALLY, LOCATOR_DRIFT,
                             NEEDS_CONFIRMATION, BINDING_INVALID, CONFLICT,
                             TRANSACTION_PENDING})

# observed state -> (allowedActions, nextSafeAction, requiresExplicitApproval)。
# UNKNOWN_TARGET 的自动动作里**没有** adopt（契约 §16）：低能力 AI 只能报告冲突并请作者裁决。
_OBSERVED_ACTIONS: dict[str, tuple[list[str], str, bool]] = {
    UNBOUND: (["finalize_first", "move_unbound"], "finalize_first", False),
    OWNED_CLEAN: (["refinalize", "move_managed"], "refinalize", False),
    OWNED_MODIFIED_EXTERNALLY: (["accept_external", "report_conflict"], "accept_external", True),
    UNKNOWN_TARGET: (["report_conflict", "ask_author_decision"], "ask_author_decision", True),
    LOCATOR_DRIFT: (["report_conflict", "ask_author_decision"], "ask_author_decision", True),
    NEEDS_CONFIRMATION: (["ask_author_decision"], "ask_author_decision", True),
    BINDING_INVALID: ([], "ask_author_decision", True),
    CONFLICT: ([], "ask_author_decision", True),
    TRANSACTION_PENDING: (["workflow_recover"], "workflow_recover", False),
}

# observed state -> 该状态下普通 finalization/refactor 的确定性拒绝码（契约 §15.2/§77）。
_OBSERVED_ERROR_CODE = {
    UNKNOWN_TARGET: "PROSE_TARGET_NOT_OWNED",
    OWNED_MODIFIED_EXTERNALLY: "PROSE_EXTERNAL_MODIFICATION",
    LOCATOR_DRIFT: "PROSE_LOCATOR_DRIFT",
    NEEDS_CONFIRMATION: "OWNERSHIP_CONFIRMATION_REQUIRED",
    BINDING_INVALID: "OWNERSHIP_BINDING_INVALID",
    CONFLICT: "PROSE_LOCATOR_CONFLICT",
    TRANSACTION_PENDING: "TRANSACTION_INCOMPLETE",
}


def pending_transaction_present(root: Path) -> bool:
    """True when a durable transaction manifest is still on disk（契约 §8.1 / §15.1）。

    This is *current* crash-recovery state, not history: the manifest the engine writes
    before its first business byte is exactly what makes an interrupted write set
    unreadable as authority. Nothing here reads operation journals or backups.
    """
    from .finalization import pending_transactions
    return bool(pending_transactions(root))


def prose_slot_occupied(root: Path, relative: object) -> bool:
    """Is there a real directory entry at this locator? Decided from entries, not exists().

    A broken symlink and a second hard-linked name are both real entries that
    `Path.exists()` (false for a broken link) would miss, and both make the slot
    occupied（契约 §6.3 / §13）。An uninterpretable locator is treated as occupied so a
    caller never reads "no file" out of a path it cannot even resolve.
    """
    from .refactor_paths import canonical_key, resolve, sibling_keys
    if not _locator_interpretable(relative):
        return True
    try:
        path = resolve(root, str(relative))
    except StructureError:
        return True
    return canonical_key(path.name) in sibling_keys(path.parent)


def _observe_file_state(root: Path, relative: object) -> dict | None:
    """Single read of the current bytes at a locator: the state the observation trusts.

    Returns None when the locator resolves to something that is not a regular file
    (a directory, or an unresolvable path). The bytes read here are the same bytes a
    later write-set before-image must match; there is deliberately no second read.
    """
    from .refactor_paths import resolve
    from .write_sets import file_state
    if not _locator_interpretable(relative):
        return None
    try:
        path = resolve(root, str(relative))
    except StructureError:
        return None
    if path.exists() and not path.is_file():
        return None
    return file_state(path)


def _alias_conflict_for(root: Path, structure: dict, chapter: dict) -> dict | None:
    """Object-alias conflict involving THIS chapter, or None.

    Two distinct locators resolving to one inode cannot both be owned by different chapters
    （契约 §6.3）。A regular file with link count 1 has exactly one directory entry, so it
    cannot be aliased: that single stat rules out the whole scan for every ordinary file, and
    only a genuinely multi-linked file costs one stat per sibling. Deliberately not a
    whole-structure O(n²) sweep — observation runs once per operation and only this chapter's
    alias can change this chapter's verdict.
    """
    from .refactor_paths import resolve, same_file
    target = chapter.get("filePath")
    if not _locator_interpretable(target):
        return None
    try:
        path = resolve(root, str(target))
    except StructureError:
        return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_nlink <= 1:
            return None
    except OSError:
        return None
    chapter_id = chapter.get("chapterId")
    for other in structure.get("chapters", []):
        if not isinstance(other, dict) or other.get("chapterId") == chapter_id:
            continue
        relative = other.get("filePath")
        if not _locator_interpretable(relative):
            continue
        try:
            other_path = resolve(root, str(relative))
        except StructureError:
            continue
        if other_path.is_file() and same_file(path, other_path):
            return {"code": "PROSE_LOCATOR_CONFLICT",
                    "message": "two locators alias the same physical file object",
                    "chapters": sorted({str(chapter_id), str(other.get("chapterId"))}),
                    "locators": [str(target), str(relative)]}
    return None


def observe_chapter(root: Path, structure: dict, chapter: dict, *,
                    pending: bool | None = None) -> dict:
    """Pure current-state observation of ONE chapter's prose ownership（契约 §15.1）。

    Inputs are the current structure, the current chapter row and the current disk; the
    result is a machine-readable receipt so a low-capability caller never assembles
    ownership logic itself（契约 §16/§21）。Nothing here reads operation journals,
    continuity records, undo evidence, backup chains or move history, and the observed
    state is never persisted.

    Order matters: a pending transaction `outranks` everything (the disk and the
    structure are not authoritative while a write set may still be replayed/rolled
    back), then a locator/object conflict, then chapter-local binding corruption, and
    only then the state-specific disk comparison.
    """
    chapter_id = chapter.get("chapterId")
    file_path = chapter.get("filePath")
    binding = chapter.get("proseBinding")
    binding = binding if isinstance(binding, dict) else None
    receipt = {
        "chapterId": chapter_id,
        "state": None,
        "persistedState": binding.get("state") if binding else None,
        "currentLocator": file_path,
        "bindingGeneration": binding.get("generation") if binding else None,
        "expectedContentDigest": binding.get("expectedContentDigest") if binding else None,
        "observedContentDigest": None,
        "blockers": [],
        "allowedActions": [],
        "nextSafeAction": None,
        "requiresExplicitApproval": False,
    }
    if pending is None:
        pending = pending_transaction_present(root)
    if pending:
        return _finish_observation(receipt, TRANSACTION_PENDING, blocking=[
            {"code": "TRANSACTION_INCOMPLETE",
             "message": "a durable transaction manifest is still on disk; run workflow recover" +
                        " before any destructive prose operation"}])
    collision = _locator_collision_for(structure, chapter)
    alias = None if collision else _alias_conflict_for(root, structure, chapter)
    if collision or alias:
        return _finish_observation(receipt, CONFLICT, blocking=[collision or alias])
    errors = validate_binding(structure, chapter)
    if errors:
        return _finish_observation(receipt, BINDING_INVALID, blocking=errors)
    disk = _observe_file_state(root, file_path)
    if disk is None:
        return _finish_observation(receipt, CONFLICT, blocking=[
            {"code": "PROSE_LOCATOR_UNSAFE",
             "message": "the declared locator does not resolve to a readable regular file slot"}])
    receipt["observedContentDigest"] = f"sha256:{disk['sha256']}" if disk["exists"] else None
    state = binding["state"]
    if state == "unresolved":
        # 待决索赔，绝不是正向归属：盘上存在与否、哈希吻合与否都不改变这一点（契约 §17）。
        return _finish_observation(receipt, NEEDS_CONFIRMATION)
    if state == "unbound":
        # 目标槽被占用 == UNKNOWN_TARGET，即使盘上字节与某个候选完全相同也不接管（契约 §13）。
        # 占用按**真实目录项**判定：断链 symlink 与折叠后同槽的兄弟都不是「空槽」。
        occupied = prose_slot_occupied(root, file_path)
        return _finish_observation(receipt, UNKNOWN_TARGET if occupied else UNBOUND)
    if not disk["exists"]:
        # 受管定位符在盘上缺失：绝不搜索、绝不按哈希猜测、绝不 auto-rebind（契约 §16）。
        return _finish_observation(receipt, LOCATOR_DRIFT)
    if f"sha256:{disk['sha256']}" == binding["expectedContentDigest"]:
        # 相同的 inode 与否无关：Current Binding 不依赖 inode identity（契约 §52）。
        return _finish_observation(receipt, OWNED_CLEAN)
    return _finish_observation(receipt, OWNED_MODIFIED_EXTERNALLY)


def _locator_collision_for(structure: dict, chapter: dict) -> dict | None:
    chapter_id = chapter.get("chapterId")
    file_path = chapter.get("filePath")
    if not _locator_interpretable(file_path):
        return None
    key = canonical_locator_key(str(file_path))
    for other in structure.get("chapters", []):
        if not isinstance(other, dict) or other.get("chapterId") == chapter_id:
            continue
        other_path = other.get("filePath")
        if _locator_interpretable(other_path) and canonical_locator_key(str(other_path)) == key:
            return {"code": "PROSE_LOCATOR_CONFLICT",
                    "message": "two chapters reserve the same canonical prose locator",
                    "canonicalKey": key,
                    "chapters": sorted({str(chapter_id), str(other.get("chapterId"))})}
    return None


def _finish_observation(receipt: dict, state: str, *, blocking: list[dict] | None = None) -> dict:
    allowed, next_action, approval = _OBSERVED_ACTIONS[state]
    receipt["state"] = state
    receipt["blockers"] = list(blocking or [])
    receipt["allowedActions"] = list(allowed)
    receipt["nextSafeAction"] = next_action
    receipt["requiresExplicitApproval"] = approval
    return receipt


def observation_error_code(observed_state: str) -> str:
    """The deterministic refusal code for a destructive operation in this observed state."""
    return _OBSERVED_ERROR_CODE.get(observed_state, "OWNERSHIP_MODEL_UNSUPPORTED_OPERATION")


def require_observation(root: Path, structure: dict, chapter: dict, allowed: tuple[str, ...], *,
                        pending: bool | None = None, action: str) -> dict:
    """Observe once under the caller's lock and refuse unless the state is `allowed`.

    Every destructive Phase-2 operation runs this at BOTH ends: the preview proves the
    state it plans against, and the commit re-observes the current structure + current
    disk under the lock. Previewing a plan is not proof the bytes are still there when
    it commits（契约 §10.1）。
    """
    observation = observe_chapter(root, structure, chapter, pending=pending)
    if observation["state"] in allowed:
        return observation
    _reject(observation_error_code(observation["state"]),
            f"{action} is not allowed while the chapter is {observation['state']}",
            **{"chapterId": observation["chapterId"], "observedState": observation["state"],
               "currentLocator": observation["currentLocator"],
               "expectedContentDigest": observation["expectedContentDigest"],
               "observedContentDigest": observation["observedContentDigest"],
               "blockers": observation["blockers"]})


# Every plan-declared transition kind and the observed state it must still be in at commit.
_PLAN_TRANSITION_STATE = {
    "first_finalize": (UNBOUND,),
    "refinalize": (OWNED_CLEAN,),
    "accept_external": (OWNED_MODIFIED_EXTERNALLY,),
    "move_managed": (OWNED_CLEAN,),
    "move_unbound": (UNBOUND,),
}


def require_plan_transitions_current(root: Path, current: dict, plan: dict) -> None:
    """Commit-lock re-verification of every binding transition a plan declares（契约 §63）。

    A preview proved a state; this proves the SAME state is still current, under the lock,
    against the freshly loaded structure and the current disk: observed state, binding
    generation token, expected content digest, destination occupancy and locator
    reservation are all re-derived. "The plan still hashes" is not enough — approving an
    old plan never proves the disk still matches it（契约 §10.1）。
    """
    for transition in plan.get("bindingTransitions") or []:
        chapter_id = transition.get("chapterId")
        chapter = _chapter_by_id(current, chapter_id)
        if chapter is None:
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                    "binding transition target is missing from the current structure",
                    chapterId=chapter_id)
        kind = transition.get("kind")
        allowed = _PLAN_TRANSITION_STATE.get(kind)
        if allowed is None:
            _reject("OWNERSHIP_MODEL_UNSUPPORTED_OPERATION",
                    "plan declares an unknown binding transition kind",
                    kind=kind, chapterId=chapter_id)
        if not request_generation_current(chapter, transition.get("bindingGeneration")):
            _reject("OWNERSHIP_GENERATION_CONFLICT",
                    "the plan's binding generation is stale for this chapter",
                    chapterId=chapter_id,
                    requestBindingGeneration=transition.get("bindingGeneration"),
                    currentBindingGeneration=chapter["proseBinding"].get("generation"))
        observation = require_observation(root, current, chapter, allowed, action="binding transition")
        if transition.get("expectedContentDigest") != observation["expectedContentDigest"]:
            _reject("PROSE_CONTENT_CHANGED",
                    "the expected content digest changed after the plan was previewed",
                    chapterId=chapter_id,
                    plannedExpectedContentDigest=transition.get("expectedContentDigest"),
                    currentExpectedContentDigest=observation["expectedContentDigest"])
        # accept_external 绑定的不只是「仍然受管」：它绑定的是**当时看到的 H2**。预览之后盘上
        # 再被改动一次，接受的就成了另一批字节 —— 必须拒绝，而不是顺势接受最新的一批。
        if (transition.get("observedContentDigest") is not None
                and transition["observedContentDigest"] != observation["observedContentDigest"]):
            _reject("PROSE_CONTENT_CHANGED",
                    "the observed content digest changed after the plan was previewed",
                    chapterId=chapter_id,
                    plannedObservedContentDigest=transition["observedContentDigest"],
                    currentObservedContentDigest=observation["observedContentDigest"])
        if kind in ("move_managed", "move_unbound"):
            apply_locator_move_plan(root, current, chapter,
                                    new_path=transition.get("newFilePath"), kind=kind)


# ---------------------------------------------------------------------------
# Privileged approval boundary（契约 §10.1，Phase 2 返修）
# ---------------------------------------------------------------------------

def require_privileged_approval(plan: dict, *, operation_id: str | None = None,
                               approval=None, root: Path | None = None) -> dict | None:
    """Refuse a high-risk ownership commit that carries no verified privileged authority.

    `approved=True`、CLI `--approve`、`actor` 字符串、`planHash` 回传与 `_BindingAuthority`
    能证明的只是「提交的就是被预览的 exact plan」或「这条流转在 ownership 模型上合法」；
    它们**不能**证明「独立特权通道批准了这个动作」（契约 §10.1/§39）。外改采用入口的
    本机确认能力在提交锁内一次消费，仍须通过 current-state 校验；两者缺一不可。

    判定同时看 operation 名与声明的 transition kind：改掉 operation 字符串、只留
    `accept_external` 流转的伪造计划同样 fail closed（计划哈希不是 authority，operation
    字符串也不是）。
    """
    kinds = {transition.get("kind") for transition in plan.get("bindingTransitions") or []
             if isinstance(transition, dict) and transition.get("kind")}
    if plan.get("operation") not in PRIVILEGED_APPROVAL_OPERATIONS and not (
            kinds & PRIVILEGED_APPROVAL_TRANSITIONS):
        return
    if approval is not None:
        from .external_approval import consume_local_approval
        return consume_local_approval(plan, approval, root=root)
    _reject("APPROVAL_CAPABILITY_REQUIRED",
            "接受外部修改（accept external modification）是高风险所有权流转，需要独立特权批准"
            "通道签发的 approval capability；--approve、approved=True、actor 字符串与 planHash "
            "都是普通调用参数，不构成 authority。请通过外改采用入口在本机完成确认。",
            operation=plan.get("operation"), operationId=operation_id,
            transitionKinds=sorted(kinds), requiredCapability="verified_privileged_approval")


# ---------------------------------------------------------------------------
# Phase 2 — the one central transition sealer（契约 §65/§66）
# ---------------------------------------------------------------------------

def binding_authority(transitions: list[dict]) -> _BindingAuthority:
    """Build the formal-transition capability a plan carries（契约 §5.3 / §20.4）。

    `_BindingAuthority` is module-internal on purpose: a generic writer cannot obtain or
    forge one, which is what makes the protected-field boundary meaningful.
    """
    records: dict[str, dict] = {}
    for transition in transitions:
        if not isinstance(transition, dict):
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION", "binding transition must be an object")
        chapter_id = transition.get("chapterId")
        if not isinstance(chapter_id, str) or not chapter_id:
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION", "binding transition requires a chapterId")
        if chapter_id in records:
            _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                    "two binding transitions for one chapter in a single transaction",
                    chapterId=chapter_id)
        records[chapter_id] = {key: value for key, value in transition.items() if key != "chapterId"}
    return _BindingAuthority(records)


def _require_content_digest(value: object, *, field: str, chapter_id: object) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        _reject("OWNERSHIP_BINDING_INVALID",
                f"{field} must be sha256:<64 lowercase hex>", chapterId=chapter_id, field=field)
    return value


# A `formal_undo` declaration names the state the reverse was derived from (`current*`) and
# the semantic fields to restore (`restore*`). The boundary re-derives the whole declaration
# against the freshly loaded structure, so a stale or forged one is a refusal, never a silent
# re-targeting; `generation` / `integrityDigest` are deliberately NOT restorable — the new
# transaction seals them from its own committed revision（绝不回放历史代次/封签）。


def _formal_undo_result(structure: dict, chapter: dict, transition: dict, *, generation: int) -> dict:
    """The canonical reverse of ONE committed operation, sealed at the undo revision.

    This is the one place a formal undo computes protected fields, and it is the same
    central sealer every other transition uses: given the persisted current binding (the
    before-state of the reverse) and the declared semantics, it re-derives the sealed
    result. The producer (`structure_transaction`) and the writer boundary both come
    through here, so neither trusts the other's assembly（契约 §64/§66）。
    """
    chapter_id = chapter.get("chapterId")
    binding = chapter.get("proseBinding")
    if not isinstance(binding, dict):
        _reject("OWNERSHIP_BINDING_INVALID", "formal undo target has no binding", chapterId=chapter_id)
    operation_id = transition.get("reversedOperationId")
    if not isinstance(operation_id, str) or not operation_id:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "formal undo must name the operation it reverses", chapterId=chapter_id)
    journal_digest = transition.get("reversedJournalDigest")
    if not isinstance(journal_digest, str) or not journal_digest:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "formal undo must bind the reversed operation's committed journal",
                chapterId=chapter_id)
    # 声明的 current 语义必须能从**现在**的持久化 binding 重推出来；对不上就是过期/伪造的
    # 逆向声明，fail closed——绝不用它去覆盖一个已经变了的当前状态（契约 §12/§63）。
    if binding.get("generation") != transition.get("currentBindingGeneration"):
        _reject("OWNERSHIP_GENERATION_CONFLICT",
                "formal undo declaration does not match this chapter's current binding generation",
                chapterId=chapter_id, declaredGeneration=transition.get("currentBindingGeneration"),
                currentBindingGeneration=binding.get("generation"))
    if chapter.get("filePath") != transition.get("currentFilePath"):
        _reject("UNDO_REVERSE_STATE_MISMATCH",
                "formal undo declaration's current locator is not this chapter's locator",
                chapterId=chapter_id, declaredLocator=transition.get("currentFilePath"),
                currentLocator=chapter.get("filePath"))
    if binding.get("expectedContentDigest") != transition.get("currentExpectedContentDigest"):
        _reject("UNDO_REVERSE_STATE_MISMATCH",
                "formal undo declaration's current expected digest is not this binding's",
                chapterId=chapter_id,
                declaredExpectedContentDigest=transition.get("currentExpectedContentDigest"),
                currentExpectedContentDigest=binding.get("expectedContentDigest"))
    restore_path = transition.get("restoreFilePath")
    restore_state = transition.get("restoreState")
    restore_digest = transition.get("restoreExpectedContentDigest")
    if not _locator_interpretable(restore_path):
        _reject("PROSE_LOCATOR_UNSAFE",
                "the restored locator is not a safe canonical project-relative path",
                chapterId=chapter_id, filePath=restore_path)
    if restore_state not in _FORMAL_UNDO_RESTORE_STATES:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "formal undo restores the semantics of a real forward transition; "
                "it never manufactures another binding state",
                chapterId=chapter_id, restoreState=restore_state)
    if restore_state == "unbound" and restore_digest is not None:
        _reject("OWNERSHIP_BINDING_INVALID",
                "an unbound restore target must not declare an expected content digest",
                chapterId=chapter_id)
    if restore_state == "managed":
        _require_content_digest(restore_digest, field="restoreExpectedContentDigest",
                                chapter_id=chapter_id)
    current_key = (chapter.get("filePath"), binding.get("state"), binding.get("expectedContentDigest"))
    if (restore_path, restore_state, restore_digest) == current_key:
        _reject("OWNERSHIP_UNAUTHORIZED_MUTATION",
                "formal undo must restore different semantics than the chapter already has",
                chapterId=chapter_id)
    return {"filePath": restore_path,
            "proseBinding": seal_binding(structure.get("projectId"), chapter_id, restore_path,
                                         state=restore_state, generation=generation,
                                         expected_content_digest=restore_digest)}


def binding_transition_result(structure: dict, chapter: dict, transition: dict, *,
                              generation: int) -> dict:
    """The canonical protected-field result of ONE declared transition（唯一封签入口）。

    Input: the current chapter (its persisted binding is the before-state) plus a declared
    transition. Output: the `filePath` and sealed `proseBinding` that transition MUST
    produce at `generation`. Pure and deterministic — no disk, no history — so the
    producer (`apply_binding_transitions`) and the writer boundary
    (`enforce_writer_boundary`) derive the same answer independently instead of trusting
    each other, and no two modules hand-roll a digest（契约 §66）。

    Disk-dependent preconditions (slot empty / digest matches / locator present) are NOT
    decided here: they belong to the observation that preview and commit each run.
    """
    kind = transition.get("kind")
    chapter_id = chapter.get("chapterId")
    if kind not in TRANSITION_KINDS or kind == "materialize":
        _reject("OWNERSHIP_MODEL_UNSUPPORTED_OPERATION",
                "not a current-binding transition on an existing chapter", kind=kind,
                chapterId=chapter_id)
    binding = chapter.get("proseBinding")
    file_path = chapter.get("filePath")
    if not isinstance(binding, dict):
        _reject("OWNERSHIP_BINDING_INVALID", "binding transition target has no binding",
                chapterId=chapter_id)
    if kind == "formal_undo":
        return _formal_undo_result(structure, chapter, transition, generation=generation)
    required = _TRANSITION_BEFORE_STATE[kind]
    if binding.get("state") != required:
        _reject("OWNERSHIP_BINDING_INVALID",
                f"{kind} requires the persisted binding state to be {required}",
                chapterId=chapter_id, kind=kind, state=binding.get("state"))
    state = binding.get("state")
    digest_value = binding.get("expectedContentDigest")
    if kind in ("first_finalize", "refinalize"):
        # 首次/再次定稿：新正文字节由写入器在同一写集内落盘，期望哈希就是它的 sha256。
        state, digest_value = "managed", _require_content_digest(
            transition.get("contentDigest"), field="contentDigest", chapter_id=chapter_id)
    elif kind == "accept_external":
        # 接受外部修改不重写正文字节：期望哈希是**当前盘上**已被显式接受的 H2（契约 §37）。
        state, digest_value = "managed", _require_content_digest(
            transition.get("observedContentDigest"), field="observedContentDigest",
            chapter_id=chapter_id)
    else:
        new_path = transition.get("newFilePath")
        if not _locator_interpretable(new_path):
            _reject("PROSE_LOCATOR_UNSAFE", "new locator is not a safe canonical project-relative path",
                    chapterId=chapter_id)
        if new_path == file_path:
            _reject("OWNERSHIP_BINDING_INVALID", "locator move target equals the current locator",
                    chapterId=chapter_id)
        # 纯定位符移动：正文字节没变，所以 expectedContentDigest 不变（契约 §44/§47）。
        file_path = new_path
    return {"filePath": file_path,
            "proseBinding": seal_binding(structure.get("projectId"), chapter_id, file_path,
                                         state=state, generation=generation,
                                         expected_content_digest=digest_value)}


def apply_locator_move_plan(root: Path, structure: dict, chapter: dict, *, new_path: str,
                            kind: str, pending: bool | None = None) -> dict:
    """Preview-side validation of a managed/unbound locator move（契约 §41/§42/§45/§46）。

    Runs the observation the move kind requires, then refuses an occupied destination
    slot from real directory entries. Returns the observation so the caller can bind the
    generation/digest token into the plan it asks the author to approve.
    """
    required_state = {"move_managed": OWNED_CLEAN, "move_unbound": UNBOUND}[kind]
    if not _locator_interpretable(new_path):
        _reject("PROSE_LOCATOR_UNSAFE",
                "the new locator is not a safe canonical project-relative path",
                chapterId=chapter.get("chapterId"), targetPath=new_path)
    observation = require_observation(root, structure, chapter, (required_state,),
                                      pending=pending, action="locator move")
    reservations, fatal = locator_reservations(structure)
    if fatal:
        raise StructureError("STRUCTURE_INVALID", "prose locator invariant violated", {"errors": fatal})
    key = canonical_locator_key(new_path)
    holder = reservations.get(key)
    if holder is not None and holder != chapter.get("chapterId"):
        _reject("PROSE_LOCATOR_CONFLICT",
                "the destination locator is reserved by another chapter", chapterId=holder,
                targetPath=new_path)
    if prose_slot_occupied(root, new_path):
        _reject("PROSE_TARGET_NOT_OWNED",
                "the destination locator already holds a file; existing bytes are never taken over",
                chapterId=chapter.get("chapterId"), targetPath=new_path)
    return observation


def require_no_alias_conflict(root: Path, structure: dict, chapter: dict) -> None:
    """Commit-time object-alias check for the chapter whose locator is changing（契约 §53）。

    Two distinct locators resolving to one inode is an occupancy conflict, not identity:
    the write set is about to give this chapter a locator another chapter's file already
    occupies. Fail closed rather than grow a second object-identity model.
    """
    conflict = _alias_conflict_for(root, structure, chapter)
    if conflict:
        raise StructureError(conflict["code"], conflict["message"], conflict)


# ---------------------------------------------------------------------------
# Future-transition semantic locks（实施轮 §35/§36/§41 — helpers, not runtime）
# ---------------------------------------------------------------------------

def validate_unbound_locator_move(old_chapter: dict, *, old_prose_exists: bool,
                                  new_prose_exists: bool) -> None:
    """Locks the `unbound` locator-move precondition before Phase 2 implements it.

    Moving an unbound reservation may only ever reposition an EMPTY slot: no prose
    file at the old locator, none at the new one. It can never take over an
    existing file（那是 Phase 2+ 的显式 adopt 领域）。
    """
    binding = old_chapter.get("proseBinding") if isinstance(old_chapter, dict) else None
    if not isinstance(binding, dict) or binding.get("state") != "unbound":
        _reject("OWNERSHIP_BINDING_INVALID", "unbound locator move requires an unbound binding")
    if old_prose_exists:
        _reject("PROSE_TARGET_NOT_OWNED", "unbound move requires the old locator to hold no prose file")
    if new_prose_exists:
        _reject("PROSE_TARGET_NOT_OWNED", "unbound move cannot take over an existing file")


def first_finalization_write_set_plan(root: Path, structure: dict, chapter: dict, *,
                                      prose_relative: str, prose_text: str) -> list[dict]:
    """The Phase-2 first-finalization write-set SHAPE, computed as one atomic plan.

    Freezes the contract that a first finalization is a single write-set holding
    BOTH the prose creation（空 before-image）AND the binding transition
    unbound→managed（digest + generation 前进）, all derived from the pre-state in
    one call. There is deliberately no second step that could observe an existing
    file and "adopt" it（实施轮 §41）。
    """
    from .write_sets import digest, file_state, json_bytes, safe_project_path
    binding = chapter.get("proseBinding")
    if not isinstance(binding, dict) or binding.get("state") != "unbound":
        _reject("OWNERSHIP_BINDING_INVALID", "first finalization starts from an unbound binding",
                chapterId=chapter.get("chapterId"))
    target = safe_project_path(root, prose_relative)
    prose_state = file_state(target)
    if prose_state["exists"]:
        _reject("PROSE_TARGET_NOT_OWNED",
                "first finalization requires an empty target slot; existing prose is never adopted",
                chapterId=chapter.get("chapterId"), filePath=prose_relative)
    prose_bytes = prose_text.encode("utf-8")
    content_digest = f"sha256:{digest(prose_bytes)}"
    revision = structure["revision"] + 1
    sealed = seal_binding(structure["projectId"], chapter["chapterId"], chapter["filePath"],
                          state="managed", generation=revision, expected_content_digest=content_digest)
    proposed = deepcopy(structure)
    proposed["revision"] = revision
    _chapter_by_id(proposed, chapter["chapterId"])["proseBinding"] = sealed
    structure_state = file_state(safe_project_path(root, ".govel/structure.json"))
    return [
        {"path": prose_relative, "action": "create", "kind": "prose",
         "before": prose_state,
         "after": {"exists": True, "sha256": digest(prose_bytes), "mode": None}},
        {"path": ".govel/structure.json", "action": "update", "kind": "binding-transition",
         "before": structure_state,
         "after": {"exists": True, "sha256": digest(json_bytes(proposed)), "mode": None},
         "bindingChange": {"chapterId": chapter["chapterId"],
                           "locator": chapter["filePath"],
                           "from": binding, "to": sealed}},
    ]
