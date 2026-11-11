"""Large transaction evidence remains readable through the advertised report route."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.source_model import snapshot_hash
from scripts.core.source_proposal_batch import preview_proposal_batch
from scripts.core.source_reports import public_report, query_change_report
from scripts.core.structure_commands import _plan_hash
from scripts.core.structure_store import StructureError
from tests.fixtures_source import source_fixture


class SourceReportTextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source = source_fixture(self.root)
        self.value = '人物说明🙂\r\n保持原文。' * 1800
        self.preview = preview_proposal_batch(self.root, sourceHash=snapshot_hash(source), items=[{
            'kind': 'entity', 'recordId': 'character-ALPHA', 'patch': [{'op': 'set', 'path': ['summary'], 'value': self.value}],
            'reason': 'long explicit source change', 'evidence': [{'fragmentId': 'fact-LIFE', 'quote': '他独自守过城门。'}],
        }], requestId='large-report', approvalActor='author', approvalEvidence='explicit')

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def read(self, **options):
        return query_change_report(self.root, operationId=self.preview['operationId'], section='rationale', **options)

    def test_large_proposal_uses_advertised_text_arguments_and_preserves_all_data(self):
        self.assertTrue(self.preview['responseCompacted'])
        with self.assertRaises(StructureError) as caught:
            self.read()
        self.assertEqual(caught.exception.code, 'FIELD_TOO_LARGE')
        before = self.inventory()
        arguments = {**self.preview['details']['arguments'], 'section': 'rationale', 'maxBytes': 3000}
        chunks, cursor, pages = [], None, 0
        while True:
            result = call_operation('source.change-report', self.root, {**arguments, **({'cursor': cursor} if cursor else {})})
            pages += 1
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()), 3000)
            for row in result['items']:
                self.assertEqual(row['start'], sum(len(piece) for piece in chunks))
                chunks.append(row['text'])
            if not result['hasMore']:
                break
            cursor = result['nextCursor']
        proposal = json.loads(''.join(chunks))
        self.assertEqual(proposal['proposal']['newValue'], self.value)
        self.assertEqual(proposal['operation'], 'canon_change_proposal')
        self.assertTrue(result['historical'])
        self.assertFalse(result['coverage']['semanticReviewComplete'])
        self.assertGreater(pages, 1)
        self.assertEqual(before, self.inventory())

    def test_cursor_does_not_switch_view_budget_or_report_version(self):
        first = self.read(view='text', maxBytes=3000)
        for overrides, code in (({'view': 'records', 'maxBytes': 3000}, 'CURSOR_QUERY_MISMATCH'),
                                ({'view': 'text', 'maxBytes': 4000}, 'CURSOR_QUERY_MISMATCH')):
            with self.subTest(overrides=overrides), self.assertRaises(StructureError) as caught:
                self.read(cursor=first['nextCursor'], **overrides)
            self.assertEqual(caught.exception.code, code)
        path = self.root / '.govel/plans' / (self.preview['operationId'] + '.json')
        plan = read_json(path)
        plan['parameters']['proposals'][0]['proposal']['reason'] += ' changed'
        plan['planHash'] = _plan_hash({key: value for key, value in plan.items() if key != 'planHash'})
        write_json(path, plan)
        with self.assertRaises(StructureError) as caught:
            self.read(view='text', maxBytes=3000, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_default_record_view_stays_structured_for_small_sections(self):
        implicit = query_change_report(self.root, operationId=self.preview['operationId'], section='identities')
        explicit = query_change_report(self.root, operationId=self.preview['operationId'], section='identities', view='records')
        self.assertEqual(implicit, explicit)
        self.assertNotIn('view', implicit)
        for options in ({'view': []}, {'view': 'wrong'}, {'view': 'text', 'maxBytes': 0}, {'view': 'text', 'limit': True}):
            with self.subTest(options=options), self.assertRaises(StructureError):
                self.read(**options)

    def test_cli_mcp_and_direct_text_views_match(self):
        arguments = {'projectRoot': str(self.root), 'operationId': self.preview['operationId'],
                     'section': 'rationale', 'view': 'text', 'maxBytes': 3000}
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source', 'change-report',
                              '--project-root', str(self.root), '--operation-id', self.preview['operationId'],
                              '--section', 'rationale', '--view', 'text', '--max-bytes', '3000'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        from scripts.mcp_server import handle
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'query_source_change_report', 'arguments': arguments}})['result']
        self.assertFalse(response['isError'])
        self.assertEqual(json.loads(cli.stdout), response['structuredContent'])
        self.assertEqual(response['structuredContent'], self.read(view='text', maxBytes=3000))

    def test_compact_undo_details_keep_the_selected_undo_revision(self):
        result = public_report({'operationId': 'OP-TEST', 'revision': 7, 'undoReceiptPath': 'receipt.json',
                                'impact': {'changedIds': ['fact-LONG-' + str(index) for index in range(2000)]}})
        self.assertTrue(result['responseCompacted'])
        self.assertEqual(result['details']['arguments'], {'operationId': 'OP-TEST', 'view': 'text', 'undoRevision': 7})

    def test_success_projection_preserves_hash_and_privileged_preview_stays_nonexecutable(self):
        for large in (False, True):
            report = {'operationId': 'OP-FROZEN', 'operation': 'accept_external_modification',
                      'planHash': 'f' * 64, 'requiresApproval': True,
                      'bindingTransitions': [{'kind': 'accept_external'}],
                      'impact': {'changedIds': ['fact-' + str(index) for index in range(3000)] if large else []}}
            original = json.loads(json.dumps(report))
            result = public_report(report, root=self.root)
            self.assertEqual(report, original)
            self.assertEqual(result['planHash'], original['planHash'])
            self.assertIs(result['ok'], True)
            self.assertEqual(result['code'], 'SOURCE_CHANGE_PREVIEW')
            self.assertIs(result['execution']['executable'], False)
            self.assertNotIn('nextAction', result)
            if large: self.assertIs(result['responseCompacted'], True)
        error = public_report({'ok': False, 'code': 'SOURCE_INVALID', 'error': 'broken'})
        self.assertIs(error['ok'], False)
        self.assertEqual(error['code'], 'SOURCE_INVALID')


if __name__ == '__main__':
    unittest.main()
