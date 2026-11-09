#!/usr/bin/env python3
"""Check that Claude Code and Codex can actually start the gnovel MCP server.

2026-08-06 删掉旧的 `mcp-server.py` 后，Claude Code（`~/.claude.json`）与 Codex
（`~/.codex/config.toml`）的注册仍指向它。server 起不来却没有任何报错：模型看不到
工具，只能退回 Bash 与内联脚本，这个状态持续了近三个月。当时 `install_antigravity.py`
只校验 Gemini 的 `settings.json`，这两个客户端没有对应检查。Antigravity当前官方配置
由其安装器检查 `~/.gemini/config/mcp_config.json`；本脚本仍只检查Claude Code与Codex。

本脚本只读：列出两处配置里全部 `gnovel` 注册，确认目标文件存在，再按注册的
command/args 真实启动 server，完成 initialize 与 tools/list 握手。command 按当前
PATH 解析，可能与 GUI 客户端的 PATH 不同。`--dir` 报告在该目录新开的 Claude Code
会话实际生效哪条注册（local 优先于 user）。

不自动修复：`~/.claude.json` 由运行中的 Claude Code 持续改写，脚本直接写入可能与
它互相覆盖。发现问题时只打印 `claude mcp` 命令或 TOML 片段，由人执行。

用法：

    python3 scripts/check_mcp_registration.py                   # 检查当前目录
    python3 scripts/check_mcp_registration.py --dir <项目目录>   # 可重复
    python3 scripts/check_mcp_registration.py --json            # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO / "scripts" / "mcp_server.py"
# 客户端配置里的注册名；install_antigravity.py 用同一个名字（测试绑定）。
SERVER_NAME = "gnovel"
# mcp_server.py 在 initialize 返回的 serverInfo.name（测试以真实握手绑定）。
HANDSHAKE_NAME = "gnovel"
CLAUDE_CONFIG = Path.home() / ".claude.json"
CODEX_CONFIG = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
CLIENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
# 冷启动要导入 scripts/core；超时只防 server 卡死，不衡量性能。
HANDSHAKE_TIMEOUT = 60
HANDSHAKE_REQUESTS = "".join(json.dumps(message) + "\n" for message in (
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "check-mcp-registration", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
))


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def claude_registrations(config: Path) -> tuple[list[dict], list[str]]:
    """user 级取顶层 mcpServers，local 级取 projects[目录].mcpServers。"""
    if not config.exists():
        return [], []
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"Claude Code 配置 {config} 不可读：{exc}"]
    if not isinstance(data, dict):
        return [], [f"Claude Code 配置 {config} 顶层不是对象"]
    items = []
    user = _as_dict(data.get("mcpServers")).get(SERVER_NAME)
    if user is not None:
        items.append({"client": "claude", "scope": "user", "project": None, "entry": user})
    for project, settings in sorted(_as_dict(data.get("projects")).items()):
        local = _as_dict(_as_dict(settings).get("mcpServers")).get(SERVER_NAME)
        if local is not None:
            items.append({"client": "claude", "scope": "local", "project": project, "entry": local})
    return items, []


def codex_registrations(config: Path) -> tuple[list[dict], list[str]]:
    """Codex 只有全局一份：[mcp_servers.gnovel]。"""
    if not config.exists():
        return [], []
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], [f"Codex 配置 {config} 不可读：{exc}"]
    entry = _as_dict(data.get("mcp_servers")).get(SERVER_NAME)
    if entry is None:
        return [], []
    return [{"client": "codex", "scope": "global", "project": None, "entry": entry}], []


def handshake(command: str, args: list[str], env: dict, cwd: str) -> dict:
    """像客户端那样启动 server，走一遍 initialize 与 tools/list。"""
    try:
        proc = subprocess.run([command, *args], input=HANDSHAKE_REQUESTS, capture_output=True,
                              encoding="utf-8", errors="replace", env=env, cwd=cwd,
                              timeout=HANDSHAKE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"{HANDSHAKE_TIMEOUT} 秒内未完成握手"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "detail": f"无法启动：{exc}"}
    replies = {}
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") in (1, 2):
            replies[message["id"]] = message
    info = _as_dict(_as_dict(_as_dict(replies.get(1)).get("result")).get("serverInfo"))
    tools = _as_dict(_as_dict(replies.get(2)).get("result")).get("tools")
    if "name" not in info:
        stderr = proc.stderr.strip().splitlines()
        tail = stderr[-1] if stderr else "无 stderr 输出"
        return {"ok": False, "detail": f"没有握手响应（退出码 {proc.returncode}）：{tail}"}
    if info["name"] != HANDSHAKE_NAME:
        return {"ok": False, "detail": f"响应方是 {info['name']!r}，不是 {HANDSHAKE_NAME!r}"}
    if not isinstance(tools, list) or not tools:
        return {"ok": False, "detail": "tools/list 没有返回任何工具"}
    return {"ok": True, "serverVersion": info.get("version"), "toolCount": len(tools)}


def _inspect(item: dict, cache: dict) -> dict:
    """校验一条注册。fixable 只标配置层的问题：路径对但 server 起不来时，重新注册修不好。"""
    report = {key: item[key] for key in ("client", "scope", "project")}
    report.update(command=None, args=[], script=None, handshake=None,
                  problems=[], warnings=[], fixable=False)

    def fail(message: str) -> dict:
        report["problems"].append(message)
        report["fixable"] = True
        return report

    entry = item["entry"]
    if not isinstance(entry, dict):
        return fail("注册不是对象")
    if entry.get("type", "stdio") != "stdio":
        return fail(f"传输类型是 {entry.get('type')!r}，不是 stdio")
    command, args, extra_env = entry.get("command"), entry.get("args", []), entry.get("env", {})
    if not isinstance(command, str) or not command:
        return fail("缺少 command")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return fail("args 不是字符串列表")
    if not isinstance(extra_env, dict):
        return fail("env 不是表")
    report.update(command=command, args=args)
    if entry.get("enabled") is False:
        fail("注册被 enabled = false 关闭")
    cwd = entry.get("cwd")
    if not isinstance(cwd, str):
        cwd = item["project"] or str(Path.home())
    if not os.path.isdir(cwd):
        report["warnings"].append(f"目录 {cwd} 已不存在，这条注册不会再被用到")
        return report
    scripts = [os.path.join(cwd, arg) for arg in args if arg.endswith(".py")]
    if not scripts:
        return fail("args 里没有 .py 入口")
    report["script"] = scripts[0]
    if not os.path.isfile(scripts[0]):
        return fail(f"入口文件不存在：{scripts[0]}")
    elsewhere = os.path.realpath(scripts[0]) != os.path.realpath(SERVER_SCRIPT)
    if elsewhere:
        report["warnings"].append(f"入口指向另一份仓库：{scripts[0]}")
    executable = shutil.which(command)
    if executable is None:
        return fail(f"command {command!r} 在当前 PATH 里找不到")
    env = {**os.environ, **{str(k): str(v) for k, v in extra_env.items()},
           "PYTHONDONTWRITEBYTECODE": "1"}
    key = (executable, tuple(args), cwd, tuple(sorted(env.items())))
    if key not in cache:
        cache[key] = handshake(executable, args, env, cwd)
    report["handshake"] = cache[key]
    if not cache[key]["ok"]:
        report["problems"].append(f"握手失败：{cache[key]['detail']}")
        report["fixable"] = report["fixable"] or elsewhere
    return report


def _claude_effective(reports: list[dict], directory: str) -> dict | None:
    """在 directory 新开的 Claude Code 会话用哪条：local（目录精确匹配）优先于 user。"""
    keys = (os.path.abspath(directory), os.path.realpath(directory))
    claude = [report for report in reports if report["client"] == "claude"]
    for report in claude:
        if report["scope"] == "local" and report["project"] in keys:
            return report
    return next((report for report in claude if report["scope"] == "user"), None)


def _usable(report: dict | None) -> bool:
    return bool(report) and not report["problems"] and bool(_as_dict(report["handshake"]).get("ok"))


def _fix(client: str, scope: str | None, project: str | None, codex_config: Path) -> str:
    """scope 为 None 表示缺注册，只需 add。"""
    if client == "codex":
        return (f"编辑 {codex_config}，令 [mcp_servers.{SERVER_NAME}] 为 command = \"python3\"、"
                f"args = [{json.dumps(str(SERVER_SCRIPT), ensure_ascii=False)}]（如有 enabled = false 一并删掉）")
    steps = [f"cd {shlex.quote(project)}"] if project else []
    if scope:
        steps.append(f"claude mcp remove -s {scope} {SERVER_NAME}")
    steps.append(f"claude mcp add -s {scope or 'local'} {SERVER_NAME} -- "
                 f"python3 {shlex.quote(str(SERVER_SCRIPT))}")
    return " && ".join(steps)


def _label(report: dict) -> str:
    where = f" {report['project']}" if report["project"] else ""
    return f"{CLIENT_NAMES[report['client']]} {report['scope']}{where}"


def check(dirs: list[str] | None = None, *, claude_config: Path | None = None,
          codex_config: Path | None = None) -> dict:
    """只读检查。dirs 是要确认 Claude Code 生效注册的目录，缺省为当前目录。"""
    claude_config = claude_config or CLAUDE_CONFIG
    codex_config = codex_config or CODEX_CONFIG
    targets = list(dict.fromkeys(os.path.abspath(os.path.expanduser(d)) for d in (dirs or [os.getcwd()])))
    problems, warnings, fixes, items, readable = [], [], [], [], {}
    for client, path, reader in (("claude", claude_config, claude_registrations),
                                 ("codex", codex_config, codex_registrations)):
        found, errors = reader(path)
        items += found
        problems += errors
        readable[client] = path.exists() and not errors
    cache: dict = {}
    reports = [_inspect(item, cache) for item in items]
    for report in reports:
        label = _label(report)
        problems += [f"{label}：{message}" for message in report["problems"]]
        warnings += [f"{label}：{message}" for message in report["warnings"]]
        if report["fixable"]:
            fixes.append(_fix(report["client"], report["scope"], report["project"], codex_config))
    rows = [("claude", d, _claude_effective(reports, d)) for d in targets] if readable["claude"] else []
    if readable["codex"]:
        rows.append(("codex", None, next((r for r in reports if r["client"] == "codex"), None)))
    coverage = []
    for client, directory, effective in rows:
        coverage.append({"client": client, "dir": directory, "usable": _usable(effective),
                         "scope": effective["scope"] if effective else None})
        where = f"在 {directory} " if directory else ""
        if effective is None:
            problems.append(f"{CLIENT_NAMES[client]} {where}没有 {SERVER_NAME} 注册")
            fixes.append(_fix(client, None, directory, codex_config))
        elif not _usable(effective) and not effective["problems"]:
            problems.append(f"{CLIENT_NAMES[client]} {where}生效的注册未完成握手")
    public = ("client", "scope", "project", "command", "args", "script", "handshake", "problems", "warnings")
    return {
        "ok": not problems,
        "serverScript": str(SERVER_SCRIPT),
        "configs": {client: {"path": str(path), "exists": path.exists()}
                    for client, path in (("claude", claude_config), ("codex", codex_config))},
        "registrations": [{key: report[key] for key in public} for report in reports],
        "coverage": coverage,
        "problems": problems,
        "warnings": warnings,
        "fixes": list(dict.fromkeys(fixes)),
    }


def render(report: dict) -> str:
    lines = [f"server 入口：{report['serverScript']}"]
    for client, config in report["configs"].items():
        lines.append(f"{CLIENT_NAMES[client]} 配置：{config['path']}" + ("" if config["exists"] else "（不存在）"))
    lines.append("注册：")
    for item in report["registrations"]:
        shake = _as_dict(item["handshake"])
        if shake.get("ok") and not item["problems"]:
            state = f"✓  {_label(item)}（{shake['toolCount']} 个工具，server {shake.get('serverVersion') or '?'}）"
        else:
            state = f"{'✗' if item['problems'] else '·'}  {_label(item)}"
        lines.append(f"  {state}")
    if not report["registrations"]:
        lines.append("  （无）")
    if report["coverage"]:
        lines.append("生效：")
        for row in report["coverage"]:
            where = f" {row['dir']}" if row["dir"] else ""
            lines.append(f"  {'✓' if row['usable'] else '✗'}  {CLIENT_NAMES[row['client']]}{where}"
                         f" → {row['scope'] or '未注册'}")
    for title, key in (("问题", "problems"), ("提示", "warnings"), ("建议修复（需手动执行）", "fixes")):
        if report[key]:
            lines.append(f"{title}：")
            lines += [f"  - {message}" for message in report[key]]
    if not any(config["exists"] for config in report["configs"].values()):
        lines.append("未发现 Claude Code 或 Codex 配置，没有可检查的注册")
    else:
        lines.append("✓ 注册全部可用" if report["ok"] else "需修复")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Claude Code 与 Codex 能否真正启动 gnovel MCP server（只读）")
    parser.add_argument("--dir", action="append", dest="dirs", metavar="DIR",
                        help="确认在该目录新开的 Claude Code 会话生效哪条注册；可重复，缺省为当前目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读报告")
    args = parser.parse_args(argv)
    report = check(args.dirs)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json else render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
