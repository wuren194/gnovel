from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.source_candidate_scan import scan_prose_candidates
from scripts.core.source_presets import PRESETS_PATH, query_presets, save_preset
from scripts.core.source_query import query_source
from scripts.core.source_render import render_source
from scripts.core.structure_store import StructureError, validate_structure
from tests.fixtures_source import source_fixture


class SourcePresetAndCandidateScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = source_fixture()
        volume_id = self.source['volumes'][0]['volumeId']
        arc_id = self.source['arcs'][0]['arcId']
        for ordinal, chapter_id in enumerate(('CH-SCAN-A', 'CH-SCAN-B'), 1):
            self.source['chapters'].append({
                'chapterId': chapter_id, 'displayOrdinal': ordinal, 'displayCode': f'CH{ordinal:03d}',
                'title': f'第{ordinal}章', 'placement': {'scope': 'volume', 'volumeId': volume_id},
                'filePath': f'正文/{chapter_id}.md',
                'status': 'stable',
                'storyTimeStartId': None, 'storyTimeEndId': None,
            })
            self.source['chapterArcLinks'].append({'chapterId': chapter_id, 'arcId': arc_id, 'role': 'primary'})
        validate_structure(self.source)
        write_json(self.root / '.gnovel/structure.json', self.source)
        (self.root / '正文').mkdir()
        (self.root / '正文/CH-SCAN-A.md').write_text('他先救了甲。后来又救了乙。', encoding='utf-8')

    def test_preset_stores_parameters_and_drives_query_and_render(self):
        saved = save_preset(self.root, requestId='person-without-height', name='人物不含身高',
                            selection={'ownerIds': ['C-ALPHA'], 'excludeFields': ['appearance.height']})
        preset_id = saved['presetId']
        state = (self.root / PRESETS_PATH).read_text(encoding='utf-8')
        self.assertNotIn('他独自守过城门', state)
        self.assertFalse(query_presets(self.root, presetId=preset_id)['storesStoryContent'])
        direct = query_source(self.root, selection={'ownerIds': ['C-ALPHA'],
                                                    'excludeFields': ['appearance.height']}, view='detail')
        reused = query_source(self.root, presetId=preset_id, view='detail')
        self.assertEqual([row['id'] for row in direct['items']], [row['id'] for row in reused['items']])
        rendered = render_source(self.root, presetId=preset_id)
        self.assertEqual(rendered['recordIds'], [row['id'] for row in direct['items']])
        self.assertEqual(rendered['preset']['presetId'], preset_id)
        replay = save_preset(self.root, requestId='person-without-height', name='人物不含身高',
                             selection={'ownerIds': ['C-ALPHA'], 'excludeFields': ['appearance.height']})
        self.assertTrue(replay['alreadySaved'])

    def test_preset_rejects_ambiguous_or_stale_parameters(self):
        saved = save_preset(self.root, requestId='one', name='甲', selection={'ownerIds': ['C-ALPHA']})
        with self.assertRaises(StructureError) as caught:
            query_source(self.root, presetId=saved['presetId'], selection={})
        self.assertEqual(caught.exception.code, 'SOURCE_SELECTION_INVALID')
        with self.assertRaises(StructureError):
            save_preset(self.root, requestId='two', name='甲', selection={'ownerIds': ['C-BETA']})
        with self.assertRaises(StructureError):
            save_preset(self.root, requestId='one', name='甲', selection={'ownerIds': ['C-BETA']})

    def test_literal_scan_reports_candidates_and_explicit_coverage(self):
        result = scan_prose_candidates(self.root, terms=['救了'], chapterIds=['CH-SCAN-A'],
                                       targetIds=['AT-LIFE'])
        self.assertEqual(result['totalMatching'], 2)
        self.assertTrue(result['coverage']['completeForRequestedScope'])
        self.assertTrue(result['coverage']['semanticJudgmentMade'] is False)
        self.assertTrue(all(not row['provesRuleViolation'] and not row['provesDependency']
                            for row in result['items']))
        incomplete = scan_prose_candidates(self.root, terms=['救了'],
                                           chapterIds=['CH-SCAN-A', 'CH-SCAN-B'])
        self.assertFalse(incomplete['coverage']['completeForRequestedScope'])
        self.assertIn('CH-SCAN-B', incomplete['coverage']['missingChapterIds'])

    def test_candidate_cursor_binds_current_manuscript_bytes(self):
        text = '救了。' * 30
        (self.root / '正文/CH-SCAN-A.md').write_text(text, encoding='utf-8')
        first = scan_prose_candidates(self.root, terms=['救了'], chapterIds=['CH-SCAN-A'], limit=2)
        self.assertTrue(first['hasMore'])
        (self.root / '正文/CH-SCAN-A.md').write_text(text + '改', encoding='utf-8')
        with self.assertRaises(StructureError) as caught:
            scan_prose_candidates(self.root, terms=['救了'], chapterIds=['CH-SCAN-A'],
                                  limit=2, cursor=first['nextCursor'])
        self.assertEqual(caught.exception.code, 'CURSOR_STALE')


if __name__ == '__main__':
    unittest.main()
