"""Bounded public transaction reports with pageable persisted details."""
from __future__ import annotations

from pathlib import Path

from .consistency import require_no_pending
from .jsonio import read_json
from .project import query_policy
from .query import _size, query_bounds
from .source_review import bounded_page, fingerprint
from .structure_store import StructureError


def commit_action(report: dict, root: Path | None = None) -> dict:
    from .project import TOOLBOX_ROOT
    arguments = {'projectRoot': str(root.resolve()) if root else '<projectRoot>',
                 'operationId': report['operationId'], 'planHash': report['planHash'], 'approve': True}
    return {'operation': 'source.commit', 'tool': 'commit_source_change',
            'requiresExplicitApproval': True, 'arguments': arguments,
            'cli': ['python3', str(TOOLBOX_ROOT / 'scripts/novelctl.py'), 'project', 'source', 'commit',
                    '--project-root', arguments['projectRoot'], '--operation-id', arguments['operationId'],
                    '--plan-hash', arguments['planHash'], '--approve']}


def public_report(report: dict, *, root: Path | None = None) -> dict:
    result = {key: value for key, value in report.items() if key != 'proposedStructure'}
    if 'planHash' in result and result.get('requiresApproval'):
        result.setdefault('ok', True)
        result.setdefault('code', 'SOURCE_CHANGE_PREVIEW')
    parameters = report.get('parameters', {})
    result['identities'] = {key: parameters[key] for key in
        ('chapterId', 'entityId', 'arcId', 'volumeId', 'unitId', 'candidateId', 'decisionId') if key in parameters}
    if report.get('chapter'):
        result['identities']['chapterId'] = report['chapter']['chapterId']
    result['identityRoles'] = {'operationId': 'transaction; use only to commit/query/undo this preview',
                               'identities': 'story object stable IDs; never infer type from a display list'}
    if ('planHash' in result and result.get('requiresApproval') and not result.get('historical')
            and result.get('ok') is not False):
        from .prose_binding import PRIVILEGED_APPROVAL_OPERATIONS, PRIVILEGED_APPROVAL_TRANSITIONS
        privileged = (report.get('operation') in PRIVILEGED_APPROVAL_OPERATIONS or any(
            row.get('kind') in PRIVILEGED_APPROVAL_TRANSITIONS for row in report.get('bindingTransitions', [])))
        if privileged:
            result.pop('nextAction', None)
            result['execution'] = {'executable': False, 'code': 'PRIVILEGED_APPROVAL_REQUIRED',
                'reason': '预览已冻结；采用前须在本机 macOS 确认，普通 commit 不授予该能力。',
                'missingPrerequisites': ['independent_user_presence_capability',
                    *report.get('externalReviewMaterials', {}).get('missingPrerequisites', [])],
                'approvalPerformed': False}
            if root is not None and report.get('externalReviewMaterials', {}).get('protocol'):
                result['approvalAction'] = {'operation': 'workflow.external-commit',
                    'tool': 'accept_external_manuscript_edit', 'requiresLocalConfirmation': True,
                    'requiresExplicitApproval': True, 'arguments': {'projectRoot': str(root.resolve()),
                        'operationId': report['operationId'], 'planHash': report['planHash'],
                        'approve': True, 'confirmOnDevice': True}}
        else:
            result['nextAction'] = commit_action(report, root)
    if 'diff' in result:
        result['diffSummary'] = {'changeCount': len(result['diff']),
                                 'recordCount': len({row['recordId'] for row in result['diff']})}
        result['diffAction'] = {'operation': 'source.change-report', 'tool': 'query_source_change_report',
                                'arguments': {'operationId': report['operationId'], 'section': 'diff', 'view': 'markdown'}}
    if 'importOrganization' in report:
        result['reviewAction'] = {'operation': 'source.change-report', 'tool': 'query_source_change_report',
                                 'arguments': {'operationId': report['operationId'], 'section': 'rationale', 'view': 'text'}}
    if _size(result) <= query_policy()['maxBytes']['detail']:
        return result
    impact = report.get('impact', {})
    summary = {key: value for key, value in impact.items() if not isinstance(value, list)}
    summary['counts'] = {key: len(value) for key, value in impact.items() if isinstance(value, list)}
    summary['detailsIncluded'] = False
    keys = {'ok', 'code', 'operationId', 'operation', 'planHash', 'baseRevision', 'afterRevision',
            'historical', 'requestId', 'currentRevision', 'beforeRevision', 'revision', 'undoReceiptPath', 'undoAuthorityPath', 'requiresApproval', 'expiresAtEpoch', 'alreadyCommitted', 'operationState',
            'nextAction', 'approvalAction', 'execution', 'diffSummary', 'diffAction', 'identities', 'identityRoles',
            'reversible', 'recovery', 'authorConfirmation', 'replayed', 'reviewAction'}
    compact = {key: value for key, value in result.items() if key in keys}
    # Ownership observations are part of the reviewable operation, not optional
    # prose detail. Keep the tokens visible when large source diffs are paged.
    compact_parameters = {key: parameters[key] for key in (
        'chapterId', 'filePath', 'bindingGeneration', 'expectedContentDigest', 'observedContentDigest') if key in parameters}
    if compact_parameters:
        compact['parameters'] = compact_parameters
    for key in ('bindingTransitions', 'pathMoves'):
        if key in report:
            compact[key] = report[key]
    if 'externalReviewMaterials' in report:
        materials = report['externalReviewMaterials']
        compact['externalReviewMaterials'] = {key: materials[key] for key in (
            'protocol', 'availability', 'missingPrerequisites', 'metadataPath', 'H1', 'H2', 'diff',
            'semanticReview', 'adoptionExecutable') if key in materials}
    compact.update({'impact': summary, 'writeCount': len(report.get('writeSet', report.get('backupFiles', []))),
                    'details': {'operationId': report['operationId'], 'tool': 'query_source_change_report',
                                'sections': ['impact', 'writes', 'identities', 'rationale', 'maintenance', 'diff'],
                                'arguments': {'operationId': report['operationId'], 'view': 'text'}}, 'responseCompacted': True})
    if report.get('undoReceiptPath'):
        compact['details'].update({'undoRevision': report['revision'], 'sections': ['impact']})
        compact['details']['arguments']['undoRevision'] = report['revision']
    if _size(compact) > query_policy()['maxBytes']['detail']:
        raise StructureError('SOURCE_REPORT_BUDGET_INVALID', 'configured budget cannot fit a transaction receipt')
    if 'diff' in result:
        compact['diff'] = []
        for row in result['diff'][:query_policy()['defaultLimit']]:
            compact['diff'].append(row)
            if _size(compact) > query_policy()['maxBytes']['detail']:
                compact['diff'].pop()
                break
        compact['diffSummary']['includedChangeCount'] = len(compact['diff'])
        # Account for the summary's own bytes after adding its final count.
        while compact['diff'] and _size(compact) > query_policy()['maxBytes']['detail']:
            compact['diff'].pop()
            compact['diffSummary']['includedChangeCount'] = len(compact['diff'])
    if _size(compact) > query_policy()['maxBytes']['detail']:
        raise StructureError('SOURCE_REPORT_BUDGET_INVALID', 'configured budget cannot fit a transaction receipt')
    return compact


