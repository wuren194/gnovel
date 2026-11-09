"""Curated writing knowledge. Markdown files are authoritative; indexes are projections."""
from __future__ import annotations

import hashlib
import json
import re
import fcntl
import os
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path, PurePosixPath

from .jsonio import write_bytes
from .project import knowledge_policy
from .data_provider import storage_root, binding as provider_binding, confined_path, ProviderError
from .structure_store import StructureError

_INITIALIZATION_ERROR = None
try:
    TOOLBOX_ROOT = storage_root()
    _PROVIDER_BINDING = provider_binding()
except (ProviderError, OSError) as exc:
    # Discovery/help must remain available so callers can diagnose a bad pack.
    # This sentinel is never used for I/O: every store operation rethrows first.
    _INITIALIZATION_ERROR = exc
    TOOLBOX_ROOT = Path(__file__).resolve().parents[2] / '.provider-unavailable'
    _PROVIDER_BINDING = None
ASSETS = TOOLBOX_ROOT / 'knowledge/assets'
STATIONS = {'composer', 'router', 'planner', 'writer', 'editor', 'titler'}
KINDS = {'technique', 'reading_note', 'em_card', 'style_analysis', 'review'}
STATUSES = {'draft', 'active', 'archived'}
ID = re.compile(r'^KN-[A-Z0-9-]+$')
# Catalog row key order. SQLite seed rows come back sort_keys-ordered while parsed rows
# follow their frontmatter, so without one order a warm rebuild rewrote every
# unchanged row of catalog.json.
CATALOG_FIELDS = ('assetId', 'title', 'kind', 'status', 'stations', 'intents', 'tags', 'source',
                  'applicability', 'exclusions', 'license', 'path', 'sha256')
_ASSET_ROWS = {}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _catalog_order(row: dict) -> dict:
    return {**{key: row[key] for key in CATALOG_FIELDS if key in row},
            **{key: row[key] for key in sorted(row) if key not in CATALOG_FIELDS}}


def _parse(data: bytes, relative: str) -> tuple[dict, str]:
    try:
        text = data.decode('utf-8')
        if not text.startswith('---\n'):
            raise ValueError('missing frontmatter')
        front, body = text[4:].split('\n---\n', 1)
        meta = json.loads(front)
        required = {'assetId', 'title', 'kind', 'status', 'stations', 'intents', 'tags',
                    'source', 'applicability', 'exclusions', 'license'}
        if not isinstance(meta, dict) or required - set(meta):
            raise ValueError('incomplete metadata')
        if not ID.fullmatch(meta['assetId']) or not isinstance(meta['title'], str) or not meta['title'].strip():
            raise ValueError('invalid identity')
        if meta['kind'] not in KINDS or meta['status'] not in STATUSES:
            raise ValueError('invalid type or status')
        for field in ('stations', 'intents', 'tags'):
            if not isinstance(meta[field], list) or any(not isinstance(v, str) or not v for v in meta[field]):
                raise ValueError('invalid ' + field)
        if set(meta['stations']) - STATIONS:
            raise ValueError('unknown station')
        policy_intents = set(knowledge_policy()['intents'])
        if set(meta['intents']) - policy_intents:
            raise ValueError('unknown intent')
        if not isinstance(meta['source'], dict) or not isinstance(meta['source'].get('path'), str) or not isinstance(meta['source'].get('sha256'), str):
            raise ValueError('invalid provenance')
        source_path = meta['source']['path']
        source_parts = PurePosixPath(source_path)
        if (source_path != 'authored' and (source_parts.is_absolute() or '..' in source_parts.parts
                or source_parts.parts[:3] != ('knowledge', 'sources', 'legacy')
                or not re.fullmatch(r'[0-9a-f]{64}', meta['source']['sha256']))):
            raise ValueError('unsafe or incomplete provenance')
        if source_path == 'authored' and meta['source']['sha256']:
            raise ValueError('authored asset has a source hash')
        if not all(isinstance(meta[field], str) for field in ('applicability', 'exclusions', 'license')):
            raise ValueError('invalid notes')
        if not body.strip():
            raise ValueError('empty body')
        return meta, body
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise StructureError('KNOWLEDGE_ASSET_INVALID', f'{relative}: {exc}') from exc


