"""File-level GENERATED protocol; examples in prose or fences confer no ownership."""
from __future__ import annotations


def is_generated(text: str) -> bool:
    lines = [line.strip() for line in text.lstrip('﻿').splitlines() if line.strip()]
    if lines and lines[0].startswith('# '):
        lines = lines[1:]
    return bool(lines and lines[0].startswith('> GENERATED:'))
