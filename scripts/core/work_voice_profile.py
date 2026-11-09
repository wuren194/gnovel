"""Project-owned narrative voice guidance, separate from character dialogue voices."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

from .consistency import require_no_pending
from .source_model import require_current_source
from .structure_commands import _base_plan, _write_plan
from .structure_store import StructureError, canonical_json, load_structure, validate_structure

AXES = ('narrativeDistance', 'sentenceVariation', 'dialogueExposition',
        'emotionLabeling', 'somaticExpression', 'sceneTransition')


def _content(profile: dict) -> dict:
    return {key: profile[key] for key in ('axes', 'examples', 'applicability')}


def _hash(profile: dict) -> str:
    return hashlib.sha256(canonical_json(_content(profile)).encode('utf-8')).hexdigest()


def validate_voice_profile(source: dict) -> list[dict]:
    profile = source.get('workVoiceProfile')
    if profile is None:
        return []
    if not True or not isinstance(profile, dict):
        return [{'code': 'VOICE_PROFILE_INVALID'}]
    required = {'axes', 'examples', 'applicability', 'status'}
    if set(profile) - (required | {'approval'}) or required - set(profile):
        return [{'code': 'VOICE_PROFILE_FIELDS_INVALID'}]
    axes = profile['axes']
    if (not isinstance(axes, dict) or set(axes) != set(AXES)
            or any(not isinstance(axes[key], str) for key in AXES)
            or not isinstance(profile['examples'], list)
            or any(not isinstance(item, str) or not item.strip() for item in profile['examples'])
            or not isinstance(profile['applicability'], str)
            or profile['status'] not in {'draft', 'active'}):
        return [{'code': 'VOICE_PROFILE_CONTENT_INVALID'}]
    approval = profile.get('approval')
    if profile['status'] == 'active':
        if (not isinstance(approval, dict) or approval.get('profileHash') != _hash(profile)
                or not isinstance(approval.get('actorId'), str) or not approval['actorId'].strip()
                or not isinstance(approval.get('evidence'), str) or not approval['evidence'].strip()
                or approval.get('verifiedHuman') is not False):
            return [{'code': 'VOICE_PROFILE_APPROVAL_INVALID'}]
    elif approval is not None:
        return [{'code': 'VOICE_PROFILE_DRAFT_APPROVAL_INVALID'}]
    return []


def query_voice_profile(root: Path) -> dict:
    require_no_pending(root)
    source = load_structure(root)
    profile = source.get('workVoiceProfile')
    return {'ok': True, 'code': 'WORK_VOICE_PROFILE', 'profile': profile,
            'profileHash': _hash(profile) if profile else None,
            'epistemicRole': 'approved_plan' if profile and profile['status'] == 'active' else 'unapproved_candidate'}


def preview_voice_profile(root: Path, *, action: str, expectedRevision: int, reason: str,
                          axes: dict | None = None, examples: list[str] | None = None,
                          applicability: str | None = None, approvalActor: str | None = None,
                          approvalEvidence: str | None = None) -> dict:
    root = root.resolve()
    require_no_pending(root)
    source = load_structure(root)
    require_current_source(source)
    if type(expectedRevision) is not int or source['revision'] != expectedRevision:
        raise StructureError('REVISION_CONFLICT', 'voice profile preview requires current revision')
    if not isinstance(reason, str) or not reason.strip():
        raise StructureError('VOICE_PROFILE_INVALID', 'reason required')
    proposed = deepcopy(source)
    if action == 'set':
        if axes is None or examples is None or applicability is None or approvalActor is not None or approvalEvidence is not None:
            raise StructureError('VOICE_PROFILE_INVALID', 'set requires profile content only')
        proposed['workVoiceProfile'] = {'axes': axes, 'examples': examples,
                                        'applicability': applicability, 'status': 'draft'}
    elif action == 'approve':
        profile = proposed.get('workVoiceProfile')
        if (not isinstance(profile, dict) or profile.get('status') != 'draft' or not approvalActor or not approvalEvidence
                or any(value is not None for value in (axes, examples, applicability))):
            raise StructureError('VOICE_PROFILE_APPROVAL_INVALID', 'current draft and explicit approval required')
        profile['status'] = 'active'
        profile['approval'] = {'actorId': approvalActor.strip(), 'evidence': approvalEvidence.strip(),
                               'profileHash': _hash(profile), 'verifiedHuman': False}
    else:
        raise StructureError('VOICE_PROFILE_ACTION_INVALID', 'unknown action')
    validate_structure(proposed)
    plan = _base_plan(source, operation='work_voice_profile_' + action, actor='app',
                      parameters={'action': action, 'reason': reason}, impact={}, proposed=proposed)
    return _write_plan(root, plan)
