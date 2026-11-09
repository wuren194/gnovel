"""Editable local roundtables, their exact read receipts, and independent knowledge."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core import data_provider as provider
from scripts.core.prompt_plan import resolve_prompt_plan
from scripts.core.review_plan import SOURCE_CONTRACT
from tests.fixtures_current import create_schema5_project


class LocalRoundtableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'local'
        self.root.mkdir()
        self.path = self.root / 'roundtable.md'

    def environment(self):
        return patch.dict(os.environ, {'GOVEL_DATA_HOME': str(self.root)}, clear=True)

    def plan(self, station='titler'):
        return resolve_prompt_plan(self.base, 'govel-' + station, design={},
                                   decision_type='title' if station == 'titler' else 'design')

    def private(self):
        self.path.write_text('# 私人圆桌\n逐席核对当前事实，保留作者选择。\n')

    def fresh(self, code):
        env = {**os.environ, 'GOVEL_DATA_HOME': str(self.root), 'PROBE_ROOT': str(self.root),
               'PROBE_BASE': str(self.base), 'GOVEL_LOG_MODE': 'off', 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('GOVEL_PROVIDER_CONFIG', None)
        result = subprocess.run([sys.executable, '-c', code], cwd=provider.ROOT, env=env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_fixed_checkout_location_needs_no_configuration(self):
        self.private()
        with patch.object(provider, 'ROOT', self.base), patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(provider.active_provider())
            self.assertEqual(provider.storage_root(), self.root)
            self.assertEqual(self.plan()['roundtable']['label'], '用户圆桌')

    def test_missing_empty_and_whitespace_use_default_without_creating_files(self):
        with self.environment():
            self.assertEqual(self.plan()['roundtable']['source'], 'default')
            self.assertFalse(self.path.exists())
            for raw in (b'', b' \n\t', b'\xef\xbb\xbf\n'):
                self.path.write_bytes(raw)
                plan = self.plan()
                self.assertEqual(plan['roundtable']['label'], '通用圆桌')
                self.assertEqual(plan['reviewPlan']['sourceContract']['root'], 'toolbox')
                self.assertIn(SOURCE_CONTRACT, [row['path'] for row in plan['contracts']])
                self.assertEqual(plan['providerMaterials'], [])

    def test_all_review_stations_share_one_live_source(self):
        self.private()
        with self.environment():
            for station in ('composer', 'planner', 'editor', 'router', 'titler'):
                plan = self.plan(station)
                self.assertNotIn(SOURCE_CONTRACT, [row['path'] for row in plan['contracts']])
                self.assertEqual([row['path'] for row in plan['providerMaterials']], ['roundtable.md'])
                self.assertEqual(plan['reviewPlan']['sourceContract']['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
            self.assertEqual(provider.materials('writer'), [])
            self.assertIsNone(resolve_prompt_plan(self.base, 'govel-writer', design={})['roundtable'])

    def test_invalid_text_links_directories_and_read_errors_do_not_fall_back(self):
        with self.environment():
            self.path.write_bytes(b'\xff\xfe')
            with self.assertRaises(provider.ProviderError) as caught:
                self.plan()
            self.assertEqual(caught.exception.code, 'ROUNDTABLE_INVALID')
            self.path.unlink()
            outside = self.base / 'outside.md'
            outside.write_text('Example')
            self.path.symlink_to(outside)
            with self.assertRaises(provider.ProviderError) as caught:
                self.plan()
            self.assertEqual(caught.exception.code, 'PROVIDER_PATH_UNSAFE')
            self.path.unlink()
            self.path.mkdir()
            with self.assertRaises(provider.ProviderError) as caught:
                self.plan()
            self.assertEqual(caught.exception.code, 'ROUNDTABLE_UNREADABLE')
            self.path.rmdir()
            from scripts.core.static_cache import cached_bytes
            def denied(path):
                if Path(path) == self.path:
                    raise PermissionError('denied')
                return cached_bytes(path)
            with patch('scripts.core.static_cache.cached_bytes', side_effect=denied):
                with self.assertRaises(provider.ProviderError) as caught:
                    self.plan()
                self.assertEqual(caught.exception.code, 'ROUNDTABLE_UNREADABLE')

    def test_live_roundtable_is_not_allowed_in_the_pinned_manifest(self):
        self.private()
        manifest = {'schemaVersion': 1, 'providerId': 'example', 'version': '1', 'registries': {}, 'materials': []}
        manifest['roundtable'] = {'path': 'roundtable.md', 'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest()}
        (self.root / 'provider.json').write_text(json.dumps(manifest))
        with self.environment(), self.assertRaises(provider.ProviderError) as caught:
            provider.active_provider()
        self.assertEqual(caught.exception.code, 'PROVIDER_INVALID')

    def test_manual_edits_need_no_manifest_update_or_knowledge_restart(self):
        self.private()
        manifest = {'schemaVersion': 1, 'providerId': 'example', 'version': '1', 'registries': {}, 'materials': []}
        (self.root / 'provider.json').write_text(json.dumps(manifest))
        sample = json.loads((provider.ROOT / 'knowledge/example.json').read_text())
        assets = self.root / 'knowledge/assets'
        assets.mkdir(parents=True)
        (assets / 'example.md').write_text('---\n' + json.dumps(sample['metadata'], ensure_ascii=False) + '\n---\n' + sample['body'])
        result = self.fresh('''
import os, json
from pathlib import Path
from scripts.core import knowledge_store as store
from scripts.core.data_provider import binding, roundtable_resource
root = Path(os.environ['PROBE_ROOT']); path = root / 'roundtable.md'
before_manifest = (root / 'provider.json').read_bytes()
before_binding = binding(); before_assets = store.list_assets()['items']
old = roundtable_resource(); old_stat = path.stat()
path.write_bytes(path.read_bytes().replace('事实'.encode(), '依据'.encode()))
os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
new = roundtable_resource()
assert old['sha256'] != new['sha256']
assert binding() == before_binding
assert store.list_assets()['items'] == before_assets
assert (root / 'provider.json').read_bytes() == before_manifest
path.write_text('   ')
assert roundtable_resource() is None
assert store.list_assets()['items'] == before_assets
print(json.dumps({'manualEditApplied': True, 'knowledgeUnchanged': True}))
''')
        self.assertTrue(result['manualEditApplied'] and result['knowledgeUnchanged'])

    def test_source_transitions_invalidate_review_receipts_but_not_writer(self):
        project = self.base / 'project'
        project.mkdir()
        create_schema5_project(project, chapter_count=1)
        result = self.fresh('''
import os, json
from pathlib import Path
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError
from scripts.core.data_provider import ROUNDTABLE_MATERIAL_ID
project = Path(os.environ['PROBE_BASE']) / 'project'; path = Path(os.environ['PROBE_ROOT']) / 'roundtable.md'
chapter = json.loads((project / '.govel/structure.json').read_text())['chapters'][0]['chapterId']
def prepare(task):
    return call_operation('task.prepare', project, {'task': task, 'chapterId': chapter, 'view': 'station'})
def selection(report):
    custom = [row for row in report['promptPlan']['providerMaterials'] if row['id'] == ROUNDTABLE_MATERIAL_ID]
    return custom[0]['readId'] if custom else report['promptPlan']['contracts'][0]['readIds'][0]
def read(report):
    return call_operation('task.read-batch', project, {'credential': report['reading']['credential'], 'readIds': [selection(report)]})
errors = []
default = prepare('titler'); writer = prepare('writer')
for previous, body in [(default, '# User roundtable\\nFirst rule.\\n')]:
    path.write_text(body)
    try: read(previous)
    except StructureError as exc: errors.append(exc.code)
user = prepare('titler'); current = read(user)
assert current['items'][0]['root'] == 'provider'
assert current['items'][0]['text'] == path.read_text()
path.write_text('# User roundtable\\nChanged rule.\\n')
try: read(user)
except StructureError as exc: errors.append(exc.code)
user = prepare('titler'); path.write_text(' \\n')
try: read(user)
except StructureError as exc: errors.append(exc.code)
default = prepare('titler'); path.unlink()
assert read(default)['items']
assert read(writer)['items']
assert not (Path(os.environ['PROBE_ROOT']) / 'provider.json').exists()
print(json.dumps(errors))
''')
        self.assertEqual(result, ['READ_BATCH_STALE'] * 3)

    def test_roundtable_edit_during_plan_build_cannot_mix_sources(self):
        from scripts.core import data_provider
        original = data_provider.materials
        def changed(station, design=None):
            self.private()
            return original(station, design)
        with self.environment(), patch.object(data_provider, 'materials', side_effect=changed):
            with self.assertRaises(provider.ProviderError) as caught:
                self.plan()
        self.assertEqual(caught.exception.code, 'PROVIDER_CHANGED')


if __name__ == '__main__':
    unittest.main()
