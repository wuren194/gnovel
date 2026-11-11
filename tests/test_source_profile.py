"""Reminder weights never rewrite personality or become scene/action probabilities."""
from copy import deepcopy
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.operations import call_operation
from scripts.core.source_profile import _draw, query_profile, sample_profile, task_source_actions
from scripts.core.structure_store import StructureError, load_structure, validate_structure
from tests.fixtures_source import source_fixture


class SourceProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = source_fixture(self.root)
        for rid, topic, text in (
                ('fact-COMPETITIVE', 'trait', '好胜，也能真心欣赏别人做得更好的地方；两面共同存在。'),
                ('fact-OLD-ITEMS', 'preference', '喜欢保留用惯的旧物，对细小磨损很熟悉。'),
                ('fact-DIRECT', 'tendency', '说话直接，对措辞分寸的把握并不总是及时。')):
            self.source['fragments'].append({'fragmentId': rid, 'ownerId': 'character-ALPHA',
                'text': text, 'order': len(self.source['fragments']) + 1, 'contentRole': 'fact',
                'topic': topic, 'contentBindings': ['attribute-LIFE']})
        self.pool = {'poolId': 'pool-PERSONALITY', 'characterId': 'character-ALPHA', 'count': 2,
                     'facets': [{'fragmentId': 'fact-COMPETITIVE', 'weight': 2},
                                {'fragmentId': 'fact-OLD-ITEMS', 'weight': 3},
                                {'fragmentId': 'fact-DIRECT', 'weight': 1}]}
        self.source['recallPools'].append(self.pool)
        self.save()

    def save(self):
        validate_structure(self.source)
        write_json(self.root / '.govel/structure.json', self.source)

    def sample(self, request='writing-1', pool='pool-PERSONALITY'):
        return sample_profile(self.root, poolId=pool, requestId=request)

    def test_same_request_reuses_the_actual_receipt_and_never_draws_again(self):
        before = (self.root / '.govel/structure.json').read_bytes()
        result = self.sample()
        self.assertFalse(result['replayed'])
        with patch('scripts.core.source_profile._draw', side_effect=AssertionError('must not reroll')):
            replay = self.sample()
        self.assertTrue(replay['replayed'])
        self.assertEqual(result['sampleId'], replay['sampleId'])
        self.assertEqual(result['facets'], replay['facets'])
        self.assertEqual(len(result['facets']), 2)
        self.assertEqual(len({row['fragmentId'] for row in result['facets']}), 2)
        self.assertEqual((self.root / '.govel/structure.json').read_bytes(), before)
        self.assertNotIn('weight', str(result))
        self.assertFalse(result['changesPersonality'])

    def test_content_or_weight_drift_refuses_replay_instead_of_silently_rerolling(self):
        result = self.sample()
        self.source['recallPools'][0]['facets'][0]['weight'] = 4
        self.save()
        with patch('scripts.core.source_profile._draw', side_effect=AssertionError('must not reroll')):
            with self.assertRaises(StructureError) as caught:
                self.sample()
        self.assertEqual(caught.exception.code, 'SOURCE_SAMPLE_STALE')
        self.assertTrue((self.root / '.govel/profile-samples' / (result['sampleId'] + '.json')).is_file())
        self.sample('new-writing-task')

    def test_unrelated_content_change_does_not_invalidate_the_sample(self):
        initial = self.sample()
        self.source['entities'][1]['name'] = '旁观者'
        self.save()
        replay = self.sample()
        self.assertEqual(replay['facets'], initial['facets'])
        self.assertTrue(replay['replayed'])

    def test_request_cannot_be_reused_for_another_pool(self):
        self.source['recallPools'].append({**deepcopy(self.pool), 'poolId': 'pool-SECOND'})
        self.save()
        self.sample()
        with self.assertRaises(StructureError) as caught:
            self.sample(pool='pool-SECOND')
        self.assertEqual(caught.exception.code, 'SOURCE_SAMPLE_REQUEST_REUSED')

    def test_receipt_tampering_is_reported_and_does_not_generate_another_sample(self):
        result = self.sample()
        path = self.root / '.govel/profile-samples' / (result['sampleId'] + '.json')
        receipt = read_json(path)
        receipt['selectedIds'] = []
        write_json(path, receipt)
        with self.assertRaises(StructureError) as caught:
            self.sample()
        self.assertEqual(caught.exception.code, 'SOURCE_SAMPLE_INVALID')

    def test_full_profile_still_contains_all_facets_after_a_small_sample(self):
        sampled = self.sample()
        profile = query_profile(self.root, characterId='character-ALPHA')
        self.assertEqual(len(profile['items']), 3)
        self.assertGreater(len(profile['items']), len(sampled['facets']))
        competitive = next(row for row in profile['items'] if row['fragmentId'] == 'fact-COMPETITIVE')
        self.assertIn('也能真心欣赏', competitive['text'])

    def test_only_established_long_term_facets_are_eligible(self):
        for field, value in (('topic', 'goal'), ('topic', 'priority'), ('topic', 'voice'),
                             ('contentRole', 'plan'), ('ownerId', 'character-BETA'), ('status', 'archived')):
            with self.subTest(field=field, value=value):
                source = deepcopy(self.source)
                next(row for row in source['fragments'] if row['fragmentId'] == 'fact-COMPETITIVE')[field] = value
                with self.assertRaises(StructureError):
                    validate_structure(source)

    def test_temporary_state_requires_a_story_interval_and_is_not_in_the_pool(self):
        row = deepcopy(self.source['fragments'][-1])
        row.update(fragmentId='fact-FATIGUED', topic='state', text='这几日容易困倦。')
        self.source['fragments'].append(row)
        with self.assertRaises(StructureError):
            self.save()
        row['validDuring'] = {'fromId': 'time-START', 'toId': 'time-LATER'}
        self.save()
        self.assertEqual(len(query_profile(self.root, characterId='character-ALPHA')['items']), 4)
        self.assertNotIn('fact-FATIGUED', {row['fragmentId'] for row in self.sample()['facets']})

    def test_pool_input_boundaries_reject_invalid_weights_and_duplicates(self):
        for value in (0, -1, True, float('inf'), float('nan'), '3'):
            with self.subTest(weight=value):
                source = deepcopy(self.source)
                source['recallPools'][0]['facets'][0]['weight'] = value
                with self.assertRaises(StructureError):
                    validate_structure(source)
        source = deepcopy(self.source)
        source['recallPools'][0]['facets'].append(source['recallPools'][0]['facets'][0])
        with self.assertRaises(StructureError):
            validate_structure(source)

    def test_fixed_weights_control_reminder_frequency_without_replacement(self):
        pool = {'count': 1, 'facets': [{'fragmentId': 'fact-A', 'weight': 9}, {'fragmentId': 'fact-B', 'weight': 1}]}
        with patch('scripts.core.source_profile.random.SystemRandom', return_value=random.Random(814)):
            samples = [_draw(pool)[0] for _ in range(1000)]
        self.assertGreater(samples.count('fact-A'), 850)
        self.assertLess(samples.count('fact-A'), 950)
        pool['count'] = 2
        self.assertEqual(set(_draw(pool)), {'fact-A', 'fact-B'})

    def test_weight_scaling_handles_large_finite_weights(self):
        pool = {'count': 2, 'facets': [{'fragmentId': 'fact-A', 'weight': 1e308}, {'fragmentId': 'fact-B', 'weight': 1e308}]}
        self.assertEqual(set(_draw(pool)), {'fact-A', 'fact-B'})

    def test_public_schema_has_no_scene_or_action_probability_parameter(self):
        from scripts.core.operations import operations
        spec = next(row for row in operations() if row.id == 'source.profile-sample')
        self.assertEqual(set(spec.input_schema['properties']), {'projectRoot', 'poolId', 'requestId'})
        with self.assertRaises(StructureError):
            call_operation('source.profile-sample', self.root, {'poolId': self.pool['poolId'],
                           'requestId': 'scene-injection', 'scene': {'mood': '紧张'}})

    def test_task_preparation_offers_explicit_samples_without_writing_them(self):
        from tests.fixtures_source import single_source_chapter
        chapter = single_source_chapter(self.source, 'chapter-RECALL', volume_id=self.source['volumes'][0]['volumeId'])
        self.source['chapters'].append(chapter)
        self.source['chapterArcLinks'].append({'chapterId': chapter['chapterId'],
            'arcId': self.source['arcs'][0]['arcId'], 'role': 'primary'})
        self.source['references'].append({'sourceId': chapter['chapterId'], 'targetId': 'character-ALPHA', 'kind': 'appears_in'})
        self.save()
        actions = task_source_actions(self.source, chapter, 'writer')
        self.assertEqual(actions['profileRecall'][0]['operation'], 'source.profile-sample')
        self.assertFalse((self.root / '.govel/profile-samples').exists())
        result = call_operation(actions['profileRecall'][0]['operation'], self.root, actions['profileRecall'][0]['arguments'])
        self.assertTrue(result['sampleId'].startswith('sample-'))


if __name__ == '__main__':
    unittest.main()
