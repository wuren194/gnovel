"""Directory access under sandbox grants and concurrent path replacement."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.core import file_preservation
from scripts.core.structure_store import StructureError


class FilePreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'book'
        self.root.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.sentinel = self.outside / 'file.txt'
        self.sentinel.write_bytes(b'outside-original')

    def restricted_ancestor(self):
        real_open = os.open

        def open_descendant(path, *args, **kwargs):
            # The ancestor may be traversed, but opening that directory itself
            # for reading is denied, as with a subtree-only Seatbelt grant.
            if os.fspath(path) in (str(self.base), self.base.name):
                raise PermissionError(errno.EPERM, 'ancestor read denied', os.fspath(path))
            return real_open(path, *args, **kwargs)

        return patch.object(file_preservation.os, 'open', side_effect=open_descendant)

    @staticmethod
    def write_at(directory, name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    @unittest.skipUnless(getattr(os, 'O_NOFOLLOW_ANY', 0), 'requires whole-path nofollow')
    def test_observation_with_unreadable_ancestor(self):
        target = self.root / 'file.txt'
        target.write_bytes(b'project-original')
        with self.restricted_ancestor():
            info, data = file_preservation.observation(target)
        self.assertEqual(data, b'project-original')
        self.assertTrue(info['exists'])
        self.assertEqual(info['ino'], target.stat().st_ino)

    @unittest.skipUnless(getattr(os, 'O_NOFOLLOW_ANY', 0), 'requires whole-path nofollow')
    def test_create_nested_parent_with_unreadable_ancestor(self):
        target = self.root / '.govel' / 'transactions' / 'new' / 'file.txt'
        with self.restricted_ancestor():
            with file_preservation.parent_directory(target, create=True) as directory:
                self.write_at(directory, target.name, b'created')
        self.assertEqual(target.read_bytes(), b'created')
        self.assertEqual((self.root / '.govel').stat().st_mode & 0o777, 0o700)

    def test_parent_path_symlinks_are_rejected_without_external_changes(self):
        nested = self.outside / 'inside'
        nested.mkdir()
        (nested / 'file.txt').write_bytes(b'nested-original')
        (self.root / 'ancestor-link').symlink_to(self.outside, target_is_directory=True)
        (self.root / 'parent-link').symlink_to(nested, target_is_directory=True)
        missing = self.base / 'missing-external'
        (self.root / 'broken-link').symlink_to(missing, target_is_directory=True)
        targets = (self.root / 'ancestor-link' / 'inside' / 'file.txt',
                   self.root / 'parent-link' / 'file.txt',
                   self.root / 'broken-link' / 'inside' / 'file.txt')
        for target in targets:
            for create in (False, True):
                with self.subTest(target=target, create=create):
                    with self.assertRaises(OSError) as caught:
                        with file_preservation.parent_directory(target, create=create):
                            self.fail('symlinked parent was opened')
                    self.assertIn(caught.exception.errno, (errno.ELOOP, errno.ENOTDIR))
        self.assertEqual(self.sentinel.read_bytes(), b'outside-original')
        self.assertEqual((nested / 'file.txt').read_bytes(), b'nested-original')
        self.assertFalse(missing.exists())

    def test_observation_rejects_leaf_symlink(self):
        link = self.root / 'file.txt'
        link.symlink_to(self.sentinel)
        with self.assertRaises(OSError) as caught:
            file_preservation.observation(link)
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertEqual(self.sentinel.read_bytes(), b'outside-original')

    def test_symlink_inserted_during_creation_cannot_redirect_writes(self):
        target = self.root / 'new' / 'nested' / 'file.txt'
        real_mkdir = os.mkdir

        def competing_mkdir(path, mode=0o777, *, dir_fd=None):
            if os.fspath(path) == 'new' and dir_fd is not None:
                os.symlink(self.outside, path, dir_fd=dir_fd)
                raise FileExistsError(errno.EEXIST, 'created by another process', path)
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with patch.object(file_preservation.os, 'mkdir', side_effect=competing_mkdir):
            with self.assertRaises(OSError) as caught:
                with file_preservation.parent_directory(target, create=True) as directory:
                    self.write_at(directory, target.name, b'wrong destination')
        self.assertIn(caught.exception.errno, (errno.ELOOP, errno.ENOTDIR))
        self.assertFalse((self.outside / 'nested').exists())
        self.assertEqual(self.sentinel.read_bytes(), b'outside-original')

    def test_directory_descriptor_stays_bound_after_path_replacement(self):
        parent = self.root / 'held'
        parent.mkdir()
        target = parent / 'file.txt'
        target.write_bytes(b'held-original')
        original_inode = parent.stat().st_ino
        with file_preservation.parent_directory(target) as directory:
            parent.rename(self.base / 'displaced')
            parent.symlink_to(self.outside, target_is_directory=True)
            self.assertEqual(os.fstat(directory).st_ino, original_inode)
            fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                self.assertEqual(os.read(fd, 100), b'held-original')
            finally:
                os.close(fd)
        with self.assertRaises(OSError) as caught:
            os.fstat(directory)
        self.assertEqual(caught.exception.errno, errno.EBADF)
        self.assertEqual(self.sentinel.read_bytes(), b'outside-original')

    @unittest.skipUnless(getattr(os, 'O_NOFOLLOW_ANY', 0), 'requires whole-path nofollow')
    def test_permission_error_is_not_bypassed_by_opening_an_ancestor(self):
        target = self.root / 'denied' / 'file.txt'
        target.parent.mkdir()
        real_open = os.open

        def denied_parent(path, *args, **kwargs):
            if os.fspath(path) == str(target.parent):
                raise PermissionError(errno.EACCES, 'parent denied', os.fspath(path))
            return real_open(path, *args, **kwargs)

        with patch.object(file_preservation.os, 'open', side_effect=denied_parent):
            with self.assertRaises(PermissionError) as caught:
                with file_preservation.parent_directory(target, create=True) as directory:
                    self.write_at(directory, target.name, b'bypassed')
        self.assertEqual(caught.exception.errno, errno.EACCES)
        self.assertFalse(target.exists())

    def test_regular_file_parent_is_not_treated_as_missing_directory(self):
        parent = self.root / 'file-parent'
        parent.write_bytes(b'not-a-directory')
        with self.assertRaises(NotADirectoryError):
            with file_preservation.parent_directory(parent / 'nested' / 'file.txt', create=True):
                self.fail('regular file was accepted as a parent directory')
        self.assertEqual(parent.read_bytes(), b'not-a-directory')

    def test_missing_observation_does_not_create_directories(self):
        for target in (self.root / 'absent.txt', self.root / 'missing' / 'nested' / 'absent.txt'):
            with self.subTest(target=target):
                info, data = file_preservation.observation(target)
                self.assertFalse(info['exists'])
                self.assertIsNone(data)
                self.assertFalse(target.exists())
        self.assertFalse((self.root / 'missing').exists())

    def test_fallback_creates_directories_and_rejects_symlink_ancestors(self):
        target = self.root / 'fallback' / 'nested' / 'file.txt'
        with patch.object(file_preservation.os, 'O_NOFOLLOW_ANY', 0, create=True):
            with file_preservation.parent_directory(target, create=True) as directory:
                self.write_at(directory, target.name, b'fallback-created')
            self.assertEqual(file_preservation.observation(target)[1], b'fallback-created')
            (self.root / 'link').symlink_to(self.outside, target_is_directory=True)
            with self.assertRaises(OSError) as caught:
                with file_preservation.parent_directory(self.root / 'link' / 'new' / 'file.txt', create=True):
                    self.fail('fallback followed a symlink')
            self.assertIn(caught.exception.errno, (errno.ELOOP, errno.ENOTDIR))
        self.assertFalse((self.outside / 'new').exists())
        self.assertEqual(self.sentinel.read_bytes(), b'outside-original')

    def test_relative_paths_remain_rejected(self):
        for create in (False, True):
            with self.subTest(create=create):
                with self.assertRaises(FileNotFoundError):
                    with file_preservation.parent_directory(Path('relative/file.txt'), create=create):
                        self.fail('relative path was accepted')

    def test_exclusive_move_keeps_the_original_inode_and_open_descriptor(self):
        source = self.root / 'original'
        source.write_bytes(b'original')
        target = self.root / 'nested' / 'published'
        with source.open('r+b') as held:
            inode = os.fstat(held.fileno()).st_ino
            file_preservation.exclusive_move(source, target)
            self.assertFalse(source.exists())
            self.assertEqual(target.stat().st_ino, inode)
            held.write(b'late-save')
            held.flush()
            self.assertEqual(target.read_bytes(), b'late-save')

    def test_exclusive_move_never_overwrites_any_existing_destination(self):
        source = self.root / 'original'
        source.write_bytes(b'original')
        inode = source.stat().st_ino
        regular, link, directory = (self.root / name for name in ('regular', 'link', 'directory'))
        regular.write_bytes(b'occupant')
        link.symlink_to(self.sentinel)
        directory.mkdir()
        for target in (regular, link, directory):
            with self.subTest(target=target):
                with self.assertRaises(FileExistsError):
                    file_preservation.exclusive_move(source, target)
                self.assertEqual(source.read_bytes(), b'original')
                self.assertEqual(source.stat().st_ino, inode)
        self.assertEqual(regular.read_bytes(), b'occupant')
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.sentinel.read_bytes(), b'outside-original')
        self.assertTrue(directory.is_dir())

    def test_competing_exclusive_moves_have_one_winner_and_keep_the_loser(self):
        sources = [self.root / name for name in ('first', 'second')]
        for source in sources:
            source.write_bytes(source.name.encode())
        target = self.root / 'destination'
        barrier = threading.Barrier(2)
        outcomes = []

        def move(source):
            barrier.wait(timeout=5)
            try:
                file_preservation.exclusive_move(source, target)
            except OSError as exc:
                outcomes.append((source, exc.errno))
            else:
                outcomes.append((source, None))

        workers = [threading.Thread(target=move, args=(source,)) for source in sources]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 2)
        winner = [source for source, error in outcomes if error is None]
        loser = [source for source, error in outcomes if error == errno.EEXIST]
        self.assertEqual(len(winner), 1)
        self.assertEqual(len(loser), 1)
        self.assertFalse(winner[0].exists())
        self.assertEqual(target.read_bytes(), winner[0].name.encode())
        self.assertEqual(loser[0].read_bytes(), loser[0].name.encode())

    def test_native_unsupported_rename_keeps_source_and_reports_capability(self):
        source = self.root / 'original'
        source.write_bytes(b'original')
        target = self.root / 'destination'
        for system, name, flag in (('Darwin', 'renameatx_np', 4), ('Linux', 'renameat2', 1)):
            for code in sorted({errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}):
                with self.subTest(system=system, errno=code):
                    native = Mock(return_value=-1)
                    library = SimpleNamespace(**{name: native})
                    with patch.object(file_preservation.platform, 'system', return_value=system), \
                            patch.object(file_preservation.ctypes, 'CDLL', return_value=library), \
                            patch.object(file_preservation.ctypes, 'get_errno', return_value=code):
                        with self.assertRaises(StructureError) as caught:
                            file_preservation.exclusive_move(source, target)
                    self.assertEqual(caught.exception.code, 'SAFE_PUBLICATION_UNAVAILABLE')
                    self.assertEqual(caught.exception.details['primitive'], name)
                    self.assertEqual(caught.exception.details['errno'], code)
                    args = native.call_args.args
                    self.assertEqual((args[1], args[3], args[4]), (b'original', b'destination', flag))
                    self.assertIsInstance(args[0], int)
                    self.assertIsInstance(args[2], int)
                    self.assertEqual(source.read_bytes(), b'original')
                    self.assertFalse(target.exists())

    def test_missing_native_primitive_fails_before_creating_target_directories(self):
        source = self.root / 'original'
        source.write_bytes(b'original')
        target = self.root / 'missing' / 'destination'
        for system in ('Darwin', 'Linux', 'Windows'):
            with self.subTest(system=system), \
                    patch.object(file_preservation.platform, 'system', return_value=system), \
                    patch.object(file_preservation.ctypes, 'CDLL', return_value=SimpleNamespace()):
                with self.assertRaises(StructureError) as caught:
                    file_preservation.exclusive_move(source, target)
                self.assertEqual(caught.exception.code, 'SAFE_PUBLICATION_UNAVAILABLE')
                self.assertEqual(source.read_bytes(), b'original')
                self.assertFalse(target.parent.exists())

    def test_native_permission_denial_is_not_reported_as_missing_capability(self):
        source = self.root / 'original'
        source.write_bytes(b'original')
        target = self.root / 'destination'
        with patch.object(file_preservation, '_exclusive_rename', return_value=(Mock(return_value=-1), 1, 'renameat2')), \
                patch.object(file_preservation.ctypes, 'get_errno', return_value=errno.EACCES):
            with self.assertRaises(PermissionError):
                file_preservation.exclusive_move(source, target)
        self.assertEqual(source.read_bytes(), b'original')
        self.assertFalse(target.exists())
