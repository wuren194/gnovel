from __future__ import annotations

import tempfile
import unittest
import json
import subprocess
import sys
import base64
from copy import deepcopy
from pathlib import Path

from scripts.core.source_changes import preview_source_change
from scripts.core.source_content_proposals import resolve_content_target, preview_content_change
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_commands import _base_plan, _write_plan
from scripts.core.structure_transaction import undo
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_source import source_fixture


class ContentProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source_fixture(self.root)

    def propose(self, record_id='AT-HEIGHT', *, new_record=None, evidence=None):
        kind = 'fact' if record_id.startswith('SF-') else 'attribute'
        resolved = resolve_content_target(self.root, recordId=record_id, kind=kind)
        old = resolved['record']
        if new_record is None:
            new_record = deepcopy(old)
            if kind == 'attribute':
                new_record['values'][0]['value']['number'] = 185
            else:
                new_record['text'] += '后来离开。'
        if evidence is None:
            evidence = ([{'fragmentId': record_id, 'quote': old['text']}]
                        if kind == 'fact' else
                        [{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}])
        return preview_content_change(self.root, sessionRef=resolved['sessionRef'],
                                      expectedOldRecord=old, newRecord=new_record,
                                      reason='作者核对源记录', evidence=evidence,
                                      approvalActor='author', approvalEvidence='本地确认')

    def test_attribute_and_fact_use_old_record_and_same_transaction(self):
        plan = self.propose()
        self.assertFalse(plan['parameters']['contentProposal']['approval']['verifiedHuman'])
        self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], 190)
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], 185)
        fact = self.propose('SF-LIFE')
        commit_plan(self.root, operation_id=fact['operationId'], plan_hash=fact['planHash'], approved=True)
        self.assertIn('后来离开', next(row['text'] for row in load_structure(self.root)['fragments']
                                    if row['fragmentId'] == 'SF-LIFE'))

    def test_fact_delete_is_reviewed_and_undo_restores_identity(self):
        resolved = resolve_content_target(self.root, recordId='SF-LIFE', kind='fact')
        plan = preview_content_change(self.root, sessionRef=resolved['sessionRef'],
                                      expectedOldRecord=resolved['record'], newRecord=None,
                                      reason='作者确认撤销该事实',
                                      evidence=[{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}],
                                      approvalActor='author', approvalEvidence='本地确认')
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertFalse(any(row['fragmentId'] == 'SF-LIFE' for row in load_structure(self.root)['fragments']))
        undo(self.root, plan['operationId'], actor='test')
        self.assertTrue(any(row['fragmentId'] == 'SF-LIFE' for row in load_structure(self.root)['fragments']))

    def test_old_record_unrelated_quote_and_stale_session_fail(self):
        resolved = resolve_content_target(self.root, recordId='AT-HEIGHT', kind='attribute')
        wrong = deepcopy(resolved['record'])
        wrong['values'][0]['value']['number'] = 180
        with self.assertRaises(StructureError) as caught:
            preview_content_change(self.root, sessionRef=resolved['sessionRef'], expectedOldRecord=wrong,
                                   newRecord=resolved['record'], reason='测试',
                                   evidence=[{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}],
                                   approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'EXPECTED_OLD_VALUE_CONFLICT')
        with self.assertRaises(StructureError) as caught:
            self.propose(evidence=[{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}])
        self.assertEqual(caught.exception.code, 'CONTENT_EVIDENCE_SCOPE_INVALID')
        plan = self.propose()
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        with self.assertRaises(StructureError) as caught:
            preview_content_change(self.root, sessionRef=resolved['sessionRef'], expectedOldRecord=resolved['record'],
                                   newRecord=wrong, reason='测试',
                                   evidence=[{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}],
                                   approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')
        malformed = {'sourceHash': resolved['sourceHash'], 'kind': 'attribute',
                     'recordId': 'AT-HEIGHT', 'ownerId': 'C-ALPHA', 'chapterId': []}
        token = base64.urlsafe_b64encode(json.dumps(malformed).encode()).decode().rstrip('=')
        with self.assertRaises(StructureError) as caught:
            preview_content_change(self.root, sessionRef=token, expectedOldRecord=resolved['record'],
                                   newRecord=wrong, reason='测试',
                                   evidence=[{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}],
                                   approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(caught.exception.code, 'SESSION_REF_INVALID')

    def test_generic_change_cannot_bypass_fact_or_attribute_gate(self):
        source = load_structure(self.root)
        attr = deepcopy(source['attributes'][0]); attr['values'][0]['value']['number'] = 185
        for change in ({'action': 'put', 'collection': 'attributes', 'record': attr},
                       {'action': 'delete', 'collection': 'fragments', 'id': 'SF-LIFE'}):
            with self.subTest(change=change), self.assertRaises(StructureError) as caught:
                preview_source_change(self.root, changes=[change], requestId='bypass',
                                      baseRevision=source['revision'], reason='绕过提案')
            self.assertEqual(caught.exception.code, 'CONTENT_PROPOSAL_REQUIRED')
        altered = deepcopy(source)
        altered['attributes'][0]['values'][0]['value']['number'] = 185
        direct = _base_plan(source, operation='source_change', actor='app', parameters={},
                            impact={}, proposed=altered)
        with self.assertRaises(StructureError) as caught:
            _write_plan(self.root, direct)
        self.assertEqual(caught.exception.code, 'CONTENT_PROPOSAL_REQUIRED')

    def test_unbound_attribute_requires_exact_snapshot_evidence(self):
        resolved = resolve_content_target(self.root, recordId='AT-BETA-HEIGHT', kind='attribute')
        edited = deepcopy(resolved['record'])
        edited['values'][0]['value']['number'] = 175
        with self.assertRaises(StructureError):
            preview_content_change(self.root, sessionRef=resolved['sessionRef'],
                                   expectedOldRecord=resolved['record'], newRecord=edited,
                                   reason='测试', evidence=[{'recordHash': 'wrong'}],
                                   approvalActor='author', approvalEvidence='本地确认')
        from scripts.core.source_review import fingerprint
        plan = preview_content_change(self.root, sessionRef=resolved['sessionRef'],
                                      expectedOldRecord=resolved['record'], newRecord=edited,
                                      reason='作者核对', evidence=[{'recordHash': fingerprint(resolved['record'])}],
                                      approvalActor='author', approvalEvidence='本地确认')
        self.assertEqual(plan['operation'], 'canon_content_proposal')

    def test_cli_and_mcp_resolve_same_current_record_without_writes(self):
        project = Path(__file__).resolve().parents[1]
        before = (self.root / '.govel/structure.json').read_bytes()
        cli = subprocess.run([sys.executable, str(project / 'scripts/novelctl.py'), 'project',
                              'source', 'content-resolve', '--project-root', str(self.root),
                              '--record-id', 'AT-HEIGHT', '--kind', 'attribute'],
                             cwd=project, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'resolve_source_content_target', 'arguments': {
                'projectRoot': str(self.root), 'recordId': 'AT-HEIGHT', 'kind': 'attribute', 'view': 'full'}}}
        mcp = subprocess.run([sys.executable, str(project / 'scripts/mcp_server.py')],
                             input=json.dumps(request) + '\n', cwd=project,
                             capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        actual = json.loads(json.loads(mcp.stdout.splitlines()[-1])['result']['content'][0]['text'])
        self.assertEqual(json.loads(cli.stdout), actual)
        self.assertEqual(before, (self.root / '.govel/structure.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
