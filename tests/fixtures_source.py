"""Small authored source; no Markdown views or model-produced review claims."""
from __future__ import annotations

from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.project import source_model_policy
from scripts.core.source_placement import placement_from_volume, project_placement
from scripts.core.structure_store import empty_structure, validate_structure


def source_fixture(root: Path | None = None) -> dict:
    """Current source records; managed prose fixtures use the publication tools."""
    source = empty_structure('source-test', '单源测试书')
    source['entities'] = [
        {'entityId': 'character-ALPHA', 'entityType': 'character', 'name': '人物甲', 'aliases': [], 'status': 'active'},
        {'entityId': 'character-BETA', 'entityType': 'character', 'name': '人物乙', 'aliases': [], 'status': 'active'},
        {'entityId': 'time-START', 'entityType': 'time_anchor', 'name': '成年', 'timeOrdinal': 0},
        {'entityId': 'time-LATER', 'entityType': 'time_anchor', 'name': '后来', 'timeOrdinal': 10},
        {'entityId': 'time-UNKNOWN', 'entityType': 'time_anchor', 'name': '未定时点'},
    ]
    source['fieldDefinitions'] = [
        {'fieldId': 'field-HEIGHT', 'key': 'appearance.height', 'label': '身高', 'valueType': 'measurement', 'appliesTo': ['character']},
        {'fieldId': 'field-SKIN', 'key': 'appearance.skin', 'label': '肤色', 'valueType': 'text', 'appliesTo': ['character']},
        {'fieldId': 'field-LIFE', 'key': 'biography.experience', 'label': '经历', 'valueType': 'topic', 'appliesTo': ['character']},
    ]
    source['attributes'] = [
        {'attributeId': 'attribute-HEIGHT', 'ownerId': 'character-ALPHA', 'fieldId': 'field-HEIGHT', 'values': [
            {'valueId': 'value-HEIGHT', 'value': {'number': 190, 'unit': 'cm'}, 'validDuring': {'fromId': 'time-START'}}]},
        {'attributeId': 'attribute-SKIN', 'ownerId': 'character-ALPHA', 'fieldId': 'field-SKIN', 'values': [
            {'valueId': 'value-SKIN', 'value': '白皙'}]},
        {'attributeId': 'attribute-LIFE', 'ownerId': 'character-ALPHA', 'fieldId': 'field-LIFE', 'values': []},
        {'attributeId': 'attribute-BETA-HEIGHT', 'ownerId': 'character-BETA', 'fieldId': 'field-HEIGHT', 'values': [
            {'valueId': 'value-BETA-HEIGHT', 'value': {'number': 180, 'unit': 'cm'}}]},
    ]
    source['fragments'] = [
        {'fragmentId': 'fact-HEIGHT', 'ownerId': 'character-ALPHA', 'text': '他身高一米九。', 'order': 1,
         'contentBindings': ['attribute-HEIGHT'], 'dependsOn': ['value-HEIGHT'], 'validDuring': {'fromId': 'time-START'}, 'contentRole': 'fact'},
        {'fragmentId': 'fact-LOOK', 'ownerId': 'character-ALPHA', 'text': '他高大而白皙。', 'order': 2,
         'contentBindings': ['attribute-HEIGHT', 'attribute-SKIN'], 'dependsOn': ['value-HEIGHT', 'value-SKIN'], 'contentRole': 'fact'},
        {'fragmentId': 'fact-MIXED', 'ownerId': 'character-ALPHA', 'text': '初次守门时，他一米九的身形挡住了门。', 'order': 3,
         'contentBindings': ['attribute-HEIGHT', 'attribute-LIFE'], 'dependsOn': ['value-HEIGHT'], 'contentRole': 'fact'},
        {'fragmentId': 'fact-LIFE', 'ownerId': 'character-ALPHA', 'text': '他独自守过城门。', 'order': 4,
         'contentBindings': ['attribute-LIFE'], 'dependsOn': ['value-HEIGHT'], 'contentRole': 'fact'},
    ]
    validate_structure(source)
    if root is not None:
        write_json(root / '.govel/structure.json', source)
    return source






def single_source_chapter(source: dict, chapter_id: str, *, title: str = '章节',
                          volume_id: str | None = None, ordinal: int = 1,
                          status: str = 'planning_required') -> dict:
    """Build a current chapter with placement and an unbound prose identity."""
    placement = placement_from_volume(volume_id) if volume_id is not None else project_placement()
    from scripts.core.prose_binding import seal_binding
    source['revision'] = max(1, source['revision'])
    row = {'chapterId': chapter_id, 'title': title, 'displayOrdinal': ordinal,
            'displayCode': f'CH{ordinal:03d}', 'placement': placement, 'filePath': f'正文/{chapter_id}.md',
            'status': status,
            'storyTimeStartId': None, 'storyTimeEndId': None}
    row['proseBinding'] = seal_binding(source['projectId'], chapter_id, row['filePath'],
        state='unbound', generation=source['revision'], expected_content_digest=None)
    return row
