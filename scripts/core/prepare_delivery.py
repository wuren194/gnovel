"""Opt-in preparation deltas require the caller to affirm a held baseline."""
from copy import deepcopy

from .prepare_dependencies import validate_dependencies
from .structure_store import StructureError

ALWAYS = {'ok', 'code', 'task', 'projectId', 'chapterId', 'prepareRef', 'readiness', 'missing', 'notes',
          'scope', 'nextAction', 'workflowNextAction', 'actionReadiness', 'pendingContinuity', 'authorization'}


def model_delivery(root, report, held_reference, previous):
    from .context_transport import task_model_response
    current = task_model_response(report)

    def full(reason):
        return {**current, 'delivery': {'mode': 'full', 'reason': reason, 'baselineUsed': False}}

    if previous is None:
        return full('baseline_snapshot_unavailable')
    old = deepcopy(previous['report'])
    if old.get('projectId') is None or old.get('projectId') != report.get('projectId'):
        return full('project_identity_changed_or_legacy_baseline')
    old_scope = previous['handoff']['scope']
    from .reading_handoff import decode_receipt
    new_scope = decode_receipt(report['reading']['credential'], root)['scope']
    if old_scope['arguments'] != new_scope['arguments'] or old_scope['scope'] != new_scope['scope']:
        return full('task_or_pov_scope_changed')
    if not isinstance(previous.get('model'), dict):
        return full('baseline_projection_unavailable')
    baseline = deepcopy(previous['model'])
    baseline['reading']['credential'] = held_reference
    baseline['prepareRef'] = held_reference
    changed_inputs = []
    try:
        validate_dependencies(root, previous.get('dependencies'), code='PREPARE_BASELINE_CHANGED')
    except StructureError as exc:
        changed_inputs = exc.details.get('changedItems', [])
        if not changed_inputs:
            return full('baseline_dependencies_unavailable')
    unchanged = sorted(key for key in current if key not in ALWAYS and key in baseline and current[key] == baseline[key])
    removed = sorted(set(baseline) - set(current))
    return {**{key: value for key, value in current.items() if key not in unchanged},
            'delivery': {'mode': 'delta', 'baseRef': held_reference, 'baselineUsed': True,
                         'unchangedFields': unchanged, 'removedFields': removed, 'changedInputs': changed_inputs,
                         'merge': 'replace supplied fields, retain unchanged fields, remove removedFields; use the current prepareRef for reading'}}
