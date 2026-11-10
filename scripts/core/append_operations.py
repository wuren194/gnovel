"""One append operation schema for CLI, MCP and capability discovery."""


def append_operations():
    from .operations import OperationSpec, _schema, STRING, BOOLEAN, INTEGER, STRINGS
    from .manuscript_import import ENCODINGS
    from .manuscript_append import preview_append, inspect_append, read_append_text, commit_append
    from .operation_schemas import object_schema
    boundary = object_schema({'sourcePath': STRING, 'startLine': {'type': 'integer', 'minimum': 1},
                              'title': STRING}, ('sourcePath', 'startLine', 'title'))
    identity = {'operationId': STRING, 'planHash': STRING}
    return [
        OperationSpec('import.append-preview', 'preview_manuscript_append',
            '把外部 TXT/Markdown 原稿追加到已有项目：自动分章、生成新 ID 并冻结原文。显式选择卷或项目位置及主弧，可插入指定章之前；不继承批准。',
            _schema({'requestId': STRING, 'source': STRING, 'primaryArcId': STRING,
                'volumeId': STRING, 'placement': {'type': 'object'}, 'beforeChapterId': STRING,
                'secondaryArcIds': STRINGS, 'encoding': {'type': 'string', 'enum': list(ENCODINGS)},
                'split': {'type': 'string', 'enum': ['auto', 'file']},
                'boundaries': {'type': 'array', 'items': boundary}, 'reason': STRING, 'baseRevision': INTEGER},
                ['projectRoot', 'requestId', 'source', 'primaryArcId']) |
                {'oneOf': [{'required': ['volumeId']}, {'required': ['placement']}]},
            'plan-write', lambda root, a: preview_append(root, **a), ('import', 'append-preview'),
            category='author', discover=True,
            examples=({'projectRoot': '/path/to/书', 'requestId': 'append-chapters-1', 'source': '/path/to/新增章节.txt',
                       'volumeId': 'VOL-EXAMPLE', 'primaryArcId': 'ARC-EXAMPLE'},)),
        OperationSpec('import.append-inspect', 'inspect_manuscript_append',
            '分页核对追加原稿的冻结分章、来源区段、稳定 ID 与位置；提交后同样可查来源回执。',
            _schema({**identity, 'offset': INTEGER, 'limit': INTEGER}, ['projectRoot', 'operationId', 'planHash']),
            'read', lambda root, a: inspect_append(root, **a), ('import', 'append-inspect'), category='author'),
        OperationSpec('import.append-read', 'read_append_manuscript',
            '按字符偏移分页读取追加预览中的冻结原文，保留源行号；不把原稿提升为作品事实。',
            _schema({**identity, 'sourcePath': STRING, 'offset': INTEGER}, ['projectRoot', 'operationId', 'planHash', 'sourcePath']),
            'read', lambda root, a: read_append_text(root, **a), ('import', 'append-read'), category='author'),
        OperationSpec('import.append-commit', 'commit_manuscript_append',
            '作者明确批准后，以原结构事务一次性追加章节、正文归属和来源证据；新章规划待整理，同一计划可重试。',
            _schema({**identity, 'approve': {**BOOLEAN, 'const': True}}, ['projectRoot', 'operationId', 'planHash', 'approve']),
            'protected-write', lambda root, a: commit_append(root, **a), ('import', 'append-commit'), category='author'),
    ]
