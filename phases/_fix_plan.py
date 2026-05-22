#!/usr/bin/env python3
"""
FixPlan 数据模型与解析工具 — V4 优化新增
提供 Review 阶段的结构化修复计划定义和解析。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class FixPriority(str, Enum):
    """修复优先级，按严重度降序排列。"""

    CRITICAL = "critical"  # 编译/构建失败、崩溃、安全漏洞
    HIGH = "high"  # 逻辑错误、回归风险、NPE
    MEDIUM = "medium"  # 性能问题、可维护性
    LOW = "low"  # 风格/nit

    @classmethod
    def order(cls, p: "FixPriority | str") -> int:
        """返回排序权重，越小越优先。"""
        mapping = {
            cls.CRITICAL: 0,
            cls.HIGH: 1,
            cls.MEDIUM: 2,
            cls.LOW: 3,
        }
        return mapping.get(cls(p) if isinstance(p, str) else p, 99)


@dataclass
class FixItem:
    """单个修复项。"""

    priority: FixPriority
    category: str  # e.g. "build", "logic", "security", "performance"
    description: str
    target_files: list[str] = field(default_factory=list)
    suggested_fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": self.priority.value,
            "category": self.category,
            "description": self.description,
            "target_files": self.target_files,
            "suggested_fix": self.suggested_fix,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FixItem":
        return cls(
            priority=FixPriority(d.get("priority", "medium")),
            category=d.get("category", ""),
            description=d.get("description", ""),
            target_files=d.get("target_files", []),
            suggested_fix=d.get("suggested_fix", ""),
        )


@dataclass
class FixPlan:
    """结构化修复计划。"""

    items: list[FixItem] = field(default_factory=list)
    summary: str = ""

    @property
    def total_critical(self) -> int:
        return sum(1 for i in self.items if i.priority == FixPriority.CRITICAL)

    @property
    def total_high(self) -> int:
        return sum(1 for i in self.items if i.priority == FixPriority.HIGH)

    @property
    def total_medium(self) -> int:
        return sum(1 for i in self.items if i.priority == FixPriority.MEDIUM)

    @property
    def total_low(self) -> int:
        return sum(1 for i in self.items if i.priority == FixPriority.LOW)

    @property
    def highest_priority(self) -> FixPriority | None:
        if not self.items:
            return None
        return min(self.items, key=lambda x: FixPriority.order(x.priority)).priority

    def sorted_items(self) -> list[FixItem]:
        """按优先级排序的修复项列表。"""
        return sorted(self.items, key=lambda x: FixPriority.order(x.priority))

    def items_by_priority(self, priority: FixPriority) -> list[FixItem]:
        """获取指定优先级的所有修复项。"""
        return [i for i in self.items if i.priority == priority]

    def remaining_items(self, exclude_files: list[str] | None = None) -> list[FixItem]:
        """排除已处理文件后的剩余修复项。"""
        if not exclude_files:
            return list(self.items)
        return [i for i in self.items if not any(f in i.target_files for f in exclude_files)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items],
            "summary": self.summary,
            "total_critical": self.total_critical,
            "total_high": self.total_high,
            "total_medium": self.total_medium,
            "total_low": self.total_low,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FixPlan":
        return cls(
            items=[FixItem.from_dict(i) for i in d.get("items", [])],
            summary=d.get("summary", ""),
        )

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "FixPlan":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


def parse_fix_plan_from_text(text: str) -> FixPlan:
    """
    从非结构化的 Review 报告文本中，尝试提取结构化的 FixPlan。

    支持两种格式：
    1. JSON block（含 priority/category/description 等字段）
    2. Markdown list（含 [CRITICAL] / [HIGH] / [MEDIUM] / [LOW] 标记）
    """
    items: list[FixItem] = []

    # 尝试提取 JSON block
    json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict) and "items" in data:
                plan = FixPlan.from_dict(data)
                items.extend(plan.items)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        items.append(FixItem.from_dict(item))
        except (json.JSONDecodeError, ValueError):
            continue

    # 尝试从 Markdown 文本提取优先级标记
    if not items:
        # 匹配形如 "- [CRITICAL] ..." 或 "## Critical\n- ..." 的结构
        priority_patterns = [
            (FixPriority.CRITICAL, r"\[CRITICAL\]|\*\*Critical\*\*|##\s*Critical"),
            (FixPriority.HIGH, r"\[HIGH\]|\*\*High\*\*|##\s*High"),
            (FixPriority.MEDIUM, r"\[MEDIUM\]|\*\*Medium\*\*|##\s*Medium"),
            (FixPriority.LOW, r"\[LOW\]|\*\*Low\*\*|##\s*Low"),
        ]

        for priority, pattern in priority_patterns:
            # 查找该优先级下的所有列表项
            matches = re.findall(
                rf"(?:{pattern}).*?(?:\n- |\n\d+\. )([^\n]+(?:\n  [^\n]+)*)",
                text,
                re.IGNORECASE,
            )
            for m in matches:
                desc = m.strip().replace("\n", " ")
                # 尝试提取文件名
                files = re.findall(r"`([^`]+\.(?:kt|xml|gradle))`", desc)
                items.append(
                    FixItem(
                        priority=priority,
                        category="auto_extracted",
                        description=desc,
                        target_files=files,
                        suggested_fix="",
                    )
                )

    return FixPlan(items=items, summary="auto-parsed from review report")


def merge_fix_plans(*plans: FixPlan) -> FixPlan:
    """合并多个 FixPlan，去重并按优先级排序。"""
    seen = set()
    merged = []
    for plan in plans:
        for item in plan.items:
            key = (item.priority.value, item.description[:100])
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return FixPlan(items=merged, summary="merged from multiple reviews")
