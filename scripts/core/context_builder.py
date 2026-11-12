#!/usr/bin/env python3
"""统一 Context Builder：确定性装配一份工位续写上下文。

这是全库**唯一**的正式上下文装配入口（P5）。此前该逻辑住在
``scripts/continuation_context.py``；现在正文装配全部收敛在此，
``scripts/continuation_context.py`` 退化为只做 re-export 与 CLI 的兼容 facade。
task_prepare / context_health / context_preheat / operations 都只**消费**本 Builder，
不再各自拼装或二次检索。

装配管线（project truth + source plan + POV/认识层过滤 + author decisions +
knowledge retrieval + voice profile + recent manuscript context → 唯一 Builder →
station context + dependency manifest + 确定性 contextHash + health/invalidation 证据）：

- 确定性提取（不烧 token）：数据源是不可变 StoryState/StoryDesign 投影与单一真源快照。
- 显式认识层（``layers``）：CANON FACTS / CANON SEMANTIC CONTRACT / CURRENT BELIEFS /
  IN-WORLD TEXT / APPROVED PLAN / UNAPPROVED CANDIDATES / AUTHOR DECISIONS /
  REFERENCE KNOWLEDGE / WORK VOICE PROFILE，
  各层不互相冒充；层是**紧凑的 ID+hash+role 出处**，不注入正文（正文仍走 POV 限定查询）。
- memory tier（working/episodic/semantic）与 epistemic role 是两个维度，互不替代。

数据源（只读，不初始化完整 Runtime）：
  真源布局：.govel/structure.json → 按需构建的机器投影；正文按注册路径读取
  checkout 布局：章节路径来自 structure 的 filePath
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .artifacts import file_sha256, sha256_json
from .paths import ProjectPathError, safe_project_path
from .project import continuation_policy
from .structure_store import is_quarantined, ordered_chapters, usable_chapters

try:  # state_cache/text_utils live at scripts/ level, not under core/
    from scripts import state_cache
    from scripts.text_utils import HAN_PATTERN
except ImportError:  # 以 `python3 scripts/xxx.py` 直接运行时
    import state_cache
    from text_utils import HAN_PATTERN

# 工位 → 该工位消费的上下文范围。这是唯一的工位覆盖策略，新增工位必须显式登记，
# 不允许复制一份装配逻辑另起一套。
#
#   full     走统一 Context Builder 的预算化工位包（默认站内视图）
#   none     不消费续写上下文：这些工位读准备清单、工作流工件与真源查询
#
# Titler/Repair/Continuity 的「参考」来自 promptPlan.onDemandReferences（工位**写作契约**，
# 由 prompt_plan 装载），与叙事上下文不是一回事，也不经过本 Builder；两条通道互不冒充。
STATION_CONTEXT = {
    'planner': 'full', 'writer': 'full', 'editor': 'full', 'query': 'full',
    'repair': 'none', 'titler': 'none', 'continuity': 'none', 'composer': 'none',
}


def station_context_scope(task: str) -> str:
    """Return 'full' or 'none' for a station; unknown stations get no context."""
    return STATION_CONTEXT.get(task, 'none')


class ContextBuilderError(ValueError):
    """Raised when required project evidence is missing or invalid."""

    def __init__(self, message: str):
        super().__init__(message)
        prefix = re.match(r"^([A-Z][A-Z_]+):", message)
        self.code = prefix.group(1) if prefix else "CONTEXT_INVALID"
        self.details = {}


# Legacy name kept so the continuation_context facade and existing callers/tests
# keep raising and catching the same type after the P5 move into core.
ContinuationContextError = ContextBuilderError



def _chapter_record(state: dict, chapter_id: str) -> dict:
    matches = [item for item in state.get("chapters", []) if item.get("chapterId") == chapter_id]
    if not matches:
        raise ContinuationContextError(f"找不到章节稳定 ID: {chapter_id!r}")
    return matches[0]


def _chapter_placement(record: dict) -> dict | None:
    """Read current structural placement without inferring historical fields."""
    placement = record.get('placement')
    return placement if isinstance(placement, dict) else None


def _chapter_volume(record: dict) -> str | None:
    placement = _chapter_placement(record)
    if isinstance(placement, dict) and placement.get("scope") == "volume":
        return placement.get("volumeId")
    return None


def _chapter_ordinal(state: dict, chapter_id: str) -> int:
    record = _chapter_record(state, chapter_id)
    ordinal = record.get("displayOrdinal")
    if not isinstance(ordinal, int) or ordinal < 1:
        raise ContinuationContextError(f"章节缺少有效 displayOrdinal: {chapter_id!r}")
    return ordinal


def _load_commit_data(project_root: Path, *, read_only: bool = False) -> tuple[object, dict, dict]:
    """加载当前 StoryState 与 StoryDesign，必要时重建派生缓存。

    真源：schema-5 的 structure.json，经 state_cache 构建状态与设计投影。
    只读取当前结构与注册正文；阅读导出不参与缓存输入。
    返回轻量 store 对象（仅暴露 root 供路径拼接）。
    """
    root = project_root.expanduser().resolve()
    try:
        state, design = state_cache.project_projections(root) if read_only else state_cache.load(root)
    except state_cache.StateCacheError as exc:
        raise ContinuationContextError(str(exc)) from exc

    class _Store:
        def __init__(self, root: Path) -> None:
            self.root = root

    return _Store(root), state, design














def _entities(state: dict) -> list[dict]:
    """实体图谱快照：道具/地点/角色的当前状态与来源章，供 Planner 直接取用。"""
    out = []
    for item in state.get("entities", []):
        out.append({
            "entityId": item.get("entityId"),
            "entityType": item.get("entityType"),
            "name": item.get("name"),
            "aliases": item.get("aliases", []),
            "introducedChapter": item.get("introducedChapter"),
            "status": item.get("status"),
            "sourceChapter": item.get("sourceChapter"),
        })
    return out


def _relations(state: dict) -> list[dict]:
    out = []
    for item in state.get("relations", []):
        out.append({
            "relationId": item.get("relationId"),
            "fromEntity": item.get("fromEntity"),
            "toEntity": item.get("toEntity"),
            "relationType": item.get("relationType"),
            "description": item.get("description"),
            "sourceChapter": item.get("sourceChapter"),
        })
    return out


def _plot_line(state: dict) -> list[dict]:
    chapters = usable_chapters(state)
    return [
        {
            "chapterId": item.get("chapterId"),
            "displayCode": item.get("displayCode"),
            "displayOrdinal": item.get("displayOrdinal"),
            "title": item.get("title"),
            "volumeId": _chapter_volume(item),
            "arcIds": [link["arcId"] for link in state.get("chapterArcLinks", []) if link.get("chapterId") == item.get("chapterId")],
        }
        for item in chapters
        if item.get("chapterId")
    ]


def _recent_chapters(store, state: dict, chapter_id: str, recent: int) -> list[dict]:
    """最近 N 章（序号不超过 chapter_id 的最后 N 章）的正文路径与标题。

    只输出路径与标题，不读全文——供 Planner 按需读 1-2 章原文，避免续写上下文膨胀。
    """
    target = _chapter_ordinal(state, chapter_id)
    chapters = sorted(
        (item for item in state.get("chapters", [])
         if item.get("status") != "archived" and not is_quarantined(item)
         and isinstance(item.get("displayOrdinal"), int) and item.get("displayOrdinal") <= target),
        key=lambda item: item.get("displayOrdinal", 0),
    )
    out = []
    for item in chapters[-recent:]:
        cid = item["chapterId"]
        relative = item.get("filePath") or f"正文/{cid}.md"
        try:
            path = safe_project_path(store.root, relative)
        except ProjectPathError:
            path = store.root / "__invalid__"
        out.append({
            "chapterId": cid,
            "title": item.get("title"),
            "relativePath": relative,
            "exists": path.is_file() and not path.is_symlink(),
        })
    return out


def _prose_digest(store, row: dict) -> str | None:
    """Content hash of one recent chapter's prose, or None when it is absent.

    Recent-chapter prose is optional continuity material: a missing or racing file is
    recorded as an absent dependency rather than failing the whole context build.
    """
    if not row.get("exists"):
        return None
    try:
        return file_sha256(safe_project_path(store.root, row["relativePath"]))
    except (ProjectPathError, OSError, ValueError):
        return None


def _committed_chapter_count(store, state: dict) -> int:
    """Count current finalized prose, not every allocated chapter identity."""
    count = 0
    for chapter in state.get("chapters", []):
        if chapter.get("status") != "stable":
            continue
        relative = chapter.get("filePath")
        if not isinstance(relative, str) or not relative:
            continue
        try:
            path = safe_project_path(store.root, relative)
        except ProjectPathError:
            continue
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
            count += 1
    return count


def _characters(design: dict) -> list[dict]:
    out = []
    for item in design.get("characters", []):
        out.append({
            "characterId": item.get("characterId"),
            "name": item.get("name"),
        })
    return out


def _load_story_summary(store) -> dict | None:
    """读取 .govel/aux/story-summary.json（非事务辅助摘要）。

    文件缺失或 schema 非法时返回 None（回退标题聚合），不阻断续写上下文。
    """
    from .summary_contract import load_summary
    return load_summary(store.root)


LAYERS = ("working", "semantic", "episodic")
CONFIG_SOURCE = "shared/registries/workflow.json.continuation"


def serialize_context(payload: dict) -> str:
    """The budget counts Unicode characters, including JSON escaping and whitespace."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _measure(payload: dict, budget_key: str) -> int:
    budget = payload[budget_key]
    budget["usedCharacters"] = 0
    while True:
        size = len(serialize_context(payload))
        if budget["usedCharacters"] == size:
            return size
        budget["usedCharacters"] = size


