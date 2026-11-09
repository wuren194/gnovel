"""Presentation boundary over the validated Builder result, never a second builder.

Legacy station/full objects remain the machine API. The model sees semantic
records once; provenance is carried out of band, without a second copy of text.
Source prose remains behind the existing POV-scoped queries.
"""
from __future__ import annotations

from copy import deepcopy

from .artifacts import sha256_json
from .structure_store import StructureError


def semantic_view(context: dict) -> tuple[dict, dict]:
    """Return deterministic model content and a text-free machine sidecar.

    Only known envelope metadata is removed. All memory value fields (including
    unknown extensions) survive unchanged. Identical memory rows across tiers
    share one handle; every location is retained in its proof mapping.
    """
    from .context_health import assess_context
    health = assess_context(context)
    if not health['ready']:
        raise StructureError('CONTEXT_MODEL_BLOCKED', 'context health blocks model transport', health)
    signed = {k: v for k, v in context.items() if k != 'contextHash'}
    # Operations add ok/code after the Builder; legacy direct CLI rebinds them.
    if context.get('contextHash') not in {
        sha256_json(signed), sha256_json({k: v for k, v in signed.items() if k not in {'ok', 'code'}})
    }:
        raise StructureError('CONTEXT_PROOF_INVALID', 'context content no longer matches its hash')
    model = {'task': context['task'], 'chapter': context['targetChapter'],
             'pov': context.get('povCharacter'), 'memories': [], 'sourceReads': [],
             'authorDecisions': [], 'referenceKnowledge': [], 'recentProseReads': []}
    proof = deepcopy(context)
    handles = {}
    proof['handles'] = handles
    seen = {}
    for tier in ('working', 'semantic', 'episodic'):
        for index, row in enumerate(context['memoryPack'][tier]):
            identity = sha256_json({'field': row['field'], 'value': row['value']})
            location = ['memoryPack', tier, index]
            if identity not in seen:
                handle = f'MEM-{len(seen) + 1}'
                seen[identity] = handle
                model['memories'].append({'handle': handle, 'role': 'PROJECT STATE',
                                          'kind': row['field'], 'content': deepcopy(row['value'])})
                handles[handle] = {'locations': [], 'valueHash': sha256_json(row['value'])}
            handle = seen[identity]
            handles[handle]['locations'].append(location)
            # value remains in the legacy interface, not duplicated in the sidecar.
            proof['memoryPack'][tier][index]['value'] = {'handle': handle, 'sha256': sha256_json(row['value'])}
    source = context.get('sourceContext', {})
    if source:
        model['sourceReads'] = [
            {'roles': ['CANON FACTS', 'CANON SEMANTIC CONTRACT', 'CURRENT BELIEFS', 'IN-WORLD TEXT'],
             'query': deepcopy(source['contentQuery'])},
            {'roles': ['APPROVED PLAN'], 'query': deepcopy(source['planQuery'])},
            {'roles': ['RULES'], 'query': deepcopy(source['rulesQuery'])}]
        model['sourceReadInstruction'] = '按查询先定位，再以 detail 分页阅读相关内容；保持查询的 POV selection。belief 和世界内文本不等于 Canon。原文路径不证明全文通过 POV 检验。'
        for index, key in enumerate(('contentQuery', 'planQuery', 'rulesQuery')):
            handle = f'READ-{index + 1}'
            model['sourceReads'][index]['handle'] = handle
            handles[handle] = {'locations': [['sourceContext', key]], 'contentHash': sha256_json(source[key])}
        if not source.get('planReady'):
            model['planStatus'] = '本章计划未就绪'
    # No source text is newly injected; unknown/unclassified records stay visible.
    for layer in context.get('layers', []):
        if layer.get('unclassifiedRecordIds'):
            model['unclassifiedRecordIds'] = deepcopy(layer['unclassifiedRecordIds'])
        if layer['role'] == 'UNAPPROVED CANDIDATES' and layer['items']:
            model['unapprovedCandidates'] = [{'id': row['id'], 'status': row.get('status')} for row in layer['items']]
        if layer['role'] == 'APPROVED PLAN':
            approved = [row for row in layer['items'] if row.get('kind') == 'planning_candidate']
            if approved:
                model['approvedCandidates'] = [{'id': row['id'], 'status': row.get('status')} for row in approved]
    for index, decision in enumerate(context.get('authorDecisions', [])):
        handle = f'DEC-{index + 1}'
        model['authorDecisions'].append({'handle': handle, 'role': 'AUTHOR DECISIONS',
                                        **{k: deepcopy(v) for k, v in decision.items()
                                           if k not in {'decisionId', 'scopeIds', 'role'}}})
        handles[handle] = {'locations': [['authorDecisions', index]], 'decisionId': decision['decisionId'],
                           'contentHash': sha256_json(decision)}
        proof['authorDecisions'][index] = {'handle': handle, **{k: deepcopy(v) for k, v in decision.items()
                                                              if k not in {'statement', 'rationale'}}}
    for index, item in enumerate(context.get('knowledgePack', {}).get('items', [])):
        handle = f'KN-{index + 1}'
        # Strip only consumed identity/provenance/ranking fields, keep unknowns.
        metadata = {'assetId', 'path', 'sha256', 'source', 'bodyLineStart', 'matchedIntents', 'score'}
        model['referenceKnowledge'].append({'handle': handle, 'role': 'REFERENCE KNOWLEDGE',
                                            **{k: deepcopy(v) for k, v in item.items() if k not in metadata}})
        handles[handle] = {'locations': [['knowledgePack', 'items', index]],
                           **{k: deepcopy(v) for k, v in item.items() if k in metadata}}
        proof['knowledgePack']['items'][index] = {'handle': handle, 'contentHash': sha256_json(item),
            **{k: deepcopy(v) for k, v in item.items() if k not in {'title', 'excerpt', 'sectionHeading', 'role', 'truncated'}}}
    for index, row in enumerate(context.get('recentChapters', [])):
        handle = f'PROSE-{index + 1}'
        model['recentProseReads'].append({'handle': handle, **deepcopy(row)})
        handles[handle] = {'locations': [['recentChapters', index]],
                           'chapterId': row['chapterId'], 'path': row['relativePath'],
                           'sha256': next((r['sha256'] for r in context['dependencyManifest']['recentChapters']
                                           if r['chapterId'] == row['chapterId']), None)}
    if context.get('workVoiceProfile'):
        model['workVoiceProfile'] = deepcopy(context['workVoiceProfile'])
        proof['workVoiceProfile'] = {'sha256': sha256_json(context['workVoiceProfile'])}
    if context.get('candidateInputs'):
        model['candidateInputs'] = deepcopy(context['candidateInputs'])
    # All known top-level fields have explicit consumers. Unrecognized fields stay
    # in both views until their consumer is proven; no name-based deletion.
    known = {'projectId', 'targetChapter', 'targetDisplayCode', 'targetOrdinal', 'targetPlacement',
             'targetVolumeId', 'targetArcLinks', 'committedChapters', 'recentChapters', 'memoryPack',
             'summaryFreshness', 'povCharacter', 'task', 'filterProvenance', 'readiness', 'sourceContext',
             'layers', 'knowledgePack', 'knowledgeRoute', 'authorDecisions', 'dependencyManifest',
             'staleDecisionIds', 'workVoiceProfile', 'candidateInputs', 'view',
             'foreshadowingWarningCount', 'contextBudget', 'recentChapterProvenance', 'contextHash', 'ok', 'code'}
    unknown = {k: deepcopy(v) for k, v in context.items() if k not in known}
    if unknown:
        model['unclassifiedContext'] = unknown
    # Operational states that can affect reading are stated once, without hashes.
    model['povScoped'] = bool(context.get('povCharacter'))
    if context.get('staleDecisionIds'):
        model['decisionStatus'] = '存在过期作者决定，已排除；需作者重新批准或废止'
    from .context_builder import serialize_context
    envelope = {'ok': True, 'code': 'CONTEXT_MODEL', 'view': 'model', 'modelContext': model}
    if len(serialize_context(envelope)) > context['contextBudget']['total']:
        raise StructureError('CONTEXT_BUDGET_IMPOSSIBLE', 'semantic presentation exceeds station budget')
    return model, proof


