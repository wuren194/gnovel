#!/usr/bin/env python3
"""长卷剧情摘要的确定性外壳（LLM 语义摘要由摘要生成环节产出，本脚本管输入与落盘）。

参照 DeterminFlow ``src/compression/`` 的 FULL 压缩理念：长卷续写上下文必然膨胀，
旧章节压成"剧情/人物/设定摘要" + 保留最近原文。确定性版（continuation_context
的 plotLine 标题聚合）只给骨架；语义摘要（因果链、冲突推进、人物关系变化）只有
正文里有，必须由 LLM 读取正文生成。

本脚本是摘要能力的**确定性边界**：
- ``prepare``：从状态缓存与已提交章节提取摘要输入（章节清单/正文路径/StoryState 概要），
  输出给摘要生成环节的上下文——生成者按需读正文全文后产出摘要 JSON。
- ``commit``：校验摘要生成环节返回的 JSON（必填键校验），原子写入
  ``.gnovel/aux/story-summary.json``（独立辅助产物，不属于项目标记与派生缓存）。

续写时 ``continuation_context.py`` 读取 aux 摘要注入 `storySummary`，缺失则回退
标题聚合。

CLI:
  python3 scripts/story_summarizer.py prepare --project-root <项目>
  python3 scripts/story_summarizer.py commit --project-root <项目> --input <摘要.json>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from scripts import state_cache
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, EXIT_OK, FailClosedArgumentParser
    from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, ordered_chapters
except ImportError:  # 以 `python3 scripts/xxx.py` 直接运行时
    import state_cache
    from core.exit_codes import EXIT_FAIL_CLOSED, EXIT_OK, FailClosedArgumentParser
    from core.structure_store import PROJECT_SCHEMA_VERSION, ordered_chapters

TOOLBOX_ROOT = Path(__file__).resolve().parent.parent
CHAPTER_ORDINAL_RE = re.compile(r"CH(\d+)", re.IGNORECASE)
try:
    from scripts.text_utils import chinese_char_count
except ImportError:
    from text_utils import chinese_char_count


class StorySummarizerError(ValueError):
    """Raised when required project evidence is missing or invalid."""


def _load_commit_data(project_root: Path) -> tuple[object, dict, dict, dict]:
    """加载当前 StoryState 与 StoryDesign（只读），返回轻量 store 对象。

    真源：设定/ 的 md → state_cache 重建派生缓存 .gnovel/cache/；
    无 md 真源时回退旧版 状态/*.json + 设定/*.json。
    sourceCommitId 取 StoryState 的 head 事件（历史 commit 归档存在时不改语义）。
    """
    root = Path(project_root).expanduser().resolve()
    try:
        state, design = state_cache.project_projections(root)
    except state_cache.StateCacheError as exc:
        raise StorySummarizerError(str(exc)) from exc

    class _Store:
        def __init__(self, root: Path) -> None:
            self.root = root

    return _Store(root), state, state, design


def _char_count(path: Path) -> int:
    if not path.is_file() or path.is_symlink():
        return 0
    try:
        return chinese_char_count(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def _load_aux_summary(project_root: Path) -> dict | None:
    """读 .gnovel/aux/story-summary.json（存在且合法时）。"""
    try:
        from scripts.core.summary_contract import load_summary
    except ImportError:
        from core.summary_contract import load_summary
    return load_summary(project_root)


def _prepare_context(project_root: Path, *, view: str = 'full') -> dict:
    try:
        from scripts.core.prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
        from scripts.core.summary_updates import prepare_update, _load_prior
    except ImportError:
        from core.prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
        from core.summary_updates import prepare_update, _load_prior
    root = project_root.expanduser().resolve()
    dependencies = capture_dependencies(root)
    baseline, _ = _load_prior(root)
    with observe_reads(root) as observed:
        report = _build_prepare_context(root, view=view)
    bind_observed(dependencies, observed)
    validate_dependencies(root, dependencies, code='SUMMARY_INPUT_CHANGED')
    return prepare_update(root, report, dependencies, baseline)


def _build_prepare_context(project_root: Path, *, view: str = 'full') -> dict:
    try:
        from scripts.core.project import current_schema_version
    except ImportError:
        from core.project import current_schema_version
    store, commit, state, design = _load_commit_data(project_root)
    head = commit.get("head") or state.get("head") or {}
    head_id = head.get("eventId") if isinstance(head, dict) else head

    chapters = ordered_chapters(state)
    old = _load_aux_summary(project_root)
    try:
        from scripts.core.summary_inputs import summary_inputs, summary_delta
        from scripts.core.structure_store import load_structure
    except ImportError:
        from core.summary_inputs import summary_inputs, summary_delta
        from core.structure_store import load_structure
    source = load_structure(store.root)
    if source['revision'] != state.get('structureRevision'):
        raise StorySummarizerError('摘要结构输入在准备中变化，请重试')
    inputs = summary_inputs(store.root, source)
    delta = summary_delta(old.get('inputSnapshot') if old else None, inputs,
                          old.get('coveredChapterIds', []) if old else [])
    selected_ids = set(delta['affectedChapterIds'])
    new_chapters = [item for item in chapters if item['chapterId'] in selected_ids]
    if not chapters:
        return {'ok': True, 'upToDate': True, 'reason': 'no_chapters', 'schemaVersion': current_schema_version(),
                'asOfChapterId': None, 'projectId': state.get('projectId'), 'committedChapters': 0,
                'chapters': [], 'incremental': old is not None, 'refresh': delta,
                'message': '没有可生成摘要的章节'}

    chapter_meta = []
    for item in new_chapters:
        cid = item["chapterId"]
        relative = item.get("filePath") or f"正文/{cid}.md"
        path = store.root / relative
        chapter_meta.append({
            "chapterId": cid,
            "title": item.get("title"),
            "relativePath": relative,
            "charCount": _char_count(path),
        })

    state_digest = {
        "headChapter": state.get("head", {}).get("chapterId"),
        "decisions": [
            {"decisionId": item.get("decisionId"), "decision": item.get("decision")}
            for item in state.get("decisions", [])
        ][-12:],
        "characterStates": [
            {
                "characterId": item.get("characterId"),
                "physical": item.get("physical"),
                "emotional": item.get("emotional"),
                "inventoryIds": item.get("inventoryIds", []),
            }
            for item in state.get("characterStates", [])
        ],
        "openForeshadowing": [
            {"foreshadowId": item.get("foreshadowId"), "description": item.get("description")}
            for item in state.get("foreshadowing", [])
            if item.get("status") in ("open", "advanced")
        ],
    }

    incremental = old is not None and not delta["fullRefresh"]
    if incremental:
        instruction = (
            "已有上一版剧情摘要（见 priorSummary）。请只阅读下方 chapters 列出的"
            "新增或受影响章节正文（按 relativePath，相对项目根），在 priorSummary 基础上"
            "增量合并：storySummary 修订受影响因果并融入新章节推进、plotLine 更新"
            "旧章条目并追加新章推进、characterSummaries 更新到最新状态。未列出的章节无需重读。"
            "只返回字段齐全的完整摘要 JSON（非仅新增）。"
        )
    else:
        instruction = (
            "请阅读下方 chapters 的正文（按 relativePath，相对项目根），生成初始"
            "story-summary JSON：storySummary 整体剧情（因果链/冲突推进/伏笔脉络）、"
            "plotLine 每章关键推进、characterSummaries 每角色到当前的状态与关系变化。"
            "只返回字段齐全的摘要 JSON。"
        )

    result = {
        'ok': True, 'upToDate': not new_chapters and not delta['fullRefresh'],
        "schemaVersion": current_schema_version(),
        "projectId": state.get("projectId"),
        "asOfChapterId": chapters[-1]["chapterId"],
        "asOfDisplayOrdinal": chapters[-1]["displayOrdinal"],
        "structureRevision": state.get("structureRevision"),
        "coveredChapterIds": [item["chapterId"] for item in chapters],
        "sourceCommitId": head_id,
        "committedChapters": len(chapters),
        "incremental": incremental,
        "inputSnapshot": inputs,
        "refresh": delta,
        "priorAsOfChapterId": old.get("asOfChapterId") if old else None,
        "chapters": chapter_meta,
        "storyStateDigest": state_digest,
        "characters": [
            {"characterId": item.get("characterId"), "name": item.get("name")}
            for item in design.get("characters", [])
        ],
        "instruction": instruction,
    }
    if old is not None:
        result["priorSummary"] = {
            "storySummary": old.get("storySummary"),
            "plotLine": old.get("plotLine", []),
            "characterSummaries": old.get("characterSummaries", []),
        }
    result['instruction'] += ' 回传 inputSnapshot 原样绑定已读输入；fullRefresh 时用 priorSummary 对照重审完整因果。'
    if view == 'model':
        try:
            from scripts.core.transport_cache import put
        except ImportError:
            from core.transport_cache import put
        result['inputSnapshotRef'] = put(store.root, 'summary', result.pop('inputSnapshot'))
        result['instruction'] = result['instruction'].replace('inputSnapshot 原样', 'inputSnapshotRef 原样')
    result['inputSkeleton'] = {key: result[key] for key in
        ('schemaVersion', 'projectId', 'asOfChapterId', 'structureRevision', 'coveredChapterIds',
         'sourceCommitId', 'asOfDisplayOrdinal')}
    snapshot_key = 'inputSnapshotRef' if view == 'model' else 'inputSnapshot'
    result['inputSkeleton'][snapshot_key] = result[snapshot_key]
    result['inputSkeleton'].update(storySummary='', plotLine=[], characterSummaries=[])
    return result


def _commit_summary(project_root: Path, input_path: str) -> dict:
    try:
        from scripts.core.locks import project_lock_reentrant
    except ImportError:
        from core.locks import project_lock_reentrant
    with project_lock_reentrant(project_root.expanduser().resolve()):
        return _commit_summary_locked(project_root, input_path)


def _commit_summary_locked(project_root: Path, input_path: str) -> dict:
    path = Path(input_path).expanduser()
    if not path.is_absolute():
        path = project_root / path
    raw = path.resolve().read_text(encoding="utf-8")
    payload = json.loads(raw)
    try:
        from scripts.core.summary_contract import validate_summary
    except ImportError:
        from core.summary_contract import validate_summary
    try:
        validate_summary(payload)
    except ValueError as exc:
        raise StorySummarizerError(str(exc)) from exc
    if 'inputSnapshotRef' in payload:
        try:
            from scripts.core.transport_cache import get
        except ImportError:
            from core.transport_cache import get
        snapshot = get(project_root, payload['inputSnapshotRef'], kind='summary')
        if 'inputSnapshot' in payload and payload['inputSnapshot'] != snapshot:
            raise StorySummarizerError('摘要输入快照与引用不一致')
        payload['inputSnapshot'] = snapshot
    for required in ("schemaVersion", "asOfChapterId", "storySummary", "projectId"):
        if required not in payload:
            raise StorySummarizerError(f"摘要 JSON 缺必需字段: {required}")

    store, commit, state, design = _load_commit_data(project_root)
    if payload.get("projectId") != state.get("projectId"):
        raise StorySummarizerError("摘要 projectId 与项目不一致")
    chapter_ids = {item.get("chapterId") for item in state.get("chapters", [])}
    if payload.get("asOfChapterId") not in chapter_ids:
        raise StorySummarizerError("摘要 asOfChapterId 不属于当前项目")
    if payload.get("structureRevision") != state.get("structureRevision"):
        raise StorySummarizerError("摘要 structureRevision 已过期，请重新 prepare")

    if 'inputSnapshot' in payload:
        try:
            from scripts.core.summary_inputs import summary_inputs
            from scripts.core.structure_store import load_structure
        except ImportError:
            from core.summary_inputs import summary_inputs
            from core.structure_store import load_structure
        if payload['inputSnapshot'] != summary_inputs(store.root, load_structure(store.root)):
            raise StorySummarizerError('摘要输入已变化，请重新 prepare')
        if payload.get('coveredChapterIds') != payload['inputSnapshot']['chapterOrder']:
            raise StorySummarizerError('摘要覆盖范围必须与已准备的章节一致')

    try:
        from scripts.core.summary_updates import persist_summary
    except ImportError:
        from core.summary_updates import persist_summary
    return persist_summary(store.root, payload, prepare_ref=payload.get('prepareRef'))


def main(argv: list[str] | None = None) -> int:
    parser = FailClosedArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="提取摘要输入给摘要生成环节")
    p_prepare.add_argument("--project-root", required=True)
    p_prepare.add_argument("--summary", action="store_true", help="输出纯文本摘要而不是 JSON")

    p_commit = sub.add_parser("commit", help="校验并落盘摘要 JSON 到 .gnovel/aux/")
    p_commit.add_argument("--project-root", required=True)
    p_commit.add_argument("--input", required=True, help="摘要生成环节返回的 JSON 文件路径")

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            ctx = _prepare_context(Path(args.project_root).expanduser().resolve())
            if args.summary:
                lines = [f"项目 {ctx['projectId']} · 已提交 {ctx['committedChapters']} 章 · asOf {ctx['asOfChapterId']}"]
                for item in ctx["chapters"]:
                    lines.append(f"- {item['chapterId']} {item['title']}（{item['charCount']} 字）→ {item['relativePath']}")
                print("\n".join(lines))
            else:
                print(json.dumps(ctx, ensure_ascii=False, indent=2))
        else:
            report = _commit_summary(Path(args.project_root).expanduser().resolve(), args.input)
            print(json.dumps(report, ensure_ascii=False, indent=2))
    except (StorySummarizerError, json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"story_summarizer: {exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
