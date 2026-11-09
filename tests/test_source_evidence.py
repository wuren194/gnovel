from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.jsonio import write_json, read_json
from scripts.core.source_changes import preview_source_change
from scripts.core.source_evidence import register_evidence, query_evidence, EVIDENCE_PATH
from scripts.core.source_model import snapshot_hash
from scripts.core.structure_store import StructureError, validate_structure, load_structure
from scripts.core.structure_transaction import commit
from scripts.core.write_sets import digest
from tests.fixtures_source import source_fixture
from tests.fixtures_content_proposal import attribute_value_proposal


class ProseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture()
        cid = 'CH-ALPHA'
        self.source['chapters'] = [{'chapterId': cid, 'title': '守门', 'displayOrdinal': 1,
            'placement': {'scope': 'volume', 'volumeId': self.source['volumes'][0]['volumeId']},
            'status': 'stable', 'filePath': '正文/守门.md'}]
        self.source['chapterArcLinks'] = [{'chapterId': cid, 'arcId': self.source['arcs'][0]['arcId'], 'role': 'primary'}]
        validate_structure(self.source)
        write_json(self.root / '.gnovel/structure.json', self.source)
        write_json(self.root / '.gnovel/workflow.json', {'approvals': {cid: {'approved': True}}, 'artifacts': {}})
        self.prose = self.root / '正文/守门.md'; self.prose.parent.mkdir()
        self.prose.write_bytes('他一米九的身形挡住了城门。\r\n天色已暗。'.encode())

    def args(self, kind='depends_on', key='one'):
        return {'chapterId': 'CH-ALPHA', 'requestId': key, 'fileHash': digest(self.prose.read_bytes()),
                'sourceHash': snapshot_hash(load_structure(self.root)), 'citations': [{
                'start': 0, 'end': 13, 'text': self.prose.read_bytes().decode()[:13],
                'targetIds': ['AT-HEIGHT'], 'kind': kind, 'reason': '明确使用成年身高。'}]}

    def plan(self):
        return attribute_value_proposal(self.root, 'AT-HEIGHT', 185)

    def test_dependency_invalidation_has_exact_location_without_prose_write(self):
        original = self.prose.read_bytes()
        register_evidence(self.root, **self.args())
        plan = self.plan()
        self.assertEqual(plan['impact']['approvalInvalidations'], ['CH-ALPHA'])
        self.assertEqual(plan['impact']['proseEvidence'][0]['locator']['start'], 0)
        commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')
        self.assertEqual(self.prose.read_bytes(), original)
        self.assertEqual(query_evidence(self.root)['items'][0]['status'], 'needs_review')
        self.assertFalse(read_json(self.root / '.gnovel/workflow.json')['approvals']['CH-ALPHA']['approved'])

    def test_mention_is_candidate_and_does_not_invalidate_approval(self):
        register_evidence(self.root, **self.args(kind='mentions'))
        plan = self.plan()
        self.assertEqual(plan['impact']['approvalInvalidations'], [])
        self.assertEqual(plan['impact']['proseEvidence'][0]['kind'], 'mentions')

    def test_old_hash_quote_and_source_rejected(self):
        args = self.args()
        for bad in ({**args, 'fileHash': 'old'}, {**args, 'sourceHash': 'old'},
                    {**args, 'citations': [{**args['citations'][0], 'text': '错误引文'}]}):
            with self.subTest(bad=bad), self.assertRaises(StructureError): register_evidence(self.root, **bad)
        self.assertFalse((self.root / EVIDENCE_PATH).exists())

    def test_prose_edit_stales_locator_and_prevents_committing_old_preview(self):
        register_evidence(self.root, **self.args())
        plan = self.plan()
        self.prose.write_text('全新稿件。')
        row = query_evidence(self.root)['items'][0]
        self.assertFalse(row['locatorCurrent'])
        self.assertEqual(row['status'], 'stale_location')
        with self.assertRaises(StructureError):
            commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')
        self.assertEqual(load_structure(self.root)['revision'], 0)

    def test_changed_ledger_invalidates_frozen_impact(self):
        plan = self.plan()
        register_evidence(self.root, **self.args())
        with self.assertRaises(StructureError):
            commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def test_idempotent_replay_and_no_whole_chapter_claim(self):
        args = self.args()
        first = register_evidence(self.root, **args)
        self.assertFalse(first['coverage']['wholeChapterReviewed'])
        self.assertTrue(register_evidence(self.root, **args)['alreadyRecorded'])
        self.assertEqual(len(query_evidence(self.root)['items']), 1)
        with self.assertRaises(StructureError): register_evidence(self.root, **{**args, 'actor': 'other'})

    def test_changed_field_definition_invalidates_indirect_evidence(self):
        register_evidence(self.root, **self.args())
        self.source['fieldDefinitions'][0]['description'] = '成年身高，以赤足为准。'
        write_json(self.root / '.gnovel/structure.json', self.source)
        self.assertEqual(query_evidence(self.root)['items'][0]['status'], 'needs_review')
