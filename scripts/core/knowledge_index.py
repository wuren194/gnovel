"""Incremental, rebuildable SQLite search over curated Markdown assets only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .jsonio import write_json
from .knowledge_store import TOOLBOX_ROOT, list_assets, _read_selected, store_path
from .project import knowledge_policy
from .static_cache import signature
from .structure_store import StructureError

INDEX = TOOLBOX_ROOT / 'knowledge/.cache/search.sqlite3'
SCHEMA_VERSION = 3
_INDEX_CACHE = {}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _fingerprint(rows: list[dict]) -> str:
    return _hash([(row['assetId'], row['sha256'], row['path']) for row in rows])


def body_tokens(text: str) -> list[str]:
    """Deterministic CJK bigrams and ASCII words; unchanged retrieval semantics."""
    lower = text.casefold()
    tokens = set(re.findall(r'[a-z0-9_]{2,}', lower))
    for run in re.findall(r'[\u4e00-\u9fff]+', lower):
        tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    return sorted(tokens)


def _index_signature():
    store_path(INDEX.relative_to(TOOLBOX_ROOT).as_posix())
    paths = [Path(str(INDEX) + suffix) for suffix in ('', '-wal', '-shm', '-journal')]
    if any(path.is_symlink() or any(parent.is_symlink() for parent in path.parents) for path in paths):
        raise StructureError('KNOWLEDGE_PATH_UNSAFE', 'knowledge index path is a symlink')
    from .static_cache import derived_index_signature
    from .prepare_dependencies import observe_knowledge
    observe_knowledge(TOOLBOX_ROOT, {path.relative_to(TOOLBOX_ROOT).as_posix(): signature(path) for path in paths})
    return (derived_index_signature(paths[0]), *(signature(path) for path in paths[1:]))


def _read_index():
    try:
        before = _index_signature()
        if before[0] is None:
            return None, 'missing'
        if any(value is not None for value in before[1:]):
            return None, 'busy'
        key = str(INDEX.absolute())
        old = _INDEX_CACHE.get(key)
        if old is not None and old[0] == before:
            return old[1], 'current'
        with closing(sqlite3.connect(INDEX.absolute().as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            meta = dict(connection.execute('SELECT key, value FROM meta'))
            if meta.get('schemaVersion') != str(SCHEMA_VERSION):
                return None, 'schema'
            docs = list(connection.execute('SELECT assetId, sha256, tokens FROM docs ORDER BY assetId'))
            catalog = list(connection.execute('SELECT path, signature, metadata FROM catalog ORDER BY path'))
        if before != _index_signature():
            return None, 'stale'
        if (meta.get('docsHash') != _hash(docs) or meta.get('catalogHash') != _hash(catalog)
                or len({row[0] for row in docs}) != len(docs)):
            return None, 'invalid'
        value = {'meta': meta, 'docs': docs, 'catalog': catalog}
        _INDEX_CACHE[key] = (before, value)
        return value, 'current'
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError):
        return None, 'invalid'


def cached_catalog(root, assets):
    """Optional metadata seed; callers still validate the live path set and stat identities."""
    value, _ = _read_index()
    if value is None:
        return {}
    meta = value['meta']
    if (meta.get('toolboxRoot') != str(root.resolve()) or meta.get('assetsRoot') != str(assets.resolve())
            or meta.get('policyHash') != _hash(knowledge_policy())):
        return {}
    try:
        return {path: (tuple(json.loads(state)), json.loads(row)) for path, state, row in value['catalog']}
    except (ValueError, TypeError):
        return {}


def index_state(rows: list[dict] | None = None) -> dict:
    rows = list_assets()['items'] if rows is None else rows
    value, reason = _read_index()
    if value is None:
        if not rows and reason == 'missing':
            return {'current': True, 'reason': 'empty'}
        return {'current': False, 'reason': reason}
    meta = value['meta']
    active = {row['assetId']: row['sha256'] for row in rows if row['status'] == 'active'}
    indexed = {rid: sha for rid, sha, _ in value['docs']}
    current = (meta.get('fingerprint') == _fingerprint(rows) and active == indexed
               and meta.get('toolboxRoot') == str(TOOLBOX_ROOT.resolve())
               and meta.get('policyHash') == _hash(knowledge_policy()))
    return {'current': current, 'reason': 'current' if current else 'stale'}


def rebuild_index() -> dict:
    from .knowledge_store import ASSETS, _asset_lock
    _index_signature()
    with _asset_lock():
        if any(value is not None for value in _index_signature()[1:]):
            raise StructureError('KNOWLEDGE_INDEX_BUSY', 'close the external SQLite writer before rebuilding knowledge')
        catalog = list_assets()
        catalog_path = store_path('knowledge/catalog.json')
        if catalog_path.is_symlink():
            raise StructureError('KNOWLEDGE_PATH_UNSAFE', 'knowledge catalog path is a symlink')
        try:
            previous_catalog = json.loads(catalog_path.read_bytes())
        except (FileNotFoundError, ValueError):
            previous_catalog = None
        rows = catalog['items']
        states = {row['path']: signature(TOOLBOX_ROOT / row['path']) for row in rows}
        previous, _ = _read_index()
        reusable = {}
        if previous and previous['meta'].get('toolboxRoot') == str(TOOLBOX_ROOT.resolve()):
            reusable = {rid: (sha, tokens) for rid, sha, tokens in previous['docs']}
        docs, vocabulary, reused, changed = [], set(), 0, 0
        for row in rows:
            if row['status'] != 'active':
                continue
            prior = reusable.get(row['assetId'])
            if prior and prior[0] == row['sha256']:
                tokens = prior[1]
                reused += 1
            else:
                document = _read_selected(row, expected_hash=row['sha256'])
                tokens = ' '.join(body_tokens(document['body']))
                changed += 1
            docs.append((row['assetId'], row['sha256'], tokens))
            vocabulary.update(tokens.split())
        docs.sort()
        entries = [(row['path'], json.dumps(states[row['path']]), json.dumps(row, ensure_ascii=False, sort_keys=True)) for row in rows]
        entries.sort()
        meta = {'fingerprint': _fingerprint(rows), 'schemaVersion': str(SCHEMA_VERSION),
                'toolboxRoot': str(TOOLBOX_ROOT.resolve()), 'assetsRoot': str(ASSETS.resolve()),
                'policyHash': _hash(knowledge_policy()), 'docsHash': _hash(docs), 'catalogHash': _hash(entries)}
        if any(value is not None for value in _index_signature()[1:]):
            raise StructureError('KNOWLEDGE_INDEX_BUSY', 'knowledge index was opened for writing during rebuild')
        INDEX.parent.mkdir(parents=True, exist_ok=True)
        store_path(INDEX.relative_to(TOOLBOX_ROOT).as_posix())
        fd, temporary = tempfile.mkstemp(prefix='search-', suffix='.sqlite3', dir=INDEX.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                connection.execute('CREATE TABLE catalog (path TEXT PRIMARY KEY, signature TEXT NOT NULL, metadata TEXT NOT NULL)')
                connection.execute("CREATE VIRTUAL TABLE docs USING fts5(assetId UNINDEXED, sha256 UNINDEXED, tokens, tokenize='unicode61')")
                connection.executemany('INSERT INTO docs(assetId,sha256,tokens) VALUES (?,?,?)', docs)
                connection.executemany('INSERT INTO catalog(path,signature,metadata) VALUES (?,?,?)', entries)
                connection.executemany('INSERT INTO meta(key,value) VALUES (?,?)', sorted(meta.items()))
                connection.commit()
            if (rows != list_assets()['items'] or any(signature(TOOLBOX_ROOT / path) != state for path, state in states.items())
                    or meta['policyHash'] != _hash(knowledge_policy())):
                raise StructureError('KNOWLEDGE_STALE', 'assets or policy changed while rebuilding index')
            if any(value is not None for value in _index_signature()[1:]):
                raise StructureError('KNOWLEDGE_INDEX_BUSY', 'knowledge index was opened for writing during rebuild')
            if catalog_path.is_symlink():
                raise StructureError('KNOWLEDGE_PATH_UNSAFE', 'knowledge catalog path is a symlink')
            os.replace(temporary, store_path(INDEX.relative_to(TOOLBOX_ROOT).as_posix()))
            if _hash(previous_catalog) != _hash(catalog):
                write_json(catalog_path, catalog)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {'ok': True, 'code': 'KNOWLEDGE_INDEX_REBUILT', 'indexedAssets': len(docs),
            'reusedAssets': reused, 'retokenizedAssets': changed,
            'vocabulary': len(vocabulary), 'fingerprint': _fingerprint(rows)}


def asset_token_sets(rows: list[dict] | None = None) -> dict[str, set[str]] | None:
    rows = list_assets()['items'] if rows is None else rows
    value, _ = _read_index()
    if value is None or not index_state(rows)['current']:
        return None
    if value['meta'].get('fingerprint') != _fingerprint(rows):
        return None
    return {asset_id: set(tokens.split()) for asset_id, _, tokens in value['docs']}
