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
    """修复优先级，按严重度降序排列。CRITICAL > HIGH > MEDIUM > LOW。"""

    CRITICAL = "critical"  # 编译/构建失败、崩溃、安全漏洞
    HIGH = "high"  # 逻辑错误、回归风险、NPE
    MEDIUM = "medium"  # 性能问题、可维护性
    LOW = "low"  # 风格/nit

    @classmethod
    def order(cls, p: "FixPriority | str") -> int:
        """返回排序权重，越小越优先（CRITICAL=0，LOW=3）。"""
        if isinstance(p, str) and not isinstance(p, cls):
            p = cls.from_string(p)
        mapping = {cls.CRITICAL: 0, cls.HIGH: 1, cls.MEDIUM: 2, cls.LOW: 3}
        return mapping.get(p, 99)

    @classmethod
    def from_string(cls, value: str) -> "FixPriority":
        """大小写不敏感的查找，未知值返回 MEDIUM。"""
        try:
            return cls(value.lower())
        except ValueError:
            return cls.MEDIUM

    # 显式定义比较运算符，避免 str 继承覆盖 total_ordering 生成的方法
    def __gt__(self, other: object) -> bool:
        if not isinstance(other, FixPriority):
            return NotImplemented
        return self.order(self) < self.order(other)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, FixPriority):
            return NotImplemented
        return self.order(self) > self.order(other)

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, FixPriority):
            return NotImplemented
        return self.order(self) <= self.order(other)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, FixPriority):
            return NotImplemented
        return self.order(self) >= self.order(other)


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
            priority=FixPriority.from_string(d.get("priority", "medium")),
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

    sorted_by_priority = sorted_items

    def items_by_priority(self, priority: FixPriority) -> list[FixItem]:
        """获取指定优先级的所有修复项。"""
        return [i for i in self.items if i.priority == priority]

    def filter_by_priority(self, min_priority: FixPriority) -> list[FixItem]:
        """返回优先级 >= min_priority 的所有项（按优先级降序排列）。"""
        threshold = FixPriority.order(min_priority)
        return sorted(
            [i for i in self.items if FixPriority.order(i.priority) <= threshold],
            key=lambda x: FixPriority.order(x.priority),
        )

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

    to_json = write

    @classmethod
    def read(cls, path: Path) -> "FixPlan":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    from_json = read


def parse_fix_plan_from_text(text: str) -> FixPlan:
    """
    从非结构化的 Review 报告文本中，尝试提取结构化的 FixPlan。

    支持格式：
    1. JSON block（直接含 items 或嵌套在 fix_plan 键下）
    2. 内联 Markdown：`1. **[HIGH] title** — desc`
    3. 章节式 Markdown：`## Critical\n- item`
    """
    items: list[FixItem] = []

    # 1. 尝试从 JSON block 提取（支持 {"items":[]} 和 {"fix_plan":{"items":[]}} 两种格式）
    json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict):
                # 支持嵌套格式：{"fix_plan": {"items": [...]}}
                if "fix_plan" in data and isinstance(data["fix_plan"], dict):
                    data = data["fix_plan"]
                if "items" in data:
                    plan = FixPlan.from_dict(data)
                    items.extend(plan.items)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        items.append(FixItem.from_dict(item))
        except (json.JSONDecodeError, ValueError):
            continue

    if items:
        items.sort(key=lambda x: FixPriority.order(x.priority))
        return FixPlan(items=items, summary="auto-parsed from review report")

    # 2. 内联 Markdown 格式：`1. **[HIGH] title**` 或 `- **[CRITICAL] title**`
    inline_matches = re.findall(
        r"(?:^\d+\.\s+|-\s+)\*\*\[(\w+)\][^*]*\*\*([^\n]*)",
        text,
        re.MULTILINE,
    )
    for priority_str, rest in inline_matches:
        priority = FixPriority.from_string(priority_str)
        desc = rest.strip(" —-")
        files = re.findall(r"`?(\w[\w./]*\.(?:kt|xml|gradle))`?", desc)
        items.append(FixItem(priority=priority, category="auto_extracted", description=desc, target_files=files, suggested_fix=""))

    if items:
        items.sort(key=lambda x: FixPriority.order(x.priority))
        return FixPlan(items=items, summary="auto-parsed from review report")

    # 3. 章节式 Markdown：`[CRITICAL] desc` 后面跟着下一个列表项
    priority_patterns = [
        (FixPriority.CRITICAL, r"\[CRITICAL\]|\*\*Critical\*\*|##\s*Critical"),
        (FixPriority.HIGH, r"\[HIGH\]|\*\*High\*\*|##\s*High"),
        (FixPriority.MEDIUM, r"\[MEDIUM\]|\*\*Medium\*\*|##\s*Medium"),
        (FixPriority.LOW, r"\[LOW\]|\*\*Low\*\*|##\s*Low"),
    ]
    for priority, pattern in priority_patterns:
        matches = re.findall(
            rf"(?:{pattern}).*?(?:\n- |\n\d+\. )([^\n]+(?:\n  [^\n]+)*)",
            text,
            re.IGNORECASE,
        )
        for m in matches:
            desc = m.strip().replace("\n", " ")
            files = re.findall(r"`([^`]+\.(?:kt|xml|gradle))`", desc)
            items.append(FixItem(priority=priority, category="auto_extracted", description=desc, target_files=files, suggested_fix=""))

    items.sort(key=lambda x: FixPriority.order(x.priority))
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
    merged.sort(key=lambda x: FixPriority.order(x.priority))
    return FixPlan(items=merged, summary="merged from multiple reviews")


def merge_review_fix_plans(workspace: Path) -> "FixPlan | None":
    """扫描 workspace 目录中所有 *_fix_plan.json 文件，合并成单一 FixPlan。无文件时返回 None。"""
    plan_files = sorted(workspace.glob("*_fix_plan.json"))
    if not plan_files:
        return None
    plans = [FixPlan.read(p) for p in plan_files]
    return merge_fix_plans(*plans)
