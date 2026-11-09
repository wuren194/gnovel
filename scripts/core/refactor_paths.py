"""Filesystem-safe locator handling for authoring refactors.

A locator is never identity. This module is the single place that decides whether
a proposed project-relative path may be written, what bytes move with it, and how
a display name becomes a deterministic file name.

Three rules hold everywhere:

* `chapter.filePath` is read from the structure and never parsed back into
  identity. A file named `017-白塔坠落.md` identifies nothing on its own.
* Two spellings of one name share a canonical collision key (NFC + casefold), so
  `白塔.md` / `白塔́.md` and `ABC.md` / `abc.md` are recognised as the same slot
  before anything is written.
* Nothing is ever overwritten. A proposed target that already exists is refused,
  unless it is literally the directory entry the source already occupies (a case-only
  or normalization-only rename on a case-insensitive filesystem).

A case-only or normalization-only rename is one logical author action, but the volume
decides how it is carried. Where the two spellings resolve to one inode *and* to one
directory entry the rename is applied in place; where the target spelling does not exist
yet — a case-sensitive volume, where `A.md` is stored and `a.md` is not — it is an
ordinary transactional move. The source file is never counted as a collision with itself
in either case. One inode is not one entry, though: a hard link gives the same bytes a
second, real directory entry, and POSIX renames onto it succeed while changing nothing,
which would leave the old locator on disk. So the entry decides, and an inode reached
through two entries is an occupied target.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from .paths import ProjectPathError, safe_project_path
from .structure_store import StructureError

# Illegal in a path component on POSIX (`/`, NUL) plus the classic macOS/Windows
# set. Everything else — CJK, spaces, emoji, full-width punctuation — is preserved
# verbatim: a display name belongs to the author and is never transliterated.
_ILLEGAL = re.compile(r'[:/\\<>"|?*\x00-\x1f\x7f]')
_COLLAPSE = re.compile(r'-{2,}')

# Locator-only bookkeeping is allowed when the file was never authored (a chapter
# still at planning_required has no body yet); these are the statuses whose body
# file must already exist on disk for a move to carry bytes.
_BODY_REQUIRED_STATUSES = frozenset({'stable', 'stale'})


class RefactorPathError(StructureError):
    """A proposed locator is unsafe or would collide with an existing file."""


def safe_component(text: str) -> str:
    """Deterministic filesystem-safe path component from a display name.

    Unicode is normalized to NFC and only illegal characters are replaced, so the
    same input always yields the same component and `第十七章：白塔/坠落` becomes
    `第十七章-白塔-坠落` rather than a pinyin transliteration.
    """
    normalized = unicodedata.normalize('NFC', str(text))
    cleaned = _COLLAPSE.sub('-', _ILLEGAL.sub('-', normalized)).strip('-').strip()
    return cleaned or '未命名'


def canonical_key(relative: str | Path) -> str:
    """Collision key for one locator: NFC then casefold, separators unified.

    This is the only way the framework decides that two paths name the same slot.
    It is deliberately stricter than the filesystem: a case-sensitive volume keeps
    `ABC.md` and `abc.md` apart as real files, but the *proposal* stage still reports
    a clash between two of them, so an author is never surprised by a later overwrite.
    The file being moved is not a clash with itself — `check_target` excludes it.
    """
    text = unicodedata.normalize('NFC', str(relative).replace(os.sep, '/'))
    return text.casefold()


def stem_of(relative: str) -> str:
    return Path(relative).stem


def suffix_of(relative: str) -> str:
    return Path(relative).suffix


def parent_of(relative: str) -> str:
    parent = Path(relative).parent
    return '' if str(parent) == '.' else parent.as_posix()


def same_file(source: Path, target: Path) -> bool:
    """True when both paths resolve to one inode.

    That is identity, and identity is not location: a hard link answers True for two
    separate directory entries, and an insensitive volume answers True for two spellings
    of one entry. Only `separate_directory_entry()` tells the two shapes apart.
    """
    try:
        return os.path.samestat(source.stat(), target.stat())
    except OSError:
        return False


def resolve(root: Path, relative: str) -> Path:
    try:
        return safe_project_path(root, relative)
    except ProjectPathError as exc:
        raise RefactorPathError(exc.code, str(exc), exc.details) from exc


def is_canonical(root: Path, relative: str) -> bool:
    """A locator is canonical only when it round-trips through the project root."""
    if not isinstance(relative, str) or not relative.strip() or relative != relative.strip():
        return False
    if relative.startswith('/') or '\\' in relative or '\x00' in relative:
        return False
    if any(part in ('', '.', '..') for part in relative.split('/')):
        return False
    try:
        return str(resolve(root, relative).relative_to(root)) == relative
    except (RefactorPathError, ValueError):
        return False


def sibling_keys(directory: Path) -> dict[str, str]:
    """Canonical key → existing entry name for every entry in one directory.

    One name per key, so this answers *is the slot occupied* and nothing finer. On a
    case-sensitive volume several real entries can share a key and only the last is
    kept here; collision detection uses `canonical_siblings()` instead.
    """
    if not directory.is_dir():
        return {}
    return {canonical_key(entry.name): entry.name for entry in directory.iterdir()}


def canonical_siblings(directory: Path, key: str) -> list[Path]:
    """Every real directory entry that occupies one canonical slot.

    A case-sensitive volume holds `A.md`, `a.MD` and `a.md` at the same time, and all
    three share one key — so deciding *who* occupies the slot needs every occupant, not
    one per key. Callers exclude the entry they are themselves moving.
    """
    if not directory.is_dir():
        return []
    return [directory / entry.name for entry in directory.iterdir()
            if canonical_key(entry.name) == key]


def directory_entry_names(directory: Path) -> set[str] | None:
    """The spellings actually stored as entries of one directory, or `None` if unreadable.

    Filesystem lookup cannot answer this. Where the volume folds case or normalization,
    `a.md` resolves even when the only name stored is `A.md`, so `exists()` reports the
    inode and not the entry. A listing reports the entries, which is the difference
    between one entry under two spellings and two entries over one inode.
    """
    try:
        return {entry.name for entry in directory.iterdir()}
    except OSError:
        return None


def separate_directory_entry(source: Path, target: Path) -> bool:
    """True when the target spelling is a directory entry of its own, beside the source's.

    Call this where `same_file` already holds. An insensitive volume reaches one entry
    through both spellings, so the target name appears in no listing and the rename is
    genuinely in place. A hard link instead stores the target name as a second entry for
    that inode, and that is a target already occupied. An unreadable listing proves
    nothing, so it is reported as separate and the caller refuses.
    """
    target_entries = directory_entry_names(target.parent)
    if target_entries is None:
        return True
    if target.name not in target_entries:
        return False
    source_entries = directory_entry_names(source.parent)
    return source_entries is None or source.name in source_entries


def _classify_target_slot(target: Path, target_relative: str, *, exclude: Path | None) -> 'RefactorPathError | None':
    """Refusal for a target slot occupied by a directory entry other than `exclude`, else None.

    This decides occupancy from real directory entries, never from `exists()`. A broken
    symlink and a second hard-linked name are both real entries that `exists()` (false for
    a broken link) or an inode check (blind to a path that does not exist yet) would miss,
    and both make the slot occupied. A symlink is inspected as an entry and never followed,
    so nothing off the volume is read. `exclude` is the source entry when a present file is
    renaming onto its own slot; pass None when the source is missing, so no occupant is
    ever mistaken for the file being moved.
    """
    name = Path(target_relative).name
    target_nfc = unicodedata.normalize('NFC', name)
    key = canonical_key(name)
    exact: Path | None = None
    variants: list[Path] = []
    for entry in canonical_siblings(target.parent, key):
        if exclude is not None and entry == exclude:
            continue
        if unicodedata.normalize('NFC', entry.name) == target_nfc:
            exact = entry
        else:
            variants.append(entry)
    if exact is not None:
        if exact.is_dir() and not exact.is_symlink():
            return RefactorPathError('REFACTOR_TARGET_IS_DIRECTORY', 'target locator is occupied by a directory',
                                     {'path': target_relative})
        return RefactorPathError('REFACTOR_TARGET_EXISTS', 'target locator already exists',
                                 {'path': target_relative})
    if variants:
        return RefactorPathError('REFACTOR_PATH_CANONICAL_COLLISION',
                                 'target name collides with an existing entry under Unicode/case folding',
                                 {'path': target_relative, 'canonicalKey': key,
                                  'existingEntry': sorted(entry.name for entry in variants)})
    return None


def check_target(root: Path, source_relative: str, target_relative: str) -> dict:
    """Validate one proposed locator change and report how it must be applied.

    Returns a row with `action` in {`move`, `in_place`, `locator_only`}, the
    resolved source/target paths, and the source digest when bytes move. Every
    refusal raises `RefactorPathError` with a code the CLI and Doctor can surface.
    """
    if not is_canonical(root, source_relative):
        raise RefactorPathError('REFACTOR_PATH_INVALID', 'source locator is not a canonical project-relative path',
                                {'path': source_relative})
    if not is_canonical(root, target_relative):
        raise RefactorPathError('REFACTOR_PATH_INVALID', 'target locator is not a canonical project-relative path',
                                {'path': target_relative})
    if canonical_key(source_relative) == canonical_key(target_relative) and source_relative == target_relative:
        raise RefactorPathError('REFACTOR_PATH_UNCHANGED', 'target locator is identical to the source',
                                {'path': source_relative})

    source = resolve(root, source_relative)
    target = resolve(root, target_relative)
    row = {'sourcePath': source_relative, 'targetPath': target_relative,
           'sourceExists': source.is_file(), 'sourceHash': None, 'action': 'locator_only'}

    if not source.is_file():
        if source.exists() or source.is_symlink():
            # A directory or a symlink sitting at the source locator is not a movable body.
            # It must not be silently treated as "missing" and rebound past.
            raise RefactorPathError('REFACTOR_SOURCE_NOT_FILE', 'source locator is not a regular file',
                                    {'path': source_relative})
        # Nothing to carry. A missing source is only an error when the chapter has
        # already formed a formal body, which the caller decides from status. But a
        # locator being valid never proved the target slot is free: a missing source
        # is not the entry being moved, so it cannot be excluded from the slot, and any
        # existing directory entry there — regular file, directory, broken symlink or a
        # second hard-linked name — belongs to someone else. Rebinding the locator onto
        # it and then finalizing would overwrite bytes this chapter never owned. Only an
        # empty, unoccupied, canonical-safe slot may take a locator-only rebind.
        row['sourceState'] = 'missing'
        refusal = _classify_target_slot(target, target_relative, exclude=None)
        if refusal is not None:
            raise refusal
        return row
    row['sourceState'] = 'present'
    row['sourceHash'] = _digest(source)

    if same_file(source, target) and not separate_directory_entry(source, target):
        # Case-only or normalization-only rename: one entry, spelled two ways by a
        # volume that folds them. Applying it in place keeps the write set free of two
        # rows that name the same file. A second entry over one inode is a hard link,
        # so it is not this case and falls through to the refusal below — `os.rename`
        # would succeed there while changing nothing, stranding the old locator.
        row['action'] = 'in_place'
        return row
    if target.exists():
        if target.is_dir():
            raise RefactorPathError('REFACTOR_TARGET_IS_DIRECTORY', 'target locator is occupied by a directory',
                                    {'path': target_relative})
        raise RefactorPathError('REFACTOR_TARGET_EXISTS', 'target locator already exists',
                                {'path': target_relative})

    target_key = canonical_key(Path(target_relative).name)
    # Reaching here means the two spellings are different inodes and the target does not
    # exist yet, which is exactly the case-sensitive-volume shape of a case-only or
    # normalization-only rename: the source still occupies the slot the target is about
    # to take. That entry is the file being moved, so it is excluded by directory entry
    # — never by inode, which cannot speak for a path that does not exist yet, and never
    # for a symlink or hard link, which would alias the slot instead of moving it. Any
    # other occupant is a third file and is refused.
    occupants = [entry for entry in canonical_siblings(target.parent, target_key) if entry != source]
    if occupants:
        raise RefactorPathError('REFACTOR_PATH_CANONICAL_COLLISION',
                                'target name collides with an existing entry under Unicode/case folding',
                                {'path': target_relative, 'canonicalKey': target_key,
                                 'existingEntry': sorted(entry.name for entry in occupants)})
    row['action'] = 'move'
    return row


def check_body_required(chapter: dict) -> bool:
    """A stable or stale chapter has formed a formal body; its file must exist."""
    return chapter.get('status') in _BODY_REQUIRED_STATUSES


def _digest(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synced_body_path(root: Path, chapter: dict) -> str:
    """Deterministic body locator derived from ordinal and title.

    The ordinal prefix keeps the shelf readable after a reorder without ever
    becoming identity: reorder does not rewrite this path, only an explicit
    path-sync refactor does.
    """
    ordinal = chapter.get('displayOrdinal')
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise RefactorPathError('REFACTOR_ORDINAL_UNAVAILABLE',
                                'chapter has no display ordinal to build a file name from',
                                {'chapterId': chapter.get('chapterId')})
    title = str(chapter.get('title', '')).strip()
    if not title:
        raise RefactorPathError('REFACTOR_TITLE_REQUIRED', 'chapter title is required to sync a file name',
                                {'chapterId': chapter.get('chapterId')})
    return f"正文/{ordinal:03d}-{safe_component(title)}.md"