def _chapter_keyword_filter(chapter_goal: str, item_text: str) -> bool:
    return not chapter_goal or any(term in item_text for term in _extract_terms(chapter_goal))


def _extract_terms(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(rf"{HAN_PATTERN}+|[A-Za-z0-9_-]+", text or "")))


def _explicitly_visible(item: dict, pov: str) -> bool:
    audience = item.get("visibleTo")
    if "visibleTo" in item:
        return isinstance(audience, list) and pov in audience
    return item.get("visibility") == "public" or item.get("povCharacter") == pov


def _hard(item: dict) -> bool:
    return item.get("mandatory") is True or item.get("hardConstraint") is True


def _pov_state(state: dict, pov: str | None, chapter_id: str) -> tuple[dict, dict, int]:
    """Relevance never grants visibility. Unscoped narrative records are not POV evidence."""
    result = dict(state)
    provenance = {}
    blocked = 0
    for key in ('entities', 'relations', 'pairs', 'volumes', 'arcs', 'chapterArcLinks', 'references'):
        rows = state.get(key, [])
        kept = []
        for item in rows:
            if not pov:
                visible = True
            elif key == "entities" and item.get("entityId") == pov:
                visible = "visibleTo" not in item or _explicitly_visible(item, pov)
            else:
                visible = _explicitly_visible(item, pov)
            if visible:
                kept.append(item)
            elif _hard(item):
                blocked += 1
        result[key] = kept
        provenance[key] = {"input": len(rows), "retained": len(kept), "omittedPov": len(rows) - len(kept)}
    if pov:
        # Chapter identity/order/path are routing metadata; titles can disclose unseen events.
        result["chapters"] = [dict(item, title=item.get("title") if _explicitly_visible(item, pov) else None)
                              for item in state.get("chapters", [])]
    title_input = sum(bool(item.get("title")) for item in state.get("chapters", []))
    title_retained = sum(bool(item.get("title")) for item in result.get("chapters", []))
    provenance["chapterTitles"] = {"input": title_input, "retained": title_retained,
                                    "omittedPov": title_input - title_retained}
    return result, provenance, blocked


