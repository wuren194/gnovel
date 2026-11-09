"""Text parsing primitives shared by validation tools."""

from __future__ import annotations

import re

DIALOGUE_PREFIXES = ("“", "‘", '"', "『", "「")
SEPARATOR_RE = re.compile(r"^(?:[-*_]{3,}|[—–]{3,}|第[一二三四五六七八九十百\d]+节)$")


def is_dialogue_paragraph(paragraph: str) -> bool:
    stripped = paragraph.strip()
    return bool(stripped) and stripped.startswith(DIALOGUE_PREFIXES)


def is_separator_paragraph(paragraph: str) -> bool:
    return bool(SEPARATOR_RE.fullmatch(paragraph.strip()))


def nonempty_paragraphs(text: str) -> list[str]:
    return [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]


def sentence_units(text: str) -> list[str]:
    return [unit.strip() for unit in re.split(r"(?<=[。！？!?])|\n", text) if unit.strip()]


def select_paragraphs(text: str, spec: str) -> tuple[str, list[int], int]:
    paragraphs = nonempty_paragraphs(text)
    selected: set[int] = set()
    normalized = spec.replace("，", ",").strip()
    if not normalized:
        raise ValueError("--scene-paragraphs 不得为空")
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            raise ValueError("--scene-paragraphs 含空段")
        if token.isdigit():
            start = end = int(token)
        else:
            match = re.fullmatch(r"(\d+)-(\d+)", token)
            if not match:
                raise ValueError(f"非法段落范围：{token}")
            start, end = int(match.group(1)), int(match.group(2))
        if start < 1 or end < start or end > len(paragraphs):
            raise ValueError(f"段落范围越界：{token}（正文共{len(paragraphs)}个非空段）")
        selected.update(range(start, end + 1))
    indexes = sorted(selected)
    return "\n\n".join(paragraphs[index - 1] for index in indexes), indexes, len(paragraphs)
