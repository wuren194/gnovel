#!/usr/bin/env python3
"""Loopback-only HTTP server for the govel story workspace.

请求边界（Phase 0 §10 / §65）：

- 只绑定 loopback（`--host` 仅 127.0.0.1 / localhost）。
- **Host**：每个请求恰好一个 Host 头，主机必须是本服务绑定的 loopback 名、端口必须
  等于实际监听端口；缺失、重复、畸形、非预期主机、错误端口一律拒绝（DNS rebinding
  在这里被挡下）。不做后缀匹配，不信任 X-Forwarded-*。
- **Origin / Fetch Metadata**：携带 Origin 的 API 请求必须与 `http://<本机名>:<端口>`
  完全一致；无 Origin 时以 Sec-Fetch-Site 为准（same-origin / none）。两类证据都缺的
  浏览器式请求只能走显式凭据路径（token 头）。
- **会话**：`/api/config` 是唯一免会话的 API 路由；它在签发会话 cookie 前强制要求
  「合法 Host + 同源 Origin 或同源 Fetch Metadata」，两者皆无则拒绝，不自动降级。
  其余全部 /api 路由（含全部 GET）需要会话 cookie 或机器客户端的显式 token 头。
- **结构化错误**：`{ok:false, code, error, details, recommendedAction, evidence,
  requestId}`，HTTP 状态码按错误类别映射；不静默吞异常。

传输权限不是作者批准（§11）：会话只证明可访问本地服务；批准/入库仍由受控
CLI/MCP/宿主工位执行。
"""

from __future__ import annotations

try:
    from scripts.runtime_requirements import require_supported_platform, require_supported_python
except ImportError:
    from runtime_requirements import require_supported_platform, require_supported_python

require_supported_python()
require_supported_platform()

import argparse
import json
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from scripts.core.candidate_versions import (
        activate_candidate_version,
        candidate_recovery_status,
        list_candidate_versions,
        read_candidate_version,
        save_candidate_version,
    )
    from scripts.core.catalog_api import CATALOG_OPERATIONS, call_catalog_operation
    from scripts.core.chapter_receipt import chapter_receipt
    from scripts.core.commands import chapter_prose, entity_graph, get_entity, list_entities, list_foreshadowing, project_overview, story_timeline, structure_graph
    from scripts.core.handoff_records import (
        create_handoff,
        handoff_result_status,
        read_handoffs,
        register_handoff_return,
        stop_handoff,
    )
    from scripts.core.project import resolve_knowledge_mode, toolbox_manifest
    from scripts.core.project_doctor import assess_project
    from scripts.core.review_localization import note_issue, review_report_view
    from scripts.core.structure_commands import (
        commit_plan, list_operations, list_pending_plans,
        preview_archive_chapter, preview_create_time_anchor, preview_insert_chapter,
        preview_move_chapter, preview_set_chapter_timespan, undo_operation,
    )
    from scripts.core.structure_store import StructureError
    from scripts.core.view_adapters import plan_impact_view, project_summary
    from scripts.core.knowledge_store import (list_assets, read_asset, save_asset, create_asset,
                                              knowledge_health, list_asset_versions, read_asset_version, restore_asset)
    from scripts.core.knowledge_router import route_knowledge
    from scripts.core.knowledge_index import rebuild_index
    from scripts.novelctl import project_doctor