def _record_ids(item: dict) -> set[str]:
    result = set()
    for key, value in item.items():
        if key.endswith(("Id", "Ids", "Chapter")) or key in {
            "id", "fromEntity", "toEntity", "expectedResolveBy", "subject", "source_chapter"
        }:
            values = value if isinstance(value, list) else [value]
            result.update(entry for entry in values if isinstance(entry, str) and entry)
    return result


def _relevance_ids(ctx: dict, state: dict, goal: str) -> set[str]:
    target = ctx["targetChapter"]
    ids = {target}
    if ctx.get("povCharacter"):
        ids.add(ctx["povCharacter"])
    ids.update(_record_ids(_chapter_record(state, target)))
    for ref in state.get("references", []):
        if ref.get("sourceId") == target:
            ids.add(ref.get("targetId"))
        elif ref.get("targetId") == target:
            ids.add(ref.get("sourceId"))
    for entity in state.get("entities", []):
        names = [entity.get("entityId"), entity.get("name"), *entity.get("aliases", [])]
        if goal and any(isinstance(name, str) and name and name in goal for name in names):
            ids.add(entity.get("entityId"))
    return {value for value in ids if isinstance(value, str)}


def _memory_row(layer: str, field: str, item: dict, subject: str | None, mandatory: bool) -> dict:
    return {"layer": layer, "subject": subject, "field": field, "value": item,
            "status": item.get("status", "active"), "mandatory": mandatory,
            "source_chapter": item.get("sourceChapter") or item.get("plantedChapter"),
            "evidence": f"state:{field}:{subject}"}


def _refresh_pack(pack: dict) -> int:
    for layer in LAYERS:
        counts = pack["provenance"][layer]
        counts["retained"] = len(pack[layer])
        counts["retainedMandatory"] = sum(row["mandatory"] for row in pack[layer])
        counts["omittedBudget"] = counts["eligible"] - counts["retained"]
    pack["warnings"] = (["MEMORY_BUDGET_OMITTED"] if any(
        pack["provenance"][layer]["omittedBudget"] for layer in LAYERS) else [])
    return _measure(pack, "budget")


def _build_memory_pack(
    ctx: dict,
    state: dict,
    chapter_goal: str = "",
    task_type: str = "planner",
) -> dict:
    try:
        policy = continuation_policy()
    except ValueError as exc:
        raise ContinuationContextError(f"CONTINUATION_CONFIG_INVALID: {exc}") from exc
    if task_type not in policy["memoryBudgets"]:
        raise ContinuationContextError(f"CONTINUATION_TASK_INVALID: {task_type}")
    limit = policy["memoryBudgets"][task_type]
    # Also protect callers of this helper from rebuilding layers with unfiltered state.
    state, _, _ = _pov_state(state, ctx.get("povCharacter"), ctx["targetChapter"])
    relevant_ids = _relevance_ids(ctx, state, chapter_goal)
    scoped = bool(chapter_goal) or relevant_ids != {ctx["targetChapter"]}
    candidates = {layer: [] for layer in LAYERS}
    omitted_relevance = dict.fromkeys(LAYERS, 0)

    def add(layer: str, field: str, item: dict, subject: str | None, *, required: bool = False) -> None:
        related = bool(_record_ids(item) & relevant_ids) or (
            bool(chapter_goal) and _chapter_keyword_filter(chapter_goal, serialize_context(item)))
        mandatory = required or _hard(item)
        if not mandatory and scoped and not related:
            omitted_relevance[layer] += 1
            return
        row = _memory_row(layer, field, item, subject, mandatory)
        row["relevance"] = "required" if mandatory else "chapter" if related else "unscoped"
        candidates[layer].append(row)

    if chapter_goal:
        add("working", "goal", {"text": chapter_goal, "chapterId": ctx["targetChapter"]},
            ctx["targetChapter"], required=True)
    for collection, field, id_key, layer in (
        ("entities", "entity", "entityId", "semantic"),
        ("relations", "relation", "relationId", "semantic"),
        ("pairs", "pair", "pairId", "semantic"),
        ("volumes", "volume", "volumeId", "semantic"),
        ("arcs", "arc", "arcId", "semantic"),
        ("chapterArcLinks", "chapterArcLink", "arcId", "semantic"),
        ("references", "reference", "targetId", "semantic"),
    ):
        for item in state.get(collection, []):
            add(layer, field, item, item.get(id_key))
    pack = {"budget": {"total": limit, "unit": policy["memoryBudgetUnit"],
                       "scope": "memoryPack", "usedCharacters": 0},
            **{layer: [] for layer in LAYERS}, "warnings": [], "provenance": {},
            "configSource": CONFIG_SOURCE}
    for layer in LAYERS:
        pack["provenance"][layer] = {
            "candidates": len(candidates[layer]) + omitted_relevance[layer],
            "eligible": len(candidates[layer]), "omittedRelevance": omitted_relevance[layer],
        }
        candidates[layer].sort(key=lambda row: (not row["mandatory"], str(row["subject"]), serialize_context(row)))
        pack[layer] = [row for row in candidates[layer] if row["mandatory"]]
    required_size = _refresh_pack(pack)
    if required_size > limit:
        raise ContinuationContextError(
            f"CONTEXT_BUDGET_IMPOSSIBLE: mandatory memory and provenance require {required_size} characters; limit={limit}")
    for layer in LAYERS:
        for row in candidates[layer]:
            if row["mandatory"]:
                continue
            pack[layer].append(row)
            if _refresh_pack(pack) > limit:
                pack[layer].pop()
    _refresh_pack(pack)
    return pack




