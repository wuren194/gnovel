"""Independent transaction images using copy-on-write when the filesystem allows it.

Cloning saves physical writes, never identity checks: both the source and the
result are read and hashed. No image is hard-linked to a mutable project file.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import secrets
import stat

from .file_preservation import _exclusive_rename, parent_directory
from .structure_store import StructureError


_CLONE_UNAVAILABLE = {errno.EXDEV, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP,
                      errno.ENOTTY, errno.EINVAL}


def _identity(info):
    return info.st_dev, info.st_ino


def _conflict(path: Path, message: str):
    return StructureError('WRITE_SET_CONFLICT', message, {'path': str(path)})


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(fd, 1 << 20, offset):
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _check_source(fd: int, directory: int, source: Path, expected: dict):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise _conflict(source, 'snapshot source is no longer a regular file')
    observed = {'exists': True, 'sha256': _hash_fd(fd), 'mode': stat.S_IMODE(before.st_mode),
                'dev': before.st_dev, 'ino': before.st_ino}
    after = os.fstat(fd)
    try:
        current = os.stat(source.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _conflict(source, 'snapshot source disappeared') from exc
    if (observed != expected or _identity(current) != _identity(before)
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise _conflict(source, 'snapshot source changed after its validated observation')
    return before


def _unlink_owned(directory: int, name: str, fd: int) -> None:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(current) == _identity(os.fstat(fd)):
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _create_file(directory: int, name: str) -> int:
    return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                   0o600, dir_fd=directory)


def _clone_file(source_fd: int, directory: int, name: str) -> tuple[int, bool]:
    """Return a new writable inode and whether it contains a CoW source image."""
    system = platform.system()
    if system == 'Darwin':
        try:
            clone = ctypes.CDLL(None, use_errno=True).fclonefileat
        except AttributeError:
            return _create_file(directory, name), False
        clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        clone.restype = ctypes.c_int
        if clone(source_fd, directory, os.fsencode(name), 0x0002):  # CLONE_NOOWNERCOPY
            value = ctypes.get_errno()
            if value in _CLONE_UNAVAILABLE:
                return _create_file(directory, name), False
            raise OSError(value, os.strerror(value), name)
        # A read-only source produces a read-only clone. Change permissions only
        # through its descriptor before reopening the same new inode for writes.
        reader = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if (not stat.S_ISREG(os.fstat(reader).st_mode)
                    or _identity(os.fstat(reader)) == _identity(os.fstat(source_fd))):
                raise _conflict(Path(name), 'clone did not create an independent regular file')
            os.fchmod(reader, 0o600)
            writer = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if _identity(os.fstat(reader)) != _identity(os.fstat(writer)):
                    raise _conflict(Path(name), 'snapshot temporary file was replaced')
            except BaseException:
                os.close(writer)
                raise
            return writer, True
        except BaseException:
            _unlink_owned(directory, name, reader)
            raise
        finally:
            os.close(reader)
    fd = _create_file(directory, name)
    if system == 'Linux':
        try:
            try:
                fcntl.ioctl(fd, 0x40049409, source_fd)  # FICLONE
                return fd, True
            except OSError as exc:
                if exc.errno not in _CLONE_UNAVAILABLE:
                    raise
                os.ftruncate(fd, 0)
        except BaseException:
            try:
                _unlink_owned(directory, name, fd)
            finally:
                os.close(fd)
            raise
    return fd, False


def _write_payload(fd: int, payload: bytes, *, cloned: bool) -> None:
    block_size = max(1, os.fstat(fd).st_blksize) if cloned else 1 << 20
    for offset in range(0, len(payload), block_size):
        block = payload[offset:offset + block_size]
        if cloned and os.pread(fd, len(block), offset) == block:
            continue
        written = 0
        while written < len(block):
            count = os.pwrite(fd, block[written:], offset + written)
            if count <= 0:
                raise OSError(errno.EIO, 'snapshot write made no progress')
            written += count
    os.ftruncate(fd, len(payload))


def _snapshot(directory: int, target: Path, payload: bytes, mode: int,
              source_fd: int | None, source_directory: int | None,
              source: Path | None, expected_source: dict | None) -> None:
    rename, flag, primitive = _exclusive_rename()
    name = '.govel-snapshot-' + secrets.token_hex(16)
    if source_fd is None:
        fd, cloned = _create_file(directory, name), False
    else:
        fd, cloned = _clone_file(source_fd, directory, name)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _conflict(target, 'snapshot temporary file is not regular')
        if source_fd is not None:
            source_info = _check_source(source_fd, source_directory, source, expected_source)
            if _identity(info) == _identity(source_info):
                raise _conflict(target, 'snapshot must have an independent inode')
            if cloned and (info.st_size != source_info.st_size or _hash_fd(fd) != expected_source['sha256']):
                raise _conflict(source, 'cloned snapshot differs from the validated source')
        _write_payload(fd, payload, cloned=cloned)
        if os.fstat(fd).st_size != len(payload) or _hash_fd(fd) != hashlib.sha256(payload).hexdigest():
            raise _conflict(target, 'snapshot output differs from the requested bytes')
        os.fchmod(fd, mode)
        os.fsync(fd)
        if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(info):
            raise _conflict(target, 'snapshot temporary file was replaced before publication')
        # Both names stay anchored to this directory. Snapshot construction does
        # not trigger the business-publication crash checkpoints.
        if rename(directory, os.fsencode(name), directory, os.fsencode(target.name), flag):
            value = ctypes.get_errno()
            if value in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise StructureError('SAFE_PUBLICATION_UNAVAILABLE',
                                     'snapshot publication requires exclusive rename',
                                     {'primitive': primitive, 'errno': value, 'path': str(target)})
            raise OSError(value, os.strerror(value), str(target))
        os.fsync(directory)
    finally:
        try:
            _unlink_owned(directory, name, fd)
        finally:
            os.close(fd)


def write_snapshot(target: Path, payload: bytes, *, mode: int, source: Path | None = None,
                   expected_source: dict | None = None) -> None:
    """Publish one durable independent image at a new path without overwriting it.

    An optional source must match its full file_preservation.observation proof.
    Clone support affects physical storage only; source drift is always an error.
    """
    if (source is None) != (expected_source is None):
        raise ValueError('source and expected_source must be provided together')
    if source is None:
        with parent_directory(target, create=True) as directory:
            _snapshot(directory, target, payload, mode, None, None, None, None)
        return
    with parent_directory(source) as source_directory:
        try:
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=source_directory)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                raise _conflict(source, 'snapshot source disappeared or changed type') from exc
            raise
        try:
            _check_source(fd, source_directory, source, expected_source)
            with parent_directory(target, create=True) as directory:
                _snapshot(directory, target, payload, mode, fd, source_directory, source, expected_source)
        finally:
            os.close(fd)
