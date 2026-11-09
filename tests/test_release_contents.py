from __future__ import annotations

import subprocess
import tempfile
import unittest
import zipfile
import stat
from pathlib import Path

import scripts.check_release_contents as checker
from scripts.check_release_contents import check_paths, check_worktree, check_zip


class ReleaseContentsTests(unittest.TestCase):
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
