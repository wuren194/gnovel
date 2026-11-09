"""Neutral reproductions from two real authoring sessions; originals never mutate."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from scripts import novelctl
from scripts.check_degeneration import detect_ai_self_reference
from scripts.sepia_bridge import analyze_zh_calibration, split_sentences
from scripts.ai_cliche_linter import build_report
from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifact_templates import artifact_guide
from scripts.core.chapter_refs import resolve_chapter_arguments
from scripts.core.chapter_contract import source_contract
from scripts.core.jsonio import write_json, read_json
from scripts.core.operations import call_operation
from scripts.core.outline_facts import inspect_outline
from scripts.core.source_changes import preview_source_change
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import validate_structure
from scripts.core.writing_checks import audit_chapter, outline_requirements
from scripts.core.writing_notes import validate_notes
from scripts.text.validator import ai_pattern_violations, check_ai_patterns
from tests.test_source_plan import planning_source
from tests.fixtures_workflow import review_ready_project


class AuthoringSurfaceTests(unittest.TestCase):
    def test_scope_uses_placement_and_does_not_merge_two_volumes_into_none(self):
        source = {'schemaVersion': 5, 'chapters': [
            {'chapterId': 'CH-A', 'displayOrdinal': 1, 'status': 'planned',
             'placement': {'scope': 'volume', 'volumeId': 'VOL-A'}},
            {'chapterId': 'CH-B', 'displayOrdinal': 2, 'status': 'planned',
             'placement': {'scope': 'volume', 'volumeId': 'VOL-B'}}]}
        with self.assertRaises(novelctl.NovelError) as caught:
            novelctl._approval_scope(source, ['CH-A', 'CH-B'], allow_gaps=False, reason='')
        self.assertEqual(caught.exception.code, 'APPROVAL_RANGE_CROSSES_VOLUME')
        scope = novelctl._approval_scope(source, ['CH-A'], allow_gaps=False, reason='')
        self.assertEqual(scope['volumeIds'], ['VOL-A'])
        source['chapters'][1]['placement'] = {'scope': 'project'}
        scope = novelctl._approval_scope(source, ['CH-B'], allow_gaps=False, reason='')
        self.assertEqual(scope['volumeIds'], [])
        self.assertTrue(scope['includesProjectScope'])

    def test_append_singular_chapter_argument_resolves_display_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / '.gnovel/structure.json', {'chapters': [
                {'chapterId': 'CH-STABLE', 'displayOrdinal': 1, 'status': 'planned'}]})
            args = {'chapter_id': ['CH001']}
            resolved = resolve_chapter_arguments(root, args)
            self.assertEqual(args['chapter_id'], ['CH-STABLE'])
            self.assertEqual(resolved[0]['reference'], 'CH001')

    def test_inline_character_dialogue_is_not_an_ai_refusal(self):
        self.assertFalse(detect_ai_self_reference('甲说：“我不能保证今天到。”乙点头。'))
        self.assertFalse(detect_ai_self_reference('“我不能保证今天到。”'))
        self.assertTrue(detect_ai_self_reference('甲说：“今天到。”作为一个AI，我不能继续。'))

    def test_pronoun_locations_are_not_guessed_as_inanimate_agency(self):
        for text in ('箱子很重，它在一个人怀里。', '烟盒不见了，它在口袋里。', '他看它停在掌中。'):
            self.assertFalse([r for r in build_report(text)['findings'] if r['ruleId'] == 'INANIMATE-AGENCY'])
        self.assertTrue([r for r in build_report('那个秘密一直在等。')['findings']
                         if r['ruleId'] == 'INANIMATE-AGENCY'])

    def test_closing_quote_is_not_a_sentence_or_a_flat_run(self):
        text = '“看清再说。吃饭。”'
        self.assertEqual(len(split_sentences(text)), 2)
        self.assertTrue(split_sentences(text)[-1].endswith('”'))
        report = analyze_zh_calibration(text)
        self.assertEqual(report['sentence_count'], 2)
        self.assertEqual(report['flat_runs_count'], 0)
        self.assertFalse(analyze_zh_calibration('甲走了。\n\n乙坐下。\n\n丙来了。')['flat_runs'])
        self.assertTrue(analyze_zh_calibration('甲走了。乙坐下。丙来了。')['flat_runs'])

    def test_triple_parallelism_has_exact_offsets_and_line(self):
        text = '门开着。\n纸干透了，脆了，使猛了。'
        self.assertFalse(check_ai_patterns(text)[0])
        hit = next(r for r in ai_pattern_violations(text) if r['pattern'] == '三项同构排比')
        self.assertEqual(hit['line'], 2)
        self.assertEqual(text[hit['start']:hit['end']], hit['text'])
        self.assertIn('干透了', hit['text'])

    def test_outline_quoted_book_name_is_not_a_required_prose_quote(self):
        text = '# 第三卷「看守」\n## 必须落地\n1. 关键台词“明天再来”。\n2. 推进「看守」关系。\n## 节奏段\nB1 冲突“某句”。'
        self.assertEqual(outline_requirements(text)['quotedTargets'], ['明天再来'])


    def test_guides_explain_ten_seats_two_gates_and_state_projections(self):
        guide = artifact_guide('stage_b_review')['seatStructure']
        self.assertEqual(len(guide['primarySeats']), 10)
        self.assertEqual(guide['auxiliaryGates'], ['cognition', 'organization'])
        state = artifact_guide('state_candidate')
        self.assertEqual(state['changes']['value'], [])
        self.assertIn('source.change-preview', state['proposedFacts'])


class SourceContractReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source, self.chapter = planning_source(self.root)
        self.cid = self.chapter['chapterId']

    def test_existing_local_plan_prose_budget_reaches_diagnostics(self):
        self.source['fragments'][-1].pop('chapterContract', None)
        self.source['fragments'][-1]['text'] = '字数：8500–10000汉字。\nB01 4000字：进门。\nB02 5000：出门。'
        write_json(self.root / '.gnovel/structure.json', self.source)
        facts = inspect_outline(self.root, chapterId=self.cid)
        self.assertEqual(facts['targetRange'], [8500, 10000])
        self.assertEqual(facts['beatBudgetSum'], 9000)
        report = audit_chapter(self.root, self.source, self.cid, '字' * 7474)
        self.assertFalse(report['passed'])
        self.assertEqual(report['lengthDelta']['belowMinimum'], 1026)
        self.assertIn('sourcePlanHash', report)
        self.assertNotIn('outlineSha256', report)

    def test_missing_or_ambiguous_range_is_explicitly_not_evaluated(self):
        self.source['fragments'][-1].pop('chapterContract', None)
        report = audit_chapter(self.root, self.source, self.cid, '简短正文。')
        self.assertFalse(report['passed'])
        self.assertEqual(report['findings'][0]['evaluationStatus'], 'not_evaluated')
        self.source['fragments'][-1]['text'] = '8500–10000汉字；9000–12000汉字。'
        facts = source_contract(self.source, self.cid)
        self.assertIn('CHAPTER_CONTRACT_AMBIGUOUS', facts['issues'])

    def test_typed_contract_supports_short_story_and_hash_invalidation(self):
        before = chapter_plan(self.source, self.cid)['sourcePlanHash']
        contract = {'targetRange': [10, 50], 'beats': [{'id': 'B1', 'budget': 30}],
                    'pov': 'C-ALPHA', 'mode': 'plot', 'intensityLevel': 1,
                    'requirements': ['值班员关门'], 'quotedTargets': ['明天再来']}
        self.source['fragments'][-1]['chapterContract'] = contract
        validate_structure(self.source)
        write_json(self.root / '.gnovel/structure.json', self.source)
        self.assertNotEqual(chapter_plan(self.source, self.cid)['sourcePlanHash'], before)
        report = audit_chapter(self.root, self.source, self.cid, '值班员合上门，告诉来人明天再来。')
        self.assertTrue(report['passed'])
        self.assertEqual(report['quotedTargets'][0]['status'], 'literal_found')
        self.assertEqual(report['pov']['characterId'], 'C-ALPHA')
        from scripts.core.source_render import render_source
        rendered = render_source(self.root)['markdown']
        self.assertIn('targetRange', rendered)
        self.assertIn('明天再来', rendered)

    def test_contract_write_rejects_invalid_numbers_and_fact_scope(self):
        fragment = self.source['fragments'][-1]
        fragment['chapterContract'] = {'targetRange': [True, 100]}
        with self.assertRaises(ValueError): validate_structure(self.source)
        fragment['chapterContract'] = {'targetRange': [10, 100]}
        fragment['contentRole'] = 'fact'
        with self.assertRaises(ValueError): validate_structure(self.source)

    def test_generic_preview_accepts_contract_and_binds_stable_plan(self):
        fragment = deepcopy(self.source['fragments'][-1])
        fragment['chapterContract'] = {'targetRange': [10, 50]}
        pre = preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'fragments', 'record': fragment}],
                                    requestId='contract', baseRevision=self.source['revision'], reason='author supplied contract')
        commit_plan(self.root, operation_id=pre['operationId'], plan_hash=pre['planHash'], approved=True)
        self.assertEqual(inspect_outline(self.root, chapterId=self.cid)['targetRange'], [10, 50])

    def test_doctor_uses_source_plan_even_if_outline_locator_is_missing(self):
        from scripts.core.project_doctor import refactor_path_issues
        self.source['chapters'][0]['status'] = 'review'
        # Check just the actual path/plan subcheck; no fake Markdown export.
        issues = refactor_path_issues(self.root, self.source)
        self.assertNotIn('REFACTOR_OUTLINE_PATH_MISSING', {r['code'] for r in issues})
        self.assertNotIn('SOURCE_CHAPTER_PLAN_MISSING', {r['code'] for r in issues})
        self.source['fragments'][-1]['status'] = 'archived'
        issues = refactor_path_issues(self.root, self.source)
        self.assertIn('SOURCE_CHAPTER_PLAN_MISSING', {r['code'] for r in issues})


class PackHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        (self.root/'工作台/body.txt').write_text('甲说：“稍后再来。”')

    def test_notes_inline_json_returns_actionable_error(self):
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
                          input='工作台/body.txt', notes='{"newFacts": []}')
        self.assertEqual(caught.exception.code, 'WRITING_NOTES_PATH_REQUIRED')
        self.assertEqual(caught.exception.details['example']['notes'], '工作台/写作说明.json')

    def test_wording_note_requires_quotes_from_both_plan_and_candidate(self):
        source = read_json(self.root/'.gnovel/structure.json')
        source['fragments'][0]['text'] = '原计划台词：明天再来。'
        write_json(self.root/'.gnovel/structure.json', source)
        note = {'deviations': [{'id': 'W1', 'kind': 'wording', 'plannedText': '明天再来',
                              'quote': '稍后再来', 'reason': '提议呈现措辞，阶段B仍需判断是否改动事实。'}]}
        validate_notes(note, '甲说：“稍后再来。”')
        note['deviations'][0]['quote'] = '根本没写'
        with self.assertRaises(ValueError): validate_notes(note, '甲说：“稍后再来。”')

    def test_new_facts_require_source_proposal_and_preserve_external_files(self):
        target = self.root/'设定/回写测试.md'; target.write_text('# 外部资料\n')
        notes = {'newFacts': [{'id': 'F1', 'statement': '甲说稍后再来', 'quote': '稍后再来'}]}
        with self.assertRaises(ValueError) as caught:
            pack_artifact(self.root, chapterId=self.cid, artifactType='candidate_prose',
                          text='甲说：“稍后再来。”', writingNotes=notes)
        self.assertEqual(caught.exception.code, 'SOURCE_FACTS_REQUIRE_PROPOSAL')
        self.assertEqual(target.read_text(), '# 外部资料\n')


class SummaryHandoffTests(unittest.TestCase):
    def test_commit_returns_continuity_hash_and_explicit_reading_export(self):
        from scripts.story_summarizer import _prepare_context, _commit_summary
        from scripts.core.artifacts import file_sha256
        from tests.fixtures_current import create_schema5_project
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            create_schema5_project(root, chapter_count=1)
            prepared = _prepare_context(root, view='model')
            payload = prepared['inputSkeleton']
            payload['storySummary'] = '甲先关门，再将钥匙交还。'
            write_json(root/'input.json', payload)
            result = _commit_summary(root, 'input.json')
            self.assertEqual(result['sha256'], file_sha256(root/result['path']))
            self.assertEqual(result['summaryVersion'], result['sha256'])
            self.assertEqual(result['continuitySummary']['sha256'], result['sha256'])
            exported = call_operation('summary.export', root, {'output': '工作台/摘要.md'})
            self.assertIn(payload['storySummary'], Path(exported['output']).read_text())
            self.assertFalse(exported['maintained'])
            Path(exported['output']).write_text('作者修改的阅读文件')
            with self.assertRaises(ValueError):
                call_operation('summary.export', root, {'output': '工作台/摘要.md'})

    def test_discovery_finds_bootstrap_schema_and_summary(self):
        from scripts.core.tool_catalog import list_capabilities
        for query, operation in [('新建项目', 'project.quick-init'), ('查询源数据结构', 'source.schema'),
                                 ('导出故事摘要', 'summary.export')]:
            report = list_capabilities(query=query)
            self.assertIn(operation, {row['id'] for row in report['operations']})
