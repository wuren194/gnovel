"""Local CI 编排层回归。

只验证 orchestration 层——子进程门映射、证据校验、退出码契约、无副作用、Git 身份与
index 完整性、工作树字节指纹——绝不在此嵌套跑完整套件（否则就不是毫秒级单测）。
真实 git 仓库只在系统临时目录里现建（``ScratchRepo``），本仓库自身不被写入。
"""
from __future__ import annotations

import hashlib
import io
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import local_ci


def _entry(path: str, sha: str | None, *, file_type: str = "file", exists: bool = True,
           mode: int | None = 0o644, target: str | None = None) -> dict:
    entry = {"path": path, "exists": exists, "fileType": file_type if exists else "missing",
             "sha256": sha, "mode": mode if exists else None}
    if target is not None:
        entry["symlinkTarget"] = target
    return entry


def _snapshot(*, head: str = "a" * 40, branch: str | None = "main", index: str = "i" * 64,
              tracked: dict | None = None, untracked: dict | None = None, valid: bool = True,
              status: tuple[str, ...] = ()) -> dict:
    return {"valid": valid, "tracked": dict(tracked or {}), "untracked": dict(untracked or {}),
            "head": head, "branch": branch, "indexSha256": index, "status": list(status),
            "exclusions": [], "verificationReason": "PASS" if valid else "SNAPSHOT_FAILED"}


def _process(*, name: str = "gate", stdout: str = "", stderr: str = "", exit_code: int = 0,
             timed_out: bool = False, start_failed: bool = False) -> dict:
    success = exit_code == 0 and not timed_out and not start_failed
    reason = ("PROCESS_TIMEOUT" if timed_out else "PROCESS_START_FAILED" if start_failed
              else "PASS" if success else "PROCESS_FAILED")
    return {"name": name, "command": [], "exitCode": exit_code, "timedOut": timed_out,
            "processSuccess": success, "evidenceValid": not timed_out and not start_failed,
            "verificationReason": reason, "durationSeconds": 0.01, "pass": success,
            "stdout": stdout, "stderr": stderr}


