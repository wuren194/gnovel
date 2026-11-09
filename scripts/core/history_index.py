"""Disposable historical metadata index over existing ledgers; never an authority."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .artifacts import sha256_json
from .consistency import require_no_pending
from .jsonio import read_json
from .locks import project_lock_reentrant
from .project import query_policy
from .query import _cursor, _cursor_decode, _page, _size
from .static_cache import signature
from .structure_store import StructureError, load_structure
from .write_sets import safe_project_path

INDEX_PATH = '.govel/cache/history.sqlite3'
SCHEMA_VERSION = 1
KINDS = ('operation', 'candidate', 'diagnostics', 'approval', 'evidence')
_CACHE = {}


def _inventory(root):
    paths = {'.govel/structure.json': 'binding', '.govel/project.json': 'binding',
             '.govel/workflow.json': 'workflow',
             '.govel/source-review-receipts.json': 'review',
             '.govel/source-prose-evidence.json': 'evidence',
             '.govel/source-rule-issues.json': 'rule'}
    for directory, pattern, kind in (('.govel/operations', 'OP-*.json', 'operation'),
                                     ('.govel/artifact-requests', '*.json', 'request'),
                                     ('.govel/workbench/candidates', '*/index.json', 'candidate'),
                                     ('.govel/backups', 'OP-*/.govel/workflow.json', 'workflow'),
                                     ('.govel/undo-receipts', '*.json', 'binding')):
        base = safe_project_path(root, directory)
        for path in sorted(base.glob(pattern)):
            relative = path.relative_to(root).as_posix()
            safe_project_path(root, relative)
            paths[relative] = kind
    base = safe_project_path(root, '工作台')
    for path in sorted(base.glob('*.json')):
        if re.fullmatch(r'.+-(?:diagnostics|candidate_prose)-[a-f0-9]{64}\.json', path.name):
            relative = path.relative_to(root).as_posix()
            safe_project_path(root, relative)
            paths[relative] = 'artifact'
    return {relative: {'kind': kind, 'signature': list(state) if state is not None else None}
            for relative, kind in sorted(paths.items())
            for state in [signature(safe_project_path(root, relative))]}


def _row(kind, identity, relative, *, chapters=(), records=(), requests=(), **metadata):
    if (not isinstance(identity, str) or not identity
            or any(not isinstance(values, (list, tuple, set)) or any(not isinstance(v, str) or not v for v in values)
                   for values in (chapters, records, requests))):
        raise ValueError('history metadata requires stable string identities')
    return {'kind': kind, 'id': identity, 'path': relative, 'historicalSnapshot': True,
            'chapterIds': sorted(set(chapters)), 'recordIds': sorted(set(records)),
            'requestIds': sorted(set(requests)), **metadata}


def _workflow_rows(data, relative):
    from .workflow import normalize_workflow
    data = normalize_workflow(data)
    rows = []
    for cid, entries in data.get('artifacts', {}).items():
        if not isinstance(entries, dict):
            raise ValueError('invalid chapter artifact registrations')
        for kind, entry in entries.items():
            if kind not in {'candidate_prose', 'diagnostics'}:
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get('artifactSha256'), str):
                raise ValueError('invalid artifact registration snapshot')
            rows.append(_row('candidate' if kind == 'candidate_prose' else 'diagnostics',
                entry['artifactSha256'], relative, chapters=[cid],
                artifactType=kind, artifactPath=entry.get('artifactPath'),
                artifactSha256=entry['artifactSha256'], invalidated=bool(entry.get('invalidatedBy')),
                recordType='registration_snapshot', detailsOperation='workflow.repair-context'))
    for cid, approval in data.get('approvals', {}).items():
        if not isinstance(approval, dict) or not isinstance(approval.get('artifactHashes', {}), dict):
            raise ValueError('invalid approval snapshot')
        hashes = approval.get('artifactHashes', {})
        if any(not isinstance(value, str) for value in hashes.values()):
            raise ValueError('invalid approved artifact identities')
        for kind, artifact_hash in hashes.items() or [(None, None)]:
            rows.append(_row('approval', f'{cid}:{kind}:{artifact_hash}', relative, chapters=[cid],
                artifactType=kind, artifactSha256=artifact_hash, recordedApproved=approval.get('approved') is True,
                mode=approval.get('mode')))
    return rows


def _parent_requests(root, state, project_id):
    from .evidence_submission import _completed, _identity, _validate
    parents = {}
    values = state.get('requests', {})
    if not isinstance(values, dict):
        raise ValueError('invalid evidence request map')
    for key, value in values.items():
        _validate(value)
        request_id = value['requestId']
        if key != _identity(request_id) or value['snapshot']['projectId'] != project_id:
            raise ValueError('evidence request map identity differs')
        for receipt_id in _completed(root, value).values():
            parents.setdefault(receipt_id, []).append(request_id)
    return parents


def _extract(root, relative, kind, project_id):
    path = safe_project_path(root, relative)
    if not path.exists() or kind == 'binding':
        return []
    if not path.is_file():
        raise StructureError('HISTORY_SOURCE_INVALID', 'history source must be a regular file', {'path': relative})
    try:
        if kind == 'review':
            from .source_review import _receipts
            data = _receipts(root)
            parents = _parent_requests(root, data, project_id)
            return [_row('evidence', receipt['receiptId'] + ':' + row['recordId'], relative,
                         records=[row['recordId']], requests=parents.get(receipt['receiptId'], []),
                         receiptId=receipt['receiptId'], evidenceType='source-review',
                         recordedStatus=row['decision'], detailsOperation='source.review-receipt')
                    for receipt in data['receipts'].values() for row in receipt['reviews']]
        if kind == 'evidence':
            from .source_evidence import read_evidence
            data = read_evidence(root)
            parents = _parent_requests(root, data, project_id)
            return [_row('evidence', row['evidenceId'], relative, chapters=[batch['chapterId']],
                         records=row['targetIds'], requests=parents.get(batch['batchId'], []),
                         receiptId=batch['batchId'], evidenceType='prose',
                         citationKind=row['kind'], fileHash=batch['fileHash'], sourceHash=batch['sourceHash'],
                         detailsOperation='source.evidence')
                    for batch in data['batches'].values() for row in batch['citations']]
        if kind == 'rule':
            from .source_rule_issues import read_rule_issues
            data = read_rule_issues(root)
            return [_row('evidence', rid, relative, chapters=[receipt['issue']['chapterId']],
                         records=[receipt['issue']['ruleId'], *receipt['issue']['affectedIds']],
                         receiptId=rid, evidenceType='rule', recordedStatus=receipt['issue']['status'],
                         detailsOperation='source.rule-issue-receipt') for rid, receipt in data['receipts'].items()]
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError('history source must be an object')
        if kind == 'operation':
            from .structure_commands import _operation_paths
            _operation_paths(root, path.stem)
            if data.get('operationId') != path.stem or not data.get('committedAt'):
                raise ValueError('invalid committed operation')
            impact = data.get('impact', {})
            if not isinstance(impact, dict):
                raise ValueError('invalid operation impact')
            changed = impact.get('changedIds', [])
            chapters = set(impact.get('staleCandidates', [])) | set(impact.get('approvalInvalidations', []))
            chapters.update(rid for rid in changed if isinstance(rid, str) and rid.startswith('CH-'))
            for key in ('chapterId', 'anchorId'):
                if isinstance(impact.get(key), str) and impact[key].startswith('CH-'):
                    chapters.add(impact[key])
            requests = [data['requestId']] if isinstance(data.get('requestId'), str) else []
            return [_row('operation', data['operationId'], relative, chapters=chapters, records=changed,
                requests=requests, operation=data.get('operation'), beforeRevision=data.get('beforeRevision'),
                afterRevision=data.get('afterRevision'), committedAt=data['committedAt'],
                detailsOperation='source.change-report')]
        if kind == 'workflow':
            return _workflow_rows(data, relative)
        if kind == 'candidate':
            from .candidate_versions import load_candidate_index
            cid = path.parent.name
            data = load_candidate_index(root, cid)
            if data.get('projectId') not in (None, '', project_id):
                raise ValueError('candidate index belongs to another project')
            return [_row('candidate', f'{cid}:v{row["version"]}', relative, chapters=[cid],
                         requests=[row['requestId']], version=row['version'], bodySha256=row['bodySha256'],
                         byteLength=row['byteLength'], createdAt=row['createdAt'], recordType='saved_version',
                         detailsOperation='candidate.read-version') for row in data['versions']]
        if kind == 'request':
            if (data.get('schemaVersion') != 1 or not isinstance(data.get('requestId'), str)
                    or data.get('requestHash') != sha256_json({k: v for k, v in data.items() if k != 'requestHash'})
                    or path.stem != sha256_json({'projectId': project_id, 'requestId': data['requestId']})):
                raise ValueError('artifact request fingerprint or project identity differs')
            receipt = data['receipt']; artifact_type = receipt['artifactType']
            if artifact_type not in {'candidate_prose', 'diagnostics'}:
                return []
            return [_row('candidate' if artifact_type == 'candidate_prose' else 'diagnostics',
                         receipt['artifactSha256'], relative, chapters=[receipt['chapterId']],
                         requests=[data['requestId']], artifactType=artifact_type,
                         artifactPath=receipt['artifactPath'], artifactSha256=receipt['artifactSha256'],
                         recordType='request_receipt', detailsOperation='workflow.repair-context')]
        if kind == 'artifact':
            from .review_artifacts import validate_artifact_binding
            validate_artifact_binding(data, project_id=project_id)
            artifact_type = data['artifactType']
            if artifact_type not in {'candidate_prose', 'diagnostics'}:
                raise ValueError('unexpected artifact type in retained artifact path')
            return [_row('candidate' if artifact_type == 'candidate_prose' else 'diagnostics',
                         data['artifactSha256'], relative, chapters=[data['chapterId']],
                         artifactType=artifact_type, artifactSha256=data['artifactSha256'],
                         recordType='retained_artifact', detailsOperation='workflow.repair-context')]
    except (ValueError, KeyError, TypeError, OSError) as exc:
        if isinstance(exc, StructureError):
            raise
        raise StructureError('HISTORY_SOURCE_INVALID', 'history source cannot be indexed',
                             {'path': relative, 'reason': str(exc)}) from exc
    return []


def _index_signature(root):
    paths = [safe_project_path(root, INDEX_PATH + suffix) for suffix in ('', '-wal', '-shm', '-journal')]
    return tuple(signature(path) for path in paths)


def _db_state(root):
    target = safe_project_path(root, INDEX_PATH)
    if not target.is_file():
        return None, 'missing'
    state = _index_signature(root)
    if any(value is not None for value in state[1:]):
        return None, 'busy'
    cached = _CACHE.get(str(target))
    if cached is not None and cached[0] == state:
        return deepcopy(cached[1]), 'current'
    try:
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            meta = dict(connection.execute('SELECT key,value FROM meta'))
            if meta.get('schemaVersion') != str(SCHEMA_VERSION):
                return None, 'schema'
            files = list(connection.execute('SELECT path,state,rows FROM files ORDER BY path'))
            entries = list(connection.execute('SELECT key,kind,payload FROM entries ORDER BY key'))
            tags = list(connection.execute('SELECT entryKey,field,value FROM tags ORDER BY entryKey,field,value'))
        if state != _index_signature(root):
            return None, 'stale'
        if (meta.get('filesHash') != sha256_json(files) or meta.get('entriesHash') != sha256_json(entries)
                or meta.get('tagsHash') != sha256_json(tags) or meta.get('recordCount') != str(len(entries))
                or any(not isinstance(meta.get(key), str) for key in ('root', 'projectId', 'inputsHash'))):
            return None, 'invalid'
        for _, encoded_state, encoded_rows in files:
            if not isinstance(json.loads(encoded_state), dict) or not isinstance(json.loads(encoded_rows), list):
                return None, 'invalid'
        if any(kind not in KINDS or not isinstance(json.loads(payload), dict) for _, kind, payload in entries):
            return None, 'invalid'
        value = {'meta': meta, 'files': files, 'state': state}
        _CACHE[str(target)] = (state, deepcopy(value))
        return value, 'current'
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError):
        return None, 'invalid'


def _current_cache(root, inventory, project_id):
    value, reason = _db_state(root)
    if value is None:
        return None, reason
    if (value['meta'].get('root') != str(root) or value['meta'].get('projectId') != project_id
            or value['meta'].get('inputsHash') != sha256_json(inventory)):
        return value, 'stale'
    return value, 'current'


def _snapshot(root):
    require_no_pending(root)
    structure_state = signature(safe_project_path(root, '.govel/structure.json'))
    source = load_structure(root)
    inputs = _inventory(root)
    if structure_state != signature(safe_project_path(root, '.govel/structure.json')):
        raise StructureError('HISTORY_INPUT_CHANGED', 'project binding changed while reading history inputs')
    return source, inputs


def history_index_state(root: Path):
    root = root.expanduser().resolve()
    source, inputs = _snapshot(root)
    value, reason = _current_cache(root, inputs, source['projectId'])
    if inputs != _inventory(root):
        raise StructureError('HISTORY_INPUT_CHANGED', 'history inputs changed while querying index state')
    require_no_pending(root)
    return {'ok': True, 'code': 'HISTORY_INDEX_STATE', 'current': reason == 'current', 'reason': reason,
            'rebuildOperation': 'history.index-rebuild', 'derivedOnly': True,
            'recordCount': int(value['meta']['recordCount']) if value and reason == 'current' else None}


def _collect(root, inputs, project_id, previous=None):
    prior = {}
    if previous and previous['meta'].get('root') == str(root) and previous['meta'].get('projectId') == project_id:
        prior = {path: (json.loads(state), json.loads(rows)) for path, state, rows in previous['files']}
    files, entries, tags, read_count = [], [], [], 0
    for relative, state in inputs.items():
        cached = prior.get(relative)
        if cached and cached[0] == state:
            rows = cached[1]
        else:
            rows = _extract(root, relative, state['kind'], project_id)
            read_count += state['kind'] != 'binding' and state['signature'] is not None
        files.append((relative, json.dumps(state, sort_keys=True), json.dumps(rows, ensure_ascii=False, sort_keys=True)))
        for n, row in enumerate(rows):
            key = sha256_json([relative, row['kind'], row['id'], n])
            entries.append((key, row['kind'], json.dumps(row, ensure_ascii=False, sort_keys=True)))
            for field, values in (('chapterId', row['chapterIds']), ('recordId', row['recordIds']), ('requestId', row['requestIds'])):
                tags.extend((key, field, value) for value in values)
            tags.append((key, 'id', row['id']))
            if row.get('receiptId'):
                tags.append((key, 'receiptId', row['receiptId']))
    return sorted(files), sorted(entries), sorted(set(tags)), read_count


def rebuild_history_index(root: Path):
    root = root.expanduser().resolve()
    with project_lock_reentrant(root):
        source, inputs = _snapshot(root)
        previous, _ = _db_state(root)
        files, entries, tags, read_count = _collect(root, inputs, source['projectId'], previous)
        meta = {'schemaVersion': str(SCHEMA_VERSION), 'root': str(root), 'projectId': source['projectId'],
                'inputsHash': sha256_json(inputs), 'filesHash': sha256_json(files),
                'entriesHash': sha256_json(entries), 'tagsHash': sha256_json(tags), 'recordCount': str(len(entries))}
        target = safe_project_path(root, INDEX_PATH)
        if any(value is not None for value in _index_signature(root)[1:]):
            raise StructureError('HISTORY_INDEX_BUSY', 'close the external SQLite writer before rebuilding history')
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='history-', suffix='.sqlite3', dir=target.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(name)) as connection:
                connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                connection.execute('CREATE TABLE files (path TEXT PRIMARY KEY, state TEXT NOT NULL, rows TEXT NOT NULL)')
                connection.execute('CREATE TABLE entries (key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)')
                connection.execute('CREATE TABLE tags (entryKey TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(entryKey,field,value))')
                connection.execute('CREATE INDEX entries_kind ON entries(kind,key)')
                connection.execute('CREATE INDEX tags_value ON tags(field,value,entryKey)')
                connection.executemany('INSERT INTO files VALUES (?,?,?)', files)
                connection.executemany('INSERT INTO entries VALUES (?,?,?)', entries)
                connection.executemany('INSERT INTO tags VALUES (?,?,?)', tags)
                connection.executemany('INSERT INTO meta VALUES (?,?)', sorted(meta.items()))
                connection.commit()
            if inputs != _inventory(root):
                raise StructureError('HISTORY_INPUT_CHANGED', 'history inputs changed while rebuilding')
            require_no_pending(root)
            if any(value is not None for value in _index_signature(root)[1:]):
                raise StructureError('HISTORY_INDEX_BUSY', 'history index was opened for writing during rebuild')
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
    return {'ok': True, 'code': 'HISTORY_INDEX_REBUILT', 'recordCount': len(entries),
            'sourceFilesRead': read_count, 'derivedOnly': True, 'path': INDEX_PATH}


def query_history(root: Path, *, kind=None, chapterId=None, recordId=None, requestId=None,
                  limit=None, maxBytes=None, cursor=None):
    root = root.expanduser().resolve()
    require_no_pending(root)
    filters = {'chapterId': chapterId, 'recordId': recordId, 'requestId': requestId}
    if (kind is not None and kind not in KINDS
            or any(v is not None and (not isinstance(v, str) or not v.strip()) for v in filters.values())):
        raise StructureError('HISTORY_FILTER_INVALID', 'select a supported history kind and exact stable identities')
    policy = query_policy()
    count = policy['defaultLimit'] if limit is None else limit
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    if type(count) is not int or not 1 <= count <= policy['maxLimit'] or type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid history query budget')
    source, inputs = _snapshot(root)
    cache, reason = _current_cache(root, inputs, source['projectId'])
    source_hash = sha256_json({'inputs': inputs, 'policy': policy, 'root': str(root)})
    binding = sha256_json({'kind': kind, **filters, 'limit': count, 'maxBytes': budget})
    offset = 0
    if cursor is not None:
        saved = _cursor_decode(cursor)
        if saved.get('source') != source_hash:
            raise StructureError('CURSOR_STALE', 'history inputs changed after this page')
        if saved.get('binding') != binding:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'history cursor belongs to different filters')
        offset = saved['offset']
    receipt_ids = []
    if requestId is not None:
        key = sha256_json({'projectId': source['projectId'], 'requestId': requestId})
        receipt_ids = [prefix + key.upper() for prefix in ('SR-', 'PE-', 'RR-')]
    envelope = {'ok': True, 'code': 'HISTORY_QUERY',
                'index': {'current': reason == 'current', 'reason': reason, 'used': reason == 'current',
                          'rebuildOperation': 'history.index-rebuild'},
                'coverage': {'historicalMetadataOnly': True, 'proseIncluded': False,
                             'currentStatusNotEvaluated': True, 'approvalGranted': False,
                             'authorOnly': True, 'knowledgeMode': 'historical_metadata',
                             'authority': 'existing operation, candidate, workflow and evidence files'}}
    if reason == 'current':
        clauses, parameters = [], []
        if kind is not None:
            clauses.append('e.kind=?'); parameters.append(kind)
        for field in ('chapterId', 'recordId'):
            if filters[field] is not None:
                clauses.append('EXISTS (SELECT 1 FROM tags t WHERE t.entryKey=e.key AND t.field=? AND t.value=?)')
                parameters.extend([field, filters[field]])
        if requestId is not None:
            clauses.append('EXISTS (SELECT 1 FROM tags t WHERE t.entryKey=e.key AND ((t.field=? AND t.value=?) OR (t.field=? AND t.value IN (?,?,?))))')
            parameters.extend(['requestId', requestId, 'receiptId', *receipt_ids])
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        target = safe_project_path(root, INDEX_PATH)
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            total = connection.execute('SELECT COUNT(*) FROM entries e' + where, parameters).fetchone()[0]
            selected = connection.execute('SELECT payload FROM entries e' + where + ' ORDER BY e.key LIMIT ? OFFSET ?',
                                          [*parameters, count, offset]).fetchall()
        if offset > total:
            raise StructureError('CURSOR_INVALID', 'history cursor exceeds the result set')
        rows = [json.loads(value[0]) for value in selected]
        while True:
            end = offset + len(rows)
            result = {**envelope, 'totalMatching': total, 'returned': len(rows), 'items': rows,
                      'hasMore': end < total, 'nextCursor': _cursor(binding, source_hash, end) if end < total else None}
            if _size(result) <= budget and (rows or offset == total):
                break
            if not rows:
                raise StructureError('FIELD_TOO_LARGE', 'history metadata does not fit requested budget')
            rows.pop()
        if _index_signature(root) != cache['state']:
            raise StructureError('HISTORY_INPUT_CHANGED', 'history index changed while querying')
    else:
        _, entries, _, _ = _collect(root, inputs, source['projectId'], cache)
        rows = [json.loads(row[2]) for row in entries]
        rows = [row for row in rows if (kind is None or row['kind'] == kind)
                and (chapterId is None or chapterId in row['chapterIds'])
                and (recordId is None or recordId in row['recordIds'])
                and (requestId is None or requestId in row['requestIds'] or row.get('receiptId') in receipt_ids)]
        result = _page(envelope, rows, limit=count, budget=budget, offset=offset, binding=binding, source=source_hash)
    if inputs != _inventory(root):
        raise StructureError('HISTORY_INPUT_CHANGED', 'history source changed while querying')
    require_no_pending(root)
    return result