def query_change_report(root: Path, *, operationId: str, section: str = 'impact', undoRevision=None,
                        limit=None, maxBytes=None, cursor=None, view='records'):
    from .structure_commands import _operation_paths, _plan_hash
    require_no_pending(root)
    if not isinstance(view, str) or view not in {'records', 'text', 'markdown'} or view == 'markdown' and section != 'diff':
        raise StructureError('SOURCE_REPORT_VIEW_INVALID', 'view must be records, text, or markdown for the diff section')
    plan_path, journal_path, _ = _operation_paths(root, operationId)
    path = journal_path if journal_path.is_file() else plan_path
    if undoRevision is not None:
        from .write_sets import safe_project_path
        from .undo_receipt import receipt_relative_path
        if type(undoRevision) is not int or undoRevision < 1 or section != 'impact':
            raise StructureError('SOURCE_REPORT_SECTION_INVALID', 'undo reports require a positive revision and impact section')
        path = safe_project_path(root, receipt_relative_path(operationId, undoRevision))
    if not path.is_file():
        raise StructureError('PLAN_NOT_FOUND', 'unknown source operation')
    report = read_json(path)
    if report.get('operationId') != operationId:
        raise StructureError('OPERATION_ID_MISMATCH', 'stored operation identity differs')
    if path == plan_path and report.get('planHash') != _plan_hash({k: v for k, v in report.items() if k != 'planHash'}):
        raise StructureError('PLAN_HASH_MISMATCH', 'stored plan was changed')
    if section == 'diff':
        if 'diff' not in report:
            raise StructureError('SOURCE_REPORT_DIFF_UNAVAILABLE', 'this historical operation has no frozen readable diff')
        rows = report['diff']
    elif section == 'impact':
        rows = [{'category': key, **(value if isinstance(value, dict) else {'recordId': value})}
                for key, values in report.get('impact', {}).items() if isinstance(values, list) for value in values]
    elif section == 'writes':
        rows = report.get('writeSet') or [{'path': path, 'before': report['beforeFiles'][path],
                                         'after': report['afterFiles'][path]} for path in report.get('backupFiles', [])]
    elif section == 'identities':
        identities = report.get('identityMap', report.get('parameters', {}).get('identityMap', {}))
        rows = [{'handle': key, 'id': value} for key, value in sorted(identities.items())]
    elif section == 'maintenance':
        binding = report.get('maintenance')
        rows = ([{'source': binding['source'], 'checklistPath': binding['checklistPath'],
                  'checklistHash': binding['checklistHash'], **item} for item in binding['items']]
                if binding else [])
        if binding:
            rows.extend({'unmappedChangedId': rid} for rid in binding.get('unmappedChangedIds', []))
    elif section == 'rationale':
        parameters = report.get('parameters', {})
        proposals = report.get('proposals', parameters.get('proposals', parameters.get('contentProposals', [])))
        proposal = report.get('proposal') or parameters.get('contentProposal') or parameters.get('canonProposal') or parameters.get('relationProposal')
        if proposal:
            proposals = [proposal]
        reason = report.get('reason', parameters.get('reason'))
        rows = ([{'reason': reason}] if reason is not None else []) + list(proposals)
        if 'importOrganization' in report:
            organization = report['importOrganization']
            rows.extend({'kind': 'import_retrospective_review', 'chapterId': cid, 'review': review,
                         'acceptedHistoryRequested': cid in organization['accepted'], 'historicalStageAClaimed': False}
                        for cid, review in organization['reviews'].items())
            rows.extend({'kind': 'import_quote', **row} for row in organization['citations'])
    else:
        raise StructureError('SOURCE_REPORT_SECTION_INVALID', 'unknown report section')
    require_no_pending(root)
    snapshot = {'operationId': operationId, 'reportHash': fingerprint(report)}
    if 'planHash' in report:
        snapshot['planHash'] = report['planHash']
    selection = {'section': section, 'undoRevision': undoRevision}
    coverage = report.get('impact', {}).get('coverage', {})
    if view in {'text', 'markdown'}:
        from .structure_store import canonical_json
        from .text_pages import text_page
        policy = query_policy()
        count, budget = query_bounds(limit=limit, maxBytes=maxBytes)
        from .source_diff import markdown_change
        materials = [{'id': f'{section}-{index}', 'text': markdown_change(row) if view == 'markdown' else canonical_json(row)}
                     for index, row in enumerate(rows)]
        return text_page({'ok': True, 'code': 'SOURCE_CHANGE_REPORT', 'snapshot': snapshot,
                          'view': view, 'historical': True, 'coverage': coverage}, materials,
                         limit=count, budget=budget, cursor=cursor, key='items',
                         source=fingerprint({'snapshot': snapshot, 'policy': policy}),
                         binding=fingerprint({'selection': {**selection, 'view': view}, 'limit': count,
                                              'budget': budget, 'code': 'SOURCE_CHANGE_REPORT'}))
    return bounded_page(rows, snapshot=snapshot, selection=selection, code='SOURCE_CHANGE_REPORT', limit=limit,
                        maxBytes=maxBytes, cursor=cursor, historical=True,
                        coverage=coverage)
