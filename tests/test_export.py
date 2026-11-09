"""`novelctl project export` assembles finished chapters in reading order.

动机：`正文/` 下的文件名是 chapterId（`正文/CH-XXXXXXXXXXXX.md`），与章节顺序和标题
都解耦——要读全本或发出去，此前只能手工按 structure 的 displayOrdinal 排序再逐个拼接。
导出是只读操作，不得改 structure、正文或状态。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tests.cli_harness import cli_json


ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


def run_cli(args: list[str], *, process: bool = False) -> tuple[int, dict]:
    if not process:
        return cli_json(args)
    proc = subprocess.run(
        [sys.executable, str(NOVELCTL), *args], cwd=ROOT, capture_output=True, text=True
    )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"code": "NON_JSON", "stdout": proc.stdout, "stderr": proc.stderr}


CHAPTERS = [
    ("CH-EXPORT00000A", "雨夜第一次", "stable", "他把伞收了。\n\n水从袖口灌进去。"),
    ("CH-EXPORT00000B", "第二天早上", "stable", "桌面是凉的。\n\n他先摸到的是空的那半边。"),
    ("CH-EXPORT00000C", "还没写完", "draft", "草稿内容。"),
]


def make_project(root: Path) -> None:
    code, result = run_cli(
        ["project", "quick-init", "--project-root", str(root), "--book-name", "导出测试", "--no-agents-md"]
    )
    assert code == 0, result
    path = root / ".govel" / "structure.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["volumes"].append(
        {"volumeId": "VOL-EXPORT01", "title": "第一卷", "summary": "", "status": "planned"}
    )
    data["arcs"].append(
        {
            "arcId": "ARC-EXPORT01",
            "title": "主线",
            "summary": "",
            "status": "planned",
            "volumeIds": ["VOL-EXPORT01"],
        }
    )
    data["revision"] = 1
    from scripts.core.prose_binding import seal_binding
    from scripts.core.artifacts import sha256_text
    for index, (chapter_id, title, status, body) in enumerate(CHAPTERS, 1):
        data["chapters"].append(
            {
                "chapterId": chapter_id,
                "displayOrdinal": index,
                "displayCode": f"CH{index:03d}",
                "title": title,
                "placement": {"scope": "volume", "volumeId": "VOL-EXPORT01"},
                "filePath": f"正文/{chapter_id}.md",
                "status": status,
                "storyTimeStartId": None,
                "storyTimeEndId": None,
            }
        )
        data["chapters"][-1]["proseBinding"] = seal_binding(data['projectId'], chapter_id,
            f"正文/{chapter_id}.md", state='managed', generation=data['revision'],
            expected_content_digest='sha256:'+sha256_text(body))
        data["chapterArcLinks"].append(
            {"chapterId": chapter_id, "arcId": "ARC-EXPORT01", "role": "primary"}
        )
        (root / "正文" / f"{chapter_id}.md").write_text(body, encoding="utf-8")
    from scripts.core.structure_store import validate_structure
    validate_structure(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class ExportTests(unittest.TestCase):
    def test_default_exports_only_stable_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            code, result = run_cli(["project", "export", "--project-root", str(root)], process=True)
            self.assertEqual(code, 0, result)
            self.assertEqual(result["code"], "MANUSCRIPT_EXPORTED")
            self.assertEqual(result["chapterCount"], 2, "草稿章不应进入成品")
            self.assertEqual([item["reason"] for item in result["skipped"]], ["status=draft"])
            self.assertTrue(Path(result["output"]).is_file())

    def test_chapters_are_ordered_by_display_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            code, result = run_cli(["project", "export", "--project-root", str(root)])
            self.assertEqual(code, 0, result)
            text = Path(result["output"]).read_text(encoding="utf-8")
            first = text.index("雨夜第一次")
            second = text.index("第二天早上")
            self.assertLess(first, second, "章节必须按阅读顺序拼接")
            self.assertIn("导出测试", text, "成品应带书名")
            self.assertIn("CH001", text, "成品应带章节号")

    def test_status_filter_can_include_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            code, result = run_cli(
                [
                    "project", "export", "--project-root", str(root),
                    "--status", "stable", "--status", "draft",
                ]
            )
            self.assertEqual(code, 0, result)
            self.assertEqual(result["chapterCount"], 3)
            self.assertEqual(result["skipped"], [])

    def test_markdown_format_uses_headings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            out = Path(temp) / "full.md"
            code, result = run_cli(
                [
                    "project", "export", "--project-root", str(root),
                    "--format", "md", "--output", str(out),
                ]
            )
            self.assertEqual(code, 0, result)
            self.assertEqual(result["format"], "md")
            self.assertIn("# CH001 雨夜第一次", out.read_text(encoding="utf-8"))

    def test_export_does_not_mutate_the_project(self) -> None:
        """导出是只读操作：structure、正文、workflow 都不得变化。"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            watched = {
                rel: (root / rel).read_text(encoding="utf-8")
                for rel in (
                    ".govel/structure.json",
                    ".govel/workflow.json",
                    f"正文/{CHAPTERS[0][0]}.md",
                )
            }
            code, _ = run_cli(["project", "export", "--project-root", str(root)])
            self.assertEqual(code, 0)
            for rel, before in watched.items():
                self.assertEqual(
                    (root / rel).read_text(encoding="utf-8"), before, f"{rel} 被导出改动了"
                )

    def test_missing_prose_is_reported_not_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            (root / "正文" / f"{CHAPTERS[0][0]}.md").unlink()
            code, result = run_cli(["project", "export", "--project-root", str(root)])
            self.assertEqual(code, 0, result)
            self.assertEqual(result["chapterCount"], 1)
            reasons = [item["reason"] for item in result["skipped"]]
            self.assertIn("prose file missing", reasons, "缺失正文必须显式报告")

    def test_nothing_to_export_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, result = run_cli(
                ["project", "quick-init", "--project-root", str(root), "--book-name", "空书", "--no-agents-md"]
            )
            self.assertEqual(code, 0, result)
            code, result = run_cli(["project", "export", "--project-root", str(root)])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "NOTHING_TO_EXPORT")

    def test_invalid_format_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            make_project(root)
            proc = subprocess.run(
                [sys.executable, str(NOVELCTL), "project", "export",
                 "--project-root", str(root), "--format", "epub"],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0, "argparse 应拒绝未支持的格式")

    def test_command_is_documented_and_registered(self) -> None:
        registry = json.loads((ROOT / "shared/registries/tools.json").read_text(encoding="utf-8"))
        entry = next(
            item for item in registry["entrypoints"] if item["id"] == "novelctl"
        )
        self.assertIn("export", entry["surface"], "novelctl 入口 surface 未提及 export")


if __name__ == "__main__":
    unittest.main()
