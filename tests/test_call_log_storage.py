from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.core.call_log_redaction import json_bytes, sanitize
from scripts.core.call_log_store import CallLogStore, CallLogStoreError
from scripts.core.project import call_logging_policy


def call_id() -> str:
    return "CL-" + uuid.uuid4().hex


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.policy = copy.deepcopy(call_logging_policy())

    def test_nested_credentials_and_embedded_json_are_copies(self):
        original = {
            "body": "他说忘了密码。密码不是这一段的秘密主题。",
            "nested": [{"API_KEY": "test-key-private", "Cookie": "sid=local-cookie; token=other"}],
            "result": '{"password":"local-pass","text":"保留小说正文与审校意见。"}',
            "review": {"quote": "他没再问，把钥匙放了回去。", "count": 2},
        }
        before = copy.deepcopy(original)
        cleaned = sanitize(original, self.policy)
        encoded = json_bytes(cleaned).decode()
        for secret in ("test-key-private", "local-cookie", "local-pass", "token=other"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(original, before)
        self.assertEqual(cleaned["value"]["body"], original["body"])
        self.assertEqual(cleaned["value"]["review"], original["review"])
        self.assertTrue(cleaned["complete"])
        self.assertGreater(cleaned["redactions"], 0)
        self.assertEqual(cleaned["originalBytes"], len(json_bytes(original)))

    def test_cli_headers_urls_and_known_keys_redact_without_erasing_prose(self):
        original = {
            "argv": ["source", "--password", "argv-password", "--api-key=argv-key", "--title", "正文"],
            "error": "Authorization: Bearer local-authorization\nCookie: sid=cookie-one; uid=cookie-two\n"
                     "request https://name:url-password@example.invalid/path?access_token=query-token\n"
                     "token sk-abcdefghijklmnopqrstuv; api_key=error-key; Basic YmFzaWM6cGFzcw==",
            "prose": "门上的密码没有改。他输入四位数，把提袋留在门口。",
        }
        cleaned = sanitize(original, self.policy)
        encoded = json_bytes(cleaned["value"]).decode()
        for secret in ("argv-password", "argv-key", "local-authorization", "cookie-one", "cookie-two",
                       "url-password", "query-token", "abcdefghijklmnopqrstuv", "error-key", "YmFzaWM6cGFzcw=="):
            self.assertNotIn(secret, encoded, secret)
        self.assertEqual(cleaned["value"]["prose"], original["prose"])
        self.assertEqual(sanitize(cleaned["value"], self.policy)["value"], cleaned["value"])

    def test_credential_headers_inside_error_lines_hide_the_whole_header_value(self):
        result = sanitize({"errors": ["request failed: Cookie: sid=first; uid=second",
                                      "request failed: Authorization: Token custom-authorization"]}, self.policy)
        encoded = json.dumps(result)
        for secret in ("sid=first", "uid=second", "custom-authorization"):
            self.assertNotIn(secret, encoded)

    def test_embedded_json_header_redaction_preserves_other_fields_and_valid_json(self):
        message = {"ok": False, "error": "Authorization: Bearer synthetic-credential-only",
                   "details": {"note": "应保留的后续审校说明"}}
        cleaned = sanitize({"text": json.dumps(message, ensure_ascii=False)}, self.policy)
        parsed = json.loads(cleaned["value"]["text"])
        self.assertEqual(parsed["details"], message["details"])
        self.assertEqual(parsed["ok"], False)
        self.assertNotIn("synthetic-credential-only", cleaned["value"]["text"])
        self.assertTrue(cleaned["complete"])

    def test_registry_fields_patterns_and_depth_have_real_effect(self):
        self.policy["credentialFields"].append("privatePin")
        self.policy["credentialTextPatterns"].append({"pattern": r"CUSTOM-CREDENTIAL-\w+", "replacement": "[REDACTED]"})
        cleaned = sanitize({"private_pin": "pin-secret", "text": "private-pin=pin-other CUSTOM-CREDENTIAL-hide"}, self.policy)
        self.assertNotIn("pin-secret", json.dumps(cleaned))
        self.assertNotIn("pin-other", json.dumps(cleaned))
        self.assertNotIn("CUSTOM-CREDENTIAL-hide", json.dumps(cleaned))
        self.policy["maxDepth"] = 1
        too_deep = sanitize({"a": {"b": {"c": "unseen"}}}, self.policy)
        self.assertFalse(too_deep["complete"])
        self.assertIn("max_depth", too_deep["issues"])

    def test_non_json_values_are_explicitly_incomplete_and_never_repr(self):
        class Dangerous:
            def __repr__(self):
                raise AssertionError("must not repr")

            def __str__(self):
                raise AssertionError("must not str")

        circular = []
        circular.append(circular)
        cleaned = sanitize({"object": Dangerous(), "cycle": circular, "number": float("nan"),
                            "bytes": b"password=raw-binary"}, self.policy)
        self.assertFalse(cleaned["complete"])
        self.assertIsNone(cleaned["originalBytes"])
        self.assertEqual(set(cleaned["issues"]), {"unsupported_type", "cycle", "non_finite_number"})
        self.assertNotIn("raw-binary", json.dumps(cleaned))


class CallLogStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "diagnostics"
        self.policy = copy.deepcopy(call_logging_policy())
        self.store = CallLogStore(self.root, self.policy)

    def begin(self, ident: str | None = None, value=None, **metadata):
        ident = ident or call_id()
        details = {"pid": os.getpid(), "startedAt": datetime.now(timezone.utc).isoformat(),
                   "entrypoint": "cli", "name": "example", **metadata}
        self.store.start(ident, details, {"request": sanitize(value, self.policy)}, "full")
        return ident

    def finish(self, ident, value=None, **metadata):
        self.store.finish(ident, {"status": "success", "finishedAt": datetime.now(timezone.utc).isoformat(),
                                 **metadata}, {"response": sanitize(value, self.policy)}, "full")

    def snapshot(self):
        if not self.root.exists():
            return {}
        return {str(path.relative_to(self.root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.root.rglob("*") if path.is_file()}

    def test_read_methods_do_not_initialize_storage_and_config_survives_pruning(self):
        self.assertEqual(self.store.list_calls(), {"calls": [], "issues": []})
        self.assertEqual(self.store.stats()["callCount"], 0)
        self.assertIsNone(self.store.read_config())
        self.assertEqual(self.store.prune()["removedCount"], 0)
        with self.assertRaises(CallLogStoreError):
            self.store.show(call_id())
        self.assertFalse(self.root.exists())
        self.store.write_config({"schemaVersion": 1, "mode": "full"})
        self.store.write_config({"schemaVersion": 1, "mode": "off"})
        self.assertEqual(self.store.read_config(), {"schemaVersion": 1, "mode": "off"})
        ident = self.begin(value={"scene": "正文"})
        self.finish(ident)
        self.assertEqual(self.store.prune(all_calls=True)["removedCount"], 1)
        self.assertEqual(self.store.read_config()["mode"], "off")

    def test_full_payload_roundtrip_private_permissions_and_read_only_queries(self):
        content = {"body": "未公开稿件。" * self.policy["inlinePayloadBytes"], "access_token": "private-token"}
        ident = self.begin(value=content, projectRoot="/example/project", chapterId="CH-CURRENT")
        self.finish(ident, value={"review": "保留具体引文。", "artifactSha256": "observed-sha"})
        before = self.snapshot()
        shown = self.store.show(ident)
        request = shown["start"]["payloads"]["request"]
        self.assertEqual(request["storage"], "blob")
        self.assertEqual(request["availability"], "available")
        self.assertEqual(request["value"]["body"], content["body"])
        self.assertEqual(request["value"]["access_token"], "[REDACTED]")
        self.assertEqual(shown["metadata"]["chapterId"], "CH-CURRENT")
        self.assertEqual(shown["status"], "success")
        self.assertEqual(self.store.list_calls()["calls"][0]["callId"], ident)
        self.assertEqual(self.store.stats()["callCount"], 1)
        self.assertNotIn("value", self.store.show(ident, include_payloads=False)["start"]["payloads"]["request"])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(mode == 0o600 for _, mode in before.values()))
        for directory in (self.root, self.root / "calls", self.root / "calls" / ident):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertNotIn(b"private-token", b"".join(data for data, _ in before.values()))

    def test_summary_and_size_limit_do_not_claim_full_payload(self):
        ident = call_id()
        payload = {"body": "summary-private-content"}
        self.store.start(ident, {"pid": os.getpid()}, {"request": sanitize(payload, self.policy)}, "summary")
        request = self.store.show(ident)["start"]["payloads"]["request"]
        self.assertEqual((request["capture"], request["availability"]), ("summary", "not_stored"))
        self.assertNotIn(b"summary-private-content", b"".join(data for data, _ in self.snapshot().values()))
        self.policy["maxPayloadBytes"] = 128
        self.policy["inlinePayloadBytes"] = 64
        self.finish(ident, value="oversized-private-content" * 100)
        output = self.store.show(ident)["finish"]["payloads"]["response"]
        self.assertEqual((output["capture"], output["storage"]), ("omitted", "none"))
        self.assertIn("payload_size_limit", output["issues"])
        self.assertGreater(output["originalBytes"], self.policy["maxPayloadBytes"])
        self.assertNotIn(b"oversized-private-content", b"".join(data for data, _ in self.snapshot().values()))

    def test_missing_corrupt_and_escaping_references_are_visible(self):
        self.policy["inlinePayloadBytes"] = 16
        ident = self.begin(value={"body": "完整正文" * 100})
        payload = self.store.show(ident)["start"]["payloads"]["request"]
        reference = self.root / "calls" / ident / payload["ref"]
        original = reference.read_bytes()
        reference.unlink()
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["availability"], "missing")
        reference.write_bytes(original + b"altered")
        reference.chmod(0o600)
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["availability"], "corrupt")
        event_path = self.root / "calls" / ident / "start.json"
        event = json.loads(event_path.read_text())
        event["payloads"]["request"]["ref"] = "../../outside-secret.json"
        event_path.write_bytes(json_bytes(event))
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["availability"], "corrupt")

    def test_missing_finish_and_failed_finish_never_look_successful(self):
        ident = self.begin(value={"input": "current"})
        self.assertEqual(self.store.list_calls()["calls"][0]["status"], "incomplete")
        with patch("scripts.core.call_log_store._write_new", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.finish(ident, value={"completedBusiness": True})
        shown = self.store.show(ident)
        self.assertEqual(shown["status"], "incomplete")
        self.assertIsNone(shown["finish"])
        self.assertIn("finish_missing", shown["issues"])
        self.finish(ident, value={"completedBusiness": True})
        before = self.snapshot()
        with self.assertRaises(CallLogStoreError):
            self.finish(ident, value={"differentBusiness": True})
        self.assertEqual(self.snapshot(), before)

    def test_killed_process_leaves_a_durable_incomplete_record(self):
        ident = call_id()
        script = """
import os, signal, sys
from pathlib import Path
from scripts.core.call_log_store import CallLogStore
from scripts.core.call_log_redaction import sanitize
from scripts.core.project import call_logging_policy
policy = call_logging_policy()
store = CallLogStore(Path(sys.argv[1]), policy)
store.start(sys.argv[2], {"pid": os.getpid()}, {"request": sanitize({"body": "durable input"}, policy)}, "full")
os.kill(os.getpid(), signal.SIGKILL)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), ident],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=10)
        self.assertLess(result.returncode, 0)
        shown = self.store.show(ident)
        self.assertEqual(shown["status"], "incomplete")
        self.assertEqual(shown["start"]["payloads"]["request"]["value"], {"body": "durable input"})
        self.assertEqual(self.store.prune(all_calls=True)["removedCallIds"], [ident])

    def test_parallel_calls_keep_each_request_and_response_together(self):
        def run(index):
            ident = self.begin(value={"input": index})
            self.finish(ident, value={"output": index})
            return ident, index

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(8)))
        self.assertEqual(len(self.store.list_calls()["calls"]), 8)
        for ident, index in results:
            shown = self.store.show(ident)
            self.assertEqual(shown["start"]["payloads"]["request"]["value"], {"input": index})
            self.assertEqual(shown["finish"]["payloads"]["response"]["value"], {"output": index})

    def test_long_held_lock_has_a_bounded_explicit_failure(self):
        ident = self.begin()
        self.policy["lockTimeoutSeconds"] = 0.04
        self.policy["lockRetrySeconds"] = 0.005
        with self.store._lock():
            started = time.monotonic()
            with self.assertRaises(CallLogStoreError) as caught:
                self.finish(ident)
            elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "LOG_STORE_BUSY")
        self.assertGreaterEqual(elapsed, self.policy["lockTimeoutSeconds"])
        self.assertLess(elapsed, 1)
        self.assertEqual(self.store.show(ident)["status"], "incomplete")

    def test_count_capacity_preserves_active_call_and_evicts_completed_call(self):
        self.policy["maxCalls"] = 1
        first = self.begin(value="in progress")
        with self.assertRaises(CallLogStoreError) as caught:
            self.begin(value="must not evict active")
        self.assertEqual(caught.exception.code, "LOG_CAPACITY_EXCEEDED")
        self.assertEqual(self.store.show(first)["status"], "incomplete")
        self.finish(first)
        second = self.begin(value="next call")
        self.assertEqual([row["callId"] for row in self.store.list_calls()["calls"]], [second])
        self.assertEqual(self.store.prune(all_calls=True)["activeRetained"], 1)

    def test_retention_removes_old_finished_and_dead_calls_but_keeps_live_calls(self):
        old = self.begin(startedAt="2000-01-01T00:00:00+00:00")
        self.finish(old)
        new = self.begin()
        self.assertFalse((self.root / "calls" / old).exists())
        dead = self.begin(pid=2147483647)
        self.assertEqual(self.store.show(dead)["status"], "incomplete")
        cleaned = self.store.prune(all_calls=True)
        self.assertIn(dead, cleaned["removedCallIds"])
        self.assertNotIn(new, cleaned["removedCallIds"])

    def test_byte_capacity_fails_without_deleting_live_evidence(self):
        ident = self.begin(value="known input")
        before = self.snapshot()
        self.policy["maxStoreBytes"] = self.store.stats()["storedBytes"] + 20
        with self.assertRaises(CallLogStoreError) as caught:
            self.finish(ident, value="new output")
        self.assertEqual(caught.exception.code, "LOG_CAPACITY_EXCEEDED")
        self.assertEqual(self.snapshot(), before)

    def test_symlinks_and_corrupt_configuration_cannot_read_external_content(self):
        external = Path(self.temporary.name) / "external.json"
        external.write_text('{"private":"do not disclose"}')
        external.chmod(0o600)
        self.root.symlink_to(external.parent, target_is_directory=True)
        with self.assertRaises(CallLogStoreError):
            self.store.stats()
        self.root.unlink()
        self.store.write_config({"schemaVersion": 1, "mode": "full"})
        config = self.root / "config.json"
        config.unlink()
        config.symlink_to(external)
        with self.assertRaises(CallLogStoreError) as caught:
            self.store.read_config()
        self.assertEqual(str(caught.exception), "LOG_CONFIG_UNREADABLE")
        config.unlink()
        config.write_text("{broken")
        config.chmod(0o600)
        with self.assertRaises(CallLogStoreError) as caught:
            self.store.read_config()
        self.assertEqual(str(caught.exception), "LOG_CONFIG_INVALID")
        self.assertEqual(external.read_text(), '{"private":"do not disclose"}')

    def test_reference_symlink_and_hard_link_are_not_followed(self):
        self.policy["inlinePayloadBytes"] = 16
        ident = self.begin(value="body" * 100)
        descriptor = self.store.show(ident)["start"]["payloads"]["request"]
        reference = self.root / "calls" / ident / descriptor["ref"]
        external = Path(self.temporary.name) / "private"
        external.write_bytes(reference.read_bytes())
        external.chmod(0o600)
        reference.unlink()
        reference.symlink_to(external)
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["availability"], "corrupt")
        reference.unlink()
        os.link(external, reference)
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["availability"], "corrupt")

    def test_warm_retention_cache_avoids_reopening_unchanged_event_text(self):
        ident = self.begin(value={"body": "unchanged prose" * 400})
        self.finish(ident)
        self.store.prune()
        before = self.store.stats()
        with patch.object(self.store, "_read_pruning_event", side_effect=AssertionError("reopened old event")):
            self.store.prune()
        actual_bytes = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        self.assertEqual(self.store.stats()["storedBytes"], actual_bytes)
        self.assertEqual(self.store.stats()["callCount"], before["callCount"])

    def test_damaged_cache_cannot_erase_an_active_call(self):
        ident = self.begin(value="still active")
        self.store.prune()
        cache_path = self.root / ".metadata-cache.json"
        cached = json.loads(cache_path.read_text())
        cached["entries"][ident]["start"]["pid"] = 2147483647
        cache_path.write_bytes(json_bytes(cached))
        self.assertEqual(self.store.prune(all_calls=True)["removedCount"], 0)
        self.assertEqual(self.store.show(ident)["status"], "incomplete")
        cache_path.write_text("{broken cache")
        self.assertEqual(self.store.prune(all_calls=True)["removedCount"], 0)
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["value"], "still active")

    def test_changed_start_and_missing_finish_rebuild_from_real_events(self):
        ident = self.begin()
        self.finish(ident)
        self.store.prune()
        finish_path = self.root / "calls" / ident / "finish.json"
        finish_path.unlink()
        self.assertEqual(self.store.prune(all_calls=True)["removedCount"], 0)
        self.assertEqual(self.store.show(ident)["status"], "incomplete")
        self.finish(ident)
        self.store.prune()
        start_path = self.root / "calls" / ident / "start.json"
        before = start_path.stat()
        event = json.loads(start_path.read_text())
        event["metadata"]["startedAt"] = "2000-01-01T00:00:00+00:00"
        start_path.write_bytes(json_bytes(event))
        os.utime(start_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIn(ident, self.store.prune()["removedCallIds"])

    def test_metadata_is_bounded_before_directory_creation(self):
        with self.assertRaises(CallLogStoreError) as caught:
            self.begin(value="input", extra="x" * self.policy["maxMetadataBytes"])
        self.assertEqual(caught.exception.code, "LOG_METADATA_TOO_LARGE")
        self.assertFalse(self.root.exists())

    def test_truncated_finish_does_not_release_active_call_protection(self):
        ident = self.begin(value="active input")
        finish = self.root / "calls" / ident / "finish.json"
        prefix = (b'{"schemaVersion":1,"callId":' + json_bytes(ident)
                  + b',"event":"finish","metadata":{"status":"success"},"payloads":')
        finish.write_bytes(prefix)
        finish.chmod(0o600)
        self.assertEqual(self.store.show(ident)["status"], "incomplete")
        result = self.store.prune(all_calls=True)
        self.assertEqual(result["removedCount"], 0)
        self.assertEqual(result["activeRetained"], 1)
        self.assertEqual(self.store.show(ident)["start"]["payloads"]["request"]["value"], "active input")

    def test_unknown_inventory_is_not_free_capacity(self):
        self.store.write_config({"schemaVersion": 1, "mode": "full"})
        orphan = self.root / "calls" / "orphan.bin"
        orphan.write_bytes(b"x" * 8192)
        orphan.chmod(0o600)
        self.policy["maxStoreBytes"] = 4096
        self.policy["maxPayloadBytes"] = 4096
        measured = self.store.stats()
        self.assertGreaterEqual(measured["storedBytes"], 8192)
        self.assertFalse(measured["sizeComplete"])
        with self.assertRaises(CallLogStoreError) as caught:
            self.begin(value="must not treat unknown data as zero")
        self.assertEqual(caught.exception.code, "LOG_STORE_INVENTORY_INCOMPLETE")
        self.assertEqual(orphan.stat().st_size, 8192)

    def test_lowered_capacity_discards_cache_before_evicting_real_history(self):
        self.store.write_config({"schemaVersion": 1, "mode": "full"})
        payloads = self.store._payloads("start", {"request": sanitize("small body", self.policy)}, "full")[0]
        now = datetime.now(timezone.utc).isoformat()
        for number in range(500):
            ident = "CL-" + f"{number:032x}"
            directory = self.root / "calls" / ident
            directory.mkdir(mode=0o700)
            for event in ("start", "finish"):
                metadata = {"pid": os.getpid(), "startedAt": now} if event == "start" else {"status": "success"}
                path = directory / f"{event}.json"
                path.write_bytes(json_bytes({"schemaVersion": 1, "callId": ident, "event": event,
                                             "metadata": metadata, "payloads": payloads}))
                path.chmod(0o600)
        self.store.prune()
        self.assertGreater((self.root / ".metadata-cache.json").stat().st_size, 65536)
        self.policy["maxStoreBytes"] = 65536
        self.policy["maxPayloadBytes"] = 65536
        new_call = self.begin(value="new call fits after cache eviction")
        remaining = self.store.list_calls()["calls"]
        self.assertGreater(len(remaining), 1)
        self.assertLess(len(remaining), 501)
        self.assertIn(new_call, {row["callId"] for row in remaining})
        self.assertLessEqual(self.store.stats()["storedBytes"], self.policy["maxStoreBytes"])


if __name__ == "__main__":
    unittest.main()
