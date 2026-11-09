"""Public call logging keeps business results intact and diagnostic evidence honest."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import mcp_server, novelctl
from scripts.core.artifacts import sha256_text
from scripts.core.jsonio import write_json
from scripts.core.prose_binding import seal_binding
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/novelctl.py"
MCP = ROOT / "scripts/mcp_server.py"


class CallLoggingEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.project = self.directory / "book"
        self.logs = self.directory / "diagnostics"
        self.fixture = create_schema5_project(self.project, chapter_count=1)
        environment = patch.dict(os.environ, {
            "GNOVEL_LOG_DIR": str(self.logs),
            "GNOVEL_LOG_MODE": "full",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def _env(self, mode="full", log_root=None):
        env = dict(os.environ)
        env["GNOVEL_LOG_DIR"] = str(log_root or self.logs)
        if mode is None:
            env.pop("GNOVEL_LOG_MODE", None)
        else:
            env["GNOVEL_LOG_MODE"] = mode
        return env

    def _cli(self, *args, mode="full", log_root=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args], cwd=ROOT,
            env=self._env(mode, log_root), capture_output=True, text=True,
            timeout=30,
        )

    def _mcp(self, requests, *, mode="full", log_root=None):
        text = requests if isinstance(requests, str) else "\n".join(
            json.dumps(request, ensure_ascii=False) for request in requests
        ) + "\n"
        return subprocess.run(
            [sys.executable, str(MCP)], cwd=ROOT, input=text,
            env=self._env(mode, log_root), capture_output=True, text=True,
            timeout=30,
        )

    def _request(self, name="query_logic", arguments=None, request_id=1):
        return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments if arguments is not None
                           else {"projectRoot": str(self.project)}}}

    def _calls(self):
        return sorted(path for path in (self.logs / "calls").glob("CL-*")
                      if path.is_dir())

    def _events(self, path):
        start = json.loads((path / "start.json").read_text())
        finish_path = path / "finish.json"
        finish = json.loads(finish_path.read_text()) if finish_path.is_file() else None
        return start, finish

    def _single(self):
        calls = self._calls()
        self.assertEqual(len(calls), 1, calls)
        return calls[0], *self._events(calls[0])

    def _show(self, call_id):
        process = self._cli("project", "logs", "show", "--call-id", call_id)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def _list(self, *filters):
        process = self._cli("project", "logs", "list", *filters)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def _project_snapshot(self):
        return {path.relative_to(self.project).as_posix(): path.read_bytes()
                for path in self.project.rglob("*") if path.is_file()}

    def _log_text(self):
        return "\n".join(path.read_text() for path in self.logs.rglob("*.json"))

    def _query_args(self):
        return ("project", "query", "logic", "--project-root", str(self.project))

    def test_default_full_captures_cli_result_without_changing_read_only_project(self):
        before = self._project_snapshot()
        process = self._cli(*self._query_args(), mode=None)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        path, start, finish = self._single()
        self.assertEqual(start["metadata"]["entrypoint"], "cli")
        self.assertEqual(start["payloads"]["request"]["capture"], "full")
        self.assertEqual(finish["metadata"]["status"], "success")
        self.assertEqual(finish["metadata"]["exitCode"], process.returncode)
        self.assertGreaterEqual(finish["metadata"]["durationMs"], 0)
        shown = self._show(path.name)
        self.assertEqual(shown["finish"]["payloads"]["response"]["value"], json.loads(process.stdout))
        self.assertEqual(shown["finish"]["payloads"]["stdout"]["value"], process.stdout)
        self.assertEqual(before, self._project_snapshot())

    def test_cli_and_mcp_modes_preserve_exact_stdout_stderr_and_exit_code(self):
        before = self._project_snapshot()
        cli_off = self._cli(*self._query_args(), mode="off")
        mcp_off = self._mcp([self._request()], mode="off")
        self.assertFalse(self.logs.exists())
        cli_full = self._cli(*self._query_args())
        mcp_full = self._mcp([self._request()])
        for off, full in ((cli_off, cli_full), (mcp_off, mcp_full)):
            self.assertEqual((off.returncode, off.stdout, off.stderr),
                             (full.returncode, full.stdout, full.stderr))
        self.assertEqual(len(self._calls()), 2)
        self.assertEqual(before, self._project_snapshot())

    def test_cli_argument_and_business_errors_are_recorded_without_initializing_project(self):
        missing = self.directory / "does-not-exist"
        argument_off = self._cli("project", "not-a-command", mode="off")
        argument_full = self._cli("project", "not-a-command")
        business_off = self._cli("project", "overview", "--project-root", str(missing), mode="off")
        business_full = self._cli("project", "overview", "--project-root", str(missing))
        for off, full in ((argument_off, argument_full), (business_off, business_full)):
            self.assertNotEqual(full.returncode, 0)
            self.assertEqual((off.returncode, off.stdout, off.stderr),
                             (full.returncode, full.stdout, full.stderr))
        self.assertFalse(missing.exists())
        events = [self._events(path)[1] for path in self._calls()]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["metadata"]["status"] == "error" for event in events))
        by_code = {event["payloads"]["response"]["value"]["code"]: event for event in events}
        self.assertEqual(by_code["ARGUMENTS_INVALID"]["metadata"]["outcome"], "argument_error")
        self.assertEqual(by_code["ARGUMENTS_INVALID"]["metadata"]["exitCode"], argument_full.returncode)

    def test_mcp_validation_protocol_and_unknown_tool_errors_keep_protocol_clean(self):
        requests = [self._request("missing"), self._request(arguments=[])]
        off = self._mcp(requests, mode="off")
        full = self._mcp(requests)
        self.assertEqual((off.returncode, off.stdout, off.stderr),
                         (full.returncode, full.stdout, full.stderr))
        responses = [json.loads(line) for line in full.stdout.splitlines()]
        self.assertEqual([row["result"]["structuredContent"]["code"] for row in responses],
                         ["TOOL_NOT_FOUND", "ARGUMENTS_INVALID"])
        malformed = self._mcp("{broken\n")
        self.assertEqual(json.loads(malformed.stdout)["error"]["code"], -32700)
        events = [self._events(path)[1] for path in self._calls()]
        self.assertEqual(len(events), 3)
        self.assertTrue(all(event["metadata"]["status"] == "error" for event in events))
        self.assertIn("protocol_error", {event["metadata"]["outcome"] for event in events})
        self.assertIn("argument_error", {event["metadata"]["outcome"] for event in events})

    def test_repeated_mcp_legacy_calls_have_distinct_ids_without_nested_cli_records(self):
        arguments = {"projectRoot": str(self.project), "selector": self.fixture["characterId"]}
        request = self._request("settings_show", arguments)
        process = self._mcp([request, deepcopy(request)])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(all(not json.loads(line)["result"]["isError"] for line in process.stdout.splitlines()))
        calls = self._calls()
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].name, calls[1].name)
        self.assertEqual({self._events(path)[0]["metadata"]["entrypoint"] for path in calls}, {"mcp"})
        self.assertEqual(self._events(calls[0])[0]["payloads"]["request"]["value"],
                         self._events(calls[1])[0]["payloads"]["request"]["value"])

    def test_complete_large_business_text_is_retrievable_from_blob(self):
        prose = "他忘了密码，仍记得把钥匙放回桌上。\n" * 1800
        structure = self.fixture["structure"]
        chapter = structure["chapters"][0]
        (self.project / chapter["filePath"]).write_text(prose)
        chapter["proseBinding"] = seal_binding(
            structure["projectId"], chapter["chapterId"], chapter["filePath"], state="managed",
            generation=1, expected_content_digest="sha256:" + sha256_text(prose),
        )
        write_json(self.project / ".gnovel/structure.json", structure)
        before = self._project_snapshot()
        process = self._cli("project", "prose", "--chapter-id", chapter["chapterId"],
                            "--project-root", str(self.project))
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        path, _, finish = self._single()
        descriptor = finish["payloads"]["response"]
        self.assertEqual(descriptor["capture"], "full")
        self.assertEqual(descriptor["storage"], "blob")
        shown = self._show(path.name)
        payload = shown["finish"]["payloads"]["response"]["value"]
        self.assertEqual(payload["text"], prose)
        self.assertEqual(before, self._project_snapshot())

    def test_at_file_records_the_parsed_business_input_not_later_file_contents(self):
        selection = {"ownerTypes": ["character"]}
        path = self.directory / "selection.json"
        path.write_text(json.dumps(selection))
        process = self._cli("project", "source", "query", "--project-root", str(self.project),
                            "--selection", "@" + str(path))
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        call_path, _, _ = self._single()
        path.write_text('{"later": "unrelated-file-now-has-different-content"}')
        shown = self._show(call_path.name)
        self.assertEqual(shown["finish"]["payloads"]["arguments"]["value"]["selection"], selection)
        self.assertNotIn("unrelated-file-now-has-different-content", self._log_text())

    def test_nested_credential_copies_are_redacted_without_altering_cli_objects_or_prose(self):
        prose = "他提到密码，随后核对了审校里的因果说明。"
        selection = {"password": "request-password-unique", "nested": [{"apiKey": "request-api-key-unique"}],
                     "activeToken": "candidate-business-token", "text": prose}
        result = {"ok": True, "payload": {"clientSecret": "response-secret-unique", "text": prose},
                  "activeToken": "candidate-business-token"}
        original_selection, original_result = deepcopy(selection), deepcopy(result)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(novelctl, "_dispatch", return_value=result) as dispatch:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = novelctl.main(["project", "source", "query", "--project-root", str(self.project),
                                     "--selection", json.dumps(selection, ensure_ascii=False)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), original_result)
        self.assertEqual(dispatch.call_args.args[0].selection, original_selection)
        self.assertEqual(result, original_result)
        disk = self._log_text()
        for secret in ("request-password-unique", "request-api-key-unique", "response-secret-unique"):
            self.assertNotIn(secret, disk)
        self.assertIn(prose, disk)
        self.assertIn("candidate-business-token", disk)
        self.assertIn("[REDACTED]", disk)

    def test_mcp_error_response_is_preserved_while_credential_error_copy_is_redacted(self):
        request = self._request()
        original = deepcopy(request)
        details = {"nested": [{"password": "error-password-unique"}], "note": "应保留的后续审校说明"}
        with patch.object(mcp_server, "call_tool", side_effect=StructureError(
                "EXPECTED_TEST_BLOCK", "Authorization: Bearer error-bearer-unique", details)):
            response = mcp_server.handle(request)
        self.assertEqual(request, original)
        self.assertEqual(response["result"]["structuredContent"]["details"], details)
        self.assertTrue(response["result"]["isError"])
        _, _, finish = self._single()
        self.assertEqual(finish["metadata"]["status"], "error")
        recorded = finish["payloads"]["response"]["value"]["result"]
        recorded_text = json.loads(recorded["content"][0]["text"])
        self.assertEqual(recorded_text["details"]["note"], details["note"])
        self.assertEqual(recorded_text["details"]["nested"], [{"password": "[REDACTED]"}])
        self.assertNotIn("error-password-unique", self._log_text())
        self.assertNotIn("error-bearer-unique", self._log_text())

    def test_mcp_business_failure_is_not_counted_as_successful_only_because_rpc_returned(self):
        payload = {"ok": False, "code": "SUBMISSION_INCOMPLETE", "completedStages": ["candidate"],
                   "error": {"code": "REVIEW_REQUIRED", "message": "审校工件尚未就绪"}}
        with patch.object(mcp_server, "call_tool", return_value=payload):
            response = mcp_server.handle(self._request())
        self.assertEqual(response["result"]["structuredContent"], payload)
        _, _, finish = self._single()
        self.assertEqual(finish["metadata"]["status"], "error")
        self.assertEqual(finish["metadata"]["outcome"], "business_error")
        self.assertEqual(finish["payloads"]["response"]["value"], response)

    def test_uncaught_exception_still_propagates_and_keeps_original_message(self):
        error = RuntimeError("password='uncaught-secret-unique'")
        with patch.object(novelctl, "_dispatch", side_effect=error):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError) as caught:
                    novelctl.main(list(self._query_args()))
        self.assertIs(caught.exception, error)
        _, _, finish = self._single()
        self.assertEqual(finish["metadata"]["status"], "error")
        self.assertEqual(finish["metadata"]["outcome"], "exception")
        self.assertNotIn("uncaught-secret-unique", self._log_text())

    def test_mcp_write_failure_is_not_misreported_as_delivered_success(self):
        error = BrokenPipeError("synthetic closed protocol output")
        payload = {"ok": True, "code": "EXPECTED_RESULT", "text": "已经生成的业务结果"}

        def failed_send(response):
            raise error

        with patch.object(mcp_server, "call_tool", return_value=payload):
            with self.assertRaises(BrokenPipeError) as caught:
                mcp_server.handle(self._request(), send_response=failed_send)
        self.assertIs(caught.exception, error)
        _, _, finish = self._single()
        self.assertEqual(finish["metadata"]["status"], "error")
        self.assertEqual(finish["metadata"]["outcome"], "transport_error")
        self.assertEqual(finish["metadata"]["delivery"], "failed")
        self.assertEqual(finish["payloads"]["exception"]["value"]["businessOutcome"], "success")
        self.assertEqual(finish["payloads"]["response"]["value"]["result"]["structuredContent"], payload)

    def test_logging_does_not_follow_artifact_paths_in_returned_data(self):
        unrelated = self.directory / "private-input.json"
        unrelated.write_text('{"private": "unrequested-file-secret-unique"}')
        result = {"ok": True, "artifactPath": str(unrelated), "artifactSha256": "observed-digest"}
        real_open = Path.open
        attempted_reads = []

        def guarded_open(path, *args, **kwargs):
            if path == unrelated:
                attempted_reads.append(path)
                raise AssertionError("logging followed an arbitrary artifact reference")
            return real_open(path, *args, **kwargs)

        with patch.object(novelctl, "_dispatch", return_value=result), patch.object(Path, "open", guarded_open):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(novelctl.main(list(self._query_args())), 0)
        path, _, _ = self._single()
        shown = self._show(path.name)
        self.assertEqual(attempted_reads, [])
        self.assertEqual(shown["finish"]["payloads"]["response"]["value"], result)
        self.assertNotIn("unrequested-file-secret-unique", self._log_text())

    def test_unavailable_logging_directory_does_not_change_successful_business_result(self):
        unavailable = self.directory / "api_key=log-directory-secret-unique"
        unavailable.write_text("an existing ordinary file")
        before = self._project_snapshot()
        off = self._cli(*self._query_args(), mode="off", log_root=unavailable)
        full = self._cli(*self._query_args(), log_root=unavailable)
        self.assertEqual((off.returncode, off.stdout), (full.returncode, full.stdout))
        self.assertEqual(full.returncode, 0)
        self.assertIn("CALL_LOG_UNAVAILABLE", full.stderr)
        self.assertNotIn("log-directory-secret-unique", full.stderr)
        self.assertEqual(unavailable.read_text(), "an existing ordinary file")
        self.assertEqual(before, self._project_snapshot())

    def test_process_interruption_stays_incomplete_in_public_query(self):
        script = """
