"""Whole-manuscript scan report: one pass, grouped by chapter, budget-capped."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_scan_report import scan_prose_report
from scripts.core.structure_store import StructureError, validate_structure
from tests.fixtures_source import source_fixture

ROOT = Path(__file__).resolve().parents[1]


class ScanReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture()
        volume_id = self.source['volumes'][0]['volumeId']
        arc_id = self.source['arcs'][0]['arcId']
        for ordinal, chapter_id in enumerate(('chapter-SCAN-A', 'chapter-SCAN-B', 'chapter-SCAN-C'), 1):
            self.source['chapters'].append({
                'chapterId': chapter_id, 'displayOrdinal': ordinal, 'displayCode': f'CH{ordinal:03d}',
                'title': f'第{ordinal}章', 'placement': {'scope': 'volume', 'volumeId': volume_id},
                'filePath': f'正文/{chapter_id}.md',
                'status': 'stable',
                'storyTimeStartId': None, 'storyTimeEndId': None,
            })
            self.source['chapterArcLinks'].append({'chapterId': chapter_id, 'arcId': arc_id, 'role': 'primary'})
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)
        (self.root / '正文').mkdir()
        (self.root / '正文/chapter-SCAN-A.md').write_text('他先救了甲。城门又救了乙。', encoding='utf-8')
        (self.root / '正文/chapter-SCAN-B.md').write_text('城门开了。', encoding='utf-8')
        # CH-SCAN-C 无正文文件：扫描记为 missing，不冒充已读

    def test_groups_hits_by_chapter_in_one_pass(self):
        report = scan_prose_report(self.root, terms=['救', '城门'])
        by_chapter = {g['chapterId']: g for g in report['chapters']}
        self.assertEqual(by_chapter['chapter-SCAN-A']['hitCount'], 3)  # 救×2 + 城门×1
        self.assertEqual(by_chapter['chapter-SCAN-B']['hitCount'], 1)
        self.assertNotIn('chapter-SCAN-C', by_chapter)
        self.assertFalse(report['summary']['truncated'])
        self.assertEqual(report['summary']['totalHits'],
                         sum(g['hitCount'] for g in report['chapters']))
        hit = by_chapter['chapter-SCAN-A']['hits'][0]
        self.assertEqual(hit['locator']['start'], hit['locator']['end'] - len(hit['text']))
        self.assertEqual(report['coverage']['missingChapterIds'], ['chapter-SCAN-C'])
        self.assertFalse(report['coverage']['completeForRequestedScope'])
        self.assertIn('absence is not proof', report['coverage']['absenceMeaning'])

    def test_budget_truncation_keeps_counts_and_marks_incomplete(self):
        report = scan_prose_report(self.root, terms=['救', '城门'], maxBytes=1)
        self.assertTrue(report['summary']['truncated'])
        self.assertEqual(report['summary']['chaptersWithHits'], 2)
        for group in report['chapters']:
            self.assertEqual(group['hits'], [])
            self.assertEqual(group['omittedHits'], group['hitCount'])
        self.assertIn('incomplete', report['coverage']['absenceMeaning'])

    def test_default_scope_complete_even_when_details_truncated(self):
        (self.root / '正文/chapter-SCAN-C.md').write_text('没有匹配。', encoding='utf-8')
        for budget in (None, 1):
            report = scan_prose_report(self.root, terms=['城门'], maxBytes=budget)
            self.assertTrue(report['coverage']['completeForRequestedScope'])
            self.assertEqual(report['summary']['chaptersWithHits'], 2)
            self.assertEqual(report['coverage']['reportTruncated'], budget == 1)

    def test_explicit_scope_and_invalid_inputs(self):
        report = scan_prose_report(self.root, terms=['城门'], chapterIds=['chapter-SCAN-B'])
        self.assertEqual([g['chapterId'] for g in report['chapters']], ['chapter-SCAN-B'])
        self.assertTrue(report['coverage']['completeForRequestedScope'])
        with self.assertRaises(StructureError):
            scan_prose_report(self.root, terms=[])
        with self.assertRaises(StructureError):
            scan_prose_report(self.root, terms=['x'], chapterIds=['chapter-NOPE'])
        with self.assertRaises(StructureError):
            scan_prose_report(self.root, terms=['x'], maxBytes=10 ** 9)

    def test_cli_report(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts/novelctl.py'), 'project',
                               'source', 'scan-report', '--project-root', str(self.root),
                               '--terms', '城门'], capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['ok'], payload)
        # CLI 未限定章节：A、B 两章都含「城门」
        self.assertEqual(payload['summary']['chapterGroupCount'], 2)


if __name__ == '__main__':
    unittest.main()
