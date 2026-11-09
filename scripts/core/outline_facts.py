"""Read-only outline arithmetic and exact entity resolution, not plan approval."""
import re
from .structure_store import StructureError, load_structure






def parse_beat_budgets(text):
    beats, header, in_section, fence = [], None, False, False
    for line in text.splitlines():
        if re.match(r'^\s*(```|~~~)', line):
            fence = not fence
            continue
        if fence: continue
        if re.match(r'^#{1,6}\s', line):
            in_section = '节奏段' in line
            header = None
        if not in_section or not line.strip().startswith('|'): continue
        cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
        if '节奏段' in cells and '字数' in cells:
            header = cells
        elif header and cells and re.fullmatch(r'B\d+', cells[0]):
            value = cells[header.index('字数')] if len(cells) == len(header) else ''
            beats.append({'id': cells[0], 'budget': int(value) if re.fullmatch(r'\d+', value) else None})
    return beats


def inspect_outline(root, *, chapterId, structure=None):
    root = root.expanduser().resolve()
    source = load_structure(root) if structure is None else structure
    chapter = next((r for r in source['chapters'] if r['chapterId'] == chapterId), None)
    if chapter is None:
        raise StructureError('CHAPTER_NOT_FOUND', 'unknown outline chapter')
    from .chapter_contract import source_contract
    facts = source_contract(source, chapterId)
    from .outline_constraints import inspect_project_levels
    from .chapter_contract import outline_text
    facts['projectConstraints'] = inspect_project_levels(root, source, chapter,
        outline_text(root, facts, structure=source), facts.get('intensityLevel'))
    return facts


def require_mode_level_review(root, structure, chapter_id, payload):
    """Mode and a high ceiling can coexist, but their intended use needs an explicit review."""
    from .writing_checks import text_module
    from .review_artifacts import require_completed_text
    facts = inspect_outline(root, chapterId=chapter_id, structure=structure)
    if payload.get('decision') in {'adopt', 'modify'} and facts.get('issues'):
        raise StructureError('STAGE_A_OUTLINE_INVALID', '结构化推演的定位、篇幅合同或放慢余量尚未完整', {'issues': facts['issues']})
    floor = text_module('intensity_registry').degradation_floor()['minLevel']
    if (payload.get('decision') in {'adopt', 'modify'} and facts.get('mode') in {'plot', 'buildUp'}
            and facts.get('intensityLevel') is not None and facts['intensityLevel'] >= floor):
        try:
            require_completed_text(payload.get('modeLevelReason'), 'modeLevelReason')
        except ValueError as exc:
            raise StructureError('OUTLINE_MODE_LEVEL_REVIEW_REQUIRED',
                '高档上限与铺垫模式须说明：本章预期强度、适用段与不采用地板的理由；组合本身不自动非法') from exc


def plan_readiness(root, structure, chapter_id):
    """Reuse actual plan validators without filling a review or blocking drafts."""
    facts = inspect_outline(root, chapterId=chapter_id, structure=structure)
    issues = [{'code': code, 'source': 'chapterContract'} for code in facts.get('issues', [])]
    if not facts.get('available'):
        issues.append({'code': 'SOURCE_CHAPTER_PLAN_MISSING', 'source': 'chapterContract'})
    from .behavior_review import plan_coverage_issues
    issues.extend(plan_coverage_issues(structure, chapter_id))
    return {'readyForCompletionReview': not issues, 'issues': issues,
            'semanticReviewComplete': False, 'draftSaveAllowed': True,
            'notChecked': ['seat_conclusions', 'literary_judgment', 'author_approval'],
            'readAction': {'operation': 'source.chapter-plan', 'arguments': {'chapterId': chapter_id, 'view': 'detail'}},
            'instruction': '这些是当前计划可独立检测的缺项；先通过原源事务修订需要改变的计划，再实际审查。空表不授予完成信用。'}
