"""Explicit synthetic fixtures in the only supported project format."""
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]

from scripts import state_cache
from scripts.core.jsonio import read_json, write_json
from scripts.core.prose_binding import seal_binding
from scripts.core.structure_store import new_id, validate_structure
from scripts.novelctl import _initialize


def initialize_project(root: Path, *, book_name: str = '测试书') -> dict:
    _initialize(root, project_id=str(uuid.uuid4()), book_name=book_name,
                idempotency_key=str(uuid.uuid4()), approval_mode='manual')
    return read_json(root/'.govel/structure.json')


def create_current_project(root: Path, *, book_name: str = '测试书', chapter_count: int = 2,
                           arc_title: str = '主线') -> dict:
    structure = initialize_project(root, book_name=book_name)
    structure['pairAssessment'] = {'status': 'not_applicable', 'reason': '合成夹具只核对钥匙来源，没有配对设计。'}
    structure['revision'] = 1
    volume_id = structure['volumes'][0]['volumeId']
    arc_id = structure['arcs'][0]['arcId']
    structure['arcs'][0]['title'] = arc_title
    structure['entities'] = [
        {'entityId': 'character-TEST-ALPHA', 'entityType': 'character', 'name': '沈砚', 'aliases': ['阿砚'],
         'status': 'active', 'summary': '主角', 'voiceProfile': {'bannedPhrasesForRole': ['该死']}},
        {'entityId': 'prop-TEST-KEY', 'entityType': 'prop', 'name': '储物柜钥匙', 'aliases': ['钥匙'],
         'status': 'active', 'summary': '关键物件'},
        {'entityId': 'location-TEST-ROOM', 'entityType': 'location', 'name': '仓库', 'aliases': [],
         'status': 'active', 'summary': '开场地点'},
        {'entityId': 'hook-TEST-SECRET', 'entityType': 'hook', 'name': '钥匙秘密', 'aliases': [],
         'status': 'active', 'summary': '待揭晓'},
    ]
    structure['fieldDefinitions'] = [{'fieldId': 'field-TEST-PLAN', 'key': 'plot.plan', 'label': '章节计划',
                                    'valueType': 'topic', 'appliesTo': ['chapter']}]
    ids = []
    for number in range(1, chapter_count + 1):
        cid = new_id('chapter'); ids.append(cid)
        path = f'正文/{cid}.md'; body = f'第{number}章正文。'
        (root/path).write_text(body)
        from scripts.core.artifacts import sha256_text
        chapter = {'chapterId': cid, 'displayOrdinal': number, 'displayCode': f'CH{number:03}',
                   'title': f'第{number}章', 'placement': {'scope': 'volume', 'volumeId': volume_id},
                   'filePath': path, 'status': 'stable', 'storyTimeStartId': None, 'storyTimeEndId': None}
        chapter['proseBinding'] = seal_binding(structure['projectId'], cid, path, state='managed',
            generation=1, expected_content_digest='sha256:'+sha256_text(body))
        structure['chapters'].append(chapter)
        structure['chapterArcLinks'].append({'chapterId': cid, 'arcId': arc_id, 'role': 'primary'})
        aid = new_id('attribute')
        structure['attributes'].append({'attributeId': aid, 'ownerId': cid, 'fieldId': 'field-TEST-PLAN', 'values': []})
        structure['fragments'].append({'fragmentId': new_id('fragment', content_role='plan'), 'ownerId': cid, 'order': 1,
            'text': '主角核对钥匙来源，决定留在仓库。', 'contentRole': 'plan', 'contentBindings': [aid],
            'dependsOn': [], 'chapterContract': {'targetRange': [1, 10000], 'mode': 'plot', 'intensityLevel': 1,
                'behaviorCoverage': {'relationship': 'not_applicable', 'living': 'not_applicable'}}})
    validate_structure(structure)
    write_json(root/'.govel/structure.json', structure)
    state_cache.rebuild(root)
    return {'structure': structure, 'chapterIds': ids, 'volumeId': volume_id, 'arcId': arc_id,
            'characterId': 'character-TEST-ALPHA', 'propId': 'prop-TEST-KEY', 'locationId': 'location-TEST-ROOM', 'hookId': 'hook-TEST-SECRET'}
