#!/usr/bin/env python3
"""将 TencentDB Memory 召回结果格式化为可注入 LLM 的上下文块。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_MEMORY_HEADER = "## TencentDB Agent Memory（长期记忆召回）\n\n"


def format_memory_block(recall_text: str) -> str:
    if not recall_text or not recall_text.strip():
        return ""
    return _MEMORY_HEADER + recall_text.strip() + "\n"


def prepend_memory_to_parts(parts: list[str], recall_text: str) -> None:
    block = format_memory_block(recall_text)
    if block:
        parts.insert(0, block)


def write_memory_recall_file(workspace: Path, recall_text: str) -> Optional[Path]:
    if not recall_text or not recall_text.strip():
        return None
    path = workspace / "memory_recall.md"
    path.write_text(format_memory_block(recall_text), encoding="utf-8")
    return path


def load_memory_recall_from_workspace(workspace: Path) -> str:
    path = workspace / "memory_recall.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
