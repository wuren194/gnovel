"""Every executable entrypoint must be declared in the tool registry.

动机：`scripts/mcp_server.py`（stdio MCP）与 `scripts/dashboard_server.py` + `dashboard/`
（loopback HTTP）是与 novelctl 平级的传输通道，都直接调用 scripts/core/，却既不在
tools.json、也不在 ARCHITECTURE.md 里——它们绕过哪些边界、暴露多少 surface 无处可查。
未登记的入口是影子架构，本测试把登记变成硬要求。
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "shared" / "registries" / "tools.json"
SCRIPTS = ROOT / "scripts"

# 库模块：被入口导入，自身不是入口（无 __main__ 卫哨即由本测试自动判定）。
EXEMPT = {"__init__.py"}


def has_main_guard(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            return True
    return False


class RegistryShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_registry_declares_entrypoints_and_tools(self) -> None:
        self.assertEqual(self.registry["schemaVersion"], 3)
        self.assertIsInstance(self.registry["entrypoints"], list)
        self.assertIsInstance(self.registry["tools"], list)
        self.assertTrue(self.registry["entrypoints"], "entrypoints 不得为空")

    def test_every_declared_path_exists(self) -> None:
        for group in ("entrypoints", "tools"):
            for item in self.registry[group]:
                with self.subTest(group=group, id=item["id"]):
                    self.assertTrue((ROOT / item["path"]).is_file(), f"{item['path']} 不存在")

    def test_entrypoints_declare_transport_surface_and_mutations(self) -> None:
        """入口的价值在于说清它暴露什么、能改什么；缺字段等于没登记。"""
        for item in self.registry["entrypoints"]:
            with self.subTest(id=item["id"]):
                for key in ("id", "path", "transport", "audience", "surface", "mutations"):
                    self.assertTrue(str(item.get(key, "")).strip(), f"缺少 {key}")

    def test_ids_are_unique_across_groups(self) -> None:
        ids = [item["id"] for group in ("entrypoints", "tools") for item in self.registry[group]]
        self.assertEqual(len(ids), len(set(ids)), "注册表存在重复 id")


class EntrypointCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        self.declared = {
            item["path"]
            for group in ("entrypoints", "tools")
            for item in self.registry[group]
        }

    def test_every_runnable_script_is_declared(self) -> None:
        undeclared = []
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in EXEMPT or not has_main_guard(path):
                continue
            relative = str(path.relative_to(ROOT))
            if relative not in self.declared:
                undeclared.append(relative)
        self.assertEqual(
            undeclared, [], "可执行脚本未登记 tools.json（entrypoints 或 tools 二者之一）"
        )

    def test_known_transports_are_registered(self) -> None:
        by_id = {item["id"]: item for item in self.registry["entrypoints"]}
        for expected in ("novelctl", "mcp-server", "dashboard-server"):
            self.assertIn(expected, by_id, f"入口 {expected} 未登记")

    def test_dashboard_static_assets_exist(self) -> None:
        entry = next(
            item for item in self.registry["entrypoints"] if item["id"] == "dashboard-server"
        )
        self.assertIn("dashboard/", entry["notes"])
        # This distribution exposes the JSON service and a small landing page.
        for asset in ("index.html",):
            self.assertTrue((ROOT / "dashboard" / asset).is_file(), f"dashboard/{asset} 缺失")

    def test_architecture_doc_describes_every_entrypoint(self) -> None:
        text = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
        for item in self.registry["entrypoints"]:
            with self.subTest(id=item["id"]):
                self.assertIn(item["path"], text, f"ARCHITECTURE.md 未说明 {item['path']}")


class BoundaryClaimTests(unittest.TestCase):
    """平级通道不得绕过事务与批准边界。"""

    def test_mutating_transports_require_explicit_approval(self) -> None:
        for relative in ("scripts/mcp_server.py", "scripts/dashboard_server.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(script=relative):
                self.assertIn("commit_plan", source, "变更必须走 commit_plan 事务")
                self.assertIn("planHash", source, "commit 必须校验 planHash")

    def test_dashboard_binds_loopback_only(self) -> None:
        source = (ROOT / "scripts/dashboard_server.py").read_text(encoding="utf-8")
        self.assertIn('choices=["127.0.0.1", "localhost"]', source, "dashboard 必须只绑定 loopback")
        self.assertIn("compare_digest", source, "变更令牌必须常量时间比较")


if __name__ == "__main__":
    unittest.main()
