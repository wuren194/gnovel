"""Whole-manuscript scan report: one pass, grouped by chapter, budget-capped.

`candidate-scan` 的分页协议面向逐页消费；编排场景（给 AI 一份全稿命中概览）需要
一次拿全并按章节归组。本模块复用同一扫描内核，只组织呈现：

- 一次扫描（同一快照、同一稿件 hash），不做翻页循环；
- 按章节归组，组内命中带 locator，AI 只需要决定看哪几章哪几处；
- 总字节预算复用 query 注册表的 traverse 上限：超限即停并标 truncated——报告
  被截断就是不完整，不冒充全量；
- 词表仍由调用方给出：工具不选词、不做依赖或规则结论（与 candidate-scan 同边界）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .project import query_policy
from .source_candidate_scan import _scan_all
from .source_model import SourceIndex, snapshot_hash
from .source_review import fingerprint, source_snapshot
from .structure_store import StructureError

GROUPED_FIELDS = ('id', 'term', 'text', 'locator', 'classification')


def scan_report(root: Path, *, scope='prose', terms=None, termsPath=None, termsFormat='json',
                startLine=None, endLine=None, selection=None, chapterIds=None, targetIds=None,
                limit=None, maxBytes=None, cursor=None) -> dict:
    from .source_lexicon import scan_terms
    root = root.expanduser().resolve()
    if scope not in {'prose', 'source'}:
        raise StructureError('SOURCE_SCAN_INVALID', 'scope must be prose or source')
    words, term_source = scan_terms(root, terms=terms, termsPath=termsPath, termsFormat=termsFormat,
                                   startLine=startLine, endLine=endLine)
    if scope == 'source':
        if chapterIds is not None or targetIds is not None:
            raise StructureError('SOURCE_SCAN_INVALID', 'source scans use selection, not prose chapterIds or targetIds')
        from .source_search import search_source
        return search_source(root, terms=words, selection=selection, limit=limit, maxBytes=maxBytes,
                             cursor=cursor, _term_source=term_source, _code='SOURCE_FRAGMENT_SCAN_REPORT')
    if selection is not None or limit is not None or cursor is not None:
        raise StructureError('SOURCE_SCAN_INVALID', 'prose scan-report is grouped; use candidate-scan for pagination')
    result = scan_prose_report(root, terms=words, chapterIds=chapterIds, targetIds=targetIds, maxBytes=maxBytes)
    if term_source is not None:
        result['snapshot']['termSource'] = term_source
    return result


def scan_prose_report(root: Path, *, terms: list[str], chapterIds: list[str] | None = None,
                      targetIds: list[str] | None = None, maxBytes: int | None = None) -> dict:
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
    policy = query_policy()
    from .query import query_bounds
    _, budget = query_bounds(maxBytes=maxBytes, view='traverse')
    rows, scanned, missing, state = _scan_all(root, index, terms, chapters, targetIds)
    groups: list[dict] = []
    used = 0
    truncated = False
    for chapter_id in chapters:
        hits = [row for row in rows if row['chapterId'] == chapter_id]
        if not hits:
            continue
        group = {'chapterId': chapter_id, 'hitCount': len(hits),
                 'hits': [{key: row[key] for key in GROUPED_FIELDS} for row in hits]}
        size = len(json.dumps(group, ensure_ascii=False).encode())
        if not truncated and used + size <= budget:
            used += size
        else:
            # 预算一旦耗尽，后续组只保留计数；报告不完整就不冒充全量
            truncated = True
            group['hits'] = []
            group['omittedHits'] = len(hits)
        groups.append(group)
    return {'ok': True, 'code': 'SOURCE_PROSE_SCAN_REPORT',
            'snapshot': {'sourceHash': snapshot_hash(source), 'manuscriptStateHash': fingerprint(state)},
            'selection': {'terms': terms, 'chapterIds': chapters if explicit_scope else None,
                          'targetIds': sorted(set(targetIds or []))},
            'summary': {'totalHits': len(rows), 'chapterGroupCount': len(groups),
                        'chaptersWithHits': len(groups),
                        'budgetBytes': budget, 'usedBytes': used, 'truncated': truncated},
            'chapters': groups,
            'coverage': {'requestedChapterCount': len(chapters), 'scannedChapterCount': len(scanned),
                         'excludedQuarantinedCount': len(excluded_quarantined),
                         'excludedQuarantinedChapterIds': excluded_quarantined[:policy['maxLimit']],
                         'excludedQuarantinedIdsTruncated': len(excluded_quarantined) > policy['maxLimit'],
                         'missingChapterCount': len(missing), 'missingChapterIds': missing[:policy['maxLimit']],
                         'completeForRequestedScope': not missing,
                         'reportTruncated': truncated,
                         'absenceMeaning': ('no literal match in every requested current manuscript'
                                            if not missing and not truncated
                                            else 'report incomplete or scope unmet; absence is not proof'),
                         'semanticJudgmentMade': False}}
