"""Read current owned prose and publish a protected, on-demand manuscript copy."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .commands import EXPORT_STATUSES, resolve_export_target
from .jsonio import _new_file_mode
from .locks import project_lock_reentrant
from .paths import ProjectPathError
from .prose_binding import (LOCATOR_DRIFT, OWNED_CLEAN, UNBOUND,
                            pending_transaction_present, require_observation)
from .source_model import require_current_source, snapshot_hash
from .structure_store import StructureError, ordered_chapters, validate_structure
from .write_sets import guard_observation, safe_project_path, validate_guards

try:
    from ..text_utils import chinese_char_count
except ImportError:
    from text_utils import chinese_char_count


def checkpoint(stage: str) -> None:
    """Fault-injection seam; production execution has no hook."""


def _require_ready(root: Path) -> None:
    # Export may be entered through a transport already holding the project lock.
    # Owning that lock does not make an interrupted transaction's bytes authoritative.
    if pending_transaction_present(root):
        raise StructureError('TRANSACTION_INCOMPLETE', 'recover the pending transaction before exporting prose')


@contextmanager
def _output_directory(target: Path):
    """Anchor every output operation to a directory opened without following links."""
    fd = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as exc:
        raise StructureError('EXPORT_WRITE_FAILED', 'cannot safely publish the export',
                             {'path': str(target), 'error': str(exc)}) from exc
    finally:
        os.close(fd)


def _slot(fd: int, name: str) -> tuple[dict | None, bytes | None]:
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise StructureError('EXPORT_TARGET_UNSAFE', 'export target cannot be read without following links',
                             {'name': name}) from exc
    with os.fdopen(handle, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise StructureError('EXPORT_TARGET_NOT_FILE', 'export target is not a regular file', {'name': name})
        data = stream.read()
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise StructureError('EXPORT_TARGET_CONFLICT', 'export target changed while being inspected', {'name': name})
    return {'sha256': hashlib.sha256(data).hexdigest(), 'dev': before.st_dev,
            'ino': before.st_ino, 'mode': stat.S_IMODE(before.st_mode)}, data


def _directory_current(fd: int, target: Path) -> None:
    current_fd = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child
        current = os.fstat(current_fd)
    except OSError as exc:
        raise StructureError('EXPORT_TARGET_CONFLICT', 'export directory moved during publication') from exc
    finally:
        os.close(current_fd)
    opened = os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise StructureError('EXPORT_TARGET_CONFLICT', 'export directory changed during publication')


def _publish(target: Path, payload: bytes, *, overwrite: bool, expected_file_hash: str | None,
             validate_inputs) -> dict:
    """Never replace an uninspected destination; keep the old output when replacing it.

    A rename first preserves the actual destination object, then an exclusive link
    publishes the prepared copy. If another writer occupies the brief empty slot,
    publication fails and both objects survive. This does not lock external editors.
    """
    with _output_directory(target) as fd:
        before, old = _slot(fd, target.name)
        current_hash = before['sha256'] if before else None
        if expected_file_hash is not None and expected_file_hash != current_hash:
            raise StructureError('EXPORT_TARGET_CONFLICT', 'target changed after inspection', {'sha256': current_hash})
        if old == payload:
            validate_inputs()
            _directory_current(fd, target)
            if _slot(fd, target.name)[0] != before:
                raise StructureError('EXPORT_TARGET_CONFLICT', 'target changed during export')
            return {}
        if before and (not overwrite or expected_file_hash is None):
            raise StructureError('EXPORT_TARGET_EXISTS', 'choose a new path or explicitly overwrite the inspected file',
                                 {'path': str(target), 'sha256': current_hash})

        temporary = f'.{target.name}.export-{uuid.uuid4().hex}'
        previous = f'{target.stem}.previous-{uuid.uuid4().hex}{target.suffix}'
        temporary_created = False
        moved = False
        previous_reserved = False
        try:
            handle = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
            temporary_created = True
            with os.fdopen(handle, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fchmod(stream.fileno(), before['mode'] if before else _new_file_mode(target))
                os.fsync(stream.fileno())
            checkpoint('prepared')
            validate_inputs()
            _directory_current(fd, target)
            if _slot(fd, target.name)[0] != before:
                raise StructureError('EXPORT_TARGET_CONFLICT', 'target changed during export')
            if before:
                handle = os.open(previous, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
                os.close(handle)
                previous_reserved = True
                checkpoint('before_preserve')
                os.rename(target.name, previous, src_dir_fd=fd, dst_dir_fd=fd)
                moved = True
                os.fsync(fd)
                preserved, _ = _slot(fd, previous)
                if preserved != before:
                    raise StructureError('EXPORT_TARGET_CONFLICT', 'target changed before it was preserved')
                checkpoint('preserved')
                validate_inputs()
                _directory_current(fd, target)
            checkpoint('before_publish')
            validate_inputs()
            _directory_current(fd, target)
            # Exclusive creation also protects a new target that appeared after inspection.
            try:
                os.link(temporary, target.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError as exc:
                raise StructureError('EXPORT_TARGET_CONFLICT', 'another writer occupied the export target') from exc
            os.fsync(fd)
            return {'previousOutput': str(target.with_name(previous))} if moved else {}
        except BaseException as exc:
            error = (StructureError('EXPORT_WRITE_FAILED', 'cannot safely publish the export',
                                    {'path': str(target), 'error': str(exc)})
                     if isinstance(exc, OSError) else exc)
            if moved:
                try:
                    os.link(previous, target.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except OSError:
                    if isinstance(error, StructureError):
                        error.details['previousOutput'] = str(target.with_name(previous))
                else:
                    os.unlink(previous, dir_fd=fd)
                    moved = False
                os.fsync(fd)
            raise error
        finally:
            for name in ([temporary] if temporary_created else []) + ([previous] if previous_reserved and not moved else []):
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass


def export_manuscript(root: Path, *, statuses: tuple[str, ...] = ('stable',), fmt: str = 'txt',
                      output: Path | None = None, overwrite: bool = False,
                      expected_file_hash: str | None = None) -> dict:
    if fmt not in {'txt', 'md'}:
        raise StructureError('INVALID_EXPORT_FORMAT', f'format must be txt or md: {fmt}')
    if 'quarantined' in statuses:
        raise StructureError('INVALID_EXPORT_STATUS', 'quarantined chapters cannot be exported',
                             {'status': 'quarantined'})
    unknown = [item for item in statuses if item not in EXPORT_STATUSES]
    if unknown:
        raise StructureError('INVALID_EXPORT_STATUS', 'unsupported chapter status', {'supported': list(EXPORT_STATUSES)})
    if expected_file_hash is not None and (not isinstance(expected_file_hash, str)
                                          or re.fullmatch('[0-9a-f]{64}', expected_file_hash) is None):
        raise StructureError('INVALID_EXPORT_HASH', 'expected_file_hash must be a lowercase SHA-256')
    with project_lock_reentrant(root):
        return _export_locked(root, statuses=statuses, fmt=fmt, output=output,
                              overwrite=overwrite, expected_file_hash=expected_file_hash)


def _export_locked(root: Path, *, statuses: tuple[str, ...], fmt: str, output: Path | None,
                   overwrite: bool, expected_file_hash: str | None) -> dict:
    _require_ready(root)
    source_path = '.govel/structure.json'
    state, source_payload = guard_observation(safe_project_path(root, source_path))
    guards = [{'path': source_path, **state}]
    if source_payload is None:
        raise StructureError('STRUCTURE_NOT_INITIALIZED', 'missing readable .govel/structure.json')
    try:
        structure = json.loads(source_payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StructureError('STRUCTURE_INVALID', 'cannot read current structure') from exc
    validate_structure(structure)
    require_current_source(structure)
    validate_guards(root, guards, stage='export_loaded')
    parts, chapters_meta, skipped, missing = [], [], [], []
    for chapter in ordered_chapters(structure):
        identity = {'chapterId': chapter['chapterId'], 'displayCode': chapter.get('displayCode', '')}
        if chapter.get('status') == 'quarantined':
            skipped.append({**identity, 'reason': 'quarantined'})
            continue
        if chapter.get('status') not in statuses:
            skipped.append({**identity, 'reason': f"status={chapter.get('status')}"})
            continue
        relative = chapter['filePath']
        path = safe_project_path(root, relative)
        observed = require_observation(root, structure, chapter, (OWNED_CLEAN, LOCATOR_DRIFT, UNBOUND),
                                       pending=False, action='manuscript export')
        state, payload = guard_observation(path)
        guards.append({'path': relative, **state})
        if not state['exists'] and observed['state'] in {LOCATOR_DRIFT, UNBOUND}:
            row = {**identity, 'reason': 'prose file missing', 'observedState': observed['state']}
            missing.append(row)
            skipped.append(row)
            continue
        if payload is None or observed['state'] != OWNED_CLEAN or f"sha256:{state['sha256']}" != observed['expectedContentDigest']:
            raise StructureError('PROSE_EXTERNAL_MODIFICATION', 'prose changed between ownership check and export read', identity)
        try:
            body = payload.decode('utf-8').strip()
        except UnicodeDecodeError as exc:
            raise StructureError('PROSE_ENCODING_INVALID', 'owned prose is not valid UTF-8', identity) from exc
        title = str(chapter.get('title', '')).strip()
        heading = f"{identity['displayCode']} {title}".strip()
        parts.append(f"{'# ' if fmt == 'md' else ''}{heading}\n\n{body}")
        chapters_meta.append({**identity, 'title': title, 'status': chapter.get('status'),
                              'hanCharCount': chinese_char_count(body), 'contentHash': state['sha256'],
                              'bindingGeneration': observed['bindingGeneration'], 'observedState': observed['state']})
    if not chapters_meta:
        raise StructureError('NOTHING_TO_EXPORT', 'no readable owned chapter matched the requested statuses',
                             {'statuses': list(statuses), 'skipped': skipped})
    book_name = structure.get('bookName', '未命名')
    notices = []
    if missing:
        notices.append('【部分导出】以下已选章节正文缺失，未收入本文件：' + '、'.join(row['displayCode'] or row['chapterId'] for row in missing))
    if any(row['status'] != 'stable' for row in chapters_meta):
        notices.append('【含非稳定章节】本文件包含草稿或审校中章节；章节归属核验不代表已经定稿。')
    text = book_name + '\n\n' + ''.join(line + '\n\n' for line in notices) + '\n\n\n'.join(parts) + '\n'
    try:
        target = resolve_export_target(root, output, book_name=book_name, fmt=fmt)
    except ProjectPathError as exc:
        raise StructureError(exc.code, str(exc), exc.details) from exc

    def validate_inputs():
        _require_ready(root)
        validate_guards(root, guards, stage='export_publish')

    payload = text.encode('utf-8')
    publication = _publish(target, payload, overwrite=overwrite, expected_file_hash=expected_file_hash,
                           validate_inputs=validate_inputs)
    return {'code': 'MANUSCRIPT_PARTIALLY_EXPORTED' if missing else 'MANUSCRIPT_EXPORTED',
            'bookName': book_name, 'output': str(target), 'format': fmt, 'statuses': list(statuses),
            'complete': not missing, 'missingChapters': missing, 'chapterCount': len(chapters_meta),
            'hanCharCount': sum(row['hanCharCount'] for row in chapters_meta), 'chapters': chapters_meta,
            'skipped': skipped, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload),
            'snapshot': {'revision': structure['revision'], 'sourceHash': snapshot_hash(structure)}, **publication}


def export_candidate_batch(root: Path, *, batchRef: dict, output: str, fmt: str = 'md',
                           overwrite: bool = False, expectedFileHash: str | None = None) -> dict:
    """Explicit candidate reading snapshot; formal manuscript export is unchanged."""
    from .candidate_batch import batch_record
    from .finalization import _load_workflow
    from .structure_store import load_structure
    from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
    if fmt not in {'md', 'txt'}:
        raise StructureError('INVALID_EXPORT_FORMAT', 'candidate reading format must be md or txt')
    root = Path(root).expanduser().resolve()
    with project_lock_reentrant(root), observe_reads(root) as observed:
        _require_ready(root)
        dependencies = capture_dependencies(root)
        source, workflow = load_structure(root), _load_workflow(root)
        batch_record(root, source, batchRef)
        from .candidate_batch import candidate_reading
        progress, dependencies = candidate_reading(root, batchRef=batchRef, _with_dependencies=True)
        parts, identities = [], []
        for row in progress['chapters']:
            cid = row['chapterId']
            lines = [f"{'# ' if fmt == 'md' else ''}{row['displayCode']} {row['title']}",
                     f"候选状态：{row['state']}；不是正式定稿。"]
            if row['text'] is not None:
                identity = {key: row.get(key) for key in ('chapterId', 'artifactSha256', 'candidateSelection')}
                identities.append(identity)
                lines.extend(['候选身份：' + json.dumps(identity, ensure_ascii=False), row['text']])
                if row.get('suggestions'):
                    lines.append('随稿审校意见（历史报告的适用性以候选状态为准）：')
                    lines.extend(f"- {finding.get('severity', '')} {finding.get('summary', '')} {finding.get('repairSuggestion', '')}"
                                 for finding in row['suggestions'])
                if row.get('writingNotes'):
                    lines.append('写作说明：' + json.dumps(row['writingNotes'], ensure_ascii=False))
            else:
                lines.append('本章尚未开始或当前稿不可读取，暂无可交付候选。')
            if row.get('blocker'):
                lines.append('待完成：' + row['blocker']['code'] + ' · ' + row['blocker']['message'])
            parts.append('\n\n'.join(lines))
        text = source['bookName'] + '\n\n【候选阅读快照】' + ('完整候选交付' if progress['complete'] else '部分交付；未完成章节与原因列于各章')
        text += '\n\n' + '\n\n\n'.join(parts) + '\n\n续办：' + json.dumps(progress['resumeAction'], ensure_ascii=False) + '\n'
        target = resolve_export_target(root, Path(output), book_name=source['bookName'], fmt=fmt)
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='CANDIDATE_EXPORT_STALE')
        def check():
            _require_ready(root)
            validate_dependencies(root, dependencies, code='CANDIDATE_EXPORT_STALE')
        raw = text.encode('utf-8')
        published = _publish(target, raw, overwrite=overwrite, expected_file_hash=expectedFileHash, validate_inputs=check)
        return {'ok': True, 'code': 'CANDIDATE_BATCH_EXPORTED', 'complete': progress['complete'],
                'output': str(target), 'sha256': hashlib.sha256(raw).hexdigest(), 'chapters': progress['chapters'],
                'candidateIdentities': identities, 'resumeAction': progress['resumeAction'], 'formalApprovalPerformed': False, **published}
