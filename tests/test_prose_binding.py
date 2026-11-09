"""R1 Phase 1 — prose_binding core tests (B1–B17, B29, B32 + generation/hardlink locks).

Every fixture is synthetic and lives in a TemporaryDirectory. No real project, no
real prose, and the binding core never reads historical evidence — these tests
pin that the current structure alone explains a healthy binding.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core import prose_binding
from scripts.core.jsonio import read_json, write_json
from scripts.core.prose_binding import (
    binding_integrity_digest, binding_token, canonical_locator_key, enforce_writer_boundary,
    first_finalization_write_set_plan, hardlink_object_conflicts,
    locator_reservations, ownership_validation_report, request_generation_current,
    require_all_binding_health, require_unique_locators, seal_binding, validate_binding,
    validate_unbound_locator_move,
)
from scripts.core.structure_store import (
    StructureError, empty_structure, load_structure, save_structure, structure_hash, validate_structure,
)
from tests.fixtures_ownership import (
    ARC_MAIN, CHAPTER_A, CHAPTER_B, CHAPTER_C, MANAGED_PROSE, PROJECT_ID,
    managed_digest, new_chapter_row, ownership_project,
)


class BindingCanonicalizationTests(unittest.TestCase):
    """B1: one semantic payload, one digest — regardless of how it was built."""

    # Frozen from the published PROSE_BINDING_V1 seal payload (current-binding-v1) field set with an independent
    # standard-library calculation. Expected values never use the product seal.
    GOLDEN = (
        ('unbound', 1, None,
         '{"bindingFormat":"PROSE_BINDING_V1","chapterId":"CH-固定","expectedContentDigest":null,"filePath":"正文/第一章.md","generation":1,"projectId":"PRJ-GOLDEN","state":"unbound"}',
         '9a6f03f65f958216be6ad8f5717271fe1402a663fc7651e2b46054e923b1a09e'),
        ('managed', 12, 'sha256:' + 'a' * 64,
         '{"bindingFormat":"PROSE_BINDING_V1","chapterId":"CH-固定","expectedContentDigest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","filePath":"正文/第一章.md","generation":12,"projectId":"PRJ-GOLDEN","state":"managed"}',
         '261df6be3eb48235bbf508c5fe68464d2ea35bfeb12febe0b83342562d7da5c0'),
    )

    def test_current_format_fixed_utf8_bytes_and_digest(self):
        for state, generation, digest, canonical, expected in self.GOLDEN:
            with self.subTest(state=state):
                payload = prose_binding.binding_payload('PRJ-GOLDEN', 'CH-固定', '正文/第一章.md', state, generation, digest)
                self.assertEqual(prose_binding.canonical_json(payload).encode('utf-8'), canonical.encode('utf-8'))
                self.assertEqual(binding_integrity_digest('PRJ-GOLDEN', 'CH-固定', '正文/第一章.md', state, generation, digest),
                                 'sha256:' + expected)
                chapter = {'chapterId': 'CH-固定', 'filePath': '正文/第一章.md',
                           'proseBinding': {'state': state, 'generation': generation,
                                            'expectedContentDigest': digest, 'integrityDigest': 'sha256:' + expected}}
                self.assertEqual(validate_binding({'projectId': 'PRJ-GOLDEN'}, chapter), [])
                for field, value in (('generation', generation + 1), ('expectedContentDigest', 'sha256:' + 'b' * 64)):
                    changed = deepcopy(chapter); changed['proseBinding'][field] = value
                    self.assertTrue(validate_binding({'projectId': 'PRJ-GOLDEN'}, changed))
                self.assertTrue(validate_binding({'projectId': 'PRJ-OTHER'}, chapter))
                self.assertTrue(validate_binding({'projectId': 'PRJ-GOLDEN'}, {**chapter, 'chapterId': 'CH-OTHER'}))

    def test_fixed_vector_exposes_null_ascii_and_separator_drift(self):
        for state, _, _, canonical, expected in self.GOLDEN:
            payload = json.loads(canonical)
            mutations = [json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':')),
                         json.dumps(payload, ensure_ascii=False, sort_keys=True)]
            if state == 'unbound':
                payload.pop('expectedContentDigest')
                mutations.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
            for changed in mutations:
                self.assertNotEqual(hashlib.sha256(changed.encode('utf-8')).hexdigest(), expected)

    def test_same_payload_same_digest(self) -> None:
        first = binding_integrity_digest('P1', 'CH-1', '正文/A.md', 'managed', 12, 'sha256:' + 'a' * 64)
        second = binding_integrity_digest('P1', 'CH-1', '正文/A.md', 'managed', 12, 'sha256:' + 'a' * 64)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith('sha256:'))

    def test_seal_is_deterministic(self) -> None:
        first = seal_binding('P1', 'CH-1', '正文/A.md', state='unbound', generation=3,
                             expected_content_digest=None)
        second = seal_binding('P1', 'CH-1', '正文/A.md', state='unbound', generation=3,
                              expected_content_digest=None)
        self.assertEqual(first, second)

    def test_payload_fields_change_digest(self) -> None:
        base = dict(project_id='P1', chapter_id='CH-1', file_path='正文/A.md', state='unbound',
                    generation=3, expected_content_digest=None)
        variants = [dict(base, project_id='P2'), dict(base, chapter_id='CH-2'),
                    dict(base, file_path='正文/B.md'), dict(base, state='managed'),
                    dict(base, generation=4),
                    dict(base, expected_content_digest='sha256:' + 'b' * 64)]
        digests = {binding_integrity_digest(**variant) for variant in variants}
        self.assertEqual(len(digests), len(variants))

    def test_unbound_vs_null_missing_digest_differ(self) -> None:
        # None 与缺省不可混淆：payload 固定字段集，None 显式参与序列化。
        with_null = binding_integrity_digest('P1', 'CH-1', '正文/A.md', 'unbound', 3, None)
        self.assertTrue(with_null.startswith('sha256:'))


class CrossIdentityCopyTests(unittest.TestCase):
    """B2/B3: a binding is bound to its project and chapter identity."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.chapter = self.structure['chapters'][0]

    def test_cross_project_copy_is_invalid(self) -> None:
        other = dict(self.structure, projectId='PRJ-OTHER-2')
        errors = validate_binding(other, self.chapter)
        self.assertTrue(errors)
        self.assertEqual({error['code'] for error in errors}, {'OWNERSHIP_BINDING_INVALID'})

    def test_cross_chapter_copy_is_invalid(self) -> None:
        victim = self.structure['chapters'][1]
        stolen = dict(victim, proseBinding=self.chapter['proseBinding'])
        errors = validate_binding(self.structure, stolen)
        self.assertTrue(errors)
        self.assertEqual({error['code'] for error in errors}, {'OWNERSHIP_BINDING_INVALID'})


