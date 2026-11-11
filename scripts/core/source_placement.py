"""Current structural placement: volume scope or project scope, independent of arc and reading order."""
from __future__ import annotations

from .project import source_model_policy

VOLUME_SCOPE = 'volume'
PROJECT_SCOPE = 'project'


def placement_scopes() -> list[str]:
    return list(source_model_policy()['placementScopes'])


def unit_member_types() -> list[str]:
    return list(source_model_policy()['unitMemberTypes'])


def placement_from_volume(volume_id: str) -> dict:
    """Construct a volume placement from its stable volume identity."""
    return {'scope': VOLUME_SCOPE, 'volumeId': volume_id}


def project_placement() -> dict:
    return {'scope': PROJECT_SCOPE}


def placement_volume_id(placement) -> str | None:
    """Volume a *valid* placement points at, or None for project scope."""
    if isinstance(placement, dict) and placement.get('scope') == VOLUME_SCOPE:
        return placement.get('volumeId')
    return None


def placement_error(placement) -> str | None:
    """Return an error code if ``placement`` is not one of the two legal shapes.

    Only ``{"scope": "volume", "volumeId": "volume-..."}`` and ``{"scope": "project"}``
    are accepted. A volume placement without ``volumeId`` and a project placement
    that still carries ``volumeId`` are both rejected: no third shape, and never a
    ``volumeId`` sitting beside the placement to become a second source of truth.
    """
    if not isinstance(placement, dict):
        return 'PLACEMENT_INVALID'
    scope = placement.get('scope')
    if scope not in source_model_policy()['placementScopes']:
        return 'PLACEMENT_SCOPE_INVALID'
    if scope == VOLUME_SCOPE:
        if set(placement) != {'scope', 'volumeId'} or not isinstance(placement.get('volumeId'), str) or not placement['volumeId'].strip():
            return 'PLACEMENT_INVALID'
    elif set(placement) != {'scope'}:
        return 'PLACEMENT_INVALID'
    return None


def chapter_placement(chapter: dict, schema_version: int) -> dict | None:
    """Read the current placement; no inference from historical chapter fields."""
    placement = chapter.get('placement')
    return placement if isinstance(placement, dict) else None


def chapter_volume_id(chapter: dict, schema_version: int) -> str | None:
    return placement_volume_id(chapter.get('placement'))
