from __future__ import annotations

import subprocess
import tempfile
import unittest
import zipfile
import stat
from pathlib import Path
from unittest.mock import patch

import scripts.check_release_contents as checker
import scripts.package_toolbox as packager
from scripts.check_release_contents import check_paths, check_worktree, check_zip


class ReleaseContentsTests(unittest.TestCase):
    def test_local_data_stays_untracked_and_out_of_packages_even_after_force_add(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / '.gitignore').write_bytes((checker.TOOLBOX_ROOT / '.gitignore').read_bytes())
            (root / 'README.md').write_text('Example source')
            private = ['local/knowledge/assets/example.md', 'local/materials/seat.md',
                       'local/provider.json', 'local/roundtable.md', 'local/bin/govel']
            public = ['local/README.md', 'local/knowledge/.gitkeep']
            for name in public:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('Local layout instructions' if name.endswith('README.md') else '')
            for name in private:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('LOCAL_PRIVATE_MARKER')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
            self.assertFalse(set(private) & set(tracked))
            self.assertTrue(set(public) <= set(tracked))
            with patch.object(checker, 'TOOLBOX_ROOT', root), patch.object(packager, 'TOOLBOX_ROOT', root):
                self.assertTrue(checker.check_worktree()['ok'])
                subprocess.run(['git', 'add', '-f', *private], cwd=root, check=True)
                self.assertFalse(checker.check_worktree()['ok'])
                collected = packager.collect()
                self.assertFalse(set(private) & set(collected['packaged']))
                self.assertTrue(set(private) <= set(collected['excluded']))
                self.assertTrue(set(public) <= set(collected['packaged']))
                manifest = packager.build_manifest(mode='zip', local_paths={})
                archive = root / 'release.zip'
                packager.build_zip(archive, manifest)
                self.assertTrue(checker.check_zip(archive)['ok'])
                with zipfile.ZipFile(archive) as output:
                    self.assertFalse(set(private) & set(output.namelist()))
                    self.assertTrue(set(public) <= set(output.namelist()))
                    self.assertEqual(output.read('local/knowledge/.gitkeep'), b'')
                    self.assertNotIn(b'LOCAL_PRIVATE_MARKER', b''.join(output.read(name) for name in output.namelist()))
                with zipfile.ZipFile(archive, 'a') as output:
                    output.writestr(private[0], 'LOCAL_PRIVATE_MARKER')
                self.assertFalse(checker.check_zip(archive)['ok'])
                self.assertFalse(packager.check(root / 'release.zip.MANIFEST.json', archive)['ok'])

    def test_public_placeholder_cannot_carry_private_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            name = 'local/knowledge/.gitkeep'
            target = root / name
            target.parent.mkdir(parents=True)
            target.write_text('PRIVATE CONTENT')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            with patch.object(checker, 'TOOLBOX_ROOT', root), patch.object(packager, 'TOOLBOX_ROOT', root):
                self.assertFalse(checker.check_worktree()['ok'])
                self.assertNotIn(name, packager.collect()['packaged'])
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr(name, 'PRIVATE CONTENT')
            self.assertIn('local_placeholder_not_empty', {row['code'] for row in checker.check_zip(archive)['problems']})

    def test_private_originals_and_alias_are_excluded_from_worktree(self):
        result = check_worktree()
        self.assertTrue(result['ok'], result['problems'])

    def test_unicode_collision_and_private_zip_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('knowledge/sources/private/reference/raw.txt', 'private')
                archive.writestr('café.txt', 'one')
                archive.writestr('cafe\u0301.txt', 'two')
            result = check_zip(path)
        codes = {row['code'] for row in result['problems']}
        self.assertIn('private_source', codes)
        self.assertIn('unicode_path_collision', codes)

    def test_public_mode_requires_license_clearance(self):
        result = check_paths(['README.md', 'knowledge/sources/legacy/example.md'], public=True)
        self.assertEqual({row['code'] for row in result['problems']},
                         {'root_license_missing', 'source_rights_not_cleared'})

    def test_archive_unsafe_worktree_symlinks_are_flagged_like_the_packager(self):
        """同一棵树在 worktree 校验入口与打包入口必须得到同一判定。

        机制版本 95 前 worktree 入口按 resolve() 判定：绝对路径但解析回仓库内的
        链接会被放行，随后打包器 collect()（旧版）把它打进包、--check 又拒绝。
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "note.md").write_text("正文", encoding="utf-8")
            (root / "alias-abs.md").symlink_to(root / "note.md")
            (root / "alias-missing.md").symlink_to("gone.md")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            original = checker.TOOLBOX_ROOT
            checker.TOOLBOX_ROOT = root
            try:
                report = checker.check_worktree()
            finally:
                checker.TOOLBOX_ROOT = original
        self.assertFalse(report["ok"], report["problems"])
        flagged = {row["path"]: row["code"] for row in report["problems"]}
        self.assertEqual(flagged.get("alias-abs.md"), "unsafe_archive_symlink",
                         "绝对路径软链必须在校验入口被拦下，而不是先打入包再被 --check 拒绝")
        self.assertEqual(flagged.get("alias-missing.md"), "unsafe_archive_symlink")

    def test_credentials_duplicate_entries_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'unsafe.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('README.md', 'one')
                archive.writestr('README.md', 'two')
                archive.writestr('.env', 'secret')
                link = zipfile.ZipInfo('references/raw')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, '../../outside')
            report = check_zip(path)
        codes = {row['code'] for row in report['problems']}
        self.assertTrue({'credential_candidate', 'duplicate_archive_path', 'unsafe_archive_symlink'} <= codes)
