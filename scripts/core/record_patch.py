"""Hash-bound field patches expanded before existing semantic proposal gates."""
from copy import deepcopy
from .source_review import fingerprint
from .structure_store import StructureError

MISSING = object()


def _parent(record, path):
    parent = record
    for key in path[:-1]:
        if isinstance(parent, dict) and isinstance(key, str) and key in parent:
            parent = parent[key]
        elif isinstance(parent, list) and type(key) is int and 0 <= key < len(parent):
            parent = parent[key]
        else:
            raise StructureError('RECORD_PATCH_INVALID', 'patch parent does not exist', {'path': path})
    key = path[-1]
    if not (isinstance(parent, dict) and isinstance(key, str) or
            isinstance(parent, list) and type(key) is int and 0 <= key < len(parent)):
        raise StructureError('RECORD_PATCH_INVALID', 'patch target does not exist', {'path': path})
    return parent, key


def apply_record_patch(current, patch, expected_hash):
    if not isinstance(expected_hash, str) or fingerprint(current) != expected_hash:
        raise StructureError('EXPECTED_OLD_VALUE_CONFLICT', 'record hash differs from selected snapshot')
    if not isinstance(patch, list) or not patch:
        raise StructureError('RECORD_PATCH_INVALID', 'patch must be a nonempty list')
    result = deepcopy(current)
    used, text_spans = [], {}
    for index, item in enumerate(patch):
        if not isinstance(item, dict) or not isinstance(item.get('op'), str) or item.get('op') not in {'set', 'remove', 'text'}:
            raise StructureError('RECORD_PATCH_INVALID', 'supported patch operations: set, remove, text')
        op = item['op']
        keys = {'op', 'path'} | ({'value'} if op == 'set' else {'original', 'replacement'} if op == 'text' else set())
        path = item.get('path')
        if set(item) != keys or not isinstance(path, list) or not path or any(not (isinstance(k, str) and k or type(k) is int and k >= 0) for k in path):
            raise StructureError('RECORD_PATCH_INVALID', 'path must contain object keys or nonnegative array indexes')
        for old_path, old_op, old_index in used:
            if (path[:len(old_path)] == old_path or old_path[:len(path)] == path) and not (
                    path == old_path and op == old_op == 'text'):
                raise StructureError('RECORD_PATCH_OVERLAP', 'patch paths overlap',
                                     {'path': path, 'patchIndex': index, 'conflictingPatchIndex': old_index})
        used.append((path, op, index))
        parent, key = _parent(result, path)
        if isinstance(parent, list) and op == 'remove':
            raise StructureError('RECORD_PATCH_INVALID', 'remove an array field as a whole to avoid index drift')
        if op == 'set':
            parent[key] = deepcopy(item['value'])
        elif op == 'remove':
            if key not in parent:
                raise StructureError('RECORD_PATCH_INVALID', 'remove target does not exist')
            del parent[key]
        else:
            original_parent, original_key = _parent(current, path)
            value = original_parent.get(original_key) if isinstance(original_parent, dict) else original_parent[original_key]
            original, replacement = item['original'], item['replacement']
            if (not isinstance(value, str) or not isinstance(original, str) or not original
                    or not isinstance(replacement, str) or value.find(original) < 0
                    or value.find(original, value.find(original) + 1) >= 0):
                raise StructureError('RECORD_PATCH_ANCHOR_INVALID', 'text anchor must occur exactly once in the original record',
                                     {'path': path, 'patchIndex': index, 'original': original})
            start = value.index(original)
            spans = text_spans.setdefault(tuple(path), [])
            end = start + len(original)
            for left, right, _, other_index in spans:
                if start < right and left < end:
                    raise StructureError('RECORD_PATCH_OVERLAP', 'text ranges overlap in the original record',
                                         {'path': path, 'patchIndex': index, 'conflictingPatchIndex': other_index,
                                          'range': {'start': start, 'end': end}})
            spans.append((start, end, replacement, index))
    for path, spans in text_spans.items():
        parent, key = _parent(result, path)
        value = parent[key]
        for start, end, replacement, _ in sorted(spans, reverse=True):
            value = value[:start] + replacement + value[end:]
        parent[key] = value
    return deepcopy(current), result


def expand_patch(current, old, new, patch, expected_hash):
    if patch is not None:
        if old is not MISSING or new is not MISSING:
            raise StructureError('RECORD_PATCH_INVALID', 'choose full records or patch, not both')
        return apply_record_patch(current, patch, expected_hash)
    if expected_hash is not None or old is MISSING or new is MISSING:
        raise StructureError('RECORD_PATCH_INVALID', 'full records or patch with expectedRecordHash required')
    return old, new
