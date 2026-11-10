"""Deterministic chapter split/merge proposals; original manuscripts stay reserved."""
from __future__ import annotations

from copy import deepcopy

from .prose_binding import seal_binding
from .structure_commands import _new_chapter
from .structure_store import StructureError, derive_display_codes, validate_structure
from .write_sets import digest


OPERATIONS = {'chapter_split', 'chapter_merge'}


def source_chapters(source: dict, chapter_ids: list[str]) -> list[dict]:
    if (not isinstance(chapter_ids, list) or not chapter_ids
            or any(not isinstance(cid, str) or not cid.strip() for cid in chapter_ids)
            or len(chapter_ids) != len(set(chapter_ids))):
        raise StructureError('CHAPTER_RESHAPE_INPUT_INVALID', 'select distinct chapter IDs in the intended reading order')
    indexed = {row['chapterId']: row for row in source['chapters']}
    chapters = []
    for chapter_id in chapter_ids:
        chapter = indexed.get(chapter_id)
        if chapter is None:
            raise StructureError('CHAPTER_NOT_FOUND', 'source chapter does not exist', {'chapterId': chapter_id})
        if chapter['status'] == 'archived':
            raise StructureError('CHAPTER_ALREADY_ARCHIVED', 'reshape requires active source chapters', {'chapterId': chapter_id})
        chapters.append(chapter)
    return chapters


def _unit(source: dict, chapter_id: str) -> str | None:
    return next((row['unitId'] for row in source['units'] if row.get('status') != 'archived'
                 and any(m['memberType'] == 'chapter' and m['memberId'] == chapter_id for m in row['members'])), None)


def target_assignment(source: dict, chapters: list[dict], target: dict | None) -> dict:
    target = {} if target is None else target
    if not isinstance(target, dict) or set(target) - {'placement', 'primaryArcId', 'secondaryArcIds', 'unitId'}:
        raise StructureError('CHAPTER_RESHAPE_TARGET_INVALID', 'target accepts placement, primaryArcId, secondaryArcIds and unitId')
    first = chapters[0]
    links = [row for row in source['chapterArcLinks'] if row['chapterId'] == first['chapterId']]
    assignment = {'placement': deepcopy(target.get('placement', first['placement'])),
                  'primaryArcId': target.get('primaryArcId', next(row['arcId'] for row in links if row['role'] == 'primary')),
                  'secondaryArcIds': deepcopy(target.get('secondaryArcIds', [row['arcId'] for row in links if row['role'] == 'secondary'])),
                  'unitId': target.get('unitId', _unit(source, first['chapterId']))}
    if not isinstance(assignment['primaryArcId'], str) or not assignment['primaryArcId'].strip():
        raise StructureError('CHAPTER_RESHAPE_TARGET_INVALID', 'primaryArcId must identify an existing arc')
    secondary = assignment['secondaryArcIds']
    if (not isinstance(secondary, list) or any(not isinstance(x, str) or not x.strip() for x in secondary)
            or len(secondary) != len(set(secondary))):
        raise StructureError('CHAPTER_RESHAPE_TARGET_INVALID', 'secondaryArcIds must be distinct arc IDs')
    uid = assignment['unitId']
    if uid is not None and (not isinstance(uid, str) or not uid.strip()):
        raise StructureError('CHAPTER_RESHAPE_TARGET_INVALID', 'unitId must be a unit ID or null')
    if uid is not None and not any(row['unitId'] == uid and row.get('status') != 'archived' for row in source['units']):
        raise StructureError('UNIT_NOT_FOUND', 'select an active target unit', {'unitId': uid})
    return assignment


