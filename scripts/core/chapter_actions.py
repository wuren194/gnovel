"""Typed chapter actions use OperationSpec, not executable shell suggestions."""
from copy import deepcopy


def describe_action(root, operation, arguments, *, needs_approval=False, semantic_inputs=()):
    from .operations import operations, validate_arguments
    specs = {spec.id: spec for spec in operations()}
    spec = specs.get(operation)
    missing = list(semantic_inputs)
    if spec is None:
        return {'operation': None, 'arguments': {}, 'missingInputs': missing,
                'needsApproval': needs_approval, 'execution': 'semantic_input_required', 'executable': False}
    args = {'projectRoot': str(root), **deepcopy(arguments)}
    required = set(spec.input_schema.get('required', []))
    missing += sorted(required - args.keys())
    # Validate supplied fields even when the action deliberately lacks semantic inputs.
    schema = {**spec.input_schema, 'required': sorted(required & args.keys())}
    validate_arguments(schema, args)
    approval = needs_approval or spec.effects in {'approval', 'undo', 'maintenance', 'plan-write'}
    execution = ('semantic_input_required' if missing else 'author_approval_required' if approval
                 else 'read_only' if spec.effects == 'read' else 'explicit_authorization_required')
    return {'operation': operation, 'arguments': args, 'missingInputs': list(dict.fromkeys(missing)),
            'needsApproval': approval, 'effects': spec.effects, 'execution': execution,
            'executable': execution == 'read_only'}


def blocker_route(blocker, chapter_id):
    code, details = blocker['code'], blocker.get('details', {})
    if code == 'SOURCE_RULES_BLOCKED':
        return 'source.rules', {'chapterId': chapter_id, 'action': 'publish'}, ()
    if code == 'TRANSACTION_INCOMPLETE':
        return 'project.workflow.recover', {'apply': False}, ()
    if code in {'STAGE_A_NOT_ADOPTED', 'STAGE_A_OUTLINE_MISMATCH'}:
        return 'task.prepare', {'chapterId': chapter_id, 'task': 'planner', 'view': 'model'}, ()
    if code in {'DIAGNOSTICS_NOT_PASSED', 'STAGE_B_NOT_READY'}:
        return 'workflow.repair-context', {'chapterId': chapter_id}, ()
    if code == 'TITLE_DECISION_REQUIRED':
        return 'project.workflow.chapter-receipt', {'chapterId': chapter_id}, ()
    if code == 'FINAL_ARTIFACTS_MISSING':
        missing = set(details.get('missing', []))
        task = ('planner' if 'stage_a_review' in missing else 'writer' if 'candidate_prose' in missing
                else 'editor' if missing & {'diagnostics', 'stage_b_review'} else 'titler' if 'title_candidates' in missing
                else 'continuity')
        return 'task.prepare', {'chapterId': chapter_id, 'task': task, 'view': 'model'}, ()
    if code.startswith('ARTIFACT_') and code not in {'ARTIFACT_APPROVAL_HASH_MISMATCH'}:
        return 'task.prepare', {'chapterId': chapter_id, 'task': 'editor', 'view': 'model'}, ()
    return None, {}, (f'resolution:{code}',)
