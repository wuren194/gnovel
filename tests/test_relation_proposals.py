from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.source_changes import preview_source_change
from scripts.core.source_relation_proposals import resolve_relation_target, preview_relation_change
from scripts.core.structure_commands import _base_plan, _write_plan, commit_plan
from scripts.core.structure_commands import _plan_hash
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_transaction import undo
from scripts.core.structure_store import StructureError, load_structure, save_structure
from tests.fixtures_source import source_fixture
from scripts.core.source_evidence import register_evidence, query_evidence
from scripts.core.source_model import snapshot_hash
from scripts.core.write_sets import digest


class RelationProposalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source = source_fixture(self.root)
        source['relations'] = [{'relationId': 'REL-ALPHA-BETA', 'fromEntity': 'C-ALPHA',
                                'toEntity': 'C-BETA', 'kind': 'protects'}]
        save_structure(self.root, source)

    def resolve(self):
        return resolve_relation_target(self.root, relationId='REL-ALPHA-BETA')

    def propose(self, *, new_record='updated', evidence=None, evidence_mode='author_declaration',
                old=None, session=None):
        resolved = self.resolve()
        if new_record == 'updated':
            new_record = {**resolved['record'], 'kind': 'trusts'}
        return preview_relation_change(self.root, sessionRef=session or resolved['sessionRef'],
                                       expectedOldRecord=old or resolved['record'], newRecord=new_record,
                                       reason='作者确认', evidence=[] if evidence is None else evidence,
                                       evidenceMode=evidence_mode, approvalActor='author',
                                       approvalEvidence='本地声明')

    def test_resolve_exact_and_ambiguous_without_writable_session(self):
        resolved = self.resolve()
        self.assertEqual(resolved['status'], 'resolved')
        self.assertEqual(resolved['recordHash'], __import__('scripts.core.source_review', fromlist=['fingerprint']).fingerprint(resolved['record']))
        source = load_structure(self.root)
        source['relations'].append({'relationId': 'REL-SECOND', 'fromEntity': 'C-ALPHA',
                                    'toEntity': 'C-BETA', 'kind': 'protects'})
        save_structure(self.root, source)
        ambiguous = resolve_relation_target(self.root, fromEntity='C-ALPHA', toEntity='C-BETA', kind='protects')
        self.assertEqual(ambiguous['status'], 'ambiguous')
        self.assertNotIn('sessionRef', ambiguous)

    def test_old_record_stale_session_and_unrelated_evidence_fail(self):
        resolved = self.resolve()
        wrong = {**resolved['record'], 'kind': 'wrong'}
        with self.assertRaises(StructureError) as caught:
            self.propose(old=wrong)
        self.assertEqual(caught.exception.code, 'EXPECTED_OLD_VALUE_CONFLICT')
        with self.assertRaises(StructureError) as caught:
            self.propose(evidence_mode='source', evidence=[{'fragmentId': 'SF-LIFE',
                                                           'quote': '他独自守过城门。'}])
        self.assertEqual(caught.exception.code, 'RELATION_EVIDENCE_SCOPE_INVALID')
        plan = self.propose()
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        with self.assertRaises(StructureError) as caught:
            self.propose(session=resolved['sessionRef'])
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')

    def test_declared_author_approval_is_required(self):
        resolved = self.resolve()
        with self.assertRaises(StructureError) as caught:
            preview_relation_change(self.root, sessionRef=resolved['sessionRef'],
                                    expectedOldRecord=resolved['record'],
                                    newRecord={**resolved['record'], 'kind': 'trusts'},
                                    reason='作者确认', evidence=[], evidenceMode='author_declaration',
                                    approvalActor='', approvalEvidence='')
        self.assertEqual(caught.exception.code, 'RELATION_PROPOSAL_INCOMPLETE')

    def test_source_evidence_requires_both_registered_endpoints(self):
        source = load_structure(self.root)
        source['fragments'].append({'fragmentId': 'SF-RELATION', 'ownerId': 'C-ALPHA',
                                    'text': '甲和乙有约定。', 'order': 5, 'contentRole': 'fact',
                                    'contentBindings': ['AT-LIFE'], 'dependsOn': ['C-BETA']})
        save_structure(self.root, source)
        plan = self.propose(evidence_mode='source', evidence=[{'fragmentId': 'SF-RELATION',
                                                               'quote': '甲和乙有约定。'}])
        self.assertEqual(plan['parameters']['relationProposal']['evidenceMode'], 'source')

    def test_update_commit_delete_undo_and_generic_gate(self):
        source = load_structure(self.root)
        for change in ({'action': 'put', 'collection': 'relations',
                        'record': {**source['relations'][0], 'kind': 'trusts'}},
                       {'action': 'delete', 'collection': 'relations', 'id': 'REL-ALPHA-BETA'}):
            with self.assertRaises(StructureError) as caught:
                preview_source_change(self.root, changes=[change], requestId='bypass',
                                      baseRevision=source['revision'], reason='bypass')
            self.assertEqual(caught.exception.code, 'RELATION_PROPOSAL_REQUIRED')
        plan = self.propose()
        self.assertEqual(load_structure(self.root)['relations'][0]['kind'], 'protects')
        self.assertFalse(plan['parameters']['relationProposal']['approval']['verifiedHuman'])
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(load_structure(self.root)['relations'][0]['kind'], 'trusts')
        journal = read_json(self.root / '.govel/operations' / (plan['operationId'] + '.json'))
        self.assertFalse(journal['proposal']['approval']['verifiedHuman'])
        deleted = self.propose(new_record=None)
        commit_plan(self.root, operation_id=deleted['operationId'], plan_hash=deleted['planHash'], approved=True)
        self.assertEqual(load_structure(self.root)['relations'], [])
        undo(self.root, deleted['operationId'], actor='test')
        self.assertEqual(load_structure(self.root)['relations'][0]['relationId'], 'REL-ALPHA-BETA')
        self.assertEqual(load_structure(self.root)['relations'][0]['kind'], 'trusts')

    def test_relation_identity_and_second_record_are_immutable(self):
        resolved = self.resolve()
        with self.assertRaises(StructureError) as caught:
            self.propose(new_record={**resolved['record'], 'toEntity': 'C-ALPHA'})
        self.assertEqual(caught.exception.code, 'RELATION_PROPOSAL_INVALID')
        with self.assertRaises(StructureError) as caught:
            self.propose(new_record={**resolved['record'], 'kind': 'trusts', 'extra': 'hidden'})
        self.assertEqual(caught.exception.code, 'RELATION_PROPOSAL_INVALID')
        source = load_structure(self.root)
        proposed = deepcopy(source)
        proposed['relations'][0]['kind'] = 'trusts'
        proposed['entities'][0]['summary'] = 'hidden'
        valid = self.propose()
        direct = _base_plan(source, operation='canon_relation_proposal', actor='app',
                            parameters=valid['parameters'], impact={}, proposed=proposed)
        with self.assertRaises(StructureError) as caught:
            _write_plan(self.root, direct)
        self.assertEqual(caught.exception.code, 'CANON_PROPOSAL_REQUIRED')
        for collection, edit in (('attributes', lambda s: s['attributes'][0]['values'][0]['value'].update(number=185)),
                                 ('relations', lambda s: s['relations'].append({
                                     'relationId': 'REL-SECOND', 'fromEntity': 'C-BETA',
                                     'toEntity': 'C-ALPHA', 'kind': 'trusts'}))):
            with self.subTest(collection=collection):
                extra = deepcopy(source)
                extra['relations'][0]['kind'] = 'trusts'
                edit(extra)
                direct = _base_plan(source, operation='canon_relation_proposal', actor='app',
                                    parameters=valid['parameters'], impact={}, proposed=extra)
                with self.assertRaises(StructureError) as caught:
                    _write_plan(self.root, direct)
                self.assertEqual(caught.exception.code,
                                 'CONTENT_PROPOSAL_REQUIRED' if collection == 'attributes'
                                 else 'RELATION_PROPOSAL_SCOPE_INVALID')

    def test_relation_impact_uses_registered_chapter_and_prose_evidence(self):
        source = load_structure(self.root)
        chapter = {'chapterId': 'CH-RELATION', 'title': '关系章', 'displayOrdinal': 1,
                   'placement': {'scope': 'volume', 'volumeId': source['volumes'][0]['volumeId']},
                   'status': 'stable', 'filePath': '正文/关系章.md'}
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': chapter['chapterId'],
                                          'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        source['references'].append({'sourceId': chapter['chapterId'], 'targetId': 'C-ALPHA', 'kind': 'uses'})
        source['fragments'].append({'fragmentId': 'SF-REL-DEPENDENT', 'ownerId': 'C-ALPHA',
                                    'text': '甲乙维系约定。', 'order': 6, 'contentRole': 'fact',
                                    'contentBindings': ['AT-LIFE'],
                                    'dependsOn': ['REL-ALPHA-BETA']})
        from scripts.core.prose_binding import seal_binding
        source['revision'] = max(1, source['revision'])
        chapter['proseBinding'] = seal_binding(source['projectId'], chapter['chapterId'], chapter['filePath'],
            state='managed', generation=source['revision'], expected_content_digest='sha256:'+digest('甲守护乙。'.encode()))
        write_json(self.root/'.govel/structure.json', source)
        write_json(self.root / '.govel/workflow.json',
                   {'approvals': {chapter['chapterId']: {'approved': True}}, 'artifacts': {}})
        prose = self.root / chapter['filePath']
        prose.parent.mkdir(exist_ok=True)
        prose.write_text('甲守护乙。', encoding='utf-8')
        register_evidence(self.root, chapterId=chapter['chapterId'], fileHash=digest(prose.read_bytes()),
                          sourceHash=snapshot_hash(load_structure(self.root)), requestId='relation-evidence',
                          citations=[{'start': 0, 'end': 5, 'text': '甲守护乙。',
                                      'targetIds': ['REL-ALPHA-BETA'], 'kind': 'depends_on',
                                      'reason': '句子依赖关系类型。'}])
        historical_evidence = (self.root / '.govel/source-prose-evidence.json').read_bytes()
        plan = self.propose()
        self.assertIn(chapter['chapterId'], plan['impact']['approvalInvalidations'])
        self.assertIn('SF-REL-DEPENDENT', plan['impact']['reviewFragmentIds'])
        self.assertEqual(plan['impact']['proseEvidence'][0]['matchedTargetIds'], ['REL-ALPHA-BETA'])
        original = prose.read_bytes()
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(prose.read_bytes(), original)
        self.assertEqual(query_evidence(self.root)['items'][0]['status'], 'needs_review')
        self.assertEqual((self.root / '.govel/source-prose-evidence.json').read_bytes(), historical_evidence)
        self.assertFalse(read_json(self.root / '.govel/workflow.json')['approvals'][chapter['chapterId']]['approved'])

    def test_commit_boundary_rechecks_handcrafted_plan_diff(self):
        plan = self.propose()
        path = self.root / '.govel/plans' / (plan['operationId'] + '.json')
        altered = read_json(path)
        altered['proposedStructure']['entities'][0]['summary'] = 'unreviewed'
        altered['planHash'] = _plan_hash({key: value for key, value in altered.items()
                                         if key != 'planHash'})
        write_json(path, altered)
        with self.assertRaises(StructureError) as caught:
            commit_plan(self.root, operation_id=plan['operationId'], plan_hash=altered['planHash'], approved=True)
        self.assertEqual(caught.exception.code, 'CANON_PROPOSAL_REQUIRED')
        self.assertNotIn('summary', load_structure(self.root)['entities'][0])

    def test_cli_and_mcp_use_same_resolver(self):
        repo = Path(__file__).resolve().parents[1]
        before = (self.root / '.govel/structure.json').read_bytes()
        cli = subprocess.run([sys.executable, str(repo / 'scripts/novelctl.py'), 'project', 'source',
                              'relation-resolve', '--project-root', str(self.root),
                              '--relation-id', 'REL-ALPHA-BETA'], cwd=repo, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'resolve_source_relation_target', 'arguments': {
                'projectRoot': str(self.root), 'relationId': 'REL-ALPHA-BETA'}}}
        mcp = subprocess.run([sys.executable, str(repo / 'scripts/mcp_server.py')],
                             input=json.dumps(request) + '\n', cwd=repo, capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        actual = json.loads(json.loads(mcp.stdout.splitlines()[-1])['result']['content'][0]['text'])
        self.assertEqual(json.loads(cli.stdout), actual)
        self.assertEqual(before, (self.root / '.govel/structure.json').read_bytes())

    def test_cli_and_mcp_share_proposal_preview(self):
        repo = Path(__file__).resolve().parents[1]
        resolved = self.resolve()
        old = resolved['record']
        new = {**old, 'kind': 'trusts'}
        before = (self.root / '.govel/structure.json').read_bytes()
        cli = subprocess.run([sys.executable, str(repo / 'scripts/novelctl.py'), 'project', 'source',
                              'relation-proposal-preview', '--project-root', str(self.root),
                              '--session-ref', resolved['sessionRef'], '--expected-old-record', json.dumps(old),
                              '--new-record', json.dumps(new), '--reason', '作者确认', '--evidence', '[]',
                              '--evidence-mode', 'author_declaration', '--approval-actor', 'author',
                              '--approval-evidence', '本地声明'],
                             cwd=repo, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preview_source_relation_change', 'arguments': {
                'projectRoot': str(self.root), 'sessionRef': resolved['sessionRef'],
                'expectedOldRecord': old, 'newRecord': new, 'reason': '作者确认',
                'evidence': [], 'evidenceMode': 'author_declaration',
                'approvalActor': 'author', 'approvalEvidence': '本地声明'}}}
        mcp = subprocess.run([sys.executable, str(repo / 'scripts/mcp_server.py')],
                             input=json.dumps(request, ensure_ascii=False) + '\n', cwd=repo,
                             capture_output=True, text=True)
        self.assertEqual(mcp.returncode, 0, mcp.stderr)
        actual = json.loads(json.loads(mcp.stdout.splitlines()[-1])['result']['content'][0]['text'])
        cli_plan = json.loads(cli.stdout)
        self.assertEqual(cli_plan['parameters'], actual['parameters'])
        self.assertEqual(cli_plan['impact'], actual['impact'])
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
