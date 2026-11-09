"""Explicit source-plan contracts and bounded extraction of existing plan text.

No semantic inference: only local chapter plans, declared numbers and labelled
requirements are consumed. Ambiguous ranges are reported, never guessed.
"""
import re
from copy import deepcopy

FIELDS = {'targetRange', 'beats', 'pov', 'mode', 'intensityLevel', 'requirements',
          'quotedTargets', 'slowdownReserve', 'relationshipCausality', 'behaviorCoverage', 'livingArrangements'}
REQUIRED_FIELDS = {'targetRange'}
MODES = ('plot', 'buildUp', 'mixed', 'peakScene')


def contract_schema():
    """Public input shape; cross-field, reference and semantic checks stay local."""
    from .behavior_review import coverage_schema
    text = {'type': 'string', 'minLength': 1}
    texts = {'type': 'array', 'items': text}
    properties = {
        'targetRange': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1},
                        'minItems': 2, 'maxItems': 2,
                        'description': '汉字数下限、上限；上限不得小于下限。'},
        'beats': {'type': 'array', 'items': {
            'type': 'object', 'properties': {'id': {**text, 'pattern': r'^B\d+$'},
                                            'budget': {'type': 'integer', 'minimum': 1}},
            'required': ['id', 'budget'], 'additionalProperties': False},
            'description': '节奏段ID不得重复；实际预算仍由章节合同校验。'},
        'pov': text, 'mode': {'type': 'string', 'enum': list(MODES)},
        'intensityLevel': {'type': 'integer', 'description': '取当前张力等级注册表允许的等级。'},
        'requirements': texts, 'quotedTargets': texts, 'slowdownReserve': text,
        'behaviorCoverage': coverage_schema(),
        'relationshipCausality': {'type': 'array', 'items': {'type': 'object'},
            'description': '完整字段与依赖要求见source.schema的chapterContract.relationshipCausality；领域校验继续执行。'},
        'livingArrangements': {'type': 'array', 'items': {'type': 'object'},
            'description': '完整字段与依赖要求见source.schema的chapterContract.livingArrangements；领域校验继续执行。'},
    }
    return {'type': 'object', 'properties': properties, 'required': sorted(REQUIRED_FIELDS),
            'additionalProperties': False,
            'description': '当前章节计划合同的基础输入形状；交叉字段、资料引用和语义审查仍由各自领域规则负责。'}


def contract_error_details(code):
    """Explain known structural failures without changing or approving input."""
    if code == 'CHAPTER_CONTRACT_MODE_INVALID':
        return {'field': 'mode', 'allowed': list(MODES),
                'schemaAction': {'operation': 'source.schema', 'arguments': {}},
                'instruction': '使用允许的模式值；修改已登记计划须经源预览与批准提交，之后按新材料重新审查。'}
    if code == 'BEHAVIOR_COVERAGE_INVALID':
        from .behavior_review import coverage_error_details
        return coverage_error_details()
    return {}


def validate_contract(value):
    if not isinstance(value, dict) or set(value) - FIELDS or REQUIRED_FIELDS - set(value):
        return ['CHAPTER_CONTRACT_FIELDS_INVALID']
    errors = []
    interval = value['targetRange']
    if (not isinstance(interval, list) or len(interval) != 2
            or any(type(n) is not int for n in interval) or not 0 < interval[0] <= interval[1]):
        errors.append('CHAPTER_CONTRACT_RANGE_INVALID')
    beats = value.get('beats', [])
    if (not isinstance(beats, list) or any(not isinstance(row, dict) or set(row) != {'id', 'budget'}
            or not isinstance(row['id'], str) or not re.fullmatch(r'B\d+', row['id'])
            or type(row['budget']) is not int or row['budget'] <= 0 for row in beats)):
        errors.append('CHAPTER_CONTRACT_BEATS_INVALID')
    elif len({row['id'] for row in beats}) != len(beats):
        errors.append('CHAPTER_CONTRACT_BEATS_DUPLICATE')
    for key in ('requirements', 'quotedTargets'):
        if key in value and (not isinstance(value[key], list)
                or any(not isinstance(t, str) or not t.strip() for t in value[key])):
            errors.append('CHAPTER_CONTRACT_TEXT_INVALID:' + key)
    for key in ('pov', 'slowdownReserve'):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            errors.append('CHAPTER_CONTRACT_TEXT_INVALID:' + key)
    if 'mode' in value and value['mode'] not in MODES:
        errors.append('CHAPTER_CONTRACT_MODE_INVALID')
    if 'intensityLevel' in value:
        from importlib import import_module
        registry = import_module('scripts.intensity_registry' if __package__.startswith('scripts.') else 'intensity_registry')
        low, high = registry.level_bounds()
        if type(value['intensityLevel']) is not int or not low <= value['intensityLevel'] <= high:
            errors.append('CHAPTER_CONTRACT_LEVEL_INVALID')
    if 'relationshipCausality' in value:
        from .relationship_causality import validate_declarations
        errors.extend(validate_declarations(value['relationshipCausality']))
    from .behavior_review import validate_coverage, validate_arrangements
    if 'behaviorCoverage' in value and not validate_coverage(value['behaviorCoverage']):
        errors.append('BEHAVIOR_COVERAGE_INVALID')
    errors.extend(validate_arrangements(value.get('livingArrangements', [])))
    return errors


