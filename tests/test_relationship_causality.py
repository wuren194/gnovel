"""Relationship causality declarations remain source-bound review obligations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import envelope, file_sha256, sha256_text
from scripts.core.authoring_forms import review_form
from scripts.core.chapter_contract import source_contract
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.workflow import approve_artifacts, record_artifact
from tests.fixtures_workflow import evidenced_slots, review_ready_project


class RelationshipCausalityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.body = '他把钥匙放回桌上，合上了登记本。'
        self.plan_text = '沈砚能独自搬运货箱，但共同核验清单能保留货主的签收证明，因此接受分工。'
        self.source = load_structure(self.root)
        self.source['entities'].append({
            'entityId': 'character-CAUSAL-PARTNER', 'entityType': 'character', 'name': '陈舟',
            'summary': '负责核对货单的成年同事'})
        self.source['relations'].append({
            'relationId': 'relation-CAUSAL-COOPERATION', 'fromEntity': 'character-TEST-ALPHA',
            'toEntity': 'character-CAUSAL-PARTNER', 'kind': '共同交接货物'})
        self.source['fieldDefinitions'].append({
            'fieldId': 'field-CAUSAL-BASIS', 'key': 'character.cooperationBasis',
            'label': '合作依据', 'valueType': 'topic', 'appliesTo': ['character']})
        self.source['attributes'].append({
            'attributeId': 'attribute-CAUSAL-BASIS', 'ownerId': 'character-TEST-ALPHA',
            'fieldId': 'field-CAUSAL-BASIS', 'values': []})
        for order, (rid, text) in enumerate([
            ('fact-CAUSAL-CAPABILITY', '沈砚能独自搬运货箱，熟悉仓库的交接流程。'),
            ('fact-CAUSAL-LEVERAGE', '货主要求两名同事共同核验货单后才确认签收。'),
            ('fact-CAUSAL-SUCCESSOR', '签收后双方约定轮流核验下一批货物，分担差错责任。'),
        ], 1):
            self.source['fragments'].append({
                'fragmentId': rid, 'ownerId': 'character-TEST-ALPHA', 'order': order,
                'text': text, 'contentRole': 'fact',
                'contentBindings': ['attribute-CAUSAL-BASIS'], 'dependsOn': []})
        self.plan = next(row for row in self.source['fragments'] if row['ownerId'] == self.cid)
        self.plan['text'] = self.plan_text
        self.declaration = {
            'id': 'RC-COOPERATION', 'relationId': 'relation-CAUSAL-COOPERATION',
            'capabilityBasisIds': ['fact-CAUSAL-CAPABILITY'],
            'leverageBasisIds': ['fact-CAUSAL-LEVERAGE'],
            'capabilityAssessment': '有独立搬运能力，签收仍需共同核验。',
            'resistanceCost': '拒绝核验会延迟货主签收。',
            'complianceCost': '需要按双方约定留出核验时间。',
            'choiceReason': '他选择保留交接凭据，并未失去独立工作的能力。',
            'scope': '本次交接的核验分工。',
            'continuation': {'status': 'retained', 'successorBasisIds': [],
                             'reason': '本章结束时共同签收的要求仍有效。'},
        }
        self.plan['chapterContract']['relationshipCausality'] = [self.declaration]
        self.plan['chapterContract']['behaviorCoverage']['relationship'] = 'applicable'
        self.plan['dependsOn'] = [self.declaration['relationId'],
                                 *self.declaration['capabilityBasisIds'],
                                 *self.declaration['leverageBasisIds']]
        self.save_source()

    def save_source(self):
        write_json(self.root / '.govel/structure.json', self.source)

    def workflow(self):
        return read_json(self.root / '.govel/workflow.json')

    def files(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*')
                if path.is_file() and path.name != 'structure.lock'}

    def completed_payload(self, kind, *, text=None):
        text = self.body if text is None else text
        payload = review_form(self.root, chapterId=self.cid, artifactType=kind, view='full')['payload']
        if kind == 'stage_a_review':
            payload['decision'] = 'adopt'
            for row in payload['seatReviews'].values():
                row.update(conclusion='合成测试已核对当前计划材料。',
                           evidence=[{'text': self.plan['text']}])
            quote = self.plan['text']
        else:
            payload.update(verdict='POLISH_READY', slots=evidenced_slots(text))
            quote = text
        payload['behaviorCoverage'] = {'relationship': 'applicable', 'living': 'not_applicable'}
        if kind == 'stage_b_review':
            payload['factAssessment'] = {'status': 'no_change'}
        payload['relationshipCausalityReviews'] = [{
            'id': self.declaration['id'], 'conclusion': '合成测试记录了当前材料中的选择依据。',
            'evidence': [{'text': quote}],
        }]
        return payload

    def direct_record(self, kind, payload):
        workflow = self.workflow()
        before = deepcopy(workflow)
        artifact = envelope(kind, payload, project_id=self.source['projectId'], chapter_id=self.cid)
        try:
            record_artifact(workflow, self.cid, artifact, structure=self.source,
                            project_root=self.root, project_id=self.source['projectId'])
        except ValueError:
            self.assertEqual(workflow, before, 'a rejected direct registration mutated workflow')
            raise
        self.assertEqual(workflow['approvals'], before['approvals'])
        return artifact, workflow

    def approve(self, kind, result):
        workflow = self.workflow()
        approve_artifacts(workflow, self.cid,
                          artifact_hashes={kind: result['artifactSha256']}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.govel/workflow.json', workflow)

    def prepare_current_candidate(self, *, text=None):
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='stage_a_review',
                               payload=self.completed_payload('stage_a_review'))
        self.approve('stage_a_review', result)
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
                               text=self.body if text is None else text,
                               advanceLifecycle=True,
                               diagnostics={'mode': 'plot', 'intensityLevel': 1})
        self.assertTrue(result['ok'], result)
        self.approve('candidate_prose', result)
        return result

    def test_retained_replaced_and_released_are_distinct_valid_declarations(self):
        for status in ('retained', 'replaced', 'released'):
            with self.subTest(status=status):
                self.declaration['continuation'] = {
                    'status': status,
                    'successorBasisIds': ['fact-CAUSAL-SUCCESSOR'] if status == 'replaced' else [],
                    'reason': '本次交接结束后按约定处理合作安排。'}
                if 'fact-CAUSAL-SUCCESSOR' not in self.plan['dependsOn']:
                    self.plan['dependsOn'].append('fact-CAUSAL-SUCCESSOR')
                validate_structure(self.source)
                self.assertEqual(source_contract(self.source, self.cid)['relationshipCausality'],
                                 [self.declaration])

    def test_missing_costs_unknown_fields_and_duplicate_declaration_ids_are_rejected(self):
        cases = []
        missing = deepcopy(self.declaration); missing.pop('resistanceCost'); cases.append([missing])
        blank = deepcopy(self.declaration); blank['choiceReason'] = '  '; cases.append([blank])
        extra = deepcopy(self.declaration); extra['automaticallyPassed'] = True; cases.append([extra])
        cases.append([deepcopy(self.declaration), deepcopy(self.declaration)])
        for rows in cases:
            with self.subTest(rows=rows):
                self.plan['chapterContract']['relationshipCausality'] = rows
                with self.assertRaises(StructureError):
                    validate_structure(self.source)

    def test_replacement_requires_successor_while_release_and_retention_forbid_one(self):
        for status, ids in [('replaced', []), ('retained', ['fact-CAUSAL-SUCCESSOR']),
                            ('released', ['fact-CAUSAL-SUCCESSOR']), ('unknown', [])]:
            with self.subTest(status=status):
                if 'fact-CAUSAL-SUCCESSOR' not in self.plan['dependsOn']:
                    self.plan['dependsOn'].append('fact-CAUSAL-SUCCESSOR')
                self.declaration['continuation'] = {'status': status, 'successorBasisIds': ids,
                                                    'reason': '本次交接已经结束。'}
                with self.assertRaises(StructureError):
                    validate_structure(self.source)

    def test_relation_must_exist_and_connect_two_characters(self):
        for target in ('relation-MISSING', 'prop-TEST-KEY'):
            with self.subTest(target=target):
                changed = deepcopy(self.source)
                plan = next(row for row in changed['fragments'] if row['ownerId'] == self.cid)
                if target.startswith('relation-'):
                    plan['chapterContract']['relationshipCausality'][0]['relationId'] = target
                else:
                    changed['relations'][-1]['toEntity'] = target
                with self.assertRaises(StructureError):
                    validate_structure(changed)

    def test_every_relation_and_basis_id_requires_an_explicit_fragment_dependency(self):
        self.declaration['continuation'] = {'status': 'replaced',
            'successorBasisIds': ['fact-CAUSAL-SUCCESSOR'], 'reason': '改用下一批货物的轮值约定。'}
        self.plan['dependsOn'].append('fact-CAUSAL-SUCCESSOR')
        dependencies = list(self.plan['dependsOn'])
        for omitted in dependencies:
            with self.subTest(omitted=omitted):
                self.plan['dependsOn'] = [rid for rid in dependencies if rid != omitted]
                with self.assertRaises(StructureError):
                    validate_structure(self.source)

    def test_duplicate_or_unknown_basis_ids_cannot_be_used_as_evidence(self):
        for ids in (['fact-CAUSAL-CAPABILITY', 'fact-CAUSAL-CAPABILITY'], ['fact-UNKNOWN']):
            with self.subTest(ids=ids):
                self.declaration['capabilityBasisIds'] = ids
                with self.assertRaises(StructureError):
                    validate_structure(self.source)

    def test_basis_changes_reach_plan_hash_without_importing_unrelated_basis(self):
        before = chapter_plan(self.source, self.cid)
        ids = {row['id'] for row in before['records']}
        self.assertTrue(set(self.plan['dependsOn']) <= ids)
        self.assertNotIn('fact-CAUSAL-SUCCESSOR', ids)
        next(row for row in self.source['fragments']
             if row['fragmentId'] == 'fact-CAUSAL-SUCCESSOR')['text'] += '新增一个未使用的轮值说明。'
        self.assertEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], before['sourcePlanHash'])
        next(row for row in self.source['fragments']
             if row['fragmentId'] == 'fact-CAUSAL-LEVERAGE')['text'] += '货主现在允许单人签收。'
        self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], before['sourcePlanHash'])

    def test_forms_are_blank_bound_read_only_and_do_not_approve(self):
        before = self.files()
        for kind in ('stage_a_review', 'stage_b_review'):
            with self.subTest(kind=kind):
                result = review_form(self.root, chapterId=self.cid, artifactType=kind, view='full')
                self.assertEqual(result['payload']['relationshipCausalityReviews'], [
                    {'id': 'RC-COOPERATION', 'conclusion': '', 'evidence': []}])
                self.assertEqual(result['payload']['sourcePlanHash'],
                                 chapter_plan(self.source, self.cid)['sourcePlanHash'])
                self.assertFalse(result['registered'])
                self.assertFalse(result['approved'])
                self.assertFalse(result['semanticReviewComplete'])
        self.assertEqual(self.files(), before)

    def test_chapter_without_declarations_does_not_gain_new_review_obligations(self):
        self.plan['chapterContract'].pop('relationshipCausality')
        self.save_source()
        self.assertEqual(source_contract(self.source, self.cid)['relationshipCausality'], [])
        for kind in ('stage_a_review', 'stage_b_review'):
            payload = review_form(self.root, chapterId=self.cid, artifactType=kind, view='full')['payload']
            self.assertFalse(payload.get('relationshipCausalityReviews'))

    def test_direct_stage_a_registration_cannot_bypass_required_item_reviews(self):
        for decision in ('adopt', 'modify'):
            with self.subTest(decision=decision):
                payload = self.completed_payload('stage_a_review')
                payload['decision'] = decision
                payload.pop('relationshipCausalityReviews')
                with self.assertRaises(ValueError) as caught:
                    self.direct_record('stage_a_review', payload)
                self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_REQUIRED')

    def test_stage_a_accepts_current_plan_quotes_without_approving_semantic_truth(self):
        before = self.files()
        payload = self.completed_payload('stage_a_review')
        artifact, workflow = self.direct_record('stage_a_review', payload)
        self.assertEqual(workflow['artifacts'][self.cid]['stage_a_review']['artifactSha256'],
                         artifact['artifactSha256'])
        self.assertEqual(self.files(), before)

    def test_stage_a_rejects_stale_quotes_even_when_the_plan_hash_is_current(self):
        payload = self.completed_payload('stage_a_review')
        payload['relationshipCausalityReviews'][0]['evidence'] = [{'text': '上一版计划中的旧句子。'}]
        with self.assertRaises(ValueError) as caught:
            self.direct_record('stage_a_review', payload)
        self.assertEqual(caught.exception.code, 'EVIDENCE_QUOTE_MISSING')

    def test_stage_a_can_quote_its_structured_reasoning_fields_with_field_relative_offsets(self):
        fields = ('capabilityAssessment', 'resistanceCost', 'complianceCost',
                  'choiceReason', 'scope', 'continuation.reason')
        for field in fields:
            with self.subTest(field=field):
                material = (self.declaration['continuation']['reason'] if field == 'continuation.reason'
                            else self.declaration[field])
                self.assertNotIn(material, self.plan_text)
                payload = self.completed_payload('stage_a_review')
                payload['relationshipCausalityReviews'][0]['evidence'] = [{
                    'declarationField': field, 'text': material[1:-1],
                    'start': 1, 'end': len(material) - 1}]
                artifact, workflow = self.direct_record('stage_a_review', payload)
                self.assertEqual(workflow['artifacts'][self.cid]['stage_a_review']['artifactSha256'],
                                 artifact['artifactSha256'])

    def test_stage_a_field_quotes_cannot_cross_items_or_fields_or_forge_bounds(self):
        other = deepcopy(self.declaration)
        other.update(id='RC-SECOND-SHIFT', choiceReason='陈舟愿意换班，因为他需要腾出下午接待客户。')
        self.plan['chapterContract']['relationshipCausality'].append(other)
        self.save_source()
        material = self.declaration['choiceReason']
        cases = [
            ({'declarationField': 'choiceReason', 'text': other['choiceReason']}, 'EVIDENCE_QUOTE_MISSING'),
            ({'declarationField': 'resistanceCost', 'text': material}, 'EVIDENCE_QUOTE_MISSING'),
            ({'declarationField': 'choiceReason', 'text': material, 'start': 0,
              'end': len(material) + 1}, 'EVIDENCE_QUOTE_MISMATCH'),
            ({'declarationField': 'choiceReason', 'text': '没有登记过的选择理由。'}, 'EVIDENCE_QUOTE_MISSING'),
            ({'declarationField': 'continuation.status', 'text': 'retained'}, 'CAUSALITY_REVIEW_FIELD_INVALID'),
        ]
        for quote, error_code in cases:
            with self.subTest(quote=quote):
                payload = self.completed_payload('stage_a_review')
                payload['relationshipCausalityReviews'][0]['evidence'] = [quote]
                payload['relationshipCausalityReviews'].append({
                    'id': other['id'], 'conclusion': '合成测试核对了另一项独立选择。',
                    'evidence': [{'declarationField': 'choiceReason', 'text': other['choiceReason']}]})
                with self.assertRaises(ValueError) as caught:
                    self.direct_record('stage_a_review', payload)
                self.assertEqual(caught.exception.code, error_code)

    def test_stage_b_rejects_structured_field_quotes_even_when_text_also_occurs_in_prose(self):
        body = self.body + self.declaration['choiceReason']
        self.prepare_current_candidate(text=body)
        payload = self.completed_payload('stage_b_review', text=body)
        payload['relationshipCausalityReviews'][0]['evidence'] = [{
            'declarationField': 'choiceReason', 'text': self.declaration['choiceReason']}]
        with self.assertRaises(ValueError) as caught:
            self.direct_record('stage_b_review', payload)
        self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_FIELD_INVALID')

    def test_ordinary_chapter_stage_b_cannot_offer_a_stale_plan_binding(self):
        self.plan['chapterContract'].pop('relationshipCausality')
        self.save_source()
        payload = self.completed_payload('stage_b_review')
        payload.pop('relationshipCausalityReviews')
        payload['sourcePlanHash'] = '0' * 64
        with self.assertRaises(ValueError) as caught:
            self.direct_record('stage_b_review', payload)
        self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_PLAN_STALE')

    def test_direct_completed_reviews_reject_plan_changes_after_form_preparation(self):
        payloads = {kind: self.completed_payload(kind)
                    for kind in ('stage_a_review', 'stage_b_review')}
        self.declaration['scope'] = '本次交接及下次共同复核。'
        self.save_source()
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as caught:
                    self.direct_record(kind, payload)
                self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_PLAN_STALE')

    def test_direct_stage_b_registration_cannot_bypass_missing_duplicate_or_unknown_items(self):
        for case in ('missing', 'duplicate', 'unknown'):
            with self.subTest(case=case):
                payload = self.completed_payload('stage_b_review')
                if case == 'missing':
                    payload.pop('relationshipCausalityReviews')
                elif case == 'duplicate':
                    payload['relationshipCausalityReviews'] *= 2
                else:
                    payload['relationshipCausalityReviews'][0]['id'] = 'RC-OTHER'
                with self.assertRaises(ValueError) as caught:
                    self.direct_record('stage_b_review', payload)
                self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_INVALID' if case == 'duplicate'
                                 else 'CAUSALITY_REVIEW_REQUIRED')

    def test_declared_review_cannot_be_skipped_or_filled_with_a_placeholder(self):
        for kind in ('stage_a_review', 'stage_b_review'):
            for case in ('notApplicable', 'placeholder', 'noEvidence'):
                with self.subTest(kind=kind, case=case):
                    payload = self.completed_payload(kind)
                    row = payload['relationshipCausalityReviews'][0]
                    if case == 'notApplicable':
                        row['evidence'] = []
                        row['notApplicableReason'] = '本章已经写了合作过程。'
                    elif case == 'placeholder':
                        row['conclusion'] = '待填写'
                    else:
                        row['evidence'] = []
                    with self.assertRaises(ValueError):
                        self.direct_record(kind, payload)

    def test_stage_b_accepts_current_prose_quotes_and_rejects_plan_quotes(self):
        self.prepare_current_candidate()
        payload = self.completed_payload('stage_b_review')
        artifact, workflow = self.direct_record('stage_b_review', payload)
        self.assertEqual(workflow['artifacts'][self.cid]['stage_b_review']['artifactSha256'],
                         artifact['artifactSha256'])
        payload['relationshipCausalityReviews'][0]['evidence'] = [{'text': self.plan_text}]
        with self.assertRaises(ValueError) as caught:
            self.direct_record('stage_b_review', payload)
        self.assertEqual(caught.exception.code, 'EVIDENCE_QUOTE_MISSING')

    def test_stage_b_rejects_old_quote_after_body_changes_even_with_fresh_body_binding(self):
        self.prepare_current_candidate()
        old = self.completed_payload('stage_b_review')
        new_body = '他收好钥匙，把登记本交给同事。'
        self.prepare_current_candidate(text=new_body)
        current = self.completed_payload('stage_b_review', text=new_body)
        current['relationshipCausalityReviews'] = old['relationshipCausalityReviews']
        self.assertEqual(current['finalProseSha256'], sha256_text(new_body))
        with self.assertRaises(ValueError) as caught:
            self.direct_record('stage_b_review', current)
        self.assertEqual(caught.exception.code, 'EVIDENCE_QUOTE_MISSING')

    def test_direct_stage_b_without_project_root_cannot_skip_the_candidate_check(self):
        payload = self.completed_payload('stage_b_review')
        workflow = self.workflow(); before = deepcopy(workflow)
        artifact = envelope('stage_b_review', payload,
                            project_id=self.source['projectId'], chapter_id=self.cid)
        with self.assertRaises(ValueError) as caught:
            record_artifact(workflow, self.cid, artifact, structure=self.source,
                            project_id=self.source['projectId'])
        self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_MATERIAL_REQUIRED')
        self.assertEqual(workflow, before)

    def test_multiple_active_contracts_are_not_silently_selected_for_review(self):
        other = deepcopy(self.plan)
        other.update(fragmentId='plan-CAUSAL-OTHER-PLAN', order=2)
        other['chapterContract']['relationshipCausality'] = []
        self.source['fragments'].append(other)
        self.save_source()
        with self.assertRaises(StructureError) as caught:
            review_form(self.root, chapterId=self.cid, artifactType='stage_a_review')
        self.assertEqual(caught.exception.code, 'CHAPTER_CONTRACT_AMBIGUOUS')

    def test_repair_handoff_preserves_blank_causality_reviews_and_current_bindings(self):
        from scripts.core.repair_context import repair_context
        from scripts.core.transport_cache import get

        self.prepare_current_candidate()
        material = repair_context(self.root, chapterId=self.cid)
        payload = get(self.root, material['reviewInputRef'], kind='repair-review-input')
        form = review_form(self.root, chapterId=self.cid, artifactType='stage_b_review',
                           repairRef=material['repairRef'], view='full')['payload']
        for field in ('relationshipCausalityReviews', 'sourcePlanHash', 'finalProseSha256'):
            self.assertEqual(payload[field], form[field], field)
        self.assertEqual(payload['relationshipCausalityReviews'], [
            {'id': 'RC-COOPERATION', 'conclusion': '', 'evidence': []}])
        self.assertEqual(payload['sourcePlanHash'], chapter_plan(self.source, self.cid)['sourcePlanHash'])
        self.assertEqual(payload['finalProseSha256'], sha256_text(self.body))

    def test_finalization_rechecks_item_reviews_despite_matching_artifact_hashes(self):
        self.prepare_current_candidate()
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review',
                               payload=self.completed_payload('stage_b_review'))
        self.approve('stage_b_review', result)
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='state_candidate',
                               payload={'changes': []})
        self.approve('state_candidate', result)
        ready = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertTrue(ready.ready, ready.blockers)

        # Simulate a foreign producer writing a self-consistent envelope/approval.
        # Only the semantic-completion gate can reject this missing obligation.
        workflow = self.workflow()
        entry = workflow['artifacts'][self.cid]['stage_b_review']
        path = Path(entry['artifactPath'])
        old = read_json(path)
        payload = deepcopy(old['payload']); payload.pop('relationshipCausalityReviews')
        changed = envelope('stage_b_review', payload,
                           project_id=self.source['projectId'], chapter_id=self.cid)
        write_json(path, changed)
        entry.update(artifactSha256=changed['artifactSha256'], artifactFileSha256=file_sha256(path))
        workflow['approvals'][self.cid]['artifactHashes']['stage_b_review'] = changed['artifactSha256']
        write_json(self.root / '.govel/workflow.json', workflow)
        before = self.files()
        result = preflight_finalization(self.root, self.cid, skip_title=True)
        self.assertFalse(result.ready)
        self.assertIn('CAUSALITY_REVIEW_REQUIRED', {row['code'] for row in result.blockers})
        self.assertEqual(self.files(), before)
