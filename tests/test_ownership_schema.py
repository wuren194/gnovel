"""Current project materialization, ownership authority and source-plan invariants.

All tests use synthetic current-format projects; no real novel is touched.
"""
from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.core import refactor, structure_commands
from scripts.core.importer import create_project_from_candidate
from scripts.core.jsonio import read_json, write_json
from scripts.core.prose_binding import (
    binding_integrity_digest, canonical_locator_key, commit_binding_transitions,
    enforce_writer_boundary, locator_reservations, ownership_validation_report,
    request_generation_current, seal_binding,
)
from scripts.core.source_model import is_current_binding, require_current_source
from scripts.core.source_plan import chapter_plan
from scripts.core.structure_store import (
    StructureError, empty_structure, load_structure, save_structure, structure_hash,
)
from scripts.core.structure_transaction import prepare_writes
from scripts.core.finalization import finalize_chapter
from scripts.core.finalization_preflight import preflight_finalization
from scripts.core.workflow import WorkflowError
from tests.fixtures_ownership import (
    ARC_MAIN, CHAPTER_A, CHAPTER_B, CHAPTER_C, PROJECT_ID, VOLUME_A, new_chapter_row,
    ownership_project,
)
from tests.fixtures_refactor import CHAPTER_TWO, refactor_project




class MaterializationTests(unittest.TestCase):
    """Chapter creation seals an unbound binding at the commit revision."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)

    def _insert(self) -> dict:
        plan = structure_commands.preview_insert_chapter(
            self.root, volume_id=VOLUME_A, primary_arc_id=ARC_MAIN, title='新章', actor='test')
        return structure_commands.commit_plan(
            self.root, operation_id=plan['operationId'], plan_hash=plan['planHash'],
            approved=True, actor='test')

    def test_materialize_seals_unbound_at_commit_revision(self) -> None:
        result = self._insert()
        structure = load_structure(self.root)
        self.assertEqual(structure['revision'], result['afterRevision'])
        chapter = next(row for row in structure['chapters']
                       if row['chapterId'] == result['chapter']['chapterId'])
        binding = chapter['proseBinding']
        self.assertEqual(binding['state'], 'unbound')
        self.assertIsNone(binding['expectedContentDigest'])
        self.assertEqual(binding['generation'], structure['revision'])  # B18
        self.assertEqual(ownership_validation_report(structure)['chapterLocal'], {})

    def test_request_bound_to_materialized_generation(self) -> None:
        result = self._insert()
        structure = load_structure(self.root)
        chapter = next(row for row in structure['chapters']
                       if row['chapterId'] == result['chapter']['chapterId'])
        self.assertTrue(request_generation_current(chapter, structure['revision']))
        self.assertFalse(request_generation_current(chapter, structure['revision'] - 1))

    def test_materialize_never_adopts_existing_file(self) -> None:
        # B19：目标槽被占时不允许自动 adopt，fail closed。
        occupied = self.root / '正文' / 'OCCUPIED.md'
        occupied.write_text('外来正文', encoding='utf-8')
        # 建章 filePath 由 chapterId 决定，无法预先占位；直接对 proposed 注入占用
        # 路径，走同一条 boundary（preview 阶段即拒）。
        structure = load_structure(self.root)
        proposed = deepcopy(structure)
        chapter_id = 'CH-BINDING-0088'
        proposed['chapters'].append({
            'chapterId': chapter_id, 'displayOrdinal': 9, 'displayCode': 'CH009',
            'title': '占用', 'volumeId': VOLUME_A, 'filePath': '正文/OCCUPIED.md', 'status': 'planning_required',
            'storyTimeStartId': None, 'storyTimeEndId': None,
            'proseBinding': seal_binding(structure['projectId'], chapter_id,
                                         '正文/OCCUPIED.md', state='unbound',
                                         generation=structure['revision'] + 1,
                                         expected_content_digest=None)})
        proposed['chapterArcLinks'].append({'chapterId': chapter_id, 'arcId': ARC_MAIN,
                                            'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''})
        plan = {'operationId': 'OP-0000000000000000', 'operation': 'insert_chapter',
                'actor': 'test', 'parameters': {'chapterId': chapter_id},
                'proposedStructure': proposed}
        with self.assertRaises(StructureError) as caught:
            prepare_writes(self.root, plan, structure)
        self.assertEqual(caught.exception.code, 'PROSE_TARGET_NOT_OWNED')
        self.assertEqual(occupied.read_text(encoding='utf-8'), '外来正文')


class TransactionBoundaryTests(unittest.TestCase):
    """The write-set path is bound by the same Option B boundary as save_structure."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)

    def test_forged_plan_file_path_mutation_rejected(self) -> None:
        structure = load_structure(self.root)
        proposed = deepcopy(structure)
        proposed['chapters'][0]['filePath'] = '正文/走私.md'
        plan = {'operationId': 'OP-0000000000000001', 'operation': 'move_chapter',
                'actor': 'test', 'parameters': {}, 'proposedStructure': proposed}
        with self.assertRaises(StructureError) as caught:
            prepare_writes(self.root, plan, structure)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')

    def test_non_protected_transaction_still_commits(self) -> None:
        plan = structure_commands.preview_move_chapter(
            self.root, chapter_id=CHAPTER_C, before_chapter_id=CHAPTER_A, actor='test')
        structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                       plan_hash=plan['planHash'], approved=True, actor='test')
        structure = load_structure(self.root)
        # binding 原样保留（重排不改归属），generation 不前进（§16）。
        chapter = next(row for row in structure['chapters'] if row['chapterId'] == CHAPTER_C)
        self.assertEqual(chapter['proseBinding']['generation'], self.structure['revision'])


