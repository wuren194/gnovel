"""Call associations describe observed data, not inferred project state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from scripts.core import call_log_metadata as metadata


class CallLogReferenceTests(unittest.TestCase):
    def test_existing_fields_keep_their_pointer_and_do_not_follow_paths(self):
        payload = {
            'chapterId': 'CH-CURRENT', 'activeToken': 7,
            'nextAction': {'arguments': {'chapterId': 'CH-NEXT', 'operationId': 'OP-NEXT'}},
            'dependencyManifest': [{'path': '/private/novel/source.json', 'sha256': 'abc'}],
            'notes': '正文提到 CH-NOT-A-REFERENCE，也提到密码和 activeToken。',
            'taskId': 'invented-task', 'artifactId': 'invented-artifact',
            'a~/b': {'artifactSha256': 'def'},
        }
        before = deepcopy(payload)
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('no referenced reads')):
            rows = metadata.correlations(payload, 'response', 8)
        self.assertEqual(payload, before)
        observed = {row['pointer']: row for row in rows}
        self.assertEqual(observed['/activeToken']['value'], 7)
        self.assertEqual(observed['/nextAction/arguments/chapterId']['value'], 'CH-NEXT')
        self.assertEqual(observed['/dependencyManifest/0/path']['value'], '/private/novel/source.json')
        self.assertEqual(observed['/a~0~1b/artifactSha256']['value'], 'def')
        self.assertTrue(all(row['origin'] == 'response' for row in rows))
        self.assertNotIn('/notes', observed)
        self.assertNotIn('/taskId', observed)
        self.assertNotIn('/artifactId', observed)

    def test_depth_and_cycles_are_bounded_without_losing_shared_references(self):
        shared = {'chapterId': 'CH-A'}
        payload = {'left': shared, 'right': shared, 'chapterId': 'CH-ROOT'}
        payload['cycle'] = payload
        rows = metadata.correlations(payload, 'request', 1)
        self.assertEqual(rows, [{'field': 'chapterId', 'value': 'CH-ROOT',
                                 'pointer': '/chapterId', 'origin': 'request'}])
        rows = metadata.correlations(payload, 'request', 3)
        self.assertEqual([row['pointer'] for row in rows],
                         ['/left/chapterId', '/right/chapterId', '/chapterId'])

    def test_target_context_uses_direct_cli_and_rpc_targets_only(self):
        direct = {'project_root': './never-initialized', 'chapter_id': 'CH001',
                  'task': 'writer', 'nextAction': {'chapterId': 'CH-NEXT'}}
        with patch.object(Path, 'resolve', side_effect=AssertionError('no project resolution')):
            context = metadata.target_context(direct)
        self.assertEqual(context, {'projectRoot': os.path.abspath('./never-initialized'),
                                   'chapterId': 'CH001', 'task': 'writer'})
        self.assertNotIn('projectId', context)
        rpc = {'method': 'tools/call', 'params': {
            'name': 'prepare_task', 'arguments': {'projectRoot': '/novel',
                                                'chapterId': 'CH-A', 'station': 'writer'}}}
        self.assertEqual(metadata.target_context(rpc),
                         {'projectRoot': '/novel', 'chapterId': 'CH-A', 'station': 'writer'})
        self.assertEqual(metadata.target_context({'nextAction': direct}), {})
        self.assertEqual(metadata.target_context({'method': 'initialize', 'params': direct}), {})

    def test_discovery_bridge_retains_outer_project_and_inner_chapter(self):
        arguments = {'projectRoot': '/outer', 'operation': 'source.chapter-plan',
                     'arguments': {'projectRoot': '/invalid-inner', 'chapterId': 'CH-A',
                                   'nextAction': {'chapterId': 'CH-NEXT'}}}
        rpc = {'method': 'tools/call', 'params': {
            'name': 'read_discovered_operation', 'arguments': arguments}}
        expected = {'projectRoot': '/outer', 'chapterId': 'CH-A'}
        self.assertEqual(metadata.target_context(rpc), expected)
        self.assertEqual(metadata.target_context({**arguments, 'operation_binding': 'capabilities.read'}),
                         expected)
        self.assertEqual(metadata.target_context(arguments), {'projectRoot': '/outer'})


class CallLogIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'scripts').mkdir()
        self.source = self.root / 'scripts/loaded.py'
        self.source.write_text('value = 1\n')
        self.manifest = {'version': '13.5.0', 'schemaVersion': 5, 'compatibilityVersion': 5}
        (self.root / 'toolbox.manifest.json').write_text(json.dumps(self.manifest))
        module = ModuleType('call_log_identity_fixture')
        module.__file__ = str(self.source)
        external = ModuleType('call_log_external_fixture')
        external.__file__ = str(self.root / 'unloaded.py')
        for mock in (patch.object(metadata, 'TOOLBOX_ROOT', self.root),
                     patch.dict(metadata.sys.modules, {'call_log_identity_fixture': module,
                                                       'call_log_external_fixture': external})):
            mock.start()
            self.addCleanup(mock.stop)

    def test_identity_tracks_loaded_sources_once_and_reports_subsequent_edits(self):
        with patch.object(Path, 'rglob', side_effect=AssertionError('no directory scan')):
            first = metadata.toolbox_identity()
            self.assertEqual(first['sourceFileCount'], 1)
            original_read = Path.read_bytes

            def read(path):
                if path == self.source:
                    raise AssertionError('source was already observed')
                return original_read(path)

            with patch.object(Path, 'read_bytes', read):
                again = metadata.toolbox_identity()
                self.assertEqual(first['sourceFingerprint'], again['sourceFingerprint'])
                self.source.write_text('value = 200\n')
                changed = metadata.toolbox_identity()
        self.assertEqual(changed['sourceFingerprint'], first['sourceFingerprint'])
        self.assertEqual(changed['sourceChangedSinceObservation'], ['scripts/loaded.py'])
        self.assertTrue(changed['sourceObservationIncomplete'])
        self.assertEqual(first['sourceFingerprintKind'], 'firstObservedSourceBytes')
        self.assertEqual(first['version'], self.manifest['version'])
        self.assertEqual(first['schemaVersion'], self.manifest['schemaVersion'])

    def test_new_loaded_module_and_manifest_change_have_distinct_fingerprints(self):
        first = metadata.toolbox_identity()
        source = self.root / 'scripts/newly_loaded.py'
        source.write_text('other = True\n')
        module = ModuleType('call_log_new_module_fixture')
        module.__file__ = str(source)
        with patch.dict(metadata.sys.modules, {'call_log_new_module_fixture': module}):
            second = metadata.toolbox_identity()
        self.assertEqual(second['sourceFileCount'], 2)
        self.assertNotEqual(first['sourceFingerprint'], second['sourceFingerprint'])
        self.manifest['version'] = '13.6.0'
        (self.root / 'toolbox.manifest.json').write_text(json.dumps(self.manifest))
        third = metadata.toolbox_identity()
        self.assertEqual(third['version'], '13.6.0')
        self.assertNotEqual(second['sourceFingerprint'], third['sourceFingerprint'])

    def test_unreadable_loaded_source_remains_explicitly_incomplete(self):
        self.source.unlink()
        identity = metadata.toolbox_identity()
        self.assertTrue(identity['sourceObservationIncomplete'])
        self.assertEqual(identity['unavailableSources'], ['scripts/loaded.py'])
        self.assertEqual(identity['sourceChangedSinceObservation'], [])
