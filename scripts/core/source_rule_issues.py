"""Version-bound rule issues, semantic judgments, and explicit exceptions.

The ledger records App judgments. Current readiness is always recomputed from
the authored source and live evidence; a historical receipt never proves the
new version.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .jsonio import read_json
from .locks import project_lock
from .source_model import SourceIndex, snapshot_hash
from .project import source_model_policy
from .structure_store import StructureError
from .write_sets import apply_write_set, json_bytes, safe_project_path

ISSUES_PATH = '.govel/source-rule-issues.json'
STATUSES = set(source_model_policy()['ruleIssueStatuses'])


def issue_id(rule_id: str, chapter_id: str, action: str) -> str:
    from .source_review import fingerprint
    return 'RI-' + fingerprint({'ruleId': rule_id, 'chapterId': chapter_id, 'action': action}).upper()


def read_rule_issues(root: Path) -> dict:
    path = safe_project_path(root, ISSUES_PATH)
    state = read_json(path) if path.is_file() else {'items': {}, 'receipts': {}}
    if (not isinstance(state, dict) or set(state) != {'items', 'receipts'}
            or not isinstance(state['items'], dict) or not isinstance(state['receipts'], dict)):
        raise StructureError('SOURCE_RULE_ISSUES_INVALID', 'rule issue ledger is unreadable')
    required = {'issueId', 'ruleId', 'ruleVersion', 'chapterId', 'action', 'inputHash', 'sourceHash',
                'status', 'reason', 'affectedIds', 'sourceOperationId', 'evidence',
                'deferredUntilScopeIds', 'receiptId', 'actor'}
    def valid_issue(row, key=None):
        return (isinstance(row, dict) and (key is None or row.get('issueId') == key) and set(row) == required
                and all(isinstance(row.get(k), str) for k in
                        ('ruleId', 'ruleVersion', 'chapterId', 'action', 'inputHash', 'sourceHash',
                         'status', 'reason', 'receiptId', 'actor'))
                and row['status'] in STATUSES and isinstance(row['affectedIds'], list)
                and isinstance(row['evidence'], list) and isinstance(row['deferredUntilScopeIds'], list)
                and (row['sourceOperationId'] is None or isinstance(row['sourceOperationId'], str)))
    for key, row in state['items'].items():
        if not valid_issue(row, key):
            raise StructureError('SOURCE_RULE_ISSUES_INVALID', 'invalid current rule issue')
    for rid, receipt in state['receipts'].items():
        if (not isinstance(receipt, dict) or receipt.get('receiptId') != rid
                or not isinstance(receipt.get('requestHash'), str)
                or not valid_issue(receipt.get('issue'))):
            raise StructureError('SOURCE_RULE_ISSUES_INVALID', 'invalid rule issue receipt')
        from .source_review import fingerprint
        issue = receipt['issue']
        if receipt['requestHash'] != fingerprint({'actor': issue['actor'], 'issue': issue}):
            raise StructureError('SOURCE_RULE_ISSUES_INVALID', 'rule issue receipt fingerprint differs')
    return state


def _current_evidence(root: Path, source: dict, evidence: list) -> tuple[bool, list]:
    """Validate source quotes and references to already registered prose spans."""
    index = SourceIndex.build(source)
    prose = None
    normalized = []
    for item in evidence:
        if not isinstance(item, dict) or item.get('kind') not in {'source_quote', 'prose_evidence'}:
            raise StructureError('SOURCE_RULE_EVIDENCE_INVALID', 'evidence must be a source quote or prose evidence ID')
        if item['kind'] == 'source_quote':
            if set(item) != {'kind', 'recordId', 'start', 'end', 'text'}:
                raise StructureError('SOURCE_RULE_EVIDENCE_INVALID', 'source quote fields do not match the schema')
            rid, start, end = item['recordId'], item['start'], item['end']
            record = index.records.get(rid)
            text = record.get('text') if isinstance(record, dict) else None
            if (not isinstance(text, str) or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(text) or text[start:end] != item['text']):
                raise StructureError('SOURCE_RULE_EVIDENCE_STALE', 'source quote does not match current content')
        else:
            if set(item) != {'kind', 'evidenceId'} or not isinstance(item['evidenceId'], str):
                raise StructureError('SOURCE_RULE_EVIDENCE_INVALID', 'prose evidence requires one evidenceId')
            if prose is None:
                from .source_evidence import evidence_rows
                prose = {row['evidenceId']: row for row in evidence_rows(root, source)[0]}
            row = prose.get(item['evidenceId'])
            if row is None or row['status'] != 'registered':
                raise StructureError('SOURCE_RULE_EVIDENCE_STALE', 'prose evidence is absent or no longer current')
        normalized.append(deepcopy(item))
    return True, normalized


def current_issue(root: Path | None, source: dict, rule: dict, chapter_id: str, action: str,
                  input_hash: str) -> dict | None:
    if root is None:
        return None
    state = read_rule_issues(root)
    iid = issue_id(rule['entityId'], chapter_id, action)
    candidates = []
    if iid in state['items']:
        candidates.append(state['items'][iid])
    candidates.extend(receipt['issue'] for receipt in reversed(list(state['receipts'].values()))
                      if receipt['issue'].get('issueId') == iid)
    row = next((item for item in candidates
                if item['ruleVersion'] == rule['ruleSpec']['version'] and item['inputHash'] == input_hash),
               candidates[0] if candidates else None)
    if row is None:
        return None
    current = row['ruleVersion'] == rule['ruleSpec']['version'] and row['inputHash'] == input_hash
    # The full source hash is retained as historical identity. Current validity is
    # bound to registered rule inputs so unrelated edits do not revoke approval.
    if current:
        try:
            _current_evidence(root, source, row['evidence'])
        except StructureError:
            current = False
    return {**deepcopy(row), 'current': current,
            'effectiveStatus': row['status'] if current else 'obsolete'}


def submit_rule_issue(root: Path, *, requestId: str, ruleId: str, chapterId: str, action: str,
                      inputHash: str, status: str, reason: str, evidence: list,
                      affectedIds: list, sourceOperationId=None, deferredUntilScopeIds=None,
                      actor: str = 'app') -> dict:
    from .source_review import source_snapshot, fingerprint
    from .source_impact import ReviewInputs
    from .source_rules import RULE_ACTIONS, applicable_rules
    if (not isinstance(requestId, str) or not requestId.strip() or not isinstance(actor, str) or not actor.strip()
            or status not in STATUSES or action not in RULE_ACTIONS or not isinstance(reason, str) or not reason.strip()):
        raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'request, status, action, actor and reason are required')
    if (not isinstance(affectedIds, list) or not affectedIds or any(not isinstance(v, str) for v in affectedIds)
            or len(affectedIds) != len(set(affectedIds))):
        raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'affectedIds must be a nonempty unique ID list')
    deferred = [] if deferredUntilScopeIds is None else deferredUntilScopeIds
    if (not isinstance(deferred, list) or any(not isinstance(v, str) for v in deferred)
            or len(deferred) != len(set(deferred)) or (status == 'deferred') != bool(deferred)):
        raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'deferred status requires explicit unique future scope IDs')
    if sourceOperationId is not None and (not isinstance(sourceOperationId, str) or not sourceOperationId.strip()):
        raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'sourceOperationId must be absent or nonempty')
    with project_lock(root):
        source = source_snapshot(root)
        inputs = ReviewInputs(source); index = inputs.index
        if index.owner_types.get(chapterId) != 'chapter' or ruleId not in index.objects:
            raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'unknown chapter or rule')
        rule = index.objects[ruleId]
        if rule.get('entityType') != 'rule' or not rule.get('ruleSpec'):
            raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'receipt target must be a source rule')
        if rule['ruleSpec']['method'] == 'mechanical' and status != 'exception':
            raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'mechanical rules accept only an explicit exception receipt')
        if status == 'exception' and not rule['ruleSpec'].get('exceptionsAllowed', False):
            raise StructureError('SOURCE_RULE_EXCEPTION_FORBIDDEN', 'this rule does not declare an exception policy')
        from .source_plan import chapter_plan
        scope = set(chapter_plan(source, chapterId, _inputs=inputs)['recipe']['ruleScopeIds'])
        if rule not in applicable_rules(source, scope, action=action):
            raise StructureError('SOURCE_RULE_ISSUE_SCOPE_INVALID', 'rule does not apply to this chapter and action')
        current_hash = inputs.fingerprint(ruleId)
        if inputHash != current_hash:
            raise StructureError('SOURCE_RULE_ISSUE_STALE', 'rule or registered inputs changed')
        if any(rid not in index.records for rid in affectedIds) or any(rid not in index.objects for rid in deferred):
            raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'affected or deferred scope ID is unknown')
        if status == 'deferred' and scope.intersection(deferred):
            raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'deferred scope must be outside the current chapter scope')
        if sourceOperationId is not None:
            from .structure_commands import _operation_paths
            plan_path, journal_path, _ = _operation_paths(root, sourceOperationId)
            operation_path = journal_path if journal_path.is_file() else plan_path
            if not operation_path.is_file() or read_json(operation_path).get('operationId') != sourceOperationId:
                raise StructureError('SOURCE_RULE_ISSUE_INVALID', 'sourceOperationId does not identify a source operation')
        if status != 'pending' and not evidence:
            raise StructureError('SOURCE_RULE_EVIDENCE_REQUIRED', 'checked outcomes require current evidence')
        if not isinstance(evidence, list):
            raise StructureError('SOURCE_RULE_EVIDENCE_INVALID', 'evidence must be a list')
        _, checked_evidence = _current_evidence(root, source, evidence)
        state = deepcopy(read_rule_issues(root))
        iid = issue_id(ruleId, chapterId, action)
        receipt_id = 'RR-' + fingerprint({'projectId': source['projectId'], 'requestId': requestId}).upper()
        row = {'issueId': iid, 'ruleId': ruleId, 'ruleVersion': rule['ruleSpec']['version'],
               'chapterId': chapterId, 'action': action, 'inputHash': inputHash,
               'sourceHash': snapshot_hash(source), 'status': status, 'reason': reason,
               'affectedIds': sorted(affectedIds), 'sourceOperationId': sourceOperationId,
               'evidence': checked_evidence, 'deferredUntilScopeIds': sorted(deferred),
               'receiptId': receipt_id, 'actor': actor}
        request_hash = fingerprint({'actor': actor, 'issue': row})
        prior = state['receipts'].get(receipt_id)
        if prior is not None:
            if prior['requestHash'] != request_hash:
                raise StructureError('SOURCE_REQUEST_REUSED', 'rule issue requestId belongs to another payload')
            return {'ok': True, 'code': 'SOURCE_RULE_ISSUE_ALREADY_RECORDED', 'issueId': iid,
                    'receiptId': receipt_id, 'alreadyRecorded': True}
        state['items'][iid] = row
        state['receipts'][receipt_id] = {'receiptId': receipt_id, 'requestHash': request_hash,
                                         'sourceHash': snapshot_hash(source), 'issue': deepcopy(row)}
        apply_write_set(root, {ISSUES_PATH: json_bytes(state)}, operation='source_rule_issue')
        return {'ok': True, 'code': 'SOURCE_RULE_ISSUE_RECORDED', 'issueId': iid,
                'receiptId': receipt_id, 'status': status, 'proseWrite': False,
                'machineProvesSemanticCorrectness': False}


def query_rule_issues(root: Path, *, chapterId=None, ruleId=None, statuses=None,
                      limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_review import source_snapshot, bounded_page, fingerprint
    source = source_snapshot(root); index = SourceIndex.build(source)
    state = read_rule_issues(root)
    if chapterId is not None and index.owner_types.get(chapterId) != 'chapter':
        raise StructureError('SOURCE_RULE_ISSUE_FILTER_INVALID', 'unknown chapter')
    if ruleId is not None and (index.owner_types.get(ruleId) != 'rule' or not index.objects[ruleId].get('ruleSpec')):
        raise StructureError('SOURCE_RULE_ISSUE_FILTER_INVALID', 'unknown source rule')
    if statuses is not None and (not isinstance(statuses, list) or set(statuses) - (STATUSES | {'obsolete'})):
        raise StructureError('SOURCE_RULE_ISSUE_FILTER_INVALID', 'unknown issue status')
    checks = {}
    from .source_impact import ReviewInputs
    from .source_rules import RULE_ACTIONS, evaluate_rules
    from .source_plan import chapter_plan
    inputs = ReviewInputs(source)
    chapters = {chapterId} if chapterId is not None else {
        row['chapterId'] for row in state['items'].values()
        if index.owner_types.get(row['chapterId']) == 'chapter'
        and (ruleId is None or row['ruleId'] == ruleId)}
    for cid in sorted(chapters):
        plan = chapter_plan(source, cid, _inputs=inputs)
        for action in RULE_ACTIONS:
            for check in evaluate_rules(source, cid, action, root=root, _inputs=inputs, _plan=plan)['items']:
                if ruleId is None or check['ruleId'] == ruleId:
                    checks[issue_id(check['ruleId'], cid, action)] = (cid, action, check)
    rows, represented = [], set()
    for row in state['items'].values():
        if chapterId is not None and row['chapterId'] != chapterId or ruleId is not None and row['ruleId'] != ruleId:
            continue
        rule = index.objects.get(row['ruleId'])
        check = checks.get(row['issueId'])
        current = None if check is None else current_issue(
            root, source, rule, row['chapterId'], row['action'], check[2]['inputHash'])
        if current and current['current']:
            item = {**deepcopy(current), 'current': True, 'effectiveStatus': current['effectiveStatus']}
        elif check is not None and check[2]['method'] == 'semantic':
            _, action, check = check
            item = {'issueId': row['issueId'], 'ruleId': check['ruleId'],
                    'ruleVersion': index.objects[check['ruleId']]['ruleSpec']['version'],
                    'chapterId': row['chapterId'], 'action': action, 'inputHash': check['inputHash'],
                    'sourceHash': snapshot_hash(source), 'status': 'pending', 'effectiveStatus': 'pending',
                    'current': True, 'reason': 'previous receipt is obsolete; semantic review is required',
                    'affectedIds': [row['chapterId']], 'sourceOperationId': None, 'evidence': [],
                    'deferredUntilScopeIds': [], 'receiptId': None, 'actor': 'tool',
                    'previousReceiptId': row['receiptId'], 'previousStatus': row['status']}
        else:
            item = {**deepcopy(row), 'current': False, 'effectiveStatus': 'obsolete'}
        represented.add(row['issueId'])
        if statuses is None or item['effectiveStatus'] in statuses:
            rows.append(item)
    for iid, (cid, action, check) in checks.items():
        if iid in represented or check['method'] != 'semantic':
            continue
        item = {'issueId': iid, 'ruleId': check['ruleId'],
                'ruleVersion': index.objects[check['ruleId']]['ruleSpec']['version'],
                'chapterId': cid, 'action': action, 'inputHash': check['inputHash'],
                'sourceHash': snapshot_hash(source), 'status': 'pending', 'effectiveStatus': 'pending',
                'current': True, 'reason': 'semantic rule has no current App receipt',
                'affectedIds': [cid], 'sourceOperationId': None, 'evidence': [],
                'deferredUntilScopeIds': [], 'receiptId': None, 'actor': 'tool'}
        if statuses is None or 'pending' in statuses:
            rows.append(item)
    rows.sort(key=lambda r: r['issueId'])
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source), 'ledgerHash': fingerprint(state)},
                        selection={'chapterId': chapterId, 'ruleId': ruleId,
                                   'statuses': sorted(statuses) if statuses is not None else None},
                        code='SOURCE_RULE_ISSUES', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={'currentEvidenceRevalidated': True, 'historicalReceiptsPreserved': True})


def query_rule_issue_receipt(root: Path, *, receiptId: str, limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_review import source_snapshot, bounded_page, fingerprint
    source = source_snapshot(root)
    state = read_rule_issues(root)
    receipt = state['receipts'].get(receiptId) if isinstance(receiptId, str) else None
    if receipt is None:
        raise StructureError('SOURCE_RULE_RECEIPT_NOT_FOUND', 'unknown rule issue receipt')
    row = receipt['issue']; rule = SourceIndex.build(source).objects.get(row['ruleId'])
    current = False
    if isinstance(rule, dict) and rule.get('ruleSpec'):
        from .source_impact import ReviewInputs
        current = (rule['ruleSpec']['version'] == row['ruleVersion']
                   and ReviewInputs(source).fingerprint(row['ruleId']) == row['inputHash'])
        if current:
            try:
                _current_evidence(root, source, row['evidence'])
            except StructureError:
                current = False
    return bounded_page([deepcopy(row)], snapshot={'sourceHash': snapshot_hash(source),
                        'receiptHash': fingerprint(receipt)}, selection={'receiptId': receiptId},
                        code='SOURCE_RULE_ISSUE_RECEIPT', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        historical=True, inputsStillCurrent=current)
