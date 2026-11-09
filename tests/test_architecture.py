from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts.core.project import current_schema_version, workflow_policy
from scripts.core.structure_store import new_id

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"


def run_cli(args: list[str]) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(NOVELCTL), *args], cwd=ROOT, capture_output=True, text=True)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"code": "NON_JSON", "stdout": proc.stdout, "stderr": proc.stderr}


class VersionSourceTests(unittest.TestCase):
    def test_manifest_is_schema5_source(self) -> None:
        manifest = json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schemaVersion"], current_schema_version())
        from scripts.core.structure_store import WORKFLOW_VERSION
        self.assertEqual(WORKFLOW_VERSION, manifest["workflowVersion"])
        self.assertEqual(manifest['name'], 'govel')
        latest = re.search(r"^## (\S+) · 机制 \d+$", (ROOT/'docs/CHANGELOG.md').read_text(), re.M)
        self.assertIsNotNone(latest, "CHANGELOG 缺少当前发行条目")
        self.assertEqual(latest.group(1), manifest['version'])
        self.assertNotIn("Editor 当前机制版本 v15", (ROOT / "SKILL.md").read_text(encoding="utf-8"))

    def test_changelog_newest_entry_is_first(self) -> None:
        """CHANGELOG 追加在顶部；断言最新版本号，防止只在文末堆积而断言停在旧版。"""
        text = (ROOT / "docs/CHANGELOG.md").read_text(encoding="utf-8")
        versions = re.findall(r"^## .*机制 (\d+)$", text, flags=re.M)
        self.assertTrue(versions, "CHANGELOG 缺少机制版本条目")
        numbers = [int(item) for item in versions]
        self.assertEqual(numbers, sorted(numbers, reverse=True), "机制版本条目必须从新到旧排列")

    def test_old_project_is_not_runtime_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".govel").mkdir()
            (root / ".govel/project.json").write_text(json.dumps({"schemaVersion": 4, "workflowVersion": "4.0"}), encoding="utf-8")
            code, result = run_cli(["project", "doctor", "--project-root", str(root)])
            self.assertNotEqual(code, 0)
            self.assertEqual(result["code"], "DOCTOR_UNHEALTHY")


class WorkflowPolicyTests(unittest.TestCase):
    def test_policy_is_schema2_and_strict(self) -> None:
        policy = workflow_policy()
        self.assertEqual(policy["schemaVersion"], 2)
        self.assertEqual(policy["chapterLength"]["minHanCharacters"], 8500)
        self.assertFalse(policy["serialPlanning"]["prewarmNextChapter"])


class RuntimeModuleTests(unittest.TestCase):
    def test_novelctl_exposes_new_project_commands(self) -> None:
        completed = subprocess.run([sys.executable, str(NOVELCTL), "project", "--help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for name in ("import", "overview", "structure", "entity", "workflow"):
            self.assertIn(name, completed.stdout)

    def test_quick_init_creates_strict_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            code, result = run_cli(["project", "quick-init", "--project-root", str(root), "--book-name", "测试", "--no-agents-md"])
            self.assertEqual(code, 0, result)
            self.assertEqual(result["project"]["schemaVersion"], current_schema_version())
            structure = json.loads((root / ".govel" / "structure.json").read_text(encoding="utf-8"))
            self.assertEqual(structure["schemaVersion"], current_schema_version())
            for collection in ('attributes', 'fragments', 'fieldDefinitions', 'units', 'presentations'):
                self.assertEqual(structure[collection], [])
            self.assertNotIn("proseBinding", json.dumps(structure))
            # Current source truth does not require eager optional cache publication.
            self.assertEqual(result["doctor"]["stateCache"]["status"], "stale")
            self.assertEqual(result["doctor"]["stateCache"]["problems"], [])
            self.assertFalse((root / '.govel/cache/story-state.json').exists())


class ArchiveBoundaryTests(unittest.TestCase):
    def test_archive_declarations_are_explicit(self) -> None:
        for relative in ('docs/archive', 'scripts/archive', 'tests/archive', 'corpus'):
            self.assertFalse((ROOT / relative).exists())
        self.assertEqual(set(p.name for p in (ROOT / 'knowledge').iterdir() if p.is_file()),
                         {'README.md', 'schema.json', 'example.json'})


if __name__ == "__main__":
    unittest.main()
