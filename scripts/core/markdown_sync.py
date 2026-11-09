"""Explicit graph reading exports; no maintained project Markdown views."""
from __future__ import annotations

from pathlib import Path

from .commands import resolve_export_target
from .jsonio import write_text
from .structure_store import StructureError


def write_view_file(root: Path, relative: str, *, title: str, mermaid: str) -> dict:
    """Write an explicit reading snapshot under the same boundary as manuscript export."""
    path = resolve_export_target(root, Path(relative), book_name=title, fmt='md')
    content = f"# {title}\n\n> 阅读快照；不参与设定、规划或正文归属判断。\n\n```mermaid\n{mermaid.rstrip()}\n```\n"
    existing = path.read_text(encoding='utf-8') if path.is_file() else None
    if existing == content:
        return {'path': str(path), 'written': False, 'maintained': False, 'refused': None}
    if existing is not None:
        raise StructureError('EXPORT_EXISTS', 'choose a new snapshot output; existing content is preserved',
                             {'path': str(path)})
    write_text(path, content)
    return {'path': str(path), 'written': True, 'maintained': False, 'refused': None}
