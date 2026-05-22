#!/usr/bin/env python3
"""
Correcting 阶段处理器 — V4 重构
职责：
1. 读取 fix_prompt.md（由 building/codex_review/requirement_review 生成）
2. 增加 attempt_count
3. 流转回 CODING 进行修正
4. 检测不可解循环（复用 utils/escape_detector）

注意：此 handler 本身不调用 AI，它只是状态转换 + 逃逸检测。
      实际的修正编码由 CodingHandler 完成。
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentFatalError
from utils.config_loader import cfg_int
from utils.escape_detector import detect_unsolvable, record_escape
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class CorrectingHandler(PhaseHandler):
    """
    Correcting 阶段：修正前的逃逸检测与状态准备。

    输入状态：CORRECTING
    输出状态：
      - CODING（正常修正）
      - FAILED（不可解检测触发 或 超出最大重试次数）
    """

    def __init__(self):
        pass

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        task.attempt_count += 1
        save_task(task)

        max_retries = cfg_int("retries.coding", 3) if task.max_retries is None else task.max_retries

        # 1. 超出最大重试次数
        if task.attempt_count > max_retries:
            logger.error(
                "Max retries exceeded for %s (%d/%d)",
                task.task_id,
                task.attempt_count,
                max_retries,
            )
            raise AgentFatalError(f"max retries exceeded ({max_retries})")

        # 2. 不可解检测（复用 escape_detector）
        error_history = self._collect_error_history(workspace)
        if len(error_history) >= 2:
            is_unsolvable, reason = detect_unsolvable(error_history)
            if is_unsolvable:
                logger.error("[ESCAPE] Unsolvable detected for %s: %s", task.task_id, reason)
                self._write_unrecoverable_error(workspace, task.raw_requirement, reason)
                record_escape(workspace, "UNSOLVABLE_LOOP", reason)
                raise AgentFatalError(f"unsolvable error loop: {reason}")

        # 3. 流转回编码阶段进行修正
        logger.info(
            "Correcting -> Coding for %s (attempt %d/%d)",
            task.task_id,
            task.attempt_count,
            max_retries,
        )
        return PhaseResult(
            State.CODING,
            f"correcting attempt {task.attempt_count}",
            {"attempt_count": task.attempt_count},
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_error_history(workspace: Path) -> list[str]:
        """收集历史错误指纹（供 detect_unsolvable 使用）"""
        history = []
        for filename in ["build.log", "codex_review.md", "requirement_review.md", "red_team_audit.md"]:
            path = workspace / filename
            if path.exists():
                text = path.read_text(encoding="utf-8")
                fingerprint = text[:500].strip()
                if fingerprint:
                    history.append(fingerprint)
        return history

    @staticmethod
    def _write_unrecoverable_error(workspace: Path, requirement: str, errors: str) -> None:
        """写入不可恢复错误记录"""
        from datetime import datetime
        lines = [
            f"# Unrecoverable Error\n",
            f"## Time\n{datetime.now().isoformat()}\n",
            f"## Requirement\n{requirement}\n",
            f"## Errors\n{errors}\n",
        ]
        (workspace / "unrecoverable_error.md").write_text("".join(lines), encoding="utf-8")
