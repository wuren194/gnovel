from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

#: Every script documented as a command in a SKILL.md or contract.  A broken
#: entrypoint stays invisible until someone actually follows the instructions.
#: 与 scripts/ 目录现有脚本一致；新增命令必须同步这里。
DOCUMENTED_SCRIPTS = (
    "scripts/novelctl.py",
    "scripts/ai_cliche_linter.py",
    "scripts/validate-text.py",
    "scripts/check_voice_lexicon.py",
    "scripts/surgical_editor.py",
    "scripts/aggregate_diagnostics.py",
    "scripts/check_degeneration.py",
    "scripts/continuation_context.py",
    "scripts/provider.py",
    "scripts/release_audit.py",
    "scripts/state_compactor.py",
    "scripts/story_summarizer.py",
)


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        text=True,
        input=stdin,
        capture_output=True,
        check=False,
    )


class CliEntrypointTests(unittest.TestCase):
    def test_novelctl_entrypoint_works(self) -> None:
        completed = _run(str(ROOT / "scripts/novelctl.py"), "--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("project", completed.stdout)

    def test_every_documented_script_has_a_working_help(self) -> None:
        for relative in DOCUMENTED_SCRIPTS:
            with self.subTest(script=relative):
                completed = _run(str(ROOT / relative), "--help")
                self.assertEqual(completed.returncode, 0, f"{relative}: {completed.stderr}")

    def test_novelctl_exposes_project_subcommands(self) -> None:
        completed = _run(str(ROOT / "scripts/novelctl.py"), "project", "--help")
        self.assertIn("quick-init", completed.stdout)
        self.assertIn("doctor", completed.stdout)
        self.assertIn("state", completed.stdout)
        self.assertIn("context", completed.stdout)
        self.assertIn("summary", completed.stdout)

    def test_linters_emit_parsable_json_with_declared_limits(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as temp:
            draft = Path(temp) / "draft.md"
            draft.write_text("他把杯子推过去，指腹压在杯沿上没松。\n", encoding="utf-8")
            for relative in ("scripts/ai_cliche_linter.py", "scripts/check_voice_lexicon.py"):
                with self.subTest(script=relative):
                    completed = _run(str(ROOT / relative), str(draft), "--json")
                    self.assertIn(completed.returncode, (0, 1), completed.stderr)
                    report = json.loads(completed.stdout)
                    self.assertIn("recommendation", report)
                    self.assertIn("limitations", report)


if __name__ == "__main__":
    unittest.main()
