from __future__ import annotations

from scripts.core.project import current_schema_version

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.artifacts import sha256_text
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_impact import dependency_edges, review_input_hash, source_impact
from scripts.core.source_model import snapshot_hash
from scripts.core.source_plan import chapter_plan
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.source_time import story_time_states
from scripts.core.structure_store import load_structure, validate_structure
from scripts.core.structure_transaction import commit
from scripts.core.workflow import default_workflow, require_approved_artifact, WorkflowError
from tests.fixtures_source import single_source_chapter, source_fixture
from tests.fixtures_workflow import evidenced_slots, register_and_approve


def add_plan(source, chapter_id, dependencies):
    chapter = single_source_chapter(source, chapter_id,
        volume_id=source['volumes'][0]['volumeId'], ordinal=len(source['chapters']) + 1)
    source['chapters'].append(chapter)
    source['chapterArcLinks'].append({'chapterId': chapter_id,
        'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
    attribute_id, fragment_id = 'attribute-' + chapter_id, 'plan-' + chapter_id
    source['attributes'].append({'attributeId': attribute_id, 'ownerId': chapter_id,
                                'fieldId': 'field-PLAN', 'values': []})
    fragment = {'fragmentId': fragment_id, 'ownerId': chapter_id,
                'text': '按登记的事件时间安排本章。', 'order': 1, 'contentRole': 'plan',
                'contentBindings': [attribute_id], 'dependsOn': dependencies,
                'chapterContract': {'targetRange': [1, 1000],
                    'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}}
    source['fragments'].append(fragment)
    return chapter, fragment


def time_plan_source():
    """Current-schema version of the frozen F1 reproduction, with an unrelated plan."""
    source = source_fixture()
    source['fieldDefinitions'] = [{'fieldId': 'field-PLAN', 'key': 'plot.plan',
        'label': '计划', 'valueType': 'topic', 'appliesTo': ['chapter']}]
    source['attributes'], source['fragments'] = [], []
    source['entities'].append({'entityId': 'event-REL', 'entityType': 'event', 'name': '三日后事件',
        'timeSpec': {'kind': 'relative', 'anchorId': 'time-START', 'offset': 3}})
    add_plan(source, 'chapter-TIME', ['event-REL'])
    add_plan(source, 'chapter-OTHER', ['character-BETA'])
    validate_structure(source)
    return source


def entity(source, entity_id):
    return next(row for row in source['entities'] if row['entityId'] == entity_id)


def move_anchor(source, anchor_id, ordinal):
    after = deepcopy(source)
    entity(after, anchor_id)['timeOrdinal'] = ordinal
    validate_structure(after)
    return after


class TimeDependencyClosureTests(unittest.TestCase):
    def test_shared_span_consumes_time_fields_without_sibling_title_or_locator(self):
        from scripts.core.author_decisions import _scope_hashes
        before = time_plan_source()
        first, later = before['chapters']
        first.update(storyTimeStartId='time-START', storyTimeEndId='time-START', status='stable')
        later.update(storyTimeStartId='time-START', storyTimeEndId='time-LATER')
        validate_structure(before)
        for field, value in [('title', '后来重新选择的标题'), ('filePath', '正文/moved.md'), ('status', 'draft')]:
            after = deepcopy(before); after['chapters'][1][field] = value
            with self.subTest(field=field):
                self.assertEqual(chapter_plan(before, first['chapterId'])['sourcePlanHash'],
                                 chapter_plan(after, first['chapterId'])['sourcePlanHash'])
                self.assertEqual(review_input_hash(before, 'plan-chapter-TIME'), review_input_hash(after, 'plan-chapter-TIME'))
                self.assertEqual(_scope_hashes(before, [first['chapterId']]), _scope_hashes(after, [first['chapterId']]))
                self.assertNotIn(first['chapterId'], source_impact(before, after)['approvalInvalidations'])
        changed = deepcopy(before)
        changed['chapters'][1]['storyTimeEndId'] = 'event-REL'
        self.assertNotEqual(chapter_plan(before, first['chapterId'])['sourcePlanHash'],
                            chapter_plan(changed, first['chapterId'])['sourcePlanHash'])
        self.assertIn(first['chapterId'], source_impact(before, changed)['approvalInvalidations'])
        # An explicit reference to the sibling still consumes its authored title.
        before['references'].append({'sourceId': first['chapterId'], 'targetId': later['chapterId'], 'kind': 'reference'})
        changed = deepcopy(before); changed['chapters'][1]['title'] = '显式依赖已改变'
        self.assertNotEqual(chapter_plan(before, first['chapterId'])['sourcePlanHash'],
                            chapter_plan(changed, first['chapterId'])['sourcePlanHash'])

    def assert_plan_changes(self, before, after, affected):
        for chapter in before['chapters']:
            chapter_id = chapter['chapterId']
            with self.subTest(chapter=chapter_id):
                prior = chapter_plan(before, chapter_id)['sourcePlanHash']
                current = chapter_plan(after, chapter_id)['sourcePlanHash']
                if chapter_id in affected:
                    self.assertNotEqual(prior, current)
                else:
                    self.assertEqual(prior, current)
        impact = source_impact(before, after)
        self.assertEqual(impact['staleCandidates'], sorted(affected))
        self.assertEqual(impact['approvalInvalidations'], sorted(affected))
        return impact

    def test_frozen_relative_repro_changes_time_plan_and_review_binding(self):
        before = time_plan_source()
        after = move_anchor(before, 'time-START', 2)
        self.assertEqual(before['schemaVersion'], current_schema_version())
        self.assertEqual(story_time_states(before)['event-REL']['lower'], 3)
        self.assertEqual(story_time_states(after)['event-REL']['lower'], 5)
        self.assertIn(('time-START', 'event-REL', 'time_spec'), dependency_edges(before))
        self.assertIn('time-START', {row['id'] for row in chapter_plan(before, 'chapter-TIME')['records']})
        impact = self.assert_plan_changes(before, after, {'chapter-TIME'})
        dependents = {row['recordId']: row for row in impact['registeredDependents']}
        self.assertEqual(dependents['event-REL']['via'],
                         {'fromId': 'time-START', 'toId': 'event-REL', 'kind': 'time_spec'})
        self.assertEqual(dependents['plan-chapter-TIME']['causeId'], 'time-START')
        self.assertEqual(impact['reviewFragmentIds'], ['plan-chapter-TIME'])
        self.assertNotEqual(review_input_hash(before, 'plan-chapter-TIME'),
                            review_input_hash(after, 'plan-chapter-TIME'))
        self.assertEqual(review_input_hash(before, 'plan-chapter-OTHER'),
                         review_input_hash(after, 'plan-chapter-OTHER'))

    def test_range_both_endpoints_and_open_bounds_change_dependent_plan(self):
        cases = [
            ({'afterId': 'time-START', 'beforeId': 'time-LATER'}, 'time-START', 2, (2, 10)),
            ({'afterId': 'time-START', 'beforeId': 'time-LATER'}, 'time-LATER', 12, (0, 12)),
            ({'afterId': 'time-START', 'beforeId': None}, 'time-START', 2, (2, None)),
            ({'beforeId': 'time-LATER'}, 'time-LATER', 12, (None, 12)),
        ]
        for bounds, anchor_id, ordinal, expected in cases:
            with self.subTest(bounds=bounds, anchor=anchor_id):
                before = time_plan_source()
                entity(before, 'event-REL')['timeSpec'] = {'kind': 'range', **bounds}
                validate_structure(before)
                after = move_anchor(before, anchor_id, ordinal)
                state = story_time_states(after)['event-REL']
                self.assertEqual((state['lower'], state['upper']), expected)
                self.assertIn((anchor_id, 'event-REL', 'time_spec'), dependency_edges(before))
                impact = self.assert_plan_changes(before, after, {'chapter-TIME'})
                self.assertEqual(impact['reviewFragmentIds'], ['plan-chapter-TIME'])
                self.assertNotEqual(review_input_hash(before, 'plan-chapter-TIME'),
                                    review_input_hash(after, 'plan-chapter-TIME'))

    def test_chain_through_relative_time_anchor_range_and_event(self):
        before = time_plan_source()
        before['entities'].extend([
            {'entityId': 'time-REL', 'entityType': 'time_anchor', 'name': '次日',
             'timeSpec': {'kind': 'relative', 'anchorId': 'time-START', 'offset': 1}},
            {'entityId': 'event-RANGE', 'entityType': 'event', 'name': '区间事件',
             'timeSpec': {'kind': 'range', 'afterId': 'time-REL', 'beforeId': 'time-LATER'}},
        ])
        entity(before, 'event-REL')['timeSpec']['anchorId'] = 'event-RANGE'
        add_plan(before, 'chapter-ANCHOR', ['time-REL'])
        add_plan(before, 'chapter-RANGE', ['event-RANGE'])
        validate_structure(before)
        after = move_anchor(before, 'time-START', 2)
        old_time, new_time = story_time_states(before)['event-REL'], story_time_states(after)['event-REL']
        self.assertEqual((old_time['lower'], old_time['upper']), (4, 13))
        self.assertEqual((new_time['lower'], new_time['upper']), (6, 13))
        impact = self.assert_plan_changes(before, after, {'chapter-TIME', 'chapter-ANCHOR', 'chapter-RANGE'})
        dependents = {row['recordId']: row for row in impact['registeredDependents']}
        for origin, target in [('time-START', 'time-REL'), ('time-REL', 'event-RANGE'), ('event-RANGE', 'event-REL')]:
            self.assertEqual(dependents[target]['via'],
                             {'fromId': origin, 'toId': target, 'kind': 'time_spec'})
            self.assertEqual(dependents[target]['causeId'], 'time-START')
        self.assertEqual(impact['reviewFragmentIds'], ['plan-chapter-ANCHOR', 'plan-chapter-RANGE', 'plan-chapter-TIME'])
        records = {row['id'] for row in chapter_plan(before, 'chapter-TIME')['records']}
        self.assertTrue({'time-START', 'time-REL', 'time-LATER', 'event-RANGE', 'event-REL'} <= records)

    def test_time_edges_compose_with_existing_dependency_kinds(self):
        for route in ('reference_value', 'valid_during', 'storyTimeStartId', 'storyTimeEndId',
                      'event_presentation', 'chapter_reference'):
            with self.subTest(route=route):
                before = time_plan_source()
                chapter, fragment = before['chapters'][0], before['fragments'][0]
                fragment['dependsOn'] = []
                if route == 'reference_value':
                    before['fieldDefinitions'].append({'fieldId': 'field-EVENT', 'key': 'plot.event',
                        'label': '关联事件', 'valueType': 'reference', 'appliesTo': ['chapter']})
                    before['attributes'].append({'attributeId': 'attribute-EVENT', 'ownerId': 'chapter-TIME',
                        'fieldId': 'field-EVENT', 'values': [{'valueId': 'value-EVENT', 'value': 'event-REL'}]})
                    fragment['contentBindings'].append('attribute-EVENT')
                elif route == 'valid_during':
                    fragment['validDuring'] = {'fromId': 'event-REL'}
                elif route in ('storyTimeStartId', 'storyTimeEndId'):
                    chapter[route] = 'event-REL'
                elif route == 'event_presentation':
                    before['presentations'].append({'presentationId': 'presentation-TIME', 'eventId': 'event-REL',
                        'chapterId': 'chapter-TIME', 'kind': 'reveal', 'order': 1})
                else:
                    before['references'].append({'sourceId': 'chapter-TIME', 'targetId': 'event-REL', 'kind': 'event'})
                validate_structure(before)
                after = move_anchor(before, 'time-START', 2)
                impact = self.assert_plan_changes(before, after, {'chapter-TIME'})
                dependents = {row['recordId']: row for row in impact['registeredDependents']}
                target = 'plan-chapter-TIME' if route in ('reference_value', 'valid_during') else 'chapter-TIME'
                self.assertEqual(dependents[target]['causeId'], 'time-START')
                self.assertIn('time-START', {row['id'] for row in chapter_plan(before, 'chapter-TIME')['records']})

    def test_relative_zero_cycle_remains_bounded_and_preserves_declared_dependencies(self):
        before = time_plan_source()
        before['entities'].append({'entityId': 'event-CYCLE', 'entityType': 'event', 'name': '循环事件',
            'timeSpec': {'kind': 'relative', 'anchorId': 'event-REL', 'offset': -3}})
        entity(before, 'event-REL')['timeSpec']['anchorId'] = 'event-CYCLE'
        validate_structure(before)
        self.assertEqual(story_time_states(before)['event-REL']['status'], 'unknown')
        records = [row['id'] for row in chapter_plan(before, 'chapter-TIME')['records']]
        self.assertEqual(records.count('event-CYCLE'), 1)
        self.assertEqual(records.count('event-REL'), 1)
        after = deepcopy(before)
        entity(after, 'event-CYCLE')['name'] = '重新说明循环事件'
        validate_structure(after)
        self.assert_plan_changes(before, after, {'chapter-TIME'})
        self.assertNotEqual(review_input_hash(before, 'plan-chapter-TIME'),
                            review_input_hash(after, 'plan-chapter-TIME'))

    def test_committed_anchor_change_invalidates_only_dependent_approved_artifacts(self):
        cases = [
            ({'kind': 'relative', 'anchorId': 'time-START', 'offset': 3}, 'time-START', 2, (5, 5)),
            ({'kind': 'range', 'afterId': 'time-START', 'beforeId': 'time-LATER'}, 'time-START', 2, (2, 10)),
            ({'kind': 'range', 'afterId': 'time-START', 'beforeId': 'time-LATER'}, 'time-LATER', 12, (0, 12)),
        ]
        for time_spec, anchor_id, ordinal, expected in cases:
            with self.subTest(spec=time_spec, anchor=anchor_id), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source = time_plan_source()
                entity(source, 'event-REL')['timeSpec'] = time_spec
                source['entities'].append({'entityId': 'event-CHAIN', 'entityType': 'event', 'name': '后续事件',
                    'timeSpec': {'kind': 'relative', 'anchorId': 'event-REL', 'offset': 1}})
                add_plan(source, 'chapter-CHAIN', ['event-CHAIN'])
                source['fieldDefinitions'].append({'fieldId': 'field-ANCHOR', 'key': 'time.evidence',
                    'label': '日期依据', 'valueType': 'topic', 'appliesTo': ['time_anchor']})
                source['attributes'].append({'attributeId': 'attribute-ANCHOR', 'ownerId': anchor_id,
                    'fieldId': 'field-ANCHOR', 'values': []})
                quote = '起点日期已经核定。'
                source['fragments'].append({'fragmentId': 'fact-ANCHOR-EVIDENCE', 'ownerId': anchor_id,
                    'text': quote, 'order': 1, 'contentRole': 'fact', 'contentBindings': ['attribute-ANCHOR']})
                validate_structure(source)
                write_json(root / '.govel/structure.json', source)
                workflow = default_workflow()
                chapter_ids = [chapter['chapterId'] for chapter in source['chapters']]
                workflow['approvedChapterIds'] = chapter_ids
                write_json(root / '.govel/workflow.json', workflow)
                text = '他核对了行程，随后关上门。'
                for chapter_id in chapter_ids:
                    plan_hash = chapter_plan(source, chapter_id)['sourcePlanHash']
                    register_and_approve(root, chapter_id, 'stage_a_review', {
                        'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'sourcePlanHash': plan_hash,
                        'consensus': [], 'disagreements': [], 'openItems': []})
                    register_and_approve(root, chapter_id, 'candidate_prose', {'text': text})
                    register_and_approve(root, chapter_id, 'stage_b_review', {
                        'verdict': 'POLISH_READY', 'sourcePlanHash': plan_hash,
                        'finalProseSha256': sha256_text(text), 'slots': evidenced_slots(text), 'findings': []})
                workflow_before = read_json(root / '.govel/workflow.json')
                approved = {(cid, kind): require_approved_artifact(workflow_before, cid, kind)
                            for cid in chapter_ids for kind in ('stage_a_review', 'candidate_prose', 'stage_b_review')}
                source_bytes = (root / '.govel/structure.json').read_bytes()
                workflow_bytes = (root / '.govel/workflow.json').read_bytes()
                preview = preview_proposal_batch(root, sourceHash=snapshot_hash(source), items=[{
                    'kind': 'entity', 'recordId': anchor_id,
                    'patch': [{'op': 'set', 'path': ['timeOrdinal'], 'value': ordinal}],
                    'reason': '核对锚点日期', 'evidence': [{'fragmentId': 'fact-ANCHOR-EVIDENCE', 'quote': quote}],
                }], requestId='time-anchor-update', approvalActor='author', approvalEvidence='合成测试批准')
                self.assertEqual(preview['impact']['approvalInvalidations'], ['chapter-CHAIN', 'chapter-TIME'])
                self.assertEqual((root / '.govel/structure.json').read_bytes(), source_bytes)
                self.assertEqual((root / '.govel/workflow.json').read_bytes(), workflow_bytes)
                receipt = commit(root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                                 approved=True, actor='test')
                self.assertEqual(receipt['code'], 'STRUCTURE_COMMITTED')
                after = load_structure(root)
                self.assertEqual(after['revision'], source['revision'] + 1)
                state = story_time_states(after)['event-REL']
                self.assertEqual((state['lower'], state['upper']), expected)
                self.assert_plan_changes(source, after, {'chapter-TIME', 'chapter-CHAIN'})
                workflow_after = read_json(root / '.govel/workflow.json')
                for chapter_id in ('chapter-TIME', 'chapter-CHAIN'):
                    self.assertFalse(workflow_after['approvals'][chapter_id]['approved'])
                    self.assertEqual(workflow_after['approvals'][chapter_id]['invalidatedBy'],
                                     'source_dependencies_changed')
                    for kind in ('stage_a_review', 'candidate_prose', 'stage_b_review'):
                        self.assertEqual(workflow_after['artifacts'][chapter_id][kind]['invalidatedBy'],
                                         'source_dependencies_changed')
                        with self.assertRaises(WorkflowError) as caught:
                            require_approved_artifact(workflow_after, chapter_id, kind)
                        self.assertEqual(caught.exception.code, 'ARTIFACT_NOT_REGISTERED')
                for key in ('approvals', 'artifacts'):
                    self.assertEqual(workflow_after[key]['chapter-OTHER'], workflow_before[key]['chapter-OTHER'])
                for kind in ('stage_a_review', 'candidate_prose', 'stage_b_review'):
                    self.assertEqual(require_approved_artifact(workflow_after, 'chapter-OTHER', kind),
                                     approved['chapter-OTHER', kind])
                review = read_json(root / '.govel/source-review.json')['items']
                self.assertEqual(set(review), {'plan-chapter-TIME', 'plan-chapter-CHAIN'})
                for fragment_id, item in review.items():
                    self.assertEqual(item['status'], 'pending')
                    self.assertEqual(item['inputHash'], review_input_hash(after, fragment_id))


if __name__ == '__main__':
    unittest.main()
