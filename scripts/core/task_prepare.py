"""Read-only preparation inventory for one station task.

工位的输入此前散在 SKILL、docs/CONTEXT.md 和 CLI 命令里：哪些材料必读、哪些
条件成立、缺什么、下一步做什么，都要调用方自己拼。本模块把它们合成一份可复现
清单，供任务准备与门禁共用，并且只读——它不建缓存、不写正文、不改设定。

机器就绪不等于语义正确：卡片结构通过不代表散文一致，原文路径也不代表该文件
全文已通过 POV 检验。清单只负责把「读什么、缺什么」讲清楚。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .artifacts import file_sha256
from .continuity import ContinuityError, continuity_status, pending_continuity, require_predecessors_ready
from .jsonio import read_json
from .paths import ProjectPathError, safe_project_path, stored_project_path
from .prompt_plan import PromptPlanError, resolve_prompt_plan
from .structure_store import StructureError, load_structure, ordered_chapters
from .workflow import default_workflow

TASK_STATIONS = {
    "planner": "govel-planner",
    "writer": "govel-writer",
    "editor": "govel-editor",
    "repair": "govel-editor",
    "titler": "govel-titler",
    "continuity": "govel-router",
}
TASK_DECISIONS = {"planner": "chapter", "titler": "title", "editor": "stage_b", "repair": "stage_b"}


class TaskPrepareError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _relative(root: Path, value: str, *, allow_absolute: bool = False) -> str | None:
    try:
        path = stored_project_path(root, value) if allow_absolute else safe_project_path(root, value)
        return str(path.relative_to(root))
    except (ProjectPathError, OSError, ValueError):
        return None


def _entry(root: Path, relative: str, reason: str, *, required: bool = True, lines: tuple[int, int] | None = None) -> dict:
    row = {"path": relative, "reason": reason, "required": required,
           "lines": list(lines) if lines else None, "sha256": None}
    try:
        path = safe_project_path(root, relative)
        from .prepare_dependencies import observe_dependency
        observe_dependency(root, relative)
        if path.is_file():
            row["sha256"] = file_sha256(path)
    except (ProjectPathError, OSError, ValueError):
        row["sha256"] = None
    return row


def _chapter_entities(structure: dict, chapter_id: str | None) -> tuple[list[str], list[str]]:
    """Entities the chapter actually references, plus an explicit-scope warning."""
    if chapter_id is None:
        return [], []
    found: list[str] = []
    for row in structure.get("references", []):
        if row.get("sourceId") == chapter_id and row.get("targetId"):
            found.append(str(row["targetId"]))
    for link in structure.get("chapterArcLinks", []):
        if link.get("chapterId") == chapter_id and link.get("arcId"):
            found.append(str(link["arcId"]))
    unresolved = [item for item in found if item and item not in {row["entityId"] for row in structure.get("entities", [])}
                  and item not in {row["arcId"] for row in structure.get("arcs", [])}]
    return sorted(set(found)), sorted(set(unresolved))


def _resolve_selectors(structure: dict, selectors: list[str]) -> tuple[list[str], list[str]]:
    by_name = {}
    for row in structure.get("entities", []):
        for key in (row.get("entityId"), row.get("name"), *row.get("aliases", [])):
            if key:
                by_name[str(key)] = row["entityId"]
    for row in structure.get("arcs", []):
        by_name.setdefault(str(row.get("arcId")), row["arcId"])
        by_name.setdefault(str(row.get("title")), row["arcId"])
    resolved, unknown = [], []
    for selector in selectors:
        (resolved if selector in by_name else unknown).append(by_name.get(selector, selector))
    return sorted(set(resolved)), sorted(set(unknown))


def _upstream(workflow: dict, chapter_id: str | None) -> dict:
    entries = workflow.get("artifacts", {}).get(chapter_id, {}) if chapter_id else {}
    approval = workflow.get("approvals", {}).get(chapter_id, {}) if chapter_id else {}
    hashes = approval.get("artifactHashes", {}) if isinstance(approval, dict) else {}
    rows = {}
    for kind in ("stage_a_review", "candidate_prose", "diagnostics", "stage_b_review", "state_candidate", "title_candidates"):
        entry = entries.get(kind) if isinstance(entries, dict) else None
        rows[kind] = {
            "registered": isinstance(entry, dict),
            "approved": bool(isinstance(entry, dict) and hashes.get(kind) == entry.get("artifactSha256")),
            "artifactSha256": entry.get("artifactSha256") if isinstance(entry, dict) else None,
            "artifactPath": entry.get("artifactPath") if isinstance(entry, dict) else None,
        }
    return rows


def _artifact_payload(root: Path, path: str) -> dict:
    try:
        data = read_json(stored_project_path(root, path))
    except (ProjectPathError, OSError, ValueError):
        return {}
    return data.get("payload", {}) if isinstance(data, dict) else {}


def _plan(root: Path, station: str, decision: str | None, chapter_id: str | None, review_class: str | None) -> dict:
    from .data_provider import ProviderError
    try:
        plan = resolve_prompt_plan(root, station, decision_type=decision, chapter_id=chapter_id,
                                   view="station", review_class=review_class)
    except (PromptPlanError, StructureError, ProviderError) as exc:
        return {"complete": False, "warnings": [{"code": exc.code, "message": str(exc), **exc.details}]}
    return {"planHash": plan["planHash"], "complete": plan["complete"], "warnings": plan["warnings"],
            'provider': plan['provider'], 'providerMaterials': plan['providerMaterials'],
            'roundtable': plan['roundtable'],
            'conditionInputsHash': plan['conditionInputsHash'],
            "contracts": [{"path": row["path"], "sha256": row["sha256"], "readRanges": row["readRanges"]}
                          for row in plan["contracts"]],
            "conditional": plan["conditional"],
            "onDemandReferences": [row["path"] for row in plan["onDemandReferences"]],
            "referenceEntries": plan['onDemandReferences'],
            "reviewPlan": plan.get("reviewPlan"),
            "characterCounts": plan["characterCounts"]}


def _context(root: Path, task: str, chapter_id: str | None, pov_character: str | None, chapter_goal: str, knowledge_mode: str | None = None) -> dict:
    from .context_builder import station_context_scope, build_station_context
    if station_context_scope(task) != "full" or chapter_id is None:
        return {"applicable": False, "ready": None, "reason": "本任务不消费续写上下文"}
    try:
        ctx = build_station_context(
            str(root), chapter_id, task_type=task,
            pov_character=pov_character, chapter_goal=chapter_goal, knowledge_mode=knowledge_mode, view="station", read_only=True)
    except (ValueError, OSError) as exc:
        return {"applicable": True, "ready": False, "code": getattr(exc, "code", "CONTEXT_INVALID"), "message": str(exc)}
    readiness = ctx.get("readiness", {})
    from .context_health import assess_context
    health = assess_context(ctx)
    from .context_transport import semantic_view
    model, proof = semantic_view(ctx) if health['ready'] else (None, None)
    return {"applicable": True, "ready": health['ready'],
            "code": readiness.get("code") or ("CONTEXT_HEALTH_BLOCKED" if not health['ready'] else None),
            "contextHash": ctx.get("contextHash"),
            # station 视图把预算放在顶层 contextBudget；此前误读不存在的 ctx["station"]，
            # 导致准备报告里的 budget 永远是 null。
            "budget": ctx.get("contextBudget") if isinstance(ctx.get("contextBudget"), dict) else None,
            "health": health,
            "modelContext": model,
            "modelProof": proof,
            "layers": ctx.get('layers'),
            "knowledgePack": ctx.get('knowledgePack'),
            # 知识路由只在统一 Builder 内执行一次；这里直接消费其结果，不再二次 route。
            "knowledgeRoute": ctx.get('knowledgeRoute'),
            "authorDecisions": ctx.get('authorDecisions', []),
            "dependencyManifest": ctx.get('dependencyManifest'),
            # 不传 --pov-character 时上下文**完全不做 POV 过滤**（continuation_context
            # 的 _pov_state 对 pov=None 一律可见）。同一对话里连续跑多工位时会连同其他
            # 角色的知识和上一轮规划层的全知内容一起带入，所以把「有没有过滤」显式报出。
            "povScoped": bool(pov_character),
            "message": "上下文缺少当前视角可用的硬约束或存在冲突" if not health['ready'] else ""}


def _chapter_summary(chapter: dict) -> dict:
    """Chapter projection that treats schema-3 ``placement`` as truth.

    schema 3 chapters carry ``placement``; the older ``volumeId`` is no longer the
    source field, so it is exposed as ``derivedVolumeId`` (never as ``volumeId``) to
    avoid looking like current chapter truth.
    """
    placement = chapter.get("placement")
    derived_volume = placement.get("volumeId") if isinstance(placement, dict) and placement.get("scope") == "volume" else None
    return {key: chapter.get(key) for key in
            ("chapterId", "displayCode", "title", "status", "displayOrdinal", "filePath")} | {
        "placement": placement, "derivedVolumeId": derived_volume}


def prepare_task(root: Path, task: str, *, held_prepare_ref: str | None = None, **kwargs) -> dict:
    from .transport_cache import put, get
    from .reading_handoff import decode_receipt
    from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
    root = root.expanduser().resolve()
    held = None
    if held_prepare_ref is not None:
        try:
            held = get(root, held_prepare_ref, kind='prepare')
        except StructureError:
            pass
    dependencies = capture_dependencies(root, chapter_id=kwargs.get('chapter_id'))
    with observe_reads(root) as observed:
        report = _prepare_task(root, task, **kwargs)
    bind_observed(dependencies, observed)
    try:
        validate_dependencies(root, dependencies, code='TASK_INPUT_CHANGED')
    except StructureError as exc:
        raise TaskPrepareError(exc.code, str(exc), exc.details) from exc
    from .context_transport import task_model_response
    model = task_model_response(report)
    # The exact projection is the baseline, not a future renderer's
    # reinterpretation of an old machine report. References are filled after hashing.
    model['prepareRef'] = None
    model['reading']['credential'] = None
    reference = put(root, 'prepare', {'dependencies': dependencies, 'report': report, 'model': model,
                                    'handoff': decode_receipt(report['reading']['credential'])})
    report['reading']['credential'] = reference
    report['prepareRef'] = reference
    if held_prepare_ref is not None:
        from .prepare_delivery import model_delivery
        report['_modelDelivery'] = model_delivery(root, report, held_prepare_ref, held)
        validate_dependencies(root, dependencies, code='TASK_INPUT_CHANGED')
    return report


def _prepare_task(root: Path, task: str, *, chapter_id: str | None = None, pov_character: str | None = None,
                 entities: list[str] | None = None, chapter_goal: str = "", review_class: str | None = None, knowledge_mode: str | None = None,
                 batch_ref: dict | None = None) -> dict:
    from .project import user_workflow_policy
    presentation = user_workflow_policy()
    route = None
    if task == "continue":
        # Consume the existing conflict-aware reconciler; no second status table.
        from .finalization import reconcile_workflow
        route = reconcile_workflow(root, persist=False)
        # A failed continuation still supplies public, non-authorizing reads.
        # The caller does not need to inspect workflow.json or structure.json.
        route = {**route, 'recovery': {
            'automatic': False, 'instruction': '先查询项目概览与诊断，核对缺项及作者范围，再按领域入口处理；不要修改内部 JSON 绕过。',
            'readActions': [
                {'operation': 'project.overview', 'arguments': {'projectRoot': str(root)}},
                {'operation': 'project.doctor', 'arguments': {'projectRoot': str(root), 'view': 'model'}},
            ]}}
        if not route["ok"]:
            raise TaskPrepareError(route["code"], "机器工作流存在冲突，先解决再继续", route)
        action = route["nextAction"]
        if action is None:
            raise TaskPrepareError("WORKFLOW_NO_NEXT_ACTION", "没有可继续的已授权任务", route)
        if chapter_id is not None and chapter_id != action["chapterId"]:
            raise TaskPrepareError("WORKFLOW_CHAPTER_MISMATCH", "指定章节与机器下一步不一致", route)
        chapter_id = action["chapterId"]
        task = presentation["actionTasks"][action["action"]]
        if task is None:
            raise TaskPrepareError("WORKFLOW_ACTION_REQUIRED", "下一步需要批准或维护操作", route)
    else:
        task = presentation["stages"].get(task, {}).get("task", task)
    if task not in TASK_STATIONS:
        raise TaskPrepareError("TASK_UNKNOWN", f"unsupported task: {task}", {"supported": sorted(TASK_STATIONS)})
    root = root.expanduser().resolve()
    missing: list[dict] = []
    stage = next(alias for alias, row in presentation["stages"].items()
                 if task in [row["task"], *row["subtasks"]])
    notes: list[str] = [f"当前阶段：{presentation['stages'][stage]['label']}。内部任务依据请求或机器下一步选择，审批仍由作者决定。", "机器就绪不代表语义正确：卡片结构通过不等于散文一致，本清单不代替工位审查。"]

    def require(ok: bool, code: str, message: str, *, blocking: bool = True, **details: Any) -> bool:
        if not ok:
            missing.append({"code": code, "message": message, "blocking": blocking, "details": details})
        return ok

    structure = None
    try:
        structure = load_structure(root)
    except (StructureError, OSError, ValueError) as exc:
        missing.append({"code": getattr(exc, "code", "STRUCTURE_INVALID"), "message": str(exc), "blocking": True, "details": {}})
    try:
        workflow = read_json(root / ".govel" / "workflow.json")
        if not isinstance(workflow, dict):
            raise ValueError("workflow state must be an object")
    except (OSError, ValueError):
        workflow = default_workflow()
    transactions = sorted(path.stem for path in (root / ".govel/transactions").glob("*.json"))
    require(not transactions, "TRANSACTION_INCOMPLETE", "先运行 workflow recover 处理中断事务", transactions=transactions)

    chapter = None
    if structure is not None and chapter_id is not None:
        chapter = next((row for row in structure["chapters"] if row["chapterId"] == chapter_id), None)
        require(chapter is not None, "CHAPTER_NOT_FOUND", "结构中没有这个章节稳定 ID", chapterId=chapter_id)
    ready_flags = {"structureHealthy": structure is not None and not transactions}
    source_plan = None
    if structure is not None and chapter is not None:
        from .source_plan import chapter_plan
        source_plan = chapter_plan(structure, chapter_id)
    settings_ready = structure is not None
    read_list: list[dict] = []
    if task == 'planner' and structure is not None:
        read_list.append({'kind': 'source-query', 'tool': 'query_source_pairs', 'arguments': {},
                          'required': True, 'reason': '核对配对分类及适用范围；缺失评估不能当作不适用'})
    if source_plan is not None:
        read_list.append({'kind': 'source-query', 'tool': 'query_source_chapter_plan',
                          'arguments': {'chapterId': chapter_id, 'view': 'detail'},
                          'sourcePlanHash': source_plan['sourcePlanHash'],
                          'sourceHash': source_plan['snapshot']['sourceHash'], 'required': True,
                          'reason': '真源中的章节与上层计划，以及登记的依赖输入；按页读取'})
    if task == 'planner' and chapter is not None and structure is not None and pov_character is None:
        preceding = [row for row in ordered_chapters(structure) if row['displayOrdinal'] < chapter['displayOrdinal']]
        if preceding:
            prior = preceding[-1]
            read_list.append({'kind': 'source-query', 'tool': 'query_source_chapter_plan',
                              'arguments': {'chapterId': prior['chapterId'], 'view': 'detail'},
                              'required': False, 'reason': '前章计划与未解决事项；不继承旧审查结论'})
            for kind in ('candidate_prose', 'stage_a_review', 'stage_b_review'):
                path = _upstream(workflow, prior['chapterId'])[kind]['artifactPath']
                relative = _relative(root, path, allow_absolute=True) if path else None
                if relative:
                    read_list.append(_entry(root, relative, f'前章 {kind} 历史材料，不授予当前通过', required=False))
    agents = _entry(root, "AGENTS.md", "项目级最高写作约束（可选）", required=False)
    if agents["sha256"]:
        read_list.append(agents)

    referenced, unresolved = _chapter_entities(structure, chapter_id) if structure is not None else ([], [])
    # 本章范围来自当前结构中已登记的 `references`（章 → 实体）与章节弧线链接；
    # 下方另并入 source_plan 的登记依赖闭包与本次显式选择器。
    # 阅读导出中的定位表或人物名称不会建立实体引用，也不提供计划权威。
    referenced = sorted(set(referenced))
    resolved, unknown = _resolve_selectors(structure, entities or []) if structure is not None else ([], list(entities or []))
    for selector in unknown:
        missing.append({"code": "ENTITY_UNKNOWN", "message": "无法把选择器解析为稳定 ID", "blocking": True, "details": {"selector": selector}})
    scope = sorted(set(referenced) | set(resolved))
    if source_plan is not None:
        from .source_model import SourceIndex
        source_index = SourceIndex.build(structure)
        scope = sorted(set(scope) | {rid for rid in (row['id'] for row in source_plan['records'])
                                   if rid in source_index.objects})
    if structure is not None and resolved:
        read_list.append({'kind': 'source-query', 'tool': 'query_source',
                          'arguments': {'selection': {'ownerIds': resolved}, 'view': 'detail'},
                          'required': True, 'reason': '本次显式选择的源对象'})
    require(bool(scope), "SCOPE_UNRESOLVED", "未能从结构或参数定位本章相关实体；相关卡片尚未核验")
    if unresolved:
        notes.append("章节引用了结构中没有的 ID：" + "、".join(unresolved))

    if task == "continuity":
        if chapter is None:
            missing.append({"code": "CONTINUITY_CHAPTER_REQUIRED", "message": "连续性任务必须指定已入库章节", "blocking": True, "details": {}})
        elif structure is not None:
            status = continuity_status(root, structure, workflow, chapter)
            require(status["status"] not in {"pending", "receipt_missing"}, "CONTINUITY_REQUIRED",
                    "该章的连续性收尾尚未确认", chapterId=chapter_id, status=status["status"])
            require(status.get("readyForContinuation", False), "CHAPTER_FACTS_REQUIRED",
                    "连续性确认与事实交接分别核对；完成保留的B事实待办后继续", chapterId=chapter_id,
                    factInventory=status.get("factInventory"))
    elif structure is not None and chapter is not None:
        try:
            candidate_recipe = None
            if batch_ref is not None:
                from .candidate_batch import prepare_candidate_recipe
                prepared_recipe = prepare_candidate_recipe(root, batchRef=batch_ref, chapterId=chapter_id)
                candidate_recipe = prepared_recipe['recipe']
                read_list.append({'kind': 'candidate-inputs', 'required': True, 'recipeRef': prepared_recipe['recipeRef'],
                    'recipe': candidate_recipe, 'reason': '冻结上游候选及条件交接材料；不是Canon，不代表正式采用'})
            require_predecessors_ready(structure, workflow, chapter, root=root, candidate_recipe=candidate_recipe)
        except ContinuityError as exc:
            missing.append({"code": exc.code, "message": str(exc), "blocking": True, "details": exc.details})
        except (StructureError, ValueError) as exc:
            missing.append({"code": getattr(exc, "code", "PREDECESSOR_INVALID"), "message": str(exc), "blocking": True, "details": {}})

    if structure is not None and chapter is not None:
        approved = chapter_id in workflow.get("approvedChapterIds", [])
        require(approved, "CHAPTER_OUTSIDE_APPROVED_RANGE", "本章不在作者批准范围内", chapterId=chapter_id)
        from .source_rules import evaluate_rules
        rule_check = evaluate_rules(structure, chapter_id, 'prepare', root=root)
        require(rule_check['ready'], 'SOURCE_RULES_BLOCKED', '本章准备阶段存在未通过的必检规则',
                ruleIds=[row['ruleId'] for row in rule_check['items'] if row['blocking']])
        if task in {'writer', 'editor', 'repair', 'titler'}:
            require(bool(source_plan and source_plan['ready']), 'SOURCE_CHAPTER_PLAN_MISSING',
                    '真源中缺少本章已编写的计划片段', chapterId=chapter_id)
        upstream = _upstream(workflow, chapter_id)
        if task in {'writer', 'editor', 'repair', 'titler'}:
            try:
                from .current_candidate import require_candidate_selection
                from .mechanical_evidence import _registered
                candidate = _registered(root, structure, workflow, chapter_id, 'candidate_prose')
                require_candidate_selection(root, structure, chapter_id, candidate['payload'] if candidate else None)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                missing.append({'code': getattr(exc, 'code', 'CURRENT_CANDIDATE_UNAVAILABLE'),
                    'message': str(exc), 'blocking': True, 'details': getattr(exc, 'details', {})})
        if task in {"writer", "editor", "repair"}:
            require(upstream["stage_a_review"]["approved"], "STAGE_A_NOT_APPROVED",
                    "阶段 A 工件未登记或未按原 hash 批准", chapterId=chapter_id)
        if task in {"writer", "editor", "repair", "titler"} and source_plan is not None and upstream["stage_a_review"]["artifactPath"]:
            binding_field = 'sourcePlanHash'
            declared = str(_artifact_payload(root, upstream["stage_a_review"]["artifactPath"]).get(binding_field, ""))
            current = source_plan['sourcePlanHash']
            require(bool(declared) and declared == current, "STAGE_A_OUTLINE_MISMATCH",
                    "已批准阶段 A 未绑定当前推演；推演改动后须重新审查", chapterId=chapter_id)
        if task in {"editor", "repair", "titler"}:
            require(upstream["candidate_prose"]["registered"], "CANDIDATE_PROSE_MISSING", "候选正文尚未登记", chapterId=chapter_id)
        previous = [row for row in ordered_chapters(structure) if row["displayOrdinal"] < chapter["displayOrdinal"]
                    and row.get("status") == "stable"]
        if task in {"planner", "writer"}:
            if previous:
                last = previous[-1]
                read_list.append(_entry(root, last["filePath"], f"前一稳定章正文（{last['chapterId']}）"))
            else:
                notes.append("尚无前一稳定章正文；CH001 依赖 ARC001 已填实。")
        for kind, row in upstream.items():
            relative = _relative(root, row["artifactPath"], allow_absolute=True) if row["artifactPath"] else None
            if relative:
                read_list.append(_entry(root, relative, f"已登记工件（{kind}）",
                                        required=kind in {"stage_a_review", "candidate_prose"}))
        if task == "continuity":
            read_list.append(_entry(root, f"工作台/{chapter_id}-设定回写候选.md", "可选讨论材料；事实须经受审源提案、批准提交与 evidence 登记，候选本身不是真源或完成证据", required=False))
        notes.append(f"章节状态 {chapter.get('status')}；本任务要求的上游证据见 upstream。")


    from .outline_facts import inspect_outline
    outline_facts = inspect_outline(root, chapterId=chapter_id, structure=structure) if chapter_id and chapter and structure else None
    if pov_character is None and outline_facts and outline_facts.get('pov'):
        pov_character = outline_facts['pov']
    context = _context(root, task, chapter_id, pov_character, chapter_goal, knowledge_mode)
    if context.get("applicable") and context.get("ready") is False:
        missing.append({"code": context.get("code") or "CONTEXT_NOT_READY", "message": context.get("message") or "上下文就绪检查未通过", "blocking": True, "details": {}})
    # 知识检索唯一发生在统一 Context Builder 内；准备清单只消费其结果，杜绝二次 route
    # 得到与 context.read/context.health 不一致的 knowledge。
    knowledge_route = context.get("knowledgeRoute")
    knowledge_pack = context.get('knowledgePack')

    plan = _plan(root, TASK_STATIONS[task], TASK_DECISIONS.get(task), chapter_id if task != "continuity" else chapter_id, review_class)
    if plan.get("complete") is False:
        missing.append({"code": "CONTRACT_PLAN_INCOMPLETE", "message": "契约装载计划存在未解析条件", "blocking": True,
                        "details": {"warnings": plan.get("warnings", [])}})
    tool_plan = None
    if structure is not None:
        try:
            from .tool_plan import build_tool_plan
        except ImportError:
            from core.tool_plan import build_tool_plan
        tool_plan = build_tool_plan(root, task, chapter_id=chapter_id, outline_facts=outline_facts)
        # 必要工具的当前证据缺失时不能静默放行：只读/候选动作可自动执行，
        # 需作者批准的动作进 pendingApprovals，语义审查由工位执行并交回结论工件。
        for row in tool_plan["toolActions"]:
            if row["status"] != "action-required" or not row["necessary"]:
                continue
            if row["authorization"] == "station":
                notes.append(f"{row['capabilityId']}：由工位执行语义审查，系统只验证结论工件与绑定。")
                continue
            missing.append({"code": "TOOL_ACTION_REQUIRED", "blocking": True,
                            "message": row["reason"], "details": {"capabilityId": row["capabilityId"],
                                                                   "operation": row["binding"].get("ref"),
                                                                   "evidence": row["evidenceRequired"],
                                                                   "authorization": row["authorization"]}})
        for row in tool_plan["unresolved"]:
            missing.append({"code": "CAPABILITY_UNRESOLVED", "blocking": True, "message": row["reason"], "details": row})
    blocking = [row for row in missing if row.get("blocking", True)]
    authorization_ready = not any(row["code"] in {
        "CHAPTER_OUTSIDE_APPROVED_RANGE", "PREDECESSOR_UNSTABLE", "CONTINUITY_REQUIRED", "CHAPTER_NOT_FOUND"} for row in blocking)
    # 本清单只给**读取参考**：说明该读哪些文件、在哪几行，由工位按任务自行读取。
    # 不把卡片正文抬成字段值塞进报告——那等于替工位决定读到的内容，与「参考而非注入」相反。
    # 范围为空时单列可读标志；--entity 仅补充本次读取范围。
    # 持久计划输入由所属 plan 片段的 dependsOn 声明，经源预览／提交登记；
    # dependsOn 只展开显式输入闭包，不把实体 ID 当作整套事实的隐式引用。
    scope_ready = bool(scope)
    if not scope_ready and structure is not None and chapter is not None and task in {"planner", "writer", "editor", "repair"}:
        missing.append({"code": "CHAPTER_ENTITY_LINK_MISSING", "blocking": False,
                        "message": "本章尚未定位到相关源对象；可用 --entity 补充本次读取范围，该参数不登记依赖或授予批准。持久计划依赖须查询当前源取得所需属性／片段的稳定 ID，在本章所属 plan 片段的 dependsOn 中声明，并经 source change-preview → source commit 登记；dependsOn 只取显式输入闭包，阅读快照不建立实体引用。",
                        "details": {"chapterId": chapter_id, "scope": scope[:10]}})
    readiness = {
        "structureHealthy": ready_flags["structureHealthy"],
        "settingsReady": settings_ready,
        "contextReady": context.get("ready"),
        "authorizationReady": authorization_ready,
        "scopeReady": scope_ready,
        "taskReady": not blocking,
    }
    notes.append("settingsReady 只证明真源数据结构与绑定有效；散文语义仍由 App 核对。")
    notes.append("列为必读的材料不再由脚本代读；上下文里的原文路径只是定位信息。")
    pending = []
    if structure is not None:
        try:
            pending = pending_continuity(structure, workflow, root=root)
        except ContinuityError as exc:
            missing.append({"code": exc.code, "message": str(exc), "blocking": True, "details": exc.details})
    # Include late continuity failures before publishing readiness or an executable action.
    blocking = [row for row in missing if row.get("blocking", True)]
    readiness["taskReady"] = not blocking
    workflow_action = route["nextAction"] if route is not None else workflow.get("nextAction")
    action_matches = (workflow_action is not None and workflow_action.get("chapterId") == chapter_id
                      and presentation["actionTasks"].get(workflow_action.get("action")) == task)
    result = {
        "ok": readiness["taskReady"],
        "code": "TASK_READY" if readiness["taskReady"] else "TASK_NOT_READY",
        "task": task,
        "projectId": structure['projectId'] if structure is not None else None,
        "station": TASK_STATIONS[task],
        "userStage": stage,
        "chapterId": chapter_id,
        "outlineFacts": outline_facts,
        "chapter": None if chapter is None else _chapter_summary(chapter),
        "readiness": readiness,
        "readList": read_list,
        # 上下文状态此前算完即弃：只取了 readiness.contextReady，contextHash／budget／
        # povScoped／message 从未进入报告，工位无法判断 POV 过滤到底做没做。补进报告。
        "context": context,
        "knowledgeRoute": knowledge_route,
        "knowledgePack": knowledge_pack,
        "missing": missing,
        "blocking": blocking,
        "notes": notes,
        "scope": {"entities": scope, "povCharacter": pov_character, "coverage": "本章引用的实体与显式选择器；未列出的卡片不在此次核验范围"},
        "upstream": _upstream(workflow, chapter_id),
        "nextAction": workflow_action if readiness["taskReady"] and action_matches else None,
        "workflowNextAction": workflow_action,
        "actionReadiness": {"executable": readiness["taskReady"] and action_matches,
                            "blockingCodes": sorted({row["code"] for row in blocking}),
                            "reason": "resolve task blockers before continuing" if blocking else
                                      "workflow action belongs to another task" if not action_matches else "ready"},
        "pendingContinuity": pending,
        "promptPlan": plan,
        "toolPlan": tool_plan,
        "settingsReport": {"valid": settings_ready, "ready": settings_ready,
                           "errorCount": 0 if settings_ready else 1, "warningCount": 0},
    }
    if task == 'planner' and chapter is None and structure is not None and not structure.get('chapters'):
        result['bootstrap'] = {'station': 'govel-composer', 'chapterPrepareRequired': False,
            'missingInputs': ['核心人物与边界', '作品视角与文风', '卷与弧线', '首章计划及作者批准'],
            'nextOperation': 'project.structure.insert-preview', 'grantsApproval': False}
        result['notes'].append('空项目先按 govel-composer/SKILL.md 形成设计；本报告的章节范围缺失不禁止创意讨论。设计获批后建章并登记源计划，再准备写作。')
    if route is not None and chapter is not None:
        following = next((row for row in ordered_chapters(structure) if row['displayOrdinal'] > chapter['displayOrdinal']), None)
        result['nextChapterPlanning'] = ({'chapterId': following['chapterId'], 'status': following['status'],
            'operation': 'task.prepare', 'arguments': {'chapterId': following['chapterId'], 'task': 'planner'},
            'grantsWritingApproval': False} if following else {
            'status': 'not_created', 'operation': 'project.structure.insert-preview',
            'requiredInputs': ['title', 'volumeId', 'primaryArcId'], 'grantsWritingApproval': False,
            'instruction': '当前章仍按现有下一步处理；如需先规划后章，从此入口预览建章，不能跳过当前章收尾或范围批准。'})
    from .reading_handoff import handoff
    result['reading'] = handoff(root, result, {'task': task, 'chapterId': chapter_id,
        'povCharacter': pov_character, 'chapterGoal': chapter_goal, 'knowledgeMode': knowledge_mode})
    from .operations import new_operations
    result['readingTools'] = [{'operation': spec.id, 'tool': spec.name, 'inputSchema': spec.input_schema}
                              for spec in new_operations() if result['station'].removeprefix('govel-') in spec.audiences
                              and task in spec.tasks]
    return result
