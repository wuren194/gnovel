"""Path safety for locators: nothing is guessed, nothing is overwritten.

A locator is a mutable address, never an identity. These tests exercise both sides
of that rule: the proposal stage must refuse every unsafe or colliding target
before a byte moves, and the commit stage must move the file together with the
structure — so an interrupted transaction leaves the old file in place, the new
file absent, and the prose byte-identical.
"""
from __future__ import annotations
from scripts.core.locks import project_lock_observation

import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.core.jsonio import read_json, write_json
from scripts.core.project_doctor import assess_project
from scripts.core.refactor import (apply_locator_renames, commit_refactor, preview_relocate_chapter_paths,
                                   preview_rename_book, preview_rename_chapter, preview_rename_volume)
from scripts.core.refactor_paths import (RefactorPathError, canonical_key, canonical_siblings, check_target,
                                         directory_entry_names, safe_component, same_file, separate_directory_entry,
                                         synced_body_path)
from scripts.core.structure_commands import list_pending_plans
from scripts.core.structure_store import StructureError, load_structure, save_structure


ROOT = Path(__file__).resolve().parents[1]


def _pending(root: Path) -> list[dict]:
    return list_pending_plans(root)
from tests.fixtures_refactor import refactor_project


def _commit(root: Path, preview: dict) -> dict:
    return commit_refactor(root, operation_id=preview['operationId'], plan_hash=preview['planHash'],
                           approved=True, actor='test')


def _other_process_commit(root: Path, plan: dict) -> str:
    """Ask a *different PID* to commit and report which way it refused.

    The project lock is a PID-bearing file, so an in-process thread would share the
    holder's identity and prove nothing about real concurrency.
    """
    script = """import sys
from pathlib import Path
from scripts.core.structure_commands import commit_plan
try:
    commit_plan(Path(sys.argv[1]), operation_id=sys.argv[2], plan_hash=sys.argv[3], approved=True, actor='rival')
    print('COMMITTED')
except Exception as exc:
    print(getattr(exc, 'code', type(exc).__name__))
"""
    result = subprocess.run([sys.executable, '-c', script, str(root), plan['operationId'], plan['planHash']],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    return result.stdout.strip() or result.stderr.strip()


class FileNameRuleTests(unittest.TestCase):
    def test_only_filesystem_illegal_characters_are_replaced(self) -> None:
        # The full-width colon is authored text and stays; `/` and `\\` are path
        # separators and cannot be written into a component at all.
        self.assertEqual(safe_component('第十七章：白塔/坠落'), '第十七章：白塔-坠落')
        self.assertEqual(safe_component('a/b\\c'), 'a-b-c')
        self.assertEqual(safe_component('bad:chars?*\x00'), 'bad-chars',
                         'trailing separators are stripped; a name never ends in punctuation')

    def test_unicode_spaces_and_emoji_survive_untouched(self) -> None:
        self.assertEqual(safe_component('白塔 坠落🜂'), '白塔 坠落🜂')
        self.assertEqual(safe_component('艾琳）、旧钥匙（第三'), '艾琳）、旧钥匙（第三')

    def test_the_same_display_name_always_builds_the_same_component(self) -> None:
        names = ('第一卷：旧名/新名', '白塔🜂坠落', 'a  b//c')
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(safe_component(name), safe_component(name))

    def test_a_name_with_nothing_legal_left_gets_a_deterministic_fallback(self) -> None:
        self.assertEqual(safe_component('///'), '未命名')
        self.assertEqual(safe_component('   '), '未命名')
        self.assertEqual(safe_component('::'), '未命名')

    def test_the_collision_key_normalizes_unicode_and_case_together(self) -> None:
        composed = '白塔\u00e9.md'
        decomposed = unicodedata.normalize('NFD', composed)
        self.assertNotEqual(composed, decomposed, 'the two spellings differ byte for byte')
        self.assertEqual(canonical_key(composed), canonical_key(decomposed))
        self.assertEqual(canonical_key('正文/ABC.md'), canonical_key('正文/abc.md'))


class PathProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / '书').resolve()
        self.info = refactor_project(self.root)
        self.body = f'正文/{self.info["chapterOne"]}.md'

    def _refuses(self, source: str, target: str, code: str) -> None:
        with self.assertRaises(RefactorPathError) as caught:
            check_target(self.root, source, target)
        self.assertEqual(caught.exception.code, code)

    def test_traversal_and_absolute_escape_are_refused(self) -> None:
        self._refuses(self.body, '../逃逸.md', 'REFACTOR_PATH_INVALID')
        self._refuses(self.body, '正文/../../逃逸.md', 'REFACTOR_PATH_INVALID')
        self._refuses(self.body, str(self.root.parent / '外面.md'), 'REFACTOR_PATH_INVALID')
        self._refuses('../outside/正文/x.md', '正文/新名.md', 'REFACTOR_PATH_INVALID')

    def test_a_symlinked_locator_is_not_a_canonical_path(self) -> None:
        outside = self.root.parent / 'outside.md'
        outside.write_text('项目外的正文。\n', encoding='utf-8')
        (self.root / '正文' / 'link.md').symlink_to(outside)
        self._refuses('正文/link.md', '正文/被指.md', 'REFACTOR_PATH_INVALID')
        self._refuses(self.body, '正文/link.md', 'REFACTOR_PATH_INVALID')

    def test_an_existing_target_is_never_overwritten(self) -> None:
        self._refuses(self.body, f'正文/{self.info["chapterTwo"]}.md', 'REFACTOR_TARGET_EXISTS')
        self._refuses(self.body, '正文', 'REFACTOR_TARGET_IS_DIRECTORY')
        self._refuses(self.body, self.body, 'REFACTOR_PATH_UNCHANGED')

    def test_a_second_spelling_of_one_name_occupies_one_slot(self) -> None:
        # Two spellings of one name are one slot. Where the volume treats them as
        # one file the existing bytes are refused (never overwritten); where it keeps
        # them apart the collision key still reports the clash up front.
        (self.root / '正文' / 'ABC.md').write_text('已有文件。\n', encoding='utf-8')
        one_file = (self.root / '正文' / 'abc.md').exists()
        self._refuses(self.body, '正文/abc.md',
                      'REFACTOR_TARGET_EXISTS' if one_file else 'REFACTOR_PATH_CANONICAL_COLLISION')
        (self.root / '正文' / '白塔é.md').write_text('合成形。\n', encoding='utf-8')
        decomposed = unicodedata.normalize('NFD', '白塔é.md')
        self.assertNotEqual('白塔é.md', decomposed)
        one_file = (self.root / '正文' / decomposed).exists()
        self._refuses(self.body, f'正文/{decomposed}',
                      'REFACTOR_TARGET_EXISTS' if one_file else 'REFACTOR_PATH_CANONICAL_COLLISION')

    def test_a_missing_source_only_matters_once_the_body_is_formal(self) -> None:
        ghost = check_target(self.root, '正文/CH-000000000000.md', '正文/幽灵.md')
        self.assertEqual(ghost['sourceState'], 'missing')
        self.assertEqual(ghost['action'], 'locator_only')
        chapter = load_structure(self.root)['chapters'][0]
        (self.root / chapter['filePath']).unlink()
        with self.assertRaises(StructureError) as caught:
            preview_relocate_chapter_paths(self.root, chapter_id=chapter['chapterId'],
                                           file_path='正文/白塔坠落.md')
        self.assertEqual(caught.exception.code, 'PROSE_LOCATOR_DRIFT')

    def test_synced_paths_are_ordinal_prefixed_and_deterministic(self) -> None:
        chapter = dict(load_structure(self.root)['chapters'][0], title='第十七章：白塔/坠落')
        self.assertEqual(synced_body_path(self.root, chapter), '正文/001-第十七章：白塔-坠落.md')
        self.assertEqual(synced_body_path(self.root, chapter), synced_body_path(self.root, chapter))
        for broken in ({**chapter, 'displayOrdinal': None}, {**chapter, 'displayOrdinal': 0},
                       {**chapter, 'title': '  '}):
            with self.subTest(broken=broken):
                with self.assertRaises(RefactorPathError):
                    synced_body_path(self.root, broken)

    def test_a_file_name_never_resolves_back_to_an_identity(self) -> None:
        _commit(self.root, preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'],
                                                 new_title='白塔坠落', file_name_strategy='sync_safe_paths'))
        self.assertTrue((self.root / '正文' / '001-白塔坠落.md').is_file())
        for guessed in ('001', '白塔坠落', '001-白塔坠落'):
            with self.subTest(chapter_id=guessed):
                with self.assertRaises(StructureError) as caught:
                    preview_relocate_chapter_paths(self.root, chapter_id=guessed, file_path='正文/别的.md')
                self.assertEqual(caught.exception.code, 'CHAPTER_NOT_FOUND')


class PathTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / '书').resolve()
        self.info = refactor_project(self.root)

    def _chapter(self, chapter_id: str) -> dict:
        return next(row for row in load_structure(self.root)['chapters']
                    if row['chapterId'] == chapter_id)

    def _seed_a_case_only_chapter(self) -> None:
        """One stable chapter whose body is `正文/CH-case.md`, bytes `case\\n`."""
        (self.root / '正文' / 'CH-case.md').write_bytes(b'case\n')
        structure = load_structure(self.root)
        structure['chapters'].append({'chapterId': 'CH-CASE00000001', 'displayOrdinal': 4,
                                      'displayCode': 'CH004', 'title': 'Case',
                                      'placement': {'scope': 'volume', 'volumeId': self.info['volumeA']},
                                      'filePath': '正文/CH-case.md',
                                      'status': 'stable', 'storyTimeStartId': None, 'storyTimeEndId': None})
        structure['chapterArcLinks'].append({'chapterId': 'CH-CASE00000001', 'arcId': self.info['arcMain'],
                                             'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''})
        from scripts.core.prose_binding import seal_binding
        from scripts.core.artifacts import file_sha256
        chapter = structure['chapters'][-1]
        chapter['proseBinding'] = seal_binding(structure['projectId'], chapter['chapterId'], chapter['filePath'],
            state='managed', generation=structure['revision'], expected_content_digest='sha256:'+file_sha256(self.root/chapter['filePath']))
        write_json(self.root/'.govel/structure.json', structure)

    def test_a_relocated_body_keeps_its_bytes_and_its_identity(self) -> None:
        payload = (self.root / f'正文/{self.info["chapterOne"]}.md').read_bytes()
        _commit(self.root, preview_relocate_chapter_paths(
            self.root, chapter_id=self.info['chapterOne'],
            file_path='正文/001-白塔 坠落🜂.md'))
        chapter = self._chapter(self.info['chapterOne'])
        self.assertEqual(chapter['filePath'], '正文/001-白塔 坠落🜂.md')
        self.assertEqual((self.root / chapter['filePath']).read_bytes(), payload)
        self.assertFalse((self.root / f'正文/{self.info["chapterOne"]}.md').exists())
        self.assertEqual({row['chapterId'] for row in load_structure(self.root)['chapters']},
                         {self.info['chapterOne'], self.info['chapterTwo'], self.info['chapterThree']})

    def test_an_interrupted_move_rolls_the_file_and_the_structure_back(self) -> None:
        """The body lands, the next write fails — and everything returns."""
        preview = preview_relocate_chapter_paths(
            self.root, chapter_id=self.info['chapterOne'],
            file_path='正文/001-白塔坠落.md')
        old_body = self.root / f'正文/{self.info["chapterOne"]}.md'
        new_body = self.root / '正文' / '001-白塔坠落.md'
        prose = old_body.read_bytes()
        structure_before = (self.root / '.govel' / 'structure.json').read_bytes()

        def fail_on_body_write(phase, relative=None):
            if phase == 'write' and relative == '正文/001-白塔坠落.md':
                raise RuntimeError('injected move failure')

        with patch('scripts.core.write_sets.checkpoint', side_effect=fail_on_body_write):
            with self.assertRaisesRegex(RuntimeError, 'injected move failure'):
                _commit(self.root, preview)
        self.assertTrue(old_body.is_file(), 'the old file is still there')
        self.assertFalse(new_body.exists(), 'the new file is not there')
        self.assertEqual(old_body.read_bytes(), prose, 'prose is byte-for-byte intact')
        self.assertEqual((self.root / '.govel' / 'structure.json').read_bytes(), structure_before)
        self.assertEqual(self._chapter(self.info['chapterOne'])['filePath'],
                         f'正文/{self.info["chapterOne"]}.md')
        self.assertFalse(list((self.root / '.govel' / 'transactions').glob('*.json')))
        _commit(self.root, preview_relocate_chapter_paths(
            self.root, chapter_id=self.info['chapterOne'], file_path='正文/001-白塔坠落.md'))
        self.assertEqual(self._chapter(self.info['chapterOne'])['filePath'], '正文/001-白塔坠落.md')
        self.assertEqual(new_body.read_bytes(), prose)

    def test_case_only_rename_is_explicitly_deferred_without_writes(self) -> None:
        self._seed_a_case_only_chapter()
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(StructureError) as caught:
            preview_relocate_chapter_paths(self.root, chapter_id='CH-CASE00000001', file_path='正文/CH-Case.md')
        self.assertEqual(caught.exception.code, 'REFACTOR_CASE_ONLY_RENAME_DEFERRED')
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})



    @staticmethod
    def _break_write(phase, relative=None):
        if phase == 'prepared':
            raise RuntimeError('injected write set failure')

    def test_rename_and_file_sync_are_two_independent_choices(self) -> None:
        files_before = sorted(path.name for path in (self.root / '正文').iterdir())
        _commit(self.root, preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'],
                                                  new_title='白塔坠落'))
        self.assertEqual(sorted(path.name for path in (self.root / '正文').iterdir()), files_before)
        self.assertEqual(self._chapter(self.info['chapterOne'])['filePath'],
                         f'正文/{self.info["chapterOne"]}.md', 'the locator is not silently synced')
        _commit(self.root, preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'],
                                                  new_title='白塔坠毁', file_name_strategy='sync_safe_paths'))
        self.assertEqual(self._chapter(self.info['chapterOne'])['filePath'], '正文/001-白塔坠毁.md')
        self.assertEqual(self._chapter(self.info['chapterOne'])['title'], '白塔坠毁')

    def test_an_unsupported_file_name_strategy_is_refused_before_any_plan(self) -> None:
        with self.assertRaises(StructureError) as caught:
            preview_rename_chapter(self.root, chapter_id=self.info['chapterOne'], new_title='新名',
                                   file_name_strategy='auto')
        self.assertEqual(caught.exception.code, 'REFACTOR_FILE_NAME_STRATEGY_INVALID')




