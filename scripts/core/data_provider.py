"""Explicit local data providers. Providers contain data, never executable code."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = {'intensity', 'text-patterns', 'knowledge'}
STATIONS = {'composer', 'router', 'planner', 'writer', 'editor', 'titler'}
_VALIDATED = {}
EXPECTED_PROVIDER_ENV = 'GNOVEL_EXPECTED_PROVIDER'


def _check_expected(current):
    value = os.environ.get(EXPECTED_PROVIDER_ENV)
    if value is None:
        return
    try:
        expected = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError('PROVIDER_BINDING_INVALID', 'diagnostic provider binding is invalid') from exc
    if expected != current:
        raise ProviderError('PROVIDER_CHANGED', 'provider differs from the diagnostic input snapshot')


def pinned_environment(expected):
    """Pass the same existing binding to every owned diagnostic subprocess."""
    if binding() != expected:
        raise ProviderError('PROVIDER_CHANGED', 'provider changed before starting diagnostics')
    return {**os.environ, EXPECTED_PROVIDER_ENV: json.dumps(expected, sort_keys=True, separators=(',', ':'))}


def _resource_states(root, manifest):
    resources = list(manifest['registries'].values()) + [row['resource'] for row in manifest['materials']]
    files = {root / resource['path'] for resource in resources}
    parents = {parent for path in files for parent in path.parents}
    states = []
    for path in sorted(files | parents):
        try:
            state = path.lstat()
            fields = (state.st_dev, state.st_ino, state.st_mode)
            if path in files:
                fields += (state.st_size, state.st_mtime_ns, state.st_ctime_ns)
            states.append((str(path), fields))
        except OSError:
            states.append((str(path), None))
    return tuple(states)


class ProviderError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.details = {}


def _json(path):
    from .static_cache import cached_json
    if not path.is_file():
        raise ProviderError('PROVIDER_UNREADABLE', 'configured provider file is unavailable or invalid')
    try:
        value = cached_json(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProviderError('PROVIDER_UNREADABLE', 'configured provider file is unavailable or invalid') from exc
    if not isinstance(value, dict):
        raise ProviderError('PROVIDER_INVALID', 'provider document must be an object')
    return value


def confined_path(root, relative):
    rel = PurePosixPath(relative) if isinstance(relative, str) else None
    if (rel is None or not relative or rel.is_absolute() or '..' in rel.parts
            or '\\' in relative or relative != rel.as_posix()):
        raise ProviderError('PROVIDER_PATH_UNSAFE', 'provider paths must be normalized relative paths')
    path = root / relative
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ProviderError('PROVIDER_PATH_UNSAFE', 'provider paths cannot use symbolic links')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ProviderError('PROVIDER_PATH_UNSAFE', 'provider resource escapes its directory')
    return path


def _resource(root, spec):
    if (not isinstance(spec, dict) or set(spec) != {'path', 'sha256'}
            or not isinstance(spec['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', spec['sha256'])):
        raise ProviderError('PROVIDER_RESOURCE_INVALID', 'resource requires path and SHA-256')
    path = confined_path(root, spec['path'])
    if not path.is_file():
        raise ProviderError('PROVIDER_RESOURCE_MISSING', 'declared provider resource must be a regular file')
    from .static_cache import cached_bytes
    try:
        data = cached_bytes(path)
    except OSError as exc:
        raise ProviderError('PROVIDER_RESOURCE_MISSING', 'declared provider resource is missing') from exc
    if hashlib.sha256(data).hexdigest() != spec['sha256']:
        raise ProviderError('PROVIDER_RESOURCE_CHANGED', 'provider resource changed; review and update its manifest')
    return path, data


def active_provider():
    """Absence is optional. An explicitly configured but broken provider fails."""
    configured = os.environ.get('GNOVEL_PROVIDER_CONFIG')
    if configured is None:
        _check_expected({'providerId': None, 'version': None, 'sha256': None})
        return None
    path = Path(configured).expanduser()
    if (not configured or not path.is_absolute() or path.is_symlink()
            or path.resolve().is_relative_to(ROOT)):
        raise ProviderError('PROVIDER_CONFIG_INVALID', 'GNOVEL_PROVIDER_CONFIG must name an absolute regular file outside the checkout')
    config = _json(path)
    if set(config) != {'schemaVersion', 'root'} or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1:
        raise ProviderError('PROVIDER_CONFIG_INVALID', 'unsupported provider configuration')
    if not isinstance(config['root'], str) or not config['root']:
        raise ProviderError('PROVIDER_CONFIG_INVALID', 'provider root must be a directory')
    root = Path(config['root']).expanduser()
    if (not root.is_absolute() or root.is_symlink() or not root.is_dir()
            or root.resolve().is_relative_to(ROOT)):
        raise ProviderError('PROVIDER_ROOT_INVALID', 'provider must be an existing directory outside the source checkout')
    manifest = _json(confined_path(root, 'provider.json'))
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    _check_expected({key: manifest.get(key) for key in ('providerId', 'version')} | {'sha256': identity})
    key = (str(root), identity)
    previous = _VALIDATED.get(key)
    if previous is not None and _resource_states(root, manifest) == previous:
        return {'root': root, 'manifest': manifest, 'sha256': identity}
    if (set(manifest) != {'schemaVersion', 'providerId', 'version', 'registries', 'materials'}
            or type(manifest['schemaVersion']) is not int or manifest['schemaVersion'] != 1
            or not isinstance(manifest['providerId'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', manifest['providerId'])
            or not isinstance(manifest['version'], str) or not manifest['version']
            or not isinstance(manifest['registries'], dict) or set(manifest['registries']) - REGISTRIES
            or not isinstance(manifest['materials'], list)):
        raise ProviderError('PROVIDER_INVALID', 'unsupported provider manifest')
    for spec in manifest['registries'].values():
        _resource(root, spec)
    seen = set()
    for item in manifest['materials']:
        if (not isinstance(item, dict) or set(item) != {'id', 'role', 'stations', 'resource', 'when'}
                or not isinstance(item['id'], str) or not re.fullmatch('[A-Za-z0-9-]+', item['id'])
                or item['id'] in seen or item['role'] not in {'reference', 'rule'}
                or not isinstance(item['stations'], list) or not item['stations']
                or any(not isinstance(s, str) for s in item['stations'])
                or set(item['stations']) - STATIONS):
            raise ProviderError('PROVIDER_MATERIAL_INVALID', 'invalid provider material declaration')
        condition = item['when']
        if condition is not None and (not isinstance(condition, dict)
                or set(condition) != {'field', 'op', 'value'} or condition['op'] not in {'eq', 'includes'}
                or not isinstance(condition['field'], str)):
            raise ProviderError('PROVIDER_MATERIAL_INVALID', 'invalid material applicability condition')
        seen.add(item['id'])
        _resource(root, item['resource'])
    before = _resource_states(root, manifest)
    # Bind the cached validation to a stable physical observation. Resource
    # reads already compare their own before/after states via cached_bytes.
    for spec in manifest['registries'].values():
        _resource(root, spec)
    for item in manifest['materials']:
        _resource(root, item['resource'])
    if before != _resource_states(root, manifest):
        raise ProviderError('PROVIDER_CHANGED', 'provider resources changed while validating')
    _VALIDATED.clear()
    _VALIDATED[key] = before
    return {'root': root, 'manifest': manifest, 'sha256': identity}


def binding():
    provider = active_provider()
    if provider is None:
        return {'providerId': None, 'version': None, 'sha256': None}
    return {key: provider['manifest'][key] for key in ('providerId', 'version')} | {'sha256': provider['sha256']}


def require_project_provider(root):
    """A project created with a provider cannot silently run without that pack."""
    marker = Path(root) / '.gnovel/project.json'
    if not marker.is_file():
        return
    required = _json(marker).get('dataProvider')
    if required is None:
        return
    if (not isinstance(required, dict) or set(required) != {'providerId'}
            or not isinstance(required['providerId'], str)):
        raise ProviderError('PROVIDER_PROJECT_INVALID', 'project provider requirement is invalid')
    current = binding()
    if current['providerId'] != required['providerId']:
        raise ProviderError('PROVIDER_REQUIRED', 'load the data provider declared by this project')


def registry(name, default_path):
    if name not in REGISTRIES:
        raise ProviderError('PROVIDER_REGISTRY_INVALID', 'registry is not extensible')
    provider = active_provider()
    if provider is None or name not in provider['manifest']['registries']:
        return _json(default_path)
    _, data = _resource(provider['root'], provider['manifest']['registries'][name])
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise ProviderError('PROVIDER_REGISTRY_INVALID', 'provider registry is invalid') from exc
    if not isinstance(value, dict):
        raise ProviderError('PROVIDER_REGISTRY_INVALID', 'provider registry must be an object')
    return value


def storage_root():
    provider = active_provider()
    if provider is not None:
        return provider['root']
    value = os.environ.get('GNOVEL_DATA_HOME')
    path = Path(value).expanduser() if value else Path.home() / 'Library/Application Support/gnovel'
    if not path.is_absolute() or path.resolve().is_relative_to(ROOT):
        raise ProviderError('PROVIDER_ROOT_INVALID', 'data directory must be absolute and outside the source checkout')
    return path


def materials(station, design=None):
    provider = active_provider()
    if provider is None:
        return []
    if station not in STATIONS:
        raise ProviderError('PROVIDER_STATION_INVALID', 'unknown workstation')
    result = []
    for item in provider['manifest']['materials']:
        if station in item['stations']:
            matched, evidence = True, None
            if item['when'] is not None:
                from .prompt_conditions import evaluate_condition
                matched, evidence = evaluate_condition(design or {}, item['when'])
            if matched is False:
                continue
            path, body = _resource(provider['root'], item['resource'])
            result.append({'id': item['id'], 'role': item['role'], 'path': item['resource']['path'],
                           'sha256': item['resource']['sha256'], 'bytes': len(body),
                           'active': matched is True, 'condition': evidence})
    return result
