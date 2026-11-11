"""Delivery receipts preserve actual character coverage across review batches."""
import base64
import json
from pathlib import Path
import tempfile
import unittest

from scripts.core.jsonio import write_json
from scripts.core.operations import call_operation
from scripts.core.query import _cursor_decode
from scripts.core.structure_store import StructureError, validate_structure
from scripts.core.transport_cache import get
from tests.fixtures_source import source_fixture


class EvidenceMaterialReceiptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.source = source_fixture(self.root)

    def read(self, ids, **options):
        return call_operation('evidence.prepare', self.root, {'kind': 'review', 'recordIds': ids,
                              'includeContext': True, 'holdMaterials': True, **options})

    def test_default_reading_has_no_new_receipt_cost(self):
        page = self.read(['fact-HEIGHT'], holdMaterials=False)
        self.assertNotIn('materialReadReceipt', page)
        self.assertNotIn('reusedMaterials', page)

    def pages(self, ids, **options):
        rows, pages, cursor = [], [], None
        while True:
            page = self.read(ids, **options, **({'cursor': cursor} if cursor else {}))
            pages.append(page); rows.extend(page['materials'])
            if not page['hasMore']:
                return pages, rows
            cursor = page['nextCursor']

    def test_shared_dependencies_are_not_delivered_again_but_tasks_are(self):
        first, _ = self.pages(['fact-HEIGHT'])
        full, raw = self.pages(['fact-MIXED'])
        small, rows = self.pages(['fact-MIXED'], priorReceipt=first[-1]['materialReadReceipt'])
        original = {row['dependencyId'] for row in raw if row.get('materialKind') == 'dependency'}
        delivered = {row['dependencyId'] for row in rows if row.get('materialKind') == 'dependency'}
        self.assertTrue(original - delivered)
        self.assertTrue(any(row.get('materialKind') == 'review-task' for row in rows))
        self.assertGreater(small[0]['reusedMaterials']['characters'], 0)
        self.assertFalse(small[-1]['semanticReviewComplete'])
        self.assertEqual(full[0]['snapshotRef'], small[0]['snapshotRef'])
        repeated, rows = self.pages(['fact-MIXED'], priorReceipt=small[-1]['materialReadReceipt'])
        self.assertTrue(rows)
        self.assertTrue(all(row.get('materialKind') == 'review-task' for row in rows))

    def test_large_shared_material_reduces_pages_and_total_delivery(self):
        self.source['fragments'].append({'fragmentId': 'fact-SHARED', 'ownerId': 'character-ALPHA',
            'text': 'Shared historical detail.\r\n' * 2000, 'order': 5, 'contentRole': 'fact',
            'contentBindings': ['attribute-LIFE'], 'dependsOn': []})
        for row in self.source['fragments']:
            if row['fragmentId'] in {'fact-HEIGHT', 'fact-MIXED'}:
                row['dependsOn'].append('fact-SHARED')
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)
        self.shared_metrics = {}
        for reuse in (False, True):
            all_pages, prior = [], None
            for rid in ('fact-HEIGHT', 'fact-MIXED'):
                pages, _ = self.pages([rid], holdMaterials=reuse, **({'priorReceipt': prior} if prior else {}))
                prior = pages[-1].get('materialReadReceipt')
                all_pages.extend(pages)
            self.shared_metrics[reuse] = {
                'pages': len(all_pages),
                'responseCharacters': sum(len(json.dumps(page, ensure_ascii=False, separators=(',', ':')))
                                          for page in all_pages)}
        self.assertLess(self.shared_metrics[True]['pages'], self.shared_metrics[False]['pages'])
        self.assertLess(self.shared_metrics[True]['responseCharacters'], self.shared_metrics[False]['responseCharacters'])

    def test_partial_unicode_material_only_reuses_delivered_characters(self):
        self.source['fragments'][0]['text'] = '长句😀\r\n' * 2400
        write_json(self.root / '.govel/structure.json', self.source)
        first = self.read(['fact-HEIGHT'], maxBytes=3000)
        self.assertTrue(first['hasMore'])
        next_read = self.read(['fact-HEIGHT'], maxBytes=3000, priorReceipt=first['materialReadReceipt'])
        body = next(row for row in next_read['materials'] if not row.get('materialKind'))
        self.assertEqual(body['start'], first['materials'][0]['end'])
        self.assertEqual(body['text'], self.source['fragments'][0]['text'][body['start']:body['end']])
        self.assertLessEqual(len(json.dumps(next_read, ensure_ascii=False, indent=2).encode()) + 1, 3000)

    def test_last_page_receipt_accumulates_only_issued_cursor_chain(self):
        pages, _ = self.pages(['fact-HEIGHT', 'fact-MIXED'], limit=1, maxBytes=2400)
        receipt = get(self.root, pages[-1]['materialReadReceipt'], kind='evidence-read-receipt')
        self.assertTrue(all(row['ranges'] == [[0, row['totalCharacters']]] for row in receipt['entries']))
        repeated, rows = self.pages(['fact-HEIGHT', 'fact-MIXED'], priorReceipt=pages[-1]['materialReadReceipt'])
        self.assertTrue(all(row.get('materialKind') == 'review-task' for row in rows))
        first = pages[0]
        for change in ('offset', 'missing', 'foreign'):
            cursor = _cursor_decode(first['nextCursor'])
            if change == 'offset': cursor['offset'] += 1
            elif change == 'missing': cursor.pop('materialReadReceipt')
            else: cursor['materialReadReceipt'] = pages[-1]['materialReadReceipt']
            forged = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode()
            with self.subTest(change=change), self.assertRaises(StructureError):
                self.read(['fact-HEIGHT', 'fact-MIXED'], limit=1, maxBytes=2400, cursor=forged)

    def test_own_review_progress_can_resume_without_rereading_held_dependencies(self):
        first = self.read(['fact-HEIGHT', 'fact-MIXED'], limit=1)
        packed = call_operation('evidence.pack', self.root, {'snapshotRef': first['snapshotRef'], 'requestId': 'own',
            'decisions': [{'recordId': 'fact-HEIGHT', 'decision': 'not_checked', 'reason': 'Explicit deferral', 'quotes': []}]})
        call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef']})
        with self.assertRaises(StructureError):
            self.read(['fact-HEIGHT', 'fact-MIXED'], limit=1, cursor=first['nextCursor'])
        resumed = self.read(['fact-HEIGHT', 'fact-MIXED'], limit=1, cursor=first['nextCursor'], resumeRequestIds=['own'])
        self.assertEqual(resumed['continuation']['recordedCount'], 1)
        self.assertFalse(resumed['approvalGranted'])

    def test_source_permission_and_pov_changes_never_inherit_omission(self):
        first, _ = self.pages(['fact-HEIGHT'])
        reference = first[-1]['materialReadReceipt']
        with self.assertRaises(StructureError):
            self.read(['fact-HEIGHT'], priorReceipt=reference, selection={'ownerIds': ['character-BETA']})
        with self.assertRaises(StructureError):
            self.read(['fact-HEIGHT'], priorReceipt=reference,
                      selection={'knowledgeMode': 'perspective', 'perspectiveIds': ['character-ALPHA']})
        (self.root / 'AGENTS.md').write_text('New authorization boundary')
        changed = self.read(['fact-HEIGHT'], priorReceipt=reference)
        self.assertTrue(changed['reusedMaterials']['scopeChanged'])
        self.assertEqual(changed['reusedMaterials']['characters'], 0)
        self.source['fragments'][0]['text'] += ' Changed.'
        write_json(self.root / '.govel/structure.json', self.source)
        current = self.read(['fact-HEIGHT'], priorReceipt=changed['materialReadReceipt'])
        self.assertEqual(current['reusedMaterials']['characters'], 0)

    def test_bad_cross_project_or_changed_prior_receipt_fails_closed(self):
        first = self.read(['fact-HEIGHT'], limit=1)
        with self.assertRaises(StructureError):
            self.read(['fact-HEIGHT'], priorReceipt='gn:evidence-read-receipt:' + '0' * 32)
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp).resolve(); source_fixture(other)
            with self.assertRaises(StructureError):
                call_operation('evidence.prepare', other, {'kind': 'review', 'recordIds': ['fact-HEIGHT'],
                               'priorReceipt': first['materialReadReceipt']})
        with self.assertRaises(StructureError):
            self.read(['fact-HEIGHT'], limit=1, cursor=first['nextCursor'], priorReceipt=first['materialReadReceipt'])

    def test_author_material_cannot_suppress_or_leak_into_valid_pov_reading(self):
        for rid, pov, text, dependencies in (
            ('belief-KNOWN', 'character-ALPHA', 'Visible belief.', ['fact-HEIGHT', 'belief-SECRET', 'belief-KNOWN-DEP']),
            ('belief-KNOWN-DEP', 'character-ALPHA', 'Visible dependency.', []),
            ('belief-SECRET', 'character-BETA', 'OTHER PERSPECTIVE SECRET', [])):
            self.source['fragments'].append({'fragmentId': rid, 'ownerId': 'character-ALPHA', 'text': text,
                'order': len(self.source['fragments']) + 1, 'contentRole': 'belief', 'perspectiveId': pov,
                'contentBindings': ['attribute-HEIGHT'], 'dependsOn': dependencies})
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)
        first, _ = self.pages(['belief-KNOWN'])
        pages, rows = self.pages(['belief-KNOWN'], priorReceipt=first[-1]['materialReadReceipt'],
                                selection={'knowledgeMode': 'perspective', 'perspectiveIds': ['character-ALPHA']})
        self.assertTrue(pages[0]['reusedMaterials']['scopeChanged'])
        self.assertEqual(pages[0]['reusedMaterials']['characters'], 0)
        encoded = json.dumps(rows, ensure_ascii=False)
        self.assertIn('Visible belief.', encoded)
        self.assertIn('Visible dependency.', encoded)
        for hidden in ('OTHER PERSPECTIVE SECRET', 'belief-SECRET', 'fact-HEIGHT'):
            self.assertNotIn(hidden, encoded)

    def test_receipt_does_not_replace_current_review_tasks(self):
        first, _ = self.pages(['fact-HEIGHT'])
        packed = call_operation('evidence.pack', self.root, {'snapshotRef': first[0]['snapshotRef'], 'requestId': 'reviewed',
            'decisions': [{'recordId': 'fact-HEIGHT', 'decision': 'not_checked', 'reason': 'Needs judgment', 'quotes': []}]})
        call_operation('evidence.submit', self.root, {'submissionRef': packed['submissionRef']})
        pages, rows = self.pages(['fact-HEIGHT'], priorReceipt=first[-1]['materialReadReceipt'])
        tasks = [json.loads(row['text']) for row in rows if row.get('materialKind') == 'review-task']
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['status'], 'not_checked')
        self.assertIsNone(tasks[0]['decisionInput']['decision'])
        self.assertTrue(any(row.get('materialKind') == 'prior-review' for row in rows))
        self.assertGreater(pages[0]['reusedMaterials']['characters'], 0)
        self.assertFalse(pages[-1]['semanticReviewComplete'])

    def test_complete_partial_reading_roundtrips_unicode_and_crlf(self):
        text = 'Full line 😀\r\n' * 90
        self.source['fragments'][0]['text'] = text
        write_json(self.root / '.govel/structure.json', self.source)
        first = self.read(['fact-HEIGHT'], maxBytes=1900)
        pages, rows = self.pages(['fact-HEIGHT'], maxBytes=1900, priorReceipt=first['materialReadReceipt'])
        body = [row for row in first['materials'] + rows if not row.get('materialKind')]
        self.assertEqual(''.join(row['text'] for row in body), text)
        position = 0
        for row in body:
            self.assertEqual(row['start'], position)
            position = row['end']
        for page in [first, *pages]:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, indent=2).encode()) + 1, 1900)

    def test_mcp_and_cli_can_continue_the_same_receipt(self):
        import subprocess
        import sys
        from scripts.core.project import TOOLBOX_ROOT
        first, _ = self.pages(['fact-HEIGHT'])
        command = [sys.executable, str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'evidence', 'prepare',
                   '--project-root', str(self.root), '--kind', 'review', '--record-ids', 'fact-MIXED',
                   '--include-context', '--prior-receipt', first[-1]['materialReadReceipt']]
        cli = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertGreater(json.loads(cli.stdout)['reusedMaterials']['characters'], 0)
