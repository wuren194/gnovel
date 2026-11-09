from __future__ import annotations

from tests.fixtures_workflow import evidenced_slots

import tempfile
import unittest
from pathlib import Path

from scripts import state_cache
from scripts.core.artifacts import file_sha256, sha256_json, sha256_text
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.continuity import continuity_preview, complete_continuity
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import write_json
from scripts.core.review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, load_structure
from scripts.core.workflow import default_workflow
from tests.fixtures_workflow import register_and_approve
from tests.test_source_plan import planning_source


class SourceFinalizationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_phase2_ownership_cutover import review_ready_binding_project
        from scripts.core.chapter_receipt import chapter_receipt
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prose = '甲把手按在城门上，确认门闩已经落下。'
        source = review_ready_binding_project(self.root, prose=self.prose)
        self.cid = source['chapters'][0]['chapterId']
        self.prose_path = self.root/source['chapters'][0]['filePath']
        self.generation = chapter_receipt(self.root, self.cid)['bindingGeneration']

    def test_source_finalize_writes_prose_without_reading_or_rewrite_markdown(self):
        self.assertTrue(preflight_finalization(self.root, self.cid, skip_title=True).ready)
        result = finalize_chapter(self.root, self.cid, skip_title=True, binding_generation=self.generation)
        self.assertEqual(result['code'], 'CHAPTER_FINALIZED')
        self.assertIsNone(result['settingRewriteCandidate'])
        self.assertEqual(self.prose_path.read_text(), self.prose)
        self.assertEqual(list((self.root/'设定').rglob('*.md')), [])
        self.assertFalse((self.root / '规划/结构索引.md').exists())
        preview = continuity_preview(self.root, self.cid)
        self.assertTrue(preview['batch']['sourceMode'])
        self.assertEqual(preview['batch']['items'], [])
        confirmation = {'decisions': [], 'summary': {'resolution': 'not_required',
                                                      'reason': '本章无需刷新全书摘要。'}}
        completed = complete_continuity(self.root, self.cid, batch_hash=preview['batchHash'],
                                        confirmation=confirmation, approved=True)
        self.assertTrue(completed['readyForContinuation'])
        self.assertEqual(list((self.root/'设定').rglob('*.md')), [])

    def test_source_state_changes_must_use_source_transaction(self):
        from scripts.core.artifact_pack import pack_artifact
        from scripts.core.jsonio import read_json
        before = (self.root/'.gnovel/workflow.json').read_bytes()
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='state_candidate', payload={
                'changes': [{'op': 'append', 'collection': 'decisions',
                             'value': {'decisionId': 'D-OLD', 'decision': '越过源事务'}}]})
        self.assertEqual(caught.exception.code, 'SOURCE_STATE_CHANGE_REQUIRES_TRANSACTION')
        self.assertEqual((self.root/'.gnovel/workflow.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
