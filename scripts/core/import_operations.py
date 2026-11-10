"""One public import interface shared by MCP, CLI and capability discovery."""


def import_operations():
    from .operations import OperationSpec, STRING, INTEGER, BOOLEAN, STRINGS, _schema
    from .manuscript_import import ENCODINGS, inspect_import, preview_import, read_import_text
    from .import_commit import commit_import
    from .import_organization import prepare_organization, read_organization, preview_organization
    from .import_verification import verify_import
    from .import_status import organization_status
    from .operation_schemas import QUOTE, object_schema
    from .entity_registry import PREFIX_BY_TYPE
    def array(item):
        return {'type': 'array', 'items': item}
    def item(properties):
        return object_schema(properties, tuple(properties))
    chapter = item({'chapterId': STRING, 'synopsis': STRING, 'quotes': array(QUOTE),
                    'review': item({'facts': STRING, 'relationships': STRING, 'continuity': STRING, 'structure': STRING}),
                    'unresolved': STRINGS, 'existingRecordIds': STRINGS})
    content = object_schema({'chapters': array(chapter), 'acceptHistory': BOOLEAN,
        'entities': array(item({'ref': STRING, 'entityType': {'type': 'string', 'enum': [t for t in PREFIX_BY_TYPE if t != 'time_anchor']},
            'name': STRING, 'summary': STRING, 'chapterId': STRING, 'quote': QUOTE})),
        'facts': array(item({'ownerRef': STRING, 'text': STRING, 'chapterId': STRING, 'quote': QUOTE})),
        'relations': array(item({'fromRef': STRING, 'toRef': STRING, 'kind': STRING,
            'chapterId': STRING, 'quote': QUOTE, 'reason': STRING}))}, ('chapters',))
    boundary = {'type': 'object', 'properties': {'sourcePath': STRING, 'startLine': {'type': 'integer', 'minimum': 1}, 'title': STRING},
                'required': ['sourcePath', 'startLine', 'title'], 'additionalProperties': False}
    identity = {'planId': STRING, 'planHash': STRING}
    return [
        OperationSpec('import.verify', 'verify_manuscript_import',
            '根据冻结原稿、导入回执范围、内容摘要和当前正文归属重新验证全文覆盖。可选检查外部源文件是否仍匹配；不证明语义已审。',
            _schema({'checkSource': BOOLEAN}, ['projectRoot']), 'read', lambda root, a: verify_import(root, **a),
            ('import', 'verify'), category='author', discover=True, examples=({'projectRoot': '/path/to/小说'},)),
        OperationSpec('import.organization-status', 'query_import_followup',
            '统一查询导入后的实际待办、回溯审阅、关系盘点、风格与摘要入口；零关系和空规划目录不自动算错误。',
            _schema({}, ['projectRoot']), 'read', lambda root, a: organization_status(root),
            ('import', 'organization-status'), category='author', discover=True, examples=({'projectRoot': '/path/to/小说'},)),
        OperationSpec('import.organization-prepare', 'prepare_import_organization',
            '导入后由 AI 整理原稿：分批选择未整理章，返回回溯梗概、事实关系审阅表与分页原文入口。只准备，不推断事实或批准。',
            _schema({'chapterIds': {**STRINGS, 'minItems': 1, 'uniqueItems': True}, 'offset': INTEGER, 'limit': INTEGER}, ['projectRoot']),
            'read', lambda root, a: prepare_organization(root, **a), ('import', 'organization-prepare'),
            category='author', discover=True, examples=({'projectRoot': '/path/to/小说', 'limit': 3},)),
        OperationSpec('import.organization-read', 'read_import_organization',
            '分页读取本批已冻结原稿；每页绑定项目真源与正文对象，材料变化须重新准备。',
            _schema({'prepareRef': STRING, 'cursor': STRING, 'limit': INTEGER}, ['projectRoot', 'prepareRef']),
            'read', lambda root, a: read_organization(root, **a), ('import', 'organization-read'), category='author'),
        OperationSpec('import.organization-preview', 'preview_import_organization',
            '把 AI 读稿后提交的实体、事实、关系及回溯梗概装配为受审源事务，自动分配 ID 与引文偏移。acceptHistory=true 提请作者采用已写原稿及本批盘点；未决项拒绝采用，不补造 A/B。提交使用返回的 source.commit。',
            _schema({'prepareRef': STRING, 'requestId': STRING, 'content': content}, ['projectRoot', 'prepareRef', 'requestId', 'content']),
            'plan-write', lambda root, a: preview_organization(root, **a), ('import', 'organization-preview'), category='author'),
        OperationSpec('import.preview', 'preview_manuscript_import',
            '把外部 TXT/Markdown 小说导入新项目：自动分章并冻结全部原文，返回供作者确认的分章预览。工具生成 ID，无需编写脚本或 candidate.json。目标必须不存在。',
            _schema({'source': STRING, 'bookName': STRING, 'encoding': {'type': 'string', 'enum': list(ENCODINGS)},
                     'split': {'type': 'string', 'enum': ['auto', 'file']}, 'boundaries': {'type': 'array', 'items': boundary}}, ['projectRoot', 'source']),
            'plan-write', lambda root, a: preview_import(root, **a), ('import', 'preview'), category='author', discover=True,
            examples=({'projectRoot': '/path/to/新项目', 'source': '/path/to/原稿.txt', 'bookName': '山城旧事'},)),
        OperationSpec('import.inspect', 'inspect_manuscript_import',
            '分页核对冻结导入预览的章标题、源文件与行号范围；不创建项目或批准内容。',
            _schema({**identity, 'offset': INTEGER, 'limit': INTEGER}, ['projectRoot', 'planId', 'planHash']),
            'read', lambda root, a: inspect_import(root, **a), ('import', 'inspect'), category='author'),
        OperationSpec('import.read', 'read_import_manuscript',
            '分页读取导入预览冻结的原文，用于核对目录、章节边界与内容；offset 是字符偏移，startLine 为原文件行号。',
            _schema({**identity, 'sourcePath': STRING, 'offset': INTEGER}, ['projectRoot', 'planId', 'planHash', 'sourcePath']),
            'read', lambda root, a: read_import_text(root, **a), ('import', 'read'), category='author'),
        OperationSpec('import.commit', 'commit_manuscript_import',
            '作者明确批准预览后，一次性创建带原稿正文的新项目。保留来源证据、建立新正文归属；不继承旧批准，不声明文学审校完成。同一计划可重试。',
            _schema({**identity, 'approve': {**BOOLEAN, 'const': True}}, ['projectRoot', 'planId', 'planHash', 'approve']),
            'protected-write', lambda root, a: commit_import(root, **a), ('import', 'commit'), category='author'),
    ]
