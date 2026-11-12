"""Three-valued tool planning and task-scoped execution.

Planning is read-only. Execution runs only bounded reads by default; registering
a candidate artifact additionally requires an explicit `candidate` authorization
and never approves anything or writes protected content.
"""
from __future__ import annotations

from pathlib import Path

from .paths import stored_project_path
from .artifacts import sha256_json
from .jsonio import read_json
from .mechanical_evidence import evidence_current
from .operations import call_operation, operations
from .project import TOOLBOX_ROOT
from .structure_store import StructureError, load_structure
from .summary_status import summary_freshness
from .tool_catalog import FACT_TYPES, load_catalog
from .workflow import default_workflow

TASKS = ('planner', 'writer', 'editor', 'repair', 'titler', 'continuity')
AUTHORIZATION_BY_EFFECT = {
    'read': 'read', 'candidate-write': 'candidate', 'plan-write': 'author-approval',
    'derived-write': 'author-approval', 'protected-write': 'author-approval',
    'approval': 'author-approval', 'undo': 'author-approval', 'export-write': 'author-approval',
    'maintenance': 'repository-maintenance', 'semantic': 'station',
}


def _workflow(root: Path) -> dict:
    path = root / '.govel' / 'workflow.json'
    return read_json(path) if path.is_file() else default_workflow()


def boundary_for(task: str, status: str | None) -> str | None:
    """Which evidence boundary this task is about to cross, if any."""
    if status == 'draft' and task in ('editor', 'repair'):
        return 'chapter.review'
    if status == 'review' and task in ('editor', 'repair', 'titler'):
        return 'chapter.finalize'
    return None


def build_facts(root: Path, chapter_id: str | None, task: str) -> dict:
    facts: dict[str, str] = {}
    structure, workflow = None, None
    try:
        structure = load_structure(root)
        facts['project.initialized'] = 'true'
    except (StructureError, OSError, ValueError):
        facts['project.initialized'] = 'false'
    if structure is None:
        return {fact: 'unknown' for fact in FACT_TYPES}
    workflow = _workflow(root)
    artifacts = workflow.get('artifacts', {}).get(chapter_id or '', {})
    facts['candidate.ready'] = 'true' if isinstance(artifacts.get('candidate_prose'), dict) else 'false'
    try:
        diagnostic_status = evidence_current(root, chapter_id, structure=structure) if chapter_id else {}
        facts['diagnostics.current'] = 'true' if diagnostic_status.get('fresh') else 'false'
    except (StructureError, OSError, ValueError):
        facts['diagnostics.current'] = 'unknown'
    try:
        entry = artifacts.get('stage_b_review')
        approval = workflow.get('approvals', {}).get(chapter_id or '', {})
        hashes = approval.get('artifactHashes', {}) if isinstance(approval, dict) else {}
        fresh = bool(isinstance(entry, dict) and not entry.get('invalidatedBy')
                     and hashes.get('stage_b_review') == entry.get('artifactSha256'))
        facts['review.current'] = 'true' if fresh else 'false'
        payload = {}
        if isinstance(entry, dict) and entry.get('artifactPath'):
            path = stored_project_path(root, entry['artifactPath'])
            payload = read_json(path).get('payload', {}) if path.is_file() else {}
        facts['review.hasLocalFinding'] = 'true' if any(
            str(row.get('classification', '')).lower() == 'local' for row in payload.get('findings', []) if isinstance(row, dict)) else 'false'
    except (StructureError, OSError, ValueError, TypeError):
        facts['review.current'] = facts.get('review.current', 'unknown')
        facts['review.hasLocalFinding'] = 'unknown'
    try:
        facts['summary.stale'] = 'true' if summary_freshness(root, structure)['stale'] else 'false'
    except (OSError, ValueError, TypeError):
        facts['summary.stale'] = 'unknown'
    try:
        from .commands import story_timeline
        problems = story_timeline(root, structure=structure)['problems']
        facts['timeline.hasInversion'] = 'true' if any(p.get('code') == 'TIME_ORDER_INVERSION' for p in problems) else 'false'
    except (StructureError, OSError, ValueError, TypeError):
        facts['timeline.hasInversion'] = 'unknown'
    return facts


def _evaluate(fact_value: str, when: dict) -> str:
    if fact_value == 'unknown':
        return 'unknown'
    expected = 'true' if when['value'] else 'false'
    hit = fact_value == expected
    if when['op'] == 'ne':
        hit = not hit
    return 'true' if hit else 'false'