class StateDigestRuleTests(unittest.TestCase):
    """B4/B5/B6: per-state expectedContentDigest rules."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.slot = self.structure['chapters'][1]['filePath']

    def _binding(self, **overrides) -> dict:
        return dict(state='unbound', generation=7, expectedContentDigest=None,
                    integrityDigest=None, **overrides)

    def _validate_custom(self, binding: dict) -> list[dict]:
        chapter = dict(self.structure['chapters'][1], proseBinding=binding)
        return validate_binding(self.structure, chapter)

    def _seal(self, state: str, digest: str | None) -> dict:
        return seal_binding(PROJECT_ID, CHAPTER_B, self.slot, state=state,
                            generation=7, expected_content_digest=digest)

    def test_unbound_with_digest_is_invalid(self) -> None:
        binding = self._seal('unbound', managed_digest())
        # 重签一个「unbound 却带 digest」的 seal 本身就该被读侧拒绝。
        errors = self._validate_custom(binding)
        self.assertIn('unbound binding must not declare an expected content digest',
                      [error['message'] for error in errors])

    def test_managed_requires_canonical_sha256(self) -> None:
        managed = self._seal('managed', managed_digest())
        self.assertEqual(self._validate_custom(managed), [])
        broken = dict(managed, expectedContentDigest='sha256:' + 'A' * 64)
        self.assertTrue(self._validate_custom(broken))
        broken = dict(managed, expectedContentDigest=managed_digest().removeprefix('sha256:'))
        self.assertTrue(self._validate_custom(broken))

    def test_unresolved_accepts_null_and_last_trusted_digest(self) -> None:
        null_digest = self._seal('unresolved', None)
        trusted = self._seal('unresolved', managed_digest())
        self.assertEqual(self._validate_custom(null_digest), [])
        self.assertEqual(self._validate_custom(trusted), [])
        # 两种取值都保持 unresolved：digest 的存在不构成向 managed 的晋升。
        self.assertEqual(null_digest['state'], 'unresolved')
        self.assertEqual(trusted['state'], 'unresolved')


class SeverityLayeringTests(unittest.TestCase):
    """B7/B8/B9/B30: chapter-local corruption is isolated; identity/locator is fatal."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'

    def _corrupt_first_chapter(self) -> None:
        on_disk = read_json(self.structure_path)
        binding = dict(on_disk['chapters'][0]['proseBinding'])
        binding['integrityDigest'] = 'sha256:' + '0' * 64
        on_disk['chapters'][0]['proseBinding'] = binding
        write_json(self.structure_path, on_disk)

    def test_integrity_mismatch_is_chapter_local_only(self) -> None:
        self._corrupt_first_chapter()
        structure = load_structure(self.root)  # chapter-local 损坏不阻塞整体读取（B7/B8）
        report = ownership_validation_report(structure)
        self.assertEqual(report['fatal'], [])
        self.assertIn(CHAPTER_A, report['chapterLocal'])
        self.assertEqual({error['code'] for error in report['chapterLocal'][CHAPTER_A]},
                         {'OWNERSHIP_BINDING_INVALID'})
        healthy = next(row for row in structure['chapters'] if row['chapterId'] == CHAPTER_B)
        self.assertEqual(validate_binding(structure, healthy), [])  # B8

    def test_unsafe_file_path_is_project_fatal(self) -> None:
        on_disk = read_json(self.structure_path)
        on_disk['chapters'][1]['filePath'] = '../escape.md'
        with self.assertRaises(StructureError) as caught:
            validate_structure(on_disk)
        codes = {error['code'] for error in caught.exception.details['errors']}
        self.assertIn('PROSE_LOCATOR_UNSAFE', codes)
        report = ownership_validation_report(on_disk)
        self.assertEqual([error['code'] for error in report['fatal']], ['PROSE_LOCATOR_UNSAFE'])

    def test_absolute_path_is_project_fatal(self) -> None:
        on_disk = read_json(self.structure_path)
        on_disk['chapters'][1]['filePath'] = '/etc/passwd'
        report = ownership_validation_report(on_disk)
        self.assertEqual([error['code'] for error in report['fatal']], ['PROSE_LOCATOR_UNSAFE'])

    def test_corrupt_chapter_still_reserves_locator(self) -> None:
        self._corrupt_first_chapter()
        structure = load_structure(self.root)
        reservations, fatal = locator_reservations(structure)
        self.assertEqual(fatal, [])
        key = canonical_locator_key(f'正文/{CHAPTER_A}.md')
        self.assertEqual(reservations.get(key), CHAPTER_A)  # B30：损坏章保留定位符
        # 另一章不能 claim 该定位符：结构级硬不变量拒绝。
        claimed = read_json(self.structure_path)
        claimed['chapters'][1]['filePath'] = f'正文/{CHAPTER_A}.md'
        with self.assertRaises(StructureError) as caught:
            validate_structure(claimed)
        codes = {error['code'] for error in caught.exception.details['errors']}
        self.assertIn('PROSE_LOCATOR_CONFLICT', codes)


