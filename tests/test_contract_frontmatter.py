"""Contract frontmatter must be complete and version-aligned.

动机：`compatibilityVersion` 曾在 16 份契约里停在 4、manifest 已到 5，无人发现——
因为该字段零消费者；同期 `G-RELATIONSHIP-MOTIVES.md` 落盘却完全没有
frontmatter，也不在 contractsList 里，任何工位都装载不到。本测试给这两个字段
建立消费者：字段存在即被校验，漂移即红测试。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = ROOT / "shared" / "contracts"
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def parse_frontmatter(path: Path) -> dict[str, str]:
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class ContractFrontmatterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contracts = sorted(CONTRACT_DIR.glob("*.md"))
        self.manifest = json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(self.contracts, "契约目录为空")

    def test_every_contract_has_complete_frontmatter(self) -> None:
        for path in self.contracts:
            fields = parse_frontmatter(path)
            with self.subTest(contract=path.name):
                self.assertTrue(fields, "缺少 frontmatter")
                for key in ("id", "version", "compatibilityVersion", "description"):
                    self.assertIn(key, fields, f"缺少 {key}")
                self.assertRegex(fields["version"], r"^\d+\.\d+\.\d+$", "version 必须是 x.y.z")
                self.assertEqual(fields["id"], path.stem, "id 必须与文件名一致")
                self.assertTrue(fields["description"], "description 不得为空")

    def test_compatibility_version_matches_manifest(self) -> None:
        expected = str(self.manifest["compatibilityVersion"])
        for path in self.contracts:
            fields = parse_frontmatter(path)
            with self.subTest(contract=path.name):
                self.assertEqual(
                    fields.get("compatibilityVersion"),
                    expected,
                    "契约 compatibilityVersion 必须与 manifest 一致；"
                    "协议大版本变更时全部契约同步提升",
                )

    def test_every_contract_on_disk_is_declared(self) -> None:
        listed = set(self.manifest["sharedProtocols"]["contractsList"])
        on_disk = {path.name for path in self.contracts}
        self.assertEqual(on_disk - listed, set(), "落盘契约未登记 contractsList，任何工位都装载不到")
        self.assertEqual(listed - on_disk, set(), "contractsList 声明了不存在的契约")

    def test_description_is_a_summary_not_a_changelog(self) -> None:
        """description 曾被当成版本台账用（635 字符、串联 14 条变更）。"""
        for path in self.contracts:
            fields = parse_frontmatter(path)
            with self.subTest(contract=path.name):
                self.assertLess(len(fields.get("description", "")), 300, "description 过长，历史应移入沿革文件")


if __name__ == "__main__":
    unittest.main()
