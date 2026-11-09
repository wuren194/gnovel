"""Reviewed semantic field changes preserve identities and the complete value scope."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifacts import envelope
from scripts.core.canon_guard import require_canon_proposal
from scripts.core.jsonio import read_json, write_json
from scripts.core.locks import project_lock, project_lock_observation
from scripts.core.project import TOOLBOX_ROOT, source_model_policy, workflow_policy
from scripts.core.source_changes import preview_source_change
from scripts.core.source_fields import query_settings_source
from scripts.core.source_impact import REVIEW_PATH, ReviewInputs, dependency_edges
from scripts.core.source_model import snapshot_hash
from scripts.core.source_plan import chapter_plan
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.source_query import query_source
from scripts.core.source_render import render_source
from scripts.core.source_reports import query_change_report
from scripts.core.source_review import fingerprint, query_reviews, submit_review
from scripts.core.structure_commands import _plan_hash
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.structure_transaction import commit, undo
from scripts.core.workflow import approve_artifacts, default_workflow, record_artifact
from tests.fixtures_source import single_source_chapter, source_fixture
from tests.fixtures_workflow import assessed_review_payload


class FieldMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='field-migration-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        with project_lock(self.root):
            self.assertTrue(project_lock_observation(self.root)['ownedByCurrentThread'])
        self.lock_inode = (self.root / '.gnovel/structure.lock').stat().st_ino

    def attribute(self, identity, source=None):
        return next(row for row in (self.source if source is None else source)['attributes']
                    if row['attributeId'] == identity)

    def install_fixture(self):
        """Complete neutral data before its first transaction, never accept external edits."""
        validate_structure(self.source)
        write_json(self.root / '.gnovel/structure.json', self.source)

    def height_item(self, *, field_patch=None, values=None):
        if values is None:
            values = {'FV-HEIGHT': 190, 'FV-BETA-HEIGHT': 180}
        return {'kind': 'field_migration', 'recordId': 'FD-HEIGHT',
                'patch': field_patch if field_patch is not None else [
                    {'op': 'set', 'path': ['valueType'], 'value': 'number'},
                    {'op': 'set', 'path': ['description'], 'value': '以厘米计。'}],
                'attributeMigrations': [
                    {'attributeId': 'AT-HEIGHT', 'values': [
                        {'valueId': 'FV-HEIGHT', 'value': deepcopy(values['FV-HEIGHT'])}],
                     'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}]},
                    {'attributeId': 'AT-BETA-HEIGHT', 'values': [
                        {'valueId': 'FV-BETA-HEIGHT', 'value': deepcopy(values['FV-BETA-HEIGHT'])}],
                     'evidence': [{'recordHash': fingerprint(self.attribute('AT-BETA-HEIGHT'))}]}],
                'reason': '统一字段含义和现有值的记录方式', 'evidence': []}

    def unchanged_heights(self):
        return {row['values'][0]['valueId']: deepcopy(row['values'][0]['value'])
                for row in self.source['attributes'] if row['fieldId'] == 'FD-HEIGHT'}

    def key_item(self):
        return self.height_item(field_patch=[
            {'op': 'set', 'path': ['key'], 'value': 'appearance.height_cm'}],
            values=self.unchanged_heights())

    def preview(self, *, items=None, **overrides):
        args = {'sourceHash': snapshot_hash(self.source), 'items': [self.height_item()] if items is None else items,
                'requestId': 'migrate-height', 'approvalActor': '测试作者',
                'approvalEvidence': '已确认字段及列出的全部属性转换'}
        return preview_proposal_batch(self.root, **{**args, **overrides})

    def commit(self, preview, *, approved=True):
        return commit(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                      approved=approved, actor='test')

    def plan_path(self, preview):
        return self.root / '.gnovel/plans' / (preview['operationId'] + '.json')

    def inventory(self):
        self.assertEqual(project_lock_observation(self.root),
                         {'locked': False, 'ownedByCurrentThread': False, 'protocol': 'stable-flock-v1'})
        self.assertEqual((self.root / '.gnovel/structure.lock').stat().st_ino, self.lock_inode)
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def assert_preview_rejected(self, item, *, code=None, **overrides):
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.preview(items=item if isinstance(item, list) else [item], **overrides)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.inventory(), before)
        return caught.exception

    def rewrite_plan(self, preview, plan):
        plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
        write_json(self.plan_path(preview), plan)
        return {**preview, 'planHash': plan['planHash']}

    def test_measurements_convert_together_with_bound_hashes_diff_and_receipt(self):
        prose = self.root / '正文/作者稿.md'
        prose.parent.mkdir()
        prose.write_text('人物甲站在门旁。\n', encoding='utf-8')
        before = (self.root / '.gnovel/structure.json').read_bytes()
        preview = self.preview()
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)
        self.assertEqual({row['recordId'] for row in preview['diff']},
                         {'FD-HEIGHT', 'AT-HEIGHT', 'AT-BETA-HEIGHT'})
        plan = read_json(self.plan_path(preview))
        row = plan['parameters']['proposals'][0]
        self.assertEqual(row['operation'], 'canon_field_migration_proposal')
        proposal = row['proposal']
        self.assertEqual(proposal['expectedOldRecordHash'], fingerprint(self.source['fieldDefinitions'][0]))
        self.assertFalse(proposal['approval']['verifiedHuman'])
        bound = {item['attributeId']: item for item in proposal['attributeMigrations']}
        self.assertEqual(set(bound), {'AT-HEIGHT', 'AT-BETA-HEIGHT'})
        for aid, binding in bound.items():
            self.assertEqual(binding['expectedOldRecordHash'], fingerprint(self.attribute(aid)))
            self.assertEqual(binding['newRecordHash'], fingerprint(self.attribute(aid, plan['proposedStructure'])))
            self.assertIsNone(binding['sourceChapterId'])
        receipt = self.commit(preview)
        current = load_structure(self.root)
        self.assertEqual(current['fieldDefinitions'][0]['valueType'], 'number')
        for aid, value in [('AT-HEIGHT', 190), ('AT-BETA-HEIGHT', 180)]:
            old, new = self.attribute(aid), self.attribute(aid, current)
            expected = deepcopy(old)
            expected['values'][0]['value'] = value
            self.assertEqual(new, expected)
        self.assertEqual(current['fragments'], self.source['fragments'])
        self.assertEqual(current['entities'], self.source['entities'])
        self.assertEqual(current['fieldDefinitions'][1:], self.source['fieldDefinitions'][1:])
        self.assertFalse(receipt['impact']['proseWrite'])
        self.assertEqual(prose.read_text(), '人物甲站在门旁。\n')
        journal = read_json(self.root / '.gnovel/operations' / (preview['operationId'] + '.json'))
        self.assertEqual(journal['proposals'], plan['parameters']['proposals'])
        self.assertEqual(query_change_report(self.root, operationId=preview['operationId'],
                                            section='rationale')['items'], [row])

    def test_key_rename_updates_queries_without_replacing_stable_identities(self):
        self.commit(self.preview(items=[self.key_item()]))
        source = load_structure(self.root)
        self.assertEqual(source['attributes'], self.source['attributes'])
        self.assertEqual(source['fragments'], self.source['fragments'])
        by_key = query_source(self.root, selection={'includeFields': ['appearance.height_cm']}, view='detail')
        by_id = query_source(self.root, selection={'includeFields': ['FD-HEIGHT']}, view='detail')
        self.assertEqual(by_key['items'], by_id['items'])
        settings = query_settings_source(self.root, source, selector='C-ALPHA', fields=['FD-HEIGHT'])
        self.assertIn('appearance.height_cm', settings['matches'][0]['fields'])
        self.assertNotIn('appearance.height', settings['matches'][0]['fields'])
        with self.assertRaises(StructureError):
            query_source(self.root, selection={'includeFields': ['appearance.height']})

    def test_scope_expansion_then_safe_narrowing_preserves_instances(self):
        for request, scope in [('expand', ['character', 'prop']), ('narrow', ['character'])]:
            item = self.height_item(field_patch=[{'op': 'set', 'path': ['appliesTo'], 'value': scope}],
                                    values=self.unchanged_heights())
            self.commit(self.preview(items=[item], requestId=request))
            source = load_structure(self.root)
            self.assertEqual(source['fieldDefinitions'][0]['appliesTo'], scope)
            self.assertEqual(source['attributes'], self.source['attributes'])
            self.source = source

    def test_scope_cannot_exclude_an_existing_owner_or_use_unknown_owner_kind(self):
        for scope in (['prop'], ['character', 'imaginary-kind'], []):
            item = self.height_item(field_patch=[{'op': 'set', 'path': ['appliesTo'], 'value': scope}],
                                    values=self.unchanged_heights())
            with self.subTest(scope=scope):
                self.assert_preview_rejected(item)

    def test_duplicate_field_key_and_invalid_value_type_reject_entire_preview(self):
        for field, value in [('key', 'appearance.skin'), ('valueType', 'decimal'), ('key', ' ')]:
            item = self.height_item(field_patch=[{'op': 'set', 'path': [field], 'value': value}],
                                    values=self.unchanged_heights())
            with self.subTest(field=field, value=value):
                self.assert_preview_rejected(item)

    def test_every_known_time_segment_is_explicit_and_unknown_states_are_preserved(self):
        first = self.attribute('AT-HEIGHT')['values'][0]
        first['state'] = 'known'
        first['validDuring']['toId'] = 'TA-LATER'
        self.attribute('AT-HEIGHT')['values'].append({
            'valueId': 'FV-HEIGHT-LATER', 'state': 'unknown', 'validDuring': {'fromId': 'TA-LATER'}})
        self.source['attributes'].append({'attributeId': 'AT-EMPTY-HEIGHT', 'ownerId': 'TA-UNKNOWN',
                                         'fieldId': 'FD-HEIGHT', 'values': []})
        self.source['fieldDefinitions'][0]['appliesTo'].append('time_anchor')
        self.install_fixture()
        item = self.height_item()
        item['attributeMigrations'].append({'attributeId': 'AT-EMPTY-HEIGHT', 'values': [],
            'evidence': [{'recordHash': fingerprint(self.attribute('AT-EMPTY-HEIGHT'))}]})
        self.commit(self.preview(items=[item]))
        current = load_structure(self.root)
        before = self.attribute('AT-HEIGHT')['values']
        after = self.attribute('AT-HEIGHT', current)['values']
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[0]['validDuring'], before[0]['validDuring'])
        self.assertEqual(after[0]['state'], 'known')
        self.assertEqual(self.attribute('AT-EMPTY-HEIGHT', current), self.attribute('AT-EMPTY-HEIGHT'))

    def test_unfilled_and_not_applicable_values_cannot_be_silently_reinterpreted(self):
        self.attribute('AT-HEIGHT')['values'][0] = {'valueId': 'FV-HEIGHT', 'state': 'unfilled'}
        self.attribute('AT-BETA-HEIGHT')['values'][0] = {'valueId': 'FV-BETA-HEIGHT', 'state': 'not_applicable'}
        self.install_fixture()
        item = self.height_item()
        for entry in item['attributeMigrations']:
            entry['values'] = []
        self.commit(self.preview(items=[item]))
        self.assertEqual(load_structure(self.root)['attributes'], self.source['attributes'])

    def test_multiple_known_values_keep_their_ids_and_temporal_order(self):
        self.attribute('AT-HEIGHT')['values'][0]['validDuring']['toId'] = 'TA-LATER'
        self.attribute('AT-HEIGHT')['values'].append({'valueId': 'FV-HEIGHT-LATER',
            'value': {'number': 191, 'unit': 'cm'}, 'validDuring': {'fromId': 'TA-LATER'}})
        self.install_fixture()
        item = self.height_item()
        item['attributeMigrations'][0]['values'].insert(0, {'valueId': 'FV-HEIGHT-LATER', 'value': 191})
        self.commit(self.preview(items=[item]))
        actual = self.attribute('AT-HEIGHT', load_structure(self.root))['values']
        self.assertEqual([row['valueId'] for row in actual], ['FV-HEIGHT', 'FV-HEIGHT-LATER'])
        self.assertEqual([row['value'] for row in actual], [190, 191])

    def test_missing_extra_duplicate_and_archived_attribute_coverage_is_rejected(self):
        self.source['entities'][1]['status'] = 'archived'
        self.install_fixture()
        mutations = [lambda entries: entries.pop(),
                     lambda entries: entries.append(deepcopy(entries[0])),
                     lambda entries: entries[1].update(attributeId='AT-SKIN'),
                     lambda entries: entries[1].update(attributeId='AT-MISSING')]
        for mutate in mutations:
            item = self.height_item()
            mutate(item['attributeMigrations'])
            with self.subTest(mutate=mutate):
                self.assert_preview_rejected(item)
        self.commit(self.preview())
        self.assertEqual(self.attribute('AT-BETA-HEIGHT', load_structure(self.root))['values'][0]['value'], 180)

    def test_missing_extra_duplicate_wrong_owner_and_unknown_value_mappings_are_rejected(self):
        mutations = [lambda values: values.clear(),
                     lambda values: values.append(deepcopy(values[0])),
                     lambda values: values.append({'valueId': 'FV-NEW', 'value': 1}),
                     lambda values: values[0].update(valueId='FV-BETA-HEIGHT'),
                     lambda values: values[0].update(state='unknown'),
                     lambda values: values[0].update(validDuring={})]
        for mutate in mutations:
            item = self.height_item()
            mutate(item['attributeMigrations'][0]['values'])
            with self.subTest(mutate=mutate):
                self.assert_preview_rejected(item)
        self.attribute('AT-HEIGHT')['values'][0] = {'valueId': 'FV-HEIGHT', 'state': 'unknown'}
        self.install_fixture()
        self.assert_preview_rejected(self.height_item())

    def test_identity_owner_field_and_unsupported_metadata_are_not_input_authority(self):
        for extra in [{'ownerId': 'C-BETA'}, {'fieldId': 'FD-SKIN'}, {'newRecord': {}},
                      {'expectedOldRecordHash': fingerprint(self.attribute('AT-HEIGHT'))}]:
            item = self.height_item()
            item['attributeMigrations'][0].update(extra)
            with self.subTest(extra=extra):
                self.assert_preview_rejected(item)
        item = self.height_item()
        item['patch'].append({'op': 'set', 'path': ['fieldId'], 'value': 'FD-RENAMED'})
        self.assert_preview_rejected(item)

    def test_metadata_only_noop_and_top_level_fact_scope_do_not_use_migration(self):
        for changes in ([{'op': 'set', 'path': ['label'], 'value': '人物高度'}],
                        [{'op': 'set', 'path': ['key'], 'value': 'appearance.height'}]):
            self.assert_preview_rejected(self.height_item(field_patch=changes, values=self.unchanged_heights()))
        for extra in ({'chapterId': 'CH001'}, {'evidenceMode': 'author_declaration'},
                      {'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]}):
            with self.subTest(extra=extra):
                self.assert_preview_rejected({**self.height_item(), **extra})

    def test_key_and_scope_changes_cannot_smuggle_value_changes(self):
        item = self.key_item()
        item['attributeMigrations'][0]['values'][0]['value']['number'] = 191
        self.assert_preview_rejected(item, code='FIELD_MIGRATION_VALUE_CHANGE_FORBIDDEN')
        self.source['fieldDefinitions'][0]['valueType'] = 'number'
        self.attribute('AT-HEIGHT')['values'][0]['value'] = 190
        self.attribute('AT-BETA-HEIGHT')['values'][0]['value'] = 180
        self.install_fixture()
        for value in (190.0, True):
            if value is True:
                self.attribute('AT-HEIGHT')['values'][0]['value'] = 1
                self.install_fixture()
            item = self.key_item()
            item['attributeMigrations'][0]['values'][0]['value'] = value
            with self.subTest(value=value):
                self.assert_preview_rejected(item, code='FIELD_MIGRATION_VALUE_CHANGE_FORBIDDEN')

    def test_explicit_boolean_conversion_records_actual_typed_values(self):
        self.source['fieldDefinitions'][0]['valueType'] = 'number'
        self.attribute('AT-HEIGHT')['values'][0]['value'] = 1
        self.attribute('AT-BETA-HEIGHT')['values'][0]['value'] = 0
        self.install_fixture()
        item = self.height_item(field_patch=[{'op': 'set', 'path': ['valueType'], 'value': 'boolean'}],
                                values={'FV-HEIGHT': True, 'FV-BETA-HEIGHT': False})
        preview = self.preview(items=[item])
        self.assertEqual({row['recordId'] for row in preview['diff']},
                         {'FD-HEIGHT', 'AT-HEIGHT', 'AT-BETA-HEIGHT'})
        self.commit(preview)
        current = load_structure(self.root)
        self.assertIs(self.attribute('AT-HEIGHT', current)['values'][0]['value'], True)
        self.assertIs(self.attribute('AT-BETA-HEIGHT', current)['values'][0]['value'], False)

    def test_conversion_never_guesses_or_coerces_a_typed_value(self):
        for value in ('190', True, None, {'number': 190, 'unit': 'cm'}):
            item = self.height_item()
            item['attributeMigrations'][0]['values'][0]['value'] = value
            with self.subTest(value=value):
                self.assert_preview_rejected(item)

    def test_topic_conversion_never_drops_or_fabricates_values(self):
        item = self.height_item(field_patch=[{'op': 'set', 'path': ['valueType'], 'value': 'topic'}],
                                values=self.unchanged_heights())
        self.assert_preview_rejected(item)
        item = {'kind': 'field_migration', 'recordId': 'FD-LIFE',
                'patch': [{'op': 'set', 'path': ['valueType'], 'value': 'text'}],
                'attributeMigrations': [{'attributeId': 'AT-LIFE', 'values': [],
                    'evidence': [{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}]}],
                'reason': '保留空值槽，未来可填写文本', 'evidence': []}
        self.commit(self.preview(items=[item]))
        self.source = load_structure(self.root)
        self.assertEqual(self.attribute('AT-LIFE')['values'], [])
        item['patch'][0]['value'] = 'topic'
        self.commit(self.preview(items=[item], requestId='restore-topic'))
        self.assertEqual(self.attribute('AT-LIFE', load_structure(self.root))['values'], [])

    def test_reference_conversion_updates_rendering_and_real_dependency_edges(self):
        item = {'kind': 'field_migration', 'recordId': 'FD-SKIN',
                'patch': [{'op': 'set', 'path': ['key'], 'value': 'appearance.comparison'},
                          {'op': 'set', 'path': ['valueType'], 'value': 'reference'}],
                'attributeMigrations': [{'attributeId': 'AT-SKIN',
                    'values': [{'valueId': 'FV-SKIN', 'value': 'C-BETA'}],
                    'evidence': [{'fragmentId': 'SF-LOOK', 'quote': '他高大而白皙。'}]}],
                'reason': '以已登记人物记录对照对象', 'evidence': []}
        invalid = deepcopy(item)
        invalid['attributeMigrations'][0]['values'][0]['value'] = 'C-MISSING'
        self.assert_preview_rejected(invalid)
        self.commit(self.preview(items=[item]))
        self.source = load_structure(self.root)
        self.assertIn(('C-BETA', 'FV-SKIN', 'reference_value'), dependency_edges(self.source))
        self.assertIn('肤色：人物乙', render_source(self.root, selection={'includeFields': ['FD-SKIN']})['markdown'])
        item['patch'] = [{'op': 'set', 'path': ['valueType'], 'value': 'text'}]
        item['attributeMigrations'][0]['values'][0]['value'] = '人物乙'
        self.commit(self.preview(items=[item], requestId='reference-to-text'))
        self.assertNotIn(('C-BETA', 'FV-SKIN', 'reference_value'), dependency_edges(load_structure(self.root)))

    def test_valid_field_conversion_still_cannot_break_an_existing_count_rule(self):
        self.source['fieldDefinitions'][0]['valueType'] = 'number'
        self.attribute('AT-HEIGHT')['values'][0]['value'] = 1
        self.attribute('AT-BETA-HEIGHT')['values'][0]['value'] = 2
        self.source['entities'].append({'entityId': 'RU-LIMIT', 'entityType': 'rule', 'name': '已登记上限',
            'ruleSpec': {'version': '1', 'method': 'mechanical', 'enforcement': 'required',
                'scopeIds': [self.source['projectId']], 'actions': ['publish'], 'inputIds': ['C-ALPHA'],
                'evaluation': {'kind': 'count_at_most', 'eventIds': [],
                               'limitValueId': 'FV-HEIGHT', 'complete': True}}})
        self.install_fixture()
        item = self.height_item(field_patch=[{'op': 'set', 'path': ['valueType'], 'value': 'text'}],
                                values={'FV-HEIGHT': '1', 'FV-BETA-HEIGHT': '2'})
        error = self.assert_preview_rejected(item)
        self.assertIn('SOURCE_RULE_INVALID', json.dumps(error.details))

    def test_original_attribute_evidence_rules_apply_to_every_declared_attribute(self):
        cases = [(0, []), (0, [{'recordHash': fingerprint(self.attribute('AT-HEIGHT'))}]),
                 (0, [{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}]),
                 (0, [{'fragmentId': 'SF-HEIGHT', 'quote': '并不存在的引文'}]),
                 (1, [{'recordHash': '0' * 64}])]
        for index, evidence in cases:
            item = self.height_item()
            item['attributeMigrations'][index]['evidence'] = evidence
            with self.subTest(index=index, evidence=evidence):
                self.assert_preview_rejected(item)
        self.source['fragments'][0]['contentRole'] = 'plan'
        self.install_fixture()
        self.assert_preview_rejected(self.height_item())

    def test_each_attribute_can_use_a_resolved_chapter_scope_for_bound_fact_evidence(self):
        chapter = single_source_chapter(self.source, 'CH-EVIDENCE',
                                        volume_id=self.source['volumes'][0]['volumeId'])
        self.source['chapters'].append(chapter)
        self.source['chapterArcLinks'].append({'chapterId': 'CH-EVIDENCE',
            'arcId': self.source['arcs'][0]['arcId'], 'role': 'primary'})
        self.source['references'].append({'sourceId': 'CH-EVIDENCE', 'targetId': 'C-BETA'})
        self.source['fragments'].append({'fragmentId': 'SF-BETA-CHAPTER', 'ownerId': 'CH-EVIDENCE',
            'contentRole': 'fact', 'order': 1, 'text': '人物乙身高一米八。', 'contentBindings': ['AT-BETA-HEIGHT']})
        self.install_fixture()
        item = self.height_item()
        entry = item['attributeMigrations'][1]
        entry['evidence'] = [{'fragmentId': 'SF-BETA-CHAPTER', 'quote': '人物乙身高一米八。'}]
        self.assert_preview_rejected(item)
        entry['chapterId'] = 'CH001'
        preview = self.preview(items=[item])
        proposal = read_json(self.plan_path(preview))['parameters']['proposals'][0]['proposal']
        saved = next(row for row in proposal['attributeMigrations'] if row['attributeId'] == 'AT-BETA-HEIGHT')
        self.assertEqual(saved['sourceChapterId'], 'CH-EVIDENCE')
        self.commit(preview)

    def test_unknown_or_unrelated_chapter_scope_is_rejected(self):
        self.source['chapters'].append(single_source_chapter(self.source, 'CH-UNRELATED',
                                      volume_id=self.source['volumes'][0]['volumeId']))
        self.source['chapterArcLinks'].append({'chapterId': 'CH-UNRELATED',
            'arcId': self.source['arcs'][0]['arcId'], 'role': 'primary'})
        self.install_fixture()
        for chapter in ('CH-MISSING', 'CH-UNRELATED'):
            item = self.height_item()
            item['attributeMigrations'][1]['chapterId'] = chapter
            with self.subTest(chapter=chapter):
                self.assert_preview_rejected(item)

    def test_mixed_fact_change_commits_with_migration_and_late_failure_writes_nothing(self):
        fact = {'kind': 'fact', 'recordId': 'SF-HEIGHT', 'patch': [
                    {'op': 'text', 'path': ['text'], 'original': '一米九', 'replacement': '一百九十厘米'}],
                'reason': '同步单位表述', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]}
        bad = deepcopy(fact)
        bad['evidence'][0]['quote'] = '不存在'
        self.assert_preview_rejected([self.height_item(), bad])
        self.commit(self.preview(items=[self.height_item(), fact]))
        source = load_structure(self.root)
        self.assertEqual(self.attribute('AT-HEIGHT', source)['values'][0]['value'], 190)
        self.assertEqual(source['fragments'][0]['text'], '他身高一百九十厘米。')

    def test_migration_reserves_unchanged_attributes_against_other_batch_items(self):
        attr = {'kind': 'attribute', 'recordId': 'AT-HEIGHT',
                'patch': [{'op': 'set', 'path': ['values', 0, 'value', 'number'], 'value': 185}],
                'reason': '另改数值', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]}
        metadata = {'kind': 'field_definition', 'recordId': 'FD-HEIGHT',
                    'patch': [{'op': 'set', 'path': ['label'], 'value': '人物高度'}],
                    'reason': '另改名称', 'evidence': []}
        for other in (attr, metadata, self.key_item()):
            for items in ([self.key_item(), other], [other, self.key_item()]):
                with self.subTest(other=other['kind'], first=items[0]['kind']):
                    self.assert_preview_rejected(items, code='CANON_BATCH_DUPLICATE')

    def test_commit_rejects_a_separately_valid_duplicate_attribute_proposal(self):
        migration = self.preview(items=[self.key_item()])
        ordinary = self.preview(items=[{'kind': 'attribute', 'recordId': 'AT-HEIGHT',
            'patch': [{'op': 'set', 'path': ['values', 0, 'value', 'number'], 'value': 185}],
            'reason': '另改数值', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]}], requestId='ordinary')
        original = read_json(self.plan_path(migration))
        other = read_json(self.plan_path(ordinary))['parameters']['proposals'][0]
        for rows in ([original['parameters']['proposals'][0], other],
                     [other, original['parameters']['proposals'][0]]):
            plan = deepcopy(original)
            plan['parameters']['proposals'] = deepcopy(rows)
            tampered = self.rewrite_plan(migration, plan)
            before = self.inventory()
            with self.assertRaises(StructureError):
                self.commit(tampered)
            self.assertEqual(self.inventory(), before)

    def test_target_budget_counts_field_and_all_declared_attributes_at_both_boundaries(self):
        policy = deepcopy(workflow_policy())
        policy['maintenance']['canonProposalBatchMaxItems'] = 2
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            self.assert_preview_rejected(self.key_item())
        policy['maintenance']['canonProposalBatchMaxItems'] = 3
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            preview = self.preview(items=[self.key_item()])
        policy['maintenance']['canonProposalBatchMaxItems'] = 2
        before = self.inventory()
        with patch('scripts.core.project.workflow_policy', return_value=policy), self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'CANON_BATCH_LIMIT_EXCEEDED')
        self.assertEqual(self.inventory(), before)
        policy['maintenance']['canonProposalBatchMaxItems'] = 3
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            self.commit(preview)

    def test_target_budget_accumulates_separate_migrations_and_rechecks_commit(self):
        skin = {'kind': 'field_migration', 'recordId': 'FD-SKIN',
                'patch': [{'op': 'set', 'path': ['key'], 'value': 'appearance.complexion'}],
                'attributeMigrations': [{'attributeId': 'AT-SKIN',
                    'values': [{'valueId': 'FV-SKIN', 'value': '白皙'}],
                    'evidence': [{'fragmentId': 'SF-LOOK', 'quote': '他高大而白皙。'}]}],
                'reason': '明确字段语义键', 'evidence': []}
        items = [self.key_item(), skin]
        policy = deepcopy(workflow_policy())
        policy['maintenance']['canonProposalBatchMaxItems'] = 4
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            self.assert_preview_rejected(items, code='CANON_BATCH_LIMIT_EXCEEDED')
        policy['maintenance']['canonProposalBatchMaxItems'] = 5
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            preview = self.preview(items=items)
        policy['maintenance']['canonProposalBatchMaxItems'] = 4
        before = self.inventory()
        with patch('scripts.core.project.workflow_policy', return_value=policy), self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'CANON_BATCH_LIMIT_EXCEEDED')
        self.assertEqual(self.inventory(), before)
        policy['maintenance']['canonProposalBatchMaxItems'] = 5
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            self.commit(preview)
        current = load_structure(self.root)
        self.assertEqual(current['attributes'], self.source['attributes'])
        self.assertEqual(current['fieldDefinitions'][1]['key'], 'appearance.complexion')

    def test_semantic_allowlist_is_consumed_at_preview_and_commit(self):
        policy = deepcopy(source_model_policy())
        policy['mutationPolicy']['referencedFieldDefinitions']['semanticFields'] = ['key', 'appliesTo']
        with patch('scripts.core.source_field_migrations.source_model_policy', return_value=policy):
            self.assert_preview_rejected(self.height_item())
        preview = self.preview()
        before = self.inventory()
        with patch('scripts.core.source_field_migrations.source_model_policy', return_value=policy), self.assertRaises(StructureError):
            self.commit(preview)
        self.assertEqual(self.inventory(), before)

    def test_declared_approval_does_not_replace_explicit_commit_approval(self):
        for name in ('approvalActor', 'approvalEvidence'):
            with self.subTest(name=name):
                self.assert_preview_rejected(self.height_item(), **{name: ' '})
        preview = self.preview()
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.commit(preview, approved=False)
        self.assertEqual(caught.exception.code, 'APPROVAL_REQUIRED')
        self.assertEqual(self.inventory(), before)

    def test_generic_and_metadata_routes_remain_closed_to_semantic_changes(self):
        field = {**self.source['fieldDefinitions'][0], 'key': 'appearance.height_cm'}
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'fieldDefinitions', 'record': field}],
                                  requestId='generic', baseRevision=self.source['revision'], reason='未审语义修改')
        self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')
        self.assertEqual(self.inventory(), before)
        item = self.key_item()
        item['kind'] = 'field_definition'
        item.pop('attributeMigrations')
        self.assert_preview_rejected(item, code='FIELD_DEFINITION_MIGRATION_REQUIRED')

    def test_same_request_resumes_and_undo_restores_values_at_new_revision(self):
        preview = self.preview()
        before = self.inventory()
        self.assertEqual(self.preview(), preview)
        self.assertEqual(self.inventory(), before)
        self.commit(preview)
        revision = load_structure(self.root)['revision']
        before = self.inventory()
        self.assertEqual(self.preview()['operationState'], 'committed')
        self.assertEqual(self.inventory(), before)
        undo(self.root, preview['operationId'], actor='test')
        restored = load_structure(self.root)
        self.assertGreater(restored['revision'], revision)
        self.assertEqual(restored['fieldDefinitions'], self.source['fieldDefinitions'])
        self.assertEqual(restored['attributes'], self.source['attributes'])
        before = self.inventory()
        self.assertEqual(self.preview()['operationState'], 'undone')
        self.assertEqual(self.inventory(), before)

    def test_changed_request_or_source_cannot_reuse_old_approval(self):
        preview = self.preview()
        self.assert_preview_rejected(self.height_item(), approvalEvidence='不同批准', code='SOURCE_REQUEST_REUSED')
        # Deliberate outside edit is a stale-input probe, never an accepted source update.
        changed = deepcopy(self.source)
        changed['fragments'][0]['text'] = '作者又改了资料。'
        write_json(self.root / '.gnovel/structure.json', changed)
        self.assert_preview_rejected(self.height_item(), requestId='fresh', code='SESSION_REF_STALE')
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(self.inventory(), before)

    def test_recomputed_plan_hash_cannot_forge_review_metadata_or_omit_coverage(self):
        preview = self.preview()
        original = read_json(self.plan_path(preview))
        mutations = [lambda proposal: proposal.update(sourceChapterId='CH-MISSING'),
                     lambda proposal: proposal.update(expectedOldRecordHash='0' * 64),
                     lambda proposal: proposal.update(newRecordHash='0' * 64),
                     lambda proposal: proposal.update(evidence=[]),
                     lambda proposal: proposal['approval'].update(verifiedHuman=True),
                     lambda proposal: proposal['approval'].update(anotherActor='fake'),
                     lambda proposal: proposal['attributeMigrations'].pop(),
                     lambda proposal: proposal['attributeMigrations'].append(deepcopy(proposal['attributeMigrations'][0])),
                     lambda proposal: proposal['attributeMigrations'][0].update(expectedOldRecordHash='0' * 64),
                     lambda proposal: proposal['attributeMigrations'][0].update(newRecordHash='0' * 64),
                     lambda proposal: proposal['attributeMigrations'][0].update(sourceChapterId='CH-MISSING'),
                     lambda proposal: proposal['attributeMigrations'][0].update(ownerId='C-BETA'),
                     lambda proposal: proposal['attributeMigrations'][0].update(evidence=[])]
        for mutate in mutations:
            plan = deepcopy(original)
            mutate(plan['parameters']['proposals'][0]['proposal'])
            tampered = self.rewrite_plan(preview, plan)
            before = self.inventory()
            with self.subTest(mutate=mutate), self.assertRaises(StructureError):
                self.commit(tampered)
            self.assertEqual(self.inventory(), before)

    def test_rehashed_attribute_cannot_move_identity_state_time_or_record_order(self):
        preview = self.preview()
        original = read_json(self.plan_path(preview))
        mutations = [lambda attribute: attribute.update(ownerId='C-BETA'),
                     lambda attribute: attribute.update(fieldId='FD-SKIN'),
                     lambda attribute: attribute['values'][0].update(valueId='FV-NEW'),
                     lambda attribute: attribute['values'][0].update(state='known'),
                     lambda attribute: attribute['values'][0].update(validDuring={'fromId': 'TA-LATER'}),
                     lambda attribute: attribute['values'].clear()]
        for mutate in mutations:
            plan = deepcopy(original)
            attribute = self.attribute('AT-HEIGHT', plan['proposedStructure'])
            mutate(attribute)
            proposal = plan['parameters']['proposals'][0]['proposal']
            next(row for row in proposal['attributeMigrations'] if row['attributeId'] == 'AT-HEIGHT')['newRecordHash'] = fingerprint(attribute)
            tampered = self.rewrite_plan(preview, plan)
            before = self.inventory()
            with self.subTest(mutate=mutate), self.assertRaises(StructureError):
                self.commit(tampered)
            self.assertEqual(self.inventory(), before)
        for collection in ('attributes', 'fieldDefinitions', 'fragments'):
            plan = deepcopy(original)
            plan['proposedStructure'][collection].reverse()
            with self.subTest(collection=collection), self.assertRaises(StructureError):
                require_canon_proposal(self.source, plan['proposedStructure'], plan)

    def test_recomputed_plan_hash_cannot_change_undeclared_records(self):
        preview = self.preview()
        original = read_json(self.plan_path(preview))
        for mutate in (lambda source: source['entities'][0].update(name='夹带改名'),
                       lambda source: source['fragments'][0].update(text='夹带正文事实'),
                       lambda source: self.attribute('AT-SKIN', source)['values'][0].update(value='其他肤色')):
            plan = deepcopy(original)
            mutate(plan['proposedStructure'])
            tampered = self.rewrite_plan(preview, plan)
            before = self.inventory()
            with self.subTest(mutate=mutate), self.assertRaises(StructureError):
                self.commit(tampered)
            self.assertEqual(self.inventory(), before)

    def test_failed_transaction_restores_original_source_and_can_resume_request(self):
        preview = self.preview()
        original_source = (self.root / '.gnovel/structure.json').read_bytes()
        original_plan = self.plan_path(preview).read_bytes()

        def fail(phase, relative=None):
            if phase == 'write' and relative == '.gnovel/structure.json':
                raise OSError('injected field migration write failure')

        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(OSError):
            self.commit(preview)
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), original_source)
        self.assertEqual(self.plan_path(preview).read_bytes(), original_plan)
        self.assertFalse((self.root / REVIEW_PATH).exists())
        self.assertFalse((self.root / '.gnovel/operations' / (preview['operationId'] + '.json')).exists())
        resumed = self.preview()
        self.assertEqual(resumed['operationId'], preview['operationId'])
        self.commit(resumed)
        self.assertEqual(load_structure(self.root)['fieldDefinitions'][0]['valueType'], 'number')

    def test_cli_and_mcp_share_the_existing_batch_operation_and_schema(self):
        items = [self.height_item()]
        item_file = self.root / 'migration-items.json'
        write_json(item_file, items)
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source',
            'proposal-batch-preview', '--project-root', str(self.root), '--source-hash', snapshot_hash(self.source),
            '--items', '@' + str(item_file), '--request-id', 'migrate-height', '--approval-actor', '测试作者',
            '--approval-evidence', '已确认字段及列出的全部属性转换'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        from scripts.mcp_server import handle
        args = {'projectRoot': str(self.root), 'sourceHash': snapshot_hash(self.source), 'items': items,
                'requestId': 'migrate-height', 'approvalActor': '测试作者',
                'approvalEvidence': '已确认字段及列出的全部属性转换'}
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preview_source_proposal_batch', 'arguments': args}})['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent'], json.loads(cli.stdout))
        args['items'][0]['attributeMigrations'][0]['ownerId'] = 'C-BETA'
        response = handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'preview_source_proposal_batch', 'arguments': args}})['result']
        self.assertTrue(response['isError'], response)

    def test_only_related_chapter_approvals_and_semantic_receipts_become_stale(self):
        self.source['fieldDefinitions'].append({'fieldId': 'FD-PLAN', 'key': 'plot.plan', 'label': '计划',
                                               'valueType': 'topic', 'appliesTo': ['chapter']})
        pairs = [('CH-ALPHA', 'FV-HEIGHT'), ('CH-BETA', 'FV-BETA-HEIGHT'), ('CH-OTHER', 'FV-SKIN')]
        for ordinal, (cid, dependency) in enumerate(pairs, 1):
            self.source['chapters'].append(single_source_chapter(self.source, cid, ordinal=ordinal,
                                          volume_id=self.source['volumes'][0]['volumeId']))
            self.source['chapterArcLinks'].append({'chapterId': cid, 'arcId': self.source['arcs'][0]['arcId'], 'role': 'primary'})
            self.source['attributes'].append({'attributeId': 'AT-' + cid, 'ownerId': cid,
                                              'fieldId': 'FD-PLAN', 'values': []})
            self.source['fragments'].append({'fragmentId': 'SF-' + cid, 'ownerId': cid, 'contentRole': 'plan',
                'text': '核对当前资料后出发。', 'order': 1, 'contentBindings': ['AT-' + cid], 'dependsOn': [dependency],
                'chapterContract': {'targetRange': [1, 10000], 'mode': 'plot', 'intensityLevel': 1,
                                    'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}})
        self.install_fixture()
        workflow = default_workflow()
        workflow['approvedChapterIds'] = [cid for cid, _ in pairs]
        for cid in workflow['approvedChapterIds']:
            payload = assessed_review_payload(self.source, cid, 'stage_a_review', {
                'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': [],
                'sourcePlanHash': chapter_plan(self.source, cid)['sourcePlanHash']})
            artifact = envelope('stage_a_review', payload, project_id=self.source['projectId'], chapter_id=cid)
            path = self.root / '工作台' / (cid + '-a.json')
            write_json(path, artifact)
            record_artifact(workflow, cid, artifact, structure=self.source, artifact_path=str(path),
                            project_id=self.source['projectId'], project_root=self.root)
            approve_artifacts(workflow, cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.gnovel/workflow.json', workflow)
        pending = {row['recordId']: row for row in query_reviews(self.root)['items']}
        texts = {row['fragmentId']: row['text'] for row in self.source['fragments']}
        reviews = [{key: pending[rid][key] for key in ('recordId', 'inputHash', 'contentHash')} | {
            'decision': 'compatible', 'reason': '中性测试当前输入复核',
            'evidence': [{'start': 0, 'end': len(texts[rid]), 'text': texts[rid]}]}
            for rid in ('SF-LIFE', 'SF-CH-ALPHA', 'SF-CH-BETA', 'SF-CH-OTHER')]
        submit_review(self.root, requestId='pre-migration-reviews', reviews=reviews)
        before_inputs = ReviewInputs(self.source)
        preview = self.preview()
        self.assertEqual(set(preview['impact']['approvalInvalidations']), {'CH-ALPHA', 'CH-BETA'})
        self.commit(preview)
        current = load_structure(self.root)
        after_inputs = ReviewInputs(current)
        self.assertNotEqual(before_inputs.fingerprint('SF-LIFE'), after_inputs.fingerprint('SF-LIFE'))
        self.assertEqual(before_inputs.fingerprint('SF-CH-OTHER'), after_inputs.fingerprint('SF-CH-OTHER'))
        states = {row['recordId']: row['status'] for row in query_reviews(self.root)['items']}
        self.assertEqual(states['SF-CH-ALPHA'], 'pending')
        self.assertEqual(states['SF-CH-BETA'], 'pending')
        self.assertEqual(states['SF-CH-OTHER'], 'compatible')
        current_workflow = read_json(self.root / '.gnovel/workflow.json')
        for cid in ('CH-ALPHA', 'CH-BETA'):
            self.assertFalse(current_workflow['approvals'][cid]['approved'])
        self.assertEqual(current_workflow['approvals']['CH-OTHER'], workflow['approvals']['CH-OTHER'])
        self.assertEqual(current_workflow['artifacts']['CH-OTHER'], workflow['artifacts']['CH-OTHER'])


if __name__ == '__main__':
    unittest.main()