def _station_view(ctx: dict) -> dict:
    """Bound the entire station envelope, not just its nested memory pack."""
    import copy

    keep = {
        "projectId", "targetChapter", "targetDisplayCode", "targetOrdinal",
        "targetPlacement", "targetVolumeId", "targetArcLinks", "committedChapters", "recentChapters",
        "memoryPack", "summaryFreshness", "povCharacter", "task", "filterProvenance", "readiness",
        "sourceContext", "layers",
        "knowledgePack", "knowledgeRoute", "authorDecisions", "dependencyManifest",
        "staleDecisionIds", "unavailableDecisions", "workVoiceProfile", "unapprovedCandidates",
    }
    result = copy.deepcopy({key: value for key, value in ctx.items() if key in keep})
    limit = result["memoryPack"]["budget"]["total"]
    result["view"] = "station"
    result["contextBudget"] = {"total": limit, "unit": "serializedCharacters", "scope": "station",
                               "usedCharacters": 0, "configSource": CONFIG_SOURCE}
    result["recentChapterProvenance"] = {"input": len(result["recentChapters"]),
                                          "retained": len(result["recentChapters"]), "omittedBudget": 0}
    result["contextHash"] = "0" * len(ctx["contextHash"])
    if _measure(result, 'contextBudget') > limit:
        _compact_source_provenance(result)
    knowledge = result.get('knowledgePack', {}).get('items', [])
    while knowledge and _measure(result, 'contextBudget') > limit:
        # Reference knowledge is optional and yields budget to manuscript context.
        knowledge.pop()
        result['dependencyManifest']['knowledge'] = [
            {'assetId': item['assetId'], 'sha256': item['sha256']} for item in knowledge]
        result['knowledgePack']['omittedBudget'] = result['knowledgePack'].get('omittedBudget', 0) + 1
    # The epistemic layer must report what the station actually received, so budget
    # eviction is reflected there too instead of over-reporting loaded knowledge.
    for layer in result.get('layers') or []:
        if layer.get('role') == 'REFERENCE KNOWLEDGE':
            layer['items'] = [{'assetId': item['assetId'], 'sha256': item['sha256']} for item in knowledge]
            layer['itemCount'] = len(layer['items'])
    return bound_station_context(result)



def _compact_source_provenance(context: dict) -> None:
    """Page source identity metadata without omitting facts or changing relevance.

    Full view retains every ID/hash. The bounded station uses a manifest for
    known, non-injected source provenance only. Candidates and unknown extensions
    remain inline; reading content still uses the original POV-scoped query.
    """
    source = context.get('sourceContext', {})
    if not source.get('planQuery'):
        return
    known_kinds = {*CANON_RECORD_KINDS, *PLANNING_RECORD_KINDS, 'fragment', 'field'}
    known_keys = {'id', 'kind', 'contentHash', 'ownerId', 'contentRole'}
    for layer in context.get('layers', []):
        if layer.get('contentInjected') is not False:
            continue
        paged, inline = [], []
        for item in layer.get('items', []):
            if (item.get('kind') in known_kinds and set(item) <= known_keys
                    and isinstance(item.get('id'), str) and isinstance(item.get('contentHash'), str)):
                paged.append(item)
            else:
                inline.append(item)
        if not paged:
            continue
        layer['items'] = inline
        layer['inlineItemCount'] = len(inline)
        layer['sourceRecordManifest'] = {
            'count': len(paged), 'sha256': sha256_json(paged),
            'sourceHash': context['dependencyManifest']['sourceHash'],
            'sourcePlanHash': source['sourcePlanHash'],
            'query': dict(source['planQuery']),
            'contentReadQuery': source['contentQuery'],
            'contentInjected': False,
        }


def bound_station_context(payload: dict) -> dict:
    """Rebind the station budget and hash after adding transport metadata."""
    from copy import deepcopy

    result = deepcopy(payload)
    limit = result["contextBudget"]["total"]

    def measure() -> int:
        _refresh_pack(result["memoryPack"])
        return _measure(result, "contextBudget")

    # Reverse the optional selection priority. Required records are never candidates for eviction.
    for layer in reversed(LAYERS):
        for row in list(reversed(result["memoryPack"][layer])):
            if measure() <= limit:
                break
            if not row["mandatory"]:
                result["memoryPack"][layer].remove(row)
    while measure() > limit and result["recentChapters"]:
        result["recentChapters"].pop(0)
        counts = result["recentChapterProvenance"]
        counts["retained"] -= 1
        counts["omittedBudget"] += 1
    size = measure()
    if size > limit:
        raise ContinuationContextError(
            f"CONTEXT_BUDGET_IMPOSSIBLE: mandatory station context requires {size} characters; limit={limit}")
    result["contextHash"] = sha256_json({key: value for key, value in result.items() if key != "contextHash"})
    return result


