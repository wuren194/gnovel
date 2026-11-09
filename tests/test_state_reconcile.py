from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import state_cache
from scripts.core.artifacts import envelope, sha256_json, sha256_text
from scripts.core.finalization import finalize_chapter, reconcile_workflow
from scripts.core.jsonio import read_json, write_json
from scripts.core.workflow import approve_artifacts, default_workflow, record_artifact
from tests.fixtures_current import create_schema5_project


class WorkflowReconcileTests(unittest.TestCase):
    def test_reconcile_reads_approved_chapter_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_schema5_project(root, chapter_count=1)
            workflow = default_workflow()
            workflow["approvedChapterIds"] = fixture["chapterIds"]
            write_json(root / ".govel/workflow.json", workflow)
            result = reconcile_workflow(root)
            self.assertEqual(result["code"], "WORKFLOW_RECONCILED")
            self.assertEqual(result["nextAction"]["action"], "continuity")

    def test_reconcile_selects_earliest_unstable_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_schema5_project(root, chapter_count=2)
            structure = read_json(root / ".govel/structure.json")
            structure["chapters"][0]["status"] = "review"
            write_json(root / ".govel/structure.json", structure)
            state_cache.rebuild(root)
            workflow = default_workflow()
            workflow["approvedChapterIds"] = fixture["chapterIds"]
            write_json(root / ".govel/workflow.json", workflow)
            result = reconcile_workflow(root)
            self.assertEqual(result["code"], "WORKFLOW_RECONCILED")
            self.assertEqual(result["nextAction"]["chapterId"], fixture["chapterIds"][0])
            self.assertEqual(result["nextAction"]["action"], "editor")
    def test_reconcile_reports_multiple_in_flight_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_schema5_project(root, chapter_count=2)
            structure = read_json(root / ".govel/structure.json")
            structure["chapters"][0]["status"] = "draft"
            structure["chapters"][1]["status"] = "review"
            write_json(root / ".govel/structure.json", structure)
            workflow = default_workflow()
            workflow["approvedChapterIds"] = fixture["chapterIds"]
            write_json(root / ".govel/workflow.json", workflow)
            result = reconcile_workflow(root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "WORKFLOW_AMBIGUOUS")
            self.assertTrue(any(item["code"] == "MULTIPLE_CHAPTERS_IN_FLIGHT" for item in result["conflicts"]))

    def test_reconcile_reports_chapter_state_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = create_schema5_project(root, chapter_count=1)
            chapter_id = fixture["chapterIds"][0]
            workflow = default_workflow()
            workflow["approvedChapterIds"] = [chapter_id]
            workflow["chapterStates"][chapter_id] = {"status": "draft"}
            write_json(root / ".govel/workflow.json", workflow)
            result = reconcile_workflow(root)
            self.assertFalse(result["ok"])
            self.assertTrue(any(item["code"] == "CHAPTER_STATE_DIVERGED" for item in result["conflicts"]))


class FinalizeChapterTests(unittest.TestCase):
    def ready(self, root):
        from tests.fixtures_workflow import review_ready_project, register_and_approve
        from scripts.core.chapter_receipt import chapter_receipt
        cid = review_ready_project(root, chapter_count=1)['chapterIds'][0]
        source = read_json(root/'.govel/structure.json')
        register_and_approve(root, cid, 'title_candidates', {'candidates': [{'candidateId': 'title-1', 'title': '定稿标题'}]})
        receipt = chapter_receipt(root, cid, title_candidate_id='title-1')
        self.assertTrue(receipt['readyToFinalize'], receipt['blockers'])
        return cid, source['chapters'][0], receipt['bindingGeneration']

    def test_finalize_commits_owned_prose_and_pending_continuity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); cid, chapter, generation = self.ready(root)
            result = finalize_chapter(root, cid, title_candidate_id='title-1', binding_generation=generation)
            self.assertEqual(result['code'], 'CHAPTER_FINALIZED')
            updated = read_json(root/'.govel/structure.json')
            self.assertEqual(updated['chapters'][0]['status'], 'stable')
            self.assertEqual(updated['chapters'][0]['title'], '定稿标题')
            self.assertEqual((root/chapter['filePath']).read_text(), '他把钥匙放回桌上，合上了登记本。')
            self.assertEqual(updated['chapters'][0]['proseBinding']['state'], 'managed')
            self.assertEqual(reconcile_workflow(root)['nextAction']['action'], 'continuity')
            self.assertFalse((root/'设定/追踪/回写记录.md').exists())

    def test_finalize_failure_restores_all_project_files(self):
        from scripts.core import write_sets
        from tests.fixtures_transaction import assert_preserved_rollback
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); cid, chapter, generation = self.ready(root)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            def fail(phase, relative=None):
                if phase == 'write' and relative == chapter['filePath']:
                    raise RuntimeError('injected')
            with patch.object(write_sets, 'checkpoint', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    finalize_chapter(root, cid, title_candidate_id='title-1', binding_generation=generation)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            assert_preserved_rollback(self, root, before, after)
            self.assertFalse((root/chapter['filePath']).exists())
            self.assertEqual(finalize_chapter(root, cid, title_candidate_id='title-1', binding_generation=generation)['code'], 'CHAPTER_FINALIZED')

    def test_finalization_preserves_metadata_changed_after_preflight(self):
        from scripts.core import derived_views
        from scripts.core.structure_store import StructureError
        for relative in ('.govel/structure.json', '.govel/workflow.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve(); cid, chapter, generation = self.ready(root)
                path = root / relative
                original = derived_views.derived_outputs
                changed = []

                def racing_outputs(*args, **kwargs):
                    data = read_json(path)
                    if relative.endswith('structure.json'):
                        data['bookName'] = '作者在预检后修正书名'
                        data['revision'] += 1
                    else:
                        data['nextAction'] = {'chapterId': cid, 'action': 'editor',
                            'reason': '作者在预检后要求复查', 'needsApproval': True}
                    write_json(path, data)
                    changed.append(path.read_bytes())
                    return original(*args, **kwargs)

                with patch.object(derived_views, 'derived_outputs', side_effect=racing_outputs):
                    with self.assertRaises(StructureError) as caught:
                        finalize_chapter(root, cid, title_candidate_id='title-1', binding_generation=generation)
                self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
                self.assertEqual(path.read_bytes(), changed[0])
                self.assertFalse((root / chapter['filePath']).exists())
                self.assertEqual(list((root / '.govel/transactions').glob('*.json')), [])


if __name__ == '__main__': unittest.main()
