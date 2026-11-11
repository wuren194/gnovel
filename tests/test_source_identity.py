from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.source_changes import preview_source_change
from scripts.core.source_content_proposals import preview_content_change, resolve_content_target
from scripts.core.source_review import fingerprint
from scripts.core.structure_commands import _plan_hash, gc_project
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit, undo
from tests.fixtures_source import source_fixture


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source_fixture(self.root)
        self.sequence = 0

    def fragment(self, identity='plan-ORIGINAL', **changes):
        return {'fragmentId': identity, 'ownerId': 'character-ALPHA', 'text': '准备整理档案。',
                'order': 9, 'contentRole': 'plan', 'contentBindings': ['attribute-LIFE'], **changes}

    def preview(self, changes, request_id=None):
        self.sequence += 1
        return preview_source_change(self.root, changes=changes,
            requestId=request_id or f'identity-{self.sequence}',
            baseRevision=load_structure(self.root)['revision'], reason='核对记录身份')

    def put(self, record, collection='fragments', request_id=None):
        return self.preview([{'action': 'put', 'collection': collection, 'record': record}], request_id)

    def commit(self, plan):
        return commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'],
                      approved=True, actor='test')

    def delete(self, identity='plan-ORIGINAL'):
        plan = self.preview([{'action': 'delete', 'collection': 'fragments', 'id': identity}])
        self.commit(plan)
        return plan

    def retire(self):
        self.commit(self.put(self.fragment()))
        return self.delete()

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def assert_retired(self, function, identity='plan-ORIGINAL'):
        before = self.snapshot()
        with self.assertRaises(StructureError) as error:
            function()
        self.assertEqual(error.exception.code, 'SOURCE_ID_RETIRED')
        self.assertIn(identity, error.exception.details['recordIds'])
        self.assertTrue(error.exception.details['path'].startswith('.govel/operations/'))
        self.assertEqual(self.snapshot(), before)

    def test_deleted_fragment_cannot_be_given_to_another_owner(self):
        self.retire()
        self.assert_retired(lambda: self.put(self.fragment(ownerId='character-BETA', text='另一项无关计划。')))

    def test_even_identical_recreation_requires_formal_undo(self):
        self.retire()
        self.assert_retired(lambda: self.put(self.fragment()))

    def test_existing_identity_remains_editable_after_earlier_transactions(self):
        self.commit(self.put(self.fragment()))
        self.commit(self.put(self.fragment(text='整理档案后归还钥匙。')))
        source = load_structure(self.root)
        actual = next(row for row in source['fragments'] if row['fragmentId'] == 'plan-ORIGINAL')
        self.assertEqual(actual['text'], '整理档案后归还钥匙。')

    def test_fresh_automatic_identity_is_available_after_deletion(self):
        self.retire()
        plan = self.put(self.fragment('$replacement'))
        identity = plan['parameters']['identityMap']['$replacement']
        self.assertNotEqual(identity, 'plan-ORIGINAL')
        self.commit(plan)
        self.assertIn(identity, {row['fragmentId'] for row in load_structure(self.root)['fragments']})

    def test_allocator_collision_with_retired_identity_fails_closed(self):
        self.retire()
        with patch('scripts.core.source_changes.new_id', return_value='plan-ORIGINAL'):
            self.assert_retired(lambda: self.put(self.fragment('$replacement')))

    def test_allocator_cannot_turn_creation_into_an_existing_record_update(self):
        self.commit(self.put(self.fragment()))
        before = self.snapshot()
        with patch('scripts.core.source_changes.new_id', return_value='plan-ORIGINAL'):
            with self.assertRaises(StructureError) as error:
                self.put(self.fragment('$replacement', text='另一项计划。'))
        self.assertEqual(error.exception.code, 'SOURCE_ID_COLLISION')
        self.assertEqual(before, self.snapshot())

    def test_formal_undo_restores_deleted_record_and_normal_edit_keeps_its_id(self):
        deletion = self.retire()
        undo(self.root, deletion['operationId'], actor='test')
        original = next(row for row in load_structure(self.root)['fragments']
                        if row['fragmentId'] == 'plan-ORIGINAL')
        self.assertEqual(original, self.fragment())
        self.commit(self.put(self.fragment(text='复核档案后再归还钥匙。')))

    def test_undone_creation_retries_as_history_but_cannot_lend_its_id(self):
        changes = [{'action': 'put', 'collection': 'fragments', 'record': self.fragment()}]
        args = {'changes': changes, 'requestId': 'create-once', 'baseRevision': 0, 'reason': '核对记录身份'}
        plan = preview_source_change(self.root, **args)
        self.commit(plan)
        undo(self.root, plan['operationId'], actor='test')
        before = self.snapshot()
        resumed = preview_source_change(self.root, **args)
        self.assertEqual(resumed['operationState'], 'undone')
        self.assertTrue(resumed['alreadyCommitted'])
        self.assertEqual(before, self.snapshot())
        self.assert_retired(lambda: self.put(self.fragment(text='无关的新计划。')))

    def test_committed_creation_and_delete_requests_resume_without_reapplying(self):
        original = [{'action': 'put', 'collection': 'fragments', 'record': self.fragment()}]
        create_args = {'changes': original, 'requestId': 'create-once', 'baseRevision': 0, 'reason': '核对记录身份'}
        creation = preview_source_change(self.root, **create_args)
        self.commit(creation)
        delete_args = {'changes': [{'action': 'delete', 'collection': 'fragments', 'id': 'plan-ORIGINAL'}],
                       'requestId': 'delete-once', 'baseRevision': 1, 'reason': '核对记录身份'}
        deletion = preview_source_change(self.root, **delete_args)
        self.commit(deletion)
        before = self.snapshot()
        for args in (create_args, delete_args):
            self.assertEqual(preview_source_change(self.root, **args)['operationState'], 'committed')
        self.assertEqual(before, self.snapshot())

    def test_uncommitted_preview_does_not_reserve_identity(self):
        first = self.put(self.fragment(), request_id='first-preview')
        second = self.put(self.fragment(text='选择另一项计划。'), request_id='second-preview')
        self.commit(second)
        with self.assertRaises(StructureError) as error:
            self.commit(first)
        self.assertEqual(error.exception.code, 'REVISION_CONFLICT')

    def test_failed_commit_does_not_reserve_identity(self):
        first = self.put(self.fragment())
        def fail(phase, relative=None):
            if phase == 'write' and relative == '.govel/structure.json':
                raise OSError('simulated interruption')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail):
            with self.assertRaises(OSError):
                self.commit(first)
        self.assertFalse((self.root / '.govel/operations' / f"{first['operationId']}.json").exists())
        self.commit(self.put(self.fragment(text='选定另一项计划。')))

    def test_deleted_unused_field_definition_also_retires_its_identity(self):
        field = {'fieldId': 'field-UNUSED', 'key': 'unused.topic', 'label': '备用主题',
                 'valueType': 'topic', 'appliesTo': ['character']}
        self.commit(self.put(field, collection='fieldDefinitions'))
        self.commit(self.preview([{'action': 'delete', 'collection': 'fieldDefinitions', 'id': 'field-UNUSED'}]))
        self.assert_retired(lambda: self.put({**field, 'key': 'unrelated.topic'}, collection='fieldDefinitions'),
                            'field-UNUSED')

    def test_same_batch_delete_and_readd_is_already_rejected(self):
        self.commit(self.put(self.fragment()))
        before = self.snapshot()
        with self.assertRaises(StructureError) as error:
            self.preview([{'action': 'delete', 'collection': 'fragments', 'id': 'plan-ORIGINAL'},
                          {'action': 'put', 'collection': 'fragments',
                           'record': self.fragment(ownerId='character-BETA')}])
        self.assertEqual(error.exception.code, 'SOURCE_CHANGE_DUPLICATE')
        self.assertEqual(before, self.snapshot())

    def test_retirement_survives_backup_gc(self):
        deletion = self.retire()
        self.commit(self.put(self.fragment('plan-NEXT')))
        gc_project(self.root, keep_backups=0)
        self.assertFalse((self.root / '.govel/backups' / deletion['operationId']).exists())
        self.assertTrue((self.root / '.govel/operations' / f"{deletion['operationId']}.json").is_file())
        self.assert_retired(lambda: self.put(self.fragment()))

    def test_commit_rechecks_retirement_even_if_preview_guard_was_bypassed(self):
        self.retire()
        with patch('scripts.core.source_identity.require_fresh_source_identities'):
            plan = self.put(self.fragment())
        self.assert_retired(lambda: self.commit(plan))

    def test_corrupted_ledger_cannot_be_treated_as_empty_history(self):
        deletion = self.retire()
        path = self.root / '.govel/operations' / f"{deletion['operationId']}.json"
        original = read_json(path)
        invalids = [[], {'operationId': 'wrong'}, {**original, 'impact': {}},
                    {**original, 'impact': {**original['impact'], 'changedIds': 'plan-ORIGINAL'}}]
        for invalid in invalids:
            with self.subTest(invalid=invalid):
                write_json(path, invalid)
                before = self.snapshot()
                with self.assertRaises(StructureError) as error:
                    self.put(self.fragment('plan-FRESH'))
                self.assertEqual(error.exception.code, 'SOURCE_IDENTITY_HISTORY_INVALID')
                self.assertEqual(error.exception.details['path'], path.relative_to(self.root).as_posix())
                self.assertEqual(before, self.snapshot())
        path.write_text('{unreadable', encoding='utf-8')
        with self.assertRaises(StructureError) as error:
            self.put(self.fragment('plan-FRESH'))
        self.assertEqual(error.exception.code, 'SOURCE_IDENTITY_HISTORY_INVALID')

    def test_existing_record_update_does_not_depend_on_unrelated_history(self):
        self.commit(self.put(self.fragment()))
        path = next((self.root / '.govel/operations').glob('OP-*.json'))
        path.write_text('{unreadable', encoding='utf-8')
        self.commit(self.put(self.fragment(text='整理档案并登记归还时间。')))

    def test_commit_rechecks_ledger_damage_after_preview(self):
        deletion = self.retire()
        plan = self.put(self.fragment('plan-FRESH'))
        path = self.root / '.govel/operations' / f"{deletion['operationId']}.json"
        path.write_text('{unreadable', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(StructureError) as error:
            self.commit(plan)
        self.assertEqual(error.exception.code, 'SOURCE_IDENTITY_HISTORY_INVALID')
        self.assertEqual(before, self.snapshot())

    def update_beta_values(self, values):
        resolved = resolve_content_target(self.root, recordId='attribute-BETA-HEIGHT', kind='attribute')
        old = resolved['record']
        return preview_content_change(self.root, sessionRef=resolved['sessionRef'], expectedOldRecord=old,
            newRecord={**old, 'values': values}, reason='核对属性值',
            evidence=[{'recordHash': fingerprint(old)}], approvalActor='author', approvalEvidence='测试批准声明')

    def test_nested_value_identity_is_retired_through_reviewed_content_change(self):
        deletion = self.update_beta_values([])
        self.commit(deletion)
        original = {'valueId': 'value-BETA-HEIGHT', 'value': {'number': 180, 'unit': 'cm'}}
        self.assert_retired(lambda: self.update_beta_values([original]), 'value-BETA-HEIGHT')
        self.commit(self.update_beta_values([{**original, 'valueId': 'value-REPLACEMENT'}]))

    def test_nested_value_cannot_move_to_a_new_attribute_after_retirement(self):
        self.commit(self.update_beta_values([]))
        self.assert_retired(lambda: self.preview([
            {'action': 'put', 'collection': 'fieldDefinitions', 'record': {
                'fieldId': 'field-NEW-NUMBER', 'key': 'custom.number', 'label': '编号',
                'valueType': 'number', 'appliesTo': ['character']}},
            {'action': 'put', 'collection': 'attributes', 'record': {
                'attributeId': 'attribute-NEW-NUMBER', 'ownerId': 'character-ALPHA', 'fieldId': 'field-NEW-NUMBER',
                'values': [{'valueId': 'value-BETA-HEIGHT', 'value': 8}]}}
        ]), 'value-BETA-HEIGHT')

    def test_nested_value_restoration_by_formal_undo_keeps_identity(self):
        deletion = self.update_beta_values([])
        self.commit(deletion)
        undo(self.root, deletion['operationId'], actor='test')
        attribute = next(row for row in load_structure(self.root)['attributes']
                         if row['attributeId'] == 'attribute-BETA-HEIGHT')
        self.assertEqual(attribute['values'][0]['valueId'], 'value-BETA-HEIGHT')
        self.commit(self.update_beta_values([{'valueId': 'value-BETA-HEIGHT',
                                             'value': {'number': 181, 'unit': 'cm'}}]))

    def test_shared_boundary_rejects_nested_value_reassignment_in_one_transaction(self):
        from scripts.core.source_identity import require_fresh_source_identities
        from scripts.core.structure_store import validate_structure
        source = load_structure(self.root)
        proposed = deepcopy(source)
        source_attribute = next(row for row in proposed['attributes'] if row['attributeId'] == 'attribute-BETA-HEIGHT')
        value = source_attribute['values'].pop()
        proposed['fieldDefinitions'].append({'fieldId': 'field-OTHER-MEASUREMENT', 'key': 'other.measurement',
            'label': '另一个尺寸', 'valueType': 'measurement', 'appliesTo': ['character']})
        proposed['attributes'].append({'attributeId': 'attribute-OTHER-MEASUREMENT', 'ownerId': 'character-ALPHA',
                                      'fieldId': 'field-OTHER-MEASUREMENT', 'values': [value]})
        validate_structure(proposed)
        before = self.snapshot()
        with self.assertRaises(StructureError) as error:
            require_fresh_source_identities(self.root, source, proposed)
        self.assertEqual(error.exception.code, 'SOURCE_VALUE_ID_REASSIGNED')
        self.assertEqual(error.exception.details['changes'], [{'valueId': 'value-BETA-HEIGHT',
            'beforeAttributeId': 'attribute-BETA-HEIGHT', 'afterAttributeId': 'attribute-OTHER-MEASUREMENT'}])
        self.assertEqual(before, self.snapshot())

    def test_existing_planning_fragment_may_keep_id_when_author_changes_owner(self):
        self.commit(self.put(self.fragment()))
        self.commit(self.put(self.fragment(ownerId='character-BETA')))
        actual = next(row for row in load_structure(self.root)['fragments'] if row['fragmentId'] == 'plan-ORIGINAL')
        self.assertEqual(actual['ownerId'], 'character-BETA')

    def rewrite_plan(self, plan, transform):
        path = self.root / '.govel/plans' / f"{plan['operationId']}.json"
        stored = read_json(path)
        transform(stored)
        stored['planHash'] = _plan_hash({key: value for key, value in stored.items() if key != 'planHash'})
        write_json(path, stored)
        return stored

    def assert_impact_rejected(self, plan, fields):
        before = self.snapshot()
        with self.assertRaises(StructureError) as error:
            self.commit(plan)
        self.assertEqual(error.exception.code, 'SOURCE_IDENTITY_IMPACT_MISMATCH')
        self.assertEqual(error.exception.details['fields'], fields)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / '.govel/backups' / plan['operationId']).exists())
        self.assertFalse((self.root / '.govel/operations' / f"{plan['operationId']}.json").exists())

    def test_rehashed_delete_plan_cannot_omit_initial_record_from_ledger(self):
        source = load_structure(self.root)
        source['fragments'].append(self.fragment('plan-INITIAL'))
        write_json(self.root / '.govel/structure.json', source)
        plan = self.preview([{'action': 'delete', 'collection': 'fragments', 'id': 'plan-INITIAL'}])
        self.assertEqual(plan['impact']['changedIds'], ['plan-INITIAL'])
        tampered = self.rewrite_plan(plan, lambda value: value['impact'].update(changedIds=[]))
        self.assert_impact_rejected(tampered, ['changedIds'])
        self.assertIn('plan-INITIAL', {row['fragmentId'] for row in load_structure(self.root)['fragments']})

    def test_rehashed_plan_cannot_remove_or_fabricate_any_identity_projection(self):
        plan = self.put(self.fragment())
        path = self.root / '.govel/plans' / f"{plan['operationId']}.json"
        original = read_json(path)
        keys = ('changedIds', 'planningCandidateChanges', 'authorDecisionChanges', 'workVoiceProfileChanged')
        for key in keys:
            replacements = ('missing', None, ['UNRELATED-FAKE'], True) if key == 'workVoiceProfileChanged' else ('missing', None, ['UNRELATED-FAKE'])
            for replacement in replacements:
                with self.subTest(key=key, replacement=replacement):
                    write_json(path, original)
                    def transform(value):
                        if replacement == 'missing':
                            value['impact'].pop(key)
                        else:
                            value['impact'][key] = replacement
                    tampered = self.rewrite_plan(plan, transform)
                    self.assert_impact_rejected(tampered, [key])
        write_json(path, original)
        tampered = self.rewrite_plan(plan, lambda value: value.pop('impact'))
        self.assert_impact_rejected(tampered, list(keys))

    def test_nested_value_removal_must_be_in_committed_identity_projection(self):
        plan = self.update_beta_values([])
        self.assertIn('value-BETA-HEIGHT', plan['impact']['changedIds'])
        tampered = self.rewrite_plan(plan, lambda value: value['impact']['changedIds'].remove('value-BETA-HEIGHT'))
        self.assert_impact_rejected(tampered, ['changedIds'])

    def test_candidate_and_decision_changes_cannot_be_erased_from_rehashed_plan(self):
        from scripts.core.author_decisions import preview_decision_change
        from scripts.core.planning_candidates import preview_candidate_change
        candidate = preview_candidate_change(self.root, action='create', expectedRevision=0,
            reason='保留未来候选', record={'title': '未来章节', 'summary': '待讨论的安排'})
        decision = preview_decision_change(self.root, action='create', expectedRevision=0,
            reason='保留作者取舍', statement='先保留线索', rationale='等待比较', scopeIds=['character-ALPHA'])
        for plan, key in ((candidate, 'planningCandidateChanges'), (decision, 'authorDecisionChanges')):
            with self.subTest(key=key):
                self.assertTrue(plan['impact'][key])
                tampered = self.rewrite_plan(plan, lambda value: value['impact'].update({key: []}))
                self.assert_impact_rejected(tampered, [key])

    def test_valid_commit_keeps_other_impact_details_without_recomputing_semantic_scan(self):
        plan = self.put(self.fragment())
        expected = deepcopy(plan['impact'])
        self.assertIn('refactor', expected, 'shared producer adds legitimate non-identity impact details')
        with patch('scripts.core.source_impact.source_impact', side_effect=AssertionError('unexpected semantic rescan')):
            self.commit(plan)
        journal = read_json(self.root / '.govel/operations' / f"{plan['operationId']}.json")
        self.assertEqual(journal['impact'], expected)

    def test_initial_record_delete_is_recorded_and_blocks_later_reuse(self):
        source = load_structure(self.root)
        source['fragments'].append(self.fragment('plan-INITIAL'))
        write_json(self.root / '.govel/structure.json', source)
        plan = self.preview([{'action': 'delete', 'collection': 'fragments', 'id': 'plan-INITIAL'}])
        self.commit(plan)
        journal = read_json(self.root / '.govel/operations' / f"{plan['operationId']}.json")
        self.assertEqual(journal['impact']['changedIds'], ['plan-INITIAL'])
        self.assert_retired(lambda: self.put(self.fragment('plan-INITIAL', ownerId='character-BETA')), 'plan-INITIAL')


if __name__ == '__main__':
    unittest.main()
