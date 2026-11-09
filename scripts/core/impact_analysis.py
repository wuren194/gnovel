"""Impact projections for story structure mutations."""

from __future__ import annotations

from .structure_store import ordered_chapters


def insertion_impact(structure: dict, before_chapter_id: str | None) -> dict:
    chapters = ordered_chapters(structure)
    if before_chapter_id is None:
        index = len(chapters)
    else:
        matches = [i for i, item in enumerate(chapters) if item["chapterId"] == before_chapter_id]
        if not matches:
            raise ValueError(f"unknown chapter: {before_chapter_id}")
        index = matches[0]
    renumbered = [
        {
            "chapterId": chapter["chapterId"],
            "from": chapter["displayCode"],
            "to": f"CH{chapter['displayOrdinal'] + 1:03d}",
        }
        for chapter in chapters[index:]
    ]
    affected = {item["chapterId"] for item in renumbered}
    references = [
        ref for ref in structure.get("references", [])
        if ref.get("sourceId") in affected or ref.get("targetId") in affected
    ]
    return {
        "insertionIndex": index,
        "renumberedChapters": renumbered,
        "referenceCount": len(references),
        "staleCandidates": sorted(affected),
        "approvalInvalidations": sorted(affected),
    }


def move_impact(structure: dict, chapter_id: str, before_chapter_id: str | None) -> dict:
    chapters = ordered_chapters(structure)
    ids = [item["chapterId"] for item in chapters]
    if chapter_id not in ids:
        raise ValueError(f"unknown chapter: {chapter_id}")
    if before_chapter_id == chapter_id:
        raise ValueError("chapter cannot be moved before itself")
    target_ids = [item for item in ids if item != chapter_id]
    if before_chapter_id is None:
        destination = len(target_ids)
    elif before_chapter_id in target_ids:
        destination = target_ids.index(before_chapter_id)
    else:
        raise ValueError(f"unknown chapter: {before_chapter_id}")
    target_ids.insert(destination, chapter_id)
    by_id = {item["chapterId"]: item for item in chapters}
    renumbered = []
    for ordinal, item_id in enumerate(target_ids, 1):
        chapter = by_id[item_id]
        if chapter["displayOrdinal"] != ordinal:
            renumbered.append({
                "chapterId": item_id,
                "from": chapter["displayCode"],
                "to": f"CH{ordinal:03d}",
            })
    affected = {item["chapterId"] for item in renumbered}
    references = [
        ref for ref in structure.get("references", [])
        if ref.get("sourceId") in affected or ref.get("targetId") in affected
    ]
    return {
        "chapterId": chapter_id,
        "beforeChapterId": before_chapter_id,
        "orderedChapterIds": target_ids,
        "renumberedChapters": renumbered,
        "referenceCount": len(references),
        "staleCandidates": sorted(affected),
        "approvalInvalidations": sorted(affected),
    }


def archive_impact(structure: dict, chapter_id: str) -> dict:
    chapters = ordered_chapters(structure)
    matches = [i for i, item in enumerate(chapters) if item["chapterId"] == chapter_id]
    if not matches:
        raise ValueError(f"unknown chapter: {chapter_id}")
    index = matches[0]
    renumbered = [
        {
            "chapterId": chapter["chapterId"],
            "from": chapter["displayCode"],
            "to": f"CH{chapter['displayOrdinal'] - 1:03d}",
        }
        for chapter in chapters[index + 1:]
    ]
    affected = {item["chapterId"] for item in renumbered}
    references = [
        ref for ref in structure.get("references", [])
        if ref.get("sourceId") == chapter_id or ref.get("targetId") == chapter_id
    ]
    return {
        "chapterId": chapter_id,
        "displayCode": chapters[index]["displayCode"],
        "renumberedChapters": renumbered,
        "referenceCount": len(references),
        "staleCandidates": sorted(affected),
        "approvalInvalidations": sorted(affected | {chapter_id}),
    }


def merge_entity_impact(structure: dict, source_id: str, target_id: str) -> dict:
    redirected = [ref for ref in structure.get("references", []) if ref.get("targetId") == source_id]
    chapters = sorted({
        ref.get("sourceId") for ref in redirected
        if isinstance(ref.get("sourceId"), str) and ref.get("sourceId", "").startswith("CH-")
    })
    relations = [
        rel for rel in structure.get("relations", [])
        if rel.get("fromEntity") == source_id or rel.get("toEntity") == source_id
    ]
    return {
        "sourceEntityId": source_id,
        "targetEntityId": target_id,
        "redirectedReferenceCount": len(redirected),
        "redirectedRelationCount": len(relations),
        "staleCandidates": chapters,
        "approvalInvalidations": chapters,
    }


