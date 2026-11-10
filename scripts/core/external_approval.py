"""Local, one-use approval for adopting an externally edited managed manuscript.

The capability never appears in an MCP/CLI argument or response. A native
confirmation displays the frozen plan identity; the core consumes its token
under the project lock and rechecks the original file guards on publication.
This is user-presence separation, not a sandbox against same-user native code.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import platform
from pathlib import Path
import secrets
import subprocess
import threading
import time

from .jsonio import read_json
from .project import workflow_policy
from .structure_store import StructureError, structure_hash
from .write_sets import json_bytes, safe_project_path


_DIALOG = '''on run argv
    set promptText to item 1 of argv
    set answer to display dialog promptText with title "Govel：采用外部修改" buttons {"取消", "确认采用"} default button "取消" cancel button "取消" with icon caution
    return button returned of answer
end run'''


@dataclass(frozen=True)
class _Approval:
    nonce: str
    binding: dict
    expires: float


_issued: dict[str, _Approval] = {}
_lock = threading.Lock()


def _duration(key: str) -> int:
    value = workflow_policy()['approval'][key]
    if type(value) is not int or value <= 0:
        raise StructureError('APPROVAL_POLICY_INVALID', f'{key} must be a positive integer')
    return value


def _binding(plan: dict) -> dict:
    material = plan['externalReviewMaterials']
    return {'operationId': plan['operationId'], 'planHash': plan['planHash'],
            'projectRoot': material['projectRoot'], 'projectId': material['projectId'],
            'chapterId': material['chapterId'], 'bindingGeneration': material['bindingGeneration'],
            'expectedContentDigest': material['expectedContentDigest'],
            'observedContentDigest': material['observedContentDigest'],
            'observedObject': deepcopy(material['observedObject'])}


def _confirm_on_device(message: str) -> bool:
    if platform.system() != 'Darwin':
        raise StructureError('APPROVAL_CHANNEL_UNAVAILABLE', 'local author confirmation requires the supported macOS desktop')
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', _DIALOG, message],
            capture_output=True, text=True, timeout=_duration('externalConfirmationTimeoutSeconds'), check=False)
    except subprocess.TimeoutExpired as exc:
        raise StructureError('APPROVAL_EXPIRED', 'local confirmation timed out; no adoption was performed') from exc
    except OSError as exc:
        raise StructureError('APPROVAL_CHANNEL_UNAVAILABLE', 'the macOS confirmation could not be opened') from exc
    if result.returncode == 0:
        return result.stdout.strip() == '确认采用'
    if '-128' in result.stderr:
        return False
    raise StructureError('APPROVAL_CHANNEL_UNAVAILABLE', 'the macOS desktop could not display author confirmation')


def issue_local_approval(root: Path, plan: dict) -> _Approval:
    material = plan['externalReviewMaterials']
    current = plan['proposedStructure']
    chapter = next(row for row in current['chapters'] if row['chapterId'] == material['chapterId'])
    message = '\n'.join([
        f"作品：{current['bookName']}\n章节：{chapter['title']} ({chapter['chapterId']})",
        f"目录：{root}\n正文：{material['filePath']}",
        f"原版本：{material['expectedContentDigest']}\n采用版本：{material['observedContentDigest']}",
        '原版本正文：' + ('可核对' if material.get('H1') else '无法找回；本次无法提供完整前后对照'),
        f"冻结正文：{root / material['H2']['path']}",
        f"计划：{plan['operationId']}\n确认摘要：{plan['planHash']}",
        '确认后采用上述版本并重新标记本章待审。此次确认不代表文学审查、定稿或新增事实批准。',
    ])
    if not _confirm_on_device(message):
        raise StructureError('APPROVAL_CANCELLED', 'author cancelled external manuscript adoption')
    approval = _Approval(secrets.token_hex(32), _binding(plan),
                         time.monotonic() + _duration('externalCapabilityTtlSeconds'))
    with _lock:
        for nonce, previous in list(_issued.items()):
            if previous.expires < time.monotonic():
                _issued.pop(nonce, None)
        _issued[approval.nonce] = approval
    return approval


def consume_local_approval(plan: dict, approval: object, *, root: Path | None) -> dict:
    if type(approval) is not _Approval or root is None:
        raise StructureError('APPROVAL_CAPABILITY_REQUIRED', 'adoption requires a fresh local author confirmation')
    with _lock:
        issued = _issued.pop(approval.nonce, None)
    if (issued is not approval or time.monotonic() > approval.expires
            or str(root.resolve()) != approval.binding['projectRoot'] or _binding(plan) != approval.binding):
        raise StructureError('APPROVAL_CAPABILITY_INVALID', 'local approval is expired, consumed or bound to another plan')
    return {'channel': 'macos-native-confirmation', 'operationId': plan['operationId'],
            'planHash': plan['planHash'], 'chapterId': approval.binding['chapterId'],
            'bindingGeneration': approval.binding['bindingGeneration'],
            'observedContentDigest': approval.binding['observedContentDigest'], 'oneUse': True}


def discard_local_approval(approval: _Approval) -> None:
    with _lock:
        _issued.pop(approval.nonce, None)


def validate_external_plan(root: Path, current: dict, plan: dict, outputs: dict) -> None:
    """The confirmation is only for this chapter's binding and review invalidation."""
    from .prose_binding import apply_binding_transitions
    from .source_impact import review_outputs, source_impact
    from .finalization import accept_external_workflow
    params = plan.get('parameters', {})
    fields = {'chapterId', 'bindingGeneration', 'expectedContentDigest', 'observedContentDigest', 'filePath'}
    if (set(params) != fields or plan.get('pathMoves') or plan.get('manuscriptMaterialization')
            or len(plan.get('bindingTransitions', [])) != 1):
        raise StructureError('EXTERNAL_ADOPTION_SCOPE_INVALID', 'confirmation cannot cover other structure changes')
    transition = {'kind': 'accept_external', **params}
    if plan['bindingTransitions'] != [transition]:
        raise StructureError('EXTERNAL_ADOPTION_SCOPE_INVALID', 'confirmation and ownership transition differ')
    expected, _ = apply_binding_transitions(current, [transition])
    chapter = next(row for row in expected['chapters'] if row['chapterId'] == params['chapterId'])
    chapter['status'] = 'stale'
    proposed = plan['proposedStructure']
    if structure_hash(expected) != structure_hash(proposed) or proposed['revision'] != current['revision'] + 1:
        raise StructureError('EXTERNAL_ADOPTION_SCOPE_INVALID', 'external adoption may only change the selected chapter')
    impact = source_impact(current, proposed, root=root)
    extras = review_outputs(root, current, proposed, impact)
    workflow_path = safe_project_path(root, '.govel/workflow.json')
    extras['.govel/workflow.json'] = json_bytes(accept_external_workflow(read_json(workflow_path),
        params['chapterId'], updated_at=proposed['updatedAt']))
    if outputs != {'.govel/structure.json': json_bytes(proposed), **extras}:
        raise StructureError('EXTERNAL_ADOPTION_SCOPE_INVALID', 'external adoption cannot write prose or unrelated files')
