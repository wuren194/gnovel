"""External-review byte provenance and original request identity; adoption stays closed."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.finalization import (accept_external_workflow, finalize_chapter,
                                        preview_accept_external_modification)
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_requests import query_request_status, renew_request
from scripts.core.structure_commands import _plan_hash, commit_plan
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_current import create_current_project
from tests.fixtures_workflow import review_ready_project


class ExternalReviewMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'book'
        fixture = review_ready_project(self.root)
        chapter = fixture['structure']['chapters'][0]
        self.cid = chapter['chapterId']
        finalize_chapter(self.root, self.cid, skip_title=True, binding_generation=chapter['proseBinding']['generation'])
        self.chapter = next(c for c in load_structure(self.root)['chapters'] if c['chapterId'] == self.cid)
        self.path = self.root/self.chapter['filePath']; self.h1 = self.path.read_bytes()
        self.h2 = self.h1 + '\n他检查了门锁。\n'.encode()
        self.path.write_bytes(self.h2)

    def preview(self, request_id='external-request'):
        return preview_accept_external_modification(self.root, chapter_id=self.cid, request_id=request_id)

    def stored(self, result):
        return read_json(self.root/f".govel/plans/{result['operationId']}.json")

    def test_h1_h2_are_real_frozen_bytes_and_readonly_guards(self):
        report = self.preview(); plan = self.stored(report); material = plan['externalReviewMaterials']
        self.assertEqual(material['availability'], 'complete')
        self.assertEqual((self.root/material['H1']['path']).read_bytes(), self.h1)
        self.assertEqual((self.root/material['H2']['path']).read_bytes(), self.h2)
        self.assertNotEqual((self.root/material['H2']['path']).stat().st_ino, self.path.stat().st_ino)
        self.assertIn('检查了门锁', (self.root/material['diff']['path']).read_text())
        self.assertEqual(material['semanticReview']['status'], 'not_performed')
        self.assertFalse(material['adoptionExecutable'])
        guarded = {row['path'] for row in plan['externalReviewGuards']}
        self.assertIn(self.chapter['filePath'], guarded)
        self.assertFalse(guarded & {row['path'] for row in plan['writeSet']})
        self.assertFalse(report['execution']['executable'])
        self.assertNotIn('nextAction', report)
        before = self.path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            commit_plan(self.root, operation_id=report['operationId'], plan_hash=report['planHash'], approved=True)
        self.assertEqual(caught.exception.code, 'APPROVAL_CAPABILITY_REQUIRED')
        self.assertEqual(self.path.read_bytes(), before)

    def test_registered_artifact_missing_can_use_exact_committed_transaction_image(self):
        workflow_path = self.root/'.govel/workflow.json'; workflow = read_json(workflow_path)
        workflow['artifacts'][self.cid].pop('candidate_prose')
        write_json(workflow_path, workflow)
        plan = self.stored(self.preview())
        self.assertEqual(plan['externalReviewMaterials']['H1']['provenance']['kind'], 'committed_transaction_image')
        self.assertEqual((self.root/plan['externalReviewMaterials']['H1']['path']).read_bytes(), self.h1)

    def test_missing_h1_never_fabricates_diff_or_enables_adoption(self):
        root = self.root.parent/'synthetic-no-history'; fixture = create_current_project(root, chapter_count=1)
        cid = fixture['chapterIds'][0]; path = root/fixture['structure']['chapters'][0]['filePath']
        path.write_text('external replacement')
        report = preview_accept_external_modification(root, chapter_id=cid, request_id='no-h1')
        plan = read_json(root/f".govel/plans/{report['operationId']}.json")
        material = plan['externalReviewMaterials']
        self.assertEqual(material['availability'], 'missing_accepted_bytes')
        self.assertIsNone(material['H1']); self.assertNotIn('diff', material)
        self.assertIn('accepted_H1_bytes', material['missingPrerequisites'])
        self.assertFalse(material['adoptionExecutable'])
        self.assertEqual((root/material['H2']['path']).read_text(), 'external replacement')

    def test_original_request_replays_same_plan_and_rejects_h3_or_swapped_object(self):
        first = self.preview(); replay = self.preview()
        self.assertEqual((first['operationId'], first['planHash']), (replay['operationId'], replay['planHash']))
        self.assertEqual(query_request_status(self.root, requestId='external-request')['operationState'], 'prepared')
        replacement = self.root/'replacement.md'; replacement.write_bytes(self.h2); os.replace(replacement, self.path)
        status = query_request_status(self.root, requestId='external-request')
        self.assertEqual(status['operationState'], 'stale')
        self.assertIn('TRANSACTION_GUARD_DRIFT', status['staleReasons']); self.assertFalse(status['renewable'])
        with self.assertRaises(StructureError): self.preview()
        self.path.write_bytes(b'H3')
        with self.assertRaises(StructureError): renew_request(self.root, requestId='external-request')
        self.assertEqual(self.path.read_bytes(), b'H3')
        material = self.stored(first)['externalReviewMaterials']
        self.assertEqual((self.root/material['H2']['path']).read_bytes(), self.h2)

    def test_expiry_only_renews_original_operation_and_frozen_inputs(self):
        first = self.preview(); plan = self.stored(first); expiry = plan['expiresAtEpoch']
        material = deepcopy(plan['externalReviewMaterials'])
        with patch('scripts.core.source_requests.time.time', return_value=expiry+1):
            renewed = renew_request(self.root, requestId='external-request')
        self.assertEqual(renewed['operationId'], first['operationId'])
        self.assertNotEqual(renewed['planHash'], first['planHash'])
        self.assertEqual(self.stored(renewed)['externalReviewMaterials'], material)
        self.assertEqual(self.path.read_bytes(), self.h2)
        self.assertFalse(renewed['execution']['executable'])

    def test_frozen_material_tampering_cannot_be_renewed_or_rebound(self):
        report = self.preview(); plan = self.stored(report); material = plan['externalReviewMaterials']
        (self.root/material['H2']['path']).write_bytes(b'changed frozen data')
        status = query_request_status(self.root, requestId='external-request')
        self.assertEqual(status['operationState'], 'stale'); self.assertFalse(status['renewable'])
        with self.assertRaises(StructureError): self.preview()
        self.assertEqual(self.path.read_bytes(), self.h2)

    def test_same_request_different_chapter_is_rejected(self):
        self.preview()
        with self.assertRaises(StructureError) as caught:
            preview_accept_external_modification(self.root, chapter_id='chapter-OTHER', request_id='external-request')
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')

    def test_unknown_result_between_material_freeze_and_plan_reuses_original_h2(self):
        from scripts.core import refactor
        original = refactor._refactor_plan
        with patch.object(refactor, '_refactor_plan', side_effect=RuntimeError('after material freeze')):
            with self.assertRaises(RuntimeError): self.preview()
        report = self.preview()
        self.assertEqual(self.stored(report)['externalReviewMaterials']['observedContentDigest'],
                         'sha256:' + hashlib.sha256(self.h2).hexdigest())
        self.assertEqual(len(list((self.root/'.govel/plans').glob('OP-*.external'))), 1)

    def test_h3_after_material_freeze_never_replaces_uncommitted_original_request(self):
        with patch('scripts.core.refactor._refactor_plan', side_effect=RuntimeError('after material freeze')):
            with self.assertRaises(RuntimeError): self.preview()
        self.path.write_bytes(b'H3')
        with self.assertRaises(StructureError) as caught: self.preview()
        self.assertEqual(caught.exception.code, 'EXTERNAL_REVIEW_MATERIAL_CONFLICT')
        self.assertEqual(self.path.read_bytes(), b'H3')

    def test_external_invalidation_preserves_historical_finalization(self):
        workflow = read_json(self.root/'.govel/workflow.json')
        before = deepcopy(workflow['approvals'][self.cid])
        after = accept_external_workflow(workflow, self.cid)
        self.assertEqual(after['approvals'][self.cid]['finalized'], before['finalized'])
        self.assertEqual(after['approvals'][self.cid]['titleDecision'], before['titleDecision'])
        self.assertEqual(after.get('continuity'), workflow.get('continuity'))
        self.assertFalse(after['approvals'][self.cid]['approved'])


if __name__ == '__main__': unittest.main()