def build_station_context(
    project_root: str | Path,
    chapter_id: str,
    *,
    recent: int = 2,
    task_type: str = "planner",
    pov_character: str | None = None,
    chapter_goal: str = "",
    view: str = "full",
    read_only: bool = False,
    knowledge_mode: str | None = None,
) -> dict:
    """确定性装配一份工位续写上下文，返回结构化 dict。这是唯一正式装配入口。

    task_type（planner/writer/editor）决定记忆预算；pov_character 在装配阶段过滤
    注入视角（禁止信息从不进入输出）；chapter_goal 唯一驱动知识检索并计入依赖。
    view=station 只返回预算化、带显式认识层的工位包；view=full 保留完整审计结果。
    """
    if not 1 <= recent <= 10:
        raise ContextBuilderError("recent 必须在 1-10 之间")
    if view not in {"full", "station"}:
        raise ContextBuilderError("view 必须是 full 或 station")
    root = Path(project_root).expanduser().resolve()
    store, state, design = _load_commit_data(root, read_only=read_only)
    target_chapter = _chapter_record(state, chapter_id)
    if is_quarantined(target_chapter):
        raise ContextBuilderError(f"CHAPTER_QUARANTINED: {chapter_id}")
    from .project import resolve_knowledge_mode, knowledge_policy
    selected_knowledge_mode = resolve_knowledge_mode(knowledge_mode, project_root=root)
    target = _chapter_ordinal(state, chapter_id)
    try:
        continuation = continuation_policy()
    except ValueError as exc:
        raise ContextBuilderError(f"CONTINUATION_CONFIG_INVALID: {exc}") from exc

    if pov_character and not any(c.get("characterId") == pov_character for c in design.get("characters", [])):
        raise ContextBuilderError(f"POV_CHARACTER_NOT_FOUND: {pov_character}")
    state, filter_provenance, blocked_constraints = _pov_state(state, pov_character, chapter_id)
    visible_characters = {item.get("entityId") for item in state.get("entities", [])}
    characters = _characters(design)
    if pov_character:
        characters = [item for item in characters if item.get("characterId") in visible_characters]

    ctx = {
        "filterProvenance": filter_provenance,
        "readiness": {"ready": blocked_constraints == 0,
                      "reasons": ["POV_HARD_CONSTRAINTS_UNAVAILABLE"] if blocked_constraints else [],
                      "unavailableMandatoryCount": blocked_constraints},
        "projectId": state.get("projectId"),
        "task": task_type,
        "targetChapter": chapter_id,
        "targetDisplayCode": target_chapter.get("displayCode"),
        "targetOrdinal": target,
        "targetPlacement": _chapter_placement(target_chapter),
        "targetVolumeId": _chapter_volume(target_chapter),
        "targetArcLinks": [link for link in state.get("chapterArcLinks", []) if link.get("chapterId") == chapter_id],
        "volumes": state.get("volumes", []),
        "arcs": state.get("arcs", []),
        "committedChapters": _committed_chapter_count(store, state),
        "characters": characters,
        "characterStates": [],
        "foreshadowing": [],
        "knowledgeStates": [],
        "pairStates": [],
        "decisions": [],
        "entities": _entities(state),
        "relations": _relations(state),
        "plotLine": _plot_line(state),
        "recentChapters": _recent_chapters(store, state, chapter_id, recent),
    }
    if pov_character:
        ctx["povCharacter"] = pov_character

    # Baseline dependency identity: every input that can change this station context.
    # The single-source block augments it with source/decision/voice hashes below.
    ctx["dependencyManifest"] = {
        "chapterId": chapter_id, "task": task_type, "povCharacter": pov_character,
        "chapterGoalHash": sha256_json(chapter_goal) if isinstance(chapter_goal, str) and chapter_goal.strip() else None,
        "recentChapters": [{"chapterId": row["chapterId"], "sha256": _prose_digest(store, row)}
                           for row in ctx["recentChapters"]],
        "summaryHash": None, "decisions": [], "knowledge": [],
    }
    from .source_plan import chapter_plan
    from .source_rules import evaluate_rules
    from .structure_store import load_structure
    from .source_impact import ReviewInputs
    from .context_preheat import preheat_fingerprint
    source = load_structure(root)
    # These consumers share this request's source snapshot and dependency graph.
    # No inputs or plan survive the build; later source changes still invalidate it.
    inputs = ReviewInputs(source)
    plan = chapter_plan(source, chapter_id, _inputs=inputs)
    source_index = inputs.index
    action = {"planner": "prepare", "writer": "draft", "editor": "review"}.get(task_type, "prepare")
    rules = evaluate_rules(source, chapter_id, action, root=root, _inputs=inputs, _plan=plan)
    if not rules["ready"]:
        ctx["readiness"]["ready"] = False
        ctx["readiness"]["reasons"].append("SOURCE_RULES_BLOCKED")
        ctx["readiness"]["unavailableMandatoryCount"] += sum(row["blocking"] for row in rules["items"])
    selection = {"ownerIds": plan["recipe"]["scopeIds"]}
    perspective_time = None
    if pov_character:
        selection = {"knowledgeMode": "perspective", "perspectiveIds": [pov_character],
                     "unknownTime": "exclude"}
        start_id = source_index.objects[chapter_id].get("storyTimeStartId")
        if start_id:
            selection["asOf"] = start_id
            perspective_time = {"status": "anchored", "asOf": start_id,
                                "basis": "chapter_start", "unknownTime": "excluded"}
        else:
            # An undated chapter cannot inherit all future beliefs. Unbounded
            # beliefs remain available; time-limited ones are counted as unknown.
            selection["asOf"] = None
            perspective_time = {"status": "unavailable", "reason": "chapter_start_unregistered",
                                "timeLimitedBeliefsWithheld": True}
    # Preheat contract: the fingerprint a preheat run after the immediate predecessor
    # would produce for this chapter. Lets a caller verify a prior preheat is still
    # valid for the context it is about to build without caching any future context.
    chapters_in_order = usable_chapters(source)
    position = next((index for index, row in enumerate(chapters_in_order)
                     if row["chapterId"] == chapter_id), None)
    preheat_hash = None
    if position:  # position > 0: there is a predecessor to preheat from
        try:
            preheat_hash = preheat_fingerprint(source, chapters_in_order[position - 1]["chapterId"],
                                                _plan=plan)
        except Exception:  # noqa: BLE001 - preheat is advisory; never block the build
            preheat_hash = None
    ctx["sourceContext"] = {
        "sourcePlanHash": plan["sourcePlanHash"],
        "planReady": plan["ready"],
        "planFragmentIds": plan["planFragmentIds"],
        "inputRecordCount": len(plan["records"]),
        "planQuery": {"tool": "query_source_chapter_plan", "chapterId": chapter_id, "view": "list"},
        "contentQuery": {"tool": "query_source", "selection": selection, "view": "list"},
        "rulesQuery": {"tool": "query_source_rules", "chapterId": chapter_id, "action": action},
        "authorDecisionsQuery": {"tool": "query_source_author_decisions", "scopeId": chapter_id,
                                 "status": "active"},
        "ruleSummary": {"ready": rules["ready"], "count": len(rules["items"]),
                        "blockingRuleIds": [row["ruleId"] for row in rules["items"] if row["blocking"]]},
        "preheatInputFingerprint": preheat_hash,
        "coverage": {"contentInjected": False, "readingExportRequired": False,
                     "registeredInputsOnly": True,
                     **({"perspectiveTime": perspective_time} if perspective_time else {}),
                     "fullSourceSnapshot": True, "conservativeInvalidation": True},
    }
    from .candidate_batch import _current_recipe, _workflow, validate_recipe
    candidate_recipe = _current_recipe(root, source, _workflow(root), chapter_id)
    if candidate_recipe is not None:
        validate_recipe(root, source, _workflow(root), chapter_id, candidate_recipe)
        ctx['candidateInputs'] = {'role': 'CONDITIONAL CANDIDATE INPUTS — NOT CANON',
                                  'effectivePlanHash': candidate_recipe['effectivePlanHash'],
                                  'inputs': candidate_recipe['inputs']}
    by_decision = {row['decisionId']: row for row in source.get('authorDecisions', [])}
    author_inputs = plan['authorInputs']
    selected = author_inputs['decisions']
    unavailable = author_inputs['unavailableDecisions']
    stale = [row['decisionId'] for row in unavailable]
    ctx['authorDecisions'] = [{'decisionId': row['decisionId'], 'statement': row['statement'],
                               'rationale': row['rationale'], 'scopeIds': row['scopeIds'],
                               'role': 'AUTHOR DECISIONS'}
                              for row in selected]
    ctx['staleDecisionIds'] = stale
    ctx['unavailableDecisions'] = unavailable
    if unavailable:
        ctx['readiness']['ready'] = False
        ctx['readiness']['reasons'].append('AUTHOR_DECISIONS_REQUIRE_REVIEW')
        ctx['readiness']['unavailableMandatoryCount'] += len(unavailable)
    ctx['dependencyManifest'].update({
        'sourceHash': inputs.source_hash,
        'sourcePlanHash': plan['sourcePlanHash'],
        'decisions': [{'decisionId': row['decisionId'], 'sha256': sha256_json(by_decision[row['decisionId']])}
                      for row in ctx['authorDecisions']]})
    profile = author_inputs['workVoiceProfile']
    if profile is not None:
        ctx['workVoiceProfile'] = {'role': 'WORK VOICE PROFILE', 'axes': profile['axes'],
                                   'examples': profile['examples'], 'applicability': profile['applicability']}
        ctx['dependencyManifest']['voiceProfileHash'] = profile['approval']['profileHash']

    ctx['dependencyManifest']['knowledgeMode'] = selected_knowledge_mode
    knowledge_config = knowledge_policy()
    ctx['dependencyManifest']['knowledgePolicy'] = {
        'version': knowledge_config['strategy']['version'], 'sha256': sha256_json(knowledge_config)}
    ctx['knowledgePack'] = {'role': 'REFERENCE KNOWLEDGE', 'mode': selected_knowledge_mode,
                            'policyVersion': knowledge_config['strategy']['version'],
                            'policyHash': sha256_json(knowledge_config),
                            'items': [], 'omittedBudget': 0}
    if task_type in {'planner', 'writer', 'editor'} and isinstance(chapter_goal, str) and chapter_goal.strip():
        from .knowledge_router import build_knowledge_pack
        from .project import knowledge_policy
        max_query = knowledge_policy()['maxIntentChars']
        knowledge_query = chapter_goal[:max_query]
        knowledge = build_knowledge_pack(planningIntent=knowledge_query, station=task_type, mode=selected_knowledge_mode)
        route = knowledge['route']
        ctx['knowledgePack'] = {k: v for k, v in knowledge.items() if k not in {'route', 'ok', 'code'}}
        ctx['knowledgePack']['queryTruncated'] = len(chapter_goal) > max_query
        ctx['dependencyManifest']['knowledgePolicy'] = {
            'version': knowledge['policyVersion'], 'sha256': knowledge['policyHash']}
        # Single routing decision, shared by every consumer. The route summary drops the
        # heavy per-asset index so the station envelope stays bounded; it reports the ranked
        # candidates while the pack reports the material actually loaded into context.
        ctx['knowledgeRoute'] = {
            'code': route['code'], 'station': route['station'], 'mode': route['mode'],
            'normalizedIntents': route['normalizedIntents'],
            'items': [{'assetId': row['assetId'], 'title': row['title'], 'sha256': row['sha256'],
                       'matchedIntents': row['matchedIntents'], 'score': row['score']}
                      for row in route['items']],
            'matchedCount': route['matchedCount'], 'hasMore': route['hasMore'],
            'queryTruncated': len(chapter_goal) > max_query}
        ctx['dependencyManifest']['knowledge'] = [
            {'assetId': item['assetId'], 'sha256': item['sha256']} for item in knowledge['items']]

    ctx["memoryPack"] = _build_memory_pack(ctx, state, chapter_goal=chapter_goal, task_type=task_type)

    # 注入 LLM 语义摘要（存在才注入；缺失则 plotLine 停留标题聚合）
    summary = _load_story_summary(store)
    from .summary_inputs import summary_inputs
    if summary is not None:
        current_excluded = summary_inputs(root, source)['excludedChapterIds']
        if summary.get('excludedChapterIds', []) != current_excluded:
            summary = None
    if summary is not None:
        summary_visible = not pov_character or _explicitly_visible(summary, pov_character)
        if summary_visible:
            ctx["storySummary"] = {
                "asOfChapterId": summary.get("asOfChapterId"),
                "sourceCommitId": summary.get("sourceCommitId"),
                "storySummary": summary.get("storySummary"),
                "plotLine": summary.get("plotLine", []),
                "characterSummaries": [item for item in summary.get("characterSummaries", [])
                                       if not pov_character or item.get("characterId") == pov_character],
            }
        ctx["filterProvenance"]["storySummary"] = {
            "input": 1, "retained": int(summary_visible), "omittedPov": int(not summary_visible)}
        ctx["dependencyManifest"]["summaryHash"] = sha256_json(summary)
    from .summary_status import summary_freshness
    from .structure_store import load_structure
    summary_source = source
    ctx["summaryFreshness"] = summary_freshness(root, summary_source, target_chapter_id=chapter_id, _summary=summary)
    if summary is not None:
        ctx["summaryFreshness"]["withheldByPov"] = not summary_visible
    # Explicit epistemic layers: every injected fact/plan/reference carries its role so
    # a belief never passes as canon, an unapproved candidate never as approved plan, and
    # a draft voice never as active guidance. Layers reference the pieces assembled above;
    # source-derived layers list bounded ID+hash provenance, never injected prose.
    ctx["layers"] = _epistemic_layers(ctx, source, plan, source_index, task_type=task_type,
                                      chapter_id=chapter_id)
    ctx["contextHash"] = sha256_json({key: value for key, value in ctx.items() if key != "contextHash"})
    return _station_view(ctx) if view == "station" else ctx


