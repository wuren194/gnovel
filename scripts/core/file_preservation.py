"""macOS exclusive rename and descriptor-anchored file observations.

This is a filesystem primitive, not a transaction or an authorization mechanism.
No operation overwrites a destination, follows a symlink, or unlinks displaced
objects. write_sets owns intent persistence and recovery.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import stat

from .structure_store import StructureError


def checkpoint(stage: str, path: str) -> None:
    """Neutral fault-injection seam for native rename and directory durability."""


@contextmanager
def parent_directory(path: Path, *, create: bool = False):
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700 if '.govel' in path.parts else 0o777, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def observation(path: Path) -> tuple[dict, bytes | None]:
    missing = {'exists': False, 'sha256': None, 'mode': None, 'dev': None, 'ino': None}
    try:
        with parent_directory(path) as directory:
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return missing, None
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise StructureError('TRANSACTION_TARGET_INVALID', 'transaction target is not a regular file',
                                         {'path': str(path)})
                chunks = []
                while True:
                    data = os.read(fd, 1 << 20)
                    if not data:
                        break
                    chunks.append(data)
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise StructureError('WRITE_SET_CONFLICT', 'file changed during observation', {'path': str(path)})
                data = b''.join(chunks)
                return {'exists': True, 'sha256': hashlib.sha256(data).hexdigest(),
                        'mode': stat.S_IMODE(before.st_mode), 'dev': before.st_dev, 'ino': before.st_ino}, data
            finally:
                os.close(fd)
    except FileNotFoundError:
        return missing, None


def exclusive_move(source: Path, target: Path) -> None:
    """Atomically move the actual source object only into an empty destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameatx_np
    except AttributeError as exc:
        raise StructureError('SAFE_PUBLICATION_UNAVAILABLE', 'exclusive rename requires the supported macOS runtime') from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    with parent_directory(source) as source_fd, parent_directory(target, create=True) as target_fd:
        checkpoint('native-before-rename', str(target))
        result = rename(source_fd, os.fsencode(source.name), target_fd, os.fsencode(target.name), 0x00000004)
        if result:
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value), str(target))
        checkpoint('native-renamed', str(target))
        os.fsync(source_fd)
        checkpoint('native-source-synced', str(target))
        os.fsync(target_fd)
        checkpoint('native-target-synced', str(target))
