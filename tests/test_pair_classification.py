"""Current-format pair maintenance, scoped contract loading and evidence invalidation."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.artifacts import envelope
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.pair_classification import pair_inputs, query_pairs
from scripts.core.project import TOOLBOX_ROOT, _validated_source_model, source_model_policy
from scripts.core.prompt_plan import PromptPlanError, evaluate_condition, resolve_prompt_plan
from scripts.core.source_changes import apply_changes, preview_source_change
from scripts.core.source_impact import review_input_hash, source_impact
from scripts.core.source_model import SourceIndex, source_schema
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.workflow import approve_artifacts, WorkflowError
from scripts.mcp_server import handle
from tests.fixtures_current import create_current_project
from tests.fixtures_workflow import register_and_approve, review_ready_project

CONDITION = {'field': 'design.pairs[].visualPolarity', 'op': 'eq', 'value': '角色乙强势'}
ASSESSED = {'status': 'assessed', 'reason': '测试作者已逐项盘点当前配对。'}


def pair(rid='pair-AB', *, value='角色乙强势', members=None, scope=None):
    row = {'pairId': rid, 'characterIds': members or ['character-TEST-ALPHA', 'character-BETA'],
           'visualPolarityState': 'known', 'visualPolarity': value}
    if scope is not None:
        row['scopeIds'] = scope
    return row


class PairClassificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        fixture = create_current_project(self.root, chapter_count=2)
        self.source = fixture['structure']; self.cid, self.other = fixture['chapterIds']
        self.source['pairAssessment'] = {'status': 'unassessed'}
        self.source['entities'].extend({'entityId': rid, 'entityType': 'character', 'name': name}
            for rid, name in [('character-BETA', '人物乙'), ('character-GAMMA', '人物丙'), ('character-DELTA', '人物丁')])
        self.save()

    def save(self):
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def declared(self):
        self.source['pairAssessment'] = deepcopy(ASSESSED)
        self.source['pairs'] = [pair(scope=[self.cid]), pair('pair-CD', members=['character-GAMMA', 'character-DELTA'],
                                                           value='角色甲强势', scope=[self.other])]
        self.save()

    def test_empty_unassessed_and_absent_assessment_are_unresolved(self):
        for source in (self.source, {k: v for k, v in self.source.items() if k != 'pairAssessment'}):
            matched, evidence = evaluate_condition(source, CONDITION)
            self.assertIsNone(matched)
            self.assertEqual(evidence['status'], 'unresolved')
        before = self.snapshot()
        result = resolve_prompt_plan(self.root, 'govel-planner', chapter_id=self.cid)
        self.assertFalse(result['complete']); self.assertTrue(result['warnings'])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(resolve_prompt_plan(self.root, 'govel-writer', chapter_id=self.cid)['complete'])
        self.assertTrue(resolve_prompt_plan(self.root, 'govel-titler', chapter_id=self.cid)['complete'])

    def test_explicit_empty_not_applicable_has_reason_and_no_contract(self):
        self.source['pairAssessment'] = {'status': 'not_applicable', 'reason': '本项目未设计任何配对。'}
        self.save()
        plan = resolve_prompt_plan(self.root, 'govel-planner', chapter_id=self.cid)
        self.assertTrue(plan['complete'])
        self.assertTrue(all(row['evidence']['status'] == 'not_matched' for row in plan['conditional']))
        self.assertFalse(any(row['kind'] == 'conditional' for row in plan['contracts']))

    def test_target_and_other_type_use_same_scoped_input_in_plan_and_prompt(self):
        self.declared()
        for cid, expected in ((self.cid, True), (self.other, False)):
            plan = resolve_prompt_plan(self.root, 'govel-planner', chapter_id=cid)
            self.assertTrue(plan['complete']); self.assertEqual(plan['conditional'][0]['matched'], expected)
            self.assertEqual(plan['conditional'][0]['evidence']['inputs']['inputHash'],
                             chapter_plan(self.source, cid)['recipe']['pairInputHash'])
        self.assertTrue(resolve_prompt_plan(self.root, 'govel-composer')['conditional'][0]['matched'])

    def test_match_with_unknown_sibling_loads_but_preserves_unresolved_warning(self):
        self.declared()
        self.source['pairs'][1].pop('visualPolarity')
        self.source['pairs'][1]['visualPolarityState'] = 'unknown'
        self.source['pairs'][1]['scopeIds'] = [self.cid]
        self.save()
        result = resolve_prompt_plan(self.root, 'govel-planner', chapter_id=self.cid)
        self.assertFalse(result['complete'])
        self.assertTrue(result['conditional'][0]['matched'])
        self.assertEqual(result['conditional'][0]['evidence']['status'], 'unresolved')
        self.assertEqual(result['warnings'][0]['unresolvedInputs'][0]['pairId'], 'pair-CD')
        self.assertTrue(any(row['kind'] == 'conditional' for row in result['contracts']))

    def test_missing_classification_is_unassessed_not_negative(self):
        self.source['pairAssessment'] = deepcopy(ASSESSED)
        self.source['pairs'] = [{'pairId': 'pair-AB', 'characterIds': ['character-TEST-ALPHA', 'character-BETA']}]
        self.save()
        matched, evidence = evaluate_condition(self.source, CONDITION)
        self.assertIsNone(matched); self.assertEqual(evidence['status'], 'unresolved')

    def test_pair_explicit_not_applicable_is_distinct_from_unknown(self):
        self.source['pairAssessment'] = deepcopy(ASSESSED)
        self.source['pairs'] = [{'pairId': 'pair-AB', 'characterIds': ['character-TEST-ALPHA', 'character-BETA'],
                                'visualPolarityState': 'not_applicable', 'reason': '作者未以双方角色外形分类。'}]
        self.save()
        matched, evidence = evaluate_condition(self.source, CONDITION)
        self.assertFalse(matched); self.assertEqual(evidence['status'], 'not_matched')

    def test_invalid_records_fail_closed_without_writes(self):
        bad = [{}, {**pair(), 'visualPolarity': '无效角色分类'}, {**pair(), 'characterIds': ['character-BETA', 'character-BETA']},
               {**pair(), 'characterIds': ['character-BETA', 'prop-TEST-KEY']}, {**pair(), 'pairId': 'wrong'},
               {**pair(), 'visualPolarityState': []}, {**pair(), 'scopeIds': ['chapter-MISSING']},
               {**pair(), 'dominantRole': 'typo'}, {**pair(), 'visibleTo': 'character-BETA'}]
        for row in bad:
            with self.subTest(row=row):
                before = self.snapshot()
                with self.assertRaises(StructureError):
                    preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'pairs', 'record': row}],
                        pairAssessment=ASSESSED, baseRevision=self.source['revision'], reason='拒绝损坏输入', requestId='bad')
                self.assertEqual(self.snapshot(), before)

    def test_illegal_condition_input_is_error_not_negative(self):
        for design in ({'pairs': {}}, {'pairs': [{}]}, {'pairs': [{**pair(), 'visualPolarity': 'typo'}]},
                       {'pairs': [{**pair(), 'visualPolarity': None}]}):
            with self.subTest(design=design), self.assertRaises(PromptPlanError):
                evaluate_condition(design, CONDITION)
        for design in ({'chapter': {'arc': '强者跌落'}}, {'chapter': {'arc': {}}}):
            with self.assertRaises(PromptPlanError):
                evaluate_condition(design, {'field': 'chapter.arc', 'op': 'includes', 'value': '强者跌落'})

    def test_missing_chapter_arc_and_empty_explicit_arc_differ(self):
        condition = {'field': 'chapter.arc', 'op': 'includes', 'value': '强者跌落'}
        self.assertIsNone(evaluate_condition({'chapter': {}}, condition)[0])
        self.assertFalse(evaluate_condition({'chapter': {'arc': []}}, condition)[0])

    def test_assessment_conflicts_and_duplicate_endpoint_records_are_rejected(self):
        for rows, status in (([], 'assessed'), ([pair()], 'not_applicable'),
                              ([pair(), pair('pair-BA')], 'assessed')):
            source = {**self.source, 'pairs': rows, 'pairAssessment': {'status': status, 'reason': '测试'}}
            with self.assertRaises(StructureError): validate_structure(source)

    def test_pair_identity_is_indexed_and_available_to_dependencies(self):
        self.declared()
        self.source['fragments'][0]['dependsOn'].append('pair-AB')
        validate_structure(self.source)
        index = SourceIndex.build(self.source)
        self.assertEqual(index.owner_types['pair-AB'], 'pair')
        self.assertIn('pair-AB', index.records)
        self.assertIn('pairs', source_schema()['mutableCollections'])

    def test_pair_dependency_outside_declared_scope_is_rejected_instead_of_hidden(self):
        self.declared()
        changed = deepcopy(self.source)
        changed['fragments'][1]['dependsOn'] = ['pair-AB']
        with self.assertRaises(StructureError) as caught:
            validate_structure(changed)
        self.assertIn('PAIR_DEPENDENCY_SCOPE_CONFLICT', [row['code'] for row in caught.exception.details['errors']])
        # Transitive dependencies through another fragment must not bypass scope.
        changed = deepcopy(self.source)
        changed['fragments'][0]['dependsOn'] = ['pair-AB']
        changed['fragments'][1]['dependsOn'] = [changed['fragments'][0]['fragmentId']]
        with self.assertRaises(StructureError): validate_structure(changed)
        changed['pairs'][0]['scopeIds'].append(self.other)
        validate_structure(changed)

    def test_ancestor_plan_and_character_reference_cannot_hide_out_of_scope_pair(self):
        self.declared()
        changed = deepcopy(self.source)
        changed['fragments'].append({**deepcopy(changed['fragments'][0]), 'fragmentId': 'plan-GLOBAL',
            'ownerId': changed['projectId'], 'dependsOn': ['pair-AB']})
        changed['fragments'][-1].pop('chapterContract')
        with self.assertRaises(StructureError) as caught:
            validate_structure(changed)
        self.assertIn('PAIR_DEPENDENCY_SCOPE_CONFLICT', [r['code'] for r in caught.exception.details['errors']])
        changed = deepcopy(self.source)
        changed['fieldDefinitions'].append({'fieldId': 'field-PAIRREF', 'key': 'pair.reference', 'label': '配对引用',
            'valueType': 'reference', 'appliesTo': ['character']})
        changed['attributes'].append({'attributeId': 'attribute-PAIRREF', 'ownerId': 'character-GAMMA', 'fieldId': 'field-PAIRREF',
            'values': [{'valueId': 'value-PAIRREF', 'value': 'pair-AB'}]})
        changed['references'].append({'sourceId': self.other, 'targetId': 'character-GAMMA', 'kind': 'character'})
        with self.assertRaises(StructureError) as caught:
            validate_structure(changed)
        self.assertIn('PAIR_DEPENDENCY_SCOPE_CONFLICT', [r['code'] for r in caught.exception.details['errors']])
        changed['pairs'][0]['scopeIds'].append(self.other)
        validate_structure(changed)

    def test_scoped_pair_change_preserves_unrelated_plan_and_fragment_receipt(self):
        self.declared()
        changed = deepcopy(self.source); changed['pairs'][0]['visualPolarity'] = '双方强势'
        self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], chapter_plan(changed, self.cid)['sourcePlanHash'])
        self.assertEqual(chapter_plan(self.source, self.other)['sourcePlanHash'], chapter_plan(changed, self.other)['sourcePlanHash'])
        self.assertEqual(source_impact(self.source, changed)['approvalInvalidations'], [self.cid])
        for frag in self.source['fragments']:
            old, new = review_input_hash(self.source, frag['fragmentId']), review_input_hash(changed, frag['fragmentId'])
            self.assertEqual(old == new, frag['ownerId'] == self.other)

    def test_scope_move_invalidates_old_and_new_chapter(self):
        self.declared()
        changed = deepcopy(self.source); changed['pairs'][0]['scopeIds'] = [self.other]
        self.assertEqual(source_impact(self.source, changed)['approvalInvalidations'], sorted([self.cid, self.other]))

    def test_global_assessment_and_declared_global_pairs_affect_both_chapters(self):
        self.declared()
        changed = deepcopy(self.source); changed['pairAssessment']['status'] = 'unassessed'
        self.assertEqual(source_impact(self.source, changed)['approvalInvalidations'], sorted([self.cid, self.other]))
        changed = deepcopy(self.source); changed['pairs'][0].pop('scopeIds')
        after = deepcopy(changed); after['pairs'][0]['visualPolarity'] = '角色甲强势'
        self.assertEqual(source_impact(changed, after)['approvalInvalidations'], sorted([self.cid, self.other]))

    def test_source_preview_commit_idempotency_and_query_pagination(self):
        changes = [{'action': 'put', 'collection': 'pairs', 'record': pair('$pair')}]
        args = {'changes': changes, 'pairAssessment': ASSESSED, 'baseRevision': self.source['revision'],
                'reason': '登记已评估配对', 'requestId': 'pair-1'}
        before_source = (self.root / '.govel/structure.json').read_bytes()
        preview = call_operation('source.change-preview', self.root, args)
        self.assertEqual(before_source, (self.root / '.govel/structure.json').read_bytes())
        self.assertEqual(call_operation('source.change-preview', self.root, args)['planHash'], preview['planHash'])
        next_action = preview['nextAction']; before = self.snapshot()
        with self.assertRaises(StructureError):
            call_operation('source.commit', self.root, {**next_action['arguments'], 'approve': False})
        self.assertEqual(self.snapshot(), before)
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': next_action['tool'], 'arguments': next_action['arguments']}})['result']
        self.assertFalse(response['isError'], response)
        self.assertTrue(call_operation('source.change-preview', self.root, args)['alreadyCommitted'])
        source = load_structure(self.root)
        self.assertEqual(source['revision'], self.source['revision'] + 1)
        self.assertTrue(source['pairs'][0]['pairId'].startswith('pair-'))
        before = self.snapshot()
        page = query_pairs(self.root, limit=1)
        self.assertEqual(page['items'], source['pairs']); self.assertEqual(self.snapshot(), before)

    def test_new_pair_and_endpoints_can_use_handles_in_one_batch(self):
        changes = [{'action': 'put', 'collection': 'entities', 'record': {
            'entityId': '$new', 'entityType': 'character', 'name': '新人物'}},
            {'action': 'put', 'collection': 'pairs', 'record': pair('$pair', members=['$new', 'character-BETA'])}]
        proposed, identities = apply_changes(self.source, changes, pair_assessment=ASSESSED)
        self.assertEqual(proposed['pairs'][0]['characterIds'][0], identities['$new'])
        self.assertEqual(proposed['pairs'][0]['pairId'], identities['$pair'])

    def test_two_scoped_pairs_query_is_read_only_and_cursor_bound(self):
        self.declared(); before = self.snapshot()
        first = query_pairs(self.root, limit=1); second = query_pairs(self.root, limit=1, cursor=first['nextCursor'])
        self.assertEqual([r['pairId'] for r in first['items'] + second['items']], ['pair-AB', 'pair-CD'])
        self.assertEqual(self.snapshot(), before)
        self.source['pairs'][0]['visualPolarity'] = '双方强势'; self.save()
        with self.assertRaises(StructureError) as caught:
            query_pairs(self.root, limit=1, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def read_prepared_pairs(self, *, pov=None, budget=6000):
        report = call_operation('task.prepare', self.root, {'task': 'planner', 'chapterId': self.cid,
            'view': 'model', **({'povCharacter': pov} if pov else {})})
        selected = [row['readId'] for row in report['reading']['sourceReads']
                    if row['query']['tool'] == 'query_source_pairs']
        self.assertEqual(len(selected), 1)
        args = {'credential': report['reading']['credential'], 'readIds': selected, 'maxBytes': budget}
        pages, chunks, cursor = [], {}, None
        while True:
            page = call_operation('task.read-batch', self.root, {**args, **({'cursor': cursor} if cursor else {})})
            pages.append(page)
            self.assertTrue(page['coverage']['sourceQueriesExecuted'])
            for row in page['items']:
                chunks.setdefault(row['id'], []).append(row['text'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        return pages, [json.loads(''.join(parts)) for parts in chunks.values()]

    def test_prepared_pair_read_pages_exact_author_material_without_project_writes(self):
        self.declared()
        self.source['pairs'][0]['reason'] = '作者核对配对外形与适用范围。' * 900
        self.save()
        before = self.snapshot()
        pages, records = self.read_prepared_pairs()
        self.assertGreater(len(pages), 1)
        self.assertEqual(next(row['assessment'] for row in records if row['kind'] == 'pair_assessment'), ASSESSED)
        self.assertEqual([row['record'] for row in records if row['kind'] == 'pair'], self.source['pairs'])
        self.assertEqual(self.snapshot(), before)

    def test_prepared_pov_pair_read_preserves_explicit_visibility_without_private_inventory(self):
        self.source['pairAssessment'] = {'status': 'assessed', 'reason': '库存秘密：隐藏的配对理由不能进入视角读取。'}
        self.source['pairs'] = [
            {**pair('pair-PUBLIC'), 'visibility': 'public', 'reason': '公开配对资料。'},
            {**pair('pair-PERMITTED', members=['character-GAMMA', 'character-DELTA']),
             'visibleTo': ['character-TEST-ALPHA'], 'reason': '仅向甲公开的配对资料。'},
            {**pair('pair-PRIVATE', members=['character-TEST-ALPHA', 'character-GAMMA']),
             'visibility': 'private', 'reason': '私密配对字段不得泄漏。'},
            {**pair('pair-OTHER', members=['character-BETA', 'character-GAMMA']),
             'visibleTo': ['character-BETA'], 'reason': '仅对乙开放的秘密。'},
            {**pair('pair-UNDECLARED', members=['character-BETA', 'character-DELTA']), 'reason': '没有可见性不等于可见。'},
            {**pair('pair-OVERRIDE', members=['character-TEST-ALPHA', 'character-DELTA']),
             'visibility': 'public', 'visibleTo': [], 'reason': '显式空受众优先于public。'}]
        self.save()
        before = self.snapshot()
        pages, records = self.read_prepared_pairs(pov='character-TEST-ALPHA')
        self.assertEqual({row['id'] for row in records}, {'pair-PUBLIC', 'pair-PERMITTED'})
        serialized = json.dumps(pages, ensure_ascii=False)
        for pair_row in self.source['pairs'][2:]:
            self.assertNotIn(pair_row['pairId'], serialized)
            self.assertNotIn(pair_row['reason'], serialized)
        self.assertNotIn(self.source['pairAssessment']['reason'], serialized)
        self.assertTrue(pages[0]['coverage']['sourceSelections'][0]['coverage']['assessmentWithheld'])
        self.assertEqual(self.snapshot(), before)

    def test_prepared_empty_inventory_and_fully_hidden_pairs_are_readable(self):
        self.source['pairAssessment'] = {'status': 'not_applicable', 'reason': '测试无配对。'}
        self.save()
        _, records = self.read_prepared_pairs()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['assessment'], self.source['pairAssessment'])
        self.declared()
        for row in self.source['pairs']:
            row['visibility'] = 'private'
        self.save()
        pages, records = self.read_prepared_pairs(pov='character-TEST-ALPHA')
        self.assertEqual(records, [])
        self.assertTrue(pages[0]['coverage']['sourceSelections'][0]['warnings'])

    def test_pair_policy_enum_drift_is_rejected(self):
        policy = source_model_policy(); policy['pairClassification']['classificationStates'].append('maybe')
        with self.assertRaises(ValueError): _validated_source_model(policy)

    def test_conditional_contract_definition_and_loaded_content_bind_existing_evidence(self):
        from scripts.core.project import toolbox_manifest
        from scripts.core.static_cache import cached_sha256
        from scripts.core.relationship_causality_review import validate_causality_completion
        from scripts.core.review_artifacts import ReviewArtifactError
        from tests.fixtures_workflow import assessed_review_payload
        self.declared()
        old_hash = chapter_plan(self.source, self.cid)['sourcePlanHash']
        other_hash = chapter_plan(self.source, self.other)['sourcePlanHash']
        payload = assessed_review_payload(self.source, self.cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': [4], 'consensus': [], 'disagreements': [], 'openItems': []})
        def changed_digest(path):
            return 'f' * 64 if Path(path).name == 'G-RELATIONSHIP-ROLES.md' else cached_sha256(path)
        with patch('scripts.core.static_cache.cached_sha256', side_effect=changed_digest):
            self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], old_hash)
            self.assertEqual(chapter_plan(self.source, self.other)['sourcePlanHash'], other_hash)
            with self.assertRaises(ReviewArtifactError) as caught:
                validate_causality_completion(self.source, self.cid, payload, 'stage_a_review')
            self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_PLAN_STALE')
        manifest = toolbox_manifest()
        manifest['promptPlan']['govel-planner']['conditional'][0]['when']['value'] = '角色甲强势'
        with patch('scripts.core.project.toolbox_manifest', return_value=manifest):
            self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], old_hash)

    def test_arc_ancestor_condition_and_pair_scope_use_same_inputs(self):
        parent = self.source['arcs'][0]
        parent['tags'] = ['强者跌落']
        leaf = {**deepcopy(parent), 'arcId': 'arc-LEAF', 'parentArcId': parent['arcId'], 'tags': [], 'title': '子弧线'}
        self.source['arcs'].append(leaf)
        self.source['chapterArcLinks'][0]['arcId'] = leaf['arcId']
        self.source['pairAssessment'] = deepcopy(ASSESSED)
        self.source['pairs'] = [pair(scope=[parent['arcId']])]
        self.save()
        plan = resolve_prompt_plan(self.root, 'govel-editor', chapter_id=self.cid)
        self.assertTrue(plan['conditional'][0]['matched'])
        binding = chapter_plan(self.source, self.cid)['recipe']['conditionalContracts']['conditions']
        editor = next(row for row in binding if row['station'] == 'govel-editor')
        self.assertEqual(editor['inputHash'], plan['conditional'][0]['evidence']['inputHash'])
        self.assertEqual(pair_inputs(self.source, self.cid)['pairs'], self.source['pairs'])

    def test_delete_last_pair_and_explicit_empty_assessment_is_one_reviewed_change(self):
        self.declared()
        preview = preview_source_change(self.root,
            changes=[{'action': 'delete', 'collection': 'pairs', 'id': row['pairId']} for row in self.source['pairs']],
            pairAssessment={'status': 'not_applicable', 'reason': '作者决定本书不设计配对。'},
            baseRevision=self.source['revision'], requestId='remove-pairs', reason='题材调整')
        call_operation('source.commit', self.root, preview['nextAction']['arguments'])
        source = load_structure(self.root)
        self.assertEqual(source['pairs'], [])
        self.assertTrue(resolve_prompt_plan(self.root, 'govel-composer')['complete'])

    def test_assessment_only_preview_and_request_payload_binding(self):
        args = {'changes': [], 'baseRevision': self.source['revision'], 'requestId': 'assessment', 'reason': '评估',
                'pairAssessment': {'status': 'not_applicable', 'reason': '作者明确无配对。'}}
        preview = call_operation('source.change-preview', self.root, args)
        with self.assertRaises(StructureError) as caught:
            call_operation('source.change-preview', self.root, {**args, 'pairAssessment': {'status': 'unassessed'}})
        self.assertEqual(caught.exception.code, 'SOURCE_REQUEST_REUSED')
        call_operation('source.commit', self.root, preview['nextAction']['arguments'])
        self.assertEqual(query_pairs(self.root)['assessment'], args['pairAssessment'])

    def test_pair_change_rejects_real_approved_a_b_and_retains_other_approval(self):
        self.declared()
        review_ready_project(self.root)
        other_review = register_and_approve(self.root, self.other, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)), 'consensus': [], 'disagreements': [], 'openItems': []})
        before_workflow = read_json(self.root / '.govel/workflow.json')
        source = load_structure(self.root)
        row = {**source['pairs'][0], 'visualPolarity': '双方强势'}
        preview = preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'pairs', 'record': row}],
            baseRevision=source['revision'], requestId='reclassify', reason='修改分类')
        call_operation('source.commit', self.root, preview['nextAction']['arguments'])
        workflow = read_json(self.root / '.govel/workflow.json')
        for kind in ('stage_a_review', 'stage_b_review', 'candidate_prose'):
            with self.subTest(kind=kind), self.assertRaises(WorkflowError) as caught:
                approve_artifacts(workflow, self.cid, artifact_hashes={kind:
                    before_workflow['artifacts'][self.cid][kind]['artifactSha256']}, approval_mode='manual', project_root=self.root)
            self.assertEqual(caught.exception.code, 'ARTIFACT_SOURCE_CHANGED')
        self.assertEqual(workflow['approvals'][self.other], before_workflow['approvals'][self.other])
        approve_artifacts(workflow, self.other, artifact_hashes={'stage_a_review': other_review['artifactSha256']},
                          approval_mode='manual', project_root=self.root)

    def test_quick_init_through_cli_preview_mcp_commit_then_actual_condition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            def cli(*args):
                process = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'),
                    'project', *args, '--project-root', str(root)], capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                return json.loads(process.stdout)
            cli('quick-init', '--book-name', '分类验收临时书')
            result = cli('prompt-plan', '--station', 'govel-composer')
            self.assertFalse(result['complete'])
            changes = [{'action': 'put', 'collection': 'entities', 'record': {
                'entityId': handle, 'entityType': 'character', 'name': name}}
                for handle, name in [('$a', '人物甲'), ('$b', '人物乙')]]
            changes.append({'action': 'put', 'collection': 'pairs',
                            'record': pair('$pair', members=['$a', '$b'])})
            preview = cli('source', 'change-preview', '--changes', json.dumps(changes, ensure_ascii=False),
                          '--pair-assessment', json.dumps(ASSESSED, ensure_ascii=False),
                          '--request-id', 'cli-pair', '--base-revision', '0', '--reason', '测试作者已评估')
            action = preview['nextAction']
            response = handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': action['tool'], 'arguments': action['arguments']}})['result']
            self.assertFalse(response['isError'], response)
            result = cli('prompt-plan', '--station', 'govel-composer')
            self.assertTrue(result['complete']); self.assertTrue(result['conditional'][0]['matched'])
            self.assertEqual(cli('source', 'pairs')['assessment']['status'], 'assessed')


if __name__ == '__main__':
    unittest.main()
