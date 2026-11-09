from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import continuation_context, novelctl
from scripts.core.catalog_commands import preview_set_visibility
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_current import ROOT, create_schema5_project


class RoundTwoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = create_schema5_project(self.root)

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/novelctl.py"), "project", *args, "--project-root", str(self.root)], cwd=ROOT, capture_output=True, text=True, timeout=20)
        return result, json.loads(result.stdout)

    def test_explicit_perspective_content_changes_the_readable_source_not_canon(self):
        from scripts.core.source_changes import preview_source_change
        from scripts.core.source_query import query_source
        source = load_structure(self.root)
        cid = self.fixture['chapterIds'][-1]
        before = {entity['entityId']: entity for entity in source['entities']}
        attr = next(row for row in source['attributes'] if row['ownerId'] == cid)
        change = {'action': 'put', 'collection': 'fragments', 'record': {
            'fragmentId': '$belief', 'ownerId': cid, 'order': 2, 'contentRole': 'belief',
            'perspectiveId': self.fixture['characterId'], 'contentBindings': [attr['attributeId']],
            'text': '沈砚认为钥匙藏在仓库。'}}
        plan = preview_source_change(self.root, changes=[change], requestId='belief',
            baseRevision=source['revision'], reason='作者明确登记角色认知')
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        selected = query_source(self.root, selection={'knowledgeMode': 'perspective',
            'perspectiveIds': [self.fixture['characterId']]}, view='detail')
        self.assertIn('沈砚认为钥匙藏在仓库。', json.dumps(selected, ensure_ascii=False))
        self.assertEqual({entity['entityId']: entity for entity in load_structure(self.root)['entities']}, before)
        result, payload = self.cli('context', '--chapter-id', cid, '--pov-character', self.fixture['characterId'],
                                  '--view', 'station', '--task-type', 'writer')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(payload['sourceContext']['contentQuery']['selection']['perspectiveIds'], [self.fixture['characterId']])
        self.assertEqual(payload['contextBudget']['usedCharacters'], len(result.stdout.rstrip('\n')))

    def test_visibility_validates_current_metadata_scope_and_identity(self):
        cid = self.fixture['chapterIds'][0]
        for kwargs in ({}, {'visibility': 'unknown'}, {'visibility': 'public', 'visible_to': []},
                       {'visible_to': [self.fixture['locationId']]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(StructureError):
                preview_set_visibility(self.root, collection='chapters', record_id=cid, **kwargs)
        with self.assertRaises(StructureError):
            preview_set_visibility(self.root, collection='chapters', record_id='CH-MISSING', visibility='public')
        plan = preview_set_visibility(self.root, collection='chapters', record_id=cid, visibility='private')
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(load_structure(self.root)['chapters'][0]['visibleTo'], [])

    def test_station_envelope_rebinds_budget_and_hash(self):
        ctx = continuation_context.build_continuation_context(self.root, self.fixture["chapterIds"][0], view="station")
        result = continuation_context.bound_station_context({"ok": True, "code": "CONTINUATION_CONTEXT", **ctx})
        self.assertEqual(result["contextBudget"]["usedCharacters"], len(continuation_context.serialize_context(result)))
        self.assertLessEqual(result["contextBudget"]["usedCharacters"], result["contextBudget"]["total"])
        self.assertNotEqual(result["contextHash"], ctx["contextHash"])
        oversized = {**ctx, "transportMessage": "x" * ctx["contextBudget"]["total"]}
        with self.assertRaises(continuation_context.ContinuationContextError) as error:
            continuation_context.bound_station_context(oversized)
        self.assertEqual(error.exception.code, "CONTEXT_BUDGET_IMPOSSIBLE")

    def test_receipt_cli_passes_finalization_options_to_shared_model(self):
        args = novelctl.build_parser().parse_args(["project", "workflow", "chapter-receipt", "--project-root", str(self.root), "--chapter-id", self.fixture["chapterIds"][0], "--skip-title"])
        with patch.object(novelctl, "chapter_receipt", return_value={"readyToFinalize": False}) as receipt:
            novelctl._dispatch(args)
        receipt.assert_called_once_with(self.root, self.fixture["chapterIds"][0], title_candidate_id=None, skip_title=True, view='model')

    def test_removed_format_upgraders_are_not_runtime_commands(self):
        for command in ('upgrade', 'ownership-migration', 'migration'):
            result = subprocess.run([sys.executable, str(ROOT/'scripts/novelctl.py'), 'project', command],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('invalid choice', json.loads(result.stdout)['error'])


if __name__ == "__main__":
    unittest.main()
