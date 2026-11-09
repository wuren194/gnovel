"""Explicit source rules: scope selection and deterministic registered-event counts."""
from __future__ import annotations

from pathlib import Path

from .source_model import SourceIndex
from .project import source_model_policy

RULE_ACTIONS = tuple(source_model_policy()['ruleActions'])
RULE_REQUIRED = {'version', 'method', 'enforcement', 'scopeIds', 'actions', 'inputIds'}


def validate_rules(source: dict, index: SourceIndex) -> list[dict]:
    policy = source_model_policy()
    errors = []
    for row in source['entities']:
        spec = row.get('ruleSpec')
        if 'ruleSpec' not in row:
            continue
        path = 'entities.' + row['entityId'] + '.ruleSpec'
        def invalid(message):
            errors.append({'code': 'SOURCE_RULE_INVALID', 'path': path, 'message': message})
        required = RULE_REQUIRED
        if row['entityType'] != 'rule' or not isinstance(spec, dict) or set(spec) - (required | {'evaluation', 'exceptionsAllowed'}) or required - set(spec):
            invalid('invalid rule fields'); continue
        if (not isinstance(spec['version'], str) or not spec['version'].strip()
                or spec['method'] not in policy['ruleMethods']
                or spec['enforcement'] not in policy['ruleEnforcements']
                or 'exceptionsAllowed' in spec and type(spec['exceptionsAllowed']) is not bool):
            invalid('invalid version, method or enforcement')
        for key, allowed in [('scopeIds', index.objects), ('inputIds', index.records), ('actions', RULE_ACTIONS)]:
            values = spec[key]
            if (not isinstance(values, list) or not values
                    or any(not isinstance(v, str) or v not in allowed for v in values)
                    or len(values) != len(set(values))):
                invalid('invalid ' + key)
        evaluation = spec.get('evaluation')
        if spec['method'] == 'semantic':
            if evaluation is not None:
                invalid('semantic rules cannot claim mechanical evaluation')
            continue
        if (not isinstance(evaluation, dict)
                or set(evaluation) != {'kind', 'eventIds', 'limitValueId', 'complete'}
                or evaluation.get('kind') != 'count_at_most'
                or type(evaluation.get('complete')) is not bool):
            invalid('invalid count evaluator'); continue
        events = evaluation['eventIds']
        if (not isinstance(events, list)
                or any(not isinstance(v, str) or index.owner_types.get(v) != 'event' for v in events)
                or len(events) != len(set(events))):
            invalid('eventIds must be unique registered successful-use events')
        limit_id = evaluation['limitValueId']
        if not isinstance(limit_id, str) or limit_id not in index.values:
            invalid('limitValueId must reference a source value')
        else:
            value = index.values[limit_id]
            if value.get('state', 'known') == 'known' and (type(value.get('value')) is not int or value['value'] < 0):
                invalid('count limit must be a nonnegative integer or explicitly unknown')
    return errors


def applicable_rules(source: dict, scope_ids: set[str], *, action: str | None = None, rules=None) -> list[dict]:
    return [row for row in (source['entities'] if rules is None else rules)
            if row.get('entityType') == 'rule' and row.get('status') != 'archived'
            and row.get('ruleSpec') and scope_ids.intersection(row['ruleSpec']['scopeIds'])
            and (action is None or action in row['ruleSpec']['actions'])]


def rule_inputs(row: dict) -> set[str]:
    spec = row['ruleSpec']
    result = set(spec['inputIds'])
    evaluation = spec.get('evaluation')
    if evaluation:
        result.update(evaluation['eventIds'])
        result.add(evaluation['limitValueId'])
    return result


