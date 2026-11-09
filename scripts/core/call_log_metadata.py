"""Describe observed calls without resolving project state or following references.

References are fields in the supplied request or response, not claims that an
artifact was used, created, approved, or is still readable. Source fingerprints
describe the first source bytes this process observed; Python does not expose a
reliable correspondence between a loaded module and subsequently edited files.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from threading import Lock
import uuid

from .project import TOOLBOX_ROOT
from .static_cache import cached_bytes


# Protocol field names, not inferred IDs or a vocabulary searched in prose.
_REFERENCE_FIELDS = {
    'projectRoot', 'projectId', 'task', 'station', 'chapterId', 'operationId',
    'operation', 'planHash', 'baseRevision', 'baseHash', 'beforeHash', 'afterHash',
    'beforeRevision', 'afterRevision', 'revision', 'structureHash',
    'structureRevision', 'sourceHash', 'sourceFingerprint', 'sourceSha256',
    'candidateId', 'candidateVersion', 'version', 'bodySha256', 'fileName',
    'requestId', 'returnRequestId', 'replayed', 'activeToken', 'artifactType',
    'artifactSha256', 'artifactFileSha256', 'artifactPath', 'inputPath',
    'inputSha256', 'draftSha256', 'handoffId', 'candidateActiveToken',
    'contextHash', 'bindingGeneration', 'formalSha256', 'proseSha256',
    'sourcePlanHash', 'path', 'sha256', 'backupPath', 'sessionReadReceipt',
    'contextProofRef', 'readId', 'fragmentId', 'evidenceId', 'snapshotRef',
    'prepareRef', 'sessionRef',
}
_REFERENCE_FIELDS |= {re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
                      for key in _REFERENCE_FIELDS}
_TARGET_FIELDS = {
    'projectRoot': ('projectRoot', 'project_root'),
    'projectId': ('projectId', 'project_id'),
    'task': ('task',),
    'station': ('station',),
    'chapterId': ('chapterId', 'chapter_id'),
}
_BRIDGE_NAMES = {'read_discovered_operation', 'export_discovered_operation'}
_BRIDGE_BINDINGS = {'capabilities.read', 'capabilities.export'}

_SESSION_STARTED_AT = datetime.now(timezone.utc).isoformat()
_SESSION_ID = uuid.uuid4().hex
_SOURCE_OBSERVATIONS: dict[tuple[str, str], dict] = {}
_SOURCE_LOCK = Lock()


def _pointer(parent: str, key: object) -> str:
    return parent + '/' + str(key).replace('~', '~0').replace('/', '~1')


def correlations(value: object, origin: str, max_depth: int) -> list[dict]:
    """Return scalar protocol references and their exact JSON Pointers.

    The caller supplies a sanitized JSON value. Composite fields, including a
    dependency manifest, are traversed into their existing path/hash members;
    whole payloads are not copied again. A depth of one includes root fields.
    """
    if type(max_depth) is not int or max_depth < 1:
        raise ValueError('call reference depth must be a positive integer')
    rows = []
    stack = [(None, value, '', 0, frozenset())]
    while stack:
        field, current, pointer, depth, ancestors = stack.pop()
        if depth > max_depth:
            continue
        scalar = current is None or type(current) in (str, int, bool)
        scalar = scalar or (type(current) is float and math.isfinite(current))
        if field in _REFERENCE_FIELDS and scalar:
            rows.append({'field': field, 'value': current,
                         'pointer': pointer, 'origin': origin})
        if not isinstance(current, (dict, list)) or id(current) in ancestors:
            continue
        ancestors = ancestors | {id(current)}
        if isinstance(current, dict):
            stack.extend((key, child, _pointer(pointer, key), depth + 1, ancestors)
                         for key, child in reversed(list(current.items()))
                         if isinstance(key, str))
        else:
            stack.extend((None, current[index], _pointer(pointer, index),
                          depth + 1, ancestors)
                         for index in range(len(current) - 1, -1, -1))
    return rows


def _direct_targets(arguments: dict) -> dict:
    targets = {}
    for canonical, aliases in _TARGET_FIELDS.items():
        for key in aliases:
            value = arguments.get(key)
            if isinstance(value, str):
                targets[canonical] = (os.path.abspath(os.path.expanduser(value))
                                      if canonical == 'projectRoot' else value)
                break
    return targets


def target_context(value: object) -> dict:
    """Describe only directly supplied targets, never a suggested next action.

    Accepts parsed CLI arguments, direct tool arguments, or a raw tools/call RPC.
    Explicit discovery bridges may supply task/chapter targets in their inner
    arguments. Their project root remains the outer root, as the bridge requires.
    Missing IDs remain missing; no marker, chapter alias, or path is resolved.
    """
    if not isinstance(value, dict):
        return {}
    arguments, bridge = value, False
    if 'method' in value:
        if value.get('method') != 'tools/call':
            return {}
        params = value.get('params')
        if not isinstance(params, dict) or not isinstance(params.get('arguments'), dict):
            return {}
        arguments = params['arguments']
        bridge = params.get('name') in _BRIDGE_NAMES
    else:
        bridge = value.get('operation_binding') in _BRIDGE_BINDINGS
    targets = _direct_targets(arguments)
    if bridge and isinstance(arguments.get('arguments'), dict):
        inner = _direct_targets(arguments['arguments'])
        inner.pop('projectRoot', None)
        targets.update(inner)
    return targets


def _signature(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _loaded_sources(root: Path) -> set[Path]:
    """Inspect imported module filenames, not directory contents."""
    directory = root / 'scripts'
    sources = set()
    for module in sys.modules.copy().values():
        attributes = getattr(module, '__dict__', {})
        file = attributes.get('__file__') if isinstance(attributes, dict) else None
        if not isinstance(file, str) or not file.endswith('.py'):
            continue
        path = Path(file)
        if 'scripts' not in path.parts:
            continue
        try:
            path = path.resolve()
            path.relative_to(directory)
        except (OSError, RuntimeError, ValueError):
            continue
        sources.add(path)
    return sources


def _observe_source(path: Path) -> dict:
    before = _signature(path)
    try:
        raw = path.read_bytes()
    except OSError:
        return {'signature': before, 'sha256': None, 'issue': 'source_unreadable'}
    if before is None or before != _signature(path):
        return {'signature': before, 'sha256': None,
                'issue': 'source_changed_during_observation'}
    return {'signature': before, 'sha256': hashlib.sha256(raw).hexdigest(), 'issue': None}


def toolbox_identity() -> dict:
    """Identify this process and observed implementation without a tree scan.

    Hashes are taken once per loaded source path. Later calls stat those paths
    and report disk changes without assigning new disk bytes to old loaded code.
    The current manifest is separately hashed; this is not a signed build ID or
    proof that every source, registry, contract, or client has been captured.
    """
    root = TOOLBOX_ROOT.resolve()
    raw_manifest = cached_bytes(root / 'toolbox.manifest.json')
    manifest = json.loads(raw_manifest)
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('version'), str)
            or not manifest['version'] or type(manifest.get('schemaVersion')) is not int
            or type(manifest.get('compatibilityVersion')) is not int):
        raise ValueError('call logging cannot identify the toolbox manifest')
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    root_key = str(root)
    with _SOURCE_LOCK:
        for path in sorted(_loaded_sources(root)):
            key = (root_key, path.relative_to(root).as_posix())
            if key not in _SOURCE_OBSERVATIONS:
                _SOURCE_OBSERVATIONS[key] = _observe_source(path)
        observations = {relative: record
                        for (base, relative), record in _SOURCE_OBSERVATIONS.items()
                        if base == root_key}
        changed = sorted(relative for relative, record in observations.items()
                         if _signature(root / relative) != record['signature'])
        unavailable = sorted(relative for relative, record in observations.items()
                             if record['issue'] is not None)
        source_hashes = {relative: record['sha256']
                         for relative, record in sorted(observations.items())}
    fingerprint = hashlib.sha256(json.dumps(
        {'manifestSha256': manifest_hash, 'sources': source_hashes},
        ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'version': manifest['version'], 'schemaVersion': manifest['schemaVersion'],
        'compatibilityVersion': manifest['compatibilityVersion'],
        'manifestSha256': manifest_hash, 'pythonVersion': sys.version.split()[0],
        'pythonExecutable': sys.executable, 'pid': os.getpid(),
        'sessionId': _SESSION_ID, 'sessionStartedAt': _SESSION_STARTED_AT,
        'sourceFingerprint': fingerprint,
        'sourceFingerprintKind': 'firstObservedSourceBytes',
        'sourceScope': 'loaded_toolbox_python_modules_and_current_manifest',
        'sourceFileCount': len(source_hashes),
        'sourceChangedSinceObservation': changed,
        'sourceObservationIncomplete': bool(changed or unavailable),
        'unavailableSources': unavailable,
    }
