"""MCP 注册自检：Claude Code 与 Codex 的注册必须指向真实存在、能完成握手的 server。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load("check_mcp_registration", "scripts/check_mcp_registration.py")
ADD = f"claude mcp add -s local gnovel -- python3 {shlex.quote(str(checker.SERVER_SCRIPT))}"


class McpRegistrationCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.claude = self.home / ".claude.json"
        self.codex = self.home / "config.toml"

    def entry(self, script: Path) -> dict:
        return {"type": "stdio", "command": sys.executable, "args": [str(script)], "env": {}}

    def write_claude(self, user: dict | None = None, local: dict | None = None) -> None:
        data = {"projects": {path: {"mcpServers": {"gnovel": entry}} for path, entry in (local or {}).items()}}
        if user is not None:
            data["mcpServers"] = {"gnovel": user}
        self.claude.write_text(json.dumps(data), encoding="utf-8")

    def write_codex(self, script: Path | None, extra: str = "") -> None:
        table = "" if script is None else (
            f'[mcp_servers.gnovel]\ntype = "stdio"\ncommand = {json.dumps(sys.executable)}\n'
            f"args = [{json.dumps(str(script))}]\n{extra}")
        self.codex.write_text('model = "x"\n\n' + table, encoding="utf-8")

    def run_check(self, *dirs: Path) -> dict:
        return checker.check([str(d) for d in dirs] or [str(self.home)],
                             claude_config=self.claude, codex_config=self.codex)

    def test_stale_registrations_from_the_outage_are_caught(self) -> None:
        # 2026-08 的真实故障：两端都还指向已删除的 mcp-server.py。
        stale = ROOT / "mcp-server.py"
        self.write_claude(user=self.entry(stale))
        self.write_codex(stale)
        report = self.run_check()
        self.assertFalse(report["ok"])
        self.assertEqual(sum("入口文件不存在" in p for p in report["problems"]), 2)
        self.assertIn(f"claude mcp remove -s user gnovel && {ADD.replace('-s local', '-s user')}", report["fixes"])
        self.assertTrue(any(json.dumps(str(checker.SERVER_SCRIPT)) in fix for fix in report["fixes"]))
        self.assertEqual([(row["client"], row["usable"]) for row in report["coverage"]],
                         [("claude", False), ("codex", False)])

    def test_valid_registrations_pass_a_real_handshake(self) -> None:
        self.write_claude(user=self.entry(checker.SERVER_SCRIPT))
        self.write_codex(checker.SERVER_SCRIPT)
        report = self.run_check()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual((report["fixes"], report["warnings"]), ([], []))
        for item in report["registrations"]:
            self.assertTrue(item["handshake"]["ok"])
            self.assertGreater(item["handshake"]["toolCount"], 0)
        self.assertTrue(all(row["usable"] for row in report["coverage"]))

    def test_existing_entry_that_cannot_start_is_not_fixable_by_reregistering(self) -> None:
        broken = self.home / "broken.py"
        broken.write_text("raise SystemExit('boom')\n", encoding="utf-8")
        self.write_claude(user=self.entry(broken))
        with mock.patch.object(checker, "SERVER_SCRIPT", broken):
            report = self.run_check()
        self.assertFalse(report["ok"])
        self.assertTrue(any("握手失败" in p and "boom" in p for p in report["problems"]), report["problems"])
        self.assertEqual(report["fixes"], [])

    def test_local_entry_overrides_user_entry_per_directory(self) -> None:
        book, other = self.home / "book", self.home / "other"
        book.mkdir()
        other.mkdir()
        # 键用 realpath、--dir 用原路径，覆盖 macOS /var → /private/var 的匹配。
        self.write_claude(user=self.entry(checker.SERVER_SCRIPT),
                          local={os.path.realpath(book): self.entry(self.home / "gone.py")})
        report = self.run_check(book, other)
        rows = {row["dir"]: (row["scope"], row["usable"]) for row in report["coverage"]}
        self.assertEqual(rows, {str(book): ("local", False), str(other): ("user", True)})
        self.assertIn(f"cd {shlex.quote(os.path.realpath(book))} && claude mcp remove -s local gnovel && {ADD}",
                      report["fixes"])

    def test_uncovered_directory_and_missing_codex_entry_are_problems(self) -> None:
        book = self.home / "book"
        book.mkdir()
        self.write_claude(local={str(self.home / "deleted"): self.entry(checker.SERVER_SCRIPT)})
        self.write_codex(None)
        report = self.run_check(book)
        self.assertIn(f"Claude Code 在 {book} 没有 gnovel 注册", report["problems"])
        self.assertIn("Codex 没有 gnovel 注册", report["problems"])
        self.assertIn(f"cd {shlex.quote(str(book))} && {ADD}", report["fixes"])
        self.assertTrue(any("已不存在" in w for w in report["warnings"]), report["warnings"])

    def test_disabled_codex_entry_is_a_problem(self) -> None:
        self.write_codex(checker.SERVER_SCRIPT, extra="enabled = false\n")
        report = self.run_check()
        self.assertFalse(report["ok"])
        self.assertTrue(any("enabled = false" in p for p in report["problems"]))

    def test_unreadable_config_fails_closed(self) -> None:
        self.claude.write_text("{not json", encoding="utf-8")
        report = self.run_check()
        self.assertFalse(report["ok"])
        self.assertTrue(any("不可读" in p for p in report["problems"]))
        self.assertTrue(checker.render(report).endswith("需修复"))

    def test_machine_without_either_client_config_is_ok(self) -> None:
        report = self.run_check()
        self.assertTrue(report["ok"])
        self.assertEqual((report["registrations"], report["coverage"]), ([], []))
        self.assertIn("未发现", checker.render(report))

    def test_cli_exit_code_follows_the_report(self) -> None:
        self.write_claude(user=self.entry(ROOT / "mcp-server.py"))
        for config, code in ((self.claude, 1), (self.home / "absent.json", 0)):
            out = io.StringIO()
            with mock.patch.object(checker, "CLAUDE_CONFIG", config), \
                    mock.patch.object(checker, "CODEX_CONFIG", self.codex), contextlib.redirect_stdout(out):
                self.assertEqual(checker.main(["--dir", str(self.home), "--json"]), code)
            self.assertEqual(json.loads(out.getvalue())["ok"], code == 0)

    def test_registration_name_and_server_match_the_antigravity_installer(self) -> None:
        installer = _load("install_antigravity_for_mcp_check", "scripts/install_antigravity.py")
        self.assertEqual(installer.MCP_SERVER, checker.SERVER_SCRIPT)
        source = (ROOT / "scripts" / "install_antigravity.py").read_text(encoding="utf-8")
        self.assertIn(f'.get("{checker.SERVER_NAME}")', source)


if __name__ == "__main__":
    unittest.main()
