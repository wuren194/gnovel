"""Public plugin archives remain usable after moving out of the checkout."""
from __future__ import annotations

from scripts.core.project import current_schema_version

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import package_plugin
from scripts.check_release_contents import check_zip


ROOT = Path(__file__).resolve().parents[1]


def write(root, relative, data):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data if isinstance(data, bytes) else data.encode())
    return target


def small_worktree(root):
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    manifest = {'name': 'govel', 'version': '0.7.0', 'skills': ['govel-router'],
                'schemaVersion': current_schema_version(), 'workflowVersion': '6.0', 'sharedProtocols': {
                    'contracts': 'shared/contracts', 'contractsList': ['WORKSTATION.md'],
                    'workflowPolicy': 'shared/registries/workflow.json',
                    'settingsSchema': 'shared/registries/settings.json',
                    'sourceModel': 'shared/registries/source-model.json',
                    'toolRegistry': 'shared/registries/tools.json'}}
    files = {
        'LICENSE', 'THIRD_PARTY_NOTICES.md', 'SKILL.md', 'README.md',
        'mcp.json', 'mcp_config.json', '.claude-plugin/marketplace.json',
        '.agents/plugins/marketplace.json', 'skills/govel-bind/SKILL.md',
        'skills/govel-bind/binding.json', 'skills/govel-bind/scripts/bind.py',
        'skills/govel/SKILL.md', 'commands/init.md', 'scripts/mcp_server.py',
        'scripts/novelctl.py', 'scripts/runtime_requirements.py', 'scripts/core/project.py',
        'knowledge/README.md', 'knowledge/schema.json', 'knowledge/example.json',
        'local/README.md', 'local/knowledge/.gitkeep', 'govel-router/SKILL.md',
        'shared/contracts/WORKSTATION.md', 'shared/registries/workflow.json',
        'shared/registries/settings.json', 'shared/registries/source-model.json',
        'shared/registries/tools.json', '.agents/skills.json', '.claude/SKILL.md',
        '.agents/skills/govel/SKILL.md', '.agents/skills/govel-router/SKILL.md',
        '.claude/agents/govel-router.md', '.codex/agents/govel-router.toml',
    }
    for relative in files:
        write(root, relative, b'' if relative.endswith('.gitkeep') else b'public fixture\n')
    write(root, 'toolbox.manifest.json', json.dumps(manifest))
    for relative in ('plugin.json', '.claude-plugin/plugin.json', '.antigravity-plugin/plugin.json'):
        write(root, relative, json.dumps({'name': 'govel', 'version': manifest['version']}))
    write(root, '.gitignore', '/local/*\n!/local/README.md\n!/local/knowledge/\n/local/knowledge/*\n!/local/knowledge/.gitkeep\n')
    subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    return manifest


class PluginPackageBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='govel-plugin-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'source with spaces'
        small_worktree(self.root)
        self.output = self.base / 'published with spaces' / 'govel.zip'

    def test_private_forced_additions_and_developer_files_never_enter_archive(self):
        marker = b'PRIVATE_SENTINEL_FOR_PACKAGE_BOUNDARY'
        private = ['local/roundtable.md', 'local/provider.json', 'local/knowledge/assets/private.md',
                   'scripts/provider.json', 'docs/.env.production', 'scripts/.govel/structure.json',
                   'knowledge/sources/legacy/original.md', 'knowledge/assets/reference.md',
                   'tests/not-distributed.py', 'requirements-dev.txt', '.claude/settings.json',
                   '.claude/settings.local.json', 'scripts/__pycache__/generated.pyc']
        for relative in private:
            write(self.root, relative, marker)
        subprocess.run(['git', 'add', '-f', *private], cwd=self.root, check=True)
        write(self.root, 'scripts/untracked_runtime.py', 'VALUE = "current untracked bytes"\n')
        readme = self.root / 'README.md'
        readme.write_text('current dirty bytes\n')
        readme.chmod(0o640)
        report = package_plugin.build_plugin(self.root, self.output)
        self.assertTrue(report['ok'])
        self.assertEqual(report['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertTrue(check_zip(self.output, public=True)['ok'])
        with zipfile.ZipFile(self.output) as archive:
            names = set(archive.namelist())
            self.assertFalse(names & set(private))
            self.assertFalse(any(name.startswith('.git/') for name in names))
            self.assertEqual(archive.read('README.md'), b'current dirty bytes\n')
            self.assertEqual(stat.S_IMODE(archive.getinfo('README.md').external_attr >> 16), 0o640)
            self.assertEqual(archive.read('local/knowledge/.gitkeep'), b'')
            self.assertIn('scripts/untracked_runtime.py', names)
            self.assertNotIn(marker, b''.join(archive.read(name) for name in names))
        self.assertFalse(list(self.output.parent.glob('.govel-plugin-*')))

    def test_plan_is_read_only_and_incomplete_runtime_or_version_drift_is_rejected(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = package_plugin.plan(self.root)
        self.assertTrue(report['ok'])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        target = self.root / 'shared/contracts/WORKSTATION.md'
        target.unlink()
        with self.assertRaises(package_plugin.PluginPackageError) as caught:
            package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(caught.exception.code, 'PLUGIN_RUNTIME_INCOMPLETE')
        write(self.root, 'shared/contracts/WORKSTATION.md', 'restored')
        write(self.root, '.claude-plugin/plugin.json', '{"name":"govel","version":"wrong"}')
        with self.assertRaises(package_plugin.PluginPackageError) as caught:
            package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(caught.exception.code, 'PLUGIN_VERSION_MISMATCH')
        self.assertFalse(self.output.exists())

    def test_nonempty_public_placeholder_is_rejected(self):
        write(self.root, 'local/knowledge/.gitkeep', b'private content')
        with self.assertRaises(package_plugin.PluginPackageError) as caught:
            package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(caught.exception.code, 'PLUGIN_PRIVATE_CONTENT')
        self.assertFalse(self.output.exists())

    def test_output_inside_worktree_and_existing_output_are_not_written(self):
        with self.assertRaises(package_plugin.PluginPackageError):
            package_plugin.build_plugin(self.root, self.root / 'ignored' / 'package.zip')
        self.assertFalse((self.root / 'ignored').exists())
        self.output.parent.mkdir()
        self.output.write_bytes(b'existing archive')
        with self.assertRaises(FileExistsError):
            package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'existing archive')

    def test_symlink_input_cannot_copy_an_outside_file(self):
        sentinel = write(self.base, 'private.txt', b'outside private bytes')
        (self.root / 'scripts/escape.py').symlink_to(sentinel)
        with self.assertRaises(package_plugin.PluginPackageError) as caught:
            package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(caught.exception.code, 'PLUGIN_SOURCE_UNSAFE')
        self.assertFalse(self.output.exists())
        self.assertEqual(sentinel.read_bytes(), b'outside private bytes')

    def test_changed_source_never_publishes_a_successful_package(self):
        original = package_plugin._write_zip
        def changed_after_write(stream, snapshot):
            original(stream, snapshot)
            (self.root / 'README.md').write_text('changed while packaging')
        with patch.object(package_plugin, '_write_zip', side_effect=changed_after_write):
            with self.assertRaises(package_plugin.PluginPackageError) as caught:
                package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(caught.exception.code, 'PLUGIN_SOURCE_CHANGED')
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.glob('.govel-plugin-*')))

    def test_competing_output_is_not_overwritten(self):
        original = package_plugin._write_zip
        def occupy_after_write(stream, snapshot):
            original(stream, snapshot)
            self.output.write_bytes(b'other publisher')
        with patch.object(package_plugin, '_write_zip', side_effect=occupy_after_write):
            with self.assertRaises(FileExistsError):
                package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b'other publisher')

    def test_archive_tampering_after_verification_cannot_be_rebound_as_success(self):
        original = package_plugin._verify_source
        def alter_archive_after_verification(snapshot):
            original(snapshot)
            temporary = next(self.output.parent.glob('.govel-plugin-*'))
            temporary.write_bytes(b'tampered after archive verification')
        with patch.object(package_plugin, '_verify_source', side_effect=alter_archive_after_verification):
            with self.assertRaises(package_plugin.PluginPackageError) as caught:
                package_plugin.build_plugin(self.root, self.output)
        self.assertEqual(caught.exception.code, 'PLUGIN_ARCHIVE_MISMATCH')
        self.assertFalse(self.output.exists())


class InstalledPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='govel-installed-plugin-')
        cls.base = Path(cls.temporary.name).resolve()
        cls.archive = cls.base / 'public plugin.zip'
        package_plugin.build_plugin(ROOT, cls.archive)
        cls.plugin = cls.base / 'installed cache' / 'govel release'
        with zipfile.ZipFile(cls.archive) as archive:
            archive.extractall(cls.plugin)
        cls.cwd = cls.base / 'unrelated working directory'
        cls.cwd.mkdir()
        cls.state = cls.base / 'stable user data'
        cls.env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('GOVEL_', 'PYTHONPATH'))}
        cls.env.update(PYTHONDONTWRITEBYTECODE='1', GOVEL_LOG_MODE='off')
        cls.binder = cls.plugin / 'skills/govel-bind/scripts/bind.py'
        cls.version = json.loads((cls.plugin / 'toolbox.manifest.json').read_text())['version']
        cls.original = {str(path.relative_to(cls.plugin)): path.read_bytes()
                        for path in cls.plugin.rglob('*') if path.is_file()}
        for path in cls.plugin.rglob('*'):
            path.chmod(0o555 if path.is_dir() else 0o444)
        cls.plugin.chmod(0o555)

    @classmethod
    def tearDownClass(cls):
        for path in [cls.plugin, *cls.plugin.rglob('*')]:
            if path.is_dir():
                path.chmod(0o755)
        cls.temporary.cleanup()

    def run_binder(self, action, request=''):
        return subprocess.run([sys.executable, str(self.binder), action, '--host', 'plugin',
                               '--engine-root', str(self.plugin), '--state-root', str(self.state), '--json'],
                              input=request, cwd=self.cwd, env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_read_only_installed_package_checks_and_serves_from_an_unrelated_directory(self):
        checked = self.run_binder('check')
        self.assertEqual(checked.returncode, 0, checked.stderr + checked.stdout)
        report = json.loads(checked.stdout)
        self.assertTrue(report['ok'])
        self.assertEqual(report['engineRoot'], str(self.plugin))
        self.assertEqual(report['version'], self.version)
        self.assertEqual(report['dataHome'], str(self.state / 'data'))
        self.assertEqual(report['hostConnection'], 'not_observed')
        self.assertFalse(report['authorApprovalGranted'])
        self.assertTrue(report['processHandshake']['ok'])
        self.assertFalse(self.state.exists(), 'a check must not create installation or user-data state')
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'plugin-distribution-test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'get_tool_help', 'arguments': {'operation': 'project.overview'}}},
        ]
        served = self.run_binder('serve', ''.join(json.dumps(row) + '\n' for row in messages))
        self.assertEqual(served.returncode, 0, served.stderr + served.stdout)
        replies = [json.loads(line) for line in served.stdout.splitlines()]
        self.assertEqual([row['id'] for row in replies], [1, 2, 3])
        self.assertEqual(replies[0]['result']['serverInfo']['version'], self.version)
        self.assertIn('get_tool_help', {tool['name'] for tool in replies[1]['result']['tools']})
        self.assertFalse(replies[2]['result'].get('isError'))
        self.assertTrue(replies[2]['result']['structuredContent']['ok'])
        self.assertEqual(self.original, {str(p.relative_to(self.plugin)): p.read_bytes()
                                         for p in self.plugin.rglob('*') if p.is_file()})

    def test_installed_public_doctor_can_check_wrappers_without_a_git_checkout(self):
        book = self.base / 'neutral book'
        env = {**self.env, 'GOVEL_DATA_HOME': str(self.state / 'data')}
        bootstrap = '''
from pathlib import Path
import runpy
import sys
test_home = Path(sys.argv.pop(1))
Path.home = classmethod(lambda cls: test_home)
entry = Path(sys.argv[1])
sys.path.insert(0, str(entry.parent))
sys.argv = sys.argv[1:]
runpy.run_path(str(entry), run_name='__main__')
'''
        command = [sys.executable, '-c', bootstrap, str(self.base / 'isolated host home'),
                   str(self.plugin / 'scripts/novelctl.py')]
        created = subprocess.run([*command, 'project', 'quick-init', '--project-root', str(book),
                                  '--book-name', '中性安装样例', '--no-agents-md'],
                                 cwd=self.cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        checked = subprocess.run([*command, 'project', 'doctor', '--project-root', str(book)],
                                 cwd=self.cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertIn(checked.returncode, (0, 1), checked.stderr + checked.stdout)
        report = json.loads(checked.stdout)
        self.assertTrue(report['dataIntegrity']['healthy'], report['issues'])
        self.assertTrue(report['tooling']['wrappers']['ok'], report['tooling'])
        self.assertEqual(report['tooling']['wrappers']['drift'], [])
        self.assertFalse(report['tooling']['releaseContents']['applicable'])
        self.assertFalse(report['tooling']['antigravityInstall']['applicable'])
        self.assertFalse({'RELEASE_CHECK_FAILED', 'WRAPPER_DRIFT', 'WRAPPER_CHECK_FAILED'}
                         & {row['code'] for row in report['issues']}, report['issues'])
        self.assertEqual(self.original, {str(p.relative_to(self.plugin)): p.read_bytes()
                                         for p in self.plugin.rglob('*') if p.is_file()})


if __name__ == '__main__':
    unittest.main()