import signal, sys
from scripts import novelctl
def block(arguments):
    print('DISPATCH_REACHED', file=sys.stderr, flush=True)
    signal.pause()
novelctl._dispatch = block
novelctl.main(sys.argv[1:])
"""
        process = subprocess.Popen([sys.executable, "-c", script, *self._query_args()],
                                   cwd=ROOT, env=self._env(), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stderr, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=15), "child did not reach dispatch")
                self.assertEqual(process.stderr.readline().strip(), "DISPATCH_REACHED")
            _, _, finish = self._single()
            self.assertIsNone(finish)
            process.kill()
            process.wait(timeout=10)
            listing = self._list("--status", "incomplete")
            self.assertEqual(len(listing["calls"]), 1)
            self.assertEqual(listing["calls"][0]["status"], "incomplete")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_mcp_client_name_is_self_reported_and_missing_initialize_remains_unknown(self):
        client = {"name": "Antigravity", "version": "local-test"}
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25", "clientInfo": client}}
        with_initialize = self._mcp([initialize, self._request(request_id=2)])
        self.assertEqual(with_initialize.returncode, 0, with_initialize.stderr)
        tool_events = [self._events(path)[0] for path in self._calls()
                       if self._events(path)[0]["payloads"]["request"]["value"].get("method") == "tools/call"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["metadata"]["client"], {"trust": "self_reported", "info": client})
        previous = {path.name for path in self._calls()}
        without_initialize = self._mcp([self._request()])
        self.assertEqual(without_initialize.returncode, 0, without_initialize.stderr)
        new_calls = [path for path in self._calls() if path.name not in previous]
        self.assertEqual(len(new_calls), 1)
        new_event = self._events(new_calls[0])[0]
        self.assertEqual(new_event["metadata"]["client"], {"trust": "unknown", "info": None})

    def test_log_configuration_and_queries_are_excluded_and_environment_can_disable_capture(self):
        configured_off = self._cli("project", "logs", "configure", "--mode", "off", mode=None)
        self.assertEqual(configured_off.returncode, 0, configured_off.stdout + configured_off.stderr)
        self.assertEqual(self._cli(*self._query_args(), mode=None).returncode, 0)
        self.assertEqual(self._calls(), [])
        configured_full = self._cli("project", "logs", "configure", "--mode", "full", mode=None)
        self.assertEqual(configured_full.returncode, 0, configured_full.stdout + configured_full.stderr)
        self.assertEqual(self._cli(*self._query_args(), mode="off").returncode, 0)
        self.assertEqual(self._calls(), [])
        self.assertEqual(self._cli(*self._query_args(), mode=None).returncode, 0)
        call_path, _, _ = self._single()
        before = {path.relative_to(self.logs).as_posix(): path.read_bytes()
                  for path in self.logs.rglob("*") if path.is_file()}
        self._show(call_path.name)
        self._list()
        status = self._cli("project", "logs", "status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        queries = self._mcp([
            self._request("query_call_logging_status", {}, 1),
            self._request("query_call_logs", {}, 2),
            self._request("read_call_log", {"callId": call_path.name}, 3),
        ])
        self.assertEqual(queries.returncode, 0, queries.stderr)
        self.assertTrue(all(not json.loads(line)["result"]["isError"] for line in queries.stdout.splitlines()))
        after = {path.relative_to(self.logs).as_posix(): path.read_bytes()
                 for path in self.logs.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_public_list_filters_targets_status_and_pagination(self):
        success = self._cli(*self._query_args())
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        failure = self._cli("project", "not-a-command")
        self.assertNotEqual(failure.returncode, 0)
        matching = self._list("--project", str(self.project), "--status", "success")
        self.assertEqual(len(matching["calls"]), 1)
        first = self._list("--limit", "1", "--offset", "0")
        second = self._list("--limit", "1", "--offset", "1")
        self.assertEqual(len(first["calls"]), 1)
        self.assertEqual(len(second["calls"]), 1)
        self.assertNotEqual(first["calls"][0]["callId"], second["calls"][0]["callId"])
        self.assertEqual(self._list("--project", str(self.directory / "another-project"))["calls"], [])

    def test_public_list_filters_declared_task_chapter_and_time_window(self):
        chapter = self.fixture["chapterIds"][0]
        with patch.object(novelctl, "_dispatch", return_value={"ok": True, "chapterId": chapter}):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(novelctl.main([
                    "project", "prepare", "--task", "writer", "--chapter-id", chapter,
                    "--project-root", str(self.project),
                ]), 0)
        path, start, _ = self._single()
        timestamp = datetime.fromisoformat(start["metadata"]["startedAt"].replace("Z", "+00:00"))
        lower = (timestamp - timedelta(seconds=1)).isoformat()
        upper = (timestamp + timedelta(seconds=1)).isoformat()
        filtered = self._list("--task", "writer", "--chapter", chapter, "--since", lower, "--until", upper)
        self.assertEqual([row["callId"] for row in filtered["calls"]], [path.name])
        self.assertEqual(self._list("--task", "editor")["calls"], [])
        self.assertEqual(self._list("--chapter", "CH-ANOTHER")["calls"], [])
        self.assertEqual(self._list("--since", upper)["calls"], [])
        self.assertEqual(self._list("--until", lower)["calls"], [])


if __name__ == "__main__":
    unittest.main()
