"""Preparation delivery savings never cache readiness or assume retained context."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.transport_cache import _directory
from tests.fixtures_workflow import review_ready_project


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


class PrepareDeltaDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.args = {'task': 'editor', 'chapterId': self.cid, 'view': 'model'}

    def prepare(self, **arguments):
        return call_operation('task.prepare', self.root, {**self.args, **arguments})

    def merged(self, first, delta):
        result = deepcopy(first)
        for key in delta['delivery']['removedFields']:
            result.pop(key, None)
        result.update({key: value for key, value in delta.items() if key != 'delivery'})
        return result

    def test_only_explicit_held_baseline_enables_delta_and_merges_losslessly(self):
        first = self.prepare()
        self.assertEqual(first, self.prepare())
        delta = self.prepare(heldPrepareRef=first['prepareRef'])
        self.assertEqual(delta['delivery']['mode'], 'delta')
        self.assertNotIn('modelContext', delta)
        self.assertNotIn('readingTools', delta)
        self.assertEqual(self.merged(first, delta), first)
        self.assertLess(size(delta), size(first) // 2)
        for key in ('readiness', 'missing', 'scope', 'authorization', 'nextAction', 'actionReadiness'):
            self.assertEqual(delta[key], first[key])

    def test_authorization_and_blockers_are_recomputed_even_when_materials_are_held(self):
        first = self.prepare()
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['approvedChapterIds'] = []
        write_json(self.root / '.govel/workflow.json', workflow)
        delta = self.prepare(heldPrepareRef=first['prepareRef'])
        self.assertEqual(delta['delivery']['mode'], 'delta')
        self.assertFalse(delta['readiness']['authorizationReady'])
        self.assertIsNone(delta['nextAction'])
        self.assertIn('CHAPTER_OUTSIDE_APPROVED_RANGE', {row['code'] for row in delta['missing']})
        self.assertTrue(any(row['kind'] == 'authorization' for row in delta['delivery']['changedInputs']))
        self.assertEqual(self.merged(first, delta), self.prepare())

    def test_baseline_uses_the_projection_actually_produced_not_a_new_renderer(self):
        first = self.prepare()
        import scripts.core.context_transport as transport
        original = transport.task_model_response

        def upgraded(report):
            result = original(report)
            if '_modelDelivery' not in report:
                result['newPresentationField'] = 'not present in the held baseline'
            return result

        with patch.object(transport, 'task_model_response', side_effect=upgraded):
            delta = self.prepare(heldPrepareRef=first['prepareRef'])
        self.assertEqual(delta['delivery']['mode'], 'delta')
        self.assertEqual(delta['newPresentationField'], 'not present in the held baseline')

    def test_reordered_read_ids_are_redelivered_with_current_credential(self):
        first = self.prepare()
        from scripts.core.artifact_pack import pack_artifact
        write_json(self.root / '工作台/new-title.json', {'candidates': [{'title': 'New title'}]})
        pack_artifact(self.root, chapterId=self.cid, artifactType='title_candidates', input='工作台/new-title.json')
        delta = self.prepare(heldPrepareRef=first['prepareRef'])
        self.assertIn('readList', delta)
        self.assertIn('contractReads', delta)
        self.assertEqual(self.merged(first, delta), self.prepare())
        self.assertEqual(delta['reading']['credential'], delta['prepareRef'])
        read_ids = delta['contractReads'][0]['readIds']
        page = call_operation('task.read-batch', self.root, {'credential': delta['prepareRef'], 'readIds': read_ids})
        self.assertTrue(page['items'])

    def test_lost_baseline_and_changed_task_return_full_materials(self):
        first = self.prepare()
        reference = first['prepareRef']
        (_directory() / (reference.replace(':', '-') + '.json')).unlink()
        missing = self.prepare(heldPrepareRef=reference)
        self.assertEqual(missing['delivery']['mode'], 'full')
        self.assertIn('modelContext', missing)
        changed = self.prepare(heldPrepareRef=missing['prepareRef'], task='planner')
        self.assertEqual(changed['delivery']['mode'], 'full')
        self.assertEqual(changed['delivery']['reason'], 'task_or_pov_scope_changed')

    def test_snapshot_reads_complete_baseline_without_claiming_freshness(self):
        first = self.prepare()
        rows, cursor = [], None
        while True:
            page = call_operation('task.snapshot', self.root, {'reference': first['prepareRef'], 'maxBytes': 3000,
                **({'cursor': cursor} if cursor else {})})
            rows.extend(row['text'] for row in page['materials'])
            self.assertFalse(page['freshnessRevalidated'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        full = json.loads(''.join(rows))
        self.assertIn('context', full)
        self.assertIn('upstream', full)
        self.assertEqual(full['prepareRef'], first['prepareRef'])

    def test_cli_mcp_and_facade_share_the_same_delta(self):
        first = self.prepare()
        args = {**self.args, 'heldPrepareRef': first['prepareRef']}
        expected = call_operation('task.prepare', self.root, args)
        flags = ['--project-root', str(self.root), '--task', 'editor', '--chapter-id', self.cid,
                 '--view', 'model', '--held-prepare-ref', first['prepareRef']]
        for command in (['project', 'task', 'prepare'], ['project', 'prepare']):
            result = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), *command, *flags],
                                    capture_output=True, text=True)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'prepare_task', 'arguments': {'projectRoot': str(self.root), **args}}}
        result = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/mcp_server.py')],
                                input=json.dumps(request) + '\n', capture_output=True, text=True)
        payload = json.loads(result.stdout)['result']
        self.assertEqual(payload['structuredContent'], expected)
        self.assertEqual(json.loads(payload['content'][0]['text']), expected)
