from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.structure_commands import commit_plan
from scripts.core.structure_store import StructureError, load_structure
from scripts.core.work_voice_profile import AXES, preview_voice_profile, query_voice_profile
from tests.fixtures_source import source_fixture


class WorkVoiceProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source_fixture(self.root)

    def commit(self, plan):
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def test_draft_approval_and_edit_invalidation(self):
        axes = {key: '适度变化' for key in AXES}
        plan = preview_voice_profile(self.root, action='set', expectedRevision=load_structure(self.root)['revision'],
                                     reason='建立作品叙述风格', axes=axes, examples=['章节例证'], applicability='全书')
        self.commit(plan)
        self.assertEqual(query_voice_profile(self.root)['epistemicRole'], 'unapproved_candidate')
        with self.assertRaises(StructureError):
            preview_voice_profile(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                  reason='缺少批准')
        plan = preview_voice_profile(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                     reason='作者确认', approvalActor='author', approvalEvidence='审阅具体版本')
        self.commit(plan)
        approved = query_voice_profile(self.root)
        self.assertEqual(approved['epistemicRole'], 'approved_plan')
        self.assertEqual(approved['profileHash'], approved['profile']['approval']['profileHash'])
        axes['emotionLabeling'] = '减少直接标签'
        plan = preview_voice_profile(self.root, action='set', expectedRevision=load_structure(self.root)['revision'],
                                     reason='调整用词', axes=axes, examples=['新例证'], applicability='全书')
        self.commit(plan)
        self.assertEqual(query_voice_profile(self.root)['epistemicRole'], 'unapproved_candidate')


