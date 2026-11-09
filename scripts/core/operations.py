"""Operation definitions consumed by CLI, MCP, discovery and task execution.

Existing CLI parsers remain the parameter source for compatibility adapters.
New operations declare their schema here and generate both transports. The
legacy adapter can dispatch only enumerated parser leaves, never shell strings.
"""
from __future__ import annotations

import argparse
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .catalog_api import snake_case
from .query import DIMENSIONS, QUERY_SCHEMA, query_project
from .project import source_model_policy
from .structure_store import StructureError
from .operation_schemas import RECORD_PATCH, FACT_PATCH, SOURCE_QUOTE, CONTENT_EVIDENCE, DIAGNOSTICS, source_selection, source_change
from .argument_validation import validate_arguments

STRING = {'type': 'string'}
INTEGER = {'type': 'integer'}
BOOLEAN = {'type': 'boolean'}
STRINGS = {'type': 'array', 'items': STRING}
MAINTENANCE_REFERENCE = {'type': 'object', 'properties': {'checklistPath': STRING, 'itemIds': STRINGS},
                         'required': ['checklistPath', 'itemIds'], 'additionalProperties': False}


@dataclass(frozen=True)
class OperationSpec:
    id: str
    name: str
    description: str
    input_schema: dict
    effects: str
    handler: Callable
    cli: tuple[str, ...]
    mcp: bool = True
    category: str = 'station'
    audiences: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    discover: bool = False
    examples: tuple[dict, ...] = ()

    def tool(self) -> dict:
        return {'name': self.name, 'description': self.description, 'inputSchema': self.input_schema,
                'annotations': {'readOnlyHint': self.effects == 'read',
                                'destructiveHint': self.effects in ('protected-write', 'undo')}}


def _schema(properties: dict, required=()) -> dict:
    return {'type': 'object', 'properties': {'projectRoot': {**STRING, 'description': '项目根目录；未要求必填时默认当前目录。'}, **properties},
            'required': list(required), 'additionalProperties': False}


def new_operations() -> list[OperationSpec]:
    from .static_cache import cached_definition
    return cached_definition('new_operations', _new_operations)


