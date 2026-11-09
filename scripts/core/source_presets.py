"""Reusable source selections; presets store parameters, never story content."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .clock import utc_now
from .jsonio import read_json
from .locks import project_lock
from .source_model import SourceIndex, snapshot_hash
from .source_selection import normalize_selection
from .structure_store import StructureError, canonical_json, load_structure
from .write_sets import apply_write_set, digest, json_bytes, safe_project_path

PRESETS_PATH = '.gnovel/source-presets.json'


def _fingerprint(value) -> str:
    return digest(canonical_json(value).encode())


def _state(root: Path) -> dict:
    path = safe_project_path(root, PRESETS_PATH)
    state = read_json(path) if path.is_file() else {'schemaVersion': 1, 'presets': {}, 'requests': {}}
    if (not isinstance(state, dict) or state.get('schemaVersion') != 1
            or not isinstance(state.get('presets'), dict) or not isinstance(state.get('requests'), dict)):
        raise StructureError('SOURCE_PRESETS_INVALID', 'source preset registry is unreadable')
    for preset_id, row in state['presets'].items():
        if (not isinstance(row, dict) or row.get('presetId') != preset_id
                or not isinstance(row.get('name'), str) or not row['name'].strip()
                or not isinstance(row.get('selection'), dict) or not isinstance(row.get('layout'), dict)
                or set(row['layout']) != {'requireReviewed'} or type(row['layout']['requireReviewed']) is not bool
                or not isinstance(row.get('updatedAt'), str)):
            raise StructureError('SOURCE_PRESETS_INVALID', 'source preset entry is invalid')
    if any(not isinstance(key, str) or not isinstance(value, dict)
           or set(value) != {'presetId', 'requestHash'}
           or not isinstance(value['presetId'], str) or not isinstance(value['requestHash'], str)
           for key, value in state['requests'].items()):
        raise StructureError('SOURCE_PRESETS_INVALID', 'source preset request history is invalid')
    return state


def resolve_preset(root: Path, *, selection: dict | None = None,
                   presetId: str | None = None) -> tuple[dict | None, dict | None]:
    if selection is not None and presetId is not None:
        raise StructureError('SOURCE_SELECTION_INVALID', 'give selection or presetId, not both')
    if presetId is None:
        return selection, None
    if not isinstance(presetId, str) or not presetId.strip():
        raise StructureError('SOURCE_PRESET_NOT_FOUND', 'presetId must be a nonempty string')
    row = _state(root)['presets'].get(presetId)
    if row is None:
        raise StructureError('SOURCE_PRESET_NOT_FOUND', 'unknown source preset')
    return deepcopy(row['selection']), {'presetId': presetId, 'name': row['name'],
                                         'layout': deepcopy(row['layout']),
                                         'presetHash': _fingerprint(row)}


def save_preset(root: Path, *, requestId: str, name: str, selection: dict,
                presetId: str | None = None, requireReviewed: bool = False) -> dict:
    if (not isinstance(requestId, str) or not requestId.strip() or not isinstance(name, str)
            or not name.strip() or not isinstance(selection, dict) or type(requireReviewed) is not bool):
        raise StructureError('SOURCE_PRESET_INVALID', 'requestId, name, selection and layout are required')
    with project_lock(root):
        source = load_structure(root)
        normalized = normalize_selection(selection, SourceIndex.build(source))
        generated = 'SP-' + _fingerprint({'projectId': source['projectId'], 'requestId': requestId})[:20].upper()
        target = generated if presetId is None else presetId
        if not isinstance(target, str) or not target.startswith('SP-') or len(target) < 5:
            raise StructureError('SOURCE_PRESET_INVALID', 'presetId must use the SP- prefix')
        payload = {'name': name.strip(), 'selection': normalized,
                   'layout': {'requireReviewed': requireReviewed}}
        request_hash = _fingerprint({'presetId': target, **payload})
        state = _state(root)
        prior_request = state['requests'].get(requestId)
        if prior_request is not None:
            if prior_request != {'presetId': target, 'requestHash': request_hash}:
                raise StructureError('SOURCE_REQUEST_REUSED', 'preset requestId was reused with different content')
            return {'ok': True, 'code': 'SOURCE_PRESET_ALREADY_SAVED', 'presetId': target,
                    'alreadySaved': True, 'sourceHash': snapshot_hash(source)}
        same_name = [row['presetId'] for row in state['presets'].values()
                     if row['name'] == payload['name'] and row['presetId'] != target]
        if same_name:
            raise StructureError('SOURCE_PRESET_NAME_CONFLICT', 'another preset already uses this name',
                                 {'presetIds': same_name})
        row = {'presetId': target, **payload, 'updatedAt': utc_now()}
        state['presets'][target] = row
        state['requests'][requestId] = {'presetId': target, 'requestHash': request_hash}
        apply_write_set(root, {PRESETS_PATH: json_bytes(state)}, operation='source_preset_save')
        return {'ok': True, 'code': 'SOURCE_PRESET_SAVED', 'presetId': target,
                'selection': normalized, 'layout': row['layout'], 'sourceHash': snapshot_hash(source),
                'storesStoryContent': False}


def query_presets(root: Path, *, presetId: str | None = None) -> dict:
    source = load_structure(root)
    state = _state(root)
    if presetId is not None and presetId not in state['presets']:
        raise StructureError('SOURCE_PRESET_NOT_FOUND', 'unknown source preset')
    rows = [deepcopy(row) for key, row in sorted(state['presets'].items())
            if presetId is None or key == presetId]
    return {'ok': True, 'code': 'SOURCE_PRESETS', 'sourceHash': snapshot_hash(source),
            'items': rows, 'count': len(rows), 'storesStoryContent': False}
