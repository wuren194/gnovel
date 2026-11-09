"""Process-local caches for toolbox inputs, never project truth or approvals."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

_FILES = {}
_VALUES = {}


def signature(path):
    path = Path(path)
    try:
        st = path.stat()
        link = path.lstat()
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_mode, link.st_ino, link.st_mtime_ns, link.st_ctime_ns)


def cached_bytes(path):
    path = Path(path)
    key = str(path.absolute())
    before = signature(path)
    old = _FILES.get(key)
    if before is not None and old is not None and old[0] == before:
        return old[1]
    raw = path.read_bytes()
    if before != signature(path):
        raise ValueError(f'toolbox input changed while reading: {path}')
    _FILES[key] = (before, raw)
    return raw


def cached_json(path):
    raw = cached_bytes(path)
    key = ('json', str(Path(path).absolute()))
    digest = hashlib.sha256(raw).digest()
    old = _VALUES.get(key)
    if old is None or old[0] != digest:
        _VALUES[key] = (digest, json.loads(raw))
    return deepcopy(_VALUES[key][1])


def cached_derived(path, derive):
    """Value derived from one toolbox file; derive reruns only when the file bytes change.

    Hot policy reads skip re-parsing and re-validating, yet callers still receive a
    private copy exactly like cached_json. A failed derivation is never cached.
    """
    raw = cached_bytes(path)
    key = ('derived', str(Path(path).absolute()), derive)
    old = _VALUES.get(key)
    # cached_bytes keeps returning the same bytes object until the file signature
    # changes; holding it here also keeps its identity from being reused.
    if old is None or old[0] is not raw:
        _VALUES[key] = (raw, derive(json.loads(raw)))
    return deepcopy(_VALUES[key][1])


def cached_sha256(path):
    raw = cached_bytes(path)
    key = ('sha256', str(Path(path).absolute()))
    state = _FILES[str(Path(path).absolute())][0]
    old = _VALUES.get(key)
    if old is None or old[0] != state:
        _VALUES[key] = (state, hashlib.sha256(raw).hexdigest())
    return _VALUES[key][1]


def derived_index_signature(path):
    """Metadata-only ctime changes do not change a derived SQLite snapshot."""
    from .write_sets import guard_observation
    path = Path(path)
    before = signature(path)
    key = ('derived-index', str(path.absolute()))
    old = _VALUES.get(key)
    if old is not None and old[0] == before:
        return old[1]
    state, _ = guard_observation(path)
    value = (state['dev'], state['ino'], state['sha256']) if state['exists'] else None
    if before == signature(path):
        _VALUES[key] = (before, value)
    return value


def toolbox_signature():
    from .project import TOOLBOX_ROOT
    paths = [TOOLBOX_ROOT / 'toolbox.manifest.json']
    for directory in ('scripts', 'shared', 'knowledge/assets', 'libraries'):
        paths.extend(p for p in (TOOLBOX_ROOT / directory).rglob('*')
                     if p.suffix in {'.py', '.md', '.json'} and '__pycache__' not in p.parts)
    paths.extend(TOOLBOX_ROOT.glob('govel-*/SKILL.md'))
    paths.append(TOOLBOX_ROOT / 'knowledge/catalog.json')
    paths.extend(TOOLBOX_ROOT / f'knowledge/.cache/search.sqlite3{suffix}' for suffix in ('', '-wal', '-shm'))
    manifest = cached_json(TOOLBOX_ROOT / 'toolbox.manifest.json')
    for references in manifest.get('onDemandReferences', {}).values():
        if not isinstance(references, list):
            continue
        for reference in references:
            path = TOOLBOX_ROOT / reference
            paths.append(path)
            if path.is_dir():
                paths.extend(p for p in path.rglob('*') if p.is_file())
    index = TOOLBOX_ROOT / 'knowledge/.cache/search.sqlite3'
    return tuple((str(p), derived_index_signature(p) if p == index else signature(p)) for p in sorted(set(paths)))


def cached_definition(name, factory):
    # Paths and stat identity include implementation, policy and template edits.
    key = (name, factory)
    current = toolbox_signature()
    old = _VALUES.get(key)
    if old is None or old[0] != current:
        value = factory()
        after = toolbox_signature()
        if current != after:
            before_by_path, after_by_path = dict(current), dict(after)
            changed = sorted(path for path in before_by_path.keys() | after_by_path.keys()
                             if before_by_path.get(path) != after_by_path.get(path))
            raise ValueError('toolbox definitions changed while building operations: ' + ', '.join(changed))
        _VALUES[key] = (current, value)
    return deepcopy(_VALUES[key][1])