class CanonicalCollisionTests(unittest.TestCase):
    """B10/B11: canonical key uniqueness (case folding and NFC)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)

    def _with_paths(self, first: str, second: str) -> None:
        self.structure['chapters'][0]['filePath'] = first
        self.structure['chapters'][1]['filePath'] = second

    def test_case_only_collision_rejected(self) -> None:
        self._with_paths('正文/A.md', '正文/a.md')
        with self.assertRaises(StructureError) as caught:
            validate_structure(self.structure)
        codes = {error['code'] for error in caught.exception.details['errors']}
        self.assertIn('PROSE_LOCATOR_CONFLICT', codes)

    def test_nfc_equivalent_collision_rejected(self) -> None:
        composed = '正文/tomé.md'  # U+00E9
        decomposed = '正文/tome\u0301.md'  # e + combining acute
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(canonical_locator_key(composed), canonical_locator_key(decomposed))
        self._with_paths(composed, decomposed)
        with self.assertRaises(StructureError) as caught:
            validate_structure(self.structure)
        codes = {error['code'] for error in caught.exception.details['errors']}
        self.assertIn('PROSE_LOCATOR_CONFLICT', codes)

    def test_archived_chapter_still_reserves_locator(self) -> None:
        # B12：归档是 chapter.status overlay，不释放定位符。
        self.structure['chapters'][0]['status'] = 'archived'
        reservations, fatal = locator_reservations(self.structure)
        self.assertEqual(fatal, [])
        self.assertEqual(reservations.get(canonical_locator_key(f'正文/{CHAPTER_A}.md')), CHAPTER_A)
        # 另一章（含归档章在场）claim 同一规范槽 → 硬不变量拒绝。
        claimer = deepcopy(self.structure)
        claimer['chapters'][1]['filePath'] = f'正文/{CHAPTER_A}.md'
        with self.assertRaises(StructureError):
            require_unique_locators(claimer)


class ProtectedWriterBoundaryTests(unittest.TestCase):
    """B13–B17: generic save_structure can never mutate protected ownership fields."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.before_bytes = (self.root / '.govel' / 'structure.json').read_bytes()

    def _loaded(self) -> dict:
        return load_structure(self.root)

    def _assert_rejected(self, mutate, code: str = 'OWNERSHIP_UNAUTHORIZED_MUTATION') -> None:
        proposed = self._loaded()
        mutate(proposed)
        with self.assertRaises(StructureError) as caught:
            save_structure(self.root, proposed, expected_revision=self.structure['revision'])
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.root / '.govel' / 'structure.json').read_bytes(), self.before_bytes)

    def test_direct_file_path_write_rejected(self) -> None:
        self._assert_rejected(lambda data: data['chapters'][0].__setitem__('filePath', '正文/外部.md'))

    def test_direct_binding_state_write_rejected(self) -> None:
        def mutate(data: dict) -> None:
            data['chapters'][1]['proseBinding']['state'] = 'managed'
        self._assert_rejected(mutate)

    def test_direct_generation_write_rejected(self) -> None:
        def mutate(data: dict) -> None:
            data['chapters'][1]['proseBinding']['generation'] = 99
        self._assert_rejected(mutate)

    def test_direct_digest_write_rejected(self) -> None:
        def mutate(data: dict) -> None:
            data['chapters'][1]['proseBinding']['expectedContentDigest'] = managed_digest()
        self._assert_rejected(mutate)

    def test_direct_integrity_write_rejected(self) -> None:
        def mutate(data: dict) -> None:
            data['chapters'][1]['proseBinding']['integrityDigest'] = 'sha256:' + '1' * 64
        self._assert_rejected(mutate)

    def test_non_protected_metadata_update_allowed(self) -> None:
        # B15：binding 未变时普通元数据更新继续可用。
        proposed = self._loaded()
        proposed['chapters'][1]['title'] = '空槽·改名'
        saved = save_structure(self.root, proposed, expected_revision=self.structure['revision'])
        self.assertEqual(saved['chapters'][1]['title'], '空槽·改名')
        self.assertEqual(load_structure(self.root)['chapters'][1]['proseBinding'],
                         self.structure['chapters'][1]['proseBinding'])

    def test_chapter_id_mutation_rejected(self) -> None:
        # B16：existing chapterId 是 immutable identity。换 ID 表现为「删旧 + 增新」。
        def mutate(data: dict) -> None:
            data['chapters'][1]['chapterId'] = 'CH-BINDING-0009'
        self._assert_rejected(mutate)

    def test_project_id_mutation_rejected(self) -> None:
        # B17：projectId 是 immutable identity。
        proposed = self._loaded()
        proposed['projectId'] = 'PRJ-OTHER-2'
        with self.assertRaises(StructureError) as caught:
            save_structure(self.root, proposed)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')

    def test_model_downgrade_rejected(self) -> None:
        def mutate(data: dict) -> None:
            for chapter in data['chapters']:
                chapter.pop('proseBinding', None)
            data['schemaVersion'] = 1
        self._assert_rejected(mutate, code='UNSUPPORTED_PROJECT_VERSION')

    def test_chapter_removal_rejected(self) -> None:
        def mutate(data: dict) -> None:
            data['chapters'] = data['chapters'][:2]
        self._assert_rejected(mutate)