def model_response(context: dict) -> dict:
    """Explicit CLI/API model view; the machine view is available separately."""
    model, _ = semantic_view(context)
    return {'ok': True, 'code': 'CONTEXT_MODEL', 'view': 'model', 'modelContext': model}


def model_tool_plan(plan: dict | None) -> dict | None:
    """Drop execution bookkeeping, retaining every action and blocking explanation."""
    if plan is None:
        return None
    compact = deepcopy(plan)
    for key in ('policyHash', 'facts', 'executed', 'outlineFacts'):
        compact.pop(key, None)
    # Keep blockers and station/approval explanations; omit only reasons for
    # actions proven inapplicable, never unknown or unresolved obligations.
    excluded = {row['capabilityId'] for row in compact.get('toolActions', []) if row['status'] == 'not-applicable'}
    compact['whyNot'] = [row for row in compact.get('whyNot', []) if row.get('capabilityId') not in excluded]
    compact['omittedNotApplicable'] = sum(row['status'] == 'not-applicable' for row in compact.get('toolActions', []))
    compact['toolActions'] = [row for row in compact.get('toolActions', []) if row['status'] != 'not-applicable']
    for row in compact.get('toolActions', []):
        for key in ('dedupeKey', 'description', 'budget', 'necessaryFor', 'applicability'):
            row.pop(key, None)
    return compact


