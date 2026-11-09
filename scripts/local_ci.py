#!/usr/bin/env python3
"""统一本地 CI 入口 —— macOS 本机的 authoritative merge gate。

用法：
    python3 scripts/local_ci.py
    python3 scripts/local_ci.py --focus tests.test_migration_structural_syntax
    python3 scripts/local_ci.py --json

只用 stdlib、现有检查脚本与 git。进程成功不等于证据有效；所有必检门 fail closed。
检查产物与报告只写仓库外的临时目录，不读取真实小说。支持策略见 docs/LOCAL-CI.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_POLICY = "macos-local-authoritative"
AUTHORITATIVE_SYSTEM = "Darwin"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNVERIFIED = 2
TAIL_LINES = 40
GATE_KEYS = ("fullTests", "taskSpecificTests", "wrapperSync", "checkerBaseline",
             "whitespace", "workingTreeIntegrity", "gitIdentityIntegrity")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tail(text: str, n: int = TAIL_LINES) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"  # status 不刷新 index 的 stat cache。
    # Product logging is on by default; regression calls are not personal use
    # evidence. Logging tests explicitly enable their own temporary stores.
    env["GOVEL_LOG_MODE"] = "off"
    return env


def run_process_gate(name: str, argv: list[str], *, cwd: Path | None = None,
                     env: dict[str, str] | None = None, timeout: float | None = None) -> dict:
    """返回进程事实；有结构化输出的门必须另行验证证据。"""
    start = time.monotonic()
    timed_out, start_failed = False, False
    try:
        proc = subprocess.run(argv, cwd=str(cwd or ROOT), env=env if env is not None else _subprocess_env(),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, exit_code = True, None
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
    except OSError as exc:
        start_failed, exit_code = True, None
        stdout, stderr = "", f"{type(exc).__name__}: {exc}"
    success = exit_code == 0 and not timed_out and not start_failed
    reason = ("PROCESS_TIMEOUT" if timed_out else "PROCESS_START_FAILED" if start_failed
              else "PASS" if success else "PROCESS_FAILED")
    return {"name": name, "command": argv, "exitCode": exit_code, "timedOut": timed_out,
            "processSuccess": success, "evidenceValid": not timed_out and not start_failed,
            "verificationReason": reason, "durationSeconds": round(time.monotonic() - start, 3),
            "pass": success, "stdout": stdout or "", "stderr": stderr or ""}


def _public(gate: dict) -> dict:
    trimmed = dict(gate)
    trimmed["stdoutTail"] = _tail(trimmed.pop("stdout", ""))
    trimmed["stderrTail"] = _tail(trimmed.pop("stderr", ""))
    return trimmed


def _verified_process(gate: dict, evidence_valid: bool, evidence_pass: bool, reason: str) -> dict:
    """证据优先于进程：证据无效/不达标时报证据原因，证据合格才报进程原因。"""
    success = gate["processSuccess"] is True
    passed = success and evidence_valid and evidence_pass
    verdict = "PASS" if passed else reason if not (evidence_valid and evidence_pass) else gate["verificationReason"]
    return {"name": gate["name"], "exitCode": gate["exitCode"], "timedOut": gate["timedOut"],
            "processSuccess": success, "evidenceValid": evidence_valid, "pass": passed,
            "verificationReason": verdict, "process": _public(gate)}


def parse_unittest_summary(text: str) -> dict:
    """只接受「Ran N tests」摘要 + 紧随其后的 OK/FAILED 收尾，忽略日志里的计数字样。

    取最后一条候选：子进程（例如某个用例内部又跑了一次 unittest）可能把自己的摘要
    混进 stderr，而父套件的摘要在全部用例跑完后才打印，永远是最后一条。
    """
    summary = {"testsRun": None, "durationSeconds": None, "failures": 0, "errors": 0,
               "skipped": 0, "expectedFailures": 0, "unexpectedSuccesses": 0, "parsed": False,
               "result": None}
    lines = (text or "").splitlines()
    candidates = [(index, match) for index, line in enumerate(lines)
                  if (match := re.fullmatch(r"Ran (\d+) tests?(?: in (\d+(?:\.\d+)?)s)?", line.strip()))]
    if not candidates:
        return summary
    index, ran = candidates[-1]
    trailing = [line.strip() for line in lines[index + 1:] if line.strip()]
    if len(trailing) != 1:
        return summary
    result = re.fullmatch(r"(OK|FAILED)(?: \(([^()]+)\))?", trailing[0])
    if result is None:
        return summary
    keys = {"failures": "failures", "errors": "errors", "skipped": "skipped",
            "expected failures": "expectedFailures", "unexpected successes": "unexpectedSuccesses"}
    counts = {}
    if result.group(2):
        for item in result.group(2).split(", "):
            count = re.fullmatch(r"([a-z ]+)=(\d+)", item)
            if count is None or count[1] not in keys or keys[count[1]] in counts:
                return summary
            counts[keys[count[1]]] = int(count[2])
    summary.update(counts)
    summary.update(testsRun=int(ran[1]), durationSeconds=float(ran[2]) if ran[2] else None,
                   parsed=True, result=result[1])
    return summary


def _unittest_gate(name: str, argv: list[str], timeout: float | None) -> dict:
    process = run_process_gate(name, argv, timeout=timeout)
    summary = parse_unittest_summary(process["stderr"])  # unittest 摘要在 stderr
    if not summary["parsed"]:
        reason = "TEST_SUMMARY_UNPARSEABLE"
    elif summary["testsRun"] == 0:
        reason = "ZERO_TESTS_EXECUTED"
    elif summary["result"] != "OK" or any(summary[k] for k in ("failures", "errors", "unexpectedSuccesses")):
        reason = "TEST_FAILURES_PRESENT"
    else:
        reason = "PASS"
    return {**_verified_process(process, summary["parsed"], reason == "PASS", reason), "summary": summary}


def _runner():
    if __package__:
        from .core import local_test_runner
    else:
        from core import local_test_runner
    return local_test_runner


def discover_test_modules(timeout=None):
    process = _runner().run_owned_process('testDiscovery', [sys.executable, str(ROOT / 'scripts/local_ci.py'),
        '--_discover'], cwd=ROOT, env=_subprocess_env(), timeout=timeout)
    try:
        data = json.loads(process['stdout'])
        modules = data['modules']
        valid = (data.get('schemaVersion') == 1 and isinstance(modules, list) and bool(modules)
                 and type(data['testsDiscovered']) is int and data['testsDiscovered'] > 0
                 and all(isinstance(row, dict) and isinstance(row.get('module'), str)
                         and re.fullmatch(r'(?:[a-zA-Z_]\w*\.)*test_\w+', row['module'])
                         and type(row.get('count')) is int and row['count'] >= 0
                         and _test_identity_valid(row) for row in modules)
                 and len({row['module'] for row in modules}) == len(modules)
                 and sum(row['count'] for row in modules) == data['testsDiscovered'])
    except (ValueError, KeyError, TypeError):
        valid, data = False, {}
    return {**data, 'pass': process['processSuccess'] and valid, 'evidenceValid': valid,
            'process': _public(process), 'verificationReason': 'PASS' if process['processSuccess'] and valid else 'TEST_DISCOVERY_INVALID'}


def _test_identity_valid(row):
    ids = row.get('testIds')
    return (isinstance(ids, list) and all(isinstance(value, str) and bool(value) for value in ids)
            and ids == sorted(set(ids)) and len(ids) == row.get('count')
            and hashlib.sha256(json.dumps(ids, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
            == row.get('testIdsSha256'))


def _skipped_tests_valid(evidence, expected):
    items = evidence.get('skippedTests')
    return (isinstance(items, list) and len(items) == evidence.get('skipped')
            and all(isinstance(row, dict) and isinstance(row.get('reason'), str)
                    and isinstance(row.get('testId'), str) and row['testId'] in expected.get('testIds', [])
                    for row in items)
            and len({row['testId'] for row in items}) == len(items))


def _run_test_module(expected, directory, timeout):
    output = Path(directory) / (expected['module'] + '.json')
    process = _runner().run_owned_process('module:' + expected['module'],
        [sys.executable, str(ROOT / 'scripts/local_ci.py'), '--_module', expected['module'], '--_module-report', str(output)],
        cwd=ROOT, env=_subprocess_env(), timeout=timeout)
    summary = parse_unittest_summary(process['stderr'])
    evidence = None
    try:
        if output.is_file() and not output.is_symlink():
            evidence = json.loads(output.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        pass
    shape = (isinstance(evidence, dict) and evidence.get('schemaVersion') == 1
             and evidence.get('module') == expected['module'] and evidence.get('loaderErrors') == []
             and evidence.get('testIdsSha256') == expected['testIdsSha256']
             and evidence.get('testIds') == expected.get('testIds') and _test_identity_valid(evidence)
             and type(evidence.get('count')) is int and evidence['count'] == expected['count']
             and all(type(evidence.get(k)) is int and evidence[k] >= 0 for k in
                     ('testsRun', 'failures', 'errors', 'skipped', 'expectedFailures', 'unexpectedSuccesses'))
             and _skipped_tests_valid(evidence, expected))
    agreement = shape and summary['parsed'] and all(evidence[k] == summary[k] for k in
        ('testsRun', 'failures', 'errors', 'skipped', 'expectedFailures', 'unexpectedSuccesses'))
    count_matches = agreement and summary['testsRun'] == expected['count']
    reason = ('PROCESS_TIMEOUT' if process['timedOut'] else
              'TEST_SUMMARY_UNPARSEABLE' if not summary['parsed'] else
              'MODULE_EVIDENCE_INVALID' if not agreement else
              'TEST_COUNT_MISMATCH' if not count_matches else
              'TEST_FAILURES_PRESENT' if summary['result'] != 'OK' or any(summary[k] for k in ('failures', 'errors', 'unexpectedSuccesses')) else
              process['verificationReason'])
    passed = process['processSuccess'] and reason == 'PASS'
    return {'module': expected['module'], 'expectedCount': expected['count'], 'summary': summary,
            'pass': passed, 'processSuccess': process['processSuccess'], 'evidenceValid': bool(agreement),
            'verificationReason': reason, 'durationSeconds': process['durationSeconds'], 'process': _public(process),
            'skippedTests': evidence.get('skippedTests', []) if shape else [],
            'entireModuleSkipped': bool(shape and expected['count'] and evidence['skipped'] == expected['count']),
            'serialReason': _runner().serial_reason(expected['module'])}


def _module_queue(modules, jobs, directory, timeout):
    """Executor slots form a dynamic queue; a slot always starts a fresh child."""
    runs = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        tasks = {pool.submit(_run_test_module, row, directory, timeout): row for row in modules}
        for future in as_completed(tasks):
            row = tasks[future]
            try:
                run = future.result()
            except Exception as exc:
                run = {**_exception_gate('module:' + row['module'], exc), 'module': row['module'],
                       'processSuccess': False, 'summary': {}, 'durationSeconds': 0}
            runs.append(run)
            print(f"[{len(runs)}/{len(modules)}] {row['module']}: {'PASS' if run['pass'] else 'FAIL'} ({run['durationSeconds']}s)", flush=True)
    return runs


def aggregate_test_modules(discovery, runs, *, jobs, elapsed):
    expected = {row['module']: row['count'] for row in discovery['modules']}
    actual = [row.get('module') for row in runs]
    modules_match = len(actual) == len(expected) and len(set(actual)) == len(actual) and set(actual) == set(expected)
    tests = sum(row.get('summary', {}).get('testsRun') or 0 for row in runs)
    count_matches = modules_match and tests == discovery['testsDiscovered'] and all(
        type(row.get('summary', {}).get('testsRun')) is int and row['summary']['testsRun'] == expected[row['module']] for row in runs)
    evidence_valid = modules_match and count_matches and all(row.get('evidenceValid') is True for row in runs)
    success = bool(runs) and all(row.get('processSuccess') is True for row in runs)
    passed = evidence_valid and success and all(row.get('pass') is True for row in runs)
    reason = ('TEST_MODULE_SET_MISMATCH' if not modules_match else 'TEST_COUNT_MISMATCH' if not count_matches else
              'TEST_WORKER_FAILED' if not passed else 'PASS')
    summary = {'testsRun': tests, 'durationSeconds': elapsed, 'parsed': evidence_valid, 'result': 'OK' if passed else 'FAILED',
               **{key: sum(row.get('summary', {}).get(key, 0) for row in runs)
                  for key in ('failures', 'errors', 'skipped', 'expectedFailures', 'unexpectedSuccesses')}}
    return {'name': 'fullTests', 'pass': passed, 'processSuccess': success, 'evidenceValid': evidence_valid,
            'verificationReason': reason, 'exitCode': 0 if passed else 1, 'workerCount': jobs,
            'durationSeconds': elapsed, 'summary': summary, 'discovery': discovery,
            'moduleSetMatches': modules_match, 'testCountMatches': count_matches,
            'skippedTests': [{'module': row['module'], **item} for row in runs for item in row.get('skippedTests', [])],
            'moduleTimings': sorted(runs, key=lambda row: (-row['durationSeconds'], row['module'])),
            'serialModules': [{'module': row['module'], 'reason': _runner().serial_reason(row['module'])}
                              for row in discovery['modules'] if _runner().serial_reason(row['module'])]}


def test_worker_count(jobs: int | None = None) -> int:
    if jobs is None:
        if __package__:
            from .core.project import local_ci_policy
        else:
            from core.project import local_ci_policy
        jobs = min(os.cpu_count() or 1, local_ci_policy()['defaultMaxWorkers'])
    if type(jobs) is not int or jobs < 1:
        raise ValueError('jobs must be a positive integer')
    return jobs


def full_test_gate(timeout: float | None = None, jobs: int | None = None) -> dict:
    start = time.monotonic()
    jobs = test_worker_count(jobs)
    discovery = discover_test_modules(timeout)
    if discovery['pass'] is not True:
        return {'name': 'fullTests', 'pass': False, 'processSuccess': False, 'evidenceValid': False,
                'verificationReason': 'TEST_DISCOVERY_INVALID', 'discovery': discovery, 'workerCount': jobs}
    return discovered_test_gate(discovery, timeout=timeout, jobs=jobs, started=start)


def discovered_test_gate(discovery: dict, *, timeout=None, jobs=None, started=None) -> dict:
    """Both full and quick checks use the same isolated workers and evidence checks."""
    start = time.monotonic() if started is None else started
    jobs = test_worker_count(jobs)
    serial = [r for r in discovery['modules'] if _runner().serial_reason(r['module'])]
    parallel = [r for r in discovery['modules'] if not _runner().serial_reason(r['module'])]
    with tempfile.TemporaryDirectory(prefix='govel-tests-', dir=_temp_root()) as directory:
        # No other lane is running while serial tests touch shared inputs/GPU.
        runs = _module_queue(serial, 1, directory, timeout)
        runs += _module_queue(parallel, jobs, directory, timeout)
    return aggregate_test_modules(discovery, runs, jobs=jobs, elapsed=round(time.monotonic() - start, 3))


def task_specific_gate(modules: list[str], timeout: float | None = None) -> dict:
    if not modules:
        return {"name": "taskSpecificTests", "pass": True, "status": "skipped",
                "evidenceValid": True, "verificationReason": "NO_FOCUS_REQUESTED",
                "modules": [], "testsRun": 0, "runs": []}
    runs = [{**_unittest_gate(f"task:{module}", [sys.executable, "-m", "unittest", module], timeout),
             "module": module} for module in modules]
    passed = all(run["pass"] is True for run in runs)
    return {"name": "taskSpecificTests", "pass": passed, "status": "run", "modules": modules,
            "processSuccess": all(run["processSuccess"] for run in runs),
            "evidenceValid": all(run["evidenceValid"] for run in runs),
            "verificationReason": "PASS" if passed else "TASK_TESTS_FAILED",
            "testsRun": sum(run["summary"]["testsRun"] or 0 for run in runs), "runs": runs}


def wrapper_sync_gate(timeout: float | None = None) -> dict:
    process = run_process_gate("wrapperSync",
                               [sys.executable, "scripts/generate_wrappers.py", "--check", "--json"],
                               timeout=timeout)
    status, json_valid = None, False
    try:
        status = json.loads(process["stdout"])
        json_valid = True
    except (ValueError, TypeError):
        pass
    valid = (isinstance(status, dict) and type(status.get("ok")) is bool
             and isinstance(status.get("drift"), list) and all(isinstance(p, str) for p in status["drift"]))
    passed = valid and status["ok"] is True and status["drift"] == []
    reason = "WRAPPER_EVIDENCE_INVALID" if not valid else "WRAPPER_DRIFT_PRESENT"
    return {**_verified_process(process, valid, passed, reason), "jsonValid": json_valid, "status": status}


def checker_baseline_gate(temp_dir: str, timeout: float | None = None) -> dict:
    output = Path(temp_dir) / "checker-evaluation.json"
    if output.exists() or output.is_symlink():
        raise ValueError("Checker output must be fresh, not evidence from a previous run")
    process = run_process_gate("checkerBaseline", [sys.executable, "scripts/evaluate_checkers.py",
                               "--baseline", "evals/checker-eval-baseline.json", "--json", "--output", str(output)],
                               timeout=timeout)
    report, json_valid, read_error = None, False, None
    exists = output.is_file() and not output.is_symlink()
    if exists:
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
            json_valid = True
        except (ValueError, OSError) as exc:
            read_error = str(exc)
    comparison = report.get("baselineComparison") if isinstance(report, dict) else None
    valid = (isinstance(report, dict) and type(report.get("passed")) is bool
             and isinstance(comparison, dict) and type(comparison.get("regressed")) is bool
             and type(comparison.get("comparable")) is bool)
    passed = valid and report["passed"] is True and comparison["regressed"] is False and comparison["comparable"] is True
    reason = ("CHECKER_OUTPUT_MISSING" if not exists else "CHECKER_EVIDENCE_INVALID" if not valid
              else "CHECKER_BASELINE_FAILED")
    result = ({"passed": report["passed"], "regressed": comparison["regressed"],
               "comparable": comparison["comparable"], "summary": report.get("summary")} if valid else None)
    return {**_verified_process(process, valid, passed, reason), "outputPath": str(output),
            "outputExists": exists, "jsonValid": json_valid, "readError": read_error, "baselineResult": result}


def whitespace_gate(timeout: float | None = None) -> dict:
    diff = run_process_gate("git-diff-check", ["git", "diff", "--check"], timeout=timeout)
    cached = run_process_gate("git-diff-cached-check", ["git", "diff", "--cached", "--check"], timeout=timeout)
    show = run_process_gate("git-show-check", ["git", "show", "--format=", "--check", "HEAD"], timeout=timeout)
    passed = all(gate["pass"] is True for gate in (diff, cached, show))
    return {"name": "whitespace", "pass": passed, "processSuccess": passed,
            "evidenceValid": all(gate["evidenceValid"] is True for gate in (diff, cached, show)),
            "verificationReason": "PASS" if passed else "WHITESPACE_CHECK_FAILED",
            "diffCheck": _public(diff), "cachedDiffCheck": _public(cached), "showCheckHead": _public(show)}


# --- read-only repository snapshots -----------------------------------------

class SnapshotError(Exception):
    def __init__(self, reason: str, details: str):
        super().__init__(details)
        self.reason = reason


def _git_bytes(root: Path, args: list[str], timeout: float | None, *, allow_detached: bool = False) -> bytes | None:
    try:
        process = subprocess.run(["git", *args], cwd=str(root), env=_subprocess_env(),
                                 capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError("PROCESS_TIMEOUT", f"git {args}: {exc}") from exc
    except OSError as exc:
        raise SnapshotError("PROCESS_START_FAILED", f"git {args}: {exc}") from exc
    if allow_detached and process.returncode == 1:
        return None
    if process.returncode != 0:
        raise SnapshotError("PROCESS_FAILED", f"git {args}: {process.stderr.decode('utf-8', 'replace')}")
    return process.stdout


def _nul_records(data: bytes) -> list[bytes]:
    if data and not data.endswith(b"\0"):
        raise SnapshotError("SNAPSHOT_INVALID", "Git path enumeration is not NUL-terminated")
    return data[:-1].split(b"\0") if data else []


def file_fingerprint(root: Path, relative: str) -> dict:
    """逐级 O_NOFOLLOW；symlink 只记链接文本，不触碰其目标。"""
    parts = relative.split("/")
    if any(part in ("", ".", "..", ".git") for part in parts):
        raise SnapshotError("SNAPSHOT_INVALID", f"Unsafe Git path: {relative!r}")
    entry = {"path": relative, "exists": False, "fileType": "missing", "sha256": None}
    with ExitStack() as stack:
        def directory(name, parent=None):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, fd)
            return fd
        try:
            parent = directory(root)
            for part in parts[:-1]:
                parent = directory(part, parent)
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return entry
        entry.update(exists=True, mode=stat.S_IMODE(info.st_mode))
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(parts[-1], dir_fd=parent)
            entry.update(fileType="symlink", symlinkTarget=target,
                         sha256=hashlib.sha256(os.fsencode(target)).hexdigest())
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                initial = os.fstat(stream.fileno())
                if not stat.S_ISREG(initial.st_mode) or (initial.st_dev, initial.st_ino) != (info.st_dev, info.st_ino):
                    raise SnapshotError("SNAPSHOT_UNSTABLE", f"File replaced during snapshot: {relative}")
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                final = os.fstat(stream.fileno())
                if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
                    raise SnapshotError("SNAPSHOT_UNSTABLE", f"File changed during snapshot: {relative}")
            entry.update(fileType="file", sha256=digest.hexdigest())
        else:
            raise SnapshotError("SNAPSHOT_UNSUPPORTED_FILE_TYPE", f"Not a regular file or symlink: {relative}")
    return entry


def repository_snapshot(root: Path | None = None, timeout: float | None = None) -> dict:
    root = root or ROOT
    snapshot = {"valid": False, "tracked": {}, "untracked": {}, "head": None, "branch": None,
                "indexSha256": None, "status": [], "exclusions": []}
    try:
        snapshot["head"] = _git_bytes(root, ["rev-parse", "HEAD"], timeout).decode("ascii").strip()
        branch = _git_bytes(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], timeout, allow_detached=True)
        snapshot["branch"] = os.fsdecode(branch).strip() if branch is not None else None
        stage = _nul_records(_git_bytes(root, ["ls-files", "--stage", "-z"], timeout))
        snapshot["indexSha256"] = hashlib.sha256(b"\0".join(sorted(stage))).hexdigest()
        tracked = set()
        for record in stage:
            metadata, path = record.split(b"\t", 1)
            if not re.fullmatch(rb"[0-7]{6} [0-9a-f]+ [0-3]", metadata):
                raise SnapshotError("SNAPSHOT_INVALID", "Malformed git ls-files --stage record")
            tracked.add(os.fsdecode(path))
        untracked = {os.fsdecode(path) for path in _nul_records(
            _git_bytes(root, ["ls-files", "--others", "--exclude-standard", "-z"], timeout))}
        snapshot["status"] = [os.fsdecode(line) for line in _nul_records(
            _git_bytes(root, ["status", "--porcelain=v1", "--untracked-files=all", "-z"], timeout))]
        for kind, paths in (("tracked", tracked), ("untracked", untracked)):
            snapshot[kind] = {path: file_fingerprint(root, path) for path in sorted(paths)}
        if not re.fullmatch(r"[0-9a-f]{40,64}", snapshot["head"]):
            raise SnapshotError("SNAPSHOT_INVALID", "Invalid HEAD evidence")
        snapshot.update(valid=True, verificationReason="PASS")
    except Exception as exc:
        snapshot.update(verificationReason=getattr(exc, "reason", "SNAPSHOT_FAILED"), error=f"{type(exc).__name__}: {exc}")
    return snapshot


def _map_digest(entries: dict) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot_evidence(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key not in ("tracked", "untracked")} | {
        f"{kind}Fingerprint": _map_digest(snapshot[kind]) for kind in ("tracked", "untracked")
    } | {f"{kind}Paths": len(snapshot[kind]) for kind in ("tracked", "untracked")}


def git_identity_gate(before: dict, after: dict) -> dict:
    valid = before["valid"] is True and after["valid"] is True
    head = before["head"] == after["head"] if valid else None
    branch = before["branch"] == after["branch"] if valid else None
    index = before["indexSha256"] == after["indexSha256"] if valid else None
    reasons = (["SNAPSHOT_UNAVAILABLE"] if not valid else
               (["GIT_IDENTITY_CHANGED"] if not head or not branch else []) + (["GIT_INDEX_MUTATED"] if not index else []))
    return {"name": "gitIdentityIntegrity", "pass": valid and not reasons, "evidenceValid": valid,
            "headUnchanged": head, "branchUnchanged": branch, "indexUnchanged": index,
            "verificationReason": reasons[0] if reasons else "PASS", "reasons": reasons,
            "before": {k: before[k] for k in ("head", "branch", "indexSha256")},
            "after": {k: after[k] for k in ("head", "branch", "indexSha256")}}


def working_tree_gate(before: dict, after: dict) -> dict:
    valid = before["valid"] is True and after["valid"] is True
    changes, tracked_changed, untracked_changed = [], [], []
    added, removed, content_changed = [], [], []
    if valid:
        old = before["tracked"] | before["untracked"]
        new = after["tracked"] | after["untracked"]
        for path in sorted(old.keys() | new.keys()):
            previous, current = old.get(path), new.get(path)
            types = []
            old_exists, new_exists = bool(previous and previous["exists"]), bool(current and current["exists"])
            if new_exists and not old_exists:
                added.append(path)
                types.append("added")
            elif old_exists and not new_exists:
                removed.append(path)
                types.append("removed")
            if old_exists and new_exists:
                if previous["sha256"] != current["sha256"]:
                    content_changed.append(path)
                    types.append("content_changed")
                if previous["fileType"] != current["fileType"]:
                    types.append("type_changed")
                if previous.get("mode") != current.get("mode"):
                    types.append("mode_changed")
            if (path in before["tracked"]) != (path in after["tracked"]):
                types.append("tracked_path_set_changed")
            if not types:
                continue
            if path in before["tracked"] or path in after["tracked"]:
                tracked_changed.append(path)
            if path in before["untracked"] or path in after["untracked"]:
                untracked_changed.append(path)
            changes.append({"path": path, "changeTypes": types, "before": previous, "after": current,
                            "beforeHash": previous["sha256"] if previous else None,
                            "afterHash": current["sha256"] if current else None})
    identity = git_identity_gate(before, after)
    mutated = bool(changes or not identity["pass"]) if valid else None
    reason = ("SNAPSHOT_UNAVAILABLE" if not valid else "WORKTREE_MUTATED" if changes
              else identity["verificationReason"])
    return {"name": "workingTreeIntegrity", "pass": valid and mutated is False, "evidenceValid": valid,
            "verificationReason": reason, "repositoryMutatedByCI": mutated,
            "trackedChanged": tracked_changed, "untrackedChanged": untracked_changed,
            "pathsAdded": added, "pathsRemoved": removed, "pathsContentChanged": content_changed, "changes": changes,
            "trackedContentUnchanged": not tracked_changed if valid else None,
            "untrackedContentUnchanged": not untracked_changed if valid else None,
            "before": _snapshot_evidence(before), "after": _snapshot_evidence(after), "exclusions": []}


# --- environment, orchestration & decision ----------------------------------

def environment_info() -> dict:
    supported = platform.system() == AUTHORITATIVE_SYSTEM
    return {"platform": platform.platform(), "system": platform.system(),
            "architecture": platform.machine(), "pythonExecutable": sys.executable,
            "pythonVersion": platform.python_version(), "pythonVersionFull": sys.version.replace("\n", " "),
            "macOSVersion": platform.mac_ver()[0] or None, "repoRoot": str(ROOT), "supportedPlatform": supported,
            "supportedPlatformCheck": "SUPPORTED" if supported else "UNSUPPORTED / UNVERIFIED"}


def decide(gates: dict, platform_supported: bool) -> tuple[str, int]:
    if any(not isinstance(gates.get(key), dict) or gates[key].get("pass") is not True for key in GATE_KEYS):
        return "FAIL", EXIT_FAIL
    if platform_supported is not True:
        return "UNVERIFIED", EXIT_UNVERIFIED
    return "PASS", EXIT_PASS


def build_report(environment: dict, git: dict, gates: dict, overall: str) -> dict:
    return {"kind": "local_ci_report", "platformPolicy": PLATFORM_POLICY,
            "generatedAt": _now_iso(), "environment": environment, "git": git,
            **{key: gates.get(key, {"pass": False, "evidenceValid": False, "verificationReason": "GATE_MISSING"})
               for key in GATE_KEYS},
            "mergeGate": {"authoritative": environment["supportedPlatform"], "result": overall}, "overall": overall}


def _outside_repository(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == ROOT.resolve() or ROOT.resolve() in path.parents:
        raise ValueError("Local CI output must be outside the repository (including ignored paths)")
    return path


def _temp_root() -> Path:
    return _outside_repository(Path(tempfile.gettempdir()))


def resolve_report_path(report_out: str | None) -> Path:
    if report_out:
        path = _outside_repository(Path(report_out))
        if path.exists():
            raise FileExistsError("Report output must be a new file; existing files are never overwritten")
        return path
    return Path(tempfile.mkdtemp(prefix="govel-local-ci-", dir=_temp_root())) / "local-ci-report.json"


def _exception_gate(name: str, exc: Exception) -> dict:
    return {"name": name, "pass": False, "evidenceValid": False, "verificationReason": "GATE_EXCEPTION",
            "error": f"{type(exc).__name__}: {exc}"}


def run_gates(focus: list[str], timeout: float | None = None, before: dict | None = None, jobs: int | None = None) -> dict:
    gates: dict = {}
    before = before if before is not None else repository_snapshot(timeout=timeout)
    if before["valid"]:
        for name, run in (("fullTests", lambda: full_test_gate(timeout, jobs=jobs)),
                          ("taskSpecificTests", lambda: task_specific_gate(focus, timeout)),
                          ("wrapperSync", lambda: wrapper_sync_gate(timeout)),
                          ("checkerBaseline", lambda: _temporary_checker_gate(timeout)),
                          ("whitespace", lambda: whitespace_gate(timeout))):
            try:
                gates[name] = run()
            except Exception as exc:
                gates[name] = _exception_gate(name, exc)
    else:
        gates = {key: {"name": key, "pass": False, "evidenceValid": False,
                       "verificationReason": "INITIAL_SNAPSHOT_FAILED"} for key in GATE_KEYS}
    after = repository_snapshot(timeout=timeout)
    gates["workingTreeIntegrity"] = working_tree_gate(before, after)
    gates["gitIdentityIntegrity"] = git_identity_gate(before, after)
    return gates


def _temporary_checker_gate(timeout: float | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="govel-checker-", dir=_temp_root()) as temp_dir:
        return checker_baseline_gate(temp_dir, timeout)


# --- rendering & entrypoint -------------------------------------------------

def render_banner(environment: dict, before: dict | None) -> str:
    """运行开始打印的环境与 Git 身份；快照不可用时明确报 UNKNOWN，不猜。"""
    before = before or {"valid": False, "head": None, "branch": None, "status": []}
    entries = len(before["status"])
    state = (("UNKNOWN" if not before["valid"] else "clean" if not entries
              else f"dirty ({entries} pre-existing entries)"))
    branch = before["branch"] if before["branch"] is not None else "DETACHED / UNKNOWN"
    return "\n".join([
        "LOCAL CI ENVIRONMENT", "",
        f"platform:            {environment['platform']}",
        f"architecture:        {environment['architecture']}",
        f"python executable:   {environment['pythonExecutable']}",
        f"python version:      {environment['pythonVersion']}",
        f"repo root:           {environment['repoRoot']}",
        f"HEAD:                {before['head'] or 'UNKNOWN'}",
        f"branch:              {branch}",
        f"working tree state:  {state}",
        f"tracked files:       {len(before.get('tracked', {}))}",
        f"SUPPORTED PLATFORM CHECK: {environment['supportedPlatformCheck']}",
    ])


def render_summary(report: dict) -> str:
    env, git = report["environment"], report["git"]
    full = report["fullTests"]
    summary = full.get("summary", {})
    lines = ["LOCAL CI REPORT", "", "Platform", f"macOS / {env['system']}", env["architecture"], "",
             "Python", env["pythonVersion"], env["pythonExecutable"], "", "HEAD", str(git.get("head")), "",
             "Full unittest suite", f"Ran {summary.get('testsRun')} tests",
             " ".join(f"{key}={summary.get(key)}" for key in
                      ("parsed", "failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")), ""]
    if 'workerCount' in full:
        lines += [f"workers: {full['workerCount']}; Gate 1: {full.get('durationSeconds')}s",
                  f"discovered modules: {len(full.get('discovery', {}).get('modules', []))}; serial modules: {len(full.get('serialModules', []))}", '']
        for row in full.get('moduleTimings', []):
            if row.get('pass') is not True:
                lines.append(f"FAILED MODULE {row['module']}: {row.get('verificationReason')}")
                lines.append(row.get('process', {}).get('stderrTail', row.get('error', '')))
            if row.get('skippedTests'):
                scope = 'entire module not executed' if row.get('entireModuleSkipped') else 'partially not executed'
                lines.append(f"SKIPPED {row['module']}: {scope}")
                for item in row['skippedTests']:
                    lines.append(f"  {item['testId']}: {item['reason']}")
    for key in GATE_KEYS:
        gate = report[key]
        lines.append(f"{key}: {'PASS' if gate.get('pass') is True else 'FAIL'} ({gate.get('verificationReason', 'UNKNOWN')})")
    integrity = report["workingTreeIntegrity"]
    identity = report["gitIdentityIntegrity"]
    lines += ["", "Repository integrity",
              *(f"{key}: {identity.get(key)}" for key in ("headUnchanged", "branchUnchanged", "indexUnchanged")),
              *(f"{key}: {integrity.get(key)}" for key in ("trackedContentUnchanged", "untrackedContentUnchanged")),
              f"new paths: {len(integrity.get('pathsAdded', []))}; removed paths: {len(integrity.get('pathsRemoved', []))}",
              f"REPOSITORY MUTATED BY LOCAL CI: {integrity.get('repositoryMutatedByCI')}"]
    for change in integrity.get("changes", []):
        lines.append(f"{change['path']}: {', '.join(change['changeTypes'])}; {change['beforeHash']} -> {change['afterHash']}")
    lines += ["", "LOCAL MERGE GATE", report["overall"]]
    if env["supportedPlatform"] is not True:
        lines.append("note: authoritative PASS requires platform == Darwin (macOS)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description="统一本地 CI 入口（macOS 权威 merge gate）。")
    parser.add_argument("--focus", action="append", default=[], metavar="MODULE",
                        help="专项模块（可重复）；全量模式追加运行，--quick 模式只运行这些模块")
    parser.add_argument('--quick', action='store_true', help='开发快速检查；未指定 --focus 时按 Git 改动选择，不作为完整 merge gate')
    parser.add_argument('--plan', action='store_true', help='配合 --quick 只查看测试选择，不执行测试')
    parser.add_argument("--report-out", default=None, metavar="PATH",
                        help="仓库外的新报告路径；默认写临时目录，不覆盖已有文件")
    parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                        help="每个子进程门及 Git 快照命令的超时（秒）；默认不限时")
    parser.add_argument("--json", action="store_true", help="把机器可读报告也打印到 stdout")
    parser.add_argument('--jobs', type=int, default=None, help='测试并发进程数；默认取 CPU 核数与 local-ci 注册表上限的较小值，1 为串行')
    parser.add_argument('--_discover', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--_module', help=argparse.SUPPRESS)
    parser.add_argument('--_module-report', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._discover:
        print(json.dumps(_runner().discover(ROOT), ensure_ascii=True))
        return 0
    if args._module:
        if not args._module_report or not re.fullmatch(r'(?:[a-zA-Z_]\w*\.)*test_\w+', args._module):
            parser.error('internal module invocation needs a valid module and output path')
        return _runner().run_module(ROOT, args._module, _outside_repository(Path(args._module_report)))
    if args.jobs is not None and args.jobs < 1:
        parser.error('--jobs must be positive')
    if args.plan and not args.quick:
        parser.error('--plan requires --quick')
    if args.quick:
        if __package__:
            from .core.local_ci_quick import run_quick
        else:
            from core.local_ci_quick import run_quick
        return run_quick(args, ci=sys.modules[__name__])
    environment = environment_info()
    before = repository_snapshot(timeout=args.timeout)
    print(render_banner(environment, before))
    print()
    try:
        report_path = resolve_report_path(args.report_out)
    except (OSError, ValueError) as exc:
        gates = {key: _exception_gate(key, exc) for key in GATE_KEYS}
        report = build_report(environment, _snapshot_evidence(before), gates, "FAIL")
        report["reportError"] = {"verificationReason": "REPORT_DESTINATION_INVALID", "error": str(exc)}
        print(render_summary(report))
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return EXIT_FAIL
    gates = run_gates(args.focus, timeout=args.timeout, before=before, jobs=args.jobs)
    overall, code = decide(gates, environment["supportedPlatform"])
    report = build_report(environment, _snapshot_evidence(before), gates, overall)
    report['durationSeconds'] = round(time.monotonic() - started, 3)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    except OSError as exc:
        report["overall"], report["mergeGate"]["result"], code = "FAIL", "FAIL", EXIT_FAIL
        report["reportError"] = {"verificationReason": "REPORT_WRITE_FAILED", "error": str(exc)}
    print(render_summary(report))
    if "reportError" not in report:
        print(f"\nmachine-readable report: {report_path}")
    if args.json or "reportError" in report:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
