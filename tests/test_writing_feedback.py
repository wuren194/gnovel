"""Writing feedback regressions on disposable, neutral manuscripts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ai_cliche_linter import build_report, check_intensity_level_consistency
from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import file_sha256, sha256_text
from scripts.core.chapter_status import chapter_status
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import candidate_prose, evidence_current, run_diagnostics
from scripts.core.operations import call_operation
from scripts.core.project import diagnostic_policy
from scripts.core.structure_store import load_structure
from scripts.core.tool_catalog import list_capabilities
from scripts.core.writing_checks import audit_chapter, chapter_check, check_paragraph, rules_card, split_beat_markers
from scripts.core.writing_notes import validate_notes
from scripts.core.workflow import approve_artifacts
from scripts.check_voice_lexicon import characters_for_project, build_report as voice_report
from scripts.text import validator
from tests.fixtures_workflow import evidenced_slots, register_and_approve, review_ready_project


class LocalWritingRulesTests(unittest.TestCase):
    def test_rules_card_matches_high_level_checks_in_buildup(self):
        card = rules_card(mode='buildUp', intensityLevel=5)
        ids = {r['ruleId']: r for r in card['rules']}
        self.assertEqual(ids['S-09b']['severity'], 'not_applicable')
        self.assertEqual(ids['S-06']['severity'], 'not_applicable')
        self.assertFalse(card['modeLevelReviewRequired'])
        strict = rules_card(mode='buildUp', intensityLevel=5, strictFloor=True)
        self.assertEqual(next(r for r in strict['rules'] if r['ruleId'] == 'S-09b')['severity'], 'not_applicable')
        self.assertNotIn('S-09b', {r['ruleId'] for r in rules_card(mode='buildUp', intensityLevel=1)['rules']})

    def test_punctuated_long_sentence_is_reported_without_forced_split(self):
        text = '门外的风卷着细小的尘土吹进走廊，窗边那张登记纸被吹落在地板上，值班员放下手里的杯子弯腰捡起来，压回厚厚的文件夹下面。'
        report = check_paragraph(text=text, mode='buildUp', intensityLevel=1)
        length = next(r for r in report['checks'] if r['rule_id'] == 'S-10s')
        self.assertTrue(length['pass']); self.assertEqual(length['level'], 'info')
        self.assertFalse(report['chapterGateEvaluated'])
        self.assertTrue(report['lineDetails']['S-10s'])
        self.assertFalse(validator.check_sentence_length('甲' * 40, 'plot')[0])

    def test_registry_threshold_is_consumed_by_check(self):
        policy = deepcopy(diagnostic_policy()); policy['sentenceLength']['byMode']['plot'] = 10
        with patch('scripts.text.validator.diagnostic_policy', return_value=policy):
            self.assertTrue(validator.sentence_length_violations('值班员放下杯子，走到门前。', 'plot'))
        self.assertFalse(validator.sentence_length_violations('值班员放下杯子，走到门前。', 'plot'))

    def test_ordinary_ordinals_dates_and_units_are_not_blocking(self):
        for text in ('找不出第二个。第一天他就到了。', '距考试98天。', '木条长98cm。', '木条长98厘米。'):
            with self.subTest(text=text):
                self.assertEqual(build_report(text)['metrics']['counts']['blocking'], 0)
        for unit in ('cm', '厘米', 'MM'):
            report = build_report(f'甲长12{unit}，乙长13{unit}，丙长14{unit}。')
            hits = [r for r in report['findings'] if r['ruleId'] == 'NUMERIC-DENSITY']
            self.assertEqual(len(hits), 1); self.assertEqual(hits[0]['severity'], 'advisory')

    def test_bathroom_medical_and_animal_nouns_are_not_extreme_content(self):
        self.assertEqual(check_intensity_level_consistency('护士拿来导管。马桶旁的水漏了。草丛里有一条活蛇。', 1)['status'], 'PASS')

    def test_sensory_lexical_coverage_is_report_only(self):
        result = validator.check_sensory_density('登记表记载了所有出入的物品。' * 30)
        self.assertTrue(result[0]); self.assertEqual(result[2], 'info')
        self.assertEqual(validator.analyze_sensory_density('气味，味道，汗酸。')['channel_counts']['嗅觉与味觉'], 3)


class CandidateFeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.body = '他把钥匙放回桌上，合上了登记本。'

    def workflow(self): return read_json(self.root / '.gnovel/workflow.json')

    def outline(self, *, mode='buildUp', level=5):
        source = load_structure(self.root)
        pov = next(r['entityId'] for r in source['entities'] if r['entityType'] == 'character')
        plan = next(f for f in source['fragments'] if f['ownerId'] == self.cid)
        plan['text'] = '值班员取回钥匙。关键台词“明天再来”。'
        plan['chapterContract'] = {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'targetRange': [8500, 12000], 'pov': pov, 'mode': mode, 'intensityLevel': level,
            'beats': [{'id': 'B01', 'budget': 4500}, {'id': 'B02', 'budget': 4500}],
            'requirements': ['值班员取回钥匙。', '关键台词“明天再来”。'], 'quotedTargets': ['明天再来'],
            'slowdownReserve': '在取钥匙的阻力处展开。'}
        path = self.root/'.gnovel/structure.json'; write_json(path, source)
        from scripts.core.source_plan import chapter_plan
        register_and_approve(self.root, self.cid, 'stage_a_review', {
            'decision': 'adopt', 'activeSeats': list(range(1, 11)),
            'sourcePlanHash': chapter_plan(source, self.cid)['sourcePlanHash'],
            'consensus': [], 'disagreements': [], 'openItems': [],
            'modeLevelReason': '上限与实际强度分开；正文预期强度在阶段 B 核验'})
        return path

    def pack(self, *, text=None, notes=None, **kwargs):
        (self.root / '工作台/body.md').write_bytes((self.body if text is None else text).encode())
        if notes is not None:
            write_json(self.root / '工作台/notes.json', notes); kwargs['notes'] = '工作台/notes.json'
        return pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose', input='工作台/body.md', **kwargs)

    def test_short_chapter_fails_and_unmarked_beats_remain_unknown(self):
        self.outline()
        report = audit_chapter(self.root, load_structure(self.root), self.cid, '字' * 7094)
        self.assertFalse(report['passed']); self.assertEqual(report['lengthDelta']['belowMinimum'], 1406)
        self.assertTrue(all(r['hanCount'] is None for r in report['beatCounts']))
        self.assertEqual(report['pov']['status'], 'needs_semantic_review')
        self.assertEqual(len(report['requirements']), 2)

    def test_beat_markers_and_literal_quotes_report_exact_evidence(self):
        self.outline()
        raw = '<!-- beat:B01 -->\r\n取钥匙。\r\n<!-- beat:B02 -->\r\n“明天再来”。'
        text, spans = split_beat_markers(raw)
        self.assertNotIn('<!--', text); self.assertIn('\r\n', text)
        report = audit_chapter(self.root, load_structure(self.root), self.cid, text, spans)
        self.assertEqual([r['hanCount'] for r in report['beatCounts']], [3, 4])
        quote = report['quotedTargets'][0]
        self.assertEqual(text[quote['start']:quote['end']], '明天再来')
        self.assertEqual(quote['status'], 'literal_found')
        self.assertFalse(report['semanticReviewComplete'])

    def test_candidate_binds_plan_and_stage_a_and_rejects_later_drift(self):
        result = self.pack()
        self.assertIn('sourcePlanHash', result['bindings'])
        self.assertEqual(result['stageAArtifactSha256'], self.workflow()['artifacts'][self.cid]['stage_a_review']['artifactSha256'])
        source = load_structure(self.root)
        next(f for f in source['fragments'] if f['ownerId'] == self.cid)['text'] = '改过的推演。'
        write_json(self.root/'.gnovel/structure.json', source)
        with self.assertRaises(ValueError) as caught: candidate_prose(self.root, self.cid)
        self.assertEqual(caught.exception.code, 'CANDIDATE_PLAN_STALE')
        self.assertIn('CANDIDATE_PLAN_STALE', {r['code'] for r in preflight_finalization(self.root, self.cid).blockers})

    def test_deviation_is_provisional_and_does_not_change_stage_a(self):
        path = self.outline()
        prior = self.workflow()['artifacts'][self.cid]['stage_a_review'].copy()
        original = file_sha256(path)
        notes = {'deviations': [{'id': 'D1', 'kind': 'intensityLevel', 'intensityLevel': 1, 'reason': '本章只建立事件与人物距离，降低描写强度'}]}
        self.pack(notes=notes)
        report = run_diagnostics(self.root, chapter_id=self.cid, register=True)
        self.assertEqual(report['evidence']['run']['intensityLevel'], 1)
        self.assertEqual(report['evidence']['run']['plannedDirtyLevel'], 5)
        self.assertTrue(report['evidence']['run']['deviationPendingReview'])
        self.assertFalse(report['approved']); self.assertFalse(report['evidence']['passed'])
        self.assertEqual(file_sha256(path), original)
        self.assertEqual(self.workflow()['artifacts'][self.cid]['stage_a_review'], prior)
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])
        board = chapter_status(self.root, self.cid)
        self.assertEqual(board['writingNotes'], notes)
        # A stale report's suggested rerun must leave the effective level to the runner.
        structure = load_structure(self.root); structure['chapters'][0]['status'] = 'draft'; structure['revision'] += 1
        write_json(self.root / '.gnovel/structure.json', structure)
        board = chapter_status(self.root, self.cid)
        action = next(row for row in board['nextActions'] if row.get('operation') == 'diagnostics.aggregate')
        self.assertNotIn('intensityLevel', action['arguments'])

    def test_unregistered_dirty_override_is_rejected(self):
        self.outline(); self.pack()
        with self.assertRaises(ValueError) as caught:
            run_diagnostics(self.root, chapter_id=self.cid, intensity_level=1)
        self.assertEqual(caught.exception.code, 'DIAGNOSTICS_UNDECLARED_DEVIATION')

    def test_notes_cannot_self_approve_or_claim_a_nonexistent_quote(self):
        for notes in ({'approved': True}, {'newFacts': [{'id': 'F1', 'statement': '家有一座塔', 'quote': '不存在的原句'}]}):
            with self.assertRaises(ValueError): validate_notes(notes, self.body)

    def test_notes_cannot_be_dropped_by_omitting_the_option(self):
        notes = {'deviations': [{'id': 'D1', 'kind': 'intensityLevel', 'intensityLevel': 1, 'reason': '待审说明'}]}
        self.pack(notes=notes)
        before = self.workflow()
        with self.assertRaises(ValueError) as caught: self.pack(text=self.body + '风停了。')
        self.assertEqual(caught.exception.code, 'WRITING_NOTES_REQUIRED')
        self.assertEqual(self.workflow(), before)
        result = self.pack(text=self.body + '风停了。', notes={})
        self.assertEqual(result['writingNotes'], {})

    def test_stage_a_needs_valid_budget_reserve_and_mode_level_reason(self):
        outline = self.outline()
        artifact = read_json(Path(self.workflow()['artifacts'][self.cid]['stage_a_review']['artifactPath']))
        payload = {k: v for k, v in artifact['payload'].items() if k not in {'sourcePlanHash', 'modeLevelReason'}}
        path = self.root / '工作台/review-a.json'; write_json(path, payload)
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='stage_a_review', input='工作台/review-a.json')
        self.assertEqual(caught.exception.code, 'OUTLINE_MODE_LEVEL_REVIEW_REQUIRED')
        source = load_structure(self.root)
        next(f for f in source['fragments'] if f['ownerId'] == self.cid)['chapterContract']['beats'][0]['budget'] = 1
        write_json(outline, source)
        payload['modeLevelReason'] = '已区分上限与实际强度'; write_json(path, payload)
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='stage_a_review', input='工作台/review-a.json')
        self.assertEqual(caught.exception.code, 'STAGE_A_OUTLINE_INVALID')
        self.assertIn('OUTLINE_BEAT_BUDGET_OUTSIDE_RANGE', caught.exception.details['issues'])

    def test_one_call_is_retryable_and_never_approves(self):
        workflow = self.workflow(); before = deepcopy(workflow['approvals'])
        structure = load_structure(self.root); structure['chapters'][0]['status'] = 'planned'
        write_json(self.root / '.gnovel/structure.json', structure)
        kwargs = {'advanceLifecycle': True, 'diagnostics': {'mode': 'plot', 'intensityLevel': 1}, 'requestId': 'one-call'}
        result = self.pack(**kwargs)
        self.assertEqual(result['lifecycleStatus'], 'review')
        self.assertTrue(result['diagnostics']['registered'])
        self.assertEqual(result['draftSha256'], result['diagnostics']['draftSha256'])
        self.assertEqual(self.workflow()['approvals'], before)
        again = self.pack(**kwargs)
        self.assertTrue(again['replayed'])
        self.assertEqual(result['artifactSha256'], again['artifactSha256'])
        self.assertTrue(again['diagnostics']['reused'])

    def test_source_voice_lexicon_is_loaded_and_quoted_name_is_not_speaker(self):
        source = load_structure(self.root)
        character = next(c for c in source['entities'] if c['entityType'] == 'character')
        character['voiceProfile'] = {'bannedPhrasesForRole': ['本少爷', '小爷', '人家']}
        write_json(self.root/'.gnovel/structure.json', source)
        characters = characters_for_project(self.root)
        loaded = next(c for c in characters if c['characterId'] == character['entityId'])
        self.assertEqual(loaded['bannedPhrases'], ['本少爷', '小爷', '人家'])
        report = voice_report(f'队友说：“人家{loaded["name"]}已经走了。”', characters, [])
        self.assertFalse(report['findings'])
        self.assertEqual(report['coverage']['dialogueAttributions'][0]['confidence'], 'mention-only')


    def test_stage_b_requires_deviation_decisions_and_facts_require_source_proposals(self):
        notes = {'deviations': [{'id': 'D1', 'kind': 'intensityLevel', 'intensityLevel': 1, 'reason': '保持本章克制呈现'}]}
        packed = self.pack(notes=notes)
        review = {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': evidenced_slots(self.body), 'findings': []}
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', payload=review)
        self.assertEqual(caught.exception.code, 'WRITING_DECISIONS_REQUIRED')
        review['writingDecisions'] = [{'id': 'D1', 'decision': 'accept', 'reason': '引文与本章呈现强度相符'}]
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', payload=review)
        self.assertEqual(read_json(self.root/result['artifactPath'])['payload']['candidateArtifactSha256'], packed['artifactSha256'])
        state = pack_artifact(self.root, chapterId=self.cid, artifactType='state_candidate', payload={'changes': []})
        self.assertEqual(read_json(self.root/state['artifactPath'])['payload']['changes'], [])
        self.assertFalse(state['approvalPerformed'])
        before = (self.root/'.gnovel/structure.json').read_bytes()
        with self.assertRaises(ValueError) as caught:
            self.pack(notes={'newFacts': [{'id': 'F1', 'statement': '登记本放在桌上', 'quote': '合上了登记本'}]})
        self.assertEqual(caught.exception.code, 'SOURCE_FACTS_REQUIRE_PROPOSAL')
        self.assertEqual((self.root/'.gnovel/structure.json').read_bytes(), before)

    def test_fact_scan_is_read_only_and_proposals_are_not_changes(self):
        text = '八岁时他住在旧街，去年搬到了门房。'
        self.pack(text=text)
        before = (self.root / '.gnovel/structure.json').read_bytes()
        result = chapter_check(self.root, chapterId=self.cid)
        self.assertTrue(result['factCandidates'])
        self.assertEqual(result['stateCandidateDraft']['changes'], [])
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)

    def test_new_body_invalidates_diagnostics_even_for_a_small_edit(self):
        self.pack(); run_diagnostics(self.root, chapter_id=self.cid, mode='plot', intensity_level=1, register=True)
        self.assertTrue(evidence_current(self.root, self.cid)['fresh'])
        self.pack(text=self.body.replace('钥匙', '卡片'))
        self.assertFalse(evidence_current(self.root, self.cid)['fresh'])

    def test_approved_stage_b_covers_its_exact_diagnostics_without_auto_approval(self):
        self.pack()
        run_diagnostics(self.root, chapter_id=self.cid, mode='plot', intensity_level=1, register=True)
        write_json(self.root / '工作台/review.json', {'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}, 'factAssessment': {'status': 'no_change'}, 'verdict': 'POLISH_READY', 'slots': evidenced_slots(self.body), 'findings': []})
        result = pack_artifact(self.root, chapterId=self.cid, artifactType='stage_b_review', input='工作台/review.json')
        workflow = self.workflow(); workflow['approvals'][self.cid]['artifactHashes'].pop('diagnostics', None)
        approve_artifacts(workflow, self.cid, artifact_hashes={'stage_b_review': result['artifactSha256']}, approval_mode='manual', project_root=self.root)
        write_json(self.root / '.gnovel/workflow.json', workflow)
        report = preflight_finalization(self.root, self.cid, skip_title=True)
        diag = next(r for r in report.artifacts if r['artifactType'] == 'diagnostics')
        self.assertEqual(diag['approvalCoveredBy'], 'stage_b_review')
        self.assertNotIn('diagnostics', self.workflow()['approvals'][self.cid]['artifactHashes'])

    def test_natural_language_discovery_and_operation_facade(self):
        for query, expected in [('登记候选正文 candidate_prose 封装工件', 'workflow.artifact-pack'),
                                ('生效规则卡', 'diagnostics.rules'), ('写一段查一段', 'diagnostics.paragraph'),
                                ('字数核对与新增事实', 'diagnostics.chapter-check')]:
            result = list_capabilities(query=query)
            self.assertIn(expected, {r['id'] for r in result['operations']})
        result = call_operation('diagnostics.paragraph', self.root, {'text': self.body, 'intensityLevel': 1})
        self.assertFalse(result['chapterGateEvaluated'])


if __name__ == '__main__': unittest.main()