class ExactMaterializationAuthorityTests(unittest.TestCase):
    """T1–T5 / F5–F9（实施轮 §27–§34、§36、§43、§53–§55）：事务通道的建章能力精确绑定
    operation + `plan.parameters.chapterId`——只有 insert_chapter，且只覆盖它申报的那一个
    章；preview 与 commit 两端重放同一条检查，伪造 proposal 在两端都被拒绝。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'

    def _sealed_new_row(self, *, chapter_id: str, ordinal: int, generation: int) -> dict:
        row = new_chapter_row(chapter_id=chapter_id, ordinal=ordinal, title='新章')
        row['proseBinding'] = seal_binding(PROJECT_ID, chapter_id, row['filePath'],
                                           state='unbound', generation=generation,
                                           expected_content_digest=None)
        return row

    def _append_row(self, proposed: dict, row: dict) -> None:
        proposed['chapters'].append(row)
        proposed['chapterArcLinks'].append({'chapterId': row['chapterId'], 'arcId': ARC_MAIN,
                                            'role': 'primary', 'weight': 1.0, 'phaseId': None,
                                            'notes': ''})

    def _plan(self, *, operation: str, parameters: dict, proposed: dict) -> dict:
        return {'schemaVersion': 1, 'operationId': 'OP-' + 'A' * 16, 'operation': operation,
                'actor': 'test', 'parameters': parameters, 'proposedStructure': proposed,
                'impact': {}, 'requiresApproval': True}

    def _assert_prepare_rejected(self, plan: dict) -> None:
        current = load_structure(self.root)
        before = self.structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            prepare_writes(self.root, plan, current)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
        # 副作用哨兵：拒绝发生在任何 staged 文件落盘之前，磁盘结构一个字节未动。
        self.assertEqual(self.structure_path.read_bytes(), before)

    def test_t1_f5_insert_chapter_materializes_exactly_its_declared_chapter(self) -> None:
        plan = structure_commands.preview_insert_chapter(
            self.root, volume_id=VOLUME_A, primary_arc_id=ARC_MAIN, title='新章', actor='test')
        declared = plan['parameters']['chapterId']
        result = structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                                plan_hash=plan['planHash'], approved=True,
                                                actor='test')
        self.assertEqual(result['chapter']['chapterId'], declared)
        structure = load_structure(self.root)
        added = {row['chapterId'] for row in structure['chapters']} - {
            CHAPTER_A, CHAPTER_B, CHAPTER_C}
        self.assertEqual(added, {declared})  # added IDs == authorized IDs
        chapter = next(row for row in structure['chapters'] if row['chapterId'] == declared)
        self.assertEqual(chapter['proseBinding']['state'], 'unbound')
        self.assertEqual(chapter['proseBinding']['generation'], structure['revision'])

    def test_t2_f6_non_insert_plan_cannot_smuggle_a_chapter(self) -> None:
        smuggled = 'CH-BINDING-0031'
        current = load_structure(self.root)
        for parameters in ({'chapterId': CHAPTER_C, 'beforeChapterId': CHAPTER_A},
                           {'chapterId': smuggled}):
            with self.subTest(parameters=parameters):
                proposed = deepcopy(current)
                proposed['revision'] = current['revision'] + 1
                self._append_row(proposed, self._sealed_new_row(
                    chapter_id=smuggled, ordinal=9, generation=proposed['revision']))
                # 第二种参数故意把 operation 申报的 chapterId 写成走私章 ID：operation 名
                # 与 chapterId 必须同时正确，move_chapter 永远拿不到建章能力（§14/§54）。
                self._assert_prepare_rejected(self._plan(
                    operation='move_chapter', parameters=parameters, proposed=proposed))

    def test_t3_f7_insert_plan_cannot_add_an_extra_chapter(self) -> None:
        current = load_structure(self.root)
        declared, extra = 'CH-BINDING-0032', 'CH-BINDING-0033'
        proposed = deepcopy(current)
        proposed['revision'] = current['revision'] + 1
        for chapter_id, ordinal in ((declared, 9), (extra, 10)):
            self._append_row(proposed, self._sealed_new_row(
                chapter_id=chapter_id, ordinal=ordinal, generation=proposed['revision']))
        # added ⊋ authorized：多夹带一章即拒绝。
        self._assert_prepare_rejected(self._plan(
            operation='insert_chapter', parameters={'chapterId': declared}, proposed=proposed))

    def test_t4_f8_insert_plan_declaring_one_chapter_cannot_add_another(self) -> None:
        current = load_structure(self.root)
        declared, added = 'CH-BINDING-0034', 'CH-BINDING-0035'
        proposed = deepcopy(current)
        proposed['revision'] = current['revision'] + 1
        self._append_row(proposed, self._sealed_new_row(
            chapter_id=added, ordinal=9, generation=proposed['revision']))
        # authorized ⊋ added：换一个身份建章也不行，能力必须精确覆盖实际新增集合。
        self._assert_prepare_rejected(self._plan(
            operation='insert_chapter', parameters={'chapterId': declared}, proposed=proposed))

    def test_t5_f9_commit_time_replay_rejects_a_forged_staged_proposal(self) -> None:
        plan = structure_commands.preview_insert_chapter(
            self.root, volume_id=VOLUME_A, primary_arc_id=ARC_MAIN, title='新章', actor='test')
        plan_path = _staged_plan_path(self.root, plan)
        staged = read_json(plan_path)
        extra = 'CH-BINDING-0036'
        self._append_row(staged['proposedStructure'], self._sealed_new_row(
            chapter_id=extra, ordinal=90,
            generation=staged['proposedStructure']['revision']))
        from scripts.core.structure_commands import _plan_hash
        staged['planHash'] = _plan_hash({k: v for k, v in staged.items() if k != 'planHash'})
        write_json(plan_path, staged)
        before = self.structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                           plan_hash=staged['planHash'], approved=True,
                                           actor='test')
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
        self.assertEqual(self.structure_path.read_bytes(), before)
        on_disk_ids = [row['chapterId'] for row in read_json(self.structure_path)['chapters']]
        self.assertNotIn(extra, on_disk_ids)

    def test_forged_staged_revision_is_rejected_at_commit(self) -> None:
        # commit 端不把 proposed revision 当成已经查过的事实：伪造的 staged revision
        # 不能把结构推到一个跳号的版本上。
        plan = structure_commands.preview_insert_chapter(
            self.root, volume_id=VOLUME_A, primary_arc_id=ARC_MAIN, title='新章', actor='test')
        plan_path = _staged_plan_path(self.root, plan)
        staged = read_json(plan_path)
        staged['proposedStructure']['revision'] += 500
        chapter = next(row for row in staged['proposedStructure']['chapters']
                       if row['chapterId'] == plan['parameters']['chapterId'])
        chapter['proseBinding'] = seal_binding(
            PROJECT_ID, chapter['chapterId'], chapter['filePath'], state='unbound',
            generation=staged['proposedStructure']['revision'], expected_content_digest=None)
        from scripts.core.structure_commands import _plan_hash
        staged['planHash'] = _plan_hash({k: v for k, v in staged.items() if k != 'planHash'})
        write_json(plan_path, staged)
        before = self.structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                           plan_hash=staged['planHash'], approved=True,
                                           actor='test')
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(self.structure_path.read_bytes(), before)


class DeclaredChapterNeverMaterializedTests(unittest.TestCase):
    """X 系列：insert_chapter 申报了 `chapterId`、proposal 却一章没建。

    Phase 1 冻结的是 `actual added chapter IDs == authorized chapter IDs`，所以 `added ⊂
    authorized`（申报而不建）与多夹带一样是越权提案。它必须由 ownership writer boundary 以
    `OWNERSHIP_UNAUTHORIZED_MUTATION` 明确拒绝——不得依赖后续的 `StopIteration` / `KeyError`
    / 渲染失败偶然拒绝。preview 与 commit 共用 `_enforce_transaction_boundary`，两端重放同一条比较。
    """

    DECLARED = 'CH-BINDING-0141'

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def _project(self) -> Path:
        root = self.base / 'current'
        ownership_project(root)
        return root

    def _declares_but_adds_none(self, root: Path) -> tuple[dict, dict]:
        """A plan whose operation declares a chapterId while its structure adds no chapter."""
        current = load_structure(root)
        proposed = deepcopy(current)
        proposed['revision'] = current['revision'] + 1
        plan = {'schemaVersion': 1, 'operationId': 'OP-' + 'B' * 16, 'operation': 'insert_chapter',
                'actor': 'test', 'parameters': {'chapterId': self.DECLARED},
                'proposedStructure': proposed, 'impact': {}, 'requiresApproval': True}
        return plan, current

    def _assert_prepare_boundary_rejected(self, root: Path, plan: dict, current: dict) -> None:
        structure_path = root / '.govel' / 'structure.json'
        before = structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            prepare_writes(root, plan, current)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
        # 结构性识别这条拒绝来自 exact set comparison，而不是别的分支。
        self.assertEqual(caught.exception.details['added'], [])
        self.assertEqual(caught.exception.details['authorized'], [self.DECLARED])
        # 副作用哨兵：boundary 在任何 staged 文件落盘之前拒绝，磁盘结构一个字节未动。
        self.assertEqual(structure_path.read_bytes(), before)
        self.assertFalse((root / '.govel' / 'plans' / f"{plan['operationId']}.files").exists())


    def test_schema5_insert_plan_declares_chapter_but_adds_none_rejected(self) -> None:
        root = self._project()
        plan, current = self._declares_but_adds_none(root)
        self._assert_prepare_boundary_rejected(root, plan, current)

    def _staged_insert_plan(self, root: Path) -> dict:
        from scripts.core.source_placement import placement_from_volume
        return structure_commands.preview_insert_chapter(
            root, placement=placement_from_volume(VOLUME_A), primary_arc_id=ARC_MAIN,
            title='新章', actor='test')

    def test_commit_time_replay_rejects_a_staged_insert_plan_that_creates_no_chapter(self) -> None:
        # 重算 planHash 不能把这条越权洗白：staged proposal 依旧「申报 A 却不建 A」，commit
        # 端对 fresh current 重放同一条 exact 比较，必须先于 PLAN_HASH_MISMATCH 拒绝。
        root = self._project()
        plan = self._staged_insert_plan(root)
        declared = plan['parameters']['chapterId']
        plan_path = _staged_plan_path(root, plan)
        staged = read_json(plan_path)
        proposed = staged['proposedStructure']
        proposed['chapters'] = [row for row in proposed['chapters']
                                if row['chapterId'] != declared]
        proposed['chapterArcLinks'] = [row for row in proposed['chapterArcLinks']
                                       if row['chapterId'] != declared]
        from scripts.core.structure_commands import _plan_hash
        staged['planHash'] = _plan_hash({k: v for k, v in staged.items()
                                         if k != 'planHash'})
        write_json(plan_path, staged)
        structure_path = root / '.govel' / 'structure.json'
        before = structure_path.read_bytes()
        with self.assertRaises(StructureError) as caught:
            structure_commands.commit_plan(root, operation_id=plan['operationId'],
                                           plan_hash=staged['planHash'], approved=True,
                                           actor='test')
        self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
        self.assertEqual(caught.exception.details['added'], [])
        self.assertEqual(caught.exception.details['authorized'], [declared])
        # 副作用哨兵：磁盘结构一个字节未动，冒名章没有出生。
        self.assertEqual(structure_path.read_bytes(), before)
        on_disk_ids = [row['chapterId'] for row in read_json(structure_path)['chapters']]
        self.assertNotIn(declared, on_disk_ids)


class DamagedSiblingMaterializationTests(unittest.TestCase):
    """F15/F16（实施轮 §38）：writer boundary 收紧后，损坏兄弟章既不阻止合法的精确建章
    事务，也不会被顺手改动或修复。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure_path = self.root / '.govel' / 'structure.json'
        on_disk = read_json(self.structure_path)
        damaged = next(row for row in on_disk['chapters'] if row['chapterId'] == CHAPTER_A)
        broken = dict(damaged['proseBinding'])
        broken['integrityDigest'] = 'sha256:' + '0' * 64
        damaged['proseBinding'] = broken
        write_json(self.structure_path, on_disk)
        self.damaged_row = next(row for row in read_json(self.structure_path)['chapters']
                                if row['chapterId'] == CHAPTER_A)

    def _on_disk_row(self, chapter_id: str) -> dict:
        return next(row for row in read_json(self.structure_path)['chapters']
                    if row['chapterId'] == chapter_id)

    def test_f15_f16_authorized_materialization_succeeds_and_sibling_unchanged(self) -> None:
        plan = structure_commands.preview_insert_chapter(
            self.root, volume_id=VOLUME_A, primary_arc_id=ARC_MAIN, title='新章', actor='test')
        result = structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                                plan_hash=plan['planHash'], approved=True,
                                                actor='test')
        structure = load_structure(self.root)
        chapter = next(row for row in structure['chapters']
                       if row['chapterId'] == result['chapter']['chapterId'])
        self.assertEqual(chapter['proseBinding']['state'], 'unbound')
        self.assertEqual(chapter['proseBinding']['generation'], structure['revision'])
        # F16：损坏章逐字段不变（integrityDigest 保持损坏值），依旧 chapter-local、依旧
        # 保留它的规范定位符——收紧后的 boundary 不修它、也不被它挡住。
        self.assertEqual(self._on_disk_row(CHAPTER_A), self.damaged_row)
        report = ownership_validation_report(load_structure(self.root))
        self.assertEqual(report['fatal'], [])
        self.assertIn(CHAPTER_A, report['chapterLocal'])
        reservations, fatal = locator_reservations(load_structure(self.root))
        self.assertEqual(fatal, [])
        self.assertEqual(reservations.get(canonical_locator_key(f'正文/{CHAPTER_A}.md')),
                         CHAPTER_A)


