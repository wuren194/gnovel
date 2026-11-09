"""Small authored source; no Markdown views or model-produced review claims."""
from __future__ import annotations

from pathlib import Path

from scripts.core.jsonio import write_json
from scripts.core.project import source_model_policy
from scripts.core.source_placement import placement_from_volume, project_placement
from scripts.core.structure_store import empty_structure, validate_structure


def source_fixture(root: Path | None = None) -> dict:
    """A legacy-lineage single source (schema 3).

    R1 Phase 5 起 `empty_structure(single_source=True)` 默认给 schema 5；这个 fixture 锁的是
    单一真源的阅读与规则语义，不是所有权模型，所以显式声明要 legacy-lineage。需要 binding
    的测试走生产材料化通道，不在这里手搓。
    """
    source = empty_structure('source-test', '单源测试书')
    source['entities'] = [
        {'entityId': 'C-ALPHA', 'entityType': 'character', 'name': '人物甲', 'aliases': [], 'status': 'active'},
        {'entityId': 'C-BETA', 'entityType': 'character', 'name': '人物乙', 'aliases': [], 'status': 'active'},
        {'entityId': 'TA-START', 'entityType': 'time_anchor', 'name': '成年', 'timeOrdinal': 0},
        {'entityId': 'TA-LATER', 'entityType': 'time_anchor', 'name': '后来', 'timeOrdinal': 10},
        {'entityId': 'TA-UNKNOWN', 'entityType': 'time_anchor', 'name': '未定时点'},
    ]
    source['fieldDefinitions'] = [
        {'fieldId': 'FD-HEIGHT', 'key': 'appearance.height', 'label': '身高', 'valueType': 'measurement', 'appliesTo': ['character']},
        {'fieldId': 'FD-SKIN', 'key': 'appearance.skin', 'label': '肤色', 'valueType': 'text', 'appliesTo': ['character']},
        {'fieldId': 'FD-LIFE', 'key': 'biography.experience', 'label': '经历', 'valueType': 'topic', 'appliesTo': ['character']},
    ]
    source['attributes'] = [
        {'attributeId': 'AT-HEIGHT', 'ownerId': 'C-ALPHA', 'fieldId': 'FD-HEIGHT', 'values': [
            {'valueId': 'FV-HEIGHT', 'value': {'number': 190, 'unit': 'cm'}, 'validDuring': {'fromId': 'TA-START'}}]},
        {'attributeId': 'AT-SKIN', 'ownerId': 'C-ALPHA', 'fieldId': 'FD-SKIN', 'values': [
            {'valueId': 'FV-SKIN', 'value': '白皙'}]},
        {'attributeId': 'AT-LIFE', 'ownerId': 'C-ALPHA', 'fieldId': 'FD-LIFE', 'values': []},
        {'attributeId': 'AT-BETA-HEIGHT', 'ownerId': 'C-BETA', 'fieldId': 'FD-HEIGHT', 'values': [
            {'valueId': 'FV-BETA-HEIGHT', 'value': {'number': 180, 'unit': 'cm'}}]},
    ]
    source['fragments'] = [
        {'fragmentId': 'SF-HEIGHT', 'ownerId': 'C-ALPHA', 'text': '他身高一米九。', 'order': 1,
         'contentBindings': ['AT-HEIGHT'], 'dependsOn': ['FV-HEIGHT'], 'validDuring': {'fromId': 'TA-START'}, 'contentRole': 'fact'},
        {'fragmentId': 'SF-LOOK', 'ownerId': 'C-ALPHA', 'text': '他高大而白皙。', 'order': 2,
         'contentBindings': ['AT-HEIGHT', 'AT-SKIN'], 'dependsOn': ['FV-HEIGHT', 'FV-SKIN'], 'contentRole': 'fact'},
        {'fragmentId': 'SF-MIXED', 'ownerId': 'C-ALPHA', 'text': '初次守门时，他一米九的身形挡住了门。', 'order': 3,
         'contentBindings': ['AT-HEIGHT', 'AT-LIFE'], 'dependsOn': ['FV-HEIGHT'], 'contentRole': 'fact'},
        {'fragmentId': 'SF-LIFE', 'ownerId': 'C-ALPHA', 'text': '他独自守过城门。', 'order': 4,
         'contentBindings': ['AT-LIFE'], 'dependsOn': ['FV-HEIGHT'], 'contentRole': 'fact'},
    ]
    validate_structure(source)
    if root is not None:
        write_json(root / '.govel/structure.json', source)
    return source






def single_source_chapter(source: dict, chapter_id: str, *, title: str = '章节',
                          volume_id: str | None = None, ordinal: int = 1,
                          status: str = 'planning_required') -> dict:
    """Build a schema-3 chapter dict with placement (volume scope, or project when
    volume_id is None). Callers append it and add their own chapterArcLinks."""
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
