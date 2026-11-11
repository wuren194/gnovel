"""Read-only writing checks, with explicit separation of lexical evidence and review."""
from __future__ import annotations

import re
from importlib import import_module

from .artifacts import sha256_text
from .project import diagnostic_policy
from .structure_store import StructureError, load_structure
from .write_sets import safe_project_path


def text_module(name):
    return import_module(('scripts.' if __package__.startswith('scripts.') else '') + name)


def rules_card(*, mode, intensityLevel, strictFloor=False):
    validator = text_module('text.validator')
    registry = text_module('intensity_registry')
    low, high = registry.level_bounds()
    if mode not in ('plot', 'buildUp', 'mixed', 'peakScene') or type(intensityLevel) is not int or not low <= intensityLevel <= high:
        raise StructureError('DIAGNOSTICS_INPUT_INVALID', 'valid mode and intensityLevel required')
    _, checks, _ = validator.build_checks(mode, strictFloor, intensityLevel, strict_dialogue_floor=strictFloor)
    policy = diagnostic_policy()
    patterns = validator.load_text_patterns()
    ai_patterns = text_module('text.ai_patterns')
    floor = registry.degradation_floor()
    thresholds = {'S-10s': policy['sentenceLength'],
                  'S-09d': {k: v for k, v in policy['sensoryDensity'].items() if k != 'channels'},
                  'S-08': policy['paragraphRatio'], 'S-06': policy['dialogueDynamics'],
                  'S-09b': {k: floor[k] for k in ('minLevel', 'thresholds', 'level6')}, 'S-16': patterns['adverb_patch'],
                  'S-17': patterns['transition'], 'S-18': patterns['word_density'],
                  'S-19': patterns['adjective_stack'], 'S-09': policy['sceneDynamics']}
    rows = []
    for name, _ in checks:
        rid = validator.RULE_ID_BY_NAME[name]
        severity = 'error'
        if rid == 'S-08' or rid in {'S-06', 'S-09b'} and not strictFloor:
            severity = 'info'
        if rid == 'S-10s': severity = policy['sentenceLength']['lengthSeverity'] + '; unpunctuated=error'
        if rid == 'S-09d': severity = policy['sensoryDensity']['severity']
        if rid == 'S-10q': severity = policy['proseQuality']['severity']
        if rid == 'S-10':
            severity = 'error' if any(ai_patterns.finding_policy(rule)['gate'] for rule in
                                     [*ai_patterns.pattern_rules(patterns), *patterns['ai'].get('clusters', [])]
                                     if mode.lower() not in {v.lower() for v in rule.get('exemptModes', [])}) else 'info'
        row = {'ruleId': rid, 'name': name, 'severity': severity,
               'scope': 'selected paragraphs when sceneParagraphs supplied; otherwise declared scene' if name in validator.ADULT_CHECK_NAMES else 'chapter',
               'guidance': validator.FIX_HINT_BY_ID.get(rid, '')}
        enabled = (floor['enabled'] if rid == 'S-09b' else
                   policy['dialogueDynamics']['enabled'] if rid == 'S-06' else
                   policy['sceneDynamics']['enabled'] if name in validator.ADULT_CHECK_NAMES else True)
        if not enabled:
            row.update(applicability='not_applicable', severity='not_applicable')
        if rid in thresholds:
            row['thresholds'] = thresholds[rid]
            row.pop('guidance', None)
        rows.append(row)
    for rule in [*ai_patterns.pattern_rules(patterns), *patterns['ai'].get('clusters', [])]:
        if mode.lower() in {v.lower() for v in rule.get('exemptModes', [])}: continue
        rows.append({**{k: rule[k] for k in ('ruleId', 'name', 'pattern', 'scope', 'perParagraphLimit', 'rewriteHint',
                     'minMatches', 'minPerKHan', 'windowHan', 'endingWindowHan', 'members') if k in rule},
                     **ai_patterns.finding_policy(rule)})
    return {'ok': True, 'code': 'DIAGNOSTIC_RULES', 'mode': mode, 'intensityLevel': intensityLevel,
            'implementationSource': 'scripts/text/validator.py',
            'strictFloor': strictFloor, 'rules': rows,
            'ceilingWords': list(registry.ceiling_violations(intensityLevel)),
            'ambiguousCeilingWords': list(registry.ambiguous_ceiling_words()),
            'extremeMarkers': list(registry.extreme_markers()) if intensityLevel < registry.extreme_level() else [],
            'modeLevelReviewRequired': floor['enabled'] and mode in {'plot', 'buildUp'} and intensityLevel >= floor['minLevel'],
            'additionalChecks': {'voice': {'globalBanned': patterns['voice']['globalBannedInterjections'],
                'perRole': '运行诊断时从项目读取；当前加载情况见 voiceCoverage'},
                'degeneration': {'source': 'scripts/check_degeneration.py',
                    **policy['degeneration']},
                'sentenceRhythm': policy['sentenceRhythm'],
                'chapterContract': '运行项目诊断时从推演读取字数与段预算'},
            'coverage': '形式检查；张力等级数字是上限，实际强度、POV、因果和必须落地项仍须语义审校。'}


