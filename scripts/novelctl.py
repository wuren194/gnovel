#!/usr/bin/env python3
"""govel schema-5 project command line interface."""

from __future__ import annotations

try:
    from scripts.runtime_requirements import require_supported_python
except ImportError:
    from runtime_requirements import require_supported_python

require_supported_python()

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

try:
    from scripts import state_cache, story_summarizer
except ImportError:
    import state_cache
    import story_summarizer

try:
    from scripts import intensity_registry
    from scripts.core.artifact_templates import ARTIFACT_TYPES, artifact_template, validate_artifact_schema_declarations
    from scripts.core.catalog_api import CATALOG_OPERATIONS, call_catalog_operation, snake_case
    from scripts.core.chapter_receipt import bound_receipt_response, chapter_receipt
    from scripts.core.chapter_refs import annotate as annotate_chapter_refs, resolve_chapter_arguments
    from scripts.core.finalization import (finalize_chapter, pending_transactions,
                                           preview_accept_external_modification,
                                           recover_transactions, reconcile_workflow)
    from scripts.core.refactor import commit_refactor
    from scripts.core.chapter_state import ADVANCEABLE_STATUSES, advance_chapter_status
    from scripts.core.clock import utc_now
    from scripts.core.commands import (EXPORT_STATUSES, chapter_prose, entity_graph, export_manuscript, get_entity,
                                    graph_mermaid, project_overview, story_timeline, structure_graph, timeline_mermaid)
    from scripts.core.importer import (REQUIRED_PROJECT_DIRS, create_external_manuscript_review,
                                       create_project_from_candidate, validate_import_candidate,
                                       write_import_inventory)
    from scripts.core.jsonio import read_json, write_json
    from scripts.core.locks import project_lock, project_lock_reentrant
    from scripts.core.markdown_sync import write_view_file
    from scripts.core.prompt_plan import PromptPlanError, resolve_prompt_plan
    from scripts.core.setting_sync import state_preview
    from scripts.core.settings import check_settings, query_settings, settings_schema
    from scripts.core.workflow import (
        WorkflowError, approve_artifacts, default_workflow, normalize_workflow,
        record_artifact, set_next_action, validate_next_action_for_project,
    )
    from scripts.core.artifact_bindings import ArtifactBindError, bind_fields
    from scripts.core.artifacts import ArtifactError, validate_envelope
    from scripts.core.project_doctor import project_doctor
    from scripts.core.project import (
        default_approval_mode, unsupported_error_code,
        project_template_path, protected_paths, repair_policy, continuation_policy,
        serial_planning_policy, toolbox_manifest,
    )
    from scripts.core.structure_commands import (
        commit_plan, gc_project, list_operations, list_pending_plans,
        preview_archive_chapter, preview_create_time_anchor,
        preview_insert_chapter,
        preview_set_chapter_timespan,
        preview_move_chapter, undo_operation,
    )
    from scripts.core.structure_store import (
        PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, StructureError,
        empty_structure, load_structure, save_structure,
    )
except ImportError:
    import intensity_registry
    from core.artifact_templates import ARTIFACT_TYPES, artifact_template, validate_artifact_schema_declarations
    from core.catalog_api import CATALOG_OPERATIONS, call_catalog_operation, snake_case
    from core.chapter_receipt import bound_receipt_response, chapter_receipt
    from core.chapter_refs import annotate as annotate_chapter_refs, resolve_chapter_arguments
    from core.finalization import (finalize_chapter, pending_transactions,
                                   preview_accept_external_modification,
                                   recover_transactions, reconcile_workflow)
    from core.refactor import commit_refactor
    from core.chapter_state import ADVANCEABLE_STATUSES, advance_chapter_status
    from core.clock import utc_now
    from core.commands import (EXPORT_STATUSES, chapter_prose, entity_graph, export_manuscript, get_entity,
                            graph_mermaid, project_overview, story_timeline, structure_graph, timeline_mermaid)
    from core.importer import (REQUIRED_PROJECT_DIRS, create_external_manuscript_review,
                               create_project_from_candidate, validate_import_candidate,
                               write_import_inventory)
    from core.jsonio import read_json, write_json
    from core.locks import project_lock, project_lock_reentrant
    from core.markdown_sync import write_view_file
    from core.prompt_plan import PromptPlanError, resolve_prompt_plan
    from core.setting_sync import state_preview
    from core.settings import check_settings, query_settings, settings_schema
    from core.workflow import (
        WorkflowError, approve_artifacts, default_workflow, normalize_workflow,
        record_artifact, set_next_action, validate_next_action_for_project,
    )
    from core.artifact_bindings import ArtifactBindError, bind_fields
    from core.artifacts import ArtifactError, validate_envelope
    from core.project_doctor import project_doctor
    from core.project import (
        default_approval_mode, unsupported_error_code,
        project_template_path, protected_paths, repair_policy, continuation_policy,
        serial_planning_policy, toolbox_manifest,
    )
    from core.structure_commands import (
        commit_plan, gc_project, list_operations, list_pending_plans,
        preview_archive_chapter, preview_create_time_anchor,
        preview_insert_chapter,
        preview_set_chapter_timespan,
        preview_move_chapter, undo_operation,
    )
    from core.structure_store import (
        PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, StructureError,
        empty_structure, load_structure, save_structure,
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_TEMPLATE = project_template_path("agentsMd")

try:
    from scripts.core.call_logging import (call_scope, capture_cli_streams, observe_arguments,
                                           observe_file_input, observe_payload, response_outcome)
    from scripts.core.model_response import (cli_operation, presentation_target, project_response,
                                            response_view as requested_response_view)
except ImportError:
    from core.call_logging import (call_scope, capture_cli_streams, observe_arguments,
                                   observe_file_input, observe_payload, response_outcome)
    from core.model_response import (cli_operation, presentation_target, project_response,
                                     response_view as requested_response_view)


def _emit(payload, **options):
    observe_payload("response", payload)
    print(json.dumps(payload, **options))


def _read_observed_json(path, label):
    text = path.read_text(encoding="utf-8")
    observe_file_input(label, path, text)
    return json.loads(text)


class NovelError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.message, "details": self.details}


def _project_json(root: Path) -> dict:
    path = root / ".govel" / "project.json"
    try:
        marker = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise NovelError("PROJECT_NOT_INITIALIZED", "missing or corrupt project marker") from exc
    if marker.get("schemaVersion") != PROJECT_SCHEMA_VERSION or marker.get("workflowVersion") != WORKFLOW_VERSION:
        raise NovelError(unsupported_error_code(), "仅支持 schema 5 项目；旧小说请使用 project import scan/create")
    return marker


def _workflow_path(root: Path) -> Path:
    return root / ".govel" / "workflow.json"


def _workflow_default() -> dict:
    return default_workflow()


def _read_workflow(root: Path) -> dict:
    path = _workflow_path(root)
    if not path.is_file():
        return _workflow_default()
    try:
        return normalize_workflow(read_json(path))
    except WorkflowError as exc:
        raise NovelError(exc.code, str(exc), exc.details) from exc


def _write_workflow(root: Path, data: dict) -> dict:
    try:
        data = normalize_workflow(data)
    except WorkflowError as exc:
        raise NovelError(exc.code, str(exc), exc.details) from exc
    data["updatedAt"] = utc_now()
    # 重入锁：CLI 的外层门禁已持锁时不再取；MCP/直接调用（没有外层锁）在这里自保。
    with project_lock_reentrant(root):
        write_json(_workflow_path(root), data)
    return data


def _initialize(root: Path, *, project_id: str, book_name: str, idempotency_key: str,
                approval_mode: str) -> tuple[dict, bool, list[str]]:
    """The existing CLI names share the same project creation implementation."""
    try:
        from scripts.core.project_lifecycle import initialize_project
    except ImportError:
        from core.project_lifecycle import initialize_project
    try:
        return initialize_project(root, project_id=project_id, book_name=book_name,
                                  idempotency_key=idempotency_key, approval_mode=approval_mode)
    except StructureError as exc:
        raise NovelError(exc.code, str(exc), exc.details) from exc