def _split_offsets(text: str, arguments: dict) -> list[int]:
    cuts, anchors = arguments.get('cutOffsets'), arguments.get('cutBefore')
    if (cuts is None) == (anchors is None):
        raise StructureError('CHAPTER_SPLIT_BOUNDARY_REQUIRED', 'provide exactly one of cutBefore or cutOffsets')
    if anchors is not None:
        if not isinstance(anchors, list) or not anchors or any(not isinstance(value, str) or not value for value in anchors):
            raise StructureError('CHAPTER_SPLIT_ANCHOR_INVALID', 'cutBefore must contain nonempty exact source fragments')
        cuts = []
        for index, anchor in enumerate(anchors):
            start = text.find(anchor)
            if start < 0:
                raise StructureError('CHAPTER_SPLIT_ANCHOR_NOT_FOUND', 'cutBefore fragment does not occur in the original text',
                                     {'anchorIndex': index, 'instruction': '按原文重新选择完整片段；不进行近似匹配。'})
            if text.find(anchor, start + 1) >= 0:
                raise StructureError('CHAPTER_SPLIT_ANCHOR_AMBIGUOUS', 'cutBefore fragment has more than one exact match',
                                     {'anchorIndex': index, 'instruction': '扩大原文片段以唯一定位；不猜测其中一次。'})
            cuts.append(start)
        if cuts != sorted(set(cuts)) or cuts[0] <= 0:
            raise StructureError('CHAPTER_SPLIT_ANCHOR_ORDER_INVALID',
                                 'cutBefore fragments must identify distinct boundaries after the beginning, in source order',
                                 {'resolvedOffsets': cuts, 'offsetUnit': 'Unicode code points'})
    if (not isinstance(cuts, list) or not cuts or any(type(value) is not int for value in cuts)
            or cuts != sorted(set(cuts)) or cuts[0] <= 0 or cuts[-1] >= len(text)):
        raise StructureError('CHAPTER_SPLIT_OFFSETS_INVALID',
                             'cutOffsets must increase strictly inside the source Unicode character range',
                             {'characterCount': len(text), 'offsetUnit': 'Unicode code points'})
    return cuts


def content_parts(operation: str, chapters: list[dict], texts: list[str], arguments: dict) -> tuple[list[str], list[str], list[list[dict]]]:
    """Return titles, exact text and Unicode source ranges; never normalize newlines."""
    if operation == 'chapter_split':
        if len(chapters) != 1:
            raise StructureError('CHAPTER_RESHAPE_INPUT_INVALID', 'split requires exactly one source chapter')
        cuts = _split_offsets(texts[0], arguments)
        bounds = [0, *cuts, len(texts[0])]
        bodies = [texts[0][start:end] for start, end in zip(bounds, bounds[1:])]
        titles = arguments.get('titles')
        if titles is None:
            titles = [f"{chapters[0]['title']}（{index + 1}）" for index in range(len(bodies))]
        origins = [[{'chapterId': chapters[0]['chapterId'], 'start': start, 'end': end}]
                   for start, end in zip(bounds, bounds[1:])]
    elif operation == 'chapter_merge':
        if len(chapters) < 2:
            raise StructureError('CHAPTER_RESHAPE_INPUT_INVALID', 'merge requires at least two source chapters')
        separator = arguments.get('separator', '')
        if not isinstance(separator, str):
            raise StructureError('CHAPTER_RESHAPE_INPUT_INVALID', 'separator must be text')
        bodies = [separator.join(texts)]
        titles = [arguments.get('title')]
        origins = [[{'chapterId': chapter['chapterId'], 'start': 0, 'end': len(text)}
                    for chapter, text in zip(chapters, texts)]]
    else:
        raise StructureError('CHAPTER_RESHAPE_INPUT_INVALID', 'unknown reshape operation')
    if (not isinstance(titles, list) or len(titles) != len(bodies)
            or any(not isinstance(title, str) or not title.strip() for title in titles)):
        raise StructureError('CHAPTER_RESHAPE_TITLES_INVALID', 'provide one nonempty title for each resulting chapter')
    return [title.strip() for title in titles], bodies, origins


def _assign_units(proposed: dict, sources: set[str], created: list[str], target_unit: str | None) -> None:
    for unit in proposed['units']:
        if unit.get('status') == 'archived':
            continue
        members = sorted(unit['members'], key=lambda row: row['order'])
        position = next((i for i, row in enumerate(members)
                         if row['memberType'] == 'chapter' and row['memberId'] in sources), len(members))
        remaining = [row for row in members if not (row['memberType'] == 'chapter' and row['memberId'] in sources)]
        if unit['unitId'] == target_unit:
            position = sum(1 for row in members[:position]
                           if not (row['memberType'] == 'chapter' and row['memberId'] in sources))
            remaining[position:position] = [{'memberType': 'chapter', 'memberId': cid} for cid in created]
        if remaining != members:
            unit['members'] = [{**row, 'order': i + 1} for i, row in enumerate(remaining)]