except ImportError:
    from core.candidate_versions import (
        activate_candidate_version,
        candidate_recovery_status,
        list_candidate_versions,
        read_candidate_version,
        save_candidate_version,
    )
    from core.catalog_api import CATALOG_OPERATIONS, call_catalog_operation
    from core.chapter_receipt import chapter_receipt
    from core.commands import chapter_prose, entity_graph, get_entity, list_entities, list_foreshadowing, project_overview, story_timeline, structure_graph
    from core.handoff_records import (
        create_handoff,
        handoff_result_status,
        read_handoffs,
        register_handoff_return,
        stop_handoff,
    )
    from core.project import resolve_knowledge_mode, toolbox_manifest
    from core.project_doctor import assess_project
    from core.review_localization import note_issue, review_report_view
    from core.structure_commands import (
        commit_plan, list_operations, list_pending_plans,
        preview_archive_chapter, preview_create_time_anchor, preview_insert_chapter,
        preview_move_chapter, preview_set_chapter_timespan, undo_operation,
    )
    from core.structure_store import StructureError
    from core.view_adapters import plan_impact_view, project_summary
    from core.knowledge_store import (list_assets, read_asset, save_asset, create_asset,
                                      knowledge_health, list_asset_versions, read_asset_version, restore_asset)
    from core.knowledge_router import route_knowledge
    from core.knowledge_index import rebuild_index
    from novelctl import project_doctor

DASHBOARD_ROOT = Path(__file__).resolve().parent.parent / "dashboard"
# 产品版本唯一真源是 toolbox.manifest.json，不在此硬编码第二份。
TOOLBOX_VERSION = str(toolbox_manifest()["version"])

SESSION_COOKIE = "govelMutation"
TOKEN_HEADER = "X-govel-Token"
MAX_BODY_BYTES = 10 * 1024 * 1024

# 状态码映射：冲突类 409，认证类 401/403，未找到 404，其余 400。
CONFLICT_CODES = {
    "REVISION_CONFLICT", "CANDIDATE_ACTIVE_TOKEN_CONFLICT", "CANDIDATE_BASE_STALE",
    "CANDIDATE_REQUEST_CONFLICT", "CANDIDATE_BASE_REQUIRED", "HANDOFF_RETURN_CONFLICT",
    "HANDOFF_SINGLE_TRACK", "HANDOFF_ALREADY_RETURNED", "PROJECT_ROOT_OVERRIDE_FORBIDDEN",
    "TRANSACTION_GUARD_DRIFT", "TRANSACTION_INCOMPLETE", "WRITE_SET_CONFLICT",
    "WRITE_SET_MISMATCH", "OWNERSHIP_GENERATION_CONFLICT", "TRANSACTION_RECOVERY_BLOCKED",
}
NOT_FOUND_CODES = {"NOT_FOUND", "CHAPTER_NOT_FOUND", "HANDOFF_NOT_FOUND", "CANDIDATE_VERSION_NOT_FOUND"}
AUTH_CODES = {"SESSION_REQUIRED", "SESSION_REJECTED", "SESSION_BOOTSTRAP_REJECTED", "ORIGIN_REJECTED", "MUTATION_TOKEN_REQUIRED"}
RECOMMENDED_ACTIONS = {
    "SESSION_REQUIRED": "刷新页面重新建立本地会话后重试。",
    "SESSION_REJECTED": "会话已失效（服务可能重启过），刷新页面重新建立会话。",
    "SESSION_BOOTSTRAP_REJECTED": "从本机浏览器直接打开服务地址（不要嵌入其他网站的框架）。",
    "ORIGIN_REJECTED": "只在服务自己的标签页里使用本工作台；跨站调用被拒绝。",
    "HOST_INVALID": "用控制台打印的 http://127.0.0.1:<端口> 地址访问本服务。",
    "CANDIDATE_BASE_STALE": "先读取服务端最新候选版本，把你的缓冲另存为独立副本或刷新基线后重试。",
    "CANDIDATE_ACTIVE_TOKEN_CONFLICT": "另一位客户端刚激活了别的版本；先刷新候选版本列表再决定。",
    "CANDIDATE_REQUEST_CONFLICT": "换一个新的 requestId 重试；原 requestId 已绑定其他内容。",
    "HANDOFF_SINGLE_TRACK": "先查看当前未完成的交接（handoff list），停止或等它返回后再创建新任务。",
    "PROJECT_ROOT_OVERRIDE_FORBIDDEN": "服务已绑定固定项目；如需打开其他书，请重启服务更换 --project-root。",
    "TRANSACTION_INCOMPLETE": "存在未完成事务；在宿主/CLI 运行 workflow recover 完成恢复。",
    "REVISION_CONFLICT": "项目已被其他客户端修改；刷新到最新状态后重新预览。",
}