# Compatibility aliases: the single business entry, plus the legacy name the facade and
# existing callers/tests import. There is exactly one implementation.
build_context = build_station_context
build_continuation_context = build_station_context



CANON_RECORD_KINDS = ("attribute", "value", "relation", "entity")
PLANNING_RECORD_KINDS = ("unit", "volume", "arc", "chapter", "project")


def _relevant_candidate_ids(source: dict, ctx: dict, chapter_id: str) -> set[str]:
    """本章相关的 Planning Candidate：触及本章范围，或尚未定位（仍可能落入本章）。

    这是候选相关性的唯一判定，认识层与 Author Decision 适用性共用同一份结果，
    不允许两处各写一套规则。
    """
    from .author_decisions import relevant_planning_candidate_ids
    return relevant_planning_candidate_ids(source, chapter_id)


def _record_kind(rid: str, record: dict, source_index, entity_ids: set[str]) -> str:
    if rid in source_index.fragments:
        return "fragment"
    if rid in source_index.attributes:
        return "attribute"
    if rid in source_index.fields:
        return "field"
    if rid in source_index.values:
        return "value"
    if rid in entity_ids:
        return "entity"
    if isinstance(record, dict) and "relationId" in record:
        return "relation"
    return source_index.owner_types.get(rid) or "record"


