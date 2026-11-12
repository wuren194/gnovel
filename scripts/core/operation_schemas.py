"""Reusable nested input contracts for the operation facade."""
from __future__ import annotations

from .chapter_contract import MODES

STRING = {'type': 'string'}
TEXT = {'type': 'string', 'minLength': 1}
INTEGER = {'type': 'integer'}
BOOLEAN = {'type': 'boolean'}
STRINGS = {'type': 'array', 'items': STRING}


def object_schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


PAGE = object_schema({'start': INTEGER, 'end': INTEGER, 'chunkHash': TEXT}, ('start', 'end', 'chunkHash'))
EXACT_QUOTE = object_schema({'text': TEXT, 'start': INTEGER, 'end': INTEGER}, ('text', 'start', 'end'))
QUOTE = {'type': 'object', 'oneOf': [object_schema({'text': TEXT}, ('text',)), EXACT_QUOTE,
    object_schema({'text': TEXT, 'page': PAGE}, ('text', 'page'))]}
CANDIDATE_PATCH = {'type': 'object', 'oneOf': [
    object_schema({'issueId': TEXT, 'original': QUOTE, 'replacement': STRING}, ('issueId', 'original', 'replacement')),
    object_schema({'issueId': TEXT, 'anchorRef': TEXT, 'replacement': STRING,
        'range': object_schema({'start': {'type': 'integer', 'minimum': 0},
                                'end': {'type': 'integer', 'minimum': 1}}, ('start', 'end'))},
        ('issueId', 'anchorRef', 'replacement')),
]}
READ_REQUEST = object_schema({'root': {'type': 'string', 'enum': ['project', 'toolbox', 'provider']},
    'path': TEXT, 'sha256': TEXT, 'startLine': INTEGER, 'endLine': INTEGER}, ('root', 'path', 'sha256'))
PATCH_PATH = {'type': 'array', 'minItems': 1, 'items': {'oneOf': [TEXT, {'type': 'integer', 'minimum': 0}]}}
RECORD_PATCH = {**object_schema({'op': {}, 'path': PATCH_PATH, 'value': {},
                                 'original': TEXT, 'replacement': STRING}, ('op', 'path')), 'oneOf': [
    {'properties': {'op': {'const': 'set'}, 'original': False, 'replacement': False},
     'required': ['op', 'value']},
    {'properties': {'op': {'const': 'remove'}, 'value': False, 'original': False, 'replacement': False},
     'required': ['op']},
    {'properties': {'op': {'const': 'text'}, 'value': False}, 'required': ['op', 'original', 'replacement']},
]}
FACT_PATCH = object_schema({'recordId': TEXT, 'reason': TEXT,
    'replacements': {'type': 'array', 'minItems': 1,
                     'items': object_schema({'old': TEXT, 'new': STRING}, ('old', 'new'))}},
    ('recordId', 'reason', 'replacements'))
SOURCE_QUOTE = object_schema({'fragmentId': TEXT, 'quote': TEXT}, ('fragmentId', 'quote'))
CONTENT_EVIDENCE = {'type': 'object', 'oneOf': [SOURCE_QUOTE,
    object_schema({'recordHash': TEXT}, ('recordHash',))]}
MAINTENANCE_SELECTION = object_schema({
    'segmentIds': {'type': 'array', 'items': TEXT, 'minItems': 1, 'uniqueItems': True},
    'recordIds': {'type': 'array', 'items': TEXT}, 'itemId': TEXT,
    'range': object_schema({'start': {'type': 'integer', 'minimum': 0},
                            'end': {'type': 'integer', 'minimum': 1}}, ('start', 'end')),
}, ('segmentIds', 'recordIds'))
DIAGNOSTICS = object_schema({'mode': {'type': 'string', 'enum': list(MODES)},
    'intensityLevel': INTEGER, 'sceneParagraphs': STRING, 'strictFloor': BOOLEAN})


CONTINUITY_DECISION = object_schema({'itemId': TEXT,
    'resolution': {'type': 'string', 'enum': ['applied', 'no_change']}, 'reason': TEXT,
    'evidence': {'type': 'array', 'items': object_schema({'path': TEXT, 'sha256': TEXT}, ('path',))}},
    ('itemId', 'resolution', 'reason'))
