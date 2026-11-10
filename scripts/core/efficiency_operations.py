"""Efficiency operations share the existing CLI/MCP declaration facade."""
from __future__ import annotations


def efficiency_operations():
    from .operations import OperationSpec, _schema, STRING, INTEGER, STRINGS, BOOLEAN
    from .operation_schemas import CANDIDATE_PATCH, DIAGNOSTICS, READ_REQUEST, evidence_decisions
    from .evidence_pack import prepare_evidence, pack_evidence
    from .evidence_submission import submit_evidence, evidence_status, confirm_continuity
    from .repair_context import repair_context
    from .reading_batch import read_batch
    from .candidate_patch import patch_candidate
    task = {'task': STRING, 'chapterId': STRING, 'povCharacter': STRING,
            'chapterGoal': STRING, 'knowledgeMode': STRING}
    from .outline_facts import inspect_outline
    from .artifact_templates import write_review_skeleton
    from .transport_cache import read_snapshot
    from .aggregate_queries import KINDS as AGGREGATES, query_aggregates
    from .history_index import KINDS as HISTORY_KINDS, query_history, history_index_state, rebuild_history_index
    from .operation_schemas import source_selection
    pagination = {'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}
    from .operation_schemas import structure_batch_step
    from .entity_registry import PREFIX_BY_TYPE
    from .structure_batch import preview_structure_batch
    ENTITY_TYPES = sorted(kind for kind in PREFIX_BY_TYPE if kind != 'time_anchor')
    return [
        OperationSpec('query.aggregate', 'query_domain_aggregates',
                      '分页聚合章节待处理事项、设定变更影响、角色复核负担与作者决定关联计划；复用领域状态，不另判通过。',
                      _schema({'kind': {'type': 'string', 'enum': list(AGGREGATES)},
                               'view': {'type': 'string', 'enum': ['summary', 'items']},
                               'chapterId': STRING, 'characterId': STRING, 'operationId': STRING,
                               'decisionId': STRING, 'selection': source_selection(), **pagination}, ['kind']),
                      'read', lambda root, a: query_aggregates(root, **a), ('query', 'aggregate'),
                      audiences=('planner', 'editor', 'router'), tasks=('planner', 'editor', 'repair', 'continuity'),
                      examples=({'kind': 'chapter-issues', 'chapterId': 'CH-EXAMPLE'},
                                {'kind': 'character-reviews', 'characterId': 'C-EXAMPLE', 'view': 'items'})),
        OperationSpec('history.query', 'query_project_history',
                      '只读查询现有操作、候选、诊断、批准和证据的作者侧历史元数据；不返回正文，不充当当前 Canon 或 POV 材料。',
                      _schema({'kind': {'type': 'string', 'enum': list(HISTORY_KINDS)},
                               'chapterId': STRING, 'recordId': STRING, 'requestId': STRING, **pagination}),
                      'read', lambda root, a: query_history(root, **a), ('history', 'query'),
                      audiences=('editor', 'router'), tasks=('editor', 'repair', 'continuity'),
                      examples=({'kind': 'evidence', 'requestId': 'review-batch-1'},)),
        OperationSpec('history.index-state', 'query_history_index_state',
                      '只读检查可重建历史索引是否匹配现有领域文件，不扫描正文或更新索引。',
                      _schema({}), 'read', lambda root, a: history_index_state(root), ('history', 'index-state'),
                      audiences=('router',), tasks=('continuity',)),
        OperationSpec('history.index-rebuild', 'rebuild_project_history_index',
                      '显式重建可删除的 SQLite 历史索引，复用未变文件；不改历史回执、Canon 或批准。',
                      _schema({}), 'derived-write', lambda root, a: rebuild_history_index(root), ('history', 'index-rebuild'),
                      audiences=('router',), tasks=('continuity',)),
        OperationSpec('task.snapshot', 'read_transport_snapshot',
                      '按原引用分页读取派生审计快照；不代表当前事实或审查通过。',
                      _schema({'reference': STRING, 'maxBytes': INTEGER, 'cursor': STRING,
                               'view': {'type': 'string', 'enum': ['model', 'full'],
                                        'description': '仅 review-form 引用可选：model 读取语义填写表，full 读取完整冻结表；分页保持同一视图。'}}, ['reference']),
                      'read', lambda root, a: read_snapshot(root, **a), ('task', 'snapshot'), discover=True),
        OperationSpec('task.outline-check', 'inspect_chapter_outline',
                      '只读提取推演定位、预算及参与实体；不批准计划。',
                      _schema({'chapterId': STRING}, ['chapterId']), 'read',
                      lambda root, a: inspect_outline(root, **a), ('task', 'outline-check'),
                      audiences=('planner', 'writer', 'editor', 'router'), tasks=('planner', 'writer', 'editor', 'repair')),
        OperationSpec('workflow.review-skeleton', 'write_review_payload_skeleton',
                      '按实际席位与标准槽位写出未登记的空白审校载荷，不代填结论。',
                      _schema({'chapterId': STRING, 'artifactType': {'type': 'string', 'enum': ['stage_a_review', 'stage_b_review']},
                               'output': STRING, 'repairRef': STRING}, ['chapterId', 'artifactType', 'output']), 'candidate-write',
                      lambda root, a: write_review_skeleton(root, **a), ('workflow', 'review-skeleton'),
                      audiences=('planner', 'editor', 'router'), tasks=('planner', 'editor', 'repair')),
        OperationSpec('workflow.patch-candidate', 'patch_candidate_prose',
                      '在选定候选版本上原子应用整批修订；也可只传已有 requestId 接续冻结步骤。不表示修复通过。',
                      _schema({'chapterId': STRING, 'candidateSha256': STRING, 'credential': STRING,
                               'patches': {'type': 'array', 'minItems': 1, 'items': CANDIDATE_PATCH},
                               'requestId': STRING, 'advanceLifecycle': BOOLEAN, 'automaticRepair': BOOLEAN,
                               'diagnostics': DIAGNOSTICS}) | {'anyOf': [{'required': ['patches']}, {'required': ['requestId']}]},
                      'candidate-write', lambda root, a: patch_candidate(root, **a), ('workflow', 'patch-candidate'),
                      audiences=('editor', 'router'), tasks=('repair', 'editor'),
                      examples=({'requestId': 'repair-1',
                                 'patches': [{'issueId': 'issue-1', 'anchorRef': '<repair anchorRef>',
                                              'replacement': '修订文字'}]},)),
        OperationSpec('evidence.prepare', 'prepare_task_evidence',
                      '准备版本绑定证据；review 可选 includeContext 或 aggregateRef 装配依赖、历史结论与待填字段，保留 selection/POV，不作判断。',
                      _schema({'kind': {'type': 'string', 'enum': ['review', 'prose', 'continuity']},
                               'recordIds': STRINGS, 'chapterId': STRING, 'paths': STRINGS,
                               'maxBytes': INTEGER, 'limit': INTEGER, 'cursor': STRING, 'snapshotRef': STRING,
                               'selection': source_selection(),
                               'includeContext': {**BOOLEAN, 'description': 'kind=review 时装配依赖、历史结论、来源变更和逐项待填字段。'},
                               'aggregateRef': {**STRING, 'description': '原聚合 selectionRef；仅选择其中的 source-review recordIds，保留 POV 并启用材料装配。'},
                               'resumeRequestIds': {**STRINGS, 'description': '带原 cursor 显式接续本次已登记的 evidence 请求；逐项证明仅这些回执改变，其他漂移仍拒绝。'},
                               'holdMaterials': {**BOOLEAN, 'description': '按需签发材料交付回执，供后续跨批复用；默认关闭，不增加普通读取开销。仅支持 review。'},
                               'priorReceipt': {**STRING, 'description': '明确仍持有的 materialReadReceipt；当前选择和权限先校验，只省略同版本已交付字符，已装配的逐项任务仍返回。续页保持同一 priorReceipt。'},
                               'view': {'type': 'string', 'enum': ['model', 'full']}}, ['kind']),
                      'read', lambda root, a: prepare_evidence(root, **{'view': 'model', **a}), ('evidence', 'prepare'),
                      audiences=('editor', 'planner', 'router'), tasks=('editor', 'repair', 'planner', 'continuity'),
                      examples=({'kind': 'review', 'recordIds': ['SF-EXAMPLE'], 'includeContext': True},)),
        OperationSpec('evidence.pack', 'pack_task_evidence',
                      '从未变化读取快照和显式结论装配 hash 与精确引文；不提交或批准。',
                      _schema({'requestId': STRING, 'snapshot': {'type': 'object'}, 'snapshotRef': STRING,
                               'decisions': evidence_decisions(), 'maxBytes': INTEGER,
                               'view': {'type': 'string', 'enum': ['model', 'full']}}, ['decisions']),
                      'read', lambda root, a: pack_evidence(root, **{'view': 'model', **a}), ('evidence', 'pack'),
                      audiences=('editor', 'planner', 'router'), tasks=('editor', 'repair', 'planner', 'continuity'),
                      examples=({'snapshotRef': '<evidence.prepare.snapshotRef>', 'requestId': 'review-1',
                                 'decisions': [{'recordId': 'SF-EXAMPLE', 'decision': 'not_checked',
                                                'reason': '明确留待补充材料', 'quotes': []}]},)),
        OperationSpec('evidence.submit', 'submit_packed_evidence',
                      '显式登记已装配的复核或正文引证；按工具批次原子提交，重试只执行未完成部分，不批准。',
                      _schema({'submissionRef': STRING, 'requestId': STRING, 'maxBytes': INTEGER}),
                      'runtime-write', lambda root, a: submit_evidence(root, **a), ('evidence', 'submit'),
                      audiences=('editor', 'planner', 'router'), tasks=('editor', 'repair', 'planner')),
        OperationSpec('evidence.status', 'query_evidence_submission',
                      '从现有证据回执查询批次和逐项进度；缓存丢失不丢失登记历史。',
                      _schema({'requestId': STRING, 'maxBytes': INTEGER, 'cursor': STRING}, ['requestId']),
                      'read', lambda root, a: evidence_status(root, **a), ('evidence', 'status')),
        OperationSpec('evidence.confirm-continuity', 'confirm_packed_continuity',
                      '用已装配引用执行原连续性确认；仍要求明确作者批准，普通证据登记不能代替。',
                      _schema({'submissionRef': STRING, 'approve': {'type': 'boolean', 'const': True}}, ['submissionRef', 'approve']),
                      'approval', lambda root, a: confirm_continuity(root, **a), ('evidence', 'confirm-continuity')),
        OperationSpec('workflow.repair-context', 'read_repair_context',
                      '分页交付稳定问题身份、当前定位、旧报告待复核项与补丁前后原文；返回 repairRef，不继承通过或批准。',
                      _schema({'chapterId': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['chapterId']),
                      'read', lambda root, a: repair_context(root, **a), ('workflow', 'repair-context'),
                      audiences=('editor', 'router'), tasks=('editor', 'repair'),
                      examples=({'chapterId': 'CH-EXAMPLE'},)),
        OperationSpec('structure.batch-preview', 'preview_structure_batch',
                      '把有序的章节移动、章节归档、实体登记与实体修改合成一个结构计划；预览不写真源，'
                      '一次 structure commit 提交、一次 structure undo 撤销。章节字段接受稳定 ID 或显示编号 CH001，'
                      '按预览前的编号解析。不新增权限：single-source 下修改既有实体仍须走受审提案。',
                      _schema({'steps': {'type': 'array', 'minItems': 1, 'items': structure_batch_step(ENTITY_TYPES),
                                         'description': '有序步骤 JSON 数组；长列表可写入文件后用 @路径 传入'}},
                              ['steps']),
                      'plan-write', lambda root, a: preview_structure_batch(root, steps=a['steps'], actor='author'),
                      ('structure', 'batch-preview'), category='author',
                      examples=({'steps': [{'op': 'move-chapter', 'chapterId': 'CH003', 'beforeChapterId': 'CH001'},
                                           {'op': 'create-entity', 'entityType': 'location', 'name': '藏书楼'}]},)),
        OperationSpec('task.read-batch', 'read_task_references',
                      '按当前任务读取显式选定且 hash 绑定的文件范围，去重并分页；不执行裸源查询。',
                      _schema({**task, 'requests': {'type': 'array', 'minItems': 1, 'items': READ_REQUEST},
                               'credential': STRING, 'readIds': STRINGS, 'priorReceipt': {'type': ['string', 'object']},
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: read_batch(root, **a), ('task', 'read-batch'),
                      audiences=('planner', 'writer', 'editor', 'titler', 'router'),
                      tasks=('planner', 'writer', 'editor', 'repair', 'titler', 'continuity')),
    ]
