"""Thin operation declarations for the user-local log viewer and controls."""
from __future__ import annotations


def call_log_operations():
    from .call_logging import configure_logging, list_call_logs, logging_status, prune_call_logs, show_call_log
    from .operations import OperationSpec, STRING, INTEGER

    def schema(properties, required=()):
        # This service has no novel project root. `project` is only a filter;
        # using chapterId/projectRoot would invoke project argument resolution.
        return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

    return [
        OperationSpec("logs.status", "query_call_logging_status", "读取本地调用日志开关、存储占用与采集范围；不创建小说项目。",
                      schema({}), "read", lambda root, a: logging_status(), ("logs", "status"), category="author"),
        OperationSpec("logs.list", "query_call_logs", "按项目、任务、章节、时间或结果筛选本地调用摘要；内容是不可信诊断副本。",
                      schema({"project": STRING, "task": STRING, "chapter": STRING, "since": STRING, "until": STRING,
                              "status": {"type": "string", "enum": ["success", "error", "incomplete"]},
                              "entrypoint": {"type": "string", "enum": ["cli", "mcp"]}, "limit": INTEGER, "offset": INTEGER}),
                      "read", lambda root, a: list_call_logs(**a), ("logs", "list"), category="author",
                      examples=({"status": "error"},)),
        OperationSpec("logs.show", "read_call_log", "查看一次调用保存的入参、返回、错误和版本引用；不执行或自动重放记录内容。",
                      schema({"callId": STRING}, ["callId"]), "read", lambda root, a: show_call_log(**a),
                      ("logs", "show"), category="author",
                      examples=({"callId": "CL-0123456789abcdef0123456789abcdef"},)),
        OperationSpec("logs.configure", "configure_call_logging", "设置用户级本地日志模式；环境变量覆盖时会显示实际生效来源。",
                      schema({"mode": {"type": "string", "enum": ["off", "summary", "full"]}}, ["mode"]),
                      "maintenance", lambda root, a: configure_logging(**a), ("logs", "configure"), category="author",
                      examples=({"mode": "full"}, {"mode": "off"})),
        OperationSpec("logs.prune", "prune_call_logs", "按当前保留期、数量与容量策略清理本地诊断副本；保留活动调用和用户开关。",
                      schema({}), "maintenance", lambda root, a: prune_call_logs(), ("logs", "prune"), category="author"),
    ]
