"""Frozen, author-reviewable manuscript intake; no caller-built project records."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .clock import utc_now
from .file_preservation import exclusive_move, observation, parent_directory
from .jsonio import write_bytes, write_json
from .project import TOOLBOX_ROOT, query_policy, toolbox_manifest
from .structure_store import StructureError, canonical_json


ENCODINGS = ('utf-8', 'utf-8-sig', 'utf-16', 'gb18030')
ORDINAL = r'[零〇一二三四五六七八九十百千万两\d]+'
HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?((?:第\s*' + ORDINAL + r'\s*[章回节]|(?:chapter|ch)\s*\d+|' +
                     r'[（(]\s*' + ORDINAL + r'\s*[）)]|【\s*' + ORDINAL + r'\s*】|' +
                     ORDINAL + r'、)[^\r\n]*)$', re.I)
SPECIAL_HEADING = re.compile(r'^\s*(?:#{1,6}\s*)?((?:引子|楔子|序章|尾声|后记|番外)(?:$|[\s:：、.．-]+[^\r\n]*))\s*$')
PLAN_ID = re.compile(r'^IMP-[0-9a-f]{32}$')
FREE_MARKER = re.compile(r'[（(]\s*本章免费\s*[）)]\s*$')
SUSPECTED_HEADING = re.compile(r'^\s*(?:第\s*)?(' + ORDINAL + r')\s*[章掌回节][^\r\n]{0,100}$')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_hash(plan: dict) -> str:
    return digest(canonical_json({k: v for k, v in plan.items() if k != 'planHash'}).encode())


def plan_directory(root: Path, plan_id: str) -> Path:
    if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
        raise StructureError('IMPORT_PLAN_INVALID', 'invalid import plan ID')
    key = digest(str(root).encode())
    return root.parent / ('.govel-import-' + key) / plan_id


def read_plan(root: Path, plan_id: str, expected_hash: str) -> tuple[Path, dict]:
    directory = plan_directory(root, plan_id)
    try:
        _, raw = observation(directory / 'plan.json')
        plan = json.loads(raw or b'')
        valid = (plan['planId'] == plan_id and plan['projectRoot'] == str(root)
                 and plan['planHash'] == expected_hash == plan_hash(plan))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StructureError('IMPORT_PLAN_INVALID', 'import preview is missing or damaged; prepare it again') from exc
    if not valid:
        raise StructureError('IMPORT_PLAN_STALE', 'preview identity, destination or content does not match')
    return directory, plan


def source_files(source: Path) -> list[Path]:
    if source.is_symlink() or not source.exists():
        raise StructureError('IMPORT_SOURCE_INVALID', 'source must be a regular manuscript file or directory')
    if source.is_file():
        paths = [source]
    else:
        paths = []
        def unreadable(error):
            raise StructureError('IMPORT_SOURCE_INVALID', 'cannot enumerate the complete manuscript directory',
                                 {'path': str(error.filename or source)}) from error
        for folder, directories, files in os.walk(source, followlinks=False, onerror=unreadable):
            if any((Path(folder) / name).is_symlink() for name in directories):
                raise StructureError('IMPORT_SOURCE_INVALID', 'source directories must not contain symlinks')
            paths.extend(Path(folder) / name for name in files if Path(name).suffix.lower() in {'.txt', '.md'})
        # Numeric filenames read naturally (2 before 10), with a deterministic tie-breaker.
        paths.sort(key=lambda p: (tuple((0, int(s)) if s.isdigit() else (1, s.casefold())
                                       for s in re.split(r'(\d+)', str(p.relative_to(source)))), str(p)))
    if not paths or any(p.is_symlink() or p.suffix.lower() not in {'.txt', '.md'} for p in paths):
        raise StructureError('IMPORT_SOURCE_INVALID', 'source contains no safe .txt/.md manuscript files')
    return paths


def decode_source(raw: bytes, encoding: str) -> str:
    if encoding not in ENCODINGS:
        raise StructureError('IMPORT_ENCODING_INVALID', 'choose an explicit supported encoding', {'supported': list(ENCODINGS)})
    try:
        text = raw.decode('utf-8-sig' if encoding == 'utf-8' else encoding)
    except UnicodeError as exc:
        raise StructureError('IMPORT_ENCODING_INVALID', 'cannot decode without data loss; specify the source encoding',
                             {'encoding': encoding, 'supported': list(ENCODINGS)}) from exc
    if '\x00' in text or not text.strip():
        raise StructureError('IMPORT_SOURCE_INVALID', 'manuscript must contain nonempty text without NUL characters')
    return text


def _number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {'零': 0, '〇': 0, '一': 1, '二': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '两': 2}
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                total += (section + number or 1) * unit
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            return None
    total += section + number
    return total


def _heading(line: str, prefixes: list[str], ignored: re.Pattern | None) -> tuple[str, int | None] | None:
    stripped = line.rstrip('\r\n')
    if ignored and ignored.search(stripped):
        return None
    match = HEADING.fullmatch(stripped)
    if match:
        title = match[1].strip()
        ordinal = re.search(r'(?:第\s*)?(' + ORDINAL + r')\s*[章回节]', title)
        return title, _number(ordinal[1]) if ordinal else None
    match = SPECIAL_HEADING.fullmatch(stripped)
    if match:
        return match[1].strip(), None
    for prefix in prefixes:
        match = re.match(r'^\s*(?:#{1,6}\s*)?(' + re.escape(prefix) + r'(?:$|(?:\s+|\s*[:：、.．-]\s*)[^\r\n]*))\s*$', stripped)
        if match:
            return match[1].strip(), None
    return None


def _splits(text: str, relative: str, split: str, overrides: list[dict], *,
            ignore_heading_pattern: str | None = None, heading_patterns: list[str] | None = None,
            dedupe_heading_window: int = 5) -> tuple[list[dict], list[str], list[dict]]:
    lines = text.splitlines(keepends=True)
    warnings = []
    anomalies = []
    prefixes = ['引子', '楔子', '序章', '尾声', '后记', '番外', *(heading_patterns or [])]
    ignored = None
    if ignore_heading_pattern is not None:
        try:
            ignored = re.compile(ignore_heading_pattern)
        except (TypeError, re.error) as exc:
            raise StructureError('IMPORT_HEADING_PATTERN_INVALID', 'ignoreHeadingPattern must be a valid regular expression') from exc
    if type(dedupe_heading_window) is not int or not 0 <= dedupe_heading_window <= 100:
        raise StructureError('IMPORT_HEADING_PATTERN_INVALID', 'dedupeHeadingWindow must be between 0 and 100')
    selected = [row for row in overrides if row['sourcePath'] == relative]
    if selected:
        starts = [row['startLine'] for row in selected]
        if starts != sorted(set(starts)) or starts[-1] > len(lines):
            raise StructureError('IMPORT_BOUNDARIES_INVALID', 'boundaries must increase within each source file',
                                 {'sourcePath': relative, 'lineCount': len(lines)})
    elif split == 'file':
        selected = [{'startLine': 1, 'title': Path(relative).stem}]
    else:
        candidates = []
        for line_number, line in enumerate(lines, 1):
            found = _heading(line, prefixes, ignored)
            if found:
                title, ordinal = found
                candidates.append({'startLine': line_number, 'title': title[:query_policy()['searchContextCharacters']],
                                   'ordinal': ordinal})
                continue
            suspect = SUSPECTED_HEADING.fullmatch(line.rstrip('\r\n'))
            if suspect:
                raw_title = line.strip().lstrip('#').strip()
                suggestion = raw_title
                if '掌' in suggestion:
                    suggestion = suggestion.replace('掌', '章', 1)
                if not suggestion.startswith('第'):
                    suggestion = '第' + suggestion
                anomalies.append({'sourcePath': relative, 'line': line_number, 'code': 'suspected_heading',
                                  'title': raw_title, 'suggestion': suggestion,
                                  'message': '疑似章标题未匹配；请确认后用 boundaries 修正。'})
                ordinal = re.search(r'(' + ORDINAL + r')\s*[章掌回节]', raw_title)
                candidates.append({'startLine': line_number, 'title': raw_title[:query_policy()['searchContextCharacters']],
                                   'ordinal': _number(ordinal[1]) if ordinal else None})
        selected, merged_headings = [], []
        for candidate in candidates:
            normalized = FREE_MARKER.sub('', candidate['title']).strip()
            duplicate_index = next((index for index in range(len(selected) - 1, -1, -1)
                                    if candidate['startLine'] - selected[index]['startLine'] <= dedupe_heading_window
                                    and FREE_MARKER.sub('', selected[index]['title']).strip() == normalized), None)
            if duplicate_index is not None:
                merged_headings.append({'line': candidate['startLine'], 'title': candidate['title'],
                                        'chapterOrdinal': duplicate_index + 1})
                continue
            selected.append({key: candidate[key] for key in ('startLine', 'title')})
        if merged_headings:
            anomalies.append({'sourcePath': relative, 'line': merged_headings[0]['line'],
                              'code': 'duplicate_heading_merged', 'count': len(merged_headings),
                              'chapterOrdinals': sorted({row['chapterOrdinal'] for row in merged_headings}),
                              'lineExamples': [row['line'] for row in merged_headings[:10]],
                              'message': f'已合并 {len(merged_headings)} 行窗口内重复标题。'})
        if not selected:
            selected = [{'startLine': 1, 'title': Path(relative).stem}]
            warnings.append('未识别章标题，暂按整个文件一章；可按行号调整分章。')
        for finding in anomalies:
            if finding['code'] == 'suspected_heading':
                finding['chapterOrdinal'] = next((index for index, row in enumerate(selected, 1)
                                                  if row['startLine'] == finding['line']), None)
        title_ordinals = {}
        for index, item in enumerate(selected, 1):
            title_ordinals.setdefault(item['title'], []).append(index)
        duplicate_ordinals = [ordinal for ordinals in title_ordinals.values() if len(ordinals) > 1
                              for ordinal in ordinals]
        if duplicate_ordinals:
            anomalies.append({'sourcePath': relative, 'code': 'repeated_chapter_title',
                              'chapterOrdinals': duplicate_ordinals,
                              'message': '存在未合并的重复章标题，请核对是否为不同章节。'})
        if any(re.match(r'^[（(【零〇一二三四五六七八九十百千万两\d]', row['title']) for row in selected):
            warnings.append('括号／序数标题也可能是目录或正文列表，请核对分章候选。')
    selected = [dict(row) for row in selected]
    if selected[0]['startLine'] != 1:
        prefix = ''.join(lines[:selected[0]['startLine'] - 1])
        if prefix.strip():
            selected.insert(0, {'startLine': 1, 'title': '卷首内容：' + Path(relative).stem})
            anomalies.append({'sourcePath': relative, 'line': 1, 'code': 'front_matter',
                              'chapterOrdinal': 1,
                              'startLine': 1, 'endLine': selected[1]['startLine'] - 1,
                              'excerpt': prefix.strip()[:120],
                              'message': '以下卷首原文未拆为独立章，已包含在卷首章节候选中。'})
            warnings.append('首章之前的内容单独保留，请核对是否为前言或目录。')
        else:
            selected[0]['startLine'] = 1
    offsets, character_offsets = [0], [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode('utf-8')))
        character_offsets.append(character_offsets[-1] + len(line))
    encoded = text.encode('utf-8')
    result = []
    for index, item in enumerate(selected):
        start_line = item['startLine']
        end_line = selected[index + 1]['startLine'] - 1 if index + 1 < len(selected) else len(lines)
        start, end = offsets[start_line - 1], offsets[end_line]
        result.append({'sourcePath': relative, 'title': item['title'], 'startLine': start_line,
                       'endLine': end_line, 'utf8ByteStart': start, 'utf8ByteEnd': end,
                       'characterStart': character_offsets[start_line - 1], 'characterEnd': character_offsets[end_line],
                       'contentHash': digest(encoded[start:end]), 'bytes': end - start})
        chapter_text = ''.join(lines[start_line - 1:end_line])
        body = '\n'.join(chapter_text.splitlines()[1:]).strip()
        if len(body) <= 80:
            anomalies.append({'sourcePath': relative, 'line': start_line, 'code': 'short_chapter',
                              'chapterOrdinal': index + 1, 'title': item['title'],
                              'characterCount': len(chapter_text), 'message': '章节正文为空或很短，请检查源稿。'})
    ordinals = []
    for index, item in enumerate(selected):
        found = re.search(r'(?:第\s*)?(' + ORDINAL + r')\s*[章回节]', item['title'])
        ordinal = _number(found[1]) if found else None
        if ordinal is None:
            continue
        ordinals.append((index + 1, item['startLine'], ordinal, item['title']))
    for previous, current in zip(ordinals, ordinals[1:]):
        if current[2] != previous[2] + 1:
            anomalies.append({'sourcePath': relative, 'line': current[1], 'code': 'chapter_number_sequence',
                              'chapterOrdinal': current[0], 'title': current[3],
                              'expected': previous[2] + 1, 'actual': current[2],
                              'message': '章节编号不连续或发生倒退，请核对。'})
    return result, warnings, anomalies


def preview_import(root: Path, *, source: str, bookName: str | None = None, encoding: str = 'utf-8',
                   split: str = 'auto', boundaries: list[dict] | None = None,
                   ignoreHeadingPattern: str | None = None, headingPatterns: list[str] | None = None,
                   dedupeHeadingWindow: int = 5) -> dict:
    root = root.expanduser().resolve()
    requested = Path(source).expanduser().absolute()
    if requested.is_symlink():
        raise StructureError('IMPORT_SOURCE_INVALID', 'source must not be a symlink')
    source_path = requested.resolve()
    if root.is_relative_to(TOOLBOX_ROOT) and not root.is_relative_to(TOOLBOX_ROOT / 'local'):
        raise StructureError('IMPORT_TARGET_INVALID', 'manuscripts must live in local/ or outside the public engine checkout')
    if root.exists() or root.is_symlink() or root == source_path or source_path in root.parents or root in source_path.parents:
        raise StructureError('IMPORT_TARGET_EXISTS', 'choose a new project path separate from the original manuscript')
    if split not in ('auto', 'file'):
        raise StructureError('IMPORT_BOUNDARIES_INVALID', 'split must be auto or file')
    if (headingPatterns is not None and (not isinstance(headingPatterns, list)
            or any(not isinstance(prefix, str) or not prefix.strip() for prefix in headingPatterns))):
        raise StructureError('IMPORT_HEADING_PATTERN_INVALID', 'headingPatterns must be an array of nonempty strings')
    overrides = boundaries or []
    for row in overrides:
        if (not isinstance(row, dict) or set(row) != {'sourcePath', 'startLine', 'title'}
                or not isinstance(row['sourcePath'], str) or type(row['startLine']) is not int or row['startLine'] < 1
                or not isinstance(row['title'], str) or not row['title'].strip()):
            raise StructureError('IMPORT_BOUNDARIES_INVALID', 'each boundary needs sourcePath, positive startLine and title')
    name = bookName if bookName is not None else source_path.stem
    if not isinstance(name, str) or not name.strip():
        raise StructureError('MISSING_BOOK_NAME', 'bookName must be nonempty')
    files, chapters, warnings, anomalies, contents = [], [], [], [], {}
    for index, path in enumerate(source_files(source_path)):
        state, raw = observation(path)
        if raw is None:
            raise StructureError('IMPORT_SOURCE_INVALID', 'source file disappeared')
        text = decode_source(raw, encoding)
        relative = path.name if source_path.is_file() else path.relative_to(source_path).as_posix()
        stored = f'sources/{index}.bin'
        files.append({'sourcePath': relative, 'absolutePath': str(path), 'snapshotPath': stored,
                      'sourceHash': digest(raw), 'sourceObject': state, 'bytes': len(raw),
                      'textHash': digest(text.encode()), 'lineCount': len(text.splitlines())})
        contents[stored] = raw
        chapter_offset = len(chapters)
        sections, notes, findings = _splits(text, relative, split, overrides,
            ignore_heading_pattern=ignoreHeadingPattern, heading_patterns=headingPatterns,
            dedupe_heading_window=dedupeHeadingWindow)
        chapters.extend(sections)
        warnings.extend({'sourcePath': relative, 'message': note} for note in notes)
        anomalies.extend([{**finding, **({'chapterOrdinal': finding['chapterOrdinal'] + chapter_offset}
                                         if finding.get('chapterOrdinal') is not None else {}),
                           **({'chapterOrdinals': [ordinal + chapter_offset for ordinal in finding['chapterOrdinals']]}
                              if finding.get('chapterOrdinals') else {})}
                           for finding in findings])
    unknown = {row['sourcePath'] for row in overrides} - {row['sourcePath'] for row in files}
    if unknown:
        raise StructureError('IMPORT_BOUNDARIES_INVALID', 'boundary sourcePath is not in the manuscript', {'unknown': sorted(unknown)})
    from .data_provider import binding
    plan_id = 'IMP-' + uuid.uuid4().hex
    plan = {'kind': 'manuscript_import', 'schemaVersion': 1, 'planId': plan_id, 'createdAt': utc_now(),
            'projectRoot': str(root), 'projectId': str(uuid.uuid4()), 'bookName': name.strip(),
            'source': str(source_path), 'encoding': encoding, 'files': files, 'chapters': chapters,
            'warnings': warnings, 'anomalies': anomalies, 'provider': binding(), 'toolboxVersion': toolbox_manifest()['version'],
            'manifestHash': digest((TOOLBOX_ROOT / 'toolbox.manifest.json').read_bytes())}
    plan['planHash'] = plan_hash(plan)
    directory = plan_directory(root, plan_id)
    with parent_directory(directory, create=True):
        pass
    temporary = Path(tempfile.mkdtemp(prefix='.preview-', dir=directory.parent))
    try:
        for relative, raw in contents.items():
            write_bytes(temporary / relative, raw)
        write_json(temporary / 'plan.json', plan)
        exclusive_move(temporary, directory)
    finally:
        if temporary.exists():
            import shutil
            shutil.rmtree(temporary)
    return inspect_import(root, planId=plan_id, planHash=plan['planHash'])


def _inspection_chapters(plan: dict, anomalies_only: bool, search: str | None) -> list[tuple[int, dict]]:
    anomalies = plan.get('anomalies', [])
    flagged = {ordinal for row in anomalies for ordinal in
               ([row['chapterOrdinal']] if row.get('chapterOrdinal') is not None else row.get('chapterOrdinals', []))}
    line = int(search) if isinstance(search, str) and search.isdecimal() else None
    return [(ordinal, chapter) for ordinal, chapter in enumerate(plan['chapters'], 1)
            if (not anomalies_only or ordinal in flagged)
            and (search is None or search.casefold() in (chapter['title'] + ' ' + chapter['sourcePath']).casefold()
                 or line is not None and chapter['startLine'] <= line <= chapter['endLine'])]


def _page_anomalies(anomalies: list[dict], ordinals: list[int], *, include_global: bool) -> list[dict]:
    result = []
    selected = set(ordinals)
    for finding in anomalies:
        if finding.get('chapterOrdinal') in selected:
            result.append(finding)
            continue
        affected = [ordinal for ordinal in finding.get('chapterOrdinals', []) if ordinal in selected]
        if affected:
            result.append({**finding, 'chapterOrdinals': affected})
        elif include_global and 'chapterOrdinal' not in finding and 'chapterOrdinals' not in finding:
            result.append(finding)
    return result


def _export_rows(plan: dict, rows: list[tuple[int, dict]], target: Path) -> dict:
    suffix = target.suffix.lower()
    if suffix not in {'.csv', '.jsonl'}:
        raise StructureError('IMPORT_EXPORT_INVALID', 'exportPath must end in .csv or .jsonl')
    anomalies = plan.get('anomalies', [])
    records = [{**chapter, 'ordinal': ordinal,
                'anomalies': [finding for finding in anomalies
                              if finding.get('chapterOrdinal') == ordinal
                              or finding.get('chapterOrdinals') and ordinal in finding['chapterOrdinals']]}
               for ordinal, chapter in rows]
    if suffix == '.jsonl':
        payload = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records).encode('utf-8')
    else:
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=('ordinal', 'sourcePath', 'title', 'startLine',
            'endLine', 'utf8ByteStart', 'utf8ByteEnd', 'characterStart', 'characterEnd',
            'contentHash', 'bytes', 'anomalies'))
        writer.writeheader()
        for row in records:
            writer.writerow({**row, 'anomalies': json.dumps(row['anomalies'], ensure_ascii=False)})
        payload = stream.getvalue().encode('utf-8-sig')
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StructureError('IMPORT_EXPORT_EXISTS', 'export target already exists; choose a new path',
                             {'exportPath': str(target)}) from exc
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'path': str(target), 'format': suffix[1:], 'rowCount': len(records)}


def export_import_plan(root: Path, *, planId: str, planHash: str, exportPath: str,
                       anomaliesOnly: bool = False, search: str | None = None) -> dict:
    root = root.expanduser().resolve()
    _, plan = read_plan(root, planId, planHash)
    if type(anomaliesOnly) is not bool or search is not None and (not isinstance(search, str) or not search.strip()):
        raise StructureError('ARGUMENTS_INVALID', 'anomaliesOnly must be boolean and search must be nonempty when supplied')
    rows = _inspection_chapters(plan, anomaliesOnly, search)
    target = Path(exportPath).expanduser().absolute()
    exported = _export_rows(plan, rows, target)
    return {'ok': True, 'code': 'IMPORT_PLAN_EXPORTED', 'planId': planId, 'planHash': planHash,
            'anomaliesOnly': anomaliesOnly, 'search': search, 'exportedPlan': exported}


def inspect_import(root: Path, *, planId: str, planHash: str, offset: int = 0,
                   limit: int | None = None, anomaliesOnly: bool = False, search: str | None = None) -> dict:
    root = root.expanduser().resolve()
    _, plan = read_plan(root, planId, planHash)
    policy = query_policy()
    limit = policy['defaultLimit'] if limit is None else limit
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= policy['maxLimit']:
        raise StructureError('ARGUMENTS_INVALID', 'invalid import page offset or limit')
    if type(anomaliesOnly) is not bool:
        raise StructureError('ARGUMENTS_INVALID', 'anomaliesOnly must be boolean')
    if search is not None and (not isinstance(search, str) or not search.strip()):
        raise StructureError('ARGUMENTS_INVALID', 'search must be nonempty when supplied')
    anomalies = plan.get('anomalies', [])
    chapters = _inspection_chapters(plan, anomaliesOnly, search)
    if offset > len(chapters):
        raise StructureError('ARGUMENTS_INVALID', 'offset exceeds the chapter count')
    action = {'operation': 'import.commit', 'arguments': {'projectRoot': str(root), 'planId': planId, 'planHash': planHash, 'approve': True},
              'requiresApproval': True}
    result = {'ok': True, 'code': 'IMPORT_PREVIEW', 'projectRoot': str(root), 'planId': planId, 'planHash': planHash,
              'bookName': plan['bookName'], 'fileCount': len(plan['files']), 'chapterCount': len(plan['chapters']),
              'listedChapterCount': len(chapters), 'anomaliesOnly': anomaliesOnly, 'search': search,
              'anomalySummary': dict(sorted((code, sum(row.get('count', 1) for row in anomalies
                                                       if row['code'] == code))
                                            for code in {row['code'] for row in anomalies})),
              'encoding': plan['encoding'], 'sourceBytes': sum(row['bytes'] for row in plan['files']),
              'preservesAllText': True, 'chapterStatus': 'planning_required', 'semanticReviewComplete': False,
              'guidance': '核对完整分章列表；调整时重新 preview，boundaries 只需源文件相对路径、起始行与标题。导入保留正文，设定与规划待整理。',
              'warnings': plan['warnings'], 'anomalies': [], 'chapters': [], 'nextOffset': None, 'nextAction': action}
    # Reserve the actual next-page action while measuring so the final envelope
    # stays within the same byte budget as its chapter list.
    result['readAction'] = {'operation': 'import.inspect', 'arguments': {
        'projectRoot': str(root), 'planId': planId, 'planHash': planHash, 'offset': len(chapters), 'limit': limit,
        **({'anomaliesOnly': True} if anomaliesOnly else {}), **({'search': search} if search else {})}}
    result['nextOffset'] = len(chapters)
    page_chapters = []
    for index in range(offset, min(offset + limit, len(chapters))):
        ordinal, chapter = chapters[index]
        row = {**chapter, 'ordinal': ordinal,
               'readAction': {'operation': 'import.read', 'arguments': {'projectRoot': str(root),
                   'planId': planId, 'planHash': planHash, 'sourcePath': chapter['sourcePath'], 'offset': chapter['characterStart']}}}
        result['chapters'].append(row)
        page_chapters.append((ordinal, row))
        result['anomalies'] = _page_anomalies(anomalies, [item[0] for item in page_chapters],
                                              include_global=offset == 0)
        if len(json.dumps(result, ensure_ascii=False).encode()) > policy['maxBytes']['list']:
            result['chapters'].pop()
            page_chapters.pop()
            result['anomalies'] = _page_anomalies(anomalies, [item[0] for item in page_chapters],
                                                  include_global=offset == 0)
            if not result['chapters']:
                raise StructureError('IMPORT_PAGE_TOO_LARGE', 'one chapter title exceeds the response budget; shorten it in a new preview')
            break
    following = offset + len(result['chapters'])
    if following < len(chapters):
        result['nextOffset'] = following
        result['readAction'] = {'operation': 'import.inspect', 'arguments': {
            'projectRoot': str(root), 'planId': planId, 'planHash': planHash, 'offset': following, 'limit': limit,
            **({'anomaliesOnly': True} if anomaliesOnly else {}), **({'search': search} if search else {})}}
    else:
        result['nextOffset'] = None
        result.pop('readAction')
    return result


def read_import_text(root: Path, *, planId: str, planHash: str, sourcePath: str, offset: int = 0) -> dict:
    """Read frozen text in bounded pages, including manuscripts without line breaks."""
    root = root.expanduser().resolve()
    directory, plan = read_plan(root, planId, planHash)
    source = next((row for row in plan['files'] if row['sourcePath'] == sourcePath), None)
    if source is None:
        raise StructureError('IMPORT_SOURCE_INVALID', 'sourcePath is not in the frozen preview')
    _, raw = observation(directory / source['snapshotPath'])
    if raw is None or digest(raw) != source['sourceHash']:
        raise StructureError('IMPORT_SNAPSHOT_DAMAGED', 'frozen manuscript evidence changed')
    text = decode_source(raw, plan['encoding'])
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise StructureError('ARGUMENTS_INVALID', 'offset must be a character position within the source')
    budget = query_policy()['maxBytes']['detail']
    end = min(len(text), offset + budget)
    while True:
        more = end < len(text)
        result = {'ok': True, 'code': 'IMPORT_SOURCE_TEXT', 'sourcePath': sourcePath,
                  'planId': planId, 'planHash': planHash, 'sourceHash': source['sourceHash'],
                  'offset': offset, 'end': end, 'startLine': len(text[:offset].splitlines()) + (0 if offset and not text[:offset].endswith(('\n', '\r')) else 1),
                  'text': text[offset:end], 'nextOffset': end if more else None}
        if more:
            result['readAction'] = {'operation': 'import.read', 'arguments': {
                'projectRoot': str(root), 'planId': planId, 'planHash': planHash, 'sourcePath': sourcePath, 'offset': end}}
        if end == offset and offset < len(text):
            raise StructureError('IMPORT_PAGE_TOO_LARGE', 'source metadata leaves no room for text')
        if len(json.dumps(result, ensure_ascii=False).encode()) <= budget:
            return result
        if end == offset:
            raise StructureError('IMPORT_PAGE_TOO_LARGE', 'source metadata exceeds the response budget')
        end = offset + (end - offset) // 2
