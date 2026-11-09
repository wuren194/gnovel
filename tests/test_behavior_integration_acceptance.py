"""Verify rule delivery through real preparation and bounded public reading."""
from pathlib import Path
import tempfile
import unittest

from scripts.core.operations import call_operation
from scripts.core.prompt_plan import resolve_prompt_plan
from tests.fixtures_workflow import review_ready_project


class BehaviorRuleDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.chapter = review_ready_project(self.root, chapter_count=1)['chapterIds'][0]

    def test_editor_public_reading_delivers_behavior_seats_and_fact_handoff(self):
        prepared = call_operation('task.prepare', self.root, {
            'task': 'editor', 'chapterId': self.chapter, 'view': 'model'})
        self.assertTrue(prepared['ok'], prepared)
        wanted = {'shared/contracts/G-CAUSAL.md', 'shared/contracts/CHAPTER-PIPELINE.md',
                  'shared/contracts/G-MULTI-IDENTITY-REVIEW.md'}
        selected = [row for row in prepared['contractReads'] if row['path'] in wanted]
        self.assertEqual({row['path'] for row in selected}, wanted)
        arguments = {'credential': prepared['reading']['credential'],
                     'readIds': [rid for row in selected for rid in row['readIds']], 'maxBytes': 6000}
        texts, cursor = [], None
        while True:
            page = call_operation('task.read-batch', self.root,
                                  {**arguments, **({'cursor': cursor} if cursor else {})})
            texts.extend(row['text'] for row in page['items'])
            if not page['hasMore']:
                break
            cursor = page['nextCursor']
        delivered = ''.join(texts)
        for rule in ('未知不是没有', 'B 独立重扫当前正文', 'factAssessment',
                     '事实盘点', '普通住宅、自愿做饭', '公开行为另查谁能发现'):
            with self.subTest(rule=rule):
                self.assertIn(rule, delivered)

    def test_composer_receives_existing_life_baseline_without_editor_handoff(self):
        plan = resolve_prompt_plan(self.root, 'gnovel-composer', view='station')
        causal = next(row for row in plan['contracts'] if row['path'] == 'shared/contracts/G-CAUSAL.md')
        self.assertEqual([row['slice'] for row in causal['readRanges']], ['life-baseline'])
        from scripts.core.project import TOOLBOX_ROOT
        lines = (TOOLBOX_ROOT / causal['path']).read_text().splitlines(keepends=True)
        delivered = ''.join(''.join(lines[row['startLine'] - 1:row['endLine']]) for row in causal['readRanges'])
        self.assertIn('职业印象不是事实，未知不是没有', delivered)
        self.assertNotIn('CHAPTER-PIPELINE.md', {Path(row['path']).name for row in plan['contracts']})


if __name__ == '__main__':
    unittest.main()
