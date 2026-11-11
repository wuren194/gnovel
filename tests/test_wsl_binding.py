"""Windows MCP configuration, with a simulated bridge and real Govel stdio."""
from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('govel_wsl_bootstrap_test',
                                            ROOT / 'skills/govel-bind/scripts/bind.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class WslBindingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='govel-wsl-test-'))
        self.base = Path(temporary).resolve()
        self.state = self.base / 'unused-state'
        self.stack.enter_context(patch.dict(os.environ, {
            'WSL_DISTRO_NAME': 'Ubuntu-24.04', 'GOVEL_DATA_HOME': str(self.base / '资料 $(literal) & 空格'),
            'GOVEL_PROVIDER_CONFIG': '', 'GOVEL_LOG_MODE': 'off', 'PYTHONDONTWRITEBYTECODE': '1'}))
        self.stack.enter_context(patch.object(bootstrap.platform, 'system', return_value='Linux'))

    def generate(self):
        return bootstrap.run('wsl-config', engine_root=str(ROOT), state_root=str(self.state))

    def test_config_pins_distribution_user_paths_and_provider_without_writing_registration(self):
        import pwd
        with patch.object(bootstrap.shutil, 'which', return_value='/mounted/System32/wsl.exe'), \
                patch.object(bootstrap, 'probe', return_value={'ok': True}) as probe, \
                patch.object(bootstrap, 'inspect_registration') as inspect, \
                patch.object(bootstrap, 'install_registration') as install:
            report = self.generate()
        entry = report['mcpConfig']['mcpServers']['govel']
        self.assertEqual(entry['command'], 'wsl.exe')
        self.assertEqual(entry['args'][:9], ['--distribution', 'Ubuntu-24.04', '--user',
            pwd.getpwuid(os.getuid()).pw_name, '--cd', str(ROOT), '--exec', '/usr/bin/env',
            'GOVEL_DATA_HOME=' + os.environ['GOVEL_DATA_HOME']])
        self.assertIn('GOVEL_PROVIDER_CONFIG=', entry['args'])
        self.assertEqual(entry['args'][-3:], [sys.executable, '-u', str(ROOT / 'scripts/mcp_server.py')])
        checked = probe.call_args.kwargs['entry']
        self.assertEqual(checked['command'], '/mounted/System32/wsl.exe')
        self.assertIn('GOVEL_LOG_MODE=off', checked['args'])
        self.assertNotIn('GOVEL_LOG_MODE=off', entry['args'])
        self.assertEqual(report['cliCommand'][-2:], [sys.executable, str(ROOT / 'scripts/novelctl.py')])
        self.assertEqual(report['hostConnection'], 'not_observed')
        self.assertEqual(report['registration']['status'], 'manual_configuration_required')
        self.assertFalse(report['authorApprovalGranted'])
        self.assertFalse(self.state.exists())
        inspect.assert_not_called()
        install.assert_not_called()

    def test_no_overrides_pin_the_same_data_home_across_windows_launches(self):
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': '', 'GOVEL_PROVIDER_CONFIG': ''}), \
                patch.object(bootstrap.shutil, 'which', return_value='/bridge/wsl.exe'), \
                patch.object(bootstrap, 'probe', return_value={'ok': True}):
            report = self.generate()
        self.assertEqual(report['dataHome'], str(ROOT / 'local'))
        self.assertIn('GOVEL_DATA_HOME=' + str(ROOT / 'local'), report['mcpConfig']['mcpServers']['govel']['args'])

    def test_explicit_provider_remains_bound_as_a_linux_argument(self):
        provider = str(self.base / '私人 资料' / 'provider.json')
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': '', 'GOVEL_PROVIDER_CONFIG': provider}), \
                patch.object(bootstrap.shutil, 'which', return_value='/bridge/wsl.exe'), \
                patch.object(bootstrap, 'probe', return_value={'ok': True}):
            report = self.generate()
        self.assertIsNone(report['dataHome'])
        arguments = report['mcpConfig']['mcpServers']['govel']['args']
        self.assertIn('GOVEL_PROVIDER_CONFIG=' + provider, arguments)
        self.assertIn('GOVEL_DATA_HOME=', arguments)

    def test_missing_wsl_or_bridge_and_failed_handshake_do_not_claim_readiness(self):
        cases = (
            ({'WSL_DISTRO_NAME': ''}, '/bridge/wsl.exe', 'WSL_REQUIRED'),
            ({}, None, 'WSL_BRIDGE_UNAVAILABLE'),
            ({}, '/bridge/wsl.exe', 'WSL_MCP_HANDSHAKE_FAILED'),
        )
        for environment, bridge, code in cases:
            with self.subTest(code=code), patch.dict(os.environ, environment), \
                    patch.object(bootstrap.shutil, 'which', return_value=bridge), \
                    patch.object(bootstrap, 'probe', side_effect=bootstrap.BindError('MCP_HANDSHAKE_FAILED', 'failed')):
                with self.assertRaises(bootstrap.BindError) as caught:
                    self.generate()
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(self.state.exists())

    def test_simulated_bridge_transports_real_mcp_and_preserves_literal_arguments(self):
        # This executable models wsl.exe argument forwarding, not the WSL kernel.
        bridge = self.base / 'wsl.exe'
        bridge.write_text(f'#!{sys.executable}\n' + '''import os,sys
args = sys.argv[1:]
assert args[0] == '--distribution' and args[2] == '--user'
assert args[4] == '--cd' and args[6:8] == ['--exec', '/usr/bin/env']
os.chdir(args[5])
os.execv(args[7], args[7:])
''', encoding='utf-8')
        bridge.chmod(0o700)
        with patch.object(bootstrap.shutil, 'which', return_value=str(bridge)):
            report = self.generate()
        self.assertEqual(report['processHandshake']['verifiedCalls'], ['initialize', 'tools/list', 'get_tool_help'])
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(os.environ['GOVEL_DATA_HOME']).exists())
        entry = report['mcpConfig']['mcpServers']['govel']
        request = json.dumps({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                              'params': {'name': 'get_tool_help', 'arguments': {'operation': 'project.overview'}}})
        result = subprocess.run([str(bridge), *entry['args']], input=request + '\n',
                                text=True, capture_output=True, timeout=30, cwd=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response['id'], 4)
        self.assertTrue(response['result']['structuredContent']['ok'])


if __name__ == '__main__':
    unittest.main()