def require_store():
    if _INITIALIZATION_ERROR is not None:
        raise _INITIALIZATION_ERROR
    if provider_binding() != _PROVIDER_BINDING or storage_root() != TOOLBOX_ROOT:
        raise StructureError('KNOWLEDGE_PROVIDER_CHANGED', 'restart the process after changing provider configuration')


def store_path(relative):
    """Every asset, source, index, lock and history path shares one boundary."""
    require_store()
    try:
        return confined_path(TOOLBOX_ROOT, relative)
    except ProviderError as exc:
        raise StructureError('KNOWLEDGE_PATH_UNSAFE', str(exc)) from exc


def _files() -> list[Path]:
    store_path(ASSETS.relative_to(TOOLBOX_ROOT).as_posix())
    if not ASSETS.exists():
        return []
    if not ASSETS.is_dir():
        raise StructureError('KNOWLEDGE_PATH_UNSAFE', 'knowledge assets root must be a directory')
    files = []
    for path in sorted(ASSETS.rglob('*')):
        if path.is_symlink():
            raise StructureError('KNOWLEDGE_PATH_UNSAFE', f'asset path is not a safe regular file: {path}')
        if path.suffix == '.md':
            if not path.is_file():
                raise StructureError('KNOWLEDGE_PATH_UNSAFE', f'asset path is not a safe regular file: {path}')
            files.append(path)
    return files


def _catalog_rows():
    from .static_cache import signature
    files = _files()
    states = {path: signature(path) for path in files}
    policy = _digest(json.dumps(knowledge_policy(), sort_keys=True, ensure_ascii=False).encode())
    seed = None
    rows, seen = [], set()
    for path in files:
        relative = str(path.relative_to(TOOLBOX_ROOT))
        key = (str(TOOLBOX_ROOT), str(ASSETS), relative)
        cached = _ASSET_ROWS.get(key)
        if cached is None:
            if seed is None:
                from .knowledge_index import cached_catalog
                seed = cached_catalog(TOOLBOX_ROOT, ASSETS)
            entry = seed.get(relative)
            if entry and entry[0] == states[path]:
                row = entry[1]
                if (isinstance(row, dict) and row.get('path') == relative and isinstance(row.get('sha256'), str)
                        and isinstance(row.get('assetId'), str)):
                    cached = (entry[0], policy, row)
                    _ASSET_ROWS[key] = deepcopy(cached)
        if cached is not None and cached[0] == states[path] and cached[1] == policy:
            row = deepcopy(cached[2])
        else:
            data = path.read_bytes()
            meta, _ = _parse(data, relative)
            if states[path] != signature(path):
                raise StructureError('KNOWLEDGE_STALE', 'asset changed while cataloging')
            row = {**meta, 'path': relative, 'sha256': _digest(data)}
            _ASSET_ROWS[key] = (states[path], policy, deepcopy(row))
        if row['assetId'] in seen:
            raise StructureError('KNOWLEDGE_DUPLICATE_ID', f'duplicate assetId: {row["assetId"]}')
        seen.add(row['assetId'])
        rows.append(_catalog_order(row))
    if files != _files() or any(signature(path) != state for path, state in states.items()):
        raise StructureError('KNOWLEDGE_STALE', 'asset catalog membership or content changed')
    live = {str(path.relative_to(TOOLBOX_ROOT)) for path in files}
    for key in list(_ASSET_ROWS):
        if key[:2] == (str(TOOLBOX_ROOT), str(ASSETS)) and key[2] not in live:
            del _ASSET_ROWS[key]
    from .prepare_dependencies import observe_knowledge
    observe_knowledge(TOOLBOX_ROOT, {path.relative_to(TOOLBOX_ROOT).as_posix(): states[path] for path in files},
                      directory=ASSETS.relative_to(TOOLBOX_ROOT).as_posix(),
                      members=[path.relative_to(TOOLBOX_ROOT).as_posix() for path in files])
    return rows


