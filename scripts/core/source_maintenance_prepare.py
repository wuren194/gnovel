"""Read-only preparation of a source maintenance task; no semantic extraction guesses."""
from __future__ import annotations

from pathlib import Path

from .project import TOOLBOX_ROOT, query_policy
from .structure_store import StructureError
from .source_maintenance_inputs import observe_input
from .source_model import snapshot_hash
from .source_review import source_snapshot
from .write_sets import validate_guards
from .source_maintenance_document import prepare_document


def prepare_maintenance(root: Path, *, sourcePath: str, limit=None, maxBytes=None, cursor=None) -> dict:
    root = root.expanduser().resolve()
    source = source_snapshot(root)
    payload, guard = observe_input(root, sourcePath)
    policy = query_policy()
    budget = policy['maxBytes']['detail'] if maxBytes is None else maxBytes
    count = policy['defaultLimit'] if limit is None else limit
    if (type(budget) is not int or not 1 <= budget <= policy['maxBytes']['detail']
            or type(count) is not int or not 1 <= count <= policy['maxLimit']):
        raise StructureError('QUERY_BUDGET_INVALID', 'invalid maintenance reading budget')
    reference, segments = prepare_document(root, payload, guard)
    writable = True
    origin = {'path': guard['path'], 'sha256': guard['sha256']}
    report = {'ok': True, 'code': 'SOURCE_MAINTENANCE_PREPARED', 'projectRoot': str(root),
            'toolboxRoot': str(TOOLBOX_ROOT), 'cliPath': str(TOOLBOX_ROOT / 'scripts/novelctl.py'),
            'snapshot': {'sourceHash': snapshot_hash(source), 'revision': source['revision'],
                         'schemaVersion': source['schemaVersion']},
            'writeReady': writable, 'blocking': [] if writable else ['SOURCE_SCHEMA_UPGRADE_REQUIRED'],
            'checklistDraft': {'source': origin, 'items': []},
            'documentRef': reference,
            'checklistAction': {'operation': 'source.maintenance-checklist', 'arguments': {'documentRef': reference},
                                'missingInputs': ['items', 'output'], 'effects': 'candidate-write'},
            'itemFields': {'itemId': 'unique within this source document', 'quote': 'exact requirement quote',
                           'recordIds': 'stable target IDs obtained by reading',
                           'operationIds': 'leave empty for automatically linked receipts; manual historical IDs optional'},
            'readList': [{'path': guard['path'], 'reason': 'read and enumerate requirements; no automatic interpretation'},
                         {'path': 'docs/SETTING-MAINTENANCE.md', 'root': 'toolbox', 'reason': 'maintenance procedure'}],
            'routes': {'locate': 'source search', 'read': 'source read-fragment',
                       'newOrPlanned': 'source change-preview', 'factText': 'source fact-patch-preview',
                       'factOrAttribute': ['source content-resolve', 'source content-proposal-preview'],
                       'mixed': 'source proposal-batch-preview', 'commit': 'source commit',
                       'resume': 'source request-status', 'renew': 'source request-renew',
                       'coverage': 'source maintenance-check', 'review': 'source reviews'},
            'requestFields': {'requestId': 'persist a stable ID with each batch before preview',
                              'maintenanceReference': {'checklistPath': '工作台/<saved-checklist>.json', 'itemIds': []}},
            'queryBudget': policy,
            'coverage': {'checklistEnumerated': False, 'semanticReviewComplete': False,
                         'segmentation': 'syntactic_original_ranges', 'proseScanned': False, 'approvalGranted': False}}
    from .artifacts import sha256_json
    from .text_pages import text_page
    result = text_page(report, segments, budget=budget, limit=count, cursor=cursor, key='segments',
                       source=sha256_json({'documentRef': reference, 'snapshot': report['snapshot'], 'policy': policy}),
                       binding=sha256_json({'sourcePath': guard['path'], 'limit': count, 'maxBytes': budget}))
    validate_guards(root, [guard], stage='maintenance-prepare')
    return result
