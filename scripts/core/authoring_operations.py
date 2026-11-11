"""Declarations for tool-owned authoring mechanics; CLI and MCP share them."""


def authoring_operations():
    from .operations import OperationSpec, _schema, STRING, BOOLEAN, STRINGS
    from .operation_schemas import object_schema, summary_entry_updates, QUOTE
    from .authoring_forms import review_form, prepare_state_candidate
    from .chapter_closeout import prepare_closeout, closeout_chapter
    from .chapter_facts import preview_chapter_facts, complete_chapter_facts
    summary = object_schema({'resolution': {'type': 'string', 'enum': ['not_required', 'refreshed']},
                             'reason': STRING}, ('resolution', 'reason'))
    summary_update = object_schema({'storySummary': STRING, 'reason': STRING,
        'plotLine': summary_entry_updates('chapterId'),
        'characterSummaries': summary_entry_updates('characterId')}, ('storySummary', 'reason'))
    fact = object_schema({'ownerId': STRING, 'field': STRING, 'statement': STRING,
                          'quote': QUOTE, 'reason': STRING, 'dependsOn': STRINGS, 'findingId': STRING},
                         ('ownerId', 'field', 'statement', 'quote', 'reason'))
    return [
        OperationSpec('workflow.review-form', 'prepare_review_form',
            '保存当前材料和实际席位绑定的阶段A/B空表，默认交付formRef与紧凑语义fields；只填写判断、引文和裁决，用submit的formRef+edits提交。full可看完整payload，不登记或批准。',
            _schema({'chapterId': STRING, 'artifactType': {'type': 'string', 'enum': ['stage_a_review', 'stage_b_review']},
                     'repairRef': STRING, 'batchRef': {'type': 'object'}, 'view': {'type': 'string', 'enum': ['model', 'full']}}, ['chapterId', 'artifactType']),
            'read', lambda root, a: review_form(root, **a), ('workflow', 'review-form'),
            audiences=('router', 'planner', 'editor'), tasks=('planner', 'editor', 'repair'),
            examples=({'chapterId': 'chapter-EXAMPLE', 'artifactType': 'stage_b_review'},)),
        OperationSpec('workflow.state-prepare', 'prepare_chapter_state_artifact',
            '自动生成并登记当前状态工件；无需提交空 changes 或手抄绑定。只传已有 requestId 可恢复，不批准、不判断有无新增事实。',
            _schema({'requestId': STRING, 'chapterId': STRING}, ['requestId']),
            'candidate-write', lambda root, a: prepare_state_candidate(root, **a), ('workflow', 'state-prepare'),
            audiences=('router', 'editor'), tasks=('editor', 'repair', 'continuity'),
            examples=({'requestId': 'state-1', 'chapterId': 'chapter-EXAMPLE'}, {'requestId': 'state-1'})),
        OperationSpec('source.chapter-facts-preview', 'preview_chapter_facts',
            '以明确归属、已有 topic 字段、陈述、正文引文和理由预览新事实；自动装配源记录和绑定。先完成本章连续性，再由原 source.commit 明确批准。',
            _schema({'requestId': STRING, 'chapterId': STRING,
                     'facts': {'type': 'array', 'minItems': 1, 'items': fact}}, ['requestId']) |
            {'anyOf': [{'required': ['chapterId', 'facts']},
                       {'properties': {'chapterId': False, 'facts': False}}]},
            'plan-write', lambda root, a: preview_chapter_facts(root, **a), ('source', 'chapter-facts-preview'),
            audiences=('router', 'editor', 'planner'), tasks=('editor', 'continuity', 'planner'),
            examples=({'requestId': 'facts-1', 'chapterId': 'chapter-EXAMPLE', 'facts': [{
                'ownerId': 'character-EXAMPLE', 'field': 'history.events', 'statement': '守门人归还钥匙。',
                'quote': {'text': '他把钥匙放回桌上。'}, 'reason': '正文明确记录归还动作。'}]},
                {'requestId': 'facts-1'})),
        OperationSpec('source.chapter-facts-complete', 'complete_chapter_facts',
            '从源事务与证据回执核对本章事实缺项；显式 registerEvidence 时自动装配并登记已提交事实的引文。原 requestId 续办，不批准源事务。',
            _schema({'requestId': STRING, 'registerEvidence': BOOLEAN}, ['requestId']),
            'runtime-write', lambda root, a: complete_chapter_facts(root, **a), ('source', 'chapter-facts-complete'),
            audiences=('router', 'editor', 'planner'), tasks=('editor', 'continuity', 'planner'),
            examples=({'requestId': 'facts-1'}, {'requestId': 'facts-1', 'registerEvidence': True})),
        OperationSpec('workflow.closeout-prepare', 'prepare_chapter_closeout',
            '冻结已检查的定稿代次、标题与材料版本，给出收尾步骤和缺项；可核对明确的事实请求，不批准或执行。',
            _schema({'chapterId': STRING, 'titleCandidateId': STRING, 'skipTitle': BOOLEAN, 'factRequestIds': STRINGS,
                     'responseView': {'type': 'string', 'enum': ['model', 'full'],
                         'description': '返回呈现方式；model为默认，full保留同次业务详情，不改变操作授权。'}}, ['chapterId']),
            'read', lambda root, a: prepare_closeout(root, **a), ('workflow', 'closeout-prepare'),
            audiences=('router', 'editor'), tasks=('editor', 'continuity'),
            examples=({'chapterId': 'chapter-EXAMPLE', 'skipTitle': True},)),
        OperationSpec('workflow.closeout', 'closeout_chapter',
            '按准备快照定稿，保存工位提供的摘要，并在 approveContinuity 明确授权后确认连续性。每步与原领域事务同写回执，requestId 单独恢复；不批准审校工件。',
            _schema({'requestId': STRING, 'prepareRef': STRING, 'summary': summary, 'summaryUpdate': summary_update,
                     'approveContinuity': BOOLEAN, 'prepareNext': BOOLEAN,
                     'responseView': {'type': 'string', 'enum': ['model', 'full'],
                         'description': '返回呈现方式；model为默认，full保留同次业务详情，不改变操作授权。'}}, ['requestId']),
            'protected-write', lambda root, a: closeout_chapter(root, **a), ('workflow', 'closeout'),
            audiences=('router',), tasks=('continuity',),
            examples=({'requestId': 'chapter-closeout-1', 'prepareRef': '<closeout-prepare.prepareRef>'},
                      {'requestId': 'chapter-closeout-1'})),
    ]
