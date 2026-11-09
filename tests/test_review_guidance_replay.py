"""Public authoring entrypoints replace mechanical hand assembly, not review work."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.cli_examples import command_example
from scripts.core.operations import call_operation, operations, validate_arguments
from scripts.core.project import query_policy
from scripts.core.query import _size
from scripts.core.review_plan import resolve_effective_review_plan
from scripts.core.source_plan import chapter_plan
from scripts.mcp_server import handle
from scripts.novelctl import build_parser
from tests.fixtures_current import initialize_project
from tests.fixtures_workflow import review_ready_project


ROOT = Path(__file__).resolve().parents[1]


class ReviewGuidanceReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'book'
        self.cid = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        environment = patch.dict(os.environ, {'GNOVEL_LOG_MODE': 'off',
                                              'PYTHONDONTWRITEBYTECODE': '1'})
        environment.start()
        self.addCleanup(environment.stop)

    def inventory(self, root=None):
        root = root or self.root
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob('*') if path.is_file()}

    def cli(self, *arguments):
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project', *arguments],
                                 cwd=ROOT, env=dict(os.environ, GNOVEL_LOG_MODE='off',
                                                    PYTHONDONTWRITEBYTECODE='1'),
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.stderr, '', process.stderr)
        return process.returncode, json.loads(process.stdout)

    def mcp(self, operation, arguments, *, root=None):
        spec = next(row for row in operations() if row.id == operation)
        self.assertTrue(spec.mcp, operation)
        supplied = {'projectRoot': str(root or self.root), **arguments}
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': spec.name, 'arguments': supplied}})
        self.assertNotIn('error', response, response)
        return response['result']

    def full(self, response, *, root=None):
        if not response.get('responseCompacted'):
            return response
        action = response['readAction']
        self.assertEqual(action['operation'], 'task.snapshot')
        arguments = dict(action['arguments'])
        fragments, cursors = [], set()
        while True:
            page = self.mcp(action['operation'], arguments, root=root)
            self.assertFalse(page['isError'], page)
            payload = page['structuredContent']
            self.assertLessEqual(_size(payload), query_policy()['maxBytes']['detail'])
            self.assertEqual(payload['reference'], action['arguments']['reference'])
            fragments.extend(row['text'] for row in payload['materials'])
            if not payload['hasMore']:
                break
            cursor = payload['nextCursor']
            self.assertNotIn(cursor, cursors, 'snapshot pagination must make progress')
            cursors.add(cursor)
            arguments['cursor'] = cursor
        return json.loads(''.join(fragments))

    def form(self, *, root=None, chapter=None, view='full'):
        response = self.mcp('workflow.review-form', {
            'chapterId': chapter or self.cid, 'artifactType': 'stage_a_review', 'view': view}, root=root)
        self.assertFalse(response['isError'], response)
        return self.full(response['structuredContent'], root=root)

    def commit_changes(self, changes, request_id):
        source = read_json(self.root / '.gnovel/structure.json')
        preview = call_operation('source.change-preview', self.root, {
            'changes': changes, 'requestId': request_id, 'baseRevision': source['revision'],
            'reason': '合成夹具通过公开源事务改变测试材料。'})
        action = preview['nextAction']
        return call_operation(action['operation'], self.root, action['arguments'])

    def recovery_reads(self, error, kind):
        recovery = error['details']['recovery']
        self.assertEqual(recovery['kind'], kind)
        self.assertIs(recovery['automatic'], False)
        self.assertIs(recovery['preservesExistingEvidence'], True)
        self.assertTrue(recovery['instruction'])
        self.assertTrue(recovery['readActions'])
        specs = {row.id: row for row in operations()}
        results = {}
        for action in recovery['readActions']:
            spec = specs[action['operation']]
            self.assertEqual(spec.effects, 'read', action)
            supplied = {'projectRoot': str(self.root), **action['arguments']}
            validate_arguments(spec.input_schema, supplied)
            response = self.mcp(action['operation'], action['arguments'])
            self.assertFalse(response['isError'], response)
            result = self.full(response['structuredContent'])
            if action['operation'] == 'workflow.review-form' and 'payload' not in result:
                full_action = result['fullAction']
                result = self.full({'responseCompacted': True, 'reportRef': result['formRef'],
                                    'readAction': full_action})
            results[action['operation']] = result
        return recovery, results

    def test_cli_and_mcp_form_share_current_bindings_and_actual_empty_seats(self):
        source = read_json(self.root / '.gnovel/structure.json')
        chapter = deepcopy(source['chapters'][0])
        chapter.update(reviewClass='ordinary', newLocation=True)
        self.commit_changes([{'action': 'put', 'collection': 'chapters', 'record': chapter}], 'seat-scope')
        before = self.inventory()
        status, cli = self.cli('workflow', 'review-form', '--project-root', str(self.root),
                               '--chapter-id', 'CH001', '--artifact-type', 'stage_a_review', '--view', 'full')
        self.assertEqual(status, 0, cli)
        cli = self.full(cli)
        mcp = self.form()
        source = read_json(self.root / '.gnovel/structure.json')
        expected = resolve_effective_review_plan('gnovel-planner', 'chapter', chapter=source['chapters'][0])
        self.assertEqual(cli['payload'], mcp['payload'])
        self.assertEqual(mcp['payload']['sourcePlanHash'], chapter_plan(source, self.cid)['sourcePlanHash'])
        self.assertEqual(mcp['payload']['activeSeats'], expected['activeSeats'])
        self.assertEqual(set(mcp['payload']['seatReviews']), {str(seat) for seat in expected['activeSeats']})
        self.assertTrue(all(row == {'conclusion': '', 'evidence': []}
                            for row in mcp['payload']['seatReviews'].values()))
        self.assertEqual(mcp['payload']['decision'], 'pending')
        for field in ('registered', 'approved', 'semanticReviewComplete'):
            self.assertIs(mcp[field], False)
        self.assertEqual(self.inventory(), before)

    def test_large_form_read_action_delivers_the_same_payload_and_guide(self):
        expected = self.form()
        before = self.inventory()
        policy = deepcopy(query_policy())
        policy['maxBytes']['detail'] = 2500
        with patch('scripts.core.project.query_policy', return_value=policy):
            result = self.mcp('workflow.review-form', {'chapterId': self.cid, 'artifactType': 'stage_a_review', 'view': 'full'})
            self.assertFalse(result['isError'], result)
            compact = result['structuredContent']
            self.assertTrue(compact['responseCompacted'])
            self.assertLessEqual(_size(compact), policy['maxBytes']['detail'])
            restored = self.full(compact)
        self.assertEqual(restored['payload'], expected['payload'])
        self.assertEqual(restored['guide'], expected['guide'])
        self.assertEqual(restored['reviewPlan'], expected['reviewPlan'])
        self.assertEqual(self.inventory(), before)

    def test_old_plan_form_is_rejected_with_current_read_actions_and_no_reapproval(self):
        old_form = self.form()
        source = read_json(self.root / '.gnovel/structure.json')
        plan = deepcopy(next(row for row in source['fragments'] if row['ownerId'] == self.cid))
        plan['text'] += '值班员交还钥匙，登记交班结果。'
        self.commit_changes([{'action': 'put', 'collection': 'fragments', 'record': plan}], 'changed-plan')
        before = self.inventory()
        status, error = self.cli('workflow', 'artifact-pack', '--project-root', str(self.root),
                                 '--chapter-id', self.cid, '--artifact-type', 'stage_a_review',
                                 '--payload', json.dumps(old_form['payload'], ensure_ascii=False))
        self.assertEqual(status, 1, error)
        self.assertEqual(error['code'], 'ARTIFACT_BINDING_STALE')
        self.assertEqual(error['details']['field'], 'sourcePlanHash')
        self.assertEqual(self.inventory(), before)
        recovery, results = self.recovery_reads(error, 'review_inputs_changed')
        self.assertEqual(set(results), {'source.chapter-plan', 'workflow.review-form'})
        self.assertNotEqual(results['workflow.review-form']['payload']['sourcePlanHash'],
                            old_form['payload']['sourcePlanHash'])
        self.assertEqual(results['workflow.review-form']['payload']['decision'], 'pending')
        self.assertIn('不要只改 hash', recovery['instruction'])
        self.assertEqual(self.inventory(), before)

    def test_missing_seats_and_invalid_coverage_stay_rejected_and_return_blank_form(self):
        original = self.form()['payload']
        cases = [({'decision': 'adopt', 'activeSeats': [1]}, 'REVIEW_SEATS_MISSING'),
                 ({'behaviorCoverage': {'relationship': 'complete', 'living': 'not_applicable'}},
                  'BEHAVIOR_COVERAGE_INVALID')]
        for change, code in cases:
            with self.subTest(code=code):
                payload = deepcopy(original)
                payload.update(change)
                before = self.inventory()
                result = self.mcp('workflow.artifact-pack', {
                    'chapterId': self.cid, 'artifactType': 'stage_a_review', 'payload': payload})
                self.assertTrue(result['isError'], result)
                error = result['structuredContent']
                self.assertEqual(error['code'], code)
                recovery, reads = self.recovery_reads(error, 'review_form_required')
                self.assertEqual(set(reads), {'workflow.review-form'})
                self.assertEqual(reads['workflow.review-form']['payload'], original)
                self.assertEqual(self.inventory(), before)

    def test_incomplete_pending_review_is_storable_but_not_completion_credit(self):
        payload = deepcopy(self.form()['payload'])
        payload.update(decision='pending', activeSeats=[1], seatReviews={})
        result = self.mcp('workflow.artifact-pack', {
            'chapterId': self.cid, 'artifactType': 'stage_a_review', 'payload': payload})
        self.assertFalse(result['isError'], result)
        packed = result['structuredContent']
        self.assertTrue(packed['registered'])
        self.assertFalse(packed['approved'])
        self.assertFalse(packed['approvalPerformed'])
        # Approval of storing this exact pending judgment must not turn it into
        # an adopted plan; the public advancement gate still reads its decision.
        code, approval = self.cli('workflow', 'artifact-approve', '--project-root', str(self.root),
                                  '--chapter-id', self.cid, '--artifact', '@' + str(self.root / packed['artifactPath']))
        self.assertEqual(code, 0, approval)
        before = self.inventory()
        code, denied = self.cli('workflow', 'advance', '--project-root', str(self.root),
                               '--chapter-id', self.cid, '--to', 'planned')
        self.assertEqual(code, 1, denied)
        self.assertEqual(denied['code'], 'STAGE_A_NOT_ADOPTED')
        self.assertEqual(self.inventory(), before)

    def test_existing_chapter_without_plan_gets_read_only_recovery_not_duplicate_creation(self):
        source = read_json(self.root / '.gnovel/structure.json')
        plan = next(row for row in source['fragments'] if row['ownerId'] == self.cid)
        self.commit_changes([{'action': 'delete', 'collection': 'fragments', 'id': plan['fragmentId']}], 'remove-plan')
        before = self.inventory()
        result = self.mcp('workflow.review-form', {'chapterId': self.cid, 'artifactType': 'stage_a_review'})
        self.assertTrue(result['isError'], result)
        error = result['structuredContent']
        self.assertEqual(error['code'], 'SOURCE_CHAPTER_PLAN_MISSING')
        recovery, reads = self.recovery_reads(error, 'plan_required')
        self.assertEqual(set(reads), {'source.schema', 'source.chapter-plan'})
        self.assertFalse(reads['source.chapter-plan']['ready'])
        self.assertEqual(recovery['afterPlanReady']['operation'], 'workflow.review-form')
        self.assertEqual(recovery['afterPlanReady']['arguments']['chapterId'], self.cid)
        self.assertEqual(self.inventory(), before)

    def test_corrupt_source_guidance_keeps_schema_readable_without_repairing_project(self):
        # Corrupt only this disposable fixture to replay the observed rejection.
        valid_source = read_json(self.root / '.gnovel/structure.json')
        valid_plan = deepcopy(next(row for row in valid_source['fragments'] if row['ownerId'] == self.cid))
        valid_payload = self.form()['payload']
        for bad_field, expected_code in [('contentBindings', 'SOURCE_BINDINGS_REQUIRED'),
                                         ('mode', 'CHAPTER_CONTRACT_MODE_INVALID')]:
            with self.subTest(field=bad_field):
                source = deepcopy(valid_source)
                plan = next(row for row in source['fragments'] if row['ownerId'] == self.cid)
                if bad_field == 'contentBindings':
                    plan['contentBindings'] = []
                else:
                    plan['chapterContract']['mode'] = 'not-a-mode'
                write_json(self.root / '.gnovel/structure.json', source)
                before = self.inventory()
                for operation, extra in [('workflow.review-form', {}),
                                         ('workflow.artifact-pack', {'payload': valid_payload})]:
                    result = self.mcp(operation, {'chapterId': self.cid, 'artifactType': 'stage_a_review', **extra})
                    self.assertTrue(result['isError'], result)
                    error = result['structuredContent']
                    self.assertEqual(error['code'], 'STRUCTURE_INVALID')
                    self.assertIn(expected_code, {row['code'] for row in error['details']['errors']})
                    recovery, reads = self.recovery_reads(error, 'source_invalid')
                    self.assertIs(recovery['requiresMaintenance'], True)
                    self.assertIs(recovery['ordinarySourcePreviewBlocked'], True)
                    self.assertEqual(set(reads), {'source.schema'})
                    self.assertEqual(reads['source.schema']['code'], 'SOURCE_SCHEMA')
                    actions = []

                    def collect_actions(value):
                        if isinstance(value, dict):
                            if 'operation' in value:
                                actions.append(value['operation'])
                            for child in value.values():
                                collect_actions(child)
                        elif isinstance(value, list):
                            for child in value:
                                collect_actions(child)

                    collect_actions(recovery)
                    self.assertEqual(actions, ['source.schema'], 'invalid baselines cannot advertise executable source changes')
                    self.assertEqual(self.inventory(), before)
                # Even a valid replacement record cannot enter the ordinary
                # preview channel until the current invalid baseline is restored.
                preview = self.mcp('source.change-preview', {
                    'requestId': 'repair-invalid-' + bad_field, 'baseRevision': source['revision'],
                    'reason': '合成坏源场景核对普通预览仍被阻断。',
                    'changes': [{'action': 'put', 'collection': 'fragments', 'record': valid_plan}]})
                self.assertTrue(preview['isError'], preview)
                self.assertEqual(preview['structuredContent']['code'], 'STRUCTURE_INVALID')
                self.assertEqual(self.inventory(), before)

    def test_unassessed_plan_coverage_requires_plan_review_before_another_form(self):
        source = read_json(self.root / '.gnovel/structure.json')
        plan = deepcopy(next(row for row in source['fragments'] if row['ownerId'] == self.cid))
        plan['chapterContract']['behaviorCoverage']['relationship'] = 'unassessed'
        self.commit_changes([{'action': 'put', 'collection': 'fragments', 'record': plan}], 'unassessed-plan')
        payload = self.form()['payload']
        payload.update(decision='adopt', behaviorCoverage={'relationship': 'not_applicable', 'living': 'not_applicable'})
        for row in payload['seatReviews'].values():
            row.update(conclusion='合成夹具已核对当前计划中的一次钥匙交接。', evidence=[{'text': plan['text']}])
        before = self.inventory()
        result = self.mcp('workflow.artifact-pack', {
            'chapterId': self.cid, 'artifactType': 'stage_a_review', 'payload': payload})
        self.assertTrue(result['isError'], result)
        error = result['structuredContent']
        self.assertEqual(error['code'], 'BEHAVIOR_PLAN_COVERAGE_REQUIRED')
        recovery, reads = self.recovery_reads(error, 'plan_coverage_review')
        self.assertEqual(set(reads), {'source.chapter-plan', 'source.schema'})
        self.assertNotIn('workflow.review-form', {action['operation'] for action in recovery['readActions']})
        self.assertEqual(recovery['afterPlanReady'], {'operation': 'workflow.review-form',
            'arguments': {'chapterId': self.cid, 'artifactType': 'stage_a_review'}})
        self.assertEqual(read_json(self.root / '.gnovel/structure.json')['fragments'][0]
                         ['chapterContract']['behaviorCoverage']['relationship'], 'unassessed')
        self.assertEqual(self.inventory(), before)

    def test_coverage_without_items_requires_assessment_not_blank_form_retry(self):
        source = read_json(self.root / '.gnovel/structure.json')
        plan = deepcopy(next(row for row in source['fragments'] if row['ownerId'] == self.cid))
        plan['chapterContract']['behaviorCoverage']['relationship'] = 'applicable'
        self.assertEqual(plan['chapterContract'].get('relationshipCausality', []), [])
        self.commit_changes([{'action': 'put', 'collection': 'fragments', 'record': plan}], 'coverage-no-items')
        payload = self.form()['payload']
        payload.update(decision='adopt', behaviorCoverage=deepcopy(plan['chapterContract']['behaviorCoverage']))
        for row in payload['seatReviews'].values():
            row.update(conclusion='合成夹具仅提供当前文字证据，未补造关系声明。', evidence=[{'text': plan['text']}])
        before = self.inventory()
        result = self.mcp('workflow.artifact-pack', {
            'chapterId': self.cid, 'artifactType': 'stage_a_review', 'payload': payload})
        self.assertTrue(result['isError'], result)
        error = result['structuredContent']
        self.assertEqual(error['code'], 'BEHAVIOR_COVERAGE_CONFLICT')
        self.assertEqual(error['details']['domain'], 'relationship')
        recovery, reads = self.recovery_reads(error, 'coverage_evidence_conflict')
        self.assertEqual(set(reads), {'source.chapter-plan', 'source.schema'})
        self.assertNotIn('workflow.review-form', {action['operation'] for action in recovery['readActions']})
        self.assertEqual(recovery['afterAssessment'], {'operation': 'workflow.review-form',
            'arguments': {'chapterId': self.cid, 'artifactType': 'stage_a_review'}})
        self.assertEqual(self.inventory(), before)

    def test_chapter_preview_form_and_submit_do_not_require_caller_binding_fields(self):
        root = self.directory / 'new-book'
        source = initialize_project(root)
        preview = call_operation('source.change-preview', root, {
            'requestId': 'plan-field', 'baseRevision': source['revision'], 'reason': '合成章节规划字段',
            'changes': [{'action': 'put', 'collection': 'fieldDefinitions', 'record': {
                'fieldId': '$planField', 'key': 'plot.plan', 'label': '章节推演',
                'valueType': 'topic', 'appliesTo': ['chapter']}}]})
        action = preview['nextAction']
        call_operation(action['operation'], root, action['arguments'])
        outline = '值班员交还钥匙，登记交班结果。'
        arguments = {'requestId': 'chapter-first', 'title': '交班',
                     'volumeId': source['volumes'][0]['volumeId'], 'primaryArcId': source['arcs'][0]['arcId'],
                     'outlineText': outline, 'reason': '合成任务创建章节与完整推演',
                     'planField': 'plot.plan', 'dependsOn': [],
                     'chapterContract': {'targetRange': [10, 80], 'mode': 'plot', 'intensityLevel': 1,
                                         'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}}
        source_before = (root / '.gnovel/structure.json').read_bytes()
        result = self.mcp('workflow.chapter-preview', arguments, root=root)
        self.assertFalse(result['isError'], result)
        preview = result['structuredContent']
        self.assertEqual((root / '.gnovel/structure.json').read_bytes(), source_before)
        action = preview['nextAction']
        committed = self.mcp(action['operation'], action['arguments'], root=root)
        self.assertFalse(committed['isError'], committed)
        current = read_json(root / '.gnovel/structure.json')
        self.assertEqual(len(current['chapters']), 1)
        cid = current['chapters'][0]['chapterId']
        plans = [row for row in current['fragments'] if row['ownerId'] == cid and row['contentRole'] == 'plan']
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['text'], outline)
        attributes = {row['attributeId']: row for row in current['attributes']}
        self.assertEqual(len(plans[0]['contentBindings']), 1)
        self.assertEqual(attributes[plans[0]['contentBindings'][0]]['ownerId'], cid)
        form = self.form(root=root, chapter=cid, view='model')
        self.assertNotIn('payload', form)
        edits = deepcopy(form['fields'])
        self.assertNotIn('sourcePlanHash', edits)
        self.assertNotIn('activeSeats', edits)
        edits.update(decision='adopt', behaviorCoverage=arguments['chapterContract']['behaviorCoverage'])
        for row in edits['seatReviews'].values():
            row.update(conclusion='合成交班计划仅一次钥匙交接，没有承重关系或长期生活安排。',
                       evidence=[{'text': outline}])
        approvals = read_json(root / '.gnovel/workflow.json')['approvals']
        submitted = self.mcp(form['submitAction']['operation'], {
            'requestId': 'review-first', 'chapterId': cid,
            'artifacts': [{'artifactType': 'stage_a_review', 'formRef': form['formRef'], 'edits': edits}]}, root=root)
        self.assertFalse(submitted['isError'], submitted)
        result = self.full(submitted['structuredContent'], root=root)
        self.assertTrue(result['submissionComplete'], result)
        self.assertFalse(result['approvalsPerformed'])
        self.assertEqual(read_json(root / '.gnovel/workflow.json')['approvals'], approvals)
        self.assertEqual(read_json(root / '.gnovel/structure.json')['chapters'][0]['status'], 'planning_required')
        self.assertTrue(all(row['status'] == 'registered' for row in result['stages']))

    def test_documented_authoring_flow_reaches_shared_cli_and_mcp_definitions(self):
        document = (ROOT / 'SKILL.md').read_text()
        table = document.split('## 建章、审校与提交', 1)[1].split('\n## ', 1)[0]
        recommended = set(re.findall(r'\| `((?:workflow|source)\.[a-z-]+)` \|', table))
        self.assertEqual(recommended, {'workflow.chapter-preview', 'source.chapter-plan',
                                       'workflow.review-form', 'workflow.submit', 'workflow.state-prepare',
                                       'workflow.closeout-prepare', 'workflow.closeout'})
        self.assertNotIn('项目操作统一从 `novelctl.py` 进入', document)
        parser = build_parser()
        by_id = {spec.id: spec for spec in operations()}
        before = self.inventory()
        for operation in recommended:
            with self.subTest(operation=operation):
                spec = by_id[operation]
                discovered = self.mcp('capabilities.list', {'operation': operation})
                self.assertFalse(discovered['isError'], discovered)
                definition = discovered['structuredContent']['operations'][0]
                self.assertEqual(definition['mcp'], spec.name)
                self.assertEqual(definition['cli'], ['project', *spec.cli])
                example = spec.examples[0] if spec.examples else {'chapterId': 'CH001'}
                parsed = parser.parse_args(shlex.split(command_example(spec, example))[2:])
                self.assertEqual(parsed.operation_binding, operation)
        self.assertEqual(self.inventory(), before)


if __name__ == '__main__':
    unittest.main()
