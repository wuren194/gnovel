"""One split/merge declaration consumed by MCP, CLI and capability discovery."""


def chapter_reshape_operations():
    from .operations import OperationSpec, _schema, STRING, INTEGER, BOOLEAN, STRINGS
    from .operation_schemas import object_schema
    from .chapter_reshape import preview_split, preview_merge, read_reshape, commit_reshape, query_lineage
    target = object_schema({'placement': {'type': 'object'}, 'primaryArcId': STRING,
                            'secondaryArcIds': STRINGS, 'unitId': {'type': ['string', 'null']}})
    target['description'] = '省略时沿用第一个来源章的卷、弧线和活动单元；unitId:null 明确不加入单元。'
    identity = {'operationId': STRING, 'planHash': STRING}
    page = {'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}
    return [
        OperationSpec('chapter.split-preview', 'preview_chapter_split',
            '在唯一匹配的原文片段之前拆章，工具计算位置，无需手算字符；也可显式传 Unicode 偏移。cutBefore 与 cutOffsets 二选一，缺失或歧义不猜。原章与原文保留并归档，新章待规划审校。CRLF 占两个字符。',
            _schema({'requestId': STRING, 'chapterId': STRING,
                     'cutOffsets': {'type': 'array', 'minItems': 1, 'items': {'type': 'integer', 'minimum': 1}},
                     'cutBefore': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'minLength': 1},
                                   'description': '按原文顺序给出精确片段，每项必须唯一匹配；在片段起点拆分。'},
                     'titles': STRINGS, 'reason': STRING, 'target': target, 'baseRevision': INTEGER},
                    ['requestId', 'chapterId', 'reason']) |
            {'oneOf': [{'required': ['cutOffsets'], 'properties': {'cutBefore': False}},
                       {'required': ['cutBefore'], 'properties': {'cutOffsets': False}}]},
            'plan-write', lambda root, a: preview_split(root, **a), ('structure', 'chapter-split-preview'),
            category='author', discover=True,
            examples=({'requestId': 'split-1', 'chapterId': 'CH-EXAMPLE', 'cutBefore': ['他把钥匙放在桌上。'],
                       'titles': ['交班之前', '交班之后'], 'reason': '作者选择在场景交接处拆章'},
                      {'requestId': 'split-2', 'chapterId': 'CH-EXAMPLE', 'cutOffsets': [1200],
                       'reason': '作者已明确给出字符边界'})),
        OperationSpec('chapter.merge-preview', 'preview_chapter_merge',
            '按指定章顺序合并已有受管正文；默认直接连接完整原文，separator 可显式加入分隔。保留并归档源章，新章不继承批准。',
            _schema({'requestId': STRING, 'chapterIds': {'type': 'array', 'minItems': 2, 'items': STRING},
                     'title': STRING, 'reason': STRING, 'separator': STRING, 'target': target, 'baseRevision': INTEGER},
                    ['requestId', 'chapterIds', 'title', 'reason']),
            'plan-write', lambda root, a: preview_merge(root, **a), ('structure', 'chapter-merge-preview'),
            category='author', discover=True,
            examples=({'requestId': 'merge-1', 'chapterIds': ['CH-FIRST', 'CH-SECOND'], 'title': '交班',
                       'reason': '作者将两个连续场景合成一章'},)),
        OperationSpec('chapter.reshape-read', 'read_chapter_reshape',
            '分页读取拆章/合章预览中冻结的完整新正文；不批准或改写。提交后材料仍只表示该次历史结果。',
            _schema({**identity, 'chapterId': STRING, **page}, ['operationId', 'planHash', 'chapterId']),
            'read', lambda root, a: read_reshape(root, **a), ('structure', 'chapter-reshape-read'), category='author'),
        OperationSpec('chapter.reshape-commit', 'commit_chapter_reshape',
            '按作者明确批准的拆合预览执行同一结构事务，保存来源关系和新正文；原操作/hash 重试只返回已提交结果。',
            _schema({**identity, 'approve': {**BOOLEAN, 'const': True}}, ['operationId', 'planHash', 'approve']),
            'protected-write', lambda root, a: commit_reshape(root, **a), ('structure', 'chapter-reshape-commit'), category='author'),
        OperationSpec('chapter.lineage', 'query_chapter_lineage',
            '从实际已提交事务读取本章拆分/合并的来源和去向，保留原章、正文摘要及字符范围；历史来源不授予新章批准。',
            _schema({'chapterId': STRING, **page}, ['chapterId']),
            'read', lambda root, a: query_lineage(root, **a), ('structure', 'chapter-lineage'), category='author',
            examples=({'chapterId': 'CH-EXAMPLE'},)),
    ]
