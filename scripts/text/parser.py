"""Paragraph selection shared by validation tools."""

from __future__ import annotations

import re


def nonempty_paragraphs(text: str) -> list[str]:
    return [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]


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
