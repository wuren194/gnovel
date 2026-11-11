from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core import catalog_commands as catalog
from scripts.core.entity_registry import PREFIX_BY_TYPE
from scripts.core.jsonio import read_json, write_json
from scripts.core.structure_commands import commit_plan, undo_operation
from scripts.core.structure_store import StructureError, load_structure
from tests.fixtures_current import create_current_project


class CatalogCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_current_project(self.root)
        self.volume = self.fixture["structure"]["volumes"][0]["volumeId"]
        self.arc = self.fixture["structure"]["arcs"][0]["arcId"]

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=True)

    def assertCode(self, code, function, **kwargs):
        with self.assertRaises(StructureError) as result:
            function(self.root, **kwargs)
        self.assertEqual(result.exception.code, code)

    def test_every_entity_type_has_typed_id_and_undo(self):
        for kind, prefix in PREFIX_BY_TYPE.items():
            if kind == "time_anchor":
                continue
            with self.subTest(kind=kind):
                before = load_structure(self.root)
                plan = catalog.preview_create_entity(self.root, entity_type=kind, name="测试" + kind, aliases=["别称" + kind])
                self.assertEqual(load_structure(self.root), before)
                entity_id = plan["parameters"]["entityId"]
                self.assertTrue(entity_id.startswith(prefix + "-"))
                self.commit(plan)
                entity = next(e for e in load_structure(self.root)["entities"] if e["entityId"] == entity_id)
                self.assertEqual(entity["entityType"], kind)
                undo_operation(self.root, plan["operationId"])
                self.assertEqual(load_structure(self.root)["entities"], before["entities"])

    def test_reviewed_entity_archive_keeps_identity_references_and_authored_text(self):
        from scripts.core.canon_proposals import resolve_canon_target, preview_canon_change
        before = load_structure(self.root)
        hook = self.fixture['hookId']
        before['fieldDefinitions'].append({'fieldId': 'field-ARCHIVE-FACT', 'key': 'hook.result',
            'label': '结果', 'valueType': 'topic', 'appliesTo': ['hook']})
        before['attributes'].append({'attributeId': 'attribute-ARCHIVE-FACT', 'ownerId': hook,
            'fieldId': 'field-ARCHIVE-FACT', 'values': []})
        before['fragments'].append({'fragmentId': 'fact-ARCHIVE-FACT', 'ownerId': hook, 'order': 1,
            'contentRole': 'fact', 'contentBindings': ['attribute-ARCHIVE-FACT'], 'text': '钥匙秘密已经解决。'})
        before['fragments'][0]['dependsOn'] = [hook]
        write_json(self.root/'.govel/structure.json', before)
        note = self.root/'工作台/作者资料.md'; note.write_text('作者保留的原稿，不是运行时设定。')
        prose = {c['filePath']: (self.root/c['filePath']).read_bytes() for c in before['chapters']}
        plan = catalog.preview_archive_entity(self.root, selector=hook)
        self.assertEqual(plan['operation'], 'refactor_archive_entity')
        self.assertEqual(plan['impact']['approvalInvalidations'], [self.fixture['chapterIds'][0]])
        self.commit(plan)
        after = load_structure(self.root)
        self.assertEqual(after['references'], before['references'])
        self.assertEqual(note.read_text(), '作者保留的原稿，不是运行时设定。')
        self.assertEqual(prose, {path: (self.root/path).read_bytes() for path in prose})
        self.assertEqual(next(e for e in after['entities'] if e['entityId'] == hook)['status'], 'archived')


    def test_time_anchor_archive_preserves_registered_mounts_and_reports_them_on_read(self):
        structure = load_structure(self.root)
        structure['entities'].append({'entityId': 'time-TEST-0001', 'entityType': 'time_anchor',
                                     'name': '测试锚点', 'absoluteTime': '2026-01-01'})
        structure['chapters'][0]['storyTimeStartId'] = 'time-TEST-0001'
        structure['entities'][0]['timeStartId'] = 'time-TEST-0001'
        write_json(self.root/'.govel/structure.json', structure)
        plan = catalog.preview_archive_entity(self.root, selector='time-TEST-0001')
        self.commit(plan)
        source = load_structure(self.root)
        self.assertEqual(source['chapters'][0]['storyTimeStartId'], 'time-TEST-0001')
        self.assertEqual(source['entities'][0]['timeStartId'], 'time-TEST-0001')
        self.assertEqual(next(e for e in source['entities'] if e['entityId'] == 'time-TEST-0001')['status'], 'archived')
        from scripts.core.commands import story_timeline
        self.assertIn('ARCHIVED_ANCHOR_REFERENCED', {r['code'] for r in story_timeline(self.root)['problems']})



    def test_invalid_entity_data_fails_before_plan(self):
        from scripts.core.refactor import preview_rename_entity
        for name in ('', None, [], 3):
            with self.subTest(name=name), self.assertRaises(StructureError):
                preview_rename_entity(self.root, selector=self.fixture['characterId'],
                                      new_name=name, keep_old_name_as_alias=True)
        self.assertCode("STRUCTURE_INVALID", catalog.preview_create_entity, entity_type="character", name="沈砚")
        self.assertCode("ENTITY_TYPE_INVALID", catalog.preview_create_entity, entity_type="unknown", name="新人物")
        self.assertFalse(list((self.root / ".govel/plans").iterdir()))

    def test_visibility_only_changes_supported_planning_metadata(self):
        from scripts.core.catalog_api import call_catalog_operation
        before = (self.root/'.govel/structure.json').read_bytes()
        for collection, record_id in [('entities', self.fixture['characterId']), ('relations', 'relation-UNKNOWN')]:
            self.assertCode('VISIBILITY_COLLECTION_INVALID', catalog.preview_set_visibility,
                            collection=collection, record_id=record_id, visibility='public')
            with self.assertRaises(StructureError) as caught:
                call_catalog_operation('preview_set_visibility', self.root,
                    {'collection': collection, 'recordId': record_id, 'visibility': 'public'}, actor='test')
            self.assertEqual(caught.exception.code, 'ARGUMENTS_INVALID')
        self.assertEqual((self.root/'.govel/structure.json').read_bytes(), before)
        cid = self.fixture['chapterIds'][0]
        original = load_structure(self.root)['chapters'][0]
        plan = catalog.preview_set_visibility(self.root, collection='chapters', record_id=cid,
                                              visible_to=[self.fixture['characterId']])
        self.commit(plan)
        changed = load_structure(self.root)['chapters'][0]
        self.assertEqual(changed['visibleTo'], [self.fixture['characterId']])
        self.assertEqual({k: v for k, v in changed.items() if k != 'visibleTo'}, original)

    def test_entity_display_refactor_preserves_identity_and_semantic_fields(self):
        from scripts.core.refactor import preview_rename_entity
        before = load_structure(self.root)
        plan = preview_rename_entity(self.root, selector=self.fixture['characterId'],
                                     new_name='新名字', keep_old_name_as_alias=True)
        self.commit(plan)
        entity = next(e for e in load_structure(self.root)['entities'] if e['entityId'] == self.fixture['characterId'])
        original = next(e for e in before['entities'] if e['entityId'] == self.fixture['characterId'])
        self.assertEqual(entity['name'], '新名字')
        self.assertIn(original['name'], entity['aliases'])
        for key in set(original)-{'name', 'aliases'}:
            self.assertEqual(entity[key], original[key])


    def test_volume_arc_membership_and_archive_lifecycle(self):
        volume_plan = catalog.preview_create_volume(self.root, title="第二卷")
        volume_id = volume_plan["parameters"]["volumeId"]
        self.commit(volume_plan)
        arc_plan = catalog.preview_create_arc(self.root, title="支线", volume_ids=[volume_id], kind="subplot", promise="找到来信")
        arc_id = arc_plan["parameters"]["arcId"]
        self.commit(arc_plan)
        self.commit(catalog.preview_update_arc(self.root, arc_id=arc_id, changes={"title": "来信支线", "promise": "核实署名"}))
        self.commit(catalog.preview_update_volume(self.root, volume_id=volume_id, changes={"title": "第二卷·来信"}))
        chapter_id = self.fixture["chapterIds"][0]
        prose = self.root / f"正文/{chapter_id}.md"
        original = prose.read_bytes()
        self.commit(catalog.preview_relink_chapter(self.root, chapter_id=chapter_id, volume_id=volume_id, primary_arc_id=arc_id))
        self.assertEqual(prose.read_bytes(), original)
        self.assertCode("ARCHIVED_TARGET", catalog.preview_archive_arc, arc_id=arc_id)
        self.assertCode("ARCHIVED_TARGET", catalog.preview_archive_volume, volume_id=volume_id)
        self.commit(catalog.preview_relink_chapter(self.root, chapter_id=chapter_id, volume_id=self.volume, primary_arc_id=self.arc))
        self.commit(catalog.preview_archive_arc(self.root, arc_id=arc_id))
        self.commit(catalog.preview_archive_volume(self.root, volume_id=volume_id))
        self.assertEqual(next(v for v in load_structure(self.root)["volumes"] if v["volumeId"] == volume_id)["status"], "archived")

    def test_relink_requires_active_in_scope_arcs(self):
        self.assertCode("ARC_ROLE_CONFLICT", catalog.preview_relink_chapter, chapter_id=self.fixture["chapterIds"][0], volume_id=self.volume, primary_arc_id=self.arc, secondary_arc_ids=[self.arc])
        self.assertCode("ARC_NOT_FOUND", catalog.preview_relink_chapter, chapter_id=self.fixture["chapterIds"][0], volume_id=self.volume, primary_arc_id="arc-NOT-FOUND")
        plan = catalog.preview_create_volume(self.root, title="第二卷")
        self.commit(plan)
        self.assertCode("ARC_VOLUME_MISMATCH", catalog.preview_relink_chapter, chapter_id=self.fixture["chapterIds"][0], volume_id=plan["parameters"]["volumeId"], primary_arc_id=self.arc)

    def test_catalog_rejects_cycles_unknown_targets_and_identity_changes(self):
        self.assertCode("ARC_PARENT_CYCLE", catalog.preview_update_arc, arc_id=self.arc, changes={"parentArcId": self.arc})
        self.assertCode("CHAPTER_NOT_FOUND", catalog.preview_update_arc, arc_id=self.arc, changes={"resolutionTargetChapterId": "chapter-NOT-FOUND"})
        self.assertCode("VOLUME_NOT_FOUND", catalog.preview_create_arc, title="支线", volume_ids=["volume-NOT-FOUND"])
        self.assertCode("FIELD_FORBIDDEN", catalog.preview_update_volume, volume_id=self.volume, changes={"volumeId": "volume-NEW-ID"})
        self.assertCode("STATUS_INVALID", catalog.preview_update_arc, arc_id=self.arc, changes={"status": "not-a-status"})
        plan = catalog.preview_create_arc(self.root, title="子线", volume_ids=[self.volume], parent_arc_id=self.arc)
        self.commit(plan)
        self.assertCode("ARC_PARENT_CYCLE", catalog.preview_update_arc, arc_id=self.arc, changes={"parentArcId": plan["parameters"]["arcId"]})

    def test_preview_hash_approval_revision_and_pending_guards(self):
        plan = catalog.preview_create_entity(self.root, entity_type="location", name="新地点")
        self.assertCode("APPROVAL_REQUIRED", commit_plan, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=False)
        self.assertCode("PLAN_HASH_MISMATCH", commit_plan, operation_id=plan["operationId"], plan_hash="bad", approved=True)
        self.commit(catalog.preview_create_volume(self.root, title="第二卷"))
        self.assertCode("REVISION_CONFLICT", commit_plan, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=True)
        write_json(self.root / ".govel/transactions/TX-pending.json", {})
        self.assertCode("TRANSACTION_INCOMPLETE", catalog.preview_create_entity, entity_type="character", name="新人物")

    def test_symlink_plan_directory_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / ".govel/plans").rmdir()
        (self.root / ".govel/plans").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StructureError):
            catalog.preview_create_volume(self.root, title="第二卷")
        self.assertFalse(list(outside.iterdir()))


