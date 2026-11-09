from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.core.paths import ProjectPathError, safe_project_path


class ProjectPathTests(unittest.TestCase):
    def test_relative_path_stays_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(safe_project_path(root, "正文/CH-ONE.md"), (root / "正文/CH-ONE.md").resolve())

    def test_parent_escape_and_absolute_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "book"
            root.mkdir()
            with self.assertRaises(ProjectPathError) as caught:
                safe_project_path(root, "../outside.md")
            self.assertEqual(caught.exception.code, "PATH_OUTSIDE_PROJECT")
            with self.assertRaises(ProjectPathError) as caught:
                safe_project_path(root, Path(temp) / "outside.md")
            self.assertEqual(caught.exception.code, "ABSOLUTE_PATH_FORBIDDEN")

    def test_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "book"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "正文").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ProjectPathError) as caught:
                safe_project_path(root, "正文/CH-ONE.md")
            self.assertIn(caught.exception.code, {"PATH_OUTSIDE_PROJECT", "SYMLINK_PATH_FORBIDDEN"})

    def test_symlink_check_holds_when_root_is_aliased(self) -> None:
        """root 经符号链接访问时，逐段符号链接检查不得整段跳过（机制 36 审查的 4a 形态）。

        macOS 上 /var 与 /private/var 是同一目录的两种写法：经链接形态的 root +
        未 resolve 的绝对路径会让 `relative_to(base)` 抛 ValueError，旧实现据此
        跳过整段逐段检查，内链（工作台 -> 正文）因此不被拦。
        """
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real = base / "real"
            real.mkdir()
            link = base / "link"
            link.symlink_to(real, target_is_directory=True)
            project = real / "book"
            (project / "正文").mkdir(parents=True)
            (project / "正文" / "CH-ONE.md").write_text("x", encoding="utf-8")
            (project / "工作台").symlink_to(project / "正文", target_is_directory=True)

            aliased_root = link / "book"
            with self.assertRaises(ProjectPathError) as caught:
                safe_project_path(aliased_root, aliased_root / "工作台" / "CH-ONE.md", allow_absolute=True)
            self.assertEqual(caught.exception.code, "SYMLINK_PATH_FORBIDDEN")
            # 对照：同 root 下的普通文件仍应通过（不误伤链接祖先本身）
            resolved = safe_project_path(aliased_root, aliased_root / "正文" / "CH-ONE.md", allow_absolute=True)
            self.assertEqual(resolved, (project / "正文" / "CH-ONE.md").resolve())


if __name__ == "__main__":
    unittest.main()
