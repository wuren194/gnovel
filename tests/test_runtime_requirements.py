"""Entrypoints reject unsupported Python before loading application modules."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("novelctl", "mcp_server", "local_ci", "dashboard_server")


class RuntimeRequirementsTests(unittest.TestCase):
    def test_unsupported_runtime_has_actionable_stderr_for_script_and_module(self) -> None:
        bootstrap = """
import runpy
import sys
from pathlib import Path
root, entry, mode, minor = sys.argv[1:]
path = Path(root) / 'scripts' / (entry + '.py')
sys.version_info = (3, int(minor), 0)
sys.argv = [str(path), '--help']
sys.path.insert(0, str(root if mode == 'module' else path.parent))
if mode == 'module':
    runpy.run_module('scripts.' + entry, run_name='__main__')
else:
    runpy.run_path(str(path), run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as directory:
            for entry in ENTRYPOINTS:
                for mode in ("script", "module"):
                    for minor in (9, 10):
                        with self.subTest(entry=entry, mode=mode, minor=minor):
                            result = subprocess.run(
                                [sys.executable, "-c", bootstrap, str(ROOT), entry, mode, str(minor)],
                                cwd=directory, input="", capture_output=True, text=True, timeout=20,
                            )
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertIn("Python 3.11", result.stderr)
                            self.assertIn(f"Python 3.{minor}.0", result.stderr)
                            self.assertIn(sys.executable, result.stderr)
                            self.assertIn("brew install python", result.stderr)
                            self.assertIn("command", result.stderr)
                            self.assertNotIn("Traceback", result.stderr)

    def test_supported_runtime_starts_from_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for entry in ENTRYPOINTS:
                with self.subTest(entry=entry):
                    command = [sys.executable, str(ROOT / "scripts" / (entry + ".py"))]
                    request = ""
                    if entry == "mcp_server":
                        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                              "params": {"protocolVersion": "2025-11-25"}}) + "\n"
                    else:
                        command.append("--help")
                    result = subprocess.run(command, cwd=directory, input=request,
                                            capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    if entry == "mcp_server":
                        response = json.loads(result.stdout)
                        self.assertEqual(response["result"]["serverInfo"]["name"], "govel")
                    else:
                        self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
