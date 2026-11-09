"""Stable kernel project locks with explicit in-process thread ownership.

The anchor is never unlinked. Its contents identify the protocol, not the owner;
only a live descriptor holding flock and this thread's ownership record confer
reentrancy. Old PID/unlink clients must be stopped before protocol cutover.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat
import threading

from .paths import ProjectPathError, safe_project_path
from .structure_store import StructureError

_PROTOCOL = b"gnovel-stable-flock-v1\n"
_mutex = threading.RLock()
_owners: dict[tuple[int, int], '_Owner'] = {}


@dataclass
class _Owner:
    fd: int
    pid: int
    thread: int
    path: Path
    identity: tuple[int, int]
    depth: int = 1


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _after_fork() -> None:
    # Never LOCK_UN an inherited description: that would release the parent's
    # lock too. Closing the child's references leaves the parent's holder intact.
    global _mutex, _owners
    for owner in _owners.values():
        try:
            os.close(owner.fd)
        except OSError:
            pass
    _owners = {}
    _mutex = threading.RLock()


os.register_at_fork(after_in_child=_after_fork)


def _lock_paths(root: Path) -> tuple[Path, Path]:
    root = root.expanduser().resolve()
    try:
        return (safe_project_path(root, '.gnovel/structure.lock'),
                safe_project_path(root, '.gnovel/transactions'))
    except ProjectPathError as exc:
        raise StructureError(exc.code, str(exc), exc.details) from exc


def _require_project(root: Path, *, recovering: bool) -> None:
    """Refuse unsupported/broken projects before creating a lock anchor."""
    from .jsonio import read_json
    from .project import current_schema_version, unsupported_error_code
    from .structure_store import load_structure, WORKFLOW_VERSION
    marker_path = safe_project_path(root, '.gnovel/project.json')
    if marker_path.exists():
        try:
            marker = read_json(marker_path)
        except (OSError, ValueError) as exc:
            raise StructureError("PROJECT_NOT_INITIALIZED", "corrupt project marker") from exc
        if (not isinstance(marker, dict) or marker.get("schemaVersion") != current_schema_version()
                or marker.get("workflowVersion") != WORKFLOW_VERSION):
            raise StructureError(unsupported_error_code(), "only the current project format is supported")
    # During recovery structure.json may be temporarily absent. The current
    # marker is sufficient to take the lock; the recovery validator then checks
    # every immutable image and the publication protocol before any business write.
    if not recovering or not marker_path.exists():
        safe_project_path(root, '.gnovel/structure.json')
        load_structure(root)


def _acquire(lock_path: Path) -> int:
    """Open one persistent anchor and acquire its kernel lock, without unlink."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StructureError('PROJECT_LOCK_INVALID', 'project lock anchor is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise StructureError('PROJECT_LOCKED', 'another client is changing this project') from exc
            raise
        payload = os.pread(fd, len(_PROTOCOL) + 64, 0)
        if not payload:
            os.write(fd, _PROTOCOL)
            os.fsync(fd)
            directory = os.open(lock_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        elif payload != _PROTOCOL:
            raise StructureError('PROJECT_LOCK_PROTOCOL_UNSUPPORTED',
                                 'stop all previous lock-protocol clients before controlled cutover; anchor retained',
                                 {'path': str(lock_path)})
        if _identity(os.stat(lock_path, follow_symlinks=False)) != _identity(info):
            raise StructureError('PROJECT_LOCK_REPLACED', 'project lock anchor changed during acquisition')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _owner_for_path(lock_path: Path) -> _Owner | None:
    try:
        key = _identity(os.stat(lock_path, follow_symlinks=False))
    except FileNotFoundError:
        return None
    return _owners.get(key)


def _owner_is_current(owner: _Owner) -> bool:
    return owner.pid == os.getpid() and owner.thread == threading.get_ident()


def _assert_owner_path(owner: _Owner) -> None:
    try:
        info = os.stat(owner.path, follow_symlinks=False)
    except OSError as exc:
        raise StructureError('PROJECT_LOCK_REPLACED', 'held project lock anchor is no longer at its path',
                             {'path': str(owner.path)}) from exc
    if not stat.S_ISREG(info.st_mode) or _identity(info) != owner.identity:
        raise StructureError('PROJECT_LOCK_REPLACED', 'held project lock anchor was replaced',
                             {'path': str(owner.path)})


def current_thread_owns_lock(root: Path) -> bool:
    """An ownership query, never PID text or process-wide reentrancy."""
    lock_path = _lock_paths(root)[0]
    with _mutex:
        owner = _owner_for_path(lock_path)
        if owner is None or not _owner_is_current(owner):
            return False
        _assert_owner_path(owner)
        return True


def assert_lock_current(root: Path) -> None:
    """Check a held anchor immediately before transaction publication.

    Low-level neutral write-set tests have no project lock. Real callers hold it;
    if this thread has one, losing/replacing its path must never silently downgrade
    the operation to an unlocked transaction.
    """
    lock_path = _lock_paths(root)[0]
    with _mutex:
        owners = [owner for owner in _owners.values()
                  if _owner_is_current(owner) and owner.path == lock_path]
        for owner in owners:
            _assert_owner_path(owner)


def project_lock_observation(root: Path) -> dict:
    """Read-only point-in-time occupancy; an idle anchor is not a held lock."""
    lock_path = _lock_paths(root)[0]
    with _mutex:
        owner = _owner_for_path(lock_path)
        if owner is not None:
            _assert_owner_path(owner)
            return {'locked': True, 'ownedByCurrentThread': _owner_is_current(owner), 'protocol': 'stable-flock-v1'}
        try:
            fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {'locked': False, 'ownedByCurrentThread': False, 'protocol': None}
        try:
            if os.pread(fd, len(_PROTOCOL) + 64, 0) != _PROTOCOL:
                return {'locked': True, 'ownedByCurrentThread': False, 'protocol': 'unsupported'}
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return {'locked': True, 'ownedByCurrentThread': False, 'protocol': 'stable-flock-v1'}
                raise
            fcntl.flock(fd, fcntl.LOCK_UN)
            return {'locked': False, 'ownedByCurrentThread': False, 'protocol': 'stable-flock-v1'}
        finally:
            os.close(fd)


def _take(lock_path: Path) -> _Owner:
    with _mutex:
        if _owner_for_path(lock_path) is not None:
            raise StructureError('PROJECT_LOCKED', 'another operation is changing this project')
        # A replaced path must not turn into a second lock for a cooperating thread.
        if any(owner.path == lock_path for owner in _owners.values()):
            raise StructureError('PROJECT_LOCK_REPLACED', 'project lock anchor was replaced')
        fd = _acquire(lock_path)
        owner = _Owner(fd, os.getpid(), threading.get_ident(), lock_path, _identity(os.fstat(fd)))
        _owners[owner.identity] = owner
        return owner


def _release(owner: _Owner) -> None:
    with _mutex:
        if not _owner_is_current(owner) or _owners.get(owner.identity) is not owner:
            raise StructureError('PROJECT_LOCK_NOT_OWNED', 'only the holding thread may release this lock')
        del _owners[owner.identity]
        try:
            fcntl.flock(owner.fd, fcntl.LOCK_UN)
        finally:
            os.close(owner.fd)


def _refuse_pending(transactions: Path) -> None:
    pending = sorted(path.stem for path in transactions.glob('*.json'))
    if pending:
        raise StructureError('TRANSACTION_INCOMPLETE',
                             'an earlier transaction did not finish; run workflow recover first',
                             {'transactions': pending})


@contextmanager
def project_lock_reentrant(root: Path):
    lock_path, transactions = _lock_paths(root)
    with _mutex:
        owner = _owner_for_path(lock_path)
        if owner is not None and _owner_is_current(owner):
            _assert_owner_path(owner)
            _refuse_pending(transactions)
            owner.depth += 1
        else:
            owner = None
    if owner is not None:
        try:
            yield
            _assert_owner_path(owner)
        finally:
            with _mutex:
                owner.depth -= 1
        return
    with project_lock(root):
        yield


@contextmanager
def project_lock(root: Path, *, allow_pending: bool = False):
    root = root.expanduser().resolve()
    lock_path, transactions = _lock_paths(root)
    _require_project(root, recovering=any(transactions.glob('*.json')))
    owner = _take(lock_path)
    try:
        if not allow_pending:
            _refuse_pending(transactions)
        _assert_owner_path(owner)
        yield
        _assert_owner_path(owner)
    finally:
        _release(owner)


@contextmanager
def relocation_lock(root: Path):
    """Keep one open lock inode through folder rename and update its live path."""
    root = root.expanduser().resolve()
    lock_path, transactions = _lock_paths(root)
    _require_project(root, recovering=False)
    owner = _take(lock_path)

    def relocated(moved_root: Path) -> None:
        with _mutex:
            owner.path = _lock_paths(moved_root)[0]
            _assert_owner_path(owner)

    try:
        _refuse_pending(transactions)
        yield relocated
        _assert_owner_path(owner)
    finally:
        _release(owner)
