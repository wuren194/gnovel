"""Selected aggregate tasks reuse evidence transport, domains and POV filtering."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.source_review import query_reviews, submit_review
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, validate_structure
from scripts.core.transport_cache import get
from tests.fixtures_content_proposal import attribute_value_proposal
from tests.test_source_plan import planning_source


class ReviewMaterialPackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source, self.chapter = planning_source(self.root)

    def aggregate(self, **kwargs):
        return call_operation('query.aggregate', self.root, {
            'kind': 'character-reviews', 'characterId': 'C-ALPHA', 'view': 'items', **kwargs})

    def materials(self, args):
        first, rows, cursor = None, [], None
        while True:
            page = call_operation('evidence.prepare', self.root, {**args, 'maxBytes': 3000,
                **({'cursor': cursor} if cursor else {})})
            first = first or page
            rows.extend(page['materials'])
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')).encode()), 3000)
            if not page['hasMore']:
                return first, rows
            cursor = page['nextCursor']

    def restored(self, materials, kind):
        pieces = {}
        for row in materials:
            if row.get('materialKind') == kind:
                pieces.setdefault(row['id'], []).append(row['text'])
        return [json.loads(''.join(parts)) for parts in pieces.values()]

    def test_aggregate_selection_assembles_originals_dependencies_and_decision_inputs(self):
        pending = self.aggregate()
        ids = ['SF-HEIGHT', 'SF-MIXED']
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        page, rows = self.materials({'kind': 'review', 'recordIds': ids, 'aggregateRef': pending['selectionRef']})
        tasks = self.restored(rows, 'review-task')
        self.assertEqual({row['recordId'] for row in tasks}, set(ids))
        self.assertTrue(all(row['decisionInput']['decision'] is None for row in tasks))
        dependencies = self.restored(rows, 'dependency')
        self.assertEqual(sum(row.get('attributeId') == 'AT-HEIGHT' for row in dependencies), 1)
        self.assertIn('snapshotRef', page)
        self.assertEqual(page['packAction']['arguments'], {'snapshotRef': page['snapshotRef']})
        self.assertFalse(page['coverage']['semanticReviewComplete'])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        decision = {'recordId': ids[0], 'decision': 'compatible', 'reason': 'Explicit comparison',
                    'quotes': [{'text': next(r['text'] for r in self.source['fragments'] if r['fragmentId'] == ids[0])}]}
        packed = call_operation('evidence.pack', self.root, {'snapshotRef': page['snapshotRef'],
            'requestId': 'material-review', 'decisions': [decision]})
        self.assertEqual(packed['pendingRecordCount'], 1)
        submitted = call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef']})
        self.assertEqual(submitted['recordedCount'], 1)

    def test_prior_judgments_source_changes_and_rules_are_material_not_new_approval(self):
        rid = 'SF-HEIGHT'
        selected = query_reviews(self.root, recordIds=[rid])['items'][0]
        text = next(row['text'] for row in self.source['fragments'] if row['fragmentId'] == rid)
        submit_review(self.root, requestId='old-review', reviews=[{
            **{key: selected[key] for key in ('recordId', 'inputHash', 'contentHash')},
            'decision': 'compatible', 'reason': 'Prior explicit decision', 'evidence': [{'start': 0, 'end': len(text), 'text': text}]}])
        plan = attribute_value_proposal(self.root, 'AT-HEIGHT', 185)
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        source = read_json(self.root / '.govel/structure.json')
        source['entities'].append({'entityId': 'RU-HEIGHT', 'entityType': 'rule', 'name': 'Height consistency',
            'ruleSpec': {'version': '1', 'method': 'semantic', 'enforcement': 'required',
                        'scopeIds': ['C-ALPHA'], 'actions': ['review'], 'inputIds': ['AT-HEIGHT']}})
        validate_structure(source); write_json(self.root / '.govel/structure.json', source)
        page, rows = self.materials({'kind': 'review', 'recordIds': [rid], 'aggregateRef': self.aggregate()['selectionRef']})
        prior = self.restored(rows, 'prior-review')[0]
        self.assertFalse(prior['inputsStillCurrent'])
        self.assertEqual(prior['reason'], 'Prior explicit decision')
        self.assertTrue(prior['invalidationReason'])
        change = self.restored(rows, 'source-change')[0]
        self.assertEqual(change['operationId'], plan['operationId'])
        self.assertTrue(change['beforeAvailable'])
        self.assertTrue(any(row['before'] != row['current'] for row in change['changes']))
        self.assertEqual(self.restored(rows, 'rule')[0]['evaluation']['status'], 'pending')
        self.assertFalse(page['approvalGranted'])

    def test_pov_is_retained_when_dependencies_are_expanded(self):
        for rid, pov, text, dependencies in (
            ('SF-KNOWN', 'C-ALPHA', 'Visible belief.', ['SF-HEIGHT', 'SF-SECRET', 'SF-KNOWN-DEP']),
            ('SF-KNOWN-DEP', 'C-ALPHA', 'Visible dependency.', []),
            ('SF-SECRET', 'C-BETA', 'OTHER PERSPECTIVE SECRET', [])):
            self.source['fragments'].append({'fragmentId': rid, 'ownerId': 'C-ALPHA', 'text': text,
                'order': len(self.source['fragments']) + 1, 'contentRole': 'belief', 'perspectiveId': pov,
                'contentBindings': ['AT-HEIGHT'], 'dependsOn': dependencies})
        validate_structure(self.source); write_json(self.root / '.govel/structure.json', self.source)
        selection = {'knowledgeMode': 'perspective', 'perspectiveIds': ['C-ALPHA']}
        pending = self.aggregate(selection=selection)
        page, rows = self.materials({'kind': 'review', 'recordIds': ['SF-KNOWN'], 'aggregateRef': pending['selectionRef']})
        encoded = json.dumps(rows, ensure_ascii=False)
        for hidden in ('OTHER PERSPECTIVE SECRET', 'SF-SECRET', 'SF-HEIGHT'):
            self.assertNotIn(hidden, encoded)
        self.assertIn('Visible dependency.', encoded)
        self.assertTrue(self.restored(rows, 'review-task')[0]['authorContextOmitted'])
        self.assertEqual(self.restored(rows, 'prior-review'), [])
        snapshot = get(self.root, page['snapshotRef'], kind='evidence-snapshot')
        self.assertEqual(snapshot['selection']['knowledgeMode'], 'perspective')
        with self.assertRaises(StructureError):
            call_operation('evidence.prepare', self.root, {'kind': 'review', 'recordIds': ['SF-HEIGHT'],
                'aggregateRef': pending['selectionRef']})

    def test_stale_aggregate_and_changed_pagination_cannot_silently_rebind(self):
        pending = self.aggregate()
        args = {'kind': 'review', 'recordIds': ['SF-HEIGHT'], 'aggregateRef': pending['selectionRef']}
        page = call_operation('evidence.prepare', self.root, {**args, 'maxBytes': 1800})
        self.source['fragments'][0]['text'] += ' Changed.'
        write_json(self.root / '.govel/structure.json', self.source)
        with self.assertRaises(StructureError) as caught:
            call_operation('evidence.prepare', self.root, args)
        self.assertEqual(caught.exception.code, 'AGGREGATE_SELECTION_STALE')
        self.assertIsNotNone(page['nextCursor'])

    def test_history_aggregates_cannot_be_treated_as_source_review_tasks(self):
        report = call_operation('query.aggregate', self.root, {'kind': 'decision-plans'})
        with self.assertRaises(StructureError) as caught:
            call_operation('evidence.prepare', self.root, {'kind': 'review', 'recordIds': ['SF-HEIGHT'],
                'aggregateRef': report['selectionRef']})
        self.assertEqual(caught.exception.code, 'AGGREGATE_SELECTION_INVALID')

    def test_source_change_pages_bind_undo_receipt_membership(self):
        plan = attribute_value_proposal(self.root, 'AT-HEIGHT', 185)
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        args = {'kind': 'review', 'recordIds': ['SF-HEIGHT'], 'includeContext': True, 'limit': 1}
        page = call_operation('evidence.prepare', self.root, args)
        self.assertTrue(page['hasMore'])
        write_json(self.root / f'.govel/undo-receipts/{plan["operationId"]}-2.json', {'operationId': plan['operationId']})
        with self.assertRaises(StructureError) as caught:
            call_operation('evidence.prepare', self.root, {**args, 'cursor': page['nextCursor']})
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_context_uses_existing_evidence_pages_and_rejects_hidden_direct_selection(self):
        with self.assertRaises(StructureError):
            call_operation('evidence.prepare', self.root, {'kind': 'review', 'recordIds': ['SF-HEIGHT'],
                'includeContext': True, 'selection': {'knowledgeMode': 'perspective', 'perspectiveIds': ['C-ALPHA']}})
        _, rows = self.materials({'kind': 'review', 'recordIds': ['SF-HEIGHT'], 'includeContext': True})
        self.assertTrue(self.restored(rows, 'review-task'))

    def test_context_policy_changes_invalidate_unsubmitted_decisions(self):
        page, _ = self.materials({'kind': 'review', 'recordIds': ['SF-HEIGHT'], 'includeContext': True})
        decisions = [{'recordId': 'SF-HEIGHT', 'decision': 'not_checked', 'reason': 'Explicitly deferred', 'quotes': []}]
        with patch('scripts.core.evidence_pack.review_policy_hash', return_value='changed'), self.assertRaises(StructureError) as caught:
            call_operation('evidence.pack', self.root, {'snapshotRef': page['snapshotRef'], 'decisions': decisions,
                'requestId': 'old-context-policy'})
        self.assertEqual(caught.exception.code, 'EVIDENCE_SNAPSHOT_STALE')
