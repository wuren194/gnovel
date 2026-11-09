"""Bounded execution of explicitly selected task reading references."""
from __future__ import annotations

import os
from pathlib import Path

from .artifacts import sha256_json
from .consistency import require_no_pending
from .project import TOOLBOX_ROOT, query_policy
from .structure_store import StructureError
from .task_prepare import prepare_task
from .write_sets import digest, guard_observation, safe_project_path, validate_guards


def directory_aliases(references: list[str]) -> dict[str, str]:
    """Compatibility links inside declared reference directories → their knowledge asset.

    `libraries/*/sections/*.md` are symlinks into `knowledge/assets/`. The plan lists
    them by link path, but a link is never read or guarded itself: the request is
    rewritten to the asset path, which then passes the asset's status/station gate.
    Links that leave `knowledge/assets/` stay unreadable.
    """
    base = TOOLBOX_ROOT.resolve()
    assets = base / 'knowledge/assets'
    aliases = {}
    for reference in references:
        directory = TOOLBOX_ROOT / reference
        if directory.is_symlink() or not directory.is_dir():
            continue
        for current, _, names in os.walk(directory):
            for name in names:
                link = Path(current) / name
                if not link.is_symlink():
                    continue
                target = link.resolve()
                if target.is_file() and not target.is_symlink() and target.is_relative_to(assets):
                    aliases[link.relative_to(TOOLBOX_ROOT).as_posix()] = target.relative_to(base).as_posix()
    return aliases


