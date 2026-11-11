"""Model transport preserves decisions and user material with exact full access."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import mcp_server, novelctl
from scripts.core import call_logging, transport_cache
from scripts.core.context_transport import task_model_response
from scripts.core.model_response import project_response
from scripts.core.operations import call_operation
from scripts.core.structure_store import StructureError


def artifacts(count=12):
    return [{'artifactType': f'test-{index}', 'registered': True, 'approved': True, 'fresh': True,
             'envelopeValid': True, 'invalidated': False, 'approvalRequired': True,
             'artifactPath': f'工作台/工件-{index}.json',
             'artifactSha256': hashlib.sha256(str(index).encode()).hexdigest()}
            for index in range(count)]


def receipt_sample():
    blocker = {'code': 'ARTIFACTS_NOT_APPROVED', 'message': '本次工件尚待作者阅读和批准。' * 25,
               'details': {'chapterId': 'chapter-EXAMPLE', 'artifactType': 'stage_b_review',
                           'artifactPath': '工作台/B.json', 'artifactSha256': 'a' * 64},
               'recovery': {'readActions': [{'operation': 'workflow.review-form', 'arguments': {
                   'chapterId': 'chapter-EXAMPLE', 'artifactType': 'stage_b_review'}, 'effects': 'read'}],
                   'conditionalActions': [{'operation': 'project.workflow.artifact-approve',
                       'arguments': {'chapterId': 'chapter-EXAMPLE', 'artifactHash': ['stage_b_review=' + 'a' * 64]},
                       'effects': 'approval', 'needsApproval': True, 'executable': False,
                       'when': 'after_author_review'}], 'automatic': False}}
    inventory = {'complete': False, 'status': 'unassessed', 'requests': [{'requestId': 'facts-1'}]}
    return {'ok': False, 'code': 'CLOSEOUT_PREPARED', 'chapterId': 'chapter-EXAMPLE',
            'guidance': {'blockerCount': 1, 'proseTarget': {'path': '正文/chapter-EXAMPLE.md',
                         'state': 'not_written', 'finalizeWritesFormalProse': True}},
            'prepareRef': 'gn:chapter-closeout:' + 'c' * 32, 'blockers': [blocker],
            'receipt': {'blockers': [deepcopy(blocker)], 'artifacts': artifacts(),
                        'bindingGeneration': 8, 'approvedRange': True},
            'factsAssessed': False, 'factInventory': inventory,
            'continuity': {'status': 'not_checked', 'factInventory': deepcopy(inventory)},
            'runAction': {'operation': 'workflow.closeout', 'arguments': {'prepareRef': 'original-ref'}},
            'approvalPerformed': False, 'userMaterial': '作者仍需阅读这段说明。'}


class ModelResponseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / 'book'
        self.cache = self.directory / 'transport'
        self.cache.mkdir(mode=0o700)
        for item in (patch.object(transport_cache, '_directory', return_value=self.cache),
                     patch.dict(os.environ, {'GOVEL_LOG_MODE': 'off'})):
            item.start(); self.addCleanup(item.stop)

    def full(self, projected):
        reference = projected['fullDetails']['arguments']['reference']
        return transport_cache.get(self.root, reference, kind='authoring-report')

    def test_receipt_deduplicates_only_exact_copies_and_keeps_recovery_conditions(self):
        original = receipt_sample(); untouched = deepcopy(original)
        model = project_response(self.root, 'workflow.closeout-prepare', original)
        self.assertEqual(original, untouched)
        self.assertEqual(model['blockers'], original['blockers'])
        self.assertEqual(model['guidance'], original['guidance'])
        self.assertEqual(model['runAction'], original['runAction'])
        self.assertEqual(model['prepareRef'], original['prepareRef'])
        self.assertFalse(model['factsAssessed'])
        self.assertEqual(model['continuity']['status'], 'not_checked')
        self.assertEqual(model['receipt']['blockersRef'], {'path': '/blockers', 'indexes': [0]})
        self.assertEqual(model['receipt']['bindingGeneration'], 8)
        self.assertEqual(model['receipt']['artifactIntegrity']['verifiedCount'], 12)
        self.assertEqual(self.full(model), original)
        self.assertLess(len(json.dumps(model)), len(json.dumps(original)))
        self.assertFalse(self.root.exists())

    def test_unknown_unapproved_and_warning_artifact_rows_remain_visible(self):
        rows = artifacts()
        uncertain = {**rows[0], 'artifactType': 'unknown', 'fresh': None}
        not_checked = {**rows[1], 'artifactType': 'not_checked', 'envelopeValid': 'not_checked'}
        unapproved = {**rows[2], 'artifactType': 'needs_author', 'approved': False}
        warning = {**rows[3], 'artifactType': 'warning', 'summary': {'passed': True, 'findingsCount': 1}}
        extension = {**rows[4], 'artifactType': 'future', 'futureAssessment': {'status': 'unknown'}}
        original = {'ok': True, 'code': 'CHAPTER_STATUS', 'artifacts': rows + [uncertain, not_checked, unapproved, warning, extension],
                    'warnings': [{'message': '这项意见仍需保留。'}], 'semanticReviewComplete': False}
        model = project_response(self.root, 'project.workflow.chapter-status', original)
        self.assertEqual(model['artifacts'], [uncertain, not_checked, unapproved, warning, extension])
        self.assertEqual(model['warnings'], original['warnings'])
        self.assertFalse(model['semanticReviewComplete'])
        self.assertEqual(model['artifactIntegrity']['verifiedCount'], len(rows))
        self.assertEqual(model['artifactIntegrity']['artifactTypes'], [row['artifactType'] for row in rows])
        self.assertNotIn('verifiedArtifactReferences', model)

    def test_unknown_operations_full_view_and_user_payloads_are_not_stripped(self):
        original = {'artifacts': artifacts(), 'userText': '原始材料不能按字段名概括。'}
        self.assertIs(project_response(self.root, 'source.query', original), original)
        self.assertIs(project_response(self.root, 'project.workflow.chapter-receipt', original, view='full'), original)
        with_payload = {'artifacts': [{**row, 'payload': {'text': '整段正文。'}} for row in artifacts()]}
        self.assertEqual(project_response(self.root, 'project.workflow.chapter-receipt', with_payload), with_payload)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_beat_counts_only_collapse_proven_exact_counts_and_keep_quotes(self):
        exact = [{'id': f'B{index}', 'budget': 100, 'hanCount': 100, 'delta': 0, 'status': 'measured'}
                 for index in range(20)]
        unknown = {'id': 'B-UNKNOWN', 'budget': 100, 'hanCount': None, 'delta': None, 'status': 'unmeasured'}
        unequal = {'id': 'B-DIFFERENT', 'budget': 100, 'hanCount': 95, 'delta': -5, 'status': 'measured'}
        original = {'ok': True, 'passed': True, 'semanticReviewComplete': False,
                    'beatCounts': [*exact, unknown, unequal], 'findings': [{'severity': 'warning', 'message': '保留警告'}],
                    'quotedTargets': [{'text': '必须原句保留。', 'status': 'literal_found'}],
                    'requirements': [{'text': '用户的语义要求。', 'status': 'needs_semantic_review'}],
                    'coverage': {'semanticChecks': 'not_evaluated'}}
        model = project_response(self.root, 'diagnostics.chapter-check', original)
        self.assertEqual(model['beatCounts'], [unknown, unequal])
        self.assertEqual(model['exactBudgetBeats'], {'count': 20, 'ids': [row['id'] for row in exact]})
        for field in ('findings', 'quotedTargets', 'requirements', 'coverage', 'semanticReviewComplete'):
            self.assertEqual(model[field], original[field])

    def test_repeated_diagnostic_payload_is_referenced_without_losing_any_findings(self):
        evidence = {'passed': True, 'findings': [{'severity': 'warning', 'evidence': '引文不删。' * 60},
                                                {'severity': 'unknown', 'message': '尚未检查'}],
                    'voiceCoverage': {'status': 'not_checked'}}
        original = {'ok': True, 'code': 'DIAGNOSTICS_EVIDENCE', 'evidence': evidence,
                    'artifact': {'artifactType': 'diagnostics', 'artifactSha256': 'f' * 64, 'payload': deepcopy(evidence)}}
        model = project_response(self.root, 'diagnostics.aggregate', original)
        self.assertEqual(model['findings'], evidence['findings'])
        self.assertEqual(model['voiceCoverage'], evidence['voiceCoverage'])
        self.assertTrue(model['passed'])
        self.assertEqual(model['artifact']['artifactSha256'], 'f' * 64)
        self.assertEqual(model['artifact']['payloadRef'], {'detailsAction': '/fullDetails', 'path': '/artifact/payload'})
        self.assertEqual(self.full(model), original)

    def test_duplicate_error_message_keeps_all_blockers_and_exact_error_code(self):
        message = '这个实际错误说明需要保留，但不重复交付。' * 30
        blockers = [{'code': 'EXPECTED_BLOCK', 'message': message, 'details': {'chapterId': 'chapter-EXAMPLE'}},
                    {'code': 'ANOTHER_BLOCK', 'message': '另一处阻断。', 'details': {'artifactPath': '工作台/B.json'}}]
        original = {'ok': False, 'code': 'EXPECTED_BLOCK', 'error': message, 'details': {'blockers': blockers}}
        model = project_response(self.root, 'workflow.closeout', original)
        self.assertEqual(model['code'], original['code'])
        self.assertEqual(model['details']['blockers'], blockers)
        self.assertEqual(model['errorLocation'], '/details/blockers/0')
        self.assertNotIn('error', model)
        self.assertEqual(self.full(model), original)

    def test_snapshot_failure_keeps_completed_business_response(self):
        original = receipt_sample()
        with patch.object(transport_cache, 'put', side_effect=OSError('cache unavailable')):
            self.assertIs(project_response(self.root, 'workflow.closeout', original), original)

    def test_text_failure_preserves_completed_write_without_reexecution(self):
        completed = {'ok': True, 'code': 'CHAPTER_CLOSED', 'requestId': 'completed-request',
                     'chapterId': 'chapter-EXAMPLE', 'bindingGeneration': 8}
        with patch.object(mcp_server, 'call_tool', return_value=completed) as execute, \
                patch.object(mcp_server, 'render_model_text', side_effect=ValueError('presentation failed')):
            response = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
                'name': 'closeout_chapter', 'arguments': {'projectRoot': str(self.root),
                                                       'requestId': 'completed-request'}}})['result']
        self.assertEqual(execute.call_count, 1)
        self.assertFalse(response['isError'])
        self.assertEqual(response['structuredContent'], completed)
        self.assertEqual(json.loads(response['content'][0]['text']), completed)

    def test_real_mcp_preparation_reduces_packet_and_keeps_executable_read_parameters(self):
        from tests.fixtures_workflow import review_ready_project
        chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        packet = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
            'name': 'prepare_task', 'arguments': {'projectRoot': str(self.root), 'chapterId': chapter,
                                                'task': 'editor', 'view': 'model'}}})['result']
        self.assertFalse(packet['isError'])
        report = packet['structuredContent']
        text = packet['content'][0]['text']
        self.assertIn('调用成功。', text)
        self.assertIn(report['code'], text)
        original = deepcopy(packet)
        original['content'][0]['text'] = json.dumps(report, ensure_ascii=False, separators=(',', ':'))
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        self.assertLess(len(encode(packet)), len(encode(original)))
        read_id = report['contractReads'][0]['readIds'][0]
        self.assertIn(read_id, text)
        self.assertIn(report['reading']['credential'], text)
        page = call_operation('task.read-batch', self.root, {
            'credential': report['reading']['credential'], 'readIds': [read_id]})
        self.assertTrue(page['ok'], page)
        self.assertTrue(page['items'])

    def test_explicit_full_and_station_errors_remain_json_in_direct_and_discovered_calls(self):
        error = StructureError('EXPECTED_BLOCK', '当前输入仍需处理', {'missing': ['current approval']})
        for tool, arguments in (
            ('closeout_chapter', {'requestId': 'request-1', 'responseView': 'full'}),
            ('prepare_task', {'task': 'editor', 'view': 'station'}),
            ('get_tool_help', {'operation': 'task.prepare', 'view': 'full'}),
            ('read_discovered_operation', {'operation': 'capabilities.help',
                                           'arguments': {'operation': 'task.prepare', 'view': 'full'}}),
        ):
            with self.subTest(tool=tool), patch.object(mcp_server, 'call_tool', side_effect=error):
                response = mcp_server.handle({'id': 1, 'method': 'tools/call', 'params': {
                    'name': tool, 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
                self.assertTrue(response['isError'])
                self.assertEqual(json.loads(response['content'][0]['text']), response['structuredContent'])
                self.assertEqual(response['structuredContent']['code'], 'EXPECTED_BLOCK')

    def test_same_call_details_are_pageable_project_scoped_and_never_rebound(self):
        original = receipt_sample()
        model = project_response(self.root, 'workflow.closeout-prepare', original)
        reference = model['fullDetails']['arguments']['reference']
        text, cursor, pages = '', None, []
        while True:
            page = transport_cache.read_snapshot(self.root, reference=reference, maxBytes=1300, cursor=cursor)
            pages.append(page)
            text += ''.join(row['text'] for row in page['materials'])
            if not page['hasMore']:break
            cursor = page['nextCursor']
        self.assertGreater(len(pages), 1)
        self.assertEqual(json.loads(text), original)
        self.assertTrue(pages[-1]['coverage']['deliveryComplete'])
        self.assertFalse(pages[-1]['freshnessRevalidated'])
        with self.assertRaises(StructureError) as mismatch:
            transport_cache.read_snapshot(self.root, reference=reference, maxBytes=1400, cursor=pages[0]['nextCursor'])
        self.assertEqual(mismatch.exception.code, 'CURSOR_QUERY_MISMATCH')
        with self.assertRaises(StructureError):
            transport_cache.read_snapshot(self.directory / 'other-book', reference=reference)
        (self.cache / (reference.replace(':', '-') + '.json')).unlink()
        with self.assertRaises(StructureError) as expired:
            transport_cache.read_snapshot(self.root, reference=reference)
        self.assertEqual(expired.exception.code, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')

    def test_review_form_snapshot_exposes_only_public_report(self):
        report = {'payload': {'semanticJudgment': '待工位填写'}, 'approved': False}
        reference = transport_cache.put(self.root, 'review-form', {'report': report,
            'dependencies': {'privateGuard': 'NOT-FOR-MODEL'}, 'projectId': 'PROJECT-EXAMPLE'})
        page = transport_cache.read_snapshot(self.root, reference=reference)
        self.assertEqual(json.loads(page['materials'][0]['text']), report)
        self.assertNotIn('NOT-FOR-MODEL', json.dumps(page))

    def test_task_prepare_pending_approval_is_delivered_once(self):
        pending = [{'operation': 'needs-author', 'arguments': {'operationId': 'OP-EXAMPLE'}, 'requiresExplicitApproval': True}]
        original = {'ok': True, 'code': 'TASK_PREPARED', 'task': 'editor', 'readiness': {'taskReady': False},
                    'toolPlan': {'pendingApprovals': pending, 'toolActions': [], 'unresolved': ['unknown']}}
        model = task_model_response(original)
        self.assertEqual(model['authorization']['pendingApprovals'], pending)
        self.assertNotIn('pendingApprovals', model['toolPlan'])
        self.assertEqual(original['toolPlan']['pendingApprovals'], pending)
        self.assertEqual(model['toolPlan']['unresolved'], ['unknown'])

    def test_cli_and_mcp_apply_same_projection_and_full_does_not_repeat_a_write(self):
        original = receipt_sample(); original['code'] = 'CLOSEOUT_INPUT_REQUIRED'
        for view in ('model', 'full'):
            with self.subTest(view=view):
                output = io.StringIO()
                with patch.object(novelctl, '_dispatch', return_value=deepcopy(original)) as execute, redirect_stdout(output):
                    code = novelctl.main(['project', 'workflow', 'closeout', '--project-root', str(self.root),
                                         '--request-id', 'same-request', '--response-view', view])
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(code, 1)
                request = {'id': 1, 'method': 'tools/call', 'params': {'name': 'closeout_chapter', 'arguments': {
                    'projectRoot': str(self.root), 'requestId': 'same-request', 'responseView': view}}}
                with patch.object(mcp_server, 'call_tool', return_value=deepcopy(original)) as execute:
                    response = mcp_server.handle(request)['result']
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(json.loads(output.getvalue()), response['structuredContent'])
                text = response['content'][0]['text']
                if view == 'full':
                    self.assertEqual(json.loads(text), original)
                    self.assertEqual(response['structuredContent'], original)
                else:
                    self.assertIn('fullDetails', response['structuredContent'])
                    self.assertIn(original['code'], text)
                    self.assertIn(original['blockers'][0]['message'], text)
                    self.assertIn('original-ref', text)

    def test_public_dispatchers_strip_display_option_but_keep_unknown_operations_strict(self):
        from tests.fixtures_current import create_current_project
        fixture = create_current_project(self.root, chapter_count=1)
        chapter = fixture['structure']['chapters'][0]
        arguments = {'chapterId': chapter['chapterId'], 'input': chapter['filePath'], 'responseView': 'model'}
        original = deepcopy(arguments)
        result = call_operation('diagnostics.chapter-check', self.root, arguments)
        self.assertIn('findings', result)
        self.assertEqual(arguments, original)
        output = io.StringIO()
        with redirect_stdout(output):
            code = novelctl.main(['project', 'diagnostics', 'chapter-check', '--project-root', str(self.root),
                                 '--chapter-id', chapter['chapterId'], '--input', chapter['filePath'], '--response-view', 'full'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        with self.assertRaises(StructureError) as unknown:
            call_operation('source.query', self.root, {'responseView': 'model'})
        self.assertEqual(unknown.exception.code, 'ARGUMENTS_INVALID')

    def test_logger_keeps_original_business_result_and_actual_model_response(self):
        original = receipt_sample()
        output = io.StringIO()
        with patch.dict(os.environ, {'GOVEL_LOG_MODE': 'full', 'GOVEL_LOG_DIR': str(self.directory / 'logs')}), \
                patch.object(novelctl, '_dispatch', return_value=original), redirect_stdout(output):
            novelctl.main(['project', 'workflow', 'closeout-prepare', '--project-root', str(self.root),
                           '--chapter-id', 'chapter-EXAMPLE'])
            rows = call_logging.list_call_logs()
            detail = call_logging.show_call_log(rows['calls'][0]['callId'])
        self.assertEqual(detail['finish']['payloads']['businessResult']['value'], original)
        self.assertEqual(detail['finish']['payloads']['response']['value'], json.loads(output.getvalue()))
        self.assertNotEqual(detail['finish']['payloads']['businessResult']['value'],
                            detail['finish']['payloads']['response']['value'])

    def test_new_and_legacy_error_paths_keep_codes_and_full_access(self):
        message = '多个位置重复引用的错误说明。' * 50
        blockers = [{'code': 'EXPECTED_BLOCK', 'message': message, 'details': {'chapterId': 'chapter-EXAMPLE'}}]
        error = StructureError('EXPECTED_BLOCK', message, {'blockers': blockers})
        for command, tool, arguments in (
            (['workflow', 'closeout', '--request-id', 'request-1'], 'closeout_chapter', {'requestId': 'request-1'}),
            (['workflow', 'chapter-receipt', '--chapter-id', 'chapter-EXAMPLE'], 'workflow_chapter_receipt', {'chapterId': 'chapter-EXAMPLE'}),
        ):
            with self.subTest(tool=tool):
                output = io.StringIO()
                with patch.object(novelctl, '_dispatch', side_effect=error), redirect_stdout(output):
                    code = novelctl.main(['project', *command, '--project-root', str(self.root)])
                self.assertEqual(code, 1)
                shown = json.loads(output.getvalue())
                self.assertEqual(shown['code'], 'EXPECTED_BLOCK')
                self.assertEqual(shown['details']['blockers'], blockers)
                with patch.object(mcp_server, 'call_tool', side_effect=error):
                    packet = mcp_server.handle({'id': 3, 'method': 'tools/call', 'params': {
                        'name': tool, 'arguments': {'projectRoot': str(self.root), **arguments}}})['result']
                self.assertTrue(packet['isError'])
                self.assertEqual(packet['structuredContent'], shown)

    def test_invalid_view_in_cli_bridge_stays_a_structured_business_error(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = novelctl.main(['project', 'tools', 'read', '--project-root', str(self.root),
                                 '--operation', 'diagnostics.chapter-check', '--arguments', '{"responseView":"invalid"}'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['code'], 'ARGUMENTS_INVALID')

    def test_unknown_nested_status_and_incomplete_diagnostic_coverage_are_not_counted_away(self):
        rows = artifacts()
        unknown_rows = [
            {**rows[0], 'required': None},
            {**rows[1], 'bindings': {'sourcePlanHash': {'status': 'unknown', 'reason': 'still required'}}},
            {**rows[2], 'summary': {'verdict': 'POLISH_READY', 'factAssessment': {'status': 'unassessed'}}},
            {**rows[3], 'summary': {'verdict': 'POLISH_READY', 'semanticReviewComplete': False}},
        ]
        original = {'artifacts': [*rows, *unknown_rows]}
        model = project_response(self.root, 'project.workflow.chapter-receipt', original)
        self.assertEqual(model['artifacts'], unknown_rows)
        self.assertFalse(model['artifactIntegrity']['semanticReviewImplied'])
        binding = {'status': 'unknown', 'current': None, 'requiredReview': '姓名认知尚未核实。'}
        evidence = {'passed': False, 'findings': [{'severity': 'unknown', 'message': '检查未完成。'}],
                    'planBinding': binding, 'checksPresent': ['validate_text', {'status': 'not_checked'}],
                    'chapterContract': {'outlineAvailable': False, 'lengthCoverage': 'unmeasured',
                        'pov': {'status': 'needs_semantic_review'}, 'semanticReviewComplete': False,
                        'coverage': {'lengthEvaluated': False, 'semanticChecks': 'not_evaluated'}},
                    'voiceCoverage': {'status': 'unknown', 'weakAttributions': ['待核对说话者']},
                    'lineDetails': {'blocks': {'UNKNOWN': None}, 'status': 'not_checked'}}
        result = {'ok': True, 'evidence': evidence, 'artifact': {'payload': deepcopy(evidence)}}
        model = project_response(self.root, 'diagnostics.aggregate', result)
        self.assertEqual(model['planBinding'], binding)
        self.assertEqual(model['checksPresent'], evidence['checksPresent'])
        self.assertNotIn('checksRunCount', model['summary'])
        self.assertEqual(model['chapterContract'], evidence['chapterContract'])
        self.assertEqual(model['voiceCoverage'], evidence['voiceCoverage'])
        self.assertEqual(model['lineDetails'], evidence['lineDetails'])
        self.assertFalse(model['passed'])

    def test_real_ready_receipt_counts_verified_rows_and_blocked_receipt_keeps_obligations(self):
        from tests.test_finalization_preflight import ready_project
        from scripts.core.jsonio import read_json, write_json
        chapter_id, _ = ready_project(self.root)
        arguments = {'chapterId': chapter_id, 'titleCandidateId': 'title-1', 'view': 'model'}
        ready = call_operation('project.workflow.chapter-receipt', self.root, arguments)
        self.assertTrue(ready['readyToFinalize'])
        self.assertEqual(ready['blockers'], [])
        model = project_response(self.root, 'project.workflow.chapter-receipt', ready)
        self.assertGreater(model['artifactIntegrity']['verifiedCount'], 0)
        self.assertLess(len(model['artifacts']), len(ready['artifacts']))
        self.assertNotIn('verifiedArtifactReferences', model)
        self.assertFalse(model['artifactIntegrity']['semanticReviewImplied'])
        self.assertEqual(model['bindingGeneration'], ready['bindingGeneration'])
        self.assertEqual(model['finalizeAction'], ready['finalizeAction'])
        self.assertEqual(self.full(model), ready)
        workflow = read_json(self.root / '.govel/workflow.json')
        workflow['approvals'][chapter_id]['approved'] = False
        write_json(self.root / '.govel/workflow.json', workflow)
        blocked = call_operation('project.workflow.chapter-receipt', self.root, arguments)
        shown = project_response(self.root, 'project.workflow.chapter-receipt', blocked)
        self.assertFalse(shown['readyToFinalize'])
        self.assertGreater(len(shown['blockers']), 0)
        self.assertEqual(shown['blockers'], blocked['blockers'])
        self.assertEqual(shown['guidance'], blocked['guidance'])

    def test_real_registered_aggregate_defaults_and_explicit_views_use_one_full_business_result(self):
        from tests.test_finalization_preflight import ready_project
        from scripts.core.jsonio import read_json, write_json
        chapter_id, _ = ready_project(self.root)
        for transport in ('cli', 'mcp'):
            for view in (None, 'model', 'full'):
                with self.subTest(transport=transport, view=view):
                    workflow_path = self.root / '.govel/workflow.json'
                    workflow = read_json(workflow_path)
                    workflow['artifacts'][chapter_id].pop('diagnostics', None)
                    write_json(workflow_path, workflow)
                    log_dir = self.directory / f'logs-{transport}-{view}'
                    with patch.dict(os.environ, {'GOVEL_LOG_MODE': 'full', 'GOVEL_LOG_DIR': str(log_dir)}):
                        if transport == 'cli':
                            arguments = ['project', 'diagnostics', 'run', '--project-root', str(self.root),
                                         '--chapter-id', chapter_id, '--mode', 'plot', '--intensity-level', '1', '--register']
                            if view is not None:arguments += ['--view', view]
                            output = io.StringIO()
                            with redirect_stdout(output):self.assertEqual(novelctl.main(arguments), 0)
                            displayed = json.loads(output.getvalue())
                        else:
                            arguments = {'projectRoot': str(self.root), 'chapterId': chapter_id,
                                         'mode': 'plot', 'intensityLevel': 1, 'register': True}
                            if view is not None:arguments['view'] = view
                            packet = mcp_server.handle({'id': 10, 'method': 'tools/call', 'params': {
                                'name': 'run_mechanical_diagnostics', 'arguments': arguments}})['result']
                            self.assertFalse(packet['isError'], packet)
                            displayed = packet['structuredContent']
                            if view == 'full':
                                self.assertEqual(json.loads(packet['content'][0]['text']), displayed)
                            else:
                                self.assertIn(displayed['code'], packet['content'][0]['text'])
                        rows = call_logging.list_call_logs()
                        detail = call_logging.show_call_log(rows['calls'][0]['callId'])
                    payloads = detail['finish']['payloads']
                    business = payloads['businessResult']['value']
                    self.assertEqual(business['code'], 'DIAGNOSTICS_REGISTERED')
                    self.assertTrue(business['registered'])
                    self.assertNotIn('artifact', business)
                    self.assertEqual(payloads['effectiveArguments']['value']['view'], 'full')
                    if view == 'full':
                        self.assertEqual(displayed, business)
                    else:
                        self.assertEqual(displayed['passed'], business['evidence']['passed'])
                        self.assertEqual(displayed['findings'], business['evidence']['findings'])
                        self.assertEqual(displayed['voiceCoverage'], business['evidence']['voiceCoverage'])
                        self.assertFalse(displayed['chapterContract']['semanticReviewComplete'])
                        self.assertEqual(self.full(displayed), business)

    def test_explicit_snapshot_view_is_rejected_for_other_reference_kinds(self):
        reference = transport_cache.put(self.root, 'authoring-report', {'ok': True})
        for view in ('model', 'full'):
            with self.subTest(view=view), self.assertRaises(StructureError) as caught:
                transport_cache.read_snapshot(self.root, reference=reference, view=view)
            self.assertEqual(caught.exception.code, 'TRANSPORT_VIEW_INVALID')

    def test_stable_fact_quotes_and_current_request_gaps_remain_visible_at_both_boundaries(self):
        from tests.test_fact_inventory_evidence import FactInventoryEvidenceTests
        from scripts.core.evidence_quotes import quote_span
        from scripts.core.project import query_policy
        from scripts.core.query import _size
        case = FactInventoryEvidenceTests('test_model_receipt_exposes_all_quotes_and_explains_an_incomplete_request')
        self.addCleanup(case.doCleanups)
        case.setUp()
        expected_quotes = [quote_span(case.body, quote) for quote in case.quotes]
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                if incomplete:
                    case.preview(expected_quotes[:1])
                original = case.receipt()
                self.assertEqual(original['factInventory']['items'][0]['requiredEvidence'], expected_quotes)
                before = {name: (case.root / name).read_bytes()
                          for name in ('.govel/structure.json', '.govel/workflow.json')}
                output = io.StringIO()
                with redirect_stdout(output):
                    code = novelctl.main(['project', 'workflow', 'chapter-receipt', '--project-root', str(case.root),
                                         '--chapter-id', 'CH001', '--view', 'model'])
                self.assertEqual(code, 0)
                packet = mcp_server.handle({'id': 8, 'method': 'tools/call', 'params': {
                    'name': 'workflow_chapter_receipt', 'arguments': {
                        'projectRoot': str(case.root), 'chapterId': 'CH001', 'view': 'model'}}})['result']
                self.assertFalse(packet['isError'])
                responses = [json.loads(output.getvalue()), packet['structuredContent']]
                self.assertIn(packet['structuredContent']['code'], packet['content'][0]['text'])
                for shown in responses:
                    self.assertLessEqual(_size(shown), query_policy()['maxBytes']['detail'])
                    self.assertEqual(shown['factInventory'], original['factInventory'])
                    self.assertEqual(shown['nextAction'], original['nextAction'])
                    self.assertEqual(shown['blockers'], original['blockers'])
                    self.assertFalse(shown['readyForContinuation'])
                    self.assertFalse(shown['finalization']['applicable'])
                    history = shown['finalization']
                    self.assertNotIn('blockers', history)
                    archived = transport_cache.get(case.root, history['detailsAction']['arguments']['reference'],
                                                   kind='authoring-report')
                    self.assertEqual(len(archived['finalization']['blockers']), history['historicalBlockerCount'])
                    item = shown['factInventory']['items'][0]
                    self.assertEqual(item['requiredEvidenceCount'], 2)
                    self.assertEqual(item['requiredEvidence'], expected_quotes)
                    if incomplete:
                        self.assertEqual(item['requestStatus'], 'stale')
                        self.assertEqual(item['requestEvidence']['missingRequiredIndexes'], [1])
                        self.assertEqual(item['requestEvidence']['matchedEvidenceCount'], 1)
                        self.assertEqual(item['nextAction']['operation'], 'workflow.review-form')
                self.assertEqual(before, {name: (case.root / name).read_bytes() for name in before})


if __name__ == '__main__':
    unittest.main()
