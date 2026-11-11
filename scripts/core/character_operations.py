"""Public character queries, temporal checks and personality reminder sampling."""


def character_operations():
    from .operations import OperationSpec, _schema, STRING, INTEGER
    from .operation_schemas import SOURCE_QUOTE
    from .project import source_model_policy
    from .source_cognition import query_cognition, preview_check, query_checks
    from .source_profile import query_profile, sample_profile
    nullable_id = {'type': ['string', 'null']}
    page = {'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}
    readers = ('router', 'planner', 'writer', 'editor', 'composer')
    tasks = ('planner', 'writer', 'editor', 'repair')
    return [
        OperationSpec('source.cognition', 'query_source_cognition',
            '查询人物对具体命题或取值在故事时点的认知；返回计算后的timeMatch，区分明确不知情、没有记录和时点未定。不返回作者专属名称或客观内容。',
            _schema({'holderId': STRING, 'targetId': STRING, 'asOf': nullable_id}, ['holderId', 'targetId', 'asOf']),
            'read', lambda root, arguments: query_cognition(root, **arguments), ('source', 'cognition'),
            audiences=readers, tasks=tasks,
            examples=({'holderId': 'character-EXAMPLE', 'targetId': 'value-EXAMPLE', 'asOf': 'time-EXAMPLE'},)),
        OperationSpec('source.check-preview', 'preview_source_check',
            '预览独立check记录；工具绑定目标版本、实际引文和依赖。checkedAtId为故事时间，recordedAt为系统时间。人物证实不提升为作者事实，提交仍由source.commit明确批准。',
            _schema({'requestId': STRING, 'targetId': STRING, 'holderId': nullable_id,
                     'status': {'type': 'string', 'enum': source_model_policy()['checkStatuses']},
                     'checkedAtId': nullable_id, 'reason': STRING,
                     'evidence': {'type': 'array', 'items': SOURCE_QUOTE}},
                    ['requestId', 'targetId', 'status', 'checkedAtId', 'reason', 'evidence']),
            'plan-write', lambda root, arguments: preview_check(root, **arguments), ('source', 'check-preview'),
            audiences=('router', 'planner', 'editor'), tasks=tasks,
            examples=({'requestId': 'check-1', 'targetId': 'belief-EXAMPLE', 'holderId': None,
                       'status': 'unverified', 'checkedAtId': None, 'reason': '尚未核验。', 'evidence': []},)),
        OperationSpec('source.checks', 'query_source_checks',
            '分页读取目标的历史核验；返回故事时间匹配与证据是否仍匹配。旧证据失效保留旧结论，不解释为人物失忆或事实被推翻。holderId省略时读取作者核验。',
            _schema({'targetId': STRING, 'holderId': nullable_id, 'asOf': nullable_id, **page}, ['targetId']),
            'read', lambda root, arguments: query_checks(root, **arguments), ('source', 'checks'),
            audiences=readers, tasks=tasks),
        OperationSpec('source.profile', 'query_source_character_profile',
            '分页读取人物已有性格、偏好、基础反应、目标、优先事项、暂时状态和声音侧面；保留内容角色和时间，不把未来计划写成已发生变化。',
            _schema({'characterId': STRING, **page}, ['characterId']),
            'read', lambda root, arguments: query_profile(root, **arguments), ('source', 'profile'),
            audiences=readers, tasks=tasks),
        OperationSpec('source.profile-sample', 'sample_source_character_profile',
            '从作者配置的长期性格、偏好和基础反应中按固定权重不放回抽取提醒，保存sample回执。同requestId复用，资料变化时拒绝重抽。无场景参数，无强制表现或自动强化。',
            _schema({'poolId': STRING, 'requestId': STRING}, ['poolId', 'requestId']),
            'runtime-write', lambda root, arguments: sample_profile(root, **arguments), ('source', 'profile-sample'),
            audiences=readers, tasks=tasks,
            examples=({'poolId': 'pool-EXAMPLE', 'requestId': 'chapter-writing-1'},)),
    ]
