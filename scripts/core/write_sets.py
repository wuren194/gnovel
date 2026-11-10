"""Durable, conflict-checked write sets shared by project transactions.

Callers hold project_lock and authorize the complete inventory before applying it.
A manifest is durable before the first business write. Recovery accepts only the
recorded before/after images; unrelated edits are never overwritten.

Three kinds of row live in one manifest and are never interchangeable. `files` rows are
business outputs: the transaction owns their before- and after-image and recovery may
restore the before-image. `guards` rows are read-only observations of files the
transaction must not write and must not restore (authored prose being the case that
motivated them); the transaction owns neither image of a guard, and a guard can only
ever veto the commit marker. `treeGuards` rows are the membership counterpart: the set
of paths under a root that must still be the same set when the commit marker lands, so
a file that did not exist when the guards were minted cannot slip in unobserved.
`guardGroups` carry the same read-only claims for explicitly named project roots;
only `files` under the transaction root can ever be written or recovered.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from .jsonio import write_bytes, write_json
from .paths import safe_project_path as _safe_project_path, ProjectPathError
from .structure_store import StructureError, canonical_json


def safe_project_path(root: Path, relative: str | Path) -> Path:
    try:
        return _safe_project_path(root, relative)
    except ProjectPathError as exc:
        raise StructureError(exc.code, str(exc), exc.details) from exc


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def file_state(path: Path) -> dict:
    if path.exists() and not path.is_file():
        raise StructureError("TRANSACTION_TARGET_INVALID", "transaction target is not a regular file", {"path": str(path)})
    if not path.exists():
        return {"exists": False, "sha256": None, "mode": None}
    return {"exists": True, "sha256": digest(path.read_bytes()), "mode": stat.S_IMODE(path.stat().st_mode)}


def inventory(root: Path, outputs: dict[str, bytes | None], *, owners: dict | None = None) -> list[dict]:
    from .jsonio import _target_mode
    rows = []
    for relative, data in sorted(outputs.items()):
        path = safe_project_path(root, relative)
        if str(path.relative_to(root)) != relative:
            raise StructureError("TRANSACTION_PATH_INVALID", "write-set paths must be canonical", {"path": relative})
        before = file_state(path)
        after = {"exists": data is not None, "sha256": digest(data) if data is not None else None,
                 "mode": _target_mode(path) if data is not None else None}
        rows.append({"path": relative, "action": "delete" if data is None else "update" if before["exists"] else "create",
                     "before": before, "after": after, "owner": (owners or {}).get(relative, "transaction")})
    return rows


def validate_inventory(root: Path, rows: list[dict], *, side: str = "before", code: str = "WRITE_SET_CONFLICT") -> None:
    for row in rows:
        if file_state(safe_project_path(root, row["path"])) != row[side]:
            raise StructureError(code, "file changed since the approved snapshot", {"path": row["path"]})
        if side == 'before' and row.get('expectedObject') is not None:
            if _object_state(safe_project_path(root, row['path'])) != row['expectedObject']:
                raise StructureError(code, 'file object changed since the validated snapshot', {'path': row['path']})


# --- read-only guards ---------------------------------------------------------------
# A guard is a claim about an object the transaction does not own: "what I classified and
# planned against is still the bytes and still the file at the moment of commit". It is
# checked at three points around one metadata transaction and it is never a write target
# and never a recovery source, so an outsider's edit can only ever veto the commit marker.

GUARD_KEYS = ("exists", "sha256", "dev", "ino")


def guard_state(path: Path) -> dict:
    """Observe one object read-only: its bytes plus the physical object carrying them.

    The digest alone is not enough. An outsider who unlinks a chapter and writes the same
    bytes back produces identical content on a different inode, and a transaction that
    claims to have been prepared against the prose it saw must be able to say so. Bytes and
    identity are taken through one descriptor so they describe the same object.
    """
    return guard_observation(path)[0]


def guard_observation(path: Path) -> tuple[dict, bytes | None]:
    """`guard_state()` plus the exact payload it hashed, from the same descriptor read.

    A caller that classifies bytes it also guards — source migration parses authored text —
    must classify the bytes the guard will CAS. Taking the payload here is what keeps
    "validated bytes" and "guarded bytes" one observation of one object rather than two
    reads that could disagree between the validation and the commit marker.
    """
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"exists": False, "sha256": None, "dev": None, "ino": None}, None
    except OSError:
        # A swapped symlink or an unreadable object: something is at this path and it is not
        # the regular file that was observed, which is a state a guard carries and compares
        # rather than one that quietly reads as "absent".
        info = os.stat(path, follow_symlinks=False)
        return {"exists": True, "sha256": None, "dev": info.st_dev, "ino": info.st_ino}, None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            # A directory sitting at the locator: identity is still comparable, bytes are not.
            return {"exists": True, "sha256": None, "dev": info.st_dev, "ino": info.st_ino}, None
        chunks = []
        while True:
            block = os.read(handle, 1 << 20)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(handle)
    payload = b"".join(chunks)
    return {"exists": True, "sha256": digest(payload), "dev": info.st_dev, "ino": info.st_ino}, payload


def capture_guards(root: Path, relatives: list[str]) -> list[dict]:
    """Fresh guard rows for paths the transaction will only ever compare."""
    rows = []
    for relative in sorted(set(relatives)):
        path = safe_project_path(root, relative)
        if str(path.relative_to(root)) != relative:
            raise StructureError("TRANSACTION_PATH_INVALID", "guard paths must be canonical",
                                 {"path": relative})
        rows.append({"path": relative, **guard_state(path)})
    return rows


def validate_guards(root: Path, rows: list[dict], *, stage: str) -> None:
    """Re-observe every guard and fail closed on any drift, before the named commit point."""
    for row in rows:
        observed = guard_state(safe_project_path(root, row["path"]))
        if any(observed[key] != row[key] for key in GUARD_KEYS):
            raise StructureError(
                "TRANSACTION_GUARD_DRIFT", "a read-only guard file changed during the transaction",
                {"path": row["path"], "stage": stage,
                 "expected": {key: row[key] for key in GUARD_KEYS},
                 "observed": {key: observed[key] for key in GUARD_KEYS}})


# --- read-only tree guards ----------------------------------------------------------
# A per-path guard only protects a path that was already enumerated when it was minted.
# A new authored file, a deletion, a rename or a file→directory swap lands outside every
# row, so membership is guarded as its own claim about the same roots.

def tree_membership(root: Path, roots: list[str], *, suffixes: list[str] | None = None) -> dict:
    """Canonical member list of `roots` and its digest, recomputed read-only from disk.

    Membership is the *path set*, not the contents. Directories are not members — an empty
    new directory is not authored material — but everything else is, symlinks included, so
    an object swapped in where a file used to be reads as drift instead of quietly leaving
    the member set. `members` is project-relative, canonical and sorted, which is what makes
    a persisted digest meaningful: the same tree always spells the same list.
    """
    if suffixes is not None and (not isinstance(suffixes, list) or not suffixes
            or any(not isinstance(value, str) or not value.startswith('.') for value in suffixes)):
        raise StructureError('TRANSACTION_GUARD_INVALID', 'tree suffixes must be nonempty file extensions')
    accepted = {value.lower() for value in suffixes} if suffixes is not None else None
    members = []
    for relative in sorted(roots):
        base = safe_project_path(root, relative)
        if base.is_symlink():
            members.append(relative)
            continue
        if not base.is_dir():
            if base.exists() and (accepted is None or base.suffix.lower() in accepted):
                members.append(relative)
            continue
        def unreadable(error):
            raise StructureError('TRANSACTION_GUARD_DRIFT', 'cannot enumerate the complete guarded directory',
                                 {'path': str(error.filename or base)}) from error
        for folder, directories, files in os.walk(base, followlinks=False, onerror=unreadable):
            paths = [Path(folder) / name for name in files]
            paths.extend(Path(folder) / name for name in directories if (Path(folder) / name).is_symlink())
            for path in paths:
                if accepted is not None and not path.is_symlink() and path.suffix.lower() not in accepted:
                    continue
                members.append(str(path.relative_to(root)))
    members.sort()
    return {"roots": sorted(roots), "members": members,
            **({'suffixes': sorted(accepted)} if accepted is not None else {}),
            "membershipDigest": digest(canonical_json(members).encode())}


def validate_tree_guards(root: Path, rows: list[dict], *, stage: str) -> None:
    """Recompute every tree guard's membership and fail closed on any drift."""
    for row in rows:
        observed = tree_membership(root, row["roots"], suffixes=row.get('suffixes'))
        if (observed["members"] != row["members"]
                or observed["membershipDigest"] != row["membershipDigest"]):
            raise StructureError(
                "TRANSACTION_GUARD_DRIFT", "the authored tree membership changed during the transaction",
                {"roots": row["roots"], "stage": stage,
                 "added": sorted(set(observed["members"]) - set(row["members"])),
                 "removed": sorted(set(row["members"]) - set(observed["members"])),
                 "expectedMembershipDigest": row["membershipDigest"],
                 "observedMembershipDigest": observed["membershipDigest"]})


