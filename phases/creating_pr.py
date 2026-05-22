#!/usr/bin/env python3
"""
Creating PR 阶段处理器 — V4 重构
职责：
1. 调用 GitService.create_pr 创建 GitHub PR
2. 保存 pr_url 到 task
3. 流转到 NOTIFYING
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.logging_config import get_logger
from engine.state_machine import State, Task, save_task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class CreatingPRHandler(PhaseHandler):
    """
    Creating PR 阶段：创建 GitHub Pull Request。

    输入状态：CREATING_PR
    输出状态：
      - NOTIFYING（PR 创建成功或跳过）
      - FAILED（PR 创建失败且不可恢复）
    """

    def __init__(self, git_service=None):
        self._git = git_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        if self._git is None:
            raise AgentRecoverableError("GitService not available")

        # 从上一阶段获取偏差报告
        deviation_report = kwargs.get("deviation_report", "")

        # 创建 PR
        pr_url = self._git.create_pr(
            task_id=task.task_id,
            requirement=task.raw_requirement,
            branch=task.branch,
            base_branch=task.base_branch,
            deviation_report=deviation_report,
        )

        task.pr_url = pr_url
        save_task(task)

        if pr_url:
            logger.info("PR created for %s: %s", task.task_id, pr_url)
            return PhaseResult(
                State.NOTIFYING,
                f"pr created: {pr_url}",
                {"pr_url": pr_url},
            )

        logger.warning("PR creation skipped for %s (gh CLI may be unavailable)", task.task_id)
        return PhaseResult(
            State.NOTIFYING,
            "pr skipped",
            {"pr_url": ""},
        )
