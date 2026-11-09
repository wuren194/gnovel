"""Disposable, project-bound transport references outside authored project files.

These snapshots convey no permission or review credit. Missing snapshots require
preparation again; they are never rebound to newer project inputs implicitly.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .structure_store import StructureError

REFERENCE = re.compile(r'gn:([a-z-]+):([0-9a-f]{32})\Z')


def _directory():
    path = Path(tempfile.gettempdir()) / f'govel-transport-{os.getuid()}'
    path.mkdir(mode=0o700, exist_ok=True)
    state = path.lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & 0o077:
        raise StructureError('TRANSPORT_CACHE_INVALID', 'transport directory must be private and owned by this user')
    return path


def put(root, kind, value):
    document = {'version': 1, 'projectRoot': str(Path(root).expanduser().resolve()), 'kind': kind, 'value': value}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(raw).hexdigest()
    reference = f'gn:{kind}:{digest[:32]}'
    path = _directory() / (reference.replace(':', '-') + '.json')
    if path.exists():
        try:
            get(root, reference, kind=kind)
            return reference
        except StructureError:
            # Only an explicit producer may reconstruct a lost/corrupt cache;
            # readers never substitute current inputs for an old reference.
            pass
    from .jsonio import write_bytes
    write_bytes(path, raw, mode=0o600)
    return reference


def get(root, reference, *, kind):
    match = REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
    if match is None or match[1] != kind:
        raise StructureError('TRANSPORT_REFERENCE_INVALID', 'use the original typed transport reference')
    path = _directory() / (reference.replace(':', '-') + '.json')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            state = os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & 0o077:
                raise ValueError('invalid snapshot permissions')
            raw = handle.read()
        document = json.loads(raw)
        from .paths import stored_root_matches
        if (hashlib.sha256(raw).hexdigest()[:32] != match[2] or document.get('version') != 1
                or document.get('kind') != kind or not stored_root_matches(Path(root), document.get('projectRoot'))):
            raise ValueError('snapshot scope or content changed')
        return document['value']
    except (OSError, ValueError, KeyError) as exc:
        raise StructureError('TRANSPORT_SNAPSHOT_UNAVAILABLE', 'snapshot missing or changed; prepare again') from exc


def preparation_signature(root):
    from .static_cache import signature, toolbox_signature
    root = Path(root).resolve()
    paths = [root / 'AGENTS.md']
    for directory in ('.govel', '设定', '规划', '正文', '工作台'):
        paths.extend(p for p in (root / directory).rglob('*')
                     if (p.is_file() or p.is_symlink()) and p.name != 'structure.lock')
    value = (tuple((str(p), signature(p)) for p in sorted(set(paths))), toolbox_signature())
    return hashlib.sha256(repr(value).encode()).hexdigest()


def prepared(root, reference):
    snapshot = get(root, reference, kind='prepare')
    if 'dependencies' in snapshot:
        from .prepare_dependencies import validate_dependencies
        validate_dependencies(root, snapshot['dependencies'])
    elif snapshot.get('inputs') != preparation_signature(root):
        raise StructureError('READ_BATCH_STALE', 'preparation inputs changed; prepare again')
    return snapshot['report'], snapshot['handoff']


def read_snapshot(root, *, reference, maxBytes=None, cursor=None, view=None):
    from .project import query_policy
    from .text_pages import text_page
    match = REFERENCE.fullmatch(reference)
    if match is None or match[1] not in {'context-proof', 'diagnostics', 'diagnostics-diff', 'read-mappings', 'summary',
                                       'evidence-snapshot', 'evidence-submission', 'evidence-pending', 'evidence-error',
                                       'repair', 'repair-review-input', 'prepare', 'doctor-report', 'source-record', 'source-search', 'evidence-progress',
                                       'content-resolution', 'evidence-reading', 'authoring-report', 'review-form', 'candidate-handoff', 'candidate-recipe', 'candidate-reading'}:
        raise StructureError('TRANSPORT_REFERENCE_INVALID', 'this reference is not an audit snapshot')
    if view is not None and (match[1] != 'review-form' or view not in ('model', 'full')):
        raise StructureError('TRANSPORT_VIEW_INVALID', 'explicit snapshot view is available only for review-form model/full')
    value = get(root, reference, kind=match[1])
    if match[1] == 'prepare':
        from copy import deepcopy
        value = deepcopy(value['report'])
        value['reading']['credential'] = reference
        value['prepareRef'] = reference
    if match[1] == 'review-form':
        if not isinstance(value, dict) or not isinstance(value.get('report'), dict):
            raise StructureError('TRANSPORT_SNAPSHOT_UNAVAILABLE', 'review form snapshot has no public report')
        value = value['report']
        if view == 'model':
            from .authoring_forms import form_report
            value = form_report(value, reference, 'model')
    budget = query_policy()['maxBytes']['detail'] if maxBytes is None else maxBytes
    if type(budget) is not int or not 1 <= budget <= query_policy()['maxBytes']['detail']:
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid snapshot byte budget')
    binding_scope = f'{reference}:{budget}'
    if match[1] == 'review-form':
        binding_scope += ':' + (view or 'full')
    binding = hashlib.sha256(binding_scope.encode()).hexdigest()
    return text_page({'ok': True, 'code': 'TRANSPORT_SNAPSHOT', 'reference': reference,
                      'historicalSnapshot': True, 'freshnessRevalidated': False},
                     [{'id': reference, 'text': json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}],
                     budget=budget, limit=1, cursor=cursor, binding=binding, source=reference)