class SourcePlanStabilityTests(unittest.TestCase):
    """Binding metadata affects the full structure hash, while sourcePlanHash excludes it."""

    def test_source_plan_hash_excludes_binding_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            refactor_project(root)
            structure = load_structure(root)
            binding = seal_binding(structure['projectId'], CHAPTER_TWO,
                                   str(structure['chapters'][1]['filePath']),
                                   state='managed', generation=7,
                                   expected_content_digest='sha256:' + 'a' * 64)
            with_binding = deepcopy(structure)
            with_binding['chapters'][1]['proseBinding'] = binding
            advanced = deepcopy(with_binding)
            advanced['chapters'][1]['proseBinding'] = seal_binding(
                structure['projectId'], CHAPTER_TWO, str(structure['chapters'][1]['filePath']),
                state='managed', generation=9,
                expected_content_digest='sha256:' + 'a' * 64)
            plan_plain = chapter_plan(structure, CHAPTER_TWO)
            plan_bound = chapter_plan(with_binding, CHAPTER_TWO)
            plan_advanced = chapter_plan(advanced, CHAPTER_TWO)
            self.assertEqual(plan_plain['sourcePlanHash'], plan_bound['sourcePlanHash'])  # B21
            self.assertEqual(plan_bound['sourcePlanHash'], plan_advanced['sourcePlanHash'])
            self.assertNotIn('proseBinding', next(row['record'] for row in plan_bound['records']
                                                  if row['id'] == CHAPTER_TWO))
            # B22：同一 binding-only 变化在完整结构快照里必须可见。
            self.assertNotEqual(structure_hash(structure), structure_hash(with_binding))
            self.assertNotEqual(structure_hash(with_binding), structure_hash(advanced))

    def test_impact_does_not_classify_binding_metadata_as_content_change(self) -> None:
        from scripts.core.source_impact import source_impact
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            refactor_project(root)
            structure = load_structure(root)
            with_binding = deepcopy(structure)
            with_binding['chapters'][1]['proseBinding'] = seal_binding(
                structure['projectId'], CHAPTER_TWO, str(structure['chapters'][1]['filePath']),
                state='managed', generation=7,
                expected_content_digest='sha256:' + 'a' * 64)
            impact = source_impact(structure, with_binding)
            self.assertNotIn(CHAPTER_TWO, impact['changedIds'])          # §47
            self.assertNotIn(CHAPTER_TWO, impact['staleCandidates'])
            self.assertNotIn(CHAPTER_TWO, impact['approvalInvalidations'])

    def test_materialize_is_visible_to_full_structure_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = ownership_project(root)
            current = load_structure(root)
            hash_before = structure_hash(current)
            new_chapter_id = 'CH-BINDING-0006'
            proposed = deepcopy(current)
            proposed['revision'] = current['revision'] + 1
            proposed['chapters'].append(new_chapter_row(
                chapter_id=new_chapter_id, ordinal=9, title='新章'))
            proposed['chapterArcLinks'].append({'chapterId': new_chapter_id, 'arcId': ARC_MAIN,
                                                'role': 'primary', 'weight': 1.0, 'phaseId': None,
                                                'notes': ''})
            commit_binding_transitions(root, [{'kind': 'materialize', 'chapterId': new_chapter_id}],
                                       actor='test', proposed=proposed)
            after = load_structure(root)
            self.assertNotEqual(hash_before, structure_hash(after))  # B22（当前格式 实路径）
            chapter = next(row for row in after['chapters'] if row['chapterId'] == new_chapter_id)
            self.assertEqual(chapter['proseBinding']['generation'], after['revision'])
            self.assertEqual(chapter['proseBinding']['state'], 'unbound')


