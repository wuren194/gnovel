"""Typed dependencies for disposable preparation snapshots, not a task ledger."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

from .static_cache import signature, toolbox_signature
from .structure_store import StructureError

_ACTIVE = ContextVar('preparation_dependencies', default=None)


class _ObservedReads(dict):
    def __init__(self):
        super().__init__()
        self.knowledge = None


def _state(path):
    try:
        value = signature(path)
        return list(value) if value is not None else None
    except OSError as exc:
        return {'unreadable': type(exc).__name__}


def _observe_open(event, arguments):
    current = _ACTIVE.get()
    if current is None or event != 'open' or not isinstance(arguments[0], (str, bytes, os.PathLike)):
        return
    flags = arguments[2]
    if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        return
    path = Path(os.fsdecode(arguments[0])).absolute()
    if not path.is_relative_to(current['root']):
        return
    relative = path.relative_to(current['root']).as_posix()
    if relative == '.gnovel/structure.lock' or '__pycache__' in path.parts:
        return
    current['observed'].setdefault(relative, _state(path))


sys.addaudithook(_observe_open)


def observe_dependency(root, relative):
    current = _ACTIVE.get()
    if current is not None and Path(root).resolve() == current['root']:
        current['observed'].setdefault(relative, _state(current['root'] / relative))


def observe_knowledge(root, files, *, directory=None, members=None):
    """Bind only catalog, index and asset files actually consumed by this task.

    Catalog ranking reads metadata across the asset directory, so membership
    and those file identities matter even when just one excerpt is retained.
    Original sources and unrelated provider directories are never traversed.
    """
    current = _ACTIVE.get()
    if current is None:
        return
    observed = current['observed']
    if observed.knowledge is None:
        observed.knowledge = {'root': str(Path(root).resolve()), 'files': {}, 'queries': {}}
    knowledge = observed.knowledge
    if knowledge['root'] != str(Path(root).resolve()):
        raise StructureError('KNOWLEDGE_PROVIDER_CHANGED', 'knowledge root changed while preparing the task')
    for relative, state in files.items():
        knowledge['files'].setdefault(relative, list(state) if state is not None else None)
    if directory is not None:
        knowledge['queries'].setdefault(directory, list(members))


@contextmanager
def observe_reads(root):
    value = {'root': Path(root).resolve(), 'observed': _ObservedReads()}
    token = _ACTIVE.set(value)
    try:
        yield value['observed']
    finally:
        _ACTIVE.reset(token)


def _json(root, relative):
    from .paths import safe_project_path
    try:
        value = json.loads(safe_project_path(root, relative).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _members(root, directory, pattern):
    base = root / directory
    if base.is_symlink():
        return [directory]
    return sorted(path.relative_to(root).as_posix() for path in base.glob(pattern)
                  if not path.is_dir() or path.is_symlink())


def capture_dependencies(root, *, chapter_id=None, evidence=None):
    root = Path(root).resolve()
    files, queries = {}, []

    def add(relative, kind='content'):
        if not isinstance(relative, str) or not relative:
            return
        path = Path(relative)
        if path.is_absolute():
            from .paths import stored_project_path
            try:
                path = stored_project_path(root, path)
            except (ValueError, OSError):
                return
            relative = path.relative_to(root).as_posix()
        if '..' in Path(relative).parts:
            return
        files.setdefault(relative, {'kind': kind, 'state': _state(root / relative)})

    def query(directory, pattern, kind='content'):
        members = _members(root, directory, pattern)
        queries.append({'directory': directory, 'pattern': pattern, 'members': members})
        for relative in members:
            add(relative, kind)

    for relative in ('AGENTS.md', '.gnovel/project.json', '.gnovel/structure.json'):
        add(relative, 'authorization')
    query('.gnovel/transactions', '*.json', 'authorization')
    source = _json(root, '.gnovel/structure.json')
    if evidence is None:
        add('.gnovel/workflow.json', 'authorization')
        add('.gnovel/aux/story-summary.json')
        for relative in ('.gnovel/source-review.json', '.gnovel/source-review-receipts.json',
                         '.gnovel/source-prose-evidence.json', '.gnovel/source-rule-issues.json'):
            add(relative)
        for chapter in source.get('chapters', []):
            add(chapter.get('filePath'))
        workflow = _json(root, '.gnovel/workflow.json')
        artifacts = workflow.get('artifacts', {})
        selected = artifacts.values() if chapter_id is None else [artifacts.get(chapter_id, {})]
        for chapter in selected:
            for entry in chapter.values() if isinstance(chapter, dict) else []:
                if isinstance(entry, dict):
                    for key in ('artifactPath', 'previousArtifactPath'):
                        add(entry.get(key))
    elif evidence['kind'] == 'review' and evidence.get('reviewContext'):
        add('.gnovel/source-review.json')
        add('.gnovel/source-review-receipts.json')
    elif evidence['kind'] == 'continuity':
        add('.gnovel/workflow.json', 'authorization')
        add('.gnovel/aux/story-summary.json')
        for relative in evidence.get('paths') or []:
            add(relative)
    elif evidence['kind'] == 'prose':
        chapter = next((row for row in source.get('chapters', [])
                        if row['chapterId'] == evidence.get('chapterId')), {})
        add(chapter.get('filePath'))
    toolbox = {path: list(state) if state is not None else None for path, state in toolbox_signature()}
    from .data_provider import binding
    return {'version': 1, 'root': str(root), 'files': files, 'queries': queries, 'toolbox': toolbox,
            'provider': binding()}


def bind_observed(dependencies, observed):
    knowledge = getattr(observed, 'knowledge', None)
    if knowledge is not None:
        dependencies['knowledge'] = deepcopy(knowledge)
    for relative, state in list(observed.items()):
        parts = Path(relative).parts
        if (len(parts) == 5 and parts[:3] == ('.gnovel', 'workbench', 'candidates')
                and parts[-1] == 'index.json'):
            # Saving an unselected version appends to this index but does not
            # change the selected manuscript. The selected body still retains
            # its own exact file guard, and every committing caller captures the
            # index's physical CAS after validating this frozen selection.
            root = Path(dependencies['root'])
            if _state(root / relative) != state:
                raise StructureError('CANDIDATE_SELECTION_CHANGED', 'candidate index changed while freezing its selection')
            from .current_candidate import selected_candidate
            from .structure_store import load_structure
            selection = selected_candidate(root, load_structure(root), parts[3])['selection']
            if _state(root / relative) != state:
                raise StructureError('CANDIDATE_SELECTION_CHANGED', 'candidate index changed while freezing its selection')
            dependencies['files'][relative] = {'kind': 'candidate-selection', 'chapterId': parts[3],
                                               'selection': selection, 'state': state}
            continue
        dependencies['files'].setdefault(relative, {'kind': 'content', 'state': state})
    return dependencies


def capture_query(root, dependencies, directory, pattern):
    row = {'directory': directory, 'pattern': pattern, 'members': _members(root, directory, pattern)}
    if row not in dependencies['queries']:
        dependencies['queries'].append(row)


def validate_dependencies(root, dependencies, *, code='READ_BATCH_STALE'):
    root = Path(root).resolve()
    from .paths import stored_root_matches
    if (not isinstance(dependencies, dict) or dependencies.get('version') != 1
            or not stored_root_matches(root, dependencies.get('root'))):
        raise StructureError(code, 'preparation dependency snapshot is unavailable; prepare again')
    changed = []
    from .data_provider import binding
    if dependencies.get('provider') != binding():
        changed.append({'kind': 'runtime-policy', 'root': 'provider', 'change': 'modified'})
    knowledge = dependencies.get('knowledge')
    if knowledge is not None:
        from .data_provider import storage_root, confined_path, ProviderError
        store = storage_root()
        if (not isinstance(knowledge, dict) or knowledge.get('root') != str(store.resolve())
                or not isinstance(knowledge.get('files'), dict) or not isinstance(knowledge.get('queries'), dict)):
            raise StructureError(code, 'knowledge dependency root or snapshot changed; prepare again')
        try:
            for relative, state in knowledge['files'].items():
                if _state(confined_path(store, relative)) != state:
                    changed.append({'kind': 'reference-content', 'root': 'provider', 'path': relative, 'change': 'modified'})
            for relative, members in knowledge['queries'].items():
                directory = confined_path(store, relative)
                entries = list(directory.rglob('*')) if directory.is_dir() else []
                if any(path.is_symlink() for path in entries):
                    raise ProviderError('PROVIDER_PATH_UNSAFE', 'knowledge catalog includes a symbolic link')
                current = sorted(path.relative_to(store).as_posix() for path in entries if path.suffix == '.md')
                if current != members:
                    changed.append({'kind': 'reference-membership', 'root': 'provider',
                                    'directory': relative, 'change': 'membership'})
        except (ProviderError, OSError) as exc:
            raise StructureError(code, 'knowledge dependency path changed; prepare again') from exc
    for relative, row in dependencies['files'].items():
        if row['kind'] == 'candidate-selection':
            from .current_candidate import selected_candidate
            from .structure_store import load_structure
            try:
                current = selected_candidate(root, load_structure(root), row['chapterId'])['selection']
            except (OSError, ValueError, KeyError, TypeError):
                current = {'invalid': True}
            from .current_candidate import same_selection
            if not same_selection(root, current, row['selection']):
                changed.append({'kind': row['kind'], 'root': 'project', 'path': relative, 'change': 'selection'})
            continue
        current = _state(root / relative)
        if current != row['state']:
            changed.append({'kind': row['kind'], 'root': 'project', 'path': relative,
                            'change': 'added' if row['state'] is None else 'removed' if current is None else 'modified'})
    for query in dependencies['queries']:
        current = _members(root, query['directory'], query['pattern'])
        if current != query['members']:
            changed.append({'kind': 'query-membership', 'root': 'project',
                            'directory': query['directory'], 'pattern': query['pattern'],
                            'added': sorted(set(current) - set(query['members'])),
                            'removed': sorted(set(query['members']) - set(current))})
    toolbox = {path: list(state) if state is not None else None for path, state in toolbox_signature()}
    for path in sorted(toolbox.keys() | dependencies['toolbox'].keys()):
        if toolbox.get(path) != dependencies['toolbox'].get(path) or (path in toolbox) != (path in dependencies['toolbox']):
            changed.append({'kind': 'runtime-policy', 'root': 'toolbox', 'path': path,
                            'change': 'modified' if path in toolbox and path in dependencies['toolbox'] else 'membership'})
    if changed:
        raise StructureError(code, 'preparation dependencies changed; prepare again', {'changedItems': changed})
