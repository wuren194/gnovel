from tests.fixtures_workflow import evidenced_slots
"""Current source lifecycle across all knowledge retrieval modes."""
import unittest

from scripts import state_cache
from scripts.core.artifacts import sha256_json, sha256_text
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.context_health import assess_context, query_context_health
from scripts.core.context_builder import build_station_context
from scripts.core.context_transport import semantic_view
from scripts.core.continuity import continuity_preview, complete_continuity
from scripts.core.finalization import finalize_chapter, reconcile_workflow
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.project import toolbox_manifest
from scripts.core.prose_binding import OWNED_CLEAN, observe_chapter
from scripts.core.review_artifacts import STAGE_B_SLOTS, AUXILIARY_GATES
from scripts.core.source_plan import outline_binding
from scripts.core.structure_store import load_structure, save_structure
from scripts.core.task_prepare import prepare_task
from scripts.core.mechanical_evidence import run_diagnostics
from scripts.core.workflow import approve_artifacts
from tests.current_lifecycle import CurrentLifecycleFixture
from tests.fixtures_workflow import register_and_approve


class Round7CloseoutTests(unittest.TestCase):
    def test_current_lifecycles_with_title_continuity_and_next_chapter(self):
        helper = CurrentLifecycleFixture()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        for schema in (5,):
            for mode in ('off', 'broad', 'curated'):
                with self.subTest(schema=schema, knowledgeMode=mode):
                    root, cid = helper.read_ready_schema5(f's{schema}-{mode}')
                    second = helper.materialize(root, title='下一章')['chapterId']
                    structure = load_structure(root)
                    if schema == 5:
                        # This is a tiny lifecycle fixture, not a production-length chapter.
                        # The real runner now requires an explicit authored source contract.
                        plan = next(f for f in structure['fragments'] if f['ownerId'] == cid and f['contentRole'] == 'plan')
                        plan['chapterContract'] = {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'targetRange': [10, 50]}
                    structure['entities'].append({'entityId': 'C-CLOSEOUT', 'entityType': 'character',
                        'name': '合成人物', 'aliases': [], 'status': 'active', 'summary': '守门人', 'sourceRefs': []})
                    save_structure(root, structure)
                    workflow = read_json(root/'.govel/workflow.json')
                    workflow['approvedChapterIds'] = [cid, second]
                    write_json(root/'.govel/workflow.json', workflow)
                    chapter = next(c for c in structure['chapters'] if c['chapterId'] == cid)
                    state_cache.rebuild(root)
                    design = prepare_task(root, 'design', knowledge_mode=mode)
                    self.assertEqual(design['task'], 'planner')
                    self.assertNotIn('CHAPTER_REQUIRED', {r['code'] for r in design['blocking']})
                    binding = outline_binding(root, load_structure(root), chapter)
                    register_and_approve(root, cid, 'stage_a_review', {'decision': 'adopt',
                        'activeSeats': list(range(1, 11)), binding['field']: binding['hash'],
                        'consensus': [], 'disagreements': [], 'openItems': []})
                    advance_chapter_status(root, cid, 'planned')
                    writer = prepare_task(root, 'write', chapter_id=cid, pov_character='C-CLOSEOUT',
                        chapter_goal='关系推进', knowledge_mode=mode)
                    self.assertTrue(writer['ok'], writer['blocking'])
                    context = build_station_context(str(root), cid, task_type='writer',
                        pov_character='C-CLOSEOUT', chapter_goal='关系推进', knowledge_mode=mode,
                        view='station', read_only=True)
                    self.assertEqual(context['contextHash'], writer['context']['contextHash'])
                    model, proof = semantic_view(context)
                    self.assertTrue(model['povScoped'])
                    self.assertEqual(proof['dependencyManifest']['knowledgeMode'], mode)
                    self.assertEqual(proof['contextHash'], context['contextHash'])
                    if mode == 'off': self.assertEqual(model['referenceKnowledge'], [])
                    health = query_context_health(root, chapterId=cid, chapterGoal='关系推进',
                        povCharacter='C-CLOSEOUT', knowledgeMode=mode, expectedHash=context['contextHash'])
                    self.assertTrue(health['ready'], health)
                    changed = query_context_health(root, chapterId=cid, chapterGoal='关系推进',
                        povCharacter='C-CLOSEOUT', knowledgeMode='broad' if mode == 'off' else 'off',
                        expectedHash=context['contextHash'])
                    self.assertFalse(changed['ready'])
                    prose = '合成人物将钥匙放回桌上，确认登记本中的签名。'
                    register_and_approve(root, cid, 'candidate_prose', {'text': prose})
                    advance_chapter_status(root, cid, 'draft')
                    diagnostic = run_diagnostics(root, chapter_id=cid, mode='plot', intensity_level=1,
                                                 register=True, advance_lifecycle=True)
                    workflow = read_json(root/'.govel/workflow.json')
                    approve_artifacts(workflow, cid, artifact_hashes={
                        'diagnostics': diagnostic['artifactSha256']}, approval_mode='manual')
                    write_json(root/'.govel/workflow.json', workflow)
                    self.assertTrue(diagnostic['evidence']['passed'])
                    if schema == 5:
                        self.assertEqual(diagnostic['evidence']['chapterContract']['targetRange'], [10, 50])
                    editor = prepare_task(root, 'revise', chapter_id=cid, knowledge_mode=mode)
                    self.assertEqual(editor['task'], 'editor')
                    self.assertTrue(editor['ok'], editor['blocking'])
                    register_and_approve(root, cid, 'stage_b_review', {'verdict': 'POLISH_READY',
                        'slots': evidenced_slots(prose),
                        'findings': [], 'finalProseSha256': sha256_text(prose)})
                    state_cache.rebuild(root)
                    state, design = state_cache.project_projections(root)
                    register_and_approve(root, cid, 'state_candidate', {'baseStateSha256': sha256_json(state),
                        'designSha256': sha256_json(design), binding['field']: binding['hash'],
                        'finalProseSha256': sha256_text(prose), 'changes': []})
                    register_and_approve(root, cid, 'title_candidates', {'candidates': [
                        {'candidateId': 'T-1', 'title': '留下钥匙', 'patternId': 'action',
                         'voice': 'active', 'reason': '合成标题'}]})
                    self.assertFalse(preflight_finalization(root, cid, title_candidate_id='T-WRONG').ready)
                    token = next(c for c in load_structure(root)['chapters'] if c['chapterId'] == cid)['proseBinding']['generation']
                    result = finalize_chapter(root, cid, title_candidate_id='T-1', binding_generation=token)
                    self.assertEqual(result['code'], 'CHAPTER_FINALIZED')
                    current = load_structure(root)
                    landed = next(c for c in current['chapters'] if c['chapterId'] == cid)
                    self.assertEqual(observe_chapter(root, current, landed)['state'], OWNED_CLEAN)
                    self.assertEqual((root/chapter['filePath']).read_text(), prose)
                    self.assertFalse(prepare_task(root, 'write', chapter_id=second, knowledge_mode=mode)['readiness']['authorizationReady'])
                    batch = continuity_preview(root, cid)
                    complete_continuity(root, cid, batch_hash=batch['batchHash'], approved=True, confirmation={
                        'decisions': [{'itemId': r['itemId'], 'resolution': 'no_change', 'reason': '合成确认', 'evidence': []}
                                      for r in batch['batch']['items']],
                        'summary': {'resolution': 'not_required', 'reason': '合成无需摘要'}})
                    action = reconcile_workflow(root, persist=False)['nextAction']
                    self.assertEqual(action['chapterId'], second)
                    self.assertEqual(prepare_task(root, 'continue', knowledge_mode=mode)['task'], 'planner')

    def test_removed_manifest_fields_had_no_product_consumers(self):
        manifest = toolbox_manifest()
        self.assertNotIn('artifactRoot', manifest['execution'])
        self.assertNotIn('formula', manifest['classification'])

    def test_arc_edges_keep_same_chapter_conflicts_blocking(self):
        from copy import deepcopy
        from tests.test_context_builder import context_source, build
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context_source(root)
            context = build(root)
            row = next(r for r in context['memoryPack']['semantic'] if r['field'] == 'chapterArcLink')
            other = deepcopy(row)
            other['value']['chapterId'] = 'CH-OTHER'
            context['memoryPack']['semantic'].append(other)
            self.assertNotIn('CONTEXT_VALUE_CONFLICT', {r['code'] for r in assess_context(context)['issues']})
            same = deepcopy(row)
            same['value']['role'] = 'secondary'
            context['memoryPack']['semantic'].append(same)
            self.assertIn('CONTEXT_VALUE_CONFLICT', {r['code'] for r in assess_context(context)['issues']})


if __name__ == '__main__':
    unittest.main()
