"""Stateless, version-bound handoff from task preparation to file reading.

The compressed receipt is transport, not authority: read-batch still validates
the current task allowlist and every selected hash. No 'latest hash' substitution.
"""
from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path

from .artifacts import sha256_json
from .structure_store import StructureError


def encode_receipt(value: dict) -> str:
    return base64.urlsafe_b64encode(zlib.compress(json.dumps(value, ensure_ascii=False,
                                   separators=(',', ':')).encode())).decode()


def decode_receipt(token: str, root: Path | None = None) -> dict:
    if isinstance(token, str) and token.startswith('gn:'):
        from .transport_cache import get
        if root is None:
            raise StructureError('READ_CREDENTIAL_INVALID', 'project scope is required for a transport reference')
        return get(root, token, kind='prepare')['handoff']
    try:
        value = json.loads(zlib.decompress(base64.urlsafe_b64decode(token)))
        if not isinstance(value, dict):
            raise ValueError('object required')
        return value
    except (ValueError, TypeError, zlib.error) as exc:
        raise StructureError('READ_CREDENTIAL_INVALID', 'use the unchanged preparation credential') from exc


def handoff(root: Path, report: dict, arguments: dict) -> dict:
    from .paths import safe_project_path
    from .project import TOOLBOX_ROOT
    rows = []
    for row in report['readList']:
        if row.get('path') and row.get('sha256'):
            row['readId'] = f'r{len(rows)}'
            rows.append({'root': 'project', 'path': row['path'], 'sha256': row['sha256'],
                         **(dict(zip(('startLine', 'endLine'), row['lines'])) if row.get('lines') else {})})
    for row in report['promptPlan'].get('contracts', []):
        row['readIds'] = []
        for span in row['readRanges']:
            row['readIds'].append(f'r{len(rows)}')
            rows.append({'root': 'toolbox', 'path': row['path'], 'sha256': row['sha256'],
                         'startLine': span['startLine'], 'endLine': span['endLine']})
    for row in report['promptPlan'].get('providerMaterials', []):
        if row['active']:
            row['readId'] = f'r{len(rows)}'
            rows.append({'root': 'provider', 'path': row['path'], 'sha256': row['sha256']})
    source_reads = []
    queries = [r['query'] for r in (report.get('context', {}).get('modelContext') or {}).get('sourceReads', [])]
    queries += [{'tool': r['tool'], **r['arguments']} for r in report['readList'] if r.get('kind') == 'source-query']
    seen_queries = set()
    for query in queries:
        query = {k: v for k, v in query.items() if k != 'view'}
        if arguments.get('povCharacter') and query['tool'] == 'query_source':
            query['selection'] = {**query.get('selection', {}), 'knowledgeMode': 'perspective',
                                  'perspectiveIds': [arguments['povCharacter']]}
        identity = sha256_json(query)
        if identity in seen_queries:
            continue
        seen_queries.add(identity)
        read_id = f'r{len(rows)}'
        rows.append({'root': 'source', 'query': query})
        source_reads.append({'readId': read_id, 'query': query})
    references = []
    for reference in report['promptPlan'].get('referenceEntries', []):
        for entry in ([reference] if reference.get('sha256') else reference.get('entrypoints', [])):
            # Only manifest-declared reference aliases may be resolved. Requests
            # still use canonical, non-symlink paths and the original file hash.
            canonical = safe_project_path(TOOLBOX_ROOT, entry['path'], reject_symlink=False).relative_to(TOOLBOX_ROOT).as_posix()
            if canonical.startswith('knowledge/assets/'):
                from .knowledge_store import _parse
                from .write_sets import guard_observation
                state, raw = guard_observation(TOOLBOX_ROOT / canonical)
                metadata = _parse(raw, canonical)[0] if raw is not None else None
                if (metadata is None or metadata['status'] != 'active'
                        or report['station'].removeprefix('govel-') not in metadata['stations']):
                    references.append({'path': canonical, 'referencePath': entry['path'],
                                       'available': False, 'code': 'KNOWLEDGE_ASSET_NOT_ACTIVE_FOR_STATION'})
                    continue
            references.append({'path': canonical, 'referencePath': entry['path'], 'readId': f'r{len(rows)}'})
            rows.append({'root': 'toolbox', 'path': canonical, 'sha256': entry['sha256']})
    scope = {'root': str(root), 'arguments': arguments, 'scope': report['scope'],
             'candidateSha256': report.get('upstream', {}).get('candidate_prose', {}).get('artifactSha256'),
             'planHash': report['promptPlan'].get('planHash'),
             'contextHash': report['context'].get('contextHash'),
             'policyHash': (report.get('toolPlan') or {}).get('policyHash')}
    return {'credential': encode_receipt({'scope': scope, 'rows': rows}),
            'scopeHash': sha256_json(scope), 'operation': 'task.read-batch',
            'referenceReads': references, 'sourceReads': source_reads,
            'instruction': '回传 credential 并选择 readIds；续页保持选择与预算，只增加 cursor。凭据绑定此次材料版本，不标记语义已读。'}


def resolve_handoff(root: Path, credential: str, read_ids: list[str], report: dict, arguments: dict) -> list[dict]:
    saved = decode_receipt(credential, root)
    current = decode_receipt(report['reading']['credential'], root)
    if saved.get('scope') != current['scope'] or saved.get('scope', {}).get('arguments') != arguments:
        raise StructureError('READ_BATCH_STALE', 'task scope or preparation inputs changed; prepare again')
    rows = saved.get('rows')
    if (not isinstance(rows, list) or not isinstance(read_ids, list) or not read_ids
            or any(not isinstance(rid, str) or rid not in {f'r{i}' for i in range(len(rows))} for rid in read_ids)):
        raise StructureError('READ_CREDENTIAL_INVALID', 'select readIds from the preparation report')
    # Current allowlisting is done again by read_batch; saved hashes stay intact.
    return [rows[int(rid[1:])] for rid in read_ids]
