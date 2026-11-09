"""Synthetic current-format project builder for authoring-refactor tests.

Everything here is a throwaway project in a temporary directory: volumes, arcs,
units, chapters, entities, relations, references, planning candidates and author
decisions, with synthetic body and outline files on disk so path refactors have
bytes to conserve. All names and story details belong to this neutral fixture.
"""
from __future__ import annotations

from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.structure_store import (PROJECT_SCHEMA_VERSION, WORKFLOW_VERSION, empty_structure,
                                          validate_structure)
from scripts.core.source_placement import placement_from_volume

VOLUME_A = 'VOL-REFACTOR-A'
VOLUME_B = 'VOL-REFACTOR-B'
ARC_MAIN = 'ARC-REFACTOR-MAIN'
ARC_SIDE = 'ARC-REFACTOR-SIDE'
UNIT_ONE = 'UN-REFACTOR-ONE'
CHAPTER_ONE = 'CH-REFACTOR-0001'
CHAPTER_TWO = 'CH-REFACTOR-0002'
CHAPTER_THREE = 'CH-REFACTOR-0003'
CHARACTER = 'C-REFACTOR-A'
PROP = 'P-REFACTOR-KEY'
LOCATION = 'L-REFACTOR-ROOM'
CANDIDATE = 'PC-REFACTOR-0001'
DECISION = 'DR-REFACTOR-0001'
RELATION = 'REL-REFACTOR-0001'


