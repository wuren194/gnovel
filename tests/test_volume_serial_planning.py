"""Regression locks for volume-first serial planning.

The policy lives in CHAPTER-PIPELINE.md. Station Skills only keep the parts unique
to their role and point to that contract.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "shared/contracts/CHAPTER-PIPELINE.md"


class VolumeSerialPlanningTests(unittest.TestCase):
    def test_pipeline_locks_volume_serial_mode(self) -> None:
        text = PIPELINE.read_text(encoding="utf-8")
        for phrase in (
            "当前卷",
            "作者批准范围",
            "稳定事实源",
            "普通正式续写以稳定前章、连续性和事实交接完成为前提",
            "仅委托整批候选时",
            "不提前定稿解锁后章",
            "唯一 `nextAction`",
            "不自动扩大范围或跨卷",
        ):
            self.assertIn(phrase, text, f"章节流水线缺少卷化串行锁：{phrase}")

    def test_router_points_to_pipeline_and_recovery_state(self) -> None:
        text = (ROOT / "gnovel-router/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("CHAPTER-PIPELINE.md", text)
        self.assertIn("批准范围", text)
        self.assertIn("唯一 `nextAction`", text)
        self.assertNotIn("最近提交", text)

    def test_planner_keeps_role_specific_volume_boundary(self) -> None:
        text = (ROOT / "gnovel-planner/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("只对当前卷做 hard planning", text)
        self.assertIn("普通正式续写与约定候选批次分别遵守", text)
        self.assertIn("不为续写候选提前正式定稿", text)
        self.assertIn("CHAPTER-PIPELINE.md", text)

    def test_no_ch001_arc_exemption_anywhere(self) -> None:
        planner = (ROOT / "gnovel-planner/SKILL.md").read_text(encoding="utf-8")
        pipeline = PIPELINE.read_text(encoding="utf-8")
        for false_rule in (
            "确实是故事起点且无段落意图才保持空壳",
            "第一章开写前保持空壳",
            "保持空壳，逐章推演定位",
        ):
            self.assertNotIn(false_rule, planner)
            self.assertNotIn(false_rule, pipeline)
        self.assertIn("CH001 不豁免弧线前置", pipeline)
        self.assertIn("CH001 前必须存在已填实的 ARC001", planner)

    def test_composer_mode_does_not_exempt_first_arc(self) -> None:
        text = (ROOT / "gnovel-composer/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("当前卷弧线", text)
        self.assertIn("不提前硬规划未来卷", text)

    def test_manifest_has_no_transaction_approval_command(self) -> None:
        raw = json.dumps(
            json.loads((ROOT / "toolbox.manifest.json").read_text(encoding="utf-8")),
            ensure_ascii=False,
        )
        self.assertNotIn("approve-and-commit", raw)
        self.assertNotIn("AWAITING_COMMIT_APPROVAL", raw)


if __name__ == "__main__":
    unittest.main()