class ScratchRepo:
    """仓库之外的真实 git 仓库；所有快照能力都在它上面实测。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="govel-scratch-", dir=local_ci._temp_root()))

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@example.invalid",
                        "-c", "user.name=Test", *args], check=True, capture_output=True, text=True)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def snapshot(self) -> dict:
        return local_ci.repository_snapshot(self.root)

    def dispose(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class SubprocessGateTests(unittest.TestCase):
    def test_successful_subprocess_is_a_passing_gate(self):
        gate = local_ci.run_process_gate("ok", [sys.executable, "-c", "pass"])
        self.assertTrue(gate["pass"])
        self.assertTrue(gate["processSuccess"])
        self.assertEqual(gate["exitCode"], 0)
        self.assertEqual(gate["verificationReason"], "PASS")

    def test_failed_subprocess_is_a_failing_gate(self):
        gate = local_ci.run_process_gate("bad", [sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["exitCode"], 3)
        self.assertEqual(gate["verificationReason"], "PROCESS_FAILED")

    def test_timeout_is_a_failing_gate_with_structured_reason(self):
        gate = local_ci.run_process_gate(
            "slow", [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertFalse(gate["pass"])
        self.assertTrue(gate["timedOut"])
        self.assertIsNone(gate["exitCode"])
        self.assertEqual(gate["verificationReason"], "PROCESS_TIMEOUT")

    def test_unlaunchable_command_is_a_structured_failure_not_a_traceback(self):
        gate = local_ci.run_process_gate("missing", ["/nonexistent/local-ci-helper"])
        self.assertFalse(gate["pass"])
        self.assertTrue(gate["timedOut"] is False)
        self.assertEqual(gate["verificationReason"], "PROCESS_START_FAILED")
        self.assertIsNone(gate["exitCode"])


class ParseSummaryTests(unittest.TestCase):
    def test_parses_counts_from_ok(self):
        summary = local_ci.parse_unittest_summary("Ran 1474 tests in 12.0s\n\nOK (skipped=2)\n")
        self.assertEqual(summary["testsRun"], 1474)
        self.assertEqual(summary["durationSeconds"], 12.0)
        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["result"], "OK")
        self.assertTrue(summary["parsed"])

    def test_parses_counts_from_failed(self):
        summary = local_ci.parse_unittest_summary("Ran 5 tests in 0.1s\n\nFAILED (failures=2, errors=1)")
        self.assertEqual(summary["failures"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["result"], "FAILED")

    def test_singular_test_line_and_expected_failures_are_accepted(self):
        summary = local_ci.parse_unittest_summary("Ran 1 test in 0.1s\n\nOK (skipped=1, expected failures=2)")
        self.assertEqual(summary["testsRun"], 1)
        self.assertEqual(summary["expectedFailures"], 2)
        self.assertTrue(summary["parsed"])

    def test_unparseable_output_is_flagged(self):
        summary = local_ci.parse_unittest_summary("no summary here")
        self.assertFalse(summary["parsed"])
        self.assertIsNone(summary["testsRun"])

    def test_summary_must_close_the_output(self):
        text = "Ran 10 tests in 1.0s\n\nOK\ntrailing garbage after the summary\n"
        self.assertFalse(local_ci.parse_unittest_summary(text)["parsed"])

    def test_missing_result_line_is_unparseable(self):
        self.assertFalse(local_ci.parse_unittest_summary("Ran 10 tests in 1.0s\n")["parsed"])

    def test_nested_child_summary_does_not_shadow_the_final_one(self):
        text = ("Ran 0 tests in 0.0s\n\nOK\n"
                "some child process noise\n"
                "Ran 1474 tests in 12.0s\n\nOK (skipped=1)\n")
        summary = local_ci.parse_unittest_summary(text)
        self.assertTrue(summary["parsed"])
        self.assertEqual(summary["testsRun"], 1474)

    def test_unknown_or_duplicated_result_fields_are_unparseable(self):
        self.assertFalse(local_ci.parse_unittest_summary("Ran 3 tests in 0.1s\n\nOK (weird=1)")["parsed"])
        self.assertFalse(local_ci.parse_unittest_summary("Ran 3 tests in 0.1s\n\nOK (errors=1, errors=2)")["parsed"])

    def test_log_lines_mentioning_counts_are_ignored(self):
        text = "Ran 9 tests worth of fixtures in 0.5s\nRan 12 tests in 1.0s\n\nOK\n"
        summary = local_ci.parse_unittest_summary(text)
        self.assertTrue(summary["parsed"])
        self.assertEqual(summary["testsRun"], 12)


class FullTestGateTests(unittest.TestCase):
    """完整套件门必须同时要求进程成功与证据有效。"""

    def _gate(self, stdout: str = "", stderr: str = "", *, exit_code: int = 0,
              timed_out: bool = False, start_failed: bool = False) -> dict:
        fake = _process(name="fullTests", stdout=stdout, stderr=stderr, exit_code=exit_code,
                        timed_out=timed_out, start_failed=start_failed)
        with patch.object(local_ci, "run_process_gate", lambda *a, **k: fake):
            return local_ci._unittest_gate('fullTests', ['synthetic'], None)

    def test_ok_summary_is_a_pass(self):
        gate = self._gate(stderr="Ran 10 tests in 0.4s\n\nOK\n")
        self.assertTrue(gate["pass"])
        self.assertTrue(gate["evidenceValid"])
        self.assertEqual(gate["verificationReason"], "PASS")
        self.assertEqual(gate["summary"]["testsRun"], 10)

    def test_zero_tests_is_a_failure_even_with_ok_and_exit_zero(self):
        gate = self._gate(stderr="Ran 0 tests in 0.000s\n\nOK\n")
        self.assertFalse(gate["pass"])
        self.assertTrue(gate["processSuccess"])
        self.assertEqual(gate["verificationReason"], "ZERO_TESTS_EXECUTED")

    def test_exit_zero_without_any_summary_is_a_failure(self):
        gate = self._gate(stderr="import errors happened, no tests ran\n")
        self.assertFalse(gate["pass"])
        self.assertTrue(gate["processSuccess"])
        self.assertFalse(gate["evidenceValid"])
        self.assertEqual(gate["verificationReason"], "TEST_SUMMARY_UNPARSEABLE")

    def test_parsed_failures_are_a_failure(self):
        gate = self._gate(stderr="Ran 5 tests in 0.1s\n\nFAILED (failures=2)\n", exit_code=1)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["summary"]["failures"], 2)
        self.assertEqual(gate["verificationReason"], "TEST_FAILURES_PRESENT")

    def test_parsed_errors_are_a_failure(self):
        gate = self._gate(stderr="Ran 5 tests in 0.1s\n\nFAILED (errors=1)\n", exit_code=1)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "TEST_FAILURES_PRESENT")

    def test_unexpected_successes_are_a_failure(self):
        gate = self._gate(stderr="Ran 5 tests in 0.1s\n\nOK (unexpected successes=1)\n")
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "TEST_FAILURES_PRESENT")

    def test_skips_and_expected_failures_do_not_block(self):
        gate = self._gate(stderr="Ran 5 tests in 0.1s\n\nOK (skipped=3, expected failures=2)\n")
        self.assertTrue(gate["pass"])
        self.assertEqual(gate["summary"]["skipped"], 3)
        self.assertEqual(gate["summary"]["expectedFailures"], 2)

    def test_process_failure_with_valid_evidence_reports_the_process_failure(self):
        gate = self._gate(stderr="Ran 5 tests in 0.1s\n\nOK\n", exit_code=1)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "PROCESS_FAILED")

    def test_timeout_is_a_failure(self):
        gate = self._gate(timed_out=True)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "TEST_SUMMARY_UNPARSEABLE")
        self.assertTrue(gate["process"]["timedOut"])

    def test_stdout_noise_does_not_break_evidence(self):
        gate = self._gate(stdout="child wrote to the inherited stdout\n",
                          stderr="Ran 10 tests in 0.4s\n\nOK\n")
        self.assertTrue(gate["pass"])
        self.assertIn("child wrote", gate["process"]["stdoutTail"])

    def test_test_count_is_evidence_not_a_contract(self):
        for count in (1, 10, 99999):
            with self.subTest(count=count):
                gate = self._gate(stderr=f"Ran {count} tests in 1.0s\n\nOK\n")
                self.assertTrue(gate["pass"])
                self.assertEqual(gate["summary"]["testsRun"], count)

    def test_gate_exposes_process_evidence_and_verdict_separately(self):
        gate = self._gate(stderr="no summary\n")
        self.assertIs(gate["processSuccess"], True)
        self.assertIs(gate["evidenceValid"], False)
        self.assertIs(gate["pass"], False)


class ParallelFullTestGateTests(unittest.TestCase):
    def test_default_worker_limit_is_read_from_policy_and_explicit_jobs_are_preserved(self):
        from scripts.core import project
        policy = json.loads((local_ci.ROOT / 'shared/registries/local-ci.json').read_text())
        with patch.object(local_ci.os, 'cpu_count', return_value=10):
            self.assertEqual(local_ci.test_worker_count(), min(10, policy['defaultMaxWorkers']))
            for limit in (1, 3):
                with patch.object(project, 'cached_json', return_value={'schemaVersion': 1, 'defaultMaxWorkers': limit}):
                    self.assertEqual(local_ci.test_worker_count(), limit)
                    self.assertEqual(local_ci.test_worker_count(4), 4)
        for cores in (None, 1):
            with patch.object(local_ci.os, 'cpu_count', return_value=cores):
                self.assertEqual(local_ci.test_worker_count(), 1)
        for jobs in (True, 0, -1, 1.5):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                local_ci.test_worker_count(jobs)

    def test_invalid_default_worker_policy_fails_before_dispatch(self):
        from scripts.core import project
        for policy in ({}, {'schemaVersion': 1, 'defaultMaxWorkers': True},
                       {'schemaVersion': 1, 'defaultMaxWorkers': 0},
                       {'schemaVersion': 1, 'defaultMaxWorkers': '2'}):
            with self.subTest(policy=policy), patch.object(project, 'cached_json', return_value=policy), \
                    patch.object(local_ci, 'discover_test_modules') as discover, \
                    patch.object(local_ci, '_module_queue') as queue:
                with self.assertRaises(ValueError):
                    local_ci.full_test_gate()
                discover.assert_not_called()
                queue.assert_not_called()

    def test_full_and_quick_worker_dispatch_share_the_default_limit(self):
        from scripts.core import project
        for full in (False, True):
            calls = []
            def queue(rows, jobs, directory, timeout):
                calls.append(jobs)
                return [row for row in self.runs() if row['module'] in {item['module'] for item in rows}]
            with self.subTest(full=full), patch.object(project, 'local_ci_policy', return_value={'defaultMaxWorkers': 2}), \
                    patch.object(local_ci.os, 'cpu_count', return_value=10), \
                    patch.object(local_ci, 'discover_test_modules', return_value=self.discovery()), \
                    patch.object(local_ci, '_module_queue', queue):
                result = local_ci.full_test_gate() if full else local_ci.discovered_test_gate(self.discovery())
            self.assertTrue(result['pass'])
            self.assertEqual(result['workerCount'], 2)
            self.assertEqual(calls, [1, 2])

    def test_timeout_reaps_owned_children_before_the_lane_returns(self):
        import time
        with tempfile.TemporaryDirectory() as temp:
            late = Path(temp) / 'late-write'
            child = f"import time; from pathlib import Path; time.sleep(.6); Path({str(late)!r}).write_text('escaped')"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
            result = local_ci._runner().run_owned_process('timeout-probe', [sys.executable, '-c', parent],
                cwd=local_ci.ROOT, env=local_ci._subprocess_env(), timeout=.15)
            self.assertTrue(result['timedOut']); self.assertFalse(result['pass'])
            time.sleep(.7)
            self.assertFalse(late.exists(), 'a timed out worker must not leave a child able to publish later')

    def discovery(self):
        def identity(module, count):
            ids = [f'{module}.Probe.test_{index}' for index in range(count)]
            return {'module': module, 'count': count, 'testIds': ids,
                    'testIdsSha256': hashlib.sha256(json.dumps(ids, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()}
        return {'pass': True, 'schemaVersion': 1, 'testsDiscovered': 3,
                'modules': [identity('test_one', 1), identity('test_two', 2)]}

    def runs(self):
        return [{'module': row['module'], 'pass': True, 'processSuccess': True, 'evidenceValid': True,
                 'summary': {'testsRun': row['count'], 'failures': 0, 'errors': 0, 'skipped': 0,
                             'expectedFailures': 0, 'unexpectedSuccesses': 0}, 'durationSeconds': row['count']}
                for row in self.discovery()['modules']]

    def aggregate(self, runs):
        return local_ci.aggregate_test_modules(self.discovery(), runs, jobs=2, elapsed=3)

    def test_all_modules_and_counts_are_accounted_for_and_timings_sorted(self):
        report = self.aggregate(self.runs())
        self.assertTrue(report['pass']); self.assertEqual(report['summary']['testsRun'], 3)
        self.assertEqual(report['workerCount'], 2)
        self.assertEqual([row['module'] for row in report['moduleTimings']], ['test_two', 'test_one'])

    def test_worker_failure_is_never_hidden_by_other_successes(self):
        rows = self.runs(); rows[0].update(processSuccess=False)
        rows[0]['pass'] = False
        report = self.aggregate(rows)
        self.assertFalse(report['pass']); self.assertEqual(report['verificationReason'], 'TEST_WORKER_FAILED')

    def test_missing_summary_is_not_a_passing_worker(self):
        rows = self.runs(); rows[0].update(summary={}, evidenceValid=False)
        report = self.aggregate(rows)
        self.assertFalse(report['pass']); self.assertFalse(report['evidenceValid'])

    def test_missing_duplicate_or_unexpected_modules_fail_closed(self):
        for rows in (self.runs()[:1], self.runs() + self.runs()[:1],
                     [self.runs()[0], {**self.runs()[1], 'module': 'test_unknown'}]):
            with self.subTest(rows=rows):
                report = self.aggregate(rows)
                self.assertEqual(report['verificationReason'], 'TEST_MODULE_SET_MISMATCH')
                self.assertFalse(report['pass'])

    def test_count_mismatch_fails_even_when_total_is_unchanged(self):
        rows = self.runs(); rows[0]['summary']['testsRun'] = 2; rows[1]['summary']['testsRun'] = 1
        report = self.aggregate(rows)
        self.assertEqual(report['verificationReason'], 'TEST_COUNT_MISMATCH'); self.assertFalse(report['pass'])

    def test_module_verifies_dedicated_evidence_and_unittest_summary(self):
        expected = self.discovery()['modules'][0]
        for failure in ('missing-summary', 'crash', 'timeout', 'wrong-id', 'wrong-count', 'none'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                def fake(name, argv, **kwargs):
                    output = Path(argv[-1])
                    payload = {**expected, 'schemaVersion': 1, 'testsRun': 1, 'failures': 0, 'errors': 0,
                               'skipped': 0, 'skippedTests': [], 'expectedFailures': 0, 'unexpectedSuccesses': 0, 'loaderErrors': []}
                    if failure == 'wrong-id': payload['testIdsSha256'] = 'c' * 64
                    if failure == 'wrong-count': payload['testsRun'] = 2
                    output.write_text(json.dumps(payload))
                    return _process(name=name, stderr='nothing' if failure == 'missing-summary' else 'Ran 1 test in 0.1s\n\nOK\n',
                                    exit_code=-9 if failure == 'crash' else 0, timed_out=failure == 'timeout')
                with patch.object(local_ci._runner(), 'run_owned_process', fake):
                    result = local_ci._run_test_module(expected, directory, 1)
                self.assertEqual(result['pass'], failure == 'none')

    def test_serial_lane_never_overlaps_parallel_queue_and_jobs_one_is_supported(self):
        for module in ('test_browser_example', 'test_entity_card_projection'):
            with self.subTest(module=module):
                discovery = self.discovery(); discovery['modules'][0]['module'] = module
                calls = []
                def queue(rows, jobs, directory, timeout):
                    calls.append(([row['module'] for row in rows], jobs))
                    return [{**self.runs()[i], 'module': row['module'], 'summary': {'testsRun': row['count']}}
                            for i, row in enumerate(rows)]
                with patch.object(local_ci, 'discover_test_modules', return_value=discovery), patch.object(local_ci, '_module_queue', queue):
                    result = local_ci.full_test_gate(jobs=1)
                self.assertTrue(result['pass'])
                self.assertEqual(calls, [([module], 1), (['test_two'], 1)])

    def test_discovery_error_prevents_any_test_dispatch(self):
        with patch.object(local_ci, 'discover_test_modules', return_value={'pass': False}), patch.object(local_ci, '_module_queue') as queue:
            report = local_ci.full_test_gate(jobs=2)
        queue.assert_not_called(); self.assertFalse(report['pass'])

    def test_real_discovery_and_worker_use_the_same_test_identity(self):
        # Tiny fixture repository: no nested run of the real full suite.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'tests').mkdir()
            (root / 'tests/test_local_ci_probe.py').write_text('import unittest\nclass Probe(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
            code = ("from pathlib import Path; from scripts.core.local_test_runner import discover; "
                    "import json; print(json.dumps(discover(Path(" + repr(str(root)) + "))))")
            proc = subprocess.run([sys.executable, '-c', code], cwd=local_ci.ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            expected = json.loads(proc.stdout)['modules'][0]
            output = root / 'result.json'
            code = ("from pathlib import Path; from scripts.core.local_test_runner import run_module; "
                    "raise SystemExit(run_module(Path(" + repr(str(root)) + "), 'test_local_ci_probe', " + repr(str(output)) + "))")
            proc = subprocess.run([sys.executable, '-c', code], cwd=local_ci.ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result['testIdsSha256'], expected['testIdsSha256'])
            self.assertEqual(local_ci.parse_unittest_summary(proc.stderr)['testsRun'], expected['count'])

    def test_real_worker_reports_entire_class_and_partial_skip_reasons(self):
        for entirely_skipped in (False, True):
            with self.subTest(entirely_skipped=entirely_skipped), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); (root / 'tests').mkdir()
                (root / 'tests/test_skip_probe.py').write_text(
                    'import unittest\n' + ('@unittest.skip("browser unavailable")\n' if entirely_skipped else '')
                    + 'class Probe(unittest.TestCase):\n'
                    + ' @unittest.skip("optional device absent")\n def test_skip(self): pass\n'
                    + ' def test_ok(self): pass\n')
                code = ('from pathlib import Path; from scripts.core.local_test_runner import discover,run_module; '
                        f'r=Path({str(root)!r}); import json; print(json.dumps(discover(r))); '
                        f'raise SystemExit(run_module(r,"test_skip_probe",{str(root / "report.json")!r}))')
                process = subprocess.run([sys.executable, '-c', code], cwd=local_ci.ROOT,
                                         capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, 0, process.stderr)
                expected = json.loads(process.stdout)['modules'][0]
                evidence = json.loads((root / 'report.json').read_text())
                self.assertTrue(local_ci._skipped_tests_valid(evidence, expected))
                self.assertEqual(evidence['skipped'], 2 if entirely_skipped else 1)
                self.assertEqual({row['reason'] for row in evidence['skippedTests']},
                                 {'browser unavailable'} if entirely_skipped else {'optional device absent'})

    def test_skip_evidence_rejects_missing_unknown_duplicate_and_wrong_count(self):
        expected = self.discovery()['modules'][1]
        rows = [{'testId': expected['testIds'][0], 'reason': 'not installed'}]
        valid = {'skipped': 1, 'skippedTests': rows}
        self.assertTrue(local_ci._skipped_tests_valid(valid, expected))
        for invalid in ({'skipped': 1}, {'skipped': 2, 'skippedTests': rows},
                        {'skipped': 2, 'skippedTests': rows * 2},
                        {'skipped': 1, 'skippedTests': [{'testId': 'test_other.Probe.test_x', 'reason': 'absent'}]},
                        {'skipped': 1, 'skippedTests': [{'testId': expected['testIds'][0], 'reason': None}]}):
            with self.subTest(invalid=invalid):
                self.assertFalse(local_ci._skipped_tests_valid(invalid, expected))


class TaskSpecificGateTests(unittest.TestCase):
    def test_no_focus_module_skips_without_blocking(self):
        gate = local_ci.task_specific_gate([])
        self.assertEqual(gate["status"], "skipped")
        self.assertTrue(gate["pass"])

    def test_focus_module_with_zero_tests_fails(self):
        fake = _process(name="task", stderr="Ran 0 tests in 0.0s\n\nOK\n")
        with patch.object(local_ci, "run_process_gate", lambda *a, **k: fake):
            gate = local_ci.task_specific_gate(["tests.test_x"])
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["runs"][0]["verificationReason"], "ZERO_TESTS_EXECUTED")


class WrapperSyncGateTests(unittest.TestCase):
    def _gate(self, *, stdout: str = "", exit_code: int = 0):
        fake = _process(name="wrapperSync", stdout=stdout, exit_code=exit_code)
        with patch.object(local_ci, "run_process_gate", lambda *a, **k: fake):
            return local_ci.wrapper_sync_gate()

    def test_ok_payload_is_a_pass(self):
        gate = self._gate(stdout=json.dumps({"ok": True, "drift": []}))
        self.assertTrue(gate["pass"])
        self.assertTrue(gate["evidenceValid"])
        self.assertEqual(gate["status"], {"ok": True, "drift": []})

    def test_reported_drift_is_a_failure(self):
        gate = self._gate(stdout=json.dumps({"ok": False, "drift": ["a.md"]}))
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "WRAPPER_DRIFT_PRESENT")

    def test_claimed_json_mode_that_is_not_json_is_a_failure(self):
        gate = self._gate(stdout="drift: a.md\n")
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["evidenceValid"])
        self.assertFalse(gate["jsonValid"])
        self.assertEqual(gate["verificationReason"], "WRAPPER_EVIDENCE_INVALID")

    def test_payload_with_unexpected_shape_is_a_failure(self):
        for payload in ({"ok": "true", "drift": []}, {"ok": True}, [], {"ok": True, "drift": [1]}):
            with self.subTest(payload=payload):
                gate = self._gate(stdout=json.dumps(payload))
                self.assertFalse(gate["pass"])
                self.assertEqual(gate["verificationReason"], "WRAPPER_EVIDENCE_INVALID")


class CheckerBaselineGateTests(unittest.TestCase):
    @staticmethod
    def _payload(*, passed: bool = True, regressed: bool = False, comparable: bool = True,
                 comparison: bool = True) -> dict:
        report = {"passed": passed, "summary": {"cases": 3}}
        if comparison:
            report["baselineComparison"] = {"comparable": comparable, "regressed": regressed,
                                            "regressions": ["x"] if regressed else []}
        return report

    def _gate(self, payload: dict | None = None, *, write: bool = True, malformed: bool = False,
              exit_code: int = 0):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "checker-evaluation.json"

            def fake(*args, **kwargs):
                if write:
                    output.write_text("{" if malformed else json.dumps(payload or {}), encoding="utf-8")
                return _process(name="checkerBaseline", exit_code=exit_code)

            with patch.object(local_ci, "run_process_gate", fake):
                return local_ci.checker_baseline_gate(temp)

    def test_clean_baseline_comparison_is_a_pass(self):
        gate = self._gate(self._payload())
        self.assertTrue(gate["pass"])
        self.assertTrue(gate["outputExists"])
        self.assertTrue(gate["jsonValid"])
        self.assertEqual(gate["baselineResult"]["regressed"], False)

    def test_missing_output_file_is_a_failure_despite_exit_zero(self):
        gate = self._gate(write=False)
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["outputExists"])
        self.assertEqual(gate["verificationReason"], "CHECKER_OUTPUT_MISSING")

    def test_malformed_output_is_a_failure_despite_exit_zero(self):
        gate = self._gate(malformed=True)
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["jsonValid"])
        self.assertEqual(gate["verificationReason"], "CHECKER_EVIDENCE_INVALID")

    def test_failed_evaluation_is_a_failure(self):
        gate = self._gate(self._payload(passed=False))
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "CHECKER_BASELINE_FAILED")

    def test_regression_is_a_failure(self):
        gate = self._gate(self._payload(regressed=True))
        self.assertFalse(gate["pass"])
        self.assertTrue(gate["baselineResult"]["regressed"])

    def test_incomparable_baseline_is_a_failure(self):
        gate = self._gate(self._payload(comparable=False))
        self.assertFalse(gate["pass"])

    def test_missing_baseline_comparison_is_a_failure(self):
        gate = self._gate(self._payload(comparison=False))
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "CHECKER_EVIDENCE_INVALID")

    def test_stale_output_from_a_previous_run_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "checker-evaluation.json").write_text("{}", encoding="utf-8")
            with patch.object(local_ci, "run_process_gate", lambda *a, **k: _process()):
                with self.assertRaises(ValueError):
                    local_ci.checker_baseline_gate(temp)


class WhitespaceGateTests(unittest.TestCase):
    def test_all_three_git_checks_must_pass(self):
        calls = []

        def fake(name, argv, **kwargs):
            calls.append(name)
            return _process(name=name, exit_code=0 if name == "git-diff-check" else 1)

        with patch.object(local_ci, "run_process_gate", fake):
            gate = local_ci.whitespace_gate()
        self.assertEqual(calls, ["git-diff-check", "git-diff-cached-check", "git-show-check"])
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["verificationReason"], "WHITESPACE_CHECK_FAILED")

    def test_clean_whitespace_passes(self):
        with patch.object(local_ci, "run_process_gate", lambda name, argv, **k: _process(name=name)):
            self.assertTrue(local_ci.whitespace_gate()["pass"])

    def test_real_git_staged_unstaged_and_committed_whitespace_are_independent(self):
        for scope, field in (('unstaged', 'diffCheck'), ('staged', 'cachedDiffCheck'), ('head', 'showCheckHead')):
            with self.subTest(scope=scope):
                repo = ScratchRepo(); self.addCleanup(repo.dispose)
                repo.git('init', '-q'); repo.write('entry.txt', 'clean\n')
                repo.git('add', '.'); repo.git('commit', '-qm', 'initial')
                with patch.object(local_ci, 'ROOT', repo.root):
                    self.assertTrue(local_ci.whitespace_gate()['pass'])
                    repo.write('entry.txt', 'bad trailing space \n')
                    if scope != 'unstaged': repo.git('add', '.')
                    if scope == 'head': repo.git('commit', '-qm', 'fixture with whitespace')
                    gate = local_ci.whitespace_gate()
                    self.assertFalse(gate['pass']); self.assertFalse(gate[field]['pass'])
                    for other in {'diffCheck', 'cachedDiffCheck', 'showCheckHead'} - {field}:
                        self.assertTrue(gate[other]['pass'], other)

    def test_git_unavailable_or_timeout_never_passes(self):
        for process in (_process(start_failed=True), _process(timed_out=True)):
            with self.subTest(reason=process['verificationReason']), patch.object(local_ci, 'run_process_gate', return_value=process):
                gate = local_ci.whitespace_gate()
                self.assertFalse(gate['pass']); self.assertFalse(gate['evidenceValid'])


class GitIdentityGateTests(unittest.TestCase):
    def test_unchanged_identity_is_a_pass(self):
        gate = local_ci.git_identity_gate(_snapshot(), _snapshot())
        self.assertTrue(gate["pass"])
        self.assertIs(gate["headUnchanged"], True)
        self.assertIs(gate["indexUnchanged"], True)

    def test_head_change_is_detected(self):
        gate = local_ci.git_identity_gate(_snapshot(), _snapshot(head="b" * 40))
        self.assertFalse(gate["pass"])
        self.assertIn("GIT_IDENTITY_CHANGED", gate["reasons"])

    def test_branch_change_is_detected(self):
        gate = local_ci.git_identity_gate(_snapshot(), _snapshot(branch="other"))
        self.assertFalse(gate["pass"])
        self.assertIn("GIT_IDENTITY_CHANGED", gate["reasons"])

    def test_index_change_is_detected(self):
        gate = local_ci.git_identity_gate(_snapshot(), _snapshot(index="j" * 64))
        self.assertFalse(gate["pass"])
        self.assertIn("GIT_INDEX_MUTATED", gate["reasons"])

    def test_invalid_snapshots_fail_closed(self):
        gate = local_ci.git_identity_gate(_snapshot(), _snapshot(valid=False))
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["evidenceValid"])
        self.assertEqual(gate["reasons"], ["SNAPSHOT_UNAVAILABLE"])


class WorkingTreeGateTests(unittest.TestCase):
    """同一行 git status ≠ 同一份字节；这里逐字节比对。"""

    CLEAN = _snapshot(tracked={"a.py": _entry("a.py", "h1")})

    def _gate(self, before: dict, after: dict) -> dict:
        return local_ci.working_tree_gate(before, after)

    def test_clean_before_and_after_is_a_pass(self):
        gate = self._gate(self.CLEAN, self.CLEAN)
        self.assertTrue(gate["pass"])
        self.assertIs(gate["repositoryMutatedByCI"], False)
        self.assertEqual(gate["changes"], [])

    def test_preexisting_modification_left_alone_is_a_pass(self):
        before = _snapshot(tracked={"a.py": _entry("a.py", "dirty")}, status=(" M a.py",))
        self.assertTrue(self._gate(before, before)["pass"])

    def test_preexisting_modification_changed_again_is_detected(self):
        before = _snapshot(tracked={"a.py": _entry("a.py", "dirty")}, status=(" M a.py",))
        after = _snapshot(tracked={"a.py": _entry("a.py", "dirtier")}, status=(" M a.py",))
        gate = self._gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsContentChanged"], ["a.py"])
        self.assertEqual(gate["trackedChanged"], ["a.py"])
        self.assertEqual(gate["changes"][0]["beforeHash"], "dirty")
        self.assertEqual(gate["changes"][0]["afterHash"], "dirtier")

    def test_preexisting_untracked_file_left_alone_is_a_pass(self):
        before = _snapshot(untracked={"note.txt": _entry("note.txt", "n1")})
        self.assertTrue(self._gate(before, before)["pass"])

    def test_preexisting_untracked_file_content_change_is_detected(self):
        before = _snapshot(untracked={"note.txt": _entry("note.txt", "n1")})
        after = _snapshot(untracked={"note.txt": _entry("note.txt", "n2")})
        gate = self._gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["untrackedChanged"], ["note.txt"])
        self.assertEqual(gate["trackedChanged"], [])

    def test_new_untracked_file_is_detected(self):
        after = _snapshot(untracked={"new.txt": _entry("new.txt", "n1")})
        gate = self._gate(_snapshot(), after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsAdded"], ["new.txt"])
        self.assertEqual(gate["untrackedChanged"], ["new.txt"])

    def test_deleted_tracked_file_is_detected(self):
        after = _snapshot(tracked={"a.py": _entry("a.py", None, exists=False, mode=None)})
        gate = self._gate(self.CLEAN, after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsRemoved"], ["a.py"])
        self.assertEqual(gate["pathsContentChanged"], [])

    def test_unexpectedly_restored_tracked_file_is_detected(self):
        before = _snapshot(tracked={"a.py": _entry("a.py", None, exists=False, mode=None)})
        gate = self._gate(before, self.CLEAN)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsAdded"], ["a.py"])

    def test_tracked_to_untracked_transition_is_detected(self):
        before = _snapshot(tracked={"a.py": _entry("a.py", "h1")})
        after = _snapshot(untracked={"a.py": _entry("a.py", "h1")})
        gate = self._gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertIn("tracked_path_set_changed", gate["changes"][0]["changeTypes"])
        self.assertEqual(gate["trackedChanged"], ["a.py"])
        self.assertEqual(gate["pathsContentChanged"], [])

    def test_type_change_is_detected(self):
        before = _snapshot(tracked={"a.py": _entry("a.py", "h1")})
        after = _snapshot(tracked={"a.py": _entry("a.py", "h2", file_type="symlink", target="b.py")})
        self.assertIn("type_changed", self._gate(before, after)["changes"][0]["changeTypes"])

    def test_mode_change_is_detected(self):
        before = _snapshot(tracked={"a.sh": _entry("a.sh", "h1", mode=0o644)})
        after = _snapshot(tracked={"a.sh": _entry("a.sh", "h1", mode=0o755)})
        gate = self._gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertIn("mode_changed", gate["changes"][0]["changeTypes"])

    def test_symlink_target_text_change_is_detected_without_reading_the_target(self):
        before = _snapshot(tracked={"link": _entry("link", "t1", file_type="symlink", target="one.md")})
        after = _snapshot(tracked={"link": _entry("link", "t2", file_type="symlink", target="two.md")})
        gate = self._gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsContentChanged"], ["link"])
        self.assertEqual(gate["changes"][0]["after"]["symlinkTarget"], "two.md")

    def test_index_and_head_mutation_also_fail_the_integrity_gate(self):
        indexed = self._gate(self.CLEAN, _snapshot(tracked={"a.py": _entry("a.py", "h1")}, index="j" * 64))
        self.assertFalse(indexed["pass"])
        self.assertIs(indexed["repositoryMutatedByCI"], True)
        self.assertEqual(indexed["verificationReason"], "GIT_INDEX_MUTATED")
        headed = self._gate(self.CLEAN, _snapshot(head="b" * 40, tracked={"a.py": _entry("a.py", "h1")}))
        self.assertFalse(headed["pass"])
        self.assertEqual(headed["verificationReason"], "GIT_IDENTITY_CHANGED")

    def test_invalid_snapshots_fail_closed(self):
        gate = self._gate(_snapshot(valid=False), self.CLEAN)
        self.assertFalse(gate["pass"])
        self.assertFalse(gate["evidenceValid"])
        self.assertIsNone(gate["repositoryMutatedByCI"])
        self.assertEqual(gate["verificationReason"], "SNAPSHOT_UNAVAILABLE")

    def test_snapshot_evidence_is_compact_but_still_diffable(self):
        before = _snapshot(tracked={"a.py": _entry("a.py", "h1")},
                           untracked={"note.txt": _entry("note.txt", "n1")})
        after = _snapshot(tracked={"a.py": _entry("a.py", "h1")})
        gate = local_ci.working_tree_gate(before, after)
        for side in ("before", "after"):
            self.assertNotIn("tracked", gate[side])
            self.assertNotIn("untracked", gate[side])
        self.assertEqual(gate["before"]["trackedPaths"], 1)
        self.assertEqual(gate["before"]["untrackedPaths"], 1)
        self.assertNotEqual(gate["before"]["untrackedFingerprint"], gate["after"]["untrackedFingerprint"])
        self.assertTrue(json.dumps(gate))


class RepositorySnapshotTests(unittest.TestCase):
    """真实 git 仓库上的快照能力（建在仓库之外的临时目录）。"""

    def setUp(self):
        self.repo = ScratchRepo()
        self.repo.git("init", "-q")
        self.repo.write("正文/a.md", "alpha\n")
        self.repo.write("规划/b.md", "beta\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "init")
        self.addCleanup(self.repo.dispose)

    def test_clean_repository_tracks_file_bytes(self):
        snapshot = self.repo.snapshot()
        self.assertTrue(snapshot["valid"], snapshot.get("error"))
        self.assertEqual(sorted(snapshot["tracked"]), sorted(["规划/b.md", "正文/a.md"]))
        self.assertEqual(snapshot["untracked"], {})
        self.assertEqual(snapshot["tracked"]["正文/a.md"]["sha256"],
                         hashlib.sha256(b"alpha\n").hexdigest())
        self.assertNotIn(".git", snapshot["status"])

    def test_new_untracked_file_is_visible_to_the_snapshot(self):
        before = self.repo.snapshot()
        self.repo.write("笔记/临时.md", "scratch\n")
        after = self.repo.snapshot()
        gate = local_ci.working_tree_gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsAdded"], ["笔记/临时.md"])
        self.assertIs(gate["repositoryMutatedByCI"], True)

    def test_ignored_cache_directory_is_never_walked(self):
        self.repo.write(".gitignore", "cache/\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "ignore cache")
        before = self.repo.snapshot()
        (self.repo.root / "cache").mkdir()
        (self.repo.root / "cache" / "huge.bin").write_bytes(b"x" * 4096)
        after = self.repo.snapshot()
        self.assertNotIn("cache/huge.bin", after["untracked"])
        self.assertTrue(local_ci.working_tree_gate(before, after)["pass"])

    def test_preexisting_dirty_file_changing_again_is_detected(self):
        path = self.repo.write("正文/a.md", "dirty one\n")
        before = self.repo.snapshot()
        path.write_text("dirty two\n", encoding="utf-8")
        after = self.repo.snapshot()
        gate = local_ci.working_tree_gate(before, after)
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsContentChanged"], ["正文/a.md"])
        self.assertEqual(before["status"], after["status"])  # porcelain 行完全相同

    def test_deleting_a_tracked_file_is_detected(self):
        before = self.repo.snapshot()
        (self.repo.root / "规划/b.md").unlink()
        gate = local_ci.working_tree_gate(before, self.repo.snapshot())
        self.assertFalse(gate["pass"])
        self.assertEqual(gate["pathsRemoved"], ["规划/b.md"])

    def test_staging_a_change_mutates_the_index_fingerprint(self):
        before = self.repo.snapshot()
        self.repo.write("正文/a.md", "staged\n")
        self.repo.git("add", "正文/a.md")
        after = self.repo.snapshot()
        self.assertNotEqual(before["indexSha256"], after["indexSha256"])
        self.assertFalse(local_ci.git_identity_gate(before, after)["pass"])

    def test_a_commit_moves_head_and_is_detected(self):
        before = self.repo.snapshot()
        self.repo.write("正文/a.md", "committed\n")
        self.repo.git("commit", "-q", "-am", "second")
        after = self.repo.snapshot()
        self.assertNotEqual(before["head"], after["head"])
        self.assertFalse(local_ci.git_identity_gate(before, after)["pass"])

    def test_a_symlink_is_fingerprinted_by_its_link_text_not_its_target(self):
        self.repo.write("共享/target.md", "TARGET BYTES\n")
        (self.repo.root / "共享" / "link.md").symlink_to("target.md")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "symlink")
        entry = self.repo.snapshot()["tracked"]["共享/link.md"]
        self.assertEqual(entry["fileType"], "symlink")
        self.assertEqual(entry["symlinkTarget"], "target.md")
        self.assertEqual(entry["sha256"], hashlib.sha256(b"target.md").hexdigest())
        self.assertNotEqual(entry["sha256"], hashlib.sha256(b"TARGET BYTES\n").hexdigest())

    def test_changing_a_symlinks_external_target_is_not_proof_of_its_own_change(self):
        outside = Path(tempfile.mkdtemp(prefix="govel-outside-", dir=local_ci._temp_root()))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "payload.txt").write_text("one\n", encoding="utf-8")
        (self.repo.root / "外部链接.md").symlink_to(outside / "payload.txt")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "external symlink")
        before = self.repo.snapshot()
        (outside / "payload.txt").write_text("two\n", encoding="utf-8")
        after = self.repo.snapshot()
        self.assertEqual(before["tracked"]["外部链接.md"], after["tracked"]["外部链接.md"])
        self.assertTrue(local_ci.working_tree_gate(before, after)["pass"])

    def test_file_fingerprint_refuses_unsafe_and_non_regular_paths(self):
        for relative in ("../escape.md", ".git/config", "/etc/hosts", "正文/./a.md"):
            with self.subTest(relative=relative):
                with self.assertRaises(local_ci.SnapshotError):
                    local_ci.file_fingerprint(self.repo.root, relative)
        (self.repo.root / "正文" / "subdir").mkdir()
        with self.assertRaises(local_ci.SnapshotError):
            local_ci.file_fingerprint(self.repo.root, "正文/subdir")

    def test_snapshot_of_the_real_repository_is_read_only_and_valid(self):
        snapshot = local_ci.repository_snapshot()
        self.assertTrue(snapshot["valid"], snapshot.get("error"))
        self.assertTrue(snapshot["tracked"])
        self.assertEqual(snapshot["head"], subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(local_ci.ROOT), capture_output=True,
            text=True, check=True).stdout.strip())


class RunGatesTests(unittest.TestCase):
    """编排层 fail closed：无法自证时不许跑门，门抛异常也要产出结构化失败。"""

    def _run(self, *, snapshot: dict, full_test=None) -> dict:
        stubs = {
            "full_test_gate": lambda *a, **k: {"name": "fullTests", "pass": True, "summary": {"testsRun": 5}},
            "task_specific_gate": lambda *a, **k: {"name": "taskSpecificTests", "pass": True, "status": "skipped"},
            "wrapper_sync_gate": lambda *a, **k: {"name": "wrapperSync", "pass": True},
            "checker_baseline_gate": lambda *a, **k: {"name": "checkerBaseline", "pass": True},
            "whitespace_gate": lambda *a, **k: {"name": "whitespace", "pass": True},
        }
        if full_test is not None:
            stubs["full_test_gate"] = full_test
        with ExitStack() as stack:
            stack.enter_context(patch.object(local_ci, "repository_snapshot", lambda *a, **k: snapshot))
            for name, stub in stubs.items():
                stack.enter_context(patch.object(local_ci, name, stub))
            return local_ci.run_gates([])

    def test_invalid_initial_snapshot_fails_closed_without_running_any_gate(self):
        calls = []

        def must_not_run(*args, **kwargs):
            calls.append(args)
            raise AssertionError("gates must not run without a usable baseline snapshot")

        gates = self._run(snapshot=_snapshot(valid=False), full_test=must_not_run)
        self.assertEqual(calls, [], "快照不可用时不得执行任何门")
        for key in local_ci.GATE_KEYS:
            self.assertFalse(gates[key]["pass"])
        for key in ("fullTests", "taskSpecificTests", "wrapperSync", "checkerBaseline", "whitespace"):
            self.assertEqual(gates[key]["verificationReason"], "INITIAL_SNAPSHOT_FAILED")
        for key in ("workingTreeIntegrity", "gitIdentityIntegrity"):
            self.assertEqual(gates[key]["verificationReason"], "SNAPSHOT_UNAVAILABLE")
        self.assertEqual(local_ci.decide(gates, True), ("FAIL", local_ci.EXIT_FAIL))

    def test_gate_exception_becomes_a_structured_failure(self):
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        gates = self._run(snapshot=_snapshot(), full_test=explode)
        self.assertFalse(gates["fullTests"]["pass"])
        self.assertFalse(gates["fullTests"]["evidenceValid"])
        self.assertEqual(gates["fullTests"]["verificationReason"], "GATE_EXCEPTION")
        self.assertIn("boom", gates["fullTests"]["error"])
        self.assertEqual(local_ci.decide(gates, True), ("FAIL", local_ci.EXIT_FAIL))

    def test_healthy_gates_with_a_byte_identical_repository_pass(self):
        gates = self._run(snapshot=_snapshot())
        self.assertEqual(local_ci.decide(gates, True), ("PASS", 0))
        self.assertTrue(gates["workingTreeIntegrity"]["pass"])
        self.assertTrue(gates["gitIdentityIntegrity"]["pass"])
        self.assertIs(gates["workingTreeIntegrity"]["repositoryMutatedByCI"], False)


class DecisionTests(unittest.TestCase):
    def _all_pass(self) -> dict:
        return {key: {"pass": True} for key in local_ci.GATE_KEYS}

    def test_all_pass_on_darwin_is_pass_exit0(self):
        self.assertEqual(local_ci.decide(self._all_pass(), True), ("PASS", 0))
        self.assertEqual(local_ci.EXIT_PASS, 0)

    def test_all_pass_off_darwin_is_unverified_and_nonzero(self):
        overall, code = local_ci.decide(self._all_pass(), False)
        self.assertEqual(overall, "UNVERIFIED")
        self.assertEqual(code, local_ci.EXIT_UNVERIFIED)

    def test_any_gate_failure_is_fail_and_nonzero_even_on_darwin(self):
        gates = self._all_pass()
        gates["fullTests"]["pass"] = False
        self.assertEqual(local_ci.decide(gates, True), ("FAIL", local_ci.EXIT_FAIL))

    def test_skipped_task_specific_does_not_block(self):
        gates = self._all_pass()
        gates["taskSpecificTests"] = {"pass": True, "status": "skipped"}
        self.assertEqual(local_ci.decide(gates, True), ("PASS", 0))

    def test_unknown_or_missing_evidence_is_a_failure(self):
        for broken in (None, {}, {"pass": None}, {"pass": "yes"}, {"evidenceValid": False}):
            with self.subTest(broken=broken):
                gates = self._all_pass()
                gates["wrapperSync"] = broken
                self.assertEqual(local_ci.decide(gates, True), ("FAIL", local_ci.EXIT_FAIL))
        gates = self._all_pass()
        del gates["gitIdentityIntegrity"]
        self.assertEqual(local_ci.decide(gates, True), ("FAIL", local_ci.EXIT_FAIL))

    def test_gate_keys_cover_every_reported_gate(self):
        gates = {key: {"pass": True} for key in local_ci.GATE_KEYS}
        report = local_ci.build_report({"supportedPlatform": True}, {}, gates, "PASS")
        self.assertEqual(set(local_ci.GATE_KEYS) - set(report), set())
        self.assertIn("gitIdentityIntegrity", report)

    def test_missing_gate_is_reported_as_failed_placeholder(self):
        report = local_ci.build_report({"supportedPlatform": True}, {}, {}, "FAIL")
        self.assertEqual(report["fullTests"]["verificationReason"], "GATE_MISSING")
        self.assertFalse(report["fullTests"]["pass"])


class ReportSchemaTests(unittest.TestCase):
    def test_report_has_required_schema_keys(self):
        env = local_ci.environment_info()
        gates = {key: {"pass": True} for key in local_ci.GATE_KEYS}
        gates["fullTests"] = {"pass": True, "summary": {"testsRun": 1}}
        report = local_ci.build_report(env, {"head": "abc"}, gates, "PASS")
        for key in ("kind", "platformPolicy", "environment", "git", "fullTests", "taskSpecificTests",
                    "wrapperSync", "checkerBaseline", "whitespace", "workingTreeIntegrity",
                    "gitIdentityIntegrity", "mergeGate", "overall"):
            self.assertIn(key, report)
        self.assertEqual(report["kind"], "local_ci_report")
        self.assertEqual(report["platformPolicy"], "macos-local-authoritative")
        self.assertEqual(report["mergeGate"], {"authoritative": env["supportedPlatform"], "result": "PASS"})

    def test_environment_metadata_captured(self):
        env = local_ci.environment_info()
        for key in ("platform", "system", "architecture", "pythonExecutable", "pythonVersion",
                    "repoRoot", "supportedPlatform", "supportedPlatformCheck"):
            self.assertIn(key, env)
        self.assertEqual(env["supportedPlatform"], platform.system() == "Darwin")

    def test_report_is_ascii_serializable_with_non_ascii_paths(self):
        gates = {key: {"pass": True} for key in local_ci.GATE_KEYS}
        gates["workingTreeIntegrity"] = local_ci.working_tree_gate(
            _snapshot(untracked={"笔记/临时.md": _entry("笔记/临时.md", "n1")}), _snapshot())
        report = local_ci.build_report(local_ci.environment_info(), {}, gates, "FAIL")
        encoded = json.dumps(report, ensure_ascii=True)
        self.assertTrue(encoded.isascii())
        self.assertEqual(json.loads(encoded)["workingTreeIntegrity"]["pathsRemoved"], ["笔记/临时.md"])


class OutputLocationTests(unittest.TestCase):
    def test_default_report_path_is_outside_the_repo(self):
        path = local_ci.resolve_report_path(None)
        try:
            self.assertFalse(str(path).startswith(str(local_ci.ROOT)))
            self.assertNotIn(local_ci.ROOT.resolve(), path.resolve().parents)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

    def test_report_paths_inside_the_repository_are_refused(self):
        for candidate in (str(local_ci.ROOT / "local-ci-report.json"), "local-ci-report.json",
                          str(local_ci.ROOT / "knowledge" / ".cache" / "report.json"), "."):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    local_ci.resolve_report_path(candidate)

    def test_existing_report_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            existing = Path(temp) / "report.json"
            existing.write_text("keep me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                local_ci.resolve_report_path(str(existing))
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")

    def test_temp_root_and_checker_temp_dir_live_outside_the_repository(self):
        self.assertNotIn(local_ci.ROOT.resolve(), local_ci._temp_root().parents)
        seen = {}

        def fake_checker(temp_dir, timeout=None):
            seen["dir"] = Path(temp_dir)
            return {"pass": True}

        with patch.object(local_ci, "checker_baseline_gate", fake_checker):
            local_ci._temporary_checker_gate(None)
        self.assertFalse(seen["dir"].exists(), "检查器临时目录必须自清理")
        self.assertNotIn(local_ci.ROOT.resolve(), seen["dir"].resolve().parents)


class MainWiringTests(unittest.TestCase):
    """main() 把 decide() 的退出码接出去，并把报告写到工作树之外——不跑真实门。"""

    def _gates(self, full_pass: bool = True) -> dict:
        gates = {key: {"pass": True} for key in local_ci.GATE_KEYS}
        gates["fullTests"] = {"name": "fullTests", "pass": full_pass, "processSuccess": True,
                              "evidenceValid": True, "exitCode": 0 if full_pass else 1,
                              "summary": {"testsRun": 10, "failures": 0 if full_pass else 1}}
        gates["taskSpecificTests"] = {"pass": True, "status": "skipped", "testsRun": 0, "modules": []}
        gates["workingTreeIntegrity"] = {"pass": True, "repositoryMutatedByCI": False,
                                         "pathsAdded": [], "pathsRemoved": [], "changes": []}
        gates["gitIdentityIntegrity"] = {"pass": True, "before": {"head": "a" * 40}}
        return gates

    def _run_main(self, gates: dict, argv: list[str]):
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "local-ci-report.json"
            with patch.object(local_ci, "run_gates", lambda focus, timeout=None, before=None, jobs=None: gates):
                with patch.object(local_ci, "repository_snapshot", lambda *a, **k: _snapshot()):
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        code = local_ci.main(argv + ["--report-out", str(report_path)])
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
        return code, report, buffer.getvalue()

    def test_main_returns_nonzero_and_records_fail_when_a_gate_fails(self):
        code, report, _ = self._run_main(self._gates(full_pass=False), [])
        self.assertEqual(code, local_ci.EXIT_FAIL)
        self.assertEqual(report["overall"], "FAIL")

    def test_main_pass_on_darwin_unverified_elsewhere(self):
        code, report, _ = self._run_main(self._gates(), [])
        if platform.system() == "Darwin":
            self.assertEqual((code, report["overall"]), (0, "PASS"))
        else:
            self.assertEqual((code, report["overall"]), (local_ci.EXIT_UNVERIFIED, "UNVERIFIED"))

    def test_main_json_flag_prints_the_machine_readable_report(self):
        _, _, stdout = self._run_main(self._gates(), ["--json"])
        self.assertIn('"kind": "local_ci_report"', stdout)
        self.assertIn("LOCAL CI ENVIRONMENT", stdout)

    def test_main_refuses_a_report_path_inside_the_repository(self):
        with patch.object(local_ci, "run_gates", lambda focus, timeout=None, before=None, jobs=None: self._gates()):
            with patch.object(local_ci, "repository_snapshot", lambda *a, **k: _snapshot()):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = local_ci.main(["--report-out", str(local_ci.ROOT / "local-ci-report.json")])
        self.assertEqual(code, local_ci.EXIT_FAIL)
        self.assertFalse((local_ci.ROOT / "local-ci-report.json").exists())
        self.assertIn("REPORT_DESTINATION_INVALID", buffer.getvalue())

    def test_main_reports_a_write_failure_as_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            target = blocker / "report.json"
            with patch.object(local_ci, "run_gates", lambda focus, timeout=None, before=None, jobs=None: self._gates()):
                with patch.object(local_ci, "repository_snapshot", lambda *a, **k: _snapshot()):
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        code = local_ci.main(["--report-out", str(target)])
        self.assertEqual(code, local_ci.EXIT_FAIL)
        self.assertIn("REPORT_WRITE_FAILED", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
