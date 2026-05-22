"""
phases/_fix_plan.py 单元测试
覆盖：FixPlan 序列化/反序列化、优先级排序、文本解析、合并
"""
import json
from pathlib import Path

import pytest

from phases._fix_plan import (
    FixItem,
    FixPlan,
    FixPriority,
    merge_review_fix_plans,
    parse_fix_plan_from_text,
)


class TestFixPriority:
    def test_ordering(self):
        assert FixPriority.CRITICAL > FixPriority.HIGH
        assert FixPriority.HIGH > FixPriority.MEDIUM
        assert FixPriority.MEDIUM > FixPriority.LOW

    def test_from_string(self):
        assert FixPriority.from_string("CRITICAL") == FixPriority.CRITICAL
        assert FixPriority.from_string("high") == FixPriority.HIGH
        assert FixPriority.from_string("Medium") == FixPriority.MEDIUM
        assert FixPriority.from_string("unknown") == FixPriority.MEDIUM


class TestFixPlanSerialization:
    def test_to_json_and_back(self, tmp_path: Path):
        plan = FixPlan(
            items=[
                FixItem(
                    priority=FixPriority.HIGH,
                    category="NPE",
                    description="Null pointer risk",
                    target_files=["A.kt"],
                    suggested_fix="Add null check",
                ),
                FixItem(
                    priority=FixPriority.LOW,
                    category="Style",
                    description="Formatting",
                    target_files=["B.kt"],
                    suggested_fix="Reformat",
                ),
            ]
        )
        jpath = tmp_path / "plan.json"
        plan.to_json(jpath)
        loaded = FixPlan.from_json(jpath)
        assert len(loaded.items) == 2
        assert loaded.items[0].priority == FixPriority.HIGH
        assert loaded.items[0].target_files == ["A.kt"]

    def test_sorted_by_priority(self):
        plan = FixPlan(
            items=[
                FixItem(FixPriority.LOW, "A", "low", [], ""),
                FixItem(FixPriority.CRITICAL, "B", "critical", [], ""),
                FixItem(FixPriority.HIGH, "C", "high", [], ""),
            ]
        )
        sorted_items = plan.sorted_by_priority()
        assert [i.priority for i in sorted_items] == [
            FixPriority.CRITICAL,
            FixPriority.HIGH,
            FixPriority.LOW,
        ]

    def test_filter_by_priority(self):
        plan = FixPlan(
            items=[
                FixItem(FixPriority.CRITICAL, "A", "a", [], ""),
                FixItem(FixPriority.HIGH, "B", "b", [], ""),
                FixItem(FixPriority.LOW, "C", "c", [], ""),
            ]
        )
        high_and_above = plan.filter_by_priority(FixPriority.HIGH)
        assert len(high_and_above) == 2


class TestParseFixPlanFromText:
    def test_parse_json_block(self):
        text = """
Some review text.
```json
{
  "fix_plan": {
    "items": [
      {"priority": "HIGH", "category": "NPE", "description": "risk", "target_files": ["A.kt"], "suggested_fix": "check"}
    ]
  }
}
```
"""
        plan = parse_fix_plan_from_text(text)
        assert len(plan.items) == 1
        assert plan.items[0].priority == FixPriority.HIGH
        assert plan.items[0].target_files == ["A.kt"]

    def test_parse_markdown_list(self):
        text = """
## Fixes
1. **[HIGH] NPE** — risk — fix: check — files: A.kt, B.kt
2. **[CRITICAL] Race** — deadlock — fix: lock — files: C.kt
"""
        plan = parse_fix_plan_from_text(text)
        assert len(plan.items) == 2
        assert plan.items[0].priority == FixPriority.CRITICAL
        assert plan.items[1].priority == FixPriority.HIGH
        assert plan.items[1].target_files == ["A.kt", "B.kt"]

    def test_no_match_returns_empty(self):
        plan = parse_fix_plan_from_text("No fixes here")
        assert len(plan.items) == 0


class TestMergeReviewFixPlans:
    def test_merge_multiple_plans(self, tmp_path: Path):
        (tmp_path / "codex_fix_plan.json").write_text(
            json.dumps(
                {
                    "items": [
                        {"priority": "HIGH", "category": "A", "description": "a", "target_files": ["1.kt"], "suggested_fix": ""}
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (tmp_path / "red_team_fix_plan.json").write_text(
            json.dumps(
                {
                    "items": [
                        {"priority": "CRITICAL", "category": "B", "description": "b", "target_files": ["2.kt"], "suggested_fix": ""}
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        merged = merge_review_fix_plans(tmp_path)
        assert merged is not None
        assert len(merged.items) == 2
        assert merged.items[0].priority == FixPriority.CRITICAL

    def test_no_plans_returns_none(self, tmp_path: Path):
        assert merge_review_fix_plans(tmp_path) is None