def validate_guard_groups(rows: list[dict], *, stage: str) -> None:
    """Compare other projects read-only; their files are never recovery targets."""
    for group in rows:
        root = Path(group['root'])
        try:
            validate_guards(root, group['guards'], stage=stage)
            validate_tree_guards(root, group['treeGuards'], stage=stage)
        except StructureError as exc:
            exc.details['root'] = str(root)
            raise


def checkpoint(phase: str, relative: str | None = None) -> None:
    """Fault-injection seam; production execution does not alter control flow."""


def _persist(root: Path, manifest: dict) -> None:
    path = safe_project_path(root, f".govel/transactions/{manifest['transactionId']}.json")
    write_json(path, manifest)


PUBLICATION_PROTOCOL = "preserve-exclusive-v1"


def _transaction_path(root: Path, manifest: dict, relative: str) -> Path:
    return safe_project_path(root, f".govel/transactions/{manifest['transactionId']}/{relative}")


def _clear(root: Path, manifest: dict) -> None:
    """Archive this exact terminal manifest; retain displaced inodes and images.

    An editor can still hold a descriptor to a displaced inode after our commit.
    Deleting that inode's last name would lose its later save, so neither normal
    cleanup nor GC removes this transaction directory.
    """
    from .file_preservation import exclusive_move
    active = safe_project_path(root, f".govel/transactions/{manifest['transactionId']}.json")
    terminal = _transaction_path(root, manifest, 'completed.json')
    checkpoint('before-archive')
    exclusive_move(active, terminal)
    checkpoint('archived')


