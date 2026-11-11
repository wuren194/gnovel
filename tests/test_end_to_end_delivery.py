"""End-to-end delivery acceptance for the P7 toolbox closure.

These are not helper unit tests. Each one drives a real transport boundary:

* CLI  — ``python3 scripts/novelctl.py`` in a subprocess
* MCP  — ``python3 scripts/mcp_server.py`` over stdio, line-delimited JSON-RPC
* HTTP — ``scripts/dashboard_server.py`` bound to a real loopback socket

and each one follows a whole author workflow (Knowledge → Context, Candidate →
Chapter, Canon → Impact, External Manuscript, Doctor
attribution) rather than a single function.

The Doctor section is the load-bearing one: it must attribute each deliberately
broken thing to the right domain, and must not collapse "Canon Ready but
Writer Ready is not" into one red light.
"""

from __future__ import annotations

from scripts.core.project import current_schema_version

import http.client
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts.core.catalog_api import snake_case
from scripts.core.jsonio import write_json
from scripts.core.structure_store import load_structure, new_id, save_structure
from scripts.dashboard_server import DashboardHandler
from scripts.mcp_server import handle as mcp_handle
from tests.fixtures_source import single_source_chapter, source_fixture
from tests.cli_harness import run_cli as in_process_cli

ROOT = Path(__file__).resolve().parents[1]
NOVELCTL = ROOT / "scripts" / "novelctl.py"
MCP_SERVER = ROOT / "scripts" / "mcp_server.py"


class QuietHandler(DashboardHandler):
    def log_message(self, *_):
        pass


