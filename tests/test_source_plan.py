from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.core.artifacts import envelope
from scripts.core.chapter_state import advance_chapter_status
from scripts.core.jsonio import write_json, read_json
from scripts.core.source_plan import chapter_plan, query_chapter_plan
from scripts.core.source_impact import source_impact
from scripts.core.structure_store import StructureError, validate_structure, load_structure
from scripts.core.structure_commands import preview_insert_chapter
from scripts.core.workflow import default_workflow, record_artifact, approve_artifacts, WorkflowError
from scripts.core.review_artifacts import validate_stage_a, ReviewArtifactError
from tests.fixtures_source import single_source_chapter, source_fixture


def planning_source(root: Path):
    source = source_fixture()
    cid = 'CH-PLAN'
    source['chapters'].append({'chapterId': cid, 'title': '守门', 'displayOrdinal': 1,
        'placement': {'scope': 'volume', 'volumeId': source['volumes'][0]['volumeId']},
        'filePath': '正文/守门.md', 'status': 'planning_required'})
    source['chapterArcLinks'].append({'chapterId': cid, 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
    source['fieldDefinitions'].append({'fieldId': 'FD-PLAN', 'key': 'plot.plan', 'label': '剧情计划',
                                     'valueType': 'topic', 'appliesTo': ['chapter', 'project', 'volume']})
    source['attributes'].append({'attributeId': 'AT-PLAN', 'ownerId': cid, 'fieldId': 'FD-PLAN', 'values': []})
    source['fragments'].append({'fragmentId': 'SF-PLAN', 'ownerId': cid, 'text': '甲守城门。', 'order': 1,
                               'contentRole': 'plan', 'contentBindings': ['AT-PLAN'], 'dependsOn': ['AT-HEIGHT'],
                               'chapterContract': {'targetRange': [1, 10000], 'mode': 'plot', 'intensityLevel': 1,
                                   'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}})
    validate_structure(source)
    write_json(root / '.gnovel/structure.json', source)
    return source, source['chapters'][0]


class SourcePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.chapter = planning_source(self.root)
        self.cid = self.chapter['chapterId']

    def approve(self, *, legacy=False):
        payload = {'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': []}
        payload['outlineSha256' if legacy else 'sourcePlanHash'] = chapter_plan(self.source, self.cid)['sourcePlanHash']
        from tests.fixtures_workflow import assessed_review_payload
        payload = assessed_review_payload(self.source, self.cid, 'stage_a_review', payload)
        artifact = envelope('stage_a_review', payload, project_id=self.source['projectId'], chapter_id=self.cid)
        path = self.root / '工作台/stage-a.json'; write_json(path, artifact)
        workflow = default_workflow(); workflow['approvedChapterIds'] = [self.cid]
        record_artifact(workflow, self.cid, artifact, structure=self.source, artifact_path=str(path),
                        project_id=self.source['projectId'], project_root=self.root)
        approve_artifacts(workflow, self.cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.gnovel/workflow.json', workflow)
        return path

    def test_stage_a_advances_without_any_outline_export(self):
        self.approve()
        self.assertFalse((self.root / '规划').exists())
        initial = chapter_plan(self.source, self.cid)['sourcePlanHash']
        result = advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(result['evidence'], {'sourcePlanHash': initial})
        self.assertEqual(chapter_plan(load_structure(self.root), self.cid)['sourcePlanHash'], initial)
        self.assertFalse((self.root / '规划').exists())

    def test_unrelated_character_edit_does_not_change_plan_but_dependency_does(self):
        initial = chapter_plan(self.source, self.cid)['sourcePlanHash']
        self.source['entities'][1]['name'] = '另一人物'
        self.assertEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], initial)
        self.source['attributes'][0]['values'][0]['value']['number'] = 185
        self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], initial)

    def test_new_global_plan_is_selected_and_invalidates_affected_approval(self):
        before = deepcopy(self.source)
        self.source['attributes'].append({'attributeId': 'AT-GLOBAL', 'ownerId': self.source['projectId'], 'fieldId': 'FD-PLAN', 'values': []})
        self.source['fragments'].append({'fragmentId': 'SF-GLOBAL', 'ownerId': self.source['projectId'], 'text': '全书守门事件必须登记代价。',
            'order': 0, 'contentRole': 'plan', 'contentBindings': ['AT-GLOBAL']})
        validate_structure(self.source)
        impact = source_impact(before, self.source)
        self.assertIn(self.cid, impact['approvalInvalidations'])

    def test_project_fact_only_affects_chapter_with_registered_dependency(self):
        self.source['attributes'].append({'attributeId': 'AT-GLOBAL', 'ownerId': self.source['projectId'],
                                          'fieldId': 'FD-PLAN', 'values': []})
        self.source['fragments'].append({'fragmentId': 'SF-GLOBAL', 'ownerId': self.source['projectId'],
            'text': '城门朝东。', 'order': 0, 'contentRole': 'fact', 'contentBindings': ['AT-GLOBAL']})
        before = deepcopy(self.source)
        self.source['fragments'][-1]['text'] = '城门朝西。'
        self.assertEqual(chapter_plan(before, self.cid)['sourcePlanHash'], chapter_plan(self.source, self.cid)['sourcePlanHash'])
        self.assertNotIn(self.cid, source_impact(before, self.source)['approvalInvalidations'])
        for source in (before, self.source):
            next(row for row in source['fragments'] if row['fragmentId'] == 'SF-PLAN')['dependsOn'].append('SF-GLOBAL')
            validate_structure(source)
        self.assertIn(self.cid, source_impact(before, self.source)['approvalInvalidations'])

    def test_whole_character_card_reference_invalidates_only_the_referencing_chapter(self):
        self.source['chapters'].append(single_source_chapter(self.source, 'CH-OTHER',
                                      volume_id=self.source['volumes'][0]['volumeId'], ordinal=2))
        self.source['chapterArcLinks'].append({'chapterId': 'CH-OTHER', 'arcId': self.source['arcs'][0]['arcId'],
                                               'role': 'primary'})
        self.source['references'].append({'sourceId': self.cid, 'targetId': 'C-ALPHA', 'kind': 'character'})
        validate_structure(self.source)
        changed = deepcopy(self.source)
        changed['fragments'][3]['text'] = '他曾独自守过城门。'
        validate_structure(changed)
        self.assertEqual(source_impact(self.source, changed)['approvalInvalidations'], [self.cid])
        records = {row['id'] for row in chapter_plan(self.source, self.cid)['records']}
        self.assertIn('AT-LIFE', records)
        self.assertIn('SF-LIFE', records)

    def test_unrelated_fact_edit_preserves_approval_bytes(self):
        from scripts.core.source_impact import review_outputs
        self.approve()
        changed = deepcopy(self.source)
        changed['fragments'][3]['text'] = '他曾独自守过城门。'
        impact = source_impact(self.source, changed)
        self.assertNotIn(self.cid, impact['approvalInvalidations'])
        self.assertNotIn('.gnovel/workflow.json', review_outputs(self.root, self.source, changed, impact))

    def test_old_markdown_approval_cannot_be_reused_as_source_approval(self):
        with self.assertRaises(ReviewArtifactError) as error:
            self.approve(legacy=True)
        self.assertEqual(error.exception.code, 'REVIEW_PLAN_BINDING_INVALID')

    def test_changed_dependency_or_changed_artifact_blocks_stage_a(self):
        path = self.approve()
        data = read_json(path)
        # A valid replacement envelope still has no approval for its new hash.
        replacement = envelope('stage_a_review', {**data['payload'], 'decision': 'modify'},
                               project_id=self.source['projectId'], chapter_id=self.cid)
        write_json(path, replacement)
        with self.assertRaises(WorkflowError) as error: advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(error.exception.code, 'ARTIFACT_APPROVAL_HASH_MISMATCH')
        write_json(path, data)
        self.source['fragments'][-1]['text'] = '甲离开城门。'
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(WorkflowError) as error: advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(error.exception.code, 'STAGE_A_OUTLINE_MISMATCH')

    def test_query_pagination_covers_all_inputs_and_same_plan_hash(self):
        expected = chapter_plan(self.source, self.cid)
        ids, cursor = [], None
        while True:
            page = query_chapter_plan(self.root, chapterId=self.cid, limit=2, cursor=cursor)
            self.assertEqual(page['sourcePlanHash'], expected['sourcePlanHash'])
            ids.extend(row['id'] for row in page['items']); cursor = page['nextCursor']
            if not cursor: break
        self.assertEqual(ids, [row['id'] for row in expected['records']])

    def test_ambiguous_dual_binding_is_rejected(self):
        artifact = envelope('stage_a_review', {'decision': 'adopt', 'activeSeats': [4],
            'sourcePlanHash': 'source', 'outlineSha256': 'file'}, project_id=self.source['projectId'], chapter_id=self.cid)
        with self.assertRaises(ReviewArtifactError): validate_stage_a(artifact)

    def test_prepare_uses_source_read_list_and_does_not_demand_cards_or_exports(self):
        from scripts.core.task_prepare import prepare_task
        self.approve()
        with patch('scripts.core.task_prepare._context', return_value={'applicable': False, 'ready': None}), \
             patch('scripts.core.task_prepare._plan', return_value={'complete': True}), \
             patch('scripts.core.tool_plan.build_tool_plan', return_value={'toolActions': [], 'unresolved': []}):
            result = prepare_task(self.root, 'writer', chapter_id=self.cid)
        self.assertTrue(result['readiness']['taskReady'], result['blocking'])
        self.assertTrue(any(row.get('tool') == 'query_source_chapter_plan' for row in result['readList']))
        self.assertFalse(any(row.get('path', '').startswith('设定/') for row in result['readList']))

    def test_publication_preflight_uses_same_source_binding(self):
        from scripts.core.finalization_preflight import preflight_finalization
        self.approve()
        self.source['chapters'][0]['status'] = 'review'
        write_json(self.root / '.gnovel/structure.json', self.source)
        result = preflight_finalization(self.root, self.cid, skip_title=True)
        codes = {row['code'] for row in result.blockers}
        self.assertNotIn('OUTLINE_MISSING', codes)
        self.assertNotIn('STAGE_A_OUTLINE_MISMATCH', codes)
        self.assertIn('FINAL_ARTIFACTS_MISSING', codes, 'source binding must not bypass other publishing gates')
        self.source['fragments'][-1]['text'] = '甲弃门而去。'
        write_json(self.root / '.gnovel/structure.json', self.source)
        changed = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertIn('STAGE_A_OUTLINE_MISMATCH', {row['code'] for row in changed.blockers})

    def test_cache_snapshot_ignores_reading_files_in_source_mode(self):
        from scripts import state_cache
        from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION
        write_json(self.root / '.gnovel/project.json', {'schemaVersion': PROJECT_SCHEMA_VERSION,
            'workflowVersion': WORKFLOW_VERSION, 'projectId': self.source['projectId']})
        before = state_cache._source_snapshots(self.root)
        reading = self.root / '规划/推演阅读.md'; reading.parent.mkdir()
        reading.write_text('旧阅读快照。')
        self.assertEqual(state_cache._source_snapshots(self.root), before)
        reading.unlink()
        self.assertEqual(state_cache._source_snapshots(self.root), before)

    def test_missing_current_plan_cannot_advance_even_with_old_approval(self):
        self.approve()
        self.source['fragments'][-1]['status'] = 'archived'
        write_json(self.root / '.gnovel/structure.json', self.source)
        with self.assertRaises(WorkflowError) as error: advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(error.exception.code, 'SOURCE_CHAPTER_PLAN_MISSING')

    def test_corrupt_source_cache_status_returns_diagnostics(self):
        from scripts import state_cache
        from scripts.core.structure_store import PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION
        write_json(self.root / '.gnovel/project.json', {'schemaVersion': PROJECT_SCHEMA_VERSION,
            'workflowVersion': WORKFLOW_VERSION, 'projectId': self.source['projectId']})
        self.source['fragments'][-1]['contentBindings'] = ['AT-MISSING']
        write_json(self.root / '.gnovel/structure.json', self.source)
        status = state_cache.status(self.root)
        self.assertEqual(status['status'], 'invalid')
        self.assertTrue(status['problems'])


def _cross_volume_source():
    source = source_fixture()
    v1 = source['volumes'][0]['volumeId']
    v2 = 'VOL-CROSS0002'
    source['volumes'].append({'volumeId': v2, 'ordinal': 2, 'title': '第二卷', 'status': 'planned'})
    source['arcs'][0]['volumeIds'] = [v1, v2]
    arc = source['arcs'][0]['arcId']
    source['chapters'] = [single_source_chapter(source, 'CH-AAA', volume_id=v1, ordinal=1),
                          single_source_chapter(source, 'CH-BBB', volume_id=v2, ordinal=2)]
    source['chapterArcLinks'] = [{'chapterId': 'CH-AAA', 'arcId': arc, 'role': 'primary'},
                                 {'chapterId': 'CH-BBB', 'arcId': arc, 'role': 'primary'}]
    source['units'] = [{'unitId': 'UN-PRJ', 'title': '跨卷单元', 'ordinal': 1, 'status': 'active',
                        'placement': {'scope': 'project'},
                        'members': [{'memberType': 'chapter', 'memberId': 'CH-AAA', 'order': 1},
                                    {'memberType': 'chapter', 'memberId': 'CH-BBB', 'order': 2}]}]
    source['fragments'].append({'fragmentId': 'SF-UNIT', 'ownerId': 'UN-PRJ', 'text': '本单元统一的规划。',
                                'order': 1, 'contentBindings': ['AT-LIFE'], 'contentRole': 'plan'})
    validate_structure(source)
    return source, v1


class SchemaThreePlanScopeTests(unittest.TestCase):
    def test_volume_scope_includes_volume(self):
        source, v1 = _cross_volume_source()
        recipe = chapter_plan(source, 'CH-AAA')['recipe']
        self.assertEqual(recipe['kind'], 'chapter-plan-v2')
        self.assertIn(v1, recipe['scopeIds'])
        self.assertIn('UN-PRJ', recipe['scopeIds'])

    def test_project_chapter_has_no_fake_volume(self):
        source = source_fixture()
        source['arcs'][0]['volumeIds'] = []
        source['chapters'] = [single_source_chapter(source, 'CH-PRJ', volume_id=None, ordinal=1)]
        source['chapterArcLinks'] = [{'chapterId': 'CH-PRJ', 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'}]
        validate_structure(source)
        scope = chapter_plan(source, 'CH-PRJ')['recipe']['scopeIds']
        self.assertFalse([sid for sid in scope if sid.startswith('VOL-')])

    def test_cross_volume_unit_fragment_reaches_members(self):
        source, _ = _cross_volume_source()
        record_ids = {row['id'] for row in chapter_plan(source, 'CH-AAA')['records']}
        self.assertIn('SF-UNIT', record_ids)
        self.assertIn('UN-PRJ', record_ids)

    def test_unit_change_changes_plan_hash_unrelated_does_not(self):
        source, _ = _cross_volume_source()
        base = chapter_plan(source, 'CH-AAA')['sourcePlanHash']
        changed = deepcopy(source)
        changed['fragments'][-1]['text'] = '改写后的单元规划。'
        self.assertNotEqual(chapter_plan(changed, 'CH-AAA')['sourcePlanHash'], base)
        # A second unit that does not contain CH-AAA must not perturb its plan.
        unrelated = deepcopy(source)
        unrelated['chapters'].append(single_source_chapter(unrelated, 'CH-CCC',
                                                           volume_id=unrelated['volumes'][1]['volumeId'], ordinal=3))
        unrelated['chapterArcLinks'].append({'chapterId': 'CH-CCC', 'arcId': unrelated['arcs'][0]['arcId'], 'role': 'primary'})
        unrelated['units'].append({'unitId': 'UN-OTHER', 'title': '别单元', 'ordinal': 2, 'status': 'active',
                                   'placement': {'scope': 'project'},
                                   'members': [{'memberType': 'chapter', 'memberId': 'CH-CCC', 'order': 1}]})
        validate_structure(unrelated)
        self.assertEqual(chapter_plan(unrelated, 'CH-AAA')['sourcePlanHash'], base)

    pass

    pass
