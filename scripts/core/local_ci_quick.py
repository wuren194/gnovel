"""Development checks selected from explicit modules or direct test imports.

Selection is a heuristic for feedback, never proof of complete impact coverage.
The full merge gate remains the owner of repository-wide acceptance.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import time


def _module_name(path):
    return path.removesuffix('.py').replace('/', '.').removesuffix('.__init__')


def _test_imports(path: Path, module: str, known: set[str]) -> set[str]:
    """Resolve both package-style and script-style imports without executing code."""
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ''
            if node.level:
                package = module if path.name == '__init__.py' else module.rpartition('.')[0]
                name = importlib.util.resolve_name('.' * node.level + name, package)
            imports.add(name)
            imports.update(name + '.' + alias.name for alias in node.names if alias.name != '*')
    return {candidate for name in imports for candidate in (name, 'scripts.' + name, 'tests.' + name)
            if candidate in known}


def select_tests(root: Path, discovery: dict, paths: list[str], *, focus=(), files=()) -> dict:
    available = {row['module']: row for row in discovery['modules']}
    selection = {'mode': 'explicit' if focus else 'changed', 'changedPaths': sorted(set(paths)),
                 'modules': [], 'reasons': {}, 'unmappedPaths': [], 'invalidModules': [],
                 'coverage': 'explicit modules' if focus else 'direct test imports, test helpers and matching filenames',
                 'fullSuiteRequired': True, 'transitiveProductCoverageProven': False}
    if focus:
        for supplied in focus:
            name = supplied.removeprefix('tests.')
            if name not in available or available[name]['count'] < 1:
                selection['invalidModules'].append(supplied)
            else:
                selection['reasons'].setdefault(name, []).append('explicit:' + supplied)
    else:
        module_paths = {_module_name(path): path for path in files if path.endswith('.py')}
        # Deleted modules remain resolvable as import targets in surviving test code.
        known = set(module_paths) | {_module_name(path) for path in paths if path.endswith('.py')}
        imports = {name: _test_imports(root / path, name, known)
                   for name, path in module_paths.items()
                   if path.startswith('tests/') and (root / path).is_file() and not (root / path).is_symlink()}
        test_modules = {'tests.' + name: name for name in available}
        for path in sorted(set(paths)):
            targets = set()
            if path.endswith('.py'):
                changed = _module_name(path)
                reached, pending = {changed}, [changed]
                while pending:
                    dependency = pending.pop()
                    for name, dependencies in imports.items():
                        if dependency in dependencies and name not in reached:
                            reached.add(name)
                            pending.append(name)
                targets.update(test_modules[name] for name in reached if name in test_modules)
                # A matching test can use subprocesses rather than a Python import.
                stem = Path(path).stem
                if 'test_' + stem in available:
                    targets.add('test_' + stem)
            elif path.endswith('.md') and (path.startswith(('docs/', 'shared/contracts/'))
                                           or path in {'AGENTS.md', 'README.md'}):
                targets.update(name for name in ('test_documented_cli_exists', 'test_docs_consistency') if name in available)
                if path.startswith('shared/contracts/') and 'test_contract_frontmatter' in available:
                    targets.add('test_contract_frontmatter')
            targets = {name for name in targets if available[name]['count'] > 0}
            if not targets:
                selection['unmappedPaths'].append(path)
            for name in sorted(targets):
                selection['reasons'].setdefault(name, []).append(path)
    selection['modules'] = sorted(selection['reasons'])
    selection['selectedTestCount'] = sum(available[name]['count'] for name in selection['modules'])
    selection['totalTestCount'] = discovery['testsDiscovered']
    selection['ready'] = bool(selection['modules']) and not selection['invalidModules'] and not selection['unmappedPaths']
    return selection


def _changed_paths(ci, before, timeout):
    changed = ci._git_bytes(ci.ROOT, ['diff', '--name-only', '--no-renames', '-z', 'HEAD', '--'], timeout)
    return sorted({os.fsdecode(value) for value in changed.split(b'\0') if value} | set(before['untracked']))


def render_quick(report):
    selection = report.get('selection', {})
    lines = ['LOCAL QUICK CHECK', '开发反馈；完整 merge gate 尚未运行。',
             f"selection: {selection.get('mode', 'unavailable')}; modules: {len(selection.get('modules', []))}; "
             f"tests: {selection.get('selectedTestCount', 0)} / {selection.get('totalTestCount', 'unknown')}"]
    for module in selection.get('modules', []):
        lines.append(f"  {module} <- {', '.join(selection['reasons'][module])}")
    for key in ('invalidModules', 'unmappedPaths'):
        if selection.get(key):
            lines.append(f"{key}: {', '.join(selection[key])}")
    tests = report.get('selectedTests', {})
    for row in tests.get('moduleTimings', []):
        if row.get('pass') is not True:
            lines.append(f"FAILED MODULE {row['module']}: {row.get('verificationReason')}")
            lines.append(row.get('process', {}).get('stderrTail', row.get('error', '')))
        if row.get('skippedTests'):
            scope = 'entire module not executed' if row.get('entireModuleSkipped') else 'partially not executed'
            lines.append(f"SKIPPED {row['module']}: {scope}")
            for item in row['skippedTests']:
                lines.append(f"  {item['testId']}: {item['reason']}")
    for name, gate in report.get('checks', {}).items():
        lines.append(f"{name}: {'PASS' if gate.get('pass') is True else 'FAIL'} ({gate.get('verificationReason')})")
    if 'error' in report:
        lines.append(report['error'])
    lines.append(report['overall'])
    if report['overall'] == 'NO_CHANGES':
        lines.append('没有相对 HEAD 的待测改动；未执行测试，也未核验历史门禁。')
    else:
        lines.append('收尾验证：python3 scripts/local_ci.py')
    if report['overall'] == 'NEEDS_FULL':
        lines.append('自动选择不足以覆盖这些改动；可用 --quick --focus 指定模块继续迭代，收尾仍跑全量。')
    return '\n'.join(lines)


def run_quick(args, *, ci):
    started = time.monotonic()
    before = ci.repository_snapshot(timeout=args.timeout)
    report = {'kind': 'local_ci_quick_report', 'generatedAt': ci._now_iso(), 'environment': ci.environment_info(),
              'git': ci._snapshot_evidence(before), 'checks': {}, 'overall': 'FAIL',
              'mergeGate': {'authoritative': False, 'result': 'NOT_RUN'}, 'fullSuiteRequired': True}
    code, output = ci.EXIT_FAIL, None
    try:
        output = ci.resolve_report_path(args.report_out)
        if before['valid'] is not True:
            raise ValueError('INITIAL_SNAPSHOT_FAILED')
        discovery = ci.discover_test_modules(args.timeout)
        if discovery['pass'] is not True:
            report['discovery'] = discovery
            raise ValueError('TEST_DISCOVERY_INVALID')
        paths = _changed_paths(ci, before, args.timeout)
        selection = select_tests(ci.ROOT, discovery, paths, focus=args.focus,
                                 files=before['tracked'].keys() | before['untracked'].keys())
        report['selection'] = selection
        if selection['invalidModules']:
            report['overall'] = 'FAIL'
        elif not args.focus and not paths:
            report['overall'], code = 'NO_CHANGES', ci.EXIT_PASS
        elif not selection['ready']:
            report['overall'], code = 'NEEDS_FULL', ci.EXIT_UNVERIFIED
        elif args.plan:
            report['overall'], code = 'QUICK_PLAN', ci.EXIT_PASS
        else:
            selected = [row for row in discovery['modules'] if row['module'] in selection['modules']]
            subset = {**discovery, 'modules': selected, 'testsDiscovered': selection['selectedTestCount']}
            print(f"LOCAL QUICK CHECK: {len(selected)} modules / {selection['selectedTestCount']} tests; merge gate NOT_RUN", flush=True)
            tests = ci.discovered_test_gate(subset, timeout=args.timeout, jobs=args.jobs)
            tests['name'] = 'selectedTests'
            report['selectedTests'] = tests
            report['checks']['selectedTests'] = {key: tests[key] for key in ('pass', 'evidenceValid', 'verificationReason')}
            report['checks']['whitespace'] = ci.whitespace_gate(args.timeout)
            if all(check['pass'] is True for check in report['checks'].values()):
                report['overall'], code = 'QUICK_PASS', ci.EXIT_PASS
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['overall'], code = 'FAIL', ci.EXIT_FAIL
    after = ci.repository_snapshot(timeout=args.timeout)
    report['checks']['workingTreeIntegrity'] = ci.working_tree_gate(before, after)
    report['checks']['gitIdentityIntegrity'] = ci.git_identity_gate(before, after)
    if any(check.get('pass') is not True for check in report['checks'].values()):
        report['overall'], code = 'FAIL', ci.EXIT_FAIL
    report['durationSeconds'] = round(time.monotonic() - started, 3)
    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(report, ensure_ascii=True, indent=2) + '\n')
        except OSError as exc:
            report['error'] = f'REPORT_WRITE_FAILED: {exc}'
            report['overall'], code = 'FAIL', ci.EXIT_FAIL
    print(render_quick(report))
    if output is not None and 'error' not in report:
        print(f'\nmachine-readable report: {output}')
    if args.json or 'error' in report:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return code
