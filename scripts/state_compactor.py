#!/usr/bin/env python3
"""State compactor for govel long-form StoryState cache projections.

Four-tier compression when item count exceeds threshold:

  Tier 1 — outdated dedup: same (subject, field) outdated entries → keep latest only
  Tier 2 — resolved cleanup: status=resolved/abandoned（含历史 closed/paid_off）foreshadowing → remove
  Tier 3 — timeline summarization: events > 50 chapters old → merge into single fact
  Tier 4 — recency cutoff: still over limit → keep active first, then by recency

Operates on project cache layer (.claude/ or .govel/aux/), NOT on
immutable StoryState objects. Outputs compression report with audit trail.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from core.exit_codes import EXIT_FAIL_CLOSED, FailClosedArgumentParser
    from core.project import state_compaction_policy
except ImportError:  # 作为 scripts 包模块被导入（如 tests）时走包路径
    from scripts.core.exit_codes import EXIT_FAIL_CLOSED, FailClosedArgumentParser
    from scripts.core.project import state_compaction_policy


COMPACTION_THRESHOLD = state_compaction_policy()["threshold"]
OLD_CHAPTER_THRESHOLD = state_compaction_policy()["oldChapterThreshold"]
DATE_FMT = "%Y-%m-%dT%H:%M:%S"

# 伏笔/承诺的存活状态。状态枚举为 open/advanced/resolved/abandoned；
# 缓存历史遗留的 closed/paid_off 按已了结处理。
UNRESOLVED_HOOK_STATUSES = {"open", "advanced"}


def _is_unresolved_hook(item: dict) -> bool:
    """缓存项是否为未回收伏笔/承诺。

    识别三路：field/category 显式标记（foreshadowing/promise），或 id 符合
    StoryState foreshadowId 前缀（F0001）。resolved/abandoned 不算——它们已
    了结，Tier 2 本就该清。
    """
    if item.get("status") not in UNRESOLVED_HOOK_STATUSES:
        return False
    field = item.get("field", "")
    category = item.get("category", "")
    ident = str(item.get("id", "") or item.get("subject", "") or "")
    return (
        field in ("foreshadowing", "promise")
        or category in ("foreshadowing", "promise")
        or bool(re.fullmatch(r"F\d+", ident))
    )


@dataclass
class CompactionReport:
    tier1_removed: int = 0
    tier2_removed: int = 0
    tier3_merged: int = 0
    tier4_truncated: int = 0
    before_count: int = 0
    after_count: int = 0


def _item_key(item: dict) -> str:
    """Deterministic primary key for dedup grouping."""
    subject = item.get("subject", "")
    field = item.get("field", "")
    category = item.get("category", "")
    return f"{category}|{subject}|{field}"


def compact(
    items: list[dict],
    current_chapter: int,
    threshold: int = COMPACTION_THRESHOLD,
) -> tuple[list[dict], CompactionReport]:
    report = CompactionReport(before_count=len(items))

    if len(items) <= threshold:
        report.after_count = len(items)
        return items, report

    # Tier 1: same key outdated → keep latest (by source_chapter or updated_at)
    by_key: dict[str, list[dict]] = {}
    for item in items:
        key = _item_key(item)
        by_key.setdefault(key, []).append(item)

    tier1_result: list[dict] = []
    for key, group in by_key.items():
        outdated = [i for i in group if i.get("status") == "outdated"]
        active = [i for i in group if i.get("status") != "outdated"]
        # Keep all active items + latest outdated
        if outdated and len(outdated) > 1:
            latest = max(outdated, key=lambda i: i.get("source_chapter", 0))
            tier1_result.extend(active)
            tier1_result.append(latest)
            report.tier1_removed += len(outdated) - 1
        else:
            tier1_result.extend(active + outdated)

    # Tier 2: remove resolved/closed foreshadowing and promises
    before_t2 = len(tier1_result)
    tier2_result = [
        i for i in tier1_result
        if i.get("status") not in ("resolved", "closed", "paid_off", "abandoned")
    ]
    report.tier2_removed = before_t2 - len(tier2_result)

    # Tier 3: timeline items > OLD_CHAPTER_THRESHOLD → merge
    timeline_items = [i for i in tier2_result if i.get("category") == "timeline"]
    old_timeline = [
        t for t in timeline_items
        if t.get("source_chapter", 0) < current_chapter - OLD_CHAPTER_THRESHOLD
    ]
    if old_timeline:
        merged_count = len(old_timeline)
        values = [t.get("value", "").strip() for t in old_timeline if t.get("value")]
        merged_value = " · ".join(values)
        summary_item = {
            "category": "story_fact",
            "subject": "timeline_merged",
            "field": f"chapters_{old_timeline[0].get('source_chapter','?')}_to_{old_timeline[-1].get('source_chapter','?')}",
            "value": merged_value,
            "payload": {
                "from_chapter": old_timeline[0].get("source_chapter"),
                "to_chapter": old_timeline[-1].get("source_chapter"),
                "merged_count": merged_count,
                "entry_summaries": values,
            },
            "status": "active",
            "source_chapter": current_chapter,
            "evidence": "compactor:timeline",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tier3_result = [
            i for i in tier2_result if i not in old_timeline
        ] + [summary_item]
        report.tier3_merged = merged_count
    else:
        tier3_result = tier2_result

    # Tier 4: still over threshold → keep unresolved hooks first (不可丢),
    # then active, then by recency. 未回收伏笔超预算时报错，不静默截断。
    if len(tier3_result) > threshold:
        protected = [i for i in tier3_result if _is_unresolved_hook(i)]
        rest = [i for i in tier3_result if not _is_unresolved_hook(i)]
        active = [i for i in rest if i.get("status") == "active"]
        other = [i for i in rest if i.get("status") != "active"]
        active.sort(key=lambda i: i.get("source_chapter", 0), reverse=True)
        other.sort(key=lambda i: i.get("source_chapter", 0), reverse=True)
        combined = protected + active + other
        if len(protected) > threshold:
            raise ValueError(
                f"未回收伏笔/承诺 {len(protected)} 条超出压缩预算 {threshold}，"
                "拒绝截断——请提高 threshold 或先处理（resolve/abandon）部分伏笔"
            )
        final = combined[:threshold]
        report.tier4_truncated = len(tier3_result) - threshold
        report.after_count = len(final)
        return final, report

    report.after_count = len(tier3_result)
    return tier3_result, report


def main():

    parser = FailClosedArgumentParser(
        description="Compact project memory/state cache to prevent unbounded growth"
    )
    parser.add_argument("--input", required=True,
                        help="Path to memory scratchpad or cache JSON")
    parser.add_argument("--output", required=True,
                        help="Path to write compacted output")
    parser.add_argument("--current-chapter", type=int, required=True,
                        help="Current chapter number for recency threshold")
    parser.add_argument("--threshold", type=int, default=COMPACTION_THRESHOLD,
                        help=f"Item count threshold (default: {COMPACTION_THRESHOLD})")
    parser.add_argument("--json", action="store_true",
                        help="Output compaction report as JSON")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"WARNING: {args.input} not found — nothing to compact", file=sys.stderr)
        sys.exit(0)

    data = json.loads(input_path.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("items", data.get("facts", []))
    if not isinstance(items, list):
        print(f"ERROR: unexpected data format in {args.input}", file=sys.stderr)
        sys.exit(EXIT_FAIL_CLOSED)

    try:
        compacted, report = compact(items, args.current_chapter, args.threshold)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAIL_CLOSED)

    # Write compacted output
    if isinstance(data, list):
        output = compacted
    else:
        output = dict(data)
        output["items"] = compacted
        output["compacted_at"] = datetime.now(timezone.utc).isoformat()
        output["compaction_report"] = {
            "before": report.before_count,
            "after": report.after_count,
            "tier1_outdated_dedup": report.tier1_removed,
            "tier2_resolved_cleanup": report.tier2_removed,
            "tier3_timeline_merged": report.tier3_merged,
            "tier4_recency_cutoff": report.tier4_truncated,
        }

    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json:
        json.dump({
            "before": report.before_count,
            "after": report.after_count,
            "removed": report.tier1_removed + report.tier2_removed,
            "merged": report.tier3_merged,
            "truncated": report.tier4_truncated,
        }, sys.stdout, ensure_ascii=False, indent=2)
    else:
        print(f"Compacted {report.before_count} → {report.after_count} items")
        print(f"  T1 outdated dedup: -{report.tier1_removed}")
        print(f"  T2 resolved cleanup: -{report.tier2_removed}")
        print(f"  T3 timeline merged: {report.tier3_merged} → 1")
        print(f"  T4 recency cutoff: -{report.tier4_truncated}")
        print(f"  Written: {args.output}")

    sys.exit(0)


if __name__ == "__main__":
    main()