class ImporterOwnershipTests(unittest.TestCase):
    """B20: importer-produced ownership structures go through the same validator."""

    def _candidate(self, chapters: list[dict]) -> dict:
        return {'bookName': '导入书', 'volumes': [
                    {'volumeId': VOLUME_A, 'ordinal': 1, 'title': '第一卷', 'status': 'planned'}],
                'arcs': [{'arcId': ARC_MAIN, 'title': '主线', 'kind': 'main', 'status': 'planned',
                          'volumeIds': [VOLUME_A], 'parentArcId': None, 'promise': '',
                          'resolutionTargetChapterId': None}],
                'chapters': chapters,
                'chapterArcLinks': [{'chapterId': row['chapterId'], 'arcId': ARC_MAIN,
                                     'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''}
                                    for row in chapters],
                'entities': [], 'relations': [], 'references': []}

    def test_greenfield_import_seals_unbound_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = self._candidate([{
                'chapterId': 'CH-IMPORT-0001', 'displayOrdinal': 1, 'displayCode': 'CH001',
                'title': '第一章', 'placement': {'scope': 'volume', 'volumeId': VOLUME_A}, 'filePath': '正文/CH-IMPORT-0001.md', 'status': 'planned',
                'storyTimeStartId': None, 'storyTimeEndId': None}])
            result = create_project_from_candidate(root, candidate)
            structure = result['structure']
            self.assertEqual(structure['schemaVersion'], 5)
            binding = structure['chapters'][0]['proseBinding']
            self.assertEqual(binding['state'], 'unbound')
            self.assertIsNone(binding['expectedContentDigest'])
            self.assertEqual(binding['generation'], structure['revision'])
            # 不绕过校验：同一结构能通过 load_structure（含 ownership 硬不变量）。
            reloaded = load_structure(root)
            self.assertEqual(ownership_validation_report(reloaded)['chapterLocal'], {})

    def test_candidate_carrying_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chapter = {'chapterId': 'CH-IMPORT-0001', 'displayOrdinal': 1, 'displayCode': 'CH001',
                       'title': '第一章', 'placement': {'scope': 'volume', 'volumeId': VOLUME_A}, 'filePath': '正文/CH-IMPORT-0001.md', 'status': 'planned',
                       'storyTimeStartId': None, 'storyTimeEndId': None,
                       'proseBinding': {'state': 'managed', 'generation': 1,
                                        'expectedContentDigest': None, 'integrityDigest': 'sha256:' + '0' * 64}}
            with self.assertRaises(StructureError) as caught:
                create_project_from_candidate(root, self._candidate([chapter]))
            self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')


    def test_import_default_is_current_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = create_project_from_candidate(Path(temp), self._candidate([]))
            self.assertEqual(result['structure']['schemaVersion'], 5)


class CurrentWriterGateTests(unittest.TestCase):
    """Finalization, locator refactoring and undo enforce current binding authority."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)

    def test_finalization_runs_the_current_binding_path(self) -> None:
        # R24 / B26 —— Phase 2：当前格式 的定稿不再报 OWNERSHIP_MODEL_UNSUPPORTED_OPERATION，
        # 而是走 Current Binding 运行时。缺代次令牌仍然被确定性拒绝（§22/§23），
        # 章的 review 前置检查也照旧生效。
        with self.assertRaises(WorkflowError) as caught:
            finalize_chapter(self.root, CHAPTER_B)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_GENERATION_CONFLICT')
        preflight = preflight_finalization(self.root, CHAPTER_B, binding_generation=7)
        self.assertFalse(preflight.ready)
        codes = [b['code'] for b in preflight.blockers]
        # 归属闸放行（unbound + 空槽），剩下的全是既有的文学门：章还没进 review。
        self.assertIn('INVALID_STATE_TRANSITION', codes)
        self.assertNotIn('OWNERSHIP_MODEL_UNSUPPORTED_OPERATION', codes)

    def test_refactor_locator_move_is_a_formal_binding_transition(self) -> None:
        # R25 / B27 —— Phase 2：unbound 空槽的定位符移动走 move_unbound 正式流转。
        plan = refactor.preview_relocate_chapter_paths(self.root, chapter_id=CHAPTER_B,
                                                       file_path='正文/搬家.md')
        self.assertEqual(plan['bindingTransitions'],
                         [{'kind': 'move_unbound', 'chapterId': CHAPTER_B,
                           'newFilePath': '正文/搬家.md',
                           'bindingGeneration': self.structure['revision'],
                           'expectedContentDigest': None}])
        refactor.commit_refactor(self.root, operation_id=plan['operationId'],
                                 plan_hash=plan['planHash'], approved=True, actor='test')
        chapter = next(row for row in load_structure(self.root)['chapters']
                       if row['chapterId'] == CHAPTER_B)
        self.assertEqual(chapter['filePath'], '正文/搬家.md')
        self.assertEqual(chapter['proseBinding']['state'], 'unbound')

    def test_case_only_rename_returns_deferred_code(self) -> None:
        target = f'正文/{CHAPTER_B.upper()}.MD'
        with self.assertRaises(StructureError) as caught:
            refactor.preview_relocate_chapter_paths(self.root, chapter_id=CHAPTER_B,
                                                    file_path=target)
        self.assertEqual(caught.exception.code, 'REFACTOR_CASE_ONLY_RENAME_DEFERRED')

    def test_rename_with_path_sync_routes_through_a_binding_transition(self) -> None:
        plan = refactor.preview_rename_chapter(self.root, chapter_id=CHAPTER_B, new_title='改名',
                                               file_name_strategy='sync_safe_paths')
        self.assertEqual(plan['bindingTransitions'][0]['kind'], 'move_unbound')

    def test_display_only_rename_still_works(self) -> None:
        plan = refactor.preview_rename_chapter(self.root, chapter_id=CHAPTER_B, new_title='空槽·新名')
        refactor.commit_refactor(self.root, operation_id=plan['operationId'],
                                 plan_hash=plan['planHash'], approved=True, actor='test')
        structure = load_structure(self.root)
        chapter = next(row for row in structure['chapters'] if row['chapterId'] == CHAPTER_B)
        self.assertEqual(chapter['title'], '空槽·新名')
        self.assertEqual(chapter['filePath'], f'正文/{CHAPTER_B}.md')  # locator 未动
        self.assertEqual(chapter['proseBinding']['generation'], self.structure['revision'])

    def test_undo_refuses_an_unknown_operation(self) -> None:
        # R26 / B28（Phase 3 更新）：undo 已切到单调逆向事务，不再按模型 fail closed；
        # 未知 operation 仍被确定性拒绝，且拒绝发生在任何写入之前。逆向事务本身的
        # 语义覆盖在 tests/test_phase3_monotonic_undo.py。
        from scripts.core.structure_commands import undo_operation
        with self.assertRaises(StructureError) as caught:
            undo_operation(self.root, 'OP-0000000000000000')
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_FOUND')

    def test_unresolved_move_rejected(self) -> None:
        # B31：unresolved 不是 ownership-positive，普通 move 由当前观测状态确定性拒绝。
        with self.assertRaises(StructureError) as caught:
            refactor.preview_relocate_chapter_paths(self.root, chapter_id=CHAPTER_C,
                                                    file_path='正文/悬案新址.md')
        self.assertEqual(caught.exception.code, 'OWNERSHIP_CONFIRMATION_REQUIRED')

    def test_schema5_runs_the_same_current_binding_runtime(self) -> None:
        root5 = Path(self.temp.name) / 'schema5'
        ownership_project(root5)
        with self.assertRaises(WorkflowError) as caught:
            finalize_chapter(root5, CHAPTER_B)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_GENERATION_CONFLICT')
        plan = refactor.preview_relocate_chapter_paths(root5, chapter_id=CHAPTER_B,
                                                       file_path='正文/搬家.md')
        self.assertEqual(plan['bindingTransitions'][0]['kind'], 'move_unbound')

    def test_schema5_undo_routes_to_the_monotonic_engine(self) -> None:
        # R26（schema 5 分支，Phase 3 更新）：两条 undo 通路都按当前结构的 ownership 模型
        # 分流——schema 5 不再是「模型不支持」，而是单调逆向事务的确定性拒绝；
        # 绝不因为 source helper 修好而落入 旧版撤销。
        from scripts.core.structure_commands import undo_operation
        from scripts.core.structure_transaction import undo
        root5 = Path(self.temp.name) / 'schema5-undo'
        ownership_project(root5)
        with self.assertRaises(StructureError) as caught:
            undo_operation(root5, 'OP-0000000000000000')
        self.assertEqual(caught.exception.code, 'OPERATION_NOT_FOUND')
        # 直接入口同样在写入之前按证据形状确定性拒绝（空 journal 连操作身份都对不上）。
        (root5 / '.govel' / 'operations').mkdir(parents=True, exist_ok=True)
        write_json(root5 / '.govel' / 'operations' / 'OP-0000000000000000.json', {})
        with self.assertRaises(StructureError) as direct:
            undo(root5, 'OP-0000000000000000', actor='test')
        self.assertEqual(direct.exception.code, 'OPERATION_ID_MISMATCH')

    def test_schema5_preflight_reports_current_binding_state(self) -> None:
        # Phase 2：schema 5 的定稿前置不再报「模型不支持」，而是给出 Current Binding
        # 的确定性 blocker（这里章还没进 review）。
        root5 = Path(self.temp.name) / 'schema5-preflight'
        ownership_project(root5)
        preflight = preflight_finalization(root5, CHAPTER_B, binding_generation=7)
        self.assertFalse(preflight.ready)
        codes = [b['code'] for b in preflight.blockers]
        self.assertIn('INVALID_STATE_TRANSITION', codes)
        self.assertNotIn('OWNERSHIP_MODEL_UNSUPPORTED_OPERATION', codes)


class ReservedLocatorInvariantTests(unittest.TestCase):
    """B30 结构级：损坏章保留的定位符不能被其他章 claim——通用保存路径也拦。"""

    def test_boundary_prevents_locator_theft(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = ownership_project(root)
            structure_path = root / '.govel' / 'structure.json'
            on_disk = read_json(structure_path)
            victim = next(row for row in on_disk['chapters'] if row['chapterId'] == CHAPTER_C)
            # 复制一份带相同 filePath 的章（模拟另一章 claim 同一规范槽）；换新身份与
            # 新 displayOrdinal，保证唯一可能的结构失败就是定位符冲突。
            stolen = dict(victim)
            stolen['chapterId'] = 'CH-BINDING-0099'
            stolen['displayOrdinal'] = 9
            stolen['displayCode'] = 'CH009'
            on_disk['chapters'].append(stolen)
            on_disk['chapterArcLinks'].append({'chapterId': stolen['chapterId'],
                                               'arcId': ARC_MAIN, 'role': 'primary'})
            before = structure_path.read_bytes()
            # 通用保存路径先在 ownership 边界拒绝「凭空加章」（当前格式 的 added
            # chapter 必须被精确 materialization capability 覆盖），磁盘一个字节未动。
            with self.assertRaises(StructureError) as caught:
                save_structure(root, on_disk, expected_revision=structure['revision'])
            self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
            self.assertEqual(structure_path.read_bytes(), before)
            # 结构级定位符唯一性照旧独立成立：同一份被盗结构过校验器即 CONFLICT，
            # 因此合法建章通道也不会把两个章绑到同一个规范槽。
            from scripts.core.structure_store import validate_structure as validate
            with self.assertRaises(StructureError) as structural:
                validate(on_disk)
            codes = {error['code'] for error in structural.exception.details['errors']}
            self.assertEqual(codes, {'PROSE_LOCATOR_CONFLICT'})


def _staged_plan_path(root: Path, plan: dict) -> Path:
    """The staged plan file a preview writes; committing reads this, not the report."""
    return root / '.govel' / 'plans' / f"{plan['operationId']}.json"


def _staged_plan(root: Path, plan: dict) -> dict:
    """The staged plan file — previews return the public report, not the proposal."""
    return read_json(_staged_plan_path(root, plan))


class Schema5FoundationTests(unittest.TestCase):
    """Source plans, placements and materialization share the current-format contract."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.structure = ownership_project(self.root)
        self.structure['fieldDefinitions'] = [
            {'fieldId': 'FD-PLAN-1', 'key': 'goal', 'label': '章节目标', 'valueType': 'text',
             'appliesTo': ['chapter']}]
        self.structure['attributes'] = [
            {'attributeId': 'AT-PLAN-1', 'ownerId': CHAPTER_A, 'fieldId': 'FD-PLAN-1',
             'values': [{'valueId': 'FV-PLAN-1', 'value': '白塔塌陷'}]}]
        self.structure['fragments'] = [
            {'fragmentId': 'SF-PLAN-1', 'ownerId': CHAPTER_A, 'text': '本章推演：白塔在雾里塌了一半。',
             'order': 0, 'contentBindings': ['AT-PLAN-1'], 'contentRole': 'plan'}]
        write_json(self.root / '.govel' / 'structure.json', self.structure)
        from scripts.core.structure_store import validate_structure as validate
        validate(self.structure)

    def test_schema5_fixture_passes_full_validation(self) -> None:
        # S5-1：schema 5 fixture 必须真正通过 validate_structure（含 ownership 硬不变量）。
        from scripts.core.structure_store import validate_structure as validate
        validate(self.structure)
        report = ownership_validation_report(self.structure)
        self.assertEqual(report['fatal'], [])
        self.assertEqual(report['chapterLocal'], {})

    def test_chapter_plan_serves_schema5(self) -> None:
        # R1 / Counterexample D：schema 5 chapter_plan 绝不报 SOURCE_MIGRATION_REQUIRED。
        result = chapter_plan(self.structure, CHAPTER_A)
        self.assertEqual(len(result['sourcePlanHash']), 64)
        self.assertEqual(result['recipe']['chapterId'], CHAPTER_A)
        self.assertEqual(result['planFragmentIds'], ['SF-PLAN-1'])
        self.assertTrue(result['ready'])

    def test_outline_binding_goes_through_source_plan_hash(self) -> None:
        # R2：outline_binding 走 sourcePlanHash，而不是 outlineSha256 文件。
        from scripts.core.source_plan import outline_binding
        binding = outline_binding(self.root, self.structure,
                                  next(row for row in self.structure['chapters']
                                       if row['chapterId'] == CHAPTER_A))
        self.assertEqual(binding['kind'], 'source')
        self.assertEqual(binding['field'], 'sourcePlanHash')
        self.assertEqual(binding['hash'], chapter_plan(self.structure, CHAPTER_A)['sourcePlanHash'])
        # 没有活动推演片的章走 source 分支并明确缺席，绝不回落到 外部推演文件。
        with self.assertRaises(StructureError) as caught:
            outline_binding(self.root, self.structure,
                            next(row for row in self.structure['chapters']
                                 if row['chapterId'] == CHAPTER_B))
        self.assertEqual(caught.exception.code, 'SOURCE_CHAPTER_PLAN_MISSING')

    def test_insert_preview_materializes_placement_chapter(self) -> None:
        # R3/R4 / Counterexample C：新章带 placement、无 volumeId、unbound、generation 前进。
        from scripts.core.source_placement import placement_from_volume
        plan = structure_commands.preview_insert_chapter(
            self.root, placement=placement_from_volume(VOLUME_A), primary_arc_id=ARC_MAIN,
            title='新章', actor='test')
        proposed = _staged_plan(self.root, plan)['proposedStructure']
        chapter = next(row for row in proposed['chapters']
                       if row['chapterId'] == plan['parameters']['chapterId'])
        self.assertEqual(chapter['placement'], {'scope': 'volume', 'volumeId': VOLUME_A})
        self.assertNotIn('volumeId', chapter)
        self.assertNotIn('unitId', chapter)
        self.assertEqual(chapter['proseBinding']['state'], 'unbound')
        self.assertIsNone(chapter['proseBinding']['expectedContentDigest'])
        self.assertEqual(chapter['proseBinding']['generation'], proposed['revision'])
        self.assertEqual(plan['parameters']['placement'], {'scope': 'volume', 'volumeId': VOLUME_A})

    def test_insert_full_preview_and_commit(self) -> None:
        # S5-5：Phase 1 允许 schema 5 的完整建章事务——preview 与 commit 都必须成功。
        from scripts.core.source_placement import placement_from_volume
        plan = structure_commands.preview_insert_chapter(
            self.root, placement=placement_from_volume(VOLUME_A), primary_arc_id=ARC_MAIN,
            title='新章', actor='test')
        result = structure_commands.commit_plan(self.root, operation_id=plan['operationId'],
                                                plan_hash=plan['planHash'], approved=True, actor='test')
        structure = load_structure(self.root)
        self.assertEqual(structure['revision'], result['afterRevision'])
        chapter = next(row for row in structure['chapters']
                       if row['chapterId'] == plan['parameters']['chapterId'])
        self.assertEqual(chapter['proseBinding']['generation'], structure['revision'])
        self.assertEqual(ownership_validation_report(structure)['chapterLocal'], {})
        self.assertEqual(ownership_validation_report(structure)['fatal'], [])

    def test_derived_outputs_use_single_source_branch(self) -> None:
        # R5 / S5-6：schema 5 只落 structure.json（+ extras），绝不走 维护型 Markdown 视图。
        from scripts.core.derived_views import derived_outputs, source_files
        outputs, owners = derived_outputs(self.root, self.structure)
        self.assertEqual(list(outputs), ['.govel/structure.json'])
        self.assertEqual(owners, {})
        sources = source_files(self.root)
        self.assertNotIn('规划/结构索引.md', outputs)
        self.assertNotIn('设定/实体索引.md', outputs)
        self.assertTrue(all('views.json' not in path for path in sources))

    def test_source_schema_exposes_only_current_format(self) -> None:
        from scripts.core.source_model import source_schema
        schema = source_schema()
        self.assertEqual(schema['schemaVersion'], 5)
        self.assertNotIn('structureVersions', schema)
        self.assertNotIn('sourceModes', schema)




class InitialOwnershipBoundaryTests(unittest.TestCase):
    """R13–R15: generic initial save can never create an ownership-aware project."""

    def _current_structure(self, root: Path) -> dict:
        structure = ownership_project(root)
        (root / '.govel' / 'structure.json').unlink()
        return structure

    def test_generic_initial_managed_save_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = self._current_structure(root)
            self.assertEqual(structure['chapters'][0]['proseBinding']['state'], 'managed')
            with self.assertRaises(StructureError) as caught:
                save_structure(root, structure)
            self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
            self.assertFalse((root / '.govel' / 'structure.json').exists())


    def test_generic_initial_unbound_only_save_rejected(self) -> None:
        # Option A 不做内容分级：所有权模型的初次落地一律走 sanctioned initializer。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            structure = self._current_structure(root)
            for chapter in structure['chapters']:
                chapter['proseBinding'] = seal_binding(
                    structure['projectId'], chapter['chapterId'], chapter['filePath'],
                    state='unbound', generation=structure['revision'],
                    expected_content_digest=None)
            with self.assertRaises(StructureError) as caught:
                save_structure(root, structure)
            self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')


    def test_generic_initial_current_binding_save_needs_the_initializer_capability(self) -> None:
        # 默认 当前格式 的初次落地仍受 Option A 保护：通用保存没有 capability 就拒绝，
        # 只有 sanctioned initializer（生产 `project init`）持有的那枚 capability 能过。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(StructureError) as caught:
                save_structure(root, empty_structure('PRJ-GREENFIELD-1', '新项目'))
            self.assertEqual(caught.exception.code, 'OWNERSHIP_UNAUTHORIZED_MUTATION')
            self.assertFalse((root / '.govel' / 'structure.json').exists())
            from scripts.core.prose_binding import initializer_authority
            saved = save_structure(root, empty_structure('PRJ-GREENFIELD-1', '新项目'),
                                   _initializer=initializer_authority('test'))
            self.assertEqual(saved['schemaVersion'], 5)
            self.assertEqual(saved['chapters'], [])

    def test_sanctioned_initializer_still_works(self) -> None:
        # R15：明确的初始化通道（importer）继续创建 当前格式。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = create_project_from_candidate(root, self._candidate())
            self.assertEqual(result['structure']['schemaVersion'], 5)
            self.assertEqual(load_structure(root)['chapters'][0]['proseBinding']['state'], 'unbound')
            # 通道之外没有第二条：再对同一个已存在的结构做 generic initial 写也不适用，
            # 而常规保护写路径（update-in-place）由 ProtectedWriterBoundaryTests 覆盖。
            structure = load_structure(root)
            structure['bookName'] = '改名'
            saved = save_structure(root, structure, expected_revision=structure['revision'])
            self.assertEqual(saved['bookName'], '改名')

    def _candidate(self) -> dict:
        return {'bookName': '导入书', 'volumes': [
                    {'volumeId': VOLUME_A, 'ordinal': 1, 'title': '第一卷', 'status': 'planned'}],
                'arcs': [{'arcId': ARC_MAIN, 'title': '主线', 'kind': 'main', 'status': 'planned',
                          'volumeIds': [VOLUME_A], 'parentArcId': None, 'promise': '',
                          'resolutionTargetChapterId': None}],
                'chapters': [{'chapterId': 'CH-IMPORT-0001', 'displayOrdinal': 1, 'displayCode': 'CH001',
                              'title': '第一章', 'placement': {'scope': 'volume', 'volumeId': VOLUME_A},
                              'filePath': '正文/CH-IMPORT-0001.md', 'status': 'planned',
                              'storyTimeStartId': None, 'storyTimeEndId': None}],
                'chapterArcLinks': [{'chapterId': 'CH-IMPORT-0001', 'arcId': ARC_MAIN,
                                     'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''}],
                'entities': [], 'relations': [], 'references': []}


class ImporterPathSafetyTests(unittest.TestCase):
    """R16–R18 / Counterexample E: path safety runs BEFORE any filesystem probe."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'proj'
        self.sentinel = self.base / 'sentinel.md'
        self.sentinel.write_text('项目外正文', encoding='utf-8')
        self.probed: list[str] = []

    def _candidate(self, file_path: str) -> dict:
        return {'bookName': '导入书', 'volumes': [
                    {'volumeId': VOLUME_A, 'ordinal': 1, 'title': '第一卷', 'status': 'planned'}],
                'arcs': [{'arcId': ARC_MAIN, 'title': '主线', 'kind': 'main', 'status': 'planned',
                          'volumeIds': [VOLUME_A], 'parentArcId': None, 'promise': '',
                          'resolutionTargetChapterId': None}],
                'chapters': [{'chapterId': 'CH-IMPORT-0001', 'displayOrdinal': 1, 'displayCode': 'CH001',
                              'title': '第一章', 'placement': {'scope': 'volume', 'volumeId': VOLUME_A}, 'filePath': file_path, 'status': 'planned',
                              'storyTimeStartId': None, 'storyTimeEndId': None}],
                'chapterArcLinks': [{'chapterId': 'CH-IMPORT-0001', 'arcId': ARC_MAIN,
                                     'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''}],
                'entities': [], 'relations': [], 'references': []}

    def _probe_guard(self):
        """Record any content probe that would touch the sentinel path."""
        import os
        from pathlib import Path as PathClass
        base = self.base
        sentinel_name = self.sentinel.name
        real_exists, real_read, real_stat = PathClass.exists, PathClass.read_bytes, os.stat
        probed = self.probed

        def note(path) -> None:
            text = str(path)
            if sentinel_name in text and 'proj' not in text:
                probed.append(text)

        def exists(path_self):
            note(path_self)
            return real_exists(path_self)

        def read_bytes(path_self):
            note(path_self)
            return real_read(path_self)

        def stat(path, *args, **kwargs):
            note(path)
            return real_stat(path, *args, **kwargs)

        PathClass.exists = exists
        PathClass.read_bytes = read_bytes
        os.stat = stat

        def restore() -> None:
            PathClass.exists = real_exists
            PathClass.read_bytes = real_read
            os.stat = real_stat
        return restore

    def _assert_rejected_before_probe(self, file_path: str) -> None:
        restore = self._probe_guard()
        try:
            with self.assertRaises(StructureError) as caught:
                create_project_from_candidate(self.root, self._candidate(file_path))
        finally:
            restore()
        self.assertEqual(caught.exception.code, 'PROSE_LOCATOR_UNSAFE')
        self.assertEqual(self.probed, [])  # 项目外路径从未被 exists/read/stat 探测
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), '项目外正文')

    def test_relative_traversal_rejected_before_probe(self) -> None:
        self._assert_rejected_before_probe('../sentinel.md')

    def test_absolute_path_rejected_before_probe(self) -> None:
        self._assert_rejected_before_probe(str(self.sentinel))

    def test_symlink_escape_rejected(self) -> None:
        # R18 / §30：root 内路径经 symlink 指到项目外，必须按既有 safe_project_path 策略
        # 拒绝（不发明第二套规则），且拒绝发生在任何内容读取之前。
        #
        # 可达性说明：`create_project_from_candidate` 要求目标目录为空，所以「root 内已存在
        # symlink」在导入器里构不出来。可证明的链路是——导入器对每个候选 filePath 都先走
        # 共享规则（下面的 spy 证明它确实被调用），而该规则会拒绝 symlink 逃逸（本测试第二段）。
        from unittest import mock
        from scripts.core import refactor_paths
        from scripts.core.write_sets import safe_project_path
        real_is_canonical = refactor_paths.is_canonical
        calls: list[tuple] = []

        def spy(root_path, relative):
            calls.append((str(root_path), relative))
            return real_is_canonical(root_path, relative)

        restore = self._probe_guard()
        try:
            with mock.patch.object(refactor_paths, 'is_canonical', side_effect=spy):
                create_project_from_candidate(self.root, self._candidate('正文/CH-IMPORT-0001.md'))
        finally:
            restore()
        self.assertIn((str(self.root), '正文/CH-IMPORT-0001.md'), calls)
        self.assertEqual(self.probed, [])
        # 共享规则本身拒绝 symlink 逃逸，且不读项目外内容。
        (self.root / '正文' / 'link.md').symlink_to(self.sentinel)
        with self.assertRaises(StructureError) as caught:
            safe_project_path(self.root, '正文/link.md')
        self.assertEqual(caught.exception.code, 'SYMLINK_PATH_FORBIDDEN')
        self.assertEqual(self.probed, [])
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), '项目外正文')

    def test_safe_slot_still_imports(self) -> None:
        # 正控：合法候选照常导入，证明拒绝来自路径安全而非别的门。
        result = create_project_from_candidate(self.root, self._candidate('正文/CH-IMPORT-0001.md'))
        self.assertEqual(result['structure']['schemaVersion'], 5)


class SourceImpactLocatorTests(unittest.TestCase):
    """R19–R21 / Counterexample F: a locator move is ownership metadata, not content."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        refactor_project(self.root, book_name='合成测试书')
        self.structure = load_structure(self.root)

    def test_file_path_only_change_moves_neither_plan_hash_nor_impact(self) -> None:
        from scripts.core.source_impact import source_impact
        moved = deepcopy(self.structure)
        chapter = next(row for row in moved['chapters'] if row['chapterId'] == CHAPTER_TWO)
        chapter['filePath'] = '正文/搬家后的位置.md'
        # R19：sourcePlanHash 不看 filePath（管理元数据）。
        self.assertEqual(chapter_plan(self.structure, CHAPTER_TWO)['sourcePlanHash'],
                         chapter_plan(moved, CHAPTER_TWO)['sourcePlanHash'])
        impact = source_impact(self.structure, moved)
        # R20/R21：纯定位符移动不产生内容变更、staleCandidates 与审批失效。
        self.assertNotIn(CHAPTER_TWO, impact['changedIds'])
        self.assertEqual(impact['staleCandidates'], [])
        self.assertEqual(impact['approvalInvalidations'], [])
        self.assertEqual(impact['registeredDependents'], [])
        # 完整结构快照继续看见它（management metadata 不被从真源快照里删掉）。
        self.assertNotEqual(structure_hash(self.structure), structure_hash(moved))

    def test_status_changes_are_not_content_changes(self) -> None:
        from scripts.core.source_impact import source_impact
        touched = deepcopy(self.structure)
        chapter = next(row for row in touched['chapters'] if row['chapterId'] == CHAPTER_TWO)
        chapter['status'] = 'draft'
        impact = source_impact(self.structure, touched)
        self.assertNotIn(CHAPTER_TWO, impact['changedIds'])
        self.assertEqual(impact['staleCandidates'], [])

    def test_authored_content_change_still_moves_the_plan_hash(self) -> None:
        # 阴性对照：真正的创作内容变化必须照旧被看见（隔离不是把 diff 变空）。
        from scripts.core.source_impact import source_impact
        edited = deepcopy(self.structure)
        chapter = next(row for row in edited['chapters'] if row['chapterId'] == CHAPTER_TWO)
        chapter['title'] = '旧楼道·重写'
        self.assertNotEqual(chapter_plan(self.structure, CHAPTER_TWO)['sourcePlanHash'],
                            chapter_plan(edited, CHAPTER_TWO)['sourcePlanHash'])
        impact = source_impact(self.structure, edited)
        self.assertIn(CHAPTER_TWO, impact['changedIds'])
        self.assertIn(CHAPTER_TWO, impact['staleCandidates'])
        self.assertEqual(impact['approvalInvalidations'], [CHAPTER_TWO])


if __name__ == '__main__':
    unittest.main()
