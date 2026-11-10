"""Independent CoW images, source drift, and fail-closed snapshot publication."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.core import snapshot_files
from scripts.core.file_preservation import observation
from scripts.core.structure_store import StructureError


class SnapshotFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.write_bytes(b'A' * 32768 + b'original tail')
        self.expected, self.payload = observation(self.source)
        self.target = self.root / 'images' / 'snapshot'

    def write(self, payload=None, **kwargs):
        snapshot_files.write_snapshot(self.target, self.payload if payload is None else payload,
                                      mode=kwargs.pop('mode', 0o600), source=self.source,
                                      expected_source=self.expected, **kwargs)

    def assert_unpublished(self):
        self.assertFalse(self.target.exists())
        if self.target.parent.exists():
            self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_new_snapshot_exact_bytes_and_mode(self):
        snapshot_files.write_snapshot(self.target, b'new\x00content', mode=0o640)
        self.assertEqual(self.target.read_bytes(), b'new\x00content')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)

    def test_new_files_are_written_in_large_blocks_without_empty_block_reads(self):
        payload = b'new content' * 200000
        with patch.object(snapshot_files.os, 'pwrite', wraps=os.pwrite) as write, \
                patch.object(snapshot_files.os, 'pread', wraps=os.pread) as read:
            snapshot_files.write_snapshot(self.target, payload, mode=0o600)
        self.assertLess(write.call_count, 10)
        self.assertLess(read.call_count, 10)  # Final hash only, not reads of empty per-block data.
        self.assertEqual(self.target.read_bytes(), payload)

    def test_partial_writes_are_completed_and_zero_progress_aborts(self):
        write = os.pwrite

        def short_write(fd, data, offset):
            return write(fd, data[:3], offset)

        with patch.object(snapshot_files.os, 'pwrite', side_effect=short_write):
            snapshot_files.write_snapshot(self.target, b'complete contents', mode=0o600)
        self.assertEqual(self.target.read_bytes(), b'complete contents')
        self.target = self.root / 'stalled' / 'snapshot'
        with patch.object(snapshot_files.os, 'pwrite', return_value=0):
            with self.assertRaises(OSError) as caught:
                snapshot_files.write_snapshot(self.target, b'contents', mode=0o600)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assert_unpublished()

    def test_shorter_longer_and_equal_length_changes_are_complete(self):
        replacements = (b'', b'short', b'B' + self.payload[1:], self.payload + b'longer' * 1200)
        for number, payload in enumerate(replacements):
            with self.subTest(length=len(payload)):
                self.target = self.root / str(number) / 'snapshot'
                self.write(payload, mode=0o440)
                self.assertEqual(self.target.read_bytes(), payload)
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o440)
                self.assertEqual(self.source.read_bytes(), self.payload)
                self.assertNotEqual(self.target.stat().st_ino, self.source.stat().st_ino)

    def test_read_only_source_can_be_cloned_to_a_writable_image(self):
        self.source.chmod(0o400)
        self.expected, self.payload = observation(self.source)
        self.write(b'changed', mode=0o600)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.target.read_bytes(), b'changed')

    def test_late_writes_through_old_source_fd_and_image_fd_are_independent(self):
        with self.source.open('r+b') as held:
            self.write()
            self.assertNotEqual(os.fstat(held.fileno()).st_ino, self.target.stat().st_ino)
            held.write(b'late source save')
            held.flush()
            self.assertEqual(self.target.read_bytes(), self.payload)
            self.target.write_bytes(b'late image write')
            self.assertTrue(self.source.read_bytes().startswith(b'late source save'))

    def test_source_byte_inode_mode_and_type_drift_are_rejected(self):
        mutations = ('bytes', 'inode', 'mode', 'symlink', 'missing')
        for number, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                self.source = self.root / f'source-{number}'
                self.source.write_bytes(b'original')
                self.expected, self.payload = observation(self.source)
                self.target = self.root / str(number) / 'snapshot'
                if mutation == 'bytes':
                    self.source.write_bytes(b'modified')
                elif mutation == 'inode':
                    replacement = self.root / f'replacement-{number}'
                    replacement.write_bytes(self.payload)
                    os.replace(replacement, self.source)
                elif mutation == 'mode':
                    self.source.chmod(0o400)
                elif mutation == 'symlink':
                    self.source.unlink()
                    self.source.symlink_to(self.root / 'source')
                else:
                    self.source.unlink()
                with self.assertRaises(StructureError) as caught:
                    self.write()
                self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
                self.assert_unpublished()

    def test_source_changed_during_clone_is_not_replaced_by_trusted_payload(self):
        clone = snapshot_files._clone_file

        def clone_then_change(*args):
            result = clone(*args)
            self.source.write_bytes(b'late source mutation')
            return result

        with patch.object(snapshot_files, '_clone_file', side_effect=clone_then_change):
            with self.assertRaises(StructureError) as caught:
                self.write()
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertEqual(self.source.read_bytes(), b'late source mutation')
        self.assert_unpublished()

    def test_clone_bytes_are_verified_before_different_payload_is_applied(self):
        def wrong_clone(source_fd, directory, name):
            fd = snapshot_files._create_file(directory, name)
            os.write(fd, b'X' * len(self.payload))
            return fd, True

        with patch.object(snapshot_files, '_clone_file', side_effect=wrong_clone):
            with self.assertRaises(StructureError) as caught:
                self.write(b'new payload')
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assert_unpublished()

    def test_a_hard_link_is_rejected_before_changing_source_bytes_or_mode(self):
        def hard_link(source_fd, directory, name):
            os.link(self.source, name, dst_dir_fd=directory)
            return os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory), True

        with patch.object(snapshot_files, '_clone_file', side_effect=hard_link):
            with self.assertRaises(StructureError) as caught:
                self.write(b'changed', mode=0o400)
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(self.source.stat().st_mode & 0o777, self.expected['mode'])
        self.assert_unpublished()

    def test_linux_unsupported_clone_falls_back_but_io_and_permission_errors_do_not(self):
        native_rename = snapshot_files._exclusive_rename()
        for number, code in enumerate((errno.EOPNOTSUPP, errno.EXDEV, errno.ENOTTY,
                                       errno.EIO, errno.ENOSPC, errno.EACCES)):
            with self.subTest(errno=code):
                self.target = self.root / str(number) / 'snapshot'
                with patch.object(snapshot_files.platform, 'system', return_value='Linux'), \
                        patch.object(snapshot_files, '_exclusive_rename', return_value=native_rename), \
                        patch.object(snapshot_files.fcntl, 'ioctl', side_effect=OSError(code, 'clone failed')) as ioctl:
                    if code in snapshot_files._CLONE_UNAVAILABLE:
                        self.write(b'fallback payload')
                        self.assertEqual(self.target.read_bytes(), b'fallback payload')
                    else:
                        with self.assertRaises(OSError) as caught:
                            self.write()
                        self.assertEqual(caught.exception.errno, code)
                        self.assert_unpublished()
                    self.assertEqual(ioctl.call_args.args[1], 0x40049409)
                self.assertEqual(self.source.read_bytes(), self.payload)

    def test_darwin_missing_or_unsupported_clone_uses_ordinary_copy(self):
        native_rename = snapshot_files._exclusive_rename()
        for number, library in enumerate((SimpleNamespace(), SimpleNamespace(fclonefileat=Mock(return_value=-1)))):
            with self.subTest(case=number):
                self.target = self.root / str(number) / 'snapshot'
                with patch.object(snapshot_files.platform, 'system', return_value='Darwin'), \
                        patch.object(snapshot_files, '_exclusive_rename', return_value=native_rename), \
                        patch.object(snapshot_files.ctypes, 'CDLL', return_value=library), \
                        patch.object(snapshot_files.ctypes, 'get_errno', return_value=errno.ENOTSUP):
                    self.write()
                self.assertEqual(self.target.read_bytes(), self.payload)
                if hasattr(library, 'fclonefileat'):
                    self.assertEqual(library.fclonefileat.call_args.args[3], 2)

    def test_linux_fallback_truncate_failure_closes_its_unpublished_file(self):
        native_rename = snapshot_files._exclusive_rename()
        create = snapshot_files._create_file
        descriptors = []

        def record_fd(*args):
            fd = create(*args)
            descriptors.append(fd)
            return fd

        with patch.object(snapshot_files.platform, 'system', return_value='Linux'), \
                patch.object(snapshot_files, '_exclusive_rename', return_value=native_rename), \
                patch.object(snapshot_files, '_create_file', side_effect=record_fd), \
                patch.object(snapshot_files.fcntl, 'ioctl', side_effect=OSError(errno.ENOTSUP, 'no clone')), \
                patch.object(snapshot_files.os, 'ftruncate', side_effect=OSError(errno.EIO, 'truncate failed')):
            with self.assertRaises(OSError) as caught:
                self.write()
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assert_unpublished()
        for fd in descriptors:
            with self.assertRaises(OSError) as closed:
                os.fstat(fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_chmod_failure_closes_its_unpublished_file(self):
        create = snapshot_files._create_file
        descriptors = []

        def record_fd(*args):
            fd = create(*args)
            descriptors.append(fd)
            return fd

        with patch.object(snapshot_files, '_create_file', side_effect=record_fd), \
                patch.object(snapshot_files.os, 'fchmod', side_effect=OSError(errno.EIO, 'chmod failed')):
            with self.assertRaises(OSError) as caught:
                snapshot_files.write_snapshot(self.target, b'new content', mode=0o600)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assert_unpublished()
        for fd in descriptors:
            with self.assertRaises(OSError) as closed:
                os.fstat(fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_cleanup_failure_still_closes_new_and_linux_clone_descriptors(self):
        native_rename = snapshot_files._exclusive_rename()
        create = snapshot_files._create_file
        for number, cloning in enumerate((False, True)):
            with self.subTest(cloning=cloning):
                self.target = self.root / str(number) / 'snapshot'
                descriptors = []

                def record_fd(*args):
                    fd = create(*args)
                    descriptors.append(fd)
                    return fd

                with patch.object(snapshot_files.platform, 'system', return_value='Linux'), \
                        patch.object(snapshot_files, '_exclusive_rename', return_value=native_rename), \
                        patch.object(snapshot_files, '_create_file', side_effect=record_fd), \
                        patch.object(snapshot_files.fcntl, 'ioctl', side_effect=OSError(errno.EACCES, 'clone failed')), \
                        patch.object(snapshot_files.os, 'fchmod', side_effect=OSError(errno.EACCES, 'chmod failed')), \
                        patch.object(snapshot_files, '_unlink_owned', side_effect=OSError(errno.EIO, 'cleanup failed')):
                    with self.assertRaises(OSError) as caught:
                        if cloning:
                            self.write()
                        else:
                            snapshot_files.write_snapshot(self.target, b'new', mode=0o600)
                self.assertEqual(caught.exception.errno, errno.EIO)
                self.assertFalse(self.target.exists())
                self.assertEqual(self.source.read_bytes(), self.payload)
                for fd in descriptors:
                    with self.assertRaises(OSError) as closed:
                        os.fstat(fd)
                    self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_existing_target_and_symlink_are_never_overwritten(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'occupant')
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(self.target.read_bytes(), b'occupant')
        self.target.unlink()
        self.target.symlink_to(self.source)
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_symlinked_target_parent_cannot_redirect_snapshot_writes(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.target.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError) as caught:
            self.write()
        self.assertIn(caught.exception.errno, {errno.ELOOP, errno.ENOTDIR})
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_final_hash_detects_incorrect_writes(self):
        with patch.object(snapshot_files, '_write_payload', return_value=None):
            with self.assertRaises(StructureError) as caught:
                self.write(b'changed')
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assert_unpublished()

    def test_only_changed_native_blocks_are_written(self):
        clone = snapshot_files._clone_file
        cloned = []

        def observed_clone(*args):
            result = clone(*args)
            cloned.append(result[1])
            return result

        with patch.object(snapshot_files, '_clone_file', side_effect=observed_clone), \
                patch.object(snapshot_files.os, 'pwrite', wraps=os.pwrite) as write:
            self.write(b'B' + self.payload[1:])
        if not cloned[0]:
            self.skipTest('the test filesystem does not support native CoW cloning')
        self.assertEqual(write.call_count, 1)
        self.assertLess(len(write.call_args.args[1]), len(self.payload))
        self.assertEqual(self.target.read_bytes(), b'B' + self.payload[1:])

    def test_cleanup_does_not_remove_a_replacement_temporary_inode(self):
        rewrite = snapshot_files._write_payload
        replacement = []

        def replace_temporary(fd, payload, **kwargs):
            temporary = next(self.target.parent.glob('.govel-snapshot-*'))
            temporary.rename(self.target.parent / 'held-image')
            temporary.write_bytes(b'foreign file')
            replacement.append(temporary)
            rewrite(fd, payload, **kwargs)

        with patch.object(snapshot_files, '_write_payload', side_effect=replace_temporary):
            with self.assertRaises(StructureError) as caught:
                self.write()
        self.assertEqual(caught.exception.code, 'WRITE_SET_CONFLICT')
        self.assertFalse(self.target.exists())
        self.assertEqual(replacement[0].read_bytes(), b'foreign file')

    def test_source_requires_an_observation_proof(self):
        with self.assertRaises(ValueError):
            snapshot_files.write_snapshot(self.target, self.payload, mode=0o600, source=self.source)
        self.assert_unpublished()