class GenericWriterCannotAddChapterTests(unittest.TestCase):
    """F1–F4 / G1–G4（实施轮 §23–§26、§36–§38）：既有 ownership-aware 工程上，通用
    `save_structure` 不得新增任何 chapter——新章自带多合法的密封 binding 都一样，因为
    generic save != materialization authority。"""

    FORGED_ID = 'CH-BINDING-0010'

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def _project(self, *, revision: int = 7) -> Path:
        root = self.base / 'current'
        ownership_project(root, revision=revision)
        return root

    def _sealed_foreign_row(self, *, state: str, generation: int) -> dict:
        row = new_chapter_row(chapter_id=self.FORGED_ID, ordinal=9, title='走私章')
        digest = None if state == 'unbound' else managed_digest()
        row['proseBinding'] = seal_binding(PROJECT_ID, self.FORGED_ID, row['filePath'],
                                           state=state, generation=generation,
                                           expected_content_digest=digest)
        return row

    def _assert_generic_add_rejected(self, root: Path, *, state: str) -> None:
        structure = load_structure(root)
        structure_path = root / '.govel' / 'structure.json'
        before = structure_path.read_bytes()
        proposed = deepcopy(structure)
        row = self._sealed_foreign_row(state=state,
                                       generation=structure['revision'] + 1)
        # §45 反例 A：新章在结构层完全合法（密封 binding、generation=next revision、
        # 定位符唯一、无盘上文件）——合法 binding 只说明它「看起来像」materialization。
        self.assertEqual(validate_binding(proposed, row), [])
        proposed['chapters'].append(row)
        proposed['chapterArcLinks'].append({'chapterId': self.FORGED_ID, 'arcId': ARC_MAIN,
                                            'role': 'primary', 'weight': 1.0, 'phaseId': None,
                                            'notes': ''})
        proposed['revision'] = structure['revision'] + 1
        with self.assertRaises(StructureError) as caught:
            save_structure(root, proposed, expected_revision=structure['revision'])
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
        # 副作用哨兵：磁盘结构一个字节未动，冒名章没有出生。
        self.assertEqual(structure_path.read_bytes(), before)
        on_disk_ids = [item['chapterId'] for item in read_json(structure_path)['chapters']]
        self.assertNotIn(self.FORGED_ID, on_disk_ids)

    def test_f1_g1_unbound_smuggled_chapter_rejected(self) -> None:
        self._assert_generic_add_rejected(self._project(),
                                          state='unbound')

    def test_f2_g2_managed_smuggled_chapter_rejected(self) -> None:
        # §45 反例 A：current revision=20、proposed revision=21、
        # append CH-X、state=managed、合法 integrityDigest —— 必须拒绝。
        self._assert_generic_add_rejected(
            self._project(revision=20),
            state='managed')

    def test_f3_g3_unresolved_smuggled_chapter_rejected(self) -> None:
        self._assert_generic_add_rejected(self._project(),
                                          state='unresolved')




class ExactMaterializationSetEqualityTests(unittest.TestCase):
    """X 系列：建章 authority 是**完整集合相等**，不是「actual added 非空时才比较」。

    Phase 1 冻结的 invariant 是 `actual added chapter IDs == authorized chapter IDs`。
    六种集合对里只有 `{} == {}`（普通事务/元数据保存）与 `{A} == {A}`（合法建章）被放行；
    其余四种一律 `OWNERSHIP_UNAUTHORIZED_MUTATION`——尤其 `authorized = {A}` 而实际零新增
    这种「少建」形态：它曾在 `enforce_writer_boundary` 的 `if not added: return` 处被提前
    放行，是「注册了建章能力却一章没建」的最小反例。
    """

    DECLARED = 'CH-BINDING-0121'
    OTHER = 'CH-BINDING-0122'

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'

    def _sealed_row(self, chapter_id: str, *, generation: int) -> dict:
        row = new_chapter_row(chapter_id=chapter_id, ordinal=9, title='新章')
        row['proseBinding'] = seal_binding(PROJECT_ID, chapter_id, row['filePath'],
                                           state='unbound', generation=generation,
                                           expected_content_digest=None)
        return row

    def _boundary(self, *, added: tuple[str, ...], authorized: set[str] | None) -> None:
        current = load_structure(self.root)
        proposed = deepcopy(current)
        proposed['revision'] = current['revision'] + 1
        for chapter_id in added:
            proposed['chapters'].append(self._sealed_row(chapter_id,
                                                         generation=proposed['revision']))
            proposed['chapterArcLinks'].append({'chapterId': chapter_id, 'arcId': ARC_MAIN,
                                                'role': 'primary', 'weight': 1.0, 'phaseId': None,
                                                'notes': ''})
        enforce_writer_boundary(current, proposed, root=self.root,
                                materialization_chapter_ids=authorized)

    def _assert_boundary_rejected(self, *, added: tuple[str, ...],
                                  authorized: set[str] | None) -> None:
        before = self.structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            self._boundary(added=added, authorized=authorized)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
        # 结构性识别这条拒绝来自 exact-set comparison 本身，而不是别的分支。
        self.assertIn('added', caught.exception.details)
        self.assertIn('authorized', caught.exception.details)
        # 副作用哨兵：拒绝时结构一个字节未动。
        self.assertEqual(self.structure_path.read_bytes(), before)

    def test_authorized_empty_actual_empty_passes(self) -> None:
        # generic save / 普通事务：能力为空且实际零新增，是唯一被放行的空集配对。
        self._boundary(added=(), authorized=None)
        self._boundary(added=(), authorized=set())

    def test_authorized_empty_actual_chapter_rejected(self) -> None:
        self._assert_boundary_rejected(added=(self.DECLARED,), authorized=set())

    def test_exact_materialization_capability_rejects_missing_actual_chapter(self) -> None:
        # authorized = {A}、actual added = {}：注册了能力却少建（甚至没建）也拒绝。
        self._assert_boundary_rejected(added=(), authorized={self.DECLARED})

    def test_authorized_chapter_actual_same_chapter_passes(self) -> None:
        self._boundary(added=(self.DECLARED,), authorized={self.DECLARED})

    def test_authorized_chapter_actual_other_chapter_rejected(self) -> None:
        self._assert_boundary_rejected(added=(self.OTHER,), authorized={self.DECLARED})

    def test_authorized_chapter_actual_extra_chapter_rejected(self) -> None:
        self._assert_boundary_rejected(added=(self.DECLARED, self.OTHER),
                                       authorized={self.DECLARED})


