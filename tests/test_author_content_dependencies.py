"""Author-content dependency regressions using current-format reviewed changes."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.author_decisions import (decision_validity, preview_decision_change,
                                           query_author_decisions)
from scripts.core.context_builder import build_station_context
from scripts.core.context_health import assess_context
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_changes import preview_source_change
from scripts.core.source_content_proposals import preview_content_change, resolve_content_target
from scripts.core.source_impact import source_impact
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, canonical_json, load_structure, save_structure
from scripts.core.work_voice_profile import AXES, preview_voice_profile
from tests.fixtures_content_proposal import attribute_value_proposal
from tests.test_context_builder import context_source, save_context_source


class AuthorContentDependencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); (self.root/'正文').mkdir()
        self.source = context_source(self.root)
        # Both chapters have an actual plan; BBB does not consume Alpha's facts.
        self.source['attributes'].append({'attributeId': 'AT-B-PLAN', 'ownerId': 'CH-BBB',
                                          'fieldId': 'FD-PLAN', 'values': []})
        self.source['fragments'].append({'fragmentId': 'SF-B-PLAN', 'ownerId': 'CH-BBB',
            'text': '乙等待回信。', 'order': 1, 'contentRole': 'plan', 'contentBindings': ['AT-B-PLAN'], 'dependsOn': []})
        save_context_source(self.root, self.source)
        from scripts.core.workflow import default_workflow
        write_json(self.root/'.govel/workflow.json', default_workflow())

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def draft(self, scope, *, depends=(), supersedes=None):
        plan = preview_decision_change(self.root, action='create',
            expectedRevision=load_structure(self.root)['revision'], reason='合成工程决定',
            statement='该范围内保留已选限制。', rationale='测试依赖', scopeIds=list(scope),
            dependsOn=list(depends), supersedes=supersedes)
        self.commit(plan)
        return plan['parameters']['decisionId']

    def approve(self, did):
        self.commit(preview_decision_change(self.root, action='approve',
            expectedRevision=load_structure(self.root)['revision'], reason='合成明确批准', decisionId=did,
            approvalActor='synthetic-author', approvalEvidence='fixture-only explicit authorization'))
        return did

    def decision(self, scope, *, depends=()):
        return self.approve(self.draft(scope, depends=depends))

    def validity(self, did):
        return query_author_decisions(self.root, decisionId=did)['items'][0]

    def context(self, chapter='CH-AAA'):
        return build_station_context(str(self.root), chapter, task_type='planner', view='station', read_only=True)

    def change_plan(self, fid, text):
        ref = resolve_content_target(self.root, recordId=fid, kind='planning')
        changed = deepcopy(ref['record']); changed['text'] = text
        plan = preview_content_change(self.root, sessionRef=ref['sessionRef'],
            expectedOldRecord=ref['record'], newRecord=changed, reason='合成计划变更', evidence=[],
            approvalActor='synthetic-author', approvalEvidence='fixture-only choice')
        self.commit(plan)
        return plan

    def test_chapter_scope_tracks_plan_and_explicit_fact_dependency_but_other_chapter_stays_current(self):
        first, other = self.decision(['CH-AAA']), self.decision(['CH-BBB'])
        self.commit(attribute_value_proposal(self.root, 'AT-HEIGHT', 185))
        self.assertEqual(self.validity(first)['validity'], 'stale')
        self.assertEqual(self.validity(other)['validity'], 'current')
        context = self.context()
        self.assertIn(first, context['staleDecisionIds']); self.assertFalse(assess_context(context)['ready'])
        self.assertEqual(self.context('CH-BBB')['staleDecisionIds'], [])

    def test_fragment_scope_tracks_its_registered_dependency_values(self):
        decision = self.decision(['SF-PLAN'])
        self.commit(attribute_value_proposal(self.root, 'AT-BETA-HEIGHT', 175))
        self.assertEqual(self.validity(decision)['validity'], 'current')
        self.commit(attribute_value_proposal(self.root, 'AT-HEIGHT', 185))
        self.assertEqual(self.validity(decision)['validity'], 'stale',
                         'SF-PLAN explicitly dependsOn FV-HEIGHT; changed input needs author re-review')

    def test_owner_scope_tracks_its_fragment_explicit_external_dependency(self):
        # Register an actual source edge from Alpha's planning fragment to Beta's value.
        source = load_structure(self.root)
        source['fieldDefinitions'].append({'fieldId': 'FD-ALPHA-PLAN', 'key': 'story.constraint',
            'label': '行动计划', 'valueType': 'topic', 'appliesTo': ['character']})
        source['attributes'].append({'attributeId': 'AT-ALPHA-PLAN', 'ownerId': 'C-ALPHA',
                                    'fieldId': 'FD-ALPHA-PLAN', 'values': []})
        source['fragments'].append({'fragmentId': 'SF-ALPHA-DEPENDENCY', 'ownerId': 'C-ALPHA',
            'text': '甲根据乙的现有条件选择行动。', 'order': 10, 'contentRole': 'plan',
            'contentBindings': ['AT-ALPHA-PLAN'], 'dependsOn': ['FV-BETA-HEIGHT']})
        save_context_source(self.root, source)
        decision = self.decision(['C-ALPHA'])
        self.commit(attribute_value_proposal(self.root, 'AT-BETA-HEIGHT', 175))
        self.assertEqual(self.validity(decision)['validity'], 'stale',
                         'owner-owned fragment has a registered outside dependency')

    def test_content_round_trip_does_not_restore_old_author_approval(self):
        decision = self.decision(['CH-AAA'])
        original = deepcopy(self.validity(decision))
        self.commit(attribute_value_proposal(self.root, 'AT-HEIGHT', 185))
        self.assertEqual(self.validity(decision)['validity'], 'stale')
        self.commit(attribute_value_proposal(self.root, 'AT-HEIGHT', 190))
        returned = self.validity(decision)
        self.assertNotEqual(returned['validity'], 'current',
                            'restoring content is not new approval of an invalidated author decision')
        self.assertEqual(returned['scopeSnapshot'], original['scopeSnapshot'])
        self.assertEqual(returned['approval'], original['approval'])

    def test_scoped_own_content_changes_and_same_calculation_is_pure(self):
        source = load_structure(self.root)
        volume = source['volumes'][0]['volumeId']; arc = source['arcs'][0]['arcId']
        source['units'] = [{'unitId': 'UN-SCOPE', 'title': '条件单元', 'ordinal': 1, 'status': 'active',
            'placement': deepcopy(source['chapters'][0]['placement']),
            'members': [{'memberType': 'chapter', 'memberId': 'CH-AAA', 'order': 1}]}]
        source['planningCandidates'] = [{'candidateId': 'PC-SCOPE', 'title': '候选', 'summary': '',
            'placement': deepcopy(source['chapters'][0]['placement']), 'primaryArcId': arc,
            'secondaryArcIds': [], 'beforeChapterId': None, 'status': 'draft'}]
        save_context_source(self.root, source)
        cases = [(source['projectId'], 'project'), (volume, 'volume'), (arc, 'arc'), ('UN-SCOPE', 'unit'),
                 ('C-ALPHA', 'entity'), ('SF-PLAN', 'fragment'), ('AT-HEIGHT', 'attribute'),
                 ('FV-HEIGHT', 'value'), ('PC-SCOPE', 'candidate')]
        decisions = {scope: self.decision([scope]) for scope, _ in cases}
        source = load_structure(self.root); serialized = canonical_json(source)
        for scope, kind in cases:
            with self.subTest(kind=kind):
                changed = deepcopy(source)
                if kind == 'project': changed['bookName'] += '新'
                elif kind in ('volume', 'arc', 'unit'):
                    collection, key = {'volume': ('volumes', 'volumeId'), 'arc': ('arcs', 'arcId'), 'unit': ('units', 'unitId')}[kind]
                    next(row for row in changed[collection] if row[key] == scope)['title'] += '新'
                elif kind == 'entity': next(row for row in changed['entities'] if row['entityId'] == scope)['name'] += '新'
                elif kind == 'fragment': next(row for row in changed['fragments'] if row['fragmentId'] == scope)['text'] += '新'
                elif kind in ('attribute', 'value'): changed['attributes'][0]['values'][0]['value']['number'] = 185
                else: changed['planningCandidates'][0]['summary'] = '新的候选安排'
                by_id = {row['decisionId']: row for row in changed['authorDecisions']}
                self.assertEqual(decision_validity(changed, by_id, decisions[scope])['validity'], 'stale')
        self.assertEqual(canonical_json(source), serialized)
        self.assertEqual(chapter_plan(source, 'CH-AAA'), chapter_plan(source, 'CH-AAA'))

    def test_scope_metadata_missing_unknown_and_wrong_domain_never_gain_credit(self):
        did = self.decision(['CH-AAA']); source = load_structure(self.root)
        for basis in (None, 'raw-record', 'author-content-v0', ''):
            changed = deepcopy(source); row = changed['authorDecisions'][0]
            if basis is None: row.pop('scopeSnapshotBasis')
            else: row['scopeSnapshotBasis'] = basis
            before = canonical_json(changed)
            self.assertEqual(decision_validity(changed, {did: row}, did)['validity'], 'unproven')
            self.assertEqual(canonical_json(changed), before)
        changed = deepcopy(source); row = changed['authorDecisions'][0]
        row['scopeSnapshot'] = {'CH-AAA': chapter_plan(source, 'CH-AAA')['sourcePlanHash']}
        self.assertEqual(decision_validity(changed, {did: row}, did)['validity'], 'stale')

    def test_scope_management_progress_does_not_stale_new_proof(self):
        did = self.decision(['CH-AAA']); source = load_structure(self.root)
        expected = chapter_plan(source, 'CH-AAA')['sourcePlanHash']
        for status in ('planning_required', 'planned', 'draft', 'review', 'stable', 'stale'):
            changed = deepcopy(source); changed['revision'] += 4; changed['updatedAt'] = 'later'
            chapter = changed['chapters'][0]
            chapter.update(status=status, filePath='正文/new-location.md')
            chapter['proseBinding']['generation'] += 3
            by_id = {row['decisionId']: row for row in changed['authorDecisions']}
            self.assertEqual(decision_validity(changed, by_id, did)['validity'], 'current', status)
            self.assertEqual(chapter_plan(changed, 'CH-AAA')['sourcePlanHash'], expected)
        self.assertEqual(source['authorDecisions'][0]['scopeSnapshot'], self.validity(did)['scopeSnapshot'])

    def test_actual_lifecycle_finalization_and_locator_move_preserve_decision(self):
        from tests.fixtures_workflow import review_ready_project
        from scripts.core.finalization import finalize_chapter
        from scripts.core.refactor import preview_relocate_chapter_paths
        source = load_structure(self.root)
        source['pairAssessment'] = {'status': 'not_applicable', 'reason': '合成生命周期测试无配对设计'}
        for fragment in source['fragments']:
            if fragment['contentRole'] == 'plan' and fragment['ownerId'] in {'CH-AAA', 'CH-BBB'}:
                fragment['chapterContract'] = {'targetRange': [1, 10000], 'mode': 'plot', 'intensityLevel': 1,
                    'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}
        save_context_source(self.root, source)
        did = self.decision(['CH-AAA']); initial = self.validity(did)
        plan_hash = chapter_plan(load_structure(self.root), 'CH-AAA')['sourcePlanHash']
        # Fixture setup is explicit; its planned/draft/review transitions execute
        # the actual gates, diagnostics, registration and approvals.
        review_ready_project(self.root)
        review = load_structure(self.root)
        self.assertEqual(review['chapters'][0]['status'], 'review')
        self.assertEqual(self.validity(did)['validity'], 'current')
        self.assertEqual(chapter_plan(review, 'CH-AAA')['sourcePlanHash'], plan_hash)
        finalize_chapter(self.root, 'CH-AAA', skip_title=True,
                         binding_generation=review['chapters'][0]['proseBinding']['generation'])
        self.assertEqual(self.validity(did)['validity'], 'current')
        self.commit(preview_relocate_chapter_paths(self.root, chapter_id='CH-AAA', file_path='正文/moved.md'))
        current = self.validity(did)
        self.assertEqual(current['validity'], 'current')
        self.assertEqual(current['approval'], initial['approval'])
        self.assertEqual(current['scopeSnapshot'], initial['scopeSnapshot'])
        self.assertEqual(chapter_plan(load_structure(self.root), 'CH-AAA')['sourcePlanHash'], plan_hash)
        self.assertIn(did, [row['decisionId'] for row in self.context()['authorDecisions']])

    def test_depends_on_superseding_and_withdrawal_preserve_old_proof_without_silently_rebinding(self):
        first = self.decision(['C-ALPHA']); dependent = self.decision(['CH-BBB'], depends=[first])
        old = deepcopy(self.validity(dependent))
        replacement = self.draft(['C-ALPHA'], supersedes=first); self.approve(replacement)
        dependent_row = self.validity(dependent)
        self.assertEqual(dependent_row['validity'], 'stale')
        self.assertEqual(dependent_row['staleDecisionIds'], [first])
        self.assertEqual(dependent_row['dependsOn'], [first])
        self.assertEqual(dependent_row['approval'], old['approval'])
        self.assertEqual(self.validity(replacement)['validity'], 'current')

    def test_generic_source_batch_cannot_rewrite_active_decision_content(self):
        did = self.decision(['C-ALPHA']); row = deepcopy(self.validity(did))
        row['statement'] = '另一个未经采用的决定'
        with self.assertRaises(StructureError) as caught:
            preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'authorDecisions', 'record': row}],
                requestId='no-generic-decision-edit', baseRevision=load_structure(self.root)['revision'], reason='反例')
        self.assertEqual(caught.exception.code, 'SOURCE_CHANGES_INVALID')

    def test_active_dependency_is_immutable_through_the_dedicated_public_change_route(self):
        first = self.decision(['C-ALPHA']); dependent = self.decision(['CH-BBB'], depends=[first])
        before = (self.root/'.govel/structure.json').read_bytes()
        for args in ({'action': 'approve', 'decisionId': first, 'approvalActor': 'synthetic-author',
                      'approvalEvidence': 'cannot reapprove an active version'},
                     {'action': 'create', 'decisionId': first, 'statement': 'new semantics', 'rationale': '',
                      'scopeIds': ['C-ALPHA'], 'dependsOn': []},
                     {'action': 'supersede', 'decisionId': first, 'statement': 'cannot mutate in place'}):
            with self.assertRaises(StructureError):
                preview_decision_change(self.root, expectedRevision=load_structure(self.root)['revision'],
                    reason='same-ID mutation negative control', **args)
        self.assertEqual((self.root/'.govel/structure.json').read_bytes(), before)
        self.assertEqual(self.validity(dependent)['validity'], 'current')

    def test_public_decision_adoption_and_withdrawal_invalidate_only_affected_approval(self):
        # Actual A envelopes and approvals are produced through the fixture's
        # sanctioned registration path; no bare dictionary masquerades as a gate.
        from tests.fixtures_workflow import register_and_approve
        source = load_structure(self.root)
        for chapter in source['chapters']:
            fragment = next(f for f in source['fragments'] if f['ownerId'] == chapter['chapterId'] and f['contentRole'] == 'plan')
            fragment['chapterContract'] = {'targetRange': [1, 10000], 'mode': 'plot', 'intensityLevel': 1,
                'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}
        source['pairAssessment'] = {'status': 'not_applicable', 'reason': '合成测试无配对设计'}
        save_context_source(self.root, source)
        for cid in ('CH-AAA', 'CH-BBB'):
            register_and_approve(self.root, cid, 'stage_a_review', {'decision': 'adopt', 'activeSeats': list(range(1, 11))})
        before = read_json(self.root/'.govel/workflow.json')
        did = self.draft(['CH-AAA'])
        self.assertEqual(read_json(self.root/'.govel/workflow.json')['approvals'], before['approvals'])
        self.approve(did)
        after = read_json(self.root/'.govel/workflow.json')
        self.assertEqual(after['approvals']['CH-BBB'], before['approvals']['CH-BBB'])
        self.assertTrue(after['artifacts']['CH-AAA']['stage_a_review']['invalidatedBy'])
        self.assertEqual(after['artifacts']['CH-BBB'], before['artifacts']['CH-BBB'])
        scope = preview_decision_change(self.root, action='supersede', expectedRevision=load_structure(self.root)['revision'],
                                       reason='撤回同一范围', decisionId=did)
        self.assertEqual(scope['impact']['approvalInvalidations'], ['CH-AAA'])
        self.commit(scope)
        self.assertEqual(read_json(self.root/'.govel/workflow.json')['approvals']['CH-BBB'], before['approvals']['CH-BBB'])

    def test_generic_save_cannot_change_approved_dependency_or_drop_invalidation(self):
        first = self.decision(['C-ALPHA']); dependent = self.decision(['CH-BBB'], depends=[first])
        source = load_structure(self.root)
        changed = deepcopy(source); changed['authorDecisions'][0]['statement'] += '未经新采用的内容'
        with self.assertRaises(StructureError) as error:
            save_structure(self.root, changed, expected_revision=source['revision'])
        self.assertEqual(error.exception.code, 'AUTHOR_DECISION_WRITE_REQUIRES_TRANSACTION')
        self.assertEqual(self.validity(dependent)['validity'], 'current')
        changed = deepcopy(source); changed['fragments'][-1]['text'] += '新计划'
        with self.assertRaises(StructureError) as error:
            save_structure(self.root, changed, expected_revision=source['revision'])
        self.assertEqual(error.exception.code, 'AUTHOR_DECISION_INVALIDATION_REQUIRES_TRANSACTION')
        self.commit(attribute_value_proposal(self.root, 'AT-HEIGHT', 185))
        source = load_structure(self.root); changed = deepcopy(source)
        for row in changed['authorDecisions']: row.pop('scopeInvalidatedBy', None)
        with self.assertRaises(StructureError): save_structure(self.root, changed, expected_revision=source['revision'])
        self.assertEqual(load_structure(self.root), source)

    def test_two_undos_preserve_invalidation_and_original_journal_bytes(self):
        from scripts.core.structure_commands import undo_operation
        did = self.decision(['CH-AAA']); before = deepcopy(self.validity(did))
        first = attribute_value_proposal(self.root, 'AT-HEIGHT', 185); self.commit(first)
        first_journal = self.root/f".govel/operations/{first['operationId']}.json"
        original_journal = first_journal.read_bytes()
        marker = deepcopy(self.validity(did)['scopeInvalidatedBy'])
        second = self.change_plan('SF-B-PLAN', '乙等到回信后核对署名。')
        undo_operation(self.root, second['operationId'])
        undo_operation(self.root, first['operationId'])
        returned = self.validity(did)
        self.assertEqual(returned['validity'], 'stale')
        self.assertEqual(returned['scopeInvalidatedBy'], marker)
        self.assertEqual(returned['scopeSnapshot'], before['scopeSnapshot'])
        self.assertEqual(returned['approval'], before['approval'])
        self.assertEqual(first_journal.read_bytes(), original_journal)
        # The immediately earlier author approval can still be undone using
        # original content plus independently verified monotonic proof additions.
        from scripts.core.structure_commands import list_operations
        earlier = next(row for row in list_operations(self.root) if row.get('operation') == 'author_decision_approve')
        undo_operation(self.root, earlier['operationId'])
        self.assertEqual(self.validity(did)['validity'], 'inactive')

    def test_undo_supersession_does_not_revive_the_restored_active_approval(self):
        from scripts.core.structure_commands import undo_operation
        did = self.decision(['CH-AAA']); before = deepcopy(self.validity(did))
        plan = preview_decision_change(self.root, action='supersede',
            expectedRevision=load_structure(self.root)['revision'], reason='撤回批准', decisionId=did)
        self.commit(plan)
        undo_operation(self.root, plan['operationId'])
        row = self.validity(did)
        self.assertEqual(row['status'], 'active')
        self.assertEqual(row['validity'], 'stale')
        self.assertEqual(row['scopeInvalidatedBy']['reason'], 'historical_approval_restored')
        self.assertEqual(row['approval'], before['approval'])
        self.assertEqual(row['scopeSnapshot'], before['scopeSnapshot'])

    def test_failed_content_transaction_never_leaves_partial_invalidation(self):
        from unittest.mock import patch
        did = self.decision(['CH-AAA']); before = (self.root/'.govel/structure.json').read_bytes()
        plan = attribute_value_proposal(self.root, 'AT-HEIGHT', 185)
        def fail(stage, relative=None):
            if stage == 'write' and relative == '.govel/structure.json':
                raise RuntimeError('interrupted scoped change')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(RuntimeError):
            self.commit(plan)
        self.assertEqual((self.root/'.govel/structure.json').read_bytes(), before)
        self.assertEqual(self.validity(did)['validity'], 'current')
        self.commit(plan)
        self.assertEqual(self.validity(did)['validity'], 'stale')
        self.assertEqual(self.validity(did)['scopeInvalidatedBy']['operationId'], plan['operationId'])

    def test_aggregate_context_and_decision_query_share_current_and_unproven_scope(self):
        from scripts.core.aggregate_queries import query_aggregates
        did = self.decision(['SF-PLAN'])
        report = query_aggregates(self.root, kind='decision-plans', decisionId=did)
        self.assertEqual(report['items'][0]['chapterCount'], 1)
        self.assertEqual(report['items'][0]['validity'], self.validity(did)['validity'])
        source = load_structure(self.root)
        source['authorDecisions'][0].pop('scopeSnapshotBasis')
        save_context_source(self.root, source)  # explicitly unproven historical evidence fixture
        query = self.validity(did)
        aggregate = query_aggregates(self.root, kind='decision-plans', decisionId=did)['items'][0]
        self.assertEqual(aggregate['validity'], 'unproven')
        self.assertEqual(aggregate['validity'], query['validity'])
        self.assertEqual(aggregate['chapterCount'], 1)
        self.assertEqual(self.context()['unavailableDecisions'][0]['validity'], 'unproven')

    def test_mixed_canon_batch_keeps_exact_proposals_and_derives_only_needed_invalidation(self):
        from scripts.core.source_proposal_batch import preview_proposal_batch
        from scripts.core.source_model import snapshot_hash
        did = self.decision(['CH-AAA'])
        source = load_structure(self.root)
        plan = preview_proposal_batch(self.root, sourceHash=snapshot_hash(source), requestId='decision-batch',
            approvalActor='synthetic-author', approvalEvidence='fixture-only explicit batch approval', items=[
                {'kind': 'attribute', 'recordId': 'AT-HEIGHT',
                 'patch': [{'op': 'set', 'path': ['values', 0, 'value', 'number'], 'value': 185}],
                 'reason': '校正字段', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]},
                {'kind': 'fact', 'recordId': 'SF-HEIGHT',
                 'patch': [{'op': 'text', 'path': ['text'], 'original': '一米九', 'replacement': '一米八五'}],
                 'reason': '对应修改原有表述', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]}])
        self.commit(plan)
        self.assertEqual(self.validity(did)['validity'], 'stale')
        self.assertEqual(self.validity(did)['scopeInvalidatedBy']['operationId'], plan['operationId'])
        self.assertEqual(load_structure(self.root)['attributes'][0]['values'][0]['value']['number'], 185)

    def test_forged_invalidation_cannot_extend_undo_equivalence(self):
        from scripts.core.author_decisions import undo_content_equivalent
        from scripts.core.structure_commands import undo_operation
        did = self.decision(['CH-AAA'])
        prior_approval = next(path for path in (self.root/'.govel/operations').glob('OP-*.json')
                              if read_json(path).get('operation') == 'author_decision_approve')
        prior = read_json(prior_approval)
        change = attribute_value_proposal(self.root, 'AT-HEIGHT', 185); self.commit(change)
        undo_operation(self.root, change['operationId'])
        current = load_structure(self.root)
        self.assertTrue(undo_content_equivalent(self.root, current, prior))
        original = deepcopy(current)
        marker = current['authorDecisions'][0]['scopeInvalidatedBy']
        marker['operationId'] = prior['operationId']
        marker['revision'] = prior['afterRevision']
        self.assertFalse(undo_content_equivalent(self.root, current, prior))
        current = deepcopy(original)
        proof_op = current['authorDecisions'][0]['scopeInvalidatedBy']['operationId']
        proof_path = self.root/f'.govel/operations/{proof_op}.json'
        raw = proof_path.read_bytes(); proof_path.unlink()
        self.assertFalse(undo_content_equivalent(self.root, current, prior))
        proof_path.write_bytes(raw)
        current['chapters'][1]['title'] += '真实另改内容'
        self.assertFalse(undo_content_equivalent(self.root, current, prior))

    def test_commit_rederives_invalidation_even_if_caller_rehashes_tampered_plan(self):
        from scripts.core.structure_commands import _plan_hash
        from scripts.core.write_sets import digest, json_bytes
        did = self.decision(['CH-AAA']); before = (self.root/'.govel/structure.json').read_bytes()
        report = attribute_value_proposal(self.root, 'AT-HEIGHT', 185)
        path = self.root/f".govel/plans/{report['operationId']}.json"; plan = read_json(path)
        target = next(row for row in plan['proposedStructure']['authorDecisions'] if row['decisionId'] == did)
        self.assertIn('scopeInvalidatedBy', target); target.pop('scopeInvalidatedBy')
        payload = json_bytes(plan['proposedStructure'])
        (self.root/f".govel/plans/{report['operationId']}.files/.govel/structure.json").write_bytes(payload)
        next(row for row in plan['writeSet'] if row['path'] == '.govel/structure.json')['after']['sha256'] = digest(payload)
        plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
        write_json(path, plan)
        with self.assertRaises(StructureError) as error: self.commit(plan)
        self.assertEqual(error.exception.code, 'AUTHOR_DECISION_PROOF_READ_ONLY')
        self.assertEqual((self.root/'.govel/structure.json').read_bytes(), before)

    def test_project_voice_content_is_actual_global_input(self):
        old = load_structure(self.root)
        base = {cid: chapter_plan(old, cid)['sourcePlanHash'] for cid in ('CH-AAA', 'CH-BBB')}
        self.commit(preview_voice_profile(self.root, action='set', expectedRevision=old['revision'], reason='合成文风',
            axes={key: '保留当前已批准表达要求' for key in AXES}, examples=['句式示例'], applicability='第一章使用的自然语言描述不缩小真实全项目消费者'))
        draft = load_structure(self.root)
        self.assertEqual(source_impact(old, draft)['approvalInvalidations'], [])
        plan = preview_voice_profile(self.root, action='approve', expectedRevision=draft['revision'], reason='合成批准',
            approvalActor='synthetic-author', approvalEvidence='fixture-only authorization')
        self.assertEqual(plan['impact']['approvalInvalidations'], ['CH-AAA', 'CH-BBB'])
        self.commit(plan); active = load_structure(self.root)
        for cid in base: self.assertNotEqual(chapter_plan(active, cid)['sourcePlanHash'], base[cid])


if __name__ == '__main__': unittest.main()