def refactor_project(root: Path, *, book_name: str = '山城旧事') -> dict:
    """Create a current-format single-source project and return its identity map."""
    root.mkdir(parents=True, exist_ok=True)
    for relative in ('.gnovel', '正文', '规划', '设定', '工作台'):
        (root / relative).mkdir(parents=True, exist_ok=True)
    structure = empty_structure('PRJ-REFACTOR-1', book_name)
    structure['volumes'] = [
        {'volumeId': VOLUME_A, 'ordinal': 1, 'title': '第一卷：旧名', 'status': 'planned'},
        {'volumeId': VOLUME_B, 'ordinal': 2, 'title': '第二卷：旧名', 'status': 'planned'},
    ]
    structure['arcs'] = [
        {'arcId': ARC_MAIN, 'title': '主线：旧名', 'kind': 'main', 'status': 'planned',
         'volumeIds': [VOLUME_A, VOLUME_B], 'parentArcId': None, 'promise': '', 'resolutionTargetChapterId': None},
        {'arcId': ARC_SIDE, 'title': '支线：旧名', 'kind': 'subplot', 'status': 'planned',
         'volumeIds': [], 'parentArcId': None, 'promise': '', 'resolutionTargetChapterId': None},
    ]
    structure['chapters'] = [
        {'chapterId': CHAPTER_ONE, 'displayOrdinal': 1, 'displayCode': 'CH001', 'title': '白塔',
         'placement': placement_from_volume(VOLUME_A), 'filePath': f'正文/{CHAPTER_ONE}.md', 'status': 'stable',
         'storyTimeStartId': None, 'storyTimeEndId': None},
        {'chapterId': CHAPTER_TWO, 'displayOrdinal': 2, 'displayCode': 'CH002', 'title': '旧楼道',
         'placement': placement_from_volume(VOLUME_A), 'filePath': f'正文/{CHAPTER_TWO}.md', 'status': 'stable',
         'storyTimeStartId': None, 'storyTimeEndId': None},
        {'chapterId': CHAPTER_THREE, 'displayOrdinal': 3, 'displayCode': 'CH003', 'title': '旧结局',
         'placement': placement_from_volume(VOLUME_B), 'filePath': f'正文/{CHAPTER_THREE}.md', 'status': 'stable',
         'storyTimeStartId': None, 'storyTimeEndId': None},
    ]
    structure['chapterArcLinks'] = [
        {'chapterId': CHAPTER_ONE, 'arcId': ARC_MAIN, 'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''},
        {'chapterId': CHAPTER_TWO, 'arcId': ARC_MAIN, 'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''},
        {'chapterId': CHAPTER_THREE, 'arcId': ARC_MAIN, 'role': 'primary', 'weight': 1.0, 'phaseId': None, 'notes': ''},
        {'chapterId': CHAPTER_TWO, 'arcId': ARC_SIDE, 'role': 'secondary', 'weight': 0.5, 'phaseId': None, 'notes': ''},
    ]
    structure['units'] = [
        {'unitId': UNIT_ONE, 'placement': placement_from_volume(VOLUME_A), 'title': '旧单元', 'ordinal': 1,
         'status': 'active',
         'members': [{'memberType': 'chapter', 'memberId': CHAPTER_ONE, 'order': 1},
                     {'memberType': 'chapter', 'memberId': CHAPTER_TWO, 'order': 2}]},
    ]
    structure['entities'] = [
        {'entityId': CHARACTER, 'entityType': 'character', 'name': '艾琳', 'aliases': ['小艾'], 'status': 'active',
         'summary': '主角', 'sourceRefs': []},
        {'entityId': PROP, 'entityType': 'prop', 'name': '旧钥匙', 'aliases': [], 'status': 'active',
         'summary': '关键物件', 'sourceRefs': []},
        {'entityId': LOCATION, 'entityType': 'location', 'name': '旧仓库', 'aliases': [], 'status': 'active',
         'summary': '开场地点', 'sourceRefs': []},
    ]
    structure['relations'] = [
        {'relationId': RELATION, 'fromEntity': CHARACTER, 'toEntity': PROP, 'kind': 'owns',
         'status': 'active', 'notes': ''},
    ]
    structure['references'] = [
        {'sourceId': CHAPTER_ONE, 'targetId': PROP, 'kind': 'uses'},
        {'sourceId': CHAPTER_ONE, 'targetId': LOCATION, 'kind': 'located_in'},
        {'sourceId': CHAPTER_TWO, 'targetId': CHARACTER, 'kind': 'features'},
    ]
    structure['planningCandidates'] = [
        {'candidateId': CANDIDATE, 'title': '旧候选章', 'summary': '尚未定位的候选',
         'placement': None, 'primaryArcId': None, 'secondaryArcIds': [], 'beforeChapterId': None,
         'status': 'draft'},
    ]
    structure['authorDecisions'] = [
        {'decisionId': DECISION, 'statement': '旧决定：主角先隐藏身份', 'rationale': '张力优先',
         'scopeIds': [VOLUME_A, CHARACTER], 'dependsOn': [], 'status': 'active',
         'scopeSnapshot': {'volumeId': VOLUME_A, 'entityId': CHARACTER},
         'approval': {'actorId': 'author-test', 'evidence': 'synthetic fixture approval'},
         'alternatives': [{'label': '先隐藏', 'outcome': 'chosen', 'reason': '张力'},
                          {'label': '直接揭露', 'outcome': 'rejected', 'reason': '太早'}]},
    ]
    write_json(root / '.gnovel' / 'project.json',
               {'schemaVersion': PROJECT_SCHEMA_VERSION, 'workflowVersion': WORKFLOW_VERSION,
                'projectId': structure['projectId'], 'bookName': book_name, 'projectRevision': 0,
                'approvalMode': 'manual'})
    bodies = {
        CHAPTER_ONE: '白塔立在河口。艾琳握着旧钥匙。\n',
        CHAPTER_TWO: '旧楼道里没有灯。艾琳又一次经过旧仓库。\n',
        CHAPTER_THREE: '旧结局落在那年冬天。\n',
    }
    for chapter_id, text in bodies.items():
        (root / '正文' / f'{chapter_id}.md').write_text(text, encoding='utf-8')
    from scripts.core.prose_binding import seal_binding
    from scripts.core.artifacts import sha256_text
    structure['revision'] = 1
    for chapter in structure['chapters']:
        chapter['proseBinding'] = seal_binding(structure['projectId'], chapter['chapterId'], chapter['filePath'],
            state='managed', generation=1, expected_content_digest='sha256:'+sha256_text(bodies[chapter['chapterId']]))
    structure['fieldDefinitions'] = [{'fieldId': 'FD-REFACTOR-PLAN', 'key': 'plot.plan', 'label': '章节计划', 'valueType': 'topic', 'appliesTo': ['chapter']}]
    for i, chapter in enumerate(structure['chapters']):
        aid, fid = f'AT-REFACTOR-PLAN-{i}', f'SF-REFACTOR-PLAN-{i}'
        structure['attributes'].append({'attributeId': aid, 'ownerId': chapter['chapterId'], 'fieldId': 'FD-REFACTOR-PLAN', 'values': []})
        structure['fragments'].append({'fragmentId': fid, 'ownerId': chapter['chapterId'], 'order': 1, 'text': '确认门口的钥匙。',
            'contentRole': 'plan', 'contentBindings': [aid], 'chapterContract': {'targetRange': [1, 1000], 'mode': 'plot', 'intensityLevel': 1}})
    validate_structure(structure)
    write_json(root/'.gnovel/structure.json', structure)
    return {'structure': structure, 'bodies': bodies,
            'volumeA': VOLUME_A, 'volumeB': VOLUME_B, 'arcMain': ARC_MAIN, 'arcSide': ARC_SIDE,
            'unit': UNIT_ONE, 'chapterOne': CHAPTER_ONE, 'chapterTwo': CHAPTER_TWO,
            'chapterThree': CHAPTER_THREE, 'character': CHARACTER, 'prop': PROP, 'location': LOCATION,
            'candidate': CANDIDATE, 'decision': DECISION, 'relation': RELATION}


IDENTITY_KEYS = ('projectId', 'volumeId', 'arcId', 'unitId', 'chapterId', 'entityId',
                 'relationId', 'candidateId', 'decisionId')


def identity_snapshot(structure: dict) -> dict:
    """Every registered stable ID, keyed by collection, for invariance assertions."""
    return {
        'projectId': {structure['projectId']},
        'volumeId': {row['volumeId'] for row in structure['volumes']},
        'arcId': {row['arcId'] for row in structure['arcs']},
        'unitId': {row['unitId'] for row in structure.get('units', [])},
        'chapterId': {row['chapterId'] for row in structure['chapters']},
        'entityId': {row['entityId'] for row in structure['entities']},
        'relationId': {row['relationId'] for row in structure['relations']},
        'candidateId': {row['candidateId'] for row in structure.get('planningCandidates', [])},
        'decisionId': {row['decisionId'] for row in structure.get('authorDecisions', [])},
    }
