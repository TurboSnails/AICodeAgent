"""
CorrectingHandler 单元测试
覆盖：attempt 递增、max retries 超限、逃逸检测、正常流转回 CODING
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.exceptions import AgentFatalError
from engine.state_machine import State, Task
from phases.correcting import CorrectingHandler


class TestCorrectingAttemptCount:
    def test_increments_attempt_count(self, tmp_path: Path):
        handler = CorrectingHandler()
        task = Task(
            task_id="t1", raw_requirement="r", level="L0",
            site_hint="", source="test", chat_id="",
            attempt_count=0, max_retries=3,
        )
        with patch("phases.correcting.save_task"):
            handler.handle(task, tmp_path)
        assert task.attempt_count == 1

    def test_max_retries_exceeded_raises_fatal(self, tmp_path: Path):
        handler = CorrectingHandler()
        task = Task(
            task_id="t2", raw_requirement="r", level="L0",
            site_hint="", source="test", chat_id="",
            attempt_count=4, max_retries=3,
        )
        with patch("phases.correcting.save_task"):
            with pytest.raises(AgentFatalError, match="max retries exceeded"):
                handler.handle(task, tmp_path)


class TestCorrectingEscapeDetection:
    def test_repeated_error_triggers_escape(self, tmp_path: Path):
        handler = CorrectingHandler()
        task = Task(
            task_id="t3", raw_requirement="r", level="L0",
            site_hint="", source="test", chat_id="",
            attempt_count=1, max_retries=3,
        )
        # detect_unsolvable 需要至少 3 条历史（threshold=2，需要 N+1=3）
        same_error = "Unresolved reference: Foo\n" * 10
        (tmp_path / "build.log").write_text(same_error, encoding="utf-8")
        (tmp_path / "codex_review.md").write_text(same_error, encoding="utf-8")
        (tmp_path / "requirement_review.md").write_text(same_error, encoding="utf-8")

        with patch("phases.correcting.save_task"):
            with pytest.raises(AgentFatalError, match="unsolvable"):
                handler.handle(task, tmp_path)

    def test_different_errors_continue_to_coding(self, tmp_path: Path):
        handler = CorrectingHandler()
        task = Task(
            task_id="t4", raw_requirement="r", level="L0",
            site_hint="", source="test", chat_id="",
            attempt_count=1, max_retries=3,
        )
        (tmp_path / "build.log").write_text("Error A", encoding="utf-8")
        (tmp_path / "codex_review.md").write_text("Error B", encoding="utf-8")

        with patch("phases.correcting.save_task"):
            result = handler.handle(task, tmp_path)

        assert result.next_state == State.CODING


class TestCorrectingStateTransition:
    def test_returns_coding_state(self, tmp_path: Path):
        handler = CorrectingHandler()
        task = Task(
            task_id="t5", raw_requirement="r", level="L0",
            site_hint="", source="test", chat_id="",
            attempt_count=0, max_retries=3,
        )
        with patch("phases.correcting.save_task"):
            result = handler.handle(task, tmp_path)
        assert result.next_state == State.CODING
        assert result.artifacts["attempt_count"] == 1