class RequestRejected(Exception):
    """请求边界拒绝：在进入任何项目数据之前就终止。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


def _status_for(code: str) -> int:
    if code in CONFLICT_CODES:
        return 409
    if code in NOT_FOUND_CODES:
        return 404
    if code in AUTH_CODES:
        return 403 if code in {"SESSION_REJECTED", "SESSION_BOOTSTRAP_REJECTED", "ORIGIN_REJECTED"} else 401
    return 400


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = f"govelDashboard/{TOOLBOX_VERSION}"
    # HTTP/1.0 下浏览器每个 ES module 单开一条连接，一个页面的十几条并发连接会打爆
    # listen backlog 变成 ERR_CONNECTION_RESET；keep-alive 让它复用至多 6 条。
    protocol_version = "HTTP/1.1"
    _body_read = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_ROOT), **kwargs)

    def end_headers(self) -> None:
        # 本地工具:静态资源总是要求浏览器回源校验,否则启发式缓存会把旧模块当新画面。
        if not any(line.startswith(b"Cache-Control:") for line in getattr(self, "_headers_buffer", [])):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ---- 基础设施 ---------------------------------------------------------------

    @property
    def _server_port(self) -> int:
        return int(getattr(self.server, "server_port", 0))

    def _json(self, status: int, payload: dict, *, set_session: bool = False, request_id: str | None = None) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if request_id:
                self.send_header("X-Request-Id", request_id)
            if set_session:
                token = getattr(self.server, "mutation_token", "")
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, code: str, message: str, *, details: dict | None = None,
               request_id: str | None = None) -> None:
        payload = {
            "ok": False,
            "code": code,
            "error": message,
            "details": details or {},
            "recommendedAction": RECOMMENDED_ACTIONS.get(code, "查看 details 中的机器证据后重试。"),
            "evidence": details or {},
        }
        if request_id:
            payload["requestId"] = request_id
        self._json(status, payload, request_id=request_id)

    def _request_id(self) -> str:
        return f"REQ-{secrets.token_hex(4)}"

    def _body(self) -> dict:
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get('Transfer-Encoding') is not None or len(lengths) > 1
                or lengths and (not lengths[0].isascii() or not lengths[0].isdigit())):
            raise RequestRejected(400, 'REQUEST_LENGTH_INVALID', 'provide one nonnegative Content-Length')
        length = int(lengths[0]) if lengths else 0
        if length > MAX_BODY_BYTES:
            raise RequestRejected(413, "REQUEST_TOO_LARGE", "request body exceeds the local limit", {"limitBytes": MAX_BODY_BYTES})
        raw = self.rfile.read(length) if length else b""
        self._body_read = True
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestRejected(400, "REQUEST_BODY_UNPARSEABLE", f"request body is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RequestRejected(400, "REQUEST_BODY_INVALID", "request body must be a JSON object")
        return data

    # ---- 请求边界（§10） ---------------------------------------------------------

    def _check_host(self) -> None:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1:
            raise RequestRejected(400, "HOST_INVALID", "request must carry exactly one Host header", {"received": len(hosts)})
        raw = hosts[0].strip()
        if not raw:
            raise RequestRejected(400, "HOST_INVALID", "Host header is empty")
        candidate, sep, port_part = raw.rpartition(":")
        if sep and (":" in candidate or not port_part.isdigit()):
            raise RequestRejected(400, "HOST_INVALID", f"malformed Host header: {raw!r}")
        if not sep:
            candidate, port_part = raw, "80"
        port = int(port_part)
        allowed_hosts = {"127.0.0.1", "localhost", getattr(self.server, "server_name", "") or ""}
        if candidate.lower() not in {name.lower() for name in allowed_hosts if name}:
            raise RequestRejected(400, "HOST_INVALID", f"unexpected Host: {candidate!r}", {"expected": sorted(allowed_hosts)})
        if port != self._server_port:
            raise RequestRejected(400, "HOST_INVALID", f"Host port {port} does not match the listening port {self._server_port}")

    def _same_origin(self, origin: str) -> bool:
        parsed = urlparse(origin)
        if parsed.scheme != "http":
            return False
        host = parsed.hostname or ""
        if host not in {"127.0.0.1", "localhost"}:
            return False
        port = parsed.port if parsed.port is not None else 80
        return port == self._server_port

    def _check_origin_and_metadata(self) -> None:
        """Origin 优先；无 Origin 看 Fetch Metadata；两者皆无 → 只有显式凭据可通行。"""
        origins = self.headers.get_all("Origin") or []
        if len(origins) > 1:
            raise RequestRejected(403, "ORIGIN_REJECTED", "multiple Origin headers", {"received": len(origins)})
        if origins:
            if not self._same_origin(origins[0]):
                raise RequestRejected(403, "ORIGIN_REJECTED", f"cross-origin request rejected: {origins[0]}")
            return
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise RequestRejected(403, "ORIGIN_REJECTED", f"cross-site fetch metadata rejected: {fetch_site}")

    def _bootstrap_allowed(self) -> None:
        """会话签发前的硬门：合法 Host +（同源 Origin 或 同源 Fetch Metadata）。"""
        self._check_origin_and_metadata()
        origins = self.headers.get_all("Origin") or []
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if not origins and not fetch_site:
            raise RequestRejected(403, "SESSION_BOOTSTRAP_REJECTED",
                                  "session issuance needs a same-origin Origin or same-origin fetch metadata; no downgrade")

    def _presented_credential(self) -> str:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return (morsel.value if morsel else "") or (self.headers.get(TOKEN_HEADER) or "")


    def _require_session(self) -> None:
        """敏感读取与写入统一凭据门（§10：敏感 GET 也要 session）。"""
        expected = getattr(self.server, "mutation_token", "")
        presented = self._presented_credential()
        if not presented:
            raise RequestRejected(401, "SESSION_REQUIRED", "this route needs a local session")
        if not expected or not secrets.compare_digest(presented, expected):
            raise RequestRejected(403, "SESSION_REJECTED", "session token rejected (server may have restarted)")

    def _root(self, payload: dict | None = None) -> Path:
        configured = getattr(self.server, "project_root", None)
        requested = (payload or {}).get("projectRoot") or self.headers.get("X-Project-Root")
        if configured:
            root = Path(configured).expanduser().resolve()
            if requested and Path(unquote(str(requested))).expanduser().resolve() != root:
                raise StructureError("PROJECT_ROOT_OVERRIDE_FORBIDDEN",
                                     "requested projectRoot differs from the bound project")
            return root
        raw = str(requested or "")
        if not raw:
            raise StructureError("PROJECT_ROOT_REQUIRED", "projectRoot is required")
        return Path(unquote(raw)).expanduser().resolve()

    # ---- 路由 -------------------------------------------------------------------

    def do_GET(self) -> None:
        request_id = self._request_id()
        try:
            self._check_host()
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._check_origin_and_metadata()
                return self._route_get(parsed, request_id)
            return super().do_GET()
        except RequestRejected as exc:
            return self._error(exc.status, exc.code, str(exc), details=exc.details, request_id=request_id)
        except (StructureError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", "REQUEST_ERROR")
            return self._error(_status_for(code), code, str(exc), details=getattr(exc, "details", {}), request_id=request_id)

    def do_POST(self) -> None:
        request_id = self._request_id()
        try:
            self._check_host()
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                return self._error(404, "NOT_FOUND", "POST is only supported on /api routes", request_id=request_id)
            self._check_origin_and_metadata()
            return self._route_post(parsed, request_id)
        except RequestRejected as exc:
            return self._error(exc.status, exc.code, str(exc), details=exc.details, request_id=request_id)
        except (StructureError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            code = getattr(exc, "code", "REQUEST_ERROR")
            return self._error(_status_for(code), code, str(exc), details=getattr(exc, "details", {}), request_id=request_id)
        finally:
            # keep-alive 下没读尽的请求体会串进这条连接上的下一个请求，宁可断开。
            if not self._body_read:
                self.close_connection = True

    def _route_get(self, parsed, request_id: str) -> None:
        path = parsed.path
        if path == "/api/config":
            self._bootstrap_allowed()
            bound = getattr(self.server, "project_root", None)
            return self._json(200, {
                "ok": True,
                "projectRoot": bound,
                "serverBound": bool(bound),
                "projectSwitchSupported": not bool(bound),
                "session": "cookie",
                "toolboxVersion": TOOLBOX_VERSION,
            }, set_session=True, request_id=request_id)
        self._require_session()
        query = parse_qs(parsed.query)
        if path == '/api/knowledge/catalog':
            return self._json(200, list_assets(status=(query.get('status') or [None])[0],
                                               station=(query.get('station') or [None])[0],
                                               kind=(query.get('kind') or [None])[0]), request_id=request_id)
        if path == '/api/knowledge/read':
            return self._json(200, read_asset((query.get('assetId') or [''])[0],
                                              expected_hash=(query.get('expectedHash') or [None])[0]), request_id=request_id)
        if path == '/api/knowledge/versions':
            return self._json(200, list_asset_versions((query.get('assetId') or [''])[0]), request_id=request_id)
        if path == '/api/knowledge/version-read':
            return self._json(200, read_asset_version((query.get('assetId') or [''])[0],
                                                      (query.get('versionHash') or [''])[0]), request_id=request_id)
        if path == '/api/knowledge/route':
            return self._json(200, route_knowledge(planningIntent=(query.get('intent') or [''])[0],
                                                   station=(query.get('station') or [None])[0]), request_id=request_id)
        if path == '/api/knowledge/health':
            return self._json(200, knowledge_health(), request_id=request_id)
        if path == '/api/knowledge/mode':
            root = self._root()
            override = (query.get('override') or [None])[0]
            marker_mode = resolve_knowledge_mode(None, project_root=root)
            effective = resolve_knowledge_mode(override, project_root=root)
            valid_override = override in {"off", "broad", "curated"}
            return self._json(200, {"ok": True, "effective": effective,
                                    "projectDefault": marker_mode,
                                    "override": override if valid_override else None,
                                    "source": "task_override" if valid_override else "project_default",
                                    "modes": ["off", "broad", "curated"]}, request_id=request_id)
        if path == "/api/summary":
            return self._json(200, project_summary(self._root()), request_id=request_id)
        if path == "/api/overview":
            return self._json(200, {"ok": True, **project_overview(self._root())}, request_id=request_id)
        if path == "/api/structure":
            return self._json(200, {"ok": True, **structure_graph(self._root())}, request_id=request_id)
        if path == "/api/chapter/prose":
            chapter_id = (query.get("chapterId") or [""])[0]
            return self._json(200, {"ok": True, **chapter_prose(self._root(), chapter_id)}, request_id=request_id)
        if path == "/api/doctor":
            # /api/doctor 是只读路由：需要会话但不吃 mutation 语义。409 只在真有 blocking
            # 域时返回，且始终带完整分域报告——前端要用 domains 渲染诊断页。
            try:
                chapter_id = (query.get("chapterId") or [None])[0]
                report = project_doctor(self._root(), strict=False) if chapter_id is None else assess_project(
                    self._root(), chapter_id=chapter_id)
                return self._json(200 if report["healthy"] else 409,
                                  {"ok": report["healthy"], "doctor": report}, request_id=request_id)
            except Exception as exc:  # noqa: BLE001 - 检查失败必须明确报出，不吞成健康
                return self._json(400, {"ok": False, "code": "DOCTOR_FAILED", "error": str(exc), "requestId": request_id,
                                        "doctor": {"healthy": False, "problems": [str(exc)]}})
        if path == "/api/workflow/receipt":
            chapter_id = (query.get("chapterId") or [None])[0]
            title_candidate_id = (query.get("titleCandidateId") or [None])[0]
            skip_values = query.get("skipTitle", ["false"])
            if len(skip_values) != 1 or skip_values[0] not in {"true", "false"}:
                raise ValueError("skipTitle must be true or false")
            skip_title = skip_values[0] == "true"
            try:
                return self._json(200, {"ok": True, **chapter_receipt(self._root(), chapter_id, title_candidate_id=title_candidate_id, skip_title=skip_title)}, request_id=request_id)
            except Exception as exc:  # noqa: BLE001 - 未知章节等错误转为前端可读响应
                code = getattr(exc, "code", "RECEIPT_FAILED")
                return self._error(_status_for(code), code, str(exc), details=getattr(exc, "details", {}), request_id=request_id)
        if path == "/api/entities":
            return self._json(200, {"ok": True, **list_entities(self._root())}, request_id=request_id)
        if path == "/api/operations":
            return self._json(200, {"ok": True, "operations": list_operations(self._root())}, request_id=request_id)
        if path == "/api/plans":
            plan_rows = list_pending_plans(self._root())
            for plan in plan_rows:
                plan["impactView"] = plan_impact_view(plan)
            return self._json(200, {"ok": True, "plans": plan_rows}, request_id=request_id)
        if path == "/api/foreshadowing":
            return self._json(200, {"ok": True, **list_foreshadowing(self._root())}, request_id=request_id)
        if path == "/api/graph":
            return self._json(200, {"ok": True, **entity_graph(self._root())}, request_id=request_id)
        if path == "/api/timeline":
            return self._json(200, {"ok": True, **story_timeline(self._root())}, request_id=request_id)
        if path == "/api/candidate/versions":
            return self._json(200, list_candidate_versions(self._root(), (query.get("chapterId") or [""])[0]), request_id=request_id)
        if path == "/api/candidate/handoff":
            try:
                from scripts.core.current_candidate import prepare_candidate_handoff
            except ImportError:
                from core.current_candidate import prepare_candidate_handoff
            return self._json(200, prepare_candidate_handoff(self._root(), chapterId=(query.get("chapterId") or [""])[0]), request_id=request_id)
        if path in {'/api/candidate/batches', '/api/candidate/reading'}:
            try:
                from scripts.core.candidate_batch import list_candidate_batches, candidate_reading
            except ImportError:
                from core.candidate_batch import list_candidate_batches, candidate_reading
            if path == '/api/candidate/batches':
                return self._json(200, list_candidate_batches(self._root()), request_id=request_id)
            reference = json.loads((query.get('batchRef') or ['null'])[0])
            return self._json(200, candidate_reading(self._root(), batchRef=reference), request_id=request_id)
        if path == "/api/candidate/read":
            version_raw = (query.get("version") or [""])[0]
            if not version_raw.isdigit():
                raise ValueError("version must be an integer")
            return self._json(200, read_candidate_version(self._root(), (query.get("chapterId") or [""])[0], int(version_raw)), request_id=request_id)
        if path == "/api/candidate/recovery-status":
            return self._json(200, {"ok": True, "chapterId": (query.get("chapterId") or [""])[0],
                                    "recovery": candidate_recovery_status(self._root(), (query.get("chapterId") or [""])[0])}, request_id=request_id)
        if path == "/api/handoff/list":
            chapter_id = (query.get("chapterId") or [None])[0]
            return self._json(200, read_handoffs(self._root(), chapter_id=chapter_id), request_id=request_id)
        if path == "/api/handoff/read":
            return self._json(200, read_handoffs(self._root(), handoff_id=(query.get("handoffId") or [""])[0]), request_id=request_id)
        if path == "/api/handoff/result-status":
            return self._json(200, handoff_result_status(self._root(), (query.get("handoffId") or [""])[0]), request_id=request_id)
        if path == "/api/review/report":
            return self._json(200, self._review_report(query), request_id=request_id)
        return self._error(404, "NOT_FOUND", f"unknown API route: {path}", request_id=request_id)

    def _review_report(self, query: dict) -> dict:
        """两传输共用持锁的报告与稿件核验，不在传输层拼接不同快照。"""
        root = self._root()
        chapter_id = (query.get("chapterId") or [""])[0]
        return review_report_view(root, chapter_id)

    def _route_post(self, parsed, request_id: str) -> None:
        path = parsed.path
        if path == "/api/config":
            raise RequestRejected(405, "METHOD_NOT_ALLOWED", "use GET /api/config to bootstrap a session")
        self._require_session()
        payload = self._body()
        root = self._root(payload)
        if path == '/api/knowledge/save':
            return self._json(200, save_asset(asset_id=payload['assetId'], metadata=payload['metadata'],
                                              body=payload['body'], expected_hash=payload['expectedHash']), request_id=request_id)
        if path == '/api/knowledge/restore':
            return self._json(200, restore_asset(asset_id=payload['assetId'],
                                                 version_hash=payload['versionHash'],
                                                 expected_hash=payload['expectedHash']), request_id=request_id)
        if path == '/api/knowledge/create':
            return self._json(200, create_asset(title=payload['title'], kind=payload['kind'],
                                                stations=payload['stations'], intents=payload['intents'],
                                                tags=payload['tags'], body=payload['body'],
                                                applicability=payload.get('applicability', ''),
                                                exclusions=payload.get('exclusions', ''),
                                                license=payload.get('license', 'authored-local')), request_id=request_id)
        if path == '/api/knowledge/index-rebuild':
            return self._json(200, rebuild_index(), request_id=request_id)
        catalog_spec = next((s for s in CATALOG_OPERATIONS if s["route"] == path), None)
        if catalog_spec:
            result = call_catalog_operation(catalog_spec["name"], root, payload, actor="human_ui")
            return self._json(200, {"ok": True, **result}, request_id=request_id)
        if path == "/api/entity":
            return self._json(200, {"ok": True, **get_entity(root, payload["selector"])}, request_id=request_id)
        if path == "/api/chapter/insert/preview":
            result = preview_insert_chapter(root, title=payload["title"], volume_id=payload["volumeId"], primary_arc_id=payload["primaryArcId"], before_chapter_id=payload.get("beforeChapterId"), secondary_arc_ids=payload.get("secondaryArcIds", []), actor="human_ui")
            return self._json(200, {"ok": True, **result}, request_id=request_id)
        if path == "/api/chapter/move/preview":
            result = preview_move_chapter(root, chapter_id=payload["chapterId"], before_chapter_id=payload.get("beforeChapterId"), actor="human_ui")
            return self._json(200, {"ok": True, **result}, request_id=request_id)
        if path == "/api/chapter/archive/preview":
            result = preview_archive_chapter(root, chapter_id=payload["chapterId"], reason=payload.get("reason", ""), actor="human_ui")
            return self._json(200, {"ok": True, **result}, request_id=request_id)
        if path == "/api/chapter/timespan/preview":
            result = preview_set_chapter_timespan(root, chapter_id=payload["chapterId"], start_anchor_id=payload.get("startAnchorId"), end_anchor_id=payload.get("endAnchorId"), actor="human_ui")
            return self._json(200, {"ok": True, **result}, request_id=request_id)
        if path == "/api/anchor/create/preview":
            result = preview_create_time_anchor(root, name=payload["name"], absolute_time=payload["absoluteTime"], summary=payload.get("summary", ""), actor="human_ui")
            return self._json(200, {"ok": True, **result}, request_id=request_id)
        if path == "/api/chapter/prose":
            return self._json(200, {"ok": True, **chapter_prose(root, payload["chapterId"])}, request_id=request_id)
        if path == "/api/plan/commit":
            return self._json(200, commit_plan(root, operation_id=payload["operationId"], plan_hash=payload["planHash"], approved=payload.get("approve") is True, actor="human_ui"), request_id=request_id)
        if path == "/api/operation/undo":
            # bindingGeneration 由 UI 回显它自己看到的代次（/api/operations 每行都带）；
            # 服务端不代为读取当前代次，缺失即由引擎 fail closed。
            return self._json(200, undo_operation(root, payload["operationId"], actor="human_ui",
                                                  binding_generation=payload.get("bindingGeneration")), request_id=request_id)
        if path == "/api/candidate/save":
            receipt = save_candidate_version(
                root, payload.get("chapterId", ""), body=payload.get("body"),
                request_id=payload.get("requestId", ""),
                base_candidate_version=payload.get("baseCandidateVersion"),
                source=str(payload.get("source") or "browser"))
            return self._json(200, receipt, request_id=request_id)
        if path == "/api/candidate/activate":
            return self._json(200, activate_candidate_version(
                root, payload.get("chapterId", ""),
                candidate_version=payload.get("candidateVersion"),
                expected_active_token=payload.get("expectedActiveToken")), request_id=request_id)
        if path == "/api/handoff/create":
            result = create_handoff(root, chapter_id=payload.get("chapterId"), task=payload.get("task", ""),
                                    station=payload.get("station", ""),
                                    produces_candidate=bool(payload.get("producesCandidate", False)),
                                    candidate_version=payload.get("candidateVersion"),
                                    candidate_active_token=payload.get("candidateActiveToken"),
                                    context_hash=payload.get("contextHash"),
                                    context_applicable=bool(payload.get("contextApplicable", True)),
                                    prompt_plan_fingerprint=payload.get("promptPlanFingerprint"),
                                    dependency_manifest=payload.get("dependencyManifest"),
                                    pov=payload.get("pov"),
                                    effective_knowledge_mode=payload.get("effectiveKnowledgeMode"),
                                    knowledge_mode_source=payload.get("knowledgeModeSource"),
                                    input_scope=payload.get("inputScope"),
                                    rerun_of=payload.get("rerunOf"))
            return self._json(200, result, request_id=request_id)
        if path == "/api/handoff/stop":
            return self._json(200, stop_handoff(root, payload.get("handoffId", ""), reason=payload.get("reason", "")), request_id=request_id)
        if path == "/api/handoff/register-return":
            return self._json(200, register_handoff_return(
                root, handoff_id=payload.get("handoffId", ""),
                return_request_id=payload.get("returnRequestId", ""),
                body=payload.get("body")), request_id=request_id)
        if path == "/api/review/note":
            return self._json(200, note_issue(
                root, payload.get("chapterId", ""), report_sha256=payload.get("reportSha256", ""),
                finding_index=payload.get("findingIndex", -1), state=payload.get("state", ""),
                note=payload.get("note", "")), request_id=request_id)
        return self._error(404, "NOT_FOUND", f"unknown API route: {path}", request_id=request_id)

    def log_message(self, format: str, *args) -> None:
        print(format % args)


class DashboardServer(ThreadingHTTPServer):
    # 默认 5：首屏的模块图会同时握十几条连接，超出的直接被内核重置。
    request_queue_size = 128


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="govel local story workspace server")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root")
    args = parser.parse_args(argv)
    server = DashboardServer((args.host, args.port), DashboardHandler)
    server.project_root = str(Path(args.project_root).expanduser().resolve()) if args.project_root else None
    server.mutation_token = secrets.token_urlsafe(24)
    print(f"govel dashboard: http://{args.host}:{server.server_port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