class TransportHarness(unittest.TestCase):
    """Shared CLI / MCP / HTTP wiring over one throwaway project."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.server.project_root = str(self.root)
        self.server.mutation_token = "acceptance-token"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- CLI ---------------------------------------------------------------
    def _doctor_flags(self, arguments):
        flags = []
        for key, value in arguments.items():
            flags.extend(["--" + snake_case(key).replace("_", "-"), str(value)])
        return flags

    def preview(self, *arguments):
        """Preview through the CLI, then commit through the real structure transaction."""
        code, payload = self.cli(*arguments)
        self.assertEqual(code, 0, payload)
        self.assertIn("operationId", payload)
        self.assertIn("planHash", payload)
        code, committed = self.cli("structure", "commit", "--operation-id", payload["operationId"],
                                   "--plan-hash", payload["planHash"], "--approve")
        self.assertEqual(code, 0, committed)
        return code, payload

    def cli(self, *arguments, expect=None):
        # TransportParityTests protects real exit/stdio/HTTP classification.
        # Domain and workflow cases exercise the same parser/main in-process.
        proc = (subprocess.run([sys.executable, str(NOVELCTL), "project", *arguments,
                               "--project-root", str(self.root)],
                              cwd=ROOT, text=True, capture_output=True, timeout=120)
                if isinstance(self, (TransportParityTests, DoctorAttributionTests)) else
                in_process_cli(['project', *arguments, '--project-root', str(self.root)]))
        payload = {}
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {"code": "NON_JSON", "stdout": proc.stdout, "stderr": proc.stderr}
        if expect is not None:
            self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc.returncode, payload

    # -- MCP ---------------------------------------------------------------
    def mcp(self, name, arguments):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": {"projectRoot": str(self.root), **arguments}}}
        response = mcp_handle(request)
        self.assertIn("result", response, response)
        return response["result"]

    @staticmethod
    def mcp_report(result):
        """MCP 把失败结果整包放进 structuredContent.details；成功时就是结果本身。

        返回 (isError, report)，因为调用方要同时断言效果分类和核心 result。
        """
        payload = result["structuredContent"]
        report = payload["details"] if isinstance(payload.get("details"), dict) else payload
        return result.get("isError", False), report

    def mcp_subprocess(self, name, arguments):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name,
                                         "arguments": {"projectRoot": str(self.root), **arguments}}})
        proc = subprocess.run([sys.executable, str(MCP_SERVER)], input=request + "\n",
                              text=True, capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.splitlines()[0])["result"]

    # -- HTTP --------------------------------------------------------------
    def http(self, method, path, payload=None, headers=None):
        # 机制版本 104 起 /api 全部需要会话：先用同源 Origin 引导会话 cookie，
        # 之后的请求自动携带（显式传 Cookie 的调用方不受影响）。
        if getattr(self, "_session_cookie", None) is None and path != "/api/config":
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
            conn.request("GET", "/api/config", headers={
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
                "X-Project-Root": str(self.root)})
            bootstrap = conn.getresponse()
            set_cookie = bootstrap.getheader("Set-Cookie") or ""
            bootstrap.read()
            conn.close()
            if set_cookie:
                self._session_cookie = set_cookie.split(";", 1)[0]
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        body = json.dumps(payload).encode() if payload is not None else None
        combined = {"Content-Type": "application/json",
                    "X-Project-Root": str(self.root), **(headers or {})}
        if getattr(self, "_session_cookie", None) and "Cookie" not in combined:
            combined["Cookie"] = self._session_cookie
        conn.request(method, path, body=body, headers=combined)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except json.JSONDecodeError:
            return response.status, {}


class SingleSourceProjectTests(TransportHarness):
    """A current-format project with one volume, one arc and a materialized chapter."""

    def setUp(self):
        super().setUp()
        (self.root / ".govel").mkdir()
        source = source_fixture(self.root)
        write_json(self.root / ".govel/project.json", {
            "schemaVersion": current_schema_version(), "workflowVersion": '6.0', "projectId": source["projectId"],
            "bookName": source["bookName"], "approvalMode": "manual"})
        self.volume = source['volumes'][0]['volumeId']
        self.arc = source['arcs'][0]['arcId']
        from scripts.core.structure_commands import preview_insert_chapter, commit_plan
        preview = preview_insert_chapter(self.root, title='开场', volume_id=self.volume, primary_arc_id=self.arc)
        committed = commit_plan(self.root, operation_id=preview['operationId'],
                                plan_hash=preview['planHash'], approved=True)
        self.chapter = committed['chapter']['chapterId']
        from scripts import state_cache
        state_cache.rebuild(self.root)

    def doctor(self, **arguments):
        code, payload = self.cli("doctor", *self._doctor_flags(arguments))
        return code, payload


class TransportParityTests(SingleSourceProjectTests):
    """The same operation must agree on result, error code and effect class.

    Parity is about the *classification*, not the envelope: a blocking doctor must
    be a non-zero CLI exit, an MCP `isError`, and an HTTP 409 at the same time,
    with identical issue counts and identical per-domain status underneath.
    """

    def _doctor_on_all_three(self, **arguments):
        code, cli_payload = self.cli("doctor", *self._doctor_flags(arguments))
        cli_doctor = cli_payload.get("details", cli_payload) if code != 0 else cli_payload
        mcp_is_error, mcp_payload = self.mcp_report(self.mcp("doctor", arguments))
        status, http_payload = self.http("GET", "/api/doctor")
        return code, cli_doctor, mcp_is_error, mcp_payload, status, http_payload

    def test_doctor_is_identical_on_cli_mcp_and_http(self):
        code, cli_doctor, mcp_is_error, mcp_payload, status, http_payload = self._doctor_on_all_three()
        self.assertEqual(set(cli_doctor["domains"]),
                         {"project", "planning", "writerReadiness", "canon",
                          "context", "knowledge", "workflow", "tooling"})

        # 效果分类三通道一致：阻断 = CLI 非零 + MCP isError + HTTP 409。
        self.assertEqual(code != 0, not cli_doctor["healthy"])
        self.assertEqual(mcp_is_error, not cli_doctor["healthy"])
        self.assertEqual(status, 200 if cli_doctor["healthy"] else 409)
        self.assertTrue(http_payload["ok"] if cli_doctor["healthy"] else not http_payload["ok"])

        # 核心 result 一致（允许 transport envelope 不同）。
        self.assertEqual(mcp_payload["issueCounts"],
                         cli_doctor["issueCounts"], "CLI 与 MCP 的核心 result 必须一致")
        self.assertEqual(http_payload["doctor"]["issueCounts"], cli_doctor["issueCounts"])
        self.assertEqual(http_payload["doctor"]["domains"]["writerReadiness"]["status"],
                         cli_doctor["domains"]["writerReadiness"]["status"])
        self.assertEqual(mcp_payload["code"], cli_doctor["code"])
        self.assertEqual(http_payload["doctor"]["code"], cli_doctor["code"])

    def test_healthy_doctor_is_a_success_on_all_three(self):
        """一个健康的项目必须在三通道都是成功，否则 parity 只是错得一致。"""
        with tempfile.TemporaryDirectory() as temp:
            self.root = Path(temp).resolve()
            self.server.project_root = str(self.root)
            (self.root / ".govel").mkdir()
            source = source_fixture(self.root)
            write_json(self.root / ".govel/project.json", {
                "schemaVersion": current_schema_version(), "workflowVersion": '6.0',
                "projectId": source["projectId"], "bookName": source["bookName"],
                "approvalMode": "manual"})
            save_structure(self.root, source)
            code, cli_payload = self.cli("doctor")
            self.assertEqual(code, 0, cli_payload)
            self.assertEqual(cli_payload["code"], "DOCTOR_OK")
            self.assertTrue(cli_payload["healthy"])
            mcp_payload = self.mcp("doctor", {})
            self.assertFalse(mcp_payload["isError"], mcp_payload)
            status, http_payload = self.http("GET", "/api/doctor")
            self.assertEqual(status, 200)
            self.assertTrue(http_payload["ok"])
            self.assertEqual(http_payload["doctor"]["issueCounts"], cli_payload["issueCounts"])

    def test_doctor_agrees_over_a_real_stdio_mcp_subprocess(self):
        payload = self.mcp_subprocess("doctor", {})
        code, cli_payload = self.cli("doctor")
        cli_doctor = cli_payload.get("details", cli_payload) if code != 0 else cli_payload
        report = payload["structuredContent"].get("details", payload["structuredContent"])
        self.assertIn('healthy', cli_doctor, cli_payload)
        self.assertIn('healthy', report, payload)
        self.assertEqual(payload["isError"], not cli_doctor["healthy"])
        self.assertEqual(report["issueCounts"], cli_doctor["issueCounts"])
        self.assertEqual(report["domains"]["writerReadiness"]["status"],
                         cli_doctor["domains"]["writerReadiness"]["status"])

    def test_read_only_annotations_match_across_transports(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        tools = {row["name"]: row for row in mcp_handle(request)["result"]["tools"]}
        doctor = tools["doctor"]
        self.assertTrue(doctor["annotations"]["readOnlyHint"])
        self.assertFalse(doctor["annotations"]["destructiveHint"])
        # readOnlyHint 不是装饰：doctor 是只读域报告，但机制版本 104 起仍需本地会话
        # （敏感读取统一设防）；它不需要的是 mutation 语义，不会返回 token 类错误码。
        status, payload = self.http("GET", "/api/doctor", headers={"Cookie": ""})
        self.assertIn(status, {200, 401, 409}, payload)
        self.assertNotEqual(payload.get("code"), "MUTATION_TOKEN_REQUIRED")

    def test_error_code_is_shared_not_reworded_per_transport(self):
        """同一失败在三通道必须是同一个 code；措辞可以不同，归属不能不同。"""
        code, cli_payload = self.cli("doctor", *self._doctor_flags({"chapterId": "chapter-DOES-NOT-EXIST"}))
        cli_doctor = cli_payload.get("details", cli_payload)
        _, mcp_payload = self.mcp_report(self.mcp("doctor", {"chapterId": "chapter-DOES-NOT-EXIST"}))
        status, http_payload = self.http("GET", "/api/doctor?chapterId=chapter-DOES-NOT-EXIST")
        self.assertIn("CHAPTER_NOT_FOUND",
                      {row["code"] for row in cli_doctor["issues"]},
                      "不存在的章必须归到 writerReadiness 的 CHAPTER_NOT_FOUND，而不是笼统失败")
        self.assertEqual(cli_doctor["domains"]["writerReadiness"]["status"], "blocked")
        self.assertEqual(mcp_payload["code"], cli_doctor["code"])
        self.assertEqual(http_payload["doctor"]["code"], cli_doctor["code"])


class DoctorAttributionTests(SingleSourceProjectTests):
    """Deliberately break one thing at a time; the right domain must light up."""

    def setUp(self):
        super().setUp()
        # `knowledge` 必须真的健康，否则「本章计划缺失不该染红资料域」这条断言在
        # 没有本地索引的检出上会假失败。这里只重建本模块独占副本的派生物。
        from scripts.core.knowledge_index import rebuild_index
        rebuild_index()
        # tooling 域会读本机真实的 Antigravity 安装（~/.gemini 下的软链）：在主检出以外的
        # worktree 里跑，或本机安装落后于仓库，就报与本章无关的漂移。给 doctor 子进程一个
        # 空 HOME，tooling 域只剩「本机未安装该客户端」这条提示。
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": home.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def doctor_domains(self, **arguments):
        code, payload = self.doctor(**arguments)
        if code != 0 and "details" in payload:
            payload = payload["details"]
        return payload

    def test_stale_context_and_missing_plan_are_separate_domains(self):
        report = self.doctor_domains()
        # 本章没有计划材料：可开工域必须说「待作者决定」——不是「引擎坏了」。
        # 作者下一步是去补规划，不是去修脚本。
        self.assertEqual(report["domains"]["writerReadiness"]["status"], "author_decision_required")
        self.assertEqual(report["domains"]["writerReadiness"]["issues"][0]["status"],
                         "author_decision_required")
        context_codes = {row["code"] for row in report["domains"]["context"]["issues"]}
        self.assertIn("CONTEXT_CHAPTER_PLAN_MISSING", context_codes)
        # 而项目骨架、工作流、资料、安装都不该被这一件事染红。
        # Missing plan belongs to the authoring/context domains, not project corruption.
        self.assertEqual(report['domains']['project']['status'], 'healthy')
        self.assertEqual(report['domains']['project']['issues'], [])
        for domain in ("workflow", "knowledge"):
            self.assertEqual(report["domains"][domain]["status"], "healthy",
                             f"{domain} 不该被本章计划缺失染红")
        self.assertIn(report["domains"]["tooling"]["status"], {"healthy", "informational"})
        self.assertNotIn(report["domains"]["tooling"]["status"], {"needs_rebuild", "blocked", "stale"},
                         "安装与工具域的异常必须源于工具链本身，不是本章计划")

    def test_stale_knowledge_index_is_its_own_domain(self):
        from scripts.core.knowledge_index import INDEX
        INDEX.write_bytes(b"not a sqlite database")
        report = self.doctor_domains()
        self.assertEqual(report["domains"]["knowledge"]["status"], "needs_rebuild")
        codes = {row["code"] for row in report["domains"]["knowledge"]["issues"]}
        self.assertIn("KNOWLEDGE_INDEX_STALE", codes)
        self.assertEqual(report['domains']['project']['status'], 'healthy')
        self.assertEqual(report['domains']['project']['issues'], [])

    def test_pending_transaction_is_reported_as_needs_rebuild(self):
        directory = self.root / ".govel" / "transactions"
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "TX-STUCK.json", {
            "schemaVersion": 2, "transactionId": "TX-STUCK", "chapterId": self.chapter,
            "phase": "prepared", "files": []})
        report = self.doctor_domains()
        self.assertEqual(report["domains"]["workflow"]["status"], "needs_rebuild")
        codes = {row["code"] for row in report["domains"]["workflow"]["issues"]}
        self.assertIn("TRANSACTION_INCOMPLETE", codes)
        blocker = next(row for row in report["domains"]["workflow"]["issues"]
                       if row["code"] == "TRANSACTION_INCOMPLETE")
        self.assertEqual(blocker["recommendedAction"],
                         "运行 project workflow recover 查看，由作者确认后 --apply 回滚")
        self.assertIn("project.workflow.recover", blocker["affectedOperations"])

    def test_unplaced_candidate_is_an_author_decision_not_a_mechanical_error(self):
        structure = load_structure(self.root)
        structure["planningCandidates"] = [{
            "candidateId": "candidate-UNPLACED", "title": "还没有定位", "summary": "未定卷与弧线",
            "placement": None, "primaryArcId": None, "secondaryArcIds": [],
            "beforeChapterId": None, "status": "draft"}]
        save_structure(self.root, structure)
        report = self.doctor_domains()
        self.assertEqual(report["domains"]["planning"]["status"], "author_decision_required")
        issue = next(row for row in report["domains"]["planning"]["issues"]
                     if row["code"] == "PLANNING_CANDIDATE_UNPLACED")
        self.assertEqual(issue["status"], "author_decision_required")
        self.assertEqual(issue["candidateId"], "candidate-UNPLACED")
        self.assertEqual(report["lifecycleObjects"]["planningCandidates"]["unplaced"], ["candidate-UNPLACED"])
        # An unplaced planning candidate does not corrupt the current project source.
        self.assertEqual(report['domains']['project']['issues'], [])
        self.assertNotIn("PLANNING_CANDIDATE_UNPLACED",
                         {row["code"] for row in report["domains"]["project"]["issues"]})

    def test_every_issue_carries_the_full_contract(self):
        report = self.doctor_domains()
        required = {"code", "domain", "severity", "status", "objectType", "objectId", "message",
                    "evidence", "affectedOperations", "recommendedAction"}
        for issue in report["issues"]:
            with self.subTest(code=issue["code"]):
                self.assertTrue(required <= set(issue), required - set(issue))
                self.assertIn(issue["severity"], {"blocking", "warning", "informational"})
                self.assertIn(issue["status"], {"mechanical_error", "stale", "needs_rebuild",
                                                "author_decision_required",
                                                "semantic_review_required", "informational"})
                self.assertIsInstance(issue["evidence"], dict)



class WriterReadinessConsumesTaskPrepareTests(SingleSourceProjectTests):
    """writerReadiness.ready must BE task_prepare(writer).readiness.taskReady.

    The doctor is an aggregator: it consumes the module that already owns the
    writer-open rule (task_prepare) instead of keeping a second plan+voice gate.
    These run assess_project in-process so a patched prepare_task is observable
    (the CLI subprocess would not see the patch).
    """

    def assess(self, **kwargs):
        from scripts.core.project_doctor import assess_project
        return assess_project(self.root, **kwargs)

    def test_writer_ready_is_parity_with_task_prepare_taskReady(self):
        from scripts.core.task_prepare import prepare_task
        report = self.assess()
        scoped = report["scopedChapterId"]
        prep = prepare_task(self.root, "writer", chapter_id=scoped)
        wr = report["domains"]["writerReadiness"]
        self.assertEqual(wr["ready"], prep["readiness"]["taskReady"],
                         "Doctor writerReadiness.ready 必须等于 task_prepare(writer).readiness.taskReady")
        self.assertFalse(wr["ready"], "全新单源章还没批准范围/Stage A，不该可开工")
        self.assertEqual(wr["readinessSource"], "task_prepare(writer)")

    def test_ready_follows_taskReady_not_a_local_plan_gate(self):
        """真源里本章计划未就绪；若 prepare_task 说 taskReady=True，doctor 必须也 ready=True。

        旧实现从本地 plan+voice 公式推 ready，这里 plan 未就绪会给出 False；证明 ready
        已改由 task_prepare 决定，而不是第二套门。
        """
        from scripts.core import task_prepare as tp_mod
        from unittest.mock import patch
        ready_flags = {"structureHealthy": True, "settingsReady": True, "contextReady": True,
                       "authorizationReady": True, "scopeReady": True, "taskReady": True}
        fake = {"readiness": ready_flags, "blocking": [], "missing": []}
        with patch.object(tp_mod, "prepare_task", return_value=fake):
            report = self.assess()
        self.assertTrue(report["domains"]["writerReadiness"]["ready"],
                        "ready 必须来自 task_prepare 的 taskReady，而不是本地 plan+voice 公式")

    def test_blockers_are_surfaced_and_classified_by_kind(self):
        from scripts.core import task_prepare as tp_mod
        from unittest.mock import patch
        codes = {"STAGE_A_NOT_APPROVED": "author_decision_required",
                 "CHAPTER_OUTSIDE_APPROVED_RANGE": "author_decision_required",
                 "SOURCE_RULES_BLOCKED": "semantic_review_required",
                 "CONTINUITY_REQUIRED": "author_decision_required",
                 "CONTEXT_HEALTH_BLOCKED": "needs_rebuild",
                 "CHAPTER_NOT_FOUND": "mechanical_error"}
        blocking = [{"code": code, "message": code, "details": {}} for code in codes]
        fake = {"readiness": {"taskReady": False, "structureHealthy": True, "settingsReady": True,
                              "contextReady": False, "authorizationReady": False, "scopeReady": True},
                "blocking": blocking, "missing": blocking}
        with patch.object(tp_mod, "prepare_task", return_value=fake):
            report = self.assess()
        wr = report["domains"]["writerReadiness"]
        self.assertFalse(wr["ready"])
        by_code = {row["code"]: row["status"] for row in wr["issues"]}
        for code, expected in codes.items():
            self.assertEqual(by_code.get(code), expected, f"{code} 分类错误")
        self.assertEqual(wr["status"], "blocked", "有机械阻断项时域状态应为 blocked")

    def test_range_and_stage_a_gate_readiness_on_the_real_pipeline(self):
        """真实（未打桩）流水线：批准范围与 Stage A 都是旧 plan+voice 门不看、现在纳入的开工前置。"""
        from scripts.core.task_prepare import prepare_task
        report = self.assess()
        wr = report["domains"]["writerReadiness"]
        self.assertFalse(wr["ready"])
        self.assertIn("CHAPTER_OUTSIDE_APPROVED_RANGE", wr["blocking"])
        self.assertIn("STAGE_A_NOT_APPROVED", wr["blocking"])
        prep = prepare_task(self.root, "writer", chapter_id=report["scopedChapterId"])
        self.assertEqual(wr["ready"], prep["readiness"]["taskReady"])

    def test_stage_a_still_gates_when_plan_ready_voice_active_range_approved(self):
        """旧公式在 plan ready + voice active + 无过期决定时会误判可开工；Stage A 未批准必须仍 not ready。"""
        from scripts.core.structure_commands import commit_plan
        from scripts.core.task_prepare import prepare_task
        from scripts.core.work_voice_profile import AXES, preview_voice_profile
        axes = {key: "适度变化" for key in AXES}
        for action, extra in (("set", {"axes": axes, "examples": ["例证"], "applicability": "全书"}),
                              ("approve", {"approvalActor": "作者", "approvalEvidence": "审阅"})):
            plan = preview_voice_profile(self.root, action=action,
                                         expectedRevision=load_structure(self.root)["revision"],
                                         reason="声纹", **extra)
            commit_plan(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=True)
        structure = load_structure(self.root)
        structure["fieldDefinitions"].append({"fieldId": "field-DOC-PLAN", "key": "plot.plan", "label": "剧情计划",
                                              "valueType": "topic", "appliesTo": ["chapter"]})
        structure["attributes"].append({"attributeId": "attribute-DOC-PLAN", "ownerId": self.chapter,
                                        "fieldId": "field-DOC-PLAN", "values": []})
        structure["fragments"].append({"fragmentId": "plan-DOC-PLAN", "ownerId": self.chapter, "text": "本章计划。",
                                       "order": 1, "contentRole": "plan", "contentBindings": ["attribute-DOC-PLAN"],
                                       "dependsOn": ["value-HEIGHT"]})
        save_structure(self.root, structure)
        write_json(self.root / ".govel/workflow.json", {"approvedChapterIds": [self.chapter]})

        # Preconditions: the exact inputs the old plan+voice formula read are now all "ok".
        prep = prepare_task(self.root, "writer", chapter_id=self.chapter)
        blocking = {row["code"] for row in prep["blocking"]}
        self.assertNotIn("SOURCE_CHAPTER_PLAN_MISSING", blocking, "计划已就绪")
        self.assertNotIn("CHAPTER_OUTSIDE_APPROVED_RANGE", blocking, "范围已批准")
        self.assertEqual(load_structure(self.root)["workVoiceProfile"]["status"], "active")

        report = self.assess(chapter_id=self.chapter)
        wr = report["domains"]["writerReadiness"]
        self.assertFalse(wr["ready"], "Stage A 未批准，即便 plan ready + voice active 也不能开工")
        self.assertIn("STAGE_A_NOT_APPROVED", wr["blocking"])
        self.assertEqual(wr["ready"], prep["readiness"]["taskReady"])


class ContextIssueClassificationTests(SingleSourceProjectTests):
    """The context domain passes through context_health's own status, not a code-substring guess."""

    def assess(self, **kwargs):
        from scripts.core.project_doctor import assess_project
        return assess_project(self.root, **kwargs)

    def test_missing_plan_is_author_decision_and_summary_stale_is_stale(self):
        report = self.assess()
        context_issues = {row["code"]: row["status"] for row in report["domains"]["context"]["issues"]}
        self.assertEqual(context_issues.get("CONTEXT_CHAPTER_PLAN_MISSING"), "author_decision_required",
                         "缺章节计划是作者决定，不是 needs_rebuild")
        if "CONTEXT_SUMMARY_STALE" in context_issues:
            self.assertEqual(context_issues["CONTEXT_SUMMARY_STALE"], "stale")

    def test_optional_omission_does_not_flip_a_ready_context_to_needs_rebuild(self):
        report = self.assess()
        for row in report["domains"]["context"]["issues"]:
            if row["code"] in {"CONTEXT_KNOWLEDGE_OMITTED", "CONTEXT_OPTIONAL_OMITTED"}:
                self.assertEqual(row["status"], "informational")
                self.assertNotEqual(row["status"], "needs_rebuild")



    """Sample A: intent → router → slice (REFERENCE KNOWLEDGE) → station context.

    The load-bearing assertion is traceability: the slice the writer eventually
    receives must carry its asset path, version hash and role, so a claim in the
    prose can be traced back to the material it came from.
    """

    def test_intent_router_and_context_agree_on_the_same_slice(self):
        # A real external provider must reach fresh CLI/MCP processes as well as
        # the context builder. No in-process asset-path patch supplies the data.
        with tempfile.TemporaryDirectory(prefix="govel-delivery-data-") as temporary:
            data_root = Path(temporary).resolve() / "provider"
            self.assertFalse(data_root.is_relative_to(ROOT))
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GOVEL_LOG_MODE": "off"}
            environment.pop("GOVEL_PROVIDER_CONFIG", None)
            created = subprocess.run(
                [sys.executable, str(ROOT / "scripts/provider.py"), "create", "--root", str(data_root)],
                cwd=ROOT, env=environment, text=True, capture_output=True, timeout=120)
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            environment["GOVEL_PROVIDER_CONFIG"] = str(data_root / "config.json")
            sample = json.loads((ROOT / "knowledge/example.json").read_text())
            asset_id = sample["metadata"]["assetId"]
            asset_path = "knowledge/assets/example.md"
            asset_hash = hashlib.sha256((data_root / asset_path).read_bytes()).hexdigest()
            intent = "旧书店 伏笔"

            def cli(*arguments):
                result = subprocess.run(
                    [sys.executable, str(NOVELCTL), "project", *arguments,
                     "--project-root", str(self.root)],
                    cwd=ROOT, env=environment, text=True, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)

            def files(directory):
                return {path.relative_to(directory).as_posix(): path.read_bytes()
                        for path in directory.rglob("*") if path.is_file()}

            project_before, data_before = files(self.root), files(data_root)
            route = cli("source", "knowledge-route", "--planning-intent", intent)
            self.assertEqual(route["code"], "KNOWLEDGE_ROUTE")
            self.assertEqual([(row["assetId"], row["path"], row["sha256"]) for row in route["items"]],
                             [(asset_id, asset_path, asset_hash)])

            pack = cli("knowledge", "pack", "--planning-intent", intent, "--station", "writer")
            self.assertEqual(pack["role"], "REFERENCE KNOWLEDGE")
            self.assertEqual(len(pack["items"]), 1)
            item = pack["items"][0]
            self.assertEqual((item["assetId"], item["path"], item["sha256"]),
                             (asset_id, asset_path, asset_hash))
            self.assertEqual(item["role"], "REFERENCE KNOWLEDGE")
            self.assertEqual(item["source"], sample["metadata"]["source"])
            self.assertEqual(item["excerpt"], sample["body"].strip())

            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "build_knowledge_pack", "arguments": {
                    "projectRoot": str(self.root), "planningIntent": intent, "station": "writer"}}}
            completed = subprocess.run(
                [sys.executable, str(MCP_SERVER)], input=json.dumps(request) + "\n",
                cwd=ROOT, env=environment, text=True, capture_output=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            mcp_payload = json.loads(completed.stdout)["result"]
            self.assertFalse(mcp_payload["isError"], mcp_payload)
            self.assertEqual(mcp_payload["structuredContent"]["items"], pack["items"],
                             "MCP 与 CLI 必须交付同一资产版本、来源与摘录")

            context = cli("task", "context", "--chapter-id", self.chapter,
                          "--task-type", "writer", "--chapter-goal", intent, "--view", "station")
            self.assertEqual(context["knowledgePack"]["role"], "REFERENCE KNOWLEDGE")
            self.assertEqual(context["knowledgePack"]["items"], pack["items"])
            self.assertEqual(context["dependencyManifest"]["knowledge"],
                             [{"assetId": asset_id, "sha256": asset_hash}])
            self.assertEqual(files(self.root), project_before, "资料读取不得改写作品")
            self.assertEqual(files(data_root), data_before, "资料读取不得修改资产或生成索引")


class CandidateToChapterAcceptanceTests(SingleSourceProjectTests):
    """Sample B: create → revise → approve → materialize; CH only at materialization."""

    def test_full_candidate_lifecycle(self):
        record = json.dumps({"title": "候选章", "summary": "后来发生的事",
                             "placement": {"scope": "volume", "volumeId": self.volume},
                             "primaryArcId": self.arc, "secondaryArcIds": [],
                             "beforeChapterId": None}, ensure_ascii=False)
        code, created = self.preview("source", "candidate-preview", "--action", "create",
                                     "--expected-revision", str(load_structure(self.root)["revision"]), "--reason", "先开一个候选",
                                     "--record", record)
        candidate_id = created["parameters"]["candidateId"]
        self.assertTrue(candidate_id.startswith("candidate-"))
        self.assertNotIn(candidate_id, {row["chapterId"] for row in load_structure(self.root)["chapters"]},
                         "候选 ID 不得提前变成正式章节 ID")

        structure = load_structure(self.root)
        self.preview("source", "candidate-preview", "--action", "approve",
                     "--expected-revision", str(structure["revision"]), "--reason", "作者批准",
                     "--candidate-id", candidate_id, "--approval-actor", "作者",
                     "--approval-evidence", "当面确认")
        structure = load_structure(self.root)
        row = next(item for item in structure["planningCandidates"] if item["candidateId"] == candidate_id)
        self.assertEqual(row["status"], "approved")

        code, materialized = self.preview("source", "candidate-preview", "--action", "materialize",
                                          "--expected-revision", str(structure["revision"]),
                                          "--reason", "物化", "--candidate-id", candidate_id)
        chapter_id = materialized["parameters"]["chapterId"]
        self.assertTrue(chapter_id.startswith("chapter-"))
        after = load_structure(self.root)
        row = next(item for item in after["planningCandidates"] if item["candidateId"] == candidate_id)
        self.assertEqual(row["status"], "materialized")
        self.assertEqual(row["materializedChapterId"], chapter_id)
        self.assertIn(chapter_id, {item["chapterId"] for item in after["chapters"]})



# Execute every toolbox mutation (including CLI children) in an owned physical copy.
from tests.toolbox_isolation import install_isolation
install_isolation(globals())

if __name__ == "__main__":
    unittest.main()
