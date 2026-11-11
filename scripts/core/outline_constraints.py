"""Recognize explicit project level ranges; exceptions remain unapproved declarations."""
import re
from .artifacts import sha256_text
from .paths import safe_project_path


def inspect_project_levels(root, structure, chapter, outline, intensity_level):
    path = safe_project_path(root, 'AGENTS.md')
    result = {'source': 'AGENTS.md', 'ranges': [], 'conflicts': [], 'exceptionDeclared': False,
              'exceptionApproved': False, 'coverage': '仅识别明确的卷级 L下限–L上限；自由文本约束仍须通读。'}
    if not path.is_file(): return result
    text = path.read_bytes().decode('utf-8')
    result['sha256'] = sha256_text(text)
    volume_id = chapter.get('placement', {}).get('volumeId')
    volume = next((row for row in structure.get('volumes', []) if row['volumeId'] == volume_id), {})
    ordinal = volume.get('ordinal')
    digits = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    pattern = r'第([一二三四五六七八九十\d]+)卷[^\n。；;]*?L(\d+)\s*[-–—~～至]\s*L?(\d+)'
    for match in re.finditer(pattern, text):
        number = int(match[1]) if match[1].isdigit() else digits.get(match[1])
        row = {'volumeOrdinal': number, 'range': [int(match[2]), int(match[3])],
               'quote': match[0], 'line': text.count('\n', 0, match.start()) + 1}
        result['ranges'].append(row)
        if ordinal == number and intensity_level is not None and not row['range'][0] <= intensity_level <= row['range'][1]:
            result['conflicts'].append({**row, 'declaredLevel': intensity_level})
    for line in outline.splitlines():
        if re.search(r'AGENTS\.md', line, re.I) and '偏离' in line and re.search(rf'L\s*{intensity_level}(?!\d)', line):
            result.update(exceptionDeclared=True, exceptionDeclaration=line.strip())
            break
    result['requiresAuthorExceptionApproval'] = bool(result['conflicts'])
    return result


def stage_a_seats(root, structure, chapter_id, payload):
    """Project authored review lines into the artifact; never supply a missing verdict."""
    from .outline_facts import inspect_outline
    from .structure_store import StructureError
    facts = inspect_outline(root, chapterId=chapter_id, structure=structure)
    if payload.get('decision') not in {'adopt', 'modify'}:
        return
    from .chapter_contract import outline_text
    text = outline_text(root, facts, structure=structure)
    seats = payload.get('seatReviews')
    if seats is None:
        seats = {}
        for line in text.splitlines():
            match = re.match(r'^\s*-?\s*身份\s*(\d+)\s*[（(][^）)]+[）)]\s*[：:]\s*(.+)', line)
            if match:
                if match[1] in seats: raise StructureError('STAGE_A_SEATS_INVALID', 'duplicate authored review seat')
                seats[match[1]] = {'conclusion': match[2], 'evidence': [{'text': line.strip()}]}
        payload['seatReviews'] = seats
    from .review_plan import require_stage_a_completion
    require_stage_a_completion(structure, chapter_id, payload)