def list_assets(*, status: str | None = None, station: str | None = None, kind: str | None = None) -> dict:
    if status is not None and status not in STATUSES or station is not None and station not in STATIONS or kind is not None and kind not in KINDS:
        raise StructureError('KNOWLEDGE_FILTER_INVALID', 'invalid knowledge filter')
    rows = [row for row in _catalog_rows()
            if (status is None or row['status'] == status)
            and (station is None or station in row['stations'])
            and (kind is None or row['kind'] == kind)]
    return {'ok': True, 'code': 'KNOWLEDGE_CATALOG', 'items': rows, 'count': len(rows)}


def read_asset(asset_id: str, *, expected_hash: str | None = None) -> dict:
    if not isinstance(asset_id, str) or not ID.fullmatch(asset_id):
        raise StructureError('KNOWLEDGE_ID_INVALID', 'invalid assetId')
    row = next((row for row in list_assets()['items'] if row['assetId'] == asset_id), None)
    if row is None:
        raise StructureError('KNOWLEDGE_NOT_FOUND', 'asset does not exist')
    return _read_selected(row, expected_hash=expected_hash)


def _read_selected(row, *, expected_hash=None):
    from .static_cache import signature
    from .write_sets import guard_observation
    path = store_path(row['path'])
    if not path.is_relative_to(ASSETS) or not path.is_file():
        raise StructureError('KNOWLEDGE_PATH_UNSAFE', 'asset selection is not a safe asset file')
    before = signature(path)
    state, data = guard_observation(path)
    current = state['sha256']
    if (data is None or before != signature(path)
            or (expected_hash is not None and current != expected_hash) or current != row['sha256']):
        raise StructureError('KNOWLEDGE_STALE', 'asset changed after selection')
    meta, body = _parse(data, row['path'])
    from .prepare_dependencies import observe_knowledge
    observe_knowledge(TOOLBOX_ROOT, {row['path']: before})
    return {'ok': True, 'code': 'KNOWLEDGE_ASSET', 'metadata': meta, 'path': row['path'],
            'sha256': current, 'body': body}


