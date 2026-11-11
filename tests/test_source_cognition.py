"""Story time, evidence drift and POV boundaries stay independent."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import write_json
from scripts.core.operations import call_operation
from scripts.core.source_cognition import preview_check, query_checks, query_cognition
from scripts.core.source_impact import review_input_hash
from scripts.core.source_selection import select_content
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.structure_transaction import commit
from tests.fixtures_source import source_fixture


class SourceCognitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = source_fixture(self.root)
        self.source['entities'].extend([
            {'entityId': 'time-MID', 'entityType': 'time_anchor', 'name': '中间时点', 'timeOrdinal': 5},
            {'entityId': 'time-END', 'entityType': 'time_anchor', 'name': '末时点', 'timeOrdinal': 20},
        ])
        self.target = self.source['attributes'][0]['values'][0]['valueId']
        self.add('fact-UNAWARE', 'fact', 'unaware', {'fromId': 'time-START', 'toId': 'time-MID'}, None)
        self.add('fact-KNOWS', 'fact', 'knows', {'fromId': 'time-MID', 'toId': 'time-LATER'}, 'time-MID')
        self.add('belief-LATER', 'belief', 'believes', {'fromId': 'time-LATER'}, 'time-LATER')
        self.save()

    def add(self, identity, role, attitude, period, acquired, *, holder='character-ALPHA', target=None):
        row = {'fragmentId': identity, 'ownerId': holder, 'order': len(self.source['fragments']) + 1,
               'text': '他已经看过书脊上的记号。', 'contentRole': role, 'validDuring': period,
               'contentBindings': ['attribute-LIFE'],
               'cognition': {'holderId': holder, 'targetId': target or self.target, 'attitude': attitude,
                             'acquiredAtId': acquired, 'basisIds': ['time-MID']}}
        if role == 'belief':
            row['perspectiveId'] = holder
        self.source['fragments'].append(row)
        return row

    def save(self):
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)

    def query(self, when, **overrides):
        return query_cognition(self.root, holderId='character-ALPHA', targetId=self.target, asOf=when, **overrides)

    def check(self, **overrides):
        arguments = {'requestId': 'check-1', 'targetId': 'belief-LATER', 'status': 'supported',
                     'checkedAtId': 'time-LATER', 'reason': '与已登记片段核对。',
                     'evidence': [{'fragmentId': 'fact-KNOWS', 'quote': '书脊上的记号'}]}
        return preview_check(self.root, **{**arguments, **overrides})

    def publish(self, plan):
        return commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')

    def test_half_open_interval_keeps_explicit_unawareness_separate_from_missing_records(self):
        self.assertEqual(self.query('time-START')['state'], 'explicitly_unaware')
        self.assertEqual(self.query('time-MID')['state'], 'recorded_knowledge')
        later = self.query('time-LATER')
        self.assertEqual(later['state'], 'recorded_belief')
        self.assertEqual([row['fragmentId'] for row in later['records']], ['belief-LATER'])
        missing = query_cognition(self.root, holderId='character-BETA', targetId=self.target, asOf='time-MID')
        self.assertEqual(missing['state'], 'no_record')
        self.assertEqual(missing['verification'], 'unverified')

    def test_unknown_acquisition_and_unknown_query_time_are_not_explicit_unawareness(self):
        self.add('belief-UNDATED', 'belief', 'suspects', {}, None, holder='character-BETA')
        self.save()
        result = query_cognition(self.root, holderId='character-BETA', targetId=self.target, asOf='time-MID')
        self.assertEqual(result['state'], 'time_unknown')
        self.assertEqual(self.query(None)['state'], 'time_unknown')

    def test_overlapping_amnesia_and_old_belief_remain_a_reported_conflict(self):
        self.add('fact-AMNESIA', 'fact', 'unaware', {'fromId': 'time-LATER'}, 'time-LATER')
        self.save()
        self.assertEqual(self.query('time-END')['state'], 'inconsistent')

    def test_memory_loss_cannot_apply_before_its_acquisition_event(self):
        self.add('fact-AMNESIA', 'fact', 'unaware', {}, 'time-LATER')
        self.save()
        self.assertEqual(self.query('time-MID')['state'], 'recorded_knowledge')
        self.assertEqual(self.query('time-END')['state'], 'inconsistent')

    def test_cognition_target_requires_a_concrete_value_or_proposition(self):
        for identity in ('attribute-HEIGHT', 'field-HEIGHT', 'character-ALPHA'):
            with self.subTest(identity=identity), self.assertRaises(StructureError):
                query_cognition(self.root, holderId='character-ALPHA', targetId=identity, asOf='time-MID')

    def test_belief_cannot_claim_author_established_knowledge(self):
        self.source['fragments'][-1]['cognition']['attitude'] = 'knows'
        with self.assertRaises(StructureError) as caught:
            self.save()
        self.assertIn('SOURCE_COGNITION_ROLE_INVALID', {row['code'] for row in caught.exception.details['errors']})

    def test_acquisition_is_a_real_dependency_without_moving_objective_values(self):
        before = review_input_hash(self.source, 'fact-KNOWS')
        old_value = deepcopy(self.source['attributes'][0]['values'])
        next(row for row in self.source['entities'] if row['entityId'] == 'time-MID')['timeOrdinal'] = 6
        after = review_input_hash(self.source, 'fact-KNOWS')
        self.assertNotEqual(before, after)
        self.assertEqual(self.source['attributes'][0]['values'], old_value)

    def test_check_is_a_preview_then_an_append_only_source_commit(self):
        plan = self.check()
        self.assertEqual(load_structure(self.root)['checks'], [])
        self.assertTrue(plan['parameters']['check']['checkId'].startswith('check-'))
        repeated = self.check()
        self.assertEqual(repeated['planHash'], plan['planHash'])
        self.publish(plan)
        source = load_structure(self.root)
        self.assertEqual(len(source['checks']), 1)
        self.assertEqual(next(row for row in source['fragments'] if row['fragmentId'] == 'belief-LATER')['contentRole'], 'belief')
        from scripts.core.source_changes import preview_source_change
        with self.assertRaises(StructureError) as caught:
            preview_source_change(self.root, requestId='erase-check', baseRevision=source['revision'], reason='删除',
                changes=[{'action': 'delete', 'collection': 'checks', 'id': source['checks'][0]['checkId']}])
        self.assertEqual(caught.exception.code, 'SOURCE_CHECK_IMMUTABLE')
        from scripts.core.structure_transaction import undo
        with self.assertRaises(StructureError) as caught:
            undo(self.root, plan['operationId'], actor='test')
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_REVERSIBLE')
        self.assertEqual(len(load_structure(self.root)['checks']), 1)
        from scripts.core.jsonio import read_json
        journal_path = self.root / '.govel/operations' / (plan['operationId'] + '.json')
        journal = read_json(journal_path)
        journal['reversible'] = True
        write_json(journal_path, journal)
        with self.assertRaises(StructureError) as caught:
            undo(self.root, plan['operationId'], actor='test')
        self.assertEqual(caught.exception.code, 'SOURCE_CHECK_IMMUTABLE')

    def test_evidence_drift_preserves_historical_status_and_does_not_cause_fictional_forgetting(self):
        self.publish(self.check())
        self.source = load_structure(self.root)
        before = query_checks(self.root, targetId='belief-LATER', asOf='time-LATER')['items'][0]
        self.assertEqual(before['evidenceState'], 'current')
        next(row for row in self.source['fragments'] if row['fragmentId'] == 'fact-KNOWS')['text'] += '他又翻了一页。'
        self.save()
        after = query_checks(self.root, targetId='belief-LATER', asOf='time-LATER')['items'][0]
        self.assertEqual(after['status'], 'supported')
        self.assertEqual(after['evidenceState'], 'stale')
        self.assertEqual(self.query('time-LATER')['state'], 'recorded_belief')
        self.assertFalse(after['grantsObjectiveTruth'])

    def test_historical_check_survives_deleted_target_and_source_evidence(self):
        self.publish(self.check())
        self.source = load_structure(self.root)
        self.source['fragments'] = [row for row in self.source['fragments'] if row['fragmentId'] not in {'belief-LATER', 'fact-KNOWS'}]
        self.save()
        # The inventory can still be read by its stable historical target ID.
        result = query_checks(self.root, targetId='belief-LATER')
        self.assertEqual(result['items'][0]['evidenceState'], 'stale')

    def test_character_check_refuses_another_perspective_and_unavailable_times(self):
        for holder, when in (('character-BETA', 'time-LATER'), ('character-ALPHA', 'time-START'), ('character-ALPHA', None)):
            with self.subTest(holder=holder, when=when), self.assertRaises(StructureError):
                self.check(holderId=holder, checkedAtId=when)
        # A character's own available belief may support a character check but is never author truth.
        plan = self.check(holderId='character-ALPHA', evidence=[{'fragmentId': 'belief-LATER', 'quote': '书脊上的记号'}])
        self.publish(plan)
        row = query_checks(self.root, targetId='belief-LATER', holderId='character-ALPHA', asOf='time-LATER')['items'][0]
        self.assertEqual(row['epistemicRole'], 'character_check')
        self.assertFalse(row['grantsObjectiveTruth'])

    def test_future_and_undated_checks_have_explicit_temporal_states(self):
        self.publish(self.check())
        row = query_checks(self.root, targetId='belief-LATER', asOf='time-START')['items'][0]
        self.assertEqual(row['timeMatch'], 'outside')
        self.publish(self.check(requestId='check-2', status='unverified', checkedAtId=None, evidence=[]))
        rows = query_checks(self.root, targetId='belief-LATER', asOf='time-END')['items']
        self.assertEqual(rows[-1]['timeMatch'], 'unknown')

    def test_scoped_designation_never_returns_the_author_only_real_name(self):
        entity = self.source['entities'][0]
        entity.update(name='作者专属真名', visibleTo=[], designations=[
            {'label': '修书人', 'perspectiveIds': ['character-ALPHA'], 'validDuring': {'fromId': 'time-MID'}}])
        self.save()
        result = select_content(self.source, {'knowledgeMode': 'perspective', 'perspectiveIds': ['character-ALPHA'], 'asOf': 'time-LATER'})
        self.assertEqual(result['owners'][0]['label'], '修书人')
        self.assertNotIn('作者专属真名', str(result))
        result = select_content(self.source, {'knowledgeMode': 'perspective', 'perspectiveIds': ['character-ALPHA']})
        self.assertEqual(result['owners'][0]['label'], 'character-ALPHA')

    def test_designations_are_editable_through_an_existing_canon_proposal(self):
        from scripts.core.canon_proposals import resolve_canon_target, preview_canon_change
        resolved = resolve_canon_target(self.root, selector='character-ALPHA')
        plan = preview_canon_change(self.root, sessionRef=resolved['sessionRef'], fieldPath='designations',
            expectedOldExists=False, expectedOldValue=None,
            newValue=[{'label': '守门人', 'perspectiveIds': ['character-BETA'], 'validDuring': {'fromId': 'time-START'}}],
            reason='登记称呼的受众与时期。', evidence=[{'fragmentId': 'fact-LIFE', 'quote': '他独自守过城门。'}],
            approvalActor='test-author', approvalEvidence='已确认称呼范围。')
        self.publish(plan)
        self.assertEqual(load_structure(self.root)['entities'][0]['designations'][0]['label'], '守门人')

    def test_reviewed_relation_keeps_direction_and_exposes_subjective_scope(self):
        from scripts.core.source_links import query_source_links
        from scripts.core.source_relation_proposals import resolve_relation_target, preview_relation_change
        self.source['relations'].append({'relationId': 'relation-TRUST', 'fromEntity': 'character-ALPHA',
                                        'toEntity': 'character-BETA', 'kind': 'trusts', 'origin': 'author'})
        self.save()
        resolved = resolve_relation_target(self.root, relationId='relation-TRUST')
        old = resolved['record']
        arguments = {'sessionRef': resolved['sessionRef'], 'expectedOldRecord': old,
            'newRecord': {**old, 'perspectiveId': 'character-ALPHA', 'validDuring': {'fromId': 'time-MID'},
                          'basisIds': ['fact-LIFE']}, 'reason': '限定为一方判断。',
            'evidenceMode': 'author_declaration', 'evidence': [], 'approvalActor': 'test-author',
            'approvalEvidence': '已确认方向与视角。'}
        with self.assertRaises(StructureError):
            preview_relation_change(self.root, **{**arguments, 'newRecord': {**arguments['newRecord'], 'fromEntity': 'character-BETA'}})
        self.publish(preview_relation_change(self.root, **arguments))
        canon = query_source_links(self.root, recordId='character-ALPHA', graph='canon')
        self.assertFalse(any(row.get('relationId') == 'relation-TRUST' for row in canon['items']))
        registered = query_source_links(self.root, recordId='character-ALPHA')
        row = next(row for row in registered['items'] if row.get('relationId') == 'relation-TRUST')
        self.assertEqual(row['epistemicRole'], 'character_belief')
        self.assertEqual(row['sourceId'], 'character-ALPHA')
        self.assertEqual(row['targetId'], 'character-BETA')

    def test_public_operation_validates_parameters_and_returns_same_cognition(self):
        arguments = {'holderId': 'character-ALPHA', 'targetId': self.target, 'asOf': 'time-MID'}
        result = call_operation('source.cognition', self.root, arguments)
        self.assertEqual(result['state'], 'recorded_knowledge')
        with self.assertRaises(StructureError):
            call_operation('source.cognition', self.root, {**arguments, 'scene': '诱导知道'})


if __name__ == '__main__':
    unittest.main()
