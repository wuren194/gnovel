"""Explicit prose scan that returns locations only; matches are never rule conclusions."""
from __future__ import annotations

from pathlib import Path

from .source_evidence import _manuscript
from .source_model import SourceIndex, snapshot_hash
from .source_review import bounded_page, fingerprint, source_snapshot
from .structure_store import StructureError
from .project import query_policy


def _scan_all(root: Path, index: SourceIndex, terms: list[str], chapters: list[str],
              targetIds: list[str] | None) -> tuple[list[dict], list[str], list[str], dict]:
    """One full pass over the requested manuscripts; shared by paged scan and report."""
    rows, scanned, missing, state = [], [], [], {}
    for chapter_id in chapters:
        try:
            path, file_hash, text = _manuscript(root, index, chapter_id)
        except StructureError as exc:
            if exc.code != 'SOURCE_EVIDENCE_FILE_MISSING':
                raise
            missing.append(chapter_id); state[chapter_id] = None
            continue
        scanned.append(chapter_id); state[chapter_id] = file_hash
        for term in terms:
            start = 0
            while True:
                found = text.find(term, start)
                if found < 0:
                    break
                end = found + len(term)
                rows.append({'id': 'SC-' + fingerprint({'chapterId': chapter_id, 'fileHash': file_hash,
                                                        'start': found, 'end': end, 'term': term})[:24].upper(),
                             'chapterId': chapter_id, 'term': term, 'text': text[found:end],
                             'locator': {'path': path, 'fileHash': file_hash, 'start': found, 'end': end},
                             'targetIds': sorted(set(targetIds or [])), 'classification': 'candidate_mention',
                             'provesDependency': False, 'provesRuleViolation': False})
                start = end
    rows.sort(key=lambda row: (row['chapterId'], row['locator']['start'], row['term']))
    return rows, scanned, missing, state


def scan_prose_candidates(root: Path, *, terms: list[str], chapterIds: list[str] | None = None,
                          targetIds: list[str] | None = None, limit: int | None = None,
                          maxBytes: int | None = None, cursor: str | None = None) -> dict:
    source = source_snapshot(root)
    index = SourceIndex.build(source)
    if (not isinstance(terms, list) or not terms or any(not isinstance(term, str) or not term
                                                       for term in terms)):
        raise StructureError('SOURCE_SCAN_INVALID', 'terms must be a nonempty string list')
    terms = sorted(set(terms))
    excluded_quarantined = []
    if chapterIds is None:
        from .structure_store import is_quarantined, usable_chapters
        excluded_quarantined = sorted(row['chapterId'] for row in source['chapters'] if is_quarantined(row))
        chapters = sorted(row['chapterId'] for row in usable_chapters(source))
        explicit_scope = False
    else:
        if (not isinstance(chapterIds, list) or any(not isinstance(cid, str)
                                                   or index.owner_types.get(cid) != 'chapter'
                                                   for cid in chapterIds)):
            raise StructureError('SOURCE_SCAN_INVALID', 'chapterIds must identify registered chapters')
        chapters = sorted(set(chapterIds)); explicit_scope = True
    if targetIds is not None and (not isinstance(targetIds, list) or any(
            not isinstance(rid, str) or rid not in index.records for rid in targetIds)):
        raise StructureError('SOURCE_SCAN_INVALID', 'targetIds must identify registered source records')
    rows, scanned, missing, state = _scan_all(root, index, terms, chapters, targetIds)
    complete = explicit_scope and not missing
    sample_limit = query_policy()['maxLimit']
    return bounded_page(rows, snapshot={'sourceHash': snapshot_hash(source),
                                        'manuscriptStateHash': fingerprint(state)},
                        selection={'terms': terms, 'chapterIds': chapters if explicit_scope else None,
                                   'targetIds': sorted(set(targetIds or []))},
                        code='SOURCE_PROSE_CANDIDATES', limit=limit, maxBytes=maxBytes, cursor=cursor,
                        coverage={'requestedChapterCount': len(chapters), 'scannedChapterCount': len(scanned),
                                  'requestedChapterIdsHash': fingerprint(chapters),
                                  'missingChapterCount': len(missing), 'missingChapterIds': missing[:sample_limit],
                                  'missingChapterIdsTruncated': len(missing) > sample_limit,
                                  'completeForRequestedScope': complete,
                        'wholeProjectComplete': complete and not excluded_quarantined and len(chapters) == sum(
                                      kind == 'chapter' for kind in index.owner_types.values()),
                                  'excludedQuarantinedCount': len(excluded_quarantined),
                                  'excludedQuarantinedIds': excluded_quarantined[:sample_limit],
                                  'excludedQuarantinedIdsTruncated': len(excluded_quarantined) > sample_limit,
                                  'absenceMeaning': ('no literal match in every requested current manuscript'
                                                     if complete else 'scan incomplete; absence is not proof'),
                                  'semanticJudgmentMade': False})
