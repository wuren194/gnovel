"""Synthetic current-format project fixtures for ownership tests.

Everything here is a throwaway project in a temporary directory: a single-source
structure already carrying sealed proseBinding objects, with real prose
bytes for the managed chapter. The structure lands through the sanctioned initial-write
shape (a direct sealed write, as the importer does) because generic `save_structure`
refuses to *create* an ownership-aware project. No real project is read.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.prose_binding import seal_binding
from scripts.core.source_placement import placement_from_volume
from scripts.core.structure_store import (PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, empty_structure,
                                          validate_structure)

PROJECT_ID = 'PRJ-BINDING-1'
VOLUME_A = 'VOL-BINDING-A'
ARC_MAIN = 'ARC-BINDING-MAIN'
CHAPTER_A = 'CH-BINDING-0001'  # managed, prose on disk
CHAPTER_B = 'CH-BINDING-0002'  # unbound, empty slot
CHAPTER_C = 'CH-BINDING-0003'  # unresolved, empty slot
MANAGED_PROSE = '第一章正文：白塔在雾里塌了一半。\n'


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def managed_digest() -> str:
    return f"sha256:{sha256_hex(MANAGED_PROSE.encode('utf-8'))}"


def _chapter(chapter_id: str, ordinal: int, title: str, status: str) -> dict:
    row = {
        'chapterId': chapter_id, 'displayOrdinal': ordinal, 'displayCode': f'CH{ordinal:03d}',
        'title': title, 'filePath': f'正文/{chapter_id}.md', 'status': status,
        'storyTimeStartId': None, 'storyTimeEndId': None,
    }
    row['placement'] = placement_from_volume(VOLUME_A)
    return row


def ownership_project(root: Path, *, revision: int = 7, extra_chapters: list[dict] | None = None,
                      prose_on_disk: dict[str, str] | None = None) -> dict:
    """Create a current-format single-source project with current bindings.

    The structure lands through the same sanctioned initial-write shape the importer
    uses (a direct, sealed-structure write), because generic `save_structure` refuses
    to create an ownership-aware project on an empty root（实施轮 §22–§25）。
    """
    schema_version = PROJECT_SCHEMA_VERSION
    root.mkdir(parents=True, exist_ok=True)
    for relative in ('.govel', '正文', '规划', '设定', '工作台'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    structure = empty_structure(PROJECT_ID, '绑定测试')
    structure['volumes'] = [{'volumeId': VOLUME_A, 'ordinal': 1, 'title': '第一卷', 'status': 'planned'}]
    structure['arcs'] = [{'arcId': ARC_MAIN, 'title': '主线', 'kind': 'main', 'status': 'planned',
                          'volumeIds': [VOLUME_A], 'parentArcId': None, 'promise': '',
                          'resolutionTargetChapterId': None}]
    structure['chapters'] = [
        _chapter(CHAPTER_A, 1, '白塔', 'stable'),
        _chapter(CHAPTER_B, 2, '空槽', 'planned'),
        _chapter(CHAPTER_C, 3, '悬案', 'draft'),
        *(extra_chapters or []),
    ]
    structure['chapterArcLinks'] = [
        {'chapterId': row['chapterId'], 'arcId': ARC_MAIN, 'role': 'primary', 'weight': 1.0,
         'phaseId': None, 'notes': ''}
        for row in structure['chapters']
    ]
    structure['schemaVersion'] = schema_version
    structure['projectId'] = PROJECT_ID
    structure['revision'] = revision
    # 材料化语义：binding 的 generation 指向最近一次正式变更的提交 revision；fixture
    # 视全部 binding 为夹具初始提交时建立。
    structure['chapters'][0]['proseBinding'] = seal_binding(
        PROJECT_ID, CHAPTER_A, structure['chapters'][0]['filePath'],
        state='managed', generation=revision, expected_content_digest=managed_digest())
    structure['chapters'][1]['proseBinding'] = seal_binding(
        PROJECT_ID, CHAPTER_B, structure['chapters'][1]['filePath'],
        state='unbound', generation=revision, expected_content_digest=None)
    structure['chapters'][2]['proseBinding'] = seal_binding(
        PROJECT_ID, CHAPTER_C, structure['chapters'][2]['filePath'],
        state='unresolved', generation=revision, expected_content_digest=None)
    (root / '正文' / f'{CHAPTER_A}.md').write_text(MANAGED_PROSE, encoding='utf-8')
    for relative, text in (prose_on_disk or {}).items():
        (root / relative).write_text(text, encoding='utf-8')
    write_json(root / '.govel' / 'project.json', {
        'schemaVersion': PROJECT_SCHEMA_VERSION, 'workflowVersion': WORKFLOW_VERSION,
        'projectId': PROJECT_ID, 'bookName': '绑定测试', 'projectRevision': revision,
        'createdAt': '2026-09-28T00:00:00Z', 'approvalMode': 'manual'})
    write_json(root / '.govel' / 'workflow.json', {
        'schemaVersion': 2, 'approvedChapterIds': [], 'chapterStates': {}, 'artifacts': {},
        'approvals': {}, 'repairRounds': {}, 'nextAction': None,
        'updatedAt': '2026-09-28T00:00:00Z'})
    validate_structure(structure)
    write_json(root / '.govel' / 'structure.json', structure)
    return structure


def new_chapter_row(*, chapter_id: str, ordinal: int, title: str) -> dict:
    """A chapter row an in-flight transaction creates: a NEW identity, no binding yet.

    Materialization is only ever legal for a chapter identity absent from the current
    structure（实施轮 §19），and the binding is what the transition *produces* — so the
    row goes into `proposed` unsealed and `apply_binding_transitions` seals it. Tests
    that exercise the legal transition build the row here and hand it over.
    """
    return _chapter(chapter_id, ordinal, title, 'planning_required')
