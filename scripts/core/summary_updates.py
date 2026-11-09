"""Prepared, compare-and-swap summary updates; causal prose remains caller supplied."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path

from .artifacts import sha256_json
from .consistency import require_no_pending
from .locks import project_lock_reentrant
from .project import query_policy
from .structure_store import StructureError, load_structure
from .summary_contract import validate_summary
from .transport_cache import get, put
from .write_sets import (apply_write_set, capture_guards, digest, guard_observation,
                         inventory, json_bytes, safe_project_path, tree_membership)

SUMMARY_PATH = '.gnovel/aux/story-summary.json'
HISTORY_PATH = '.gnovel/aux/story-summary-history'
MACHINE_FIELDS = ('schemaVersion', 'projectId', 'asOfChapterId', 'structureRevision',
                  'coveredChapterIds', 'sourceCommitId', 'asOfDisplayOrdinal')


def prepare_update(root, report, dependencies, baseline):
    if not report.get('asOfChapterId') or 'inputSkeleton' not in report:
        return report
    skeleton = report['inputSkeleton']
    inputs = report.get('inputSnapshot')
    if inputs is None:
        inputs = get(root, report['inputSnapshotRef'], kind='summary')
    value = {'machine': {key: skeleton[key] for key in MACHINE_FIELDS},
             'inputSnapshot': inputs, 'base': baseline, 'dependencies': dependencies,
             'characterIds': [row['characterId'] for row in report.get('characters', [])]}
    reference = put(root, 'summary-preparation', value)
    report['prepareRef'] = reference
    report['inputSkeleton']['prepareRef'] = reference
    report['deltaSkeleton'] = {'prepareRef': reference, 'storySummary': '', 'plotLine': [], 'characterSummaries': []}
    report['priorSummaryVersion'] = baseline['sha256']
    report['instruction'] = ('阅读 chapters 指定的正文并由你重述 storySummary 的完整因果。可用 deltaSkeleton 按稳定 ID '
        '新增、替换或明确删除 plotLine/characterSummaries；未改条目由工具保留。'
        'prepareRef 同时绑定准备输入及旧摘要版本。完整 inputSkeleton 提交继续支持；机器绑定不能代替语义审查。')
    return report


def _load_prior(root):
    state, raw = guard_observation(safe_project_path(root, SUMMARY_PATH))
    if state['exists'] and raw is None:
        raise StructureError('SUMMARY_INVALID', 'summary target is not a readable regular file')
    return state, raw


def _resolve(root, reference):
    prepared = get(root, reference, kind='summary-preparation')
    from .prepare_dependencies import validate_dependencies
    current, raw = _load_prior(root)
    if current != prepared['base']:
        raise StructureError('SUMMARY_VERSION_CONFLICT', 'summary changed after preparation; prepare again',
                             {'expected': prepared['base']['sha256'], 'current': current['sha256']})
    validate_dependencies(root, prepared['dependencies'], code='SUMMARY_INPUT_CHANGED')
    if prepared['machine']['projectId'] != load_structure(root)['projectId']:
        raise StructureError('SUMMARY_INPUT_CHANGED', 'prepared summary belongs to another project identity')
    return prepared, raw


def _patch_entries(prior, updates, field, identity, allowed):
    if not isinstance(updates, list):
        raise StructureError('SUMMARY_DELTA_INVALID', f'{field} must be an array')
    entries = {row[identity]: deepcopy(row) for row in prior}
    order, touched = [row[identity] for row in prior], set()
    for index, update in enumerate(updates):
        path = f'{field}[{index}]'
        if not isinstance(update, dict) or not isinstance(update.get(identity), str):
            raise StructureError('SUMMARY_DELTA_INVALID', f'{path}.{identity} must be a stable ID')
        rid, op = update[identity], update.get('op')
        keys = {'op', identity} if op == 'delete' else {'op', identity, 'summary'}
        optional = {'title'} if field == 'plotLine' and op != 'delete' else set()
        if set(update) - keys - optional or not keys <= set(update) or op not in {'add', 'replace', 'delete'}:
            raise StructureError('SUMMARY_DELTA_INVALID', f'{path} has invalid operation fields')
        if rid in touched:
            raise StructureError('SUMMARY_DELTA_INVALID', f'{path}.{identity} is duplicated')
        touched.add(rid)
        if (op == 'add' and rid in entries) or (op != 'add' and rid not in entries):
            raise StructureError('SUMMARY_DELTA_CONFLICT', f'{path} does not match an existing entry')
        if op == 'delete':
            del entries[rid]
            order.remove(rid)
            continue
        if rid not in allowed or not isinstance(update['summary'], str) or not update['summary'].strip():
            raise StructureError('SUMMARY_DELTA_INVALID', f'{path} requires a current stable ID and explicit summary')
        if 'title' in update and not isinstance(update['title'], str):
            raise StructureError('SUMMARY_DELTA_INVALID', f'{path}.title must be text')
        entries[rid] = {key: value for key, value in update.items() if key != 'op'}
        if op == 'add':
            order.append(rid)
    unknown = set(entries) - allowed
    if unknown:
        raise StructureError('SUMMARY_DELTA_DELETE_REQUIRED', f'{field} retains removed identities; delete explicitly',
                             {'ids': sorted(unknown)})
    return [entries[rid] for rid in order]


def commit_delta(root: Path, *, prepareRef: str, storySummary: str, plotLine=None, characterSummaries=None,
                 reviewReason=None, _closeout_request=None):
    root = root.expanduser().resolve()
    with project_lock_reentrant(root):
        require_no_pending(root)
        prepared, raw = _resolve(root, prepareRef)
        try:
            prior = json.loads(raw) if raw is not None else {}
            if raw is not None:
                validate_summary(prior)
        except (ValueError, UnicodeDecodeError) as exc:
            raise StructureError('SUMMARY_INVALID', 'invalid prior summary requires a complete refreshed submission') from exc
        if not isinstance(storySummary, str) or not storySummary.strip():
            raise StructureError('SUMMARY_DELTA_INVALID', 'storySummary requires explicit causal restatement')
        payload = {**prepared['machine'], 'storySummary': storySummary, 'inputSnapshot': prepared['inputSnapshot']}
        for field, identity, allowed, updates in (
                ('plotLine', 'chapterId', set(prepared['machine']['coveredChapterIds']), plotLine),
                ('characterSummaries', 'characterId', set(prepared['characterIds']), characterSummaries)):
            payload[field] = _patch_entries(prior.get(field, []), [] if updates is None else updates, field, identity, allowed)
        validate_summary(payload)
        return persist_summary(root, payload, prepare_ref=prepareRef, review_reason=reviewReason,
                               closeout_request=_closeout_request)


def persist_summary(root, payload, *, prepare_ref=None, review_reason=None, closeout_request=None):
    require_no_pending(root)
    if review_reason is not None:
        from .continuity import _reason
        if not _reason(review_reason):
            raise StructureError('SUMMARY_REVIEW_REASON_INVALID', 'provide the actual summary review reason')
    before, old = _load_prior(root)
    from .prepare_dependencies import capture_dependencies, validate_dependencies
    dependencies = capture_dependencies(root)
    if prepare_ref is not None:
        prepared, old = _resolve(root, prepare_ref)
        for key in MACHINE_FIELDS:
            if payload.get(key) != prepared['machine'][key]:
                raise StructureError('SUMMARY_INPUT_CHANGED', f'{key} differs from the prepared binding')
        if payload.get('inputSnapshot') != prepared['inputSnapshot']:
            raise StructureError('SUMMARY_INPUT_CHANGED', 'summary input snapshot differs from preparation')
        dependencies = prepared['dependencies']
    files = [relative for relative in dependencies['files'] if relative != SUMMARY_PATH]
    guards = capture_guards(root, files)
    trees = [tree_membership(root, [query['directory']]) for query in dependencies['queries']
             if query['directory'] != '.gnovel/transactions']
    validate_dependencies(root, dependencies, code='SUMMARY_INPUT_CHANGED')
    if _load_prior(root)[0] != before:
        raise StructureError('SUMMARY_VERSION_CONFLICT', 'summary changed while assembling the update')
    saved = {key: value for key, value in payload.items() if key not in {'prepareRef', 'inputSnapshotRef'}}
    outputs = {SUMMARY_PATH: json_bytes(saved)}
    new_history = None
    if old is not None:
        history = f'{HISTORY_PATH}/{digest(old)}.json'
        target = safe_project_path(root, history)
        history_state, history_raw = guard_observation(target)
        if history_state['exists'] and history_raw != old:
            raise StructureError('SUMMARY_HISTORY_CONFLICT', 'historical summary bytes do not match their identity')
        if not history_state['exists']:
            outputs[history] = old
            new_history = history
        else:
            guards.append({'path': history, **history_state})
    continuity_summary = {'resolution': 'refreshed', 'sha256': digest(outputs[SUMMARY_PATH]),
                          'reason': review_reason or '请填写本次摘要已按当前正文复核的实际理由'}
    expected = inventory(root, outputs)
    for row in expected:
        if row['path'] == new_history:
            row['before'] = {'exists': False, 'sha256': None, 'mode': None}
    if closeout_request is not None:
        from .chapter_closeout import receipt_outputs
        receipt, request_guards, expected = receipt_outputs(root, closeout_request, 'summary', outputs,
            {'summaryVersion': digest(outputs[SUMMARY_PATH]), 'continuitySummary': continuity_summary}, expected=expected)
        outputs.update(receipt)
        guards.extend(request_guards)
    if next(row for row in expected if row['path'] == SUMMARY_PATH)['before']['sha256'] != before['sha256']:
        raise StructureError('SUMMARY_VERSION_CONFLICT', 'summary changed before writing')
    apply_write_set(root, outputs, expected=expected, operation='summary_commit', guards=guards, tree_guards=trees)
    return {'ok': True, 'path': SUMMARY_PATH, 'asOfChapterId': saved['asOfChapterId'],
            'sourceCommitId': saved.get('sourceCommitId'), 'summaryVersion': digest(outputs[SUMMARY_PATH]),
            'sha256': digest(outputs[SUMMARY_PATH]),
            'continuitySummary': continuity_summary,
            'readingExport': {'operation': 'summary.export', 'arguments': {'output': '工作台/故事摘要.md'}},
            'previousSummaryVersion': before['sha256'], 'semanticReviewComplete': False, 'approved': False}


def export_summary(root: Path, *, output: str, overwrite=False, expectedFileHash=None):
    """An explicit disposable reading snapshot, never a second maintained source."""
    from .commands import resolve_export_target
    from .jsonio import write_bytes
    root = root.expanduser().resolve()
    with project_lock_reentrant(root):
        require_no_pending(root)
        state, raw = _load_prior(root)
        if raw is None:
            raise StructureError('SUMMARY_NOT_FOUND', 'prepare and commit a summary before exporting')
        payload = json.loads(raw)
        validate_summary(payload)
        target = resolve_export_target(root, Path(output), book_name='故事摘要', fmt='md')
        lines = ['# 故事摘要', '', '> 阅读快照；修改此文件不会更新项目摘要。',
                 f'> 来源：{SUMMARY_PATH}；SHA-256：{state["sha256"]}', '', payload['storySummary'], '']
        for key, title, identity in (('plotLine', '章节推进', 'chapterId'),
                                     ('characterSummaries', '人物状态', 'characterId')):
            if payload.get(key):
                lines += ['## ' + title, '']
            for row in payload.get(key, []):
                lines += ['### ' + row.get('title', row[identity]), '', row['summary'], '']
        data = '\n'.join(lines).encode('utf-8')
        if target.exists():
            old = target.read_bytes()
            if expectedFileHash is not None and digest(old) != expectedFileHash:
                raise StructureError('SUMMARY_EXPORT_CONFLICT', 'reading snapshot changed after inspection')
            if old != data and (not overwrite or expectedFileHash is None):
                raise StructureError('SUMMARY_EXPORT_EXISTS', 'choose a new output or pass overwrite and expectedFileHash',
                                     {'sha256': digest(old)})
        write_bytes(target, data)
        return {'ok': True, 'code': 'SUMMARY_EXPORTED', 'output': str(target),
                'sha256': digest(data), 'sourceSha256': state['sha256'], 'maintained': False}


def read_summary(root: Path, *, version=None, view='detail', chapterId=None, characterId=None,
                 maxBytes=None, limit=None, cursor=None):
    root = root.expanduser().resolve()
    require_no_pending(root)
    from .source_review import bounded_page
    from .text_pages import text_page
    from .write_sets import validate_guards, validate_tree_guards
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    page_limit = policy['defaultLimit'] if limit is None else limit
    if (type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']
            or type(page_limit) is not int or not 1 <= page_limit <= policy['maxLimit']):
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid summary reading budget')
    if (view not in {'history', 'detail'}
            or version is not None and (not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{64}', version))
            or chapterId is not None and characterId is not None
            or any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (chapterId, characterId))
            or view == 'history' and any(value is not None for value in (version, chapterId, characterId))):
        raise StructureError('SUMMARY_QUERY_INVALID', 'select history, or detail with an optional version and one stable ID')
    if view == 'history':
        rows, guards = [], []
        membership = tree_membership(root, [HISTORY_PATH])
        base = safe_project_path(root, HISTORY_PATH)
        for path in sorted(base.glob('*.json')):
            if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
                raise StructureError('SUMMARY_HISTORY_CONFLICT', 'historical summary has an invalid identity')
            relative = path.relative_to(root).as_posix()
            state, raw = guard_observation(safe_project_path(root, relative))
            if raw is None or state['sha256'] != path.stem:
                raise StructureError('SUMMARY_HISTORY_CONFLICT', 'historical summary content changed')
            guards.append({'path': relative, **state})
            rows.append({'version': path.stem, 'path': relative, 'bytes': len(raw)})
        result = bounded_page(rows, snapshot={'historyHash': sha256_json(rows)}, selection={'view': view},
                              code='SUMMARY_HISTORY', limit=page_limit, maxBytes=budget, cursor=cursor)
        validate_guards(root, guards, stage='summary-history-read')
        validate_tree_guards(root, [membership], stage='summary-history-read')
    else:
        relative = SUMMARY_PATH if version is None else f'{HISTORY_PATH}/{version}.json'
        state, raw = guard_observation(safe_project_path(root, relative))
        if raw is None:
            raise StructureError('SUMMARY_NOT_FOUND', 'selected summary is missing')
        if version is not None and state['sha256'] != version:
            raise StructureError('SUMMARY_HISTORY_CONFLICT', 'historical summary content changed')
        try:
            payload = json.loads(raw)
            validate_summary(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise StructureError('SUMMARY_INVALID', 'selected summary does not match the supported format') from exc
        if chapterId is not None:
            payload = {'plotLine': [row for row in payload.get('plotLine', []) if row['chapterId'] == chapterId]}
        if characterId is not None:
            payload = {'characterSummaries': [row for row in payload.get('characterSummaries', []) if row['characterId'] == characterId]}
        result = text_page({'ok': True, 'code': 'SUMMARY_READ', 'summaryVersion': state['sha256'],
                            'historicalSnapshot': version is not None, 'semanticReviewComplete': False},
                           [{'id': state['sha256'], 'text': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}],
                           budget=budget, limit=page_limit, cursor=cursor,
                           binding=sha256_json([version, chapterId, characterId, budget, page_limit, policy]),
                           source=state['sha256'])
        validate_guards(root, [{'path': relative, **state}], stage='summary-read')
    require_no_pending(root)
    return result