def _provenance(rid: str, record: dict, source_index, entity_ids: set[str]) -> dict:
    """Compact ID+hash provenance for a source record; never injects prose."""
    item = {"id": rid, "kind": _record_kind(rid, record, source_index, entity_ids),
            "contentHash": sha256_json(record)}
    owner = source_index.owner(rid)
    if owner and owner != rid:
        item["ownerId"] = owner
    if rid in source_index.fragments:
        item["contentRole"] = source_index.fragments[rid].get("contentRole")
    return item


def _layer(role: str, epistemic_role: str, items: list, *, authoritative: bool,
           contentInjected: bool, note: str | None = None, **extra) -> dict:
    layer = {"role": role, "epistemicRole": epistemic_role, "authoritative": authoritative,
             "contentInjected": contentInjected, "items": items, "itemCount": len(items), **extra}
    if note:
        layer["note"] = note
    return layer


def _epistemic_layers(ctx: dict, source, plan, source_index, *, task_type: str, chapter_id: str) -> list[dict]:
    """Classify the assembled context into explicit, non-overlapping epistemic roles.

    Source-derived layers list bounded ID+hash provenance (content stays behind the
    POV-scoped query); the decision/knowledge/voice layers are thin pointers to the
    top-level keys so nothing is duplicated into the budgeted envelope. Records that
    match no role are listed explicitly rather than silently dropped.
    """
    canon, semantic_contract, beliefs, in_world, approved_plan = [], [], [], [], []
    approved_cand, unapproved_cand, unclassified = [], [], []
    if source is not None and plan is not None and source_index is not None:
        relations = {row["relationId"] for row in source.get("relations", [])}
        entity_ids = {row["entityId"] for row in source.get("entities", [])
                      if isinstance(row, dict) and isinstance(row.get("entityId"), str)}
        for row in plan["records"]:
            rid, record = row["id"], row["record"]
            item = _provenance(rid, record, source_index, entity_ids)
            kind = item["kind"]
            role = source_index.fragments[rid].get("contentRole") if rid in source_index.fragments else None
            if role == "belief":
                beliefs.append(item)
            elif role == "in_world_text":
                in_world.append(item)
            elif role == "plan":
                approved_plan.append(item)
            elif kind == "field":
                # FieldDefinition 规定 Attribute/Value 如何被解释，是 Canon 语义契约，
                # 不是世界事实本身；它也不进参考知识或已批准计划。
                semantic_contract.append(item)
            elif role == "fact" or kind in CANON_RECORD_KINDS:
                canon.append(item)
            elif kind in PLANNING_RECORD_KINDS:
                # Unit/volume/arc/chapter identity is approved planning structure, not world fact.
                approved_plan.append(item)
            else:
                unclassified.append(item)
        relevant_candidates = _relevant_candidate_ids(source, ctx, chapter_id)
        for cand in source.get("planningCandidates", []):
            if cand["candidateId"] not in relevant_candidates:
                continue
            entry = {"id": cand["candidateId"], "kind": "planning_candidate",
                     "status": cand.get("status"), "contentHash": sha256_json(cand)}
            (approved_cand if cand.get("status") == "approved" else unapproved_cand).append(entry)
    show_unapproved = task_type == "planner"
    decisions = ctx.get("authorDecisions", [])
    pack = ctx.get("knowledgePack") if isinstance(ctx.get("knowledgePack"), dict) else {}
    knowledge = pack.get("items", [])
    manifest = ctx.get("dependencyManifest", {})
    return [
        _layer("CANON FACTS", "canon_fact", canon, authoritative=True, contentInjected=False,
               note="现行正式世界事实；正文经 POV 限定查询读取，不在此注入"),
        _layer("CANON SEMANTIC CONTRACT", "canon_semantic_contract", semantic_contract,
               authoritative=True, contentInjected=False,
               note="字段定义（FieldDefinition）：规定 Attribute/Value 如何解释的 Canon 语义契约，不是世界事实本身"),
        _layer("CURRENT BELIEFS", "in_world_belief", beliefs, authoritative=False, contentInjected=False,
               note="某视角所相信，不等于 Canon 事实"),
        _layer("IN-WORLD TEXT", "in_world_text", in_world, authoritative=False, contentInjected=False,
               note="世界内文本；文本存在不等于其内容为真"),
        _layer("APPROVED PLAN", "authorial_plan",
               approved_plan + [{"id": c["id"], "kind": "planning_candidate", "status": "approved",
                                 "contentHash": c["contentHash"]} for c in approved_cand],
               authoritative=True, contentInjected=False,
               sourcePlanHash=(plan or {}).get("sourcePlanHash"),
               activeDecisionIds=[d["decisionId"] for d in decisions],
               unclassifiedRecordIds=[item["id"] for item in unclassified],
               note="已批准结构、计划片段与已批准候选；unclassifiedRecordIds 列出未归入任何角色的记录，不静默丢弃"),
        _layer("UNAPPROVED CANDIDATES", "unapproved_candidate",
               unapproved_cand if show_unapproved else [], authoritative=False, contentInjected=False,
               withheldForStation=not show_unapproved,
               note="未批准候选：非权威、未批准，不得当作 Canon 或已批准计划"),
        _layer("AUTHOR DECISIONS", "authorial_planning_decision",
               [{"id": d["decisionId"]} for d in decisions], authoritative=True, contentInjected=True,
               source="authorDecisions", note="已批准、当前、未 stale 的作者规划决定"),
        _layer("REFERENCE KNOWLEDGE", "reference_knowledge",
               [{"assetId": k["assetId"], "sha256": k["sha256"]} for k in knowledge],
               authoritative=False, contentInjected=True, source="knowledgePack",
               note="外部写作参考；即使写得很确定也不是 Canon"),
        _layer("WORK VOICE PROFILE", "approved_plan",
               [{"profileHash": manifest.get("voiceProfileHash")}] if ctx.get("workVoiceProfile") else [],
               authoritative=True, contentInjected=True, source="workVoiceProfile",
               note="作品级叙述风格（approved_plan）；不是 Canon，也不是角色对白声纹"),
    ]
