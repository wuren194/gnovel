"""Read-only story-summary freshness shared by workflow surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .project import continuation_policy
from .structure_store import usable_chapters

_UNLOADED = object()


def summary_freshness(
    root: Path,
    structure: dict[str, Any],
    *,
    target_chapter_id: str | None = None,
    _summary: Any = _UNLOADED,
) -> dict[str, Any]:
    """Return summary freshness without generating or mutating an artifact."""
    threshold = int(continuation_policy()["staleSummaryThreshold"])
    chapters = usable_chapters(structure)
    target = target_chapter_id or (chapters[-1].get("chapterId") if chapters else None)
    ordinals = {item.get("chapterId"): item.get("displayOrdinal") for item in chapters}
    from .summary_contract import load_summary
    # Context already loaded and validated its summary. Reuse those exact bytes
    # so freshness and the injected material cannot describe different versions.
    summary = load_summary(root) if _summary is _UNLOADED else _summary
    as_of = summary.get("asOfChapterId") if summary else None
    gap = None
    if target in ordinals and as_of in ordinals:
        gap = int(ordinals[target]) - int(ordinals[as_of])
    stale = summary is None or gap is None or gap >= threshold
    changed = []
    if summary and summary.get('inputSnapshot'):
        from .summary_inputs import summary_inputs, summary_delta
        delta = summary_delta(summary['inputSnapshot'], summary_inputs(root, structure), summary.get('coveredChapterIds', []))
        changed = [cid for cid in delta['affectedChapterIds'] if cid in summary.get('coveredChapterIds', [])]
        stale = stale or bool(changed) or delta['fullRefresh']
    return {
        "present": summary is not None,
        "valid": summary is not None,
        "asOfChapterId": as_of,
        "targetChapterId": target,
        "gap": gap,
        "stale": stale,
        # Summary freshness is advisory under the continuation contract. Missing or
        # old material remains visible; it does not become a mandatory gate merely
        # because a caller names the stale flag "required".
        "refreshRecommended": stale,
        "refreshRequired": False,
        "enforcement": "advisory",
        "threshold": threshold,
        "changedCoveredChapterIds": changed,
        "configSource": "shared/registries/workflow.json.continuation",
    }