def _object_state(path: Path) -> dict:
    from .file_preservation import observation
    return observation(path)[0]


def _bytes_state(value: dict) -> dict:
    return {key: value[key] for key in ('exists', 'sha256', 'mode')}


def _require_protocol(manifest: dict) -> None:
    import re
    txid = manifest.get('transactionId', '')
    if not isinstance(txid, str) or not re.fullmatch(r'TX-[A-Za-z0-9-]+', txid):
        raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'invalid transaction identity')
    if manifest.get('schemaVersion') != 2 or manifest.get('publicationProtocol') != PUBLICATION_PROTOCOL:
        raise StructureError('TRANSACTION_RECOVERY_BLOCKED',
                             'transaction lacks the current preservation proof; original evidence retained',
                             {'transactionId': txid})
    if not isinstance(manifest.get('files'), list) or not manifest['files']:
        raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'missing transaction inventory')


def _allowed_objects(row: dict) -> list[dict]:
    return [row[key] for key in ('beforeObject', 'afterObject', 'rollbackObject') if key in row]


def _validate_row_paths(root: Path, manifest: dict, row: dict, number: int) -> None:
    expected = {'publishSlot': f'publish/{number}', 'preservedSlot': f'displaced/forward/{number}',
                'rollbackSlot': f'rollback-ready/{number}', 'rollbackPreservedSlot': f'displaced/rollback/{number}'}
    for key, relative in expected.items():
        if row.get(key) != relative:
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'invalid publication slot', {'path': row.get('path'), 'field': key})
        _transaction_path(root, manifest, relative)
    for key in ('beforeObject', 'afterObject', 'rollbackObject'):
        value = row.get(key)
        if (not isinstance(value, dict) or set(value) != {'exists', 'sha256', 'mode', 'dev', 'ino'}
                or type(value['exists']) is not bool):
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'missing physical object proof', {'path': row.get('path'), 'field': key})


