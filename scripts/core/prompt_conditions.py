"""Three-state conditional contract evaluation with explicit missing-input evidence."""
from __future__ import annotations

import re
from typing import Any

from .artifacts import sha256_json


class PromptPlanError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _parts(field: str) -> list[str]:
    if not isinstance(field, str) or not re.fullmatch(r'\w+(?:\[\])?(?:\.\w+(?:\[\])?)*', field):
        raise PromptPlanError('PROMPT_CONDITION_INVALID', 'condition field must be a structured path')
    parts = field.split('.')
    return parts[1:] if parts[0] == 'design' else parts


def _walk(value: Any, parts: list[str], path: str, missing: list) -> list[Any]:
    if not parts:
        if value is None:
            missing.append({'path': path, 'reason': 'unknown'})
            return []
        return [value]
    part, rest = parts[0], parts[1:]
    key = part.removesuffix('[]')
    current = '.'.join(filter(None, [path, key]))
    if not isinstance(value, dict):
        raise PromptPlanError('PROMPT_INPUT_INVALID', 'condition traverses a non-object', {'path': path})
    if key not in value:
        missing.append({'path': current, 'reason': 'missing'})
        return []
    if part.endswith('[]'):
        items = value[key]
        if not isinstance(items, list):
            raise PromptPlanError('PROMPT_INPUT_INVALID', 'condition requires an array', {'path': current})
        if not items:
            missing.append({'path': current, 'reason': 'empty_unassessed_collection'})
        result = []
        for number, item in enumerate(items):
            result.extend(_walk(item, rest, f'{current}[{number}]', missing))
        return result
    return _walk(value[key], rest, current, missing)


def resolve_field(design: dict, field: str) -> list[Any]:
    return _walk(design, _parts(field), '', [])


def _pair_condition(design: dict, expected: Any) -> tuple[list, list, dict]:
    from .pair_classification import validate_pairs, pair_inputs
    from .project import toolbox_manifest
    if expected not in toolbox_manifest()['classification']['visualPolarity']:
        raise PromptPlanError('PROMPT_CONDITION_INVALID', 'unknown visual polarity condition value')
    errors = validate_pairs(design, check_endpoints=False)
    if errors:
        raise PromptPlanError('PROMPT_INPUT_INVALID', 'pair classification inputs are invalid', {'errors': errors})
    scope = design.get('pairScopeIds')
    inputs = pair_inputs(design, scope_ids=set(scope) if scope is not None else None)
    missing = []
    if inputs['assessment']['status'] == 'unassessed':
        missing.append({'path': 'pairAssessment', 'reason': 'unassessed_inventory'})
    observed = []
    for pair in inputs['pairs']:
        state = pair.get('visualPolarityState', 'unassessed')
        if state in ('unassessed', 'unknown'):
            missing.append({'path': f"pairs.{pair['pairId']}.visualPolarity", 'reason': state,
                            'pairId': pair['pairId']})
        elif state == 'known':
            observed.append(pair['visualPolarity'])
    return observed, missing, inputs


def evaluate_condition(design: dict, condition: dict) -> tuple[bool | None, dict]:
    if not isinstance(condition, dict) or set(condition) != {'field', 'op', 'value'}:
        raise PromptPlanError('PROMPT_CONDITION_INVALID', 'condition requires field, op and value')
    field, op, expected = condition['field'], condition['op'], condition['value']
    if op not in {'eq', 'includes'}:
        raise PromptPlanError('PROMPT_OPERATOR_UNSUPPORTED', f'unsupported prompt condition operator: {op}')
    parts = _parts(field)
    missing = []
    if parts == ['pairs[]', 'visualPolarity']:
        if op != 'eq':
            raise PromptPlanError('PROMPT_CONDITION_INVALID', 'visual polarity requires exact enum equality')
        values, missing, inputs = _pair_condition(design, expected)
    else:
        values = _walk(design, parts, '', missing)
        inputs = {'field': field, 'values': values, 'missing': missing}
        if parts == ['chapter', 'arc'] and any(not isinstance(value, list) or
                any(not isinstance(label, str) for label in value) for value in values):
            raise PromptPlanError('PROMPT_INPUT_INVALID', 'chapter.arc must be an array of labels')
    if op == 'eq':
        positive = any(value == expected for value in values)
    else:
        if any(not isinstance(value, (list, str)) for value in values):
            raise PromptPlanError('PROMPT_INPUT_INVALID', 'includes requires a string or array')
        positive = any(expected in value for value in values)
    # Known positive evidence still loads the contract; missing siblings remain visible
    # and keep the station incomplete instead of being swallowed by existential matching.
    matched = True if positive else None if missing else False
    status = 'unresolved' if missing else 'matched' if positive else 'not_matched'
    return matched, {'field': field, 'op': op, 'expected': expected, 'observed': values,
                     'matched': matched, 'status': status, 'unresolvedInputs': missing,
                     'inputs': inputs, 'inputHash': sha256_json(inputs)}


def chapter_condition_design(source: dict, chapter_id: str) -> dict:
    """The same pure source projection feeds plan bindings and prompt loading."""
    from .pair_classification import chapter_scope
    scope = chapter_scope(source, chapter_id)
    chapter = next(row for row in source['chapters'] if row['chapterId'] == chapter_id)
    arcs = [row for row in source.get('arcs', []) if row['arcId'] in scope]
    labels = set()
    for arc in arcs:
        tags = arc.get('tags', [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise PromptPlanError('PROMPT_INPUT_INVALID', 'arc tags must be an array of labels', {'arcId': arc['arcId']})
        labels.update(label for label in [arc.get('title', ''), *tags] if label)
    return {**source, 'chapter': {**chapter, 'arc': sorted(labels)}, 'pairScopeIds': sorted(scope)}


def chapter_condition_binding(source: dict, chapter_id: str) -> dict:
    """Bind only loaded conditional contract bytes; negative decisions bind their inputs."""
    from .project import TOOLBOX_ROOT, toolbox_manifest
    from .static_cache import cached_sha256
    design = chapter_condition_design(source, chapter_id)
    rows = []
    for station, plan in sorted(toolbox_manifest()['promptPlan'].items()):
        if not isinstance(plan, dict):
            continue
        for entry in plan.get('conditional', []):
            matched, evidence = evaluate_condition(design, entry['when'])
            row = {'station': station, 'contract': entry['contract'], 'when': entry['when'],
                   'reason': entry.get('reason', ''), 'inputHash': evidence['inputHash'],
                   'status': evidence['status'], 'matched': matched}
            if matched:
                path = TOOLBOX_ROOT / 'shared/contracts' / entry['contract']
                if not path.is_file():
                    raise PromptPlanError('PROMPT_PATH_MISSING', 'conditional contract is missing')
                row['contractHash'] = cached_sha256(path)
            rows.append(row)
    definition_paths = ('scripts/core/prompt_conditions.py', 'scripts/core/pair_classification.py')
    from .data_provider import binding
    return {'conditions': rows, 'provider': binding(), 'definitionHash': sha256_json(
        {name: cached_sha256(TOOLBOX_ROOT / name) for name in definition_paths})}