def entity_change_impact(structure: dict, entity_id: str, changes: dict) -> dict:
    direct = [ref for ref in structure.get("references", []) if ref.get("targetId") == entity_id]
    # 只把章 ID 当章节候选：结构校验已禁止非章 sourceId，这里再滤一道，保证即使真有
    # 历史遗留数据，staleCandidates 也不会混进实体/卷 ID（与 merge_entity_impact 一致）。
    chapters = sorted({
        ref.get("sourceId") for ref in direct
        if isinstance(ref.get("sourceId"), str) and ref.get("sourceId", "").startswith("CH-")
    })
    return {
        "entityId": entity_id,
        "changedFields": sorted(changes),
        "referenceCount": len(direct),
        "staleCandidates": chapters,
        "approvalInvalidations": chapters,
    }


def anchor_change_impact(structure: dict, entity_id: str, changes: dict) -> dict:
    """时间锚点改动的影响面：引用边 + 时间线挂载（写到一半改时间设定时的安全网）。

    时间线挂载（章节 `storyTimeStartId`/`storyTimeEndId`、实体 `timeStartId`/`timeEndId`）
    不走 `references`，只查引用边会漏掉全部挂载对象。这里把两侧并进 staleCandidates：
    章节时间跨度重排、时间线倒挂都能在预览阶段看见，不必等 `structure timeline` 事后发现。
    """
    base = entity_change_impact(structure, entity_id, changes)
    chapters = set(base["staleCandidates"])
    linked_entities: list[str] = []
    for chapter in structure.get("chapters") or []:
        if entity_id in (chapter.get("storyTimeStartId"), chapter.get("storyTimeEndId")):
            chapters.add(chapter.get("chapterId"))
    for entity in structure.get("entities") or []:
        if entity.get("entityId") == entity_id:
            continue
        if entity_id in (entity.get("timeStartId"), entity.get("timeEndId")):
            linked_entities.append(entity.get("entityId"))
    linked_entities.sort()
    time_linked = bool(chapters - set(base["staleCandidates"])) or bool(linked_entities)
    return {
        **base,
        "timeAnchor": True,
        "timeFieldsChanged": bool({"absoluteTime", "timeOrdinal"} & set(changes)),
        "timeLinked": time_linked,
        "linkedEntityIds": linked_entities,
        "staleCandidates": sorted(chapters),
        "approvalInvalidations": sorted(chapters),
        "note": ("时间锚点改动会重排其后的时间线：受影响的章节跨度与挂载实体列在 "
                 "staleCandidates / linkedEntityIds，提交后请运行 `structure timeline` 复查倒挂。"),
    }


def anchor_archive_impact(structure: dict, entity_id: str) -> dict:
    """归档时间锚点的影响面：仍挂载在该锚点上的章节与实体。

    归档此前走通用 entity 影响面，只查 references（章→实体边），而时间线挂载
    （章节 storyTimeStartId/EndTimeId、实体 timeStartId/timeEndId）不走 reference，
    一个都看不见（机制 36 审查的缺陷 8）。归档后锚点退出时间线，`story_timeline`
    对仍挂载它的对象报 ARCHIVED_ANCHOR_REFERENCED——这里让作者在归档前就看到
    需要迁移的对象。
    """
    base = entity_change_impact(structure, entity_id, {"status": "archived"})
    chapters = set(base["staleCandidates"])
    linked_entities: list[str] = []
    for chapter in structure.get("chapters") or []:
        if entity_id in (chapter.get("storyTimeStartId"), chapter.get("storyTimeEndId")):
            chapters.add(chapter.get("chapterId"))
    for entity in structure.get("entities") or []:
        if entity.get("entityId") == entity_id:
            continue
        if entity_id in (entity.get("timeStartId"), entity.get("timeEndId")):
            linked_entities.append(entity.get("entityId"))
    linked_entities.sort()
    return {
        **base,
        "timeAnchor": True,
        "linkedEntityIds": linked_entities,
        "staleCandidates": sorted(chapters),
        "approvalInvalidations": sorted(chapters),
        "note": ("归档后该锚点退出时间线；仍挂载它的章节与实体列在 staleCandidates / "
                 "linkedEntityIds，提交前请迁移挂载或确认这些对象不再需要时间定位。"),
    }