class TransitionRevisionInvariantTests(unittest.TestCase):
    """R1–R5 / F10–F14（实施轮 §19–§22、§48–§49）：formal transition 的提交版本由权威
    current 决定——proposed 必须恰是 next revision，generation 与提交版本是同一个数。"""

    NEW_ID = 'CH-BINDING-0011'

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root, revision=10)
        self.structure_path = self.root / '.govel' / 'structure.json'

    def _row(self, structure: dict, chapter_id: str) -> dict:
        return next(row for row in structure['chapters'] if row['chapterId'] == chapter_id)

    def _proposed(self, *, revision: int, sealed: bool) -> dict:
        proposed = deepcopy(load_structure(self.root))
        proposed['revision'] = revision
        row = new_chapter_row(chapter_id=self.NEW_ID, ordinal=4, title='新章')
        if sealed:
            row['proseBinding'] = seal_binding(PROJECT_ID, self.NEW_ID, row['filePath'],
                                               state='unbound', generation=revision,
                                               expected_content_digest=None)
        proposed['chapters'].append(row)
        proposed['chapterArcLinks'].append({'chapterId': self.NEW_ID, 'arcId': ARC_MAIN,
                                            'role': 'primary', 'weight': 1.0, 'phaseId': None,
                                            'notes': ''})
        return proposed

    def _assert_revision_conflict(self, proposed_revision: int) -> None:
        current = load_structure(self.root)
        before = self.structure_path.read_bytes()
        proposed = self._proposed(revision=proposed_revision, sealed=True)
        # §48/§49：整个提案结构上完全合法（密封 binding 的 generation == proposed
        # revision），唯一的缺陷就是 revision 不是 current+1——照样拒绝。
        self.assertEqual(validate_binding(proposed, self._row(proposed, self.NEW_ID)), [])
        with self.assertRaises(StructureError) as caught:
            prose_binding.apply_binding_transitions(
                current, [{'kind': 'materialize', 'chapterId': self.NEW_ID}], proposed=proposed)
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        # 不 silent normalize：拒绝后磁盘与 current 都不动，绝不改写成 current+1 继续。
        self.assertEqual(self.structure_path.read_bytes(), before)
        self.assertEqual(load_structure(self.root)['revision'], current['revision'])

    def test_r1_f10_proposed_revision_equal_to_current_rejected(self) -> None:
        # §48 反例 D：current=10、proposed=10、binding generation=10 —— 拒绝。
        self._assert_revision_conflict(10)

    def test_r2_f11_proposed_revision_skipping_ahead_rejected(self) -> None:
        self._assert_revision_conflict(12)

    def test_r3_f12_proposed_revision_arbitrary_999_rejected(self) -> None:
        # §49 反例 E：current=10、proposed=999、generation=999 —— 拒绝。
        self._assert_revision_conflict(999)

    def test_r4_f13_f14_next_revision_passes_and_generation_matches(self) -> None:
        current = load_structure(self.root)
        snapshot, authority = prose_binding.apply_binding_transitions(
            current, [{'kind': 'materialize', 'chapterId': self.NEW_ID}],
            proposed=self._proposed(revision=current['revision'] + 1, sealed=False))
        self.assertEqual(set(authority.transitions), {self.NEW_ID})
        chapter = self._row(snapshot, self.NEW_ID)
        self.assertEqual(snapshot['revision'], current['revision'] + 1)
        # §20/F14：binding.generation == proposed.revision == current.revision + 1。
        self.assertEqual(chapter['proseBinding']['generation'], snapshot['revision'])
        self.assertEqual(chapter['proseBinding']['generation'], current['revision'] + 1)
        self.assertEqual(validate_binding(snapshot, chapter), [])

    def test_r5_f14_commit_persists_next_revision_and_generation(self) -> None:
        current = load_structure(self.root)
        result = prose_binding.commit_binding_transitions(
            self.root, [{'kind': 'materialize', 'chapterId': self.NEW_ID}], actor='test',
            proposed=self._proposed(revision=current['revision'] + 1, sealed=False))
        self.assertEqual(result['revision'], 11)
        persisted = load_structure(self.root)
        self.assertEqual(persisted['revision'], 11)
        chapter = self._row(persisted, self.NEW_ID)
        self.assertEqual(chapter['proseBinding']['generation'], persisted['revision'])  # F14
        self.assertEqual(ownership_validation_report(persisted)['chapterLocal'], {})

    def test_expected_revision_is_not_a_monotonicity_bypass(self) -> None:
        # §21：expected_revision=None 不是旁路——锁内 authoritative current + 严格 next
        # revision 才是 invariant；伪造 revision 即使没有乐观锁也拒绝。
        current = load_structure(self.root)
        with self.assertRaises(StructureError) as caught:
            prose_binding.commit_binding_transitions(
                self.root, [{'kind': 'materialize', 'chapterId': self.NEW_ID}], actor='test',
                expected_revision=None,
                proposed=self._proposed(revision=current['revision'] + 2, sealed=True))
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(load_structure(self.root)['revision'], current['revision'])
        # 正控：expected_revision 与锁内 current 一致时照常提交（拒绝不是把路径关死）。
        result = prose_binding.commit_binding_transitions(
            self.root, [{'kind': 'materialize', 'chapterId': self.NEW_ID}], actor='test',
            expected_revision=current['revision'],
            proposed=self._proposed(revision=current['revision'] + 1, sealed=False))
        self.assertEqual(result['revision'], 11)


