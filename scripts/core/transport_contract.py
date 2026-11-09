"""Shared transport metadata for MCP and dashboard capability declarations."""

from __future__ import annotations

from .catalog_api import CATALOG_OPERATIONS

STRUCTURE_OPERATIONS = (
    "preview_set_chapter_timespan",
    "preview_create_time_anchor",
    "preview_insert_chapter",
    "preview_move_chapter",
    "preview_archive_chapter",
    "commit_structure_plan",
    "undo_structure_operation",
) + tuple(spec["name"] for spec in CATALOG_OPERATIONS)

# Preview persists a plan even though the authoritative structure is unchanged.
PREVIEW_OPERATIONS = frozenset(name for name in STRUCTURE_OPERATIONS if name.startswith("preview_"))
READ_ONLY_OPERATIONS = frozenset()
MUTATING_OPERATIONS = frozenset(STRUCTURE_OPERATIONS)


def operation_is_read_only(name: str) -> bool:
    return name in READ_ONLY_OPERATIONS


def operation_is_mutating(name: str) -> bool:
    return name in MUTATING_OPERATIONS