def _approval_scope(structure: dict, chapter_ids: list[str], *, allow_gaps: bool, reason: str) -> dict:
    """校验批准范围并生成可审计的范围记录。

    此前只保存一个任意 ID 列表：无法表达来自哪一卷、是否连续、为什么跳章，
    doctor 也无从判断范围是否越卷。默认强制串行纪律，需要离散批准时显式放行。
    """
    selected = [item for item in structure.get("chapters", []) if item.get("chapterId") in set(chapter_ids)]
    archived = sorted(item["chapterId"] for item in selected if item.get("status") == "archived")
    if archived:
        raise NovelError("APPROVAL_INCLUDES_ARCHIVED", "archived chapters cannot be approved", {"chapterIds": archived})
    from importlib import import_module
    chapter_volume_id = import_module('scripts.core.source_placement' if __package__ else 'core.source_placement').chapter_volume_id
    version = structure.get('schemaVersion')
    volumes = {chapter_volume_id(item, version) for item in selected}
    scope_volumes = sorted(v for v in volumes if v is not None)
    ordinals = sorted(int(item.get("displayOrdinal", 0)) for item in selected)
    scope = {
        "volumeIds": scope_volumes,
        "includesProjectScope": None in volumes,
        "fromOrdinal": ordinals[0] if ordinals else None,
        "toOrdinal": ordinals[-1] if ordinals else None,
        "chapterCount": len(selected),
        "allowGaps": bool(allow_gaps),
        "reason": reason.strip(),
        "approvedAt": utc_now(),
    }
    if allow_gaps:
        if not reason.strip():
            raise NovelError("APPROVAL_REASON_REQUIRED", "--allow-gaps 必须同时给出 --reason")
        return scope
    if len(volumes) > 1:
        raise NovelError("APPROVAL_RANGE_CROSSES_VOLUME", "approval range spans multiple volumes", {"volumeIds": scope_volumes, "includesProjectScope": None in volumes})
    if ordinals and ordinals != list(range(ordinals[0], ordinals[-1] + 1)):
        raise NovelError("APPROVAL_RANGE_NOT_CONTIGUOUS", "approval range must be contiguous", {"ordinals": ordinals})
    if ordinals:
        unstable = sorted(
            item["chapterId"]
            for item in structure.get("chapters", [])
            if item.get("status") not in {"stable", "archived"}
            and chapter_volume_id(item, version) in volumes
            and int(item.get("displayOrdinal", 0)) < ordinals[0]
        )
        if unstable:
            raise NovelError(
                "APPROVAL_PREDECESSOR_UNSTABLE",
                "chapters before the approved range are not stable yet",
                {"chapterIds": unstable},
            )
    return scope


def _reject_output_without_mermaid(args: argparse.Namespace, action: str) -> None:
    """`--format json` 不给 `--output` 落盘：静默忽略会让调用方以为文件已写出。"""
    if getattr(args, "output", None):
        raise NovelError(
            "OUTPUT_REQUIRES_MERMAID",
            f"{action} 的 --output 只用于 --format mermaid；json 结果直接输出到 stdout",
            {"format": getattr(args, "format", None), "output": args.output},
        )


def _view_result(root: Path, args: argparse.Namespace, code: str, title: str, mermaid: str) -> dict:
    """mermaid 视图的统一返回：默认只到 stdout；给了 --output 才落盘，且遵守生成物栅栏。"""
    result = {"ok": True, "code": code, "mermaid": mermaid}
    if getattr(args, "output", None):
        written = write_view_file(root, args.output, title=title, mermaid=mermaid)
        if written["refused"]:
            raise NovelError("VIEW_OUTPUT_REFUSED", "目标文件不带 GENERATED 标记，拒绝覆盖", written)
        result["output"] = written
    return result


def _resolve_chapter_args(args: argparse.Namespace) -> list[dict]:
    """把章节参数里的显示编号（CH001）换成稳定 chapterId；CLI 两类入口共用。"""
    values = vars(args)
    root_value = values.get("projectRoot", values.get("project_root"))
    if not isinstance(root_value, str):
        return []
    return resolve_chapter_arguments(Path(root_value or ".").expanduser().resolve(), values)


def _dispatch(args: argparse.Namespace) -> dict:
    resolved = _resolve_chapter_args(args)
    observe_arguments(vars(args), effective=True)
    result = annotate_chapter_refs(_dispatch_resolved(args), resolved)
    if result.get('code') == 'CHAPTER_RECEIPT':
        return bound_receipt_response(Path(args.project_root).expanduser().resolve(), result)
    return result


def _dispatch_resolved(args: argparse.Namespace) -> dict:
    if getattr(args, "operation_binding", None):
        try:
            from scripts.core.operations import dispatch_new
        except ImportError:
            from core.operations import dispatch_new
        return dispatch_new(args)
    workflow_write = args.action == "workflow" and args.workflow_action in {
        "approve-chapters", "repair-round", "artifact-register", "artifact-approve", "set-next",
    }
    cache_write = args.action == "state" and args.state_action == "rebuild"
    summary_write = args.action == "summary" and args.summary_action == "commit"
    view_write = (args.action == "structure" and args.structure_action in {"graph", "timeline"}
                  and getattr(args, "format", None) == "mermaid" and getattr(args, "output", None))
    # `export` 读正文并写成品稿；与 finalize 串行，免于读到写到一半的正文。
    # 外层允许取得锁；导出内部核验 pending 与正文归属，拒绝未收尾写集。
    export_write = args.action == "export"
    if workflow_write or cache_write or summary_write or view_write or export_write:
        root = Path(args.project_root).expanduser().resolve()
        _project_json(root)
        with project_lock(root, allow_pending=export_write):
            return _dispatch_command(args)
    return _dispatch_command(args)


