"""Resolve selected records once; a reference carries bindings, never approval."""
from copy import deepcopy

from .artifacts import sha256_json
from .prepare_dependencies import capture_dependencies, observe_reads, bind_observed, validate_dependencies
from .project import query_policy
from .query import _cursor_decode, _page
from .source_content_proposals import PROTECTED, resolve_record
from .source_model import snapshot_hash
from .source_review import source_snapshot
from .structure_store import StructureError
from .transport_cache import get, put


def _saved_resolution(root, reference):
    value = get(root, reference, kind='content-resolution')
    if (not isinstance(value, dict) or set(value) != {'version', 'arguments', 'projectId', 'sourceHash', 'baseRevision', 'items', 'dependencies'}
            or type(value.get('version')) is not int or value['version'] != 1
            or any(not isinstance(value[key], str) for key in ('projectId', 'sourceHash'))
            or type(value['baseRevision']) is not int
            or not isinstance(value.get('arguments'), dict) or not isinstance(value.get('items'), list)
            or not isinstance(value.get('dependencies'), dict)
            or any(not isinstance(row, dict) or not isinstance(row.get('recordId'), str)
                   or not isinstance(row.get('status'), str)
                   or row.get('status') not in {'resolved', 'not_found'} for row in value['items'])):
        raise StructureError('CONTENT_RESOLUTION_INVALID', 'resolution cache is unreadable or uses another format')
    return value


def resolve_content_batch(root, *, kind, recordIds, chapterId=None, limit=None, maxBytes=None, cursor=None):
    root = root.expanduser().resolve()
    policy = query_policy()
    if (not isinstance(kind, str) or kind not in PROTECTED or not isinstance(recordIds, list)
            or not 1 <= len(recordIds) <= policy['maxLimit']
            or any(not isinstance(rid, str) or not rid for rid in recordIds)
            or len(set(recordIds)) != len(recordIds)):
        raise StructureError('CONTENT_TARGET_INVALID', 'select distinct bounded recordIds of one supported kind')
    count = policy['defaultLimit'] if limit is None else limit
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    if (type(count) is not int or not 1 <= count <= policy['maxLimit']
            or type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']):
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid content resolution budget')
    arguments = {'kind': kind, 'recordIds': recordIds, 'chapterId': chapterId}
    binding = sha256_json({**arguments, 'limit': count, 'maxBytes': budget, 'policy': policy})
    offset = 0
    if cursor:
        page = _cursor_decode(cursor)
        reference = page.get('source')
        saved = _saved_resolution(root, reference)
        if saved['arguments'] != arguments or page.get('binding') != binding:
            raise StructureError('CURSOR_QUERY_MISMATCH', 'resolution selection or budget changed')
        validate_dependencies(root, saved['dependencies'], code='CURSOR_STALE')
        offset = page['offset']
    else:
        dependencies = capture_dependencies(root, evidence={'kind': 'review'})
        with observe_reads(root) as observed:
            source = source_snapshot(root)
            source_hash = snapshot_hash(source)
            rows = [resolve_record(root, source, rid, kind, chapterId, 'model', source_hash) for rid in recordIds]
        bind_observed(dependencies, observed)
        validate_dependencies(root, dependencies, code='CONTENT_RESOLUTION_STALE')
        saved = {'version': 1, 'arguments': arguments, 'projectId': source['projectId'], 'sourceHash': source_hash,
                 'baseRevision': source['revision'], 'items': rows, 'dependencies': dependencies}
        reference = put(root, 'content-resolution', saved)
    compact = [{k: v for k, v in row.items() if k not in {'ok', 'code', 'sourceHash', 'kind', 'chapterId'}}
               for row in saved['items']]
    result = _page({'ok': True, 'code': 'CONTENT_TARGET_RESOLUTIONS', 'kind': kind, 'chapterId': chapterId,
                    'selectedRecordCount': len(recordIds),
                    'sourceHash': saved['sourceHash'], 'baseRevision': saved['baseRevision'],
                    'resolutionRef': reference, 'allResolved': all(row['status'] == 'resolved' for row in compact),
                    'approvalGranted': False}, compact, limit=count, budget=budget,
                   offset=offset, binding=binding, source=reference)
    validate_dependencies(root, saved['dependencies'], code='CONTENT_RESOLUTION_STALE')
    return result


def bind_planning_changes(root, source, changes, base_revision, reference):
    saved = _saved_resolution(root, reference)
    validate_dependencies(root, saved['dependencies'], code='CONTENT_RESOLUTION_STALE')
    if (saved.get('version') != 1 or saved['projectId'] != source['projectId']
            or saved['sourceHash'] != snapshot_hash(source) or saved['arguments']['kind'] != 'planning'):
        raise StructureError('CONTENT_RESOLUTION_STALE', 'use a current planning resolution for this project')
    if base_revision is not None and base_revision != saved['baseRevision']:
        raise StructureError('REVISION_CONFLICT', 'explicit revision differs from the selected resolution')
    by_id = {row['recordId']: row for row in saved['items']}
    if not isinstance(changes, list) or not changes:
        raise StructureError('SOURCE_CHANGES_INVALID', 'select planning patches from the bound resolution')
    expanded = deepcopy(changes)
    for index, row in enumerate(expanded):
        if (not isinstance(row, dict) or row.get('action') != 'patch' or row.get('collection') != 'fragments'
                or not isinstance(row.get('id'), str) or row['id'] not in by_id
                or by_id[row['id']]['status'] != 'resolved'):
            raise StructureError('CONTENT_RESOLUTION_SCOPE_INVALID', 'only resolved planning patches may use the reference',
                                 {'itemIndex': index})
        expected = by_id[row['id']]['recordHash']
        if 'expectedRecordHash' in row and row['expectedRecordHash'] != expected:
            raise StructureError('EXPECTED_OLD_VALUE_CONFLICT', 'explicit record hash differs from its resolution',
                                 {'itemIndex': index})
        row['expectedRecordHash'] = expected
    return expanded, saved['baseRevision']
