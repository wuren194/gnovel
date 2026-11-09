"""Identity invariance under every supported authoring refactor.

The contract under test: `Identity != Display Name != Ordinal != File Name !=
File Path != Project Folder`. A synthetic schema-3 project is renamed, relocated,
reordered, reparented, archived and restored, and after every step the set of
registered stable IDs must be exactly the set it started as.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.jsonio import read_json
from scripts.core.project_doctor import assess_project
from scripts.core.refactor import (commit_refactor, operation_contract, preview_alias_entity,
                                   preview_archive_chapter, preview_archive_entity, preview_rename_arc,
                                   preview_rename_book, preview_rename_chapter, preview_rename_entity,
                                   preview_rename_unit, preview_rename_volume, preview_reorder_chapter,
                                   preview_reparent_chapter, preview_reparent_unit,
                                   preview_relocate_chapter_paths, preview_restore_chapter,
                                   preview_restore_entity, refactor_reference_candidates)
from scripts.core.source_placement import project_placement
from scripts.core.structure_store import load_structure
from tests.fixtures_refactor import identity_snapshot, refactor_project


def _commit(root: Path, preview: dict) -> dict:
    return commit_refactor(root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                           approved=True, actor='test')


class RefactorIdentityContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '书'
        self.info = refactor_project(self.root)
        self.identities = identity_snapshot(load_structure(self.root))
        self.bodies = {relative: path.read_bytes()
                       for relative, path in ((row['filePath'], self.root / row['filePath'])
                                              for row in load_structure(self.root)['chapters'])}

    def _assert_identities_unchanged(self) -> None:
        current = identity_snapshot(load_structure(self.root))
        self.assertEqual(current, self.identities)

    def _assert_prose_bytes_unchanged(self) -> None:
        for relative, payload in self.bodies.items():
            self.assertEqual((self.root / relative).read_bytes(), payload, relative)

    def test_contract_names_seven_identity_preserving_operations(self) -> None:
        contract = operation_contract()
        self.assertEqual(sorted(contract['supported']),
                         ['alias', 'archive', 'relocate', 'rename', 'reorder', 'reparent', 'restore'])
        for rule in contract['supported'].values():
            self.assertIn('Stable ID unchanged', rule)
        # split/merge/fork are declared but deliberately not implemented here.
        self.assertEqual(sorted(contract['deferred']), ['fork', 'merge', 'split'])
        self.assertEqual(contract['identityInvariants'],
                         ['projectId', 'volumeId', 'arcId', 'unitId', 'chapterId', 'entityId',
                          'relationId', 'candidateId', 'decisionId'])

    def test_book_rename_three_times_preserves_identity_and_prose(self) -> None:
        for name in ('神弃之冠', '冠', '最终书名'):
            preview = preview_rename_book(self.root, new_name=name)
            _commit(self.root, preview)
            structure = load_structure(self.root)
            self.assertEqual(structure['bookName'], name)
            marker = read_json(self.root / '.govel' / 'project.json')
            self.assertEqual(marker['bookName'], name, 'marker and structure must never diverge')
        self._assert_identities_unchanged()
        self._assert_prose_bytes_unchanged()

    def test_book_rename_rejects_an_identical_name(self) -> None:
        from scripts.core.structure_store import StructureError
        with self.assertRaises(StructureError) as caught:
            preview_rename_book(self.root, new_name='山城旧事')
        self.assertEqual(caught.exception.code, 'REFACTOR_NAME_UNCHANGED')

    def test_display_renames_preserve_identity_and_membership(self) -> None:
        for step in (
            lambda: preview_rename_volume(self.root, volume_id=self.info['volumeA'], new_title='第一卷：新名'),
            lambda: preview_rename_arc(self.root, arc_id=self.info['arcMain'], new_title='主线：新名'),
            lambda: preview_rename_unit(self.root, unit_id=self.info['unit'], new_title='新单元'),
            lambda: preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'], new_title='白塔坠落'),
            lambda: preview_rename_entity(self.root, selector=self.info['character'], new_name='艾琳娜',
                                          keep_old_name_as_alias=True),
        ):
            _commit(self.root, step())
        structure = load_structure(self.root)
        by_id = {row['chapterId']: row for row in structure['chapters']}
        self.assertEqual(by_id[self.info['chapterOne']]['title'], '白塔坠落')
        character = next(row for row in structure['entities'] if row['entityId'] == self.info['character'])
        self.assertEqual(character['name'], '艾琳娜')
        self.assertIn('艾琳', character['aliases'], 'keepOldNameAsAlias must be honoured verbatim')
        unit = next(row for row in structure['units'] if row['unitId'] == self.info['unit'])
        self.assertEqual([m['memberId'] for m in unit['members']],
                         [self.info['chapterOne'], self.info['chapterTwo']], 'membership is untouched')
        self.assertEqual(len(structure['chapterArcLinks']), 4, 'arc links are untouched')
        self._assert_identities_unchanged()
        self._assert_prose_bytes_unchanged()

    def test_entity_rename_can_decline_the_alias_and_still_change_nothing_else(self) -> None:
        _commit(self.root, preview_rename_entity(self.root, selector=self.info['character'],
                                                 new_name='艾琳娜', keep_old_name_as_alias=False))
        character = next(row for row in load_structure(self.root)['entities']
                         if row['entityId'] == self.info['character'])
        self.assertEqual(character['name'], '艾琳娜')
        self.assertNotIn('艾琳', character['aliases'])
        self._assert_identities_unchanged()

    def test_alias_only_change_leaves_the_name_alone(self) -> None:
        _commit(self.root, preview_alias_entity(self.root, selector=self.info['prop'],
                                                aliases=['钥匙', '小钥']))
        prop = next(row for row in load_structure(self.root)['entities'] if row['entityId'] == self.info['prop'])
        self.assertEqual(prop['name'], '旧钥匙')
        self.assertEqual(prop['aliases'], ['钥匙', '小钥'])
        self._assert_identities_unchanged()

    def test_chapter_path_relocation_moves_bytes_and_keeps_them_identical(self) -> None:
        target_body = '正文/017-白塔坠落.md'
        before_body = (self.root / f'正文/{self.info["chapterOne"]}.md').read_bytes()
        preview = preview_relocate_chapter_paths(self.root, chapter_id=self.info['chapterOne'],
                                                 file_path=target_body)
        self.assertEqual([row['field'] for row in preview['pathMoves']], ['filePath'])
        _commit(self.root, preview)
        chapter = next(row for row in load_structure(self.root)['chapters']
                       if row['chapterId'] == self.info['chapterOne'])
        self.assertEqual(chapter['filePath'], target_body)
        self.assertEqual((self.root / target_body).read_bytes(), before_body)
        self.assertFalse((self.root / f'正文/{self.info["chapterOne"]}.md').exists())
        self._assert_identities_unchanged()

    def test_chapter_rename_can_sync_file_names_in_the_same_transaction(self) -> None:
        preview = preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'],
                                         new_title='白塔坠落', file_name_strategy='sync_safe_paths')
        _commit(self.root, preview)
        chapter = next(row for row in load_structure(self.root)['chapters']
                       if row['chapterId'] == self.info['chapterOne'])
        self.assertEqual(chapter['filePath'], '正文/001-白塔坠落.md')
        self.assertTrue((self.root / '正文/001-白塔坠落.md').is_file())
        self.assertFalse((self.root / f'正文/{self.info["chapterOne"]}.md').exists())
        self._assert_identities_unchanged()

    def test_display_only_rename_never_touches_files(self) -> None:
        before = sorted(path.name for path in (self.root / '正文').iterdir())
        _commit(self.root, preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'],
                                                 new_title='白塔坠落'))
        self.assertEqual(sorted(path.name for path in (self.root / '正文').iterdir()), before)
        self._assert_identities_unchanged()

    def test_reorder_changes_ordinals_but_not_identity(self) -> None:
        before = {row['chapterId']: row['displayOrdinal'] for row in load_structure(self.root)['chapters']}
        _commit(self.root, preview_reorder_chapter(self.root, chapter_id=self.info['chapterThree'],
                                                   before_chapter_id=self.info['chapterOne']))
        after = {row['chapterId']: row['displayOrdinal'] for row in load_structure(self.root)['chapters']}
        self.assertNotEqual(after[self.info['chapterThree']], before[self.info['chapterThree']])
        self.assertEqual(after[self.info['chapterThree']], 1)
        self.assertEqual(set(after), set(before))
        self._assert_identities_unchanged()
        self._assert_prose_bytes_unchanged()

    def test_reparent_changes_placement_but_not_identity(self) -> None:
        # 卷单元的成员必须留在该卷，所以选不在任何单元里的章来跨卷移动。
        _commit(self.root, preview_reparent_chapter(self.root, chapter_id=self.info['chapterThree'],
                                                    volume_id=self.info['volumeA'],
                                                    primary_arc_id=self.info['arcMain']))
        chapter = next(row for row in load_structure(self.root)['chapters']
                       if row['chapterId'] == self.info['chapterThree'])
        self.assertEqual(chapter['placement'], {'scope': 'volume', 'volumeId': self.info['volumeA']})
        self.assertNotIn('volumeId', chapter, 'current format must not introduce a second placement field')
        self._assert_identities_unchanged()
        self._assert_prose_bytes_unchanged()

    def test_reparent_unit_to_project_scope_keeps_members(self) -> None:
        _commit(self.root, preview_reparent_unit(self.root, unit_id=self.info['unit'],
                                                 placement=project_placement()))
        unit = next(row for row in load_structure(self.root)['units'] if row['unitId'] == self.info['unit'])
        self.assertEqual(unit['placement'], {'scope': 'project'})
        self.assertEqual([m['memberId'] for m in unit['members']],
                         [self.info['chapterOne'], self.info['chapterTwo']])
        self._assert_identities_unchanged()

    def test_archive_then_restore_returns_the_same_stable_id(self) -> None:
        _commit(self.root, preview_archive_chapter(self.root, chapter_id=self.info['chapterTwo']))
        archived = next(row for row in load_structure(self.root)['chapters']
                        if row['chapterId'] == self.info['chapterTwo'])
        self.assertEqual(archived['status'], 'archived')
        self.assertTrue((self.root / f'正文/{self.info["chapterTwo"]}.md').is_file(),
                        'archive must never delete prose')
        _commit(self.root, preview_restore_chapter(self.root, chapter_id=self.info['chapterTwo']))
        restored = next(row for row in load_structure(self.root)['chapters']
                        if row['chapterId'] == self.info['chapterTwo'])
        self.assertNotEqual(restored['status'], 'archived')
        self._assert_identities_unchanged()
        self._assert_prose_bytes_unchanged()

    def test_entity_archive_then_restore_keeps_the_same_id(self) -> None:
        _commit(self.root, preview_archive_entity(self.root, selector=self.info['prop']))
        _commit(self.root, preview_restore_entity(self.root, selector=self.info['prop']))
        prop = next(row for row in load_structure(self.root)['entities'] if row['entityId'] == self.info['prop'])
        self.assertEqual(prop['status'], 'active')
        self._assert_identities_unchanged()

    def test_every_refactor_commits_a_receipt_and_can_be_undone(self) -> None:
        from scripts.core.structure_commands import list_operations, undo_operation
        preview = preview_rename_volume(self.root, volume_id=self.info['volumeA'], new_title='第一卷：新名')
        result = _commit(self.root, preview)
        self.assertEqual(result['code'], 'STRUCTURE_COMMITTED')
        self.assertEqual(result['changes'][0]['field'], 'title')
        journals = {row['operationId']: row for row in list_operations(self.root)}
        self.assertIn(result['operationId'], journals)
        undo_operation(self.root, result['operationId'])
        volume = next(row for row in load_structure(self.root)['volumes'] if row['volumeId'] == self.info['volumeA'])
        self.assertEqual(volume['title'], '第一卷：旧名')
        self._assert_identities_unchanged()

    def _assert_reference_candidates_read_only(self, name) -> dict:
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in sorted(self.root.rglob('*')) if path.is_file()}
        result = refactor_reference_candidates(self.root, old_name=name)
        self.assertTrue(result['candidates'], 'the fixture body names 旧钥匙')
        self.assertFalse(result['semanticJudgmentMadeByTool'])
        self.assertTrue(all(row['decision'] is None for row in result['candidates']))
        self.assertTrue(all(row['line'] >= 1 for row in result['candidates']))
        after = {path.relative_to(self.root).as_posix(): path.read_bytes()
                 for path in sorted(self.root.rglob('*')) if path.is_file()}
        self.assertEqual(before, after, 'a reference scan must not write anything')
        return result

    def test_repeated_renames_keep_identity_and_leave_an_auditable_trail(self) -> None:
        """A→B→C for a volume and a chapter, each with its own preview/commit/receipt."""
        from scripts.core.structure_commands import list_operations
        chain = {'volume': (self.info['volumeA'], ['第一卷：新名', '第一卷：最终名']),
                 'chapter': (self.info['chapterOne'], ['白塔坠落', '白塔之夜'])}
        for round_index, title_index in enumerate((0, 1)):
            for kind, (item_id, titles) in chain.items():
                step = (preview_rename_volume if kind == 'volume' else preview_rename_chapter)
                _commit(self.root, step(self.root, **{f'{kind}_id': item_id},
                                        new_title=titles[title_index]))
            self._assert_identities_unchanged()
        structure = load_structure(self.root)
        self.assertEqual(next(row['title'] for row in structure['volumes']
                              if row['volumeId'] == self.info['volumeA']), '第一卷：最终名')
        self.assertEqual(next(row['title'] for row in structure['chapters']
                              if row['chapterId'] == self.info['chapterOne']), '白塔之夜')
        journals = sorted(list_operations(self.root), key=lambda row: row['committedAt'])
        trail = [(row['changes'][0]['objectType'], row['changes'][0]['before'], row['changes'][0]['after'])
                 for row in journals if row.get('changes')]
        self.assertEqual(trail, [('volume', '第一卷：旧名', '第一卷：新名'),
                                 ('chapter', '白塔', '白塔坠落'),
                                 ('volume', '第一卷：新名', '第一卷：最终名'),
                                 ('chapter', '白塔坠落', '白塔之夜')])
        self._assert_prose_bytes_unchanged()

    def test_every_supported_refactor_in_sequence_keeps_the_identity_set(self) -> None:
        """The whole pipeline at once: rename everything, then move and reorder."""
        steps = (
            lambda: preview_rename_book(self.root, new_name='神弃之冠'),
            lambda: preview_rename_volume(self.root, volume_id=self.info['volumeA'], new_title='第一卷：新名'),
            lambda: preview_rename_arc(self.root, arc_id=self.info['arcMain'], new_title='主线：新名'),
            lambda: preview_rename_unit(self.root, unit_id=self.info['unit'], new_title='新单元'),
            lambda: preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'], new_title='白塔坠落',
                                           file_name_strategy='sync_safe_paths'),
            lambda: preview_relocate_chapter_paths(self.root, chapter_id=self.info['chapterTwo'],
                                                   file_path='正文/002-旧楼道.md'),
            lambda: preview_rename_entity(self.root, selector=self.info['character'], new_name='艾琳娜',
                                          keep_old_name_as_alias=True),
            lambda: preview_reorder_chapter(self.root, chapter_id=self.info['chapterThree'],
                                            before_chapter_id=self.info['chapterOne']),
            lambda: preview_reparent_chapter(self.root, chapter_id=self.info['chapterThree'],
                                             volume_id=self.info['volumeA'], primary_arc_id=self.info['arcMain']),
            lambda: preview_reparent_unit(self.root, unit_id=self.info['unit'],
                                          placement={'scope': 'project'}),
            lambda: preview_archive_chapter(self.root, chapter_id=self.info['chapterTwo']),
            lambda: preview_restore_chapter(self.root, chapter_id=self.info['chapterTwo']),
        )
        for step in steps:
            _commit(self.root, step())
        self._assert_identities_unchanged()
        structure = load_structure(self.root)
        self.assertEqual(structure['bookName'], '神弃之冠')
        self.assertEqual({row['chapterId']: row['filePath'] for row in structure['chapters']},
                         {self.info['chapterOne']: '正文/001-白塔坠落.md',
                          self.info['chapterTwo']: '正文/002-旧楼道.md',
                          self.info['chapterThree']: f'正文/{self.info["chapterThree"]}.md'})
        self.assertEqual((self.root / '正文' / '001-白塔坠落.md').read_text(encoding='utf-8'),
                         '白塔立在河口。艾琳握着旧钥匙。\n')
        for relative, payload in self.bodies.items():
            if relative.endswith(f'{self.info["chapterOne"]}.md'):
                continue  # that body moved; its bytes are asserted above
            if relative.endswith(f'{self.info["chapterTwo"]}.md'):
                self.assertEqual((self.root/'正文/002-旧楼道.md').read_bytes(), payload)
                continue
            self.assertEqual((self.root / relative).read_bytes(), payload, relative)

    def test_undoing_a_path_relocation_brings_the_file_back_to_its_old_locator(self) -> None:
        from scripts.core.structure_commands import undo_operation
        preview = preview_relocate_chapter_paths(self.root, chapter_id=self.info['chapterOne'],
                                                 file_path='正文/001-白塔坠落.md')
        result = _commit(self.root, preview)
        self.assertTrue((self.root / '正文' / '001-白塔坠落.md').is_file())
        from scripts.core.chapter_receipt import chapter_receipt
        token = chapter_receipt(self.root, self.info['chapterOne'])['bindingGeneration']
        undo_operation(self.root, result['operationId'], binding_generation=token)
        chapter = next(row for row in load_structure(self.root)['chapters']
                       if row['chapterId'] == self.info['chapterOne'])
        self.assertEqual(chapter['filePath'], f'正文/{self.info["chapterOne"]}.md')
        self.assertTrue((self.root / chapter['filePath']).is_file())
        self.assertFalse((self.root / '正文' / '001-白塔坠落.md').exists())
        self._assert_prose_bytes_unchanged()

    def test_reference_candidates_point_at_the_old_name_without_touching_it(self) -> None:
        self._assert_reference_candidates_read_only('旧钥匙')
        _commit(self.root, preview_rename_entity(self.root, selector=self.info['character'],
                                                 new_name='艾琳娜', keep_old_name_as_alias=True))
        result = self._assert_reference_candidates_read_only('艾琳')
        self.assertTrue(result['candidates'])
        self.assertEqual({row['chapterId'] for row in result['candidates']},
                         {self.info['chapterOne'], self.info['chapterTwo']})
        self.assertTrue(all(row['decision'] is None for row in result['candidates']))
        self._assert_prose_bytes_unchanged()
        # A name that was never written yields an honest empty report, not a guess.
        empty = refactor_reference_candidates(self.root, old_name='不存在的名字')
        self.assertEqual(empty['matchCount'], 0)
        self.assertFalse(empty['truncated'])

    def test_doctor_reports_book_name_divergence(self) -> None:
        marker_path = self.root / '.govel' / 'project.json'
        marker = read_json(marker_path)
        marker['bookName'] = '另一个名字'
        from scripts.core.jsonio import write_json
        write_json(marker_path, marker)
        report = assess_project(self.root)
        codes = {row['code'] for row in report['domains']['project']['issues']}
        self.assertIn('PROJECT_BOOK_NAME_DIVERGES', codes)

    def test_doctor_is_clean_on_a_freshly_refactored_project(self) -> None:
        for step in (
            lambda: preview_rename_book(self.root, new_name='新书名'),
            lambda: preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'], new_title='白塔坠落'),
            lambda: preview_relocate_chapter_paths(self.root, chapter_id=self.info['chapterTwo'],
                                                   file_path='正文/002-旧楼道.md'),
        ):
            _commit(self.root, step())
        report = assess_project(self.root)
        codes = {row['code'] for row in report['domains']['project']['issues']}
        for unexpected in ('PROJECT_BOOK_NAME_DIVERGES', 'REFACTOR_PATH_ESCAPES_PROJECT',
                           'REFACTOR_DUPLICATE_LOCATOR', 'REFACTOR_PATH_CANONICAL_COLLISION',
                           'REFACTOR_OUTLINE_PATH_MISSING', 'REFACTOR_PLAN_PENDING'):
            self.assertNotIn(unexpected, codes)


if __name__ == '__main__':
    unittest.main()