class HardLinkAliasTests(unittest.TestCase):
    """One inode is not one directory entry.

    `same_file` answers identity: do these two paths reach the same bytes. A hard link
    answers it exactly the way a case- or normalization-insensitive volume does, and only
    the entries say which shape this is — which is not cosmetic, because POSIX `rename`
    onto a second name for one inode succeeds while moving nothing. Accepted as an
    in-place rename, that leaves the old locator on disk as a real entry *and* points
    `structure.json` at a spelling that was never relabelled.

    The unrelated-name and cross-directory pairs are buildable on every volume, so those
    refusals run here and not only on CI. The case-only and NFD/NFC pairs need a volume
    that stores both spellings as two entries; where `os.link` itself refuses, that shape
    is skipped because the machine cannot express the case.
    """

    CHAPTER = 'CH-HARDLINK0001'
    PAYLOAD = b'case\n'

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / '书').resolve()
        self.info = refactor_project(self.root)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _seed_chapter(self, name: str) -> str:
        """One stable chapter whose body really is `正文/<name>`."""
        (self.root / '正文' / name).write_bytes(self.PAYLOAD)
        structure = load_structure(self.root)
        structure['chapters'].append({'chapterId': self.CHAPTER, 'displayOrdinal': 4,
                                      'displayCode': 'CH004', 'title': 'Hardlink',
                                      'placement': {'scope': 'volume', 'volumeId': self.info['volumeA']},
                                      'filePath': f'正文/{name}',
                                      'status': 'stable', 'storyTimeStartId': None, 'storyTimeEndId': None})
        structure['chapterArcLinks'].append({'chapterId': self.CHAPTER, 'arcId': self.info['arcMain'],
                                             'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''})
        from scripts.core.prose_binding import seal_binding
        from scripts.core.artifacts import file_sha256
        chapter = structure['chapters'][-1]
        chapter['proseBinding'] = seal_binding(structure['projectId'], chapter['chapterId'], chapter['filePath'],
            state='managed', generation=structure['revision'], expected_content_digest='sha256:'+file_sha256(self.root/chapter['filePath']))
        write_json(self.root/'.govel/structure.json', structure)
        return f'正文/{name}'

    def _chapter(self) -> dict:
        return next(row for row in load_structure(self.root)['chapters'] if row['chapterId'] == self.CHAPTER)

    def _linked(self, source_relative: str, target_relative: str) -> tuple[Path, Path] | None:
        """Two real entries over one inode, or `None` where this volume cannot hold both names."""
        source, target = self.root / source_relative, self.root / target_relative
        source.write_bytes(self.PAYLOAD)
        try:
            os.link(source, target)
        except OSError:
            return None
        return source, target

    def _assert_two_entries_one_inode(self, source: Path, target: Path) -> None:
        """The precondition the old judgement could not read: identity without location."""
        self.assertTrue(source.samefile(target), 'one inode — what `same_file` sees')
        self.assertIn(source.name, {path.name for path in source.parent.iterdir()},
                      'the source spelling is a real entry')
        self.assertIn(target.name, {path.name for path in target.parent.iterdir()},
                      'the target spelling is a real entry of its own, not a folded alias')

    def _assert_nothing_changed(self, source_relative: str, target_relative: str) -> None:
        source, target = self.root / source_relative, self.root / target_relative
        self.assertEqual(source.read_bytes(), self.PAYLOAD, 'the source bytes never moved')
        self.assertEqual(target.read_bytes(), self.PAYLOAD)
        self.assertTrue(source.samefile(target), 'still the same inode: no rename, no overwrite')
        listed = {path.name for path in target.parent.iterdir()}
        for relative in (source_relative, target_relative):
            self.assertIn(Path(relative).name, listed, f'{relative} is still addressable')
        self.assertEqual(self._chapter()['filePath'], source_relative,
                         'structure still names the locator the disk holds')
        self.assertEqual({row['chapterId'] for row in load_structure(self.root)['chapters']},
                         {self.info['chapterOne'], self.info['chapterTwo'], self.info['chapterThree'],
                          self.CHAPTER}, 'identity untouched by a refused rename')
        self.assertEqual(_pending(self.root), [], 'no plan represents the rename that was refused')

    def _shapes(self) -> list[tuple[str, str, str]]:
        """Every hard-link shape this volume can physically hold: (label, source, target)."""
        decomposed, composed = 'cafe\u0301.md', 'caf\u00e9.md'
        candidates = [
            ('second name in the same directory', '正文/CH-case.md', '正文/CH-case-copy.md'),
            ('same name in another directory', '正文/CH-case.md', '规划/CH-case.md'),
            ('case-only spelling', '正文/CaseName.md', '正文/casename.md'),
            ('normalization-only spelling', f'正文/{decomposed}', f'正文/{composed}'),
        ]
        shapes = []
        for label, source_relative, target_relative in candidates:
            linked = self._linked(source_relative, target_relative)
            if linked is None:
                continue
            self._assert_two_entries_one_inode(*linked)
            shapes.append((label, source_relative, target_relative))
        return shapes

    # ------------------------------------------------------------------
    # H1 / H2 / H3 — the refusal, and the shape it must never be mistaken for
    # ------------------------------------------------------------------

    def test_every_hard_link_shape_is_refused_and_never_becomes_in_place(self) -> None:
        shapes = self._shapes()
        self.assertGreaterEqual(len(shapes), 2,
                                'the unrelated-name and cross-directory shapes build on any volume, '
                                'so fewer than two here means the fixtures broke, not the volume')
        for label, source_relative, target_relative in shapes:
            with self.subTest(shape=label):
                try:
                    row = check_target(self.root, source_relative, target_relative)
                except RefactorPathError as caught:
                    self.assertEqual(caught.code, 'REFACTOR_TARGET_EXISTS',
                                     'the author-requested spelling is already a real entry')
                    continue
                self.assertNotEqual(row['action'], 'in_place',
                                    'same inode is a necessary condition, never a sufficient one')
                self.fail(f'{label} returned {row["action"]} instead of refusing')

    def test_a_refused_hard_link_rename_leaves_both_entries_and_the_record_alone(self) -> None:
        """The harm the pre-fix judgement committed: a no-op rename that reported success.

        `structure.json` ended up naming the hard link while the old locator stayed on
        disk as a second entry — one chapter, two addresses, no transaction that could
        undo it. So this walks the formal preview and asserts the absence of every part
        of that outcome, not just the error code.
        """
        source_relative = self._seed_chapter('CH-case.md')
        target_relative = '正文/CH-case-copy.md'
        linked = self._linked(source_relative, target_relative)
        if linked is None:
            self.skipTest('this volume refuses the hard link itself, so the shape cannot be built')
        with self.assertRaises(StructureError) as caught:
            preview_relocate_chapter_paths(self.root, chapter_id=self.CHAPTER, file_path=target_relative)
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')
        self._assert_nothing_changed(source_relative, target_relative)

    def test_a_hard_link_never_reaches_the_in_place_apply_step(self) -> None:
        """The commit side re-proves the entry, because the plan carries only the strategy.

        `appliedAs` was decided when the plan was previewed. If a second entry for that
        inode exists by the time the rename runs, `os.rename` would succeed as a no-op and
        the write set would land a locator the disk does not hold.
        """
        source_relative = self._seed_chapter('CH-case.md')
        target_relative = '正文/CH-case-copy.md'
        source, target = self.root / source_relative, self.root / target_relative
        source.write_bytes(self.PAYLOAD)
        os.link(source, target)
        plan = {'pathMoves': [{'sourcePath': source_relative, 'targetPath': target_relative,
                               'appliedAs': 'in_place_rename'}]}
        with self.assertRaises(RefactorPathError) as caught:
            apply_locator_renames(self.root, plan)
        self.assertEqual(caught.exception.code, 'REFACTOR_CASE_RENAME_UNSAFE')
        self._assert_nothing_changed(source_relative, target_relative)

    # ------------------------------------------------------------------
    # H4 + the fail-closed half of the listing
    # ------------------------------------------------------------------


    def test_an_unreadable_directory_listing_proves_nothing_and_is_refused(self) -> None:
        """Failing closed needs a proof, not the absence of a counter-example.

        An empty listing is what `in_place` reads as 'the target spelling is not a real
        entry', so an unlistable directory must not be reported as empty. The mode is real
        (`--x` keeps the file stat-able and breaks enumeration alone); the folded-spelling
        half needs a volume that folds.
        """
        source_relative = self._seed_chapter('CH-case.md')
        target_relative = '正文/CH-case-copy.md'
        source, target = self.root / source_relative, self.root / target_relative
        os.link(source, target)
        body = self.root / '正文'
        os.chmod(body, 0o100)
        self.addCleanup(os.chmod, body, 0o755)
        self.assertIsNone(directory_entry_names(body), 'unreadable is not the same as empty')
        self.assertTrue(separate_directory_entry(source, target), 'unprovable means occupied')
        with self.assertRaises(RefactorPathError) as caught:
            check_target(self.root, source_relative, target_relative)
        self.assertEqual(caught.exception.code, 'REFACTOR_TARGET_EXISTS')
        if (self.root / '正文' / 'CH-Case.md').exists():
            with self.assertRaises(RefactorPathError) as folded:
                check_target(self.root, source_relative, '正文/CH-Case.md')
            self.assertEqual(folded.exception.code, 'REFACTOR_TARGET_EXISTS',
                             'a folding volume may not be trusted to be folding when it cannot be listed')


class DoctorRefactorChecksTests(unittest.TestCase):
    """The Doctor half of the contract: hand-edited locators are caught, not guessed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / '书').resolve()
        self.info = refactor_project(self.root)
        self.marker = self.root / '.govel' / 'structure.json'

    def _truth(self) -> dict:
        """Authored bytes outside staged plans and the persistent lock anchor."""
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in sorted(self.root.rglob('*'))
                if path.is_file() and '.govel/plans' not in path.relative_to(self.root).as_posix()
                and path.name != 'structure.lock'}

    def _codes(self, domain='project'):
        report = assess_project(self.root)
        return {row['code'] for row in report['domains'][domain]['issues']}

    def _rewrite_chapter(self, chapter_id: str, **fields) -> None:
        # Simulated hand edit: the Doctor must catch what a transaction would refuse.
        raw = json.loads(self.marker.read_text(encoding='utf-8'))
        for row in raw['chapters']:
            if row['chapterId'] == chapter_id:
                row.update(fields)
        self.marker.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')

    def test_a_locator_that_escapes_the_project_is_blocking(self) -> None:
        self._rewrite_chapter(self.info['chapterOne'], filePath='../outside/白塔.md')
        self.assertIn('PROJECT_UNREADABLE', self._codes())

    def test_two_records_aimed_at_one_file_are_blocking(self) -> None:
        shared = f'正文/{self.info["chapterTwo"]}.md'
        self._rewrite_chapter(self.info['chapterOne'], filePath=shared)
        codes = self._codes()
        self.assertIn('PROJECT_UNREADABLE', codes)

    def test_two_spellings_of_one_locator_are_a_collision(self) -> None:
        # 'é' as one code point and as 'e' + combining accent are different strings
        # for one and the same slot; the collision key is what catches that.
        composed = unicodedata.normalize('NFC', '正文/白塔é.md')
        decomposed = unicodedata.normalize('NFD', composed)
        self.assertNotEqual(composed, decomposed)
        self._rewrite_chapter(self.info['chapterOne'], filePath=composed)
        self._rewrite_chapter(self.info['chapterTwo'], filePath=decomposed)
        self.assertIn('PROJECT_UNREADABLE', self._codes())

    def test_source_chapter_requires_source_plan(self) -> None:
        raw = read_json(self.marker)
        raw['fragments'] = [f for f in raw['fragments'] if f['ownerId'] != self.info['chapterOne']]
        write_json(self.marker, raw)
        self.assertIn('SOURCE_CHAPTER_PLAN_MISSING', self._codes())

    def test_an_uncommitted_refactor_plan_is_reported_but_still_harmless(self) -> None:
        before = self._truth()
        preview_rename_volume(self.root, volume_id=self.info['volumeA'], new_title='第一卷：待批')
        self.assertIn('REFACTOR_PLAN_PENDING', self._codes('workflow'))
        self.assertEqual(load_structure(self.root)['volumes'][0]['title'], '第一卷：旧名')
        self.assertEqual(self._truth(), before, 'an unapproved plan changes no truth')
        # Committing it is what moves the title, and the pending report goes away.
        pending = [row for row in _pending(self.root) if row['operation'] == 'refactor_rename_volume']
        _commit(self.root, {'operationId': pending[0]['operationId'], 'planHash': pending[0]['planHash']})
        self.assertEqual(load_structure(self.root)['volumes'][0]['title'], '第一卷：待批')
        self.assertNotIn('REFACTOR_PLAN_PENDING', self._codes('workflow'))

    def test_a_project_locator_receipt_that_no_longer_matches_is_a_warning(self) -> None:
        from scripts.core.project_relocate import RECEIPT_PATH
        from scripts.core.jsonio import write_json
        write_json(self.root / RECEIPT_PATH, {'schemaVersion': 1, 'kind': 'project_locator',
                                              'projectId': 'PRJ-REFACTOR-1',
                                              'rootAtOperation': '/Books/旧位置',
                                              'currentLocator': '/Books/别的位置',
                                              'history': []})
        self.assertIn('PROJECT_LOCATOR_RECEIPT_STALE', self._codes())

class ProjectFolderRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / '山城旧事'
        self.info = refactor_project(self.root)

    def test_a_moved_folder_is_still_the_same_project(self) -> None:
        from scripts.core.project_relocate import (RECEIPT_PATH, commit_relocate_project,
                                                   preview_relocate_project)
        target = self.base / '神弃之冠'
        preview = preview_relocate_project(self.root, target=str(target))
        self.assertTrue(preview['requiresApproval'])
        self.assertFalse(preview['impact']['crossDevice'])
        # The approval is durable: what the author was shown is the plan the commit
        # binds to, not a hash the caller had to keep somewhere safe.
        self.assertTrue((self.root / '.govel' / 'plans' / f"{preview['operationId']}.json").is_file())
        result = commit_relocate_project(self.root, operation_id=preview['operationId'],
                                         plan_hash=preview['planHash'], approved=True, actor='test')
        self.assertEqual(result['code'], 'PROJECT_RELOCATED')
        self.assertFalse(self.root.exists())
        structure = load_structure(target)
        self.assertEqual(structure['projectId'], 'PRJ-REFACTOR-1')
        self.assertEqual(structure['bookName'], '山城旧事', 'the folder moved; the name did not')
        receipt = read_json(target / RECEIPT_PATH)
        self.assertEqual(receipt['rootAtOperation'], str(self.root))
        self.assertEqual(receipt['currentLocator'], str(target))
        # A reader holding only the receipt can trace it back to the preview that was
        # approved and to who approved it.
        self.assertEqual(receipt['operationId'], preview['operationId'])
        self.assertEqual(receipt['planHash'], preview['planHash'])
        self.assertEqual(receipt['actor'], 'test')
        self.assertEqual(result['operationId'], preview['operationId'])
        self.assertFalse((target / '.govel' / 'plans' / f"{preview['operationId']}.json").exists(),
                         'the consumed plan is gone; the same approval cannot be spent twice')
        self.assertEqual(result['authoredInventoryHash'], receipt['authoredInventoryHash'])
        for relative, text in self.info['bodies'].items():
            self.assertEqual((target / '正文' / f'{relative}.md').read_text(encoding='utf-8'), text)
        # The moved project still refactors at its new locator, and nothing of the
        # move left a lock behind at either end.
        self.assertFalse(project_lock_observation(target)['locked'])
        _commit(target, preview_rename_book(target, new_name='神弃之冠'))
        self.assertEqual(load_structure(target)['bookName'], '神弃之冠')

    def test_a_relocation_commit_refuses_a_stale_preview(self) -> None:
        from scripts.core.project_relocate import (ProjectRelocateError, commit_relocate_project,
                                                   preview_relocate_project)
        target = self.base / '神弃之冠'
        preview = preview_relocate_project(self.root, target=str(target))
        (self.root / '设定/后加的.md').write_text('预览之后多出来的文件。\n', encoding='utf-8')
        with self.assertRaises(ProjectRelocateError) as caught:
            commit_relocate_project(self.root, operation_id=preview['operationId'],
                                    plan_hash=preview['planHash'], approved=True, actor='test')
        self.assertEqual(caught.exception.code, 'PLAN_HASH_MISMATCH')
        self.assertTrue(self.root.is_dir())

    def test_a_relocation_binds_the_issued_operation_and_its_ttl(self) -> None:
        """operationId and expiry are enforced, not decoration.

        A preview is an approval request with a lifetime and an identity. If the
        commit accepted any id, or an id whose TTL had passed, the bytes executed
        would not have to be the bytes the author saw.
        """
        from scripts.core import project_relocate
        from scripts.core.project_relocate import (ProjectRelocateError, commit_relocate_project,
                                                   preview_relocate_project)
        target = self.base / '神弃之冠'
        preview = preview_relocate_project(self.root, target=str(target))
        for kwargs, code in (
            ({'operation_id': 'OP-0000000000000000', 'plan_hash': preview['planHash']}, 'PLAN_NOT_FOUND'),
            ({'operation_id': preview['operationId'], 'plan_hash': 'deadbeef'}, 'PLAN_HASH_MISMATCH'),
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ProjectRelocateError) as caught:
                    commit_relocate_project(self.root, approved=True, actor='test', **kwargs)
                self.assertEqual(caught.exception.code, code)
        self.assertTrue(self.root.is_dir(), 'a refused commit moves nothing')
        self.assertFalse(target.exists())
        # Expired: the same plan, still byte-identical, is no longer committable.
        with patch('scripts.core.project_relocate.time.time',
                   return_value=preview['expiresAtEpoch'] + 1):
            with self.assertRaises(ProjectRelocateError) as caught:
                commit_relocate_project(self.root, operation_id=preview['operationId'],
                                        plan_hash=preview['planHash'], approved=True, actor='test')
        self.assertEqual(caught.exception.code, 'PLAN_EXPIRED')
        self.assertTrue(self.root.is_dir())


    def test_relocation_refuses_ambiguous_targets(self) -> None:
        from scripts.core.project_relocate import ProjectRelocateError, preview_relocate_project
        (self.base / '已存在').mkdir()
        for target, code in (
            (str(self.root), 'PROJECT_RELOCATE_TARGET_INVALID'),
            (str(self.root / '里面'), 'PROJECT_RELOCATE_TARGET_INVALID'),
            (str(self.base), 'PROJECT_RELOCATE_TARGET_INVALID'),
            (str(self.base / '已存在'), 'PROJECT_RELOCATE_TARGET_EXISTS'),
            (str(self.base / '缺父级' / '再深一层'), 'PROJECT_RELOCATE_PARENT_MISSING'),
            ('相对路径', 'PROJECT_RELOCATE_TARGET_INVALID'),
        ):
            with self.subTest(target=target):
                with self.assertRaises(ProjectRelocateError) as caught:
                    preview_relocate_project(self.root, target=target)
                self.assertEqual(caught.exception.code, code)

    def test_a_foreign_directory_is_not_a_project(self) -> None:
        from scripts.core.project_relocate import ProjectRelocateError, preview_relocate_project
        stranger = self.base / '不是项目'
        stranger.mkdir()
        with self.assertRaises(ProjectRelocateError) as caught:
            preview_relocate_project(stranger, target=str(self.base / '改名'))
        self.assertEqual(caught.exception.code, 'PROJECT_RELOCATE_NOT_A_PROJECT')

    def test_cross_device_move_fails_closed_instead_of_copying(self) -> None:
        from scripts.core.project_relocate import (ProjectRelocateError, preview_relocate_project)
        real_stat = os.stat
        devices = {str(self.root.resolve()): 1, str(self.base): 2}

        class OtherDevice:
            def __init__(self, inner, dev):
                self._inner, self.st_dev = inner, dev

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def fake_stat(path, *args, **kwargs):
            # The target's parent sits on another device: os.rename would raise EXDEV,
            # and a copy+delete fallback is exactly the half-applied state to avoid.
            inner = real_stat(path)
            return OtherDevice(inner, devices.get(str(Path(path)), inner.st_dev))

        with patch('scripts.core.project_relocate.os.stat', side_effect=fake_stat):
            with self.assertRaises(ProjectRelocateError) as caught:
                preview_relocate_project(self.root, target=str(self.base / '另一块盘'))
        self.assertEqual(caught.exception.code, 'PROJECT_RELOCATE_CROSS_DEVICE_UNSUPPORTED')
        self.assertTrue(self.root.is_dir(), 'fail closed means the project never moved')
        self.assertFalse((self.base / '另一块盘').exists())

    def test_a_pending_transaction_blocks_relocation(self) -> None:
        from scripts.core.project_relocate import ProjectRelocateError, preview_relocate_project
        pending = self.root / '.govel' / 'transactions'
        pending.mkdir(parents=True, exist_ok=True)
        (pending / 'TX-STUCK.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(ProjectRelocateError) as caught:
            preview_relocate_project(self.root, target=str(self.base / '等一等'))
        self.assertEqual(caught.exception.code, 'TRANSACTION_INCOMPLETE')

    def test_book_rename_with_folder_relocation_lands_both(self) -> None:
        from scripts.core.project_relocate import RECEIPT_PATH
        target = self.base / '神弃之冠'
        preview = preview_rename_book(self.root, new_name='神弃之冠', project_folder_target=str(target))
        self.assertEqual([row['field'] for row in preview['changes']], ['bookName', 'projectFolder'])
        self.assertEqual(preview['impact']['projectFolderRelocation']['targetRoot'], str(target))
        _commit(self.root, preview)
        self.assertTrue(target.is_dir())
        self.assertFalse(self.root.exists())
        self.assertEqual(load_structure(target)['bookName'], '神弃之冠')
        self.assertEqual(read_json(target / '.govel' / 'project.json')['bookName'], '神弃之冠')
        # The receipt keeps history and current locator apart, never rewriting either.
        journal = read_json(target / '.govel' / 'operations' / f"{preview['operationId']}.json")
        self.assertEqual(journal['impact']['projectFolderRelocation']['sourceRoot'], str(self.root))
        self.assertEqual([row['field'] for row in journal['changes']], ['bookName', 'projectFolder'])
        locator = read_json(target / RECEIPT_PATH)
        self.assertEqual(locator['rootAtOperation'], str(self.root))
        self.assertEqual(locator['currentLocator'], str(target))
        self.assertEqual(locator['projectId'], 'PRJ-REFACTOR-1')

    def test_combined_rename_rolls_the_folder_back_when_the_write_fails(self) -> None:
        target = self.base / '神弃之冠'
        preview = preview_rename_book(self.root, new_name='神弃之冠', project_folder_target=str(target))
        with patch('scripts.core.write_sets.checkpoint',
                   side_effect=lambda phase, relative=None: self._fail(phase, 'prepared')):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                _commit(self.root, preview)
        self.assertTrue(self.root.is_dir(), 'the folder came back')
        self.assertFalse(target.exists())
        self.assertEqual(load_structure(self.root)['bookName'], '山城旧事')
        self.assertEqual(read_json(self.root / '.govel' / 'project.json')['bookName'], '山城旧事')

    def test_a_failed_locator_receipt_write_rolls_the_book_and_the_folder_back(self) -> None:
        """Book rename, folder move and locator receipt: all three, or none.

        The receipt write is the last thing this transaction lands, which is exactly
        where a half-applied rename used to be reachable: the folder had already moved
        and the in-project book rename had already committed, so a failing receipt
        restored the path but not the name.

        The honest invariant is therefore more than "the folder is back". The author's
        approval must survive too — silently consuming a plan they already agreed to
        would lose their decision, not just the bytes. So this asserts that the old
        folder and both name sources are restored, that no receipt or journal was left
        behind, that no transaction manifest is orphaned, that the plan is still pending
        with the same hash, and that retrying it lands both halves.
        """
        from scripts.core.project_relocate import RECEIPT_PATH
        target = self.base / '神弃之冠'
        preview = preview_rename_book(self.root, new_name='神弃之冠', project_folder_target=str(target))
        # The receipt really is staged in the approved plan, which is what lets the
        # write-set engine roll it back with everything else.
        self.assertIn(RECEIPT_PATH, [row['path'] for row in preview['writeSet']])

        def fail_on_receipt(phase, relative=None):
            if phase == 'write' and relative == RECEIPT_PATH:
                raise RuntimeError('injected locator receipt failure')

        with patch('scripts.core.write_sets.checkpoint', side_effect=fail_on_receipt):
            with self.assertRaisesRegex(RuntimeError, 'injected locator receipt failure'):
                _commit(self.root, preview)
        self.assertTrue(self.root.is_dir(), 'the folder came back')
        self.assertFalse(target.exists())
        self.assertEqual(read_json(self.root / '.govel' / 'project.json')['bookName'], '山城旧事')
        self.assertEqual(load_structure(self.root)['bookName'], '山城旧事')
        self.assertFalse((self.root / RECEIPT_PATH).exists(), 'no receipt for a move that did not happen')
        self.assertFalse((self.root / '.govel' / 'operations' / f"{preview['operationId']}.json").exists(),
                         'no journal for a commit that never landed')
        self.assertFalse(list((self.root / '.govel' / 'transactions').glob('*.json')),
                         'no orphan transaction manifest')
        self.assertFalse(project_lock_observation(self.root)['locked'])
        pending = [row for row in _pending(self.root) if row['operationId'] == preview['operationId']]
        self.assertEqual([row['planHash'] for row in pending], [preview['planHash']],
                         'the approval the author gave is still on disk, unchanged')
        self.assertEqual(pending[0]['operation'], 'refactor_rename_book')
        # Same approval, second attempt: this time it lands, both halves together.
        _commit(self.root, preview)
        self.assertTrue(target.is_dir())
        self.assertFalse(self.root.exists())
        self.assertEqual(load_structure(target)['bookName'], '神弃之冠')
        self.assertEqual(read_json(target / '.govel' / 'project.json')['bookName'], '神弃之冠')
        receipt = read_json(target / RECEIPT_PATH)
        self.assertEqual(receipt['rootAtOperation'], str(self.root))
        self.assertEqual(receipt['currentLocator'], str(target))
        self.assertEqual(receipt['operationId'], preview['operationId'])
        self.assertEqual(len(receipt['history']), 1)

    def test_a_relocation_holds_the_lock_until_move_verify_and_receipt_are_done(self) -> None:
        """Relocation is serialized against ordinary mutations, not merely checked.

        `preview` used to look for a lock and then rename without ever taking one, so
        a structure commit starting in that window was lost. A second PID now tries to
        commit three times while the relocation is in flight — before the rename, after
        it (where the lock file has travelled with the folder), and again once the
        receipt has landed but the lock is still held — and every attempt must be
        refused. Afterwards the rival plan must commit normally: that is what proves
        the lock was released exactly once and not left stale at either end.
        """
        from scripts.core import project_relocate
        from scripts.core.project_relocate import (RECEIPT_PATH, commit_relocate_project,
                                                   preview_relocate_project)
        rival = preview_rename_volume(self.root, volume_id=self.info['volumeA'], new_title='第一卷：新名')
        target = self.base / '神弃之冠'
        preview = preview_relocate_project(self.root, target=str(target))
        real_rename = os.rename
        real_apply = project_relocate.apply_write_set
        observed: list[str] = []

        def observed_rename(source, destination):
            observed.append(_other_process_commit(self.root, rival))
            real_rename(source, destination)
            observed.append(_other_process_commit(Path(destination), rival))

        def apply_then_observe(*arguments, **keywords):
            rows = real_apply(*arguments, **keywords)
            observed.append(_other_process_commit(target, rival))
            return rows

        with patch('scripts.core.project_relocate.os.rename', side_effect=observed_rename), \
                patch('scripts.core.project_relocate.apply_write_set', side_effect=apply_then_observe):
            result = commit_relocate_project(self.root, operation_id=preview['operationId'],
                                             plan_hash=preview['planHash'], approved=True, actor='test')
        self.assertEqual(result['code'], 'PROJECT_RELOCATED')
        self.assertEqual(observed, ['PROJECT_LOCKED'] * 3,
                         'mid-relocation the project is locked at the old locator, the new one, '
                         'and after the receipt has landed')
        self.assertTrue((target / RECEIPT_PATH).is_file(), 'the third probe ran after the receipt')
        self.assertFalse(project_lock_observation(target)['locked'], 'no stale lock at the new root')
        self.assertFalse(self.root.exists(), 'and nothing was left behind at the old one')
        # The lock was released, not destroyed: the blocked commit now goes through.
        _commit(target, rival)
        self.assertEqual(load_structure(target)['volumes'][0]['title'], '第一卷：新名')

    @staticmethod
    def _fail(phase, expected):
        if phase == expected:
            raise RuntimeError('injected failure')


if __name__ == '__main__':
    unittest.main()
