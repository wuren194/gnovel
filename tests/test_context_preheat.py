from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.core.context_preheat import preheat_fingerprint, preheat_metadata, preheat_next_chapter
from scripts.core.jsonio import write_json
from scripts.core.structure_store import StructureError, validate_structure
from tests.test_source_plan import planning_source


class ContextPreheatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.current = planning_source(self.root)
        self.source['chapters'].append({
            'chapterId': 'CH-NEXT', 'title': '下一章', 'displayOrdinal': 2,
            'placement': self.current['placement'], 'filePath': '正文/下一章.md',
            'status': 'planning_required',
        })
        self.source['chapterArcLinks'].append({'chapterId': 'CH-NEXT',
                                               'arcId': self.source['arcs'][0]['arcId'], 'role': 'primary'})
        self.save()

    def save(self):
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)

    def test_provisional_metadata_requires_stability_and_revalidation(self):
        before = (self.root / '.govel/structure.json').read_bytes()
        first = preheat_next_chapter(self.root, afterChapterId=self.current['chapterId'])
        self.assertFalse(first['reusableMetadata'])
        self.assertFalse(first['coverage']['futurePlotGenerated'])
        self.assertFalse(first['coverage']['proseRead'])
        self.assertEqual(before, (self.root / '.govel/structure.json').read_bytes())
        self.current['status'] = 'stable'
        self.save()
        no_prose = preheat_next_chapter(self.root, afterChapterId=self.current['chapterId'],
                                         expectedInputHash=first['inputHash'])
        self.assertFalse(no_prose['reusableMetadata'])
        prose = self.root / '正文/守门.md'
        prose.parent.mkdir(parents=True)
        prose.write_text('甲守住了城门。', encoding='utf-8')
        validated = preheat_next_chapter(self.root, afterChapterId=self.current['chapterId'],
                                          expectedInputHash=first['inputHash'])
        self.assertTrue(validated['reusableMetadata'])
        self.assertNotEqual(validated['sourceHash'], first['sourceHash'])
        self.source['chapters'][1]['title'] = '改过的下一章'
        self.save()
        stale = preheat_next_chapter(self.root, afterChapterId=self.current['chapterId'],
                                     expectedInputHash=first['inputHash'])
        self.assertTrue(stale['stale'])
        self.assertFalse(stale['reusableMetadata'])

    def test_no_registered_successor_fails_closed(self):
        with self.assertRaises(StructureError) as caught:
            preheat_next_chapter(self.root, afterChapterId='CH-NEXT')
        self.assertEqual(caught.exception.code, 'NEXT_CHAPTER_NOT_FOUND')


