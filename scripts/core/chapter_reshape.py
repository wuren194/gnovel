"""Preview and read chapter split/merge through the shared structure transaction."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .artifacts import sha256_json
from .chapter_reshape_model import OPERATIONS, build_reshape, content_parts, source_chapters
from .consistency import require_no_pending
from .jsonio import read_json, write_bytes
from .locks import project_lock
from .prose_binding import OWNED_CLEAN, require_observation
from .source_model import require_current_source
from .source_requests import request_identity, resume_request
from .structure_commands import _base_plan, _operation_paths, _plan_hash, _write_plan_locked
from .structure_store import StructureError, load_structure, new_id
from .write_sets import digest, guard_observation, safe_project_path, validate_guards


def _read_sources(root: Path, source: dict, chapters: list[dict]) -> tuple[list[str], list[dict]]:
    texts, guards = [], []
    for chapter in chapters:
        require_observation(root, source, chapter, (OWNED_CLEAN,), action='chapter split/merge')
        state, payload = guard_observation(safe_project_path(root, chapter['filePath']))
        if payload is None or 'sha256:' + state['sha256'] != chapter['proseBinding']['expectedContentDigest']:
            raise StructureError('CHAPTER_RESHAPE_SOURCE_CHANGED', 'source prose changed while preparing chapter reshape',
                                 {'chapterId': chapter['chapterId']})
        try:
            texts.append(payload.decode('utf-8'))
        except UnicodeDecodeError as exc:
            raise StructureError('CHAPTER_RESHAPE_ENCODING_INVALID', 'owned source prose must be valid UTF-8',
                                 {'chapterId': chapter['chapterId']}) from exc
        guards.append({'path': chapter['filePath'], **state})
    return texts, guards


def _preview(root: Path, operation: str, request_id: str, arguments: dict, base_revision: int | None) -> dict:
    root = root.expanduser().resolve()
    require_no_pending(root)
    with project_lock(root):
        source = load_structure(root)
        require_current_source(source)
        operation_id, _ = request_identity(source, request_id)
        fingerprint = sha256_json({'operation': operation, 'arguments': arguments, 'baseRevision': base_revision})
        prior = resume_request(root, source, operation_id, fingerprint)
        if prior is not None:
            return _report(prior, root)
        if base_revision is not None and (type(base_revision) is not int or base_revision != source['revision']):
            raise StructureError('REVISION_CONFLICT', 'project changed before chapter reshape')
        chapters = source_chapters(source, arguments['sourceChapterIds'])
        texts, guards = _read_sources(root, source, chapters)
        titles, _, _ = content_parts(operation, chapters, texts, arguments)
        created_ids = [new_id('chapter') for _ in titles]
        proposed, lineage, payloads, summary = build_reshape(source, operation=operation, arguments=arguments,
            chapters=chapters, texts=texts, created_ids=created_ids)
        parameters = {'reshapeArguments': deepcopy(arguments), 'createdChapterIds': created_ids,
                      'chapterId': created_ids[0], 'reason': arguments['reason'],
                      'identityMap': {f'$chapter{index + 1}': cid for index, cid in enumerate(created_ids)}}
        plan = _base_plan(source, operation=operation, actor='app', parameters=parameters,
                          impact={'chapterReshape': lineage, 'reshapeSummary': summary}, proposed=proposed)
        plan.update(operationId=operation_id, requestId=request_id, requestFingerprint=fingerprint,
                    reshapeSourceGuards=guards, manuscriptMaterialization=[])
        for row, payload in zip(lineage, payloads):
            relative = f".govel/plans/{operation_id}.intake/chapters/{row['chapterId']}.md"
            path = safe_project_path(root, relative)
            if path.exists() and path.read_bytes() != payload:
                raise StructureError('CHAPTER_RESHAPE_SNAPSHOT_CONFLICT', 'frozen chapter material already differs')
            write_bytes(path, payload)
            plan['manuscriptMaterialization'].append({
                'chapterId': row['chapterId'], 'filePath': row['filePath'],
                'sourcePath': relative, 'contentDigest': row['contentDigest']})
        validate_guards(root, guards, stage='chapter-reshape-preview')
        return _report(_write_plan_locked(root, plan), root)


def preview_split(root: Path, *, requestId: str, chapterId: str, cutOffsets: list[int] | None = None,
                  cutBefore: list[str] | None = None,
                  titles: list[str] | None = None, reason: str, target: dict | None = None,
                  baseRevision: int | None = None) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise StructureError('CHAPTER_RESHAPE_REASON_REQUIRED', 'state the author-requested reason for the split')
    arguments = {'sourceChapterIds': [chapterId], 'titles': titles, 'reason': reason, 'target': target}
    if cutOffsets is not None:
        arguments['cutOffsets'] = cutOffsets
    if cutBefore is not None:
        arguments['cutBefore'] = cutBefore
    return _preview(root, 'chapter_split', requestId, arguments, baseRevision)


def preview_merge(root: Path, *, requestId: str, chapterIds: list[str], title: str, reason: str,
                  separator: str = '', target: dict | None = None, baseRevision: int | None = None) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise StructureError('CHAPTER_RESHAPE_REASON_REQUIRED', 'state the author-requested reason for the merge')
    arguments = {'sourceChapterIds': chapterIds, 'title': title, 'separator': separator,
                 'reason': reason, 'target': target}
    return _preview(root, 'chapter_merge', requestId, arguments, baseRevision)


def validate_reshape_plan(root: Path, current: dict, plan: dict) -> list[dict]:
    """Rebuild the complete allowed delta and exact text at both transaction boundaries."""
    if plan.get('operation') not in OPERATIONS:
        return []
    parameters = plan.get('parameters', {})
    arguments = parameters.get('reshapeArguments')
    created_ids = parameters.get('createdChapterIds')
    if not isinstance(arguments, dict) or not isinstance(created_ids, list):
        raise StructureError('CHAPTER_RESHAPE_PLAN_INVALID', 'reshape plan has no frozen inputs or chapter identities')
    chapters = source_chapters(current, arguments.get('sourceChapterIds'))
    texts, observed = _read_sources(root, current, chapters)
    guards = plan.get('reshapeSourceGuards')
    if guards != observed:
        raise StructureError('CHAPTER_RESHAPE_SOURCE_CHANGED', 'source prose bytes or file object changed after preview')
    expected, lineage, payloads, summary = build_reshape(current, operation=plan['operation'], arguments=arguments,
        chapters=chapters, texts=texts, created_ids=created_ids)
    from .author_decisions import prepare_decision_transition
    prepare_decision_transition(current, expected, operation=plan['operation'], operation_id=plan['operationId'],
                                parameters=parameters)
    proposed = plan.get('proposedStructure')
    if not isinstance(proposed, dict):
        raise StructureError('CHAPTER_RESHAPE_PLAN_INVALID', 'reshape plan has no proposed structure')
    ignored = {'updatedAt', 'lastOperation'}
    if ({k: v for k, v in expected.items() if k not in ignored}
            != {k: v for k, v in proposed.items() if k not in ignored}):
        raise StructureError('CHAPTER_RESHAPE_SCOPE_INVALID', 'reshape plan changed data outside its exact chapter transformation')
    impact = plan.get('impact', {})
    if impact.get('chapterReshape') != lineage or impact.get('reshapeSummary') != summary:
        raise StructureError('CHAPTER_RESHAPE_LINEAGE_INVALID', 'published source lineage must match the actual transformation')
    materializations = plan.get('manuscriptMaterialization')
    expected_rows = [{'chapterId': row['chapterId'], 'filePath': row['filePath'],
                      'sourcePath': f".govel/plans/{plan['operationId']}.intake/chapters/{row['chapterId']}.md",
                      'contentDigest': row['contentDigest']} for row in lineage]
    if materializations != expected_rows:
        raise StructureError('CHAPTER_RESHAPE_PLAN_INVALID', 'new prose must match the exact transformed chapter inventory')
    for row, payload in zip(materializations, payloads):
        path = safe_project_path(root, row['sourcePath'])
        state, frozen = guard_observation(path)
        if frozen != payload or state['sha256'] != row['contentDigest']:
            raise StructureError('CHAPTER_RESHAPE_SNAPSHOT_CHANGED', 'frozen result differs from the original chapter transformation')
    return deepcopy(guards)


def _report(report: dict, root: Path) -> dict:
    if report.get('historical'):
        from .operation_receipts import require_committed_operation
        require_committed_operation(root, report['operationId'], plan_hash=report['planHash'])
        return report
    result = {**report, 'code': 'CHAPTER_RESHAPE_PREVIEW'}
    result['nextAction'] = {'operation': 'chapter.reshape-commit', 'tool': 'commit_chapter_reshape',
        'requiresExplicitApproval': True, 'arguments': {'projectRoot': str(root),
            'operationId': report['operationId'], 'planHash': report['planHash'], 'approve': True}}
    result['readAction'] = {'operation': 'chapter.reshape-read', 'tool': 'read_chapter_reshape',
        'arguments': {'projectRoot': str(root), 'operationId': report['operationId'], 'planHash': report['planHash']},
        'requiredInputs': ['chapterId'], 'chapterIdsFrom': 'impact.chapterReshape'}
    if 'chapterReshape' not in report.get('impact', {}):
        result['readAction']['chapterIdsFrom'] = 'chapterIdsAction.items with category=chapterReshape'
        result['readAction']['chapterIdsAction'] = {'operation': 'source.change-report', 'tool': 'query_source_change_report',
            'arguments': {'projectRoot': str(root), 'operationId': report['operationId'], 'section': 'impact', 'view': 'records'}}
    return result


def _record(root: Path, operation_id: str, plan_hash: str) -> tuple[dict, bool]:
    plan_path, journal_path, _ = _operation_paths(root, operation_id)
    committed = journal_path.is_file()
    path = journal_path if committed else plan_path
    if not path.is_file():
        raise StructureError('PLAN_NOT_FOUND', 'unknown chapter reshape')
    record = read_json(path)
    if (not isinstance(record, dict) or record.get('operationId') != operation_id
            or record.get('operation') not in OPERATIONS or record.get('planHash') != plan_hash):
        raise StructureError('CHAPTER_RESHAPE_PLAN_INVALID', 'request does not identify the reviewed chapter reshape')
    if not committed and record['planHash'] != _plan_hash({key: value for key, value in record.items() if key != 'planHash'}):
        raise StructureError('PLAN_HASH_MISMATCH', 'stored chapter reshape plan changed')
    if committed:
        from .operation_receipts import require_committed_operation
        record = require_committed_operation(root, operation_id, plan_hash=plan_hash)
    return record, committed


def commit_reshape(root: Path, *, operationId: str, planHash: str, approve: bool) -> dict:
    from .manuscript_materialization import commit_manuscript_plan
    root = root.expanduser().resolve()
    _record(root, operationId, planHash)
    return commit_manuscript_plan(root, operationId=operationId, planHash=planHash, approve=approve)


def read_reshape(root: Path, *, operationId: str, planHash: str, chapterId: str,
                 limit=None, maxBytes=None, cursor=None) -> dict:
    from .project import query_policy
    from .query import query_bounds
    from .source_review import fingerprint
    from .text_pages import text_page
    root = root.expanduser().resolve()
    require_no_pending(root)
    record, committed = _record(root, operationId, planHash)
    row = next((r for r in record.get('impact', {}).get('chapterReshape', []) if r['chapterId'] == chapterId), None)
    if row is None:
        raise StructureError('CHAPTER_NOT_FOUND', 'chapter does not belong to this reshape preview')
    relative = f'.govel/plans/{operationId}.intake/chapters/{chapterId}.md'
    _, payload = guard_observation(safe_project_path(root, relative))
    if payload is None or digest(payload) != row['contentDigest']:
        raise StructureError('CHAPTER_RESHAPE_SNAPSHOT_CHANGED', 'frozen chapter result is missing or changed')
    count, budget = query_bounds(limit=limit, maxBytes=maxBytes)
    snapshot = {'operationId': operationId, 'planHash': planHash, 'chapterId': chapterId, 'contentDigest': row['contentDigest']}
    return text_page({'ok': True, 'code': 'CHAPTER_RESHAPE_TEXT', 'snapshot': snapshot,
                      'title': row['title'], 'historical': True, 'committed': committed},
                     [{'id': chapterId, 'text': payload.decode('utf-8')}], limit=count, budget=budget, cursor=cursor,
                     key='items', source=fingerprint({'snapshot': snapshot, 'policy': query_policy()}),
                     binding=fingerprint({'limit': count, 'maxBytes': budget, 'code': 'CHAPTER_RESHAPE_TEXT'}))


def query_lineage(root: Path, *, chapterId: str, limit=None, maxBytes=None, cursor=None) -> dict:
    """Read lineage from committed operations; previews cannot claim an origin."""
    from .source_review import bounded_page, fingerprint
    from .source_requests import operation_undone
    from .operation_receipts import require_committed_operation
    root = root.expanduser().resolve()
    require_no_pending(root)
    source = load_structure(root)
    if not any(row['chapterId'] == chapterId for row in source['chapters']):
        raise StructureError('CHAPTER_NOT_FOUND', 'chapter does not exist')
    rows = []
    directory = safe_project_path(root, '.govel/operations')
    if directory.exists():
        for path in sorted(directory.glob('OP-*.json')):
            relative = path.relative_to(root).as_posix()
            path = safe_project_path(root, relative)
            try:
                record = read_json(path)
            except (ValueError, OSError, UnicodeError) as exc:
                raise StructureError('CHAPTER_RESHAPE_HISTORY_INVALID', 'operation history cannot be read',
                                     {'path': relative}) from exc
            if not isinstance(record, dict):
                raise StructureError('CHAPTER_RESHAPE_HISTORY_INVALID', 'operation history must contain an object',
                                     {'path': relative})
            if record.get('operation') not in OPERATIONS:
                continue
            record = require_committed_operation(root, path.stem)
            for row in record.get('impact', {}).get('chapterReshape', []):
                if row['chapterId'] == chapterId or any(item['chapterId'] == chapterId for item in row['sources']):
                    rows.append({'operationId': record['operationId'], 'operation': record['operation'],
                        'committedAt': record['committedAt'], 'afterRevision': record['afterRevision'],
                        'operationUndone': operation_undone(root, record['operationId']), **row})
    require_no_pending(root)
    if load_structure(root) != source:
        raise StructureError('CHAPTER_RESHAPE_HISTORY_CHANGED', 'project changed while reading chapter history; read again')
    return bounded_page(rows, snapshot={'projectId': source['projectId'], 'revision': source['revision'],
                        'historyHash': fingerprint(rows)}, selection={'chapterId': chapterId},
                        code='CHAPTER_RESHAPE_LINEAGE', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        historical=True, coverage={'source': 'committed_operation_journals', 'approvalInherited': False})