def read_batch(root: Path, *, task: str | None = None, requests: list | None = None, chapterId: str | None = None,
               povCharacter: str | None = None, chapterGoal: str = '', knowledgeMode: str | None = None,
               maxBytes: int | None = None, limit: int | None = None, cursor: str | None = None,
               credential: str | None = None, readIds: list[str] | None = None, priorReceipt: dict | str | None = None) -> dict:
    root = root.expanduser().resolve()
    require_no_pending(root)
    from .reading_receipts import prior_entries, omit_unchanged, receipt_response
    prior = prior_entries(root, priorReceipt)
    if credential is not None:
        from .reading_handoff import decode_receipt
        if requests is not None or any(value is not None for value in (task, chapterId, povCharacter, knowledgeMode)) or chapterGoal:
            raise StructureError('READ_BATCH_INVALID', 'use either credential/readIds or explicit task/requests')
        args = decode_receipt(credential, root).get('scope', {}).get('arguments', {})
        if set(args) != {'task', 'chapterId', 'povCharacter', 'chapterGoal', 'knowledgeMode'}:
            raise StructureError('READ_CREDENTIAL_INVALID', 'credential has no task scope')
        task, chapterId, povCharacter, chapterGoal, knowledgeMode = (args[k] for k in
            ('task', 'chapterId', 'povCharacter', 'chapterGoal', 'knowledgeMode'))
    elif readIds is not None:
        raise StructureError('READ_CREDENTIAL_INVALID', 'readIds require a preparation credential')
    elif task is None or requests is None:
        raise StructureError('READ_BATCH_INVALID', 'supply credential/readIds or task/requests')
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    count = policy['defaultLimit'] if limit is None else limit
    if type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail'] or type(count) is not int or not 1 <= count <= policy['maxLimit']:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid batch reading budget')
    task_guards = []
    for relative in ('.govel/structure.json', '.govel/workflow.json'):
        state, _ = guard_observation(safe_project_path(root, relative))
        task_guards.append({'path': relative, **state})
    if credential is not None and credential.startswith('gn:'):
        from .transport_cache import prepared as cached_prepared
        prepared, _ = cached_prepared(root, credential)
    else:
        prepared = prepare_task(root, task, chapter_id=chapterId, pov_character=povCharacter,
                                chapter_goal=chapterGoal, knowledge_mode=knowledgeMode)
    if credential is not None:
        from .reading_handoff import resolve_handoff
        requests = resolve_handoff(root, credential, readIds, prepared, args)
    if not isinstance(requests, list) or not requests or len(requests) > policy['maxLimit']:
        raise StructureError('READ_BATCH_INVALID', 'select a bounded nonempty set of reading references')
    plan = prepared['promptPlan']
    if not plan.get('complete'):
        raise StructureError('READ_BATCH_BLOCKED', 'current contract plan is incomplete')
    from .data_provider import storage_root, binding as provider_binding
    data_root = storage_root()
    initial_provider = provider_binding()
    if plan.get('provider') != initial_provider:
        raise StructureError('READ_BATCH_STALE', 'provider changed after preparation')
    allowed = {}
    for row in prepared['readList']:
        if row.get('path') and row.get('sha256'):
            allowed.setdefault(('project', row['path']), []).append(row.get('lines'))
    for row in plan['contracts']:
        allowed[('toolbox', row['path'])] = [[span['startLine'], span['endLine']] for span in row['readRanges']]
    for row in plan.get('providerMaterials', []):
        if row['active']:
            allowed[('provider', row['path'])] = [None]
    provider_roles = {row['path']: row['role'] for row in plan.get('providerMaterials', []) if row['active']}
    references = plan['onDemandReferences']
    aliases = None
    selected, observations, raw_files, mappings = {}, {'project': task_guards, 'toolbox': [], 'provider': []}, {}, []
    source_chunks, source_coverage = [], []
    for index, request in enumerate(requests):
        if isinstance(request, dict) and request.get('root') == 'source':
            from .reading_handoff import decode_receipt
            allowed_queries = decode_receipt(prepared['reading']['credential'], root)['rows']
            if credential is None or request not in allowed_queries:
                raise StructureError('READ_BATCH_FORBIDDEN', 'use a source readId from this preparation')
            from .source_reading import source_materials
            selected_chunks, coverage = source_materials(root, request['query'],
                {**prepared, 'readingScope': args}, index)
            for chunk in selected_chunks:
                existing = next((c for c in source_chunks if c['id'] == chunk['id']), None)
                if existing:
                    existing['requestIndexes'].append(index)
                else:
                    source_chunks.append(chunk)
            source_coverage.append(coverage)
            mappings.append({'requestIndex': index, **request})
            continue
        if (not isinstance(request, dict) or set(request) - {'root', 'path', 'sha256', 'startLine', 'endLine'}
                or not {'root', 'path', 'sha256'} <= set(request)
                or not isinstance(request['root'], str) or request['root'] not in {'project', 'toolbox', 'provider'}
                or not isinstance(request['path'], str) or not isinstance(request['sha256'], str)):
            raise StructureError('READ_BATCH_INVALID', 'each request requires root, path and selected file sha256')
        base = root if request['root'] == 'project' else data_root if request['root'] == 'provider' else TOOLBOX_ROOT
        alias, canonical = None, request['path']
        if request['root'] == 'toolbox':
            if aliases is None:
                aliases = directory_aliases(references)
            if canonical in aliases:
                alias, canonical = canonical, aliases[canonical]
        path = safe_project_path(base, canonical)
        relative = path.relative_to(base).as_posix()
        if relative != canonical:
            raise StructureError('READ_BATCH_INVALID', 'reading paths must be canonical')
        key = (request['root'], relative)
        ranges = allowed.get(key)
        if ranges is None and request['root'] == 'toolbox' and (alias is not None or relative in aliases.values()):
            ranges = [None]
        if ranges is None and request['root'] == 'toolbox':
            for reference in references:
                from .paths import safe_project_path as reference_path
                ref_path = reference_path(TOOLBOX_ROOT, reference, reject_symlink=False)
                if ((path == ref_path and ref_path.is_file())
                        or (ref_path.is_dir() and path.is_relative_to(ref_path))):
                    ranges = [None]
                    break
        if ranges is None:
            raise StructureError('READ_BATCH_FORBIDDEN', 'path is not in the current task reading plan', {'path': relative})
        if key not in raw_files:
            state, raw = guard_observation(path)
            if raw is None:
                raise StructureError('READ_BATCH_MISSING', 'selected file is unreadable', {'path': relative})
            text = raw.decode('utf-8')
            if relative.startswith('knowledge/assets/'):
                from .knowledge_store import _parse
                metadata, _ = _parse(raw, relative)
                if metadata['status'] != 'active' or prepared['station'].removeprefix('govel-') not in metadata['stations']:
                    raise StructureError('READ_BATCH_FORBIDDEN', 'knowledge asset is not active for this station')
            lines = text.splitlines(keepends=True)
            raw_files[key] = (state, lines)
            observations[request['root']].append({'path': relative, **state})
        state, lines = raw_files[key]
        if state['sha256'] != request['sha256']:
            raise StructureError('READ_BATCH_STALE', 'file changed after reference selection', {'path': relative})
        start, end = request.get('startLine', 1), request.get('endLine', len(lines))
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise StructureError('READ_BATCH_RANGE_INVALID', 'invalid selected line range')
        if any(not any(span is None or span[0] <= line <= span[1] for span in ranges) for line in range(start, end + 1)):
            raise StructureError('READ_BATCH_FORBIDDEN', 'range extends beyond the current contract slice')
        selected.setdefault(key, []).append((start, end, index))
        mappings.append({'requestIndex': index, 'root': request['root'], 'path': relative, 'startLine': start, 'endLine': end,
                         **({'referencePath': alias} if alias is not None else {})})
    chunks = source_chunks
    for key, spans in sorted(selected.items()):
        state, lines = raw_files[key]
        chosen = sorted({line for start, end, _ in spans for line in range(start, end + 1)})
        groups = []
        for line in chosen:
            if not groups or line != groups[-1][-1] + 1:
                groups.append([])
            groups[-1].append(line)
        for group in groups:
            start, end = group[0], group[-1]
            text = ''.join(lines[start - 1:end])
            chunks.append({'id': sha256_json({'root': key[0], 'path': key[1], 'sha256': state['sha256'], 'start': start, 'end': end}),
                           'root': key[0], 'path': key[1], 'fileSha256': state['sha256'],
                           'startLine': start, 'endLine': end, 'text': text, 'contentSha256': digest(text.encode()),
                           'fileStart': sum(len(line) for line in lines[:start - 1]),
                           **({'role': provider_roles[key[1]]} if key[0] == 'provider' else {}),
                           'requestIndexes': sorted(index for a, b, index in spans if a <= end and b >= start)})
    omit_unchanged(chunks, prior, raw_files)
    omitted = [row for row in chunks if row.get('omittedAsUnchanged')]
    chunks = [row for row in chunks if not row.get('omittedAsUnchanged')]
    delivery_ranges = [row.pop('_deliveryRanges') for row in chunks]
    source = sha256_json({'plan': plan['planHash'], 'files': observations,
                         'representations': [(row['id'], row['contentSha256']) for row in chunks],
                         'task': prepared['task'], 'chapter': chapterId, 'pov': povCharacter,
                         'scope': prepared['scope'], 'taskPolicy': (prepared.get('toolPlan') or {}).get('policyHash'),
                         'context': prepared['context'].get('contextHash')})
    binding = sha256_json({'requests': requests, 'maxBytes': budget, 'limit': count, 'textPages': 2, 'priorReceipt': priorReceipt})
    validate_guards(root, observations['project'], stage='read-batch')
    validate_guards(TOOLBOX_ROOT, observations['toolbox'], stage='read-batch')
    validate_guards(data_root, observations['provider'], stage='read-batch')
    if provider_binding() != initial_provider or storage_root() != data_root:
        raise StructureError('READ_BATCH_STALE', 'provider changed during reading')
    if plan.get('roundtable') is not None:
        from .data_provider import roundtable_resource
        if roundtable_resource() != plan['roundtable']['resource']:
            raise StructureError('READ_BATCH_STALE', 'roundtable changed during reading; prepare again')
    require_no_pending(root)
    from .text_pages import text_page
    from .transport_cache import put
    mapping_ref = put(root, 'read-mappings', mappings)
    result = text_page({'ok': True, 'code': 'READ_BATCH', 'mappingsRef': mapping_ref,
                  **({'mappings': mappings} if credential is None else {}),
                  'unchangedSummary': {'chunks': len(omitted),
                                       'requestIndexes': sorted({i for r in omitted for i in r['requestIndexes']})},
                  'coverage': {'requestedCount': len(requests), 'uniqueChunkCount': len(chunks) + len(omitted),
                               'semanticReviewComplete': False, 'sourceQueriesExecuted': bool(source_coverage),
                               'sourceSelections': source_coverage,
                               'instruction': 'Source 内容继续使用当前 POV selection 查询；文件读取不证明视角或语义通过。'}},
                 chunks, limit=count, budget=budget, cursor=cursor, binding=binding, source=source, key='items',
                 ranges=delivery_ranges, transform=receipt_response(root, prior, chunks, delivery_ranges))
    if credential is not None and credential.startswith('gn:'):
        cached_prepared(root, credential)
    return result
