"""Quick checks must select honestly, execute real tests and never certify a full gate."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import local_ci
from scripts.core.local_ci_quick import _changed_paths, select_tests
from tests.test_local_ci import ScratchRepo


class QuickSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.files = {
            'scripts/core/item.py': 'VALUE = 1\n',
            'tests/helper.py': 'from core.item import VALUE\n',
            'tests/test_item.py': 'from .helper import VALUE\n',
            'tests/test_alias.py': 'from scripts.core import item\n',
            'tests/test_unrelated.py': 'import unittest\n',
        }
        for name, body in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.discovery = {'testsDiscovered': 3, 'modules': [
            {'module': name, 'count': 1, 'testIdsSha256': 'a' * 64}
            for name in ('test_item', 'test_alias', 'test_unrelated')]}

    def select(self, paths, focus=()):
        return select_tests(self.root, self.discovery, paths, focus=focus, files=self.files)

    def test_direct_imports_helpers_deleted_source_and_test_edits_are_selected(self):
        for paths, expected in ((['scripts/core/item.py'], ['test_alias', 'test_item']),
                                (['tests/helper.py'], ['test_item']),
                                (['tests/test_item.py'], ['test_item'])):
            with self.subTest(paths=paths):
                result = self.select(paths)
                self.assertEqual(result['modules'], expected)
                self.assertTrue(result['ready'])
                self.assertFalse(result['transitiveProductCoverageProven'])
        (self.root / 'scripts/core/item.py').unlink()
        self.assertEqual(self.select(['scripts/core/item.py'])['modules'], ['test_alias', 'test_item'])

    def test_unknown_config_or_test_deletion_cannot_silently_pass(self):
        for unknown in ('shared/registries/workflow.json', 'knowledge/assets/card.md',
                        'scripts/core/unknown.py', 'tests/test_deleted.py'):
            result = self.select(['tests/test_item.py', unknown])
            self.assertFalse(result['ready'])
            self.assertEqual(result['unmappedPaths'], [unknown])
        explicit = self.select(['shared/registries/workflow.json'], ['tests.test_item', 'test_item'])
        self.assertTrue(explicit['ready'])
        self.assertEqual(explicit['modules'], ['test_item'])
        self.assertEqual(explicit['mode'], 'explicit')
        self.assertTrue(explicit['fullSuiteRequired'])
        invalid = self.select([], ['tests.test_missing'])
        self.assertFalse(invalid['ready'])
        self.assertEqual(invalid['invalidModules'], ['tests.test_missing'])

    def test_git_selection_includes_staged_unstaged_deleted_renamed_and_untracked(self):
        repo = ScratchRepo()
        self.addCleanup(repo.dispose)
        repo.git('init', '-q')
        for path in ('staged.py', 'unstaged.py', 'deleted.py', 'old.py'):
            repo.write(path, 'before\n')
        repo.git('add', '-A'); repo.git('commit', '-qm', 'baseline')
        repo.write('staged.py', 'after\n'); repo.git('add', 'staged.py')
        repo.write('unstaged.py', 'after\n')
        (repo.root / 'deleted.py').unlink()
        repo.git('mv', 'old.py', 'new.py')
        repo.write('new test.py', 'new\n')
        before = repo.snapshot()
        with patch.object(local_ci, 'ROOT', repo.root):
            self.assertEqual(_changed_paths(local_ci, before, None),
                             ['deleted.py', 'new test.py', 'new.py', 'old.py', 'staged.py', 'unstaged.py'])
        self.assertTrue(local_ci.working_tree_gate(before, repo.snapshot())['pass'])


class QuickEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.repo = ScratchRepo()
        self.addCleanup(self.repo.dispose)
        self.repo.git('init', '-q')
        for path in ('scripts/local_ci.py', 'scripts/runtime_requirements.py',
                     'scripts/core/local_ci_quick.py', 'scripts/core/local_test_runner.py'):
            destination = self.repo.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_ci.ROOT / path, destination)
        self.repo.write('tests/test_one.py', 'import unittest\nclass One(unittest.TestCase):\n def test_ok(self): self.assertEqual(1+1, 2)\n')
        self.repo.write('tests/test_browser_sentinel.py',
                        'import unittest\nclass Other(unittest.TestCase):\n def test_never_selected(self): self.fail("unrelated browser test ran")\n')
        self.repo.git('add', '-A'); self.repo.git('commit', '-qm', 'baseline')
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        self.output = Path(output.name)
        self.count = 0

    def quick(self, *args, expected):
        self.count += 1
        report_path = self.output / f'report-{self.count}.json'
        proc = subprocess.run([sys.executable, str(self.repo.root / 'scripts/local_ci.py'), '--quick',
                               '--jobs', '1', '--report-out', str(report_path), *args], cwd=self.repo.root,
                              env=local_ci._subprocess_env(), text=True, capture_output=True, timeout=30)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['overall'], expected, proc.stdout + proc.stderr)
        self.assertEqual(report['mergeGate'], {'authoritative': False, 'result': 'NOT_RUN'})
        self.assertNotIn('fullTests', report)
        self.assertNotIn('LOCAL MERGE GATE\nPASS', proc.stdout)
        return proc, report

    def test_real_quick_runs_only_selected_tests_and_plan_runs_none(self):
        self.repo.write('tests/test_one.py', (self.repo.root / 'tests/test_one.py').read_text() + '# edited\n')
        before = self.repo.snapshot()
        planned, report = self.quick('--plan', expected='QUICK_PLAN')
        self.assertEqual(planned.returncode, 0)
        self.assertNotIn('selectedTests', report)
        proc, report = self.quick(expected='QUICK_PASS')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report['selectedTests']['summary']['testsRun'], 1)
        self.assertEqual(report['selection']['modules'], ['test_one'])
        self.assertTrue(report['checks']['workingTreeIntegrity']['pass'])
        self.assertTrue(local_ci.working_tree_gate(before, self.repo.snapshot())['pass'])

    def test_unmapped_empty_and_unknown_selection_never_dispatch_or_claim_pass(self):
        proc, report = self.quick(expected='NO_CHANGES')
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn('selectedTests', report)
        self.repo.write('shared/registry.json', '{}')
        proc, report = self.quick(expected='NEEDS_FULL')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report['selection']['unmappedPaths'], ['shared/registry.json'])
        self.assertNotIn('selectedTests', report)
        proc, report = self.quick('--focus', 'tests.test_absent', expected='FAIL')
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn('selectedTests', report)

    def test_selected_failure_or_repository_mutation_fail_quick(self):
        proc, _ = self.quick('--focus', 'test_browser_sentinel', expected='FAIL')
        self.assertEqual(proc.returncode, 1)
        self.repo.write('tests/test_one.py', 'import unittest\nfrom pathlib import Path\nclass One(unittest.TestCase):\n'
                        ' def test_ok(self): Path("escaped.txt").write_text("changed")\n')
        proc, report = self.quick('--focus', 'tests.test_one', expected='FAIL')
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(report['selectedTests']['pass'])
        self.assertFalse(report['checks']['workingTreeIntegrity']['pass'])
        self.assertIn('escaped.txt', report['checks']['workingTreeIntegrity']['pathsAdded'])

    def test_quick_never_calls_full_gates_and_full_default_remains_full(self):
        from scripts.core import local_ci_quick
        with patch.object(local_ci_quick, 'run_quick', return_value=0) as quick, \
                patch.object(local_ci, 'run_gates') as full:
            self.assertEqual(local_ci.main(['--quick', '--focus', 'tests.test_local_ci']), 0)
            quick.assert_called_once()
            full.assert_not_called()
        with patch.object(local_ci, 'full_test_gate', side_effect=RuntimeError('full gate reached')) as full, \
                patch.object(local_ci_quick, 'run_quick') as quick, \
                patch.object(local_ci, 'wrapper_sync_gate', return_value={'pass': True}), \
                patch.object(local_ci, '_temporary_checker_gate', return_value={'pass': True}), \
                redirect_stdout(io.StringIO()):
            result = local_ci.main(['--report-out', str(self.output / 'full.json')])
            self.assertEqual(result, 1)
            full.assert_called_once()
            quick.assert_not_called()
