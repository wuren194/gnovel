"""Used field labels can be corrected without reopening semantic field migrations."""
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
from scripts.core.project import TOOLBOX_ROOT, source_model_policy
from scripts.core.source_changes import preview_source_change
from scripts.core.source_impact import ReviewInputs
from scripts.core.source_model import snapshot_hash
from scripts.core.source_plan import chapter_plan
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.source_reports import query_change_report
from scripts.core.source_review import fingerprint, query_reviews, submit_review
from scripts.core.structure_commands import _plan_hash
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.structure_transaction import commit, undo
from scripts.core.workflow import approve_artifacts, default_workflow, record_artifact
from tests.fixtures_source import single_source_chapter, source_fixture
from tests.fixtures_workflow import assessed_review_payload


def metadata_item(record_id='FD-HEIGHT'):
    return {'kind': 'field_definition', 'recordId': record_id,
            'patch': [{'op': 'set', 'path': ['label'], 'value': '人物身高'},
                      {'op': 'set', 'path': ['description'], 'value': '自然站立时的身高。'}],
            'reason': '更正已使用字段的名称和说明', 'evidence': []}


class FieldDefinitionProposalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture(self.root)
        # Establish the current persistent lock anchor before measuring business
        # side effects. Rejected previews may acquire this same idle inode.
        with project_lock(self.root):
            self.assertTrue(project_lock_observation(self.root)['ownedByCurrentThread'])
        self.lock_inode = (self.root / '.gnovel/structure.lock').stat().st_ino
        self.arguments = {'sourceHash': snapshot_hash(self.source), 'items': [metadata_item()],
                          'requestId': 'field-metadata-1', 'approvalActor': '测试作者',
                          'approvalEvidence': '已确认字段名称与说明更正'}

    def preview(self, **overrides):
        return preview_proposal_batch(self.root, **{**self.arguments, **overrides})

    def commit(self, preview, approved=True):
        return commit(self.root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                      approved=approved, actor='test')

    def inventory(self):
        self.assertEqual(project_lock_observation(self.root),
                         {'locked': False, 'ownedByCurrentThread': False, 'protocol': 'stable-flock-v1'})
        self.assertEqual((self.root / '.gnovel/structure.lock').stat().st_ino, self.lock_inode)
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def plan(self, preview):
        return read_json(self.root / '.gnovel/plans' / (preview['operationId'] + '.json'))

    def test_metadata_commit_preserves_all_values_and_identity_and_records_diff(self):
        source_bytes = (self.root / '.gnovel/structure.json').read_bytes()
        preview = self.preview()
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), source_bytes)
        self.assertEqual(preview['diffSummary']['recordCount'], 1)
        self.assertEqual({tuple(row['path']) for row in preview['diff']}, {('label',), ('description',)})
        proposal = self.plan(preview)['parameters']['proposals'][0]
        self.assertEqual(proposal['operation'], 'canon_field_definition_proposal')
        self.assertEqual(proposal['proposal']['expectedOldRecordHash'], fingerprint(self.source['fieldDefinitions'][0]))
        self.assertFalse(proposal['proposal']['approval']['verifiedHuman'])
        self.commit(preview)
        source = load_structure(self.root)
        updated = source['fieldDefinitions'][0]
        self.assertEqual(updated['fieldId'], 'FD-HEIGHT')
        self.assertEqual(updated['label'], '人物身高')
        self.assertEqual(updated['description'], '自然站立时的身高。')
        for collection in ('attributes', 'fragments', 'entities'):
            self.assertEqual(source[collection], self.source[collection])
        rationale = query_change_report(self.root, operationId=preview['operationId'], section='rationale')['items']
        self.assertEqual(rationale, [proposal])

    def test_description_can_be_removed_and_unchanged_metadata_is_rejected(self):
        self.commit(self.preview())
        source = load_structure(self.root)
        item = metadata_item()
        item['patch'] = [{'op': 'remove', 'path': ['description']}]
        self.commit(self.preview(sourceHash=snapshot_hash(source), items=[item], requestId='remove-description'))
        source = load_structure(self.root)
        self.assertNotIn('description', source['fieldDefinitions'][0])
        item['patch'] = [{'op': 'set', 'path': ['label'], 'value': '人物身高'}]
        with self.assertRaises(StructureError) as caught:
            self.preview(sourceHash=snapshot_hash(source), items=[item], requestId='same-label')
        self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_CHANGE_EMPTY')

    def test_semantic_fields_identity_and_invalid_metadata_are_rejected_without_plan(self):
        cases = [('fieldId', 'FD-OTHER', 'FIELD_DEFINITION_MIGRATION_REQUIRED'),
                 ('key', 'appearance.standing_height', 'FIELD_DEFINITION_MIGRATION_REQUIRED'),
                 ('valueType', 'text', 'FIELD_DEFINITION_MIGRATION_REQUIRED'),
                 ('appliesTo', ['character', 'prop'], 'FIELD_DEFINITION_MIGRATION_REQUIRED'),
                 ('label', ' ', 'FIELD_DEFINITION_METADATA_INVALID'),
                 ('description', 7, 'FIELD_DEFINITION_METADATA_INVALID')]
        for field, value, code in cases:
            item = metadata_item()
            item['patch'] = [{'op': 'set', 'path': [field], 'value': value}]
            before = self.inventory()
            with self.subTest(field=field), self.assertRaises(StructureError) as caught:
                self.preview(items=[item])
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.details['itemIndex'], 0)
            self.assertEqual(self.inventory(), before)
        item['patch'] = [{'op': 'remove', 'path': ['label']}]
        with self.assertRaises(StructureError) as caught:
            self.preview(items=[item])
        self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_METADATA_INVALID')

    def test_original_generic_put_delete_and_handcrafted_plan_stay_blocked(self):
        for change in ({'action': 'put', 'collection': 'fieldDefinitions',
                        'record': {**self.source['fieldDefinitions'][0], 'label': '未审更正'}},
                       {'action': 'delete', 'collection': 'fieldDefinitions', 'id': 'FD-HEIGHT'}):
            with self.subTest(change=change), self.assertRaises(StructureError) as caught:
                preview_source_change(self.root, changes=[change], requestId='generic-field',
                                      baseRevision=self.source['revision'], reason='test')
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')
        proposed = deepcopy(self.source)
        proposed['fieldDefinitions'][0]['label'] = '未审更正'
        with self.assertRaises(StructureError) as caught:
            require_canon_proposal(self.source, proposed, {'operation': 'source_change'})
        self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')

    def test_declared_approval_and_explicit_commit_approval_are_both_required(self):
        for key in ('approvalActor', 'approvalEvidence'):
            with self.subTest(key=key), self.assertRaises(StructureError) as caught:
                self.preview(**{key: ' '})
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_PROPOSAL_INCOMPLETE')
        preview = self.preview()
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.commit(preview, approved=False)
        self.assertEqual(caught.exception.code, 'APPROVAL_REQUIRED')
        self.assertEqual(self.inventory(), before)

    def test_metadata_is_project_scoped_and_does_not_accept_fake_fact_evidence(self):
        for extra in ({'chapterId': 'CH-ANY'}, {'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '一米九'}]},
                      {'evidenceMode': 'author_declaration'}):
            with self.subTest(extra=extra), self.assertRaises(StructureError) as caught:
                self.preview(items=[{**metadata_item(), **extra}])
            self.assertEqual(caught.exception.code, 'ARGUMENTS_INVALID')

    def test_request_retry_commit_and_undo_keep_original_identity_and_never_replay(self):
        preview = self.preview()
        before = self.inventory()
        self.assertEqual(self.preview(), preview)
        self.assertEqual(self.inventory(), before)
        self.commit(preview)
        committed_revision = load_structure(self.root)['revision']
        before = self.inventory()
        self.assertEqual(self.preview()['operationState'], 'committed')
        self.assertEqual(self.inventory(), before)
        undo(self.root, preview['operationId'], actor='test')
        source = load_structure(self.root)
        self.assertGreater(source['revision'], committed_revision)
        self.assertEqual(source['fieldDefinitions'], self.source['fieldDefinitions'])
        before = self.inventory()
        self.assertEqual(self.preview()['operationState'], 'undone')
        self.assertEqual(self.inventory(), before)

    def test_changed_request_or_changed_source_never_rebinds_old_approval(self):
        preview = self.preview()
        with self.assertRaises(StructureError) as caught:
            self.preview(approvalEvidence='另一项批准')
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')
        changed = deepcopy(self.source)
        changed['fragments'][0]['text'] += '另一次修改。'
        # Deliberate external edit after preview, not a legitimate Canon write.
        write_json(self.root / '.gnovel/structure.json', changed)
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.preview(requestId='new-request')
        self.assertEqual(caught.exception.code, 'SESSION_REF_STALE')
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(self.inventory(), before)

    def test_commit_rechecks_semantic_scope_even_after_plan_hash_is_recomputed(self):
        preview = self.preview()
        path = self.root / '.gnovel/plans' / (preview['operationId'] + '.json')
        plan = self.plan(preview)
        plan['proposedStructure']['fieldDefinitions'][0]['key'] = 'appearance.other'
        proposal = plan['parameters']['proposals'][0]['proposal']
        proposal['newRecordHash'] = fingerprint(plan['proposedStructure']['fieldDefinitions'][0])
        plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
        write_json(path, plan)
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.commit({**preview, 'planHash': plan['planHash']})
        self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')
        self.assertEqual(self.inventory(), before)

    def test_commit_rejects_changed_plan_with_original_hash(self):
        preview = self.preview()
        plan = self.plan(preview)
        plan['parameters']['proposals'][0]['proposal']['reason'] = 'changed'
        write_json(self.root / '.gnovel/plans' / (preview['operationId'] + '.json'), plan)
        with self.assertRaises(StructureError) as caught:
            self.commit(preview)
        self.assertEqual(caught.exception.code, 'PLAN_HASH_MISMATCH')

    def test_commit_rejects_forged_proposal_context_even_with_recomputed_hash(self):
        preview = self.preview()
        path = self.root / '.gnovel/plans' / (preview['operationId'] + '.json')
        original = self.plan(preview)
        for extra in ({'sourceChapterId': 'CH-NONEXISTENT'},
                      {'evidence': [{'fragmentId': 'SF-MISSING', 'quote': '不存在的引文'}]},
                      {'approval': {**original['parameters']['proposals'][0]['proposal']['approval'],
                                    'sourceChapterId': 'CH-NONEXISTENT'}}):
            plan = deepcopy(original)
            plan['parameters']['proposals'][0]['proposal'].update(extra)
            plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
            write_json(path, plan)
            before = self.inventory()
            with self.subTest(extra=extra), self.assertRaises(StructureError) as caught:
                self.commit({**preview, 'planHash': plan['planHash']})
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID')
            self.assertEqual(self.inventory(), before)

    def test_exact_replay_rejects_other_records_order_deletion_and_forged_evidence(self):
        preview = self.preview()
        plan = self.plan(preview)
        row = plan['parameters']['proposals'][0]
        single_plan = {'operation': row['operation'], 'parameters': {'fieldDefinitionProposal': row['proposal']}}
        for mutate in (lambda source: source['fieldDefinitions'].reverse(),
                       lambda source: source['fieldDefinitions'].pop(0),
                       lambda source: source['entities'][0].update(name='夹带改名'),
                       lambda source: source['attributes'][0]['values'][0].update(value={'number': 200, 'unit': 'cm'}),
                       lambda source: source['chapters'].append({'chapterId': 'CH-HIDDEN'})):
            proposed = deepcopy(plan['proposedStructure'])
            mutate(proposed)
            with self.subTest(mutate=mutate), self.assertRaises(StructureError) as caught:
                require_canon_proposal(self.source, proposed, single_plan)
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID')
        for mutate in (lambda proposal: proposal.update(expectedOldRecordHash='0'*64),
                       lambda proposal: proposal.update(newRecordHash='0'*64),
                       lambda proposal: proposal.update(reason=' '),
                       lambda proposal: proposal['approval'].update(verifiedHuman=True),
                       lambda proposal: proposal['approval'].update(evidence=' ')):
            altered = deepcopy(single_plan)
            mutate(altered['parameters']['fieldDefinitionProposal'])
            with self.subTest(mutate=mutate), self.assertRaises(StructureError) as caught:
                require_canon_proposal(self.source, plan['proposedStructure'], altered)
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_PROPOSAL_SCOPE_INVALID')

    def test_metadata_can_share_batch_with_original_fact_gate(self):
        fact = {'kind': 'fact', 'recordId': 'SF-HEIGHT', 'reason': '更正事实措辞',
                'patch': [{'op': 'text', 'path': ['text'], 'original': '他身高', 'replacement': '人物甲身高'}],
                'evidence': [{'fragmentId': 'SF-HEIGHT', 'quote': '他身高一米九。'}]}
        self.commit(self.preview(items=[metadata_item(), fact]))
        source = load_structure(self.root)
        self.assertEqual(source['fieldDefinitions'][0]['label'], '人物身高')
        self.assertEqual(source['fragments'][0]['text'], '人物甲身高一米九。')

    def test_invalid_later_item_rejects_whole_batch_and_duplicate_target_is_rejected(self):
        invalid = metadata_item('FD-SKIN')
        invalid['patch'] = [{'op': 'set', 'path': ['valueType'], 'value': 'number'}]
        before = self.inventory()
        with self.assertRaises(StructureError) as caught:
            self.preview(items=[metadata_item(), invalid])
        self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')
        self.assertEqual(caught.exception.details['itemIndex'], 1)
        self.assertEqual(self.inventory(), before)
        with self.assertRaises(StructureError) as caught:
            self.preview(items=[metadata_item(), metadata_item()])
        self.assertEqual(caught.exception.code, 'CANON_BATCH_DUPLICATE')

    def test_policy_metadata_allowlist_has_preview_and_commit_consumers(self):
        policy = deepcopy(source_model_policy())
        policy['mutationPolicy']['referencedFieldDefinitions']['metadataFields'] = ['label']
        with patch('scripts.core.source_field_proposals.source_model_policy', return_value=policy):
            with self.assertRaises(StructureError) as caught:
                self.preview()
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')
        preview = self.preview()
        with patch('scripts.core.source_field_proposals.source_model_policy', return_value=policy):
            with self.assertRaises(StructureError) as caught:
                self.commit(preview)
            self.assertEqual(caught.exception.code, 'FIELD_DEFINITION_MIGRATION_REQUIRED')

    def test_cli_and_mcp_use_existing_batch_tool_for_metadata(self):
        choices = self.root / 'items.json'
        write_json(choices, self.arguments['items'])
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT/'scripts/novelctl.py'), 'project', 'source',
                              'proposal-batch-preview', '--project-root', str(self.root), '--source-hash', self.arguments['sourceHash'],
                              '--items', '@'+str(choices), '--request-id', self.arguments['requestId'],
                              '--approval-actor', self.arguments['approvalActor'],
                              '--approval-evidence', self.arguments['approvalEvidence']], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        from scripts.mcp_server import handle
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'preview_source_proposal_batch', 'arguments': {'projectRoot': str(self.root), **self.arguments}}})['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent'], json.loads(cli.stdout))

    def test_direct_value_dependencies_invalidate_only_related_receipts_and_approvals(self):
        source = load_structure(self.root)
        source['fieldDefinitions'].append({'fieldId': 'FD-PLAN', 'key': 'plot.plan', 'label': '计划',
                                          'valueType': 'topic', 'appliesTo': ['chapter']})
        for ordinal, (cid, dependency) in enumerate((('CH-HEIGHT', 'FV-HEIGHT'), ('CH-SKIN', 'FV-SKIN')), 1):
            source['chapters'].append(single_source_chapter(source, cid, ordinal=ordinal,
                                      volume_id=source['volumes'][0]['volumeId']))
            source['chapterArcLinks'].append({'chapterId': cid, 'arcId': source['arcs'][0]['arcId'], 'role': 'primary'})
            source['attributes'].append({'attributeId': 'AT-'+cid, 'ownerId': cid, 'fieldId': 'FD-PLAN', 'values': []})
            source['fragments'].append({'fragmentId': 'SF-'+cid, 'ownerId': cid, 'contentRole': 'plan',
                'text': '核对人物资料。', 'order': 1, 'contentBindings': ['AT-'+cid], 'dependsOn': [dependency],
                'chapterContract': {'targetRange': [1, 10000], 'mode': 'plot', 'intensityLevel': 1,
                                    'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}})
        # Complete the neutral current-format fixture before its first operation.
        validate_structure(source)
        write_json(self.root / '.gnovel/structure.json', source)
        source = load_structure(self.root)
        workflow = default_workflow()
        workflow['approvedChapterIds'] = ['CH-HEIGHT', 'CH-SKIN']
        for cid in workflow['approvedChapterIds']:
            payload = assessed_review_payload(source, cid, 'stage_a_review', {
                'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': [],
                'sourcePlanHash': chapter_plan(source, cid)['sourcePlanHash']})
            artifact = envelope('stage_a_review', payload, project_id=source['projectId'], chapter_id=cid)
            path = self.root / '工作台' / (cid+'-a.json')
            write_json(path, artifact)
            record_artifact(workflow, cid, artifact, structure=source, artifact_path=str(path),
                            project_id=source['projectId'], project_root=self.root)
            approve_artifacts(workflow, cid, artifact_hashes={'stage_a_review': artifact['artifactSha256']}, approval_mode='manual', project_root=self.root)
        write_json(self.root/'.gnovel/workflow.json', workflow)
        originals = {row['recordId']: row for row in query_reviews(self.root)['items']}
        reviews = []
        texts = {row['fragmentId']: row['text'] for row in source['fragments']}
        for rid in ('SF-LIFE', 'SF-CH-HEIGHT', 'SF-CH-SKIN'):
            row, text = originals[rid], texts[rid]
            reviews.append({key: row[key] for key in ('recordId', 'inputHash', 'contentHash')} | {
                'decision': 'compatible', 'reason': '中性夹具输入核验',
                'evidence': [{'start': 0, 'end': len(text), 'text': text}]})
        submit_review(self.root, requestId='before-field-change', reviews=reviews)
        preview = self.preview(sourceHash=snapshot_hash(source))
        self.assertEqual(preview['impact']['approvalInvalidations'], ['CH-HEIGHT'])
        self.assertIn('SF-LIFE', preview['impact']['reviewFragmentIds'])
        self.commit(preview)
        changed = load_structure(self.root)
        self.assertNotEqual(ReviewInputs(source).fingerprint('SF-LIFE'), ReviewInputs(changed).fingerprint('SF-LIFE'))
        statuses = {row['recordId']: row['status'] for row in query_reviews(self.root)['items']}
        self.assertEqual(statuses['SF-LIFE'], 'pending')
        self.assertEqual(statuses['SF-CH-HEIGHT'], 'pending')
        self.assertEqual(statuses['SF-CH-SKIN'], 'compatible')
        current_workflow = read_json(self.root/'.gnovel/workflow.json')
        self.assertFalse(current_workflow['approvals']['CH-HEIGHT']['approved'])
        self.assertEqual(current_workflow['approvals']['CH-SKIN'], workflow['approvals']['CH-SKIN'])
        self.assertEqual(current_workflow['artifacts']['CH-SKIN'], workflow['artifacts']['CH-SKIN'])


if __name__ == '__main__':
    unittest.main()