def _read_json_payload_file(root: Path, value: str, label: str) -> object:
    """从文件读 JSON 载荷（相对项目根解析；绝对路径按原样读）。

    读操作不出围栏：作者可以在任意位置起草散文字段文件，写入真源仍走同一条事务。
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NovelError(f"{label}_FILE_UNREADABLE", f"cannot read {label} file: {path}", {"error": str(exc)}) from exc
    observe_file_input(label, path, text)
    try:
        payload = json.loads(text)
        return payload
    except json.JSONDecodeError as exc:
        raise NovelError(f"{label}_FILE_INVALID", f"{label} file is not valid JSON: {path}", {"error": str(exc)}) from exc






def _dispatch_command(args: argparse.Namespace) -> dict:
    root = Path(getattr(args, "project_root", ".")).expanduser().resolve()
    if args.action in {"quick-init", "init"}:
        marker, created, copied_templates = _initialize(root, project_id=args.project_id or str(uuid.uuid4()), book_name=args.book_name, idempotency_key=args.idempotency_key or str(uuid.uuid4()), approval_mode=args.approval_mode or default_approval_mode())
        agents = "not_requested"
        if args.action == "quick-init":
            target = root / "AGENTS.md"
            if args.no_agents_md:
                agents = "skipped_flag"
            elif target.exists():
                agents = "skipped_existing"
            elif (template := project_template_path('agentsMd')).is_file():
                target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
                agents = "copied"
        return {"ok": True, "code": "PROJECT_INITIALIZED" if created else "IDEMPOTENT_REPLAY", "project": marker, "agentsMd": agents, "templates": copied_templates, "doctor": project_doctor(root)}
    if args.action == "import":
        if args.import_action == "scan":
            result = write_import_inventory(Path(args.source), Path(args.output))
            return {"ok": True, "code": "IMPORT_INVENTORY_WRITTEN", "output": str(Path(args.output).resolve()), "files": len(result["files"])}
        if args.import_action == "review":
            return create_external_manuscript_review(Path(args.source), targetRoot=args.target_root)
        if args.import_action == "validate":
            candidate = _read_observed_json(Path(args.candidate).expanduser().resolve(), "import_candidate")
            report = validate_import_candidate(candidate)
            if not report["valid"]:
                raise NovelError("IMPORT_CANDIDATE_INVALID", "candidate failed validation", report)
            return {"ok": True, "code": "IMPORT_CANDIDATE_VALID", **report}
        candidate = _read_observed_json(Path(args.candidate).expanduser().resolve(), "import_candidate")
        result = create_project_from_candidate(root, candidate, approval_mode=args.approval_mode or default_approval_mode())
        return {"ok": True, "code": "PROJECT_IMPORTED", "project": result["project"]}

    if args.action == "workflow" and args.workflow_action == "artifact-template":
        result = artifact_template(args.type, project_id=args.project_id, chapter_id=args.chapter_id)
        if args.output:
            write_json(Path(args.output).expanduser().resolve(), result)
            return {"ok": True, "code": "ARTIFACT_TEMPLATE_WRITTEN", "output": str(Path(args.output).expanduser().resolve()), "artifactType": args.type}
        return {"ok": True, "code": "ARTIFACT_TEMPLATE", **result}

    _project_json(root)
    if args.action == "settings":
        if args.settings_action == "show":
            result = query_settings(root, selector=args.selector, kind=args.kind, fields=args.field or None)
            if args.strict and not result["matchedCount"]:
                raise NovelError("SETTING_NOT_FOUND", "没有匹配的设定卡片", {"selector": args.selector, "kind": args.kind})
            return {"ok": True, "code": "SETTINGS_QUERY", **result}
        report = check_settings(root)
        if not report["valid"] or (args.strict and report["warnings"]):
            raise NovelError("SETTINGS_INVALID", "设定检查未通过", report)
        return {"ok": True, "code": "SETTINGS_CHECKED", **report}
    if args.action == "overview":
        return {"ok": True, "code": "PROJECT_OVERVIEW", **project_overview(root)}
    if args.action == "prompt-plan":
        try:
            return {"ok": True, "code": "PROMPT_PLAN", **resolve_prompt_plan(root, args.station, decision_type=args.decision_type, chapter_id=args.chapter_id, view=args.view, review_class=args.review_class)}
        except PromptPlanError as exc:
            raise NovelError(exc.code, str(exc), exc.details) from exc
    if args.action == "prepare":
        try:
            from scripts.core.task_prepare import prepare_task
        except ImportError:
            from core.task_prepare import prepare_task
        report = prepare_task(root, args.task, chapter_id=args.chapter_id, pov_character=args.pov_character,
                            entities=args.entity, chapter_goal=args.chapter_goal, review_class=args.review_class, knowledge_mode=args.knowledge_mode,
                            held_prepare_ref=args.held_prepare_ref)
        if args.view == 'model':
            from core.context_transport import task_model_response
            return task_model_response(report)
        return report
    if args.action == "structure":
        catalog_spec = next((s for s in CATALOG_OPERATIONS if s["cli"] == args.structure_action), None)
        if catalog_spec:
            arguments = {key: getattr(args, snake_case(key)) for key in catalog_spec["inputSchema"]["properties"]
                         if key != "projectRoot" and getattr(args, snake_case(key), None) is not None}
            return call_catalog_operation(catalog_spec["name"], root, arguments, actor="cli")
        if args.structure_action == "move-preview":
            return preview_move_chapter(root, chapter_id=args.chapter_id, before_chapter_id=args.before_chapter_id, actor="cli")
        if args.structure_action == "show":
            return {"ok": True, "code": "STRUCTURE_GRAPH", **structure_graph(root)}
        if args.structure_action == "graph":
            graph = entity_graph(root)
            if args.format == "mermaid":
                return _view_result(root, args, "ENTITY_GRAPH_MERMAID", "关系图谱", graph_mermaid(graph))
            _reject_output_without_mermaid(args, "graph")
            return {"ok": True, "code": "ENTITY_GRAPH", **graph}
        if args.structure_action == "insert-preview":
            return preview_insert_chapter(root, title=args.title, volume_id=args.volume_id, primary_arc_id=args.primary_arc_id, before_chapter_id=args.before_chapter_id, secondary_arc_ids=args.secondary_arc_id, actor="cli")
        if args.structure_action == "archive-preview":
            return preview_archive_chapter(root, chapter_id=args.chapter_id, reason=args.reason, actor="cli")
        if args.structure_action == "timespan-preview":
            return preview_set_chapter_timespan(root, chapter_id=args.chapter_id, start_anchor_id=args.start_anchor_id, end_anchor_id=args.end_anchor_id, actor="cli")
        if args.structure_action == "anchor-preview":
            return preview_create_time_anchor(root, name=args.name, absolute_time=args.absolute_time, summary=args.summary, time_ordinal=args.time_ordinal, actor="cli")
        if args.structure_action == "timeline":
            view = story_timeline(root)
            if args.format == "mermaid":
                return _view_result(root, args, "STORY_TIMELINE_MERMAID", "故事时间线", timeline_mermaid(view))
            _reject_output_without_mermaid(args, "timeline")
            return {"ok": True, "code": "STORY_TIMELINE", **view}
        if args.structure_action == "plans":
            return {"ok": True, "code": "PENDING_PLANS", "plans": list_pending_plans(root)}
        if args.structure_action == "operations":
            return {"ok": True, "code": "STRUCTURE_OPERATIONS", "operations": list_operations(root)}
        if args.structure_action == "gc":
            return gc_project(root, keep_backups=args.keep_backups)
        if args.structure_action == "commit":
            return commit_plan(root, operation_id=args.operation_id, plan_hash=args.plan_hash, approved=args.approve, actor="cli")
        if args.structure_action == "undo":
            return undo_operation(root, args.operation_id, actor="cli",
                                  binding_generation=args.binding_generation)
    if args.action == "relocate":
        from core.project_relocate import (ProjectRelocateError, commit_relocate_project,
                                           preview_relocate_project)
        try:
            if args.approve or args.operation_id or args.preview_hash:
                if not (args.operation_id and args.preview_hash):
                    raise ProjectRelocateError('APPROVAL_WITHOUT_PLAN',
                                               '执行需同时给出预览返回的 --operation-id 与 --preview-hash；先跑一次预览')
                return commit_relocate_project(root, operation_id=args.operation_id,
                                               plan_hash=args.preview_hash, approved=args.approve, actor="cli")
            if not args.target:
                raise ProjectRelocateError('PROJECT_RELOCATE_TARGET_INVALID', '预览必须给出 --target 新位置绝对路径')
            return {"ok": True, "code": "PROJECT_RELOCATE_PREVIEW",
                    **preview_relocate_project(root, target=args.target, actor="cli")}
        except ProjectRelocateError as exc:
            raise NovelError(exc.code, str(exc), exc.details) from exc
    if args.action == "entity":
        return {"ok": True, "code": "ENTITY", **get_entity(root, args.selector)}
    if args.action == "prose":
        return {"ok": True, "code": "CHAPTER_PROSE", **chapter_prose(root, args.chapter_id)}
    if args.action == "export":
        result = export_manuscript(
            root,
            statuses=tuple(args.status or ("stable",)),
            fmt=args.format,
            output=Path(args.output) if args.output else None,
            overwrite=args.overwrite,
            expected_file_hash=args.expected_file_hash,
        )
        return {"ok": True, "code": "MANUSCRIPT_EXPORTED", **result}
    if args.action == "context":
        from core.context_builder import build_station_context, bound_station_context
        ctx = build_station_context(
            str(root), args.chapter_id, recent=args.recent,
            task_type=args.task_type, pov_character=args.pov_character, chapter_goal=args.chapter_goal,
            view='station' if args.view == 'model' else args.view, read_only=True, knowledge_mode=args.knowledge_mode,
        )
        if not ctx.get("readiness", {}).get("ready", True):
            raise NovelError("CONTEXT_NOT_READY", "上下文缺少当前视角可用的硬约束，先补充可见性证据", ctx["readiness"])
        if args.view == 'model':
            from core.context_transport import model_response
            return model_response(ctx)
        result = {"ok": True, "code": "CONTINUATION_CONTEXT", **ctx}
        return bound_station_context(result) if args.view == "station" else result
    if args.action == "summary":
        if args.summary_action == "prepare":
            prep = story_summarizer._prepare_context(root, view='model')
            code = "SUMMARY_UP_TO_DATE" if prep.get("upToDate") else "SUMMARY_CONTEXT"
            return {"ok": True, "code": code, **prep}
        return {"ok": True, "code": "SUMMARY_COMMITTED", **story_summarizer._commit_summary(root, args.input)}
    if args.action == "state":
        report = state_cache.rebuild(root) if args.state_action == "rebuild" else state_cache.status(root)
        return {"ok": True, "code": "STATE_CACHE_REBUILT" if args.state_action == "rebuild" else "STATE_CACHE_STATUS", **report}
    if args.action == "workflow":
        if args.workflow_action == "recover":
            return recover_transactions(root, apply=args.apply)
        if args.workflow_action == "chapter-receipt":
            return {"ok": True, "code": "CHAPTER_RECEIPT", **chapter_receipt(root, args.chapter_id, title_candidate_id=args.title_candidate_id, skip_title=args.skip_title, view=args.view)}
        if args.workflow_action == "chapter-status":
            try:
                from scripts.core.chapter_status import chapter_status
            except ImportError:
                from core.chapter_status import chapter_status
            return {"ok": True, "code": "CHAPTER_STATUS", **chapter_status(root, args.chapter_id)}
        if args.workflow_action == "diagnostics-diff":
            try:
                from scripts.core.diagnostics_diff import diff_reports
            except ImportError:
                from core.diagnostics_diff import diff_reports
            current = Path(args.current).expanduser().resolve()
            if args.against:
                against = Path(args.against).expanduser().resolve()
            else:
                workflow_state = _read_workflow(root)
                entry = workflow_state.get("artifacts", {}).get(args.chapter_id, {}).get("diagnostics")
                if not isinstance(entry, dict) or not entry.get("artifactPath"):
                    raise NovelError("DIAGNOSTICS_BASELINE_MISSING",
                                     "no registered diagnostics artifact for this chapter; pass --against",
                                     {"chapterId": args.chapter_id})
                from core.paths import stored_project_path
                against = stored_project_path(root, entry['artifactPath'])
            result = diff_reports(against, current, view=args.view)
            return {"ok": True, "code": "DIAGNOSTICS_DIFF", "chapterId": args.chapter_id,
                    "against": str(against), "current": str(current), **result}
        if args.workflow_action == "state-preview":
            return state_preview(root, args.chapter_id)
        if args.workflow_action in {"continuity-preview", "continuity-complete"}:
            try:
                from scripts.core.continuity import complete_continuity, continuity_preview
            except ImportError:
                from core.continuity import complete_continuity, continuity_preview
            if args.workflow_action == "continuity-preview":
                return continuity_preview(root, args.chapter_id)
            return complete_continuity(root, args.chapter_id, batch_hash=args.batch_hash,
                                       confirmation=_read_observed_json(Path(args.confirmation).expanduser().resolve(), "continuity_confirmation"), approved=args.approve)
        registration_before, registration_guards = {}, []
        if args.workflow_action == 'artifact-register':
            try:
                from scripts.core.write_sets import capture_guards, file_state
            except ImportError:
                from core.write_sets import capture_guards, file_state
            registration_before = {'.govel/workflow.json': file_state(_workflow_path(root))}
            registration_guards = capture_guards(root, ['.govel/structure.json'])
        workflow = _read_workflow(root)
        if args.workflow_action == "show":
            return {"ok": True, "code": "WORKFLOW_STATE", **workflow}
        structure = load_structure(root)
        chapter_ids = {item["chapterId"] for item in structure["chapters"]}
        if args.workflow_action == "approve-chapters":
            unknown = sorted(set(args.chapter_id) - chapter_ids)
            if unknown:
                raise NovelError("CHAPTER_NOT_FOUND", "unknown chapters", {"chapterIds": unknown})
            scope = _approval_scope(structure, args.chapter_id, allow_gaps=args.allow_gaps, reason=args.reason)
            before_scope = list(workflow.get('approvedChapterIds', []))
            workflow["approvedChapterIds"] = list(dict.fromkeys(args.chapter_id))
            workflow["approvalScope"] = scope
            after_scope = workflow['approvedChapterIds']
            return {"ok": True, "code": "WORKFLOW_APPROVAL_UPDATED", "approvalScope": scope,
                    "scopeChange": {"mode": "replace", "before": before_scope, "after": after_scope,
                                    "added": [cid for cid in after_scope if cid not in before_scope],
                                    "removed": [cid for cid in before_scope if cid not in after_scope]},
                    "note": "本次替换全部章节授权范围；未列出的章节不保留在新范围中。",
                    **_write_workflow(root, workflow)}
        if args.workflow_action == "advance":
            try:
                return advance_chapter_status(root, args.chapter_id, args.to, actor="cli")
            except (WorkflowError, StructureError, ValueError, OSError) as exc:
                raise NovelError(getattr(exc, "code", "CHAPTER_ADVANCE_FAILED"), str(exc), getattr(exc, "details", {})) from exc
        if args.workflow_action == "recover":
            try:
                return recover_transactions(root, apply=args.apply)
            except (WorkflowError, StructureError, ValueError, OSError) as exc:
                raise NovelError(getattr(exc, "code", "TRANSACTION_RECOVER_FAILED"), str(exc), getattr(exc, "details", {})) from exc
        if args.workflow_action == "repair-round":
            try:
                from scripts.core.workflow import increment_repair_round
            except ImportError:
                from core.workflow import increment_repair_round
            try:
                round_number = increment_repair_round(workflow, args.chapter_id, maximum=repair_policy()["maxAutomaticRounds"])
            except (WorkflowError, ValueError) as exc:
                raise NovelError(getattr(exc, "code", "REPAIR_ROUND_FAILED"), str(exc), getattr(exc, "details", {})) from exc
            return {"ok": True, "code": "REPAIR_ROUND_RECORDED", "chapterId": args.chapter_id, "round": round_number, **_write_workflow(root, workflow)}
        if args.workflow_action == "artifact-register":
            bindings = []
            for item in getattr(args, "bind", None) or []:
                field, separator, raw_path = item.partition("=")
                field = field.strip()
                if not field or (separator and not raw_path):
                    raise NovelError("BIND_ARGUMENT_INVALID",
                                     "--bind expects FIELD or FIELD=PATH",
                                     {"given": item})
                bindings.append((field, raw_path.strip() or None))
            try:
                artifact_path = Path(args.artifact).expanduser().resolve()
                artifact = _read_observed_json(artifact_path, "registered_artifact")
                provenance = {}
                review_guards = (list(registration_guards) if artifact.get('artifactType') == 'stage_b_review'
                                 and artifact.get('payload', {}).get('cognitionReviews') else [])
                if bindings:
                    artifact, provenance = bind_fields(
                        root, structure=structure, workflow=workflow, chapter_id=args.chapter_id,
                        artifact=artifact, bindings=bindings,
                    )
                try:
                    from scripts.core.workflow import artifact_replacement
                except ImportError:
                    from core.workflow import artifact_replacement
                replacement = artifact_replacement(workflow, args.chapter_id, artifact)
                record_artifact(
                    workflow,
                    args.chapter_id,
                    artifact,
                    structure=structure,
                    artifact_path=str(artifact_path),
                    project_id=structure["projectId"],
                    project_root=root,
                    _read_guards=review_guards,
                )
                if provenance:
                    workflow["artifacts"][args.chapter_id][artifact["artifactType"]]["bindings"] = provenance
            except (OSError, json.JSONDecodeError, WorkflowError, ArtifactBindError, ValueError) as exc:
                raise NovelError(getattr(exc, "code", "ARTIFACT_INVALID"), str(exc), getattr(exc, "details", {})) from exc
            if bindings:
                try:
                    from scripts.core.write_sets import apply_write_set, digest, inventory, json_bytes
                except ImportError:
                    from core.write_sets import apply_write_set, digest, inventory, json_bytes
                artifact_bytes = json_bytes(artifact)
                entry = workflow["artifacts"][args.chapter_id][artifact["artifactType"]]
                entry["artifactFileSha256"] = digest(artifact_bytes)
                workflow = normalize_workflow(workflow)
                workflow["updatedAt"] = utc_now()
                with project_lock_reentrant(root):
                    outputs = {
                        str(artifact_path.relative_to(root)): artifact_bytes,
                        str(_workflow_path(root).relative_to(root)): json_bytes(workflow),
                    }
                    expected = inventory(root, outputs)
                    for row in expected:
                        if review_guards and row['path'] in registration_before:
                            row['before'] = registration_before[row['path']]
                    apply_write_set(root, outputs, operation="artifact_register_bind", guards=review_guards,
                                    expected=expected)
            elif review_guards:
                try:
                    from scripts.core.write_sets import apply_write_set, inventory, json_bytes
                except ImportError:
                    from core.write_sets import apply_write_set, inventory, json_bytes
                workflow = normalize_workflow(workflow)
                workflow['updatedAt'] = utc_now()
                with project_lock_reentrant(root):
                    outputs = {
                        str(_workflow_path(root).relative_to(root)): json_bytes(workflow),
                    }
                    expected = inventory(root, outputs)
                    for row in expected:
                        if row['path'] in registration_before:
                            row['before'] = registration_before[row['path']]
                    apply_write_set(root, outputs, operation='artifact_register', guards=review_guards,
                                    expected=expected)
            else:
                workflow = _write_workflow(root, workflow)
            result = {"ok": True, "code": "ARTIFACT_REGISTERED", **workflow}
            if replacement is not None:
                result['replacement'] = replacement
            if provenance:
                result["bindings"] = provenance
            return result
        if args.workflow_action == "reconcile":
            try:
                return reconcile_workflow(root, persist=args.persist)
            except (WorkflowError, StructureError, ValueError) as exc:
                raise NovelError(getattr(exc, "code", "WORKFLOW_RECONCILE_FAILED"), str(exc), getattr(exc, "details", {})) from exc
        if args.workflow_action == "finalize-chapter":
            if bool(args.title_candidate_id) == bool(args.skip_title):
                raise NovelError(
                    "TITLE_DECISION_REQUIRED",
                    "定稿标题必须二选一：--title-candidate-id <id> 或 --skip-title",
                )
            try:
                return finalize_chapter(
                    root,
                    args.chapter_id,
                    title_candidate_id=args.title_candidate_id,
                    skip_title=args.skip_title,
                    binding_generation=args.binding_generation,
                )
            except (WorkflowError, StructureError, ValueError, OSError) as exc:
                raise NovelError(getattr(exc, "code", "CHAPTER_FINALIZE_FAILED"), str(exc), getattr(exc, "details", {})) from exc
        if args.workflow_action == "accept-external":
            # Both transports request the same native confirmation. A bare
            # --approve remains insufficient for the protected core commit.
            try:
                if args.operation_id:
                    if not args.approve or not args.plan_hash:
                        raise NovelError(
                            "APPROVAL_REQUIRED",
                            "执行接受外部修改必须同时给出 --operation-id、--plan-hash 与 --approve；"
                            "注意这三者本身都不构成 authority（见 APPROVAL_CAPABILITY_REQUIRED）")
                    if getattr(args, 'confirm_on_device', False):
                        from importlib import import_module
                        module = import_module('scripts.core.external_edit_operations' if __package__ else 'core.external_edit_operations')
                        return module.commit_external_edit(root, operationId=args.operation_id,
                            planHash=args.plan_hash, approve=True, confirmOnDevice=True)
                    return commit_refactor(root, operation_id=args.operation_id,
                                           plan_hash=args.plan_hash, approved=True, actor="human_ui")
                return preview_accept_external_modification(root, chapter_id=args.chapter_id, request_id=args.request_id)
            except (WorkflowError, StructureError, ValueError, OSError) as exc:
                raise NovelError(getattr(exc, "code", "EXTERNAL_MODIFICATION_FAILED"), str(exc),
                                 getattr(exc, "details", {})) from exc
        if args.workflow_action == "artifact-approve":
            hashes = {}
            for item in args.artifact_hash:
                name, separator, digest = item.partition("=")
                if not separator or not name or not digest:
                    raise NovelError("ARTIFACT_HASH_INVALID", "--artifact-hash must use type=sha256")
                hashes[name] = digest
            for value in args.artifact:
                raw = value[1:] if value.startswith("@") else value
                payload = _read_json_payload_file(root, raw, "ARTIFACT")
                try:
                    validate_envelope(payload)
                except ArtifactError as exc:
                    raise NovelError(exc.code, str(exc), exc.details) from exc
                name, digest = payload["artifactType"], payload["artifactSha256"]
                if payload.get("chapterId") not in (None, args.chapter_id):
                    raise NovelError("ARTIFACT_CHAPTER_MISMATCH",
                                     "approve file belongs to a different chapter",
                                     {"expected": args.chapter_id, "actual": payload.get("chapterId")})
                if hashes.get(name, digest) != digest:
                    raise NovelError("ARTIFACT_HASH_CONFLICT",
                                     "the same artifact type was given two different hashes",
                                     {"artifactType": name})
                hashes[name] = digest
            if not hashes:
                raise NovelError("ARTIFACT_HASH_INVALID",
                                 "give --artifact-hash type=sha256 or --artifact @file (at least one)")
            try:
                extreme = intensity_registry.extreme_level()
                approve_artifacts(
                    workflow, args.chapter_id, artifact_hashes=hashes,
                    approval_mode=read_json(root / ".govel" / "project.json").get("approvalMode", "manual"),
                    intensity_level=args.intensity_level, extreme_level=extreme,
                    artifact_paths={key: row["artifactPath"] for key, row in workflow.get("artifacts", {}).get(args.chapter_id, {}).items() if row.get("artifactPath")},
                    project_root=root,
                )
            except (WorkflowError, OSError, json.JSONDecodeError, ValueError) as exc:
                raise NovelError(getattr(exc, "code", "ARTIFACT_APPROVAL_FAILED"), str(exc), getattr(exc, "details", {})) from exc
            return {"ok": True, "code": "ARTIFACTS_APPROVED", **_write_workflow(root, workflow)}
        try:
            set_next_action(
                workflow,
                structure,
                root=root,
                chapter_id=args.chapter_id,
                action=args.next_action,
                reason=args.reason,
                needs_approval=args.needs_approval,
                chapter_range_required=serial_planning_policy()["chapterRangeRequired"],
                current_volume_only=serial_planning_policy()["currentVolumeOnly"],
                prewarm_next_chapter=serial_planning_policy()["prewarmNextChapter"],
            )
        except WorkflowError as exc:
            raise NovelError(exc.code, str(exc), exc.details) from exc
        return {"ok": True, "code": "WORKFLOW_NEXT_ACTION_UPDATED", **_write_workflow(root, workflow)}
    raise NovelError("INVALID_REQUEST", f"unknown project action: {args.action}")


class MachineArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        options = set()
        def collect(parser):
            options.update(parser._option_string_actions)
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for child in action.choices.values(): collect(child)
        collect(self)
        suggestions = {}
        for flag in re.findall(r'--[A-Za-z][A-Za-z0-9-]*', message):
            converted = '--' + snake_case(flag[2:]).replace('_', '-')
            if converted != flag and converted in options:
                suggestions[flag] = converted
        self.print_usage(sys.stderr)
        _emit({'ok': False, 'code': 'ARGUMENTS_INVALID', 'error': message,
               'details': {'command': self.prog, 'flagSuggestions': suggestions,
                           'guidance': '动作 arguments 是 JSON 参数；CLI 使用 --project-root 等旗标。用 project tools help --operation <operation> --arguments <JSON> 生成命令。'}}, ensure_ascii=False)
        self.exit(2)


def build_parser() -> argparse.ArgumentParser:
    guide = ('命令帮助：project tools help；按意图查找：project tools help --query "续写这个"；'
             '查看具体参数：project task prepare --help。查询、准备、修改和提交均走公开命令；帮助不执行操作。')
    parser = MachineArgumentParser(prog="novelctl", description="Govel 当前格式项目工具箱", epilog=guide)
    groups = parser.add_subparsers(dest="group", required=True)
    project = groups.add_parser("project", epilog=guide)
    sub = project.add_subparsers(dest="action", required=True)
    for name in ("quick-init", "init"):
        cmd = sub.add_parser(name, description="在空目录新建小说项目；不迁移或覆盖已有项目")
        cmd.set_defaults(operation_examples=({"bookName": "新书"},))
        cmd.add_argument("--project-root", default=".")
        cmd.add_argument("--project-id")
        cmd.add_argument("--book-name", required=True)
        cmd.add_argument("--approval-mode", default=None, choices=["manual", "auto"])
        cmd.add_argument("--idempotency-key")
        if name == "quick-init":
            cmd.add_argument("--no-agents-md", action="store_true")
    imp = sub.add_parser("import", help="AI-assisted import into a new strict project")
    imp_sub = imp.add_subparsers(dest="import_action", required=True)
    scan = imp_sub.add_parser("scan")
    scan.add_argument("--source", required=True)
    scan.add_argument("--output", required=True)
    review = imp_sub.add_parser("review", help="建立只读外部稿件证据副本，不创建可写真源")
    review.add_argument("--source", required=True)
    review.add_argument("--target-root", required=True)
    create = imp_sub.add_parser("create")
    create.add_argument("--project-root", required=True)
    create.add_argument("--candidate", required=True)
    create.add_argument("--approval-mode", default=None, choices=["manual", "auto"])
    validate = imp_sub.add_parser("validate", help="校验 candidate JSON，不落盘")
    validate.add_argument("--project-root", default=".")
    validate.add_argument("--candidate", required=True)
    relocate = sub.add_parser("relocate", help="把项目文件夹改名或移动到同一文件系统内的新位置，CLI/MCP 共用预览和批准入口")
    relocate.set_defaults(operation_examples=({'projectRoot': '/path/to/书', 'target': '/path/to/新位置'},))
    relocate.add_argument("--project-root", default=".")
    relocate.add_argument("--target", help="预览用：新的项目文件夹绝对路径，必须尚不存在且与当前文件夹同一文件系统。执行时用计划里已批准的 target，不再收参数")
    relocate.add_argument("--operation-id", help="执行用：预览返回的 operationId；缺它就没有可绑定的批准")
    relocate.add_argument("--preview-hash", help="执行用：预览返回的 planHash；计划过期或被换掉一律拒绝")
    relocate.add_argument("--approve", action="store_true", help="显式批准执行；--approve 必须与 --operation-id 和 --preview-hash 同时给出")
    settings = sub.add_parser("settings", help="设定卡片规范、只读检查与非覆盖补齐")
    settings_sub = settings.add_subparsers(dest="settings_action", required=True)
    settings_check = settings_sub.add_parser("check")
    settings_check.add_argument("--project-root", default=".")
    settings_check.add_argument("--strict", action="store_true", help="警告也使检查失败；空模板不等于已完成设定")
    settings_show = settings_sub.add_parser(
        "show", help="字段级只读查询：按稳定 ID／名称／别名查看卡片字段",
        description="查看人物、地点等设定字段；整份阅读快照用 project source export。")
    settings_show.set_defaults(operation_examples=({'selector': '人物名称'}, {'kind': 'character'}))
    settings_show.add_argument("selector", nargs="?", default=None, help="稳定 ID、名称或别名；省略则列出全部")
    settings_show.add_argument("--project-root", default=".")
    settings_show.add_argument("--kind", default=None, choices=list(settings_schema()["kinds"]), help="只查某个设定分类")
    settings_show.add_argument("--field", action="append", default=[], help="只返回这些字段，可重复")
    settings_show.add_argument("--strict", action="store_true", help="没有匹配卡片时以非零退出")
    overview = sub.add_parser("overview")
    overview.description = "查询项目概览、章节状态和待办；不需要直接读取内部结构文件。"
    overview.set_defaults(operation_examples=({},))
    overview.add_argument("--project-root", default=".")
    prompt_plan = sub.add_parser("prompt-plan", help="解析工位实际契约装载计划")
    prompt_plan.add_argument("--project-root", default=".")
    prompt_plan.add_argument("--station", required=True, choices=[
        "govel-composer", "govel-router", "govel-planner",
        "govel-writer", "govel-editor", "govel-titler",
    ])
    prompt_plan.add_argument("--decision-type", choices=["design", "arc", "chapter", "title", "stage_b"])
    prompt_plan.add_argument("--chapter-id", help="为章节条件装载当前章的关联弧线标题与标签")
    prompt_plan.add_argument("--view", choices=["station", "full"], default="full", help="station 返回源片段与按需入口；full 保留完整审计清单")
    prompt_plan.add_argument("--review-class", choices=["ordinary", "heavy"], help="阶段 A 的章节类型声明；未知类型保守使用全席")
    prepare = sub.add_parser("prepare", help="只读任务准备清单：必读材料、就绪判断、缺项与下一步")
    prepare.add_argument("--project-root", default=".")
    try:
        from scripts.core.project import user_workflow_policy
        from scripts.core.task_prepare import TASK_STATIONS
    except ImportError:
        from core.project import user_workflow_policy
        from core.task_prepare import TASK_STATIONS
    prepare.add_argument("--task", required=True, choices=[*user_workflow_policy()["stages"], "continue", *TASK_STATIONS],
                         help="设计 design / 写章 write / 修改 revise；continue 消费机器下一步；旧任务兼容")
    prepare.add_argument("--chapter-id")
    prepare.add_argument("--pov-character", default=None)
    prepare.add_argument("--entity", action="append", default=[], help="显式指定本章相关实体（稳定 ID、名称或别名）")
    prepare.add_argument("--chapter-goal", default="")
    try:
        from scripts.core.project import knowledge_modes
    except ImportError:
        from core.project import knowledge_modes
    prepare.add_argument("--knowledge-mode", choices=knowledge_modes())
    prepare.add_argument("--review-class", choices=["ordinary", "heavy"])
    prepare.add_argument("--view", choices=["station", "model"], default="station", help="station 兼容机器清单；model 工位语义清单")
    prepare.add_argument("--held-prepare-ref", help="仅在仍持有该准备报告时回传；model 视图交付变化部分，换窗或压缩后不传")
    structure = sub.add_parser("structure")
    structure_sub = structure.add_subparsers(dest="structure_action", required=True)
    for spec in CATALOG_OPERATIONS:
        cmd = structure_sub.add_parser(spec["cli"], help=spec["description"])
        if spec['cli'] in {'book-rename-preview', 'chapter-rename-preview', 'entity-create-preview'}:
            cmd.set_defaults(operation_examples=({
                'book-rename-preview': {'newName': '山城旧事'},
                'chapter-rename-preview': {'chapterId': 'CH001', 'newTitle': '交班'},
                'entity-create-preview': {'entityType': 'character', 'name': '林舟'},
            }[spec['cli']],))
        cmd.add_argument("--project-root", default=".")
        schema = spec["inputSchema"]
        for key, field in schema["properties"].items():
            if key == "projectRoot":
                continue
            options = {"required": key in schema["required"]}
            if isinstance(field["type"], list):
                options["type"] = json.loads if "object" in field["type"] else str
            elif field["type"] == "array":
                options["nargs"] = "+"
            elif field["type"] == "object":
                options["type"] = json.loads
            if "enum" in field:
                options["choices"] = field["enum"]
            if field["type"] == "boolean":
                # A required boolean never gets a silent default: the author must
                # say --x or --no-x, because the framework does not guess intent.
                flag = "--" + snake_case(key).replace("_", "-")
                if key in schema["required"]:
                    group = cmd.add_mutually_exclusive_group(required=True)
                    group.add_argument(flag, dest=snake_case(key), action="store_true")
                    group.add_argument("--no-" + flag.lstrip("-"), dest=snake_case(key), action="store_false")
                else:
                    cmd.add_argument(flag, action="store_true", default=False)
                continue
            cmd.add_argument("--" + snake_case(key).replace("_", "-"), **options)
    move = structure_sub.add_parser(
        "move-preview", help="预览章节排序调整",
        description="预览把一章移到另一章之前（省略则移到末尾）；只改阅读顺序与显示编号，chapterId 与正文不变。多章一起调整用 structure batch-preview。")
    move.set_defaults(operation_examples=({'chapterId': 'CH003', 'beforeChapterId': 'CH001'},))
    move.add_argument("--project-root", default=".")
    move.add_argument("--chapter-id", required=True, help="要移动的章：稳定 ID 或显示编号 CH001")
    move.add_argument("--before-chapter-id", help="移到这一章之前：稳定 ID 或显示编号；省略则移到末尾")
    show = structure_sub.add_parser("show")
    show.add_argument("--project-root", default=".")
    graph_cmd = structure_sub.add_parser("graph", help="实体关系图谱：节点为实体，边为 relations；--format mermaid 输出分组图")
    graph_cmd.add_argument("--project-root", default=".")
    graph_cmd.add_argument("--format", choices=["json", "mermaid"], default="json")
    graph_cmd.add_argument("--output", default=None, help="把 mermaid 视图写入项目内文件（带 GENERATED 标记；不带标记的既有文件拒写）")
    preview = structure_sub.add_parser("insert-preview", description="预览新建章节，明确卷与主弧后提交；不推断下一章是否已获批准。")
    preview.set_defaults(operation_examples=({'title': '工作标题', 'volumeId': 'VOL-EXAMPLE', 'primaryArcId': 'ARC-EXAMPLE'},))
    preview.add_argument("--project-root", default=".")
    preview.add_argument("--title", required=True)
    preview.add_argument("--volume-id", required=True)
    preview.add_argument("--primary-arc-id", required=True)
    preview.add_argument("--before-chapter-id")
    preview.add_argument("--secondary-arc-id", action="append", default=[])
    archive = structure_sub.add_parser("archive-preview", help="预览归档章节（重写场景）")
    archive.add_argument("--project-root", default=".")
    archive.add_argument("--chapter-id", required=True)
    archive.add_argument("--reason", default="")
    timeline_cmd = structure_sub.add_parser(
        "timeline", help="故事时间线视图（锚点、跨度、倒挂检测）",
        description="按时间锚点排列章节跨度并报告 TIME_ORDER_INVERSION 等时间顺序问题；这是顺序检查，不判断设定语义是否一致。")
    timeline_cmd.set_defaults(operation_examples=({'format': 'json'},))
    timeline_cmd.add_argument("--project-root", default=".")
    timeline_cmd.add_argument("--format", choices=["json", "mermaid"], default="json", help="mermaid 输出 timeline 图，时段标签取锚点与其 timeOrdinal")
    timeline_cmd.add_argument("--output", default=None, help="把 mermaid 视图写入项目内文件（带 GENERATED 标记；不带标记的既有文件拒写）")
    timespan = structure_sub.add_parser("timespan-preview", help="预览设置章节故事时间跨度")
    timespan.add_argument("--project-root", default=".")
    timespan.add_argument("--chapter-id", required=True)
    timespan.add_argument("--start-anchor-id")
    timespan.add_argument("--end-anchor-id")
    anchor = structure_sub.add_parser("anchor-preview", help="预览新建时间锚点（绝对时间+别称）")
    anchor.add_argument("--project-root", default=".")
    anchor.add_argument("--name", required=True)
    anchor.add_argument("--absolute-time", required=True)
    anchor.add_argument("--summary", default="")
    anchor.add_argument("--time-ordinal", type=json.loads, default=None, help="数值排序键，给相对时间轴用（D-45 → -45、D-Day → 0）")
    plans = structure_sub.add_parser("plans", help="列出未提交的预览计划")
    plans.add_argument("--project-root", default=".")
    operations = structure_sub.add_parser("operations", help="操作审计日志（含可撤销标记）")
    operations.description = "读取修改历史、可撤销操作与必要绑定；撤销前先核对目标，不从日志文件手工回滚。"
    operations.set_defaults(operation_examples=({},))
    operations.add_argument("--project-root", default=".")
    gc = structure_sub.add_parser("gc", help="清理过期计划与多余备份")
    gc.add_argument("--project-root", default=".")
    gc.add_argument("--keep-backups", type=int, default=None,
                    help="保留的回滚备份份数（默认读注册表 maintenance.structureGcKeepBackups）")
    commit = structure_sub.add_parser("commit")
    commit.add_argument("--project-root", default=".")
    commit.add_argument("--operation-id", required=True)
    commit.add_argument("--plan-hash", required=True)
    commit.add_argument("--approve", action="store_true")
    undo = structure_sub.add_parser("undo")
    undo.add_argument("--project-root", default=".")
    undo.add_argument("--operation-id", required=True)
    undo.add_argument("--binding-generation", type=int, default=None,
                      help="回显被撤销章当前 binding 的 generation（从 project structure operations 的 "
                           "bindingGeneration 或 workflow chapter-receipt 读取）。撤销真实 binding 变更时必填："
                           "缺失一律 OWNERSHIP_GENERATION_REQUIRED，陈旧一律 OWNERSHIP_GENERATION_CONFLICT，"
                           "命令行不会代你查当前代次")
    entity = sub.add_parser("entity")
    entity.add_argument("--project-root", default=".")
    entity.add_argument("--selector", "--entity-id", dest="selector", required=True, help="稳定实体 ID、名称或唯一别名；--entity-id 是同一选择器的别名")
    prose = sub.add_parser("prose", help="只读读取章节正文")
    prose.description = "按章节 ID 或显示编号读取当前正文；使用登记的正文位置，无须打开内部结构文件找路径。"
    prose.set_defaults(operation_examples=({'chapterId': 'CH001'},))
    prose.add_argument("--project-root", default=".")
    prose.add_argument("--chapter-id", required=True)
    export = sub.add_parser(
        "export", help="按章节顺序写出成品正文文件，不修改作品真源",
        description="核验正文归属后按章节顺序导出，默认只取 stable；缺章时明确标记部分导出。设定阅读文件用 project source export。")
    export.set_defaults(operation_examples=({'format': 'md', 'output': '工作台/成品正文.md'},))
    export.add_argument("--project-root", default=".", help="项目根目录，默认当前目录")
    export.add_argument("--format", default="txt", choices=["txt", "md"], help="成品正文格式，默认 txt")
    export.add_argument(
        "--output",
        help=("目标文件路径。相对路径按 --project-root 解析，项目内只允许 工作台/ 下；"
              "导出到项目外用绝对路径。默认 工作台/<书名>-成品.<格式>"),
    )
    export.add_argument(
        "--status", action="append", choices=list(EXPORT_STATUSES),
        help="要导出的章节状态，可重复；默认只导出 stable",
    )
    export.add_argument("--overwrite", action="store_true", help="覆盖已有不同内容，须同时绑定已检查目标的 --expected-file-hash")
    export.add_argument("--expected-file-hash", help="已检查导出目标的 SHA-256；目标变化时拒绝覆盖")
    context = sub.add_parser("context", help="按章节 ID 构建续写上下文")
    context.add_argument("--project-root", default=".")
    context.add_argument("--chapter-id", required=True)
    context.add_argument("--recent", type=int, default=2)
    context.add_argument("--task-type", default="planner", choices=["planner", "writer", "editor", "query"])
    context.add_argument("--pov-character", default=None)
    context.add_argument("--chapter-goal", default="")
    context.add_argument("--knowledge-mode", choices=knowledge_modes())
    context.add_argument("--view", default="full", choices=["full", "station", "model"], help="full 完整上下文；station 兼容机器包；model 工位语义视图")
    summary = sub.add_parser("summary", help="故事摘要 prepare/commit")
    summary_sub = summary.add_subparsers(dest="summary_action", required=True)
    summary_prepare = summary_sub.add_parser("prepare")
    summary_prepare.add_argument("--project-root", default=".")
    summary_commit = summary_sub.add_parser("commit")
    summary_commit.add_argument("--project-root", default=".")
    summary_commit.add_argument("--input", required=True)
    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_action", required=True)
    for name in ("status", "rebuild"):
        cmd = state_sub.add_parser(name)
        cmd.add_argument("--project-root", default=".")
    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_action", required=True)
    workflow_show = workflow_sub.add_parser("show")
    workflow_show.add_argument("--project-root", default=".")
    receipt = workflow_sub.add_parser("chapter-receipt", help="只读汇总章节工件、批准、阻塞和下一步")
    receipt.set_defaults(operation_examples=({'chapterId': 'CH001', 'skipTitle': True, 'view': 'model'},))
    receipt.add_argument("--project-root", default=".")
    receipt.add_argument("--chapter-id")
    receipt.add_argument("--view", choices=("model", "full"), default="model",
                         help="model 保留全部阻塞与工件摘要；full 包含正文和完整工件 payload")
    receipt_title = receipt.add_mutually_exclusive_group()
    receipt_title.add_argument("--title-candidate-id", help="按该标题选择预检，与 finalize-chapter 一致")
    receipt_title.add_argument("--skip-title", action="store_true", help="预检保留工作标题的入库方案")
    state_preview_cmd = workflow_sub.add_parser("state-preview", help="只读预览已登记状态候选的前后变化")
    state_preview_cmd.add_argument("--project-root", default=".")
    state_preview_cmd.add_argument("--chapter-id", required=True)
    for action in ("continuity-preview", "continuity-complete"):
        cmd = workflow_sub.add_parser(action, help="连续性收尾预检或登记作者已确认的完成证据")
        cmd.add_argument("--project-root", default=".")
        cmd.add_argument("--chapter-id", required=True)
        if action == "continuity-complete":
            cmd.add_argument("--batch-hash", required=True)
            cmd.add_argument("--confirmation", required=True, help="逐项处置与文档 hash 的确认 JSON")
            cmd.add_argument("--approve", action="store_true", help="作者已明确批准本批次；auto 模式不豁免")
    template = workflow_sub.add_parser("artifact-template", help="生成一个可填写的工件 envelope 骨架")
    template.add_argument("--type", "--artifact-type", dest="type", required=True, choices=list(ARTIFACT_TYPES))
    template.add_argument("--project-id")
    template.add_argument("--chapter-id")
    template.add_argument("--output")
    approve = workflow_sub.add_parser("approve-chapters", help="替换全部章节授权范围（不是追加）")
    approve.add_argument("--project-root", default=".")
    approve.add_argument("--chapter-id", action="append", required=True)
    approve.add_argument("--allow-gaps", action="store_true", help="显式允许非连续或跨卷批准，必须同时给 --reason")
    approve.add_argument("--reason", default="", help="离散批准的理由，写入 approvalScope")
    advance = workflow_sub.add_parser("advance", help="推进章节生命周期状态（review → stable 走 finalize-chapter）")
    advance.add_argument("--project-root", default=".")
    advance.add_argument("--chapter-id", required=True)
    advance.add_argument("--to", required=True, choices=list(ADVANCEABLE_STATUSES))
    recover = workflow_sub.add_parser("recover", help="检测或回滚被中断的入库事务")
    recover.add_argument("--project-root", default=".")
    recover.add_argument("--apply", action="store_true", help="执行回滚；缺省只报告")
    artifact_register = workflow_sub.add_parser("artifact-register", help="登记带 hash 的审校或候选工件")
    artifact_register.add_argument("--project-root", default=".")
    artifact_register.add_argument("--chapter-id", required=True)
    artifact_register.add_argument("--artifact", required=True)
    artifact_register.add_argument("--bind", action="append", default=[], metavar="FIELD[=PATH]",
                                   help="由工具计算绑定 hash 注入 payload 字段（可重复）；不带路径按当前真源，带路径额外核对文件内容与真源一致")
    reconcile = workflow_sub.add_parser("reconcile", help="根据结构状态重建唯一下一步")
    reconcile.add_argument("--project-root", default=".")
    reconcile.add_argument("--persist", action="store_true")
    finalize = workflow_sub.add_parser("finalize-chapter", help="批准工件后原子入库章节")
    finalize.add_argument("--project-root", default=".")
    finalize.add_argument("--chapter-id", required=True)
    finalize.add_argument("--title-candidate-id", help="作者选定的 Titler 候选 ID（来自已批准 title_candidates）")
    finalize.add_argument("--skip-title", action="store_true", help="保留工作标题，入库报告标注未定稿")
    finalize.add_argument("--binding-generation", type=int,
                          help="回显 workflow chapter-receipt 顶层 bindingGeneration（从 workflow chapter-receipt 读取）；"
                               "schema 4/5 必填，缺省或过期一律拒绝")
    repair_round = workflow_sub.add_parser("repair-round", help="记录一次自动修复轮次并执行上限")
    repair_round.add_argument("--project-root", default=".")
    repair_round.add_argument("--chapter-id", required=True)
    accept_external = workflow_sub.add_parser(
        "accept-external",
        help="显式接受正文字节的外部修改：预览返回完整 ownership token；提交需要独立特权批准通道"
             "签发的 capability，本构建没有该通道，因此提交一律 fail closed（APPROVAL_CAPABILITY_REQUIRED）")
    accept_external.add_argument("--project-root", default=".")
    accept_external.add_argument("--chapter-id", required=True)
    accept_external.add_argument("--request-id", help="冻结本次外改材料；相同请求只恢复原输入")
    accept_external.add_argument("--operation-id", help="执行用：预览返回的 operationId")
    accept_external.add_argument("--plan-hash", help="执行用：预览返回的 planHash")
    accept_external.add_argument("--approve", action="store_true",
                                 help="显式批准执行；不构成 authority：普通调用参数，无法替代"
                                      "独立特权批准通道的 approval capability")
    accept_external.add_argument("--confirm-on-device", action="store_true",
                                 help="在本机显示 macOS 作者确认；确认后才签发一次性采用能力")
    artifact_approve = workflow_sub.add_parser("artifact-approve", help="按 hash 批准已登记工件")
    artifact_approve.add_argument("--project-root", default=".")
    artifact_approve.add_argument("--chapter-id", required=True)
    artifact_approve.add_argument("--artifact-hash", action="append", default=[],
                                  help="type=sha256 显式批准（兼容入口，可重复）")
    artifact_approve.add_argument("--artifact", action="append", default=[], metavar="@FILE",
                                  help="按工件文件批准：读信封自带的类型与 hash，仍走注册条目核对（可重复）")
    artifact_approve.add_argument("--intensity-level", type=int)
    chapter_status_cmd = workflow_sub.add_parser(
        "chapter-status", help="只读章节看板：阶段、工件登记/批准/新鲜、阻塞与下一步（实时重算）",
        description="只读查看一章的稳定 ID、显示编号、标题、阶段、工件与下一步；可直接传显示编号 CH001。")
    chapter_status_cmd.set_defaults(operation_examples=({'chapterId': 'CH001'},))
    chapter_status_cmd.add_argument("--project-root", default=".")
    chapter_status_cmd.add_argument("--chapter-id", required=True, help="稳定 ID 或显示编号 CH001")
    chapter_status_cmd.add_argument("--response-view", dest="responseView", choices=("model", "full"),
                                   default=argparse.SUPPRESS,
                                   help="返回呈现方式；model 为默认，full 保留完整详情，不改变授权")
    diagnostics_diff_cmd = workflow_sub.add_parser("diagnostics-diff", help="返修增量：按稳定身份对比当前机械报告与上一轮，只呈现 fixed/persisting/new/mixed/unmatched")
    diagnostics_diff_cmd.add_argument("--view", choices=("full", "model"), default="model",
                                      help="model 压缩未变化问题，保留当前位置与待处理数量；full 保留完整审计信息")
    diagnostics_diff_cmd.add_argument("--project-root", default=".")
    diagnostics_diff_cmd.add_argument("--chapter-id", required=True)
    diagnostics_diff_cmd.add_argument("--current", required=True, help="本轮 aggregate_diagnostics 报告 JSON 路径")
    diagnostics_diff_cmd.add_argument("--against", help="上一轮报告路径；缺省用该章已注册的 diagnostics 工件")
    next_cmd = workflow_sub.add_parser("set-next")
    next_cmd.add_argument("--project-root", default=".")
    next_cmd.add_argument("--chapter-id", required=True)
    next_cmd.add_argument("--next-action", required=True)
    next_cmd.add_argument("--reason", default="")
    next_cmd.add_argument("--needs-approval", action="store_true")
    try:
        from scripts.core.operations import new_operations, add_arguments
    except ImportError:
        from core.operations import new_operations, add_arguments
    # doctor 的 schema 单一真源是 project.doctor 这个 OperationSpec：CLI 的
    # --project-root/--strict/--chapter-id、MCP 的 inputSchema 与 HTTP 的查询参数
    # 都由它生成，不再各写一份。
    doctor = sub.add_parser(
        "doctor",
        help="分域聚合项目健康（project/migration/planning/writer/canon/context/knowledge/workflow/tooling）")
    add_arguments(doctor, next(spec for spec in new_operations() if spec.id == "project.doctor"))
    new_groups = {"structure": structure_sub, "workflow": workflow_sub, "import": imp_sub}
    for spec in new_operations():
        if spec.id == "project.doctor":
            continue  # doctor 已单独挂在 project 下；它的 cli 只有一段，不是 group/leaf
        if len(spec.cli) == 1:
            add_arguments(sub.add_parser(spec.cli[0], help=spec.description), spec)
            continue
        group, leaf = spec.cli
        if group not in new_groups:
            new_groups[group] = sub.add_parser(group).add_subparsers(dest=group + "_action", required=True)
        add_arguments(new_groups[group].add_parser(leaf, help=spec.description), spec)
    return parser


def _main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except (ValueError, OSError) as exc:
        # Definition loading can fail before a command has an operation or
        # project scope. Keep the same machine-readable failure boundary.
        payload = {'ok': False, 'code': getattr(exc, 'code', 'COMMAND_FAILED'),
                   'error': str(exc), 'details': getattr(exc, 'details', {})}
        observe_payload('businessResult', payload)
        _emit(payload, ensure_ascii=False, indent=2)
        return 1
    observe_arguments(vars(args))
    operation, selected_arguments = presentation_target(cli_operation(vars(args)), vars(args))
    presentation = None
    root = Path(getattr(args, 'projectRoot', getattr(args, 'project_root', '.')) or '.')
    try:
        presentation = requested_response_view(operation, selected_arguments)
        if cli_operation(vars(args)) == 'diagnostics.aggregate':
            # The facade keeps its independent view semantics. This transport
            # obtains one complete business result before choosing its display.
            args.view = 'full'
        result = _dispatch(args)
    except (NovelError, StructureError, state_cache.StateCacheError, ValueError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, NovelError):
            payload = exc.as_dict()
        elif isinstance(exc, StructureError):
            payload = {"ok": False, "code": exc.code, "error": str(exc), "details": exc.details}
        else:
            payload = {"ok": False, "code": getattr(exc, "code", "COMMAND_FAILED"), "error": str(exc), "details": getattr(exc, "details", {})}
        observe_payload("businessResult", payload)
        displayed = project_response(root, operation, payload, view=presentation)
        _emit(displayed, ensure_ascii=False, indent=2, sort_keys=presentation != 'model')
        return 1
    observe_payload("businessResult", result)
    displayed = project_response(root, operation, result, view=presentation)
    _emit(displayed, ensure_ascii=False, indent=2, sort_keys=presentation != 'model')
    return 0 if result.get("ok", True) else 1


def _log_management_command(supplied):
    if supplied[:2] == ["project", "logs"]:
        return True
    if supplied[:3] not in (["project", "tools", "read"], ["project", "tools", "export"]):
        return False
    try:
        from scripts.core.operations import new_operations
    except ImportError:
        from core.operations import new_operations
    spec = next(row for row in new_operations() if row.cli == tuple(supplied[1:3]))
    flags = {"--" + snake_case(key).replace("_", "-") for key in spec.input_schema["properties"]} | {"--help"}
    operation = None
    for index, value in enumerate(supplied[3:], start=3):
        flag, separator, inline = value.partition("=")
        matches = [candidate for candidate in flags if candidate.startswith(flag)] if flag.startswith("--") else []
        # Match argparse's existing unambiguous option abbreviations and its
        # last-value behavior, without parsing or rereading @file contents.
        if matches == ["--operation"]:
            operation = inline if separator else supplied[index + 1] if index + 1 < len(supplied) else None
    return isinstance(operation, str) and operation.startswith("logs.")


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    request = {"argv": supplied, "cwd": os.getcwd()}
    try:
        management = _log_management_command(supplied)
    except Exception:
        management = False  # the actual parser retains its original failure
    for key, field in (("--project-root", "projectRoot"), ("--task", "task"), ("--chapter-id", "chapterId")):
        for index, value in enumerate(supplied):
            if value == key and index + 1 < len(supplied) and not supplied[index + 1].startswith("--"):
                request[field] = supplied[index + 1]
            elif value.startswith(key + "="):
                request[field] = value.partition("=")[2]
    with call_scope("cli", request, name="unparsed_cli",
                    enabled=not management) as recorder:
        with capture_cli_streams(recorder):
            exit_code = _main(supplied)
        status, outcome = response_outcome(recorder.payloads.get("response", {}).get("value"))
        if exit_code != 0 and status == "success":
            status, outcome = "error", "business_error"
        recorder.finish(status=status, outcome=outcome,
                        exit_code=exit_code, delivery="written")
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
