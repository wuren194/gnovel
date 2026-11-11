from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core.source_rules import evaluate_rules, query_rules
from scripts.core.source_plan import chapter_plan
from scripts.core.source_impact import source_impact
from scripts.core.structure_store import validate_structure, StructureError
from scripts.core.jsonio import write_json
from tests.test_source_plan import planning_source


class SourceRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.chapter = planning_source(self.root)
        self.cid = self.chapter['chapterId']
        self.source['fieldDefinitions'].append({'fieldId': 'field-LIMIT', 'key': 'power.limit', 'label': '救人上限',
            'valueType': 'number', 'appliesTo': ['character']})
        self.source['attributes'].append({'attributeId': 'attribute-LIMIT', 'ownerId': 'character-ALPHA', 'fieldId': 'field-LIMIT',
            'values': [{'valueId': 'value-LIMIT', 'value': 1}]})
        self.source['entities'].extend([
            {'entityId': 'event-FIRST', 'entityType': 'event', 'name': '第一次成功救人'},
            {'entityId': 'event-SECOND', 'entityType': 'event', 'name': '第二次成功救人'},
            {'entityId': 'rule-RESCUE', 'entityType': 'rule', 'name': '救人次数规则', 'ruleSpec': {
                'version': '1', 'method': 'mechanical', 'enforcement': 'required',
                'scopeIds': [self.source['projectId']], 'actions': ['review', 'publish'],
                'inputIds': ['character-ALPHA'], 'evaluation': {'kind': 'count_at_most',
                    'eventIds': ['event-FIRST', 'event-SECOND'], 'limitValueId': 'value-LIMIT', 'complete': True}}}])
        self.rule = self.source['entities'][-1]['ruleSpec']
        validate_structure(self.source)

    def result(self, action='publish'):
        return evaluate_rules(self.source, self.cid, action)

    def test_two_registered_successes_exceed_single_use(self):
        result = self.result()
        self.assertFalse(result['ready'])
        self.assertEqual(result['items'][0]['status'], 'conflict')
        self.assertEqual(result['items'][0]['evidence']['registeredCount'], 2)

    def test_incomplete_inventory_and_unknown_limit_never_pass(self):
        self.rule['evaluation'].update(eventIds=['event-FIRST'], complete=False)
        self.assertEqual(self.result()['items'][0]['status'], 'pending')
        self.rule['evaluation']['complete'] = True
        self.source['attributes'][-1]['values'][0] = {'valueId': 'value-LIMIT', 'state': 'unknown'}
        validate_structure(self.source)
        self.assertEqual(self.result()['items'][0]['status'], 'pending')

    def test_advisory_conflict_is_reported_without_blocking(self):
        self.rule['enforcement'] = 'advisory'
        self.assertTrue(self.result()['ready'])
        self.assertEqual(self.result()['items'][0]['status'], 'conflict')

    def test_other_action_and_unrelated_object_do_not_block(self):
        self.assertTrue(self.result('draft')['ready'])
        self.rule['scopeIds'] = ['character-BETA']
        self.assertEqual(self.result()['items'], [])
        self.rule['scopeIds'] = ['character-ALPHA']
        self.assertFalse(self.result()['ready'])

    def test_later_volume_rule_does_not_invalidate_current_plan(self):
        self.source['volumes'].append({'volumeId': 'volume-LATER', 'title': '后卷', 'displayOrdinal': 2})
        self.rule['scopeIds'] = ['volume-LATER']
        before = chapter_plan(self.source, self.cid)['sourcePlanHash']
        self.rule['version'] = '2'
        self.assertEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], before)

    def test_new_global_rule_invalidates_existing_approval(self):
        before = deepcopy(self.source); before['entities'].pop()
        self.assertIn(self.cid, source_impact(before, self.source)['approvalInvalidations'])

    def test_rule_input_edit_changes_plan_hash_and_input_hash(self):
        plan = chapter_plan(self.source, self.cid)['sourcePlanHash']
        evidence = self.result()['items'][0]['inputHash']
        self.source['attributes'][-1]['values'][0]['value'] = 2
        self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], plan)
        self.assertNotEqual(self.result()['items'][0]['inputHash'], evidence)
        self.assertTrue(self.result()['ready'])

    def test_semantic_rule_remains_pending(self):
        self.rule['method'] = 'semantic'; self.rule.pop('evaluation')
        validate_structure(self.source)
        self.assertFalse(self.result()['ready'])
        self.assertEqual(self.result()['items'][0]['status'], 'pending')

    def test_invalid_and_duplicate_event_ids_rejected(self):
        for events in (['event-FIRST', 'event-FIRST'], ['character-ALPHA'], [['bad']]):
            with self.subTest(events=events):
                self.rule['evaluation']['eventIds'] = events
                with self.assertRaises(StructureError): validate_structure(self.source)

    def test_query_reports_global_readiness_not_just_page(self):
        write_json(self.root / '.govel/structure.json', self.source)
        page = query_rules(self.root, chapterId=self.cid, action='publish', limit=1)
        self.assertFalse(page['ready'])
        self.assertEqual(page['items'][0]['ruleId'], 'rule-RESCUE')

    def test_required_rule_blocks_publication_preflight(self):
        from scripts.core.finalization_preflight import preflight_finalization
        write_json(self.root / '.govel/structure.json', self.source)
        result = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertIn('SOURCE_RULES_BLOCKED', {row['code'] for row in result.blockers})
        self.rule['enforcement'] = 'advisory'
        write_json(self.root / '.govel/structure.json', self.source)
        result = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertNotIn('SOURCE_RULES_BLOCKED', {row['code'] for row in result.blockers})

    def test_lifecycle_checks_rules_before_advancement(self):
        from scripts.core.chapter_state import advance_chapter_status
        from scripts.core.workflow import WorkflowError
        self.rule['actions'] = ['prepare']
        write_json(self.root / '.govel/structure.json', self.source)
        with self.assertRaises(WorkflowError) as caught:
            advance_chapter_status(self.root, self.cid, 'planned')
        self.assertEqual(caught.exception.code, 'SOURCE_RULES_BLOCKED')

    def test_new_rule_handles_resolve_in_same_atomic_batch(self):
        from scripts.core.source_changes import apply_changes
        rule = deepcopy(self.source['entities'][-1]); rule['entityId'] = '$rule'
        rule['name'] = '第二条规则'
        rule['ruleSpec']['evaluation']['eventIds'] = ['$event']
        changed, identities = apply_changes(self.source, [
            {'action': 'put', 'collection': 'entities', 'record': rule},
            {'action': 'put', 'collection': 'entities', 'record': {
                'entityId': '$event', 'entityType': 'event', 'name': '新登记的成功事件'}}])
        saved = next(row for row in changed['entities'] if row['entityId'] == identities['$rule'])
        self.assertEqual(saved['ruleSpec']['evaluation']['eventIds'], [identities['$event']])

    def test_null_rule_spec_cannot_silently_disable_checks(self):
        self.source['entities'][-1]['ruleSpec'] = None
        with self.assertRaises(StructureError): validate_structure(self.source)

    def test_setting_impact_returns_mechanical_check_without_prose_write(self):
        before = deepcopy(self.source)
        before['attributes'][-1]['values'][0]['value'] = 2
        report = source_impact(before, self.source)
        self.assertEqual(report['ruleChecks'][0]['status'], 'conflict')
        self.assertEqual(report['ruleChecks'][0]['evidence']['limit'], 1)
        self.assertNotIn('blocking', report['ruleChecks'][0])
        self.assertFalse(report['proseWrite'])

    def test_mechanical_exception_requires_declared_policy_and_evidence(self):
        from scripts.core.source_rule_issues import submit_rule_issue
        self.rule['exceptionsAllowed'] = True
        write_json(self.root / '.govel/structure.json', self.source)
        row = evaluate_rules(self.source, self.cid, 'publish', root=self.root)['items'][0]
        submit_rule_issue(self.root, requestId='mechanical-exception', ruleId='rule-RESCUE',
            chapterId=self.cid, action='publish', inputHash=row['inputHash'], status='exception',
            reason='作者批准本章为规则明文允许的唯一例外。', affectedIds=[self.cid],
            evidence=[{'kind': 'source_quote', 'recordId': 'plan-PLAN', 'start': 0, 'end': 5,
                       'text': '甲守城门。'}])
        passed = evaluate_rules(self.source, self.cid, 'publish', root=self.root)['items'][0]
        self.assertEqual(passed['status'], 'passed')
        self.assertIn('exception', passed['evidence'])
