"""Check the built, POV-filtered station context without redoing retrieval.

Context Health owns the *classification* of every issue it raises, not just its
severity. Each issue carries a `status` drawn from the shared issue vocabulary
(`mechanical_error` / `stale` / `needs_rebuild` / `author_decision_required` /
`semantic_review_required` / `informational`) plus a `recommendedAction`. The
classification follows the issue's real production condition — a stale hash is
`stale`, a missing chapter plan is the author's decision, a broken layer/POV/
decision provenance contract is a mechanical fault, an over-budget pack is a
mechanical contract violation, a budget omission is informational. Consumers
(project_doctor's `context` domain) pass these through instead of re-deriving
them from the code string, so "this code is a rebuild vs. an author decision"
is decided in exactly one place: here, next to the check that raises it.
"""
from __future__ import annotations

from pathlib import Path

from .structure_store import StructureError

# Each context-health code's issue classification. The doctor consumes this
# verbatim; it must never re-guess a status from the code substring again.
CONTEXT_ISSUE_STATUS = {
    'CONTEXT_SNAPSHOT_STALE': 'stale',
    'CONTEXT_BUDGET_EXCEEDED': 'mechanical_error',
    'CONTEXT_SUMMARY_STALE': 'stale',
    'CONTEXT_CHAPTER_PLAN_MISSING': 'author_decision_required',
    'CONTEXT_RULES_BLOCKED': 'semantic_review_required',
    'CONTEXT_DEPENDENCY_MISSING': 'needs_rebuild',
    'CONTEXT_KNOWLEDGE_PROVENANCE_INVALID': 'mechanical_error',
    'CONTEXT_KNOWLEDGE_OMITTED': 'informational',
    'CONTEXT_DECISIONS_STALE': 'stale',
    'CONTEXT_DECISION_PROVENANCE_INVALID': 'mechanical_error',
    'CONTEXT_VOICE_PROFILE_INVALID': 'mechanical_error',
    'CONTEXT_POV_PROVENANCE_MISSING': 'mechanical_error',
    'CONTEXT_LAYER_CONTRACT_INVALID': 'mechanical_error',
    'CONTEXT_VALUE_CONFLICT': 'semantic_review_required',
    'CONTEXT_DUPLICATE': 'semantic_review_required',
    'CONTEXT_OPTIONAL_OMITTED': 'informational',
}

CONTEXT_ISSUE_ACTION = {
    'CONTEXT_SNAPSHOT_STALE': '输入在构建后发生过变化；重建上下文（context read）后再用',
    'CONTEXT_BUDGET_EXCEEDED': '装配结果超出字符预算，属机械契约违规；检查预算配置与必填层体量',
    'CONTEXT_SUMMARY_STALE': '故事摘要落后于当前章节；刷新摘要后重建上下文',
    'CONTEXT_CHAPTER_PLAN_MISSING': '本章计划未就绪；补齐已批准的规划输入（这是作者决定，不是引擎故障）',
    'CONTEXT_RULES_BLOCKED': '有必检规则在本章被阻断；由 App/作者按 source_rules 语义审查解决',
    'CONTEXT_DEPENDENCY_MISSING': '依赖清单缺失或与源计划不一致；重建上下文以重新生成清单',
    'CONTEXT_KNOWLEDGE_PROVENANCE_INVALID': '参考资料层的角色/出处与依赖清单不符，属装配契约问题；重建上下文',
    'CONTEXT_KNOWLEDGE_OMITTED': '部分参考资料因预算被省略；仅提示，检索结论可能不完整',
    'CONTEXT_DECISIONS_STALE': '本章适用的作者决定已失效或缺证明；沿 supersedes 建立受审新决定后批准，或明确撤回',
    'CONTEXT_DECISION_PROVENANCE_INVALID': '作者决定的版本出处与依赖清单不符，属装配契约问题',
    'CONTEXT_VOICE_PROFILE_INVALID': '声纹层角色无效或缺已批准版本 hash，属装配契约问题',
    'CONTEXT_POV_PROVENANCE_MISSING': '做了 POV 限定却缺过滤出处，属装配契约问题',
    'CONTEXT_LAYER_CONTRACT_INVALID': '认识层契约被破坏（某层冒充另一层认识地位），属装配契约问题',
    'CONTEXT_VALUE_CONFLICT': '同一认识槽出现互相冲突的值；需语义审查裁定',
    'CONTEXT_DUPLICATE': '同一认识槽出现重复项；建议核查记忆包是否冗余',
    'CONTEXT_OPTIONAL_OMITTED': '可选层因预算被省略；仅提示',
    'CONTEXT_MANDATORY_UNAVAILABLE': '必填层不可用，工位不能在缺失硬约束下开工；见 reasons 逐项解决',
}