def build_tool_plan(root: Path, task: str, *, chapter_id: str | None = None, outline_facts=None) -> dict:
    if task not in TASKS:
        raise StructureError('TASK_UNKNOWN', 'unsupported task', {'supported': list(TASKS)})
    root = root.expanduser().resolve()
    catalog = load_catalog()
    specs = {s.id: s for s in operations()}
    facts = build_facts(root, chapter_id, task)
    status = None
    try:
        structure = load_structure(root)
        status = next((row.get('status') for row in structure['chapters'] if row['chapterId'] == chapter_id), None)
    except (StructureError, OSError, ValueError):
        structure = None
    boundary = boundary_for(task, status)
    policy_hash = sha256_json({'catalog': catalog, 'schema': 1})
    actions, pending, unresolved, why_not = [], [], [], []
    seen = set()
    for cap in catalog['capabilities']:
        if task not in cap['tasks']:
            continue
        binding = cap['binding']
        spec = specs.get(binding['ref']) if binding['kind'] == 'operation' else None
        if binding['kind'] == 'operation' and spec is None:
            unresolved.append({'capabilityId': cap['id'], 'reason': 'bound operation is not registered'})
            continue
        if binding['kind'] == 'skill' and not (TOOLBOX_ROOT / binding['ref']).is_file():
            unresolved.append({'capabilityId': cap['id'], 'reason': 'skill binding is missing'})
            continue
        evaluation = _evaluate(facts.get(cap['when']['fact'], 'unknown'), cap['when'])
        inputs_ready = facts.get('project.initialized') == 'true' and (chapter_id is not None or task not in ('editor', 'repair', 'titler', 'continuity'))
        effects = spec.effects if spec else 'semantic'
        authorization = AUTHORIZATION_BY_EFFECT[effects]
        necessary = bool(cap['requiredFor']) and boundary in cap['requiredFor']
        key = sha256_json({'capability': cap['id'], 'chapterId': chapter_id, 'task': task,
                           'inputs': f"{facts.get('project.initialized')}:{facts.get('diagnostics.current')}",
                           'policy': policy_hash})
        row = {'capabilityId': cap['id'], 'description': cap['description'], 'binding': binding,
               'effects': effects if binding['kind'] == 'operation' else 'semantic', 'dedupeKey': key,
               'applicability': evaluation, 'observed': facts.get(cap['when']['fact']),
               'reason': cap['when']['reason'], 'necessaryFor': cap['requiredFor'] if necessary else [],
               'necessary': necessary, 'inputsReady': inputs_ready, 'authorization': authorization,
               'evidenceRequired': cap['evidence'], 'budget': {'bounded': effects == 'read'}}
        if evaluation == 'unknown':
            row['status'] = 'unknown'
            why_not.append({'capabilityId': cap['id'], 'reason': f"fact {cap['when']['fact']} is unreadable"})
        elif evaluation == 'false':
            row['status'] = 'not-applicable'
        elif not inputs_ready:
            row['status'] = 'input-not-ready'
            why_not.append({'capabilityId': cap['id'], 'reason': 'required inputs are not available'})
        elif necessary and not _evidence_satisfied(root, cap, chapter_id, facts, structure):
            row['status'] = 'action-required'
        else:
            # 条件为真但当前阶段不强制：建议动作仍出现在计划里，另带 necessary 标记。
            row['status'] = 'recommended'
        if row['status'] in ('action-required', 'recommended'):
            if authorization == 'station':
                why_not.append({'capabilityId': cap['id'], 'reason': '语义审查由工位执行，系统只验证结论工件'})
            elif authorization == 'author-approval':
                pending.append({'capabilityId': cap['id'], 'operation': binding['ref'] if spec else None,
                                'reason': '需作者批准的变更或批准动作'})
            if key not in seen:
                seen.add(key)
                actions.append(row)
        if necessary and evaluation == 'unknown':
            unresolved.append({'capabilityId': cap['id'], 'reason': f"necessary evidence cannot be evaluated: {cap['when']['fact']}"})
    from .outline_facts import inspect_outline
    if outline_facts is None:
        outline_facts = inspect_outline(root, chapterId=chapter_id) if chapter_id and structure else None
    return {'outlineFacts': outline_facts, 'ok': True, 'code': 'TOOL_PLAN', 'task': task, 'chapterId': chapter_id, 'chapterStatus': status,
            'boundary': boundary, 'facts': facts, 'toolActions': actions, 'pendingApprovals': pending,
            'unresolved': unresolved, 'whyNot': why_not, 'policyHash': policy_hash,
            'executed': False}


def _evidence_satisfied(root: Path, cap: dict, chapter_id: str | None, facts: dict, structure) -> bool:
    kind = cap['evidence']
    if kind is None:
        return True
    if not cap['requiredFor']:
        return True
    if kind == 'diagnostics':
        return facts.get('diagnostics.current') == 'true'
    if kind == 'stage_b_review':
        return facts.get('review.current') == 'true'
    return False