def validate_recovery(root: Path, manifest: dict) -> list[tuple[dict, Path, Path]]:
    _require_protocol(manifest)
    checked, seen = [], set()
    for number, row in enumerate(manifest['files']):
        relative = row['path']
        path = safe_project_path(root, relative)
        if relative in seen or str(path.relative_to(root)) != relative:
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'duplicate or noncanonical path')
        seen.add(relative)
        _validate_row_paths(root, manifest, row, number)
        source = _transaction_path(root, manifest, f'before/{relative}')
        if row['before']['exists'] and (not source.is_file() or digest(source.read_bytes()) != row['before']['sha256']):
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'backup missing or damaged', {'path': relative})
        after = _transaction_path(root, manifest, f'after/{relative}')
        if row['after']['exists'] and (not after.is_file() or digest(after.read_bytes()) != row['after']['sha256']):
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'after image missing or damaged', {'path': relative})
        current = _object_state(path)
        # A missing slot is expected after preserve and before exclusive publish.
        # It is never enough to claim the after-image was committed.
        if current['exists'] and current not in _allowed_objects(row):
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'file was edited or replaced after interruption',
                                 {'path': relative, 'preservedPath': str(_transaction_path(root, manifest, row['preservedSlot']))})
        if not current['exists'] and row['before']['exists']:
            preserved = _object_state(_transaction_path(root, manifest, row['preservedSlot']))
            rollback_preserved = _object_state(_transaction_path(root, manifest, row['rollbackPreservedSlot']))
            known = any(item['exists'] and (item['dev'], item['ino']) == (expected['dev'], expected['ino'])
                        for item, expected in ((preserved, row['beforeObject']),
                                               (rollback_preserved, row['afterObject'])))
            if not known:
                raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'empty slot has no recorded preservation object',
                                     {'path': relative})
        if row['before'] == row['after'] and current != row['beforeObject']:
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'unchanged input no longer matches', {'path': relative})
        if row['before']['exists'] and current != row['beforeObject'] and current != row['rollbackObject']:
            restored = _transaction_path(root, manifest, row['rollbackSlot'])
            if _object_state(restored) != row['rollbackObject']:
                raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'prepared rollback copy missing or damaged', {'path': relative})
        checked.append((row, path, source))
    return checked


def _move_checked(root: Path, manifest: dict, path: Path, slot: str, expected: dict, *, stage: str) -> None:
    """Preserve the object actually moved, then validate it. Never blind rollback."""
    from .file_preservation import exclusive_move
    from .locks import assert_lock_current
    target = _transaction_path(root, manifest, slot)
    assert_lock_current(root)
    checkpoint(f'{stage}-before-preserve', str(path.relative_to(root)))
    exclusive_move(path, target)
    checkpoint(f'{stage}-preserved', str(path.relative_to(root)))
    if _object_state(target) != expected:
        # Best effort puts the exact foreign object back only if its slot is empty.
        # If occupied, retain both objects and let recovery report the evidence.
        try:
            exclusive_move(target, path)
        except OSError:
            pass
        raise StructureError('WRITE_SET_CONFLICT' if stage == 'publish' else 'TRANSACTION_RECOVERY_BLOCKED',
                             'the actual displaced object differs from the expected object',
                             {'path': str(path.relative_to(root)), 'preservedPath': str(target), 'stage': stage})


