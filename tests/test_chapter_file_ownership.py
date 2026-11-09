"""Current binding ownership, target-slot safety and finalization race regressions."""
from contextlib import contextmanager
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.core import refactor
from scripts.core.chapter_receipt import chapter_receipt
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.locks import project_lock
from scripts.core.prose_binding import seal_binding
from scripts.core.refactor_paths import check_target, RefactorPathError
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.workflow import WorkflowError
from tests.fixtures_refactor import CHAPTER_ONE, refactor_project
from tests.fixtures_workflow import review_ready_project
from tests.test_phase2_ownership_cutover import review_ready_binding_project, NEW_PROSE


class LocatorOnlyOwnershipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); refactor_project(self.root)
        source = load_structure(self.root)
        chapter = next(c for c in source['chapters'] if c['chapterId'] == CHAPTER_ONE)
        chapter['status'] = 'draft'
        chapter['proseBinding'] = seal_binding(source['projectId'], CHAPTER_ONE, chapter['filePath'],
            state='unbound', generation=source['revision'], expected_content_digest=None)
        self.source_rel = chapter['filePath']; (self.root/self.source_rel).unlink()
        write_json(self.root/'.gnovel/structure.json', source)

    def _relocate(self, target):
        return refactor.preview_relocate_chapter_paths(self.root, chapter_id=CHAPTER_ONE, file_path=target)

    def test_missing_source_empty_target_is_locator_only(self):
        row = check_target(self.root, self.source_rel, '正文/EMPTY.md')
        self.assertEqual(row['action'], 'locator_only'); self.assertFalse(row['sourceExists'])
        plan = self._relocate('正文/EMPTY.md')
        self.assertEqual(plan['bindingTransitions'][0]['kind'], 'move_unbound')

    def test_existing_target_is_blocked_without_changes(self):
        path = self.root/'正文/B.md'; path.write_text('DO NOT OVERWRITE')
        before = (self.root/'.gnovel/structure.json').read_bytes()
        with self.assertRaises(RefactorPathError): check_target(self.root, self.source_rel, '正文/B.md')
        with self.assertRaises(ValueError): self._relocate('正文/B.md')
        self.assertEqual(path.read_text(), 'DO NOT OVERWRITE')
        self.assertEqual((self.root/'.gnovel/structure.json').read_bytes(), before)

    def test_directory_target_is_blocked(self):
        (self.root/'正文/DIR.md').mkdir()
        with self.assertRaises(RefactorPathError) as caught: check_target(self.root, self.source_rel, '正文/DIR.md')
        self.assertEqual(caught.exception.code, 'REFACTOR_TARGET_IS_DIRECTORY')

    def test_broken_symlink_is_blocked_without_following_it(self):
        outside = self.root.parent/'outside-secret.md'; link = self.root/'正文/LINK.md'; link.symlink_to(outside)
        with self.assertRaises(RefactorPathError): check_target(self.root, self.source_rel, '正文/LINK.md')
        self.assertFalse(outside.exists()); self.assertTrue(link.is_symlink())

    def test_hardlink_target_is_an_occupied_entry(self):
        original = self.root/'正文/original.md'; original.write_text('shared bytes')
        link = self.root/'正文/HARD.md'; os.link(original, link)
        with self.assertRaises(RefactorPathError) as caught: check_target(self.root, self.source_rel, '正文/HARD.md')
        self.assertEqual(caught.exception.code, 'REFACTOR_TARGET_EXISTS')
        self.assertEqual(original.read_text(), 'shared bytes')

    def test_canonical_equivalent_sibling_is_blocked(self):
        (self.root/'正文/Casebook.md').write_text('sibling bytes')
        with self.assertRaises(RefactorPathError) as caught: check_target(self.root, self.source_rel, '正文/casebook.md')
        self.assertIn(caught.exception.code, {'REFACTOR_TARGET_EXISTS', 'REFACTOR_PATH_CANONICAL_COLLISION'})


class FinalizationOwnershipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve(); self.root = self.base/'initial'
        fixture = review_ready_project(self.root, chapter_count=1)
        self._select(fixture['chapterIds'][0])

    def _select(self, cid):
        self.chapter_id = cid
        source = load_structure(self.root); self.chapter = next(c for c in source['chapters'] if c['chapterId'] == cid)
        self.prose_path = self.root/self.chapter['filePath']
        self.generation = chapter_receipt(self.root, cid)['bindingGeneration']

    def _managed(self):
        self.root = self.base/'managed'
        source = review_ready_binding_project(self.root, state='managed', prose=NEW_PROSE)
        self._select(source['chapters'][0]['chapterId'])

    def _finalize(self):
        return finalize_chapter(self.root, self.chapter_id, skip_title=True, binding_generation=self.generation)

    def _snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}

    @contextmanager
    def _inject_after_locked_preflight(self, foreign):
        from scripts.core import derived_views
        original = derived_views.derived_outputs
        def racing(*args, **kwargs):
            self.prose_path.write_bytes(foreign)
            return original(*args, **kwargs)
        with patch('scripts.core.derived_views.derived_outputs', racing): yield

    def test_empty_target_finalizes_with_caller_generation(self):
        self.assertFalse(self.prose_path.exists())
        self.assertTrue(preflight_finalization(self.root, self.chapter_id, skip_title=True).ready)
        self.assertEqual(self._finalize()['code'], 'CHAPTER_FINALIZED')
        self.assertTrue(self.prose_path.is_file())

    def test_foreign_bytes_at_unbound_target_fail_closed(self):
        sentinel = b'DO NOT OVERWRITE'; self.prose_path.write_bytes(sentinel); before = self._snapshot()
        report = preflight_finalization(self.root, self.chapter_id, skip_title=True)
        self.assertFalse(report.ready)
        self.assertIn('PROSE_TARGET_NOT_OWNED', {b['code'] for b in report.blockers})
        with self.assertRaises(WorkflowError) as caught: self._finalize()
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')
        self.assertEqual(self._snapshot(), before)

    def test_same_bytes_as_candidate_do_not_grant_ownership(self):
        self.prose_path.write_text('他把钥匙放回桌上，合上了登记本。')
        report = preflight_finalization(self.root, self.chapter_id, skip_title=True)
        self.assertIn('PROSE_TARGET_NOT_OWNED', {b['code'] for b in report.blockers})

    def test_external_edit_to_managed_target_is_refused(self):
        self._managed(); self.prose_path.write_text('外部改过的正文。'); before = self._snapshot()
        with self.assertRaises(WorkflowError) as caught: self._finalize()
        self.assertEqual(caught.exception.code, 'PROSE_EXTERNAL_MODIFICATION')
        self.assertEqual(self._snapshot(), before)

    def test_managed_refinalization_is_independent_of_history(self):
        self._managed()
        self.assertEqual(read_json(self.root/'.gnovel/workflow.json').get('continuity', {}), {})
        result = self._finalize()
        self.assertEqual(result['code'], 'CHAPTER_FINALIZED')
        self.assertEqual(self.prose_path.read_text(), NEW_PROSE)
        self.assertGreater(chapter_receipt(self.root, self.chapter_id)['bindingGeneration'], self.generation)

    def test_target_appearing_after_locked_preflight_is_preserved(self):
        foreign = b'raced in after locked preflight'
        source_before = (self.root/'.gnovel/structure.json').read_bytes()
        workflow_before = (self.root/'.gnovel/workflow.json').read_bytes()
        with self._inject_after_locked_preflight(foreign), self.assertRaises((StructureError, WorkflowError)) as caught: self._finalize()
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertEqual(self.prose_path.read_bytes(), foreign)
        self.assertEqual((self.root/'.gnovel/structure.json').read_bytes(), source_before)
        self.assertEqual((self.root/'.gnovel/workflow.json').read_bytes(), workflow_before)
        self.assertFalse(list((self.root/'.gnovel/transactions').glob('*.json')))

    def test_managed_target_edited_after_locked_preflight_is_preserved(self):
        self._managed(); foreign = b'external H2 after preflight'
        with self._inject_after_locked_preflight(foreign), self.assertRaises((StructureError, WorkflowError)) as caught: self._finalize()
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertEqual(self.prose_path.read_bytes(), foreign)
        self.assertFalse(list((self.root/'.gnovel/transactions').glob('*.json')))

    def test_target_appearing_before_lock_is_revalidated(self):
        sentinel = b'raced in before lock'
        @contextmanager
        def racing(root):
            self.prose_path.write_bytes(sentinel)
            with project_lock(root): yield
        with patch('scripts.core.finalization.project_lock', racing), self.assertRaises(WorkflowError) as caught: self._finalize()
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')
        self.assertEqual(self.prose_path.read_bytes(), sentinel)

    def test_malformed_continuity_is_rejected_independently_of_empty_target(self):
        path = self.root/'.gnovel/workflow.json'; workflow = read_json(path)
        workflow['continuity'] = {self.chapter_id: {'chapterId': self.chapter_id, 'status': 'complete'}}
        write_json(path, workflow)
        before = self._snapshot()
        report = preflight_finalization(self.root, self.chapter_id, skip_title=True)
        self.assertIn('CONTINUITY_STATE_INVALID', {b['code'] for b in report.blockers})
        with self.assertRaises(WorkflowError): self._finalize()
        self.assertEqual(self._snapshot(), before)

    def test_another_chapter_cannot_claim_the_same_locator(self):
        source = load_structure(self.root); chapter = dict(source['chapters'][0]); chapter['chapterId'] = 'CH-ANOTHER'
        chapter['displayOrdinal'] = 2; chapter['displayCode'] = 'CH002'
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': chapter['chapterId'], 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        write_json(self.root/'.gnovel/structure.json', source)
        self.prose_path.write_text('另一章的正文'); before = self._snapshot()
        with self.assertRaises((StructureError, WorkflowError)): self._finalize()
        self.assertEqual(self._snapshot(), before)