def suggested_actions(root: Path, task: str, *, chapter_id: str | None = None, limit: int = 3) -> list[dict]:
    """Compact, read-only action suggestions for doctor/receipt feedback.

    建议携带能力 ID、理由与输入版本，但**不授权执行**——运行反馈只是补救入口，
    权限与批准边界高于任何发现提示。
    """
    try:
        plan = build_tool_plan(root, task, chapter_id=chapter_id)
    except (StructureError, OSError, ValueError, TypeError):
        return []
    facts = {key: plan['facts'].get(key) for key in
             ('project.initialized', 'diagnostics.current', 'review.current', 'summary.stale', 'timeline.hasInversion')}
    rows = []
    for row in plan['toolActions'][:limit]:
        rows.append({'capabilityId': row['capabilityId'], 'reason': row['reason'], 'observed': row['observed'],
                     'binding': row['binding'], 'status': row['status'], 'necessary': row['necessary'],
                     'authorization': row['authorization'], 'chapterId': chapter_id, 'facts': facts})
    if plan['unresolved']:
        rows.append({'capabilityId': 'capability.unresolved', 'reason': '必要事实不可读或能力未接线',
                     'unresolved': plan['unresolved'], 'authorization': None, 'chapterId': chapter_id, 'facts': facts})
    return rows


def _action_arguments(operation: str, *, task: str, chapter_id: str | None,
                      pov_character: str | None, chapter_goal: str, knowledge_mode: str | None,
                      diagnostics: dict | None, held_prepare_ref: str | None = None) -> dict:
    """Route declared inputs; never infer literary parameters from a missing value."""
    args = {'chapterId': chapter_id} if chapter_id else {}
    if operation.startswith('query.'):
        args = {'referencedBy': chapter_id} if chapter_id else {}
    elif operation in ('task.prepare', 'context.read'):
        args['task' if operation == 'task.prepare' else 'taskType'] = task
        args['view'] = 'model'
        args.update({key: value for key, value in {
            'povCharacter': pov_character, 'chapterGoal': chapter_goal,
            'knowledgeMode': knowledge_mode}.items() if value is not None})
        if operation == 'task.prepare' and held_prepare_ref is not None:
            args['heldPrepareRef'] = held_prepare_ref
    elif operation == 'diagnostics.aggregate':
        args = {**(diagnostics or {}), **args}
    elif operation == 'summary.prepare':
        args = {}
    return args


