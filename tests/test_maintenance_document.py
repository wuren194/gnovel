"""Document syntax is transport; only explicit selections become checklist items."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.project import TOOLBOX_ROOT
from scripts.core.source_changes import preview_source_change
from scripts.core.source_maintenance import check_maintenance
from scripts.core.source_maintenance_document import split_document, write_checklist
from scripts.core.source_maintenance_prepare import prepare_maintenance
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.structure_transaction import commit, undo
from tests.fixtures_source import source_fixture


class MaintenanceDocumentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source_fixture(self.root)
        (self.root / '工作台').mkdir()
        self.path = self.root / '工作台/方案.md'
        self.path.write_bytes('# 修订方案\r\n\r\n1. 核对身高。\r\n2. 核对经历。\r\n3. 新增规划。'.encode())

    def prepare(self, **options):
        return prepare_maintenance(self.root, sourcePath='工作台/方案.md', **options)

    def inventory(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def write(self, report, items, output='工作台/清单.json'):
        return write_checklist(self.root, documentRef=report['documentRef'], items=items, output=output)

    def test_segments_preserve_original_bytes_and_do_not_enumerate_requirements(self):
        before = self.inventory()
        result = self.prepare()
        self.assertEqual(''.join(row['text'] for row in result['segments']).encode(), self.path.read_bytes())
        self.assertEqual([row['kind'] for row in result['segments']], ['heading', 'list-item', 'list-item', 'list-item'])
        for row in result['segments']:
            self.assertEqual(self.path.read_bytes().decode()[row['fileStart']:row['fileEnd']], row['text'])
        self.assertEqual(result['checklistDraft']['items'], [])
        self.assertFalse(result['coverage']['checklistEnumerated'])
        self.assertFalse(result['coverage']['semanticReviewComplete'])
        self.assertEqual(self.prepare()['documentRef'], result['documentRef'])
        self.assertEqual(before, self.inventory())

    def test_fences_and_edge_whitespace_are_lossless(self):
        samples = ['', '\r\n  \r\n', '末尾无换行',
                   '\n# 方案\n\n```json\r\n# 不是标题\n1. 不是条目\n```\n正文\n',
                   '段落\n续行\n\n- 条目\n  续行\n~~~\n- 代码\n~~~\n最后一段',
                   '```\n未闭合\n# 仍是代码\n']
        for text in samples:
            with self.subTest(text=text):
                rows = split_document(text, {'path': 'doc.md', 'sha256': 'f' * 64})
                self.assertEqual(''.join(text[row['fileStart']:row['fileEnd']] for row in rows), text)
                for left, right in zip(rows, rows[1:]):
                    self.assertEqual(left['fileEnd'], right['fileStart'])
                code = [text[row['fileStart']:row['fileEnd']] for row in rows if row['kind'] == 'code']
                if '# 不是标题' in text:
                    self.assertTrue(any('# 不是标题\n1. 不是条目' in part for part in code))

    def test_unicode_pagination_reconstructs_every_character_and_rejects_drift(self):
        raw = ('# 方案\r\n\r\n1. ' + '原文🙂\r\n续行' * 900 + '\r\n2. 收尾').encode()
        self.path.write_bytes(raw)
        before = self.inventory()
        cursor = None
        pieces, pages = {}, 0
        while True:
            report = self.prepare(limit=1, maxBytes=6000, cursor=cursor)
            pages += 1
            self.assertLessEqual(len(json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode()), 6000)
            for row in report['segments']:
                prior = pieces.setdefault(row['id'], [])
                self.assertEqual(row['start'], prior[-1]['end'] if prior else 0)
                prior.append(row)
            if not report['hasMore']:
                break
            cursor = report['nextCursor']
        self.assertGreater(pages, 1)
        self.assertEqual(''.join(row['text'] for rows in pieces.values() for row in rows).encode(), raw)
        self.assertEqual(before, self.inventory())
        first = self.prepare(limit=1)
        self.path.write_bytes(raw + b'\n')
        with self.assertRaises(StructureError) as caught:
            self.prepare(limit=1, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')

    def test_selected_merge_and_subrange_generate_exact_quotes_and_machine_fields(self):
        report = self.prepare()
        segments = report['segments']
        raw = self.path.read_bytes().decode()
        before_source = (self.root / '.govel/structure.json').read_bytes()
        result = self.write(report, [
            {'segmentIds': [segments[1]['id'], segments[2]['id']], 'recordIds': ['fact-HEIGHT', 'fact-LIFE']},
            {'segmentIds': [segments[3]['id']], 'range': {'start': 3, 'end': 7}, 'recordIds': []},
        ])
        checklist = read_json(self.root / result['checklistPath'])
        self.assertEqual(checklist['items'][0]['quote'], raw[segments[1]['fileStart']:segments[2]['fileEnd']])
        self.assertEqual(checklist['items'][1]['quote'], segments[3]['text'][3:7])
        self.assertTrue(all(row['itemId'].startswith('item-') and row['operationIds'] == [] for row in checklist['items']))
        self.assertEqual(checklist['source'], report['checklistDraft']['source'])
        self.assertTrue(result['needsMapping'])
        self.assertFalse(result['registered'])
        self.assertFalse(result['approvalGranted'])
        checked = check_maintenance(self.root, checklistPath=result['checklistPath'])
        self.assertTrue(checked['summary']['needsMapping'])
        self.assertEqual(checked['summary']['submittedItemCount'], 0)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before_source)

    def test_bad_selections_cannot_write_partial_checklists(self):
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass
        report = self.prepare()
        ids = [row['id'] for row in report['segments']]
        valid = {'segmentIds': [ids[1]], 'recordIds': ['fact-HEIGHT']}
        invalid = [
            {'segmentIds': ['foreign-segment'], 'recordIds': []},
            {'segmentIds': [ids[1], ids[3]], 'recordIds': []},
            {'segmentIds': [ids[2], ids[1]], 'recordIds': []},
            {'segmentIds': [ids[2]], 'recordIds': ['fact-UNKNOWN']},
            {'segmentIds': [ids[2]], 'recordIds': [], 'range': {'start': 8, 'end': 5}},
            {'segmentIds': [ids[2]], 'recordIds': [], 'range': {'start': 0, 'end': 1000}},
            {'segmentIds': [ids[2]], 'recordIds': [], 'range': {'start': True, 'end': 5}},
            valid,
        ]
        before = self.inventory()
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(StructureError):
                self.write(report, [valid, selection])
            self.assertEqual(before, self.inventory())

    def test_existing_outputs_and_protected_paths_are_never_overwritten(self):
        from scripts.core.locks import project_lock_reentrant
        with project_lock_reentrant(self.root):
            pass
        report = self.prepare()
        selection = [{'segmentIds': [report['segments'][1]['id']], 'recordIds': []}]
        (self.root / '工作台/existing.json').write_text('authored')
        before = self.inventory()
        for output in ('工作台/existing.json', '.govel/structure.json', '正文/new.json', '.'):
            with self.subTest(output=output), self.assertRaises(StructureError):
                self.write(report, selection, output)
            self.assertEqual(before, self.inventory())

    def test_identical_retry_reuses_the_existing_checklist_without_touching_it(self):
        report = self.prepare()
        selection = [{'segmentIds': [report['segments'][1]['id']], 'recordIds': ['fact-HEIGHT']}]
        first = self.write(report, selection)
        path = self.root / first['checklistPath']
        before, stamp = self.inventory(), path.stat()
        retried = self.write(report, selection)
        self.assertTrue(retried['reused'])
        self.assertEqual(retried['code'], 'SOURCE_MAINTENANCE_CHECKLIST_REUSED')
        self.assertEqual(retried['checklistPath'], first['checklistPath'])
        self.assertFalse(retried['registered'])
        self.assertFalse(retried['approvalGranted'])
        self.assertEqual(self.inventory(), before)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (stamp.st_ino, stamp.st_mtime_ns))

    def test_output_created_during_assembly_is_not_overwritten(self):
        report = self.prepare()
        from scripts.core import write_sets
        original = write_sets.inventory
        path = self.root / '工作台/清单.json'

        def create_external(root, outputs, **kwargs):
            path.write_text('externally authored draft')
            return original(root, outputs, **kwargs)

        with patch.object(write_sets, 'inventory', side_effect=create_external), self.assertRaises(StructureError):
            self.write(report, [{'segmentIds': [report['segments'][1]['id']], 'recordIds': []}])
        self.assertEqual(path.read_text(), 'externally authored draft')

    def test_changed_replaced_or_missing_document_reference_is_rejected(self):
        report = self.prepare()
        selection = [{'segmentIds': [report['segments'][1]['id']], 'recordIds': []}]
        raw = self.path.read_bytes()
        self.path.write_bytes(raw + b'\n')
        with self.assertRaises(StructureError) as caught:
            self.write(report, selection)
        self.assertEqual(caught.exception.code, 'MAINTENANCE_SOURCE_STALE')
        self.path.write_bytes(raw)
        report = self.prepare()
        replacement = self.path.with_suffix('.new')
        replacement.write_bytes(raw)
        replacement.replace(self.path)
        with self.assertRaises(StructureError) as caught:
            self.write(report, selection)
        self.assertEqual(caught.exception.code, 'MAINTENANCE_SOURCE_STALE')
        with tempfile.TemporaryDirectory() as temp, patch('scripts.core.transport_cache._directory', return_value=Path(temp)):
            with self.assertRaises(StructureError) as caught:
                self.write(report, selection)
        self.assertEqual(caught.exception.code, 'TRANSPORT_SNAPSHOT_UNAVAILABLE')
        self.assertFalse((self.root / '工作台/清单.json').exists())

    def test_cross_project_reference_cannot_write_a_checklist(self):
        report = self.prepare()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source_fixture(root)
            (root / '工作台').mkdir()
            (root / '工作台/方案.md').write_bytes(self.path.read_bytes())
            with self.assertRaises(StructureError):
                write_checklist(root, documentRef=report['documentRef'],
                                items=[{'segmentIds': [report['segments'][1]['id']], 'recordIds': []}],
                                output='工作台/清单.json')
            self.assertFalse((root / '工作台/清单.json').exists())

    def test_late_document_edit_rolls_back_output_but_preserves_external_edit(self):
        report = self.prepare()
        from scripts.core import write_sets
        original = write_sets.validate_guards
        edited = self.path.read_bytes() + b'\nexternal'

        def mutate(root, guards, *, stage):
            if stage == 'before-committed':
                self.path.write_bytes(edited)
            return original(root, guards, stage=stage)

        with patch.object(write_sets, 'validate_guards', side_effect=mutate), self.assertRaises(StructureError):
            self.write(report, [{'segmentIds': [report['segments'][1]['id']], 'recordIds': []}])
        self.assertEqual(self.path.read_bytes(), edited)
        self.assertFalse((self.root / '工作台/清单.json').exists())

    def test_cli_and_mcp_use_the_same_selection_contract(self):
        report = self.prepare()
        items = [{'segmentIds': [report['segments'][1]['id']], 'recordIds': ['fact-HEIGHT']}]
        choices = self.root / '工作台/selection.json'
        write_json(choices, items)
        cli = subprocess.run([sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source',
                              'maintenance-checklist', '--project-root', str(self.root), '--document-ref', report['documentRef'],
                              '--items', '@' + str(choices), '--output', '工作台/cli.json'], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        from scripts.mcp_server import handle
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'write_source_maintenance_checklist', 'arguments': {
                               'projectRoot': str(self.root), 'documentRef': report['documentRef'],
                               'items': items, 'output': '工作台/mcp.json'}}})['result']
        self.assertFalse(response['isError'], response)
        self.assertEqual((self.root / '工作台/cli.json').read_bytes(), (self.root / '工作台/mcp.json').read_bytes())

    def create_linked(self, request_id, reference, order):
        source = load_structure(self.root)
        record = deepcopy(source['fragments'][0])
        record.update(fragmentId='$planned', contentRole='plan', order=order)
        plan = preview_source_change(self.root, changes=[{'action': 'put', 'collection': 'fragments', 'record': record}],
                                     requestId=request_id, baseRevision=source['revision'], reason='explicit selected requirement',
                                     maintenanceReference=reference)
        receipt = commit(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True, actor='test')
        return plan, receipt

    def test_creation_receipt_resolves_handles_without_rewriting_checklist(self):
        report = self.prepare()
        result = self.write(report, [{'segmentIds': [report['segments'][3]['id']], 'recordIds': ['$planned']}])
        raw = (self.root / result['checklistPath']).read_bytes()
        checklist = read_json(self.root / result['checklistPath'])
        reference = {'checklistPath': result['checklistPath'], 'itemIds': [checklist['items'][0]['itemId']]}
        plan, receipt = self.create_linked('new-plan-1', reference, 99)
        checked = check_maintenance(self.root, checklistPath=result['checklistPath'])
        target = receipt['identityMap']['$planned']
        self.assertEqual(checked['items'][0]['recordIds'], [target])
        self.assertEqual(checked['items'][0]['handleResolution']['resolved'], {'$planned': target})
        self.assertEqual(checked['summary']['submittedItemCount'], 1)
        self.assertEqual((self.root / result['checklistPath']).read_bytes(), raw)
        undo(self.root, plan['operationId'], actor='test')
        checked = check_maintenance(self.root, checklistPath=result['checklistPath'])
        self.assertEqual(checked['summary']['submittedItemCount'], 0)
        self.assertEqual(checked['items'][0]['handleResolution']['unresolved'], {'$planned': []})

    def test_reused_handle_is_ambiguous_and_other_documents_do_not_inherit_it(self):
        report = self.prepare()
        result = self.write(report, [{'segmentIds': [report['segments'][3]['id']], 'recordIds': ['$planned']}])
        checklist = read_json(self.root / result['checklistPath'])
        reference = {'checklistPath': result['checklistPath'], 'itemIds': [checklist['items'][0]['itemId']]}
        self.create_linked('new-plan-1', reference, 99)
        self.create_linked('new-plan-2', reference, 100)
        checked = check_maintenance(self.root, checklistPath=result['checklistPath'])
        self.assertEqual(checked['summary']['submittedItemCount'], 0)
        self.assertEqual(len(checked['items'][0]['handleResolution']['unresolved']['$planned']), 2)
        other = self.root / '工作台/other.md'
        other.write_bytes(self.path.read_bytes())
        checklist['source']['path'] = '工作台/other.md'
        write_json(self.root / result['checklistPath'], checklist)
        checked = check_maintenance(self.root, checklistPath=result['checklistPath'])
        self.assertEqual(checked['items'][0]['handleResolution']['unresolved']['$planned'], [])
        self.assertEqual(checked['summary']['submittedItemCount'], 0)


if __name__ == '__main__':
    unittest.main()
