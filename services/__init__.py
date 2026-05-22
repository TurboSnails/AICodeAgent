#!/usr/bin/env python3
"""
services 包 — V4 重构基础设施服务层

提供：
  - AIClient: 统一 LLM 调用（claude --print / codex CLI）
  - BuildService: Gradle 构建封装
  - GitService: Git 操作封装
  - NotificationService: 多通道通知抽象
  - TaskService: 任务生命周期管理

用法：
    from services import AIClient, BuildService, GitService, NotificationService, TaskService
"""

from __future__ import annotations

from services.ai_client import AIClient
from services.build_service import BuildService
from services.git_service import GitService
from services.notification_service import NotificationService
from services.task_service import TaskService

__all__ = [
    "AIClient",
    "BuildService",
    "GitService",
    "NotificationService",
    "TaskService",
]
