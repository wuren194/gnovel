"""Portable plugin entrypoints and generated installation metadata stay bound."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from scripts import generate_wrappers as wrappers
from scripts.runtime_requirements import MIN_PYTHON


ROOT = Path(__file__).resolve().parents[1]


class PluginShellTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='govel-plugin-shells-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / '插件 带空格 $root'
        self.root.mkdir()
        self.manifest = json.loads((ROOT / 'toolbox.manifest.json').read_text())
        for relative in ['toolbox.manifest.json', 'SKILL.md', 'shared/registries/integration.json',
                         *[name + '/SKILL.md' for name in self.manifest['skills']]]:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        root_patch = patch.object(wrappers, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def documents(self):
        return {str(path.relative_to(self.root)): json.loads(text)
                for path, text in wrappers.expected_plugin_files().items() if path.suffix == '.json'}

    def test_plugin_identity_and_version_follow_the_engine_manifest(self):
        self.manifest['version'] = '9.8.7'
        (self.root / 'toolbox.manifest.json').write_text(json.dumps(self.manifest))
        documents = self.documents()
        for relative in ('plugin.json', '.claude-plugin/plugin.json', '.antigravity-plugin/plugin.json'):
            with self.subTest(manifest=relative):
                self.assertEqual(documents[relative]['name'], self.manifest['name'])
                self.assertEqual(documents[relative]['version'], '9.8.7')
        self.assertEqual(documents['.claude-plugin/marketplace.json']['version'], '9.8.7')

    def test_standalone_policy_uses_registry_and_current_runtime_requirements(self):
        policy_path = self.root / 'shared/registries/integration.json'
        policy = json.loads(policy_path.read_text())
        policy['handshakeTimeoutSeconds'] += 1
        policy_path.write_text(json.dumps(policy))
        self.manifest['schemaVersion'] += 1
        (self.root / 'toolbox.manifest.json').write_text(json.dumps(self.manifest))
        binding = self.documents()['skills/govel-bind/binding.json']
        self.assertEqual(binding, {**policy, 'pythonMinimum': list(MIN_PYTHON),
                                   'projectSchemaVersion': self.manifest['schemaVersion']})

    def test_all_host_mcp_declarations_resolve_the_same_script_and_engine(self):
        documents = self.documents()
        configs = (('mcp.json', '${PLUGIN_ROOT}'),
                   ('.claude-plugin/plugin.json', '${CLAUDE_PLUGIN_ROOT}'),
                   ('mcp_config.json', '${PLUGIN_ROOT}'))
        expected = self.root / 'skills/govel-bind/scripts/bind.py'
        for relative, variable in configs:
            with self.subTest(host=relative):
                server = documents[relative]['mcpServers']['govel']
                args = [part.replace(variable, str(self.root)) for part in server['args']]
                self.assertEqual(server['command'], 'python3')
                self.assertEqual(Path(args[0]), expected)
                self.assertEqual(args[1:], ['serve', '--host', 'plugin', '--engine-root', str(self.root)])
                self.assertNotIn('shell', server)
                self.assertNotIn('env', server, 'the launcher owns data-root selection and preserves user overrides')
        self.assertEqual(documents['mcp.json']['mcpServers']['govel']['type'], 'stdio')

    def test_marketplaces_name_the_same_contained_complete_plugin(self):
        documents = self.documents()
        for relative in ('.agents/plugins/marketplace.json', '.claude-plugin/marketplace.json'):
            marketplace = documents[relative]
            self.assertEqual(marketplace['name'], self.manifest['name'])
            entry, = marketplace['plugins']
            self.assertEqual(entry['name'], self.manifest['name'])
            source = entry['source']
            path = source['path'] if isinstance(source, dict) else source
            self.assertTrue(path.startswith('./'))
            self.assertNotIn('..', Path(path).parts)
            self.assertEqual((self.root / path).resolve(), self.root)
            self.assertEqual(documents['plugin.json']['version'], self.manifest['version'])

    def test_onboarding_and_claude_alias_resolve_only_to_packaged_binding_skill(self):
        files = wrappers.expected_plugin_files()
        documents = self.documents()
        onboarding = documents['plugin.json']['extensions']['com.openai']['onboardingSkill']
        self.assertEqual((self.root / onboarding).resolve(), self.root / 'skills/govel-bind/SKILL.md')
        self.assertTrue(documents['plugin.json']['extensions']['com.openai']['requires_local_executor'])
        alias = files[self.root / 'commands/init.md']
        reference, = re.findall(r'\]\(([^)]+)\)', alias)
        self.assertEqual((self.root / 'commands' / reference).resolve(), self.root / 'skills/govel-bind/SKILL.md')
        self.assertNotIn(self.root / 'skills/init/SKILL.md', files)

    def test_workspace_wrapper_api_remains_about_workstation_delegation(self):
        for path, body in wrappers.expected_files().items():
            self.assertNotEqual(path.suffix, '.json')
            self.assertIn('MCP／CLI 选择按根 Skill 执行', body)
        self.assertTrue(wrappers.expected_plugin_files())

    def test_generation_and_check_cover_plugin_declarations_without_changing_sources(self):
        sources = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertTrue(wrappers.write()['ok'])
        self.assertTrue(wrappers.check()['ok'])
        self.assertEqual(wrappers.write()['written'], [])
        for path, raw in sources.items():
            self.assertEqual(path.read_bytes(), raw)
        metadata = self.root / '.claude-plugin/plugin.json'
        modified = json.loads(metadata.read_text())
        modified['mcpServers']['govel']['args'] = ['wrong-server.py']
        metadata.write_text(json.dumps(modified))
        self.assertEqual(wrappers.check()['drift'], ['.claude-plugin/plugin.json'])

    def test_invalid_numerical_policy_cannot_generate_a_binding(self):
        path = self.root / 'shared/registries/integration.json'
        original = json.loads(path.read_text())
        for value in (0, -1, True, '30'):
            with self.subTest(value=value):
                policy = deepcopy(original)
                policy['handshakeTimeoutSeconds'] = value
                path.write_text(json.dumps(policy))
                with self.assertRaises(ValueError):
                    wrappers.expected_plugin_files()


if __name__ == '__main__':
    unittest.main()
