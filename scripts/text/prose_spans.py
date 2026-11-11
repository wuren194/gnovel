"""Mask non-prose and balanced quotations without changing source offsets."""
from __future__ import annotations

import re


QUOTED_SPAN = re.compile(
    r'''“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"(?:[^"\\]|\\.)*"'''
    r"|(?<![A-Za-z0-9_])'(?:[^'\\]|\\.)*'(?![A-Za-z0-9_])",
    re.DOTALL,
)
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
CHAPTER_HEADING = re.compile(r"^第[零〇一二三四五六七八九十百千万两\d]+[章节卷](?:\s+[^。！？!?]+)?$")


def blank_span(text: str) -> str:
    return re.sub(r"[^\r\n]", " ", text)


def mask_prose(text: str, *, include_dialogue: bool = False) -> str:
    """Keep zero-based Python character offsets and original line endings.

    Comments are metadata, never instructions to waive nearby prose findings.
    Only balanced quotations are masked, including quotations spanning lines.
    """
    lines = text.splitlines(keepends=True)
    frontmatter_end = -1
    if lines and lines[0].strip().lstrip("\ufeff") == "---":
        frontmatter_end = next(
            (i for i, line in enumerate(lines[1:], 1) if line.strip() in {"---", "..."}), -1
        )
    masked = []
    fence = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        marker = FENCE.match(line)
        if index <= frontmatter_end:
            masked.append(blank_span(line))
            continue
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
            masked.append(blank_span(line))
            continue
        if marker:
            fence = marker[1]
            masked.append(blank_span(line))
            continue
        if (stripped.startswith(("#", ">", "|")) or CHAPTER_HEADING.fullmatch(stripped)
                or re.fullmatch(r"(?:[-*_][ \t]*){3,}", stripped)):
            masked.append(blank_span(line))
        else:
            masked.append(line)
    source = re.sub(r"<!--[\s\S]*?-->", lambda match: blank_span(match[0]), "".join(masked))
    if not include_dialogue:
        source = QUOTED_SPAN.sub(lambda match: blank_span(match[0]), source)
    return source
