#!/usr/bin/env python3
"""
通知服务 — V4 重构
支持多通道通知抽象：Telegram / 未来可扩展钉钉/企业微信/Slack
- 所有通知统一格式转换
- 任务状态变更时自动通知
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from utils.config_loader import cfg_str
from utils.logging_config import get_logger
from engine.state_machine import State

logger = get_logger(__name__)


class NotificationChannel(ABC):
    """通知通道抽象基类"""

    @abstractmethod
    def send(self, message: str, **kwargs) -> bool:
        """发送消息，返回是否成功"""
        ...


class TelegramChannel(NotificationChannel):
    """Telegram Bot API 通知通道"""

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or cfg_str("notifications.telegram.bot_token", "")
        self.chat_id = chat_id or cfg_str("notifications.telegram.chat_id", "")
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    def send(self, message: str, parse_mode: str = "HTML", chat_id: Optional[str] = None) -> bool:
        cid = chat_id or self.chat_id
        if not cid or not self.bot_token:
            logger.warning("Telegram not configured, skipping: %s", message[:100])
            return False
        try:
            payload = json.dumps(
                {"chat_id": cid, "text": message, "parse_mode": parse_mode},
                ensure_ascii=False,
            ).encode("utf-8")
            req = Request(
                f"{self.api_base}/sendMessage",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    return True
                logger.warning("Telegram API error: %s", data)
        except URLError as e:
            logger.error("Telegram network error: %s", e)
        except Exception as e:
            logger.error("Telegram send error: %s", e)
        return False


class NotificationService:
    """
    通知服务：管理多个通道，统一消息格式。

    职责：
    1. 任务状态变更通知
    2. 需求澄清通知
    3. L2 核准通知
    4. PR 创建通知
    """

    def __init__(self):
        self._channels: list[NotificationChannel] = []
        telegram = TelegramChannel()
        if telegram.bot_token:
            self._channels.append(telegram)

    def notify_task_status(
        self,
        task,
        pr_url: str = "",
        extra_message: str = "",
    ) -> None:
        """任务状态变更时发送通知"""
        status_emoji = "✅" if task.current_state == State.COMPLETED.value else "❌"
        msg = (
            f"<b>{status_emoji} Android Agent</b>\n"
            f"任务ID: <code>{task.task_id}</code>\n"
            f"状态: {task.current_state}\n"
            f"需求: {task.raw_requirement[:80]}\n"
        )
        if pr_url:
            msg += f"PR: {pr_url}\n"
        if task.error_log and task.current_state == State.FAILED.value:
            msg += f"错误: <pre>{task.error_log[:400]}</pre>\n"
        if extra_message:
            msg += f"\n{extra_message}\n"
        self._broadcast(msg)

    def notify_clarification(self, task, questions: list[str]) -> None:
        """需求待澄清时通知用户"""
        qs = "\n".join(f"• {q}" for q in questions)
        msg = (
            f"<b>需求待澄清</b>\n"
            f"任务ID: <code>{task.task_id}</code>\n"
            f"需求: {task.raw_requirement[:100]}\n\n"
            f"{qs}\n\n"
            f"请回复: <code>/reply {task.task_id} 你的回答</code>\n"
            f"或 Web POST /api/reply"
        )
        self._broadcast(msg)

    def notify_l2_gate(self, task) -> None:
        """L2 任务等待人工核准"""
        msg = (
            f"<b>L2 任务等待人工核准</b>\n"
            f"任务ID: <code>{task.task_id}</code>\n"
            f"需求: {task.raw_requirement[:120]}\n"
            f"共识方案已生成于 workspace/{task.task_id}/consensus.md\n"
            f"请回复: <code>/continue {task.task_id}</code> 开始编码"
        )
        self._broadcast(msg)

    def notify_bot_online(self) -> None:
        """Bot 启动上线通知"""
        self._broadcast("Android Headless Agent V4 Bot 已上线\n发送 /help 查看命令")

    def _broadcast(self, message: str, **kwargs) -> None:
        for ch in self._channels:
            try:
                ch.send(message, **kwargs)
            except Exception as e:
                logger.error("Channel %s failed: %s", type(ch).__name__, e)