def check_paragraph(*, text, mode='buildUp', intensityLevel=None):
    validator = text_module('text.validator')
    if not isinstance(text, str) or not text.strip():
        raise StructureError('DIAGNOSTICS_INPUT_INVALID', 'paragraph text must be nonempty')
    if intensityLevel is None: intensityLevel = text_module('intensity_registry').default_level()
    rules_card(mode=mode, intensityLevel=intensityLevel)  # shared argument validation
    checks = [validator._norm(validator.check_sentence_length(text, mode), '句子长度'),
              validator._norm(validator.check_subject_sentences(text), '句式节奏'),
              validator._norm(validator.check_ai_patterns(text, mode=mode), 'AI句式套路')]
    cliche = text_module('ai_cliche_linter').build_report(text, intensity_level=intensityLevel, mode=mode)
    return {'ok': True, 'code': 'PARAGRAPH_CHECK', 'scope': 'paragraph', 'textSha256': sha256_text(text),
            'checks': checks, 'findings': cliche['findings'], 'ceiling': cliche['intensityLevelCheck'],
            'lineDetails': {'S-10s': validator.sentence_length_violations(text, mode),
                            'S-11': validator.subject_sentence_violations(text)},
            'chapterGateEvaluated': False,
            'limitations': ['不检查整章字数、段际节奏、密度地板或语义；局部零命中不代表全章通过。']}


BEAT_MARKER = re.compile(r'^[ \t]*<!--[ \t]*beat[ \t]*:[ \t]*(B\d+)[ \t]*-->[ \t]*(?=\r?$)', re.M)


def split_beat_markers(raw):
    """Strip only explicit beat markers and retain spans over the exact resulting body."""
    chunks, spans, end, cursor = [], [], 0, 0
    for marker in BEAT_MARKER.finditer(raw):
        chunk = raw[end:marker.start()]
        chunks.append(chunk); cursor += len(chunk)
        if spans: spans[-1]['end'] = cursor
        spans.append({'id': marker[1], 'start': cursor})
        end = marker.end()
    chunks.append(raw[end:]); cursor += len(raw[end:])
    if spans: spans[-1]['end'] = cursor
    if len({row['id'] for row in spans}) != len(spans):
        raise StructureError('BEAT_MARKER_DUPLICATE', 'beat markers must be unique')
    return ''.join(chunks), spans


def outline_requirements(text):
    rows, active, fence = [], False, False
    for number, line in enumerate(text.splitlines(), 1):
        if re.match(r'^\s*(```|~~~)', line): fence = not fence; continue
        if fence: continue
        if re.match(r'^#{1,6}\s', line): active = '必须落地' in line
        item = re.match(r'^\s*\d+[.、]\s*(.+)', line) if active else None
        if item: rows.append({'id': f'R{len(rows) + 1:02}', 'text': item[1], 'outlineLine': number})
    # Only declared literal targets; chapter/volume names elsewhere are not obligations.
    targets = [row['text'] for row in rows if re.search(r'关键台词|必须原文|原句落地|逐字落地', row['text'])]
    targets += [line for line in text.splitlines() if re.match(r'^\s*原句落地[：:]', line)]
    quotes = list(dict.fromkeys(re.findall(r'[“「]([^”」\n]+)[”」]', '\n'.join(targets))))
    pov = re.search(r'POV\s*纪律[：:]\s*([^\n]+)', text)
    return {'requirements': rows, 'quotedTargets': quotes, 'povInstruction': pov[1].strip() if pov else None}


def chapter_check(root, *, chapterId, input=None):
    root = root.expanduser().resolve()
    structure = load_structure(root)
    if input is None:
        from .mechanical_evidence import candidate_prose
        prose = candidate_prose(root, chapterId, structure=structure)
        text, spans = prose['text'], prose.get('beatSpans', [])
    else:
        text, spans = split_beat_markers(safe_project_path(root, input).read_bytes().decode('utf-8'))
    result = audit_chapter(root, structure, chapterId, text, spans)
    result['factCandidates'] = fact_candidates(root, structure, text)
    result['stateCandidateDraft'] = {'changes': [], 'proposedFacts': [
        {'id': row['id'], 'statement': row['quote'], 'quote': row['quote']} for row in result['factCandidates'] if not row['knownExact']]}
    result['factCoverage'] = '只查前史、数字、地点线索与设定逐字重合；不等于完整抽取或语义判新。候选须人工选择后放入 notes.newFacts。'
    return result