def _publish_row(root: Path, manifest: dict, row: dict) -> None:
    from .file_preservation import exclusive_move
    from .locks import assert_lock_current
    path = safe_project_path(root, row['path'])
    if row['before'] == row['after']:
        return
    if row['before']['exists']:
        _move_checked(root, manifest, path, row['preservedSlot'], row['beforeObject'], stage='publish')
    assert_lock_current(root)
    checkpoint('publish-before-install', row['path'])
    if row['after']['exists']:
        exclusive_move(_transaction_path(root, manifest, row['publishSlot']), path)
    elif path.exists():
        raise StructureError('WRITE_SET_CONFLICT', 'another writer occupied the deleted slot', {'path': row['path']})
    checkpoint('publish-installed', row['path'])
    if _object_state(path) != row['afterObject']:
        raise StructureError('WRITE_SET_CONFLICT', 'published output changed before confirmation', {'path': row['path']})


def recover_write_set(root: Path, manifest: dict) -> list[dict]:
    """Restore COMPLETE OLD or report blocked; committed archives never restore guards."""
    from .file_preservation import exclusive_move
    from .locks import assert_lock_current
    _require_protocol(manifest)
    if manifest.get('phase') == 'committed':
        _clear(root, manifest)
        return [{'action': 'committed_cleanup'}]
    checked = validate_recovery(root, manifest)
    manifest['phase'] = 'rollback'
    _persist(root, manifest)
    checkpoint('rollback-prepared')
    actions = []
    for row, path, source in reversed(checked):
        assert_lock_current(root)
        current = _object_state(path)
        if current == row['beforeObject'] or current == row['rollbackObject']:
            actions.append({'path': row['path'], 'action': 'unchanged'})
            continue
        if current['exists']:
            if current != row['afterObject']:
                raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'file changed during recovery', {'path': row['path']})
            _move_checked(root, manifest, path, row['rollbackPreservedSlot'], row['afterObject'], stage='rollback')
        checkpoint('rollback-before-install', row['path'])
        assert_lock_current(root)
        if row['before']['exists']:
            exclusive_move(_transaction_path(root, manifest, row['rollbackSlot']), path)
        elif path.exists():
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'foreign object occupies rollback slot', {'path': row['path']})
        checkpoint('rollback', row['path'])
        if _object_state(path) != row['rollbackObject']:
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'restored object changed during recovery', {'path': row['path']})
        actions.append({'path': row['path'], 'action': 'restored' if row['before']['exists'] else 'removed'})
    for row, path, source in checked:
        if _object_state(path) not in (row['beforeObject'], row['rollbackObject']):
            raise StructureError('TRANSACTION_RECOVERY_BLOCKED', 'restored state changed before completion', {'path': row['path']})
    manifest['phase'] = 'rolled_back'
    _persist(root, manifest)
    _clear(root, manifest)
    return actions