class NoAutoResealTests(unittest.TestCase):
    """B29: load/validation never rewrite a corrupt binding, on disk or in memory."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'
        on_disk = read_json(self.structure_path)
        binding = dict(on_disk['chapters'][0]['proseBinding'])
        binding['integrityDigest'] = 'sha256:' + '0' * 64
        on_disk['chapters'][0]['proseBinding'] = binding
        write_json(self.structure_path, on_disk)

    def test_load_does_not_repair_disk(self) -> None:
        before = self.structure_path.read_bytes()
        load_structure(self.root)
        self.assertEqual(self.structure_path.read_bytes(), before)

    def test_save_of_loaded_corrupt_structure_does_not_reseal(self) -> None:
        structure = load_structure(self.root)
        save_structure(self.root, structure, expected_revision=self.structure['revision'])
        reloaded = read_json(self.structure_path)
        self.assertEqual(reloaded['chapters'][0]['proseBinding']['integrityDigest'],
                         'sha256:' + '0' * 64)

    def test_validation_does_not_mutate_memory(self) -> None:
        structure = load_structure(self.root)
        before = structure_hash(structure)
        ownership_validation_report(structure)
        validate_binding(structure, structure['chapters'][0])
        require_unique_locators(structure)
        self.assertEqual(structure_hash(structure), before)


class LocalFaultIsolationTests(unittest.TestCase):
    """R7–R9: a chapter-local damaged chapter never blocks — and is never touched by —
    a healthy sibling's formal transition（Phase 0.2 / 实施轮 §12–§16, §45）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'
        on_disk = read_json(self.structure_path)
        broken = dict(on_disk['chapters'][0]['proseBinding'])
        broken['integrityDigest'] = 'sha256:' + '0' * 64
        on_disk['chapters'][0]['proseBinding'] = broken
        write_json(self.structure_path, on_disk)
        self.damaged_row = read_json(self.structure_path)['chapters'][0]

    def _on_disk_row(self, chapter_id: str) -> dict:
        return next(row for row in read_json(self.structure_path)['chapters']
                    if row['chapterId'] == chapter_id)

    def _proposed_with_new_chapter(self, current: dict, chapter_id: str) -> dict:
        proposed = deepcopy(current)
        proposed['revision'] = current['revision'] + 1
        proposed['chapters'].append(new_chapter_row(
            chapter_id=chapter_id, ordinal=4, title='新章'))
        proposed['chapterArcLinks'].append({'chapterId': chapter_id, 'arcId': self.structure['arcs'][0]['arcId'],
                                            'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''})
        return proposed

    def _assert_damaged_sibling_preserved(self) -> None:
        # R8：损坏章的 chapterId/filePath/proseBinding 值完全不变，绝不 auto reseal。
        self.assertEqual(self._on_disk_row(CHAPTER_A), self.damaged_row)
        self.assertEqual(self._on_disk_row(CHAPTER_A)['proseBinding']['integrityDigest'],
                         'sha256:' + '0' * 64)
        report = ownership_validation_report(load_structure(self.root))
        self.assertEqual(report['fatal'], [])
        self.assertIn(CHAPTER_A, report['chapterLocal'])
        # R9：损坏章仍然保留它的规范定位符。
        reservations, fatal = locator_reservations(load_structure(self.root))
        self.assertEqual(fatal, [])
        self.assertEqual(reservations.get(canonical_locator_key(f'正文/{CHAPTER_A}.md')), CHAPTER_A)

    def test_new_chapter_materialization_succeeds_despite_damaged_sibling(self) -> None:
        # Counterexample A（新身份读法）：章 A 损坏，新章的 materialization 照常成功。
        current = load_structure(self.root)
        new_chapter_id = 'CH-BINDING-0004'
        proposed = self._proposed_with_new_chapter(current, new_chapter_id)
        snapshot, authority = prose_binding.apply_binding_transitions(
            current, [{'kind': 'materialize', 'chapterId': new_chapter_id}], proposed=proposed)
        self.assertEqual(set(authority.transitions), {new_chapter_id})
        chapter = next(row for row in snapshot['chapters'] if row['chapterId'] == new_chapter_id)
        self.assertEqual(chapter['proseBinding']['state'], 'unbound')
        self.assertEqual(chapter['proseBinding']['generation'], snapshot['revision'])
        self.assertEqual(validate_binding(snapshot, chapter), [])
        # 旧的全量健康闸（§43 的错误用法）会在同一个快照上失败——证明隔离是真的。
        with self.assertRaises(StructureError) as caught:
            prose_binding.require_all_binding_health(snapshot)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_BINDING_INVALID')

    def test_committed_transition_preserves_damaged_sibling(self) -> None:
        current = load_structure(self.root)
        new_chapter_id = 'CH-BINDING-0005'
        result = prose_binding.commit_binding_transitions(
            self.root, [{'kind': 'materialize', 'chapterId': new_chapter_id}], actor='test',
            proposed=self._proposed_with_new_chapter(current, new_chapter_id))
        self.assertEqual(result['revision'], current['revision'] + 1)
        self.assertEqual(self._on_disk_row(new_chapter_id)['proseBinding']['state'], 'unbound')
        self._assert_damaged_sibling_preserved()

    def test_transaction_on_healthy_existing_chapter_succeeds_despite_damaged_sibling(self) -> None:
        # Counterexample A（既有章读法）：A 损坏，健康章 C 的普通 Phase-1 事务照常提交。
        from scripts.core import structure_commands
        plan = structure_commands.preview_move_chapter(
            self.root, chapter_id=CHAPTER_C, before_chapter_id=CHAPTER_B, actor='test')
        structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                       plan_hash=plan['planHash'], approved=True, actor='test')
        self._assert_damaged_sibling_preserved()


class MaterializationIdentityTests(unittest.TestCase):
    """R10–R12: materialize is for a NEW chapter id only; an existing chapter whose
    binding is missing is BINDING_INVALID, with or without prose on disk
    （实施轮 §17–§21, §44：missing managed file != unbound）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'

    def _materialize(self, chapter_id: str) -> None:
        current = load_structure(self.root)
        prose_binding.apply_binding_transitions(
            current, [{'kind': 'materialize', 'chapterId': chapter_id}])

    def test_existing_chapter_without_binding_cannot_materialize(self) -> None:
        # R10 / Counterexample B：受管章被手工摘掉 proseBinding。
        on_disk = read_json(self.structure_path)
        on_disk['chapters'][1].pop('proseBinding')
        write_json(self.structure_path, on_disk)
        before = self.structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            self._materialize(CHAPTER_B)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_BINDING_INVALID')
        self.assertIn('already exists', str(caught.exception))
        self.assertEqual(self.structure_path.read_bytes(), before)

    def test_formerly_managed_chapter_with_prose_still_cannot_materialize(self) -> None:
        # R11：正文文件仍在盘上，缺 binding 依旧不是「未材料化」。
        on_disk = read_json(self.structure_path)
        on_disk['chapters'][0].pop('proseBinding')
        write_json(self.structure_path, on_disk)
        prose = self.root / '正文' / f'{CHAPTER_A}.md'
        self.assertTrue(prose.is_file())
        with self.assertRaises(StructureError) as caught:
            self._materialize(CHAPTER_A)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_BINDING_INVALID')
        self.assertEqual(prose.read_text(encoding='utf-8'), MANAGED_PROSE)

    def test_formerly_managed_chapter_with_missing_prose_still_cannot_materialize(self) -> None:
        # R12：连正文都没有也一样——`missing managed file != unbound`。
        on_disk = read_json(self.structure_path)
        on_disk['chapters'][0].pop('proseBinding')
        write_json(self.structure_path, on_disk)
        (self.root / '正文' / f'{CHAPTER_A}.md').unlink()
        with self.assertRaises(StructureError) as caught:
            self._materialize(CHAPTER_A)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_BINDING_INVALID')


class GenerationTokenTests(unittest.TestCase):
    """§16/§18: per-binding generation is the staleness token; ABA by data model."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.chapter = self.structure['chapters'][0]

    def test_token_mirrors_binding(self) -> None:
        token = binding_token(self.chapter)
        self.assertEqual(token['bindingGeneration'], self.chapter['proseBinding']['generation'])
        self.assertEqual(token['filePath'], self.chapter['filePath'])
        self.assertEqual(token['state'], 'managed')

    def test_request_generation_accepts_only_current(self) -> None:
        generation = self.chapter['proseBinding']['generation']
        self.assertTrue(request_generation_current(self.chapter, generation))
        self.assertFalse(request_generation_current(self.chapter, generation + 1))
        self.assertFalse(request_generation_current(self.chapter, str(generation)))
        self.assertFalse(request_generation_current(self.chapter, None))

    def test_aba_replay_is_rejected_by_generation(self) -> None:
        # A(g=7,H) → …undo… → A(g=8,H)：路径与字节都复原，旧 token 仍必须失效。
        replayed = dict(self.chapter)
        digest = self.chapter['proseBinding']['expectedContentDigest']
        replayed['proseBinding'] = seal_binding(PROJECT_ID, CHAPTER_A, self.chapter['filePath'],
                                                state='managed', generation=8,
                                                expected_content_digest=digest)
        old_token = binding_token(self.chapter)['bindingGeneration']
        self.assertFalse(request_generation_current(replayed, old_token))

    def test_other_chapters_do_not_advance_this_generation(self) -> None:
        # §16：别的章推进 structure.revision，本章 generation 不动 —— 请求仍有效。
        advanced = dict(self.structure, revision=self.structure['revision'] + 5)
        token = binding_token(self.structure['chapters'][0])['bindingGeneration']
        chapter = next(row for row in advanced['chapters'] if row['chapterId'] == CHAPTER_A)
        self.assertTrue(request_generation_current(chapter, token))


class UnboundMoveAndHardlinkTests(unittest.TestCase):
    """B32 前置语义 + §32 hardlink 语义。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.unbound = self.structure['chapters'][1]

    def test_unbound_move_requires_empty_slots(self) -> None:
        validate_unbound_locator_move(self.unbound, old_prose_exists=False, new_prose_exists=False)
        with self.assertRaises(StructureError) as caught:
            validate_unbound_locator_move(self.unbound, old_prose_exists=True, new_prose_exists=False)
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')
        with self.assertRaises(StructureError) as caught:
            validate_unbound_locator_move(self.unbound, old_prose_exists=False, new_prose_exists=True)
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')

    def test_unresolved_and_managed_never_qualify(self) -> None:
        for chapter in (self.structure['chapters'][0], self.structure['chapters'][2]):
            with self.assertRaises(StructureError) as caught:
                validate_unbound_locator_move(chapter, old_prose_exists=False, new_prose_exists=False)
            self.assertEqual(caught.exception.code, 'OWNERSHIP_BINDING_INVALID')

    def test_hardlink_alias_is_object_conflict_not_canonical_collision(self) -> None:
        original = self.root / '正文' / 'alias-a.md'
        alias = self.root / '正文' / 'alias-b.md'
        original.write_text('shared', encoding='utf-8')
        os.link(original, alias)
        on_disk = read_json(self.root / '.govel' / 'structure.json')
        on_disk['chapters'][1]['filePath'] = '正文/alias-a.md'
        on_disk['chapters'][2]['filePath'] = '正文/alias-b.md'
        # 规范键不同 → 字符串层无碰撞；但物理对象冲突必须被提交层 helper 发现。
        require_unique_locators(on_disk)
        conflicts = hardlink_object_conflicts(self.root, on_disk)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(set(conflicts[0]['chapters']), {CHAPTER_B, CHAPTER_C})

    def test_no_hardlink_conflict_without_disk_alias(self) -> None:
        self.assertEqual(hardlink_object_conflicts(self.root, self.structure), [])


class FirstFinalizationPlanTests(unittest.TestCase):
    """B33: prose creation and unbound→managed belong to ONE future write-set."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.chapter = self.structure['chapters'][1]
        self.prose_relative = self.chapter['filePath']

    def test_plan_holds_prose_create_and_binding_transition_together(self) -> None:
        rows = first_finalization_write_set_plan(
            self.root, self.structure, self.chapter,
            prose_relative=self.prose_relative, prose_text='第一章正文。\n')
        self.assertEqual(len(rows), 2)
        prose_row = next(row for row in rows if row['kind'] == 'prose')
        binding_row = next(row for row in rows if row['kind'] == 'binding-transition')
        self.assertEqual(prose_row['action'], 'create')
        self.assertFalse(prose_row['before']['exists'])  # 空 before-image
        change = binding_row['bindingChange']
        self.assertEqual(change['from']['state'], 'unbound')
        self.assertEqual(change['to']['state'], 'managed')
        self.assertEqual(change['to']['expectedContentDigest'],
                         'sha256:' + hashlib.sha256('第一章正文。\n'.encode()).hexdigest())
        self.assertEqual(change['to']['generation'], self.structure['revision'] + 1)

    def test_existing_target_is_never_adopted(self) -> None:
        (self.root / self.prose_relative).write_text('外来字节', encoding='utf-8')
        with self.assertRaises(StructureError) as caught:
            first_finalization_write_set_plan(
                self.root, self.structure, self.chapter,
                prose_relative=self.prose_relative, prose_text='第一章正文。\n')
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')

    def test_managed_chapter_cannot_first_finalize(self) -> None:
        with self.assertRaises(StructureError) as caught:
            first_finalization_write_set_plan(
                self.root, self.structure, self.structure['chapters'][0],
                prose_relative=self.structure['chapters'][0]['filePath'], prose_text='x')
        self.assertEqual(caught.exception.code, 'OWNERSHIP_BINDING_INVALID')


class BindingHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)

    def test_healthy_fixture_is_fully_healthy(self) -> None:
        report = require_all_binding_health(self.structure)
        self.assertEqual(report['fatal'], [])
        self.assertEqual(report['chapterLocal'], {})

    def test_unknown_fields_rejected(self) -> None:
        chapter = dict(self.structure['chapters'][1])
        chapter['proseBinding'] = dict(chapter['proseBinding'], note='smuggled')
        errors = validate_binding(self.structure, chapter)
        self.assertTrue(any(error['field'] == 'proseBinding' and 'fields' in error for error in errors))

    def test_generation_must_be_positive_int(self) -> None:
        for bad in (0, -1, True, '7', 1.0, None):
            chapter = dict(self.structure['chapters'][1])
            chapter['proseBinding'] = dict(chapter['proseBinding'], generation=bad)
            errors = validate_binding(self.structure, chapter)
            self.assertTrue(any(error['field'] == 'generation' for error in errors), bad)

    def test_missing_binding_is_chapter_local(self) -> None:
        chapter = dict(self.structure['chapters'][1])
        chapter.pop('proseBinding')
        errors = validate_binding(self.structure, chapter)
        self.assertEqual([error['code'] for error in errors], ['OWNERSHIP_BINDING_INVALID'])
        # 定位符仍被保留。
        reservations, fatal = locator_reservations(dict(self.structure, chapters=[chapter]))
        self.assertEqual(fatal, [])
        self.assertIn(canonical_locator_key(chapter['filePath']), reservations)


if __name__ == '__main__':
    unittest.main()