class CurrentPlacementCatalogTests(unittest.TestCase):
    """Current source placement, scope and arc constraints share the catalog transaction boundary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = create_current_project(self.root, chapter_count=1)
        source = load_structure(self.root)
        self.volume = source['volumes'][0]['volumeId']
        self.arc = source['arcs'][0]['arcId']
        self.chapter = self.fixture['chapterIds'][0]

    def commit(self, plan):
        return commit_plan(self.root, operation_id=plan["operationId"], plan_hash=plan["planHash"], approved=True)

    def chapter_record(self):
        return next(c for c in load_structure(self.root)['chapters'] if c['chapterId'] == self.chapter)

    def global_arc(self):
        plan = catalog.preview_create_arc(self.root, title="全局线", volume_ids=[])
        self.commit(plan)
        return plan["parameters"]["arcId"]

    def test_volume_chapter_placement_via_placement_argument(self):
        self.commit(catalog.preview_relink_chapter(self.root, chapter_id=self.chapter,
                    placement={'scope': 'volume', 'volumeId': self.volume}, primary_arc_id=self.arc))
        self.assertEqual(self.chapter_record()['placement'], {'scope': 'volume', 'volumeId': self.volume})
        self.assertNotIn('volumeId', self.chapter_record())

    def test_volume_argument_maps_to_current_placement(self):
        self.commit(catalog.preview_relink_chapter(self.root, chapter_id=self.chapter,
                    volume_id=self.volume, primary_arc_id=self.arc))
        self.assertEqual(self.chapter_record()['placement'], {'scope': 'volume', 'volumeId': self.volume})

    def test_global_arc_allows_project_chapter(self):
        arc = self.global_arc()
        self.commit(catalog.preview_relink_chapter(self.root, chapter_id=self.chapter,
                    placement={'scope': 'project'}, primary_arc_id=arc))
        self.assertEqual(self.chapter_record()['placement'], {'scope': 'project'})

    def test_volume_only_arc_rejected_for_project_chapter(self):
        with self.assertRaises(StructureError) as caught:
            catalog.preview_relink_chapter(self.root, chapter_id=self.chapter,
                                           placement={'scope': 'project'}, primary_arc_id=self.arc)
        self.assertEqual(caught.exception.code, 'ARC_VOLUME_MISMATCH')

    def test_relink_rejects_both_or_neither_placement_argument(self):
        with self.assertRaises(StructureError) as caught:
            catalog.preview_relink_chapter(self.root, chapter_id=self.chapter, primary_arc_id=self.arc,
                                           volume_id=self.volume, placement={'scope': 'project'})
        self.assertEqual(caught.exception.code, 'PLACEMENT_ARGUMENT_INVALID')

    def test_volume_archive_constraint_with_active_chapter(self):
        with self.assertRaises(StructureError) as caught:
            catalog.preview_archive_volume(self.root, volume_id=self.volume)
        self.assertEqual(caught.exception.code, 'ARCHIVED_TARGET')


if __name__ == "__main__":
    unittest.main()