class ContextPreheatContractTests(unittest.TestCase):
    """Preheat 与 Builder 的正式契约：共享 fingerprint，且预取不缓存任何未来上下文。"""

    def setUp(self):
        from tests.test_context_builder import context_source, save_context_source
        self.save_context_source = save_context_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / '正文').mkdir()
        self.source = context_source(self.root)

    def save(self, source=None):
        self.save_context_source(self.root, source if source is not None else self.source)

    def preheat(self, **kwargs):
        return preheat_next_chapter(self.root, afterChapterId='CH-AAA', **kwargs)

    def test_metadata_preheat_does_not_read_future_prose_or_generate_plot(self):
        first = self.preheat()
        coverage = first['coverage']
        self.assertFalse(coverage['futurePlotGenerated'])
        self.assertFalse(coverage['proseRead'])
        self.assertFalse(coverage['contextPackCached'])
        self.assertTrue(coverage['registeredPlanningMetadataOnly'])
        self.assertNotIn('context', first)
        self.assertNotIn('layers', first)
        self.assertNotIn('knowledgePack', first)
        self.assertNotIn('memoryPack', first)

    def test_matching_input_hash_is_reusable_only_after_stability(self):
        before = self.preheat()
        self.assertFalse(before['reusableMetadata'])
        current = self.source
        current['chapters'][0]['status'] = 'stable'
        self.save(current)
        (self.root / '正文/CH-AAA.md').write_text('第一章正文。', encoding='utf-8')
        self.assertTrue(self.preheat(expectedInputHash=before['inputHash'])['reusableMetadata'])

    def test_source_plan_change_makes_preheat_stale(self):
        changed = self.source
        changed['attributes'].append({'attributeId': 'AT-NEXT', 'ownerId': 'CH-BBB',
                                      'fieldId': 'FD-PLAN', 'values': []})
        changed['fragments'].append({'fragmentId': 'SF-NEXT', 'ownerId': 'CH-BBB', 'text': '乙在码头等待。',
                                     'order': 1, 'contentRole': 'plan', 'contentBindings': ['AT-NEXT']})
        self.save(changed)
        first = self.preheat()
        self.assertTrue(first['metadata']['planReady'])
        changed['fragments'][-1]['text'] = '乙离开码头。'
        self.save(changed)
        again = self.preheat(expectedInputHash=first['inputHash'])
        self.assertTrue(again['stale'])
        self.assertFalse(again['reusableMetadata'])
        self.assertNotEqual(again['inputHash'], first['inputHash'])
        self.assertNotEqual(again['metadata']['sourcePlanHash'], first['metadata']['sourcePlanHash'])

    def test_reference_change_makes_preheat_stale(self):
        first = self.preheat()
        changed = self.source
        changed['references'].append({'sourceId': 'CH-BBB', 'targetId': 'C-BETA', 'kind': 'uses'})
        self.save(changed)
        again = self.preheat(expectedInputHash=first['inputHash'])
        self.assertTrue(again['stale'])
        self.assertNotEqual(again['metadata']['registeredReferenceIds'],
                            first['metadata']['registeredReferenceIds'])

    def test_arc_change_makes_preheat_stale(self):
        from scripts.core.structure_store import new_id
        first = self.preheat()
        changed = self.source
        volume = changed['volumes'][0]['volumeId']
        arc_id = new_id('arc')
        changed['arcs'].append({'arcId': arc_id, 'title': '支线', 'kind': 'sub', 'status': 'planned',
                                'volumeIds': [volume], 'parentArcId': None, 'promise': '',
                                'resolutionTargetChapterId': None})
        changed['chapterArcLinks'].append({'chapterId': 'CH-BBB', 'arcId': arc_id, 'role': 'secondary'})
        self.save(changed)
        again = self.preheat(expectedInputHash=first['inputHash'])
        self.assertTrue(again['stale'])
        self.assertNotEqual(again['metadata']['arcIds'], first['metadata']['arcIds'])

    def test_placement_change_makes_preheat_stale(self):
        first = self.preheat()
        changed = self.source
        changed['chapters'][1]['title'] = '改过的下一章'
        self.save(changed)
        again = self.preheat(expectedInputHash=first['inputHash'])
        self.assertTrue(again['stale'])
        self.assertNotEqual(again['metadata']['nextChapterTitle'], first['metadata']['nextChapterTitle'])

    def test_previous_chapter_without_stable_prose_cannot_produce_reusable_metadata(self):
        current = self.source
        current['chapters'][0]['status'] = 'stable'
        self.save(current)
        # 状态是 stable 但正文缺失：仍不可复用。
        missing = self.preheat()
        self.assertTrue(missing['afterChapterStable'])
        self.assertFalse(missing['afterChapterProsePresent'])
        self.assertFalse(self.preheat(expectedInputHash=missing['inputHash'])['reusableMetadata'])

    def test_builder_records_the_preheat_fingerprint_for_its_chapter(self):
        from scripts.core.context_builder import build_station_context
        expected = preheat_fingerprint(self.source, 'CH-AAA')
        context = build_station_context(str(self.root), 'CH-BBB', task_type='planner',
                                        view='station', read_only=True)
        self.assertEqual(context['sourceContext']['preheatInputFingerprint'], expected)
        first_chapter = build_station_context(str(self.root), 'CH-AAA', task_type='planner',
                                              view='station', read_only=True)
        self.assertIsNone(first_chapter['sourceContext']['preheatInputFingerprint'],
                          '没有前驱章的章节没有可复用的预取指纹')
        # spec 变化后同一指纹必须失效：预取不能悄悄被当成仍然有效。
        changed = self.source
        changed['chapters'][1]['title'] = '又改了'
        self.save(changed)
        moved = build_station_context(str(self.root), 'CH-BBB', task_type='planner',
                                     view='station', read_only=True)
        self.assertNotEqual(moved['sourceContext']['preheatInputFingerprint'], expected)
        self.assertTrue(preheat_next_chapter(self.root, afterChapterId='CH-AAA',
                                             expectedInputHash=expected)['stale'])

    def test_supplied_plan_must_belong_to_the_registered_successor(self):
        from scripts.core.source_plan import chapter_plan
        correct = chapter_plan(self.source, 'CH-BBB')
        self.assertEqual(preheat_metadata(self.source, 'CH-AAA', _plan=correct),
                         preheat_metadata(self.source, 'CH-AAA'))
        self.assertEqual(preheat_fingerprint(self.source, 'CH-AAA', _plan=correct),
                         preheat_fingerprint(self.source, 'CH-AAA'))
        wrong = chapter_plan(self.source, 'CH-AAA')
        for read in (preheat_metadata, preheat_fingerprint):
            with self.subTest(reader=read.__name__):
                with self.assertRaisesRegex(ValueError, 'another chapter'):
                    read(self.source, 'CH-AAA', _plan=wrong)

    def test_preheat_never_caches_complete_station_context(self):
        result = self.preheat()
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for marker in ('memoryPack', 'dependencyManifest', 'contextHash', 'storySummary', 'layers'):
            self.assertNotIn(marker, serialized)


if __name__ == '__main__':
    unittest.main()