def task_model_response(report: dict) -> dict:
    """Preparation inventory for model reading; hashes remain in legacy report.

    readList contains locators, never prose. Keep paths/ranges/reasons so material
    not injected by Context (including current Editor draft) remains readable.
    PromptPlan retains its separate contract loading responsibility.
    """
    from copy import deepcopy
    if '_modelDelivery' in report:
        return deepcopy(report['_modelDelivery'])
    reads = []
    seen = set()
    for row in report.get('readList', []):
        clean = {k: deepcopy(v) for k, v in row.items() if k not in {'sha256', 'sourceHash', 'sourcePlanHash'}}
        if clean.get('lines') is None:
            clean.pop('lines', None)
        recent = (report.get('context', {}).get('modelContext') or {}).get('recentProseReads', [])
        match = next((item for item in recent if item['relativePath'] == row.get('path') and not row.get('lines')), None)
        if match:
            clean.pop('path', None)
            clean['contextHandle'] = match['handle']
        key = sha256_json(clean)
        if key not in seen:
            reads.append(clean)
            seen.add(key)
    plan = report.get('promptPlan', {})
    outline = deepcopy(report.get('outlineFacts'))
    if isinstance(outline, dict):
        # The full prepared receipt owns machine bindings. The model already has
        # its prepareRef and source read IDs, so it need not transcribe this hash.
        outline.pop('sourcePlanHash', None)
        constraints = outline.get('projectConstraints')
        if (isinstance(constraints, dict) and 'sha256' not in constraints
                and constraints.get('ranges') == [] and constraints.get('conflicts') == []
                and constraints.get('exceptionDeclared') is False
                and constraints.get('exceptionApproved') is False):
            # No file was read: empty ranges and the recognition-method prose are
            # repeated boilerplate, not a claim that authored constraints passed.
            outline['projectConstraints'] = {'source': constraints['source'], 'available': False}
    reading = deepcopy(report.get('reading'))
    reference_paths = set()
    if isinstance(reading, dict):
        # The opaque credential already binds the exact scope; callers never
        # echo this second digest. The complete snapshot retains it for audits.
        reading.pop('scopeHash', None)
        for reference in reading.get('referenceReads', []):
            reference_paths.add(reference.get('referencePath', reference.get('path')))
            # Keep real aliases and every read ID. An identical referencePath
            # repeats the canonical locator; the full snapshot still owns it.
            if reference.get('referencePath') == reference.get('path'):
                reference.pop('referencePath', None)
    tool_plan = model_tool_plan(report.get('toolPlan'))
    pending_approvals = tool_plan.pop('pendingApprovals', []) if isinstance(tool_plan, dict) else []
    return {'ok': report['ok'], 'code': report['code'], 'task': report['task'],
            'projectId': report.get('projectId'), 'chapterId': report.get('chapterId'),
            'prepareRef': report.get('prepareRef', (report.get('reading') or {}).get('credential')),
            'authorization': {'preparationGrantsApproval': False,
                              'pendingApprovals': pending_approvals,
                              'stationJudgmentsRequired': True},
            'readiness': deepcopy(report['readiness']),
            'modelContext': deepcopy(report.get('context', {}).get('modelContext')),
            'readList': reads, 'outlineFacts': outline,
            'bootstrap': deepcopy(report.get('bootstrap')),
            'reading': reading, 'readingTools': {
                # The full schema already lives in the prepared snapshot and
                # the single-operation catalogue. A repeated top-level required
                # list is incomplete for oneOf/anyOf and is not a call contract.
                row['operation']: row['tool'] for row in report.get('readingTools', [])},
            'readingToolsGuide': 'operation→tool 映射。复用已持有定义；缺参数查 capabilities.list(operation=所选ID)，原定义见 task.snapshot(reference=prepareRef)。',
            'contractReads': [{'path': row['path'], 'readIds': row.get('readIds', []), 'readRanges': [
                {k: deepcopy(v) for k, v in span.items() if k != 'sha256'} for span in row['readRanges']]}
                for row in plan.get('contracts', [])],
            'providerReads': [{'id': row['id'], 'role': row['role'], 'path': row['path'],
                               'readId': row.get('readId'), 'active': row['active']}
                              for row in plan.get('providerMaterials', [])],
            # Exact file references already carry their locator and readId in
            # referenceReads. Keep directory roots here for additional slices.
            'onDemandReferences': [path for path in plan.get('onDemandReferences', [])
                                   if path not in reference_paths],
            'reviewPlan': deepcopy(plan.get('reviewPlan')),
            'missing': deepcopy(report.get('missing', [])), 'notes': deepcopy(report.get('notes', [])),
            'scope': deepcopy(report.get('scope')), 'nextAction': deepcopy(report.get('nextAction')),
            'workflowNextAction': deepcopy(report.get('workflowNextAction')),
            'nextChapterPlanning': deepcopy(report.get('nextChapterPlanning')),
            'actionReadiness': deepcopy(report.get('actionReadiness')),
            'toolPlan': tool_plan, 'pendingContinuity': deepcopy(report.get('pendingContinuity', []))}


def model_default_arguments(operation, arguments):
    """AI transports choose compact views; explicit full requests remain available."""
    if operation in {'project.doctor', 'source.search', 'source.content-resolve'}:
        return {'view': 'model', **arguments}
    return arguments