CONTINUITY_CONFIRMATION = object_schema({'decisions': {'type': 'array', 'items': CONTINUITY_DECISION},
    'summary': object_schema({'resolution': {'type': 'string', 'enum': ['refreshed', 'not_required']},
                              'reason': TEXT, 'sha256': TEXT}, ('resolution', 'reason'))}, ('decisions', 'summary'))


def summary_entry_updates(identity):
    return {'type': 'array', 'items': {'oneOf': [
        object_schema({'op': {'type': 'string', 'enum': ['add', 'replace']}, identity: TEXT,
                       'summary': TEXT, **({'title': STRING} if identity == 'chapterId' else {})},
                      ('op', identity, 'summary')),
        object_schema({'op': {'const': 'delete'}, identity: TEXT}, ('op', identity)),
    ]}}


def source_selection():
    from .source_selection import LIST_KEYS
    from .project import source_model_policy
    descriptions = {
        'ownerIds': '只选择这些对象的稳定 ID；省略表示范围内全部，显式空数组表示不选。',
        'ownerTypes': '对象类型，如 character、location、chapter；省略不按对象类型限制。',
        'includeFields': '包含字段 ID 或 key；与 includeAttributes 取并集，显式指定后仅选择其中匹配内容。',
        'excludeFields': '排除字段 ID 或 key；排除优先，但混合片段仍整段保留，不是脱敏。',
        'includeAttributes': '包含对象属性 ID；与 includeFields 取并集。',
        'excludeAttributes': '排除对象属性 ID，不保证混合片段中相关字词消失。',
        'roles': '片段内容身份筛选；规范属性值不受片段身份筛选影响。',
        'perspectiveIds': '人物或势力稳定 ID；perspective 模式必填，只读取显式登记的认知片段。',
    }
    result = object_schema({**{key: {'type': 'array', 'items': TEXT, 'description': descriptions[key]}
                               for key in sorted(LIST_KEYS)},
        'includeArchived': {**BOOLEAN, 'description': '默认 false，不含已归档对象及片段；true 才包含归档。'},
        'asOf': {'type': ['string', 'null'], 'minLength': 1,
                 'description': '按事件或时间锚 ID 选择故事时点；省略不按时点限制；null 表示时点未定，限时内容按 unknownTime 处理。'},
        'knowledgeMode': {'type': 'string', 'enum': source_model_policy()['knowledgeModes'],
                          'description': '默认 author；perspective 只交付明确登记的信念或文献，不推测人物已知内容。'},
        'unknownTime': {'type': 'string', 'enum': ['include', 'exclude'],
                        'description': '时间关系不明时默认 include 并标明；exclude 才排除。'}})
    result['description'] = ('JSON 选择器；不指定 selection 或 presetId 时选择全部已登记设定与规划，默认不含归档。'
                             '不是正文、项目备份或审校通过证明；仅适用于 single-source。')
    return result


def source_change(*, require_patch_hash=True):
    from .source_changes import _collections
    collection = {'type': 'string', 'enum': sorted(_collections())}
    return {'type': 'object', 'oneOf': [
        object_schema({'action': {'const': 'put'}, 'collection': collection, 'record': {'type': 'object'}},
                      ('action', 'collection', 'record')),
        object_schema({'action': {'const': 'delete'}, 'collection': collection, 'id': TEXT},
                      ('action', 'collection', 'id')),
        object_schema({'action': {'const': 'patch'}, 'collection': {'const': 'fragments'}, 'id': TEXT,
                       'expectedRecordHash': TEXT, 'patch': {'type': 'array', 'minItems': 1, 'items': RECORD_PATCH}},
                      ('action', 'collection', 'id', 'patch', *(('expectedRecordHash',) if require_patch_hash else ()))),
    ]}


def evidence_decisions():
    from .source_review import DECISIONS
    from .source_evidence import KINDS
    review = object_schema({'recordId': TEXT, 'decision': {'type': 'string', 'enum': sorted(DECISIONS)},
        'reason': TEXT, 'quotes': {'type': 'array', 'items': QUOTE}}, ('recordId', 'decision', 'reason', 'quotes'))
    prose = object_schema({'quote': QUOTE, 'targetIds': {'type': 'array', 'items': TEXT, 'minItems': 1},
        'kind': {'type': 'string', 'enum': sorted(KINDS)}, 'reason': TEXT}, ('quote', 'targetIds', 'kind', 'reason'))
    return {'type': ['array', 'object'], 'oneOf': [
        {'type': 'array', 'minItems': 1, 'items': review},
        {'type': 'array', 'minItems': 1, 'items': prose},
        CONTINUITY_CONFIRMATION,
    ]}


