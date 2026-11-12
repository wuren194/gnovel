"""Parse explicit chapter-plan tables and resolve registered entity labels."""
from __future__ import annotations

import re


OUTLINE_ORIGIN = "outline"
PARTICIPANT_FIELD = "参与实体"
_FENCE = re.compile(r"^(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SEPARATOR = re.compile(r":?-{3,}:?")
SECTION_KEYWORD = "本章定位"


def _table_rows(text: str, *, section: str | None = None):
    """产出两列表行：代码围栏内不算，给了 ``section`` 时只看标题含它的小节。

    ``section`` 用标题栈判定：小节在同级或更高级标题处结束，子小节仍算在内；
    标题行在围栏内不参与判定。
    """
    fence = None
    levels: list[tuple[int, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        marker = _FENCE.match(stripped)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if fence is None:
            heading = _HEADING.match(stripped)
            if heading:
                level = len(heading.group(1))
                while levels and levels[-1][0] >= level:
                    levels.pop()
                levels.append((level, heading.group(2).strip()))
                continue
        if fence or not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        if section is not None and not any(section in title for _, title in levels):
            continue
        cells = [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]
        if len(cells) == 2 and not all(_SEPARATOR.fullmatch(cell) for cell in cells):
            yield cells
