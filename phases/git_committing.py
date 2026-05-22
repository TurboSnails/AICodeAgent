#!/usr/bin/env python3
"""
Git Committing 阶段处理器 — V4 重构
职责：
1. 调用 GitService.commit_from_consensus 执行安全 commit
2. 调用 GitService.push 推送分支
3. 流转到 CREATING_PR
"""

from __future__ import annotations

from pathlib import Path

from engine.exceptions import AgentRecoverableError
from utils.logging_config import get_logger
from engine.state_machine import State, Task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class GitCommittingHandler(PhaseHandler):
    """
    Git Committing 阶段：提交代码并推送分支。

    输入状态：GIT_COMMITTING
    输出状态：
      - CREATING_PR（推送成功）
      - FAILED（提交或推送失败）
    """

    def __init__(self, git_service=None):
        self._git = git_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        if self._git is None:
            raise AgentRecoverableError("GitService not available")

        # 1. 生成共识偏差审计报告
        deviation_report = self._git.generate_deviation_report(workspace, task)
        if deviation_report:
            logger.info("Deviation report generated for %s", task.task_id)

        # 2. 执行 commit（只提交共识中列出的文件）
        try:
            self._git.commit_from_consensus(
                task.task_id,
                workspace,
                base_branch=task.base_branch or "",
            )
            logger.info("Git commit done for %s", task.task_id)
        except Exception as e:
            logger.error("Git commit failed for %s: %s", task.task_id, e)
            raise AgentRecoverableError(f"git commit failed: {e}")

        # 3. 推送分支
        try:
            self._git.push(task.branch)
            logger.info("Git push done for branch %s", task.branch)
        except Exception as e:
            logger.error("Git push failed for %s: %s", task.task_id, e)
            raise AgentRecoverableError(f"git push failed: {e}")

        return PhaseResult(
            State.CREATING_PR,
            "git committed and pushed",
            {"deviation_report": deviation_report},
        )
