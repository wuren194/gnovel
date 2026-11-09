"""A repair first page pairs an actionable issue with its shared prose window."""
from __future__ import annotations

import json
import unittest

from scripts.core.project import query_policy
from scripts.core.query import _size
from scripts.core.repair_windows import repair_materials
from scripts.core.text_pages import text_page


class RepairFirstScreenTests(unittest.TestCase):
    def test_many_issues_cannot_push_all_prose_past_the_first_page(self):
        body = '前文。' * 100 + '待修句。' + '后文。' * 100
        start = body.index('待修句。')
        rows = [{'id': f'issue-{index}', 'source': 'diagnostics',
                 'finding': {'ruleId': f'rule-{index}', 'severity': 'warning',
                             'message': f'第 {index} 条独立问题需要修订。', 'raw': '完整证据' * 2000},
                 'location': {'state': 'unique', 'occurrences': 1,
                              'spans': [{'start': start, 'end': start + len('待修句。'),
                                         'startLine': 1, 'endLine': 1}]}}
                for index in range(60)]
        materials = repair_materials(body, rows, [])
        policy = query_policy()
        page = text_page({'ok': True}, materials, budget=policy['maxBytes']['detail'],
                         limit=policy['defaultLimit'], cursor=None, binding='selection', source='version', key='items')
        first = page['items'][0]
        self.assertEqual(first['source'], 'issuePreview')
        self.assertIn('第 0 条独立问题', first['finding']['summary'])
        window = next(item for item in page['items'] if item['id'] in first['windowIds'])
        self.assertEqual(window['source'], 'contextWindow')
        self.assertIn('待修句。', window['text'])
        self.assertEqual(window['text'], body[window['fileStart'] + window['start']:window['fileStart'] + window['end']])
        self.assertLessEqual(_size(page), policy['maxBytes']['detail'])
        self.assertEqual(sum(row['source'] == 'contextWindow' for row in materials), 1)
        originals = [json.loads(row['text']) for row in materials if row['source'] == 'diagnostics']
        self.assertEqual([row['finding'] for row in originals], [row['finding'] for row in rows])
        self.assertFalse(page['coverage']['semanticReviewComplete'])

    def test_empty_location_and_window_do_not_mint_edit_anchors(self):
        row = {'id': 'empty', 'source': 'review', 'finding': {'summary': '核对空行'},
               'location': {'state': 'line', 'occurrences': 1,
                            'spans': [{'start': 0, 'end': 0, 'startLine': 1, 'endLine': 1}]}}
        def anchor(*args):
            self.fail('empty ranges cannot mint edit anchors')
        materials = repair_materials('', [row], [], anchor=anchor)
        preview, window, original = materials
        self.assertEqual(preview['issueId'], row['id'])
        self.assertEqual(preview['windowIds'], [window['id']])
        self.assertNotIn('anchorRef', preview['location']['spans'][0])
        self.assertEqual(window['text'], '')
        self.assertNotIn('anchorRef', window)
        self.assertEqual(json.loads(original['text'])['finding'], row['finding'])

    def test_missing_locations_stay_explicit_without_starving_located_issues(self):
        body = '准确位置。'
        missing = {'id': 'missing', 'source': 'review', 'finding': {'summary': '全章核对'},
                   'location': {'state': 'missing', 'occurrences': 0, 'spans': []}}
        located = {'id': 'located', 'source': 'review', 'finding': {'summary': '局部核对'},
                   'location': {'state': 'unique', 'occurrences': 1,
                                'spans': [{'start': 0, 'end': len(body), 'startLine': 1, 'endLine': 1}]}}
        materials = repair_materials(body, [missing, located], [])
        self.assertEqual(materials[0]['issueId'], 'located')
        self.assertEqual(materials[1]['source'], 'contextWindow')
        unknown = next(row for row in materials if row.get('issueId') == 'missing')
        self.assertEqual(unknown['windowIds'], [])
        self.assertEqual(unknown['location']['state'], 'missing')
        self.assertEqual(unknown['location']['spans'], [])


if __name__ == '__main__':
    unittest.main()
