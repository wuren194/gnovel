"""Runtime diagnostics do not infer maintenance scope from copied Git metadata."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.core import project, project_doctor


class PackagedToolingTests(unittest.TestCase):
    def test_installed_runtime_does_not_probe_git_or_an_unrelated_host_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrappers = {'ok': True, 'drift': []}
            with patch.object(project, 'TOOLBOX_ROOT', root), \
                    patch('importlib.import_module', return_value=SimpleNamespace(check=lambda: wrappers)) as loader:
                result, issues = project_doctor._tooling_domain()
            self.assertEqual([call.args[0] for call in loader.call_args_list], ['generate_wrappers'])
            self.assertEqual(result['wrappers'], wrappers)
            self.assertEqual(issues, [])
            for name in ('releaseContents', 'antigravityInstall'):
                self.assertFalse(result[name]['applicable'])
                self.assertIsNone(result[name]['ok'])
            self.assertEqual(result['antigravityInstall']['hostConnection'], 'not_observed')

    def test_packaging_does_not_hide_a_broken_runtime_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            wrappers = {'ok': False, 'drift': ['skills/govel/SKILL.md']}
            with patch.object(project, 'TOOLBOX_ROOT', Path(directory)), \
                    patch('importlib.import_module', return_value=SimpleNamespace(check=lambda: wrappers)):
                result, issues = project_doctor._tooling_domain()
            self.assertFalse(result['wrappers']['ok'])
            self.assertEqual([item['code'] for item in issues], ['WRAPPER_DRIFT'])

    def test_git_metadata_does_not_enable_release_or_unrelated_host_checks(self):
        for kind in ('directory', 'file'):
            with self.subTest(gitMetadata=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if kind == 'directory':
                    (root / '.git').mkdir()
                else:
                    (root / '.git').write_text('gitdir: external-checkout-metadata\n')
                wrappers = {'ok': True, 'drift': []}
                with patch.object(project, 'TOOLBOX_ROOT', root), \
                        patch('importlib.import_module', return_value=SimpleNamespace(check=lambda: wrappers)) as loader:
                    result, issues = project_doctor._tooling_domain()
                self.assertEqual([call.args[0] for call in loader.call_args_list], ['generate_wrappers'])
                self.assertEqual(result['wrappers'], wrappers)
                self.assertFalse(result['releaseContents']['applicable'])
                self.assertFalse(result['antigravityInstall']['applicable'])
                self.assertIsNone(result['releaseContents']['ok'])
                self.assertIsNone(result['antigravityInstall']['ok'])
                self.assertEqual(issues, [])
