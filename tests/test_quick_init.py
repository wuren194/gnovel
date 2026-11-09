"""One-shot project initialization tests (`novelctl project quick-init`)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from tests.cli_harness import cli_json


ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


def run_cli(argv: list[str], cwd: Path | None = None, *, process: bool = False) -> tuple[int, dict]:
    if not process:
        return cli_json(argv, cwd=cwd)
    proc = subprocess.run(
        [sys.executable, str(NOVELCTL), *argv],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
    )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"code": "NON_JSON", "stdout": proc.stdout, "stderr": proc.stderr}


class QuickInitTests(unittest.TestCase):
    def test_quick_init_creates_full_project_in_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "新书"
            code, result = run_cli(
                ["project", "quick-init", "--project-root", str(root), "--book-name", "测试书名"], process=True
            )
            self.assertEqual(code, 0, result)
            self.assertEqual(result["code"], "PROJECT_INITIALIZED")
            self.assertEqual(result["agentsMd"], "copied")
            self.assertEqual(result["project"]["bookName"], "测试书名")
            self.assertEqual(result["doctor"]["healthy"], True)
            self.assertEqual(result["doctor"]["code"], "DOCTOR_OK")
            self.assertTrue((root / ".govel" / "workflow.json").is_file())
            marker = json.loads((root / ".govel" / "project.json").read_text(encoding="utf-8"))
            uuid.UUID(marker["projectId"])
            self.assertTrue((root / "AGENTS.md").is_file())

    def test_quick_init_keeps_existing_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            project_id = str(uuid.uuid4())
            idem = str(uuid.uuid4())
            code, result = run_cli([
                "project", "quick-init", "--project-root", str(root),
                "--book-name", "保留模板", "--project-id", project_id, "--idempotency-key", idem,
            ])
            self.assertEqual(code, 0, result)
            agents_md = root / "AGENTS.md"
            agents_md.write_text("自定义项目约束\n", encoding="utf-8")
            code, result = run_cli([
                "project", "quick-init", "--project-root", str(root),
                "--book-name", "保留模板", "--project-id", project_id, "--idempotency-key", idem,
            ])
            self.assertEqual(code, 0, result)
            self.assertEqual(result["code"], "IDEMPOTENT_REPLAY")
            self.assertEqual(result["agentsMd"], "skipped_existing")
            self.assertEqual(agents_md.read_text(encoding="utf-8"), "自定义项目约束\n")

    def test_quick_init_no_agents_md_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, result = run_cli(
                ["project", "quick-init", "--project-root", str(root), "--book-name", "无模板", "--no-agents-md"]
            )
            self.assertEqual(code, 0, result)
            self.assertEqual(result["agentsMd"], "skipped_flag")
            self.assertFalse((root / "AGENTS.md").exists())

    def test_quick_init_rejects_non_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            root.mkdir()
            (root / "chapter.md").write_text("正文", encoding="utf-8")
            code, result = run_cli(
                ["project", "quick-init", "--project-root", str(root), "--book-name", "冲突"]
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "PROJECT_NOT_EMPTY")

    def test_quick_init_rejects_existing_project_with_different_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, _ = run_cli(
                ["project", "quick-init", "--project-root", str(root), "--book-name", "第一本"]
            )
            self.assertEqual(code, 0)
            code2, result2 = run_cli(
                ["project", "quick-init", "--project-root", str(root), "--book-name", "第二本"]
            )
            self.assertNotEqual(code2, 0)
            self.assertEqual(result2["code"], "PROJECT_ALREADY_INITIALIZED")


class InitAutoIdTests(unittest.TestCase):
    def test_init_auto_generates_project_id_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            root.mkdir()
            code, result = run_cli(
                ["project", "init", "--project-root", str(root), "--book-name", "自动ID"]
            )
            self.assertEqual(code, 0, result)
            uuid.UUID(result["project"]["projectId"])


class WorkflowStateTests(unittest.TestCase):
    def _init_with_chapter(self, root: Path) -> str:
        code, result = run_cli(["project", "quick-init", "--project-root", str(root), "--book-name", "范围测试"])
        self.assertEqual(code, 0, result)
        data = json.loads((root/".govel/structure.json").read_text())
        code, preview = run_cli(["project", "structure", "insert-preview", "--project-root", str(root),
            "--title", "第一章", "--volume-id", data['volumes'][0]['volumeId'],
            "--primary-arc-id", data['arcs'][0]['arcId']])
        self.assertEqual(code, 0, preview)
        code, committed = run_cli(["project", "structure", "commit", "--project-root", str(root),
            "--operation-id", preview['operationId'], "--plan-hash", preview['planHash'], "--approve"])
        self.assertEqual(code, 0, committed)
        return committed['chapter']['chapterId']

    def test_approval_and_next_action_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            chapter_id = self._init_with_chapter(root)
            code, result = run_cli(
                ["project", "workflow", "approve-chapters", "--project-root", str(root), "--chapter-id", chapter_id]
            )
            self.assertEqual(code, 0, result)
            code, result = run_cli([
                "project", "workflow", "set-next", "--project-root", str(root),
                "--chapter-id", chapter_id, "--next-action", "planner", "--reason", "继续当前弧线",
                "--needs-approval",
            ])
            self.assertEqual(code, 0, result)
            code, result = run_cli(["project", "workflow", "show", "--project-root", str(root)])
            self.assertEqual(code, 0, result)
            self.assertEqual(result["approvedChapterIds"], [chapter_id])
            self.assertEqual(result["nextAction"]["chapterId"], chapter_id)
            self.assertTrue(result["nextAction"]["needsApproval"])

    def test_unknown_chapter_approval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            self._init_with_chapter(root)
            code, result = run_cli(
                ["project", "workflow", "approve-chapters", "--project-root", str(root), "--chapter-id", "CH-DOESNOTEXIST"]
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "CHAPTER_NOT_FOUND")


class StructureTransactionCliTests(unittest.TestCase):
    """重规划走 preview/commit 事务，替代已移除的破坏式 replan。"""

    def _init_with_structure(self, root: Path) -> dict:
        code, _ = run_cli(
            ["project", "quick-init", "--project-root", str(root), "--book-name", "事务测试"]
        )
        self.assertEqual(code, 0)
        structure_path = root / ".govel" / "structure.json"
        data = json.loads(structure_path.read_text(encoding="utf-8"))
        data["volumes"].append({"volumeId": "VOL-TX000001", "title": "第一卷", "summary": "", "status": "planned"})
        data["arcs"].append({"arcId": "ARC-TX000001", "title": "主线", "summary": "", "status": "planned", "volumeIds": ["VOL-TX000001"]})
        structure_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def test_insert_preview_then_commit_creates_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            self._init_with_structure(root)
            code, preview = run_cli([
                "project", "structure", "insert-preview", "--project-root", str(root),
                "--title", "新章", "--volume-id", "VOL-TX000001", "--primary-arc-id", "ARC-TX000001",
            ])
            self.assertEqual(code, 0, preview)
            self.assertEqual(preview["code"], "SOURCE_CHANGE_PREVIEW")
            self.assertEqual(preview["operation"], "insert_chapter")
            self.assertTrue(preview["requiresApproval"])
            code, result = run_cli([
                "project", "structure", "commit", "--project-root", str(root),
                "--operation-id", preview["operationId"], "--plan-hash", preview["planHash"], "--approve",
            ])
            self.assertEqual(code, 0, result)
            self.assertEqual(result["code"], "STRUCTURE_COMMITTED")
            data = json.loads((root / ".govel" / "structure.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["chapters"]), 1)
            self.assertEqual(data["chapters"][0]["displayCode"], "CH001")

    def test_commit_without_approve_flag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            self._init_with_structure(root)
            code, preview = run_cli([
                "project", "structure", "insert-preview", "--project-root", str(root),
                "--title", "新章", "--volume-id", "VOL-TX000001", "--primary-arc-id", "ARC-TX000001",
            ])
            self.assertEqual(code, 0, preview)
            code, result = run_cli([
                "project", "structure", "commit", "--project-root", str(root),
                "--operation-id", preview["operationId"], "--plan-hash", preview["planHash"],
            ])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "APPROVAL_REQUIRED")

    def test_unknown_arc_preview_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            self._init_with_structure(root)
            code, result = run_cli([
                "project", "structure", "insert-preview", "--project-root", str(root),
                "--title", "新章", "--volume-id", "VOL-TX000001", "--primary-arc-id", "ARC-NOPE",
            ])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "ARC_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
