"""Public-operation counterexamples; synthetic semantic decisions are explicit inputs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from scripts.core.operations import call_operation
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_store import load_structure, StructureError
from scripts.core.artifacts import sha256_text
from scripts.core.fact_inventory import inventory_status
from scripts.core.review_artifacts import ReviewArtifactError
from tests.fixtures_workflow import review_ready_project, evidenced_slots


class BehaviorInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root)['chapterIds'][0]
        self.body = '他把钥匙放回桌上，合上了登记本。'

    def op(self, name, **args):
        return call_operation(name, self.root, args)

    def full(self, result):
        if result.get('responseCompacted'):
            from scripts.core.transport_cache import get
            return get(self.root, result['reportRef'], kind='authoring-report')
        return result

    def payload(self):
        result = self.full(self.op('workflow.review-form', chapterId=self.cid, artifactType='stage_b_review', view='full'))
        payload = result['payload']
        payload.update(verdict='POLISH_READY', slots=evidenced_slots(self.body),
                       behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'},
                       factAssessment={'status': 'no_change'})
        return payload

    def record(self, payload, approve=True):
        result = self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='stage_b_review', payload=payload)
        if approve:
            self.op('project.workflow.artifact-approve', chapterId=self.cid, artifact=['@'+result['artifactPath']])
        return result

    def fact_payload(self, *, target=None):
        payload = self.payload()
        payload['factAssessment'] = {'status': 'changes'}
        payload['findings'] = [{'findingId': 'FACT-KEY', 'severity': 'info', 'ruleId': 'CHAPTER-PIPELINE',
            'summary': '钥匙完成归还，供后续人物持有链引用。', 'factChange': {
                'status': 'pending', 'reason': '后续章节持续依赖该次交接事实。',
                'evidence': [{'text': self.body}], **({'targetIds': [target]} if target else {'requestId': 'key-fact'})}}]
        return payload

    def closeout(self, request='close', confirm=True):
        prepared = self.op('workflow.closeout-prepare', chapterId=self.cid, skipTitle=True)
        args = {'requestId': request, 'prepareRef': prepared['prepareRef'], 'prepareNext': False}
        if confirm:
            args.update(approveContinuity=True, summary={'resolution': 'not_required', 'reason': '合成短章交接读取完整正式正文。'})
        return self.full(self.op('workflow.closeout', **args))

    def add_topic(self):
        # Reuse the chapter's existing topic field; the owner is already bound
        # into its plan, so committing a fact genuinely invalidates A/B approval.
        source = load_structure(self.root)
        return next(r for r in source['fieldDefinitions'] if 'chapter' in r['appliesTo'] and r['valueType'] == 'topic')['fieldId']

    def commit_fact(self):
        report = self.op('source.chapter-facts-preview', requestId='key-fact', chapterId=self.cid, facts=[{
            'findingId': 'FACT-KEY', 'ownerId': self.cid, 'field': self.add_topic(), 'statement': '钥匙已放回桌面。',
            'quote': {'text': self.body}, 'reason': '已审正文完成归还动作，后续持有链将依赖此事实。'}])
        action = report['nextAction']; args = dict(action['arguments']); args.pop('projectRoot', None)
        self.op(action['operation'], **args)
        return report

    def test_unassessed_and_empty_coverages_cannot_complete(self):
        for field, value, code in [('behaviorCoverage', None, 'BEHAVIOR_COVERAGE_REQUIRED'),
                                   ('behaviorCoverage', {'relationship': 'unassessed', 'living': 'not_applicable'}, 'BEHAVIOR_COVERAGE_REQUIRED'),
                                   ('factAssessment', None, 'FACT_ASSESSMENT_REQUIRED')]:
            payload = self.payload()
            if value is None: payload.pop(field)
            else: payload[field] = value
            with self.assertRaises(ReviewArtifactError) as caught: self.record(payload)
            self.assertEqual(caught.exception.code, code)

    def test_a_missing_contract_cannot_be_called_not_applicable(self):
        source = load_structure(self.root)
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.cid and f['contentRole'] == 'plan')
        del plan['chapterContract']; write_json(self.root/'.govel/structure.json', source)
        payload = self.full(self.op('workflow.review-form', chapterId=self.cid, artifactType='stage_a_review', view='full'))['payload']
        payload['decision'] = 'adopt'; payload['behaviorCoverage'] = {'relationship': 'not_applicable', 'living': 'not_applicable'}
        for row in payload['seatReviews'].values(): row.update(conclusion='当前合成计划没有承重安排。', evidence=[{'text': plan['text']}])
        with self.assertRaises(ReviewArtifactError) as caught:
            self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='stage_a_review', payload=payload)
        self.assertEqual(caught.exception.code, 'BEHAVIOR_PLAN_COVERAGE_REQUIRED')

    def test_b_independent_discovery_requires_real_disposition(self):
        self.body = '他把公寓钥匙放回桌上，说：“从今天起，我每天回来给你做三餐，你就住下吧。”'
        self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='candidate_prose', text=self.body)
        payload = self.payload(); payload['behaviorCoverage']['living'] = 'applicable'
        payload['behaviorReviews'] = [{'id': 'LIFE-NEW', 'kind': 'living', 'origin': 'prose',
            'disposition': 'replan_required', 'conclusion': '正文新增长期照料改变关键关系条件，须返回A。',
            'evidence': [{'text': self.body}], 'basisIds': []}]
        with self.assertRaises(ReviewArtifactError) as caught: self.record(payload)
        self.assertEqual(caught.exception.code, 'BEHAVIOR_DISPOSITION_PENDING')
        payload['verdict'] = 'INCOMPLETE'; self.record(payload, approve=False)
        clean = self.payload(); clean['behaviorReviews'] = []
        with self.assertRaises(ReviewArtifactError) as caught: self.record(clean)
        self.assertEqual(caught.exception.code, 'BEHAVIOR_FINDING_DROPPED')
        payload['verdict'] = 'POLISH_READY'; payload['behaviorReviews'][0]['disposition'] = 'within_plan'
        with self.assertRaises(ReviewArtifactError) as caught: self.record(payload)
        self.assertEqual(caught.exception.code, 'BEHAVIOR_REPLAN_REQUIRED')

    def test_known_fact_survives_report_replacement_and_explicit_no_change(self):
        self.record(self.fact_payload())
        clean = self.payload(); clean['findings'] = []
        with self.assertRaises(ReviewArtifactError) as caught: self.record(clean)
        self.assertEqual(caught.exception.code, 'FACT_FINDINGS_DROPPED')
        disposed = self.fact_payload(); disposed['factAssessment']['status'] = 'no_change'
        change = disposed['findings'][0]['factChange']; change.pop('requestId'); change['status'] = 'not_required'
        change['reason'] = '重新核对发现这是一次临时动作，后续不引用，持续持有状态已在源中登记。'
        self.record(disposed)
        self.record(clean)  # disposition is retained in history, not copied forever

    def test_candidate_rewording_cannot_drop_a_pending_fact(self):
        self.record(self.fact_payload())
        self.body = '他把钥匙放回桌上，合上登记本后走向窗边。'
        self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='candidate_prose', text=self.body)
        payload = self.payload(); payload['findings'] = []
        with self.assertRaises(ReviewArtifactError) as caught: self.record(payload)
        self.assertEqual(caught.exception.code, 'FACT_FINDINGS_DROPPED')
        self.record(self.fact_payload())

    def test_repair_form_preserves_fact_obligation_without_calling_it_a_prose_defect(self):
        self.record(self.fact_payload())
        from scripts.core.repair_context import repair_context
        from scripts.core.transport_cache import get
        material = repair_context(self.root, chapterId=self.cid)
        payload = get(self.root, material['reviewInputRef'], kind='repair-review-input')
        saved = get(self.root, material['repairRef'], kind='repair')
        payload['repairDecisions'] = [{'issueId': row['id'], 'decision': 'resolved',
            'reason': '合成短段仅供机械工作流测试，本项统计提示已核对当前正文，不要求改写。',
            'quotes': [{'text': self.body}]} for row in saved['rows']
            if row['source'] in {'diagnostics', 'stage_b_review'} and not row['finding'].get('factChange')]
        payload.update(verdict='POLISH_READY', slots=evidenced_slots(self.body),
                       behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'},
                       factAssessment={'status': 'changes'})
        result = self.record(payload)
        artifact = read_json(self.root/result['artifactPath'])
        self.assertEqual(artifact['payload']['findings'][0]['factChange']['status'], 'pending')
        self.assertEqual(artifact['payload']['repairReview']['pendingIssueIds'], [])

    def test_parent_owner_and_field_cannot_masquerade_as_fact(self):
        for rid in ('character-TEST-ALPHA', self.add_topic()):
            with self.subTest(rid=rid), self.assertRaises(ReviewArtifactError) as caught:
                self.record(self.fact_payload(target=rid))
            self.assertEqual(caught.exception.code, 'FACT_TARGET_INVALID')

    def test_real_public_fact_cycle_and_invalidated_approval_are_not_deadlocked(self):
        self.record(self.fact_payload())
        initial = self.closeout()
        self.assertFalse(initial['readyForContinuation']); self.assertTrue(initial['factsAssessed'])
        self.assertTrue(initial['continuity']['continuityComplete'])
        self.assertFalse(initial['continuity']['readyForContinuation'])
        self.assertEqual(initial['factInventory']['items'][0]['requestId'], 'key-fact')
        # Empty caller request list never erased the known obligation.
        prep = self.op('task.prepare', task='continue', view='model')
        self.assertFalse(prep['ok'])
        source_before = load_structure(self.root); prose = self.root/source_before['chapters'][0]['filePath']
        raw = prose.read_bytes(); generation = source_before['chapters'][0]['proseBinding']['generation']
        report = self.commit_fact()
        workflow = read_json(self.root/'.govel/workflow.json')
        self.assertFalse(workflow['approvals'][self.cid]['approved'])
        self.assertEqual(workflow['approvals'][self.cid]['invalidatedBy'], 'source_dependencies_changed')
        waiting = self.closeout('waiting', confirm=False)
        self.assertFalse(waiting['readyForContinuation'])
        result = self.op('source.chapter-facts-complete', requestId='key-fact', registerEvidence=True)
        self.assertTrue(result['complete'], result)
        final = self.closeout('final', confirm=False)
        self.assertTrue(final['readyForContinuation'], final)
        self.assertEqual(prose.read_bytes(), raw)
        self.assertEqual(load_structure(self.root)['chapters'][0]['proseBinding']['generation'], generation)
        self.assertFalse(read_json(self.root/'.govel/workflow.json')['approvals'][self.cid]['approved'])
        # Source fact changes invalidate old citations and the handoff again.
        fragment_id = report['facts'][0]['fragmentId']
        source = load_structure(self.root); fragment = next(f for f in source['fragments'] if f['fragmentId'] == fragment_id)
        changed = deepcopy(fragment); changed['text'] = '钥匙转交给了另一人。'
        resolved = self.op('source.content-resolve', recordId=fragment_id, kind='fact')
        preview = self.op('source.content-proposal-preview', sessionRef=resolved['sessionRef'],
            expectedOldRecord=fragment, newRecord=changed, reason='合成测试：作者修订已登记的交接事实。',
            evidence=[{'fragmentId': fragment_id, 'quote': fragment['text']}],
            approvalActor='测试作者', approvalEvidence='批准本临时项目的事实修订，以验证旧引证失效。')
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)
        self.assertFalse(self.closeout('changed', confirm=False)['readyForContinuation'])

    def test_inventory_rejects_generation_prose_and_review_tampering(self):
        self.closeout()
        source = load_structure(self.root); workflow = read_json(self.root/'.govel/workflow.json')
        self.assertTrue(inventory_status(self.root, source, workflow, self.cid)['complete'])
        from scripts.core.prose_binding import seal_binding
        chapter = source['chapters'][0]
        chapter['proseBinding'] = seal_binding(source['projectId'], self.cid, chapter['filePath'], state='managed',
            generation=chapter['proseBinding']['generation']+1, expected_content_digest='sha256:'+sha256_text(self.body))
        self.assertFalse(inventory_status(self.root, source, workflow, self.cid)['complete'])
        source = load_structure(self.root)
        review_path = Path(workflow['artifacts'][self.cid]['stage_b_review']['artifactPath']); review_path.write_text('{}')
        self.assertFalse(inventory_status(self.root, source, workflow, self.cid)['complete'])


class LivingArrangementTests(unittest.TestCase):
    setUp = BehaviorInventoryTests.setUp
    op = BehaviorInventoryTests.op
    full = BehaviorInventoryTests.full
    # Keep semantic outcomes authored: these tests prove declared decisions and
    # exact evidence can travel, not that Python understands a human motive.
    def plan_living(self, *, basis=None, text=None):
        source = load_structure(self.root)
        plan = deepcopy(next(f for f in source['fragments'] if f['ownerId'] == self.cid and f['contentRole'] == 'plan'))
        plan['text'] = text or '他仍住原来的普通住宅，今晚自愿做饭；明日的服务照常，后续是否照料尚未决定。'
        plan['chapterContract']['behaviorCoverage'] = {'relationship': 'not_applicable', 'living': 'applicable'}
        plan['chapterContract']['livingArrangements'] = [{
            'id': 'LIFE-MEAL', 'characterIds': ['character-TEST-ALPHA'], 'triggers': ['first_sustained'],
            'arrangement': '核对是否将本次晚饭写成长期照料。', 'basisIds': basis or [],
            'constraints': '晚间时间允许；未知服务未推成不存在。', 'choiceReason': '本人自愿完成本次晚饭。',
            'scope': '仅今晚；未来安排保持计划。'}]
        plan['dependsOn'] = ['character-TEST-ALPHA', *(basis or [])]
        source['chapters'][0]['reviewClass'] = 'ordinary'
        write_json(self.root/'.govel/structure.json', source)
        result = self.op('source.change-preview', baseRevision=source['revision'], requestId='living-plan', reason='本临时项目明确生活安排审校范围。',
                         changes=[{'action': 'put', 'collection': 'fragments', 'record': plan}])
        args = dict(result['nextAction']['arguments']); args.pop('projectRoot', None); self.op('source.commit', **args)
        return plan

    def test_living_source_and_public_a_form_require_seat_nine_and_current_quotes(self):
        plan = self.plan_living()
        form = self.full(self.op('workflow.review-form', chapterId=self.cid, artifactType='stage_a_review', view='full'))
        payload = form['payload']; self.assertIn(9, payload['activeSeats'])
        payload['decision'] = 'adopt'; payload['behaviorCoverage'] = plan['chapterContract']['behaviorCoverage']
        for row in payload['seatReviews'].values():
            row.update(conclusion='当前安排限于今晚，不因职业预设住房或服务，未将未知解释为没有。', evidence=[{'text': plan['text']}])
        payload['behaviorReviews'][0].update(disposition='within_plan', conclusion='自愿做饭和普通住宅可成立；跨章义务尚未发生。', evidence=[{'text': plan['text']}])
        bad = deepcopy(payload); bad['activeSeats'].remove(9)
        with self.assertRaises(StructureError) as caught:
            self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='stage_a_review', payload=bad)
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_MISSING')
        self.assertEqual(caught.exception.details['missing'], [9])
        result = self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='stage_a_review', payload=payload)
        self.assertTrue(result['registered']); self.assertFalse(result['approvalPerformed'])

    def test_living_source_rejects_unregistered_basis_edges(self):
        source = load_structure(self.root)
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.cid and f['contentRole'] == 'plan')
        plan['chapterContract']['livingArrangements'] = [{
            'id': 'LIFE-UNKNOWN', 'characterIds': ['character-TEST-ALPHA'], 'triggers': ['alternative_excluded'],
            'arrangement': '排除服务选项', 'basisIds': ['fact-NOT-REGISTERED'],
            'constraints': '材料不足', 'choiceReason': '需要核验', 'scope': '本次安排'}]
        from scripts.core.structure_store import validate_structure, StructureError
        with self.assertRaises(StructureError) as caught: validate_structure(source)
        codes = str(caught.exception.details)
        self.assertIn('LIVING_BASIS_MISSING', codes)
        self.assertIn('LIVING_DEPENDENCY_MISSING', codes)


class BehaviorRetentionTests(unittest.TestCase):
    setUp = BehaviorInventoryTests.setUp
    op = BehaviorInventoryTests.op
    full = BehaviorInventoryTests.full
    payload = BehaviorInventoryTests.payload
    record = BehaviorInventoryTests.record

    def prose(self, text):
        self.body = text
        self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='candidate_prose', text=text)

    def review(self, disposition, *, verdict='INCOMPLETE'):
        payload = self.payload()
        payload.update(verdict=verdict, behaviorCoverage={'relationship': 'not_applicable', 'living': 'applicable'})
        source = load_structure(self.root)
        planned = any(row['id'] == 'LIFE-MEALS' for fragment in source['fragments']
                      if fragment['ownerId'] == self.cid and fragment['contentRole'] == 'plan'
                      for row in fragment.get('chapterContract', {}).get('livingArrangements', []))
        payload['behaviorReviews'] = [{'id': 'LIFE-MEALS', 'kind': 'living', 'origin': 'plan' if planned else 'prose',
            'disposition': disposition, 'conclusion': {
                'replan_required': '长期照料成为新的同住条件，必须修订计划并重走阶段A。',
                'local_repair': '原计划只安排今晚一餐，正文的每天是局部误写，需修复范围词。',
                'needs_material': '仍需核对照料的时间范围和已经成立的动机。',
                'within_plan': '已对照当前计划和正文核对安排范围、选择理由及可用时段。',
            }[disposition], 'evidence': [{'text': self.body}], 'basisIds': []}]
        return payload

    def approve_meal_plan(self, *, approve=True):
        source = load_structure(self.root)
        plan = deepcopy(next(f for f in source['fragments'] if f['ownerId'] == self.cid and f['contentRole'] == 'plan'))
        plan['text'] = '他在休假期间自愿每天做饭，双方约定照料只到假期结束；同住不妨碍随时离开。'
        plan['dependsOn'] = ['character-TEST-ALPHA']
        plan['chapterContract']['behaviorCoverage'] = {'relationship': 'not_applicable', 'living': 'applicable'}
        plan['chapterContract']['livingArrangements'] = [{
            'id': 'LIFE-MEALS', 'characterIds': ['character-TEST-ALPHA'], 'triggers': ['first_sustained'],
            'arrangement': '休假期间自愿每日做饭，假期结束重新协商。', 'basisIds': [],
            'constraints': '假期内安排可容纳做饭，未排除对方的住所与服务。',
            'choiceReason': '本人提出照料，对方保留离开的选择。', 'scope': '仅休假期间。'}]
        preview = self.op('source.change-preview', baseRevision=source['revision'], requestId='retention-plan',
                          reason='测试作者明确修订照料安排的事件范围。',
                          changes=[{'action': 'put', 'collection': 'fragments', 'record': plan}])
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)
        payload = self.full(self.op('workflow.review-form', chapterId=self.cid, artifactType='stage_a_review', view='full'))['payload']
        payload.update(decision='adopt', behaviorCoverage=plan['chapterContract']['behaviorCoverage'])
        for row in payload['seatReviews'].values():
            row.update(conclusion='合成计划已明确照料期限与选择边界，工位同意该次修订。', evidence=[{'text': plan['text']}])
        payload['behaviorReviews'][0].update(disposition='within_plan',
            conclusion='修订计划给出自愿照料的期限与可用时间，不把一次承诺扩成永久义务。',
            evidence=[{'text': plan['text']}])
        result = self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='stage_a_review', payload=payload)
        if approve:
            self.op('project.workflow.artifact-approve', chapterId=self.cid, artifact=['@' + result['artifactPath']])
        self.prose(self.body)
        return result

    def test_replan_cannot_be_laundered_through_material_or_local_repair_reports(self):
        self.prose('他把公寓钥匙放回桌上，说：“从今天起，我每天回来给你做三餐，你就住下吧。”')
        original = self.review('replan_required'); self.record(original, approve=False)
        for intermediate in ('needs_material', 'local_repair', 'needs_material'):
            self.record(self.review(intermediate), approve=False)
        final = self.review('within_plan', verdict='POLISH_READY')
        self.assertEqual(final['sourcePlanHash'], original['sourcePlanHash'])
        self.assertEqual(final['finalProseSha256'], original['finalProseSha256'])
        with self.assertRaises(ReviewArtifactError) as caught: self.record(final)
        self.assertEqual(caught.exception.code, 'BEHAVIOR_REPLAN_REQUIRED')
        # Merely changing the prose cannot satisfy the retained plan obligation.
        self.prose('他把公寓钥匙放回桌上，承诺从今天起每天回来做三餐，请对方住下。')
        with self.assertRaises(ReviewArtifactError) as caught:
            self.record(self.review('within_plan', verdict='POLISH_READY'))
        self.assertEqual(caught.exception.code, 'BEHAVIOR_REPLAN_REQUIRED')

    def test_local_repair_cannot_be_laundered_through_material_reports(self):
        self.prose('他今天有空，却把原本只限今晚的约定说成了“以后每天我都做饭”。')
        original = self.review('local_repair'); self.record(original, approve=False)
        self.record(self.review('needs_material'), approve=False)
        final = self.review('within_plan', verdict='POLISH_READY')
        self.assertEqual(final['finalProseSha256'], original['finalProseSha256'])
        with self.assertRaises(ReviewArtifactError) as caught: self.record(final)
        self.assertEqual(caught.exception.code, 'BEHAVIOR_REPAIR_REQUIRED')
        self.approve_meal_plan()
        # A changed plan alone must not wash out the explicit prose repair.
        with self.assertRaises(ReviewArtifactError) as caught:
            self.record(self.review('within_plan', verdict='POLISH_READY'))
        self.assertEqual(caught.exception.code, 'BEHAVIOR_REPAIR_REQUIRED')

    def test_material_only_review_can_complete_without_fabricating_an_edit(self):
        self.prose('他今天休假，想试试新菜；“只做今晚一餐，明早仍去食堂。”')
        pending = self.review('needs_material'); self.record(pending, approve=False)
        final = self.review('within_plan', verdict='POLISH_READY')
        final['behaviorReviews'][0]['conclusion'] = '补读当前完整对白后确认仅今晚一餐，明早仍去食堂；没有长期义务。'
        self.assertEqual(final['sourcePlanHash'], pending['sourcePlanHash'])
        self.assertEqual(final['finalProseSha256'], pending['finalProseSha256'])
        self.assertTrue(self.record(final)['registered'])

    def test_actual_plan_and_prose_updates_discharge_both_retained_obligations(self):
        self.prose('他把公寓钥匙放回桌上，说：“以后每天我都回来做三餐，你就住下吧。”')
        original = self.review('replan_required'); self.record(original, approve=False)
        self.record(self.review('local_repair'), approve=False)
        self.record(self.review('needs_material'), approve=False)
        self.approve_meal_plan()
        self.prose('他正在休假，提出假期里每天做饭；“假期结束重新商量，你也随时可以回家。”')
        final = self.review('within_plan', verdict='POLISH_READY')
        self.assertNotEqual(final['sourcePlanHash'], original['sourcePlanHash'])
        self.assertNotEqual(final['finalProseSha256'], original['finalProseSha256'])
        self.assertTrue(self.record(final)['registered'])

    def test_changed_plan_cannot_discharge_replan_without_current_a_approval(self):
        self.prose('他提出以后每天做饭，让对方留下同住。')
        self.record(self.review('replan_required'), approve=False)
        self.record(self.review('needs_material'), approve=False)
        stage_a = self.approve_meal_plan(approve=False)
        with self.assertRaises(ValueError) as caught:
            self.record(self.review('within_plan', verdict='POLISH_READY'))
        self.assertEqual(caught.exception.code, 'ARTIFACTS_NOT_APPROVED')
        self.op('project.workflow.artifact-approve', chapterId=self.cid, artifact=['@' + stage_a['artifactPath']])
        self.assertTrue(self.record(self.review('within_plan', verdict='POLISH_READY'))['registered'])

    def test_incomplete_resolution_does_not_retire_the_original_repair_binding(self):
        first = '他把今晚一餐误写成了“以后每天做饭”。'
        self.prose(first)
        self.record(self.review('local_repair'), approve=False)
        self.prose('他改口：“只做今晚一餐。”')
        self.record(self.review('within_plan'), approve=False)
        self.record(self.review('needs_material'), approve=False)
        self.prose(first)
        with self.assertRaises(ReviewArtifactError) as caught:
            self.record(self.review('within_plan', verdict='POLISH_READY'))
        self.assertEqual(caught.exception.code, 'BEHAVIOR_REPAIR_REQUIRED')

    def test_verified_resolution_retires_old_prose_binding_for_a_new_issue(self):
        first = '他把今晚试做的一餐误说成了“以后每天都做”。'
        self.prose(first)
        self.record(self.review('local_repair'), approve=False)
        self.record(self.review('needs_material'), approve=False)
        self.prose('他纠正说：“只做今晚一餐，明天照旧。”')
        self.assertTrue(self.record(self.review('within_plan', verdict='POLISH_READY'))['registered'])
        # A later independent issue starts at the newly reviewed text. The first
        # obsolete hash must not be retained forever as a forbidden candidate.
        pending = self.review('local_repair'); self.record(pending, approve=False)
        self.record(self.review('needs_material'), approve=False)
        self.prose(first)
        final = self.review('within_plan', verdict='POLISH_READY')
        final['behaviorReviews'][0]['conclusion'] = '这次核对已批准的还原叙述：引号内是人物承认的口误，不是新义务。'
        self.assertNotEqual(final['finalProseSha256'], pending['finalProseSha256'])
        self.assertTrue(self.record(final)['registered'])


class FactRequestBindingTests(unittest.TestCase):
    op = BehaviorInventoryTests.op
    full = BehaviorInventoryTests.full
    payload = BehaviorInventoryTests.payload
    record = BehaviorInventoryTests.record
    closeout = BehaviorInventoryTests.closeout

    def setUp(self):
        BehaviorInventoryTests.setUp(self)
        source = load_structure(self.root)
        preview = self.op('source.change-preview', requestId='fact-topic', baseRevision=source['revision'],
            reason='给临时项目建立独立人物经历字段，后续事实不改变当前章计划。', changes=[{
                'action': 'put', 'collection': 'fieldDefinitions', 'record': {
                    'fieldId': 'field-RETENTION-FACT', 'key': 'retention.history', 'label': '经历',
                    'valueType': 'topic', 'appliesTo': ['character']}}])
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)

    def review_fact(self, quote, *, identity='F-KEY', request_id='facts-1', summary='钥匙归还事实需要回流。'):
        payload = self.payload(); payload['factAssessment'] = {'status': 'changes'}
        payload['findings'] = [{'findingId': identity, 'severity': 'info', 'ruleId': 'CHAPTER-PIPELINE',
            'summary': summary, 'factChange': {'status': 'pending', 'requestId': request_id,
                'reason': summary, 'evidence': [quote]}}]
        return payload

    def finish_first_fact(self, quote):
        from tests.fixtures_workflow import run_and_approve_diagnostics
        run_and_approve_diagnostics(self.root, self.cid)
        self.record(self.review_fact(quote))
        state = self.op('workflow.state-prepare', requestId='fact-state', chapterId=self.cid)
        self.op('project.workflow.artifact-approve', chapterId=self.cid, artifact=['@' + state['artifactPath']])
        self.assertFalse(self.closeout()['readyForContinuation'])
        return self.commit_fact('F-KEY', 'facts-1', quote, '他已把钥匙放回桌上。')

    def commit_fact(self, identity, request_id, quote, statement):
        preview = self.op('source.chapter-facts-preview', requestId=request_id, chapterId=self.cid, facts=[{
            'findingId': identity, 'ownerId': 'character-TEST-ALPHA', 'field': 'retention.history',
            'statement': statement, 'quote': quote, 'reason': '根据当前已审义务登记这次动作。'}])
        args = dict(preview['nextAction']['arguments']); args.pop('projectRoot', None)
        self.op('source.commit', **args)
        result = self.full(self.op('source.chapter-facts-complete', requestId=request_id, registerEvidence=True))
        self.assertTrue(result['complete'], result)
        return result

    def receipt(self):
        return self.op('project.workflow.chapter-receipt', chapterId=self.cid, view='full')

    def test_completed_request_cannot_satisfy_reused_finding_with_a_different_quote(self):
        completed = self.finish_first_fact({'text': '他把钥匙放回桌上'})
        self.assertEqual(completed['facts'][0]['quote'], {'text': '他把钥匙放回桌上', 'start': 0, 'end': 8})
        self.assertTrue(self.receipt()['readyForContinuation'])
        self.record(self.review_fact({'text': '合上了登记本'}, summary='合上登记本这一新事项尚未回流。'))
        receipt = self.receipt()
        self.assertFalse(receipt['readyForContinuation'])
        self.assertFalse(receipt['factInventory']['items'][0]['complete'])
        self.assertEqual(receipt['factInventory']['items'][0]['requestStatus'], 'stale')
        self.assertIn('FACT_REQUEST_BINDING_STALE', {r['code'] for r in receipt['factInventory']['blockers']})
        self.assertEqual(receipt['factInventory']['items'][0]['nextAction']['operation'], 'workflow.review-form')

    def test_same_words_at_a_different_position_do_not_reuse_the_original_citation(self):
        self.body = '他把钥匙放回桌上。走到门边，他又把钥匙放回桌上。'
        candidate = self.op('workflow.artifact-pack', chapterId=self.cid, artifactType='candidate_prose', text=self.body)
        self.op('project.workflow.artifact-approve', chapterId=self.cid, artifact=['@' + candidate['artifactPath']])
        quote_text = '把钥匙放回桌上'
        first, second = self.body.index(quote_text), self.body.rindex(quote_text)
        self.finish_first_fact({'text': quote_text, 'start': first, 'end': first + len(quote_text)})
        self.record(self.review_fact({'text': quote_text, 'start': second, 'end': second + len(quote_text)},
                                     summary='第二次放回钥匙才是本次盘点事项，尚未登记。'))
        self.assertFalse(self.receipt()['readyForContinuation'])
        self.assertEqual(self.receipt()['factInventory']['items'][0]['requestStatus'], 'stale')

    def test_same_obligation_can_be_reworded_and_explicitly_disposed_of(self):
        self.finish_first_fact({'text': '他把钥匙放回桌上'})
        payload = self.review_fact({'text': '他把钥匙放回桌上', 'start': 0, 'end': 8},
                                   summary='重新核对同一次归还动作：钥匙现已回到桌面。')
        self.record(payload)
        self.assertTrue(self.receipt()['readyForContinuation'])
        payload = self.review_fact({'text': '他把钥匙放回桌上'}, summary='归还动作已有精确源事实，不需新增记录。')
        payload['factAssessment']['status'] = 'no_change'
        change = payload['findings'][0]['factChange']; change['status'] = 'not_required'; del change['requestId']
        self.record(payload)
        self.assertTrue(self.receipt()['readyForContinuation'])

    def test_a_new_obligation_uses_a_new_request_and_retains_the_original_fact(self):
        self.finish_first_fact({'text': '他把钥匙放回桌上'})
        payload = self.review_fact({'text': '他把钥匙放回桌上'})
        new = self.review_fact({'text': '合上了登记本'}, identity='F-BOOK', request_id='facts-2',
                               summary='合上登记本是另外一件需登记的事项。')
        payload['findings'].extend(new['findings'])
        self.record(payload)
        self.assertFalse(self.receipt()['readyForContinuation'])
        self.commit_fact('F-BOOK', 'facts-2', {'text': '合上了登记本'}, '他已合上登记本。')
        self.assertTrue(self.receipt()['readyForContinuation'])


class PendingBehaviorBindingTests(unittest.TestCase):
    setUp = BehaviorInventoryTests.setUp
    op = BehaviorInventoryTests.op
    full = BehaviorInventoryTests.full
    payload = BehaviorInventoryTests.payload
    record = BehaviorInventoryTests.record
    prose = BehaviorRetentionTests.prose
    review = BehaviorRetentionTests.review
    approve_meal_plan = BehaviorRetentionTests.approve_meal_plan

    def files(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file() and path.name != 'structure.lock'}

    def direct_input(self, payload, name):
        from scripts.core.artifacts import envelope
        source = load_structure(self.root)
        path = self.root / '工作台' / (name + '.json')
        write_json(path, envelope('stage_b_review', payload, project_id=source['projectId'], chapter_id=self.cid))
        return path

    def assert_direct_rejected(self, payload, name, code):
        path = self.direct_input(payload, name)
        before = self.files()
        with self.assertRaises(ValueError) as caught:
            self.op('project.workflow.artifact-register', chapterId=self.cid, artifact=str(path))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.files(), before, 'a rejected pending report must not write workflow, history or input bytes')

    def test_direct_missing_plan_binding_is_rejected_without_poisoning_history(self):
        self.prose('他提出以后每天做饭，让对方留下同住。')
        payload = self.review('replan_required'); payload.pop('sourcePlanHash')
        self.assert_direct_rejected(payload, 'missing-plan-binding', 'BEHAVIOR_PENDING_BINDING_REQUIRED')
        path = self.direct_input(self.review('replan_required'), 'bound-plan-obligation')
        self.assertTrue(self.op('project.workflow.artifact-register', chapterId=self.cid, artifact=str(path))['ok'])
        self.approve_meal_plan()
        self.prose('他正在休假，只在假期里每天做饭；假期结束重新商量，对方也随时可以回家。')
        self.assertTrue(self.record(self.review('within_plan', verdict='POLISH_READY'))['registered'])

    def test_direct_fake_plan_binding_cannot_create_an_already_satisfied_obligation(self):
        self.prose('他提出以后每天做饭，让对方留下同住。')
        payload = self.review('replan_required'); payload['sourcePlanHash'] = '0' * 64
        self.assert_direct_rejected(payload, 'stale-plan-binding', 'BEHAVIOR_PENDING_BINDING_STALE')

    def test_direct_local_repair_requires_current_prose_but_not_a_plan_field(self):
        self.prose('他把原定今晚一餐误写成了“以后每天都做饭”。')
        payload = self.review('local_repair'); payload.pop('sourcePlanHash')
        payload['finalProseSha256'] = '0' * 64
        self.assert_direct_rejected(payload, 'stale-prose-binding', 'BEHAVIOR_PENDING_BINDING_STALE')
        missing = deepcopy(payload); missing.pop('finalProseSha256')
        self.assert_direct_rejected(missing, 'missing-prose-binding', 'REVIEW_FIELD_REQUIRED')
        payload['finalProseSha256'] = sha256_text(self.body)
        path = self.direct_input(payload, 'bound-prose-obligation')
        self.assertTrue(self.op('project.workflow.artifact-register', chapterId=self.cid, artifact=str(path))['ok'])
        self.prose('他改正了那句话：“只做今晚这一餐，明天照旧。”')
        self.assertTrue(self.record(self.review('within_plan', verdict='POLISH_READY'))['registered'])

    def test_needs_material_can_remain_incomplete_without_a_plan_binding(self):
        payload = self.review('needs_material'); payload.pop('sourcePlanHash')
        path = self.direct_input(payload, 'unknown-material')
        self.assertTrue(self.op('project.workflow.artifact-register', chapterId=self.cid, artifact=str(path))['ok'])
        self.assertTrue(self.record(self.review('within_plan', verdict='POLISH_READY'))['registered'])

    def test_low_level_pending_registration_requires_project_material(self):
        from scripts.core.artifacts import envelope
        from scripts.core.workflow import record_artifact
        source = load_structure(self.root)
        for disposition in ('replan_required', 'local_repair'):
            with self.subTest(disposition=disposition):
                workflow = read_json(self.root / '.govel/workflow.json'); before = deepcopy(workflow)
                artifact = envelope('stage_b_review', self.review(disposition),
                                    project_id=source['projectId'], chapter_id=self.cid)
                with self.assertRaises(ValueError) as caught:
                    record_artifact(workflow, self.cid, artifact, structure=source, project_id=source['projectId'])
                self.assertEqual(caught.exception.code, 'CAUSALITY_REVIEW_MATERIAL_REQUIRED')
                self.assertEqual(workflow, before)

    def test_pack_and_submit_bind_pending_inputs_and_reject_explicit_stale_versions(self):
        for operation, request in (('workflow.artifact-pack', 'pack-pending'), ('workflow.submit', 'submit-pending')):
            with self.subTest(operation=operation):
                payload = self.review('replan_required'); payload.pop('sourcePlanHash')
                if operation == 'workflow.artifact-pack':
                    result = self.record(payload, approve=False)
                else:
                    submitted = self.full(self.op(operation, requestId=request, chapterId=self.cid,
                        artifacts=[{'artifactType': 'stage_b_review', 'payload': payload}]))
                    self.assertTrue(submitted['submissionComplete'], submitted)
                    result = submitted['results'][0]
                registered = read_json(self.root / result['artifactPath'])['payload']
                current = self.review('replan_required')
                self.assertEqual(registered['sourcePlanHash'], current['sourcePlanHash'])
                self.assertEqual(registered['finalProseSha256'], current['finalProseSha256'])
                workflow_before = (self.root / '.govel/workflow.json').read_bytes()
                stale = self.review('replan_required'); stale['sourcePlanHash'] = '0' * 64
                if operation == 'workflow.artifact-pack':
                    before = self.files()
                    with self.assertRaises(ValueError) as caught: self.record(stale, approve=False)
                    self.assertEqual(caught.exception.code, 'ARTIFACT_BINDING_STALE')
                    self.assertEqual(self.files(), before)
                else:
                    result = self.full(self.op(operation, requestId=request + '-stale', chapterId=self.cid,
                        artifacts=[{'artifactType': 'stage_b_review', 'payload': stale}]))
                    self.assertFalse(result['submissionComplete'])
                    self.assertEqual(result['error']['code'], 'ARTIFACT_BINDING_STALE')
                self.assertEqual((self.root / '.govel/workflow.json').read_bytes(), workflow_before)


if __name__ == '__main__': unittest.main()
