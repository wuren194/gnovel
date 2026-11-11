"""Copied bootstrap skills, host configuration isolation, and real MCP startup."""
from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/govel-bind'
spec = importlib.util.spec_from_file_location('govel_bootstrap_test', SKILL / 'scripts/bind.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
import engine_install
import host_registration


class GovelBindTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='govel-bind-test-'))
        self.base = Path(temporary).resolve()
        self.state = self.base / 'state'
        self.policy = bootstrap.read_policy()
        self.engine = bootstrap.engine_info(ROOT, self.policy)
        self.stack.enter_context(patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.base / 'data'),
            'GOVEL_PROVIDER_CONFIG': '', 'GOVEL_LOG_MODE': 'off', 'PYTHONDONTWRITEBYTECODE': '1'}))

    def copy_skill(self):
        target = self.base / 'separate installation' / 'govel-bind'
        shutil.copytree(SKILL, target, ignore=shutil.ignore_patterns('__pycache__'))
        return target

    def child(self, script, *arguments, input=''):
        return subprocess.run([sys.executable, str(script), *arguments], cwd=self.base,
                              input=input, capture_output=True, text=True, timeout=30,
                              env=dict(os.environ))

    def configure_host(self, host, data):
        suffix = 'config.toml' if host == 'codex' else '.claude.json' if host == 'claude' else 'mcp_config.json'
        self.config = self.base / host / suffix
        self.config.parent.mkdir()
        self.config.write_text(data, encoding='utf-8')
        self.stack.enter_context(patch.object(host_registration, 'configuration_path', return_value=self.config))
        if host != 'antigravity':
            self.stack.enter_context(patch.object(host_registration.shutil, 'which', return_value='/test/' + host))

    def fake_cli(self, host, *, after=None, extra=False, code=0):
        real_run = subprocess.run
        self.cli_calls = []

        def run(command, **kwargs):
            if command[0] != '/test/' + host:
                return real_run(command, **kwargs)
            self.cli_calls.append((command, kwargs))
            variable = 'CODEX_HOME' if host == 'codex' else 'CLAUDE_CONFIG_DIR'
            self.assertNotIn('SECRET_TEST_SENTINEL', kwargs['env'])
            self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
            directory = Path(kwargs['env'][variable])
            self.assertEqual(Path(kwargs['cwd']), directory)
            self.assertNotEqual(directory, self.config.parent)
            self.assertFalse((directory / self.config.name).exists(), 'original config must not be copied to CLI')
            if host == 'codex':
                position = command.index('--')
                environment = {command[i + 1].split('=', 1)[0]: command[i + 1].split('=', 1)[1]
                               for i, value in enumerate(command[:position]) if value == '--env'}
                raw = ('[mcp_servers.govel]\ncommand = ' + json.dumps(command[position + 1])
                       + '\nargs = ' + json.dumps(command[position + 2:]) + '\n')
                if environment:
                    raw += '[mcp_servers.govel.env]\n'
                    raw += ''.join(json.dumps(k) + ' = ' + json.dumps(v) + '\n' for k, v in environment.items())
                if extra:
                    raw += '[unsafe_extra]\nallow = true\n'
                (directory / 'config.toml').write_text(raw)
            else:
                entry = json.loads(command[-1])
                if extra:
                    entry['unexpectedPermission'] = True
                (directory / '.claude.json').write_text(json.dumps({'mcpServers': {'govel': entry},
                                                                  'cliGeneratedMetadata': 'not copied'}))
            if after:
                after()
            return subprocess.CompletedProcess(command, code, 'private CLI output not returned', '')
        self.stack.enter_context(patch.object(host_registration.subprocess, 'run', side_effect=run))

    def install(self, host='plugin'):
        return bootstrap.run('install', host=host, engine_root=str(ROOT), state_root=str(self.state))

    def test_copied_skill_checks_a_real_engine_from_an_unrelated_directory(self):
        copied = self.copy_skill()
        result = self.child(copied / 'scripts/bind.py', 'check', '--engine-root', str(ROOT),
                            '--state-root', str(self.state), '--json')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report['engineRoot'], str(ROOT))
        self.assertEqual(report['processHandshake']['verifiedCalls'], ['initialize', 'tools/list', 'get_tool_help'])
        self.assertEqual(report['hostConnection'], 'not_observed')
        self.assertFalse(report['authorApprovalGranted'])
        self.assertFalse(self.state.exists())

    def test_serve_has_only_mcp_stdout_and_does_not_create_binding_state(self):
        copied = self.copy_skill()
        messages = '\n'.join(json.dumps(row) for row in (
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'get_tool_help', 'arguments': {'operation': 'project.overview'}}},
        )) + '\n'
        result = self.child(copied / 'scripts/bind.py', 'serve', '--host', 'plugin', '--engine-root', str(ROOT),
                            '--state-root', str(self.state), input=messages)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row['id'] for row in replies], [1, 2, 3])
        self.assertEqual(replies[0]['result']['serverInfo']['name'], 'govel')
        self.assertFalse(self.state.exists())

    def test_plugin_install_is_idempotent_and_never_claims_current_host_connection(self):
        first = self.install()
        path = self.state / 'binding.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        second = self.install()
        self.assertTrue(first['bindingChanged'])
        self.assertFalse(second['bindingChanged'])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(second['registration']['status'], 'managed_by_host_plugin')
        self.assertEqual(second['hostConnection'], 'not_observed')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_native_plugin_data_is_stable_even_when_engine_has_git_metadata(self):
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': '', 'GOVEL_PROVIDER_CONFIG': ''}):
            environment, data = bootstrap.data_environment(self.engine, self.state, 'plugin')
            self.assertEqual(environment['GOVEL_DATA_HOME'], str(self.state / 'data'))
            self.assertEqual(data['dataHomeSource'], 'stable_user_directory')
            if (ROOT / '.git').exists():
                environment, data = bootstrap.data_environment(self.engine, self.state, 'codex')
                self.assertEqual(environment, {})
                self.assertEqual(data['dataHome'], str(ROOT / 'local'))
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': '', 'GOVEL_PROVIDER_CONFIG': '/chosen/provider.json'}):
            environment, data = bootstrap.data_environment(self.engine, self.state, 'plugin')
            self.assertEqual(environment, {'GOVEL_PROVIDER_CONFIG': '/chosen/provider.json'})
            self.assertIsNone(data['dataHome'])

    def test_missing_runtime_and_wrong_engine_fail_before_state_or_registration(self):
        for attribute, value, expected in (('version_info', (3, 10, 0), 'PYTHON_UNSUPPORTED'),):
            with patch.object(bootstrap.sys, attribute, value), self.assertRaises(bootstrap.BindError) as caught:
                self.install()
            self.assertEqual(caught.exception.code, expected)
        with patch.object(bootstrap.platform, 'system', return_value='Windows'), self.assertRaises(bootstrap.BindError) as caught:
            self.install()
        self.assertEqual(caught.exception.code, 'PLATFORM_UNSUPPORTED')
        with self.assertRaises(bootstrap.BindError) as caught:
            bootstrap.run('install', engine_root=str(self.base), state_root=str(self.state))
        self.assertEqual(caught.exception.code, 'ENGINE_INVALID')
        self.assertFalse((self.state / 'binding.json').exists())

    def test_old_python_stops_before_tomllib_import_in_a_fresh_process(self):
        code = '''import runpy,sys
sys.version_info = (3, 10, 0)
sys.modules['tomllib'] = None
path, state = sys.argv[1:]
sys.argv = [path, 'install', '--host', 'codex', '--state-root', state, '--json']
runpy.run_path(path, run_name='__main__')
'''
        result = subprocess.run([sys.executable, '-c', code, str(SKILL / 'scripts/bind.py'), str(self.state)],
                                cwd=self.base, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['code'], 'PYTHON_UNSUPPORTED')
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(self.state.exists())

    def test_standalone_check_never_downloads_or_creates_a_state_directory(self):
        copied = self.copy_skill()
        result = self.child(copied / 'scripts/bind.py', 'check', '--state-root', str(self.state), '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['code'], 'ENGINE_NOT_BOUND')
        self.assertFalse(self.state.exists())

    def test_server_failure_and_malformed_proofs_do_not_create_active_binding(self):
        for stdout, code in (('not json', 0), ('{}\n', 0), ('', 1)):
            with self.subTest(stdout=stdout, code=code), patch.object(bootstrap.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], code, stdout, 'private stderr')):
                with self.assertRaises(bootstrap.BindError) as caught:
                    self.install()
                self.assertEqual(caught.exception.code, 'MCP_HANDSHAKE_FAILED')
                self.assertFalse((self.state / 'binding.json').exists())

    def test_codex_cli_only_writes_an_isolated_candidate_and_other_settings_survive(self):
        self.configure_host('codex', 'model = "chosen"\n[mcp_servers.other]\ncommand = "other-server"\n'
                            '[mcp_servers.other.env]\nAPI_KEY = "private-other-value"\n')
        original = host_registration.decode_configuration('codex', self.config.read_bytes())
        original_raw = self.config.read_bytes()
        self.stack.enter_context(patch.dict(os.environ, {'SECRET_TEST_SENTINEL': 'do-not-pass-to-cli',
                                                        'OPENAI_API_KEY': 'not-a-real-key'}))
        self.fake_cli('codex')
        result = self.install('codex')
        updated = host_registration.decode_configuration('codex', self.config.read_bytes())
        self.assertEqual(updated['model'], original['model'])
        self.assertEqual(updated['mcp_servers']['other'], original['mcp_servers']['other'])
        self.assertEqual(updated['mcp_servers']['govel']['args'], [str(ROOT / 'scripts/mcp_server.py')])
        self.assertNotIn('private-other-value', json.dumps(result))
        backups = list(self.config.parent.glob('.config.toml.govel-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_raw)
        self.assertEqual(len(self.cli_calls), 1)
        binding = self.state / 'binding.json'
        before_binding = binding.read_bytes(), binding.stat().st_mtime_ns
        second = self.install('codex')
        self.assertFalse(second['registration']['changed'])
        self.assertFalse(second['bindingChanged'])
        self.assertEqual((binding.read_bytes(), binding.stat().st_mtime_ns), before_binding)
        self.assertEqual(len(self.cli_calls), 1)

    def test_claude_cli_metadata_is_not_copied_and_user_settings_are_preserved(self):
        initial = {'account': {'private': 'must-not-be-sent-to-cli'}, 'mcpServers': {'other': {'command': 'other'}}}
        self.configure_host('claude', json.dumps(initial))
        self.fake_cli('claude')
        result = self.install('claude')
        updated = json.loads(self.config.read_text())
        self.assertEqual(updated['account'], initial['account'])
        self.assertEqual(updated['mcpServers']['other'], initial['mcpServers']['other'])
        self.assertNotIn('cliGeneratedMetadata', updated)
        self.assertNotIn('must-not-be-sent-to-cli', json.dumps(result))
        self.assertTrue(result['registration']['changed'])

    def test_existing_host_environment_is_used_and_not_reported_or_rebuilt(self):
        chosen = str(self.base / 'chosen-data')
        entry = {'command': sys.executable, 'args': [str(ROOT / 'scripts/mcp_server.py')],
                 'env': {'PRIVATE_VALUE': 'never-report-this-value', 'GOVEL_DATA_HOME': chosen}}
        self.configure_host('claude', json.dumps({'mcpServers': {'govel': entry}, 'unrelated': True}))
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns
        self.fake_cli('claude')
        result = self.install('claude')
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        self.assertEqual(self.cli_calls, [])
        self.assertEqual(result['dataHome'], chosen)
        self.assertNotIn('never-report-this-value', json.dumps(result))
        self.assertNotIn('never-report-this-value', (self.state / 'binding.json').read_text())

    def test_existing_registration_does_not_receive_an_unwritten_data_default(self):
        self.configure_host('claude', json.dumps({'mcpServers': {'govel': {
            'command': sys.executable, 'args': [str(ROOT / 'scripts/mcp_server.py')], 'env': {}}}}))
        original_probe = bootstrap.probe
        environments = []
        def observed(engine, environment, policy, **kwargs):
            environments.append(dict(environment))
            return original_probe(engine, environment, policy, **kwargs)
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': '', 'GOVEL_PROVIDER_CONFIG': ''}), \
                patch.object(bootstrap, 'probe', side_effect=observed):
            result = self.install('claude')
        self.assertEqual(environments, [{}, {}])
        self.assertEqual(result['dataHome'], str(ROOT / 'local'))
        self.assertEqual(result['dataHomeSource'], 'engine_default')

    def test_existing_provider_only_entry_does_not_get_a_new_data_home(self):
        entry = {'command': sys.executable, 'args': [str(ROOT / 'scripts/mcp_server.py')],
                 'env': {'GOVEL_PROVIDER_CONFIG': str(self.base / 'provider.json')}}
        self.configure_host('claude', json.dumps({'mcpServers': {'govel': entry}}))
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': '', 'GOVEL_PROVIDER_CONFIG': ''}), \
                patch.object(bootstrap, 'probe', return_value={'ok': True}) as observed:
            result = self.install('claude')
        self.assertTrue(all(call.args[1] == {} for call in observed.call_args_list))
        self.assertIsNone(result['dataHome'])
        self.assertEqual(result['providerConfig'], entry['env']['GOVEL_PROVIDER_CONFIG'])
        self.assertEqual(json.loads(self.config.read_text())['mcpServers']['govel'], entry)

    def test_new_relative_data_paths_become_absolute_before_host_registration(self):
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': 'relative-data', 'GOVEL_PROVIDER_CONFIG': 'relative-provider.json'}):
            environment, data = bootstrap.data_environment(self.engine, self.state, 'codex')
        self.assertEqual(environment['GOVEL_DATA_HOME'], str(Path('relative-data').resolve()))
        self.assertEqual(environment['GOVEL_PROVIDER_CONFIG'], str(Path('relative-provider.json').resolve()))
        self.assertIsNone(data['dataHome'])

    def test_existing_conflicts_and_invalid_json_never_invoke_host_writer(self):
        for value in ({'mcpServers': {'govel': None}}, {'mcpServers': {'govel': {'command': '/bin/sh'}}}):
            with self.subTest(value=value):
                directory = self.base / 'conflict'
                directory.mkdir(exist_ok=True)
                path = directory / '.claude.json'; path.write_text(json.dumps(value))
                with patch.object(host_registration, 'configuration_path', return_value=path), \
                        patch.object(host_registration, '_generated_entry') as generated:
                    with self.assertRaises(bootstrap.BindError) as caught:
                        self.install('claude')
                    self.assertEqual(caught.exception.code, 'MCP_REGISTRATION_CONFLICT')
                    generated.assert_not_called()
                    self.assertEqual(json.loads(path.read_text()), value)
        with self.assertRaises(bootstrap.BindError):
            host_registration.decode_configuration('claude', b'{"mcpServers":{},"mcpServers":{}}')

    def test_claude_project_scope_conflict_prevents_a_shadowed_user_registration(self):
        self.configure_host('claude', '{"mcpServers":{}}')
        project = self.base / 'project'; nested = project / 'subdirectory'; nested.mkdir(parents=True)
        (project / '.git').mkdir()
        (project / '.mcp.json').write_text('{"mcpServers":{"govel":{"command":"/bin/sh"}}}')
        before = self.config.read_bytes()
        with patch.object(host_registration.Path, 'cwd', return_value=nested), \
                patch.object(host_registration, '_generated_entry') as writer:
            with self.assertRaises(bootstrap.BindError) as caught:
                self.install('claude')
        self.assertEqual(caught.exception.code, 'MCP_REGISTRATION_CONFLICT')
        self.assertEqual(caught.exception.details['scope'], 'project')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.state / 'binding.json').exists())
        writer.assert_not_called()

    def test_claude_local_project_user_precedence_is_reported(self):
        project = self.base / 'project'; nested = project / 'nested'; nested.mkdir(parents=True)
        (project / '.git').mkdir()
        entry = {'command': sys.executable, 'args': [str(ROOT / 'scripts/mcp_server.py')]}
        user = {**entry, 'env': {'SCOPE': 'user'}}
        local = {**entry, 'env': {'SCOPE': 'local'}}
        scoped = {**entry, 'env': {'SCOPE': 'project'}}
        self.configure_host('claude', json.dumps({'mcpServers': {'govel': user},
            'projects': {str(project): {'mcpServers': {'govel': local}}}}))
        project_config = project / '.mcp.json'
        project_config.write_text(json.dumps({'mcpServers': {'govel': scoped}}))
        with patch.object(host_registration.Path, 'cwd', return_value=nested):
            observed = host_registration.inspect_registration('claude', self.engine)
            self.assertEqual((observed['scope'], observed['entry']), ('local', local))
            self.config.write_text(json.dumps({'mcpServers': {'govel': user}}))
            observed = host_registration.inspect_registration('claude', self.engine)
            self.assertEqual((observed['scope'], observed['entry']), ('project', scoped))
            self.assertEqual(observed['configPath'], str(project_config))
            project_config.unlink()
            observed = host_registration.inspect_registration('claude', self.engine)
            self.assertEqual((observed['scope'], observed['entry']), ('user', user))

    def test_concurrent_config_edit_is_preserved_and_not_activated(self):
        self.configure_host('codex', 'model = "before"\n')
        late = b'model = "edited while staging"\n'
        self.fake_cli('codex', after=lambda: self.config.write_bytes(late))
        with self.assertRaises(bootstrap.BindError) as caught:
            self.install('codex')
        self.assertEqual(caught.exception.code, 'MCP_CONFIG_CHANGED')
        self.assertEqual(self.config.read_bytes(), late)
        self.assertFalse((self.state / 'binding.json').exists())

    def test_cli_failure_or_extra_candidate_permissions_do_not_change_config(self):
        self.configure_host('codex', 'model = "untouched"\n')
        original = self.config.read_bytes()
        self.fake_cli('codex', extra=True)
        with self.assertRaises(bootstrap.BindError) as caught:
            self.install('codex')
        self.assertEqual(caught.exception.code, 'MCP_CANDIDATE_INVALID')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.state / 'binding.json').exists())

    def test_antigravity_registration_uses_stable_data_without_clobbering_other_entries(self):
        self.configure_host('antigravity', '{"mcpServers":{"other":{"command":"other"}}}')
        result = self.install('antigravity')
        value = json.loads(self.config.read_text())
        self.assertEqual(value['mcpServers']['other'], {'command': 'other'})
        self.assertEqual(value['mcpServers']['govel']['env']['GOVEL_DATA_HOME'], str(self.base / 'data'))
        self.assertTrue(result['registration']['changed'])

    def test_antigravity_environment_conflict_and_locked_absence_check(self):
        publication = host_registration.publication_module(self.engine)
        self.configure_host('antigravity', json.dumps({'mcpServers': {'govel': {
            'command': sys.executable, 'args': [str(ROOT / 'scripts/mcp_server.py')],
            'env': {'GOVEL_DATA_HOME': 'already-chosen', 'CUSTOM': 'keep'}}}}))
        before = self.config.read_bytes()
        with self.assertRaises(ValueError) as caught:
            publication.install_registration(self.config, ROOT / 'scripts/mcp_server.py',
                                             environment={'GOVEL_DATA_HOME': 'other'})
        self.assertEqual(caught.exception.code, 'MCP_ENV_CONFLICT')
        with self.assertRaises(ValueError) as caught:
            publication.install_registration(self.config, ROOT / 'scripts/mcp_server.py', only_if_missing=True)
        self.assertEqual(caught.exception.code, 'MCP_REGISTRATION_CONFLICT')
        self.assertEqual(self.config.read_bytes(), before)

    def test_two_host_files_in_one_directory_do_not_consume_each_others_pending_record(self):
        publication = host_registration.publication_module(self.engine)
        directory = self.base / 'shared-config'; directory.mkdir()
        first, second = directory / 'config.toml', directory / '.claude.json'
        first.write_bytes(b'model="original"\n'); second.write_bytes(b'{"untouched":true}')
        real_rename = publication.os.rename
        class SimulatedKill(BaseException):
            pass
        def stop_after_move(source, destination):
            real_rename(source, destination)
            raise SimulatedKill()
        with patch.object(publication.os, 'rename', side_effect=stop_after_move), self.assertRaises(SimulatedKill):
            publication.publish_host_configuration(first, lambda snapshot: b'model="new"\n')
        pending = publication.pending_registration(first)
        paths = [Path(pending[key]) for key in ('markerPath', 'backupPath', 'stagedPath')]
        frozen = {path: path.read_bytes() for path in paths}
        publication.publish_host_configuration(second, lambda snapshot: b'{"updated":true}')
        self.assertEqual({path: path.read_bytes() for path in paths}, frozen)
        self.assertIsNone(publication.pending_registration(second))
        publication.publish_host_configuration(first, lambda snapshot: snapshot['raw'])
        self.assertEqual(first.read_bytes(), b'model="original"\n')
        self.assertEqual(second.read_bytes(), b'{"updated":true}')
        self.assertIsNone(publication.pending_registration(first))

    def test_legacy_antigravity_pending_record_is_preserved_for_its_original_target(self):
        publication = host_registration.publication_module(self.engine)
        directory = self.base / 'legacy'; directory.mkdir()
        first, second = directory / 'mcp_config.json', directory / '.claude.json'
        first.write_text('{"preserved":true,"mcpServers":{}}'); second.write_text('{}')
        real_rename = publication.os.rename
        class SimulatedKill(BaseException):
            pass
        def stop_after_move(source, destination):
            real_rename(source, destination)
            raise SimulatedKill()
        with patch.object(publication.os, 'rename', side_effect=stop_after_move), self.assertRaises(SimulatedKill):
            publication.install_registration(first, ROOT / 'scripts/mcp_server.py')
        pending = publication.pending_registration(first)
        marker = Path(pending['markerPath'])
        value = json.loads(marker.read_text()); value['schemaVersion'] = 1; del value['configurationName']
        legacy = directory / '.govel-mcp-install-pending.json'
        legacy.write_text(json.dumps(value)); marker.unlink()
        saved = legacy.read_bytes(), Path(pending['backupPath']).read_bytes(), Path(pending['stagedPath']).read_bytes()
        with self.assertRaises(ValueError) as caught:
            publication.publish_host_configuration(second, lambda snapshot: b'{"new":true}')
        self.assertEqual(caught.exception.code, 'MCP_RECOVERY_TARGET_UNBOUND')
        self.assertEqual((legacy.read_bytes(), Path(pending['backupPath']).read_bytes(),
                          Path(pending['stagedPath']).read_bytes()), saved)
        publication.install_registration(first, ROOT / 'scripts/mcp_server.py')
        self.assertTrue(json.loads(first.read_text())['preserved'])
        self.assertFalse(legacy.exists())

    def test_archive_path_symlink_private_and_size_errors_leave_no_engine(self):
        commit = 'a' * 40
        cases = [('../escape', b'bad', None), ('local/roundtable.md', b'private', None),
                 ('link', b'outside', stat.S_IFLNK | 0o777)]
        for number, (name, payload, mode) in enumerate(cases):
            with self.subTest(name=name):
                raw = io.BytesIO()
                with zipfile.ZipFile(raw, 'w') as archive:
                    item = zipfile.ZipInfo('govel-' + commit + '/' + name)
                    if mode:
                        item.external_attr = mode << 16
                    archive.writestr(item, payload)
                target = self.base / ('extract-' + str(number)); target.mkdir()
                with self.assertRaises(bootstrap.BindError):
                    engine_install.extract_engine(raw.getvalue(), target, commit, self.policy)
                self.assertEqual(list(target.iterdir()), [])
        with self.assertRaises(bootstrap.BindError) as caught:
            engine_install.extract_engine(raw.getvalue(), self.base, commit, {**self.policy, 'archiveMaxBytes': 1})
        self.assertEqual(caught.exception.code, 'ENGINE_ARCHIVE_LIMIT')

    def test_corrupt_active_binding_is_not_silently_replaced(self):
        self.state.mkdir()
        marker = self.state / 'binding.json'; marker.write_text('broken')
        with patch.object(bootstrap, 'install_registration') as writer:
            with self.assertRaises(bootstrap.BindError) as caught:
                self.install()
        self.assertEqual(caught.exception.code, 'BIND_STATE_INVALID')
        self.assertEqual(marker.read_text(), 'broken')
        writer.assert_not_called()

    def minimal_archive(self, commit):
        body = '''import json,sys
from pathlib import Path
version=json.loads((Path(__file__).resolve().parents[1]/'toolbox.manifest.json').read_text())['version']
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request: continue
 if request['id']==1: result={'serverInfo':{'name':'govel','version':version}}
 elif request['id']==2: result={'tools':[{'name':'get_tool_help'}]}
 else: result={'structuredContent':{'ok':True},'content':[]}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
'''
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            prefix = 'govel-' + commit + '/'
            archive.writestr(prefix + 'toolbox.manifest.json', json.dumps({
                'name': 'govel', 'version': self.engine['version'], 'schemaVersion': self.policy['projectSchemaVersion']}))
            for name in ('SKILL.md', 'scripts/novelctl.py', 'scripts/core/project.py',
                         'shared/registries/tools.json', 'shared/registries/workflow.json'):
                archive.writestr(prefix + name, '')
            archive.writestr(prefix + 'shared/contracts/', '')
            archive.writestr(prefix + 'scripts/mcp_server.py', body)
            archive.writestr(prefix + 'scripts/core/antigravity_registration.py',
                             'def publish_host_configuration(path, prepare): pass\n')
        return raw.getvalue()

    def test_independent_install_downloads_a_pinned_engine_then_reuses_binding_without_network(self):
        copied = self.copy_skill()
        commit = 'a' * 40
        archive = self.minimal_archive(commit)
        urls = []
        def download(url, policy):
            urls.append(url)
            return json.dumps({'sha': commit}).encode() if 'api.github.com' in url else archive
        synced = set()
        real_sync, real_publish = os.fsync, engine_install.publish_directory
        def sync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                synced.add((info.st_dev, info.st_ino))
            return real_sync(fd)
        def publish(source, destination):
            for path in [source, *[path for path in source.rglob('*') if path.is_dir()]]:
                info = path.stat()
                self.assertIn((info.st_dev, info.st_ino), synced, 'all child directories must be synced before publication')
            return real_publish(source, destination)
        with patch.object(bootstrap, 'SCRIPT', copied / 'scripts/bind.py'), \
                patch.object(engine_install, 'read_download', side_effect=download), \
                patch.object(engine_install.os, 'fsync', side_effect=sync), \
                patch.object(engine_install, 'publish_directory', side_effect=publish):
            first = bootstrap.run('install', state_root=str(self.state))
        self.assertTrue(first['ok'])
        self.assertEqual(first['commit'], commit)
        self.assertEqual(first['engineRoot'], str(self.state / 'engines' / commit))
        self.assertEqual(urls, ['https://api.github.com/repos/wuren194/govel/commits/main',
                               'https://codeload.github.com/wuren194/govel/zip/' + commit])
        with patch.object(bootstrap, 'SCRIPT', copied / 'scripts/bind.py'), \
                patch.object(engine_install, 'read_download') as network:
            second = bootstrap.run('install', state_root=str(self.state))
            network.assert_not_called()
        self.assertFalse(second['bindingChanged'])
        self.assertEqual(second['engineRoot'], first['engineRoot'])

    def test_download_or_extraction_failure_does_not_create_an_active_binding(self):
        copied = self.copy_skill()
        with patch.object(bootstrap, 'SCRIPT', copied / 'scripts/bind.py'), \
                patch.object(engine_install, 'read_download', side_effect=bootstrap.BindError('ENGINE_DOWNLOAD_FAILED', 'offline')):
            with self.assertRaises(bootstrap.BindError):
                bootstrap.run('install', state_root=str(self.state))
        self.assertFalse((self.state / 'binding.json').exists())
        responses = [json.dumps({'sha': 'b' * 40}).encode(), b'not a zip archive']
        with patch.object(bootstrap, 'SCRIPT', copied / 'scripts/bind.py'), \
                patch.object(engine_install, 'read_download', side_effect=responses):
            with self.assertRaises(bootstrap.BindError) as caught:
                bootstrap.run('install', state_root=str(self.state))
        self.assertEqual(caught.exception.code, 'ENGINE_ARCHIVE_INVALID')
        self.assertFalse((self.state / 'binding.json').exists())
        self.assertEqual(list((self.state / 'engines').iterdir()), [])

    def test_broken_engine_syntax_has_a_structured_error_without_source_text(self):
        commit = 'c' * 40
        engine = self.base / 'broken-engine'; engine.mkdir()
        engine_install.extract_engine(self.minimal_archive(commit), engine, commit, self.policy)
        (engine / 'scripts/core/antigravity_registration.py').write_text('def PRIVATE_SOURCE_SENTINEL(\n')
        copied = self.copy_skill()
        result = self.child(copied / 'scripts/bind.py', 'check', '--engine-root', str(engine),
                            '--state-root', str(self.state), '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['code'], 'ENGINE_INVALID')
        self.assertNotIn('PRIVATE_SOURCE_SENTINEL', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
