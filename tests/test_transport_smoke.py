from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.core.catalog_api import CATALOG_OPERATIONS, snake_case
from scripts.core.jsonio import write_json
from scripts.core.structure_store import load_structure, save_structure
from scripts.dashboard_server import DashboardHandler
from tests.fixtures_current import ROOT, create_current_project


class QuietHandler(DashboardHandler):
    def log_message(self, *_):
        pass


class TransportSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_current_project(self.root)
        structure = load_structure(self.root)
        structure["volumes"].append({"volumeId": "volume-SPARE-ID", "ordinal": 2, "title": "备用卷", "status": "planned"})
        structure["arcs"].append({"arcId": "arc-SPARE-ID", "title": "备用弧线", "kind": "subplot", "status": "planned", "volumeIds": [], "parentArcId": None})
        structure["entities"].append({"entityId": "prop-ARCHIVED-ID", "entityType": "prop", "name": "归档物件", "aliases": [], "status": "archived", "summary": "恢复预览用", "sourceRefs": []})
        save_structure(self.root, structure)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.project_root = str(self.root)
        self.server.mutation_token = "smoke-test-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def cli(self, action, arguments, *, error=None):
        command = [sys.executable, str(ROOT / "scripts/novelctl.py"), "project", "structure", action, "--project-root", str(self.root)]
        for key, value in arguments.items():
            command.append("--" + snake_case(key).replace("_", "-"))
            if value is True:
                continue
            if isinstance(value, list):
                command.extend(value)
            else:
                command.append(json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value))
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=20)
        payload = json.loads(result.stdout)
        if error is None:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(payload['code'], error)
        return payload

    def mcp(self, name, arguments, *, error=None):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {"projectRoot": str(self.root), **arguments}}},
        ]
        result = subprocess.run([sys.executable, str(ROOT / "scripts/mcp_server.py")], input="\n".join(json.dumps(r) for r in requests) + "\n", cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2])
        payload = responses[1]["result"]
        if error is None:
            self.assertFalse(payload['isError'], payload)
        else:
            self.assertTrue(payload['isError'], payload)
            self.assertEqual(payload['structuredContent']['code'], error)
        return payload['structuredContent']

    def http(self, path, arguments, *, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.server.server_port}"}
        if authorized:
            # 机制版本 104：会话签发需要合法 Host + 同源 Origin / Fetch Metadata。
            connection.request("GET", "/api/config", headers={"Origin": headers["Origin"]})
            config = connection.getresponse()
            headers["Cookie"] = config.getheader("Set-Cookie").split(";", 1)[0]
            config.read()
        connection.request("POST", path, json.dumps(arguments), headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, payload

    def arguments(self, name):
        volume = self.fixture["structure"]["volumes"][0]["volumeId"]
        arc = self.fixture["structure"]["arcs"][0]["arcId"]
        return {
            "preview_create_entity": {"entityType": "location", "name": "新码头", "aliases": ["旧渡口"], "summary": "候选地点"},
            "preview_archive_entity": {"selector": self.fixture["characterId"]},
            "preview_create_volume": {"title": "新卷"},
            "preview_update_volume": {"volumeId": volume, "changes": {"title": "更新卷名"}},
            "preview_archive_volume": {"volumeId": "volume-SPARE-ID"},
            "preview_create_arc": {"title": "新弧线", "volumeIds": [volume]},
            "preview_update_arc": {"arcId": arc, "changes": {"promise": "新承诺"}},
            "preview_archive_arc": {"arcId": "arc-SPARE-ID"},
            "preview_relink_chapter": {"chapterId": self.fixture["chapterIds"][0], "volumeId": volume, "primaryArcId": arc},
            "preview_release_quarantined_chapter": {"chapterId": self.fixture["chapterIds"][0],
                "reason": "作者复核边界后解除隔离"},
            "preview_rename_book": {"newName": "烟波引"},
            "preview_rename_chapter": {"chapterId": self.fixture["chapterIds"][0], "newTitle": "第一章改名"},
            "preview_relocate_chapter_paths": {"chapterId": self.fixture["chapterIds"][1], "filePath": "正文/改名.md"},
            "preview_rename_entity": {"selector": self.fixture["characterId"], "newName": "沈砚之", "keepOldNameAsAlias": True},
            "preview_alias_entity": {"selector": self.fixture["propId"], "aliases": ["柜钥匙"]},
            "preview_restore_chapter": {"chapterId": self.fixture["chapterIds"][0], "status": "planned"},
            "preview_restore_entity": {"selector": "prop-ARCHIVED-ID"},
            "preview_set_visibility": {"collection": "chapters", "recordId": self.fixture["chapterIds"][0], "visibleTo": [self.fixture["characterId"]]},
        }[name]

    # Unit operations use a dedicated current-format fixture with typed members.
    UNIT_SPECS = {"preview_rename_unit", "preview_reparent_unit"}

    def test_all_catalog_previews_have_real_cli_stdio_http_routes(self):
        original = load_structure(self.root)
        for spec in CATALOG_OPERATIONS:
            if spec["name"] in self.UNIT_SPECS:
                continue
            with self.subTest(operation=spec["name"]):
                expected_structure = original
                if spec["name"] == "preview_release_quarantined_chapter":
                    from scripts.core.jsonio import write_json
                    quarantined = json.loads(json.dumps(original))
                    chapter = next(row for row in quarantined['chapters']
                                   if row['chapterId'] == self.fixture['chapterIds'][0])
                    chapter.update(status='quarantined', quarantineReason='smoke quarantine',
                                   quarantineCategory='content_boundary')
                    write_json(self.root / '.govel/structure.json', quarantined)
                    expected_structure = quarantined
                arguments = self.arguments(spec["name"])
                cli = self.cli(spec["cli"], arguments)
                mcp = self.mcp(spec["name"], arguments)
                status, http = self.http(spec["route"], arguments)
                self.assertEqual(status, 200, http)
                for plan in (cli, mcp, http):
                    self.assertIs(plan['ok'], True)
                    self.assertEqual(plan['code'], 'SOURCE_CHANGE_PREVIEW')
                    self.assertTrue(plan["requiresApproval"])
                    expected_operation = spec["name"].removeprefix("preview_")
                    # 作者重构操作在收据里保留 refactor_ 前缀，以区分普通结构写入。
                    if not plan["operation"] in (expected_operation, f"refactor_{expected_operation}"):
                        self.fail(f'{plan["operation"]} != {expected_operation}')
                    self.assertTrue((self.root / f".govel/plans/{plan['operationId']}.json").is_file())
                self.assertEqual(load_structure(self.root), expected_structure)
                denied, payload = self.http(spec["route"], arguments, authorized=False)
                # 机制版本 104：无凭据统一 401 SESSION_REQUIRED（旧契约 403）。
                self.assertEqual(denied, 401, payload)
                self.assertEqual(payload["code"], "SESSION_REQUIRED")
                if spec["name"] == "preview_release_quarantined_chapter":
                    from scripts.core.jsonio import write_json
                    write_json(self.root / '.govel/structure.json', original)

    def test_pending_plan_keeps_identical_id_and_hash_in_cli_stdio_and_http(self):
        preview = self.cli('book-rename-preview', {'newName': '冻结预览'})
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', 'structure',
                                  'plans', '--project-root', str(self.root)], cwd=ROOT,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        cli = json.loads(process.stdout)['plans']
        mcp = self.mcp('list_pending_plans', {})['plans']
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        origin = f'http://127.0.0.1:{self.server.server_port}'
        connection.request('GET', '/api/config', headers={'Origin': origin})
        response = connection.getresponse(); cookie = response.getheader('Set-Cookie').split(';', 1)[0]; response.read()
        connection.request('GET', '/api/plans', headers={'Cookie': cookie, 'Origin': origin})
        response = connection.getresponse(); self.assertEqual(response.status, 200)
        http_plans = json.loads(response.read())['plans']
        for rows in (cli, mcp, http_plans):
            plan = next(row for row in rows if row['operationId'] == preview['operationId'])
            self.assertEqual(plan['planHash'], preview['planHash'])

    def test_unit_refactor_previews_have_real_cli_stdio_http_routes(self):
        from tests.fixtures_refactor import refactor_project
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "书"
        info = refactor_project(root)
        self.server.project_root = str(root)
        self.root = root
        specs = {spec["name"]: spec for spec in CATALOG_OPERATIONS}
        original = load_structure(root)
        for name, arguments in (
            ("preview_rename_unit", {"unitId": info["unit"], "newTitle": "新单元"}),
            ("preview_reparent_unit", {"unitId": info["unit"], "placement": {"scope": "project"}}),
        ):
            with self.subTest(operation=name):
                spec = specs[name]
                cli = self.cli(spec["cli"], arguments)
                mcp = self.mcp(name, arguments)
                status, http = self.http(spec["route"], arguments)
                self.assertEqual(status, 200, http)
                for plan in (cli, mcp, http):
                    self.assertTrue(plan["requiresApproval"])
                    self.assertEqual(plan["operation"], "refactor_" + name.removeprefix("preview_"))
                    self.assertTrue((root / f".govel/plans/{plan['operationId']}.json").is_file())
                self.assertEqual(load_structure(root), original)

    def test_each_transport_completes_preview_commit_undo(self):
        spec = CATALOG_OPERATIONS[0]
        for transport in ("cli", "mcp", "http"):
            with self.subTest(transport=transport):
                arguments = {"entityType": "character", "name": "新人物"}
                if transport == "cli":
                    plan = self.cli(spec["cli"], arguments)
                    result = self.cli("commit", {"operationId": plan["operationId"], "planHash": plan["planHash"], "approve": True})
                elif transport == "mcp":
                    plan = self.mcp(spec["name"], arguments)
                    result = self.mcp("commit_structure_plan", {"operationId": plan["operationId"], "planHash": plan["planHash"], "approve": True})
                else:
                    _, plan = self.http(spec["route"], arguments)
                    status, result = self.http("/api/plan/commit", {"operationId": plan["operationId"], "planHash": plan["planHash"], "approve": True})
                    self.assertEqual(status, 200, result)
                self.assertEqual(result["code"], "STRUCTURE_COMMITTED")
                self.assertIn(plan["parameters"]["entityId"], {e["entityId"] for e in load_structure(self.root)["entities"]})
                if transport == "cli":
                    self.cli("undo", {"operationId": plan["operationId"]})
                elif transport == "mcp":
                    self.mcp("undo_structure_operation", {"operationId": plan["operationId"]})
                else:
                    status, result = self.http("/api/operation/undo", {"operationId": plan["operationId"]})
                    self.assertEqual(status, 200, result)
                self.assertNotIn(plan["parameters"]["entityId"], {e["entityId"] for e in load_structure(self.root)["entities"]})

    def test_cli_move_and_entity_rename_use_current_routes(self):
        self.cli('move-preview', {'chapterId': self.fixture['chapterIds'][0]})
        before = (self.root/'.govel/structure.json').read_bytes()
        self.cli('entity-rename-preview', {'selector': self.fixture['characterId'],
                 'newName': '新名字', 'keepOldNameAsAlias': True})
        self.assertEqual((self.root/'.govel/structure.json').read_bytes(), before)

    def test_shared_catalog_schema_rejects_bad_arguments_without_plan(self):
        before = set((self.root / ".govel/plans").iterdir())
        for args in ({"entityType": "character"}, {"entityType": "character", "name": []}, {"entityType": "character", "name": "新名", "unexpected": True}):
            status, payload = self.http("/api/entity/create/preview", args)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "ARGUMENTS_INVALID")
        self.assertEqual(set((self.root / ".govel/plans").iterdir()), before)
    def test_http_receipt_uses_explicit_title_options(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        origin = {"Origin": f"http://127.0.0.1:{self.server.server_port}"}
        connection.request("GET", "/api/config", headers=origin)
        config = connection.getresponse()
        cookie = {"Cookie": config.getheader("Set-Cookie").split(";", 1)[0]}
        config.read()
        chapter_id = self.fixture["chapterIds"][0]
        for suffix, expected in (("skipTitle=true", {"titleCandidateId": None, "skipTitle": True}),
                                 ("titleCandidateId=title-1", {"titleCandidateId": "title-1", "skipTitle": False})):
            connection.request("GET", f"/api/workflow/receipt?chapterId={chapter_id}&{suffix}", headers=cookie)
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 200, payload)
            self.assertEqual(payload["finalizationOptions"], expected)
        connection.request("GET", f"/api/workflow/receipt?chapterId={chapter_id}&skipTitle=maybe", headers=cookie)
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