def evaluate_rule(rule: dict, inputs, *, root: Path | None = None,
                  chapter_id: str | None = None, action: str | None = None) -> dict:
    spec = rule['ruleSpec']
    status, evidence = 'pending', {'reason': 'App semantic review required'}
    issue = None
    if root is not None and chapter_id is not None and action is not None:
        from .source_rule_issues import current_issue
        issue = current_issue(root, inputs.source, rule, chapter_id, action,
                              inputs.fingerprint(rule['entityId']))
    if spec['method'] == 'mechanical':
        ev = spec['evaluation']; value = inputs.index.values[ev['limitValueId']]
        count = len(ev['eventIds'])
        evidence = {'eventIds': ev['eventIds'], 'registeredCount': count,
                    'limitValueId': ev['limitValueId'], 'complete': ev['complete']}
        if value.get('state', 'known') == 'known':
            limit = value['value']; evidence['limit'] = limit
            status = 'conflict' if count > limit else ('passed' if ev['complete'] else 'pending')
        else:
            evidence['reason'] = 'count limit is unknown'
        if issue and issue['current'] and issue['status'] == 'exception':
            evidence = {**evidence, 'exception': {'issueId': issue['issueId'],
                        'receiptId': issue['receiptId'], 'reason': issue['reason']}}
            status = 'passed'
    elif issue is not None:
        if issue and issue['current']:
            status = {'resolved': 'passed', 'exception': 'passed', 'confirmed': 'conflict',
                      'deferred': 'deferred', 'pending': 'pending'}[issue['status']]
            evidence = {'issueId': issue['issueId'], 'receiptId': issue['receiptId'],
                        'issueStatus': issue['status'], 'reason': issue['reason'],
                        'deferredUntilScopeIds': issue['deferredUntilScopeIds']}
    return {'ruleId': rule['entityId'], 'version': spec['version'], 'method': spec['method'],
                     'enforcement': spec['enforcement'], 'status': status,
                     'inputHash': inputs.fingerprint(rule['entityId']), 'evidence': evidence,
                     'issueId': issue['issueId'] if issue else None,
                     'blocking': spec['enforcement'] == 'required' and status != 'passed'}


def evaluate_rules(source: dict, chapter_id: str, action: str, *, root: Path | None = None,
                   _inputs=None, _plan: dict | None = None) -> dict:
    from .source_plan import chapter_plan
    from .source_impact import ReviewInputs
    from .structure_store import StructureError
    if action not in RULE_ACTIONS:
        raise StructureError('SOURCE_RULE_ACTION_INVALID', 'unsupported rule action')
    # A caller checking every action of one chapter passes the inputs and plan it
    # already built from this same source instead of rebuilding them per action.
    inputs = _inputs or ReviewInputs(source)
    plan = _plan or chapter_plan(source, chapter_id, _inputs=inputs)
    if plan['chapterId'] != chapter_id:
        raise ValueError('rule plan belongs to another chapter')
    scope = set(plan['recipe']['ruleScopeIds'])
    rows = [evaluate_rule(rule, inputs, root=root, chapter_id=chapter_id, action=action)
            for rule in applicable_rules(source, scope, action=action)]
    rows.sort(key=lambda r: r['ruleId'])
    return {'chapterId': chapter_id, 'action': action, 'items': rows,
            'ready': not any(row['blocking'] for row in rows), 'snapshot': plan['snapshot'],
            'coverage': {'registeredRulesOnly': True, 'proseScanned': False,
                         'semanticReceiptsSupported': True}}


def query_rules(root: Path, *, chapterId: str, action: str, limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_review import source_snapshot, bounded_page
    result = evaluate_rules(source_snapshot(root), chapterId, action, root=root)
    return bounded_page(result['items'], snapshot=result['snapshot'],
                        selection={'chapterId': chapterId, 'action': action}, code='SOURCE_RULES',
                        limit=limit, maxBytes=maxBytes, cursor=cursor, ready=result['ready'],
                        coverage=result['coverage'])


def require_rules(source: dict, chapter_id: str, action: str, *, root: Path | None = None) -> dict:
    from .structure_store import StructureError
    result = evaluate_rules(source, chapter_id, action, root=root)
    if not result['ready']:
        raise StructureError('SOURCE_RULES_BLOCKED', 'applicable required rules have unresolved checks',
                             {'chapterId': chapter_id, 'action': action,
                              'ruleIds': [row['ruleId'] for row in result['items'] if row['blocking']],
                              'query': {'tool': 'query_source_rules', 'chapterId': chapter_id, 'action': action}})
    return result
