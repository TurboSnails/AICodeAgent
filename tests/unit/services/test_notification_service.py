"""
NotificationService 单元测试
覆盖：消息格式化、多通道广播、错误不传播
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.state_machine import State, Task
from services.notification_service import NotificationService, TelegramChannel

class TestMessageFormatting:
    """消息格式化"""

    def test_task_status_completed(self):
        svc = NotificationService()
        task = Task(
            task_id="t1", raw_requirement="测试需求", level="L1",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.COMPLETED.value

        # 替换 channel 为 mock
        mock_ch = MagicMock()
        svc._channels = [mock_ch]
        svc.notify_task_status(task, pr_url="https://github.com/pr/1")

        mock_ch.send.assert_called_once()
        call_args = mock_ch.send.call_args[0][0]
        assert "✅" in call_args
        assert "t1" in call_args
        assert "https://github.com/pr/1" in call_args

    def test_task_status_failed(self):
        svc = NotificationService()
        task = Task(
            task_id="t2", raw_requirement="测试需求", level="L1",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.FAILED.value
        task.error_log = "Something went wrong"

        mock_ch = MagicMock()
        svc._channels = [mock_ch]
        svc.notify_task_status(task)

        call_args = mock_ch.send.call_args[0][0]
        assert "❌" in call_args
        assert "Something went wrong" in call_args

    def test_clarification_message(self):
        svc = NotificationService()
        task = Task(
            task_id="t3", raw_requirement="模糊需求", level="L1",
            site_hint="", source="test", chat_id="",
        )

        mock_ch = MagicMock()
        svc._channels = [mock_ch]
        svc.notify_clarification(task, ["问题1", "问题2"])

        call_args = mock_ch.send.call_args[0][0]
        assert "需求待澄清" in call_args
        assert "问题1" in call_args
        assert "/reply t3" in call_args

    def test_l2_gate_message(self):
        svc = NotificationService()
        task = Task(
            task_id="t4", raw_requirement="L2 需求", level="L2",
            site_hint="", source="test", chat_id="",
        )

        mock_ch = MagicMock()
        svc._channels = [mock_ch]
        svc.notify_l2_gate(task)

        call_args = mock_ch.send.call_args[0][0]
        assert "L2 任务等待人工核准" in call_args
        assert "/continue t4" in call_args

class TestBroadcastBehavior:
    """广播行为"""

    def test_channel_failure_not_propagated(self):
        svc = NotificationService()
        bad_ch = MagicMock()
        bad_ch.send.side_effect = Exception("Network error")
        good_ch = MagicMock()
        svc._channels = [bad_ch, good_ch]

        task = Task(
            task_id="t5", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.COMPLETED.value

        # 不应抛出异常
        svc.notify_task_status(task)
        good_ch.send.assert_called_once()

    def test_no_channels_does_not_crash(self):
        svc = NotificationService()
        svc._channels = []

        task = Task(
            task_id="t6", raw_requirement="test", level="L0",
            site_hint="", source="test", chat_id="",
        )
        task.current_state = State.COMPLETED.value

        # 不应抛出异常
        svc.notify_task_status(task)