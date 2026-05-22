#!/usr/bin/env python3
"""
Telegram Bot 网关 — V4 重构适配器
向后兼容 gateway/telegram_bot_v2.py 的命令接口，内部委托给 TaskService。

支持命令：
  /start <需求> [level] [site]  — 提交任务
  /continue <task_id>           — L2 核准
  /reply <task_id> <回答>       — 需求澄清
  /cancel <task_id>             — 取消任务
  /status                       — 查看任务列表
  /help                         — 帮助信息
"""

from __future__ import annotations

import json
import time
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from utils.config_loader import cfg_str
from utils.logging_config import get_logger
from services.notification_service import NotificationService, TelegramChannel
from services.task_service import TaskService

logger = get_logger(__name__)


class TelegramBot:
    """V4 Telegram Bot 处理器 — 命令解析层。
    消息发送复用 services/notification_service 的 TelegramChannel，
    避免与 NotificationService 重复维护 HTTP 逻辑。
    """

    def __init__(
        self,
        task_service: TaskService | None = None,
        notification_service: NotificationService | None = None,
    ):
        self._task_service = task_service or TaskService()
        self._offset: int = 0

        # 复用 NotificationService 里的 TelegramChannel；如果没有则新建
        ns = notification_service or NotificationService()
        self._channel: TelegramChannel | None = None
        for ch in ns._channels:
            if isinstance(ch, TelegramChannel):
                self._channel = ch
                break
        if self._channel is None:
            self._channel = TelegramChannel()

        # Bot 轮询仍需要独立的 token 配置（TelegramChannel 已包含）
        self._bot_token = cfg_str("notifications.telegram.bot_token", "")
        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    def send_message(self, text: str, chat_id: str = "", parse_mode: str = "HTML") -> bool:
        """发送消息到指定 chat（委托给 TelegramChannel，避免重复 HTTP 代码）"""
        if self._channel is None or not self._channel.bot_token:
            logger.warning("Telegram channel not configured, skipping: %s", text[:100])
            return False
        return self._channel.send(text, parse_mode=parse_mode, chat_id=chat_id)

    def start_task(self, requirement: str, level: str = "auto", site_hint: str = "", chat_id: str = "") -> Optional[str]:
        """提交新任务并返回 task_id"""
        task = self._task_service.submit_task(
            requirement=requirement,
            level=level,
            site_hint=site_hint,
            source="telegram",
            chat_id=chat_id,
        )
        self.send_message(
            f"<b>任务已提交</b>\nID: <code>{task.task_id}</code>\n等级: {task.level}\n需求: {task.raw_requirement[:100]}",
            chat_id=chat_id,
        )
        return task.task_id

    def approve_gate(self, task_id: str, chat_id: str = "") -> bool:
        """L2 核准"""
        ok = self._task_service.approve_gate(task_id)
        msg = (
            f"<b>L2 核准{'成功' if ok else '失败'}</b>\nID: <code>{task_id}</code>"
        )
        self.send_message(msg, chat_id=chat_id)
        return ok

    def submit_clarification(self, task_id: str, reply: str, chat_id: str = "") -> bool:
        """提交澄清回复"""
        ok = self._task_service.submit_clarification(task_id, reply)
        msg = (
            f"<b>澄清回复已提交{'成功' if ok else '失败'}</b>\nID: <code>{task_id}</code>"
        )
        self.send_message(msg, chat_id=chat_id)
        return ok

    def cancel_task(self, task_id: str, chat_id: str = "") -> bool:
        """取消任务"""
        ok = self._task_service.cancel(task_id, "user cancelled via telegram")
        msg = (
            f"<b>任务取消{'成功' if ok else '失败'}</b>\nID: <code>{task_id}</code>"
        )
        self.send_message(msg, chat_id=chat_id)
        return ok

    def send_status(self, chat_id: str = "") -> None:
        """发送任务状态列表"""
        tasks = self._task_service.list_tasks(limit=10)
        if not tasks:
            self.send_message("当前没有任务", chat_id=chat_id)
            return
        lines = ["<b>最近任务</b>"]
        for t in tasks:
            emoji = "🟢" if t.current_state == "completed" else "🟡" if t.current_state == "pending" else "🔴"
            lines.append(f"{emoji} <code>{t.task_id}</code> | {t.level} | {t.current_state}")
        self.send_message("\n".join(lines), chat_id=chat_id)

    def send_help(self, chat_id: str = "") -> None:
        """发送帮助信息"""
        msg = """<b>Android Headless Agent V4 Bot</b>

/start <需求> [等级] [站点] — 提交任务
/continue <任务ID> — L2 核准
/reply <任务ID> <回答> — 需求澄清
/cancel <任务ID> — 取消任务
/status — 查看任务列表
/help — 显示此帮助
"""
        self.send_message(msg, chat_id=chat_id)

    # ------------------------------------------------------------------
    # 轮询处理
    # ------------------------------------------------------------------

    def poll_updates(self, interval: int = 2) -> None:
        """主轮询循环（通常在独立线程中运行）"""
        logger.info("Telegram bot polling started")
        while True:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
            except Exception as e:
                logger.error("Poll error: %s", e)
            time.sleep(interval)

    def _get_updates(self) -> list[dict]:
        if not self._bot_token:
            return []
        try:
            req = Request(
                f"{self._api_base}/getUpdates?offset={self._offset + 1}&limit=10",
                method="GET",
                headers={"Accept": "application/json"},
            )
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    return data.get("result", [])
        except Exception as e:
            logger.warning("getUpdates error: %s", e)
        return []

    def _handle_update(self, update: dict) -> None:
        update_id = update.get("update_id", 0)
        self._offset = max(self._offset, update_id)

        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        text = (message.get("text") or "").strip()
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if not text:
            return

        logger.info("Telegram msg from %s: %s", chat_id, text[:60])

        # /help
        if text.startswith("/help"):
            self.send_help(chat_id=chat_id)
            return

        # /status
        if text.startswith("/status"):
            self.send_status(chat_id=chat_id)
            return

        # /start <requirement> [level] [site]
        if text.startswith("/start"):
            args = text[len("/start"):].strip().split(None, 2)
            if not args:
                self.send_message("用法: /start <需求> [等级] [站点]", chat_id=chat_id)
                return
            requirement = args[0]
            level = args[1] if len(args) > 1 else "auto"
            site_hint = args[2] if len(args) > 2 else ""
            self.start_task(requirement, level=level, site_hint=site_hint, chat_id=chat_id)
            return

        # /continue <task_id>
        if text.startswith("/continue"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self.send_message("用法: /continue <任务ID>", chat_id=chat_id)
                return
            self.approve_gate(parts[1].strip(), chat_id=chat_id)
            return

        # /reply <task_id> <reply>
        if text.startswith("/reply"):
            parts = text.split(None, 2)
            if len(parts) < 3:
                self.send_message("用法: /reply <任务ID> <回答>", chat_id=chat_id)
                return
            self.submit_clarification(parts[1].strip(), parts[2].strip(), chat_id=chat_id)
            return

        # /cancel <task_id>
        if text.startswith("/cancel"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self.send_message("用法: /cancel <任务ID>", chat_id=chat_id)
                return
            self.cancel_task(parts[1].strip(), chat_id=chat_id)
            return

        # 未知命令
        self.send_message("未知命令，发送 /help 查看帮助", chat_id=chat_id)


# 兼容旧版入口
def start_agent_task(requirement: str, level: str = "auto", site_hint: str = "", chat_id: str = "") -> str:
    """向后兼容的入口函数"""
    bot = TelegramBot()
    return bot.start_task(requirement, level=level, site_hint=site_hint, chat_id=chat_id) or ""


def restore_pending_gates() -> None:
    """向后兼容：Bot 启动时恢复 pending gate 通知"""
    bot = TelegramBot()
    gates = bot._task_service.get_waiting_gates()
    for task in gates:
        bot.send_message(
            f"<b>L2 任务等待人工核准</b>\nID: <code>{task.task_id}</code>\n需求: {task.raw_requirement[:120]}",
            chat_id=task.chat_id,
        )


if __name__ == "__main__":
    bot = TelegramBot()
    # 复用 NotificationService 的上线通知
    ns = NotificationService()
    ns.notify_bot_online()
    bot.poll_updates()