def _mandatory_unavailable_status(reasons: list | None) -> str:
    """Classify CONTEXT_MANDATORY_UNAVAILABLE from its actual reasons, never a blind guess.

    The builder can only give two reasons: SOURCE_RULES_BLOCKED (a required rule
    is unresolved — follow source_rules' own semantics: an App/author review) and
    POV_HARD_CONSTRAINTS_UNAVAILABLE (the POV filter mechanically withheld a
    mandatory hard constraint — a fail-closed contract situation, not a semantic
    review). Rules-only is `semantic_review_required`; anything involving the POV
    filter, or an unrecognized reason, fails closed as a mechanical error.
    """
    reasons = set(reasons or [])
    if reasons and reasons <= {'SOURCE_RULES_BLOCKED', 'AUTHOR_DECISIONS_REQUIRE_REVIEW'}:
        return 'semantic_review_required'
    return 'mechanical_error'


def assess_context(context: dict, *, expectedHash: str | None = None) -> dict:
    if context.get('view') != 'station' or not isinstance(context.get('memoryPack'), dict):
        raise StructureError('CONTEXT_HEALTH_INPUT_INVALID', 'station context required')
    issues = []
    def issue(code: str, severity: str, **details):
        status = (_mandatory_unavailable_status(details.get('reasons'))
                  if code == 'CONTEXT_MANDATORY_UNAVAILABLE'
                  else CONTEXT_ISSUE_STATUS.get(code, 'semantic_review_required'))
        issues.append({'code': code, 'severity': severity, 'status': status,
                       'recommendedAction': CONTEXT_ISSUE_ACTION.get(code, ''), **details})
    if expectedHash is not None and expectedHash != context.get('contextHash'):
        issue('CONTEXT_SNAPSHOT_STALE', 'blocking')
    readiness = context.get('readiness', {})
    if not readiness.get('ready'):
        issue('CONTEXT_MANDATORY_UNAVAILABLE', 'blocking', reasons=readiness.get('reasons', []))
    budget = context.get('contextBudget', {})
    if budget.get('usedCharacters', 0) > budget.get('total', 0):
        issue('CONTEXT_BUDGET_EXCEEDED', 'blocking')
    freshness = context.get('summaryFreshness', {})
    if freshness.get('stale'):
        issue('CONTEXT_SUMMARY_STALE', 'warning', asOfChapterId=freshness.get('asOfChapterId'))
    source = context.get('sourceContext', {})
    if source and not source.get('planReady'):
        issue('CONTEXT_CHAPTER_PLAN_MISSING', 'warning')
    if source and not source.get('ruleSummary', {}).get('ready'):
        issue('CONTEXT_RULES_BLOCKED', 'blocking', ruleIds=source['ruleSummary'].get('blockingRuleIds', []))
    dependencies = context.get('dependencyManifest', {})
    if source and (not dependencies.get('sourceHash') or dependencies.get('sourcePlanHash') != source.get('sourcePlanHash')):
        issue('CONTEXT_DEPENDENCY_MISSING', 'blocking')
    knowledge = context.get('knowledgePack', {})
    if knowledge:
        expected_knowledge = [{'assetId': row.get('assetId'), 'sha256': row.get('sha256')}
                              for row in knowledge.get('items', [])]
        if knowledge.get('role') != 'REFERENCE KNOWLEDGE' or expected_knowledge != dependencies.get('knowledge'):
            issue('CONTEXT_KNOWLEDGE_PROVENANCE_INVALID', 'blocking')
        if knowledge.get('omittedBudget'):
            issue('CONTEXT_KNOWLEDGE_OMITTED', 'warning', count=knowledge['omittedBudget'])
    if context.get('staleDecisionIds'):
        issue('CONTEXT_DECISIONS_STALE', 'blocking', decisionIds=context['staleDecisionIds'],
              decisions=context.get('unavailableDecisions', []))
    profile = context.get('workVoiceProfile')
    if profile and (profile.get('role') != 'WORK VOICE PROFILE' or not dependencies.get('voiceProfileHash')):
        issue('CONTEXT_VOICE_PROFILE_INVALID', 'blocking')
    # POV scoping must carry its provenance; a scoped context with no filter evidence
    # cannot prove forbidden information was withheld.
    if context.get('povCharacter') and not context.get('filterProvenance'):
        issue('CONTEXT_POV_PROVENANCE_MISSING', 'blocking')
    # Epistemic layer contract: the Builder must emit every role in order so no layer
    # impersonates another (a belief/candidate is never authoritative canon).
    layers = context.get('layers')
    if layers is not None:
        expected_roles = ['CANON FACTS', 'CANON SEMANTIC CONTRACT', 'CURRENT BELIEFS', 'IN-WORLD TEXT',
                          'APPROVED PLAN', 'UNAPPROVED CANDIDATES', 'AUTHOR DECISIONS', 'REFERENCE KNOWLEDGE',
                          'WORK VOICE PROFILE']
        if not isinstance(layers, list) or [row.get('role') for row in layers] != expected_roles:
            issue('CONTEXT_LAYER_CONTRACT_INVALID', 'blocking')
        else:
            by_role = {row['role']: row for row in layers}
            if (by_role['CANON FACTS'].get('epistemicRole') != 'canon_fact'
                    or not by_role['CANON FACTS'].get('authoritative')
                    # FieldDefinition 是语义契约层：权威但不注入内容，也不得回流成世界事实。
                    or by_role['CANON SEMANTIC CONTRACT'].get('epistemicRole') != 'canon_semantic_contract'
                    or not by_role['CANON SEMANTIC CONTRACT'].get('authoritative')
                    or by_role['CANON SEMANTIC CONTRACT'].get('contentInjected')
                    or by_role['CURRENT BELIEFS'].get('authoritative')
                    or by_role['IN-WORLD TEXT'].get('authoritative')
                    or by_role['UNAPPROVED CANDIDATES'].get('authoritative')):
                issue('CONTEXT_LAYER_CONTRACT_INVALID', 'blocking')
    # Decisions are injected content, so the manifest must prove *which* version was
    # loaded: an ID alone cannot tell a re-approved decision from the one that was read.
    injected = context.get('authorDecisions') or []
    recorded = dependencies.get('decisions') or []
    if [row.get('decisionId') for row in injected] != [row.get('decisionId') for row in recorded] or \
            any(not isinstance(row.get('sha256'), str) or not row['sha256'] for row in recorded):
        issue('CONTEXT_DECISION_PROVENANCE_INVALID', 'blocking')
    pack = context['memoryPack']
    seen = {}
    for layer in ('working', 'episodic', 'semantic'):
        for row in pack.get(layer, []):
            key = (str(row.get('subject')), str(row.get('field')), str(row.get('source_chapter')))
            # Arc participation is a chapter–arc edge, not one scalar on the arc.
            # Two chapters in the same arc must not look like conflicting values.
            if row.get('field') == 'chapterArcLink':
                value = row.get('value')
                if not isinstance(value, dict) or not value.get('chapterId'):
                    issue('CONTEXT_VALUE_CONFLICT', 'blocking', subject=key[0], field=key[1],
                          reason='chapterArcLink identity incomplete')
                    continue
                key += (str(value['chapterId']),)
            if key in seen:
                if seen[key] != row.get('value'):
                    issue('CONTEXT_VALUE_CONFLICT', 'blocking', subject=key[0], field=key[1])
                else:
                    issue('CONTEXT_DUPLICATE', 'warning', subject=key[0], field=key[1])
            else:
                seen[key] = row.get('value')
    omitted = {layer: pack.get('provenance', {}).get(layer, {}).get('omittedBudget', 0)
               for layer in ('working', 'episodic', 'semantic')}
    if any(omitted.values()) or context.get('recentChapterProvenance', {}).get('omittedBudget', 0):
        issue('CONTEXT_OPTIONAL_OMITTED', 'warning', byLayer=omitted,
              recentChapters=context.get('recentChapterProvenance', {}).get('omittedBudget', 0))
    return {'ok': True, 'code': 'CONTEXT_HEALTH', 'ready': not any(row['severity'] == 'blocking' for row in issues),
            'contextHash': context.get('contextHash'), 'expectedHash': expectedHash,
            'summaryFreshness': freshness, 'budget': budget, 'issues': issues,
            'coverage': {'builtContextOnly': True, 'implicitProseScanned': False,
                         'semanticContradictionsExhaustive': False}}


def query_context_health(root: Path, *, chapterId: str, task: str = 'writer',
                         povCharacter: str | None = None, chapterGoal: str = '',
                         expectedHash: str | None = None, knowledgeMode: str | None = None) -> dict:
    if task not in {'planner', 'writer', 'editor'}:
        raise StructureError('CONTEXT_TASK_INVALID', 'task must be planner, writer or editor')
    # 消费统一 Builder，而不是重新实现一遍上下文选择。
    from .context_builder import build_station_context
    context = build_station_context(str(root), chapterId, task_type=task,
                                    pov_character=povCharacter, chapter_goal=chapterGoal, knowledge_mode=knowledgeMode,
                                    view='station', read_only=True)
    return assess_context(context, expectedHash=expectedHash)