def apply_write_set(root: Path, outputs: dict[str, bytes | None], *, expected: list[dict] | None = None,
                    operation: str, modes: dict[str, int] | None = None,
                    guards: list[dict] | None = None,
                    tree_guards: list[dict] | None = None,
                    guard_groups: list[dict] | None = None) -> list[dict]:
    from .file_preservation import observation
    from .locks import assert_lock_current
    root = root.resolve()
    guard_rows = list(guards or [])
    tree_guard_rows = list(tree_guards or [])
    grouped_guards = list(guard_groups or [])
    rows = inventory(root, outputs)
    if not rows:
        return []
    if modes:
        for row in rows:
            if row['after']['exists'] and row['path'] in modes:
                row['after']['mode'] = modes[row['path']]
    if expected is not None:
        validate_inventory(root, expected)
        if {r['path']: r['after']['sha256'] for r in expected} != {r['path']: r['after']['sha256'] for r in rows}:
            raise StructureError('WRITE_SET_MISMATCH', 'derived writes differ from the approved preview')
    validate_guards(root, guard_rows, stage='preparing')
    validate_tree_guards(root, tree_guard_rows, stage='preparing')
    validate_guard_groups(grouped_guards, stage='preparing')
    assert_lock_current(root)
    txid = 'TX-' + uuid.uuid4().hex.upper()
    directory = safe_project_path(root, f'.govel/transactions/{txid}')
    missing = {'exists': False, 'sha256': None, 'mode': None, 'dev': None, 'ino': None}
    expected_objects = {row['path']: row['expectedObject'] for row in expected or [] if row.get('expectedObject') is not None}
    for number, row in enumerate(rows):
        relative = row['path']
        before, payload = observation(safe_project_path(root, relative))
        if relative in expected_objects and before != expected_objects[relative]:
            raise StructureError('WRITE_SET_CONFLICT', 'input object changed after the validated snapshot', {'path': relative})
        if _bytes_state(before) != row['before']:
            raise StructureError('WRITE_SET_CONFLICT', 'input changed while preparing immutable image', {'path': relative})
        row.update({'beforeObject': before, 'publishSlot': f'publish/{number}',
                    'preservedSlot': f'displaced/forward/{number}', 'rollbackSlot': f'rollback-ready/{number}',
                    'rollbackPreservedSlot': f'displaced/rollback/{number}'})
        if before['exists']:
            write_bytes(directory / 'before' / relative, payload, mode=row['before']['mode'])
            write_bytes(directory / row['rollbackSlot'], payload, mode=row['before']['mode'])
            row['rollbackObject'] = _object_state(directory / row['rollbackSlot'])
        else:
            row['rollbackObject'] = dict(missing)
        if outputs[relative] is not None:
            write_bytes(directory / 'after' / relative, outputs[relative], mode=row['after']['mode'])
            write_bytes(directory / row['publishSlot'], outputs[relative], mode=row['after']['mode'])
            row['afterObject'] = _object_state(directory / row['publishSlot'])
        else:
            row['afterObject'] = dict(missing)
        if row['before'] == row['after']:
            row['afterObject'] = dict(row['beforeObject'])
    manifest = {'schemaVersion': 2, 'publicationProtocol': PUBLICATION_PROTOCOL,
                'transactionId': txid, 'operation': operation, 'phase': 'prepared',
                'files': rows, 'guards': guard_rows, 'treeGuards': tree_guard_rows}
    if grouped_guards:
        manifest['guardGroups'] = grouped_guards
    # Files have been fsynced by write_bytes; persist all directory entries for
    # immutable images and publication slots before exposing the active manifest.
    directories = [directory, *[p for p in directory.rglob('*') if p.is_dir()]]
    for folder in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    checkpoint('images-synced')
    checkpoint('before-prepared')
    _persist(root, manifest)
    try:
        checkpoint('prepared')
        validate_inventory(root, rows)
        for row in rows:
            if _object_state(safe_project_path(root, row['path'])) != row['beforeObject']:
                raise StructureError('WRITE_SET_CONFLICT', 'input object changed since preparation', {'path': row['path']})
        validate_guards(root, guard_rows, stage='after-prepared')
        validate_tree_guards(root, tree_guard_rows, stage='after-prepared')
        validate_guard_groups(grouped_guards, stage='after-prepared')
        for row in rows:
            _publish_row(root, manifest, row)
            if row['before'] != row['after']:
                checkpoint('write', row['path'])
        validate_inventory(root, rows, side='after')
        for row in rows:
            if _object_state(safe_project_path(root, row['path'])) != row['afterObject']:
                raise StructureError('WRITE_SET_CONFLICT', 'output object changed before commit', {'path': row['path']})
        validate_guards(root, guard_rows, stage='before-committed')
        validate_tree_guards(root, tree_guard_rows, stage='before-committed')
        validate_guard_groups(grouped_guards, stage='before-committed')
        assert_lock_current(root)
        checkpoint('before-committed')
        manifest['phase'] = 'committed'
        _persist(root, manifest)
        checkpoint('committed')
    except BaseException as failure:
        try:
            # A failed commit-marker write may leave the durable phase prepared.
            # Recovery follows the actual persisted record, never our mutated dict.
            from .jsonio import read_json
            durable = read_json(safe_project_path(root, f'.govel/transactions/{txid}.json'))
            recover_write_set(root, durable)
        except BaseException as recovery:
            raise OSError(f'rollback incomplete（原始失败：{failure}）；{recovery}') from failure
        raise
    _clear(root, manifest)
    return [{key: value for key, value in row.items()
             if key in {'path', 'action', 'before', 'after', 'owner'}} for row in rows]
