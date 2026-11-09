"""Mixed settings updates remain old-value-bound, atomic and explicitly approved."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.canon_guard import require_canon_proposal
from scripts.core.jsonio import read_json, write_json
from scripts.core.locks import project_lock, project_lock_observation
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT, canon_proposal_batch_max_items, workflow_policy
from scripts.core.source_maintenance import check_maintenance
from scripts.core.source_model import snapshot_hash
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.source_reports import query_change_report
from scripts.core.source_requests import query_request_status
from scripts.core.structure_commands import _plan_hash
from scripts.core.structure_store import StructureError, load_structure, save_structure, validate_structure
from scripts.core.structure_transaction import commit, undo
from scripts.core.write_sets import digest
from tests.fixtures_source import source_fixture, single_source_chapter
from tests.fixtures_current import initialize_project


def batch_items():
    return [
        {'kind': 'entity', 'recordId': 'C-ALPHA', 'patch': [{'op': 'set', 'path': ['summary'], 'value': '守门人'}],
         'reason': '核对人物摘要', 'evidence': [{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}]},
        {'kind': 'attribute', 'recordId': 'AT-HEIGHT',
         'patch': [{'op': 'set', 'path': ['values', 0, 'value', 'number'], 'value': 185}],
         'reason': '核对属性', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}]},
        {'kind': 'fact', 'recordId': 'SF-HEIGHT',
         'patch': [{'op': 'text', 'path': ['text'], 'original': '一米九', 'replacement': '一米八五'}],
         'reason': '统一事实表述', 'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]},
        {'kind': 'relation', 'recordId': 'REL-AB', 'patch': [{'op': 'set', 'path': ['kind'], 'value': 'trusts'}],
         'reason': '确认关系', 'evidenceMode': 'author_declaration', 'evidence': []},
    ]


class SourceProposalBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        self.source['relations'] = [{'relationId': 'REL-AB', 'fromEntity': 'C-ALPHA',
                                     'toEntity': 'C-BETA', 'kind': 'protects'}]
        save_structure(self.root, self.source)
        self.source = load_structure(self.root)
        with project_lock(self.root):
            self.assertTrue(project_lock_observation(self.root)['ownedByCurrentThread'])
        self.lock_inode = (self.root / '.gnovel/structure.lock').stat().st_ino
        self.arguments = {'sourceHash': snapshot_hash(self.source), 'items': batch_items(),
                          'requestId': 'mixed-1', 'approvalActor': 'author', 'approvalEvidence': '已确认方案'}

    def preview(self, **overrides):
        return preview_proposal_batch(self.root, **{**self.arguments, **overrides})

    def inventory(self):
        self.assertEqual(project_lock_observation(self.root),
                         {'locked': False, 'ownedByCurrentThread': False, 'protocol': 'stable-flock-v1'})
        self.assertEqual((self.root / '.gnovel/structure.lock').stat().st_ino, self.lock_inode)
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def plan_file(self, preview):
        return self.root / '.gnovel/plans' / (preview['operationId'] + '.json')

    def commit(self, preview, approved=True):
        return commit(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                      approved=approved, actor='test')

    def test_four_kinds_commit_together_preserve_original_proposals_and_undo(self):
        original = (self.root / '.gnovel/structure.json').read_bytes()
        preview = self.preview()
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), original)
        plan = read_json(self.plan_file(preview))
        self.assertEqual(len(plan['parameters']['proposals']), 4)
        self.assertTrue(all(row['proposal']['approval']['verifiedHuman'] is False for row in plan['parameters']['proposals']))
        before_rationale = query_change_report(self.root, operationId=preview['operationId'], section='rationale')['items']
        receipt = self.commit(preview)
        source = load_structure(self.root)
        self.assertEqual(source['revision'], self.source['revision'] + 1)
        self.assertEqual(source['entities'][0]['summary'], '守门人')
        self.assertEqual(source['attributes'][0]['values'][0]['value']['number'], 185)
        self.assertIn('一米八五', source['fragments'][0]['text'])
        self.assertEqual(source['relations'][0]['kind'], 'trusts')
        journal = read_json(self.root / '.gnovel/operations' / (preview['operationId'] + '.json'))
        self.assertEqual(journal['proposals'], plan['parameters']['proposals'])
        self.assertEqual(query_change_report(self.root, operationId=preview['operationId'], section='rationale')['items'], before_rationale)
        self.assertEqual(receipt['code'], 'STRUCTURE_COMMITTED')
        undo(self.root, preview['operationId'], actor='test')
        restored = load_structure(self.root)
        for collection in ('entities', 'attributes', 'fragments', 'relations'):
            self.assertEqual(restored[collection], self.source[collection])

    def test_batch_matches_the_existing_single_record_workflow(self):
        self.commit(self.preview())
        batched = load_structure(self.root)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = initialize_project(root)
            for collection in ('entities', 'attributes', 'fieldDefinitions', 'fragments', 'relations'):
                source[collection] = deepcopy(self.source[collection])
            save_structure(root, source)
            calls = []
            for item in batch_items():
                kind = item['kind']
                if kind == 'entity':
                    resolver, resolver_args = 'source.canon-resolve', {'selector': item['recordId']}
                    operation = 'source.canon-proposal-preview'
                elif kind == 'relation':
                    resolver, resolver_args = 'source.relation-resolve', {'relationId': item['recordId']}
                    operation = 'source.relation-proposal-preview'
                else:
                    resolver, resolver_args = 'source.content-resolve', {'recordId': item['recordId'], 'kind': kind}
                    operation = 'source.content-proposal-preview'
                resolved = call_operation(resolver, root, resolver_args)
                arguments = {'sessionRef': resolved['sessionRef'], 'expectedRecordHash': resolved['recordHash'],
                             'patch': item['patch'], 'reason': item['reason'], 'evidence': item['evidence'],
                             'approvalActor': 'author', 'approvalEvidence': '已确认方案'}
                if kind == 'relation':
                    arguments['evidenceMode'] = item['evidenceMode']
                preview = call_operation(operation, root, arguments)
                call_operation('project.structure.commit', root, {
                    'operationId': preview['operationId'], 'planHash': preview['planHash'], 'approve': True})
                calls.extend((resolver, operation, 'project.structure.commit'))
            sequential = load_structure(root)
            self.assertEqual(len(calls), 12)
            for collection in ('entities', 'attributes', 'fragments', 'relations'):
                self.assertEqual(batched[collection], sequential[collection])

    def test_same_request_resumes_preview_commit_and_undo_without_replaying(self):
        preview = self.preview()
        before = self.inventory()
        self.assertEqual(self.preview(), preview)
        self.assertEqual(self.inventory(), before)
        self.assertEqual(query_request_status(self.root, requestId='mixed-1')['operationState'], 'prepared')
        self.commit(preview)
        before = self.inventory()
        self.assertEqual(self.preview()['operationState'], 'committed')
        self.assertEqual(self.inventory(), before)
        undo(self.root, preview['operationId'], actor='test')
        before = self.inventory()
        self.assertEqual(self.preview()['operationState'], 'undone')
        self.assertEqual(self.inventory(), before)

    def test_changed_request_and_stale_inputs_never_rebind(self):
        self.preview()
        for field, value in (('approvalActor', 'other'), ('approvalEvidence', 'other'), ('items', batch_items()[:1])):
            with self.subTest(field=field), self.assertRaises(StructureError) as caught:
                self.preview(**{field: value})
            self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')
        source = load_structure(self.root)
        source['fragments'][1]['text'] += '外部改动。'
        write_json(self.root / '.gnovel/structure.json', source)
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.preview()
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_STALE')
        with self.assertRaises(StructureError) as caught:
            self.preview(requestId='mixed-2')
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')
        self.assertEqual(before, self.inventory())

    def test_bad_last_item_duplicate_or_wrong_kind_creates_no_partial_plan(self):
        for change, code in (
            (lambda items: items[-1].update(evidenceMode='source'), 'RELATION_EVIDENCE_REQUIRED'),
            (lambda items: items.append(deepcopy(items[0])), 'CANON_BATCH_DUPLICATE'),
            (lambda items: items[-1].update(recordId='REL-MISSING'), 'SOURCE_RECORD_NOT_FOUND'),
            (lambda items: items[-1].update(kind='fact'), 'ARGUMENTS_INVALID'),
        ):
            items = batch_items()
            change(items)
            before = self.inventory()
            with self.subTest(code=code), self.assertRaises(StructureError) as caught:
                self.preview(items=items)
            self.assertEqual(caught.exception.code, code)
            if code != 'ARGUMENTS_INVALID':
                self.assertEqual(caught.exception.details['itemIndex'], len(items) - 1)
            self.assertEqual(before, self.inventory())

    def test_original_field_owner_relation_endpoint_and_evidence_guards_remain(self):
        cases = [
            (0, [{'op': 'set', 'path': ['entityId'], 'value': 'C-OTHER'}], 'CANON_FIELD_INVALID'),
            (0, [{'op': 'set', 'path': ['status'], 'value': []}], 'CANON_VALUE_INVALID'),
            (0, [{'op': 'set', 'path': ['summary'], 'value': 'x'}, {'op': 'set', 'path': ['aliases'], 'value': ['x']}], 'RECORD_PATCH_INVALID'),
            (1, [{'op': 'set', 'path': ['ownerId'], 'value': 'C-BETA'}], 'CONTENT_PROPOSAL_INVALID'),
            (2, [{'op': 'set', 'path': ['contentRole'], 'value': 'plan'}], 'CONTENT_PROPOSAL_INVALID'),
            (3, [{'op': 'set', 'path': ['toEntity'], 'value': 'C-ALPHA'}], 'RELATION_PROPOSAL_INVALID'),
        ]
        for index, patch_value, code in cases:
            items = batch_items()
            items[index]['patch'] = patch_value
            before = self.inventory()
            with self.subTest(index=index, code=code), self.assertRaises(StructureError) as caught:
                self.preview(items=items)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(before, self.inventory())
        for index in (0, 1, 2):
            items = batch_items()
            items[index]['evidence'][0]['quote'] = '不存在的引文'
            with self.subTest(evidence=index), self.assertRaises(StructureError):
                self.preview(items=items)

    def test_display_chapter_scope_is_resolved_once_without_bypassing_registered_references(self):
        source = load_structure(self.root)
        chapter = single_source_chapter(source, 'CH-SCOPE', volume_id=source['volumes'][0]['volumeId'])
        source['chapters'].append(chapter)
        source['chapterArcLinks'].append({'chapterId': chapter['chapterId'], 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
        source['references'].append({'sourceId': chapter['chapterId'], 'targetId': 'C-ALPHA', 'kind': 'uses'})
        write_json(self.root/'.gnovel/structure.json', source)
        source = load_structure(self.root)
        item = {**batch_items()[0], 'chapterId': 'CH001'}
        preview = self.preview(sourceHash=snapshot_hash(source), items=[item])
        plan = read_json(self.plan_file(preview))
        self.assertEqual(plan['parameters']['proposals'][0]['proposal']['sourceChapterId'], 'CH-SCOPE')
        item['recordId'] = 'C-BETA'
        with self.assertRaises(StructureError) as caught:
            self.preview(sourceHash=snapshot_hash(source), items=[item], requestId='out-of-scope')
        self.assertEqual(caught.exception.code, 'SESSION_REF_INVALID')
        self.assertEqual(caught.exception.details['itemIndex'], 0)

    def test_relation_source_evidence_keeps_both_endpoint_requirements(self):
        source = load_structure(self.root)
        source['fragments'].append({'fragmentId': 'SF-RELATION', 'ownerId': 'C-ALPHA', 'text': '甲和乙有约定。',
                                    'order': 90, 'contentRole': 'fact', 'contentBindings': ['AT-LIFE'], 'dependsOn': ['C-BETA']})
        save_structure(self.root, source)
        source = load_structure(self.root)
        item = {**batch_items()[3], 'evidenceMode': 'source',
                'evidence': [{'fragmentId': 'SF-RELATION', 'quote': '甲和乙有约定。'}]}
        preview = self.preview(sourceHash=snapshot_hash(source), items=[item])
        plan = read_json(self.plan_file(preview))
        self.assertEqual(plan['parameters']['proposals'][0]['proposal']['evidenceMode'], 'source')
        item['evidence'] = [{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}]
        with self.assertRaises(StructureError) as caught:
            self.preview(sourceHash=snapshot_hash(source), items=[item], requestId='unrelated-evidence')
        self.assertEqual(caught.exception.code, 'RELATION_EVIDENCE_SCOPE_INVALID')

    def test_commit_still_requires_approval_and_replays_every_child_gate(self):
        preview = self.preview()
        before = (self.root / '.gnovel/structure.json').read_bytes()
        with self.assertRaises(StructureError) as caught:
            self.commit(preview, approved=False)
        self.assertEqual(caught.exception.code, 'APPROVAL_REQUIRED')
        path = self.plan_file(preview)
        plan = read_json(path)
        plan['parameters']['proposals'][2]['proposal']['approval']['verifiedHuman'] = True
        plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
        write_json(path, plan)
        with self.assertRaises(StructureError) as caught:
            self.commit(plan)
        self.assertEqual(caught.exception.code, 'CONTENT_PROPOSAL_SCOPE_INVALID')
        self.assertEqual(caught.exception.details['itemIndex'], 2)
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)

    def test_rehashed_or_relabelled_batch_cannot_change_unlisted_state(self):
        preview = self.preview()
        plan = read_json(self.plan_file(preview))
        for mutate in (
            lambda value: value['proposedStructure']['entities'][1].update(summary='hidden'),
            lambda value: value['proposedStructure']['fragments'].reverse(),
            lambda value: value['proposedStructure']['fieldDefinitions'][0].update(label='hidden'),
            lambda value: value['proposedStructure']['relations'].clear(),
            lambda value: value['parameters']['proposals'][0].update(operation='refactor_rename_entity'),
            lambda value: value['parameters']['proposals'][0].update(operation='canon_proposal_batch'),
            lambda value: value['parameters']['proposals'][0].update(operation=[]),
            lambda value: value['parameters']['proposals'].append(deepcopy(value['parameters']['proposals'][0])),
        ):
            bad = deepcopy(plan)
            mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(StructureError):
                require_canon_proposal(self.source, bad['proposedStructure'], bad)

    def test_batch_bound_and_invalid_registry_values_are_real_consumers(self):
        policy = deepcopy(workflow_policy())
        policy['maintenance']['canonProposalBatchMaxItems'] = 1
        with patch('scripts.core.project.workflow_policy', return_value=policy):
            self.assertEqual(canon_proposal_batch_max_items(), 1)
            with self.assertRaises(StructureError):
                self.preview()
            self.preview(items=batch_items()[:1])
        for value in (None, True, 0, -1, '10'):
            policy['maintenance']['canonProposalBatchMaxItems'] = value
            with self.subTest(value=value), patch('scripts.core.project.workflow_policy', return_value=policy), self.assertRaises(ValueError):
                canon_proposal_batch_max_items()

    def test_old_read_only_schema_and_foreign_snapshot_are_rejected(self):
        with self.assertRaises(StructureError) as caught:
            self.preview(sourceHash='foreign')
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')
        source = deepcopy(self.source)
        source['schemaVersion'] = 2
        write_json(self.root / '.gnovel/structure.json', source)
        with self.assertRaises(StructureError) as caught:
            self.preview(sourceHash=snapshot_hash(source))
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_PROJECT_VERSION')

    def checklist(self):
        (self.root / '工作台').mkdir(exist_ok=True)
        document = self.root / '工作台/方案.md'
        document.write_text('人物摘要；属性数值；事实表述；关系更新。')
        rows = [{'itemId': f'item-{index}', 'quote': text, 'recordIds': [item['recordId']], 'operationIds': []}
                for index, (item, text) in enumerate(zip(batch_items(), ('人物摘要', '属性数值', '事实表述', '关系更新')))]
        write_json(self.root / '工作台/清单.json', {'source': {'path': '工作台/方案.md', 'sha256': digest(document.read_bytes())}, 'items': rows})
        return {'checklistPath': '工作台/清单.json', 'itemIds': [row['itemId'] for row in rows]}

    def test_all_kinds_link_to_maintenance_and_late_external_edit_survives_rollback(self):
        reference = self.checklist()
        preview = self.preview(maintenanceReference=reference)
        original = (self.root / '.gnovel/structure.json').read_bytes()
        document = self.root / '工作台/方案.md'
        changed = document.read_bytes() + b'\nexternal'
        from scripts.core import write_sets
        validate = write_sets.validate_guards

        def change_late(root, guards, *, stage):
            if stage == 'before-committed':
                document.write_bytes(changed)
            return validate(root, guards, stage=stage)

        with patch.object(write_sets, 'validate_guards', side_effect=change_late), self.assertRaises(StructureError):
            self.commit(preview)
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), original)
        self.assertEqual(document.read_bytes(), changed)

    def test_maintenance_coverage_uses_one_receipt_for_all_selected_targets(self):
        reference = self.checklist()
        before = (self.root / reference['checklistPath']).read_bytes()
        preview = self.preview(maintenanceReference=reference)
        self.commit(preview)
        checked = check_maintenance(self.root, checklistPath=reference['checklistPath'])
        self.assertEqual(checked['summary']['submittedItemCount'], 4)
        self.assertTrue(all(row['automaticallyLinkedOperationIds'] == [preview['operationId']] for row in checked['items']))
        self.assertEqual((self.root / reference['checklistPath']).read_bytes(), before)
        self.assertFalse(checked['coverage']['semanticReviewComplete'])

    def test_single_entity_and_relation_proposals_also_link_maintenance(self):
        reference = self.checklist()
        for index, resolver, arguments, operation in (
            (0, 'source.canon-resolve', {'selector': 'C-ALPHA'}, 'source.canon-proposal-preview'),
            (3, 'source.relation-resolve', {'relationId': 'REL-AB'}, 'source.relation-proposal-preview'),
        ):
            item = batch_items()[index]
            resolved = call_operation(resolver, self.root, arguments)
            request = {'sessionRef': resolved['sessionRef'], 'expectedRecordHash': resolved['recordHash'],
                       'patch': item['patch'], 'reason': item['reason'], 'evidence': item['evidence'],
                       'approvalActor': 'author', 'approvalEvidence': '已确认方案',
                       'maintenanceReference': {**reference, 'itemIds': [reference['itemIds'][index]]}}
            if index == 3:
                request['evidenceMode'] = item['evidenceMode']
            self.commit(call_operation(operation, self.root, request))
        checked = check_maintenance(self.root, checklistPath=reference['checklistPath'])
        self.assertEqual(checked['summary']['submittedItemCount'], 2)

    def test_cli_and_mcp_share_request_and_return_original_preview(self):
        (self.root / '工作台').mkdir()
        choices = self.root / '工作台/items.json'
        write_json(choices, self.arguments['items'])
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source',
                              'proposal-batch-preview', '--project-root', str(self.root), '--source-hash', self.arguments['sourceHash'],
                              '--items', '@' + str(choices), '--request-id', 'mixed-1', '--approval-actor', 'author',
                              '--approval-evidence', self.arguments['approvalEvidence']], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        from scripts.mcp_server import handle
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preview_source_proposal_batch', 'arguments': {'projectRoot': str(self.root), **self.arguments}}})['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent'], json.loads(cli.stdout))

    def test_current_binding_source_commits_and_undoes_at_a_higher_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = initialize_project(root, book_name='Bound batch')
            for collection in ('entities', 'attributes', 'fieldDefinitions', 'fragments', 'relations'):
                source[collection] = deepcopy(self.source[collection])
            validate_structure(source)
            save_structure(root, source)
            source = load_structure(root)
            preview = call_operation('source.proposal-batch-preview', root, {**self.arguments, 'sourceHash': snapshot_hash(source)})
            call_operation('project.structure.commit', root, {
                'operationId': preview['operationId'], 'planHash': preview['planHash'], 'approve': True})
            committed_revision = load_structure(root)['revision']
            undo(root, preview['operationId'], actor='test')
            restored = load_structure(root)
            self.assertGreater(restored['revision'], committed_revision)
            for collection in ('entities', 'attributes', 'fragments', 'relations'):
                self.assertEqual(restored[collection], source[collection])


if __name__ == '__main__':
    unittest.main()