def source_contract(source, chapter_id):
    from .writing_checks import outline_requirements
    from .outline_links import _table_rows, _label_index
    from .source_plan import chapter_plan
    from .project import chapter_length_bounds
    local = sorted((f for f in source.get('fragments', []) if f['ownerId'] == chapter_id
                    and f['contentRole'] == 'plan' and f.get('status') != 'archived'),
                   key=lambda f: (f['order'], f['fragmentId']))
    text = '\n\n'.join(f['text'] for f in local)
    contracts = [f['chapterContract'] for f in local if 'chapterContract' in f]
    issues = []
    if len(contracts) > 1:
        issues.append('CHAPTER_CONTRACT_AMBIGUOUS')
    explicit = contracts[0] if len(contracts) == 1 else {}
    fields = dict(_table_rows(text, section='本章定位'))
    ranges = set()
    if not contracts:
        pattern = r'(\d+)\s*[-–—~～至]\s*(\d+)\s*(?:个)?(?:汉字|字)'
        ranges.update((int(m[1]), int(m[2])) for m in re.finditer(pattern, text))
        field = re.fullmatch(r'\s*(\d+)\s*[-–—~～至]\s*(\d+)\s*(?:汉字|字)?', fields.get('字数', ''))
        if field:
            ranges.add((int(field[1]), int(field[2])))
    if len(ranges) > 1:
        issues.append('CHAPTER_CONTRACT_AMBIGUOUS')
    interval = explicit.get('targetRange') or (list(next(iter(ranges))) if len(ranges) == 1 else None)
    if interval is not None and not 0 < interval[0] <= interval[1]:
        issues.append('CHAPTER_CONTRACT_RANGE_INVALID'); interval = None
    beats = explicit.get('beats', [])
    if not contracts:
        from .outline_facts import parse_beat_budgets
        beats = parse_beat_budgets(text)
        if not beats:
            beats = [{'id': m[1], 'budget': int(m[2])} for m in re.finditer(
                r'(?m)^\s*(B\d+)\s*[，,:：]?\s*(\d+)\s*(?:汉字|字)?\s*[：:,，]', text)]
    if any(type(r['budget']) is not int or r['budget'] <= 0 for r in beats):
        issues.append('CHAPTER_CONTRACT_BEATS_INVALID')
    if len({r['id'] for r in beats}) != len(beats):
        issues.append('CHAPTER_CONTRACT_BEATS_DUPLICATE')
    total = sum(r['budget'] for r in beats) if beats and all(type(r['budget']) is int for r in beats) else None
    if interval and total is not None and not interval[0] <= total <= interval[1]:
        issues.append('OUTLINE_BEAT_BUDGET_OUTSIDE_RANGE')
    requirements = outline_requirements(text)
    if 'requirements' in explicit:
        requirements['requirements'] = [{'id': f'R{i:02}', 'text': v} for i, v in enumerate(explicit['requirements'], 1)]
    if 'quotedTargets' in explicit:
        requirements['quotedTargets'] = explicit['quotedTargets']
    labels = _label_index(source)
    pov = explicit.get('pov', fields.get('POV'))
    pov = labels.get(str(pov).casefold(), pov)
    if pov not in {e['entityId'] for e in source.get('entities', []) if e['entityType'] == 'character'}:
        pov = None
    mode = explicit.get('mode', fields.get('模式'))
    if mode not in MODES:
        mode = None
    dirty = explicit.get('intensityLevel', fields.get('张力等级'))
    dirty = int(dirty) if isinstance(dirty, str) and dirty.isdigit() else dirty
    return {'applicable': True, 'available': bool(local), 'sourceMode': True,
            'path': '.govel/structure.json', 'planFragmentIds': [f['fragmentId'] for f in local],
            'sourcePlanHash': chapter_plan(source, chapter_id)['sourcePlanHash'],
            'positionFieldsPresent': bool(fields),
            'contractSource': 'chapterContract' if contracts else 'explicit_plan_text',
            'targetRange': interval, 'policyRange': list(chapter_length_bounds()),
            'evaluationStatus': 'ready' if interval and not issues else 'not_evaluated',
            'beats': beats, 'beatBudgetSum': total, 'pov': pov, 'mode': mode,
            'intensityLevel': dirty if type(dirty) is int else None,
            'relationshipCausality': deepcopy(explicit.get('relationshipCausality', [])),
            'behaviorCoverage': deepcopy(explicit.get('behaviorCoverage', {'relationship': 'unassessed', 'living': 'unassessed'})),
            'livingArrangements': deepcopy(explicit.get('livingArrangements', [])),
            'requirements': requirements, 'issues': issues, 'valid': bool(interval) and not issues,
            'semanticReviewComplete': False}


def outline_text(root, facts, *, structure=None):
    from .structure_store import load_structure
    source = load_structure(root) if structure is None else structure
    by_id = {f['fragmentId']: f['text'] for f in source['fragments']}
    return '\n\n'.join(by_id[rid] for rid in facts['planFragmentIds'])
