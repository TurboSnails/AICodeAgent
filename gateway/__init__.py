#!/usr/bin/env python3
"""
gateway 包 — V4 重构网关集合

提供：
  - WebUIHandler: HTTP API 网关
  - TelegramBot: Telegram Bot 网关

用法：
    from gateway import WebUIHandler, TelegramBot
    from services.task_service import TaskService

    task_service = TaskService()

    # Web UI
    handler = lambda *a, **kw: WebUIHandler(*a, task_service=task_service, **kw)

    # Telegram Bot
    bot = TelegramBot(task_service=task_service)
    bot.poll_updates()
"""

from __future__ import annotations

from gateway.telegram_bot import TelegramBot, restore_pending_gates, start_agent_task
from gateway.web_ui import WebUIHandler

__all__ = [
    "WebUIHandler",
    "TelegramBot",
    "start_agent_task",
    "restore_pending_gates",
]
