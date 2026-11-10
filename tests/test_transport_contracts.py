from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.core.transport_contract import MUTATING_OPERATIONS, READ_ONLY_OPERATIONS


ROOT = Path(__file__).resolve().parents[1]


class TransportContractTests(unittest.TestCase):
    def test_dead_entity_timespan_surface_is_absent_and_chapter_time_survives(self):
        from scripts.mcp_server import TOOLS, call_tool
        from scripts.core.operations import operations, call_operation
        from scripts.core.structure_store import StructureError
        names = {row['name'] for row in TOOLS}
        self.assertNotIn('preview_set_entity_timespan', names)
        self.assertIn('preview_set_chapter_timespan', names)
        specs = {spec.id for spec in operations()}
        self.assertNotIn('project.structure.entity-timespan-preview', specs)
        self.assertIn('project.structure.timespan-preview', specs)
        with self.assertRaises(StructureError) as caught:
            call_tool('preview_set_entity_timespan', {'projectRoot': str(ROOT)})
        self.assertEqual(caught.exception.code, 'TOOL_NOT_FOUND')
        with self.assertRaises(StructureError) as caught:
            call_operation('structure.batch-preview', ROOT, {'steps': [
                {'op': 'update-entity', 'selector': 'C-EXAMPLE', 'changes': {'name': '改名'}}]})
        self.assertEqual(caught.exception.code, 'ARGUMENTS_INVALID')
        for relative in ('scripts/dashboard_server.py',):
            self.assertNotIn('/api/entity/timespan/preview', (ROOT/relative).read_text())
        self.assertNotIn('id="open-mount"', (ROOT/'dashboard/index.html').read_text())

    def test_mcp_structure_tools_follow_shared_mutation_classes(self) -> None:
        from scripts.mcp_server import TOOLS
        from scripts.core.catalog_api import CATALOG_OPERATIONS
        from scripts.core.transport_contract import PREVIEW_OPERATIONS
        tools = {t["name"]: t for t in TOOLS}
        for name in MUTATING_OPERATIONS:
            self.assertIn(name, tools)
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])
        self.assertFalse(READ_ONLY_OPERATIONS)
        self.assertEqual(MUTATING_OPERATIONS - PREVIEW_OPERATIONS, {"commit_structure_plan", "undo_structure_operation"})
        self.assertTrue({s["name"] for s in CATALOG_OPERATIONS}.issubset(PREVIEW_OPERATIONS))

    def test_entrypoints_document_shared_core_and_mutation_boundaries(self) -> None:
        registry = json.loads((ROOT / "shared/registries/tools.json").read_text(encoding="utf-8"))
        entries = {item["id"]: item for item in registry["entrypoints"]}
        # 传输和作者任务入口都须登记；CLI/MCP 对齐不代表新增 dashboard 路由。
        self.assertEqual(set(entries), {"novelctl", "mcp-server", "dashboard-server", "manuscript-import",
                                        "external-manuscript-intake", "project-relocate"})
        self.assertIn("finalize-chapter", entries["novelctl"]["surface"])
        self.assertIn("HttpOnly", entries["dashboard-server"]["mutations"])

    def test_external_manuscript_intake_is_shared_and_keeps_its_evidence_boundary(self) -> None:
        """MCP 与 CLI 提供同一入库工具，证据副本仍不得成为可写真源。"""
        registry = json.loads((ROOT / "shared/registries/tools.json").read_text(encoding="utf-8"))
        entry = next(item for item in registry["entrypoints"]
                     if item["id"] == "external-manuscript-intake")
        self.assertIn('CLI', entry['transport'])
        self.assertIn('MCP', entry['transport'])
        self.assertNotIn('transport exception', entry['notes'])
        for key in ("transport", "audience", "surface", "mutations"):
            self.assertTrue(str(entry.get(key, "")).strip(), f"共享入口必须声明 {key}")
        self.assertIn('writableStoryTruthEnabled=false', entry['surface'])
        from scripts.mcp_server import TOOLS
        tools = {row['name']: row for row in TOOLS}
        self.assertIn('import_review', tools)
        self.assertFalse(tools['import_review']['annotations']['readOnlyHint'])
        self.assertEqual(set(tools['import_review']['inputSchema']['required']), {'source', 'targetRoot'})
        server = (ROOT / "scripts" / "dashboard_server.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/import", server)

    def test_project_relocate_is_shared_and_keeps_approval_and_device_boundaries(self) -> None:
        """移动目录经共同处理器执行，两端仍须批准且拒绝跨设备移动。"""
        registry = json.loads((ROOT / "shared" / "registries" / "tools.json").read_text(encoding="utf-8"))
        entry = next(item for item in registry["entrypoints"] if item["id"] == "project-relocate")
        self.assertIn('CLI', entry['transport'])
        self.assertIn('MCP', entry['transport'])
        self.assertNotIn('transport exception', entry['notes'])
        for key in ("transport", "audience", "surface", "mutations"):
            self.assertTrue(str(entry.get(key, "")).strip(), f"共享入口必须声明 {key}")
        self.assertIn("PROJECT_RELOCATE_CROSS_DEVICE_UNSUPPORTED", entry["mutations"])
        from scripts.mcp_server import TOOLS
        tools = {row['name']: row for row in TOOLS}
        self.assertIn('relocate', tools)
        self.assertFalse(tools['relocate']['annotations']['readOnlyHint'])
        self.assertTrue(tools['relocate']['annotations']['destructiveHint'])
        self.assertTrue({'operationId', 'previewHash', 'approve'} <= set(tools['relocate']['inputSchema']['properties']))
        server = (ROOT / "scripts" / "dashboard_server.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/relocate", server)


if __name__ == "__main__":
    unittest.main()