def fact_candidates(root, structure, text):
    from .artifacts import canonical_json
    setting = canonical_json(structure)
    pattern = re.compile(diagnostic_policy()['factSignals'])
    rows = []
    for match in re.finditer(r'[^。！？\n]+[。！？]?', text):
        quote = match[0].strip()
        if not quote or not pattern.search(quote) or quote.startswith(('#', '<!--')): continue
        rows.append({'id': 'FACT-' + sha256_text(quote)[:12], 'quote': quote,
                     'line': text.count('\n', 0, match.start()) + 1,
                     'knownExact': quote in setting, 'status': 'needs_author_review'})
    return list({row['id']: row for row in rows}.values())


def audit_chapter(root, structure, chapter_id, text, beat_spans=()):
    from .outline_facts import inspect_outline
    facts = inspect_outline(root, chapterId=chapter_id, structure=structure)
    count_han = text_module('text_utils').chinese_char_count
    body = text_module('text.validator')._without_title_line(text)
    count = count_han(body)
    result = {'draftSha256': sha256_text(text), 'hanCount': count, 'findings': [], 'beatCounts': [],
              'semanticReviewComplete': False, 'requirements': [], 'quotedTargets': [],
              'pov': {'status': 'needs_semantic_review', 'characterId': facts.get('pov')},
              'outlineAvailable': facts.get('available', False)}
    interval = facts.get('targetRange')
    if facts.get('sourceMode'):
        result['sourcePlanHash'] = facts['sourcePlanHash']
        result['contractSource'] = facts['contractSource']
        if not interval or facts.get('issues'):
            result['findings'].append({'ruleId': 'CHAPTER-CONTRACT-UNAVAILABLE', 'severity': 'error',
                'source': 'chapter_contract', 'message': '源计划没有唯一可用的篇幅合同；补齐 chapterContract 或明确的本章字数区间',
                'issues': facts.get('issues', []), 'evaluationStatus': 'not_evaluated'})
    if interval:
        result['targetRange'] = interval
        result['lengthDelta'] = {'belowMinimum': max(0, interval[0] - count), 'aboveMaximum': max(0, count - interval[1])}
        if not interval[0] <= count <= interval[1]:
            result['findings'].append({'ruleId': 'CHAPTER-LENGTH', 'severity': 'error',
                'message': f'正文 {count} 汉字，推演要求 {interval[0]}–{interval[1]} 汉字', 'source': 'chapter_contract'})
    else:
        result['lengthCoverage'] = 'unmeasured: no parseable chapter target range'
    if facts.get('available'):
        from .chapter_contract import outline_text
        requirements = facts.get('requirements') or outline_requirements(outline_text(root, facts))
        if not facts.get('sourceMode'):
            result['outlineSha256'] = facts['sha256']
        result['pov']['instruction'] = requirements['povInstruction']
        result['requirements'] = [{**row, 'status': 'needs_semantic_review'} for row in requirements['requirements']]
        for quote in requirements['quotedTargets']:
            start = text.find(quote)
            result['quotedTargets'].append({'text': quote, 'status': 'literal_found' if start >= 0 else 'needs_semantic_review',
                **({'start': start, 'end': start + len(quote)} if start >= 0 else {})})
    measured = {row['id']: count_han(text[row['start']:row['end']]) for row in beat_spans}
    for beat in facts.get('beats', []):
        n = measured.get(beat['id'])
        result['beatCounts'].append({**beat, 'hanCount': n, 'delta': n - beat['budget'] if n is not None and beat['budget'] is not None else None,
                                    'status': 'measured' if n is not None else 'unmeasured'})
    result['beatCoverage'] = {'markersPresent': bool(beat_spans),
        'unknownIds': sorted(set(measured) - {r['id'] for r in facts.get('beats', [])}),
        'unassignedHanCount': count_han(text[:beat_spans[0]['start']]) if beat_spans else count}
    if beat_spans and (result['beatCoverage']['unknownIds'] or any(row['status'] == 'unmeasured' for row in result['beatCounts'])):
        result['findings'].append({'ruleId': 'CHAPTER-BEATS', 'severity': 'error', 'source': 'chapter_contract',
            'message': '节奏段标记与推演段号不一致；缺段/未知段见 beatCounts、beatCoverage'})
    result['coverage'] = {'lengthEvaluated': bool(interval), 'planAvailable': facts.get('available', False),
                          'beatBudgetsParsed': bool(facts.get('beats')), 'beatsMeasured': bool(beat_spans),
                          'requirementsDeclared': bool(result['requirements']),
                          'literalTargetsDeclared': bool(result['quotedTargets']),
                          'semanticChecks': 'not_evaluated'}
    result['passed'] = not any(r['severity'] in {'blocking', 'error'} for r in result['findings'])
    return result
