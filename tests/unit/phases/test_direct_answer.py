#!/usr/bin/env python3
"""
DirectAnswerHandler 单元测试
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engine.state_machine import State, Task
from phases.direct_answer import DirectAnswerHandler


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "ws"


@pytest.fixture
def explain_task():
    return Task(
        task_id="test-123",
        raw_requirement="How does SiteRules work?",
        level="L1",
        site_hint="",
        source="test",
        chat_id="",
        request_type="explain",
    )


class TestDirectAnswerHandler:
    def test_handle_success(self, workspace, explain_task):
        ai_client = MagicMock()
        ai_client.call.return_value = "SiteRules is a centralized rule engine..."
        notify = MagicMock()

        handler = DirectAnswerHandler(ai_client=ai_client, notification_service=notify)
        result = handler.handle(explain_task, workspace)

        assert result.next_state == State.COMPLETED
        assert "direct answer" in result.reason
        assert (workspace / "answer.md").exists()
        ai_client.call.assert_called_once()
        notify.notify_task_completed.assert_called_once()

    def test_handle_ai_none(self, workspace, explain_task):
        handler = DirectAnswerHandler(ai_client=None)
        result = handler.handle(explain_task, workspace)

        assert result.next_state == State.FAILED
        assert "AI client not available" in result.reason

    def test_handle_empty_answer(self, workspace, explain_task):
        ai_client = MagicMock()
        ai_client.call.return_value = "   "

        handler = DirectAnswerHandler(ai_client=ai_client)
        result = handler.handle(explain_task, workspace)

        assert result.next_state == State.FAILED
        assert "empty" in result.reason

    def test_handle_ai_exception(self, workspace, explain_task):
        ai_client = MagicMock()
        ai_client.call.side_effect = RuntimeError("API timeout")

        handler = DirectAnswerHandler(ai_client=ai_client)
        result = handler.handle(explain_task, workspace)

        assert result.next_state == State.FAILED
        assert "API timeout" in result.reason