def execute_task_actions(root: Path, task: str, *, chapter_id: str | None = None, authorization: str = 'read',
                         pov_character: str | None = None, chapter_goal: str = '',
                         knowledge_mode: str | None = None, diagnostics: dict | None = None,
                         held_prepare_ref: str | None = None) -> dict:
    """Run ready bounded actions; explicit candidate lifecycle options never grant approval."""
    if authorization not in ('read', 'candidate'):
        raise StructureError('AUTHORIZATION_INVALID', 'authorization must be read or candidate')
    plan = build_tool_plan(root, task, chapter_id=chapter_id)
    if pov_character is None:
        pov_character = (plan.get('outlineFacts') or {}).get('pov')
    # Explicit runner parameters are a request to evaluate those exact inputs,
    # even when a report for different parameters is already fresh.
    if diagnostics is not None and chapter_id and not any(
            row['binding']['ref'] == 'diagnostics.aggregate' for row in plan['toolActions']):
        plan['toolActions'].append({'binding': {'kind': 'operation', 'ref': 'diagnostics.aggregate'},
                                   'capabilityId': 'diagnostics.aggregate', 'status': 'action-required',
                                   'necessary': True, 'dedupeKey': 'explicit-diagnostics'})
    executed, skipped = [], []
    seen = set()
    prepared_index = None
    evidence_registered = False
    for row in plan['toolActions']:
        binding = row['binding']
        if row['status'] not in ('action-required', 'recommended'):
            continue
        if binding['kind'] != 'operation':
            skipped.append({'capabilityId': row['capabilityId'], 'reason': 'skill capabilities are executed by the station'})
            continue
        spec = next((s for s in operations() if s.id == binding['ref']), None)
        if spec is None:
            skipped.append({'capabilityId': row['capabilityId'], 'reason': 'operation is not executable'})
            continue
        if spec.effects in ('protected-write', 'approval', 'undo', 'export-write', 'maintenance', 'plan-write', 'derived-write', 'runtime-write'):
            skipped.append({'capabilityId': row['capabilityId'], 'reason': f"{spec.effects} requires an explicit author-facing command"})
            continue
        if spec.effects == 'candidate-write' and authorization != 'candidate':
            skipped.append({'capabilityId': row['capabilityId'], 'necessary': row['necessary'],
                            'reason': 'candidate-write needs explicit candidate authorization'})
            continue
        if binding['ref'] == 'context.read' and prepared_index is not None:
            skipped.append({'capabilityId': row['capabilityId'], 'coveredBy': 'chapter.prepare',
                            'reason': 'preparation owns this task context and any blockers; use its result'})
            continue
        if pov_character and (binding['ref'].startswith('query.') or binding['ref'] == 'summary.prepare'):
            skipped.append({'capabilityId': row['capabilityId'], 'necessary': row['necessary'],
                            'reason': 'this operation has no POV filter; use the scoped prepare sourceReads'})
            continue
        if binding['ref'].startswith('query.') and prepared_index is not None:
            skipped.append({'capabilityId': row['capabilityId'], 'coveredBy': 'chapter.prepare',
                            'reason': 'use prepared context and its explicit sourceReads; absent references do not prove no related settings'})
            continue
        if row['dedupeKey'] in seen:
            skipped.append({'capabilityId': row['capabilityId'], 'reason': 'duplicate of an executed action'})
            continue
        seen.add(row['dedupeKey'])
        arguments = _action_arguments(spec.id, task=task, chapter_id=chapter_id,
                                      pov_character=pov_character, chapter_goal=chapter_goal,
                                      knowledge_mode=knowledge_mode, diagnostics=diagnostics,
                                      held_prepare_ref=held_prepare_ref)
        required = set(spec.input_schema.get('required', []))
        if spec.id == 'diagnostics.aggregate':
            for field in ('mode',):
                if field not in arguments and (plan.get('outlineFacts') or {}).get(field) is not None:
                    arguments[field] = plan['outlineFacts'][field]
            required.add('mode')
            # The runner resolves a candidate's declared downgrade before using
            # the outline level. Passing the old level here would undo that choice.
            if (plan.get('outlineFacts') or {}).get('intensityLevel') is None:
                required.add('intensityLevel')
        missing = sorted(required - set(arguments) - {'projectRoot'})
        if missing:
            executed.append({'capabilityId': row['capabilityId'], 'operation': spec.id, 'ok': False,
                             'code': 'TOOL_ACTION_INPUT_REQUIRED', 'missing': missing,
                             'error': 'supply the explicit inputs for this action'})
            continue
        try:
            result = call_operation(spec.id, Path(root), arguments)
        except (StructureError, OSError, ValueError, TypeError) as exc:  # noqa: BLE001 - reported, not hidden
            executed.append({'capabilityId': row['capabilityId'], 'operation': spec.id, 'ok': False,
                             'code': getattr(exc, 'code', 'TOOL_EXECUTION_FAILED'), 'error': str(exc),
                             'details': getattr(exc, 'details', {})})
            continue
        if spec.id == 'task.prepare':
            prepared_index = len(executed)
        if spec.id == 'diagnostics.aggregate' and result.get('registered') is True:
            evidence_registered = True
        executed.append({'capabilityId': row['capabilityId'], 'operation': spec.id,
                         'ok': result.get('ok') is True, 'code': result.get('code'), 'result': result})
    # Registration may resolve the original preparation blocker. Re-evaluate once,
    # replacing the stale report instead of returning two copies of its context.
    if evidence_registered and prepared_index is not None:
        arguments = _action_arguments('task.prepare', task=task, chapter_id=chapter_id,
                                      pov_character=pov_character, chapter_goal=chapter_goal,
                                      knowledge_mode=knowledge_mode, diagnostics=None,
                                      held_prepare_ref=held_prepare_ref)
        # The intermediate prepare was never delivered: only the caller's baseline is held.
        try:
            refreshed = call_operation('task.prepare', Path(root), arguments)
            executed[prepared_index] = {'capabilityId': 'chapter.prepare', 'operation': 'task.prepare',
                                       'ok': refreshed.get('ok') is True, 'code': refreshed.get('code'),
                                       'result': refreshed, 'refreshedAfterRegistration': True}
        except (StructureError, OSError, ValueError, TypeError) as exc:
            executed[prepared_index] = {'capabilityId': 'chapter.prepare', 'operation': 'task.prepare',
                                       'ok': False, 'code': getattr(exc, 'code', 'TOOL_EXECUTION_FAILED'),
                                       'error': str(exc)}
    skipped_required = any(row.get('necessary') for row in skipped)
    succeeded = (prepared_index is not None and not plan['unresolved'] and not skipped_required
                 and all(row['ok'] for row in executed))
    prepared = executed[prepared_index].get('result', {}) if prepared_index is not None else {}
    return {'ok': succeeded, 'code': 'TOOL_ACTIONS_EXECUTED' if succeeded else 'TOOL_ACTIONS_INCOMPLETE',
            'task': task, 'chapterId': chapter_id,
            'authorization': authorization, 'executed': executed, 'skipped': skipped,
            'unresolved': plan['unresolved'], 'pendingApprovals': plan['pendingApprovals'],
            'taskReady': succeeded and prepared.get('readiness', {}).get('taskReady') is True,
            'approvalsPerformed': False, 'note': '执行不等于批准；工件批准与结构提交仍需作者批准链'}
