"""External edit review and locally confirmed adoption in both public transports."""
from pathlib import Path

from .structure_store import StructureError, load_structure


def _load_plan(root: Path, operation_id: str, plan_hash: str, *, allow_committed: bool = False) -> dict:
    from .structure_commands import _operation_paths, _plan_hash
    from .jsonio import read_json
    plan_path, journal_path, _ = _operation_paths(root, operation_id)
    if allow_committed and journal_path.is_file():
        from .operation_receipts import require_committed_operation
        record = require_committed_operation(root, operation_id, plan_hash=plan_hash)
        if record['operation'] != 'accept_external_modification':
            raise StructureError('OPERATION_ROUTE_REQUIRED', 'receipt is not an external manuscript adoption')
        return {**record, 'historical': True}
    if not plan_path.is_file():
        raise StructureError('PLAN_NOT_FOUND', 'request a current external-edit preview')
    plan = read_json(plan_path)
    if plan.get('operation') != 'accept_external_modification' or plan.get('operationId') != operation_id:
        raise StructureError('OPERATION_ROUTE_REQUIRED', 'this operation does not adopt an external manuscript')
    if plan.get('planHash') != plan_hash or _plan_hash({k: v for k, v in plan.items() if k != 'planHash'}) != plan_hash:
        raise StructureError('PLAN_HASH_MISMATCH', 'external-edit plan changed after preview')
    return plan


def read_external_edit(root: Path, *, operationId: str, planHash: str, part: str = 'H2', offset: int = 0) -> dict:
    from .project import query_policy
    from .write_sets import digest, safe_project_path
    plan = _load_plan(root, operationId, planHash, allow_committed=True)
    if part not in ('H1', 'H2', 'diff') or type(offset) is not int or offset < 0:
        raise StructureError('ARGUMENTS_INVALID', 'choose H1, H2 or diff and a nonnegative character offset')
    entry = plan['externalReviewMaterials'].get(part)
    if entry is None:
        return {'ok': True, 'operationId': operationId, 'part': part, 'available': False, 'text': None,
                'historical': plan.get('historical', False)}
    payload = safe_project_path(root, entry['path']).read_bytes()
    if digest(payload) != entry['sha256']:
        raise StructureError('EXTERNAL_REVIEW_MATERIAL_INVALID', 'frozen external manuscript changed')
    text = payload.decode('utf-8')
    budget = query_policy()['maxBytes']['detail'] // 8
    chunk = text[offset:offset + budget]
    end = offset + len(chunk)
    return {'ok': True, 'operationId': operationId, 'planHash': planHash, 'part': part,
            'historical': plan.get('historical', False),
            'available': True, 'sha256': entry['sha256'], 'offset': offset, 'text': chunk,
            'nextOffset': end if end < len(text) else None, 'totalCharacters': len(text)}


def commit_external_edit(root: Path, *, operationId: str, planHash: str, approve: bool,
                         confirmOnDevice: bool) -> dict:
    from .locks import project_lock
    from .source_requests import stale_reasons
    from .structure_transaction import _load_outputs, _commit_locked, _enforce_transaction_boundary
    from .external_approval import issue_local_approval, discard_local_approval, validate_external_plan
    from .external_review_materials import validate_external_materials
    if approve is not True or confirmOnDevice is not True:
        raise StructureError('APPROVAL_CAPABILITY_REQUIRED', 'explicit approval and local device confirmation are required')
    root = root.expanduser().resolve()
    # No lock is held while waiting for the author. The entire frozen plan and
    # its files are checked again under the commit lock after confirmation.
    with project_lock(root):
        from .structure_commands import _operation_paths
        _, journal_path, _ = _operation_paths(root, operationId)
        if journal_path.is_file():
            from .operation_receipts import require_committed_operation
            from .source_reports import public_report
            record = require_committed_operation(root, operationId, plan_hash=planHash)
            if record['operation'] != 'accept_external_modification' or 'authorConfirmation' not in record:
                raise StructureError('OPERATION_ROUTE_REQUIRED', 'receipt is not a locally approved external adoption')
            return public_report({**record, 'ok': True, 'code': 'EXTERNAL_ALREADY_ADOPTED',
                'alreadyCommitted': True, 'historical': True}, root=root)
        plan = _load_plan(root, operationId, planHash)
        current = load_structure(root)
        reasons = stale_reasons(root, current, plan)
        if reasons:
            raise StructureError('SOURCE_REQUEST_STALE', 'external-edit preview is stale', {'staleReasons': reasons})
        validate_external_materials(root, plan, stage='before-local-confirmation')
        _enforce_transaction_boundary(plan, current, plan['proposedStructure'], root=root)
        validate_external_plan(root, current, plan, _load_outputs(root, plan))
    approval = issue_local_approval(root, plan)
    try:
        with project_lock(root):
            return _commit_locked(root, operation_id=operationId, plan_hash=planHash,
                                  approved=True, actor='author', privileged_approval=approval)
    finally:
        discard_local_approval(approval)


def external_edit_operations():
    from .operations import OperationSpec, _schema, STRING, INTEGER, BOOLEAN
    from .finalization import preview_accept_external_modification
    identity = {'operationId': STRING, 'planHash': STRING}
    return [
        OperationSpec('workflow.external-preview', 'preview_external_manuscript_edit',
            '冻结作者在外部编辑器修改的已受管正文、旧版及差异；不采用、不表示审查通过。',
            _schema({'chapterId': STRING, 'requestId': STRING}, ['chapterId']), 'plan-write',
            lambda root, a: preview_accept_external_modification(root, chapter_id=a['chapterId'], request_id=a.get('requestId')),
            ('workflow', 'external-preview'), category='author', discover=True,
            examples=({'chapterId': 'chapter-EXAMPLE', 'requestId': 'external-edit-1'},)),
        OperationSpec('workflow.external-read', 'read_external_manuscript_edit',
            '分页核对外改预览的准确旧版 H1、新版 H2 或 diff；缺失旧版明确返回 unavailable。',
            _schema({**identity, 'part': {'type': 'string', 'enum': ['H1', 'H2', 'diff']}, 'offset': INTEGER}, ['operationId', 'planHash']),
            'read', lambda root, a: read_external_edit(root, **a), ('workflow', 'external-read'), category='author'),
        OperationSpec('workflow.external-commit', 'accept_external_manuscript_edit',
            '明确批准冻结预览后在本机桌面确认（macOS 或 Linux zenity）；确认成功才采用该外改版本并标记待审。单次能力由本机确认签发，不能通过普通参数代替。',
            _schema({**identity, 'approve': {**BOOLEAN, 'const': True}, 'confirmOnDevice': {**BOOLEAN, 'const': True}},
                    ['operationId', 'planHash', 'approve', 'confirmOnDevice']),
            'protected-write', lambda root, a: commit_external_edit(root, **a), ('workflow', 'external-commit'), category='author'),
    ]
