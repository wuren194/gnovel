"""Append frozen external manuscript chapters through the existing source transaction."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

from .artifacts import sha256_json
from .consistency import require_no_pending
from .file_preservation import observation, parent_directory
from .jsonio import read_json
from .locks import project_lock
from .manuscript_import import ENCODINGS, _splits, decode_source, source_files
from .project import query_policy
from .prose_binding import seal_binding
from .source_model import require_current_source
from .source_requests import request_identity, resume_request
from .structure_commands import (_base_plan, _new_chapter, _operation_paths, _plan_hash,
                                 _write_plan_locked)
from .structure_store import StructureError, canonical_json, load_structure
from .write_sets import GUARD_KEYS, digest, json_bytes, safe_project_path


OPERATION = 'append_manuscript'


def _evidence_root(operation_id: str) -> str:
    return f'.govel/manuscript-sources/{operation_id}'


def _snapshot(root: Path, relative: str, data: bytes) -> dict:
    """Create an immutable input, retaining an identical interrupted preparation."""
    path = safe_project_path(root, relative)
    with parent_directory(path, create=True) as directory:
        try:
            fd = os.open(path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        except FileExistsError:
            state, old = observation(path)
            if old != data:
                raise StructureError('APPEND_STAGING_CONFLICT', 'request already has different frozen input; use a new requestId')
            return state
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        except BaseException:
            # A partially written input cannot be mistaken for a valid preview.
            # It remains evidence of this failed request and is never overwritten.
            raise
    return observation(path)[0]


def _validate_arguments(source: str, encoding: str, split: str, boundaries: list[dict],
                        reason: str, placement: dict | None, volume_id: str | None) -> Path:
    if (placement is None) == (volume_id is None):
        raise StructureError('PLACEMENT_ARGUMENT_INVALID', 'provide exactly one of volumeId or placement')
    if not isinstance(reason, str) or not reason.strip():
        raise StructureError('APPEND_REASON_REQUIRED', 'provide a reason for appending these chapters')
    if encoding not in ENCODINGS:
        raise StructureError('IMPORT_ENCODING_INVALID', 'choose an explicit supported encoding')
    if split not in ('auto', 'file') or not isinstance(boundaries, list):
        raise StructureError('IMPORT_BOUNDARIES_INVALID', 'split must be auto or file and boundaries must be an array')
    for row in boundaries:
        if (not isinstance(row, dict) or set(row) != {'sourcePath', 'startLine', 'title'}
                or not isinstance(row['sourcePath'], str) or not row['sourcePath']
                or type(row['startLine']) is not int or row['startLine'] < 1
                or not isinstance(row['title'], str) or not row['title'].strip()):
            raise StructureError('IMPORT_BOUNDARIES_INVALID', 'each boundary needs sourcePath, positive startLine and title')
    if not isinstance(source, str) or not source.strip():
        raise StructureError('IMPORT_SOURCE_INVALID', 'provide a manuscript file or directory')
    requested = Path(source).expanduser().absolute()
    if requested.is_symlink():
        raise StructureError('IMPORT_SOURCE_INVALID', 'source must not be a symlink')
    return requested.resolve()


def _check_source_location(root: Path, source: Path) -> None:
    if source == root or source in root.parents:
        raise StructureError('APPEND_SOURCE_OVERLAP', 'source directory must not contain the destination project')
    if source.is_relative_to(root):
        relative = source.relative_to(root)
        if relative.parts[0].casefold() == '.govel' or source.is_dir() and relative.as_posix().casefold() == '正文':
            raise StructureError('APPEND_SOURCE_OVERLAP', 'source must not overlap generated intake or new manuscript destinations')


def _scan(source: Path, *, encoding: str, split: str, boundaries: list[dict]) -> tuple[list, list, list, dict]:
    files, chapters, warnings, contents = [], [], [], {}
    directory = source.is_dir()
    for path in source_files(source):
        state, raw = observation(path)
        if raw is None:
            raise StructureError('IMPORT_SOURCE_CHANGED', 'manuscript disappeared while reading it')
        text = decode_source(raw, encoding)
        relative = path.relative_to(source).as_posix() if directory else path.name
        files.append({'sourcePath': relative, 'absolutePath': str(path), 'sourceObject': state,
                      'sourceHash': digest(raw), 'bytes': len(raw), 'lineCount': len(text.splitlines())})
        contents[relative] = raw
        sections, notes = _splits(text, relative, split, boundaries)
        chapters.extend(sections)
        warnings.extend({'sourcePath': relative, 'message': note} for note in notes)
    unknown = {row['sourcePath'] for row in boundaries} - set(contents)
    if unknown:
        raise StructureError('IMPORT_BOUNDARIES_INVALID', 'boundary sourcePath is not in the manuscript', {'unknown': sorted(unknown)})
    return files, chapters, warnings, contents


def _append_structure(current: dict, chapters: list[dict], parameters: dict) -> tuple[dict, list[dict]]:
    """Use the normal insertion policy, with stored identities during validation."""
    proposed, results = deepcopy(current), []
    for item in chapters:
        proposed, generated_id, _ = _new_chapter(proposed, title=item['title'],
            placement=parameters['placement'], before_chapter_id=parameters['beforeChapterId'],
            primary_arc_id=parameters['primaryArcId'], secondary_arc_ids=parameters['secondaryArcIds'])
        chapter_id = item.get('chapterId', generated_id)
        relative = f'正文/{chapter_id}.md'
        chapter = next(row for row in proposed['chapters'] if row['chapterId'] == generated_id)
        chapter.update(chapterId=chapter_id, filePath=relative)
        chapter['proseBinding'] = seal_binding(current['projectId'], chapter_id, relative, state='managed',
            generation=current['revision'] + 1, expected_content_digest='sha256:' + item['contentHash'])
        for link in proposed['chapterArcLinks']:
            if link['chapterId'] == generated_id:
                link['chapterId'] = chapter_id
        results.append({**item, 'chapterId': chapter_id, 'filePath': relative})
    return proposed, results


def preview_append(root: Path, *, requestId: str, source: str, primaryArcId: str,
                   volumeId: str | None = None, placement: dict | None = None,
                   beforeChapterId: str | None = None, secondaryArcIds: list[str] | None = None,
                   encoding: str = 'utf-8', split: str = 'auto', boundaries: list[dict] | None = None,
                   reason: str = '作者追加外部原稿', baseRevision: int | None = None) -> dict:
    root = root.expanduser().resolve()
    overrides = [] if boundaries is None else deepcopy(boundaries)
    origin = _validate_arguments(source, encoding, split, overrides, reason, placement, volumeId)
    _check_source_location(root, origin)
    parameters = {'placement': deepcopy(placement) if placement is not None else {'scope': 'volume', 'volumeId': volumeId},
                  'primaryArcId': primaryArcId, 'secondaryArcIds': secondaryArcIds or [],
                  'beforeChapterId': beforeChapterId, 'reason': reason.strip()}
    arguments = {**parameters, 'source': str(origin), 'encoding': encoding, 'split': split,
                 'boundaries': overrides, 'baseRevision': baseRevision}
    with project_lock(root):
        current = load_structure(root)
        require_current_source(current)
        operation_id, _ = request_identity(current, requestId)
        fingerprint = sha256_json({'operation': OPERATION, 'arguments': arguments})
        prior = resume_request(root, current, operation_id, fingerprint)
        if prior is not None:
            stored, committed = _read_append(root, operation_id, prior['planHash'])
            if not committed:
                validate_append_plan(root, current, stored)
            return inspect_append(root, operationId=operation_id, planHash=prior['planHash'])
        if baseRevision is not None and (type(baseRevision) is not int or baseRevision != current['revision']):
            raise StructureError('REVISION_CONFLICT', 'project changed before append preview')
        files, chapters, warnings, contents = _scan(origin, encoding=encoding, split=split, boundaries=overrides)
        proposed, chapters = _append_structure(current, chapters, parameters)
        parameters['identityMap'] = {f'chapter-{index + 1}': row['chapterId'] for index, row in enumerate(chapters)}
        declarations = []
        for index, row in enumerate(files):
            row['snapshotPath'] = f'.govel/plans/{operation_id}.intake/sources/{index}.bin'
            row['evidencePath'] = f'{_evidence_root(operation_id)}/sources/{index}.bin'
            row['snapshotObject'] = _snapshot(root, row['snapshotPath'], contents[row['sourcePath']])
        texts = {relative: decode_source(raw, encoding).encode('utf-8') for relative, raw in contents.items()}
        for row in chapters:
            path = f".govel/plans/{operation_id}.intake/chapters/{row['chapterId']}.md"
            _snapshot(root, path, texts[row['sourcePath']][row['utf8ByteStart']:row['utf8ByteEnd']])
            declarations.append({'chapterId': row['chapterId'], 'filePath': row['filePath'],
                                 'sourcePath': path, 'contentDigest': row['contentHash']})
        plan = _base_plan(current, operation=OPERATION, actor='app', parameters=parameters,
                          impact={'appendedChapterIds': [row['chapterId'] for row in chapters],
                                  'semanticReviewComplete': False}, proposed=proposed)
        plan.update(operationId=operation_id, requestId=requestId, requestFingerprint=fingerprint,
                    manuscriptMaterialization=declarations,
                    appendManuscript={'projectId': current['projectId'], 'source': str(origin),
                        'sourceIsDirectory': origin.is_dir(), 'encoding': encoding, 'split': split,
                        'boundaries': overrides, 'files': files, 'chapters': chapters, 'warnings': warnings})
        report = _write_plan_locked(root, plan)
        return inspect_append(root, operationId=operation_id, planHash=report['planHash'])


def validate_append_sources(plan: dict) -> None:
    metadata = plan['appendManuscript']
    source = Path(metadata['source'])
    try:
        paths = source_files(source)
        if source.is_dir() != metadata['sourceIsDirectory'] or [str(p) for p in paths] != [r['absolutePath'] for r in metadata['files']]:
            raise StructureError('IMPORT_SOURCE_CHANGED', 'manuscript file selection changed after preview')
        for row in metadata['files']:
            state, raw = observation(Path(row['absolutePath']))
            if state != row['sourceObject'] or raw is None or digest(raw) != row['sourceHash']:
                raise StructureError('IMPORT_SOURCE_CHANGED', 'manuscript changed after preview', {'sourcePath': row['sourcePath']})
    except OSError as exc:
        raise StructureError('IMPORT_SOURCE_CHANGED', 'manuscript path changed after preview') from exc


def _frozen_sources(root: Path, plan: dict) -> tuple[dict[str, bytes], list[dict]]:
    contents, guards = {}, []
    metadata = plan['appendManuscript']
    for index, row in enumerate(metadata['files']):
        expected_path = f".govel/plans/{plan['operationId']}.intake/sources/{index}.bin"
        if (row['snapshotPath'] != expected_path
                or row['evidencePath'] != f"{_evidence_root(plan['operationId'])}/sources/{index}.bin"):
            raise StructureError('APPEND_PLAN_INVALID', 'frozen source path is not owned by this append plan')
        state, raw = observation(safe_project_path(root, expected_path))
        if (raw is None or state != row['snapshotObject'] or digest(raw) != row['sourceHash']
                or len(raw) != row['bytes']):
            raise StructureError('IMPORT_SNAPSHOT_DAMAGED', 'frozen original manuscript changed')
        contents[row['sourcePath']] = raw
        guards.append({'path': expected_path, **{key: state[key] for key in GUARD_KEYS}})
    return contents, guards


def validate_append_plan(root: Path, current: dict, plan: dict) -> list[dict]:
    """Rebuild the complete permitted domain delta; no unrelated source edits fit."""
    try:
        metadata = plan['appendManuscript']
        if plan['operation'] != OPERATION or metadata['projectId'] != current['projectId']:
            raise StructureError('APPEND_PLAN_INVALID', 'append plan belongs to another operation or project')
        origin = _validate_arguments(metadata['source'], metadata['encoding'], metadata['split'],
            metadata['boundaries'], plan['parameters']['reason'], plan['parameters']['placement'], None)
        if str(origin) != metadata['source']:
            raise StructureError('APPEND_PLAN_INVALID', 'append source must retain its original canonical path')
        _check_source_location(root, origin)
        validate_append_sources(plan)
        contents, guards = _frozen_sources(root, plan)
        sections, warnings = [], []
        for row in metadata['files']:
            text = decode_source(contents[row['sourcePath']], metadata['encoding'])
            if len(text.splitlines()) != row['lineCount']:
                raise StructureError('APPEND_PLAN_INVALID', 'manuscript line count differs from frozen text')
            expected_relative = (Path(row['absolutePath']).relative_to(metadata['source']).as_posix()
                                 if metadata['sourceIsDirectory'] else Path(row['absolutePath']).name)
            if row['sourcePath'] != expected_relative:
                raise StructureError('APPEND_PLAN_INVALID', 'manuscript source identity differs')
            part, notes = _splits(text, row['sourcePath'], metadata['split'], metadata['boundaries'])
            sections.extend(part)
            warnings.extend({'sourcePath': row['sourcePath'], 'message': note} for note in notes)
        chapters = metadata['chapters']
        if len(sections) != len(chapters) or warnings != metadata['warnings']:
            raise StructureError('APPEND_PLAN_INVALID', 'chapter division differs from frozen manuscript')
        for section, chapter in zip(sections, chapters):
            if section != {key: value for key, value in chapter.items() if key not in {'chapterId', 'filePath'}}:
                raise StructureError('APPEND_PLAN_INVALID', 'chapter range differs from frozen manuscript')
        expected, rebuilt = _append_structure(current, chapters, plan['parameters'])
        if rebuilt != chapters:
            raise StructureError('APPEND_PLAN_INVALID', 'chapter identity or locator differs')
        expected['revision'] = current['revision'] + 1
        from .author_decisions import prepare_decision_transition
        prepare_decision_transition(current, expected, operation=OPERATION,
            operation_id=plan['operationId'], parameters=plan['parameters'])
        ignored = {'updatedAt', 'lastOperation'}
        if {k: v for k, v in expected.items() if k not in ignored} != {
                k: v for k, v in plan['proposedStructure'].items() if k not in ignored}:
            raise StructureError('APPEND_PLAN_INVALID', 'append proposal changes unrelated project content')
        declarations = [{'chapterId': row['chapterId'], 'filePath': row['filePath'],
                         'sourcePath': f".govel/plans/{plan['operationId']}.intake/chapters/{row['chapterId']}.md",
                         'contentDigest': row['contentHash']} for row in chapters]
        if declarations != plan.get('manuscriptMaterialization'):
            raise StructureError('APPEND_PLAN_INVALID', 'materialized chapters differ from manuscript mapping')
        for chapter, declaration in zip(chapters, declarations):
            raw = decode_source(contents[chapter['sourcePath']], metadata['encoding']).encode('utf-8')
            _, staged = observation(safe_project_path(root, declaration['sourcePath']))
            if staged != raw[chapter['utf8ByteStart']:chapter['utf8ByteEnd']]:
                raise StructureError('IMPORT_SNAPSHOT_DAMAGED', 'chapter bytes differ from frozen original')
        identities = {f'chapter-{index + 1}': row['chapterId'] for index, row in enumerate(chapters)}
        if plan['parameters'].get('identityMap') != identities:
            raise StructureError('APPEND_PLAN_INVALID', 'chapter identity map differs')
        return guards
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, StructureError):
            raise
        raise StructureError('APPEND_PLAN_INVALID', 'append plan is incomplete or malformed') from exc


def append_evidence_outputs(root: Path, plan: dict) -> dict[str, bytes]:
    contents, _ = _frozen_sources(root, plan)
    metadata = plan['appendManuscript']
    outputs = {row['evidencePath']: contents[row['sourcePath']] for row in metadata['files']}
    outputs[f"{_evidence_root(plan['operationId'])}/receipt.json"] = json_bytes({
        'kind': OPERATION, 'operationId': plan['operationId'], 'requestId': plan['requestId'],
        'baseRevision': plan['baseRevision'], 'parameters': plan['parameters'],
        'appendManuscript': metadata, 'semanticReviewComplete': False, 'chapterStatus': 'planning_required'})
    for relative in outputs:
        state, _ = observation(safe_project_path(root, relative))
        if state['exists']:
            raise StructureError('APPEND_EVIDENCE_CONFLICT', 'append evidence destination is already occupied', {'path': relative})
    return outputs


def append_guard_groups(root: Path, plan: dict) -> list[dict]:
    """Original manuscripts remain read-only guards, including selected membership."""
    metadata = plan['appendManuscript']
    source = Path(metadata['source'])
    parent = source.parent
    guards = [{'path': Path(row['absolutePath']).relative_to(parent).as_posix(),
               **{key: row['sourceObject'][key] for key in GUARD_KEYS}} for row in metadata['files']]
    trees = []
    if metadata['sourceIsDirectory']:
        members = sorted(row['path'] for row in guards)
        trees.append({'roots': [source.name], 'suffixes': ['.md', '.txt'], 'members': members,
                      'membershipDigest': digest(canonical_json(members).encode())})
    return [{'root': str(parent), 'guards': guards, 'treeGuards': trees}]


def _read_append(root: Path, operation_id: str, expected_hash: str) -> tuple[dict, bool]:
    require_no_pending(root)
    plan_path, journal_path, _ = _operation_paths(root, operation_id)
    if journal_path.is_file():
        from .operation_receipts import require_committed_operation
        journal = require_committed_operation(root, operation_id, plan_hash=expected_hash)
        relative = f'{_evidence_root(operation_id)}/receipt.json'
        _, raw = observation(safe_project_path(root, relative))
        if (journal.get('operationId') != operation_id or journal.get('operation') != OPERATION
                or journal.get('planHash') != expected_hash or raw is None
                or journal.get('afterFiles', {}).get(relative, {}).get('sha256') != digest(raw)):
            raise StructureError('APPEND_PLAN_INVALID', 'append receipt is missing, damaged or belongs to another plan')
        result = json.loads(raw)
        result['planHash'] = expected_hash
        return result, True
    if not plan_path.is_file():
        raise StructureError('PLAN_NOT_FOUND', 'append preview does not exist')
    result = read_json(plan_path)
    if (result.get('operationId') != operation_id or result.get('operation') != OPERATION
            or result.get('planHash') != expected_hash
            or expected_hash != _plan_hash({k: v for k, v in result.items() if k != 'planHash'})):
        raise StructureError('PLAN_HASH_MISMATCH', 'append preview changed')
    return result, False


def inspect_append(root: Path, *, operationId: str, planHash: str, offset: int = 0,
                   limit: int | None = None) -> dict:
    root = root.expanduser().resolve()
    plan, committed = _read_append(root, operationId, planHash)
    metadata = plan['appendManuscript']
    policy = query_policy()
    limit = policy['defaultLimit'] if limit is None else limit
    if type(offset) is not int or not 0 <= offset <= len(metadata['chapters']) or type(limit) is not int or not 1 <= limit <= policy['maxLimit']:
        raise StructureError('ARGUMENTS_INVALID', 'invalid append page offset or limit')
    args = {'projectRoot': str(root), 'operationId': operationId, 'planHash': planHash}
    result = {'ok': True, 'code': 'MANUSCRIPT_APPEND_RECEIPT' if committed else 'MANUSCRIPT_APPEND_PREVIEW',
        **args, 'requestId': plan['requestId'], 'baseRevision': plan['baseRevision'],
        'fileCount': len(metadata['files']), 'chapterCount': len(metadata['chapters']),
        'sourceBytes': sum(row['bytes'] for row in metadata['files']), 'preservesAllText': True,
        'chapterStatus': 'planning_required', 'semanticReviewComplete': False,
        'alreadyCommitted': committed, 'historicalReceipt': committed,
        'placement': plan['parameters']['placement'], 'primaryArcId': plan['parameters']['primaryArcId'],
        'beforeChapterId': plan['parameters']['beforeChapterId'],
        'guidance': '核对分章、插入位置与卷弧归属；采用后新章保留原文，设定、规划及审阅待整理。',
        'chapters': [], 'nextOffset': len(metadata['chapters']),
        'readAction': {'operation': 'import.append-inspect', 'arguments': {**args, 'offset': len(metadata['chapters']), 'limit': limit}}}
    if not committed:
        result['nextAction'] = {'operation': 'import.append-commit', 'arguments': {**args, 'approve': True}, 'requiresApproval': True}
    for index in range(offset, min(offset + limit, len(metadata['chapters']))):
        chapter = metadata['chapters'][index]
        row = {**chapter, 'ordinal': index + 1,
            'warnings': [note['message'] for note in metadata['warnings'] if note['sourcePath'] == chapter['sourcePath']],
            'readAction': {'operation': 'import.append-read', 'arguments': {
                **args, 'sourcePath': chapter['sourcePath'], 'offset': chapter['characterStart']}}}
        result['chapters'].append(row)
        if len(json.dumps(result, ensure_ascii=False).encode()) > policy['maxBytes']['list']:
            result['chapters'].pop()
            if not result['chapters']:
                raise StructureError('IMPORT_PAGE_TOO_LARGE', 'one chapter exceeds the response budget; shorten its title')
            break
    following = offset + len(result['chapters'])
    if following < len(metadata['chapters']):
        result['nextOffset'] = following
        result['readAction']['arguments']['offset'] = following
    else:
        result['nextOffset'] = None
        result.pop('readAction')
    return result


def read_append_text(root: Path, *, operationId: str, planHash: str, sourcePath: str, offset: int = 0) -> dict:
    root = root.expanduser().resolve()
    plan, committed = _read_append(root, operationId, planHash)
    metadata = plan['appendManuscript']
    source = next((row for row in metadata['files'] if row['sourcePath'] == sourcePath), None)
    if source is None:
        raise StructureError('IMPORT_SOURCE_INVALID', 'sourcePath is not in the append preview')
    _, raw = observation(safe_project_path(root, source['evidencePath' if committed else 'snapshotPath']))
    if raw is None or digest(raw) != source['sourceHash']:
        raise StructureError('IMPORT_SNAPSHOT_DAMAGED', 'frozen original manuscript changed')
    text = decode_source(raw, metadata['encoding'])
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise StructureError('ARGUMENTS_INVALID', 'offset must be a character position within the source')
    budget = query_policy()['maxBytes']['detail']
    end = min(len(text), offset + budget)
    while True:
        result = {'ok': True, 'code': 'MANUSCRIPT_APPEND_TEXT', 'operationId': operationId, 'planHash': planHash,
            'sourcePath': sourcePath, 'sourceHash': source['sourceHash'], 'offset': offset, 'end': end,
            'startLine': len(text[:offset].splitlines()) + (0 if offset and not text[:offset].endswith(('\n', '\r')) else 1),
            'text': text[offset:end], 'nextOffset': end if end < len(text) else None, 'historicalReceipt': committed}
        if end < len(text):
            result['readAction'] = {'operation': 'import.append-read', 'arguments': {'projectRoot': str(root),
                'operationId': operationId, 'planHash': planHash, 'sourcePath': sourcePath, 'offset': end}}
        if len(json.dumps(result, ensure_ascii=False).encode()) <= budget:
            return result
        if end == offset:
            raise StructureError('IMPORT_PAGE_TOO_LARGE', 'source metadata exceeds the response budget')
        end = offset + (end - offset) // 2


def commit_append(root: Path, *, operationId: str, planHash: str, approve: bool = False) -> dict:
    from .manuscript_materialization import commit_manuscript_plan
    root = root.expanduser().resolve()
    plan, _ = _read_append(root, operationId, planHash)
    result = commit_manuscript_plan(root, operationId=operationId, planHash=planHash, approve=approve)
    metadata = plan['appendManuscript']
    return {**{key: result[key] for key in ('ok', 'operationId', 'planHash', 'beforeRevision', 'afterRevision',
            'alreadyCommitted', 'replayed', 'historical', 'details', 'semanticFollowUp') if key in result},
        'ok': True, 'code': 'MANUSCRIPT_APPENDED', 'projectRoot': str(root),
        'chapterCount': len(metadata['chapters']), 'fileCount': len(metadata['files']),
        'proseBytes': sum(row['bytes'] for row in metadata['chapters']), 'preservesAllText': True,
        'chapterStatus': 'planning_required', 'semanticReviewComplete': False,
        'receiptPath': f'{_evidence_root(operationId)}/receipt.json',
        'readAction': {'operation': 'import.append-inspect', 'arguments': {'projectRoot': str(root),
            'operationId': operationId, 'planHash': planHash}},
        'nextAction': {'operation': 'project.doctor', 'arguments': {'projectRoot': str(root)}}}
