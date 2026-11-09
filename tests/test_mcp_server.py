from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "mcp_server.py"
NOVELCTL = ROOT / "scripts" / "novelctl.py"


class McpServerTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        result = subprocess.run([sys.executable, str(NOVELCTL), "project", "quick-init", "--project-root", str(root), "--book-name", "MCP测试", "--no-agents-md"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _exchange(self, requests: list[dict]) -> list[dict]:
        proc = subprocess.run([sys.executable, str(SERVER)], input="\n".join(json.dumps(item) for item in requests) + "\n", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_initialize_and_tools_list(self) -> None:
        responses = self._exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"view": "audit"}},
        ])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "govel")
        manifest = json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], manifest["version"])
        names = {item["name"] for item in responses[1]["result"]["tools"]}
        self.assertIn("get_project_overview", names)
        self.assertIn("preview_insert_chapter", names)
        self.assertIn("commit_structure_plan", names)

    def test_read_only_tool_returns_structured_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            self._project(root)
            response = self._exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_project_overview", "arguments": {"projectRoot": str(root)}}}])[0]
            result = response["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["bookName"], "MCP测试")

    def test_unknown_tool_is_error_without_stdout_noise(self) -> None:
        response = self._exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "missing", "arguments": {"projectRoot": "/tmp"}}}])[0]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["code"], "TOOL_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
