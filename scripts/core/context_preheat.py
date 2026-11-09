"""Read-only metadata preheat for the next chapter, with an explicit reuse check."""
from __future__ import annotations

from pathlib import Path

from .source_plan import chapter_plan
from .source_review import fingerprint, source_snapshot
from .structure_store import StructureError, ordered_chapters
from .write_sets import safe_project_path
from .paths import ProjectPathError


def preheat_metadata(source: dict, after_chapter_id: str) -> tuple[dict, dict, dict, dict]:
    """Registered planning metadata for the chapter after ``after_chapter_id``.

    Shared by the preheat operation and the Context Builder so both derive one
    fingerprint from the same inputs. Reads only registered structure — never prose
    and never any future plot.
    """
    chapters = ordered_chapters(source)
    position = next((index for index, row in enumerate(chapters)
                     if row['chapterId'] == after_chapter_id), None)
    if position is None:
        raise StructureError('CHAPTER_NOT_FOUND', 'preheat requires an active chapter')
    if position + 1 >= len(chapters):
        raise StructureError('NEXT_CHAPTER_NOT_FOUND', 'no registered active next chapter')
    current, target = chapters[position:position + 2]
    target_id = target['chapterId']
    plan = chapter_plan(source, target_id)
    references = sorted({row['targetId'] for row in source.get('references', [])
                         if row['sourceId'] == target_id})
    arc_ids = sorted({row['arcId'] for row in source.get('chapterArcLinks', [])
                      if row['chapterId'] == target_id})
    metadata = {'afterChapterId': after_chapter_id, 'nextChapterId': target_id,
                'nextChapterTitle': target['title'], 'arcIds': arc_ids,
                'registeredReferenceIds': references,
                'sourcePlanHash': plan['sourcePlanHash'],
                'planReady': plan['ready']}
    return metadata, plan, current, target


def preheat_fingerprint(source: dict, after_chapter_id: str) -> str:
    """The reusable input fingerprint a preheat run after ``after_chapter_id`` produces.

    The Context Builder records this for the chapter it builds so a caller can verify a
    prior preheat still matches the fresh dependencies, without caching any future context.
    """
    return fingerprint(preheat_metadata(source, after_chapter_id)[0])


def preheat_next_chapter(root: Path, *, afterChapterId: str,
                         expectedInputHash: str | None = None) -> dict:
    source = source_snapshot(root)
    metadata, plan, current, target = preheat_metadata(source, afterChapterId)
    input_hash = fingerprint(metadata)
    try:
        prose = safe_project_path(root, current.get('filePath') or '')
        prose_present = prose.is_file() and not prose.is_symlink() and prose.stat().st_size > 0
    except (ProjectPathError, OSError, ValueError):
        prose_present = False
    stable = current.get('status') == 'stable' and prose_present
    matched = expectedInputHash is not None and expectedInputHash == input_hash
    return {'ok': True, 'code': 'CONTEXT_PREHEAT', 'metadata': metadata,
            'inputHash': input_hash, 'sourceHash': plan['snapshot']['sourceHash'],
            'afterChapterStable': current.get('status') == 'stable',
            'afterChapterProsePresent': prose_present, 'expectedInputHash': expectedInputHash,
            'stale': expectedInputHash is not None and not matched,
            'reusableMetadata': stable and matched,
            'coverage': {'registeredPlanningMetadataOnly': True,
                         'futurePlotGenerated': False, 'proseRead': False,
                         'contextPackCached': False,
                         'nextContextMustBeBuiltAfterStability': True}}
