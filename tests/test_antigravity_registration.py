"""The documented Antigravity config is required, safely repairable, and not a handshake."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_antigravity as installer
from scripts.core import antigravity_registration as registration


class AntigravityRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.gemini = self.directory / '.gemini'
        self.config = self.gemini / 'config/mcp_config.json'
        self.skills = self.gemini / 'config/skills/gnovel'
        self.skills.mkdir(parents=True)
        for name in installer.LINKS:
            (self.skills / name).symlink_to(installer.REPO / name)
        overrides = patch.multiple(installer, GEMINI=self.gemini, SKILL_ROOT=self.skills,
                                   MCP_CONFIG=self.config, MANIFEST_ERROR=None)
        overrides.start()
        self.addCleanup(overrides.stop)

    def entry(self):
        return {'command': sys.executable, 'args': [str(installer.MCP_SERVER)]}

    def write(self, value):
        self.config.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def backups(self):
        return list(self.config.parent.glob('.mcp_config.json.gnovel-backup-*'))

    def test_missing_current_config_is_not_satisfied_by_old_settings(self):
        old = self.gemini / 'settings.json'
        old.write_text(json.dumps({'mcpServers': {'gnovel': self.entry()}}))
        original = old.read_bytes()
        report = installer.check()
        self.assertFalse(report['ok'])
        self.assertTrue(report['installed'])
        self.assertEqual(report['mcp']['configPath'], str(self.config))
        self.assertEqual(report['mcp']['problems'][0]['code'], 'MCP_CONFIG_MISSING')
        self.assertFalse(self.config.exists())
        self.assertEqual(old.read_bytes(), original)
        self.assertFalse((self.config.parent / '.gnovel-mcp-install.lock').exists())

    def test_missing_registration_and_bad_fields_are_reported_without_secrets(self):
        secret = 'a-private-value-never-printed'
        self.write({'mcpServers': {'Other': {'command': 'server', 'env': {'API_KEY': secret}}}})
        report = installer.check()
        self.assertFalse(report['ok'])
        self.assertEqual(report['mcp']['problems'][0]['code'], 'MCP_REGISTRATION_MISSING')
        self.assertNotIn(secret, json.dumps(report))
        changes = [
            {'disabled': True}, {'enabled': False}, {'disabled': 'false'}, {'enabled': 0},
            {'command': '/this-command-does-not-exist'}, {'command': '/bin/sh'},
            {'command': '\0'}, {'args': [str(self.directory / 'exists.py')]},
            {'args': ['scripts/mcp_server.py']}, {'args': []}, {'args': [True]}, {'args': ['\0']},
            {'type': 'http'}, {'url': 'https://invalid.example/'}, {'serverUrl': ''},
            {'env': []}, {'env': {'API_KEY': 1}}, {'env': {'bad=name': secret}},
            {'cwd': 'relative'}, {'cwd': '\0'},
        ]
        (self.directory / 'exists.py').write_text('pass\n')
        for change in changes:
            with self.subTest(change=change):
                self.write({'mcpServers': {'gnovel': {**self.entry(), **change}}})
                before = self.config.read_bytes()
                report = installer.check()
                self.assertFalse(report['ok'], change)
                self.assertFalse(report['mcp']['configHealthy'])
                self.assertFalse(report['mcp']['connectionVerified'])
                self.assertEqual(self.config.read_bytes(), before)
                self.assertNotIn(secret, json.dumps(report))

    def test_static_success_does_not_claim_running_client_connection(self):
        self.write({'mcpServers': {'gnovel': self.entry()}})
        report = installer.check()
        self.assertTrue(report['ok'], report)
        self.assertTrue(report['mcp']['configHealthy'])
        self.assertEqual(report['mcp']['status'], 'configured')
        self.assertFalse(report['mcp']['connectionVerified'])
        self.assertEqual(report['mcp']['hostConnection'], 'not_observed')
        self.assertFalse((self.config.parent / '.gnovel-mcp-install.lock').exists())

    def test_non_executable_python_is_not_healthy(self):
        command = self.directory / 'python3'
        command.write_text('#!/bin/sh\nexit 0\n')
        command.chmod(0o600)
        self.write({'mcpServers': {'gnovel': {**self.entry(), 'command': str(command)}}})
        report = installer.check()
        self.assertFalse(report['ok'])
        self.assertIn('MCP_COMMAND_INVALID', {row['code'] for row in report['mcp']['problems']})

    def test_install_creates_registration_and_second_install_preserves_bytes_and_mtime(self):
        old = self.gemini / 'settings.json'
        old.write_text('{"untouched": "other application"}')
        before_old = old.read_bytes()
        first = installer.install()
        self.assertTrue(first['ok'], first)
        self.assertTrue(first['mcp']['changed'])
        self.assertIsNone(first['mcp']['backupPath'])
        data = json.loads(self.config.read_text())
        self.assertEqual(data['mcpServers']['gnovel'], self.entry())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns
        second = installer.install()
        self.assertTrue(second['ok'], second)
        self.assertFalse(second['mcp']['changed'])
        self.assertTrue(all(row.startswith('already linked:') for row in second['actions']))
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(old.read_bytes(), before_old)

    def test_repair_preserves_other_entries_options_and_exact_backup(self):
        original = {'theme': {'custom': True}, 'mcpServers': {
            'Other': {'url': 'https://other.invalid/', 'headers': {'Authorization': 'Bearer not-a-real-secret'}},
            'gnovel': {'command': 'missing-python', 'args': ['/old/mcp_server.py', '--discovery-view', 'audit'],
                        'type': 'http', 'url': 'https://obsolete.invalid', 'serverUrl': 'https://old.invalid',
                        'disabled': True, 'enabled': False, 'env': {'PRIVATE_LOCAL_SETTING': 'preserved-value'},
                        'customOption': {'retain': 'yes'}, 'timeout': 60000}}}
        self.write(original)
        before = self.config.read_bytes()
        report = installer.install()
        self.assertTrue(report['ok'], report)
        actual = json.loads(self.config.read_text())
        expected = deepcopy(original)
        entry = expected['mcpServers']['gnovel']
        entry.update(command=sys.executable, args=[str(installer.MCP_SERVER), '--discovery-view', 'audit'],
                     type='stdio', disabled=False, enabled=True)
        del entry['url'], entry['serverUrl']
        self.assertEqual(actual, expected)
        backup = Path(report['mcp']['backupPath'])
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(set(report['mcp']['changedFields']),
                         {'command', 'args', 'type', 'url', 'serverUrl', 'disabled', 'enabled'})
        self.assertNotIn('preserved-value', json.dumps(report))
        self.assertNotIn('not-a-real-secret', json.dumps(report))
        installer.install()
        self.assertEqual(self.backups(), [backup])

    def test_malformed_config_is_never_replaced(self):
        for raw in ('{broken', '[]', '{"mcpServers":[]}', '{"x":NaN}', '{"x":1,"x":2}',
                    '{"option":1e999,"mcpServers":{}}', '{"nested":{"value":-1e999},"mcpServers":{}}',
                    '{"mcpServers":{"gnovel":[]}}', '{"mcpServers":{"gnovel":null}}',
                    '{"mcpServers":{"gnovel":{"env":[]}}}'):
            with self.subTest(raw=raw):
                self.config.write_text(raw)
                report = installer.install()
                self.assertFalse(report['ok'])
                self.assertEqual(self.config.read_text(), raw)
                self.assertEqual(self.backups(), [])

    def test_wrong_boolean_types_are_repaired_even_when_python_equality_matches(self):
        self.write({'mcpServers': {'gnovel': {**self.entry(), 'disabled': 0, 'enabled': 1}}})
        report = installer.install()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['mcp']['changedFields'], ['disabled', 'enabled'])
        entry = json.loads(self.config.read_text())['mcpServers']['gnovel']
        self.assertIs(entry['disabled'], False)
        self.assertIs(entry['enabled'], True)

    def test_symlinks_hardlinks_and_directory_config_are_not_overwritten(self):
        outside = self.directory / 'outside.json'
        outside.write_text('{"doNotChange":true}')
        for kind in ('symlink', 'hardlink', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    self.config.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, self.config)
                else:
                    self.config.mkdir()
                report = installer.install()
                self.assertFalse(report['ok'])
                self.assertEqual(outside.read_text(), '{"doNotChange":true}')
                if kind == 'directory':
                    self.config.rmdir()
                else:
                    self.config.unlink()
        redirected = self.directory / 'redirected'
        redirected.mkdir()
        symbolic = self.directory / 'alias/.gemini'
        symbolic.parent.mkdir()
        symbolic.symlink_to(redirected)
        with self.assertRaises(registration.RegistrationError) as caught:
            registration.install_registration(symbolic / 'config/mcp_config.json', installer.MCP_SERVER)
        self.assertEqual(caught.exception.code, 'MCP_CONFIG_SYMLINK')
        self.assertEqual(list(redirected.iterdir()), [])

    def test_edit_after_last_check_is_preserved_as_the_actual_displaced_version(self):
        self.write({'mcpServers': {}, 'before': True})
        real_rename = os.rename
        concurrent = {'mcpServers': {'Other': {'command': 'preserve-new-server'}}, 'new': True}

        def change_before_displacement(source, destination):
            self.write(concurrent)
            return real_rename(source, destination)

        with patch.object(registration.os, 'rename', side_effect=change_before_displacement):
            report = installer.install()
        self.assertFalse(report['ok'])
        self.assertEqual(report['mcp']['errorCode'], 'MCP_CONFIG_CHANGED')
        self.assertEqual(json.loads(self.config.read_text()), concurrent)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(json.loads(self.backups()[0].read_text()), concurrent)
        self.assertEqual(stat.S_IMODE(self.backups()[0].stat().st_mode), 0o600)
        self.assertEqual(report['mcp']['backupPath'], str(self.backups()[0]))
        self.assertEqual([p for p in self.config.parent.glob('.gnovel-mcp-*')
                          if p.name != '.gnovel-mcp-install.lock'], [])

    def test_existing_install_lock_blocks_without_modifying_configuration(self):
        self.write({'mcpServers': {}})
        before = self.config.read_bytes()
        lock = self.config.parent / '.gnovel-mcp-install.lock'
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = installer.install()
        finally:
            os.close(descriptor)
        self.assertFalse(report['ok'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_concurrent_creation_of_missing_config_is_not_overwritten(self):
        concurrent = {'mcpServers': {'Other': {'command': 'new-local-service'}}}
        real_link = os.link

        def create_before_publication(source, destination, **kwargs):
            self.write(concurrent)
            return real_link(source, destination, **kwargs)

        with patch.object(registration.os, 'link', side_effect=create_before_publication):
            report = installer.install()
        self.assertFalse(report['ok'])
        self.assertEqual(json.loads(self.config.read_text()), concurrent)
        self.assertEqual(self.backups(), [])

    def test_competing_publication_preserves_both_current_and_actual_original(self):
        self.write({'mcpServers': {'Original': {'command': 'original-server'}}})
        before = self.config.read_bytes()
        concurrent = {'mcpServers': {'Concurrent': {'command': 'new-server'}}}
        real_link = os.link

        def replace_before_publication(source, destination, **kwargs):
            if Path(source).name.startswith('.gnovel-mcp-staged-'):
                self.write(concurrent)
            return real_link(source, destination, **kwargs)

        with patch.object(registration.os, 'link', side_effect=replace_before_publication):
            report = installer.install()
        self.assertFalse(report['ok'])
        self.assertEqual(report['mcp']['errorCode'], 'MCP_CONFIG_CHANGED')
        self.assertEqual(json.loads(self.config.read_text()), concurrent)
        self.assertEqual(self.backups()[0].read_bytes(), before)
        self.assertIsNone(registration.pending_registration(self.config))

    def test_failed_publication_restores_actual_original_without_replacing(self):
        self.write({'mcpServers': {}})
        before = self.config.read_bytes()
        real_link = os.link

        def fail_publish_only(source, destination, **kwargs):
            if Path(source).name.startswith('.gnovel-mcp-staged-'):
                raise OSError('simulated publication failure')
            return real_link(source, destination, **kwargs)

        with patch.object(registration.os, 'link', side_effect=fail_publish_only):
            report = installer.install()
        self.assertFalse(report['ok'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), before)
        self.assertIsNone(registration.pending_registration(self.config))

    def test_sigkill_displacement_and_publication_windows_are_recoverable(self):
        program = '''
import os, signal, sys
from pathlib import Path
from scripts.core import antigravity_registration as registration
configuration, script, stop_at = map(str, sys.argv[1:])
real_rename, real_link = os.rename, os.link
def move(source, destination):
    if stop_at == 'before_move':
        os.kill(os.getpid(), signal.SIGKILL)
    real_rename(source, destination)
    if stop_at == 'after_move':
        os.kill(os.getpid(), signal.SIGKILL)
def publish(source, destination, **kwargs):
    real_link(source, destination, **kwargs)
    if stop_at == 'after_link':
        os.kill(os.getpid(), signal.SIGKILL)
registration.os.rename, registration.os.link = move, publish
registration.install_registration(Path(configuration), Path(script))
'''
        for window in ('before_move', 'after_move', 'after_link'):
            with self.subTest(window=window):
                self.write({'keep': window, 'mcpServers': {'Other': {'command': 'preserved'}}})
                process = subprocess.run([sys.executable, '-c', program, str(self.config),
                                          str(installer.MCP_SERVER), window], cwd=installer.REPO,
                                         env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                                              'GNOVEL_LOG_MODE': 'off'}, capture_output=True, timeout=15)
                self.assertEqual(process.returncode, -9, process.stderr)
                pending = registration.pending_registration(self.config)
                self.assertIsNotNone(pending)
                self.assertFalse(installer.check()['ok'])
                self.assertEqual(installer.check()['mcp']['problems'][0]['code'], 'MCP_INSTALL_INCOMPLETE')
                if window == 'after_move':
                    self.assertFalse(self.config.exists())
                    self.assertEqual(json.loads(Path(pending['backupPath']).read_text())['keep'], window)
                report = installer.install()
                self.assertTrue(report['ok'], report)
                self.assertIsNotNone(report['mcp']['recovery'])
                result = json.loads(self.config.read_text())
                self.assertEqual(result['keep'], window)
                self.assertEqual(result['mcpServers']['Other'], {'command': 'preserved'})
                self.assertEqual(result['mcpServers']['gnovel'], self.entry())
                self.assertIsNone(registration.pending_registration(self.config))
                self.assertEqual(self.config.stat().st_nlink, 1)

    def test_server_option_tail_uses_real_parser_and_help_never_counts_as_serving(self):
        from scripts import mcp_server
        from scripts.core.mcp_options import startup_arguments_valid
        from contextlib import redirect_stderr
        tails = [(['--discovery-view', 'not-a-view'], False), (['--help'], False), (['-h'], False),
                 (['--hel'], False), (['--unknown-option'], False), (['--audience'], False),
                 (['--discovery-view', 'audit', '--task', 'repair'], True),
                 (['--aud', 'writer', '--discovery-view=model'], True), ([], True)]
        for tail, valid in tails:
            with self.subTest(tail=tail):
                self.assertEqual(startup_arguments_valid(tail), valid)
                self.write({'mcpServers': {'gnovel': {**self.entry(), 'args': [str(installer.MCP_SERVER), *tail]}}})
                self.assertEqual(installer.check()['mcp']['configHealthy'], valid)
                with patch('sys.argv', ['mcp_server.py', *tail]), patch('sys.stdin', io.StringIO()), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    if valid:
                        self.assertEqual(mcp_server.main(), 0)
                    else:
                        with self.assertRaises(SystemExit):
                            mcp_server.main()
                report = installer.install()
                self.assertTrue(report['ok'], report)
                self.assertEqual(json.loads(self.config.read_text())['mcpServers']['gnovel']['args'],
                                 [str(installer.MCP_SERVER), *(tail if valid else [])])

    def test_command_resolution_uses_configured_environment_path(self):
        environment = {'PATH': str(self.directory / 'no-executables')}
        self.write({'mcpServers': {'gnovel': {'command': 'python3',
                                              'args': [str(installer.MCP_SERVER)], 'env': environment}}})
        before = self.config.read_bytes()
        report = installer.check()
        self.assertFalse(report['mcp']['configHealthy'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn('MCP_COMMAND_INVALID', {row['code'] for row in report['mcp']['problems']})
        report = installer.install()
        self.assertTrue(report['ok'], report)
        entry = json.loads(self.config.read_text())['mcpServers']['gnovel']
        self.assertEqual(entry['env'], environment)
        self.assertEqual(entry['command'], sys.executable)
        result = subprocess.run([entry['command'], *entry['args']], input='', text=True,
                                env={**os.environ, **environment, 'GNOVEL_LOG_MODE': 'off'},
                                capture_output=True, timeout=15, cwd=installer.REPO)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_discovery_filters_do_not_count_as_healthy_registration(self):
        for tail in (['--audience', 'not-a-station'], ['--task', 'not-a-task']):
            with self.subTest(tail=tail):
                self.write({'mcpServers': {'gnovel': {**self.entry(), 'args': [str(installer.MCP_SERVER), *tail]}}})
                self.assertFalse(installer.check()['mcp']['configHealthy'])
                self.assertTrue(installer.install()['ok'])
                self.assertEqual(json.loads(self.config.read_text())['mcpServers']['gnovel']['args'],
                                 [str(installer.MCP_SERVER)])

    def test_recovery_never_overwrites_new_config_after_interruption(self):
        before = {'mcpServers': {'Original': {'command': 'old-server'}}}
        self.write(before)
        real_rename = os.rename

        class SimulatedKill(BaseException):
            pass

        def stop_after_move(source, destination):
            real_rename(source, destination)
            raise SimulatedKill()

        with patch.object(registration.os, 'rename', side_effect=stop_after_move), self.assertRaises(SimulatedKill):
            registration.install_registration(self.config, installer.MCP_SERVER)
        pending = registration.pending_registration(self.config)
        concurrent = {'mcpServers': {'Concurrent': {'command': 'new-server'}}}
        self.write(concurrent)
        report = installer.install()
        self.assertFalse(report['ok'])
        self.assertEqual(report['mcp']['errorCode'], 'MCP_RECOVERY_CONFLICT')
        self.assertEqual(json.loads(self.config.read_text()), concurrent)
        self.assertEqual(json.loads(Path(pending['backupPath']).read_text()), before)
        self.assertIsNone(registration.pending_registration(self.config))

    def test_recovery_reports_a_concurrent_write_through_the_displaced_descriptor(self):
        self.write({'mcpServers': {'Original': {'command': 'old-server'}}})
        descriptor = os.open(self.config, os.O_WRONLY)
        real_link = os.link
        concurrent = {'mcpServers': {'Concurrent': {'command': 'new-server'}}}

        class SimulatedKill(BaseException):
            pass

        def stop_after_publication(source, destination, **kwargs):
            real_link(source, destination, **kwargs)
            if Path(source).name.startswith('.gnovel-mcp-staged-'):
                payload = json.dumps(concurrent).encode('utf-8')
                os.ftruncate(descriptor, 0)
                os.write(descriptor, payload)
                os.fsync(descriptor)
                raise SimulatedKill()

        try:
            with patch.object(registration.os, 'link', side_effect=stop_after_publication), self.assertRaises(SimulatedKill):
                registration.install_registration(self.config, installer.MCP_SERVER)
        finally:
            os.close(descriptor)
        pending = registration.pending_registration(self.config)
        installed = json.loads(self.config.read_text())
        self.assertIn('gnovel', installed['mcpServers'])
        report = installer.install()
        self.assertFalse(report['ok'])
        self.assertEqual(report['mcp']['errorCode'], 'MCP_RECOVERY_CONFLICT')
        self.assertEqual(json.loads(self.config.read_text()), installed)
        self.assertEqual(json.loads(Path(pending['backupPath']).read_text()), concurrent)
        self.assertIsNone(registration.pending_registration(self.config))

    def test_cli_exit_follows_configuration_health_and_explains_scope(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(installer.main(['--check', '--json']), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report['mcp']['configHealthy'])
        installer.install()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(installer.main(['--check', '--json']), 0)
        self.assertFalse(json.loads(output.getvalue())['mcp']['connectionVerified'])


if __name__ == '__main__':
    unittest.main()
