"""The diagnostic registry has real consumers without becoming a project gate."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import local_ci, mcp_server, novelctl
from scripts.core import call_logging, project


class CallLoggingPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.logs = self.directory / "logs"
        environment = patch.dict(os.environ, {"GNOVEL_LOG_DIR": str(self.logs)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("GNOVEL_LOG_MODE", None)
        self.policy = project.call_logging_policy()

    def test_registry_default_changes_capture_without_creating_a_project(self):
        self.assertEqual(self.policy["defaultMode"], "full")
        policy = {**self.policy, "defaultMode": "off"}
        with patch.object(call_logging, "call_logging_policy", return_value=policy):
            with call_logging.call_scope("cli", {"body": "保留正文。"}, name="sample") as recorder:
                recorder.observe("response", {"ok": True})
        self.assertFalse(self.logs.exists())
        with call_logging.call_scope("cli", {"body": "保留正文。"}, name="sample") as recorder:
            recorder.observe("response", {"ok": True})
        self.assertEqual(call_logging.logging_status()["storage"]["callCount"], 1)
        self.assertFalse((self.directory / ".gnovel").exists())

    def test_policy_rejects_bad_limits_schema_patterns_and_pseudo_config(self):
        changes = [
            {"defaultMode": "enabled"}, {"schemaVersion": True}, {"retentionDays": True},
            {"maxDepth": 0}, {"lockTimeoutSeconds": float("nan")},
            {"lockRetrySeconds": self.policy["lockTimeoutSeconds"] + 1},
            {"inlinePayloadBytes": self.policy["maxPayloadBytes"] + 1},
            {"maxMetadataBytes": self.policy["maxPayloadBytes"] + 1},
            {"defaultQueryLimit": self.policy["maxQueryLimit"] + 1},
            {"credentialFields": ["apiKey", "api_key"]},
            {"credentialTextPatterns": [{"pattern": "[", "replacement": "hidden"}]},
            {"credentialTextPatterns": [{"pattern": "x", "replacement": r"\2"}]},
            {"unusedSetting": True},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                project._validated_call_logging_policy({**self.policy, **change})

    def test_policy_file_change_is_consumed_without_mutating_real_registry(self):
        registry = self.directory / "shared/registries/call-logging.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps(self.policy))
        with patch.object(project, "TOOLBOX_ROOT", self.directory):
            first = project.call_logging_policy()
            first["defaultMode"] = "summary"
            registry.write_text(json.dumps(first))
            self.assertEqual(project.call_logging_policy()["defaultMode"], "summary")
            first["unknown"] = 1
            registry.write_text(json.dumps(first))
            with self.assertRaises(ValueError):
                project.call_logging_policy()

    def test_bad_logging_config_reports_static_warning_but_business_runs(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(call_logging, "call_logging_policy", side_effect=ValueError("apiKey=do-not-print-this")), \
                patch.object(novelctl, "_dispatch", return_value={"ok": True, "answer": "业务仍然完成。"}), \
                redirect_stdout(output), redirect_stderr(errors):
            code = novelctl.main(["project", "overview"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["answer"], "业务仍然完成。")
        self.assertIn("CALL_LOG_UNAVAILABLE", errors.getvalue())
        self.assertNotIn("do-not-print-this", errors.getvalue())
        self.assertFalse(self.logs.exists())

    def test_large_metadata_follows_payload_limits_instead_of_bypassing_them(self):
        policy = {**self.policy, "maxMetadataBytes": 1024}
        note = "客户端自报信息。" * 900
        with patch.object(call_logging, "call_logging_policy", return_value=policy):
            with call_logging.call_scope("mcp", {"input": "原始入参"}, name="sample",
                                         client={"trust": "self_reported", "info": {"note": note}}) as recorder:
                recorder.observe("response", {"ok": True})
        call_id = next((self.logs / "calls").iterdir()).name
        shown = call_logging.show_call_log(call_id)
        metadata = shown["start"]["metadata"]
        self.assertLessEqual(len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()), 1024)
        self.assertEqual(metadata["metadataCapture"], "payload")
        saved = shown["start"]["payloads"][metadata["metadataPayload"]]
        self.assertEqual(saved["capture"], "full")
        self.assertEqual(saved["value"]["client"]["info"]["note"], note)
        ended = shown["finish"]
        self.assertEqual(ended["metadata"]["metadataPayload"], "finishMetadata")
        self.assertEqual(ended["payloads"]["finishMetadata"]["value"]["client"]["info"]["note"], note)

    def test_stream_and_payload_limits_are_visible_and_do_not_truncate_business_stdout(self):
        policy = {**self.policy, "maxPayloadBytes": 1024, "maxMetadataBytes": 1024, "inlinePayloadBytes": 64}
        expected = {"ok": True, "text": "明确保留的内容。" * 500}
        output = io.StringIO()
        with patch.object(call_logging, "call_logging_policy", return_value=policy), \
                patch.object(novelctl, "_dispatch", return_value=expected), redirect_stdout(output):
            self.assertEqual(novelctl.main(["project", "overview"]), 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        call_id = next((self.logs / "calls").iterdir()).name
        shown = call_logging.show_call_log(call_id)
        self.assertEqual(shown["finish"]["payloads"]["response"]["capture"], "omitted")
        stdout = shown["finish"]["payloads"]["stdout"]
        self.assertEqual(stdout["capture"], "partial")
        self.assertIn("STREAM_CAPTURE_LIMIT_EXCEEDED", stdout["issues"])

    def test_log_queries_via_cli_discovery_bridge_do_not_record_their_own_results(self):
        with call_logging.call_scope("cli", {"body": "用于复盘的调用。"}, name="sample"):
            pass
        before = set((self.logs / "calls").iterdir())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(novelctl.main(["project", "tools", "read", "--project-root", str(self.directory / "missing"),
                                           "--operation", "logs.list", "--arguments", "{}"]), 0)
        self.assertEqual(set((self.logs / "calls").iterdir()), before)
        self.assertFalse((self.directory / "missing").exists())

    def test_bridge_exemption_honors_argparse_abbreviations_and_last_operation(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(novelctl.main(["project", "tools", "read", "--project-root", str(self.directory / "missing"),
                                           "--oper", "logs.list", "--arguments", "{}"]), 0)
        self.assertFalse(self.logs.exists())
        command = ["project", "tools", "read", "--project-root", str(self.directory / "missing"),
                   "--operation", "logs.list", "--oper", "source.query", "--arguments", "{}"]
        with patch.object(novelctl, "_dispatch", return_value={"ok": True}), redirect_stdout(io.StringIO()):
            self.assertEqual(novelctl.main(command), 0)
        self.assertEqual(call_logging.logging_status()["storage"]["callCount"], 1)

    def test_mcp_records_actual_stable_chapter_after_resolving_display_number(self):
        from tests.fixtures_current import create_schema5_project
        root = self.directory / "book"
        fixture = create_schema5_project(root, chapter_count=1)
        chapter_id = fixture["structure"]["chapters"][0]["chapterId"]
        response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "query_source_chapter_plan", "arguments": {
                                          "projectRoot": str(root), "chapterId": "CH001"}}})
        self.assertFalse(response["result"]["isError"], response)
        page = call_logging.list_call_logs(chapter=chapter_id)
        self.assertEqual(page["total"], 1)
        detail = call_logging.show_call_log(page["calls"][0]["callId"])
        self.assertEqual(detail["start"]["payloads"]["request"]["value"]["params"]["arguments"]["chapterId"], "CH001")
        self.assertEqual(detail["finish"]["payloads"]["effectiveArguments"]["value"]["chapterId"], chapter_id)

    def test_cli_bridge_keeps_inner_resolved_chapter_instead_of_overwriting_it(self):
        from tests.fixtures_current import create_schema5_project
        root = self.directory / "book"
        fixture = create_schema5_project(root, chapter_count=1)
        chapter_id = fixture["structure"]["chapters"][0]["chapterId"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(novelctl.main(["project", "tools", "read", "--project-root", str(root),
                                           "--operation", "source.chapter-plan", "--arguments", '{"chapterId":"CH001"}']), 0)
        page = call_logging.list_call_logs(chapter=chapter_id)
        self.assertEqual(page["total"], 1)
        detail = call_logging.show_call_log(page["calls"][0]["callId"])
        self.assertEqual(detail["finish"]["payloads"]["arguments"]["value"]["arguments"]["chapterId"], "CH001")
        self.assertEqual(detail["finish"]["payloads"]["effectiveArguments"]["value"]["chapterId"], chapter_id)

    def test_known_credential_value_does_not_rename_structural_keys(self):
        from scripts.core.call_log_redaction import sanitize
        value = {"ok": False, "code": "EXPECTED_BLOCK", "error": "a business rejection"}
        copied = sanitize(value, self.policy, known_values={"error"})
        self.assertEqual(copied["value"], value)
        with call_logging.call_scope("mcp", {"password": "error"}, name="sample") as recorder:
            recorder.observe("response", value)
            recorder.finish(status="error", outcome="business_error")
        detail = call_logging.show_call_log(next((self.logs / "calls").iterdir()).name)
        self.assertEqual(detail["finish"]["metadata"]["status"], "error")
        self.assertEqual(detail["finish"]["payloads"]["response"]["value"], value)

    def test_story_secret_category_is_not_mistaken_for_a_credential_object(self):
        from scripts.core.call_log_redaction import credential_values, sanitize
        from scripts.core.settings import settings_schema
        schema = settings_schema()
        copied = sanitize(schema, self.policy)
        self.assertEqual(copied["value"]["kinds"]["secret"], schema["kinds"]["secret"])
        self.assertEqual(credential_values(schema, self.policy), set())
        self.assertEqual(sanitize({"secret": "credential-value-only"}, self.policy)["value"]["secret"], "[REDACTED]")

    def test_local_ci_separates_test_calls_from_personal_history(self):
        with patch.dict(os.environ, {"GNOVEL_LOG_MODE": "full"}):
            self.assertEqual(local_ci._subprocess_env()["GNOVEL_LOG_MODE"], "off")

    def test_known_credential_never_reappears_in_derived_name_or_parser_error(self):
        secret = "LOCAL-REVIEW-CREDENTIAL-NOT-REAL"
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            novelctl.main(["--password", secret, "project"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn(secret, output.getvalue(), "business error output must keep its original semantics")
        for path in self.logs.rglob("*.json"):
            self.assertNotIn(secret, path.read_text(), path)
        call_id = next((self.logs / "calls").iterdir()).name
        detail = call_logging.show_call_log(call_id)
        self.assertEqual(detail["start"]["metadata"]["name"], "unparsed_cli")
        self.assertEqual(detail["finish"]["metadata"]["outcome"], "argument_error")

    def test_malformed_record_is_reported_without_crashing_mcp_query(self):
        with call_logging.call_scope("cli", {}, name="sample"):
            pass
        directory = next((self.logs / "calls").iterdir())
        finish = directory / "finish.json"
        record = json.loads(finish.read_text())
        for change in ({"target": None}, {"startedAt": []}, {"status": []}):
            with self.subTest(change=change):
                altered = deepcopy(record)
                altered["metadata"].update(change)
                finish.write_text(json.dumps(altered))
                response = mcp_server.handle({"id": 9, "method": "tools/call", "params": {
                    "name": "query_call_logs", "arguments": {"project": "/test"}}})
                self.assertFalse(response["result"]["isError"], response)
                details = response["result"]["structuredContent"]
                self.assertEqual(details["calls"], [])
                self.assertTrue(any(issue.get("callId") == directory.name and issue.get("excludedFromResults")
                                    for issue in details["issues"]), details)

    def test_start_and_finish_clock_failures_leave_business_exit_unchanged(self):
        for phase in ("start", "finish"):
            with self.subTest(phase=phase):
                from unittest.mock import Mock
                fake_time = SimpleNamespace(monotonic=Mock(side_effect=OSError("clock unavailable") if phase == "start"
                                                          else [1.0, OSError("clock unavailable")]))
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(call_logging, "time", fake_time), \
                        patch.object(novelctl, "_dispatch", return_value={"ok": True, "done": True}), \
                        redirect_stdout(output), redirect_stderr(errors):
                    code = novelctl.main(["project", "overview"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output.getvalue()), {"ok": True, "done": True})
                self.assertIn("CALL_LOG_UNAVAILABLE", errors.getvalue())

    def test_exception_before_mcp_dispatch_is_recorded_without_changing_propagation(self):
        raw = "[" * 2000 + "]" * 2000 + "\n"
        with patch("sys.argv", ["mcp_server.py"]), patch("sys.stdin", io.StringIO(raw)), self.assertRaises(RecursionError):
            mcp_server.main()
        call_id = next((self.logs / "calls").iterdir()).name
        detail = call_logging.show_call_log(call_id)
        self.assertEqual(detail["start"]["payloads"]["request"]["value"]["rawLine"], raw)
        self.assertEqual(detail["finish"]["metadata"]["outcome"], "exception")
        self.assertEqual(detail["finish"]["payloads"]["exception"]["value"]["type"], "RecursionError")

    def test_authorization_exceptions_match_actual_public_business_values(self):
        from scripts.core.call_log_redaction import credential_exception_key
        from scripts.core.operations import operations
        from scripts.core.tool_catalog import list_capabilities
        from scripts.core.tool_plan import AUTHORIZATION_BY_EFFECT
        operation = next(row for row in operations() if row.id == "task.execute")
        property_schema = operation.input_schema["properties"]["authorization"]
        values = set(AUTHORIZATION_BY_EFFECT.values()) | set(property_schema["enum"])
        values.add(list_capabilities(operation="logs.status")["authorization"])
        values.add(list_capabilities(query="导出全部设定")["authorization"])
        expected = {credential_exception_key(value) for value in values}
        expected.add(credential_exception_key(property_schema))
        self.assertEqual(set(self.policy["credentialValueExceptions"]), {"authorization"})
        self.assertEqual({credential_exception_key(value)
                          for value in self.policy["credentialValueExceptions"]["authorization"]}, expected)

    def test_real_capability_definitions_are_saved_without_masking_public_authorization(self):
        from scripts.core.call_log_redaction import credential_values
        for operation in ("logs.status", "task.execute"):
            with self.subTest(operation=operation):
                before = set((self.logs / "calls").glob("CL-*"))
                output = io.StringIO()
                with redirect_stdout(output):
                    code = novelctl.main(["project", "tools", "list", "--operation", operation])
                self.assertEqual(code, 0)
                original = json.loads(output.getvalue())
                created = set((self.logs / "calls").glob("CL-*")) - before
                self.assertEqual(len(created), 1)
                detail = call_logging.show_call_log(created.pop().name)
                payloads = detail["finish"]["payloads"]
                self.assertEqual(payloads["response"]["value"], original)
                self.assertEqual(payloads["stdout"]["value"], output.getvalue())
                self.assertEqual(credential_values(original, self.policy), set())

    def test_public_authorization_preserves_normal_text_and_does_not_seed_credentials(self):
        from scripts.core.call_log_redaction import credential_values, sanitize
        request = {"authorization": "read", "note": "read remains normal text"}
        self.assertEqual(credential_values(request, self.policy), set())
        with call_logging.call_scope("mcp", request, name="public-scope") as recorder:
            recorder.observe("response", {**request, "ok": True})
            self.assertEqual(recorder.known_values, set())
        detail = call_logging.show_call_log(next((self.logs / "calls").iterdir()).name)
        self.assertEqual(detail["start"]["payloads"]["request"]["value"], request)
        self.assertEqual(detail["finish"]["payloads"]["response"]["value"], {**request, "ok": True})
        for value in (["--authorization", "read", "read"], "authorization=read", "Authorization: read",
                      'authorization="candidate"', "AUTHORIZATION='author-approval'"):
            with self.subTest(value=value):
                self.assertEqual(credential_values(value, self.policy), set())
                self.assertEqual(sanitize(value, self.policy)["value"], value)

    def test_real_authorization_credentials_and_unknown_objects_remain_hidden(self):
        from scripts.core.call_log_redaction import credential_values, sanitize
        from scripts.core.operations import operations
        schema = deepcopy(next(row for row in operations() if row.id == "task.execute")
                          .input_schema["properties"]["authorization"])
        values = ["Bearer read", "Basic cmVhZA==", "Token read", "opaque-credential",
                  "read extra", "READ", {"opaque": "private-value"}, {**schema, "extra": "private-value"}]
        for value in values:
            with self.subTest(value=value):
                source = {"headers": {"Authorization": value}}
                before = deepcopy(source)
                copied = sanitize(source, self.policy)
                self.assertEqual(copied["value"]["headers"]["Authorization"], "[REDACTED]")
                self.assertEqual(source, before)
                if isinstance(value, str):
                    self.assertEqual(credential_values(source, self.policy), {value})
                    self.assertEqual(sanitize(["--authorization", value], self.policy)["value"],
                                     ["--authorization", "[REDACTED]"])
        for value in ("Authorization: Bearer read", "Authorization: Basic read", "Authorization: Token read",
                      "Authorization: opaque-credential", "Authorization: read extra", "authorization=Token read",
                      "authorization=Bearer read", "authorization=read-only", "Proxy-Authorization: read"):
            with self.subTest(text=value):
                copied = sanitize(value, self.policy)["value"]
                self.assertIn("[REDACTED]", copied)
                self.assertNotIn(value.rsplit(" ", 1)[-1] if " " in value else "read-only", copied)
        self.assertEqual(sanitize({"password": "read", "apiKey": "read"}, self.policy)["value"],
                         {"password": "[REDACTED]", "apiKey": "[REDACTED]"})

    def test_credential_exception_policy_is_exact_and_rejects_non_json_values(self):
        from scripts.core.call_log_redaction import sanitize
        policy = {**self.policy, "credentialValueExceptions": {"authorization": [{"scope": True}]}}
        project._validated_call_logging_policy(policy)
        self.assertEqual(sanitize({"authorization": {"scope": True}}, policy)["value"],
                         {"authorization": {"scope": True}})
        self.assertEqual(sanitize({"authorization": {"scope": 1}}, policy)["value"],
                         {"authorization": "[REDACTED]"})
        policy["credentialValueExceptions"] = {"authorization": [{"scope": 1}]}
        self.assertEqual(sanitize({"authorization": {"scope": True}}, policy)["value"],
                         {"authorization": "[REDACTED]"})
        for values in ([], {"unknown": ["read"]}, {"authorization": []},
                       {"authorization": ["read", "read"]}, {"authorization": [float("nan")]},
                       {"authorization": [("read",)]}, {"authorization": [{1: "read"}]},
                       {"authorization": ["read"], "AUTHORIZATION": ["candidate"]}):
            with self.subTest(exceptions=values), self.assertRaises(ValueError):
                project._validated_call_logging_policy({**self.policy, "credentialValueExceptions": values})


if __name__ == "__main__":
    unittest.main()
