"""Every `novelctl` command named in active docs must exist in the parser.

动机：文档曾长期引用两条已删除/从未实现的命令——`project replan`（planner/router
SKILL 与 CHAPTER-PIPELINE 的重规划唯一入口）和 `project sync-status`（会被
quick-init 写进每个新项目的 AGENTS.md）。照文档执行必然报错，而人工同步没拦住。
本测试从 argparse 反射真实命令集，扫描活动文档比对，把这类漂移变成红测试。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from novelctl import build_parser  # noqa: E402

# 活动文档：入口、工位 SKILL、契约、被复制进项目的模板、仓库文档。
DOC_GLOBS = (
    "SKILL.md",
    "README.md", "AGENTS.md",
    "govel-*/SKILL.md",
    "govel-*/references/**/*.md",
    "shared/contracts/*.md",
    "shared/templates/**/*.md",
    "docs/**/*.md",
    ".agents/skills/**/*.md",
    ".claude/**/*.md",
)

# Exclusions are bounded history locations, never a filename-prefix exemption
# that silently excludes the next active guide added to docs/.
HISTORICAL_DIRECTORIES = {
    'docs/archive': 'Retired designs and preserved original reports.',
    'docs/contract-history': 'Version histories, excluded from runtime loading.',
    'docs/drafts': 'Unapproved proposals and the product-intent discussion transcript.',
    'docs/audit-evidence': 'Immutable historical audit evidence snapshots.',
}
HISTORICAL_FILES = {
    'docs/ROUND3-CONTEXT-REALITY.md': 'Frozen Round 3 evidence at d3f71ca, explicitly scoped in its introduction.',
    'docs/ROUND4-WORKFLOW-REALITY.md': 'Frozen Round 4 evidence at c763b04, not a current command guide.',
    'docs/ROUND5-EXPERIMENT-PLAN.md': 'Frozen Round 5 experimental protocol, not a current authoring entrypoint.',
    'docs/ROUND6-KNOWLEDGE-REALITY.md': 'Historical Round 6 implementation/experiment evidence.',
}

# Horizontal whitespace only. A following shell command on another line must
# never become a suffix of the preceding invocation.
INVOCATION_RE = re.compile(r"\bnovelctl(?:\.py)?((?:[ \t]+[a-z][a-z0-9-]*)+)")


def documented_invocations(text):
    """Return (starting line, command prefix), joining only shell backslashes."""
    logical, start = '', 1
    for number, line in enumerate(text.splitlines(), 1):
        if not logical:
            start = number
        continued = line.rstrip().endswith('\\')
        logical += line.rstrip()[:-1] + ' ' if continued else line
        if continued:
            continue
        for match in INVOCATION_RE.finditer(logical):
            yield start, ' '.join(match.group(1).split())
        logical = ''
    if logical:
        for match in INVOCATION_RE.finditer(logical):
            yield start, ' '.join(match.group(1).split())


def real_commands() -> set[str]:
    def walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...]) -> set[str]:
        groups = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if not groups:
            return {" ".join(prefix)} if prefix else set()
        found: set[str] = set()
        for action in groups:
            for name, subparser in action._name_parser_map.items():
                found |= walk(subparser, prefix + (name,))
        return found

    return walk(build_parser(), ())


def active_docs(root: Path = ROOT) -> list[Path]:
    paths: list[Path] = []
    for pattern in DOC_GLOBS:
        paths.extend(sorted(root.glob(pattern)))
    return sorted({path for path in paths if path.is_file()
                   and str(path.relative_to(root)) not in HISTORICAL_FILES
                   and not any(path.relative_to(root).is_relative_to(prefix)
                               for prefix in HISTORICAL_DIRECTORIES)
                   and 'archive' not in path.relative_to(root).parts})


class DocumentedCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commands = real_commands()
        # 每个真实命令的所有前缀都是合法书写（如只提到 `novelctl project structure`）。
        self.prefixes = {
            " ".join(parts[:size])
            for command in self.commands
            for parts in [command.split()]
            for size in range(1, len(parts) + 1)
        }

    def test_parser_exposes_expected_surface(self) -> None:
        for expected in ("project quick-init", "project doctor", "project state rebuild"):
            self.assertIn(expected, self.commands, f"CLI 丢失命令：{expected}")

    def test_removed_commands_are_not_documented(self) -> None:
        """两条历史假命令的显式回退锁。"""
        for stale in ("project replan", "project sync-status"):
            self.assertNotIn(stale, self.commands, f"{stale} 不该存在于 CLI")
            for doc in active_docs():
                self.assertNotIn(
                    f"novelctl {stale}",
                    doc.read_text(encoding="utf-8"),
                    f"{doc.relative_to(ROOT)} 引用了不存在的 novelctl {stale}",
                )

    def test_unknown_suffix_cannot_hide_behind_a_valid_prefix(self) -> None:
        from unittest.mock import patch
        with patch.object(sys.modules[__name__], "active_docs", return_value=[ROOT / "README.md"]):
            for command in ("project missing", "project workflow nonexistent", "project state rebuild nonexistent"):
                with self.subTest(command=command), patch.object(Path, "read_text", return_value=f"`novelctl {command}`"):
                    with self.assertRaises(AssertionError):
                        self.test_every_documented_invocation_resolves()
            with patch.object(Path, "read_text", return_value="`novelctl project structure`"):
                self.test_every_documented_invocation_resolves()

    def test_current_project_template_is_scanned_and_archive_is_excluded(self) -> None:
        self.assertIn(ROOT / "shared/templates/agents-template.md", active_docs())
        self.assertFalse(any('archive' in path.parts for path in active_docs()))
        self.assertFalse((ROOT / "shared/templates/project").exists())

    def test_nested_active_guides_are_discovered_without_registering_each_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = root / 'docs/new/nested/ROUND-next-guide.md'
            history = root / 'docs/archive/old/guide.md'
            for path in (active, history):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('`novelctl project missing`')
            self.assertIn(active, active_docs(root))
            self.assertNotIn(history, active_docs(root))
            self.assertNotIn(next(documented_invocations(active.read_text()))[1], self.prefixes)

    def test_newlines_quotes_and_explicit_shell_continuations(self):
        text = ('python3 scripts/novelctl.py project doctor\n'
                'python3 scripts/other.py\n'
                'python3 scripts/novelctl.py project \\\n'
                '  candidate list-versions --chapter-id CH-1\n'
                '`novelctl project structure` then `novelctl project workflow`\n')
        self.assertEqual(list(documented_invocations(text)),
                         [(1, 'project doctor'), (3, 'project candidate list-versions'),
                          (5, 'project structure'), (5, 'project workflow')])

    def test_five_frontend_examples_need_project_prefix(self):
        for command in ('candidate save-version', 'candidate activate-version', 'candidate list-versions',
                        'handoff create', 'workspace summary'):
            self.assertNotIn(command, self.prefixes)
            self.assertIn('project ' + command, self.commands)

    def test_every_documented_invocation_resolves(self) -> None:
        unknown: list[str] = []
        for doc in active_docs():
            text = doc.read_text(encoding="utf-8")
            for line, candidate in documented_invocations(text):
                if candidate not in self.prefixes:
                    unknown.append(f"{doc.relative_to(ROOT)}:{line}: novelctl {candidate}")
        self.assertEqual(unknown, [], "文档引用了不存在的 novelctl 命令")


if __name__ == "__main__":
    unittest.main()