def structure_batch_step(entity_types) -> dict:
    """One ordered step of `structure.batch-preview`; local refs bind records created earlier in the batch."""
    return {'type': 'object', 'oneOf': [
        object_schema({'op': {'const': 'move-chapter'}, 'chapterId': TEXT, 'beforeChapterId': TEXT},
                      ('op', 'chapterId')),
        object_schema({'op': {'const': 'archive-chapter'}, 'chapterId': TEXT, 'reason': STRING},
                      ('op', 'chapterId')),
        object_schema({'op': {'const': 'create-entity'}, 'entityType': {'type': 'string', 'enum': list(entity_types)},
                       'name': TEXT, 'aliases': {'type': 'array', 'items': TEXT}, 'summary': STRING},
                      ('op', 'entityType', 'name')),
        object_schema({'op': {'const': 'create-volume'}, 'ref': TEXT, 'title': TEXT}, ('op', 'ref', 'title')),
        object_schema({'op': {'const': 'create-arc'}, 'ref': TEXT, 'title': TEXT, 'volumeRefs': STRINGS,
                       'kind': TEXT, 'parentArcRef': TEXT, 'promise': STRING}, ('op', 'ref', 'title', 'volumeRefs')),
        object_schema({'op': {'const': 'relink-range'}, 'volumeRef': TEXT, 'primaryArcRef': TEXT,
                       'fromOrdinal': {'type': 'integer', 'minimum': 1},
                       'toOrdinal': {'type': 'integer', 'minimum': 1},
                       'secondaryArcRefs': STRINGS},
                      ('op', 'volumeRef', 'primaryArcRef', 'fromOrdinal', 'toOrdinal')),
    ]}


def canon_proposal_item() -> dict:
    from .source_proposal_batch import TARGETS
    from .project import canon_proposal_batch_max_items
    migration = object_schema({
        'attributeId': TEXT, 'chapterId': TEXT,
        'values': {'type': 'array', 'items': object_schema({'valueId': TEXT, 'value': {}}, ('valueId', 'value'))},
        'evidence': {'type': 'array', 'minItems': 1, 'items': CONTENT_EVIDENCE},
    }, ('attributeId', 'values', 'evidence'))
    common = {'kind': {},
              'recordId': TEXT, 'chapterId': TEXT, 'reason': TEXT,
              'patch': {'type': 'array', 'minItems': 1, 'items': RECORD_PATCH},
              'evidence': {'type': 'array', 'items': CONTENT_EVIDENCE}, 'evidenceMode': {},
              'attributeMigrations': {'type': 'array', 'maxItems': canon_proposal_batch_max_items() - 1,
                                      'items': migration}}
    variants = []
    for kind in TARGETS:
        # The common evidence union owns exact object shapes. Branches only
        # narrow it, avoiding repeated full quote schemas in tool discovery.
        properties = {'kind': {'const': kind}, 'evidenceMode': False}
        if kind != 'field_migration':
            properties['attributeMigrations'] = False
        if kind not in {'attribute', 'planning', 'field_definition', 'field_migration'}:
            properties['evidence'] = {'type': 'array', 'items': {'required': ['fragmentId']}}
        if kind in {'planning', 'field_definition', 'field_migration'}:
            properties['evidence'] = {'type': 'array', 'maxItems': 0}
        if kind in {'field_definition', 'field_migration'}:
            properties['chapterId'] = False
        required = ['kind']
        if kind == 'field_migration':
            required.append('attributeMigrations')
        if kind == 'relation':
            properties['evidenceMode'] = {'type': 'string', 'enum': ['source', 'author_declaration']}
            required.append('evidenceMode')
        variants.append({'properties': properties, 'required': required})
    return {**object_schema(common, ('kind', 'recordId', 'patch', 'reason', 'evidence')), 'oneOf': variants}
