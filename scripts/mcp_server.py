#!/usr/bin/env python3
"""Minimal stdio MCP server for the govel project service."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from scripts.core.call_logging import call_scope, exception_details, observe_payload, response_outcome
    from scripts.core.model_response import (presentation_target, project_response,
                                            response_view as requested_response_view)
    from scripts.core.model_text import render_model_text
except ImportError:
    from core.call_logging import call_scope, exception_details, observe_payload, response_outcome
    from core.model_response import (presentation_target, project_response,
                                     response_view as requested_response_view)
    from core.model_text import render_model_text

try:
    from scripts.core.commands import chapter_prose, get_entity, list_entities, list_foreshadowing, project_overview, story_timeline, structure_graph
    from scripts.core.structure_commands import (
        commit_plan, list_operations, list_pending_plans,
        preview_archive_chapter, preview_create_time_anchor, preview_insert_chapter,
        preview_move_chapter, preview_set_chapter_timespan, undo_operation,
    )
    from scripts.core.catalog_api import CATALOG_OPERATIONS, call_catalog_operation
    from scripts.core.project import toolbox_manifest
    from scripts.core.structure_store import StructureError
    from scripts.core.transport_contract import operation_is_mutating, operation_is_read_only
except ImportError:
    from core.commands import chapter_prose, get_entity, list_entities, list_foreshadowing, project_overview, story_timeline, structure_graph
    from core.structure_commands import (
        commit_plan, list_operations, list_pending_plans,
        preview_archive_chapter, preview_create_time_anchor, preview_insert_chapter,
        preview_move_chapter, preview_set_chapter_timespan, undo_operation,
    )
    from core.catalog_api import CATALOG_OPERATIONS, call_catalog_operation
    from core.project import toolbox_manifest
    from core.structure_store import StructureError
    from core.transport_contract import operation_is_mutating, operation_is_read_only

# 版本读 manifest：产品版本唯一真源是 toolbox.manifest.json，不在此硬编码第二份。
SERVER_INFO = {"name": "govel", "version": str(toolbox_manifest()["version"])}

TOOLS = [
    {"name": "get_project_overview", "description": "读取项目卷、弧线、章节和实体数量摘要，不返回正文。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "get_structure_graph", "description": "读取卷、交错弧线、章节序列和章节弧线关系。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "get_entity", "description": "按稳定 ID、名称或唯一别名读取人物、物件、地点、伏笔或规则及引用。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "selector": {"type": "string"}}, "required": ["projectRoot", "selector"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "list_entities", "description": "按类型或关键词列出稳定 ID 实体及引用数量。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "entityType": {"type": ["string", "null"]}, "query": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "list_structure_operations", "description": "读取结构操作审计日志（含 undoable 可撤销标记）。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "list_pending_plans", "description": "列出已预览但尚未提交的结构计划，用于跨会话接续或放弃。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "list_foreshadowing", "description": "读取伏笔与秘密实体、埋设章节和未回收状态。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "get_story_timeline", "description": "读取多轨道故事时间线：绝对时间锚点、章节主轨、实体挂载轨道和倒挂检测。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}}, "required": ["projectRoot"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "get_chapter_prose", "description": "只读读取指定章节的正文全文与字数。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "chapterId": {"type": "string"}}, "required": ["projectRoot", "chapterId"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "preview_set_chapter_timespan", "description": "预览设置章节的故事时间跨度（起止时间锚点实体 ID）。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "chapterId": {"type": "string"}, "startAnchorId": {"type": ["string", "null"]}, "endAnchorId": {"type": ["string", "null"]}}, "required": ["projectRoot", "chapterId"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "preview_create_time_anchor", "description": "预览新建时间锚点：absoluteTime 为可排序的绝对时间字符串，name 为别称。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "name": {"type": "string"}, "absoluteTime": {"type": "string"}, "summary": {"type": "string"}}, "required": ["projectRoot", "name", "absoluteTime"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "preview_insert_chapter", "description": "预览在章节之间插入新章的编号、弧线、stale 和审批影响；保存计划，不改结构真源。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "title": {"type": "string"}, "volumeId": {"type": "string"}, "primaryArcId": {"type": "string"}, "beforeChapterId": {"type": ["string", "null"]}, "secondaryArcIds": {"type": "array", "items": {"type": "string"}}}, "required": ["projectRoot", "title", "volumeId", "primaryArcId"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "preview_move_chapter", "description": "预览章节拖动后的显示编号与引用影响。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "chapterId": {"type": "string"}, "beforeChapterId": {"type": ["string", "null"]}}, "required": ["projectRoot", "chapterId"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "preview_archive_chapter", "description": "预览归档章节（重写场景）：后续章节重编号，正文文件不删除。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "chapterId": {"type": "string"}, "reason": {"type": "string"}}, "required": ["projectRoot", "chapterId"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
    {"name": "commit_structure_plan", "description": "提交已预览且明确批准的结构计划。要求 operationId、planHash 和 approve=true。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "operationId": {"type": "string"}, "planHash": {"type": "string"}, "approve": {"const": True}}, "required": ["projectRoot", "operationId", "planHash", "approve"], "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "undo_structure_operation", "description": "撤销最新且尚未被后续改动覆盖的结构操作。current-binding（schema 4/5）项目撤销真实 binding 变更时必须回显调用者看到的 bindingGeneration：先调 list_structure_operations 读取该 operation 的 bindingGeneration（或 workflow_chapter_receipt 的同名字段），再带着它撤销。缺令牌一律 OWNERSHIP_GENERATION_REQUIRED，令牌过期一律 OWNERSHIP_GENERATION_CONFLICT，服务端不会代你查当前代次。", "inputSchema": {"type": "object", "properties": {"projectRoot": {"type": "string"}, "operationId": {"type": "string"}, "bindingGeneration": {"type": "integer", "description": "被撤销章当前 proseBinding.generation；撤销真实 binding 变更时必填，无语义变更的 operation 可省略"}}, "required": ["projectRoot", "operationId"], "additionalProperties": False}, "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


TOOLS.extend({
    "name": spec["name"],
    "description": spec["description"],
    "inputSchema": spec["inputSchema"],
    "annotations": dict(spec.get("annotations", {})),
} for spec in CATALOG_OPERATIONS)

_LEGACY_TOOL_NAMES = {tool["name"] for tool in TOOLS}
try:
    from scripts.core.operations import operations
except ImportError:
    from core.operations import operations
TOOLS.extend(spec.tool() for spec in operations() if spec.mcp and spec.name not in _LEGACY_TOOL_NAMES)
DISCOVERY = {'view': 'model'}
CLIENT_INFO = None


for _tool in TOOLS:
    _name = _tool["name"]
    if operation_is_mutating(_name):
        _tool["annotations"].update({"readOnlyHint": False, "destructiveHint": _name == "undo_structure_operation"})
    elif operation_is_read_only(_name):
        _tool["annotations"].update({"readOnlyHint": True, "destructiveHint": False})


def _content(payload: dict, *, is_error: bool = False, name: str = '', view: str | None = None, root=None,
             operation=None, response_view=None) -> dict:
    proof = None
    if not is_error and view != 'station' and name == 'read_continuation_context' and payload.get('view') == 'station':
        try:
            from scripts.core.context_transport import semantic_view
        except ImportError:
            from core.context_transport import semantic_view
        model, proof = semantic_view(payload)
        payload = {'ok': True, 'code': 'CONTEXT_MODEL', 'view': 'model', 'modelContext': model}
    if not is_error and view != 'station' and name == 'prepare_task':
        proof = payload.get('context', {}).get('modelProof')
        try:
            from scripts.core.context_transport import task_model_response
        except ImportError:
            from core.context_transport import task_model_response
        payload = task_model_response(payload)
    if operation is not None:
        payload = project_response(root or '.', operation, payload, view=response_view)
    try:
        text = render_model_text(operation, payload, view=response_view or view)
    except Exception:
        # Presentation must not report a completed write as failed or invite a
        # retry. The completed structured result is still the call contract.
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    result = {"content": [{"type": "text", "text": text}], "isError": is_error, "structuredContent": payload}
    if proof is not None:
        if root is not None:
            try:
                from scripts.core.transport_cache import put
            except ImportError:
                from core.transport_cache import put
            result['_meta'] = {'contextProofRef': put(root, 'context-proof', proof)}
        else:
            result['_meta'] = {'contextProof': proof}
    return result


def call_tool(name: str, arguments: dict, *, resolved_spec=None) -> dict:
    try:
        from scripts.core.operations import operations, call_operation, validate_arguments
    except ImportError:
        from core.operations import operations, call_operation, validate_arguments
    # The request already resolved this catalogue for presentation. Execution
    # still goes through call_operation's fresh definition lookup and schema
    # validation; do not cache project inputs or trust an old spec for writes.
    spec = resolved_spec or next((s for s in operations() if s.name == name and s.mcp), None)
    if spec is not None and spec.name == name and spec.mcp and name not in _LEGACY_TOOL_NAMES:
        try:
            from scripts.core.context_transport import model_default_arguments
        except ImportError:
            from core.context_transport import model_default_arguments
        arguments = model_default_arguments(spec.id, arguments)
        observe_payload("effectiveArguments", arguments)
        validate_arguments(spec.input_schema, arguments)
        return call_operation(spec.id, Path(arguments.get("projectRoot", ".")).expanduser().resolve(), arguments)
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise StructureError("TOOL_NOT_FOUND", f"unknown tool: {name}")
    observe_payload("effectiveArguments", arguments)
    validate_arguments(tool["inputSchema"], arguments)
    root = Path(arguments["projectRoot"]).expanduser().resolve()
    if any(s["name"] == name for s in CATALOG_OPERATIONS):
        return call_catalog_operation(name, root, arguments, actor="mcp")
    if name == "get_project_overview":
        return project_overview(root)
    if name == "get_structure_graph":
        return structure_graph(root)
    if name == "get_entity":
        return get_entity(root, arguments["selector"])
    if name == "list_entities":
        return list_entities(root, entity_type=arguments.get("entityType"), query=arguments.get("query", ""))
    if name == "list_structure_operations":
        return {"operations": list_operations(root)}
    if name == "list_pending_plans":
        return {"plans": list_pending_plans(root)}
    if name == "list_foreshadowing":
        return list_foreshadowing(root)
    if name == "get_story_timeline":
        return story_timeline(root)
    if name == "get_chapter_prose":
        return chapter_prose(root, arguments["chapterId"])
    if name == "preview_set_chapter_timespan":
        return preview_set_chapter_timespan(root, chapter_id=arguments["chapterId"], start_anchor_id=arguments.get("startAnchorId"), end_anchor_id=arguments.get("endAnchorId"), actor="mcp")
    if name == "preview_create_time_anchor":
        return preview_create_time_anchor(root, name=arguments["name"], absolute_time=arguments["absoluteTime"], summary=arguments.get("summary", ""), actor="mcp")
    if name == "preview_insert_chapter":
        return preview_insert_chapter(root, title=arguments["title"], volume_id=arguments["volumeId"], primary_arc_id=arguments["primaryArcId"], before_chapter_id=arguments.get("beforeChapterId"), secondary_arc_ids=arguments.get("secondaryArcIds", []), actor="mcp")
    if name == "preview_move_chapter":
        return preview_move_chapter(root, chapter_id=arguments["chapterId"], before_chapter_id=arguments.get("beforeChapterId"), actor="mcp")
    if name == "preview_archive_chapter":
        return preview_archive_chapter(root, chapter_id=arguments["chapterId"], reason=arguments.get("reason", ""), actor="mcp")
    if name == "commit_structure_plan":
        return commit_plan(root, operation_id=arguments["operationId"], plan_hash=arguments["planHash"], approved=arguments.get("approve") is True, actor="mcp")
    if name == "undo_structure_operation":
        # 令牌按调用者传入的原样透传：缺失就是缺失（引擎 fail closed），MCP 层不替模型补一个
        # 当前代次——那等于把 request CAS 变成服务端自己观察当前状态。
        return undo_operation(root, arguments["operationId"], actor="mcp",
                              binding_generation=arguments.get("bindingGeneration"))
    raise StructureError("TOOL_NOT_FOUND", f"unknown tool: {name}")


def _model_request(name: str, arguments: dict) -> tuple[str, str | None, dict]:
    render_name, render_arguments = name, arguments
    if name == 'read_discovered_operation' and isinstance(arguments.get('arguments'), dict):
        target = next((spec for spec in operations() if spec.id == arguments.get('operation')), None)
        if target is not None:
            render_name, render_arguments = target.name, arguments['arguments']
    machine_arguments = arguments
    if render_name in {'read_continuation_context', 'prepare_task'} and render_arguments.get('view') == 'model':
        station_arguments = {**render_arguments, 'view': 'station'}
        machine_arguments = (station_arguments if render_arguments is arguments else
                             {**arguments, 'arguments': station_arguments})
    if render_name == 'run_mechanical_diagnostics':
        full_arguments = {**render_arguments, 'view': 'full'}
        machine_arguments = (full_arguments if render_arguments is arguments else
                             {**arguments, 'arguments': full_arguments})
    return render_name, render_arguments.get('view'), machine_arguments


def _handle(request: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None or (isinstance(method, str) and method.startswith("notifications/")):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": "不熟悉命令先调用 get_tool_help；了解后直接使用公开工具。续写先 prepare_task(projectRoot, task=continue, view=model)，进度用 project.overview；不要直接解析项目内部 JSON 代替状态判断。按返回动作读取材料与分页、处理缺项。MCP 与 CLI 均可使用。任何结构写入先 preview，再由用户明确批准 commit；帮助与查询不授予批准。"}}
    if method == "tools/list":
        try:
            try:
                from scripts.core.tool_catalog import list_capabilities
            except ImportError:
                from core.tool_catalog import list_capabilities
            params = {**DISCOVERY, **request.get('params', {})}
            catalog = list_capabilities(audience=params.get('audience'), task=params.get('task'),
                                        view=params.get('view', 'model'), operation=params.get('operation'), query=params.get('query'))
            names = {row['name'] for row in catalog['operations']}
            visible = TOOLS if params.get('view') == 'audit' and not params.get('operation') else [row for row in TOOLS if row['name'] in names]
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": visible}}
        except (StructureError, KeyError, TypeError, ValueError, OSError) as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
    if method == "tools/call":
        params = request.get("params", {})
        operation, presentation = None, None
        render_name, view = '', None
        try:
            arguments = params.get("arguments", {})
            # MCP rendering owns model selection, so explicit model requests
            # still retain proof in _meta, exactly like the default request.
            if not isinstance(arguments, dict):
                raise StructureError('ARGUMENTS_INVALID', 'arguments must be an object', {'path': 'arguments'})
            name = params.get('name', '')
            resolved_spec = next((spec for spec in operations() if spec.name == name), None)
            operation, selected_arguments = presentation_target(resolved_spec.id if resolved_spec else None, arguments)
            presentation = requested_response_view(operation, selected_arguments)
            render_name, view, machine_arguments = _model_request(name, arguments)
            observe_payload("machineArguments", machine_arguments)
            payload = call_tool(name, machine_arguments, resolved_spec=resolved_spec)
            observe_payload("businessResult", payload)
            result = _content(payload, name=render_name, view=view, root=arguments.get('projectRoot'),
                              operation=operation, response_view=presentation)
        except (StructureError, KeyError, TypeError, ValueError, OSError) as exc:
            payload = {"ok": False, "code": getattr(exc, "code", "TOOL_ERROR"), "error": str(exc), "details": getattr(exc, "details", {})}
            observe_payload("businessResult", payload)
            result = _content(payload, is_error=True, name=render_name, view=view,
                              root=arguments.get('projectRoot') if isinstance(arguments, dict) else None,
                              operation=operation, response_view=presentation)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _client():
    return {"trust": "self_reported" if CLIENT_INFO is not None else "unknown", "info": CLIENT_INFO}


def _is_log_query(request):
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return False
    params = request.get("params")
    if not isinstance(params, dict):
        return False
    if params.get("name") in {"query_call_logging_status", "query_call_logs", "read_call_log"}:
        return True
    arguments = params.get("arguments")
    return (params.get("name") == "read_discovered_operation" and isinstance(arguments, dict)
            and isinstance(arguments.get("operation"), str) and arguments["operation"].startswith("logs."))


def handle(request: dict, *, send_response=None) -> dict | None:
    global CLIENT_INFO
    name = "invalid_request"
    if isinstance(request, dict):
        name = request.get("method", "unknown")
        params = request.get("params")
        if isinstance(params, dict):
            if name == "initialize" and request.get("id") is not None and isinstance(params.get("clientInfo"), dict):
                CLIENT_INFO = params["clientInfo"]
            elif name == "tools/call":
                name = params.get("name", "unknown_tool")
    with call_scope("mcp", request, name=name, client=_client(), enabled=not _is_log_query(request)) as recorder:
        response = _handle(request)
        recorder.observe("response", response)
        status, outcome = response_outcome(response, mcp=True)
        if response is not None and send_response is not None:
            try:
                send_response(response)
            except Exception as exc:
                recorder.finish(status="error", outcome="transport_error", delivery="failed",
                                error={**exception_details(exc), "businessOutcome": outcome})
                raise
        recorder.finish(status=status, outcome=outcome,
                        delivery="written" if response is not None and send_response is not None else "not_observed")
        return response


def _send_response(response):
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    global CLIENT_INFO
    CLIENT_INFO = None
    try:
        from scripts.core.mcp_options import build_parser
    except ImportError:
        from core.mcp_options import build_parser
    args = build_parser(description=__doc__).parse_args()
    DISCOVERY.update(audience=args.audience, task=args.task, view=args.discovery_view)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            with call_scope("mcp", {"rawLine": line}, name="invalid_json", client=_client()) as recorder:
                recorder.observe("response", response)
                _send_response(response)
                recorder.finish(status="error", outcome="protocol_error", delivery="written")
        except BaseException:
            # Decoder failures such as excessive JSON nesting precede handle().
            # Record the received line, then preserve the original exception.
            with call_scope("mcp", {"rawLine": line}, name="json_parse_exception", client=_client()):
                raise
        else:
            handle(request, send_response=_send_response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
