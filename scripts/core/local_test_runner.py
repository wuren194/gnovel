"""Discover and execute one unittest module per fresh subprocess.

The parent owns the queue; each subprocess owns exactly one module, so patched
globals, sys.path, environment and module caches cannot leak to the next module.
No test selection is inferred from timings. The serial lane runs with the pool
stopped: browser GPU work and the retained serial acceptance modules never overlap other tests.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest


# These are isolation decisions, not exclusions. Every discovered module runs.
SERIAL_MODULES = {
    'test_cache_dependencies': 'Toolbox mutation runs in an owned physical copy; retain the established serial scheduling policy.',
    'test_dashboard_server': 'HTTP server/session integration; keep socket and server cleanup off the loaded pool (currently ephemeral ports).',
    'test_entity_card_projection': 'Real Chromium DOM and HTTP card integration; use the same unloaded lane as browser tests.',
    'test_knowledge_router': 'Registry/asset mutations run in an owned physical copy; retain the established serial scheduling policy.',
    'test_workflow_policy_consumers': 'Real CLI policy mutations run in an owned physical copy; retain the established serial scheduling policy.',
    'test_knowledge_retrieval_p1': 'Rebuilds its isolated knowledge catalogue/index; retain the established serial scheduling policy.',
    'test_knowledge_real_goals': 'Real-goal evaluation rebuilds its isolated knowledge index; retain the established serial scheduling policy.',
    'test_knowledge_modes': 'Knowledge operations run in an owned toolbox copy; retain the established serial scheduling policy.',
    'test_end_to_end_delivery': 'Isolated toolbox/HOME and real doctor entrypoints; retain the established serial scheduling policy.',
    'test_p7_delivery_acceptance': 'Isolated toolbox/HOME and package/import acceptance; retain the established serial scheduling policy.',
}


def serial_reason(module):
    leaf = module.rsplit('.', 1)[-1]
    if leaf.startswith('test_browser_'):
        return 'Playwright/WebGL share GPU resources; hover/raycast tests require an unloaded serial lane.'
    return SERIAL_MODULES.get(leaf)


def _test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _test_ids(item)
        else:
            yield item.id()


def _identity(suite):
    ids = sorted(_test_ids(suite))
    return {'count': len(ids), 'testIds': ids, 'testIdsSha256': hashlib.sha256(
        json.dumps(ids, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()}


def _paths(root):
    # Match the historical `unittest discover -s tests` module names, while
    # preserving imports of fixtures through the repository's tests package.
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tests'))
    if (root / 'tests/__init__.py').is_file():
        __import__('tests')


def discover(root):
    _paths(root)

    class RecordingLoader(unittest.TestLoader):
        def __init__(self):
            super().__init__()
            self.modules = {}

        def loadTestsFromModule(self, module, *, pattern=None):
            suite = super().loadTestsFromModule(module, pattern=pattern)
            if module.__name__ in self.modules:
                raise ValueError('module discovered twice: ' + module.__name__)
            self.modules[module.__name__] = _identity(suite)
            return suite

    loader = RecordingLoader()
    suite = loader.discover(str(root / 'tests'), pattern='test_*.py')
    modules = [{'module': name, **identity} for name, identity in sorted(loader.modules.items())]
    total = suite.countTestCases()
    if loader.errors or sum(row['count'] for row in modules) != total:
        raise ValueError('test discovery failed or could not account for every test: ' + '\n'.join(loader.errors))
    return {'schemaVersion': 1, 'modules': modules, 'testsDiscovered': total}


def run_module(root, module, output):
    _paths(root)
    loader = unittest.TestLoader()
    # Historical discovery imports every test module before running any tests.
    # Reproduce that import environment (no setUp/test execution): performance
    # probes reset product caches but intentionally exclude Python import I/O.
    loader.discover(str(root / 'tests'), pattern='test_*.py')
    suite = loader.loadTestsFromName(module)
    identity = _identity(suite)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    payload = {'schemaVersion': 1, 'module': module, **identity, 'testsRun': result.testsRun,
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'expectedFailures': len(result.expectedFailures),
               'unexpectedSuccesses': len(result.unexpectedSuccesses), 'loaderErrors': loader.errors,
               'skippedTests': [{'testId': test.id(), 'reason': reason} for test, reason in result.skipped]}
    with Path(output).open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=True)
    return 0 if result.wasSuccessful() and not loader.errors else 1


def run_owned_process(name, argv, *, cwd, env, timeout):
    """Kill the owned process group on timeout, then reap before a lane completes."""
    start = time.monotonic()
    process = None
    timed_out, start_failed = False, False
    group_terminated = False
    try:
        process = subprocess.Popen(argv, cwd=str(cwd), env=env, start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='replace')
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            group_terminated = True
            stdout, stderr = process.communicate()
        exit_code = process.returncode
    except OSError as exc:
        start_failed, exit_code = True, None
        stdout, stderr = '', f'{type(exc).__name__}: {exc}'
    finally:
        # A crashed test may have left its own subprocess/server alive. They are
        # in this fresh session, never the caller's process group.
        if process is not None:
            if not group_terminated:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.wait()
    success = exit_code == 0 and not timed_out and not start_failed
    reason = 'PROCESS_TIMEOUT' if timed_out else 'PROCESS_START_FAILED' if start_failed else 'PASS' if success else 'PROCESS_FAILED'
    return {'name': name, 'command': argv, 'exitCode': exit_code, 'timedOut': timed_out,
            'processSuccess': success, 'evidenceValid': not timed_out and not start_failed,
            'verificationReason': reason, 'durationSeconds': round(time.monotonic() - start, 3),
            'pass': success, 'stdout': stdout, 'stderr': stderr}
