"""Regressions for the author's source-maintenance workflow, across actual transports."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.agents_consistency import check_agents_decisions
from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT, query_policy
from scripts.core.record_patch import apply_record_patch
from scripts.core.source_model import snapshot_hash
from scripts.core.source_review import fingerprint
from scripts.core.source_reports import query_change_report
from scripts.core.source_requests import query_request_status, renew_request
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from scripts.core.structure_transaction import undo
from scripts.core.tool_catalog import list_capabilities
from scripts.mcp_server import handle
from tests.fixtures_source import source_fixture


def text_patch(original, replacement):
    return {'op': 'text', 'path': ['text'], 'original': original, 'replacement': replacement}


class SourceMaintenanceFeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = source_fixture()
        self.source['fragments'].append({**self.source['fragments'][3], 'fragmentId': 'SF-PLAN',
                                        'contentRole': 'plan', 'text': '计划：独自守门，第二日回家。'})
        write_json(self.root / '.gnovel/structure.json', self.source)

    def preview(self, items=None, request_id='feedback-1'):
        return preview_proposal_batch(self.root, sourceHash=snapshot_hash(self.source), requestId=request_id,
                                      approvalActor='author', approvalEvidence='approved maintenance',
                                      items=items or self.items())

    def items(self):
        return [{'kind': 'fact', 'recordId': 'SF-LIFE', 'reason': 'word choice',
                 'patch': [text_patch('独自', '单独'), text_patch('城门', '大门')],
                 'evidence': [{'fragmentId': 'SF-LIFE', 'quote': '他独自守过城门。'}]},
                {'kind': 'planning', 'recordId': 'SF-PLAN', 'reason': 'same revision',
                 'patch': [text_patch('独自', '单独'), text_patch('第二日', '第三日')], 'evidence': []}]

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_query_logic_stdio_with_project_fields_and_rule_records(self):
        self.source['fieldDefinitions'].append({'fieldId': 'FD-PROJECT', 'key': 'project.plan', 'label': '总规划',
                                               'valueType': 'topic', 'appliesTo': ['project']})
        self.source['entities'].append({'entityId': 'RU-GATE', 'entityType': 'rule', 'name': '门禁规则'})
        write_json(self.root / '.gnovel/structure.json', self.source)
        before = self.inventory()
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'query_logic', 'arguments': {'projectRoot': str(self.root), 'view': 'list'}}}
        result = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/mcp_server.py')],
                                input=json.dumps(request) + '\n', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual(response['structuredContent']['items'][0]['id'], 'RU-GATE')
        self.assertEqual(self.inventory(), before)

    def test_budget_error_has_registry_bounds_in_cli_and_mcp(self):
        plan = self.preview()
        for view in ('records', 'text', 'markdown'):
            args = {'projectRoot': str(self.root), 'operationId': plan['operationId'], 'maxBytes': 60000,
                    'section': 'diff', 'view': view}
            result = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'query_source_change_report', 'arguments': args}})['result']
            self.assertTrue(result['isError'])
            error = result['structuredContent']
            self.assertEqual(error['code'], 'QUERY_BUDGET_INVALID')
            self.assertEqual(error['details']['bounds']['maxBytes'], {
                'minimum': 1, 'maximum': query_policy()['maxBytes']['detail'], 'received': 60000})
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source',
                              'change-report', '--project-root', str(self.root), '--operation-id', plan['operationId'],
                              '--max-bytes', '60000'], text=True, capture_output=True)
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn(str(query_policy()['maxBytes']['detail']), cli.stdout + cli.stderr)

    def test_preview_next_action_is_callable_once_for_facts_and_plans(self):
        plan = self.preview()
        found = list_capabilities(query='提交')['operations'][0]
        self.assertEqual(found['id'], 'source.commit')
        next_action = plan['nextAction']
        self.assertEqual(next_action['tool'], 'commit_source_change')
        self.assertEqual(next_action['arguments']['projectRoot'], str(self.root))
        before = self.inventory()
        args = {**next_action['arguments'], 'approve': False}
        with self.assertRaises(StructureError):
            call_operation(next_action['operation'], self.root, args)
        self.assertEqual(self.inventory(), before)
        result = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': next_action['tool'], 'arguments': next_action['arguments']}})['result']
        self.assertFalse(result['isError'], result)
        source = load_structure(self.root)
        self.assertEqual(source['revision'], self.source['revision'] + 1)
        self.assertEqual(source['fragments'][3]['text'], '他单独守过大门。')
        self.assertEqual(source['fragments'][-1]['text'], '计划：单独守门，第三日回家。')
        self.assertEqual(len(list((self.root / '.gnovel/operations').glob('*.json'))), 1)
        undo(self.root, plan['operationId'], actor='test')
        self.assertEqual(load_structure(self.root)['fragments'], self.source['fragments'])

    def test_frozen_diff_contains_local_edits_owner_names_and_survives_commit(self):
        plan = self.preview()
        diff = query_change_report(self.root, operationId=plan['operationId'], section='diff')['items']
        self.assertEqual(len(diff), 4)
        self.assertEqual({r['ownerLabel'] for r in diff}, {'人物甲'})
        self.assertEqual({r['contentRole'] for r in diff}, {'fact', 'plan'})
        self.assertTrue(all(len(r['before']) < len(self.source['fragments'][3]['text']) for r in diff))
        cursor, parts = None, []
        while True:
            page = query_change_report(self.root, operationId=plan['operationId'], section='diff', view='markdown',
                                       maxBytes=2000, limit=1, cursor=cursor)
            parts.extend(row['text'] for row in page['items'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        text = ''.join(parts)
        self.assertIn('人物甲', text)
        self.assertIn('修改前', text)
        self.assertIn('修改后', text)
        call_operation('source.commit', self.root, plan['nextAction']['arguments'])
        self.assertEqual(query_change_report(self.root, operationId=plan['operationId'], section='diff')['items'], diff)

    def test_mixed_batch_rejects_fact_promotion_and_rolls_back_mid_commit(self):
        items = self.items()
        items[-1]['patch'].append({'op': 'set', 'path': ['contentRole'], 'value': 'fact'})
        with self.assertRaises(StructureError) as error:
            self.preview(items)
        self.assertEqual(error.exception.code, 'FACT_PROMOTION_REVIEW_REQUIRED')
        self.assertEqual(error.exception.details['itemIndex'], 1)
        self.assertFalse(list((self.root / '.gnovel/plans').glob('OP-*.json')))
        plan = self.preview()
        original = (self.root / '.gnovel/structure.json').read_bytes()
        def fail(stage, *args):
            if stage == 'write' and args == ('.gnovel/structure.json',):
                raise RuntimeError('simulated interruption')
        with patch('scripts.core.write_sets.checkpoint', side_effect=fail), self.assertRaises(RuntimeError):
            call_operation('source.commit', self.root, plan['nextAction']['arguments'])
        self.assertEqual((self.root / '.gnovel/structure.json').read_bytes(), original)
        self.assertFalse((self.root / f'.gnovel/operations/{plan["operationId"]}.json').exists())

    def test_evidence_error_identifies_quote_role_and_reason(self):
        for evidence, reason in (({'fragmentId': 'SF-PLAN', 'quote': '独自'}, 'non_fact_fragment'),
                                 ({'fragmentId': 'SF-LIFE', 'quote': '不存在'}, 'quote_not_found'),
                                 ({'fragmentId': 'SF-MISSING', 'quote': '独自'}, 'fragment_not_found')):
            items = self.items()[:1]
            items[0]['evidence'].append(evidence)
            with self.assertRaises(StructureError) as error:
                self.preview(items)
            self.assertEqual(error.exception.code, 'CONTENT_EVIDENCE_INVALID')
            details = error.exception.details
            self.assertEqual((details['itemIndex'], details['evidenceIndex']), (0, 1))
            self.assertEqual(details['quote'], evidence['quote'])
            self.assertEqual(details['fragmentId'], evidence['fragmentId'])
            self.assertEqual(details['reason'], reason)

    def test_renew_refuses_changed_staged_bytes_without_writes(self):
        plan = self.preview()
        files = self.root / f'.gnovel/plans/{plan["operationId"]}.files'
        target = files / '.gnovel/structure.json'
        target.write_bytes(target.read_bytes() + b'\n')
        before = self.inventory()
        with patch('scripts.core.source_requests.time.time', return_value=plan['expiresAtEpoch'] + 1):
            status = query_request_status(self.root, requestId='feedback-1')
            self.assertFalse(status['renewable'])
            self.assertIn('PLAN_HASH_MISMATCH', status['staleReasons'])
            with self.assertRaises(StructureError):
                renew_request(self.root, requestId='feedback-1')
        self.assertEqual(self.inventory(), before)

    def test_source_scan_word_lists_role_status_read_only_and_cursor_binding(self):
        self.source['fragments'].append({**self.source['fragments'][3], 'fragmentId': 'SF-OLD', 'status': 'archived'})
        write_json(self.root / '.gnovel/structure.json', self.source)
        words = self.root / '工作台/禁用词.json'
        write_json(words, ['独自'])
        before = self.inventory()
        args = {'scope': 'source', 'termsPath': '工作台/禁用词.json',
                'selection': {'includeArchived': True}, 'limit': 100}
        result = call_operation('source.scan-report', self.root, args)
        self.assertEqual(self.inventory(), before)
        hits = {row['fragmentId']: row for row in result['items']}
        self.assertTrue(hits['SF-LIFE']['factEvidenceEligible'])
        self.assertEqual(hits['SF-PLAN']['evidenceIneligibilityReason'], 'non_fact_fragment')
        self.assertEqual(hits['SF-OLD']['status'], 'archived')
        self.assertFalse(hits['SF-OLD']['factEvidenceEligible'])
        args['limit'] = 1
        page = call_operation('source.scan-report', self.root, args)
        self.assertTrue(page['hasMore'])
        write_json(words, ['独自', '城门'])
        with self.assertRaises(StructureError) as error:
            call_operation('source.scan-report', self.root, {**args, 'cursor': page['nextCursor']})
        self.assertEqual(error.exception.code, 'CURSOR_STALE')
        (self.root / '工作台/方案.md').write_text('说明\n禁用：「独自」「城门」\n其他「无关词」\n')
        quoted = call_operation('source.scan-report', self.root, {
            'scope': 'source', 'termsPath': '工作台/方案.md', 'termsFormat': 'quoted', 'startLine': 2, 'endLine': 2})
        self.assertEqual(quoted['query']['terms'], ['城门', '独自'])
        self.assertEqual(quoted['snapshot']['termSource']['termCount'], 2)


class RecordPatchRangeTests(unittest.TestCase):
    def test_replacements_bind_original_and_do_not_cascade(self):
        record = {'text': '甲走到乙身边。'}
        _, result = apply_record_patch(record, [text_patch('甲', '乙'), text_patch('乙', '丙')], fingerprint(record))
        self.assertEqual(result['text'], '乙走到丙身边。')
        self.assertEqual(record['text'], '甲走到乙身边。')

    def test_true_overlap_duplicate_anchors_and_parent_overlap_rejected(self):
        for record, patches, code in (
            ({'text': 'abcdef'}, [text_patch('abc', 'x'), text_patch('bc', 'y')], 'RECORD_PATCH_OVERLAP'),
            ({'text': '甲甲'}, [text_patch('甲', '乙')], 'RECORD_PATCH_ANCHOR_INVALID'),
            ({'text': '甲'}, [text_patch('甲', '乙'), {'op': 'set', 'path': ['text'], 'value': '丙'}], 'RECORD_PATCH_OVERLAP'),
        ):
            with self.subTest(code=code), self.assertRaises(StructureError) as error:
                apply_record_patch(record, patches, fingerprint(record))
            self.assertEqual(error.exception.code, code)


class AgentsDecisionConsistencyTests(unittest.TestCase):
    def test_doctor_exposes_superseded_draft_unknown_and_stale_citations(self):
        from scripts.core.project_doctor import _planning_domain
        from scripts.core.author_decisions import _scope_hashes, SCOPE_SNAPSHOT_BASIS
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = source_fixture()
            base = {'statement': '作者决定', 'rationale': 'test', 'scopeIds': [], 'dependsOn': []}
            active = {**base, 'decisionId': 'DR-ACTIVE', 'status': 'active', 'scopeSnapshot': {},
                      'scopeSnapshotBasis': SCOPE_SNAPSHOT_BASIS,
                      'approval': {'actorId': 'author', 'evidence': 'approved'}}
            source['authorDecisions'] = [active,
                {**base, 'decisionId': 'DR-OLD', 'status': 'superseded'},
                {**active, 'decisionId': 'DR-NEW', 'supersedes': 'DR-OLD'},
                {**base, 'decisionId': 'DR-DRAFT', 'status': 'draft'},
                {**active, 'decisionId': 'DR-STALE', 'scopeIds': ['C-ALPHA'],
                 'scopeSnapshot': _scope_hashes(source, ['C-ALPHA'])}]
            source['entities'][0]['name'] = '新姓名'
            validate_structure(source)
            (root / 'AGENTS.md').write_text('DR-OLD\nDR-ACTIVE DR-DRAFT DR-MISSING\nDR-STALE\nDR-OLD-SUFFIX')
            result = check_agents_decisions(root, source)
            self.assertEqual({r['decisionId'] for r in result['issues']}, {'DR-OLD', 'DR-DRAFT', 'DR-MISSING', 'DR-STALE', 'DR-OLD-SUFFIX'})
            self.assertEqual(result['issues'][0]['supersededBy'], ['DR-NEW'])
            _, issues, objects = _planning_domain(root, source)
            self.assertEqual(len(issues), len(result['issues']))
            self.assertFalse(objects['agentsConsistency']['coverage']['semanticConsistencyChecked'])
