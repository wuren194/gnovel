"""Small, explicit presentation projections over completed business responses.

Only named receipt and diagnostic operations opt in. Unknown operations and
unknown fields are left alone; a snapshot always preserves the same completed
response before any detail is omitted. This module makes no readiness or
approval decision and never executes a recovery action.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from .structure_store import StructureError


RESPONSE_VIEW_OPERATIONS = frozenset({
    'workflow.closeout-prepare', 'workflow.closeout',
    'diagnostics.chapter-check', 'project.workflow.chapter-status',
})
BUSINESS_VIEW_OPERATIONS = frozenset({
    'project.workflow.chapter-receipt', 'project.workflow.diagnostics-diff',
    'diagnostics.aggregate',
})
MODEL_OPERATIONS = RESPONSE_VIEW_OPERATIONS | BUSINESS_VIEW_OPERATIONS


def response_view(operation: str, arguments: dict) -> str | None:
    if operation not in MODEL_OPERATIONS:
        return None
    field = 'responseView' if operation in RESPONSE_VIEW_OPERATIONS else 'view'
    value = arguments.get(field, 'model')
    if value not in ('model', 'full'):
        raise StructureError('ARGUMENTS_INVALID', field + ' must be model or full', {'path': field})
    return value


def business_arguments(operation: str, arguments: dict) -> dict:
    """Strip only the declared transport option after normal schema validation."""
    if operation not in RESPONSE_VIEW_OPERATIONS:
        return arguments
    return {key: value for key, value in arguments.items() if key != 'responseView'}


def cli_operation(arguments: dict) -> str:
    binding = arguments.get('operation_binding')
    if isinstance(binding, str):
        return binding
    group, action = arguments.get('group'), arguments.get('action')
    parts = [group, action, arguments.get(str(action) + '_action')]
    return '.'.join(part for part in parts if isinstance(part, str))


def operation_for_tool(name: str) -> str | None:
    from .operations import operations
    return next((spec.id for spec in operations() if spec.name == name), None)


def presentation_target(operation: str, arguments: dict) -> tuple[str, dict]:
    if operation in {'capabilities.read', 'capabilities.export'} and isinstance(arguments.get('arguments'), dict):
        target = arguments.get('operation')
        if isinstance(target, str):
            return target, arguments['arguments']
    return operation, arguments


def _encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _same(left, right) -> bool:
    return _encoded(left) == _encoded(right)


def _conclusion_first(report: dict) -> dict:
    keys = ('ok', 'code', 'passed', 'readyToFinalize', 'readyForContinuation', 'summary', 'guidance',
            'blockers', 'findings', 'lineDetails', 'voiceCoverage', 'chapterContract', 'artifactIntegrity',
            'error', 'details', 'nextActions', 'nextAction',
            'runAction', 'resumeAction', 'recoveryActions', 'fullDetails')
    ordered = {key: report[key] for key in keys if key in report}
    ordered.update({key: value for key, value in report.items() if key not in ordered})
    return ordered


def _artifact_integrity(report: dict) -> None:
    rows = report.get('artifacts')
    if not isinstance(rows, list) or 'verifiedArtifactReferences' in report or 'artifactIntegrity' in report:
        return
    known = {'artifactType', 'required', 'approvalRequired', 'registered', 'approved', 'fresh',
             'envelopeValid', 'invalidated', 'artifactSha256', 'fileSha256', 'artifactPath',
             'bindings', 'summary', 'approvalCoveredBy', 'approvedArtifactSha256'}
    checked = ('registered', 'fresh', 'envelopeValid')
    blocked_types = {item.get('details', {}).get('artifactType') for item in report.get('blockers', [])
                     if isinstance(item, dict) and isinstance(item.get('details'), dict)
                     and isinstance(item['details'].get('artifactType'), str)}
    verified, remaining = [], []
    for row in rows:
        eligible = (isinstance(row, dict) and not set(row) - known
                    and isinstance(row.get('artifactType'), str) and isinstance(row.get('artifactSha256'), str)
                    and row['artifactType'] not in blocked_types
                    and all(row.get(key) is True for key in checked)
                    and all(type(row[key]) is bool for key in ('required', 'approvalRequired', 'approved') if key in row)
                    and (row.get('approved') is True or row.get('approvalRequired') is False)
                    and ('invalidated' not in row or row['invalidated'] is False)
                    and ('bindings' not in row or (isinstance(row['bindings'], dict) and not row['bindings'])))
        summary = row.get('summary') if isinstance(row, dict) else None
        if summary is not None:
            eligible = eligible and isinstance(summary, dict)
            if isinstance(summary, dict):
                eligible = eligible and not set(summary) - {
                    'verdict', 'decision', 'passed', 'finalProseSha256', 'draftSha256',
                    'findingsCount', 'changesCount', 'candidatesCount', 'characters', 'factAssessment'}
                eligible = eligible and ('passed' not in summary or summary['passed'] is True)
                eligible = eligible and ('decision' not in summary or summary['decision'] in ('adopt', 'modify'))
                eligible = eligible and ('verdict' not in summary or summary['verdict'] == 'POLISH_READY')
                eligible = eligible and ('factAssessment' not in summary or
                    _same(summary['factAssessment'], {'status': 'no_change'}))
                eligible = eligible and ('findingsCount' not in summary or
                    (type(summary['findingsCount']) is int and summary['findingsCount'] == 0))
        if not eligible:
            remaining.append(row)
            continue
        verified.append(row['artifactType'])
    if verified:
        report['artifacts'] = remaining
        report['artifactIntegrity'] = {'verifiedCount': len(verified), 'unverifiedCount': len(remaining),
                                       'artifactTypes': verified, 'checkedFields': list(checked),
                                       'approvalRequirementSatisfied': True, 'semanticReviewImplied': False}


def _duplicate_receipt_fields(report: dict, pointer: str = '') -> None:
    receipt = report.get('receipt')
    if isinstance(receipt, dict):
        _artifact_integrity(receipt)
        parent_blockers, nested = report.get('blockers'), receipt.get('blockers')
        if isinstance(parent_blockers, list) and isinstance(nested, list) and nested and 'blockersRef' not in receipt:
            encoded = [_encoded(item) for item in parent_blockers]
            indexes = []
            for item in nested:
                key = _encoded(item)
                if key not in encoded:
                    break
                indexes.append(encoded.index(key))
            else:
                receipt.pop('blockers')
                receipt['blockersRef'] = {'path': pointer + '/blockers', 'indexes': indexes}
        _duplicate_receipt_fields(receipt, pointer + '/receipt')
    continuity = report.get('continuity')
    if (isinstance(continuity, dict) and 'factInventory' in report and 'factInventory' in continuity
            and 'factInventoryRef' not in continuity
            and _same(report['factInventory'], continuity['factInventory'])):
        continuity.pop('factInventory')
        continuity['factInventoryRef'] = pointer + '/factInventory'


def _chapter_check(report: dict) -> None:
    rows = report.get('beatCounts')
    if isinstance(rows, list) and 'exactBudgetBeats' not in report:
        exact, remaining = [], []
        for row in rows:
            if (isinstance(row, dict) and set(row) == {'id', 'budget', 'hanCount', 'delta', 'status'}
                    and isinstance(row['id'], str) and row['status'] == 'measured'
                    and type(row['budget']) is int and type(row['hanCount']) is int
                    and row['budget'] == row['hanCount'] and type(row['delta']) is int and row['delta'] == 0):
                exact.append(row['id'])
            else:
                remaining.append(row)
        if exact:
            report['beatCounts'] = remaining
            report['exactBudgetBeats'] = {'count': len(exact), 'ids': exact}


def _duplicate_error(report: dict) -> None:
    details = report.get('details')
    blockers = details.get('blockers') if isinstance(details, dict) else None
    if not isinstance(blockers, list) or 'errorLocation' in report:
        return
    for index, blocker in enumerate(blockers):
        if (isinstance(blocker, dict) and blocker.get('code') == report.get('code')
                and isinstance(report.get('error'), str) and blocker.get('message') == report['error']):
            report.pop('error')
            report['errorLocation'] = f'/details/blockers/{index}'
            break


def _diagnostic_evidence(report: dict) -> None:
    evidence = report.get('evidence')
    if not isinstance(evidence, dict):
        return
    promoted = ('passed', 'findings', 'recommendation', 'chapterContract', 'writingNotes',
                'voiceCoverage', 'calibration', 'run')
    # Do not overwrite a future top-level meaning. The current producer has
    # these result fields only inside evidence.
    if any(key in report and key in evidence and not _same(report[key], evidence[key])
           for key in (*promoted, 'summary', 'lineDetails', 'planBinding', 'checksPresent')):
        return
    known = {*promoted, 'summary', 'lineDetails', 'draftSha256', 'candidateArtifactSha256',
             'planBinding', 'aggregateInputHash', 'inputHash', 'checksPresent'}
    remainder = {key: value for key, value in evidence.items() if key not in known}
    for key in promoted:
        if key in evidence:
            report[key] = evidence[key]
    summary = deepcopy(evidence.get('summary', {}))
    if isinstance(summary, dict):
        for key in ('perScript', 'perSeverity'):
            if isinstance(summary.get(key), dict):
                summary[key] = {name: value for name, value in summary[key].items()
                                if type(value) is not int or value != 0}
        from .mechanical_evidence import complete_evidence
        if complete_evidence(evidence):
            summary['checksRunCount'] = len(evidence['checksPresent'])
        elif 'checksPresent' in evidence:
            report['checksPresent'] = evidence['checksPresent']
        report['summary'] = summary
    elif 'summary' in evidence:
        report['summary'] = summary
        if 'checksPresent' in evidence:
            report['checksPresent'] = evidence['checksPresent']
    binding = evidence.get('planBinding')
    if 'planBinding' in evidence:
        binding_fields = {'status', 'current', 'sourcePlanHash', 'stageAArtifactSha256', 'draftSha256'}
        mechanical = (isinstance(binding, dict) and set(binding) == binding_fields
                      and binding['status'] == 'bound' and binding['current'] is True
                      and all(isinstance(binding[key], str) and re.fullmatch('[0-9a-f]{64}', binding[key])
                              for key in ('sourcePlanHash', 'stageAArtifactSha256', 'draftSha256')))
        report['planBinding'] = {'status': 'bound', 'current': True} if mechanical else binding
    details = evidence.get('lineDetails')
    if isinstance(details, dict) and isinstance(details.get('blocks'), dict):
        retained = {key: value for key, value in details['blocks'].items()
                    if not (isinstance(value, list) and not value)}
        known_detail_fields = {'mode', 'intensityLevel', 'total', 'blocks', '__returncode'}
        if (not retained and not set(details) - known_detail_fields
                and type(details.get('total')) is int and details['total'] == 0
                and type(details.get('__returncode')) is int and details['__returncode'] == 0
                and isinstance(summary, dict)):
            summary['lineDetailCount'] = 0
        else:
            report['lineDetails'] = {**details, 'blocks': retained}
    elif 'lineDetails' in evidence:
        report['lineDetails'] = details
    artifact = report.get('artifact')
    if (isinstance(evidence, dict) and isinstance(artifact, dict) and 'payload' in artifact
            and 'payloadRef' not in artifact and _same(artifact['payload'], evidence)):
        artifact.pop('payload')
        artifact['payloadRef'] = {'detailsAction': '/fullDetails', 'path': '/artifact/payload'}
    if remainder:
        report['evidence'] = remainder
    else:
        report.pop('evidence', None)
    if isinstance(report.get('chapterContract'), dict):
        _chapter_check(report['chapterContract'])


def project_response(root, operation: str, report, *, view: str | None = None):
    """Project a completed result, preserving a same-call pageable full snapshot.

    Snapshot failure returns the original response. It cannot turn a successful
    write into a reported failure or cause a write to be replayed for full output.
    """
    if operation not in MODEL_OPERATIONS or view == 'full' or not isinstance(report, dict):
        return report
    try:
        projected = deepcopy(report)
        if operation in {'project.workflow.chapter-receipt', 'project.workflow.chapter-status',
                         'workflow.closeout-prepare', 'workflow.closeout'}:
            _artifact_integrity(projected)
            _duplicate_receipt_fields(projected)
        if operation == 'diagnostics.chapter-check':
            _chapter_check(projected)
        if operation == 'diagnostics.aggregate':
            _diagnostic_evidence(projected)
        _duplicate_error(projected)
        if _same(projected, report):
            return _conclusion_first(projected)
        # Estimate the exact byte overhead before writing a snapshot that would
        # make this particular response longer. No arbitrary size threshold.
        full = {'operation': 'task.snapshot', 'arguments': {
            'projectRoot': str(Path(root).expanduser().resolve()),
            'reference': 'gn:authoring-report:' + '0' * 32}}
        if 'fullDetails' in projected or 'responseView' in projected:
            return _conclusion_first(report)
        projected.update(responseView='model', fullDetails=full)
        if len(_encoded(projected)) >= len(_encoded(report)):
            return _conclusion_first(report)
        from .transport_cache import put
        full['arguments']['reference'] = put(root, 'authoring-report', report)
        return _conclusion_first(projected)
    except Exception:
        return report
