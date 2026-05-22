#!/usr/bin/env python3
"""
Notifying 阶段处理器 — V4 重构
职责：
1. 调用 NotificationService 发送任务完成/失败通知
2. 流转到 COMPLETED
"""

from __future__ import annotations

from pathlib import Path

from utils.logging_config import get_logger
from engine.state_machine import State, Task
from phases.base import PhaseHandler, PhaseResult

logger = get_logger(__name__)


class NotifyingHandler(PhaseHandler):
    """
    Notifying 阶段：发送最终通知。

    输入状态：NOTIFYING
    输出状态：
      - COMPLETED（通知完成）
    """

    def __init__(self, notification_service=None):
        self._notify = notification_service

    def handle(self, task: Task, workspace: Path, **kwargs) -> PhaseResult:
        pr_url = task.pr_url or kwargs.get("pr_url", "")

        if self._notify:
            try:
                self._notify.notify_task_status(task, pr_url=pr_url)
                logger.info("Notification sent for %s", task.task_id)
            except Exception as e:
                logger.error("Notification failed for %s: %s", task.task_id, e)
                # 通知失败不应阻塞任务完成
        else:
            logger.info("No notification service configured for %s", task.task_id)

        logger.info("Task %s COMPLETED", task.task_id)
        return PhaseResult(
            State.COMPLETED,
            "all done",
            {"pr_url": pr_url},
        )