def _new_operations() -> list[OperationSpec]:
    from .project import resolve_knowledge_mode, knowledge_modes
    from .task_prepare import TASK_STATIONS
    station_tasks = tuple(TASK_STATIONS)
    station_audiences = tuple(sorted({station.removeprefix('govel-') for station in TASK_STATIONS.values()}))
    # Supported modes are a public protocol declaration. Discovering tools must
    # not open a user data provider; execution validates the selected policy.
    knowledge_mode_schema = {'type': 'string', 'enum': knowledge_modes()}
    result = []
    from .source_time import query_timeline
    from .chapter_contract import contract_schema
    result.append(OperationSpec('source.timeline', 'query_source_timeline', '分页读取故事时间约束与事件呈现顺序；未知时间保持未知。',
                               _schema({'readerChapterId': STRING, 'limit': INTEGER,
                                        'maxBytes': INTEGER, 'cursor': STRING}),
                               'read', lambda root, a: query_timeline(root, **a),
                               ('source', 'timeline'), category='author'))
    from .source_rules import query_rules, RULE_ACTIONS
    result.append(OperationSpec('source.rules', 'query_source_rules', '按当前章节与动作检查登记规则；未知与未审语义规则保持待检查。',
                               _schema({'chapterId': STRING, 'action': {'type': 'string', 'enum': list(RULE_ACTIONS)},
                                        'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['chapterId', 'action']),
                               'read', lambda root, a: query_rules(root, **a), ('source', 'rules'), category='author'))
    from .source_plan import query_chapter_plan
    result.append(OperationSpec('source.chapter-plan', 'query_source_chapter_plan', '读取章节计划的实际输入集合与审批摘要，不依赖导出的推演 Markdown。',
                               _schema({'chapterId': STRING, 'view': {'type': 'string', 'enum': ['list', 'detail']},
                                        'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['chapterId']),
                               'read', lambda root, a: query_chapter_plan(root, **a), ('source', 'chapter-plan'), category='author',
                               audiences=station_audiences, tasks=station_tasks))
    from .planning_candidates import preview_candidate_change, query_planning_candidates
    candidate_statuses = source_model_policy()['planningCandidates']['statuses']
    result.extend([
        OperationSpec('source.planning-candidates', 'query_planning_candidates',
                      '有界读取未来章节规划节点；候选 ID 不等于正式章节 ID。',
                      _schema({'status': {'type': 'string', 'enum': candidate_statuses},
                               'candidateId': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: query_planning_candidates(root, **a),
                      ('source', 'planning-candidates'), category='author'),
        OperationSpec('source.candidate-preview', 'preview_planning_candidate_change',
                      '预览候选章节创建、修订、拆分、合并、声明批准、撤回或物化；仅提交后生成正式 CH。',
                      _schema({'action': {'type': 'string', 'enum': ['create', 'revise', 'split', 'merge', 'approve', 'withdraw', 'materialize']},
                               'expectedRevision': INTEGER, 'reason': STRING, 'candidateId': STRING,
                               'record': {'type': 'object'}, 'approvalActor': STRING, 'approvalEvidence': STRING},
                              ['action', 'expectedRevision', 'reason']),
                      'plan-write', lambda root, a: preview_candidate_change(root, **a),
                      ('source', 'candidate-preview'), category='author'),
    ])
    from .canon_proposals import resolve_canon_target, preview_canon_change
    result.extend([
        OperationSpec('source.canon-resolve', 'resolve_source_canon_target',
                      '按当前真源精确解析实体；歧义只返回候选，不给可写 sessionRef。',
                      _schema({'selector': STRING, 'entityType': STRING, 'chapterId': STRING,
                               'includeArchived': BOOLEAN}, ['selector']),
                      'read', lambda root, a: resolve_canon_target(root, **a),
                      ('source', 'canon-resolve'), category='author'),
        OperationSpec('source.canon-proposal-preview', 'preview_source_canon_change',
                      '以新鲜 sessionRef、旧值、原文引证和声明的作者批准预览单字段变更。',
                      _schema({'sessionRef': STRING, 'fieldPath': STRING, 'expectedOldExists': BOOLEAN,
                               'patch': {'type': 'array', 'minItems': 1, 'items': RECORD_PATCH}, 'expectedRecordHash': STRING,
                               'expectedOldValue': {'type': ['string', 'array', 'object', 'number', 'boolean', 'null']},
                               'newValue': {'type': ['string', 'array', 'object', 'number', 'boolean', 'null']}, 'reason': STRING,
                               'evidence': {'type': 'array', 'items': SOURCE_QUOTE},
                               'approvalActor': STRING, 'approvalEvidence': STRING, 'maintenanceReference': MAINTENANCE_REFERENCE},
                              ['sessionRef', 'reason',
                               'evidence', 'approvalActor', 'approvalEvidence']),
                      'plan-write', lambda root, a: preview_canon_change(root, **a),
                      ('source', 'canon-proposal-preview'), category='author'),
    ])
    from .source_content_proposals import PROTECTED, resolve_content_target, preview_content_change
    from .source_resolution_batch import resolve_content_batch
    from .project import query_policy
    result.extend([
        OperationSpec('source.content-resolve-batch', 'resolve_source_content_batch',
                      '在一个源快照上批量解析同类记录并分页返回紧凑绑定；resolutionRef 可交给规划 change-preview，未找到的记录不给写入绑定。',
                      _schema({'recordIds': {**STRINGS, 'minItems': 1, 'maxItems': query_policy()['maxLimit'], 'uniqueItems': True},
                               'kind': {'type': 'string', 'enum': list(PROTECTED)}, 'chapterId': STRING,
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['recordIds', 'kind']),
                      'read', lambda root, a: resolve_content_batch(root, **a), ('source', 'content-resolve-batch'), category='author',
                      examples=({'kind': 'planning', 'recordIds': ['SF-FIRST', 'SF-SECOND']},)),
        OperationSpec('source.content-resolve', 'resolve_source_content_target',
                      '解析当前属性、事实或规划片段；model 返回补丁所需绑定和分页读取入口，不要求长记录完整返回。',
                      _schema({'recordId': STRING, 'kind': {'type': 'string', 'enum': list(PROTECTED)},
                               'chapterId': STRING, 'view': {'type': 'string', 'enum': ['full', 'model']}}, ['recordId', 'kind']),
                      'read', lambda root, a: resolve_content_target(root, **a),
                      ('source', 'content-resolve'), category='author',
                      examples=({'recordId': 'SF-EXAMPLE', 'kind': 'planning', 'view': 'model'},)),
        OperationSpec('source.content-proposal-preview', 'preview_source_content_change',
                      '以 content-resolve 的 sessionRef、expectedRecordHash 和局部 patch 预览属性、事实或规划修改；也接受完整旧新记录。规划仍走 source_change，事实仍须引证。',
                      _schema({'sessionRef': STRING, 'expectedOldRecord': {'type': 'object'},
                               'patch': {'type': 'array', 'minItems': 1, 'items': RECORD_PATCH}, 'expectedRecordHash': STRING,
                               'newRecord': {'type': ['object', 'null']}, 'reason': STRING,
                               'evidence': {'type': 'array', 'items': CONTENT_EVIDENCE},
                               'approvalActor': STRING, 'approvalEvidence': STRING, 'maintenanceReference': MAINTENANCE_REFERENCE},
                              ['sessionRef', 'reason', 'evidence',
                               'approvalActor', 'approvalEvidence']),
                      'plan-write', lambda root, a: preview_content_change(root, **a),
                      ('source', 'content-proposal-preview'), category='author', discover=True,
                      examples=({'sessionRef': '<content-resolve.sessionRef>', 'expectedRecordHash': '<recordHash>',
                                 'patch': [{'op': 'text', 'path': ['text'], 'original': '旧文', 'replacement': '新文'}],
                                 'reason': '已确认的规划修订', 'evidence': [], 'approvalActor': '作者',
                                 'approvalEvidence': '已确认本次修改'},)),
    ])
    from .source_search import search_source
    from .source_fact_patch import preview_fact_patch
    from .source_maintenance import check_maintenance
    from .source_maintenance_prepare import prepare_maintenance
    from .source_maintenance_document import write_checklist
    from .operation_schemas import MAINTENANCE_SELECTION
    from .source_requests import query_request_status, renew_request
    from .source_proposal_batch import preview_proposal_batch
    from .operation_schemas import canon_proposal_item
    from .project import canon_proposal_batch_max_items
    from .structure_commands import commit_plan
    result.extend([
        OperationSpec('source.commit', 'commit_source_change',
                      '提交明确批准的源预览；与 structure commit 共用事务入口。使用预览 nextAction 中的 operationId 和 planHash。',
                      _schema({'operationId': STRING, 'planHash': STRING, 'approve': {'type': 'boolean', 'const': True}},
                              ['operationId', 'planHash', 'approve']), 'protected-write',
                      lambda root, a: commit_plan(root, operation_id=a['operationId'], plan_hash=a['planHash'],
                                                 approved=a['approve'], actor='app'),
                      ('source', 'commit'), category='author', discover=True,
                      examples=({'operationId': 'OP-EXAMPLE', 'planHash': '<preview.planHash>', 'approve': True},)),
        OperationSpec('source.proposal-batch-preview', 'preview_source_proposal_batch',
                      '同源快照批量预览设定修改。field_definition 只更正名称/说明；field_migration 受审修改key/类型/适用对象，全量列举属性和known值及各自证据；两种字段项顶层evidence为空。整批一次提交，返回diff与提交入口。',
                      _schema({'sourceHash': STRING,
                               'items': {'type': 'array', 'minItems': 1, 'maxItems': canon_proposal_batch_max_items(),
                                         'items': canon_proposal_item()},
                               'requestId': STRING, 'approvalActor': STRING, 'approvalEvidence': STRING,
                               'maintenanceReference': MAINTENANCE_REFERENCE},
                              ['sourceHash', 'items', 'requestId', 'approvalActor', 'approvalEvidence']),
                      'plan-write', lambda root, a: preview_proposal_batch(root, **a),
                      ('source', 'proposal-batch-preview'), category='author', discover=True,
                      examples=({'sourceHash': '<sourceHash>', 'requestId': 'settings-batch-1',
                                 'approvalActor': '作者', 'approvalEvidence': '已确认的修订方案',
                                 'items': [{'kind': 'fact', 'recordId': 'SF-EXAMPLE',
                                            'patch': [{'op': 'text', 'path': ['text'], 'original': '旧称', 'replacement': '新称'}],
                                            'reason': '落实已确认条目', 'evidence': [{'fragmentId': 'SF-EXAMPLE', 'quote': '旧称'}]}]},)),
        OperationSpec('source.search', 'search_source_fragments',
                      '按字面词的任一、全部或近邻匹配检索片段；可按对象名称筛选。保留 POV 与上下文，命中不等于冲突。',
                      _schema({'terms': STRINGS, 'selection': source_selection(), 'contextCharacters': INTEGER,
                               'ownerSelectors': {**STRINGS, 'description': '当前选择范围内的对象稳定 ID、名称或唯一别名；歧义拒绝，不扩大 selection。'},
                               'matchMode': {'type': 'string', 'enum': ['any', 'all', 'near']},
                               'nearCharacters': {**INTEGER, 'description': 'near 必填：同一片段内包含全部词的窗口总字符数上限。'},
                               'literalTerms': {**BOOLEAN, 'description': '显式按字面查找形如 JSON 数组的词；否则提示疑似 CLI 数组误传。'},
                               'view': {'type': 'string', 'enum': ['full', 'model']},
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['terms']),
                      'read', lambda root, a: search_source(root, **a), ('source', 'search'), category='author',
                      examples=({'terms': ['门', '钥匙'], 'matchMode': 'all', 'view': 'model'},)),
        OperationSpec('source.fact-patch-preview', 'preview_source_fact_patch',
                      '在同一源快照上批量预览事实文字替换；旧文唯一命中、拒绝重叠，复用受审提案及原子提交。',
                      _schema({'sourceHash': STRING, 'patches': {'type': 'array', 'minItems': 1, 'items': FACT_PATCH},
                               'approvalActor': STRING, 'approvalEvidence': STRING, 'requestId': STRING,
                               'maintenanceReference': MAINTENANCE_REFERENCE},
                              ['sourceHash', 'patches', 'approvalActor', 'approvalEvidence']),
                      'plan-write', lambda root, a: preview_fact_patch(root, **a),
                      ('source', 'fact-patch-preview'), category='author'),
        OperationSpec('source.maintenance-prepare', 'prepare_source_maintenance',
                      '读取方案指纹、项目模式和修改入口，分页交付保留原文的语法分段；清单语义仍由工位选择，不授予批准。',
                      _schema({'sourcePath': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['sourcePath']), 'read',
                      lambda root, a: prepare_maintenance(root, **a), ('source', 'maintenance-prepare'), category='project-maintenance',
                      examples=({'sourcePath': '工作台/定稿方案.md'},)),
        OperationSpec('source.maintenance-checklist', 'write_source_maintenance_checklist',
                      '按明确选择的连续方案分段或子范围填写引文、版本和条目身份；创建新清单或复用完全相同文件，不覆盖其他内容，不判断要求或批准。',
                      _schema({'documentRef': STRING,
                               'items': {'type': 'array', 'minItems': 1, 'items': MAINTENANCE_SELECTION},
                               'output': STRING}, ['documentRef', 'items', 'output']), 'candidate-write',
                      lambda root, a: write_checklist(root, **a), ('source', 'maintenance-checklist'), category='project-maintenance', discover=True,
                      examples=({'documentRef': '<maintenance-prepare.documentRef>',
                                 'items': [{'segmentIds': ['<segments.id>'], 'recordIds': []}],
                                 'output': '工作台/修订覆盖.json'},)),
        OperationSpec('source.request-status', 'query_source_request_status',
                      '按稳定请求 ID 查询未开始、已准备、过期、已提交或已撤销；只读，不自动重放。',
                      _schema({'requestId': STRING}, ['requestId']), 'read',
                      lambda root, a: query_request_status(root, **a), ('source', 'request-status'), category='project-maintenance'),
        OperationSpec('source.request-renew', 'renew_source_request',
                      '仅超时且源、证据、写集和工具版本均未变化时续期原预览；保留请求与条目，返回新的 planHash，仍须明确批准提交。',
                      _schema({'requestId': STRING}, ['requestId']), 'plan-write',
                      lambda root, a: renew_request(root, **a), ('source', 'request-renew'), category='project-maintenance',
                      examples=({'requestId': 'settings-batch-1'},)),
        OperationSpec('source.maintenance-check', 'check_source_maintenance',
                      '核对工作台方案清单、提交回执与当前复核状态；不改写台账、不证明自然语言方案已全部落实。',
                      _schema({'checklistPath': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['checklistPath']),
                      'read', lambda root, a: check_maintenance(root, **a),
                      ('source', 'maintenance-check'), category='project-maintenance'),
    ])
    from .source_relation_proposals import resolve_relation_target, preview_relation_change
    result.extend([
        OperationSpec('source.relation-resolve', 'resolve_source_relation_target',
                      '按关系 ID 或端点与类型解析已登记 Canon 关系；歧义不返回可写会话。',
                      _schema({'relationId': STRING, 'fromEntity': STRING, 'toEntity': STRING,
                               'kind': STRING, 'chapterId': STRING}),
                      'read', lambda root, a: resolve_relation_target(root, **a),
                      ('source', 'relation-resolve'), category='author'),
        OperationSpec('source.relation-proposal-preview', 'preview_source_relation_change',
                      '以完整旧关系、来源引文或作者直接声明和声明批准预览单条关系变更。',
                      _schema({'sessionRef': STRING, 'expectedOldRecord': {'type': 'object'},
                               'patch': {'type': 'array', 'minItems': 1, 'items': RECORD_PATCH}, 'expectedRecordHash': STRING,
                               'newRecord': {'type': ['object', 'null']}, 'reason': STRING,
                               'evidence': {'type': 'array', 'items': SOURCE_QUOTE},
                               'evidenceMode': {'type': 'string', 'enum': ['source', 'author_declaration']},
                               'approvalActor': STRING, 'approvalEvidence': STRING, 'maintenanceReference': MAINTENANCE_REFERENCE},
                              ['sessionRef', 'reason', 'evidence',
                               'evidenceMode', 'approvalActor', 'approvalEvidence']),
                      'plan-write', lambda root, a: preview_relation_change(root, **a),
                      ('source', 'relation-proposal-preview'), category='author'),
    ])
    from .context_health import query_context_health
    result.append(OperationSpec('source.context-health', 'query_source_context_health',
                                '检查已构建的工位上下文的新鲜度、预算、缺口、重复和冲突；不扫描隐式正文。',
                                _schema({'chapterId': STRING, 'task': {'type': 'string', 'enum': ['planner', 'writer', 'editor']},
                                         'povCharacter': STRING, 'chapterGoal': STRING, 'expectedHash': STRING,
                                         'knowledgeMode': knowledge_mode_schema}, ['chapterId']),
                                'read', lambda root, a: query_context_health(root, **a),
                                ('source', 'context-health'), category='author'))
    from .context_preheat import preheat_next_chapter
    result.append(OperationSpec('source.context-preheat', 'preheat_source_next_chapter',
                                '只读预取下一登记章节的规划元数据；前章稳定后按输入 hash 复验，不生成未来剧情。',
                                _schema({'afterChapterId': STRING, 'expectedInputHash': STRING}, ['afterChapterId']),
                                'read', lambda root, a: preheat_next_chapter(root, **a),
                                ('source', 'context-preheat'), category='author'))
    from .author_decisions import preview_decision_change, query_author_decisions
    decision_statuses = source_model_policy()['authorDecisions']['statuses']
    result.extend([
        OperationSpec('source.author-decisions', 'query_source_author_decisions',
                      '有界读取作者规划决定及作用域新鲜度；不把决定当作世界事实。',
                      _schema({'decisionId': STRING, 'status': {'type': 'string', 'enum': decision_statuses},
                               'scopeId': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: query_author_decisions(root, **a),
                      ('source', 'author-decisions'), category='author'),
        OperationSpec('source.author-decision-preview', 'preview_source_author_decision',
                      '预览作者规划决定的创建、声明批准或废止；与 Canon 事实分离。',
                      _schema({'action': {'type': 'string', 'enum': ['create', 'approve', 'supersede']},
                               'expectedRevision': INTEGER, 'reason': STRING, 'decisionId': STRING,
                               'statement': STRING, 'rationale': STRING, 'scopeIds': STRINGS,
                               'dependsOn': STRINGS, 'approvalActor': STRING, 'approvalEvidence': STRING,
                               'alternatives': {'type': 'array', 'items': {'type': 'object'}},
                               'constraints': STRINGS, 'evidenceRefs': STRINGS, 'supersedes': STRING},
                              ['action', 'expectedRevision', 'reason']),
                      'plan-write', lambda root, a: preview_decision_change(root, **a),
                      ('source', 'author-decision-preview'), category='author'),
    ])
    from .work_voice_profile import query_voice_profile, preview_voice_profile
    result.extend([
        OperationSpec('source.voice-profile', 'query_source_voice_profile',
                      '读取作品级叙述风格，区别于角色对白声纹。', _schema({}),
                      'read', lambda root, a: query_voice_profile(root),
                      ('source', 'voice-profile'), category='author'),
        OperationSpec('source.voice-profile-preview', 'preview_source_voice_profile',
                      '预览作品叙述风格草稿或绑定具体版本的批准。',
                      _schema({'action': {'type': 'string', 'enum': ['set', 'approve']}, 'expectedRevision': INTEGER,
                               'reason': STRING, 'axes': {'type': 'object'}, 'examples': STRINGS,
                               'applicability': STRING, 'approvalActor': STRING, 'approvalEvidence': STRING},
                              ['action', 'expectedRevision', 'reason']),
                      'plan-write', lambda root, a: preview_voice_profile(root, **a),
                      ('source', 'voice-profile-preview'), category='author'),
    ])
    from .knowledge_router import route_knowledge, build_knowledge_pack
    result.append(OperationSpec('source.knowledge-route', 'route_source_knowledge',
                                '按写作意图检索已整理的写作资料；不加载原始作品。',
                                _schema({'planningIntent': STRING, 'limit': INTEGER, 'station': STRING, 'mode': knowledge_mode_schema}, ['planningIntent']),
                                'read', lambda root, a: route_knowledge(**{**a, 'mode': resolve_knowledge_mode(a.get('mode'), project_root=root)}),
                                ('source', 'knowledge-route'), category='author'))
    result.append(OperationSpec('knowledge.pack', 'build_knowledge_pack',
                                '按工位读取已选写作资料切片，附来源和版本。',
                                _schema({'planningIntent': STRING, 'station': STRING, 'limit': INTEGER, 'mode': knowledge_mode_schema}, ['planningIntent', 'station']),
                                'read', lambda root, a: build_knowledge_pack(**{**a, 'mode': resolve_knowledge_mode(a.get('mode'), project_root=root)}),
                                ('knowledge', 'pack'), category='author',
                                examples=({'planningIntent': '场景中的权力反转', 'station': 'writer'},)))
    from .knowledge_store import (list_assets, read_asset, save_asset, create_asset,
                                  knowledge_health, list_asset_versions, read_asset_version, restore_asset)
    from .knowledge_index import rebuild_index
    result.extend([
        OperationSpec('knowledge.catalog', 'query_knowledge_catalog', '查看写作资料目录。',
                      _schema({'status': STRING, 'station': STRING, 'kind': STRING}),
                      'read', lambda root, a: list_assets(**a), ('knowledge', 'catalog'), category='author'),
        OperationSpec('knowledge.read', 'read_knowledge_asset', '读取一份有版本的写作资料。',
                      _schema({'assetId': STRING, 'expectedHash': STRING}, ['assetId']),
                      'read', lambda root, a: read_asset(a['assetId'], expected_hash=a.get('expectedHash')),
                      ('knowledge', 'read'), category='author'),
        OperationSpec('knowledge.versions', 'list_knowledge_versions', '查看写作资料可恢复的历史版本。',
                      _schema({'assetId': STRING}, ['assetId']),
                      'read', lambda root, a: list_asset_versions(a['assetId']),
                      ('knowledge', 'versions'), category='author'),
        OperationSpec('knowledge.version-read', 'read_knowledge_version', '预览写作资料历史版本。',
                      _schema({'assetId': STRING, 'versionHash': STRING}, ['assetId', 'versionHash']),
                      'read', lambda root, a: read_asset_version(a['assetId'], a['versionHash']),
                      ('knowledge', 'version-read'), category='author'),
        OperationSpec('knowledge.save', 'save_knowledge_asset', '按当前版本保存已存在的写作资料。',
                      _schema({'assetId': STRING, 'metadata': {'type': 'object'}, 'body': STRING, 'expectedHash': STRING},
                              ['assetId', 'metadata', 'body', 'expectedHash']),
                      'protected-write', lambda root, a: save_asset(asset_id=a['assetId'], metadata=a['metadata'],
                                                                    body=a['body'], expected_hash=a['expectedHash']),
                      ('knowledge', 'save'), category='author'),
        OperationSpec('knowledge.restore', 'restore_knowledge_asset', '按当前版本哈希恢复写作资料历史版本。',
                      _schema({'assetId': STRING, 'versionHash': STRING, 'expectedHash': STRING},
                              ['assetId', 'versionHash', 'expectedHash']),
                      'protected-write', lambda root, a: restore_asset(asset_id=a['assetId'],
                                                                       version_hash=a['versionHash'],
                                                                       expected_hash=a['expectedHash']),
                      ('knowledge', 'restore'), category='author'),
        OperationSpec('knowledge.create', 'create_knowledge_asset', '创建一份待审写作资料。',
                      _schema({'title': STRING, 'kind': STRING, 'stations': STRINGS, 'intents': STRINGS,
                               'tags': STRINGS, 'body': STRING, 'applicability': STRING,
                               'exclusions': STRING, 'license': STRING},
                              ['title', 'kind', 'stations', 'intents', 'tags', 'body']),
                      'protected-write', lambda root, a: create_asset(**a),
                      ('knowledge', 'create'), category='author'),
        OperationSpec('knowledge.health', 'query_knowledge_health', '检查写作资料目录。',
                      _schema({}), 'read', lambda root, a: knowledge_health(),
                      ('knowledge', 'health'), category='author'),
        OperationSpec('knowledge.index-rebuild', 'rebuild_knowledge_index', '重建写作资料的本地搜索索引。',
                      _schema({}), 'derived-write', lambda root, a: rebuild_index(),
                      ('knowledge', 'index-rebuild'), category='author'),
    ])
    from .source_query import query_source, read_fragment
    from .source_render import render_source, export_source
    from .source_model import source_schema
    result.append(OperationSpec('source.schema', 'get_source_schema', '读取单一真源的真实字段、ID、绑定和时间契约。',
                                _schema({}), 'read', lambda root, a: source_schema(), ('source', 'schema'), category='author', examples=({},)))
    result.append(OperationSpec(
        'source.query', 'query_source', '按对象、属性、片段身份和故事时间有界查询单一真源；旧项目需明确迁移。',
        _schema({'selection': source_selection(), 'presetId': STRING,
                 'view': {'type': 'string', 'enum': ['overview', 'list', 'detail']},
                 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
        'read', lambda root, a: query_source(root, **a), ('source', 'query'), category='author',
        audiences=station_audiences, tasks=station_tasks,
        examples=({}, {'selection': {'ownerTypes': ['character']}, 'view': 'detail'})))
    from .source_changes import preview_source_change
    from .pair_classification import query_pairs
    from .project import pair_policy
    result.append(OperationSpec(
        'source.pairs', 'query_source_pairs', '分页读取配对分类、人物端点、适用范围与评估状态；未知不冒充不适用。',
        _schema({'pairId': STRING, 'includeArchived': BOOLEAN, 'limit': INTEGER,
                 'maxBytes': INTEGER, 'cursor': STRING}),
        'read', lambda root, a: query_pairs(root, **a), ('source', 'pairs'), category='author', discover=True,
        audiences=station_audiences, tasks=station_tasks, examples=({},)))
    # The common schema validates each patch; this branch only adds bindings.
    explicit_change_bindings = {'oneOf': [
        {'properties': {'action': branch['properties']['action']}, 'required': branch['required']}
        for branch in source_change()['oneOf']
    ]}
    result.append(OperationSpec(
        'source.change-preview', 'preview_source_change', '预检内容、配对分类与绑定；规划可用局部 patch。pairAssessment 声明配对库存评估；resolutionRef 交接 revision/hash，均不授予批准。',
        _schema({'changes': {'type': 'array', 'items': source_change(require_patch_hash=False)}, 'requestId': STRING,
                 'baseRevision': INTEGER, 'reason': STRING, 'maintenanceReference': MAINTENANCE_REFERENCE,
                 'pairAssessment': {'type': 'object', 'properties': {
                     'status': {'type': 'string', 'enum': pair_policy()['assessmentStates']}, 'reason': STRING},
                     'required': ['status'], 'additionalProperties': False},
                 'resolutionRef': {**STRING, 'description': '当前 content-resolve-batch 的规划引用；仅用于其中已解析的规划补丁，可省略 baseRevision 和逐项 expectedRecordHash。'}}, ['changes', 'requestId', 'reason'])
        | {'anyOf': [{'required': ['resolutionRef']}, {'required': ['baseRevision'],
                   'properties': {'changes': {'items': explicit_change_bindings}}}]},
        'plan-write', lambda root, a: preview_source_change(root, **a), ('source', 'change-preview'), category='author', discover=True,
        examples=({'baseRevision': 1, 'requestId': 'planning-1', 'reason': '已确认的规划修订',
                   'changes': [{'action': 'patch', 'collection': 'fragments', 'id': 'SF-EXAMPLE',
                                'expectedRecordHash': '<recordHash>', 'patch': [{'op': 'text', 'path': ['text'],
                                'original': '旧文', 'replacement': '新文'}]}]},)))
    result.extend([
        OperationSpec('source.read-fragment', 'read_source_fragment', '按字符范围读取长片段；续读绑定源 hash，字节预算内不截断字符。',
                      _schema({'fragmentId': STRING, 'offset': INTEGER, 'maxBytes': INTEGER, 'sourceHash': STRING}, ['fragmentId']),
                      'read', lambda root, a: read_fragment(root, **a), ('source', 'read-fragment'), category='author',
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('source.render', 'render_source_reading',
                      '预览 single-source 设定与规划的 Markdown 阅读文本，不写文件；超响应预算时改用 source.export。成品正文用 project.export。',
                      _schema({'selection': source_selection(),
                               'presetId': {'type': 'string', 'description': '已保存的阅读预设 ID；不与 selection 同用。'},
                               'requireReviewed': {**BOOLEAN, 'description': '拒绝未审、过期或未知内容；省略沿用预设，未用预设时默认不要求。'},
                               'maxBytes': {**INTEGER, 'description': '响应的实际 UTF-8 JSON 字节预算；默认使用 query 注册表。'}}),
                      'read', lambda root, a: render_source(root, **a), ('source', 'render'), category='author',
                      audiences=('author', 'router'),
                      examples=({}, {'selection': {'ownerTypes': ['character']}})),
        OperationSpec('source.export', 'export_source_reading',
                      '保存 single-source 全部或筛选后的设定与规划为 Markdown 阅读快照，不做摘要；不是成品正文或项目备份。正文导出用 project.export。',
                      _schema({'selection': source_selection(),
                               'presetId': {'type': 'string', 'description': '已保存的阅读预设 ID；不与 selection 同用。'},
                               'output': {'type': 'string', 'description': '必填目标；项目内须在工作台/，项目外须显式绝对路径。不默认覆盖已有不同内容。'},
                               'requireReviewed': {**BOOLEAN, 'description': '拒绝未审、过期或未知内容；省略沿用预设，未用预设时默认不要求。'},
                               'overwrite': {**BOOLEAN, 'description': '默认不覆盖；覆盖已有不同内容须与 expectedFileHash 同时提供。'},
                               'expectedFileHash': {'type': 'string', 'description': '覆盖前已核对目标文件的 SHA-256；变动时拒绝，不能替代 overwrite。'}}, ['output']),
                      'export-write', lambda root, a: export_source(root, **a), ('source', 'export'), category='author',
                      audiences=('author', 'router'),
                      examples=({'output': '工作台/设定与规划.md'},
                                {'output': '工作台/含归档阅读.md', 'selection': {'includeArchived': True}})),
    ])
    from .source_review import query_reviews, submit_review, query_receipt, DECISIONS
    from .source_review_triage import VIEWS as TRIAGE_VIEWS, triage_reviews
    from .source_evidence import register_evidence, query_evidence, KINDS
    from .source_rule_issues import (submit_rule_issue, query_rule_issues, query_rule_issue_receipt,
                                     STATUSES as ISSUE_STATUSES)
    from .source_reports import query_change_report
    from .source_presets import save_preset, query_presets
    from .source_candidate_scan import scan_prose_candidates
    from .source_scan_report import scan_report
    from .source_links import query_source_links
    quote_schema = {'type': 'object', 'properties': {'start': INTEGER, 'end': INTEGER, 'text': STRING},
                    'required': ['start', 'end', 'text'], 'additionalProperties': False}
    review_schema = {'type': 'object', 'properties': {
        'recordId': STRING, 'inputHash': STRING, 'contentHash': STRING,
        'decision': {'type': 'string', 'enum': sorted(DECISIONS)},
        'reason': STRING, 'evidence': {'type': 'array', 'items': quote_schema}},
        'required': ['recordId', 'inputHash', 'contentHash', 'decision', 'reason', 'evidence'], 'additionalProperties': False}
    citation_schema = {'type': 'object', 'properties': {**quote_schema['properties'], 'targetIds': STRINGS,
        'kind': {'type': 'string', 'enum': sorted(KINDS)}, 'reason': STRING},
        'required': ['start', 'end', 'text', 'targetIds', 'kind', 'reason'], 'additionalProperties': False}
    rule_evidence_schema = {'type': 'object'}
    result.extend([
        OperationSpec('source.preset-save', 'save_source_preset', '保存可复用的筛选与排版参数；预设不复制任何小说内容。',
                      _schema({'requestId': STRING, 'name': STRING, 'selection': source_selection(),
                               'presetId': STRING, 'requireReviewed': BOOLEAN},
                              ['requestId', 'name', 'selection']),
                      'runtime-write', lambda root, a: save_preset(root, **a),
                      ('source', 'preset-save'), category='author'),
        OperationSpec('source.presets', 'query_source_presets', '读取筛选预设；返回参数，不返回缓存的小说正文。',
                      _schema({'presetId': STRING}), 'read', lambda root, a: query_presets(root, **a),
                      ('source', 'presets'), category='author'),
        OperationSpec('source.candidate-scan', 'scan_source_prose_candidates',
                      '在明确章节范围内查找字面候选并返回当前稿件定位；命中不等于依赖或规则冲突。',
                      _schema({'terms': STRINGS, 'chapterIds': STRINGS, 'targetIds': STRINGS,
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['terms']),
                      'read', lambda root, a: scan_prose_candidates(root, **a),
                      ('source', 'candidate-scan'), category='author'),
        OperationSpec('source.scan-report', 'report_source_prose_scan',
                      '按词或项目禁用词表扫描正文或设定/规划片段；source 返回身份、状态、证据资格和分页定位，命中由作者判断。',
                      _schema({'terms': STRINGS, 'chapterIds': STRINGS, 'targetIds': STRINGS,
                               'scope': {'type': 'string', 'enum': ['prose', 'source']}, 'selection': source_selection(),
                               'termsPath': STRING, 'termsFormat': {'type': 'string', 'enum': ['json', 'lines', 'quoted']},
                               'startLine': INTEGER, 'endLine': INTEGER, 'limit': INTEGER, 'cursor': STRING,
                               'maxBytes': INTEGER}),
                      'read', lambda root, a: scan_report(root, **a),
                      ('source', 'scan-report'), category='author',
                      examples=({'scope': 'source', 'termsPath': '工作台/禁用词.json'},)),
        OperationSpec('source.links', 'query_source_links',
                      '有界遍历明确登记的属性、依赖、章节、时间、规则和实体关系；返回深度边界与续查入口。',
                      _schema({'recordId': STRING, 'direction': {'type': 'string', 'enum': ['in', 'out', 'both']},
                               'edgeKinds': STRINGS, 'depth': INTEGER, 'limit': INTEGER,
                               'maxBytes': INTEGER, 'cursor': STRING}, ['recordId']),
                      'read', lambda root, a: query_source_links(root, **a),
                      ('source', 'links'), category='author'),
        OperationSpec('source.graph', 'query_source_graph',
                      '有界读取已登记事实关系或作者规划关系，显式区分图的认识地位与覆盖边界。',
                      _schema({'recordId': STRING, 'graph': {'type': 'string', 'enum': ['canon', 'planning']},
                               'direction': {'type': 'string', 'enum': ['in', 'out', 'both']},
                               'edgeKinds': STRINGS, 'depth': INTEGER, 'limit': INTEGER,
                               'maxBytes': INTEGER, 'cursor': STRING}, ['recordId', 'graph']),
                      'read', lambda root, a: query_source_links(root, **a),
                      ('source', 'graph'), category='author'),
        OperationSpec('source.change-report', 'query_source_change_report', '分页读取逐处可读 diff、影响、写集及新 ID 映射；历史结果不冒充当前覆盖。',
                      _schema({'operationId': STRING, 'undoRevision': INTEGER, 'section': {'type': 'string', 'enum': ['impact', 'writes', 'identities', 'rationale', 'maintenance', 'diff']},
                               'view': {'type': 'string', 'enum': ['records', 'text', 'markdown'],
                                        'description': '默认 records 返回完整记录；text 按原 JSON 字符分页；diff 支持 markdown 可读文本分页。'},
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['operationId']),
                      'read', lambda root, a: query_change_report(root, **a), ('source', 'change-report'), category='author',
                      examples=({'operationId': 'OP-EXAMPLE', 'section': 'rationale', 'view': 'text'},)),
        OperationSpec('source.reviews', 'query_source_reviews', '分页读取当前待复核片段、输入指纹和定位；不把工具取回材料视为已审校。',
                      _schema({'ownerIds': STRINGS, 'recordIds': STRINGS, 'statuses': STRINGS, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: query_reviews(root, **a), ('source', 'reviews'), category='author'),
        OperationSpec('source.review-triage', 'triage_source_reviews',
                      '按来源操作聚类待处理复核并分层（直接修改/登记依赖/无来源归属）；只改变呈现顺序，逐条复核义务不变。',
                      _schema({'view': {'type': 'string', 'enum': list(TRIAGE_VIEWS),
                                        'description': '默认 items 逐条分页待处理记录；clusters 按字节预算分页来源操作簇（含章节与所有者清单）。summary 只给计数。'},
                               'statuses': STRINGS, 'ownerIds': STRINGS, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: triage_reviews(root, **a), ('source', 'review-triage'), category='author',
                      examples=({'view': 'clusters'},)),
        OperationSpec('source.review-receipt', 'query_source_review_receipt', '读取历史语义回执、原因和引文，并逐项核对当前输入是否仍一致。',
                      _schema({'receiptId': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['receiptId']),
                      'read', lambda root, a: query_receipt(root, **a), ('source', 'review-receipt'), category='author'),
        OperationSpec('source.review-submit', 'submit_source_review', '登记逐片段语义回执，不改设定或正文。无需手算 hash 和偏移：先用 evidence prepare 读材料，再用 evidence pack 装配、evidence submit 登记；此入口保留完整参数兼容。',
                      _schema({'requestId': STRING, 'reviews': {'type': 'array', 'items': review_schema}, 'actor': STRING}, ['requestId', 'reviews']),
                      'runtime-write', lambda root, a: submit_review(root, **a), ('source', 'review-submit'), category='author'),
        OperationSpec('source.evidence-register', 'register_source_prose_evidence', '登记已阅读稿件的明确引用；绑定章 ID、源 hash、稿件 hash 与字符范围。',
                      _schema({'chapterId': STRING, 'fileHash': STRING, 'sourceHash': STRING, 'requestId': STRING,
                               'citations': {'type': 'array', 'items': citation_schema}, 'actor': STRING},
                              ['chapterId', 'fileHash', 'sourceHash', 'requestId', 'citations']),
                      'runtime-write', lambda root, a: register_evidence(root, **a), ('source', 'evidence-register'), category='author'),
        OperationSpec('source.evidence', 'query_source_prose_evidence', '有界查询正文引用证据；稿件改动后旧定位自动失效，提及与依赖分别返回。',
                      _schema({'chapterId': STRING, 'targetId': STRING, 'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: query_evidence(root, **a), ('source', 'evidence'), category='author'),
        OperationSpec('source.rule-issue-submit', 'submit_source_rule_issue', '登记语义规则的问题结论；绑定规则版本、输入摘要、章节动作和当前证据。',
                      _schema({'requestId': STRING, 'ruleId': STRING, 'chapterId': STRING,
                               'action': {'type': 'string', 'enum': list(RULE_ACTIONS)}, 'inputHash': STRING,
                               'status': {'type': 'string', 'enum': sorted(ISSUE_STATUSES)}, 'reason': STRING,
                               'evidence': {'type': 'array', 'items': rule_evidence_schema},
                               'affectedIds': STRINGS, 'sourceOperationId': STRING,
                               'deferredUntilScopeIds': STRINGS, 'actor': STRING},
                              ['requestId', 'ruleId', 'chapterId', 'action', 'inputHash', 'status',
                               'reason', 'evidence', 'affectedIds']),
                      'runtime-write', lambda root, a: submit_rule_issue(root, **a),
                      ('source', 'rule-issue-submit'), category='author'),
        OperationSpec('source.rule-issues', 'query_source_rule_issues', '分页读取语义规则问题及其当前有效性；过期回执保留为历史。',
                      _schema({'chapterId': STRING, 'ruleId': STRING, 'statuses': STRINGS,
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: query_rule_issues(root, **a),
                      ('source', 'rule-issues'), category='author'),
        OperationSpec('source.rule-issue-receipt', 'query_source_rule_issue_receipt', '读取语义规则问题的历史回执，并核对规则输入与证据是否仍有效。',
                      _schema({'receiptId': STRING, 'limit': INTEGER, 'maxBytes': INTEGER,
                               'cursor': STRING}, ['receiptId']),
                      'read', lambda root, a: query_rule_issue_receipt(root, **a),
                      ('source', 'rule-issue-receipt'), category='author'),
    ])
    for dimension in DIMENSIONS:
        result.append(OperationSpec('query.' + dimension, 'query_' + dimension,
                                   '有界查询 ' + dimension + '：默认概览；selector 为精确匹配，query 为关键词。',
                                   QUERY_SCHEMA, 'read', lambda root, a, d=dimension: query_project(root, d, **a), ('query', dimension), category='author'))
    def catalog(root, a):
        from .tool_catalog import list_capabilities
        return list_capabilities(**a)
    def _invoke_discovered(root, arguments, effects):
        from .tool_catalog import invoke_discovered
        return invoke_discovered(root, **arguments, effects=effects)
    def plan(root, a):
        from .tool_plan import build_tool_plan
        return build_tool_plan(root, a['task'], chapter_id=a.get('chapterId'))
    def execute(root, a):
        from .tool_plan import execute_task_actions
        return execute_task_actions(root, a['task'], chapter_id=a.get('chapterId'), authorization=a.get('authorization', 'read'),
                                    pov_character=a.get('povCharacter'), chapter_goal=a.get('chapterGoal') or '',
                                    knowledge_mode=a.get('knowledgeMode'), diagnostics=a.get('diagnostics'),
                                    held_prepare_ref=a.get('heldPrepareRef'))
    def diagnose(root, a):
        from .mechanical_evidence import run_diagnostics
        return run_diagnostics(root, chapter_id=a['chapterId'], mode=a.get('mode'), intensity_level=a.get('intensityLevel'),
                               scene_paragraphs=a.get('sceneParagraphs') or '', strict_floor=bool(a.get('strictFloor')),
                               register=bool(a.get('register')), advance_lifecycle=bool(a.get('advanceLifecycle')),
                               view=a.get('view', 'model'))
    def doctor(root, a):
        from .doctor_delivery import doctor_report
        report = doctor_report(root, **a)
        # 总健康是兼容投影；真正的不健康信号是「有 blocking 域」。这里把它变成同一个
        # 错误码，CLI 与 MCP 因此拿到一致的 code 与 effects 分类。
        if not report['healthy']:
            raise StructureError('DOCTOR_UNHEALTHY', 'project doctor reported blocking errors', report)
        return report
    def station_cards(root, a):
        from .tool_cards import apply_station_cards, check_station_cards
        return apply_station_cards() if a.get('apply') else check_station_cards()
    def prepare(root, a):
        from .task_prepare import prepare_task
        report = prepare_task(root, a['task'], chapter_id=a.get('chapterId'), pov_character=a.get('povCharacter'),
                            chapter_goal=a.get('chapterGoal') or '', knowledge_mode=a.get('knowledgeMode'), entities=None,
                            held_prepare_ref=a.get('heldPrepareRef'), batch_ref=a.get('batchRef'))
        if a.get('view') == 'model':
            from .context_transport import task_model_response
            return task_model_response(report)
        return report
    def read_context(root, a):
        from .context_builder import build_station_context
        payload = build_station_context(str(root), a['chapterId'],
                                        task_type=a.get('taskType') or 'writer',
                                        pov_character=a.get('povCharacter'),
                                        chapter_goal=a.get('chapterGoal') or '', knowledge_mode=a.get('knowledgeMode'), view='station', read_only=True)
        if a.get('view') == 'model':
            from .context_transport import model_response
            return model_response(payload)
        return {'ok': True, 'code': 'CONTINUATION_CONTEXT_READ_ONLY', **payload}
    def summary_prepare(root, a):
        import importlib
        module = importlib.import_module('scripts.story_summarizer' if __package__.startswith('scripts.') else 'story_summarizer')
        prep = module._prepare_context(root, view='model')
        return {'ok': True, 'code': 'SUMMARY_UP_TO_DATE' if prep.get('upToDate') else 'SUMMARY_CONTEXT', **prep}
    def summary_commit(root, a):
        import importlib
        module = importlib.import_module('scripts.story_summarizer' if __package__.startswith('scripts.') else 'story_summarizer')
        return {'ok': True, 'code': 'SUMMARY_COMMITTED', **module._commit_summary(root, a['input'])}
    from .summary_updates import commit_delta, read_summary, export_summary
    from .operation_schemas import summary_entry_updates
    task_schema = {'task': STRING, 'chapterId': STRING, 'povCharacter': STRING, 'chapterGoal': STRING}
    from .artifact_pack import PACKABLE_TYPES, pack_artifact
    from .current_candidate import prepare_candidate_handoff, selection_review_schema
    from .candidate_batch import (prepare_candidate_batch, prepare_candidate_recipe, batch_progress,
                                 candidate_reading, list_candidate_batches)
    from .manuscript_export import export_candidate_batch
    from .writing_checks import rules_card, check_paragraph, chapter_check
    diagnostic_inputs = {**DIAGNOSTICS['properties'], 'register': BOOLEAN, 'advanceLifecycle': BOOLEAN}
    from .argument_validation import preflight_arguments
    result.extend([
        OperationSpec('workflow.candidate-batch', 'prepare_candidate_batch',
                      '冻结既有授权范围内明确的整批候选委托；不批准正文、不改变正式槽。原requestId可查询原范围。',
                      _schema({'requestId': STRING, 'chapterIds': STRINGS}, ['requestId']),
                      'candidate-write', lambda root, a: prepare_candidate_batch(root, **a), ('workflow', 'candidate-batch'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.candidate-recipe', 'prepare_candidate_recipe',
                      '从Planner准备阶段冻结实际前章候选、A/B和条件交接输入；阶段A必须继承同一recipe。',
                      _schema({'batchRef': {'type': 'object'}, 'chapterId': STRING}, ['batchRef', 'chapterId']),
                      'read', lambda root, a: prepare_candidate_recipe(root, **a), ('workflow', 'candidate-recipe'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.candidate-progress', 'read_candidate_batch_progress',
                      '从已有工件推导整批候选的可读成果、未完成原因与续办位置；不生成文学通过。',
                      _schema({'batchRef': {'type': 'object'}}, ['batchRef']),
                      'read', lambda root, a: batch_progress(root, **a), ('workflow', 'candidate-progress'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.candidate-reading', 'read_candidate_batch',
                      '冻结整批候选原文、身份和随稿建议；明确部分交付与历史审校，阅读不批准。',
                      _schema({'batchRef': {'type': 'object'}}, ['batchRef']), 'read',
                      lambda root, a: candidate_reading(root, **a), ('workflow', 'candidate-reading'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.candidate-batches', 'list_candidate_batches',
                      '读取已有候选委托范围与原请求引用，不创建委托或批准。',
                      _schema({}), 'read', lambda root, a: list_candidate_batches(root), ('workflow', 'candidate-batches'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.candidate-export', 'export_candidate_batch',
                      '导出冻结候选合订阅读稿，保留身份、建议、部分交付和续办位置；不改变正式正文导出。',
                      _schema({'batchRef': {'type': 'object'}, 'output': STRING, 'fmt': {'type': 'string', 'enum': ['md', 'txt']},
                               'overwrite': BOOLEAN, 'expectedFileHash': STRING}, ['batchRef', 'output']),
                      'export-write', lambda root, a: export_candidate_batch(root, **a), ('workflow', 'candidate-export'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.candidate-handoff', 'prepare_candidate_handoff',
                      '冻结已激活候选的身份、正文、计划、阶段A和旧写作说明，供实际核验后转交；不自动登记或批准。',
                      _schema({'chapterId': STRING}, ['chapterId']), 'read',
                      lambda root, a: prepare_candidate_handoff(root, **a), ('workflow', 'candidate-handoff'),
                      audiences=station_audiences, tasks=station_tasks),
        OperationSpec('workflow.artifact-pack', 'pack_workflow_artifact',
                      '直接传 text 正文或 payload 审校内容，工具保存并登记；也可读 input 文件。writingNotes 记录明确偏离，新增事实走源事务；advanceLifecycle+diagnostics 推进到 review，不批准。',
                      _schema({'chapterId': STRING, 'artifactType': {'type': 'string', 'enum': list(PACKABLE_TYPES)},
                               'input': STRING, 'text': STRING, 'payload': {'type': 'object'},
                               'writingNotes': {'type': 'object', 'description': '候选正文的写作说明；与 notes 文件二选一，显式空对象表示撤回旧说明。'},
                               'selectionRef': STRING, 'selectionReview': selection_review_schema(), 'recipeRef': STRING,
                               'requestId': STRING, 'advanceLifecycle': BOOLEAN,
                               'automaticRepair': {**BOOLEAN, 'description': '明确本次为自动返修；须 requestId，在新候选登记事务中计一轮，重试不重复。'},
                               'notes': {**STRING, 'description': '兼容工作台 JSON 文件路径；直接传对象时改用 writingNotes。'},
                               'diagnostics': DIAGNOSTICS}, ['chapterId', 'artifactType']) |
                      {'oneOf': [{'required': ['input']}, {'required': ['text']}, {'required': ['payload']}]},
                      'candidate-write', lambda root, a: pack_artifact(root, **a), ('workflow', 'artifact-pack'),
                      audiences=station_audiences, tasks=station_tasks,
                      examples=({'chapterId': 'CH001', 'artifactType': 'candidate_prose', 'input': '工作台/正文.md',
                                 'advanceLifecycle': True, 'diagnostics': {}},)),
        OperationSpec('diagnostics.rules', 'query_diagnostic_rules', '按模式和张力等级列出实际生效的检查、阈值、严重度及语义盲区。',
                      _schema({'mode': DIAGNOSTICS['properties']['mode'], 'intensityLevel': INTEGER, 'strictFloor': BOOLEAN}, ['mode', 'intensityLevel']),
                      'read', lambda root, a: rules_card(**a), ('diagnostics', 'rules'),
                      audiences=station_audiences, tasks=station_tasks, examples=({'mode': 'buildUp', 'intensityLevel': 1},)),
        OperationSpec('diagnostics.paragraph', 'check_writing_paragraph', '写一段查一段：句长、同主语、AI 句式和越界词；不生成整章通过证据。',
                      _schema({'text': STRING, 'mode': DIAGNOSTICS['properties']['mode'], 'intensityLevel': INTEGER}, ['text']),
                      'read', lambda root, a: check_paragraph(**a), ('diagnostics', 'paragraph'),
                      audiences=station_audiences, tasks=station_tasks, examples=({'text': '值班员放下杯子，走到门前。', 'mode': 'buildUp', 'intensityLevel': 1},)),
        OperationSpec('diagnostics.chapter-check', 'check_chapter_contract', '只读核对正文汉字数、节奏段预算、必须落地原句和 POV 待审项；input 省略时读取当前候选。',
                      _schema({'chapterId': STRING, 'input': STRING,
                               'responseView': {'type': 'string', 'enum': ['model', 'full'],
                                                'description': '默认仅交付问题与未检查项；full 返回本次完整检查结果，不改变检查范围。'}}, ['chapterId']),
                      'read', lambda root, a: chapter_check(root, **a), ('diagnostics', 'chapter-check'),
                      audiences=station_audiences, tasks=station_tasks, examples=({'chapterId': 'CH001', 'input': '工作台/正文.md'},)),
        OperationSpec('capabilities.list', 'list_capabilities',
                      '不知道工具名时用 query 描述意图；返回少量操作、参数、示例及可调用入口。已知名字用 operation 精确查询，不必展开全库。',
                      _schema({'audience': STRING, 'category': STRING, 'task': STRING,
                               'query': {'type': 'string', 'description': '常用任务描述，如导出全部设定、查看人物资料；不与 operation 或 audit 同用。'},
                               'operation': {'type': 'string', 'description': '精确 operation ID，如 source.export；返回完整参数定义。'},
                               'view': {'type': 'string', 'enum': ['model', 'audit'],
                                        'description': '默认 model；audit 是完整审计库存，不作为日常查找的首选。'}}),
                      'read', catalog, ('tools', 'list'), discover=True,
                      examples=({'query': '导出全部设定'}, {'operation': 'source.export'})),
        OperationSpec('capabilities.validate', 'preflight_operation_arguments',
                      '按真实 OperationSpec 汇总参数结构错误并分页；不调用目标操作，不校验当前事实、授权或语义。schemaValid 不等于可执行。',
                      _schema({'operation': STRING, 'arguments': {'type': 'object'}, 'validationRef': STRING,
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}, ['operation']) |
                      {'oneOf': [{'required': ['arguments']}, {'required': ['validationRef']}]},
                      'read', lambda root, a: preflight_arguments(root, **a), ('tools', 'validate'),
                      examples=({'operation': 'source.fact-patch-preview', 'arguments': {'patches': []}},)),
        OperationSpec('capabilities.read', 'read_discovered_operation',
                      '调用目录找到的只读 MCP 操作；原参数校验、POV选择和项目边界不变。不能调用写入、审批、维护或本入口。',
                      _schema({'operation': {'type': 'string', 'description': '目录返回的 effects=read 操作 ID。'},
                               'arguments': {'type': 'object', 'description': '该操作的完整参数；projectRoot只在外层指定。'}},
                              ['projectRoot', 'operation', 'arguments']),
                      'read', lambda root, a: _invoke_discovered(root, a, 'read'),
                      ('tools', 'read'), discover=True),
        OperationSpec('capabilities.export', 'export_discovered_operation',
                      '显式调用目录找到的文件导出操作，写入阅读快照或成品稿；不修改作品真源，不批准。仍执行原路径和覆盖检查。',
                      _schema({'operation': {'type': 'string', 'description': '目录返回的 effects=export-write 操作 ID。'},
                               'arguments': {'type': 'object', 'description': '按目录定义提供导出参数；projectRoot只在外层指定。'}},
                              ['projectRoot', 'operation', 'arguments']),
                      'export-write', lambda root, a: _invoke_discovered(root, a, 'export-write'),
                      ('tools', 'export'), discover=True),
        OperationSpec('project.doctor', 'doctor',
                      '分域聚合项目健康：project / migration / planning / writerReadiness / canon / '
                      'context / knowledge / workflow / tooling 各自独立给状态，每个问题带 code、severity、'
                      'status、证据、受影响 operation 与建议动作。总 healthy 只是兼容投影。',
                      _schema({'strict': {**BOOLEAN, 'description': '警告也计入阻断：空模板、未定字段与时间线倒挂都会让检查失败。'},
                               'chapterId': {**STRING, 'description': '指定本章上下文与修订就绪度；缺省用 nextAction 的未完成章，再选最早未完成章，无待写任务时不适用。显式指定已定稿章仍检查当前审查绑定。'},
                               'view': {'type': 'string', 'enum': ['full', 'model'], 'description': 'CLI 默认 full 保留完整报告；MCP 默认 model，所有检查仍完整执行。'},
                               'againstRef': {**STRING, 'description': 'model 下与原 doctorRef 比较；不同作用域或策略不冒充可比。'},
                               'limit': INTEGER, 'maxBytes': INTEGER, 'cursor': STRING}),
                      'read', doctor, ('doctor',), category='project-maintenance', discover=True,
                      examples=({}, {'strict': True})),
        OperationSpec('capabilities.station-cards', 'station_capability_cards', '从能力目录生成/校验六个工位的条件卡片，杜绝六份手写同步。',
                      _schema({'apply': BOOLEAN}), 'maintenance', station_cards, ('tools', 'station-cards')),
        OperationSpec('task.tool-plan', 'get_tool_plan', '只读生成三值工具动作计划，不替作者批准或执行。',
                      _schema(task_schema, ['task']), 'read', plan, ('tools', 'plan'), discover=True),
        OperationSpec('task.execute', 'execute_task_actions', '执行当前任务计划中的就绪动作；证据登记须显式 candidate 授权，不批准任何工件。',
                      _schema({**task_schema, 'knowledgeMode': knowledge_mode_schema,
                               'heldPrepareRef': {**STRING, 'description': '明确仍持有的准备报告；登记后刷新仍与这份已交付基线比较，不继承中间报告。'},
                               'diagnostics': {'type': 'object', 'properties': diagnostic_inputs, 'additionalProperties': False},
                               'authorization': {'type': 'string', 'enum': ['read', 'candidate']}}, ['task', 'authorization']),
                      'candidate-write', execute, ('tools', 'execute'), audiences=station_audiences, tasks=station_tasks,
                      examples=({'task': 'editor', 'chapterId': 'CH-EXAMPLE', 'authorization': 'read'},)),
        OperationSpec('task.prepare', 'prepare_task', '只读任务准备清单：必读材料、就绪判断、缺项与工具动作计划。继续或写下一章用 task=continue，按当前状态路由，不自动建章或批准。',
                      _schema({**task_schema, 'knowledgeMode': knowledge_mode_schema, 'batchRef': {'type': 'object'},
                               'heldPrepareRef': {**STRING, 'description': '仅在仍持有该准备报告时回传；model 视图只交付变化部分，换窗或压缩后不传。'},
                               'view': {'type': 'string', 'enum': ['station', 'model']}}, ['task']),
                      'read', prepare, ('task', 'prepare'),
                      examples=({'task': 'continue', 'view': 'model'}, {'task': 'write', 'chapterId': 'CH-EXAMPLE', 'view': 'model'})),
        OperationSpec('context.read', 'read_continuation_context', '只读续写上下文：不重建缓存、不修复项目。',
                      _schema({'chapterId': STRING, 'povCharacter': STRING, 'chapterGoal': STRING, 'taskType': STRING, 'knowledgeMode': knowledge_mode_schema,
                               'view': {'type': 'string', 'enum': ['station', 'model']}}, ['chapterId']),
                      'read', read_context, ('task', 'context')),
        OperationSpec('summary.prepare', 'prepare_story_summary', '准备过期摘要的输入；不生成摘要内容。',
                      _schema({}), 'read', summary_prepare, ('task', 'summary-prepare')),
        OperationSpec('summary.commit', 'commit_story_summary', '提交工位生成的摘要 JSON；这是独立写操作，不是查询。',
                      _schema({'input': STRING}, ['input']), 'candidate-write', summary_commit, ('task', 'summary-commit'),
                      audiences=('router', 'planner'), tasks=('planner', 'writer', 'continuity')),
        OperationSpec('summary.commit-delta', 'commit_story_summary_delta',
                      '按稳定章节与角色 ID 新增、替换或明确删除摘要项；绑定准备与旧摘要版本，不生成语义结论。',
                      _schema({'prepareRef': STRING, 'storySummary': STRING,
                               'plotLine': summary_entry_updates('chapterId'),
                               'characterSummaries': summary_entry_updates('characterId'),
                               'reviewReason': {**STRING, 'description': '实际摘要复核理由，直接用于返回的连续性确认载荷；不授予批准。'}}, ['prepareRef', 'storySummary']),
                      'candidate-write', lambda root, a: commit_delta(root, **a), ('task', 'summary-delta'),
                      audiences=('router', 'planner'), tasks=('planner', 'writer', 'continuity'),
                      examples=({'prepareRef': '<summary-prepare.prepareRef>', 'storySummary': '经正文复核的完整因果摘要'},)),
        OperationSpec('summary.export', 'export_story_summary', '按需导出完整故事摘要阅读快照，不写设定、不授予批准。',
                      _schema({'output': {**STRING, 'description': '项目内须在工作台/；导出不维护、不反向导入。'},
                               'overwrite': BOOLEAN, 'expectedFileHash': STRING}, ['output']),
                      'export-write', lambda root, a: export_summary(root, **a), ('task', 'summary-export'),
                      examples=({'output': '工作台/故事摘要.md'},)),
        OperationSpec('summary.read', 'read_story_summary', '分页读取当前摘要、稳定 ID 条目或历史版本；不代表当前事实或批准。',
                      _schema({'version': STRING, 'view': {'type': 'string', 'enum': ['detail', 'history']},
                               'chapterId': STRING, 'characterId': STRING, 'maxBytes': INTEGER,
                               'limit': INTEGER, 'cursor': STRING}),
                      'read', lambda root, a: read_summary(root, **a), ('task', 'summary-read'),
                      examples=({}, {'view': 'history'})),
        OperationSpec('diagnostics.aggregate', 'run_mechanical_diagnostics', '对候选正文运行机械检查，证据绑定正文、设定、规则及实现；register 仅登记，不批准。',
                      _schema({'chapterId': STRING, **diagnostic_inputs, 'view': {'type': 'string', 'enum': ['full', 'model']}},
                              ['chapterId']),
                      'candidate-write', diagnose, ('diagnostics', 'run')),
    ])
    from .authoring_submission import submit_artifacts, submission_artifact_schema
    from .chapter_creation import preview_chapter
    result.extend([
        OperationSpec('workflow.submit', 'submit_chapter_artifacts',
                      '直接提交本章正文或成组审校内容；审校可交 formRef 与 edits，工具恢复冻结绑定并保存、登记。requestId 冻结输入，中断只传原 ID 续办；不批准或定稿。',
                      _schema({'requestId': STRING, 'chapterId': STRING,
                               'artifacts': {'type': 'array', 'minItems': 1, 'maxItems': len(PACKABLE_TYPES),
                                             'items': submission_artifact_schema()},
                               'advanceLifecycle': BOOLEAN, 'automaticRepair': BOOLEAN, 'diagnostics': DIAGNOSTICS}, ['requestId']) |
                      {'anyOf': [{'required': ['chapterId', 'artifacts']},
                                 {'properties': {key: False for key in ('chapterId', 'artifacts', 'advanceLifecycle', 'automaticRepair', 'diagnostics')}}]},
                      'candidate-write', lambda root, a: submit_artifacts(root, **a), ('workflow', 'submit'),
                      audiences=('router', 'planner', 'writer', 'editor', 'titler'), tasks=station_tasks,
                      examples=({'requestId': 'chapter-draft-1', 'chapterId': 'CH001',
                                 'artifacts': [{'artifactType': 'candidate_prose', 'text': '工位完成的整章正文。'}],
                                 'advanceLifecycle': True, 'diagnostics': {}}, {'requestId': 'chapter-draft-1'})),
        OperationSpec('workflow.chapter-preview', 'preview_chapter_with_plan',
                      '将新章和工位提供的完整推演合为一次可批准预览；自动分配章节/规划 ID。source 模式显式选 planField 和依赖；相同 requestId 不重复建章。',
                      _schema({'requestId': STRING, 'title': STRING, 'primaryArcId': STRING, 'outlineText': STRING,
                               'reason': STRING, 'volumeId': STRING, 'placement': {'type': 'object'},
                               'beforeChapterId': STRING, 'secondaryArcIds': STRINGS,
                               'planField': {**STRING, 'description': 'single-source 必填：已有 chapter topic 字段 ID 或 key，未知先查 source.schema。'},
                               'dependsOn': STRINGS, 'chapterContract': contract_schema(), 'baseRevision': INTEGER},
                              ['requestId', 'title', 'primaryArcId', 'outlineText', 'reason']) |
                      {'oneOf': [{'required': ['volumeId']}, {'required': ['placement']}]},
                      'plan-write', lambda root, a: preview_chapter(root, **a), ('workflow', 'chapter-preview'),
                      audiences=('router', 'planner'), tasks=('planner',),
                      examples=({'requestId': 'chapter-plan-1', 'title': '交班', 'volumeId': 'VOL-EXAMPLE',
                                 'primaryArcId': 'ARC-EXAMPLE', 'outlineText': '工位提供的本章推演。',
                                 'planField': 'plot.plan', 'dependsOn': [], 'reason': '作者选定的章节方案'},)),
    ])
    from .refactor import operation_contract, refactor_reference_candidates
    result.extend([
        OperationSpec('refactor.contract', 'query_refactor_contract',
                      '读取作者重构契约：支持的操作、各自的身份不变量、延期的 split/merge/fork 规则。',
                      _schema({}), 'read', lambda root, a: operation_contract(),
                      ('structure', 'refactor-contract'), category='author'),
        OperationSpec('refactor.reference-candidates', 'query_refactor_reference_candidates',
                      '只读报告旧显示名在 authored 文本中的出现位置；命中只是候选，脚本不改写正文。',
                      _schema({'oldName': STRING, 'paths': {'type': 'array', 'items': STRING}, 'limit': INTEGER},
                              ['oldName']),
                      'read', lambda root, a: refactor_reference_candidates(
                          root, **{('old_name' if key == 'oldName' else key): value for key, value in a.items()}),
                      ('structure', 'refactor-references'), category='author'),
    ])
    from .handoff_records import (
        create_handoff, handoff_result_status, read_handoffs, register_handoff_return, stop_handoff,
    )
    from .review_localization import note_issue, review_report_view
    from .view_adapters import project_summary, project_view
    from .candidate_versions import (
        activate_candidate_version, candidate_recovery_status,
        list_candidate_versions, read_candidate_version, save_candidate_version,
    )
    result.extend([
        # ---- 工作台视图（有界、placement 感知）----
        OperationSpec('workspace.summary', 'query_workspace_summary',
                      '有界首页概要：结构视图 + stat 键控字数缓存，轮询不扫全书正文。',
                      _schema({}), 'read', lambda root, a: project_summary(root),
                      ('workspace', 'summary'), category='author'),
        OperationSpec('workspace.structure-view', 'query_project_structure_view',
                      'schema 无关的章节视图（placement/derivedVolumeId/unit/弧线/ownership 摘要）。',
                      _schema({}), 'read', lambda root, a: project_view(root),
                      ('workspace', 'structure-view'), category='author'),
        # ---- candidate 版本协议（§23-§28）----
        OperationSpec('candidate.save-version', 'save_candidate_version',
                      '保存一个不可变工作台候选版本；requestId 幂等，基线过期拒绝。',
                      _schema({'chapterId': STRING, 'body': STRING, 'requestId': STRING,
                               'baseCandidateVersion': {'type': ['integer', 'null']}, 'source': STRING},
                              ['chapterId', 'body', 'requestId']),
                      'candidate-write',
                      lambda root, a: save_candidate_version(root, a['chapterId'], body=a['body'],
                                                             request_id=a['requestId'],
                                                             base_candidate_version=a.get('baseCandidateVersion'),
                                                             source=a.get('source') or 'cli'),
                      ('candidate', 'save-version'), category='author'),
        OperationSpec('candidate.activate-version', 'activate_candidate_version',
                      '显式激活候选版本并原子失效旧证据；expectedActiveToken 不符即拒绝。',
                      _schema({'chapterId': STRING, 'candidateVersion': INTEGER, 'expectedActiveToken': INTEGER},
                              ['chapterId', 'candidateVersion', 'expectedActiveToken']),
                      'candidate-write', lambda root, a: activate_candidate_version(root, a['chapterId'],
                                                                                    candidate_version=a['candidateVersion'],
                                                                                    expected_active_token=a['expectedActiveToken']),
                      ('candidate', 'activate-version'), category='author'),
        OperationSpec('candidate.list-versions', 'list_candidate_versions',
                      '读取章节候选版本清单与恢复状态。',
                      _schema({'chapterId': STRING}, ['chapterId']),
                      'read', lambda root, a: list_candidate_versions(root, a['chapterId']),
                      ('candidate', 'list-versions'), category='author'),
        OperationSpec('candidate.read-version', 'read_candidate_version',
                      '读取一个候选版本的正文；hash 校验失败 fail closed。',
                      _schema({'chapterId': STRING, 'version': INTEGER}, ['chapterId', 'version']),
                      'read', lambda root, a: read_candidate_version(root, a['chapterId'], a['version']),
                      ('candidate', 'read-version'), category='author'),
        OperationSpec('candidate.recovery-status', 'query_candidate_recovery',
                      '已保存未激活/激活中断的恢复视图；存在 pending 事务时 finalization 被阻塞。',
                      _schema({'chapterId': STRING}, ['chapterId']),
                      'read', lambda root, a: {'ok': True, 'chapterId': a['chapterId'],
                                               'recovery': candidate_recovery_status(root, a['chapterId'])},
                      ('candidate', 'recovery-status'), category='author'),
        # ---- 宿主交接往返（§35-§41）----
        OperationSpec('handoff.create', 'create_host_handoff',
                      '创建可追溯交接记录；已有未完成任务时拒绝（single-track）。',
                      _schema({'chapterId': STRING, 'task': STRING, 'station': STRING,
                               'producesCandidate': BOOLEAN, 'candidateVersion': {'type': ['integer', 'null']},
                               'candidateActiveToken': {'type': ['integer', 'null']}, 'contextHash': STRING,
                               'contextApplicable': BOOLEAN, 'promptPlanFingerprint': STRING,
                               'dependencyManifest': {'type': 'array'}, 'pov': STRING,
                               'effectiveKnowledgeMode': knowledge_mode_schema,
                               'knowledgeModeSource': {'type': 'string', 'enum': ['project_default', 'task_override', 'unknown']},
                               'inputScope': {'type': 'object'}, 'rerunOf': STRING},
                              ['chapterId', 'task', 'station']),
                      'runtime-write',
                      lambda root, a: create_handoff(root, chapter_id=a['chapterId'], task=a['task'], station=a['station'],
                                                     produces_candidate=a.get('producesCandidate', False),
                                                     candidate_version=a.get('candidateVersion'),
                                                     candidate_active_token=a.get('candidateActiveToken'),
                                                     context_hash=a.get('contextHash'),
                                                     context_applicable=a.get('contextApplicable', True),
                                                     prompt_plan_fingerprint=a.get('promptPlanFingerprint'),
                                                     dependency_manifest=a.get('dependencyManifest'),
                                                     pov=a.get('pov'),
                                                     effective_knowledge_mode=a.get('effectiveKnowledgeMode'),
                                                     knowledge_mode_source=a.get('knowledgeModeSource'),
                                                     input_scope=a.get('inputScope'), rerun_of=a.get('rerunOf')),
                      ('handoff', 'create'), category='author'),
        OperationSpec('handoff.read', 'read_host_handoffs',
                      '读取交接记录（全部/按章节/按 ID）。',
                      _schema({'chapterId': STRING, 'handoffId': STRING}),
                      'read',
                      lambda root, a: read_handoffs(root, chapter_id=a.get('chapterId'), handoff_id=a.get('handoffId')),
                      ('handoff', 'read'), category='author'),
        OperationSpec('handoff.stop', 'stop_host_handoff',
                      '作者显式停止一个未完成交接。',
                      _schema({'handoffId': STRING, 'reason': STRING}, ['handoffId']),
                      'runtime-write',
                      lambda root, a: stop_handoff(root, a['handoffId'], reason=a.get('reason', '')),
                      ('handoff', 'stop'), category='author'),
        OperationSpec('handoff.register-return', 'register_host_handoff_return',
                      '登记宿主返回：returnRequestId 幂等；迟到返回只追加不激活。',
                      _schema({'handoffId': STRING, 'returnRequestId': STRING, 'body': {'type': ['string', 'null']}},
                              ['handoffId', 'returnRequestId']),
                      'candidate-write',
                      lambda root, a: register_handoff_return(root, handoff_id=a['handoffId'],
                                                              return_request_id=a['returnRequestId'],
                                                              body=a.get('body')),
                      ('handoff', 'register-return'), category='author'),
        OperationSpec('handoff.result-status', 'query_host_handoff_status',
                      '查询交接状态；页面重开后可找回，不靠计时器推进。',
                      _schema({'handoffId': STRING}, ['handoffId']),
                      'read', lambda root, a: handoff_result_status(root, a['handoffId']),
                      ('handoff', 'result-status'), category='author'),
        # ---- 审校版本绑定与定位（§42-§45）----
        OperationSpec('review.report-view', 'view_review_report',
                      '核验已登记审校报告与当前稿件；历史建议保留，版本或计划不匹配时不在当前稿定位，也不开放问题笔记。',
                      _schema({'chapterId': STRING}, ['chapterId']),
                      'read',
                      lambda root, a: review_report_view(root, a['chapterId']),
                      ('review', 'report-view'), category='author'),
        OperationSpec('review.note-issue', 'note_review_issue',
                      '记录审校问题工作状态（已处理/待复核）；这是工作笔记，不是通过判定。',
                      _schema({'chapterId': STRING, 'reportSha256': STRING, 'findingIndex': INTEGER,
                               'state': {'type': 'string', 'enum': ['processed', 'pending_review']}, 'note': STRING},
                              ['chapterId', 'reportSha256', 'findingIndex', 'state']),
                      'runtime-write',
                      lambda root, a: note_issue(root, a['chapterId'], report_sha256=a['reportSha256'],
                                                 finding_index=a['findingIndex'], state=a['state'], note=a.get('note', '')),
                      ('review', 'note-issue'), category='author'),
    ])
    from .efficiency_operations import efficiency_operations
    result.extend(efficiency_operations())
    from .authoring_operations import authoring_operations
    result.extend(authoring_operations())
    from .call_log_operations import call_log_operations
    result.extend(call_log_operations())
    return result


def _json_argument(value: str):
    """Keep large, reviewable requests on disk instead of shell-escaped strings."""
    try:
        text = Path(value[1:]).read_text(encoding='utf-8') if value.startswith('@') else value
        if value.startswith('@'):
            from .call_logging import observe_file_input
            observe_file_input('json_argument', value[1:], text)
        return json.loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f'invalid JSON argument: {exc}') from exc


def add_arguments(parser, spec: OperationSpec) -> None:
    parser.set_defaults(operation_binding=spec.id)
    parser.description = spec.description
    if spec.examples:
        from .cli_examples import command_example
        parser.formatter_class = argparse.RawDescriptionHelpFormatter
        parser.epilog = 'CLI 参数示例：\n' + '\n'.join(command_example(spec, row) for row in spec.examples)
    for key, field in spec.input_schema['properties'].items():
        flag = '--' + snake_case(key).replace('_', '-')
        kw = {'dest': key, 'default': argparse.SUPPRESS, 'required': key in spec.input_schema.get('required', [])}
        if field.get('description'):
            kw['help'] = field['description']
        kind = field.get('type')
        if kind == 'boolean': kw['action'] = 'store_true'
        elif kind == 'array':
            if field.get('items', {}).get('type') == 'string':
                kw['action'] = 'append'
                kw['help'] = (kw.get('help', '') + f' 每个值重复一次 {flag}，不是 JSON 数组。').strip()
            else: kw['type'] = _json_argument
        elif kind == 'object': kw['type'] = _json_argument
        elif isinstance(kind, list):
            def parse_union(value):
                if value.startswith('@'):
                    return _json_argument(value)
                try:
                    return json.loads(value)
                except ValueError:
                    return value  # validation reports a typed error after argparse accepts the token
            kw['type'] = parse_union
        elif kind == 'integer': kw['type'] = int
        elif kind == 'number': kw['type'] = float
        if 'enum' in field: kw['choices'] = field['enum']
        parser.add_argument(flag, **kw)


def _novelctl():
    return importlib.import_module('scripts.novelctl' if __package__.startswith('scripts.') else 'novelctl')


READ_COMMANDS = {
    ('overview',), ('prompt-plan',), ('prepare',), ('entity',), ('prose',), ('context',),
    ('settings', 'show'), ('settings', 'check'), ('state', 'status'),
    ('structure', 'show'), ('structure', 'graph'), ('structure', 'timeline'), ('structure', 'plans'), ('structure', 'operations'),
    ('structure', 'refactor-contract'), ('structure', 'refactor-references'),
    ('workflow', 'show'), ('workflow', 'chapter-receipt'), ('workflow', 'chapter-status'), ('workflow', 'diagnostics-diff'),
    ('workflow', 'state-preview'), ('workflow', 'continuity-preview'),
    ('summary', 'prepare'), ('import', 'validate'),
    # 所有权迁移的只读面：verify 与 rollback-preview 都不写任何东西（rollback-preview 只证明
    # Window 1 仍成立），因此归 read，而不是像 source migration 那样带 plan 工件的 plan-write。
}
WRITE_COMMANDS = {
    ('quick-init',): 'protected-write', ('init',): 'protected-write', ('export',): 'export-write',
    ('import', 'scan'): 'candidate-write', ('import', 'review'): 'candidate-write',
    ('import', 'create'): 'protected-write',
    ('summary', 'commit'): 'candidate-write', ('structure', 'gc'): 'maintenance', ('structure', 'commit'): 'protected-write', ('structure', 'undo'): 'undo',
    # 项目文件夹迁移与迁移重绑：三类 divergence 各自 preview → commit，都不进 MCP。
    ('relocate',): 'protected-write', ('state', 'rebuild'): 'derived-write',
    # 所有权模型切刀：preview 落一份经审查的迁移计划，commit/rollback-commit 都是项目级原子
    # 权威切换；和 source migration 同一先例，不进 MCP 白名单——无特权通道只能看预览与验证。
    ('workflow', 'artifact-template'): 'candidate-write', ('workflow', 'artifact-register'): 'candidate-write',
    ('workflow', 'approve-chapters'): 'approval', ('workflow', 'artifact-approve'): 'approval',
    ('workflow', 'continuity-complete'): 'approval', ('workflow', 'advance'): 'protected-write',
    ('workflow', 'finalize-chapter'): 'protected-write', ('workflow', 'repair-round'): 'candidate-write',
    # 接受外部修改是高风险所有权流转：preview 与 commit 同在一个 leaf，因此整体按
    # protected-write 分类，且**不进 MCP 白名单**——无特权通道只能看预览（契约 §10.1）。
    ('workflow', 'accept-external'): 'protected-write',
    ('workflow', 'set-next'): 'candidate-write', ('workflow', 'recover'): 'undo', ('workflow', 'reconcile'): 'candidate-write',
}
# MCP 只暴露创作任务链所需能力，不逐个包装维护脚本（见报告一的 MCP 通道定位）。
# `doctor` 不在此列：它是 new_operations() 里的 project.doctor，schema 单一真源。
MCP_ALLOWLIST = {
    ('overview',), ('entity',), ('prose',), ('export',),
    ('settings', 'show'), ('settings', 'check'),
    ('structure', 'show'), ('structure', 'graph'), ('structure', 'timeline'), ('structure', 'plans'), ('structure', 'operations'),
    ('structure', 'refactor-contract'), ('structure', 'refactor-references'),
    ('workflow', 'show'), ('workflow', 'chapter-receipt'), ('workflow', 'state-preview'),
    ('workflow', 'continuity-preview'), ('workflow', 'continuity-complete'), ('workflow', 'artifact-template'),
    ('workflow', 'artifact-register'), ('workflow', 'artifact-approve'), ('workflow', 'approve-chapters'),
    ('workflow', 'advance'), ('workflow', 'finalize-chapter'), ('workflow', 'repair-round'), ('workflow', 'set-next'),
}


def operations() -> list[OperationSpec]:
    from .static_cache import cached_definition
    return cached_definition('operations', _operations)


def _operations() -> list[OperationSpec]:
    result = new_operations()
    parser = _novelctl().build_parser()
    def walk(node, path=(), defaults=None):
        defaults = dict(defaults or {})
        actions = [a for a in node._actions if isinstance(a, argparse._SubParsersAction)]
        if actions:
            for action in actions:
                for name, child in action.choices.items(): walk(child, (*path, name), {**defaults, action.dest: name})
            return
        if node.get_default('operation_binding'):
            return
        command = path[1:]
        if command in READ_COMMANDS: effect = 'read'
        elif command in WRITE_COMMANDS: effect = WRITE_COMMANDS[command]
        elif len(command) == 2 and command[0] == 'structure' and command[1].endswith('-preview'): effect = 'plan-write'
        else: raise StructureError('OPERATION_UNCLASSIFIED', 'CLI leaf lacks an effect classification', {'command': list(command)})
        props, required, mapping, initial = {}, [], {}, dict(defaults)
        for action in node._actions:
            if action.dest == 'help': continue
            key = ''.join([action.dest.split('_')[0], *[s.title() for s in action.dest.split('_')[1:]]])
            field = dict(STRING)
            if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)): field = dict(BOOLEAN)
            elif action.type is int: field = dict(INTEGER)
            elif action.type is float: field = {'type': 'number'}
            elif action.type is json.loads: field = {'type': 'object'}
            if action.choices is not None: field['enum'] = list(action.choices)
            if isinstance(action, argparse._AppendAction): field = {'type': 'array', 'items': field}
            if action.help and action.help != argparse.SUPPRESS: field['description'] = action.help
            props[key] = field; mapping[key] = action.dest; initial[action.dest] = action.default
            if action.required or (not action.option_strings and action.nargs not in ('?', '*')): required.append(key)
        def invoke(root, args, initial=initial, mapping=mapping):
            values = dict(initial)
            values.update({mapping[k]: v for k, v in args.items()})
            if 'project_root' in values: values['project_root'] = str(root)
            module = _novelctl()
            try:
                return module._dispatch(argparse.Namespace(**values))
            except module.NovelError as exc:
                raise StructureError(exc.code, str(exc), exc.details) from exc
        operation_id, name = 'project.' + '.'.join(command), '_'.join(command).replace('-', '_')
        # These legacy flags can write; separate new read operations are used by task execution.
        if command in {('context',), ('structure', 'graph'), ('structure', 'timeline')}:
            effect = 'derived-write'
        if command == ('export',):
            effect = 'export-write'
        schema = {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}
        maintenance = command[0] in {'import', 'state'} or command in {('structure', 'gc')}
        result.append(OperationSpec(operation_id, name, node.description or node.prog + '（兼容入口）', schema, effect, invoke, command,
                                    mcp=command in MCP_ALLOWLIST, category='project-maintenance' if maintenance else 'station',
                                    examples=tuple(node.get_default('operation_examples') or ())))
    walk(parser)
    return result


def call_operation(operation_id: str, root: Path, arguments: dict) -> dict:
    spec = next((s for s in operations() if s.id == operation_id), None)
    if spec is None: raise StructureError('OPERATION_NOT_FOUND', 'operation is not registered')
    supplied = dict(arguments)
    if 'projectRoot' in spec.input_schema['properties']: supplied['projectRoot'] = str(root)
    from .chapter_refs import annotate, resolve_chapter_arguments
    resolved = resolve_chapter_arguments(root, supplied)
    validate_arguments(spec.input_schema, supplied)
    from .call_logging import observe_payload
    observe_payload('effectiveArguments', supplied)
    supplied.pop('projectRoot', None)
    from .model_response import business_arguments
    supplied = business_arguments(operation_id, supplied)
    from .chapter_receipt import bound_receipt_response
    return bound_receipt_response(root, annotate(spec.handler(root, supplied), resolved))


def dispatch_new(args) -> dict:
    spec = next(s for s in new_operations() if s.id == args.operation_binding)
    supplied = {key: getattr(args, key) for key in spec.input_schema['properties'] if hasattr(args, key)}
    validate_arguments(spec.input_schema, supplied)
    root = Path(supplied.pop('projectRoot', '.') or '.').expanduser().resolve()
    from .model_response import business_arguments
    supplied = business_arguments(spec.id, supplied)
    return spec.handler(root, supplied)
