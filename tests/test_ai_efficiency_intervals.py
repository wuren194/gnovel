"""Absolute coverage reuse keeps gaps, coordinates, caller possession and permissions."""
from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from scripts.core.artifacts import sha256_json
from scripts.core.operations import call_operation
from scripts.core.reading_batch import read_batch
from scripts.core.reading_receipts import FIELDS, omit_unchanged, prior_entries, receipt_response
from scripts.core.structure_store import StructureError
from scripts.core.text_pages import text_page
from scripts.core.transport_cache import get
from scripts.core.write_sets import digest
from tests.fixtures_workflow import review_ready_project


class IntervalReadingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]
        self.text = ''.join(f'第{i}行😀e\u0301\r\n' for i in range(1, 9))
        (self.root / 'AGENTS.md').write_bytes(self.text.encode())

    def request(self, start, end):
        return {'root': 'project', 'path': 'AGENTS.md', 'sha256': digest(self.text.encode()),
                'startLine': start, 'endLine': end}

    def read(self, start, end, prior=None, **extra):
        return read_batch(self.root, task='editor', chapterId=self.chapter,
                          requests=[self.request(start, end)], priorReceipt=prior, **extra)

    def test_containment_and_partial_overlap_preserve_original_coordinates(self):
        first = self.read(2, 5)
        inside = self.read(3, 4, first['sessionReadReceipt'])
        self.assertEqual(inside['items'], [])
        self.assertEqual(inside['unchangedSummary']['chunks'], 1)
        overlap = self.read(4, 7, first['sessionReadReceipt'])
        self.assertEqual(''.join(row['text'] for row in overlap['items']), ''.join(self.text.splitlines(keepends=True)[5:7]))
        for row in overlap['items']:
            self.assertEqual(row['text'], self.text[row['fileStart'] + row['start']:row['fileStart'] + row['end']])
        self.assertFalse(overlap['coverage']['semanticReviewComplete'])
        self.assertTrue(self.read(3, 4)['items'])

    def test_multiple_holes_of_one_material_accumulate_across_fixed_prior_pages(self):
        first = self.read(2, 3)
        second = self.read(5, 6, first['sessionReadReceipt'])
        prior = second['sessionReadReceipt']
        cursor, delivered, pages = None, [], []
        while True:
            page = self.read(1, 8, prior, limit=1, **({'cursor': cursor} if cursor else {}))
            pages.append(page); delivered.extend(page['items'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        self.assertEqual(len(pages), 3)
        self.assertEqual(len({row['id'] for row in delivered}), 1)
        lines = self.text.splitlines(keepends=True)
        self.assertEqual(''.join(row['text'] for row in delivered), lines[0] + lines[3] + ''.join(lines[6:8]))
        self.assertTrue(all(page['coverage']['uniqueChunkCount'] == 1 for page in pages))
        self.assertEqual(self.read(1, 8, pages[-1]['sessionReadReceipt'])['items'], [])
        with self.assertRaises(StructureError):
            self.read(1, 8, pages[0]['sessionReadReceipt'], limit=1, cursor=pages[0]['nextCursor'])

    def test_legacy_dict_compressed_and_reference_receipts_convert_only_matching_lines(self):
        result = self.read(3, 6)
        receipt = get(self.root, result['sessionReadReceipt'], kind='receipt')
        receipt.pop('version')
        for row in receipt['chunks']:
            row.pop('fileStart', None)
        receipt['receiptHash'] = sha256_json({k: v for k, v in receipt.items() if k != 'receiptHash'})
        from scripts.core.reading_handoff import encode_receipt
        from scripts.core.transport_cache import put
        for prior in (receipt, encode_receipt(receipt), put(self.root, 'receipt', receipt)):
            with self.subTest(type=type(prior).__name__):
                self.assertEqual(self.read(4, 5, prior)['items'], [])
        corrupted = deepcopy(receipt); corrupted['chunks'][0]['contentSha256'] = 'wrong'
        corrupted['receiptHash'] = sha256_json({k: v for k, v in corrupted.items() if k != 'receiptHash'})
        self.assertTrue(self.read(4, 5, corrupted)['items'])

    def test_partial_long_line_receipt_resumes_only_unread_characters(self):
        self.text = '甲😀\t"乙' * 2500
        (self.root / 'AGENTS.md').write_bytes(self.text.encode())
        first = self.read(1, 1, maxBytes=3500)
        self.assertTrue(first['hasMore'])
        next_page = self.read(1, 1, first['sessionReadReceipt'], maxBytes=3500)
        self.assertEqual(next_page['items'][0]['start'], first['items'][-1]['end'])
        self.assertEqual(next_page['items'][0]['fileStart'], 0)
        self.assertEqual(next_page['items'][0]['text'], self.text[next_page['items'][0]['start']:next_page['items'][0]['end']])

    def test_prior_receipt_does_not_override_new_permissions_or_file_version(self):
        report = call_operation('task.prepare', self.root, {'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        read_id = next(row['readId'] for row in report['readList'] if row.get('path') == 'AGENTS.md')
        arguments = {'credential': report['reading']['credential'], 'readIds': [read_id]}
        first = call_operation('task.read-batch', self.root, arguments)
        from scripts.core.jsonio import read_json, write_json
        workflow = read_json(self.root / '.govel/workflow.json'); workflow['approvedChapterIds'] = []
        write_json(self.root / '.govel/workflow.json', workflow)
        with self.assertRaises(StructureError):
            call_operation('task.read-batch', self.root, {**arguments, 'priorReceipt': first['sessionReadReceipt']})
        self.text = '改' + self.text[1:]
        (self.root / 'AGENTS.md').write_bytes(self.text.encode())
        self.assertTrue(self.read(1, 8, first['sessionReadReceipt'])['items'][0]['text'].startswith('改'))
        with self.assertRaises(StructureError):
            read_batch(self.root, task='editor', chapterId=self.chapter, requests=[{**self.request(1, 1), 'path': '秘密.txt'}],
                       priorReceipt=first['sessionReadReceipt'])


class SparseReceiptTests(unittest.TestCase):
    def chunk(self, text='abcdefghij', **extra):
        return {'id': 'one', 'root': 'project', 'path': 'x.txt', 'fileSha256': 'file-version',
                'contentSha256': digest(text.encode()), 'startLine': 1, 'endLine': 1,
                'fileStart': 100, 'text': text, 'requestIndexes': [0], **extra}

    def test_adjacent_and_duplicate_ranges_union_but_different_source_text_never_reuses(self):
        row = self.chunk()
        old = {**{key: row[key] for key in FIELDS}, 'fileStart': 100, 'totalCharacters': 10, 'ranges': [[0, 3]]}
        prior = [old, {**old, 'ranges': [[2, 5]]}, {**old, 'ranges': [[5, 8]]}]
        omit_unchanged([row], prior)
        self.assertEqual(row['_deliveryRanges'], [[8, 10]])
        source = self.chunk(root='source', contentSha256='new-serialization')
        omit_unchanged([source], [{**old, 'root': 'source', 'ranges': [[0, 10]]}])
        self.assertEqual(source['_deliveryRanges'], [[0, 10]])

    def test_sparse_page_counts_and_receipt_do_not_fill_undelivered_gaps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); row = self.chunk('a' * 1000)
            ranges = [[[0, 300], [500, 1000]]]
            result = text_page({'ok': True}, [row], budget=1400, limit=1, cursor=None,
                               source='source', binding='binding', key='items', ranges=ranges,
                               transform=receipt_response(root, [], [row], ranges))
            saved = get(root, result['sessionReadReceipt'], kind='receipt')
            self.assertEqual(saved['chunks'][0]['ranges'], [[0, result['items'][0]['end']]])
            self.assertLessEqual(result['items'][0]['end'], 300)
            self.assertEqual(result['totalMatching'], 1)
            self.assertEqual(result['coverage']['remainingMaterials'], 1)
            saved['chunks'][0]['fileStart'] = False
            saved['receiptHash'] = sha256_json({k: v for k, v in saved.items() if k != 'receiptHash'})
            with self.assertRaises(StructureError):
                prior_entries(root, saved)


if __name__ == '__main__':
    unittest.main()
