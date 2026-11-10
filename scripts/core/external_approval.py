"""Local, one-use approval for adopting an externally edited managed manuscript.

The capability never appears in an MCP/CLI argument or response. A native
confirmation displays the frozen plan identity; the core consumes its token
under the project lock and rechecks the original file guards on publication.
This is user-presence separation, not a sandbox against same-user native code.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes
from dataclasses import dataclass
import os
import platform
from pathlib import Path
import plistlib
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
    channel: str


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


def _macos_session() -> dict:
    """Read the caller's graphical session without launching a dialog or shell."""
    graphics = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    foundation = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    graphics.CGSessionCopyCurrentDictionary.argtypes = []
    graphics.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    foundation.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p]
    foundation.CFPropertyListCreateData.restype = ctypes.c_void_p
    foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
    foundation.CFDataGetLength.restype = ctypes.c_long
    foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    foundation.CFDataGetBytePtr.restype = ctypes.c_void_p
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    foundation.CFRelease.restype = None
    session = graphics.CGSessionCopyCurrentDictionary()
    if not session:
        return {}
    try:
        # kCFPropertyListBinaryFormat_v1_0; a system API constant, not policy.
        data = foundation.CFPropertyListCreateData(None, session, 200, 0, None)
        if not data:
            return {}
        try:
            return plistlib.loads(ctypes.string_at(foundation.CFDataGetBytePtr(data),
                                                   foundation.CFDataGetLength(data)))
        finally:
            foundation.CFRelease(data)
    finally:
        foundation.CFRelease(session)


def _channel_unavailable(reason: str) -> StructureError:
    return StructureError('APPROVAL_CHANNEL_UNAVAILABLE', reason, {
        'retryable': True,
        'recommendedAction': '在运行 Govel 的电脑上登录并解锁同一用户的桌面，再重试外改采用；'
                             'Linux 桌面需安装 zenity。原计划若已过期或正文已变化，重新预览后再确认。',
    })


def _confirmation_command(message: str) -> tuple[str, list[str]]:
    system = platform.system()
    if system == 'Darwin':
        try:
            session = _macos_session()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise _channel_unavailable('could not determine the current macOS desktop session') from exc
        if (not isinstance(session, dict) or session.get('kCGSSessionOnConsoleKey') is not True
                or session.get('kCGSessionLoginDoneKey') is not True
                or session.get('kCGSSessionUserIDKey') != os.geteuid()
                or session.get('CGSSessionScreenIsLocked', False) is not False):
            raise _channel_unavailable('author confirmation requires this user\'s logged-in, unlocked macOS desktop')
        return 'macos-native-confirmation', ['/usr/bin/osascript', '-e', _DIALOG, message]
    if system == 'Linux':
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            raise _channel_unavailable('author confirmation requires a Linux graphical session')
        executable = Path('/usr/bin/zenity')
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise _channel_unavailable('Linux desktop confirmation requires /usr/bin/zenity')
        # Fixed system executable; plain text stays one argument, never markup/code.
        return 'linux-zenity-confirmation', [str(executable), '--question', '--default-cancel',
            '--no-markup', '--title=Govel：采用外部修改', '--ok-label=确认采用', '--cancel-label=取消',
            '--text=' + message]
    raise _channel_unavailable('local author confirmation requires a macOS or Linux desktop')


def _confirm_on_device(message: str) -> str | None:
    """Return the backend that received approval, or None for explicit cancellation."""
    channel, command = _confirmation_command(message)
    try:
        result = subprocess.run(command,
            capture_output=True, text=True, timeout=_duration('externalConfirmationTimeoutSeconds'), check=False)
    except subprocess.TimeoutExpired as exc:
        raise StructureError('APPROVAL_EXPIRED', 'local confirmation timed out; no adoption was performed', {
            'retryable': True, 'recommendedAction': '解锁桌面并重试外改采用；若原计划已过期，重新预览后再确认。',
        }) from exc
    except OSError as exc:
        raise _channel_unavailable('the desktop confirmation could not be opened') from exc
    if channel == 'linux-zenity-confirmation':
        if result.returncode == 0:
            return channel
        if result.returncode == 1 and not result.stderr.strip():
            return None
        raise _channel_unavailable('the Linux desktop could not display author confirmation')
    if result.returncode == 0:
        if result.stdout.strip() == '确认采用':
            return channel
        raise _channel_unavailable('the macOS confirmation returned an unexpected answer')
    if '-128' in result.stderr:
        return None
    raise _channel_unavailable('the macOS desktop could not display author confirmation')


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
    try:
        channel = _confirm_on_device(message)
    except StructureError as exc:
        if exc.code in {'APPROVAL_CHANNEL_UNAVAILABLE', 'APPROVAL_EXPIRED'}:
            exc.details.update(operationId=plan['operationId'], planHash=plan['planHash'])
        raise
    if not channel:
        raise StructureError('APPROVAL_CANCELLED', 'author cancelled external manuscript adoption')
    if channel not in {'macos-native-confirmation', 'linux-zenity-confirmation'}:
        raise _channel_unavailable('confirmation did not identify a supported desktop backend')
    approval = _Approval(secrets.token_hex(32), _binding(plan),
                         time.monotonic() + _duration('externalCapabilityTtlSeconds'), channel)
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
    return {'channel': approval.channel, 'operationId': plan['operationId'],
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