@contextmanager
def _asset_lock():
    lock_path = store_path('knowledge/.cache/edit.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path('knowledge/.cache/edit.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+b') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise StructureError('KNOWLEDGE_PATH_UNSAFE', 'knowledge lock must be a regular file')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            require_store()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save_asset(*, asset_id: str, metadata: dict, body: str, expected_hash: str | None) -> dict:
    """CAS edit of an existing asset. New assets are created from a separate reviewed import."""
    with _asset_lock():
        current = read_asset(asset_id)
        if expected_hash is None or current['sha256'] != expected_hash:
            raise StructureError('KNOWLEDGE_STALE', 'asset changed since editor opened it')
        if not isinstance(metadata, dict) or metadata.get('assetId') != asset_id or not isinstance(body, str):
            raise StructureError('KNOWLEDGE_ASSET_INVALID', 'asset identity and body required')
        if current['metadata']['status'] != 'active' and metadata.get('status') == 'active':
            notes = {field: metadata.get(field) for field in ('applicability', 'exclusions', 'license')}
            if (any(not isinstance(value, str) or not value.strip() for value in notes.values())
                    or notes['license'].strip() == 'unconfirmed'):
                raise StructureError('KNOWLEDGE_PROMOTION_REVIEW_REQUIRED',
                                     'promotion needs applicability, exclusions and local usage rights')
        payload = ('---\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n---\n' + body).encode('utf-8')
        _parse(payload, current['path'])
        path = store_path(current['path'])
        if _digest(path.read_bytes()) != expected_hash:
            raise StructureError('KNOWLEDGE_STALE', 'asset changed during edit')
        history = store_path(f'knowledge/.history/{asset_id}/{expected_hash}.md')
        if history.exists() and (not history.is_file() or _digest(history.read_bytes()) != expected_hash):
            raise StructureError('KNOWLEDGE_HISTORY_CORRUPT', 'existing history snapshot differs from its content identity')
        if not history.exists():
            write_bytes(history, path.read_bytes())
        write_bytes(store_path(current['path']), payload)
        return {'ok': True, 'code': 'KNOWLEDGE_SAVED', 'assetId': asset_id, 'sha256': _digest(payload)}


def create_asset(*, title: str, kind: str, stations: list[str], intents: list[str], tags: list[str],
                 body: str, applicability: str = '', exclusions: str = '', license: str = 'authored-local') -> dict:
    require_store()
    if kind not in KINDS or not isinstance(title, str) or not title.strip() or not isinstance(body, str):
        raise StructureError('KNOWLEDGE_ASSET_INVALID', 'title, type and body required')
    asset_id = 'KN-' + uuid.uuid4().hex[:16].upper()
    meta = {'assetId': asset_id, 'title': title.strip(), 'kind': kind, 'status': 'draft',
            'stations': stations, 'intents': intents, 'tags': tags,
            'source': {'path': 'authored', 'sha256': ''}, 'applicability': applicability,
            'exclusions': exclusions, 'license': license}
    path = store_path((ASSETS / 'authored' / (asset_id + '.md')).relative_to(TOOLBOX_ROOT).as_posix())
    payload = ('---\n' + json.dumps(meta, ensure_ascii=False, indent=2) + '\n---\n' + body).encode('utf-8')
    _parse(payload, str(path))
    with _asset_lock():
        path = store_path(path.relative_to(TOOLBOX_ROOT).as_posix())
        if path.exists():
            raise StructureError('KNOWLEDGE_DUPLICATE_ID', 'generated identity collision')
        write_bytes(path, payload)
    return {'ok': True, 'code': 'KNOWLEDGE_CREATED', 'assetId': asset_id,
            'path': str(path.relative_to(TOOLBOX_ROOT)), 'sha256': _digest(payload)}


def _version_path(asset_id: str, version_hash: str) -> Path:
    if not isinstance(asset_id, str) or not ID.fullmatch(asset_id):
        raise StructureError('KNOWLEDGE_ID_INVALID', 'invalid assetId')
    if not isinstance(version_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', version_hash):
        raise StructureError('KNOWLEDGE_VERSION_INVALID', 'invalid version hash')
    return store_path(f'knowledge/.history/{asset_id}/{version_hash}.md')


def list_asset_versions(asset_id: str) -> dict:
    current = read_asset(asset_id)
    history = _version_path(asset_id, current['sha256']).parent
    items = [{'sha256': current['sha256'], 'current': True, 'title': current['metadata']['title'],
              'status': current['metadata']['status']}]
    if history.exists():
        if history.is_symlink() or not history.is_dir():
            raise StructureError('KNOWLEDGE_HISTORY_UNSAFE', 'history directory is unsafe')
        paths = list(history.glob('*.md'))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise StructureError('KNOWLEDGE_HISTORY_UNSAFE', 'history file is unsafe')
        for path in sorted(paths, key=lambda item: (-item.stat().st_mtime_ns, item.name)):
            path = store_path(path.relative_to(TOOLBOX_ROOT).as_posix())
            version = path.stem
            if not re.fullmatch(r'[0-9a-f]{64}', version):
                raise StructureError('KNOWLEDGE_HISTORY_UNSAFE', 'history file is unsafe')
            data = path.read_bytes()
            if _digest(data) != version:
                raise StructureError('KNOWLEDGE_HISTORY_CORRUPT', f'history hash mismatch: {version}')
            meta, _ = _parse(data, str(path))
            if meta['assetId'] != asset_id:
                raise StructureError('KNOWLEDGE_HISTORY_CORRUPT', 'history identity mismatch')
            if version != current['sha256']:
                items.append({'sha256': version, 'current': False, 'title': meta['title'],
                              'status': meta['status']})
    return {'ok': True, 'code': 'KNOWLEDGE_VERSIONS', 'assetId': asset_id, 'items': items}


def read_asset_version(asset_id: str, version_hash: str) -> dict:
    current = read_asset(asset_id)
    if version_hash == current['sha256']:
        return {**current, 'current': True}
    path = _version_path(asset_id, version_hash)
    if path.is_symlink() or not path.is_file():
        raise StructureError('KNOWLEDGE_VERSION_NOT_FOUND', 'history version does not exist')
    data = path.read_bytes()
    if _digest(data) != version_hash:
        raise StructureError('KNOWLEDGE_HISTORY_CORRUPT', 'history hash mismatch')
    meta, body = _parse(data, str(path))
    if meta['assetId'] != asset_id:
        raise StructureError('KNOWLEDGE_HISTORY_CORRUPT', 'history identity mismatch')
    return {'ok': True, 'code': 'KNOWLEDGE_VERSION', 'metadata': meta, 'body': body,
            'sha256': version_hash, 'current': False, 'path': current['path']}


def restore_asset(*, asset_id: str, version_hash: str, expected_hash: str) -> dict:
    old = read_asset_version(asset_id, version_hash)
    if old['current']:
        raise StructureError('KNOWLEDGE_VERSION_INVALID', 'selected version is already current')
    saved = save_asset(asset_id=asset_id, metadata=old['metadata'], body=old['body'],
                       expected_hash=expected_hash)
    return {**saved, 'code': 'KNOWLEDGE_RESTORED', 'restoredFrom': version_hash}


def knowledge_health() -> dict:
    try:
        catalog = list_assets()
        active = [row for row in catalog['items'] if row['status'] == 'active']
        problems = []
        for row in catalog['items']:
            source = row['source']
            relative = source['path']
            if relative == 'authored' and source['sha256'] == '':
                continue
            if not relative.startswith('knowledge/sources/legacy/'):
                problems.append({'code': 'KNOWLEDGE_SOURCE_UNSAFE', 'assetId': row['assetId']})
                continue
            path = store_path(relative)
            if path.is_symlink() or not path.is_file() or _digest(path.read_bytes()) != source['sha256']:
                problems.append({'code': 'KNOWLEDGE_SOURCE_MISMATCH', 'assetId': row['assetId']})
        migration = store_path('knowledge/migrations/initial-map.json')
        migration_count = 0
        if migration.is_file():
            try:
                entries = json.loads(migration.read_text(encoding='utf-8'))['entries']
                if not isinstance(entries, list):
                    raise ValueError('entries must be a list')
                migration_count = len(entries)
                known = {row['assetId']: row for row in catalog['items']}
                for entry in entries:
                    relative = entry['source']
                    source_path = PurePosixPath(relative)
                    if (source_path.is_absolute() or '..' in source_path.parts
                            or not (relative.startswith('libraries/') or relative.startswith('corpus/')
                                    or relative.startswith('govel-'))):
                        raise ValueError('unsafe source path')
                    archive = store_path('knowledge/sources/legacy/' + relative)
                    if (archive.is_symlink() or not archive.is_file()
                            or _digest(archive.read_bytes()) != entry['sha256']):
                        problems.append({'code': 'KNOWLEDGE_MIGRATION_SOURCE_MISMATCH', 'source': relative})
                    asset_id = entry.get('assetId')
                    if asset_id:
                        asset = known.get(asset_id)
                        if (asset is None or asset['path'] != entry.get('asset')
                                or asset['source']['sha256'] != entry['sha256']):
                            problems.append({'code': 'KNOWLEDGE_MIGRATION_ASSET_MISMATCH', 'source': relative})
                        original = TOOLBOX_ROOT / relative
                        if original.is_symlink() and (asset is None or original.resolve() != (TOOLBOX_ROOT / asset['path']).resolve()):
                            problems.append({'code': 'KNOWLEDGE_COMPAT_LINK_MISMATCH', 'source': relative})
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                problems.append({'code': 'KNOWLEDGE_MIGRATION_MAP_INVALID', 'message': str(exc)})
        from .knowledge_index import index_state
        index = index_state(catalog['items'])
        return {'ok': not problems, 'code': 'KNOWLEDGE_HEALTH', 'healthy': not problems,
                'total': catalog['count'], 'active': len(active), 'migrationSources': migration_count,
                'index': index, 'problems': problems}
    except (StructureError, ProviderError) as exc:
        return {'ok': False, 'code': 'KNOWLEDGE_HEALTH', 'healthy': False,
                'problems': [{'code': exc.code, 'message': str(exc)}]}
