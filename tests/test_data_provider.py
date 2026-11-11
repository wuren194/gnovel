"""Local data boundaries, real material reading and dependency invalidation."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core import data_provider as provider
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from tests.fixtures_current import create_schema5_project


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'data'
        self.root.mkdir()
        self.config = self.base / 'config.json'
        self.config.write_text(json.dumps({'schemaVersion': 1, 'root': str(self.root)}))
        self.manifest = {'schemaVersion': 1, 'providerId': 'test-data', 'version': '1', 'registries': {}, 'materials': []}
        self.save()

    def save(self):
        (self.root / 'provider.json').write_text(json.dumps(self.manifest))

    def environment(self):
        return patch.dict(os.environ, {'GOVEL_PROVIDER_CONFIG': str(self.config)})

    def material(self, *, when=None):
        path = self.root / 'rules/note.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text('只核对当前人物知道的信息。\n不把引用回执当作语义通过。\n')
        self.manifest['materials'] = [{'id': 'RULE-EXAMPLE', 'role': 'rule', 'stations': ['writer'], 'when': when,
            'resource': {'path': 'rules/note.md', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}]
        self.save()
        return path

    def asset(self):
        sample = json.loads((provider.ROOT / 'knowledge/example.json').read_text())
        path = self.root / 'knowledge/assets/example.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\n' + json.dumps(sample['metadata'], ensure_ascii=False) + '\n---\n' + sample['body'])
        return path

    def fresh_python(self, code, **extra):
        environment = {**os.environ, 'GOVEL_PROVIDER_CONFIG': str(self.config),
                       'PROBE_ROOT': str(self.root), 'PROBE_BASE': str(self.base),
                       'PYTHONDONTWRITEBYTECODE': '1', 'GOVEL_LOG_MODE': 'off', **extra}
        result = subprocess.run([sys.executable, '-c', code], cwd=provider.ROOT,
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_absent_provider_has_no_implicit_dataset(self):
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.base / 'empty')}, clear=True):
            self.assertIsNone(provider.active_provider())
            self.assertEqual(provider.materials('writer'), [])

    def test_explicit_missing_provider_does_not_fall_back(self):
        self.config.unlink()
        with self.environment(), self.assertRaises(provider.ProviderError) as caught:
            provider.active_provider()
        self.assertEqual(caught.exception.code, 'PROVIDER_UNREADABLE')

    def test_bad_configuration_keeps_discovery_available_and_operations_fail_closed(self):
        self.config.unlink()
        environment = {**os.environ, 'GOVEL_PROVIDER_CONFIG': str(self.config),
                       'PYTHONDONTWRITEBYTECODE': '1', 'GOVEL_LOG_MODE': 'off'}
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05',
             'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'build_knowledge_pack',
             'arguments': {'planningIntent': '伏笔', 'station': 'planner'}}},
        ]
        result = subprocess.run([sys.executable, 'scripts/mcp_server.py'],
                                input=''.join(json.dumps(request) + '\n' for request in requests),
                                cwd=provider.ROOT, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn('serverInfo', responses[0]['result'])
        self.assertTrue(responses[1]['result']['tools'])
        self.assertTrue(responses[2]['result']['isError'])
        self.assertIn('PROVIDER_UNREADABLE', json.dumps(responses[2]))
        result = subprocess.run([sys.executable, 'scripts/novelctl.py', 'project', 'knowledge', 'pack',
                                 '--planning-intent', '伏笔', '--station', 'planner'],
                                cwd=provider.ROOT, env=environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['code'], 'PROVIDER_UNREADABLE')
        self.assertNotIn('Traceback', result.stderr)

    def test_configuration_cannot_live_in_public_source_paths(self):
        with patch.dict(os.environ, {'GOVEL_PROVIDER_CONFIG': str(provider.ROOT / 'README.md')}), \
             self.assertRaises(provider.ProviderError) as caught:
            provider.active_provider()
        self.assertEqual(caught.exception.code, 'PROVIDER_CONFIG_INVALID')

    def test_checkout_local_provider_reads_relative_to_config_and_survives_relocation(self):
        self.material()
        checkout = self.base / 'checkout'
        local = checkout / 'local'
        local.mkdir(parents=True)
        self.root.rename(local / 'provider')
        self.config = local / 'provider-config.json'
        self.config.write_text(json.dumps({'schemaVersion': 1, 'root': 'provider'}))
        with patch.object(provider, 'ROOT', checkout), self.environment():
            before = provider.binding()
            self.assertEqual(provider.storage_root(), local / 'provider')
            self.assertEqual(provider.materials('writer')[0]['id'], 'RULE-EXAMPLE')
        moved = self.base / 'moved-checkout'
        checkout.rename(moved)
        self.config = moved / 'local/provider-config.json'
        with patch.object(provider, 'ROOT', moved), self.environment():
            self.assertEqual(provider.binding(), before)
            self.assertEqual(provider.storage_root(), moved / 'local/provider')
            note = moved / 'local/provider/rules/note.md'
            note.write_text('Changed material')
            with self.assertRaises(provider.ProviderError) as caught:
                provider.materials('writer')
            self.assertEqual(caught.exception.code, 'PROVIDER_RESOURCE_CHANGED')

    def test_local_config_cannot_point_to_public_source_or_escape_relatively(self):
        checkout = self.base / 'checkout'
        local = checkout / 'local'
        local.mkdir(parents=True)
        self.root.rename(checkout / 'public-data')
        self.config = local / 'provider-config.json'
        with patch.object(provider, 'ROOT', checkout), self.environment():
            for root, code in [(str(checkout / 'public-data'), 'PROVIDER_ROOT_INVALID'),
                               ('../public-data', 'PROVIDER_PATH_UNSAFE')]:
                self.config.write_text(json.dumps({'schemaVersion': 1, 'root': root}))
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.active_provider()
                self.assertEqual(caught.exception.code, code)
            (local / 'alias').symlink_to(checkout / 'public-data', target_is_directory=True)
            self.config.write_text(json.dumps({'schemaVersion': 1, 'root': 'alias'}))
            with self.assertRaises(provider.ProviderError) as caught:
                provider.active_provider()
            self.assertEqual(caught.exception.code, 'PROVIDER_PATH_UNSAFE')

    def test_create_accepts_local_directory_but_rejects_public_source_directory(self):
        from scripts import provider as provider_cli
        checkout = self.base / 'checkout'
        (checkout / 'knowledge').mkdir(parents=True)
        (checkout / 'knowledge/example.json').write_bytes((provider.ROOT / 'knowledge/example.json').read_bytes())
        with patch.object(provider, 'ROOT', checkout):
            for target, expected in [(checkout / 'public-data', 1), (checkout / 'local/example', 0)]:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(provider_cli.main(['create', '--root', str(target)]), expected)
            self.config = checkout / 'local/example/config.json'
            self.assertEqual(json.loads(self.config.read_text())['root'], '.')
            with self.environment():
                self.assertEqual(provider.binding()['providerId'], 'local-example')
            self.assertFalse((checkout / 'public-data').exists())

    def test_resource_bytes_are_checked(self):
        path = self.material()
        with self.environment():
            self.assertEqual(len(provider.materials('writer')), 1)
            path.write_text('资源已变化。')
            with self.assertRaises(provider.ProviderError) as caught:
                provider.active_provider()
            self.assertEqual(caught.exception.code, 'PROVIDER_RESOURCE_CHANGED')

    def test_resources_cannot_escape_or_follow_links(self):
        path = self.material()
        outside = self.base / 'outside.md'
        outside.write_bytes(path.read_bytes())
        with self.environment():
            for value in ('../outside.md', str(outside), 'rules/../note.md'):
                with self.assertRaises(provider.ProviderError):
                    provider.confined_path(self.root, value)
            path.unlink()
            path.symlink_to(outside)
            with self.assertRaises(provider.ProviderError):
                provider.active_provider()

    def test_provider_cannot_replace_transaction_or_approval_policies(self):
        self.material()
        self.manifest['registries']['workflow'] = self.manifest['materials'][0]['resource']
        self.save()
        with self.environment(), self.assertRaises(provider.ProviderError):
            provider.active_provider()

    def test_condition_unknown_is_visible_and_station_scope_is_respected(self):
        self.material(when={'field': 'chapter.title', 'op': 'eq', 'value': '初见'})
        with self.environment():
            self.assertEqual(provider.materials('editor', {}), [])
            self.assertFalse(provider.materials('writer', {})[0]['active'])
            self.assertEqual(provider.materials('writer', {'chapter': {'title': '别章'}}), [])
            self.assertTrue(provider.materials('writer', {'chapter': {'title': '初见'}})[0]['active'])

    def test_positive_condition_with_unknown_sibling_does_not_complete_the_plan(self):
        self.material(when={'field': 'checks[].ready', 'op': 'eq', 'value': True})
        from scripts.core.prompt_plan import resolve_prompt_plan
        with self.environment():
            plan = resolve_prompt_plan(self.base, 'govel-writer', design={'checks': [{'ready': True}, {}]})
        self.assertTrue(plan['providerMaterials'][0]['active'])
        self.assertFalse(plan['complete'])
        self.assertIn('PROVIDER_CONDITION_UNRESOLVED', [row['code'] for row in plan['warnings']])

    def test_real_prepare_and_read_deliver_only_declared_material(self):
        note = self.material()
        project = self.base / 'project'
        project.mkdir()
        fixture = create_schema5_project(project, chapter_count=1)
        with self.environment():
            report = call_operation('task.prepare', project, {'task': 'writer', 'chapterId': fixture['chapterIds'][0], 'view': 'model'})
            selected = report['providerReads']
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]['role'], 'rule')
            result = call_operation('task.read-batch', project, {'credential': report['reading']['credential'],
                                    'readIds': [selected[0]['readId']]})
            self.assertEqual(''.join(row['text'] for row in result['items']), note.read_text())
            self.assertFalse(result['coverage']['semanticReviewComplete'])
            self.assertEqual(result['items'][0]['role'], 'rule')
            note.write_text('新增一项待确认的说明。\n')
            self.manifest['materials'][0]['resource']['sha256'] = hashlib.sha256(note.read_bytes()).hexdigest()
            self.save()
            with self.assertRaises((StructureError, provider.ProviderError)):
                call_operation('task.read-batch', project, {'credential': report['reading']['credential'],
                               'readIds': [selected[0]['readId']]})

    def test_project_cannot_silently_lose_its_declared_provider(self):
        project = self.base / 'bound-project'
        project.mkdir()
        with self.environment():
            create_schema5_project(project, chapter_count=1)
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.base / 'empty')}, clear=True):
            from scripts.core.structure_store import load_structure
            with self.assertRaises(provider.ProviderError) as caught:
                load_structure(project)
            self.assertEqual(caught.exception.code, 'PROVIDER_REQUIRED')

    def test_example_can_be_created_and_retrieved_in_fresh_process(self):
        target = self.base / 'example'
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        environment.pop('GOVEL_PROVIDER_CONFIG', None)
        result = subprocess.run([sys.executable, 'scripts/provider.py', 'create', '--root', str(target)],
                                cwd=provider.ROOT, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        environment['GOVEL_PROVIDER_CONFIG'] = str(target / 'config.json')
        result = subprocess.run([sys.executable, 'scripts/novelctl.py', 'project', 'knowledge', 'pack',
                                 '--planning-intent', '伏笔', '--station', 'planner'],
                                cwd=provider.ROOT, env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['items'][0]['assetId'], 'KN-EXAMPLE')
        self.assertEqual(report['items'][0]['role'], 'REFERENCE KNOWLEDGE')

    def test_knowledge_parent_link_cannot_read_or_write_outside_the_package(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'knowledge').symlink_to(outside, target_is_directory=True)
        report = self.fresh_python('''
import json
from scripts.core import knowledge_store as store
from scripts.core.structure_store import StructureError
results = []
for action in [store.list_assets, lambda: store.create_asset(title='普通示例', kind='technique',
        stations=['writer'], intents=['foreshadowing'], tags=[], body='中性测试正文')]:
    try:
        action()
    except StructureError as exc:
        results.append(exc.code)
print(json.dumps(results))
''')
        self.assertEqual(report, ['KNOWLEDGE_PATH_UNSAFE', 'KNOWLEDGE_PATH_UNSAFE'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_switching_configuration_cannot_write_to_the_previous_package(self):
        second = self.base / 'second'
        second.mkdir()
        (second / 'provider.json').write_text(json.dumps(self.manifest))
        config = self.base / 'second-config.json'
        config.write_text(json.dumps({'schemaVersion': 1, 'root': str(second)}))
        report = self.fresh_python('''
import os, json
from pathlib import Path
from scripts.core import knowledge_store as store
from scripts.core.structure_store import StructureError
os.environ['GOVEL_PROVIDER_CONFIG'] = str(Path(os.environ['PROBE_BASE']) / 'second-config.json')
try:
    store.create_asset(title='普通示例', kind='technique', stations=['writer'],
                       intents=['foreshadowing'], tags=[], body='中性测试正文')
except StructureError as exc:
    print(json.dumps({'code': exc.code}))
''')
        self.assertEqual(report['code'], 'KNOWLEDGE_PROVIDER_CHANGED')
        self.assertFalse((self.root / 'knowledge').exists())
        self.assertFalse((second / 'knowledge').exists())

    def test_index_lock_and_history_directories_reject_links(self):
        self.asset()
        outside = self.base / 'outside'
        outside.mkdir()
        report = self.fresh_python('''
import os, json
from pathlib import Path
from scripts.core import knowledge_store as store, knowledge_index as index
from scripts.core.structure_store import StructureError
root = Path(os.environ['PROBE_ROOT']); outside = Path(os.environ['PROBE_BASE']) / 'outside'
current = store.read_asset('KN-EXAMPLE')
results = []
for name, action in [('.cache', lambda: store.create_asset(title='示例', kind='technique', stations=['writer'],
        intents=[], tags=[], body='普通正文')), ('.cache', index.rebuild_index),
        ('.history', lambda: store.save_asset(asset_id='KN-EXAMPLE', metadata=current['metadata'],
         body='修改后正文', expected_hash=current['sha256']))]:
    path = root / 'knowledge' / name
    path.symlink_to(outside, target_is_directory=True)
    try:
        action()
    except StructureError as exc:
        results.append(exc.code)
    finally:
        path.unlink()
print(json.dumps(results))
''')
        self.assertEqual(report, ['KNOWLEDGE_PATH_UNSAFE'] * 3)
        self.assertEqual(list(outside.iterdir()), [])

    def test_external_asset_edits_and_catalog_membership_invalidate_old_preparation(self):
        self.asset()
        project = self.base / 'project'
        project.mkdir()
        fixture = create_schema5_project(project, chapter_count=1)
        report = self.fresh_python('''
import os, json
from pathlib import Path
from scripts.core.task_prepare import prepare_task
from scripts.core.transport_cache import prepared
from scripts.core.reading_batch import read_batch
from scripts.core.structure_store import StructureError
root = Path(os.environ['PROBE_BASE']) / 'project'
asset = Path(os.environ['PROBE_ROOT']) / 'knowledge/assets/example.md'
old = prepare_task(root, 'planner', chapter_id=os.environ['PROBE_CHAPTER'], chapter_goal='伏笔')
assert old['knowledgePack']['items'][0]['assetId'] == 'KN-EXAMPLE'
asset.write_text(asset.read_text() + '\\n新参考说明。\\n')
results = []
for action in [lambda: prepared(root, old['prepareRef']), lambda: read_batch(root,
        credential=old['prepareRef'], readIds=old['promptPlan']['contracts'][0]['readIds'])]:
    try:
        action()
    except StructureError as exc:
        results.append(exc.code)
fresh = prepare_task(root, 'planner', chapter_id=os.environ['PROBE_CHAPTER'], chapter_goal='伏笔')
assert fresh['knowledgePack']['items'][0]['sha256'] != old['knowledgePack']['items'][0]['sha256']
duplicate = asset.parent / 'second.md'
duplicate.write_text(asset.read_text().replace('KN-EXAMPLE', 'KN-SECOND'))
try:
    prepared(root, fresh['prepareRef'])
except StructureError as exc:
    results.append(exc.code)
print(json.dumps(results))
''', PROBE_CHAPTER=fixture['chapterIds'][0])
        self.assertEqual(report, ['READ_BATCH_STALE'] * 3)

    def test_warm_validator_and_voice_reads_use_the_current_registry(self):
        patterns = json.loads((provider.ROOT / 'shared/registries/text-patterns.json').read_text())
        patterns['ai']['regex'] = []
        patterns['ai']['clusters'] = []
        patterns['ai']['triple'].update(pattern='旧信号', action_exempt_pattern='(?!)', name='测试规则',
                                        review='mechanical', severity='blocking', gate=True)
        patterns['voice']['globalBannedInterjections'] = ['旧信号']
        path = self.root / 'patterns.json'
        path.write_text(json.dumps(patterns, ensure_ascii=False))
        self.manifest['registries']['text-patterns'] = {'path': 'patterns.json', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.save()
        result = self.fresh_python('''
import os, json, hashlib
from pathlib import Path
from scripts.text import validator
from scripts import check_voice_lexicon as voice
before = validator.check_ai_patterns('新信号。')[0]
root = Path(os.environ['PROBE_ROOT']); path = root / 'patterns.json'
patterns = json.loads(path.read_text())
patterns['ai']['triple']['pattern'] = '新信号'
patterns['voice']['globalBannedInterjections'] = ['新信号']
path.write_text(json.dumps(patterns, ensure_ascii=False))
manifest = json.loads((root / 'provider.json').read_text())
manifest['registries']['text-patterns']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
(root / 'provider.json').write_text(json.dumps(manifest))
print(json.dumps({'before': before, 'after': validator.check_ai_patterns('新信号。')[0],
                  'voice': voice.load_global_banned()}))
''')
        self.assertEqual(result, {'before': True, 'after': False, 'voice': ['新信号']})

    def test_disabled_scene_rules_report_not_applicable(self):
        from scripts.text import validator
        with patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.base / 'empty')}, clear=True):
            _, checks, _ = validator.build_checks('peakScene', False, 5)
            selected = [(name, check) for name, check in checks if name in validator.ADULT_CHECK_NAMES]
            self.assertTrue(selected)
            for name, check in selected:
                row = validator._norm(check('普通场景。'), name)
                self.assertEqual(row['status'], 'not_applicable')
                self.assertFalse(row['evaluated'])

    def test_pinned_diagnostic_rejects_even_an_empty_provider_mismatch(self):
        with self.environment():
            expected = provider.binding()
        with patch.dict(os.environ, {provider.EXPECTED_PROVIDER_ENV: json.dumps(expected),
                                     'GOVEL_DATA_HOME': str(self.base / 'empty')}, clear=True), \
             self.assertRaises(provider.ProviderError) as caught:
            provider.binding()
        self.assertEqual(caught.exception.code, 'PROVIDER_CHANGED')

    def test_diagnostic_subprocess_keeps_the_expected_binding_through_a_switch_back(self):
        with self.environment():
            expected = provider.binding()
            environment = provider.pinned_environment(expected)
        self.manifest['version'] = '2'
        self.save()
        result = subprocess.run([sys.executable, '-c',
            'from scripts.core.data_provider import binding; binding()'],
            cwd=provider.ROOT, env=environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ProviderError', result.stderr)
        self.manifest['version'] = '1'
        self.save()
        with self.environment():
            self.assertEqual(provider.binding(), expected)


if __name__ == '__main__':
    unittest.main()
