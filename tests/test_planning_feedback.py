"""CH002 feedback probes: ordinary prose, shared readiness and explicit plan exceptions."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import intensity_registry
from scripts.ai_cliche_linter import build_report, check_intensity_level_consistency
from scripts.core.artifact_pack import pack_artifact
from scripts.core.artifacts import file_sha256
from scripts.core.chapter_status import chapter_status
from scripts.core.jsonio import read_json, write_json
from scripts.core.mechanical_evidence import run_diagnostics, evidence_current
from scripts.core.outline_constraints import inspect_project_levels
from scripts.core.outline_facts import inspect_outline
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.structure_commands import preview_insert_chapter, commit_plan
from scripts.core.structure_store import load_structure
from scripts.core.task_prepare import prepare_task
from scripts.text import validator
from tests.fixtures_workflow import review_ready_project


class PlainSceneTests(unittest.TestCase):
    def test_daily_words_do_not_create_scene_action_segments(self):
        text = '他拨开门上的插销，头顶蹭着体操垫，柜顶的手机还在拍视频，夹缝里透进一点光。她慢吞吞地走来，下摆咬在嘴里。'
        self.assertIsNone(validator.scene_action_pattern().search(text))
        self.assertTrue(validator.check_sensory_channels(text)[0])
        self.assertEqual(validator.check_sensory_channels(text)[2], 'not_applicable')
        self.assertFalse(validator._is_sticky_sensory('他看了一眼黑板，转过身，回到座位上坐好'))

    def test_ordinary_metaphor_and_substring_do_not_satisfy_floor(self):
        text = '他累得像条死狗，精神还算不错，转身回到座位。'
        from scripts import intensity_registry
        config = intensity_registry.degradation_floor()
        config.update(enabled=True, words=['声明信号词'])
        with patch.object(intensity_registry, 'degradation_floor', return_value=config):
            result = validator.check_l5_floor(text, 5)
            self.assertIn('降格词0/', result[1])
            self.assertIn('生格/降格对比≈0/', result[1])
            self.assertFalse(result[0])

    def test_ambiguous_medical_and_place_words_are_advisory(self):
        from scripts import intensity_registry
        with patch.object(intensity_registry, 'ceiling_violations', return_value=('待核对词',)), \
             patch.object(intensity_registry, 'ambiguous_ceiling_words', return_value=('待核对词',)):
            text = '这里的待核对词需要结合上下文判断。'
            result = check_intensity_level_consistency(text, 3)
            self.assertEqual(result['status'], 'REVIEW_REQUIRED')
            self.assertFalse(result['violations']); self.assertTrue(result['contextReview'])
            self.assertEqual(build_report(text, intensity_level=3)['recommendation'], 'review_advisory')

    def test_both_cli_defaults_and_explicit_scope_use_same_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'body.md'
            path.write_text('值班员放下杯子，走到门前。门外的灯还亮着。')
            base = [sys.executable]
            def report(script, extra):
                proc = subprocess.run([*base, str(TOOLBOX_ROOT / 'scripts' / script), str(path),
                    '--mode', 'peakScene', '--intensity-level', '5', '--json', *extra], capture_output=True, text=True)
                self.assertIn(proc.returncode, (0, 1, 2), proc.stderr)
                return json.loads(proc.stdout)
            for flags, strict in (([], False), (['--strict-floor'], True)):
                validate = report('validate-text.py', flags)
                aggregate = report('aggregate_diagnostics.py', flags)
                self.assertEqual(validate['strict_l5'], strict)
                self.assertEqual(validate['checks'], aggregate['scriptResults']['validate_text']['checks'])
            path.write_text('第一段记录。\n\n' + '后续普通剧情。' * 400)
            proc = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/validate-text.py'), str(path),
                '--mode', 'mixed', '--intensity-level', '5', '--scene-paragraphs', '1', '--strict-floor', '--json'], capture_output=True, text=True)
            report = json.loads(proc.stdout)
            density = next(c for c in report['checks'] if c['rule_id'] == 'S-06')
            expected = validator.check_dialogue_dynamics_density('第一段记录。', 5, strict=True)
            self.assertEqual(density['detail'], expected[1])

    def test_low_level_excludes_adult_checks_and_floor(self):
        _, checks, _ = validator.build_checks('peakScene', False, 1)
        self.assertFalse({'L5张力等级地板', '冲突对白密度', '感官通道'} & {name for name, fn in checks})

    def test_argument_errors_are_json(self):
        proc = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project',
            'structure', 'link-project-preview', '--not-an-argument'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)['code'], 'ARGUMENTS_INVALID')


class PlanningReadinessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = review_ready_project(self.root, chapter_count=2)
        self.first, self.second = self.fixture['chapterIds']

    def test_board_and_prepare_share_writing_blockers(self):
        source = load_structure(self.root); source['chapters'][0]['status'] = 'draft'; source['chapters'][1]['status'] = 'planning_required'
        write_json(self.root / '.gnovel/structure.json', source)
        workflow = read_json(self.root / '.gnovel/workflow.json'); workflow['approvedChapterIds'] = [self.first]
        write_json(self.root / '.gnovel/workflow.json', workflow)
        board = chapter_status(self.root, self.second)
        prepare = prepare_task(self.root, 'planner', chapter_id=self.second)
        codes = {'PREDECESSOR_UNSTABLE', 'CHAPTER_OUTSIDE_APPROVED_RANGE'}
        self.assertTrue(codes <= {r['code'] for r in board['blockers']})
        self.assertTrue(codes <= {r['code'] for r in prepare['blocking']})

    def test_board_distinguishes_file_bytes_from_evidence_freshness(self):
        run_diagnostics(self.root, chapter_id=self.first, mode='plot', intensity_level=1, register=True)
        source = load_structure(self.root); source['revision'] += 1
        write_json(self.root / '.gnovel/structure.json', source)
        board = chapter_status(self.root, self.first)
        row = next(r for r in board['artifacts'] if r['artifactType'] == 'diagnostics')
        self.assertTrue(row['fileFresh']); self.assertTrue(row['fresh'])
        source['entities'][0]['voiceProfile']['bannedPhrasesForRole'].append('新增禁语')
        write_json(self.root / '.gnovel/structure.json', source)
        expected = evidence_current(self.root, self.first)
        board = chapter_status(self.root, self.first)
        row = next(r for r in board['artifacts'] if r['artifactType'] == 'diagnostics')
        self.assertTrue(row['fileFresh']); self.assertFalse(row['fresh'])
        self.assertEqual(row['freshnessReason'], expected['reason'])

    def test_planner_receives_prior_plan_candidate_and_open_items(self):
        report = prepare_task(self.root, 'planner', chapter_id=self.second)
        paths = {row.get('path') for row in report['readList']}
        source = load_structure(self.root)
        self.assertTrue(any(row.get('kind') == 'source-query' for row in report['readList']))
        workflow = read_json(self.root / '.gnovel/workflow.json')
        for kind in ('candidate_prose', 'stage_a_review'):
            expected = str(Path(workflow['artifacts'][self.first][kind]['artifactPath']).relative_to(self.root))
            self.assertIn(expected, paths)

    def test_project_level_conflict_requires_declaration_without_granting_approval(self):
        source = load_structure(self.root); chapter = source['chapters'][0]
        (self.root / 'AGENTS.md').write_text('第一卷 L5–L7，第二卷 L7–L8。')
        result = inspect_project_levels(self.root, source, chapter, '声明 L1。', 1)
        self.assertTrue(result['conflicts']); self.assertFalse(result['exceptionDeclared'])
        declared = inspect_project_levels(self.root, source, chapter, '与 AGENTS.md 张力等级区间的偏离：本章 L1，待作者批准。', 1)
        self.assertTrue(declared['exceptionDeclared']); self.assertFalse(declared['exceptionApproved'])


    def test_authored_stage_a_seats_are_projected_and_missing_seats_fail(self):
        from scripts.core.outline_constraints import stage_a_seats
        source = load_structure(self.root); chapter = source['chapters'][0]
        source['fragments'][0]['text'] = ('## 本章定位\n| 章节 | ' + self.first + ' |\n\n## 十身份讨论\n'
            + ''.join(f'- 身份{seat}（合成测试）：本段因果有原文支持。\n' for seat in range(1, 11)))
        write_json(self.root/'.gnovel/structure.json', source)
        payload = {'decision': 'adopt', 'activeSeats': list(range(1, 11))}
        stage_a_seats(self.root, source, self.first, payload)
        self.assertEqual(payload['seatReviews']['1']['conclusion'], '本段因果有原文支持。')
        self.assertIn('身份1', payload['seatReviews']['1']['evidence'][0]['text'])
        with self.assertRaises(ValueError) as caught:
            stage_a_seats(self.root, source, self.first, {'decision': 'adopt', 'activeSeats': [1, 2]})
        self.assertEqual(caught.exception.code, 'REVIEW_SEATS_MISSING')


    def test_preview_reports_renderer_drift_without_writing_story(self):
        source = load_structure(self.root)
        plan = preview_insert_chapter(self.root, title='新章', volume_id=source['volumes'][0]['volumeId'],
                                      primary_arc_id=source['arcs'][0]['arcId'])
        before = (self.root / '.gnovel/structure.json').read_bytes()
        with patch('scripts.core.structure_transaction.renderer_fingerprint', return_value='different'):
            with self.assertRaises(ValueError) as caught:
                commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)
        self.assertEqual(caught.exception.code, 'PLAN_INPUT_CHANGED')
        self.assertTrue(caught.exception.details['rendererChanged'])
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), before)


if __name__ == '__main__': unittest.main()
