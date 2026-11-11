"""Registration-time binding fills computed from current project truth.

目的：把「AI 抄写绑定 hash」换成「工具计算绑定 hash」。三种 hash 在这里保持区分，
不得混同：

- 绑定源内容 hash：按字段语义选择算法（正文/推演是文件字节 hash，状态缓存是
  canonical JSON hash），只进入 payload 的绑定字段；
- 工件信封 hash（artifactSha256）：注入后对整个信封重算；
- 批准 hash：仍由 workflow.approve_artifacts 对照注册条目与文件新鲜度校验。

绑定只复刻 finalization_preflight 已有的核对语义（那里仍是权威闸门），并把会在
入库时爆掉的绑定漂移提前到注册时失败。本模块不产生语义结论、不授予批准。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import file_sha256, sha256_json, sha256_text
from .jsonio import read_json
from .paths import ProjectPathError, safe_project_path

# 每类工件允许 --bind 的 payload 字段。未列出的字段拒绝绑定：绑定语义必须与
# finalization 的核对一一对应，不允许"顺便填别的"。
BINDABLE_FIELDS: dict[str, set[str]] = {
    'candidate_prose': {'sourcePlanHash'},
    'diagnostics': {'draftSha256'},
    'stage_a_review': {'sourcePlanHash'},
    'stage_b_review': {'finalProseSha256', 'sourcePlanHash', 'proseBindingGeneration'},
    'state_candidate': {'sourcePlanHash', 'finalProseSha256',
                        'baseStateSha256', 'designSha256'},
}
PROSE_FIELDS = {'draftSha256', 'finalProseSha256'}
STATE_BUILDERS = {'baseStateSha256': 'state', 'designSha256': 'design'}


class ArtifactBindError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _resolve_source_path(root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        return safe_project_path(root, path, allow_absolute=True)
    except ProjectPathError as exc:
        raise ArtifactBindError(exc.code, str(exc), getattr(exc, 'details', {})) from exc


def _registered_prose_hash(root: Path, workflow: dict[str, Any], chapter_id: str) -> str:
    """finalization 以已注册 candidate_prose 的 payload.text 为正文基准，这里取同一基准。"""
    entry = workflow.get('artifacts', {}).get(chapter_id, {}).get('candidate_prose')
    if not isinstance(entry, dict) or not entry.get('artifactPath'):
        raise ArtifactBindError(
            'BIND_DEPENDENCY_MISSING',
            'prose binding requires a registered candidate_prose artifact first',
            {'chapterId': chapter_id, 'artifactType': 'candidate_prose'},
        )
    try:
        from .mechanical_evidence import _registered
        from .structure_store import load_structure
        artifact = _registered(root, load_structure(root), workflow, chapter_id, 'candidate_prose')
        if artifact is None:
            raise ArtifactBindError('BIND_DEPENDENCY_UNREADABLE', 'current candidate is unavailable')
        text = artifact['payload']['text']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if getattr(exc, 'code', '') in {'CURRENT_CANDIDATE_UNALIGNED', 'CANDIDATE_INDEX_INVALID', 'CANDIDATE_VERSION_CORRUPT', 'CANDIDATE_SELECTION_REVIEW_REQUIRED'}:
            raise
        raise ArtifactBindError('BIND_DEPENDENCY_UNREADABLE',
                                'registered candidate_prose artifact is unreadable',
                                {'chapterId': chapter_id}) from exc
    if not isinstance(text, str) or not text.strip():
        raise ArtifactBindError('BIND_DEPENDENCY_UNREADABLE',
                                'registered candidate_prose payload.text is empty',
                                {'chapterId': chapter_id})
    return sha256_text(text)


def _outline_truth(root: Path, structure: dict[str, Any], chapter: dict[str, Any], field: str) -> str:
    from .source_plan import outline_binding
    binding = outline_binding(root, structure, chapter)
    if binding['field'] != field:
        raise ArtifactBindError(
            'BIND_OUTLINE_FIELD_MISMATCH',
            'outline binding field does not match this project mode',
            {'expectedField': binding['field'], 'requestedField': field, 'bindingKind': binding['kind']},
        )
    return str(binding['hash'])


def _state_truth(root: Path, field: str) -> str:
    try:
        from scripts import state_cache
    except (ModuleNotFoundError, ImportError):  # 直接以 scripts/core 身份运行时
        import state_cache
    name = STATE_BUILDERS[field]
    current = state_cache.build_state(root) if name == 'state' else state_cache.build_design(root)
    # Source transactions intentionally do not maintain Markdown-derived
    # caches. Bind the current source projection, as finalization does.
    return sha256_json(current)


def bind_fields(root: Path, *, structure: dict[str, Any], workflow: dict[str, Any],
                chapter_id: str, artifact: dict[str, Any],
                bindings: list[tuple[str, str | None]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fill payload binding fields from current truth; return (artifact, provenance).

    bindings 的每一项是 (field, raw_path|None)：带路径时按文件内容计算并与当前真源
    交叉核对（文件漂移即时失败），不带路径时直接采用当前真源。返回的 artifact 已重算
    artifactSha256；写回磁盘与登记由调用方完成。
    """
    chapter = next((item for item in structure.get('chapters', []) if item.get('chapterId') == chapter_id), None)
    if chapter is None:
        raise ArtifactBindError('BIND_CHAPTER_UNKNOWN', f'unknown chapter: {chapter_id}')
    artifact_type = artifact.get('artifactType')
    supported = BINDABLE_FIELDS.get(artifact_type, set())
    seen: set[str] = set()
    payload = artifact.get('payload')
    if not isinstance(payload, dict):
        raise ArtifactBindError('BIND_PAYLOAD_INVALID', 'artifact payload must be an object')
    provenance: dict[str, Any] = {}
    root = root.expanduser().resolve()
    for field, raw_path in bindings:
        if field not in supported:
            raise ArtifactBindError('BIND_FIELD_UNSUPPORTED',
                                    f'field {field} is not bindable for {artifact_type}',
                                    {'supported': sorted(supported)})
        if field in seen:
            raise ArtifactBindError('BIND_FIELD_DUPLICATE', f'field {field} bound more than once')
        seen.add(field)
        path = _resolve_source_path(root, raw_path) if raw_path is not None else None
        if field == 'proseBindingGeneration':
            truth = chapter['proseBinding']['generation']
            if path is not None:
                raise ArtifactBindError('BIND_FIELD_UNSUPPORTED', 'generation is computed from current ownership, not a file')
        elif field in PROSE_FIELDS:
            truth = _registered_prose_hash(root, workflow, chapter_id)
            if path is not None and file_sha256(path) != truth:
                raise ArtifactBindError(
                    'BIND_PROSE_MISMATCH',
                    'bound file differs from the registered candidate prose',
                    {'field': field, 'path': str(path), 'fileHash': file_sha256(path),
                     'registeredProseHash': truth},
                )
        elif field in STATE_BUILDERS:
            truth = _state_truth(root, field)
            if path is not None:
                try:
                    content_hash = sha256_json(read_json(path))
                except (OSError, ValueError) as exc:
                    raise ArtifactBindError('BIND_SOURCE_FILE_UNREADABLE',
                                            f'cannot parse binding source as JSON: {path}') from exc
                if content_hash != truth:
                    raise ArtifactBindError(
                        'BIND_STATE_MISMATCH',
                        'bound file differs from the current story projection',
                        {'field': field, 'path': str(path)},
                    )
        else:
            truth = _outline_truth(root, structure, chapter, field)
            if path is not None and file_sha256(path) != truth:
                raise ArtifactBindError(
                    'BIND_OUTLINE_MISMATCH',
                    'bound file differs from the chapter outline binding',
                    {'field': field, 'path': str(path), 'outlineHash': truth},
                )
        provenance[field] = {'contentSha256': truth, 'previousValue': payload.get(field),
                             **({'sourcePath': str(path)} if path is not None else {'source': 'current_truth'})}
        payload[field] = truth
    updated = {**artifact, 'payload': payload}
    updated['artifactSha256'] = sha256_json({key: value for key, value in updated.items() if key != 'artifactSha256'})
    return updated, provenance