def build_reshape(source: dict, *, operation: str, arguments: dict, chapters: list[dict], texts: list[str],
                  created_ids: list[str]) -> tuple[dict, list[dict], list[bytes], dict]:
    titles, bodies, origins = content_parts(operation, chapters, texts, arguments)
    if (len(created_ids) != len(bodies) or any(not isinstance(cid, str) or not cid for cid in created_ids)
            or len(set(created_ids)) != len(created_ids)
            or any(cid in {row['chapterId'] for row in source['chapters']} for cid in created_ids)):
        raise StructureError('CHAPTER_RESHAPE_PLAN_INVALID', 'new chapter identities must exactly match the resulting pieces')
    assignment = target_assignment(source, chapters, arguments.get('target'))
    proposed = deepcopy(source)
    payloads = [text.encode('utf-8') for text in bodies]
    for chapter_id, title, payload in zip(created_ids, titles, payloads):
        proposed, generated_id, _ = _new_chapter(proposed, title=title, placement=assignment['placement'],
            before_chapter_id=chapters[0]['chapterId'], primary_arc_id=assignment['primaryArcId'],
            secondary_arc_ids=assignment['secondaryArcIds'])
        chapter = next(row for row in proposed['chapters'] if row['chapterId'] == generated_id)
        chapter.update(chapterId=chapter_id, filePath=f'正文/{chapter_id}.md')
        chapter['proseBinding'] = seal_binding(source['projectId'], chapter_id, chapter['filePath'],
            state='managed', generation=source['revision'] + 1, expected_content_digest='sha256:' + digest(payload))
        for row in proposed['chapterArcLinks']:
            if row['chapterId'] == generated_id:
                row['chapterId'] = chapter_id
    source_ids = {row['chapterId'] for row in chapters}
    for chapter in proposed['chapters']:
        if chapter['chapterId'] in source_ids:
            chapter['status'] = 'archived'
    proposed['chapters'] = ([row for row in proposed['chapters'] if row['status'] != 'archived']
                            + [row for row in proposed['chapters'] if row['status'] == 'archived'])
    derive_display_codes(proposed)
    _assign_units(proposed, source_ids, created_ids, assignment['unitId'])
    proposed['revision'] = source['revision'] + 1
    validate_structure(proposed)
    by_source = {row['chapterId']: (row, text) for row, text in zip(chapters, texts)}
    lineage = []
    for cid, title, body, payload, spans in zip(created_ids, titles, bodies, payloads, origins):
        lineage.append({'chapterId': cid, 'title': title, 'filePath': f'正文/{cid}.md',
            'contentDigest': digest(payload), 'characterCount': len(body), 'byteCount': len(payload),
            'sources': [{**span, 'filePath': by_source[span['chapterId']][0]['filePath'],
                         'contentDigest': digest(by_source[span['chapterId']][1].encode('utf-8'))} for span in spans]})
    summary = {'sourceChapterIds': [row['chapterId'] for row in chapters], 'createdChapterCount': len(created_ids),
        'sourceCharacterCount': sum(len(text) for text in texts), 'resultCharacterCount': sum(len(body) for body in bodies),
        'addedSeparatorCharacters': sum(len(body) for body in bodies) - sum(len(text) for text in texts),
        'removedCharacters': 0, 'originalProsePreserved': True, 'sourceChaptersArchived': True,
        'offsetUnit': 'Unicode code points', 'target': assignment,
        'sourceAssignments': [{'chapterId': row['chapterId'], 'placement': row['placement'],
            'arcLinks': [link for link in source['chapterArcLinks'] if link['chapterId'] == row['chapterId']],
            'unitId': _unit(source, row['chapterId'])} for row in chapters],
        'followUp': ['核对新章标题与分章衔接', '为新章重新整理规划、时间与实体引用', '完成新章审校并取得对应批准'],
        'approvalsInherited': False, 'factsInferred': False}
    return proposed, lineage, payloads, summary