class WorkVoiceProfileContextTests(unittest.TestCase):
    """作品声纹在统一 Context Builder 中的地位：approved_plan、hash 绑定、与角色声纹分离。"""

    AXES = {key: '适度变化' for key in AXES}

    def setUp(self):
        from tests.test_context_builder import context_source
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / '正文').mkdir()
        context_source(self.root)

    def commit(self, plan):
        commit_plan(self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'], approved=True)

    def set_profile(self, axes=None, examples=('章节例证',)):
        plan = preview_voice_profile(self.root, action='set', expectedRevision=load_structure(self.root)['revision'],
                                     reason='建立作品叙述风格', axes=axes or dict(self.AXES),
                                     examples=list(examples), applicability='全书')
        self.commit(plan)

    def approve_profile(self):
        plan = preview_voice_profile(self.root, action='approve', expectedRevision=load_structure(self.root)['revision'],
                                     reason='作者确认', approvalActor='author', approvalEvidence='审阅具体版本')
        self.commit(plan)

    def build(self, **kwargs):
        from scripts.core.context_builder import build_station_context
        kwargs.setdefault('task_type', 'writer')
        kwargs.setdefault('view', 'station')
        kwargs.setdefault('read_only', True)
        return build_station_context(str(self.root), 'chapter-AAA', **kwargs)

    def layers(self, context):
        return {row['role']: row for row in context['layers']}

    def test_draft_profile_not_loaded_as_active_context(self):
        self.set_profile()
        context = self.build()
        self.assertNotIn('workVoiceProfile', context)
        self.assertNotIn('voiceProfileHash', context['dependencyManifest'])
        self.assertEqual(self.layers(context)['WORK VOICE PROFILE']['items'], [])

    def test_approved_profile_loaded_as_work_voice_profile(self):
        self.set_profile()
        self.approve_profile()
        context = self.build()
        self.assertEqual(context['workVoiceProfile']['role'], 'WORK VOICE PROFILE')
        self.assertEqual(context['workVoiceProfile']['axes'], self.AXES)
        layer = self.layers(context)['WORK VOICE PROFILE']
        self.assertFalse(layer['authoritative'] is False)  # approved_plan 是权威规划，不是 Canon
        self.assertNotIn(layer['epistemicRole'], {'canon_fact', 'in_world_belief', 'in_world_text'})
        self.assertEqual(layer['epistemicRole'], 'approved_plan')
        self.assertIn('voiceProfileHash', context['dependencyManifest'])

    def test_profile_hash_is_in_dependency_manifest(self):
        self.set_profile()
        self.approve_profile()
        approved = query_voice_profile(self.root)
        context = self.build()
        self.assertEqual(context['dependencyManifest']['voiceProfileHash'],
                         approved['profile']['approval']['profileHash'])
        self.assertEqual(self.layers(context)['WORK VOICE PROFILE']['items'],
                         [{'profileHash': approved['profile']['approval']['profileHash']}])

    def test_editing_profile_invalidates_approval(self):
        self.set_profile()
        self.approve_profile()
        self.assertEqual(query_voice_profile(self.root)['epistemicRole'], 'approved_plan')
        axes = dict(self.AXES); axes['emotionLabeling'] = '减少直接标签'
        self.set_profile(axes=axes, examples=('新例证',))
        profile = query_voice_profile(self.root)
        self.assertEqual(profile['epistemicRole'], 'unapproved_candidate')
        self.assertEqual(profile['profile']['status'], 'draft')
        self.assertNotIn('approval', profile['profile'])

    def test_editing_profile_stales_existing_context(self):
        self.set_profile()
        self.approve_profile()
        before = self.build()
        axes = dict(self.AXES); axes['somaticExpression'] = '更多身体反应'
        self.set_profile(axes=axes, examples=('新例证',))
        after = self.build()
        self.assertNotEqual(before['contextHash'], after['contextHash'])
        self.assertNotIn('voiceProfileHash', after['dependencyManifest'])
        self.assertNotEqual(before['dependencyManifest']['voiceProfileHash'],
                            after['dependencyManifest'].get('voiceProfileHash'))

    def test_reapproval_produces_new_context_hash(self):
        self.set_profile()
        self.approve_profile()
        first = self.build()
        axes = dict(self.AXES); axes['sceneTransition'] = '更硬的转场'
        self.set_profile(axes=axes, examples=('改后的例证',))
        self.approve_profile()
        second = self.build()
        self.assertEqual(second['workVoiceProfile']['axes']['sceneTransition'], '更硬的转场')
        self.assertNotEqual(first['dependencyManifest']['voiceProfileHash'],
                            second['dependencyManifest']['voiceProfileHash'])
        self.assertNotEqual(first['contextHash'], second['contextHash'])

    def test_verified_human_remains_false(self):
        self.set_profile()
        self.approve_profile()
        self.assertIs(query_voice_profile(self.root)['profile']['approval']['verifiedHuman'], False)

    def test_voice_is_project_wide_planning_input(self):
        from scripts.core.source_plan import chapter_plan
        from scripts.core.source_impact import source_impact
        before = load_structure(self.root)
        self.set_profile()
        draft = load_structure(self.root)
        self.assertEqual(source_impact(before, draft)['approvalInvalidations'], [])
        self.approve_profile()
        active = load_structure(self.root)
        chapters = sorted(row['chapterId'] for row in active['chapters'])
        self.assertEqual(source_impact(draft, active)['approvalInvalidations'], chapters)
        for cid in chapters:
            self.assertNotEqual(chapter_plan(active, cid)['sourcePlanHash'],
                                chapter_plan(draft, cid)['sourcePlanHash'])
        self.set_profile(examples=('替换后待审文风',))
        revised = load_structure(self.root)
        self.assertEqual(source_impact(active, revised)['approvalInvalidations'], chapters)

    def test_character_voice_and_work_voice_remain_separate(self):
        source = load_structure(self.root)
        source['entities'][0].setdefault('voiceProfile', {})['bannedPhrasesForRole'] = ['该死']
        from tests.test_context_builder import save_context_source
        save_context_source(self.root, source)
        self.set_profile()
        self.approve_profile()
        context = self.build()
        # 作品声纹来自 workVoiceProfile；角色声纹留在实体上，且不进入 Works Voice 层。
        self.assertEqual(context['workVoiceProfile']['role'], 'WORK VOICE PROFILE')
        self.assertNotIn('voiceProfile', context['workVoiceProfile'])
        self.assertNotEqual(load_structure(self.root)['entities'][0]['voiceProfile'], self.AXES)
        self.assertNotIn('bannedPhrasesForRole', str(self.layers(context)['WORK VOICE PROFILE']))

    def test_draft_profile_is_not_voice_guidance_for_any_station(self):
        self.set_profile()
        for task in ('planner', 'writer', 'editor'):
            with self.subTest(task=task):
                context = self.build(task_type=task)
                self.assertNotIn('workVoiceProfile', context)
                self.assertEqual(self.layers(context)['WORK VOICE PROFILE']['items'], [])


if __name__ == '__main__':
    unittest.main()
