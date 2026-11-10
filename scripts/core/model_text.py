"""Deterministic, lossless reading text for named model-facing operations.

The structured response remains the call contract. This renderer neither makes
workflow decisions nor elides evidence: unknown fields keep their JSON values,
and row tables keep exact column names and JSON cells. Use prose only when both
decoded text and its JSON string reduce characters and UTF-8 bytes.
"""
from __future__ import annotations

import json

from .model_response import MODEL_OPERATIONS
from .call_log_redaction import requires_credential_labels
from .project import call_logging_policy


TEXT_OPERATIONS = MODEL_OPERATIONS | {
    'task.prepare', 'context.read', 'project.overview', 'capabilities.help',
}

# These names belong to the transport envelopes, never arbitrary user objects.
_LABELS = {
    'code': 'code', 'task': '任务', 'chapterId': '章节', 'projectId': '项目',
    'bookName': '书名', 'revision': '修订', 'structureHash': '结构指纹',
    'chapterLength': '章长要求', 'counts': '数量', 'latestStableChapter': '最新稳定章',
    'chapters': '章节列表', 'arcs': '弧线', 'chapterArcLinks': '章节弧线关联',
    'prepareRef': '准备引用', 'readiness': '就绪状态', 'authorization': '授权',
    'modelContext': '上下文', 'readList': '材料清单', 'outlineFacts': '计划依据',
    'bootstrap': '初始化', 'reading': '读取', 'readingTools': '读取工具',
    'readingToolsGuide': '工具查询', 'contractReads': '契约材料', 'roundtable': '圆桌',
    'providerReads': 'Provider材料', 'onDemandReferences': '按需参考', 'reviewPlan': '审阅计划',
    'missing': '缺项', 'notes': '说明', 'scope': '范围', 'nextAction': '下一步',
    'nextActions': '后续动作', 'workflowNextAction': '流程下一步',
    'nextChapterPlanning': '后章规划', 'actionReadiness': '动作就绪状态',
    'toolPlan': '工具计划', 'pendingContinuity': '待办连续性', 'summary': '摘要',
    'guidance': '处理说明', 'blockers': '阻断', 'findings': '发现', 'error': '错误',
    'details': '详情', 'recoveryActions': '恢复动作', 'runAction': '执行动作',
    'resumeAction': '续办动作', 'fullDetails': '完整详情', 'receipt': '回执',
    'artifactIntegrity': '工件完整性', 'lineDetails': '行级明细',
    'voiceCoverage': '声音覆盖', 'chapterContract': '章节约束',
    'operations': '操作', 'help': '帮助', 'groups': '分组', 'start': '起点',
    'lookup': '查询', 'noMatchAction': '无匹配时', 'fullHelpAction': '完整帮助',
    'suggestedActions': '建议动作', 'artifacts': '工件',
}
_CONTEXT_LABELS = {
    'task': '任务', 'chapter': '章节', 'pov': '视角', 'memories': '作品状态',
    'sourceReads': '真源读取', 'authorDecisions': '作者决定',
    'referenceKnowledge': '参考知识', 'recentProseReads': '近期正文',
    'sourceReadInstruction': '读取说明', 'planStatus': '计划状态',
    'unapprovedCandidates': '未批准候选', 'approvedCandidates': '已批准候选',
    'unclassifiedRecordIds': '未分类记录', 'unclassifiedContext': '未分类上下文',
    'workVoiceProfile': '作品声音', 'candidateInputs': '候选输入',
    'decisionStatus': '决定状态', 'povScoped': '已限定视角',
}
_READING_LABELS = {
    'credential': '凭据', 'operation': '操作', 'referenceReads': '参考读取',
    'sourceReads': '真源读取', 'instruction': '说明',
}
_READY_ITEMS = {
    'settingsReady': '设定', 'contextReady': '上下文',
    'authorizationReady': '操作授权条件', 'scopeReady': '范围', 'taskReady': '任务',
}
_TOOL_PLAN_LABELS = {
    'task': '任务', 'chapterId': '章节', 'chapterStatus': '章节状态',
    'boundary': '边界', 'toolActions': '工具动作', 'unresolved': '未解决',
    'whyNot': '不可执行原因', 'omittedNotApplicable': '不适用数量',
}
_OPERATION_LABELS = {
    'id': 'id', 'name': '工具', 'description': '用途', 'effects': '效果',
    'inputSchema': '参数', 'invocation': '调用', 'examples': '示例',
    'annotations': '标注', 'required': '必填', 'category': '类别', 'cli': 'CLI', 'mcp': 'MCP',
}
_TABLE_FIELDS = {
    'chapters', 'arcs', 'chapterArcLinks', 'readList', 'contractReads',
    'providerReads', 'memories', 'sourceReads', 'authorDecisions',
    'referenceKnowledge', 'recentProseReads', 'referenceReads', 'operations', 'groups',
    'toolActions', 'suggestedActions', 'artifacts', 'blockers', 'findings',
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _smaller(candidate: str, original: str) -> bool:
    if len(candidate) >= len(original) or len(candidate.encode()) >= len(original.encode()):
        return False
    # MCP wraps the text in JSON: newlines/backslashes also count on the wire.
    escaped, baseline = _json(candidate), _json(original)
    return len(escaped) < len(baseline) and len(escaped.encode()) < len(baseline.encode())


def _rows(rows: list) -> str:
    """Only uniform records become rows; absent fields never become null cells."""
    original = _json(rows)
    if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
        return original
    columns = list(rows[0])
    if not columns or any(set(row) != set(columns) for row in rows):
        return original
    if requires_credential_labels(rows, call_logging_policy()):
        return original
    table = '列' + _json(columns) + '\n' + '\n'.join(
        _json([row[key] for key in columns]) for row in rows)
    return table if _smaller(table, original) else original


def _record(value: dict, labels: dict) -> str:
    if not value:
        return '{}'
    lines, empty, nulls, remaining = [], [], [], {}
    for key, item in value.items():
        if key not in labels:
            remaining[key] = item
            continue
        if item is None:
            nulls.append(key)
        elif isinstance(item, list) and not item:
            empty.append(key)
        else:
            rendered = _rows(item) if key in _TABLE_FIELDS and isinstance(item, list) else _json(item)
            lines.append(labels[key] + ':' + rendered)
    # Empty values remain explicitly distinguishable from absent/unknown values.
    if empty:
        lines.append('空列表:' + ','.join(empty))
    if nulls:
        lines.append('null:' + ','.join(nulls))
    if remaining:
        lines.append('其他:' + _json(remaining))
    return '\n'.join(lines)


def _readiness(value: dict) -> str:
    remaining, ready, pending, lines = dict(value), [], [], []
    if type(remaining.get('structureHealthy')) is bool:
        lines.append('结构健康。' if remaining.pop('structureHealthy') else '结构不健康。')
    for key, label in _READY_ITEMS.items():
        if type(remaining.get(key)) is bool:
            (ready if remaining.pop(key) else pending).append(label)
    if ready:
        lines.append('已就绪:' + '、'.join(ready) + '。')
    if pending:
        lines.append('未就绪:' + '、'.join(pending) + '。')
    if remaining:
        lines.append(_json(remaining))
    return '\n'.join(lines) if lines else '{}'


def _authorization(value: dict) -> str:
    remaining, lines = dict(value), []
    if remaining.get('preparationGrantsApproval') is False:
        remaining.pop('preparationGrantsApproval')
        lines.append('准备不授予批准。')
    if remaining.get('stationJudgmentsRequired') is True:
        remaining.pop('stationJudgmentsRequired')
        lines.append('仍需工位判断。')
    # Pending actions, their preconditions and every extension keep exact JSON.
    if remaining:
        lines.append(_json(remaining))
    return '\n'.join(lines) if lines else '{}'


def render_model_text(operation: str | None, payload, *, view: str | None = None) -> str:
    """Render known envelopes, with an exact compact-JSON fallback."""
    original = _json(payload)
    if (operation not in TEXT_OPERATIONS or view in {'full', 'station', 'audit'}
            or not isinstance(payload, dict)):
        return original
    remaining = dict(payload)
    lines = []
    if type(remaining.get('ok')) is bool:
        lines.append('调用成功。' if remaining.pop('ok') else '调用失败。')
    for key, positive, negative in (
        ('readyToFinalize', '定稿就绪。', '定稿未就绪。'),
        ('readyForContinuation', '接续就绪。', '接续未就绪。'),
        ('semanticReviewComplete', '语义审阅已完成。', '语义审阅未完成。'),
        ('approvalPerformed', '本次已批准。', '本次未批准。'),
    ):
        if type(remaining.get(key)) is bool:
            lines.append(positive if remaining.pop(key) else negative)
    if operation.startswith('diagnostics.') and type(remaining.get('passed')) is bool:
        lines.append('机械检查通过。' if remaining.pop('passed') else '机械检查未通过。')
    for key in ('code', 'blockers', 'missing'):
        if key in remaining:
            value = remaining.pop(key)
            if key in {'blockers', 'missing'} and isinstance(value, list) and not value:
                lines.append(_LABELS[key] + ':无')
            else:
                lines.append(_record({key: value}, _LABELS))
    for key, render in (('readiness', _readiness), ('authorization', _authorization)):
        value = remaining.get(key)
        if isinstance(value, dict):
            rendered = '{\n' + render(value) + '\n}'
            if _smaller(rendered, _json(value)):
                remaining.pop(key)
                lines.append(_LABELS[key] + ':' + rendered)
    for key in ('authorization', 'nextAction', 'nextActions'):
        if key in remaining:
            lines.append(_record({key: remaining.pop(key)}, _LABELS))
    for key, labels in (('modelContext', _CONTEXT_LABELS), ('reading', _READING_LABELS),
                        ('toolPlan', _TOOL_PLAN_LABELS), ('receipt', _LABELS)):
        value = remaining.get(key)
        if isinstance(value, dict):
            rendered = '{\n' + _record(value, labels) + '\n}'
            if _smaller(rendered, _json(value)):
                remaining.pop(key)
                lines.append(_LABELS[key] + ':' + rendered)
    definitions = remaining.get('operations')
    if (operation == 'capabilities.help' and isinstance(definitions, list)
            and len(definitions) == 1 and isinstance(definitions[0], dict)):
        rendered = '[{\n' + _record(definitions[0], _OPERATION_LABELS) + '\n}]'
        if _smaller(rendered, _json(definitions)):
            remaining.pop('operations')
            lines.append('操作:' + rendered)
    if remaining:
        lines.append(_record(remaining, _LABELS))
    candidate = '\n'.join(lines)
    return candidate if _smaller(candidate, original) else original
